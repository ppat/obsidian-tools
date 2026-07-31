"""Tests for `obsidian_tools/local_replicator/drift.py` -- the pure spool-entry classification and
cycle-gate decision, tested as pure functions over hand-built input rather than against a real git
repository or filesystem.

Why this file exists, and why it looks like `tests/test_vault_git_baseline_selector.py`: the same
split applies here as there (see that file's own docstring) -- `select_spool_entries` and
`decide_cycle_outcome` are exactly the kind of "which of these become which decision" logic that is
cheap to test with hundreds of adversarial cases here, and expensive to test only by building one
real git repository per case. `tests/test_local_replicator_cycle.py` keeps the real-git,
real-rsync integration tests; this file is where the volume of adversarial cases for the pure core
lives.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from obsidian_tools.local_replicator.drift import (
    CycleVerdict,
    StagedChange,
    decide_cycle_outcome,
    select_spool_entries,
)

# --- select_spool_entries: status classification ----------------------------------------------


def _change(status: str, path: str, old_path: str | None = None, patch: str = "diff\n") -> StagedChange:
    return StagedChange(status=status, path=path, old_path=old_path, patch=patch)


def test_added_status_classifies_as_create() -> None:
    entries = select_spool_entries([_change("A", "00-inbox/new-note.md")])
    assert [e.kind for e in entries] == ["create"]
    assert entries[0].path == "00-inbox/new-note.md"
    assert entries[0].old_path is None


def test_deleted_status_classifies_as_delete() -> None:
    entries = select_spool_entries([_change("D", "10-areas/gone.md")])
    assert [e.kind for e in entries] == ["delete"]


def test_modified_status_classifies_as_modify() -> None:
    entries = select_spool_entries([_change("M", "10-areas/tech/note.md")])
    assert [e.kind for e in entries] == ["modify"]


def test_type_change_status_falls_back_to_modify() -> None:
    """`T` (a regular file replaced by a symlink or vice versa) isn't a status this module names
    explicitly -- the fallback is "modify", never silently dropped."""
    entries = select_spool_entries([_change("T", "10-areas/weird.md")])
    assert [e.kind for e in entries] == ["modify"]


def test_rename_status_classifies_as_rename_and_carries_old_path() -> None:
    entries = select_spool_entries([_change("R100", "10-areas/renamed.md", old_path="10-areas/original.md")])
    assert entries[0].kind == "rename"
    assert entries[0].old_path == "10-areas/original.md"


def test_copy_status_classifies_as_rename() -> None:
    """`C` (a detected copy) is folded into the same "rename" vocabulary as `R` -- both carry an
    `old_path`, and downstream (Phase 5) cares about "this path's history traces to that one", not
    about git's rename/copy distinction specifically."""
    entries = select_spool_entries([_change("C087", "10-areas/copy.md", old_path="10-areas/source.md")])
    assert entries[0].kind == "rename"


def test_low_similarity_rename_score_still_classifies_as_rename() -> None:
    entries = select_spool_entries([_change("R051", "note-b.md", old_path="note-a.md")])
    assert entries[0].kind == "rename"


def test_patch_text_is_carried_through_unmodified() -> None:
    patch = "--- a/note.md\n+++ b/note.md\n@@ -1 +1 @@\n-old\n+new\n"
    entries = select_spool_entries([_change("M", "note.md", patch=patch)])
    assert entries[0].patch == patch


# --- select_spool_entries: adversarial paths ----------------------------------------------------


def test_non_ascii_path_is_preserved_exactly() -> None:
    path = "40-journal/2026-07-31-日本語.md"  # "日本語"
    entries = select_spool_entries([_change("M", path)])
    assert entries[0].path == path


def test_path_with_embedded_quote_is_preserved_exactly() -> None:
    path = '10-areas/tech/note "with quotes".md'
    entries = select_spool_entries([_change("A", path)])
    assert entries[0].path == path


def test_patch_with_embedded_newlines_and_quotes_is_preserved_exactly() -> None:
    patch = 'diff --git a/n.md b/n.md\n+line with "quotes" and\nmultiple\nlines\n'
    entries = select_spool_entries([_change("M", "n.md", patch=patch)])
    assert entries[0].patch == patch


def test_path_at_odd_depth_is_preserved_exactly() -> None:
    path = "10-areas/tech/homelab/subsystem/deeply/nested/note.md"
    entries = select_spool_entries([_change("M", path)])
    assert entries[0].path == path


