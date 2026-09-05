"""Tests for the two HTTP seams — `batch_processor/mcp_client.py` and `agent_handle.py` — against a
real HTTP server on a real socket, never a patched `urlopen`.

The stub answers with bytes chosen per test; what is *not* stubbed is anything this code does:
`urllib`'s opener, the redirect handler, the retry loop, the header plumbing. A monkeypatched
`urlopen` would agree with whatever these modules already believe about all four, which is the
failure mode this repository's testing discipline names.

The one thing no local stub can prove is the deployed vocabulary — the MCP tool names, and the
gateway's `blocked`/`metadata` field shapes. Those are deployment facts (apps#3875), which is why
the tool names are required configuration rather than defaults: a stub cannot make a guess true.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from obsidian_tools.batch_processor.agent_handle import (
    LEASE_FIELD,
    AgentHandleClient,
    AgentHandleError,
    run_watchdog_once,
)
from obsidian_tools.batch_processor.mcp_client import (
    McpClient,
    McpFailedError,
    McpRefusedError,
    McpToolNames,
    McpUnavailableError,
)
from obsidian_tools.batch_processor.watchdog import WatchdogVerdict

_KEY = "sk-secret-agent-handle-key"


@dataclass
class Recorded:
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class Stub:
    """A scripted HTTP peer: `replies` is consumed in order, the last one repeating forever."""

    url: str
    replies: list[tuple[int, bytes, dict[str, str]]] = field(default_factory=list[tuple[int, bytes, dict[str, str]]])
    requests: list[Recorded] = field(default_factory=list[Recorded])

    def next_reply(self) -> tuple[int, bytes, dict[str, str]]:
        if len(self.replies) > 1:
            return self.replies.pop(0)
        return self.replies[0]


@pytest.fixture
def stub() -> Iterator[Stub]:
    state = Stub(url="")

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            state.requests.append(Recorded(self.path, dict(self.headers.items()), body))
            status, payload, extra = state.next_reply()
            self.send_response(status)
            for name, value in extra.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:
            self._serve()

        def do_GET(self) -> None:
            self._serve()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}"
    # `shutdown()` only returns once the serve loop notices, so the default half-second poll would
    # add half a second of teardown to every test in this file.
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def ok(text: str) -> tuple[int, bytes, dict[str, str]]:
    body = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}], "isError": False}}
    return 200, json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"}


def refusal(text: str) -> tuple[int, bytes, dict[str, str]]:
    body = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}], "isError": True}}
    return 200, json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"}


def mcp_client(stub: Stub, *, retries: int = 3) -> McpClient:
    return McpClient(
        base_url=stub.url,
        api_key=_KEY,
        tools=McpToolNames(
            read="deployment_read_tool",
            write="deployment_write_tool",
            delete="deployment_delete_tool",
            path_argument="filepath",
            content_argument="content",
        ),
        timeout_seconds=5.0,
        verify_tls=False,
        retries=retries,
        retry_base_delay_seconds=0.0,
    )


# --- the gated MCP path ---------------------------------------------------------------------------


def test_a_refusal_arriving_as_http_200_raises_rather_than_returning_content(stub: Stub) -> None:
    """The gotcha, over a real socket. Red if the status code alone decided the outcome: the write
    would be reported as applied while the gate refused it, and the chunk would be acked."""
    stub.replies = [refusal("path_forbidden: 10-areas/x.md is outside the active scope")]

    with pytest.raises(McpRefusedError, match="path_forbidden"):
        mcp_client(stub).write_note("10-areas/x.md", "body\n")


def test_a_refusal_is_attempted_exactly_once(stub: Stub) -> None:
    """Red if refusals entered the retry loop: a permanent, static-configuration refusal would be
    retried on a doubling schedule forever, turning a loud refusal into a quiet stall."""
    stub.replies = [refusal("path_forbidden")]

    with pytest.raises(McpRefusedError):
        mcp_client(stub).write_note("10-areas/x.md", "body\n")

    assert len(stub.requests) == 1


def test_a_transient_failure_is_retried_and_then_succeeds(stub: Stub) -> None:
    """Red if a restarting MCP pod failed the chunk outright — every chunk in flight during an
    ordinary rollout would be dead-lettered instead of waited for."""
    stub.replies = [(503, b"", {}), ok("# a note\n")]

    assert mcp_client(stub).read_note("10-areas/x.md") == "# a note\n"
    assert len(stub.requests) == 2


def test_a_persistent_transient_failure_exhausts_the_retries_and_stops(stub: Stub) -> None:
    """Red if the retry loop were unbounded: the run would never reach the dead-letter path and the
    agent handle would stay down for as long as the MCP surface stayed unreachable."""
    stub.replies = [(503, b"", {})]

    with pytest.raises(McpUnavailableError):
        mcp_client(stub, retries=3).read_note("10-areas/x.md")

    assert len(stub.requests) == 3


def test_a_missing_note_reads_as_absent_rather_than_as_a_refusal(stub: Stub) -> None:
    """ADR-0048's create branch. Red if absence raised: every create in the bootstrap import would
    be dead-lettered for a target that was legitimately not there."""
    stub.replies = [refusal("File not found: 05-raw/new.md")]

    assert mcp_client(stub).read_note("05-raw/new.md") is None


def test_a_delete_of_an_already_absent_note_succeeds(stub: Stub) -> None:
    """Discipline 3: a retried delete that already landed must not fail a chunk that completed. Red
    if it raised — a redelivered chunk would be dead-lettered for having worked."""
    stub.replies = [refusal("File not found: 10-areas/x.md")]

    mcp_client(stub).delete_note("10-areas/x.md")


def test_a_json_rpc_error_is_permanent_and_names_itself(stub: Stub) -> None:
    """A tool name the deployment does not have arrives exactly this way. Red if it were retried,
    or if it were reported as a gate refusal — the two are fixed in different repositories."""
    stub.replies = [(200, b'{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"unknown tool"}}', {})]

    with pytest.raises(McpFailedError, match="unknown tool"):
        mcp_client(stub).read_note("10-areas/x.md")

    assert len(stub.requests) == 1


def test_the_call_carries_the_configured_tool_name_and_argument_keys(stub: Stub) -> None:
    """The tool vocabulary is deployment configuration, and this is what proves it is actually used
    rather than shadowed by a constant. Red if a hard-coded name crept back in — the deployment's
    own setting would silently have no effect."""
    stub.replies = [ok("")]

    mcp_client(stub).write_note("10-areas/x.md", "body\n")

    sent = json.loads(stub.requests[0].body)
    assert sent["method"] == "tools/call"
    assert sent["params"]["name"] == "deployment_write_tool"
    assert sent["params"]["arguments"] == {"filepath": "10-areas/x.md", "content": "body\n"}


def test_a_session_id_from_the_handshake_rides_on_every_later_request(stub: Stub) -> None:
    """Streamable HTTP's session header. Red if it were dropped: a session-using server would
    refuse every call after `initialize`, and the refusals would look like gate refusals."""
    stub.replies = [(*ok("")[:2], {"Mcp-Session-Id": "sess-123"}), ok("")]
    client = mcp_client(stub)

    client.connect()
    client.read_note("10-areas/x.md")

    assert stub.requests[-1].headers["Mcp-Session-Id"] == "sess-123"


def test_a_redirect_is_refused_rather_than_followed(stub: Stub) -> None:
    """The key in play carries this component's write scope. Red if a redirect were followed — one
    response could retarget the next request and send that bearer token to a host the configuration
    never named."""
    stub.replies = [(302, b"", {"Location": "http://127.0.0.1:1/evil"})]

    with pytest.raises(McpFailedError):
        mcp_client(stub).read_note("10-areas/x.md")

    assert len(stub.requests) == 1


def test_the_api_key_never_appears_in_a_failure_message(stub: Stub) -> None:
    """Error text is built from the URL and the envelope. Red if a header ever reached a message —
    the widest credential in the system would be in the logs of every failed run."""
    stub.replies = [(500, b"boom", {})]

    with pytest.raises(McpUnavailableError) as caught:
        mcp_client(stub).read_note("10-areas/x.md")

    assert _KEY not in str(caught.value)


# --- the gateway handle ---------------------------------------------------------------------------


def handle_client(stub: Stub) -> AgentHandleClient:
    return AgentHandleClient(
        base_url=stub.url, admin_key="sk-admin", handle_key=_KEY, timeout_seconds=5.0, verify_tls=False
    )


def key_info(*, blocked: bool, metadata: dict[str, object] | None = None) -> tuple[int, bytes, dict[str, str]]:
    body = {"key": _KEY, "info": {"blocked": blocked, "metadata": metadata or {}}}
    return 200, json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"}


def test_disabling_the_handle_and_stamping_the_lease_is_one_request(stub: Stub) -> None:
    """Two requests would leave a killable window in which the handle is down with no lease — a
    state the watchdog is required to read as an operator's own hold and leave alone, permanently.
    Red if they were ever split."""
    stub.replies = [key_info(blocked=False), key_info(blocked=True)]
    deadline = datetime(2026, 9, 5, 12, 5, tzinfo=UTC)

    handle_client(stub).begin_batch_run(deadline)

    update = next(r for r in stub.requests if r.path == "/key/update")
    sent = json.loads(update.body)
    assert sent["blocked"] is True
    assert sent["metadata"][LEASE_FIELD] == deadline.isoformat()


def test_existing_metadata_survives_a_lease_write(stub: Stub) -> None:
    """The gateway's key update replaces metadata wholesale. Red if the write were not
    read-modify-write: every other annotation on the agent handle would be deleted by the first
    batch run, silently."""
    stub.replies = [key_info(blocked=False, metadata={"owner": "platform", "note": "keep me"}), key_info(blocked=True)]

    handle_client(stub).begin_batch_run(datetime(2026, 9, 5, 12, 5, tzinfo=UTC))

    sent = json.loads(next(r for r in stub.requests if r.path == "/key/update").body)
    assert sent["metadata"]["owner"] == "platform"
    assert sent["metadata"]["note"] == "keep me"


def test_renewing_the_lease_changes_nothing_but_the_lease(stub: Stub) -> None:
    """A renewal says one thing: this processor is still alive. Red if it also re-asserted
    `blocked` — a renewal that disables would mask a run that never took the handle down (the
    handle would end up down anyway, and no test could tell the two apart) and would silently
    override whoever changed the handle mid-run."""
    stub.replies = [key_info(blocked=False), key_info(blocked=False)]

    handle_client(stub).renew_lease(datetime(2026, 9, 5, 12, 5, tzinfo=UTC))

    sent = json.loads(next(r for r in stub.requests if r.path == "/key/update").body)
    assert sent["blocked"] is False
    assert sent["metadata"][LEASE_FIELD] == "2026-09-05T12:05:00+00:00"


def test_ending_a_run_re_enables_the_handle_and_drops_the_lease(stub: Stub) -> None:
    """Red if the lease outlived the run: an enabled handle carrying a deadline is a fact about a
    run that has already finished, and the next watchdog pass would be reasoning from debris."""
    live = key_info(blocked=True, metadata={LEASE_FIELD: "2026-09-05T12:05:00+00:00"})
    stub.replies = [live, key_info(blocked=False)]

    handle_client(stub).end_batch_run()

    sent = json.loads(next(r for r in stub.requests if r.path == "/key/update").body)
    assert sent["blocked"] is False
    assert LEASE_FIELD not in sent["metadata"]


def test_the_watchdog_re_enables_a_handle_whose_lease_has_expired(stub: Stub) -> None:
    """The end-to-end injection for unit D4: a processor that died mid-run leaves exactly this, and
    the pass turns agent writes back on. Red if no update were issued — every agent write in the
    system stays stopped."""
    stub.replies = [
        key_info(blocked=True, metadata={LEASE_FIELD: "2026-09-05T11:00:00+00:00"}),
        key_info(blocked=True, metadata={LEASE_FIELD: "2026-09-05T11:00:00+00:00"}),
        key_info(blocked=False),
    ]

    verdict = run_watchdog_once(handle_client(stub), datetime(2026, 9, 5, 12, 0, tzinfo=UTC))

    assert verdict is WatchdogVerdict.RE_ENABLE
    assert json.loads(next(r for r in stub.requests if r.path == "/key/update").body)["blocked"] is False


def test_the_watchdog_writes_nothing_while_a_lease_is_live(stub: Stub) -> None:
    """Red if a live lease were re-enabled: agent writes would open mid-run, putting an interactive
    writer and the batch through the single-threaded editor at the same time."""
    stub.replies = [key_info(blocked=True, metadata={LEASE_FIELD: "2026-09-05T12:30:00+00:00"})]

    verdict = run_watchdog_once(handle_client(stub), datetime(2026, 9, 5, 12, 0, tzinfo=UTC))

    assert verdict is WatchdogVerdict.PROCESSOR_HOLDS_THE_LEASE
    assert [r.path for r in stub.requests if r.path == "/key/update"] == []


def test_the_watchdog_writes_nothing_to_a_handle_disabled_without_a_lease(stub: Stub) -> None:
    """An operator's own hold. Red if it were re-enabled — the watchdog would undo a deliberate
    human action on a schedule, silently, every pass."""
    stub.replies = [key_info(blocked=True)]

    verdict = run_watchdog_once(handle_client(stub), datetime(2026, 9, 5, 12, 0, tzinfo=UTC))

    assert verdict is WatchdogVerdict.DISABLED_BY_SOMEONE_ELSE
    assert [r.path for r in stub.requests if r.path == "/key/update"] == []


def test_a_naive_lease_timestamp_is_refused_rather_than_assumed_to_be_utc(stub: Stub) -> None:
    """Red if a missing offset were filled in: the watchdog's trigger would shift by however many
    hours the gateway's host happens to be from UTC, in whichever direction, silently."""
    stub.replies = [key_info(blocked=True, metadata={LEASE_FIELD: "2026-09-05T12:05:00"})]

    with pytest.raises(AgentHandleError, match="timezone"):
        handle_client(stub).status()


def test_a_gateway_error_status_is_raised_rather_than_read_as_an_enabled_handle(stub: Stub) -> None:
    """Red if a non-200 fell through to the default `enabled=True`: an unreachable gateway would be
    reported as a healthy handle, and the watchdog would report success having checked nothing."""
    stub.replies = [(503, b"upstream unavailable", {})]

    with pytest.raises(AgentHandleError, match="503"):
        handle_client(stub).status()
