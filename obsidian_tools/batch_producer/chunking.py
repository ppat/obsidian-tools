"""Splitting one batch's patch into chunks. Pure — no git, no NATS, no clock.

**The atom is one file's diff, never a hunk.** `PatchUnit` is what git produced for a single
`--name-status` entry, together with every path that entry writes, and this module never divides
one. Two independent reasons, both structural rather than stylistic:

- Half a file's hunks applied and the rest redelivered is the half-apply ADR-0048's whole-chunk
  pre-flight exists to make impossible.
- A rename's two halves are one entry. Split them and the relink has nothing to point at; git
  itself only re-pairs them into a single rename patch when both paths are passed to one diff
  invocation (`GitRunner.staged_patch`).

**Packing is greedy and order-preserving.** Producer order is the only thing that carries
dependency (ADR-0022's FIFO stream: rename in chunk N, relink in chunk N+1), so no packing
strategy here may reorder units to fill chunks better. Greedy-in-order is therefore not a
simplification of a smarter bin-packer — a smarter bin-packer would be wrong.

A unit larger than the budget on its own cannot be placed and is refused by name. Silently emitting
it would produce a message the broker rejects for exceeding `max_payload`, which surfaces as a
publish failure naming nothing an operator can act on.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from obsidian_tools.batch_producer.chunk import Chunk
from obsidian_tools.batch_producer.staleness import ChunkTarget


class ChunkTooLargeError(RuntimeError):
    """One file's diff exceeds the per-chunk budget on its own, so no packing can place it."""


@dataclass(frozen=True, slots=True)
class PatchUnit:
    """One file's diff (a rename's two halves count as one), and every path it writes."""

    patch: str
    targets: tuple[ChunkTarget, ...]


def patch_size_bytes(patch: str) -> int:
    """The unit of the chunking budget: encoded bytes, not characters. A vault of non-ASCII notes
    makes those differ by up to a factor of four, and it is bytes the broker's `max_payload` counts.
    """
    return len(patch.encode("utf-8"))


def pack_units(units: Sequence[PatchUnit], *, max_patch_bytes: int) -> tuple[tuple[PatchUnit, ...], ...]:
    """Group `units` into the smallest number of order-preserving runs whose concatenated patches
    each fit `max_patch_bytes`.

    Raises `ChunkTooLargeError` naming the paths of any single unit that cannot fit on its own.
    """
    oversized = [unit for unit in units if patch_size_bytes(unit.patch) > max_patch_bytes]
    if oversized:
        paths = sorted(target.path for unit in oversized for target in unit.targets)
        raise ChunkTooLargeError(
            f"{len(oversized)} file diff(s) exceed the {max_patch_bytes}-byte per-chunk budget "
            f"and cannot be split: {paths}"
        )

    groups: list[tuple[PatchUnit, ...]] = []
    current: list[PatchUnit] = []
    current_bytes = 0
    for unit in units:
        unit_bytes = patch_size_bytes(unit.patch)
        if current and current_bytes + unit_bytes > max_patch_bytes:
            groups.append(tuple(current))
            current = []
            current_bytes = 0
        current.append(unit)
        current_bytes += unit_bytes
    if current:
        groups.append(tuple(current))
    return tuple(groups)


def build_chunks(
    units: Sequence[PatchUnit], *, batch_id: str, produced_at: str, max_patch_bytes: int
) -> tuple[Chunk, ...]:
    """Pack `units` and render each group as a `Chunk`. An empty `units` yields no chunks — there is
    no such thing as an empty batch, and a caller with nothing staged has nothing to publish."""
    groups = pack_units(units, max_patch_bytes=max_patch_bytes)
    return tuple(
        Chunk(
            batch_id=batch_id,
            chunk_index=index,
            chunk_count=len(groups),
            produced_at=produced_at,
            patch="".join(unit.patch for unit in group),
            targets=tuple(target for unit in group for target in unit.targets),
        )
        for index, group in enumerate(groups)
    )
