"""Tests for the two HTTP seams — `batch_processor/mcp_client.py` and `agent_instance.py` — against
a real HTTP server on a real socket, never a patched `urlopen`.

The stub answers with bytes chosen per test; what is *not* stubbed is anything this code does:
`urllib`'s opener, the redirect handler, the retry loop, the header plumbing, the merge-patch bodies
and the order they go out in. A monkeypatched `urlopen` would agree with whatever these modules
already believe about all of it, which is the failure mode this repository's testing discipline
names.

Two things no local stub can prove. The deployed vocabulary — the MCP tool names, and the namespace,
Deployment and Lease names — which is why all of them are required configuration rather than
defaults: a stub cannot make a guess true. And the narrowness of the RBAC grant: this stub answers
every request it is given, so only a real API server refusing one is evidence that the processor's
identity cannot scale the ingestor instance or write the agent Deployment's body
(`docs/VERIFICATIONS.md` §5).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from obsidian_tools.batch_processor.agent_instance import (
    MERGE_PATCH,
    AgentInstanceClient,
    AgentInstanceError,
    run_watchdog_once,
)
from obsidian_tools.batch_processor.mcp_client import (
    McpClient,
    McpFailedError,
    McpRefusedError,
    McpToolNames,
    McpUnavailableError,
)
from obsidian_tools.batch_processor.preflight import observed_state
from obsidian_tools.batch_processor.watchdog import WatchdogVerdict
from obsidian_tools.batch_producer.staleness import content_sha256

_KEY = "sk-secret-agent-handle-key"


@dataclass
class Recorded:
    method: str
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
            state.requests.append(Recorded(self.command, self.path, dict(self.headers.items()), body))
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

        def do_PATCH(self) -> None:
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


_FIXTURES = Path(__file__).parent / "fixtures" / "mcp"


def captured(name: str) -> tuple[int, bytes, dict[str, str]]:
    """A response captured off the wire, replayed byte for byte.

    `get_note_absent.sse` and `get_note_content.sse` were captured through the deployed gateway;
    the latter carries a short body in place of the note that was read, with the rendering rebuilt
    by the surface's own rule — the title line the capture showed, glued to the body — because the
    note itself is private vault content and its length is the only thing about it this test uses.
    The rest were captured verbatim from the pinned server image, run against the pinned Obsidian
    image, because the failures they show cannot be provoked on the deployment without breaking it.
    """
    return 200, (_FIXTURES / name).read_bytes(), {"Content-Type": "text/event-stream"}


def ok(text: str) -> tuple[int, bytes, dict[str, str]]:
    body = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}], "isError": False}}
    return 200, json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"}


def read_ok(path: str, content: str) -> tuple[int, bytes, dict[str, str]]:
    """A read the way the surface answers one: the bytes in the typed result, a decorated rendering
    of them in the content array. The two differ on purpose — see `captured`."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": f"**{path}** (format: content)\n\n{content}"}],
            "structuredContent": {"result": {"format": "content", "path": path, "content": content}},
            "isError": False,
        },
    }
    return 200, json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"}


def refusal(text: str) -> tuple[int, bytes, dict[str, str]]:
    body = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}], "isError": True}}
    return 200, json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"}


