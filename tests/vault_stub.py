"""A stand-in for the two HTTP surfaces `batch-processor` writes through: the gated MCP path and the
gateway's key API, both backed by an in-memory vault.

Not a test module. Shared by the broker-backed run tests and the crash-injection harness, because
both need the same thing — somewhere the processor's writes actually land, so an invariant can be
read off real state rather than off a recording of intentions.

**What this is and is not evidence for.** It is a real HTTP server on a real socket, so everything
between `McpClient` and the wire is exercised for real: the opener, the envelope, the retry loop,
the session header, the ordering of calls. It is *not* evidence about the deployed MCP surface —
the tool names, the argument keys and the refusal wording are this stub's choices, and the deployed
ones are apps#3875's. That is exactly why the tool names are required configuration with no
defaults: no stub can make a guess about them true.

The failure injection is by path (`refuse_paths`) *and* by count (`unavailable_writes`,
`fail_after_writes`), because the two failures a test needs to distinguish are distinguished by
position: a refusal on the *first* write leaves a chunk that changed nothing and may be
redelivered, and one on the *second* leaves a genuinely half-applied chunk, which is the state
ADR-0048 says must be parked rather than replayed.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

TOOL_READ = "stub_read_note"
TOOL_WRITE = "stub_write_note"
TOOL_DELETE = "stub_delete_note"


@dataclass
class FakeVault:
    """The stub's whole state. Every field is readable by a test as ground truth."""

    url: str = ""
    notes: dict[str, str] = field(default_factory=dict[str, str])
    handle_blocked: bool = False
    handle_metadata: dict[str, object] = field(default_factory=dict[str, object])

    calls: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    """`(tool, path)` for every MCP call that reached the stub, in order."""

    refuse_paths: set[str] = field(default_factory=set[str])
    """Paths whose *writes* the gate refuses — an HTTP 200 with the error in the envelope."""

    unavailable_writes: int = 0
    """Answer HTTP 503 — the retryable kind — to this many writes, decrementing."""

    fail_after_writes: int | None = None
    """Refuse every write after this many have succeeded, across the whole run."""

    writes: int = 0

    handle_state_during_writes: list[bool] = field(default_factory=list[bool])
    """Whether the agent handle was blocked at the moment of each write. Recorded here rather than
    asserted after the run, because "the handle was down while the batch wrote" is a statement about
    an interval, and the state after the run has deliberately been put back."""

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
            body = self._read_body()
            if self.path.startswith("/key/update"):
                sent = cast("dict[str, object]", json.loads(body))
                vault.handle_blocked = sent.get("blocked") is True
                vault.handle_metadata = cast("dict[str, object]", sent.get("metadata") or {})
                self._reply(200, self._key_info())
                return
            self._mcp(body)

        def do_GET(self) -> None:
            if self.path.startswith("/key/info"):
                self._reply(200, self._key_info())
                return
            self._reply(404, b"{}")

        def _key_info(self) -> bytes:
            info = {"blocked": vault.handle_blocked, "metadata": vault.handle_metadata}
            return json.dumps({"key": "stub", "info": info}).encode("utf-8")

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
                vault.handle_state_during_writes.append(vault.handle_blocked)
                self._reply(200, _mcp_result(""))
                return
            if tool == TOOL_DELETE:
                if path not in vault.notes:
                    self._reply(200, _mcp_result(f"File not found: {path}", is_error=True))
                    return
                del vault.notes[path]
                vault.writes += 1
                vault.handle_state_during_writes.append(vault.handle_blocked)
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
