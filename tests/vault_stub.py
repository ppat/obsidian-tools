"""A stand-in for the two HTTP surfaces `batch-processor` drives: the gated MCP path, and the slice
of the Kubernetes API that stops and starts the agent MCP instance — both backed by in-memory state.

Not a test module. Shared by the broker-backed run tests and the crash-injection harness, because
both need the same thing — somewhere the processor's writes actually land, so an invariant can be
read off real state rather than off a recording of intentions.

**What this is and is not evidence for.** It is a real HTTP server on a real socket, so everything
between `McpClient`/`AgentInstanceClient` and the wire is exercised for real: the opener, the
envelope, the retry loop, the session header, the merge-patch bodies, the ordering of calls. It is
*not* evidence about the deployed MCP surface — the tool names, the argument keys and the refusal
wording are this stub's choices, and the deployed ones are apps#3875's. That is exactly why the tool
names are required configuration with no defaults: no stub can make a guess about them true. It is
equally not evidence about the RBAC grant: this stub answers every request, and only a real API
server refusing one proves the grant is as narrow as ADR-0052 requires
(`docs/VERIFICATIONS.md` §5).

The failure injection is by path (`refuse_paths`) *and* by count (`unavailable_writes`,
`fail_after_writes`), because the two failures a test needs to distinguish are distinguished by
position: a refusal on the *first* write leaves a chunk that changed nothing and may be
redelivered, and one on the *second* leaves a genuinely half-applied chunk, which is the state
ADR-0048 says must be parked rather than replayed.
"""

from __future__ import annotations

import json
import tempfile
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

TOOL_READ = "stub_read_note"
TOOL_WRITE = "stub_write_note"
TOOL_DELETE = "stub_delete_note"

NAMESPACE = "stub-vault"
DEPLOYMENT = "stub-mcp-obsidian-agent"
LEASE = "stub-batch-mode"

SCALE_PATH = f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{DEPLOYMENT}/scale"
LEASE_PATH = f"/apis/coordination.k8s.io/v1/namespaces/{NAMESPACE}/leases/{LEASE}"

# A file, not a string, because the client re-reads the projected token on every request rather than
# caching it at startup — the bound-token rotation that would otherwise 401 a long run partway
# through, with the instance already stopped. Written once for the session; the rotation behaviour
# itself is proven directly in `test_batch_processor_http_seams.py`.
TOKEN_PATH = str(Path(tempfile.mkdtemp(prefix="obsidian-tools-tests-sa-")) / "token")
Path(TOKEN_PATH).write_text("stub-service-account-token\n", encoding="utf-8")


@dataclass
class FakeVault:
    """The stub's whole state. Every field is readable by a test as ground truth."""

    url: str = ""
    notes: dict[str, str] = field(default_factory=dict[str, str])

    agent_replicas: int = 1
    """The agent instance Deployment's *desired* count, as the `scale` subresource reports it."""

    lease: dict[str, object] = field(default_factory=dict[str, object])
    """The batch `Lease`'s spec. Ships empty, exactly as the manifest does."""

    kube_calls: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    """`(method, path)` for every Kubernetes request, in order — which is what makes the ordering
    claim ("lease before stop, start before release") checkable rather than asserted."""

    calls: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    """`(tool, path)` for every MCP call that reached the stub, in order."""

    refuse_paths: set[str] = field(default_factory=set[str])
    """Paths whose *writes* the gate refuses — an HTTP 200 with the error in the envelope."""

    unavailable_writes: int = 0
    """Answer HTTP 503 — the retryable kind — to this many writes, decrementing."""

    fail_after_writes: int | None = None
    """Refuse every write after this many have succeeded, across the whole run."""

    writes: int = 0

    agent_replicas_during_writes: list[int] = field(default_factory=list[int])
    """The agent instance's desired replica count at the moment of each write. Recorded here rather
    than asserted after the run, because "the instance was stopped while the batch wrote" is a
    statement about an interval, and the state after the run has deliberately been put back."""

    def written(self, path: str) -> str | None:
        return self.notes.get(path)


def _mcp_result(text: str, *, is_error: bool = False) -> bytes:
    body = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}], "isError": is_error}}
    return json.dumps(body).encode("utf-8")


