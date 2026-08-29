"""Deciding whether a staged change looks like data loss, as a pure function over two path lists —
no `GitRunner`, no `git write-tree`.

**Why this is split out of `commit.py` and kept pure.** `check_for_mass_deletion` is this
component's entire safety net against committing (and then pushing, permanently) a blank or
reset volume as though it were a deliberate deletion — `DESIGN.md`'s "fail loud, destroy
nothing" applies nowhere more than here. That verdict depends on exactly two counts (how many
tracked paths vanished, and whether every markdown note vanished) and one threshold; deriving it
correctly for every boundary — 0%, 49/50/51%, 100%, the `>= 1.0` escape hatch, an empty `HEAD` — is
worth proving directly against hand-picked path lists rather than only through a real repository
built to land on each exact fraction, the fixture-shaped, patchy coverage the rest of this refactor
exists to move away from (see `baseline_selector.py`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum


class DeletionVerdict(Enum):
    ALLOWED = "allowed"
    FRACTION_EXCEEDED = "fraction_exceeded"
    MARKDOWN_WIPED = "markdown_wiped"


@dataclass(frozen=True, slots=True)
class DeletionAssessment:
    """The verdict plus every count that went into it — the shell needs these to build
    `MassDeletionError`'s message; the pure function computes them once rather than the shell
    re-deriving them from the verdict alone."""

    verdict: DeletionVerdict
    tracked_count: int
    deleted_count: int
    deletion_fraction: float
    markdown_before: int
    markdown_after: int


def assess_deletion(
    *,
    tracked_before: Sequence[str],
    tracked_after: Sequence[str],
    max_deletion_fraction: float,
) -> DeletionAssessment:
    """Assess a staged change from the two path lists it moves between: everything `HEAD` tracked,
    and everything the staged tree would track if committed right now.

    `max_deletion_fraction >= 1.0` disables both tripwires, matching `check_for_mass_deletion`'s own
    documented escape hatch: at that point every deletion up to and including the whole tree is
    already accepted, which subsumes the zero-markdown tripwire too. `tracked_before` being empty is
    handled the same way, trivially — there is nothing tracked to have lost, so nothing here can be
    a deletion, and dividing by zero to compute a fraction never comes up.
    """
    tracked_count = len(tracked_before)
    if tracked_count == 0:
        return DeletionAssessment(
            verdict=DeletionVerdict.ALLOWED,
            tracked_count=0,
            deleted_count=0,
            deletion_fraction=0.0,
            markdown_before=0,
            markdown_after=0,
        )

    deleted_count = len(set(tracked_before) - set(tracked_after))
    deletion_fraction = deleted_count / tracked_count
    markdown_before = sum(1 for path in tracked_before if path.endswith(".md"))
    markdown_after = sum(1 for path in tracked_after if path.endswith(".md"))

    if max_deletion_fraction < 1.0 and deletion_fraction > max_deletion_fraction:
        verdict = DeletionVerdict.FRACTION_EXCEEDED
    elif max_deletion_fraction < 1.0 and markdown_before > 0 and markdown_after == 0:
        verdict = DeletionVerdict.MARKDOWN_WIPED
    else:
        verdict = DeletionVerdict.ALLOWED

    return DeletionAssessment(
        verdict=verdict,
        tracked_count=tracked_count,
        deleted_count=deleted_count,
        deletion_fraction=deletion_fraction,
        markdown_before=markdown_before,
        markdown_after=markdown_after,
    )
