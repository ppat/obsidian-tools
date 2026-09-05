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

3. **Retrying a write is safe because every write is a whole note.** A timed-out write may in fact
   have landed; re-sending identical full content converges either way. A delete is made idempotent
   explicitly, by treating "not found" as success — otherwise the second attempt of a delete that
   did land would fail a chunk that had actually completed.

4. **The key is never logged.** The gateway key in play carries this component's write scope, the
   widest in the system. Error text is built from the URL and the envelope; `transport.py` holds
   the redirect refusal that keeps the key from being sent anywhere the configuration did not name.

## What is configuration here, and why

The **tool names are required configuration with no defaults**, and the argument keys are
configurable. This repository has never seen the deployed MCP surface — `docs/VERIFICATIONS.md`
records the gate's refusal *shape*, not its tool vocabulary — and a guessed default would be wrong
in a way that presents as a refusal at every call site. Requiring them makes the deployment state
what it runs against (apps#3875) rather than inherit a guess from here.

The session handshake is streamable HTTP's: `initialize`, then the `notifications/initialized`
notification, carrying `Mcp-Session-Id` on every later request if the server issued one. A server
that does not use sessions omits the header and nothing changes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from obsidian_tools.batch_processor.envelope import McpOutcome, OutcomeKind, classify_response
from obsidian_tools.batch_processor.transport import TransportUnreachableError, build_opener, send
from obsidian_tools.retry import RetryExhaustedError, retry_with_backoff

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = "2025-06-18"
_SESSION_HEADER = "Mcp-Session-Id"


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
    """The deployed tool vocabulary. Every field is a deployment fact, not a preference."""

    read: str
    write: str
    delete: str
    path_argument: str
    content_argument: str


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
    ) -> None:
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
                "clientInfo": {"name": "obsidian-tools batch-processor", "version": _PROTOCOL_VERSION},
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

        Absence is what ADR-0048's create branch turns on, and it is the one answer here recovered
        from a refusal's prose rather than from a structural field — see `envelope` for why that
        recognition is an allow-list rather than a catch-all.
        """
        outcome = self._call(self._tools.read, {self._tools.path_argument: path})
        if outcome.kind is OutcomeKind.NOT_FOUND:
            return None
        _raise_for(outcome, f"read {path!r}")
        return outcome.text

    def write_note(self, path: str, content: str) -> None:
        outcome = self._call(
            self._tools.write, {self._tools.path_argument: path, self._tools.content_argument: content}
        )
        _raise_for(outcome, f"write {path!r}")

    def delete_note(self, path: str) -> None:
        outcome = self._call(self._tools.delete, {self._tools.path_argument: path})
        if outcome.kind is OutcomeKind.NOT_FOUND:
            return  # see discipline 3: a retried delete that already landed must not fail the chunk
        _raise_for(outcome, f"delete {path!r}")

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


def _raise_for(outcome: McpOutcome, what: str) -> None:
    if outcome.kind is OutcomeKind.OK:
        return
    if outcome.kind is OutcomeKind.UNAVAILABLE:
        raise McpUnavailableError(f"{what}: {outcome.detail}")
    if outcome.kind in (OutcomeKind.REFUSED, OutcomeKind.NOT_FOUND):
        raise McpRefusedError(f"{what}: the gated MCP path refused it: {outcome.detail}")
    raise McpFailedError(f"{what}: {outcome.detail}")
