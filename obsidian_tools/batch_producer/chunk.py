"""The batch stream's message format: one chunk, on the wire. Pure — no git, no NATS, no clock.

A chunk is one message on the batch stream and **the transaction and redelivery unit — atomicity is
per chunk, never per batch** (DESIGN.md's Glossary; ADR-0022). `batch-processor` consumes what this
module produces, so everything here is an interface, not an implementation detail: the processor
imports `decode_chunk` and the `ChunkTarget` vocabulary rather than parsing the payload itself.

## The payload

JSON, UTF-8, keys sorted:

    {
      "format":       "batch-chunk/1",
      "batch_id":     "9f2c1d…",          one lowercase-alphanumeric NATS subject token
      "chunk_index":  0,                   0-based position within the batch
      "chunk_count":  12,                  how many chunks the batch has in total
      "produced_at":  "2026-09-05T…+00:00",
      "patch":        "diff --git …",      unified diff text, whole files only
      "targets":      [ {"path": …, "operation": …, "base_sha256": …}, … ]
    }

`targets` is ADR-0048's staleness payload; `staleness.py` owns its meaning and its rules.

## Identity and ordering

**A chunk is identified by the pair `(batch_id, chunk_index)`** and by nothing else. `chunk_id()`
renders that pair as a string for logs and dedup keys, and is deliberately a function rather than a
carried field: a stored identifier can disagree with the pair it is derived from, and then two
sources of truth exist for one fact.

**Ordering is the stream's, not the payload's.** ADR-0022 chose one FIFO stream precisely so a
producer can express dependency by ordering — rename in chunk N, relink in chunk N+1 — so what
orders the work is the sequence the broker assigns, and the producer's job is to make that sequence
match its own. That is why the producer publishes chunks strictly one at a time, awaiting each
acknowledgement before sending the next (`nats_client.py`); concurrent publishes would let the
broker order two chunks by whichever ack raced. `chunk_index` therefore does not *establish* the
order — it lets a consumer audit that the order held, and `chunk_count` lets it notice a batch cut
short, which is otherwise invisible (ADR-0048: a short batch leaves an edit not yet made rather than
a wrong one, so noticing it is the whole remedy).

## Two invariants, and why they are not stylistic

- **A path appears in at most one chunk of a batch.** This is forced by ADR-0048, not chosen. A
  chunk records the pre-image hash of every path it touches; if a later chunk touched the same path,
  the earlier chunk's writes would already have moved it, and the later chunk would reject against
  a hash the batch itself invalidated. That is verbatim the first observation ADR-0048 names as
  falsifying per-file hashing ("rejections dominated by files an earlier chunk of the *same* batch
  changed"), so a producer that emits it manufactures its own decision record's failure case.
  `validate_batch` refuses it.
- **A file's diff is never split across chunks.** Half a file's hunks applied and the rest
  redelivered is exactly the half-apply the whole-chunk pre-flight exists to make impossible.
  `chunking.py` treats one file's diff as the indivisible atom for the same reason.

## Strictness

`decode_chunk` rejects an unknown `format`, a wrong-typed field, *and* an unknown key. Ignoring
unknown keys would be the more forgiving choice and is the wrong one here: producer and consumer
ship from this one repository in one image and are deployed together, so a key the consumer does not
recognise is a mistake rather than a version skew, and the cost of strictness is a coordinated
rollout that already happens anyway. An additive change to the payload bumps `CHUNK_FORMAT`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

from obsidian_tools.batch_producer.staleness import (
    ChunkTarget,
    ChunkTargetError,
    TargetOperation,
    validate_target,
)

__all__ = [
    "CHUNK_FORMAT",
    "Chunk",
    "ChunkFormatError",
    "ChunkTarget",
    "TargetOperation",
    "chunk_id",
    "chunk_subject",
    "decode_chunk",
    "encode_chunk",
    "validate_batch",
    "validate_chunk",
]

CHUNK_FORMAT = "batch-chunk/1"

# A batch id becomes the trailing token of the subject a chunk is published to, so it is constrained
# to what a NATS subject token may hold with no escaping: `.` would split it into two tokens, and
# `*`/`>` are the wildcards a subscriber matches with. ADR-0047 grants the producer `batch.>` and
# notes the trailing token is deliberately left free for routing; one token per batch is what takes
# it up, so an operator can select a single batch's chunks at the subject level without parsing any
# payload. The upper bound is arbitrary but finite: a subject is not a place to put unbounded text.
_MAX_BATCH_ID_LENGTH = 64
_BATCH_ID_ALPHABET = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")

_PAYLOAD_KEYS = frozenset({"format", "batch_id", "chunk_index", "chunk_count", "produced_at", "patch", "targets"})
_TARGET_KEYS = frozenset({"path", "operation", "base_sha256"})


class ChunkFormatError(ValueError):
    """A chunk that is not well-formed, in either direction: `encode_chunk` refuses to put one on
    the wire, and `decode_chunk` refuses to hand one to a consumer."""


@dataclass(frozen=True, slots=True)
class Chunk:
    """One message on the batch stream."""

    batch_id: str
    chunk_index: int
    chunk_count: int
    produced_at: str
    patch: str
    targets: tuple[ChunkTarget, ...]


def chunk_id(chunk: Chunk) -> str:
    """The chunk's identity as one string. Zero-padded so a lexical sort of a log's chunk ids
    matches the numeric order for any batch this producer can emit."""
    return f"{chunk.batch_id}/{chunk.chunk_index:06d}"


def chunk_subject(subject_prefix: str, batch_id: str) -> str:
    return f"{subject_prefix}.{batch_id}"


def is_valid_batch_id(batch_id: str) -> bool:
    return 0 < len(batch_id) <= _MAX_BATCH_ID_LENGTH and all(character in _BATCH_ID_ALPHABET for character in batch_id)


def validate_chunk(chunk: Chunk) -> tuple[str, ...]:
    """Every reason `chunk` is not well-formed, as human-readable strings. Empty means valid."""
    problems: list[str] = []

    if not is_valid_batch_id(chunk.batch_id):
        problems.append(
            f"batch_id {chunk.batch_id!r} is not 1-{_MAX_BATCH_ID_LENGTH} lowercase alphanumeric characters"
        )
    if chunk.chunk_count < 1:
        problems.append(f"chunk_count {chunk.chunk_count} is not positive")
    if not 0 <= chunk.chunk_index < chunk.chunk_count:
        problems.append(f"chunk_index {chunk.chunk_index} is outside [0, {chunk.chunk_count})")
    if not chunk.patch:
        problems.append("patch is empty")
    if not chunk.produced_at:
        problems.append("produced_at is empty")
    if not chunk.targets:
        problems.append("chunk touches no target path")

    seen: set[str] = set()
    for target in chunk.targets:
        problems.extend(validate_target(target))
        if target.path in seen:
            problems.append(f"{target.path!r}: appears as a target more than once in this chunk")
        seen.add(target.path)

    return tuple(problems)


def validate_batch(chunks: tuple[Chunk, ...]) -> tuple[str, ...]:
    """Every reason `chunks` is not a well-formed batch — the cross-chunk invariants no single
    chunk can check about itself. Empty means valid."""
    problems: list[str] = []
    if not chunks:
        return ("a batch has no chunks",)

    for chunk in chunks:
        problems.extend(validate_chunk(chunk))

    batch_ids = {chunk.batch_id for chunk in chunks}
    if len(batch_ids) != 1:
        problems.append(f"chunks disagree about batch_id: {sorted(batch_ids)}")

    declared_counts = {chunk.chunk_count for chunk in chunks}
    if declared_counts != {len(chunks)}:
        problems.append(f"chunk_count {sorted(declared_counts)} does not match the {len(chunks)} chunks present")

    indices = sorted(chunk.chunk_index for chunk in chunks)
    if indices != list(range(len(chunks))):
        problems.append(f"chunk indices {indices} are not 0..{len(chunks) - 1} exactly once each")

    # The ADR-0048 invariant: see this module's docstring for why a repeated path is the producer
    # manufacturing its own staleness failure rather than a tidiness complaint.
    owner_of: dict[str, int] = {}
    for chunk in chunks:
        for target in chunk.targets:
            previous = owner_of.get(target.path)
            if previous is not None:
                problems.append(
                    f"{target.path!r}: targeted by chunk {previous} and chunk {chunk.chunk_index}; "
                    "a path may be touched by at most one chunk of a batch"
                )
            else:
                owner_of[target.path] = chunk.chunk_index

    return tuple(problems)


def encode_chunk(chunk: Chunk) -> bytes:
    """Serialize `chunk` to the bytes published on the batch stream.

    Validates first, so no ill-formed chunk can reach the wire through any path — this is the only
    encoder, which is what makes that a property rather than a convention. Keys are sorted and
    separators are tight so two chunks differing only in content produce byte differences an
    operator can read; `ensure_ascii=False` keeps non-ASCII vault content legible in
    `nats stream view` rather than rendering every accented character as an escape.
    """
    problems = validate_chunk(chunk)
    if problems:
        raise ChunkFormatError(f"refusing to encode a malformed chunk: {'; '.join(problems)}")

    payload = {
        "format": CHUNK_FORMAT,
        "batch_id": chunk.batch_id,
        "chunk_index": chunk.chunk_index,
        "chunk_count": chunk.chunk_count,
        "produced_at": chunk.produced_at,
        "patch": chunk.patch,
        "targets": [
            {"path": target.path, "operation": str(target.operation), "base_sha256": target.base_sha256}
            for target in chunk.targets
        ],
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode_chunk(raw: bytes) -> Chunk:
    """Parse a batch-stream message body. Raises `ChunkFormatError` on anything malformed.

    Total in the sense that matters to a consumer: every input either yields a chunk that has
    already passed `validate_chunk`, or raises this module's own exception type — never a
    `KeyError`, a `TypeError`, or a `json` exception the caller would have to know to catch.
    """
    try:
        decoded: object = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ChunkFormatError(f"chunk body is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ChunkFormatError("chunk body is not a JSON object")
    payload = cast("dict[str, object]", decoded)

    _reject_unknown_keys(payload, _PAYLOAD_KEYS, "chunk")
    if payload.get("format") != CHUNK_FORMAT:
        raise ChunkFormatError(f"unknown chunk format {payload.get('format')!r}, expected {CHUNK_FORMAT!r}")

    chunk = Chunk(
        batch_id=_require_str(payload, "batch_id"),
        chunk_index=_require_int(payload, "chunk_index"),
        chunk_count=_require_int(payload, "chunk_count"),
        produced_at=_require_str(payload, "produced_at"),
        patch=_require_str(payload, "patch"),
        targets=_decode_targets(payload.get("targets")),
    )
    problems = validate_chunk(chunk)
    if problems:
        raise ChunkFormatError(f"chunk is malformed: {'; '.join(problems)}")
    return chunk


def _decode_targets(raw_targets: object) -> tuple[ChunkTarget, ...]:
    if not isinstance(raw_targets, list):
        raise ChunkFormatError("'targets' is not a JSON array")
    targets: list[ChunkTarget] = []
    for entry in cast("list[object]", raw_targets):
        if not isinstance(entry, dict):
            raise ChunkFormatError("a 'targets' entry is not a JSON object")
        target = cast("dict[str, object]", entry)
        _reject_unknown_keys(target, _TARGET_KEYS, "target")
        operation = _require_str(target, "operation")
        if operation not in tuple(TargetOperation):
            raise ChunkFormatError(f"unknown target operation {operation!r}")
        base_sha256 = target.get("base_sha256")
        if base_sha256 is not None and not isinstance(base_sha256, str):
            raise ChunkFormatError(f"'base_sha256' is {type(base_sha256).__name__}, expected a string or null")
        try:
            targets.append(
                ChunkTarget(
                    path=_require_str(target, "path"),
                    operation=TargetOperation(operation),
                    base_sha256=base_sha256,
                )
            )
        except ChunkTargetError as exc:
            raise ChunkFormatError(str(exc)) from exc
    return tuple(targets)


def _reject_unknown_keys(payload: dict[str, object], known: frozenset[str], what: str) -> None:
    unknown = sorted(set(payload) - known)
    if unknown:
        raise ChunkFormatError(f"{what} carries unknown keys: {unknown}")


def _require_str(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ChunkFormatError(f"{key!r} is {type(value).__name__}, expected a string")
    return value


def _require_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    # `bool` is a subclass of `int`, and `True` would otherwise silently decode as chunk_index 1.
    if not isinstance(value, int) or isinstance(value, bool):
        raise ChunkFormatError(f"{key!r} is {type(value).__name__}, expected an integer")
    return value
