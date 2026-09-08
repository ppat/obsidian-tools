"""What a response from the gated MCP path actually means. Pure — no sockets, no client.

**This module exists because of one fact: a write-gate refusal is HTTP 200.** The gateway and the
MCP server both answer a refused write with a perfectly ordinary success at the HTTP layer and put
the refusal inside the JSON-RPC envelope — the `path_forbidden` refusal proven in both directions
at content-foundation acceptance arrived exactly that way (`docs/VERIFICATIONS.md` §1). So no
status code, and no metric derived from one, can ever observe the gate. The envelope is the only
place a refusal is visible at all, which makes this classification a decision rather than
plumbing — and decisions in this codebase are pure functions with tables behind them.

## The four shapes a single `tools/call` can come back as

| On the wire | Means | `OutcomeKind` |
| --- | --- | --- |
| 200, `{"result": {"content": [...], "isError": false}}` | the write happened | `OK` |
| 200, `{"result": {"content": [...], "isError": true}}` | **the gate refused it** | `REFUSED` |
| the same, whose error names a reason of absence | the note is not there | `NOT_FOUND` |
| 200, `{"error": {"code": …, "message": …}}` | the call was malformed, or the tool does not exist | `FAILED` |
| a 5xx, a timeout, a dropped connection | nobody decided anything | `UNAVAILABLE` |

`isError` is the load-bearing one and the easiest to miss: MCP puts a *tool's own* failure inside a
successful result on purpose, so that a model can see and react to it. A parser that checks only
for a JSON-RPC `error` reads every gate refusal as a success and applies nothing while reporting
that it applied everything.

**`UNAVAILABLE` is the only retryable kind, and that is the whole point of separating them.**
Retrying a refusal re-runs a decision that is a property of static configuration, which turns a
loud "this write is not permitted" into an unbounded quiet loop — the same failure the producer's
`violations.py` exists to prevent one layer up.

## A result carries two renderings of itself, and only one of them is the data

A tool that declares an `outputSchema` answers with `structuredContent` — its typed result — *and*
a `content[]` text block rendering the same thing for a reader. The rendering is free to decorate:
the deployed read tool returns the note's body in `structuredContent` and, in `content[]`, that
body behind a title line. So `content[]` is where a refusal's message lives and where an operator
reads what went wrong, and it is never where a caller takes a value it intends to compare or hash.
Both travel on `McpOutcome`; which one a given tool's caller must read is that tool's contract, and
lives with the caller.

The same split decides absence. A tool error carrying `structuredContent.error.data.reason` has
stated its cause as a field, and that field is authoritative here; prose matching remains only for
an error that carries no reason at all.

## Two shapes of body, both legal

MCP's streamable-HTTP transport lets a server answer a single request with either a plain JSON body
or a one-event SSE stream, and the client advertises both in `Accept`. A parser that knows only one
reads the other as malformed, so `decode_body` accepts either. The SSE branch takes the *last*
`data:` event: a JSON-RPC response is the final event of the stream, and anything before it is
progress notification rather than the answer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum, auto
from typing import cast

# Statuses that mean "ask again later" rather than "this will never work". Everything else outside
# 200 is a permanent failure: a 401/403 from the gateway is a credential or handle problem no amount
# of retrying fixes, and retrying it would spend a batch run hammering a door that is shut on
# purpose. 408 and 429 join the 5xx range because both are the server explicitly asking for a retry.
_RETRYABLE_STATUSES = frozenset({408, 429})

# The `reason` values that mark a tool error as "the note is not there" rather than "you may not
# touch it". The distinction is load-bearing in exactly one place — ADR-0048's create branch, where
# a target must *not* exist. An allow-list, so an unrecognised reason stays a refusal and a create
# fails loudly rather than being let through in front of a note that exists.
_ABSENT_REASONS = frozenset({"note_missing"})

# The same distinction taken from the refusal's prose, used only when the error carries no
# structured reason at all. Prose is the weaker instrument in both directions: it is a property of
# the server's wording rather than of its contract, and a refusal for some *other* cause whose text
# happens to match reads as absence. Kept because a surface that declares no structured error shape
# would otherwise have no absence signal whatsoever, and matched case-insensitively as an
# allow-list for the same reason `_ABSENT_REASONS` is one.
_NOT_FOUND_MARKERS = ("not found", "does not exist", "no such file", "404")


class OutcomeKind(Enum):
    OK = auto()
    """The tool ran and reported success."""

    REFUSED = auto()
    """The gate said no, inside a 200. Permanent for this chunk: the scope is server configuration."""

    NOT_FOUND = auto()
    """A refusal whose stated reason — or, failing that, whose text — says the note is absent. A
    fact about the vault, not about authority."""

    FAILED = auto()
    """A JSON-RPC error, a permanent HTTP status, or a body this module cannot read. Never retried."""

    UNAVAILABLE = auto()
    """Nothing decided anything: a 5xx, a timeout, a dropped connection. The only retryable kind."""


@dataclass(frozen=True, slots=True)
class McpOutcome:
    """One `tools/call`, classified."""

    kind: OutcomeKind
    detail: str
    """Operator-facing text: the refusal's own message, the JSON-RPC error, or the status."""

    text: str = ""
    """The tool's `content[]` blocks, concatenated. A *rendering* — for a human or a model — and
    never the tool's data: see `structured`. Empty for everything but `OK`."""

    structured: dict[str, object] | None = None
    """The response's `structuredContent`, verbatim, when it carried one.

    A tool declaring an `outputSchema` answers with its typed result here and a human-readable
    rendering of the same thing in `content[]`, and the two are not interchangeable — the deployed
    read tool prefixes the note's body with a title line in `content[]`. Which field a caller must
    read is a fact about that tool's contract, so this module carries the object through and
    `mcp_client.py` picks the field out.
    """

    @property
    def retryable(self) -> bool:
        return self.kind is OutcomeKind.UNAVAILABLE