def tool_error(text: str, *, reason: str) -> tuple[int, bytes, dict[str, str]]:
    """A tool's own failure, with its cause as a field — the shape the deployed surface returns."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": text}],
            "structuredContent": {"error": {"code": -32001, "message": text, "data": {"reason": reason}}},
            "isError": True,
        },
    }
    return 200, json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"}


def mcp_client(stub: Stub, *, retries: int = 3) -> McpClient:
    return McpClient(
        base_url=stub.url,
        api_key=_KEY,
        tools=McpToolNames(
            read="deployment_read_tool",
            write="deployment_write_tool",
            delete="deployment_delete_tool",
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
        mcp_client(stub).write_note("10-areas/x.md", "body\n", overwrite=True)


def test_a_refusal_is_attempted_exactly_once(stub: Stub) -> None:
    """Red if refusals entered the retry loop: a permanent, static-configuration refusal would be
    retried on a doubling schedule forever, turning a loud refusal into a quiet stall."""
    stub.replies = [refusal("path_forbidden")]

    with pytest.raises(McpRefusedError):
        mcp_client(stub).write_note("10-areas/x.md", "body\n", overwrite=True)

    assert len(stub.requests) == 1


def test_a_transient_failure_is_retried_and_then_succeeds(stub: Stub) -> None:
    """Red if a restarting MCP pod failed the chunk outright — every chunk in flight during an
    ordinary rollout would be dead-lettered instead of waited for."""
    stub.replies = [(503, b"", {}), read_ok("10-areas/x.md", "# a note\n")]

    assert mcp_client(stub).read_note("10-areas/x.md") == "# a note\n"
    assert len(stub.requests) == 2


def test_a_persistent_transient_failure_exhausts_the_retries_and_stops(stub: Stub) -> None:
    """Red if the retry loop were unbounded: the run would never reach the dead-letter path and the
    agent handle would stay down for as long as the MCP surface stayed unreachable."""
    stub.replies = [(503, b"", {})]

    with pytest.raises(McpUnavailableError):
        mcp_client(stub, retries=3).read_note("10-areas/x.md")

    assert len(stub.requests) == 3


def test_a_delete_through_a_tool_the_server_lacks_fails_rather_than_succeeding(stub: Stub) -> None:
    """The captured answer to an unknown tool says "not found" — about the tool. Red on the prose
    rule this replaced, under which the delete below returned as if the note were already gone: a
    misnamed delete tool acked every delete in a batch and deleted nothing."""
    stub.replies = [captured("tool_unknown.sse")]

    with pytest.raises(McpFailedError, match="not found"):
        mcp_client(stub).delete_note("10-areas/x.md")

    assert len(stub.requests) == 1


def test_a_read_answered_by_a_case_variant_is_refused(stub: Stub) -> None:
    """Captured: `_ops/CASE.md` asked for, `_ops/case.md` answered — the server's case-insensitive
    fallback, reported as a success. Red if the client took those bytes as the asked-for note: the
    pre-flight would judge a path by another file's content, while the write that follows matches
    the exact path only and creates a second file differing in case."""
    stub.replies = [captured("get_note_case_fallback.sse")]

    with pytest.raises(McpFailedError, match=r"_ops/case\.md"):
        mcp_client(stub).read_note("_ops/CASE.md")


def test_a_write_the_vault_did_not_answer_is_retried(stub: Stub) -> None:
    """Captured: Obsidian's REST API down, reported inside an `isError` result. Red on the rule
    this replaced, which raised a gate refusal on the first attempt and never retried."""
    stub.replies = [captured("write_note_upstream_unreachable.sse"), ok("")]

    mcp_client(stub).write_note("10-areas/x.md", "body\n", overwrite=True)

    assert len(stub.requests) == 2


def test_a_delete_of_an_already_absent_note_succeeds(stub: Stub) -> None:
    """Discipline 3: a retried delete that already landed must not fail a chunk that completed. Red
    if it raised — a redelivered chunk would be dead-lettered for having worked."""
    stub.replies = [tool_error("Not found: 10-areas/x.md", reason="note_missing")]

    mcp_client(stub).delete_note("10-areas/x.md")


def test_a_json_rpc_error_is_permanent_and_names_itself(stub: Stub) -> None:
    """A tool name the deployment does not have arrives exactly this way. Red if it were retried,
    or if it were reported as a gate refusal — the two are fixed in different repositories."""
    stub.replies = [(200, b'{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"unknown tool"}}', {})]

    with pytest.raises(McpFailedError, match="unknown tool"):
        mcp_client(stub).read_note("10-areas/x.md")

    assert len(stub.requests) == 1


def test_the_call_carries_the_configured_tool_name(stub: Stub) -> None:
    """The tool vocabulary is deployment configuration, and this is what proves it is actually used
    rather than shadowed by a constant. Red if a hard-coded name crept back in — the deployment's
    own setting would silently have no effect."""
    stub.replies = [ok("")]

    mcp_client(stub).write_note("10-areas/x.md", "body\n", overwrite=True)

    sent = json.loads(stub.requests[0].body)
    assert sent["method"] == "tools/call"
    assert sent["params"]["name"] == "deployment_write_tool"


def test_every_operation_addresses_its_note_as_a_path_target(stub: Stub) -> None:
    """The surface addresses a note by a discriminated object, and the alternatives resolve against
    a running editor. Red if any operation regressed to a flat key holding a string: the call is
    rejected as malformed, every chunk in the run fails, and nothing distinguishes it from a tool
    name that does not exist."""
    stub.replies = [read_ok("10-areas/x.md", "# a note\n"), ok(""), ok("")]
    client = mcp_client(stub)

    client.read_note("10-areas/x.md")
    client.write_note("10-areas/x.md", "body\n", overwrite=True)
    client.delete_note("10-areas/x.md")

    targets = [json.loads(request.body)["params"]["arguments"]["target"] for request in stub.requests]
    assert targets == [{"type": "path", "path": "10-areas/x.md"}] * 3


def test_a_read_asks_for_the_projection_the_hash_is_taken_over(stub: Stub) -> None:
    """`format` is required by the schema and the richer projections answer questions this
    component never asks. Red if it were dropped (the call is malformed) or widened (a bulk import
    pays for parsed frontmatter and a structural map on every read)."""
    stub.replies = [read_ok("10-areas/x.md", "# a note\n")]

    mcp_client(stub).read_note("10-areas/x.md")

    assert json.loads(stub.requests[0].body)["params"]["arguments"]["format"] == "content"


def test_a_create_asserts_absence_on_the_wire_and_a_modify_does_not(stub: Stub) -> None:
    """The anti-clobber flag, asserted where it is decided rather than through an outcome. A
    surface that accepts both writes answers a create and a modify identically, so **inversion is
    invisible except here**: red if the flag is inverted, defaulted or dropped, each of which
    either clobbers a note this write was never permitted to replace or refuses every modify."""
    stub.replies = [ok(""), ok("")]
    client = mcp_client(stub)

    client.write_note("05-raw/new.md", "body\n", overwrite=False)
    client.write_note("10-areas/x.md", "body\n", overwrite=True)

    assert [json.loads(request.body)["params"]["arguments"]["overwrite"] for request in stub.requests] == [
        False,
        True,
    ]


def test_a_read_returns_the_note_and_never_the_rendering_around_it(stub: Stub) -> None:
    """Replayed from a captured response, and the assertion is the hash ADR-0048 compares.

    The surface returns the note's bytes in its typed result and, in the content array, the same
    bytes behind a title line. Red if the rendering were taken for the note: the hash matches no
    file the producer ever hashed, **every modify in every batch is rejected as stale**, and the
    defect presents as a producer that keeps generating stale patches rather than as a bug here."""
    stub.replies = [captured("get_note_content.sse")]

    content = mcp_client(stub).read_note("CLAUDE.md")

    assert content == "# a note\n\nwith two lines\n"
    assert observed_state(content).content_sha256 == content_sha256(b"# a note\n\nwith two lines\n")


def test_a_success_carrying_no_typed_result_is_refused_rather_than_read_off_the_rendering(stub: Stub) -> None:
    """The fallback that must not exist. Red if a rendering were accepted when the typed result is
    missing — the failure above would return, silently, the moment the surface stopped declaring an
    output schema."""
    stub.replies = [ok("**10-areas/x.md** (format: content)\n\n# a note\n")]

    with pytest.raises(McpFailedError, match="structured note content"):
        mcp_client(stub).read_note("10-areas/x.md")


def test_absence_is_recognised_from_the_stated_reason(stub: Stub) -> None:
    """Replayed from a captured response to a note that is not there. ADR-0048's create branch: red
    if it raised, and every create in the bootstrap import is dead-lettered for a target that was
    legitimately absent."""
    stub.replies = [captured("get_note_absent.sse")]

    assert mcp_client(stub).read_note("05-raw/does-not-exist-probe.md") is None


def test_a_refusal_stating_another_reason_is_not_absence_however_it_reads(stub: Stub) -> None:
    """The false-absence direction, closed. A refusal that names its cause has answered the
    question, so its prose is not consulted — red if the markers were matched anyway, which is how
    a create is admitted in front of a note that exists and, but for `overwrite: false`, would
    overwrite it."""
    stub.replies = [tool_error("Not found: no vault mounted at that scope", reason="scope_unmounted")]

    with pytest.raises(McpRefusedError):
        mcp_client(stub).read_note("10-areas/x.md")


def test_a_retried_create_meets_its_own_first_attempt_and_fails_loudly(stub: Stub) -> None:
    """A create that landed and lost its response: the retry is refused `file_exists`, the chunk
    fails half-applied and is regenerated. Discipline 3.

    Red if someone "fixed" retries by relaxing the flag on the second attempt — every redelivered
    create would become a whole-note overwrite of whatever is at the path, which is the silent lost
    update the flag exists to prevent, and this test asserts the flag on *both* attempts rather
    than only on the first."""
    stub.replies = [(503, b"", {}), tool_error("file_exists: 05-raw/new.md already exists", reason="file_exists")]

    with pytest.raises(McpRefusedError, match="file_exists"):
        mcp_client(stub).write_note("05-raw/new.md", "body\n", overwrite=False)

    assert [json.loads(request.body)["params"]["arguments"]["overwrite"] for request in stub.requests] == [
        False,
        False,
    ]


def test_a_session_id_from_the_handshake_rides_on_every_later_request(stub: Stub) -> None:
    """Streamable HTTP's session header. Red if it were dropped: a session-using server would
    refuse every call after `initialize`, and the refusals would look like gate refusals."""
    stub.replies = [(*ok("")[:2], {"Mcp-Session-Id": "sess-123"}), read_ok("10-areas/x.md", "# a note\n")]
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


# --- the agent MCP instance -----------------------------------------------------------------------

_NAMESPACE = "obsidian-vault"
_DEPLOYMENT = "mcp-obsidian-agent"
_LEASE = "batch-mode"
_SCALE_PATH = f"/apis/apps/v1/namespaces/{_NAMESPACE}/deployments/{_DEPLOYMENT}/scale"
_LEASE_PATH = f"/apis/coordination.k8s.io/v1/namespaces/{_NAMESPACE}/leases/{_LEASE}"
_TOKEN = "a-projected-service-account-token"

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def token(tmp_path: Path) -> Path:
    path = tmp_path / "token"
    path.write_text(f"{_TOKEN}\n", encoding="utf-8")
    return path


def instance_client(stub: Stub, token: Path) -> AgentInstanceClient:
    return AgentInstanceClient(
        api_url=stub.url,
        namespace=_NAMESPACE,
        deployment=_DEPLOYMENT,
        lease_name=_LEASE,
        holder_identity="batch-processor-29123456-abcde",
        token_path=str(token),
        ca_path=None,
        timeout_seconds=5.0,
        verify_tls=False,
    )


def scale(replicas: int, *, observed: int = 0) -> tuple[int, bytes, dict[str, str]]:
    """`status.replicas` deliberately disagrees with `spec.replicas`, so any code reading
    availability instead of intent reads the wrong number rather than accidentally the right one."""
    body = {"kind": "Scale", "spec": {"replicas": replicas}, "status": {"replicas": observed}}
    return 200, json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"}


def lease(**spec: object) -> tuple[int, bytes, dict[str, str]]:
    body = {"kind": "Lease", "spec": spec}
    return 200, json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"}


def held(*, renewed_at: datetime, seconds: int = 300) -> tuple[int, bytes, dict[str, str]]:
    return lease(
        holderIdentity="a-run",
        acquireTime=renewed_at.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
        renewTime=renewed_at.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
        leaseDurationSeconds=seconds,
    )


def patches(stub: Stub) -> list[Recorded]:
    return [request for request in stub.requests if request.method == "PATCH"]


def test_a_run_takes_the_lease_before_it_stops_the_instance(stub: Stub, token: Path) -> None:
    """ADR-0052's ordering, the half that replaces atomicity at the start of a run. A crash between
    the two writes must leave the instance *running*, so the lease has to be the first write.

    Red if reversed: a run killed in that window would leave the instance at zero replicas with no
    lease — the exact state the watchdog is required to read as an operator's own hold and leave
    alone, permanently. Every interactive write in the system would stop with nothing to recover
    it."""
    stub.replies = [lease(), scale(0)]

    instance_client(stub, token).begin_batch_run(NOW, 300.0)

    assert [request.path for request in patches(stub)] == [_LEASE_PATH, _SCALE_PATH]


def test_a_run_starts_the_instance_before_it_releases_the_lease(stub: Stub, token: Path) -> None:
    """The same ordering at the other end. Red if reversed: a run killed between the two writes ends
    with the instance stopped and its lease already dropped, which is again unrecoverable by
    design."""
    stub.replies = [scale(1), lease()]

    instance_client(stub, token).end_batch_run()

    assert [request.path for request in patches(stub)] == [_SCALE_PATH, _LEASE_PATH]


def test_the_stop_patches_the_scale_subresource_and_names_only_replicas(stub: Stub, token: Path) -> None:
    """The narrowness of the grant, expressed as what the client actually sends. The Deployment's
    own body carries `OBSIDIAN_WRITE_PATHS` — Gate 2 — and its image, so the authority to write it
    is refused (ADR-0052) and this request would be refused with it.

    Red if the path were the Deployment rather than its `scale`, or if the body carried any field
    besides `spec.replicas`: a merge patch naming a field it did not set out to change is how a
    write silently takes ownership of something it does not own."""
    stub.replies = [lease(), scale(0)]

    instance_client(stub, token).begin_batch_run(NOW, 300.0)

    stop = patches(stub)[-1]
    assert stop.path == _SCALE_PATH
    assert stop.path.endswith("/scale")
    assert json.loads(stop.body) == {"spec": {"replicas": 0}}


def test_every_write_is_a_merge_patch(stub: Stub, token: Path) -> None:
    """Red on any other content type. A strategic-merge or JSON patch would be answered differently
    by the API server — and a `PUT` would replace the Lease wholesale, taking with it whatever the
    manifest declares. Merge-patch is also what makes the release path expressible as nulls."""
    stub.replies = [lease(), scale(0)]

    instance_client(stub, token).begin_batch_run(NOW, 300.0)

    assert {request.headers["Content-Type"] for request in patches(stub)} == {MERGE_PATCH}


def test_the_lease_is_acquired_with_a_six_digit_fractional_timestamp(stub: Stub, token: Path) -> None:
    """Kubernetes parses `MicroTime` with a Go layout whose zero-padded fraction requires exactly six
    digits, and `datetime.isoformat()` omits the fraction entirely when `microsecond` is zero.

    Red if `isoformat()` crept back in: roughly one lease write in a million — the ones stamped on a
    whole second — would be rejected with a 400, which is the worst possible frequency for a bug in
    the seam that stops interactive writes."""
    stub.replies = [lease(), scale(0)]

    instance_client(stub, token).begin_batch_run(NOW, 300.0)

    sent = json.loads(patches(stub)[0].body)["spec"]
    assert sent["renewTime"] == "2026-09-05T12:00:00.000000Z"
    assert sent["acquireTime"] == sent["renewTime"]
    assert sent["leaseDurationSeconds"] == 300
    assert sent["holderIdentity"] == "batch-processor-29123456-abcde"


def test_renewing_the_lease_touches_the_renewal_time_and_nothing_else(stub: Stub, token: Path) -> None:
    """A renewal says one thing: this processor is still alive. Red if it also patched the scale — a
    renewal that stops would mask a run that never stopped the instance (the instance would end up
    stopped anyway, and no test could tell the two apart) and would silently override whoever
    changed the instance mid-run. Red equally if it re-asserted the holder or the duration, which
    would let a renewal steal a lease from another holder."""
    stub.replies = [lease()]

    instance_client(stub, token).renew_lease(NOW)

    [renewal] = patches(stub)
    assert renewal.path == _LEASE_PATH
    assert json.loads(renewal.body) == {"spec": {"renewTime": "2026-09-05T12:00:00.000000Z"}}


def test_releasing_the_lease_clears_every_field_the_run_wrote(stub: Stub, token: Path) -> None:
    """Red if the lease outlived the run: a held lease on a running instance is a fact about a run
    that has already finished, and the next hand-stop of the instance would be read as this system's
    doing and undone. Nulls rather than a replace, because a replace would also erase whatever the
    manifest declares on the object."""
    stub.replies = [scale(1), lease()]

    instance_client(stub, token).end_batch_run()

    assert json.loads(patches(stub)[-1].body) == {
        "spec": {"holderIdentity": None, "acquireTime": None, "renewTime": None, "leaseDurationSeconds": None}
    }


def test_running_is_read_from_desired_replicas_never_from_availability(stub: Stub, token: Path) -> None:
    """The stub answers `spec.replicas: 1` with `status.replicas: 0` — a Deployment somebody asked to
    run whose pod is not up, which is what an ordinary crashloop looks like.

    Red if availability decided: a crashlooping agent instance would present as a batch run holding
    the door, and the watchdog would either wait on a lease that will never appear or restart
    something that is already meant to be running. Health is a different question with a different
    owner."""
    stub.replies = [scale(1, observed=0), lease()]

    assert instance_client(stub, token).status().running is True


def test_the_lease_deadline_is_read_off_the_object(stub: Stub, token: Path) -> None:
    """Red if the deadline were taken from anywhere but `renewTime + leaseDurationSeconds`: the
    watchdog's trigger would stop being the thing the processor actually renews."""
    stub.replies = [scale(0), held(renewed_at=NOW, seconds=120)]

    status = instance_client(stub, token).status()

    assert status.running is False
    assert status.lease_expires_at == datetime(2026, 9, 5, 12, 2, 0, tzinfo=UTC)