def test_root_level_path_is_preserved_exactly() -> None:
    entries = select_spool_entries([_change("M", "log.md")])
    assert entries[0].path == "log.md"


# --- select_spool_entries: ordering, and never dropping or duplicating -------------------------


def test_entries_are_sorted_by_path_regardless_of_input_order() -> None:
    entries = select_spool_entries([_change("M", "z-note.md"), _change("A", "a-note.md"), _change("D", "m-note.md")])
    assert [e.path for e in entries] == ["a-note.md", "m-note.md", "z-note.md"]


def test_empty_input_produces_empty_output() -> None:
    assert select_spool_entries([]) == []


def test_mixed_creation_deletion_rename_modification_in_one_call() -> None:
    changes = [
        _change("A", "created.md"),
        _change("D", "deleted.md"),
        _change("M", "modified.md"),
        _change("R100", "renamed-to.md", old_path="renamed-from.md"),
    ]
    entries = select_spool_entries(changes)
    by_path = {e.path: e for e in entries}
    assert len(entries) == 4
    assert by_path["created.md"].kind == "create"
    assert by_path["deleted.md"].kind == "delete"
    assert by_path["modified.md"].kind == "modify"
    assert by_path["renamed-to.md"].kind == "rename"
    assert by_path["renamed-to.md"].old_path == "renamed-from.md"


# --- decide_cycle_outcome: the publish/tag-advance gate -----------------------------------------


def test_no_spool_failure_publishes_and_advances() -> None:
    assert decide_cycle_outcome(spool_write_failed=False) == CycleVerdict(should_publish=True, should_advance_tag=True)


def test_spool_failure_withholds_both_publish_and_tag_advance() -> None:
    """The core safety property (docs/DESIGN.md §4 Plane B, "Why the gate moved, not
    disappeared"): a single failed spool write blocks the *whole* cycle's publish, not merely the
    one path that failed -- there is no partial-publish path left in this design at all."""
    verdict = decide_cycle_outcome(spool_write_failed=True)
    assert verdict.should_publish is False
    assert verdict.should_advance_tag is False


def test_tag_advance_is_never_true_when_publish_is_false() -> None:
    """A narrower restatement of the same property, phrased as an implication rather than a
    literal equality -- protects against a future edit that decouples the two fields (drift.py's
    own docstring: "two fields, not one... a future change could pull ... apart incorrectly")."""
    for spool_write_failed in (True, False):
        verdict = decide_cycle_outcome(spool_write_failed=spool_write_failed)
        if not verdict.should_publish:
            assert not verdict.should_advance_tag


# --- Hypothesis property: select_spool_entries never drops or duplicates a change --------------

_statuses = st.sampled_from(("A", "M", "D", "T", "R100", "R087", "R051", "C100", "C065"))
_path_segments = st.sampled_from(("note", "日本語", 'quoted"name', "sub/dir/note", "a b c", "emoji-\U0001f4dd"))


@st.composite
def _staged_changes(draw: st.DrawFn) -> list[StagedChange]:
    count = draw(st.integers(min_value=0, max_value=15))
    changes: list[StagedChange] = []
    seen_paths: set[str] = set()
    for i in range(count):
        segment = draw(_path_segments)
        path = f"{segment}-{i}.md"  # index suffix keeps paths unique within one call
        if path in seen_paths:
            continue
        seen_paths.add(path)
        status = draw(_statuses)
        old_path = f"old-{path}" if status[:1] in ("R", "C") else None
        changes.append(_change(status, path, old_path=old_path))
    return changes


@given(_staged_changes())
def test_every_input_change_produces_exactly_one_spool_entry(changes: list[StagedChange]) -> None:
    """The property `select_spool_entries` exists to guarantee, stated independently of its own
    implementation (docs/DESIGN.md §1.5 R2: the device-side detector "submits every drift patch...
    and makes no judgement, so it can never silently drop a real edit"). No matter how many
    changes, of whatever statuses, arrive in whatever order, the output has exactly one entry per
    input path, and every input path is represented."""
    entries = select_spool_entries(changes)
    input_paths = {change.path for change in changes}
    output_paths = [entry.path for entry in entries]

    assert len(output_paths) == len(changes), "no change should be merged away or duplicated"
    assert set(output_paths) == input_paths, "every input path must appear in the output, and no other"
    assert len(set(output_paths)) == len(output_paths), "no path should appear more than once"
