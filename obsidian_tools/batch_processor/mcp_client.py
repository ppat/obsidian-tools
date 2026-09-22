"""The one seam onto the gated MCP path. Every vault read and every vault write crosses it.

Same reasoning as `GitRunner` (ADR-0046) and the producer's `BatchPublisher`: the disciplines below
are enforced once, here, rather than remembered at each call site.

## The four disciplines

1. **Every response goes through `envelope.py`.** A write-gate refusal is HTTP 200 with the error
   inside the JSON-RPC envelope, so a caller that reads the status code learns nothing at all about
   the gate. Nothing here decides what a response means; it classifies and raises.

2. **Only `UNAVAILABLE` is retried.** Retrying a refusal re-runs a decision made by static
   configuration, turning a loud refusal into a quiet unbounded loop — the same trap the producer's
   `violations.py` exists to avoid one component upstream. The retry is `retry.retry_with_backoff`,
   this repository's one backoff helper, not a second.

3. **A modify and a delete are safe to retry; a create is not, and it fails loudly rather than
   quietly clobbering.** A modify carries `overwrite: true` and re-sends the whole note, so a
   timed-out attempt that in fact landed converges on the retry. A delete is made idempotent
   explicitly, by treating absence as success — otherwise the second attempt of a delete that did
   land would fail a chunk that had actually completed. A create carries `overwrite: false`
   (ADR-0053), so a create that landed and lost its response meets its own first attempt on the
   retry and is refused `file_exists`: the chunk fails half-applied and is dead-lettered. That is
   the deliberate trade — the alternative, relaxing the flag on a retry, makes every redelivery of
   a create a whole-note overwrite of whatever is at the path, which is the silent lost update the
   flag exists to prevent. It also converges: the create's post-image is already at the path, so
   the regenerated chunk is settled by `patching.already_applied` without writing anything.

4. **The key is never logged.** The gateway key in play carries this component's write scope, the
   widest in the system. Error text is built from the URL and the envelope; `transport.py` holds
   the redirect refusal that keeps the key from being sent anywhere the configuration did not name.

## What is configuration here, and why

**A tool's name is deployment identity and stays required configuration with no default; a tool's
argument shape is a fact about its schema and is code.** The names are prefixed by the gateway
access group the key reaches the server through, not by the server's own name, so a regrouping
renames every tool without changing anything about what the tools do — and a guessed name would be
wrong in a way that presents as a refusal at every call site. The argument shape is the opposite
kind of fact: the target envelope below is a discriminated object the schema requires, which no
key-name setting can express, and a wrong shape arrives as a malformed-arguments JSON-RPC error
that `envelope.py` classifies `FAILED` — loud, and needing no knob to fix.

The session handshake is streamable HTTP's: `initialize`, then the `notifications/initialized`
notification, carrying `Mcp-Session-Id` on every later request if the server issued one. A server
that does not use sessions omits the header and nothing changes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import cast

from obsidian_tools.batch_processor.envelope import McpOutcome, OutcomeKind, classify_response
from obsidian_tools.batch_processor.transport import TransportUnreachableError, build_opener, send
from obsidian_tools.retry import RetryExhaustedError, retry_with_backoff

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = "2025-06-18"
_SESSION_HEADER = "Mcp-Session-Id"

# The read projection returning the raw markdown body and nothing else. Not configurable: which
# projection to ask for is fixed by what the staleness hash is computed over (ADR-0048), not by
# preference.
_CONTENT_PROJECTION = "content"


class McpError(RuntimeError):
    """Any failure of this seam."""


class McpUnavailableError(McpError):
    """Nothing decided anything — a 5xx, a timeout, a dropped connection. The retryable one."""


class McpRefusedError(McpError):
    """The gate refused the call, inside an HTTP 200. Never retried: the scope is configuration."""


class McpFailedError(McpError):
    """A permanent failure that is not a gate refusal: a JSON-RPC error, an unreadable body, a 4xx."""


@dataclass(frozen=True, slots=True)
class McpToolNames:
    """The deployed tool vocabulary. Every field is a deployment fact, not a preference.

    `delete` and `append` are optional because a key is granted only the tools its holder uses: the
    lint pass never deletes and holds no delete tool (ADR-0004), `batch-processor` never appends.
    Calling an operation whose tool is not configured fails before anything is sent.
    """

    read: str
    write: str
    delete: str | None = None
    append: str | None = None


class McpClient:
    """A configured client for one batch run's worth of calls."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        tools: McpToolNames,
        timeout_seconds: float,
        verify_tls: bool,
        retries: int,
        retry_base_delay_seconds: float,
        client_name: str = "obsidian-tools batch-processor",
    ) -> None:
        self._client_name = client_name
        self._url = base_url.rstrip("/")
        self._api_key = api_key
        self._tools = tools
        self._timeout = timeout_seconds
        self._retries = retries
        self._retry_base_delay = retry_base_delay_seconds
        self._session_id: str | None = None
        self._next_id = 0
        self._opener = build_opener(verify_tls)

    # --- the vault operations -------------------------------------------------------------------

    def connect(self) -> None:
        """Open an MCP session. A server issuing no session id leaves this a handshake and nothing
        more, which is the point: the client cannot tell in advance and must not need to."""
        outcome, headers = self._request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": self._client_name, "version": _PROTOCOL_VERSION},
            },
        )
        _raise_for(outcome, "initialize")
        self._session_id = headers.get(_SESSION_HEADER) or None
        # A notification carries no id and expects no response, so nothing classifies its reply.
        self._post(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode("utf-8"))
        logger.info(
            "opened a session on the gated MCP path",
            extra={"event": "mcp_connected", "url": self._url, "session": bool(self._session_id)},
        )

    def read_note(self, path: str) -> str | None:
        """The note's whole content, or `None` when it is not there.

        The `content` projection is asked for rather than a richer one because the bytes are the
        whole question: they are what ADR-0048's hash is taken over, and parsed frontmatter, file
        metadata and a structural map are answers to questions this component never asks, paid for
        on every read of a bulk import.

        Absence is what ADR-0048's create branch turns on, and it arrives as an ordinary tool error
        — see `envelope` for which part of that error is believed and why the recognition is an
        allow-list rather than a catch-all.
        """
        outcome = self._call(self._tools.read, {"target": _target(path), "format": _CONTENT_PROJECTION})
        if outcome.kind is OutcomeKind.NOT_FOUND:
            return None
        _raise_for(outcome, f"read {path!r}")
        return _note_content(outcome, path)

    def write_note(self, path: str, content: str, *, overwrite: bool) -> None:
        """Write the whole note. `overwrite` is the chunk's declared operation, never a preference.

        Keyword-only and without a default on purpose: this flag is the one server-side assertion
        the surface offers (ADR-0053), and a default is how a caller ends up sending whichever
        value was convenient at the call site rather than the one its chunk declared.
        """
        outcome = self._call(self._tools.write, {"target": _target(path), "content": content, "overwrite": overwrite})
        _raise_for(outcome, f"write {path!r}")

    def delete_note(self, path: str) -> None:
        outcome = self._call(_configured(self._tools.delete, "delete"), {"target": _target(path)})
        if outcome.kind is OutcomeKind.NOT_FOUND:
            return  # see discipline 3: a retried delete that already landed must not fail the chunk
        _raise_for(outcome, f"delete {path!r}")

    def append_note(self, path: str, content: str) -> None:
        """Append `content` to the end of an existing note, with no section.

        **Only for a target the caller already knows is there.** Without a section the tool creates
        an absent target with the appended text as its whole content (its own schema says so), so an
        append aimed at a note that has gone away lands as a fragment and reports success — the
        third ground of ADR-0053. Whether the target exists is the caller's to establish; nothing on
        this call can.

        Retried like a modify, and not safe in the same way: an append that landed and lost its
        response is appended again. The lint pass accepts that for its one log line, whose worst
        case is a duplicate line in an append-only file, rather than failing the pass.
        """
        outcome = self._call(_configured(self._tools.append, "append"), {"target": _target(path), "content": content})
        _raise_for(outcome, f"append to {path!r}")

    # --- the transport --------------------------------------------------------------------------

    def _call(self, tool: str, arguments: dict[str, object]) -> McpOutcome:
        def attempt() -> McpOutcome:
            outcome, _ = self._request("tools/call", {"name": tool, "arguments": arguments})
            if outcome.retryable:
                raise McpUnavailableError(f"{tool}: {outcome.detail}")
            return outcome

        try:
            return retry_with_backoff(
                attempt,
                description=f"MCP call {tool}",
                retries=self._retries,
                base_delay=self._retry_base_delay,
                retry_on=(McpUnavailableError,),
                logger=logger,
            )
        except RetryExhaustedError as exc:
            raise McpUnavailableError(str(exc)) from exc

    def _request(self, method: str, params: dict[str, object]) -> tuple[McpOutcome, dict[str, str]]:
        self._next_id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}).encode("utf-8")
        return self._post(body)

    def _post(self, body: bytes) -> tuple[McpOutcome, dict[str, str]]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            # Both, because the streamable-HTTP transport lets the server answer either way and
            # `envelope.decode_body` reads either. Advertising one would let a server that prefers
            # the other refuse the request outright.
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id is not None:
            headers[_SESSION_HEADER] = self._session_id
        try:
            response = send(self._opener, self._url, method="POST", headers=headers, body=body, timeout=self._timeout)
        except TransportUnreachableError as exc:
            return McpOutcome(OutcomeKind.UNAVAILABLE, str(exc)), {}
        return classify_response(response.status, response.body), response.headers