def test_an_unheld_lease_reads_as_no_deadline(stub: Stub, token: Path) -> None:
    """How the object ships in git, and how a released one looks. Red if an empty spec produced a
    deadline: a stopped instance would stop being readable as an operator's own hold."""
    stub.replies = [scale(0), lease()]

    assert instance_client(stub, token).status().lease_expires_at is None


def test_a_naive_renewal_time_is_refused_rather_than_assumed_to_be_utc(stub: Stub, token: Path) -> None:
    """Red if a missing offset were filled in: the watchdog's trigger would shift by however many
    hours the writer's host happens to be from UTC, in whichever direction, silently."""
    stub.replies = [scale(0), lease(renewTime="2026-09-05T12:05:00", leaseDurationSeconds=300)]

    with pytest.raises(AgentInstanceError, match="timezone"):
        instance_client(stub, token).status()


def test_a_scale_patch_that_stored_a_different_count_is_a_failure(stub: Stub, token: Path) -> None:
    """The API server answers a scale patch with the `Scale` it stored, so the count it reports is
    its own account rather than the client's hope.

    Red if the 200 alone were trusted: a restore that changed nothing would be logged as a restore,
    the run would exit clean, the lease would be released, and the instance would sit at zero with
    nothing left to notice — the failure this whole component exists to make impossible, arrived at
    from the inside. An admission webhook pinning replicas, or a `resourceNames` mismatch answered
    as a no-op, both land here."""
    stub.replies = [scale(0)]

    with pytest.raises(AgentInstanceError, match="stored 0"):
        instance_client(stub, token).start_instance()