class EnvelopeError(ValueError):
    """A body that is not a JSON-RPC response at all."""


def classify_response(status: int, body: bytes) -> McpOutcome:
    """Turn one HTTP response into the outcome it represents.

    Total: every input yields an `McpOutcome`, never an exception, because the caller's only
    correct reaction to an unreadable body is the same as to a rejected one — stop, loudly.
    """
    if status in _RETRYABLE_STATUSES or status >= 500:
        return McpOutcome(OutcomeKind.UNAVAILABLE, f"the gated MCP path answered HTTP {status}")
    if status != 200:
        return McpOutcome(OutcomeKind.FAILED, f"the gated MCP path answered HTTP {status}")

    try:
        payload = decode_body(body)
    except EnvelopeError as exc:
        return McpOutcome(OutcomeKind.FAILED, str(exc))

    error = payload.get("error")
    result = payload.get("result")
    if error is not None and result is not None:
        # JSON-RPC 2.0 permits exactly one of the two. Both present means the peer is not speaking
        # the protocol, and guessing which half to believe is how a refusal gets read as a success.
        return McpOutcome(OutcomeKind.FAILED, "the response carries both 'result' and 'error'")
    if error is not None:
        return McpOutcome(OutcomeKind.FAILED, f"json-rpc error: {_render(error)}")
    if result is None:
        return McpOutcome(OutcomeKind.FAILED, "the response carries neither 'result' nor 'error'")
    if not isinstance(result, dict):
        return McpOutcome(OutcomeKind.FAILED, f"'result' is {type(result).__name__}, expected an object")

    fields = cast("dict[str, object]", result)
    text = _content_text(fields.get("content"))
    raw_structured = fields.get("structuredContent")
    structured = cast("dict[str, object]", raw_structured) if isinstance(raw_structured, dict) else None
    if fields.get("isError") is True:
        kind = OutcomeKind.NOT_FOUND if _reads_as_absent(structured, text) else OutcomeKind.REFUSED
        return McpOutcome(kind, text or "the tool reported an error with no message", structured=structured)
    return McpOutcome(OutcomeKind.OK, "", text, structured)


def decode_body(body: bytes) -> dict[str, object]:
    """The JSON-RPC object inside a response body, whether it arrived bare or as an SSE stream."""
    raw = _sse_payload(body) if _looks_like_sse(body) else body
    try:
        decoded: object = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EnvelopeError(f"the response body is not JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise EnvelopeError("the response body is not a JSON object")
    return cast("dict[str, object]", decoded)


def _looks_like_sse(body: bytes) -> bool:
    return any(line.startswith((b"event:", b"data:")) for line in body.splitlines())


def _sse_payload(body: bytes) -> bytes:
    data = [line.removeprefix(b"data:").strip() for line in body.splitlines() if line.startswith(b"data:")]
    if not data:
        raise EnvelopeError("the response is an SSE stream carrying no data event")
    return data[-1]


def _content_text(content: object) -> str:
    """The human-readable text of an MCP content array.

    Non-text blocks (images, resource links) are skipped rather than rendered: this component
    writes markdown and reads it back, so a non-text block in a response means the surface returned
    something this processor has no use for, and inventing a placeholder for it would put invented
    text into a refusal message an operator is meant to act on.
    """
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in cast("list[object]", content):
        if isinstance(block, dict):
            value = cast("dict[str, object]", block).get("text")
            if isinstance(value, str):
                parts.append(value)
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


def _reads_as_absent(structured: dict[str, object] | None, text: str) -> bool:
    """Whether a tool error says the note is not there.

    **The structured reason decides whenever there is one, and the prose is not consulted then.**
    A refusal carrying a reason has already answered the question; falling through to the markers
    after reading one would let a refusal whose *cause* is something else be read as absence
    because its message happens to contain "not found" — the direction that puts a create in front
    of a note that exists.
    """
    reason = _error_reason(structured)
    if reason is not None:
        return reason in _ABSENT_REASONS
    lowered = text.lower()
    return any(marker in lowered for marker in _NOT_FOUND_MARKERS)


def _error_reason(structured: dict[str, object] | None) -> str | None:
    """`structuredContent.error.data.reason`, when the whole path is there and is a string."""
    if structured is None:
        return None
    error = structured.get("error")
    if not isinstance(error, dict):
        return None
    data = cast("dict[str, object]", error).get("data")
    if not isinstance(data, dict):
        return None
    reason = cast("dict[str, object]", data).get("reason")
    return reason if isinstance(reason, str) else None


def _render(error: object) -> str:
    if isinstance(error, dict):
        fields = cast("dict[str, object]", error)
        return f"{fields.get('code')} {fields.get('message')}"
    return str(error)