def _target(path: str) -> dict[str, object]:
    """A vault path as the tools' target envelope.

    The surface addresses a note by a discriminated object — a path, the file open in the UI, or a
    periodic note — and only the first of the three is ever this component's business: the other
    two are properties of a running editor, and a bulk write resolved against whichever file
    happens to be open is a write to a path nobody named.
    """
    return {"type": "path", "path": path}


def _note_content(outcome: McpOutcome, path: str) -> str:
    """The note's own bytes, taken from the read tool's typed result and never from its rendering.

    **`content[]` is a rendering, and hashing it would make every modify look stale.** The deployed
    surface returns the note's body there behind a title line, so a hash taken over it agrees with
    the producer's hash for no file — and ADR-0048's comparison would reject every modify in every
    batch while reporting exactly what a producer generating stale patches reports. The typed
    result is the note; the rendering is prose about it.

    A success carrying no typed result is refused rather than fallen back on. The fallback's
    failure mode is the silent one above; this one stops the run and names the tool.
    """
    result = (outcome.structured or {}).get("result")
    content = cast("dict[str, object]", result).get("content") if isinstance(result, dict) else None
    if not isinstance(content, str):
        raise McpFailedError(
            f"read {path!r}: the response carried no structured note content, so there are no bytes "
            "to hash that the surface has vouched for"
        )
    return content


def _configured(tool: str | None, operation: str) -> str:
    if tool is None:
        raise McpFailedError(f"no {operation} tool is configured for this client, so it may not {operation}")
    return tool


def _raise_for(outcome: McpOutcome, what: str) -> None:
    if outcome.kind is OutcomeKind.OK:
        return
    if outcome.kind is OutcomeKind.UNAVAILABLE:
        raise McpUnavailableError(f"{what}: {outcome.detail}")
    if outcome.kind in (OutcomeKind.REFUSED, OutcomeKind.NOT_FOUND):
        raise McpRefusedError(f"{what}: the gated MCP path refused it: {outcome.detail}")
    raise McpFailedError(f"{what}: {outcome.detail}")
