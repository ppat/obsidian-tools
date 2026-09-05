"""Tables for `batch_processor/envelope.py`.

The whole module exists because a write-gate refusal is HTTP 200 with the error inside the JSON-RPC
envelope, so every case below is stated as a literal body: what makes a table the right shape here
is that the inputs are exactly what a server puts on the wire, and no fixture stands between the
assertion and that text.
"""

from __future__ import annotations

import json

import pytest

from obsidian_tools.batch_processor.envelope import (
    EnvelopeError,
    OutcomeKind,
    classify_response,
    decode_body,
)


def _result(content: str, *, is_error: bool = False) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": content}], "isError": is_error}}
    ).encode("utf-8")


# --- the gotcha: a refusal is a 200 -------------------------------------------------------------


def test_a_refusal_inside_a_200_is_a_refusal_not_a_success() -> None:
    """The single most important assertion in this file. Red if `isError` is ever ignored — at
    which point every gate refusal in the system reads as an applied write, and `batch-processor`
    reports success for chunks it never wrote."""
    outcome = classify_response(200, _result("path_forbidden: 05-raw/x.md is outside the active scope", is_error=True))

    assert outcome.kind is OutcomeKind.REFUSED
    assert "path_forbidden" in outcome.detail


def test_a_refusal_is_never_retryable() -> None:
    """Red if a refusal is ever classified retryable: the scope that produced it is static server
    configuration, so retrying turns a loud permanent refusal into a quiet unbounded loop."""
    assert classify_response(200, _result("path_forbidden", is_error=True)).retryable is False


def test_a_successful_result_carries_the_tools_text() -> None:
    """Red if the content array is ever dropped — `read_note` returns exactly this text, and the
    staleness hash is computed over it."""
    outcome = classify_response(200, _result("# a note\n"))

    assert outcome.kind is OutcomeKind.OK
    assert outcome.text == "# a note\n"


def test_several_text_blocks_are_joined_rather_than_the_first_one_taken() -> None:
    """Red if only the first content block survives: a note returned in two blocks would be hashed
    against half its own bytes and every modify of it would reject as stale."""
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]},
        }
    ).encode("utf-8")

    assert classify_response(200, body).text == "one\ntwo"


# --- absence, which is a fact about the vault rather than about authority ------------------------


@pytest.mark.parametrize(
    "message",
    [
        "File not found: 10-areas/x.md",
        "10-areas/x.md does not exist",
        "no such file or directory",
        "Error 404 while reading the note",
        "FILE NOT FOUND",
    ],
)
def test_a_refusal_saying_the_note_is_absent_is_classified_as_absent(message: str) -> None:
    """ADR-0048's create branch turns on absence. Red if any of these came back as a plain refusal:
    a create would then be dead-lettered for a target that was legitimately not there."""
    assert classify_response(200, _result(message, is_error=True)).kind is OutcomeKind.NOT_FOUND


@pytest.mark.parametrize(
    "message",
    [
        "path_forbidden: outside the active write scope",
        "the tool 'obsidian_read' is not available on this key",
        "permission denied",
    ],
)
def test_a_refusal_that_does_not_say_absent_stays_a_refusal(message: str) -> None:
    """The other direction, and the one that matters more: red if the absence markers ever widened
    into a catch-all, because a refusal misread as absence turns a create's pre-flight from "the
    target is not there" into "I was not allowed to look", and the create then overwrites."""
    assert classify_response(200, _result(message, is_error=True)).kind is OutcomeKind.REFUSED


# --- the failure kinds are not interchangeable ---------------------------------------------------


def test_a_json_rpc_error_is_a_permanent_failure() -> None:
    """Red if a protocol error is retried: a malformed call or a missing tool fails identically
    every time, and the retry budget is spent proving it."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "method not found"}})

    outcome = classify_response(200, body.encode("utf-8"))

    assert outcome.kind is OutcomeKind.FAILED
    assert outcome.retryable is False
    assert "-32601" in outcome.detail


def test_a_response_carrying_both_result_and_error_is_refused_rather_than_guessed_at() -> None:
    """JSON-RPC permits exactly one. Red if either half is believed over the other — the shape is
    unspecified, and picking one is how a refusal gets read as a success."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": []}, "error": {"code": 1, "message": "no"}})

    assert classify_response(200, body.encode("utf-8")).kind is OutcomeKind.FAILED


def test_a_response_carrying_neither_result_nor_error_is_a_failure() -> None:
    """Red if an empty envelope is treated as success: nothing was applied and nothing said so."""
    assert classify_response(200, b'{"jsonrpc":"2.0","id":1}').kind is OutcomeKind.FAILED


def test_a_body_that_is_not_json_is_a_failure_not_a_crash() -> None:
    """Red if an HTML error page from a proxy raises out of the classifier instead of becoming an
    outcome — `classify_response` is the one place the caller is entitled to assume totality."""
    assert classify_response(200, b"<html>502 Bad Gateway</html>").kind is OutcomeKind.FAILED


@pytest.mark.parametrize("status", [500, 502, 503, 408, 429])
def test_the_statuses_that_mean_ask_again_are_the_only_retryable_ones(status: int) -> None:
    """Red if any of these stops being retryable: a restarting MCP pod would then dead-letter every
    chunk in flight instead of waiting for it."""
    assert classify_response(status, b"").retryable is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405])
def test_a_permanent_status_is_not_retried(status: int) -> None:
    """Red if a 401/403 is retried: the gateway key is wrong or the handle is blocked, and neither
    changes by asking again — the retry budget would be spent on a shut door."""
    assert classify_response(status, b"").retryable is False


# --- the two body shapes -------------------------------------------------------------------------


def test_an_sse_framed_response_decodes_to_the_same_object_as_a_bare_one() -> None:
    """Streamable HTTP lets the server answer either way. Red if only bare JSON parses: a server
    that prefers SSE would have every one of its responses classified as an unreadable body, and a
    working system would look permanently broken."""
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "hi"}]}}
    sse = f"event: message\ndata: {json.dumps(payload)}\n\n".encode()

    assert decode_body(sse) == payload
    assert classify_response(200, sse).text == "hi"


def test_the_last_data_event_is_the_response() -> None:
    """A stream may carry progress notifications before the answer. Red if the first event is taken
    — the response would be a progress notification and would classify as carrying neither result
    nor error."""
    sse = b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'

    assert decode_body(sse) == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_an_sse_stream_with_no_data_event_is_refused() -> None:
    """Red if an empty stream silently decoded as something — there is no response in it."""
    with pytest.raises(EnvelopeError):
        decode_body(b"event: message\n\n")


def test_a_json_array_body_is_not_a_response() -> None:
    """A JSON-RPC batch response is an array, and this client never sends a batch. Red if one were
    accepted: `payload.get` on a list is a `TypeError` escaping the classifier."""
    with pytest.raises(EnvelopeError):
        decode_body(b"[]")