@contextmanager
def running_vault(vault: FakeVault) -> Generator[FakeVault]:
    class Handler(BaseHTTPRequestHandler):
        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _reply(self, status: int, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:
            self._mcp(self._read_body())

        def do_PATCH(self) -> None:
            """Merge-patch semantics, the half of them this seam uses: a key set to `null` is
            removed, every other key is set. Implemented rather than faked, because the release path
            *is* a set of nulls and a stub that ignored them would let a lease leak undetected."""
            body = cast("dict[str, object]", json.loads(self._read_body()))
            spec = cast("dict[str, object]", body.get("spec") or {})
            vault.kube_calls.append(("PATCH", self.path))
            if self.path == SCALE_PATH:
                vault.agent_replicas = cast("int", spec["replicas"])
                self._reply(200, self._scale())
                return
            if self.path == LEASE_PATH:
                for key, value in spec.items():
                    if value is None:
                        vault.lease.pop(key, None)
                    else:
                        vault.lease[key] = value
                self._reply(200, self._lease())
                return
            self._reply(404, b"{}")

        def do_GET(self) -> None:
            if self.path == SCALE_PATH:
                vault.kube_calls.append(("GET", self.path))
                self._reply(200, self._scale())
                return
            if self.path == LEASE_PATH:
                vault.kube_calls.append(("GET", self.path))
                self._reply(200, self._lease())
                return
            self._reply(404, b"{}")

        def _scale(self) -> bytes:
            # `status.replicas` is deliberately answered as something other than the desired count:
            # nothing in this system may read availability as "a batch run is holding the door", and
            # a stub that reported them equal could never catch code that did.
            body = {
                "kind": "Scale",
                "metadata": {"name": DEPLOYMENT, "namespace": NAMESPACE},
                "spec": {"replicas": vault.agent_replicas},
                "status": {"replicas": 0},
            }
            return json.dumps(body).encode("utf-8")

        def _lease(self) -> bytes:
            body = {
                "kind": "Lease",
                "metadata": {"name": LEASE, "namespace": NAMESPACE},
                "spec": dict(vault.lease),
            }
            return json.dumps(body).encode("utf-8")

        def _mcp(self, body: bytes) -> None:
            request = cast("dict[str, object]", json.loads(body))
            method = request.get("method")
            if method != "tools/call":
                # `initialize` and the `notifications/initialized` notification both land here.
                self._reply(200, _mcp_result(""))
                return
            params = cast("dict[str, object]", request.get("params") or {})
            tool = cast("str", params.get("name"))
            arguments = cast("dict[str, str]", params.get("arguments") or {})
            path = arguments.get("filepath", "")
            vault.calls.append((tool, path))

            if tool == TOOL_READ:
                content = vault.notes.get(path)
                if content is None:
                    self._reply(200, _mcp_result(f"File not found: {path}", is_error=True))
                else:
                    self._reply(200, _mcp_result(content))
                return

            if vault.unavailable_writes > 0:
                vault.unavailable_writes -= 1
                self._reply(503, b"")
                return
            if path in vault.refuse_paths or (
                vault.fail_after_writes is not None and vault.writes >= vault.fail_after_writes
            ):
                self._reply(200, _mcp_result(f"path_forbidden: {path}", is_error=True))
                return

            if tool == TOOL_WRITE:
                vault.notes[path] = arguments.get("content", "")
                vault.writes += 1
                vault.agent_replicas_during_writes.append(vault.agent_replicas)
                self._reply(200, _mcp_result(""))
                return
            if tool == TOOL_DELETE:
                if path not in vault.notes:
                    self._reply(200, _mcp_result(f"File not found: {path}", is_error=True))
                    return
                del vault.notes[path]
                vault.writes += 1
                vault.agent_replicas_during_writes.append(vault.agent_replicas)
                self._reply(200, _mcp_result(""))
                return
            self._reply(200, b'{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"unknown tool"}}')

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    vault.url = f"http://127.0.0.1:{server.server_port}"
    # A short poll interval: `shutdown()` waits for the serve loop to notice, and the default half
    # second would be added to every test that uses this.
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        yield vault
    finally:
        server.shutdown()
        server.server_close()
