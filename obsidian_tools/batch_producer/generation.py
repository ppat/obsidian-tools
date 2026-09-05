"""Turning what is staged in the producer's repository into `PatchUnit`s. The git half of the
shell — every git call crosses `GitRunner` (ADR-0046), and nothing here decides policy.

The producer's input is **the index**: whatever has been staged in its working repository, exactly
as `commit` and `local-replicator` already read their own staged state. That keeps the boundary of
a batch something an operator sets with ordinary git (`git add`) rather than a second selection
language this component would have to invent and explain.

**`base_rev` is what the patch was generated against**, and therefore where every pre-image hash is
read from. It defaults to `HEAD` because that is what `git diff --cached` diffs against; naming it
separately would let the two drift, so the caller that changes one changes both.

**Non-UTF-8 patch text is refused, by name, rather than carried.** The chunk is JSON on the wire and
the processor applies it as text writes through the gated MCP path, which is JSON as well — so the
whole downstream path is UTF-8-only and always was. Refusing here does not narrow what the system
can carry; it moves an unavoidable failure to the one place that still knows which file caused it.
"""

from __future__ import annotations

import logging

from obsidian_tools.batch_producer.chunking import PatchUnit
from obsidian_tools.batch_producer.staleness import (
    ChunkTarget,
    TargetOperation,
    TargetSpec,
    content_sha256,
    targets_for_status,
)
from obsidian_tools.vault_git.runner import GitRunner, NameStatusEntry

logger = logging.getLogger(__name__)


class PatchEncodingError(RuntimeError):
    """A staged file's diff is not valid UTF-8, so it cannot cross the JSON wire the batch stream
    and the gated MCP path both are."""


def collect_patch_units(runner: GitRunner, *, base_rev: str) -> list[PatchUnit]:
    """One `PatchUnit` per staged `--name-status` entry, in git's own order."""
    entries = runner.staged_name_status()
    units = [_unit_for_entry(runner, entry, base_rev=base_rev) for entry in entries]
    logger.info(
        "collected staged changes",
        extra={
            "event": "batch_units_collected",
            "unit_count": len(units),
            "target_count": sum(len(unit.targets) for unit in units),
            "base_rev": base_rev,
        },
    )
    return units


def _unit_for_entry(runner: GitRunner, entry: NameStatusEntry, *, base_rev: str) -> PatchUnit:
    specs = targets_for_status(entry.status, entry.path, entry.old_path)
    # A rename's source and destination go to one diff invocation so git re-pairs them into a single
    # rename patch rather than an unrelated delete/add pair — `GitRunner.staged_patch`'s own note.
    # `:(literal)` (ADR-0043) so a filename holding `*`, `[` or `?` matches itself.
    pathspecs = [f":(literal){path}" for path in _diff_paths(entry)]
    raw_patch = runner.staged_patch_bytes(*pathspecs)
    try:
        patch = raw_patch.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchEncodingError(f"the staged diff for {entry.path!r} is not valid UTF-8: {exc}") from exc
    return PatchUnit(patch=patch, targets=tuple(_target_for_spec(runner, spec, base_rev=base_rev) for spec in specs))


def _diff_paths(entry: NameStatusEntry) -> list[str]:
    return [entry.old_path, entry.path] if entry.old_path is not None else [entry.path]


def _target_for_spec(runner: GitRunner, spec: TargetSpec, *, base_rev: str) -> ChunkTarget:
    if spec.operation is TargetOperation.CREATE:
        return ChunkTarget(path=spec.path, operation=spec.operation, base_sha256=None)
    return ChunkTarget(
        path=spec.path,
        operation=spec.operation,
        base_sha256=content_sha256(runner.blob_bytes(base_rev, spec.path)),
    )
