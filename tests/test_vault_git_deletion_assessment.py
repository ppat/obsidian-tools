"""Tests for `obsidian_tools/vault_git/deletion_assessment.py` — the pure mass-deletion verdict,
tested as a pure function over two hand-built path lists rather than a real repository walked to a
specific fraction.

`test_vault_git_commit.py` keeps the real-git integration coverage (`check_for_mass_deletion`
against an actual staged tree, including the unreadable-directory regression that motivated deriving
the after-state from `git write-tree` rather than a filesystem walk); this file is the exhaustive
boundary table for the verdict itself.
"""

from __future__ import annotations

from obsidian_tools.vault_git.deletion_assessment import DeletionVerdict, assess_deletion

_DEFAULT_FRACTION = 0.5


def _paths(count: int, *, prefix: str = "note", suffix: str = ".md") -> list[str]:
    return [f"{prefix}-{i}{suffix}" for i in range(count)]


def test_zero_percent_deletion_is_allowed() -> None:
    before = _paths(10)
    assessment = assess_deletion(tracked_before=before, tracked_after=before, max_deletion_fraction=_DEFAULT_FRACTION)
    assert assessment.verdict is DeletionVerdict.ALLOWED
    assert assessment.deleted_count == 0
    assert assessment.deletion_fraction == 0.0


def test_forty_nine_percent_deletion_is_allowed_at_the_default_fifty_percent_threshold() -> None:
    before = _paths(100)
    after = before[49:]  # 49 deleted, 51 remain -- 49% < 50%
    assessment = assess_deletion(tracked_before=before, tracked_after=after, max_deletion_fraction=_DEFAULT_FRACTION)
    assert assessment.verdict is DeletionVerdict.ALLOWED


def test_exactly_fifty_percent_deletion_is_allowed_the_threshold_is_strictly_greater_than() -> None:
    before = _paths(100)
    after = before[50:]  # exactly 50 deleted, 50 remain -- 50% is not > 50%
    assessment = assess_deletion(tracked_before=before, tracked_after=after, max_deletion_fraction=_DEFAULT_FRACTION)
    assert assessment.verdict is DeletionVerdict.ALLOWED


def test_fifty_one_percent_deletion_trips_the_fraction_tripwire() -> None:
    before = _paths(100)
    after = before[51:]  # 51 deleted, 49 remain -- 51% > 50%
    assessment = assess_deletion(tracked_before=before, tracked_after=after, max_deletion_fraction=_DEFAULT_FRACTION)
    assert assessment.verdict is DeletionVerdict.FRACTION_EXCEEDED
    assert assessment.deleted_count == 51
    assert assessment.tracked_count == 100


def test_ninety_nine_percent_deletion_trips_the_fraction_tripwire() -> None:
    before = _paths(100)
    after = before[99:]
    assessment = assess_deletion(tracked_before=before, tracked_after=after, max_deletion_fraction=_DEFAULT_FRACTION)
    assert assessment.verdict is DeletionVerdict.FRACTION_EXCEEDED


def test_one_hundred_percent_deletion_trips_the_fraction_tripwire() -> None:
    before = _paths(100)
    assessment = assess_deletion(tracked_before=before, tracked_after=[], max_deletion_fraction=_DEFAULT_FRACTION)
    assert assessment.verdict is DeletionVerdict.FRACTION_EXCEEDED
    assert assessment.deletion_fraction == 1.0


def test_max_fraction_at_or_above_one_disables_both_tripwires() -> None:
    """The operator escape hatch: a full wipe of every tracked path, including every markdown note,
    must not trip either tripwire once the fraction cap itself is >= 1.0."""
    before = _paths(100)
    for max_fraction in (1.0, 1.5, 100.0):
        assessment = assess_deletion(tracked_before=before, tracked_after=[], max_deletion_fraction=max_fraction)
        assert assessment.verdict is DeletionVerdict.ALLOWED


def test_empty_head_is_allowed_without_dividing_by_zero() -> None:
    assessment = assess_deletion(tracked_before=[], tracked_after=[], max_deletion_fraction=_DEFAULT_FRACTION)
    assert assessment.verdict is DeletionVerdict.ALLOWED
    assert assessment.tracked_count == 0
    assert assessment.deletion_fraction == 0.0


def test_zero_markdown_surviving_trips_even_with_attachments_untouched_and_a_small_fraction() -> None:
    """The fraction tripwire alone wouldn't catch this: losing the vault's only markdown note among
    many surviving attachments is a small fraction of tracked paths, but the vault that exists to
    hold notes now holds none."""
    before = ["00-index.md", *_paths(9, prefix="attachment", suffix=".bin")]
    after = [p for p in before if not p.endswith(".md")]  # only the one markdown note is gone
    assessment = assess_deletion(tracked_before=before, tracked_after=after, max_deletion_fraction=_DEFAULT_FRACTION)
    assert assessment.verdict is DeletionVerdict.MARKDOWN_WIPED
    assert assessment.deletion_fraction == 0.1


def test_some_markdown_surviving_does_not_trip_the_markdown_tripwire() -> None:
    before = _paths(4)
    after = before[1:]  # one deleted, three markdown notes remain
    assessment = assess_deletion(tracked_before=before, tracked_after=after, max_deletion_fraction=_DEFAULT_FRACTION)
    assert assessment.verdict is DeletionVerdict.ALLOWED


def test_head_with_no_markdown_at_all_cannot_trip_the_markdown_tripwire() -> None:
    """`markdown_before == 0` is excluded explicitly -- a vault that never tracked any markdown
    (attachments only) losing all of them is not "the notes are gone," there were never any."""
    before = _paths(4, prefix="attachment", suffix=".bin")
    assessment = assess_deletion(tracked_before=before, tracked_after=[], max_deletion_fraction=1.0)
    assert assessment.verdict is DeletionVerdict.ALLOWED


def test_unreadable_directory_shape_a_single_ordinary_deletion_plus_untouched_notes_elsewhere() -> None:
    """Mirrors `test_vault_git_commit.py`'s unreadable-directory regression at the pure-data level:
    one ordinary, legitimate deletion (`00-index.md`, as if promoted elsewhere) alongside markdown
    notes under directories that happened to be briefly unreadable but were never actually staged as
    deletions -- the after-state correctly still lists them, so no tripwire fires."""
    before = ["00-index.md", "locked-a/note.md", "locked-b/note.md"]
    after = ["locked-a/note.md", "locked-b/note.md"]
    assessment = assess_deletion(tracked_before=before, tracked_after=after, max_deletion_fraction=_DEFAULT_FRACTION)
    assert assessment.verdict is DeletionVerdict.ALLOWED
    assert assessment.markdown_after == 2


def test_assessment_carries_the_counts_the_shell_needs_for_its_error_message() -> None:
    before = _paths(10)
    after = before[6:]  # 6 deleted, 4 remain -- 60% > 50%
    assessment = assess_deletion(tracked_before=before, tracked_after=after, max_deletion_fraction=_DEFAULT_FRACTION)
    assert assessment.deleted_count == 6
    assert assessment.tracked_count == 10
    assert assessment.deletion_fraction == 0.6
    assert assessment.markdown_before == 10
    assert assessment.markdown_after == 4
