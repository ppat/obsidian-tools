"""Tests for `obsidian_tools/batch_producer/chunk.py` — the batch stream's wire format, as pure
functions over literals.

This file is the message format's specification in executable form. `batch-processor` is built
against it by a different agent, so the payload's exact key set and value shapes are asserted
literally here rather than round-tripped only through this module's own encoder — a round-trip test
alone would still pass if both halves changed together, which is precisely the failure a second
component would discover in production instead.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from obsidian_tools.batch_producer.chunk import (
    CHUNK_FORMAT,
    Chunk,
    ChunkFormatError,
    chunk_id,
    chunk_subject,
    decode_chunk,
    encode_chunk,
    validate_batch,
    validate_chunk,
)
from obsidian_tools.batch_producer.staleness import ChunkTarget, TargetOperation

_HASH = "0123456789abcdef" * 4
_OTHER_HASH = "fedcba9876543210" * 4
_BATCH = "9f2c1d3e4a5b6c7d8e9f0a1b2c3d4e5f"


def a_chunk(
    *,
    batch_id: str = _BATCH,
    chunk_index: int = 0,
    chunk_count: int = 1,
    patch: str = "diff --git a/x.md b/x.md\n",
    targets: tuple[ChunkTarget, ...] | None = None,
) -> Chunk:
    return Chunk(
        batch_id=batch_id,
        chunk_index=chunk_index,
        chunk_count=chunk_count,
        produced_at="2026-09-05T12:00:00+00:00",
        patch=patch,
        targets=targets if targets is not None else (ChunkTarget("05-raw/x.md", TargetOperation.CREATE, None),),
    )


# --- the payload, asserted literally -------------------------------------------------------------


def test_the_encoded_payload_is_exactly_this_shape() -> None:
    """The interface, pinned. Red on any change to a key name, a key set, or a value's JSON type —
    each of which is a change `batch-processor` must be rebuilt for, and none of which a round-trip
    through this module's own decoder would notice."""
    chunk = a_chunk(
        chunk_index=1,
        chunk_count=3,
        targets=(
            ChunkTarget("05-raw/new.md", TargetOperation.CREATE, None),
            ChunkTarget("10-areas/x.md", TargetOperation.MODIFY, _HASH),
            ChunkTarget("10-areas/gone.md", TargetOperation.DELETE, _OTHER_HASH),
        ),
    )

    assert json.loads(encode_chunk(chunk)) == {
        "format": "batch-chunk/1",
        "batch_id": _BATCH,
        "chunk_index": 1,
        "chunk_count": 3,
        "produced_at": "2026-09-05T12:00:00+00:00",
        "patch": "diff --git a/x.md b/x.md\n",
        "targets": [
            {"path": "05-raw/new.md", "operation": "create", "base_sha256": None},
            {"path": "10-areas/x.md", "operation": "modify", "base_sha256": _HASH},
            {"path": "10-areas/gone.md", "operation": "delete", "base_sha256": _OTHER_HASH},
        ],
    }


def test_the_encoding_is_utf8_json_with_sorted_keys() -> None:
    """Red if the encoder's determinism is lost. Two chunks differing only in content must differ
    only there on the wire, or an operator diffing two `nats stream view` outputs reads noise."""
    raw = encode_chunk(a_chunk())

    assert raw.decode("utf-8").startswith('{"batch_id":')


def test_non_ascii_content_is_carried_literally_rather_than_escaped() -> None:
    """Red if `ensure_ascii` is turned back on: every accented character in a vault note would
    become a `\\uXXXX` escape, which is legible to nothing an operator would use to read the queue."""
    raw = encode_chunk(a_chunk(patch="+ café\n"))

    assert "café" in raw.decode("utf-8")


def test_the_format_token_is_carried_and_is_what_a_consumer_gates_on() -> None:
    """Red if the discriminator is dropped, which would leave a consumer no way to refuse a payload
    it does not understand except by guessing from the key set."""
    assert json.loads(encode_chunk(a_chunk()))["format"] == CHUNK_FORMAT


# --- identity and subject ------------------------------------------------------------------------


def test_chunk_id_renders_the_batch_id_and_index_pair() -> None:
    """Red if identity is ever derived from anything but `(batch_id, chunk_index)` — the pair is the
    identity, and a second source for it is a second thing that can disagree."""
    assert chunk_id(a_chunk(chunk_index=7, chunk_count=9)) == f"{_BATCH}/000007"


def test_chunk_ids_sort_lexically_into_numeric_order() -> None:
    """Red if the zero-padding is dropped: chunk 10 would sort before chunk 2 in every log query."""
    ids = [chunk_id(a_chunk(chunk_index=index, chunk_count=20)) for index in (2, 10, 1)]

    assert sorted(ids) == [chunk_id(a_chunk(chunk_index=index, chunk_count=20)) for index in (1, 2, 10)]


