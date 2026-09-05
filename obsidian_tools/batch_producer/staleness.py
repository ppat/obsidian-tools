"""ADR-0048's staleness payload: what a chunk records per target path, and what a consumer does
with it. Pure — no git, no NATS, no filesystem.

This module and `chunk.py` are the interface between the producer and `batch-processor`. The
processor imports these definitions rather than re-deriving them, so "what does `base_sha256` mean"
has one answer rather than two that agree until they don't.

**The measure.** ADR-0048 fixes staleness per file, by content hash over the paths a chunk touches,
never the patch's base commit against repo head. So a target carries the hash of the content its
patch was generated against, and the processor reads each target and compares *before any write in
the chunk lands* — any mismatch rejects the whole chunk with nothing applied.

**The operation decides which check runs**, and the two do not overlap; ADR-0048 states the
composition as create → existence check, modify → hash check:

| `operation` | `base_sha256` | What the processor checks before applying |
| --- | --- | --- |
| `create` | always `None` | the target must not exist — a create has no prior content to hash |
| `modify` | always set | read the target; its `content_sha256` must equal this |
| `delete` | always set | read the target; its `content_sha256` must equal this |

`ChunkTarget` enforces that pairing at construction, so a `create` carrying a hash or a `modify`
missing one cannot be built at all — not merely rejected on the way to the wire. The pairing is the
whole reason `operation` is carried: without it a `None` hash would be ambiguous between "this is a
create" and "the producer failed to look one up".

**Renames are two targets, not a fourth operation.** Git reports a rename as one entry naming both
paths, but the pre-flight it needs is exactly delete-the-old plus create-the-new: the old path must
still hold the content the patch was generated against, and the new path must not exist. Modelling
it that way keeps the processor's rule at two branches instead of three, and the patch text still
carries the rename intact — `targets` is the pre-flight, never the instruction.

**Why sha256 over the file's raw bytes, and not git's blob id.** The processor never touches git
(DESIGN.md's "one writer, one door" — the processors take no mount and write only through the gated
MCP path), so the hash has to be one it can compute from the bytes it reads back through that door.
Git's blob id would additionally require it to reproduce git's `blob <len>\0` framing and to depend
on SHA-1 for a safety gate. `content_sha256` is what a component with no git computes by default.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

# Git's `--name-status` letters, mapped to the pre-flight each one needs. `_DECISION_DIFF_FLAGS`
# (vault_git/runner.py) turns rename detection on and copy detection off, so `C` should never
# appear; anything not listed here is refused by name rather than guessed at, because a status this
# module does not understand is a target set it cannot compute — and an uncomputed target is a check
# that silently does not run.
_STATUS_ADDED = "A"
_STATUS_MODIFIED = "M"
_STATUS_TYPE_CHANGED = "T"
_STATUS_DELETED = "D"
_STATUS_RENAMED = "R"


class UnsupportedPatchStatusError(RuntimeError):
    """A `--name-status` letter this module has no pre-flight for. Loud by design: guessing would
    produce a chunk whose staleness check covers fewer paths than the patch writes."""


class TargetOperation(StrEnum):
    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"


class ChunkTargetError(ValueError):
    """A target that cannot be part of a well-formed chunk."""


def content_sha256(content: bytes) -> str:
    """The hash a chunk records and a consumer recomputes: lowercase hex sha256 over the file's raw
    bytes, exactly as stored — no newline normalisation, no encoding step, no trailing-whitespace
    trim. Both sides must hash the same bytes for the comparison to mean anything, so this is the
    only definition either side is allowed to use."""
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True, slots=True)
class ChunkTarget:
    """One path a chunk's patch writes, and the pre-flight the processor owes it."""

    path: str
    operation: TargetOperation
    base_sha256: str | None

    def __post_init__(self) -> None:
        for problem in validate_target(self):
            raise ChunkTargetError(problem)


@dataclass(frozen=True, slots=True)
class TargetSpec:
    """A target's path and operation before its pre-image hash has been looked up — the pure half
    of `targets_for_status`, which cannot read blobs itself. The impure caller resolves each spec's
    hash and builds the `ChunkTarget`."""

    path: str
    operation: TargetOperation


def targets_for_status(status: str, path: str, old_path: str | None) -> tuple[TargetSpec, ...]:
    """Which paths a `--name-status` entry writes, and what each one's pre-flight is.

    `status` carries a similarity percentage after the letter for renames (`R100`), so only the
    first character is significant.
    """
    letter = status[:1]
    if letter == _STATUS_ADDED:
        return (TargetSpec(path=path, operation=TargetOperation.CREATE),)
    if letter in (_STATUS_MODIFIED, _STATUS_TYPE_CHANGED):
        # A type change (file becoming a symlink, or the reverse) replaces the path's content
        # wholesale, so it takes the same hash check a plain modification does.
        return (TargetSpec(path=path, operation=TargetOperation.MODIFY),)
    if letter == _STATUS_DELETED:
        return (TargetSpec(path=path, operation=TargetOperation.DELETE),)
    if letter == _STATUS_RENAMED:
        if old_path is None:
            raise UnsupportedPatchStatusError(f"rename status {status!r} for {path!r} carries no source path")
        return (
            TargetSpec(path=old_path, operation=TargetOperation.DELETE),
            TargetSpec(path=path, operation=TargetOperation.CREATE),
        )
    raise UnsupportedPatchStatusError(f"unsupported name-status {status!r} for {path!r}")


def validate_target(target: ChunkTarget) -> tuple[str, ...]:
    """Every reason `target` is not well-formed, as human-readable strings. Empty means valid.

    Returns all problems rather than the first, so an operator sees the whole shape of a malformed
    target in one log line instead of one per re-run.
    """
    problems: list[str] = []
    problems.extend(_path_problems(target.path))

    if target.operation is TargetOperation.CREATE:
        if target.base_sha256 is not None:
            problems.append(f"{target.path!r}: a create has no prior content, so base_sha256 must be absent")
    elif target.base_sha256 is None:
        problems.append(f"{target.path!r}: a {target.operation} must carry the base_sha256 it was generated against")
    elif not _is_sha256_hex(target.base_sha256):
        problems.append(f"{target.path!r}: base_sha256 {target.base_sha256!r} is not 64 lowercase hex characters")

    return tuple(problems)


def _is_sha256_hex(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _path_problems(path: str) -> list[str]:
    """A target path names a write the processor will perform, so the constraints that keep that
    write inside the vault are enforced here, at the producer, rather than trusted downstream."""
    if not path:
        return ["a target path is empty"]
    problems: list[str] = []
    try:
        # A path read out of git via `errors="surrogateescape"` (PEP 383) carries undecodable bytes
        # as lone surrogates, which have no UTF-8 encoding and so cannot cross a JSON wire at all.
        # Refusing here names the path; letting it reach `json.dumps` raises against the whole
        # message and names nothing.
        path.encode("utf-8")
    except UnicodeEncodeError:
        problems.append(f"{path!r}: target path is not encodable as UTF-8")
    if path.startswith("/"):
        problems.append(f"{path!r}: target path is absolute")
    if path.endswith("/"):
        problems.append(f"{path!r}: target path names a directory")
    if "\0" in path:
        problems.append(f"{path!r}: target path contains a NUL byte")
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        problems.append(f"{path!r}: target path has an empty, '.' or '..' segment")
    return problems