def test_a_forbidden_scale_patch_is_raised_rather_than_read_as_success(stub: Stub, token: Path) -> None:
    """A grant whose `resourceNames` does not match this Deployment answers exactly this. Red if a
    non-200 fell through: a run would proceed believing the door was shut while every interactive
    writer still had it open."""
    stub.replies = [(403, b'{"kind":"Status","reason":"Forbidden"}', {})]

    with pytest.raises(AgentInstanceError, match="403"):
        instance_client(stub, token).stop_instance()


def test_a_missing_lease_names_the_object_and_says_it_is_not_this_workload_s_to_create(stub: Stub, token: Path) -> None:
    """The grant carries no `create` verb, because RBAC cannot restrict `create` by `resourceNames`
    and granting it would widen the grant to every Lease in the namespace. So a deleted Lease is
    unrecoverable by this workload and must fail loudly.

    Red if it read as a bare 404 or as a permissions error: an operator would go looking at the Role
    rather than at the object a namespace recreate or a manifest tidy-up removed."""
    stub.replies = [scale(0), (404, b'{"kind":"Status","reason":"NotFound"}', {})]

    with pytest.raises(AgentInstanceError, match="does not exist"):
        instance_client(stub, token).status()


def test_the_service_account_token_is_read_on_every_request(stub: Stub, token: Path) -> None:
    """Bound service-account tokens are rotated in place by the kubelet. Red if the token were
    cached at construction: a run measured in hours would start answering 401 partway through, with
    the instance already stopped — the one state that must never become permanent."""
    stub.replies = [lease(), scale(0)]
    client = instance_client(stub, token)

    client.acquire_lease(NOW, 300.0)
    token.write_text("a-rotated-token\n", encoding="utf-8")
    client.stop_instance()

    assert stub.requests[0].headers["Authorization"] == f"Bearer {_TOKEN}"
    assert stub.requests[-1].headers["Authorization"] == "Bearer a-rotated-token"