def test_the_subject_is_the_prefix_plus_the_batch_id_as_one_token() -> None:
    """ADR-0047 grants `batch.>` and leaves the trailing token free for routing. Red if the batch id
    ever stops being exactly one token, which would put chunks on subjects the grant does not cover
    in the way the record describes."""
    subject = chunk_subject("batch", _BATCH)

    assert subject == f"batch.{_BATCH}"
    assert subject.count(".") == 1


@pytest.mark.parametrize("batch_id", ["has.a.dot", "has>wildcard", "has*star", "UPPER", "has space", "", "a" * 65])
def test_a_batch_id_that_is_not_one_subject_token_is_refused(batch_id: str) -> None:
    """Red if any of these reaches the wire. A `.` silently splits the subject in two; `*`/`>` are
    subscriber wildcards; the rest simply are not what this producer generates, so accepting them
    would mean the validator is not describing the thing it validates."""
    assert validate_chunk(a_chunk(batch_id=batch_id)) != ()


# --- per-chunk validation ------------------------------------------------------------------------


def test_a_well_formed_chunk_validates_clean() -> None:
    """The positive control that keeps every refusal below falsifiable."""
    assert validate_chunk(a_chunk()) == ()


@pytest.mark.parametrize(
    ("chunk", "reason"),
    [
        (a_chunk(chunk_count=0), "chunk_count"),
        (a_chunk(chunk_index=3, chunk_count=3), "chunk_index"),
        (a_chunk(chunk_index=-1), "chunk_index"),
        (a_chunk(patch=""), "patch"),
        (a_chunk(targets=()), "target"),
    ],
)
def test_a_malformed_chunk_is_refused_naming_the_field(chunk: Chunk, reason: str) -> None:
    """Red if any of these is accepted. Each is a chunk a consumer cannot act on: an out-of-range
    index breaks the ordering audit, an empty patch is a message that writes nothing, and a chunk
    with no targets is one whose staleness pre-flight checks nothing at all."""
    problems = validate_chunk(chunk)

    assert problems != ()
    assert any(reason in problem for problem in problems)


def test_a_path_repeated_within_one_chunk_is_refused() -> None:
    """Red if a chunk may write the same path twice. The pre-flight reads each target once, so a
    repeated path means the second write lands on content the first already moved — the half-apply
    the whole-chunk check exists to prevent, smuggled inside a single chunk."""
    chunk = a_chunk(
        targets=(
            ChunkTarget("10-areas/x.md", TargetOperation.MODIFY, _HASH),
            ChunkTarget("10-areas/x.md", TargetOperation.DELETE, _OTHER_HASH),
        )
    )

    assert any("more than once" in problem for problem in validate_chunk(chunk))


def test_encoding_a_malformed_chunk_raises_rather_than_emitting_it() -> None:
    """`encode_chunk` is the only encoder, which is what makes "no malformed chunk reaches the wire"
    a property rather than a convention. Red if validation is ever moved out of it."""
    with pytest.raises(ChunkFormatError):
        encode_chunk(a_chunk(patch=""))


# --- batch-level validation: the ADR-0048 invariant ----------------------------------------------


def test_a_path_touched_by_two_chunks_of_one_batch_is_refused() -> None:
    """The invariant ADR-0048 forces. A later chunk's recorded hash would be invalidated by an
    earlier chunk of its own batch — verbatim the first observation that record names as falsifying
    per-file hashing. Red if a producer may emit a batch that manufactures its own failure case."""
    chunks = (
        a_chunk(chunk_index=0, chunk_count=2, targets=(ChunkTarget("10-areas/x.md", TargetOperation.MODIFY, _HASH),)),
        a_chunk(chunk_index=1, chunk_count=2, targets=(ChunkTarget("10-areas/x.md", TargetOperation.DELETE, _HASH),)),
    )

    problems = validate_batch(chunks)

    assert any("at most one chunk" in problem for problem in problems)


def test_a_batch_whose_declared_count_disagrees_with_its_chunks_is_refused() -> None:
    """Red if `chunk_count` may lie. It is the only way a consumer notices a batch cut short, so a
    count that does not describe the batch removes the one detection ADR-0048 relies on."""
    chunks = (a_chunk(chunk_index=0, chunk_count=5),)

    assert any("does not match" in problem for problem in validate_batch(chunks))


def test_a_batch_with_a_gap_or_a_duplicate_index_is_refused() -> None:
    """Red if indices need not be exactly 0..n-1. A gap is an unnoticed lost chunk; a duplicate
    makes two different chunks share one identity."""
    with_gap = (a_chunk(chunk_index=0, chunk_count=2), a_chunk(chunk_index=2, chunk_count=2))

    assert any("0..1" in problem for problem in validate_batch(with_gap))


def test_chunks_disagreeing_about_the_batch_id_are_refused() -> None:
    """Red if one batch's chunks may carry different ids — they would be published to two different
    subjects and stop being one batch at all."""
    chunks = (
        a_chunk(chunk_index=0, chunk_count=2),
        a_chunk(chunk_index=1, chunk_count=2, batch_id="b" * 32),
    )

    assert any("disagree about batch_id" in problem for problem in validate_batch(chunks))


def test_an_empty_batch_is_refused() -> None:
    """Red if zero chunks validates. There is no such thing as an empty batch; a caller with nothing
    staged has nothing to publish and never reaches here."""
    assert validate_batch(()) != ()


def test_a_well_formed_multi_chunk_batch_validates_clean() -> None:
    """The positive control for the batch rules."""
    chunks = (
        a_chunk(chunk_index=0, chunk_count=2, targets=(ChunkTarget("a.md", TargetOperation.CREATE, None),)),
        a_chunk(chunk_index=1, chunk_count=2, targets=(ChunkTarget("b.md", TargetOperation.CREATE, None),)),
    )

    assert validate_batch(chunks) == ()


# --- decode: what the consumer actually calls ----------------------------------------------------


def test_a_chunk_survives_encode_then_decode_unchanged() -> None:
    """Red if the two halves of the codec ever disagree about any field."""
    chunk = a_chunk(
        chunk_index=1,
        chunk_count=2,
        targets=(
            ChunkTarget("05-raw/new.md", TargetOperation.CREATE, None),
            ChunkTarget("10-areas/x.md", TargetOperation.MODIFY, _HASH),
        ),
    )

    assert decode_chunk(encode_chunk(chunk)) == chunk


def test_decoding_an_unknown_format_is_refused() -> None:
    """The whole point of carrying the discriminator. Red if a payload from a future producer is
    parsed on a best-effort basis instead of refused."""
    payload = json.loads(encode_chunk(a_chunk()))
    payload["format"] = "batch-chunk/2"

    with pytest.raises(ChunkFormatError, match="unknown chunk format"):
        decode_chunk(json.dumps(payload).encode("utf-8"))


def test_decoding_an_unknown_key_is_refused() -> None:
    """Producer and consumer ship together from one repository, so an unrecognised key is a mistake
    rather than version skew. Red if unknown keys are silently ignored, which is how a field the
    sender believed it was carrying gets dropped without a trace."""
    payload = json.loads(encode_chunk(a_chunk()))
    payload["urgency"] = "high"

    with pytest.raises(ChunkFormatError, match="unknown keys"):
        decode_chunk(json.dumps(payload).encode("utf-8"))


def _index_as_string(payload: dict[str, Any]) -> None:
    payload["chunk_index"] = "0"


def _patch_as_number(payload: dict[str, Any]) -> None:
    payload["patch"] = 3


def _targets_as_object(payload: dict[str, Any]) -> None:
    payload["targets"] = {}


def _missing_batch_id(payload: dict[str, Any]) -> None:
    del payload["batch_id"]


def _unknown_operation(payload: dict[str, Any]) -> None:
    payload["targets"][0]["operation"] = "upsert"


def _hash_as_number(payload: dict[str, Any]) -> None:
    payload["targets"][0]["base_sha256"] = 5


def _create_carrying_a_hash(payload: dict[str, Any]) -> None:
    payload["targets"][0]["base_sha256"] = _HASH


@pytest.mark.parametrize(
    "mutate",
    [
        _index_as_string,
        _patch_as_number,
        _targets_as_object,
        _missing_batch_id,
        _unknown_operation,
        _hash_as_number,
        _create_carrying_a_hash,
    ],
)
def test_a_wrong_typed_or_wrong_valued_field_is_refused(mutate: Callable[[dict[str, Any]], None]) -> None:
    """Red if any of these reaches a consumer as a `Chunk`."""
    payload = json.loads(encode_chunk(a_chunk()))
    mutate(payload)

    with pytest.raises(ChunkFormatError):
        decode_chunk(json.dumps(payload).encode("utf-8"))


def test_a_boolean_where_an_integer_belongs_is_refused() -> None:
    """`True` is an `int` in Python, so without an explicit guard it decodes as chunk_index 1
    silently. The surrounding chunk declares three chunks *deliberately*: index 1 is legal there, so
    nothing but the guard itself can reject this. On a single-chunk payload the range check would
    catch it anyway and this test would pass while proving nothing — which is exactly what it did
    before a mutation check exposed it."""
    payload = json.loads(encode_chunk(a_chunk(chunk_index=1, chunk_count=3)))
    payload["chunk_index"] = True

    with pytest.raises(ChunkFormatError):
        decode_chunk(json.dumps(payload).encode("utf-8"))


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b'"a string"', b"\xff\xfe"])
def test_a_body_that_is_not_a_json_object_is_refused_as_this_modules_own_error(raw: bytes) -> None:
    """Red if any of these escapes as a `json` or `Unicode` exception. A consumer catches one
    exception type; anything else reaches it as an unhandled crash rather than a bad message."""
    with pytest.raises(ChunkFormatError):
        decode_chunk(raw)


def test_a_decoded_chunk_has_already_passed_validation() -> None:
    """Red if `decode_chunk` returns a chunk the validator would reject — a consumer would then have
    to re-validate everything this module already checked, or not, and not is what happens."""
    payload = json.loads(encode_chunk(a_chunk()))
    payload["chunk_index"] = 9

    with pytest.raises(ChunkFormatError, match="malformed"):
        decode_chunk(json.dumps(payload).encode("utf-8"))