def test_the_service_account_token_never_appears_in_a_failure_message(stub: Stub, token: Path) -> None:
    """Error text is built from the URL, the exception and the response body. Red if a header ever
    reached a message — a token the API server honours from anywhere would be in the logs of every
    failed watchdog pass, and the watchdog runs every five minutes."""
    stub.replies = [(500, b"boom", {})]

    with pytest.raises(AgentInstanceError) as caught:
        instance_client(stub, token).status()

    assert _TOKEN not in str(caught.value)


def test_a_redirect_on_the_api_path_is_refused_rather_than_followed(stub: Stub, token: Path) -> None:
    """The same refusal as on the MCP path, against the other credential. Red if a redirect were
    followed — one response could retarget the next request and send a service-account token, which
    the API server honours from anywhere, to a host the configuration never named."""
    stub.replies = [(302, b"", {"Location": "http://127.0.0.1:1/evil"})]

    with pytest.raises(AgentInstanceError):
        instance_client(stub, token).status()

    assert len(stub.requests) == 1


# --- the watchdog's four dispatches, over the wire --------------------------------------------------


def test_the_watchdog_restarts_an_instance_whose_lease_has_expired(stub: Stub, token: Path) -> None:
    """The end-to-end injection for unit D4: a processor that died mid-run leaves exactly this, and
    the pass turns interactive writes back on. Red if no scale patch were issued — every interactive
    write in the system stays stopped. Red equally if the lease were not released with it: the
    debris would outlive the run it describes."""
    stub.replies = [scale(0), held(renewed_at=NOW - timedelta(hours=1)), scale(1), lease()]

    verdict = run_watchdog_once(instance_client(stub, token), NOW)

    assert verdict is WatchdogVerdict.RESTART_INSTANCE
    assert [request.path for request in patches(stub)] == [_SCALE_PATH, _LEASE_PATH]
    assert json.loads(patches(stub)[0].body) == {"spec": {"replicas": 1}}


def test_the_watchdog_writes_nothing_while_a_lease_is_live(stub: Stub, token: Path) -> None:
    """Red if a live lease were restarted: interactive writes would open mid-run, putting an
    interactive writer and the batch through the single-threaded editor at the same time."""
    stub.replies = [scale(0), held(renewed_at=NOW)]

    verdict = run_watchdog_once(instance_client(stub, token), NOW)

    assert verdict is WatchdogVerdict.PROCESSOR_HOLDS_THE_LEASE
    assert patches(stub) == []


def test_the_watchdog_writes_nothing_to_an_instance_stopped_without_a_lease(stub: Stub, token: Path) -> None:
    """An operator's own hold. Red if it were restarted — the watchdog would undo a deliberate human
    action on a schedule, silently, every pass."""
    stub.replies = [scale(0), lease()]

    verdict = run_watchdog_once(instance_client(stub, token), NOW)

    assert verdict is WatchdogVerdict.STOPPED_BY_SOMEONE_ELSE
    assert patches(stub) == []


def test_the_watchdog_drops_a_stale_lease_without_touching_a_running_instance(stub: Stub, token: Path) -> None:
    """The debris a crash between either ordered pair leaves behind. Red if the pass also patched
    the scale: it would be acting on the instance because of a fact about a finished run. Red if it
    wrote nothing at all: the debris would survive to be met by the next operator hand-stop, which
    the watchdog would then read as its own and undo."""
    stub.replies = [scale(1), held(renewed_at=NOW - timedelta(hours=1)), lease()]

    verdict = run_watchdog_once(instance_client(stub, token), NOW)

    assert verdict is WatchdogVerdict.RELEASE_STALE_LEASE
    assert [request.path for request in patches(stub)] == [_LEASE_PATH]
