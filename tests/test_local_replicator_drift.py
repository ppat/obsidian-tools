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
    captures_content,
    decide_cycle_outcome,
    select_spool_entries,
)

# --- select_spool_entries: status classification ----------------------------------------------


# The default is a real, minimal `git diff` patch rather than the placeholder `"diff\n"` it used to
# be: `captures_content` now requires positive evidence that what it is judging is git's own patch
# format at all (drift.py, `_PATCH_HEADER_PREFIX`), so a stand-in that no git invocation could ever
# emit is no longer a neutral fixture value. Every test below asserts on classification, ordering or
# accounting -- none of them on this text -- so the assertions are unchanged.
_MINIMAL_PATCH = "diff --git a/n.md b/n.md\n@@ -1 +1 @@\n-old\n+new\n"


def _change(status: str, path: str, old_path: str | None = None, patch: str = _MINIMAL_PATCH) -> StagedChange:
    return StagedChange(status=status, path=path, old_path=old_path, patch=patch)


def test_added_status_classifies_as_create() -> None:
    entries = select_spool_entries([_change("A", "00-inbox/new-note.md")]).entries
    assert [e.kind for e in entries] == ["create"]
    assert entries[0].path == "00-inbox/new-note.md"
    assert entries[0].old_path is None


def test_deleted_status_classifies_as_delete() -> None:
    entries = select_spool_entries([_change("D", "10-areas/gone.md")]).entries
    assert [e.kind for e in entries] == ["delete"]


def test_modified_status_classifies_as_modify() -> None:
    entries = select_spool_entries([_change("M", "10-areas/tech/note.md")]).entries
    assert [e.kind for e in entries] == ["modify"]


def test_type_change_status_falls_back_to_modify() -> None:
    """`T` (a regular file replaced by a symlink or vice versa) isn't a status this module names
    explicitly -- the fallback is "modify", never silently dropped."""
    entries = select_spool_entries([_change("T", "10-areas/weird.md")]).entries
    assert [e.kind for e in entries] == ["modify"]


def test_rename_status_classifies_as_rename_and_carries_old_path() -> None:
    entries = select_spool_entries([_change("R100", "10-areas/renamed.md", old_path="10-areas/original.md")]).entries
    assert entries[0].kind == "rename"
    assert entries[0].old_path == "10-areas/original.md"


def test_copy_status_classifies_as_rename() -> None:
    """`C` (a detected copy) is folded into the same "rename" vocabulary as `R` -- both carry an
    `old_path`, and downstream (Phase 5) cares about "this path's history traces to that one", not
    about git's rename/copy distinction specifically."""
    entries = select_spool_entries([_change("C087", "10-areas/copy.md", old_path="10-areas/source.md")]).entries
    assert entries[0].kind == "rename"


def test_low_similarity_rename_score_still_classifies_as_rename() -> None:
    entries = select_spool_entries([_change("R051", "note-b.md", old_path="note-a.md")]).entries
    assert entries[0].kind == "rename"


def test_patch_text_is_carried_through_unmodified() -> None:
    # The `diff --git` header is part of the fixture for the same reason as `_MINIMAL_PATCH`'s: git
    # emits it ahead of the `---`/`+++` pair for every patch, and `captures_content` now requires it.
    patch = "diff --git a/note.md b/note.md\n--- a/note.md\n+++ b/note.md\n@@ -1 +1 @@\n-old\n+new\n"
    entries = select_spool_entries([_change("M", "note.md", patch=patch)]).entries
    assert entries[0].patch == patch


# --- select_spool_entries: adversarial paths ----------------------------------------------------


def test_non_ascii_path_is_preserved_exactly() -> None:
    path = "40-journal/2026-07-31-日本語.md"  # "日本語"
    entries = select_spool_entries([_change("M", path)]).entries
    assert entries[0].path == path


def test_path_with_embedded_quote_is_preserved_exactly() -> None:
    path = '10-areas/tech/note "with quotes".md'
    entries = select_spool_entries([_change("A", path)]).entries
    assert entries[0].path == path


def test_patch_with_embedded_newlines_and_quotes_is_preserved_exactly() -> None:
    patch = 'diff --git a/n.md b/n.md\n+line with "quotes" and\nmultiple\nlines\n'
    entries = select_spool_entries([_change("M", "n.md", patch=patch)]).entries
    assert entries[0].patch == patch


def test_path_at_odd_depth_is_preserved_exactly() -> None:
    path = "10-areas/tech/homelab/subsystem/deeply/nested/note.md"
    entries = select_spool_entries([_change("M", path)]).entries
    assert entries[0].path == path


def test_root_level_path_is_preserved_exactly() -> None:
    entries = select_spool_entries([_change("M", "log.md")]).entries
    assert entries[0].path == "log.md"


# --- select_spool_entries: ordering, and never dropping or duplicating -------------------------


def test_entries_are_sorted_by_path_regardless_of_input_order() -> None:
    entries = select_spool_entries(
        [_change("M", "z-note.md"), _change("A", "a-note.md"), _change("D", "m-note.md")]
    ).entries
    assert [e.path for e in entries] == ["a-note.md", "m-note.md", "z-note.md"]


def test_empty_input_produces_empty_output() -> None:
    assert select_spool_entries([]).entries == []


def test_mixed_creation_deletion_rename_modification_in_one_call() -> None:
    changes = [
        _change("A", "created.md"),
        _change("D", "deleted.md"),
        _change("M", "modified.md"),
        _change("R100", "renamed-to.md", old_path="renamed-from.md"),
    ]
    entries = select_spool_entries(changes).entries
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


def test_an_uncaptured_path_withholds_publish_even_with_every_spool_write_succeeding() -> None:
    """The second way a cycle can fail to hold every drifted byte, and the one that does not
    announce itself: the spool writes all succeed, because the entry that would have carried the
    content was never built. Gating on the spool write alone would read this cycle as safe and let
    the publish rsync's `--delete` remove the file from iCloud."""
    verdict = decide_cycle_outcome(spool_write_failed=False, uncaptured_paths=("_attachments/x.png",))
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


# --- captures_content: does this change's patch actually carry what changed ---------------------


def test_a_binary_patch_does_not_capture_content() -> None:
    """What git emits instead of a hunk body when either side is binary. The patch asserts that
    something changed and carries none of it."""
    patch = "diff --git a/x.png b/x.png\nnew file mode 100644\nBinary files /dev/null and b/x.png differ\n"
    assert captures_content(_change("A", "_attachments/x.png", patch=patch)) is False


def test_a_pure_rename_captures_content_despite_having_no_hunk() -> None:
    """The case that rules out the obvious-looking implementation. A 100%-similarity rename has no
    hunk body either, but its header describes the change completely -- keying on git's binary
    marker rather than on "has a hunk" is what keeps a rename captured."""
    patch = "diff --git a/a.md b/b.md\nsimilarity index 100%\nrename from a.md\nrename to b.md\n"
    assert captures_content(_change("R100", "b.md", old_path="a.md", patch=patch)) is True


def test_prose_mentioning_binary_files_mid_hunk_still_captures_content() -> None:
    """A note whose *text* contains the marker's words is ordinary markdown, fully carried by its
    own hunk. The marker is matched with its leading newline and git's exact wording, and every
    line inside a hunk carries a `+`, `-` or space prefix -- so a note quoting it never reads as
    git's own marker."""
    patch = "diff --git a/n.md b/n.md\n@@ -0,0 +1 @@\n+Binary files are excluded from this vault.\n"
    assert captures_content(_change("M", "n.md", patch=patch)) is True


def test_an_empty_patch_does_not_capture_content() -> None:
    """The strongest possible signal that nothing was carried, and the one an "absence of the
    binary marker" predicate reads as healthy. A real change always produces a patch; an empty one
    means the diff that was supposed to describe this path produced nothing at all."""
    assert captures_content(_change("M", "10-areas/note.md", patch="")) is False


def test_a_whitespace_only_patch_does_not_capture_content() -> None:
    assert captures_content(_change("A", "10-areas/note.md", patch="\n \n")) is False


def test_a_patch_that_is_not_git_patch_output_does_not_capture_content() -> None:
    """Defence in depth behind the flags and the scrubbed environment that keep git's own output
    from being replaced (`vault_git/runner.py`): whatever else a capture is, it is `git diff`'s
    patch format. Output that never carried a `diff --git` header is not a patch this component
    can claim carries anything -- the summary line an external diff tool emits in its place is the
    exact shape this rejects."""
    assert captures_content(_change("M", "note.md", patch="1 file changed (difftastic-style summary)\n")) is False


def test_a_mode_only_change_captures_content_despite_having_no_hunk() -> None:
    """The other case that rules out an obvious-looking implementation, alongside the pure rename:
    a permission change produces `old mode`/`new mode` and no hunk body, and loses nothing --
    content is not what changed. A predicate demanding positive evidence of a hunk would withhold
    it, and since a mode change persists in the tree it would regenerate every cycle: a wedge, not
    a pause."""
    patch = "diff --git a/note.md b/note.md\nold mode 100644\nnew mode 100755\n"
    assert captures_content(_change("M", "note.md", patch=patch)) is True


# --- Hypothesis property: select_spool_entries accounts for every change exactly once -----------

_statuses = st.sampled_from(("A", "M", "D", "T", "R100", "R087", "R051", "C100", "C065"))
_path_segments = st.sampled_from(("note", "日本語", 'quoted"name', "sub/dir/note", "a b c", "emoji-\U0001f4dd"))
# Both shapes git can produce, because the property below is about accounting for *every* change
# and the binary shape is the only one selection currently withholds from the spool. A generator
# drawing only text patches leaves the property green whether or not withheld paths are accounted
# for -- a green property whose generator never reached the interesting region is indistinguishable
# from a correct one, which is the specific way a property becomes vacuous.
_patches = st.sampled_from(
    (
        "diff --git a/n.md b/n.md\n@@ -1 +1 @@\n-old\n+new\n",
        "diff --git a/n.bin b/n.bin\nBinary files a/n.bin and b/n.bin differ\n",
    )
)


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
        changes.append(_change(status, path, old_path=old_path, patch=draw(_patches)))
    return changes


@given(_staged_changes())
def test_every_input_change_is_accounted_for_exactly_once(changes: list[StagedChange]) -> None:
    """The property `select_spool_entries` exists to guarantee, stated independently of its own
    implementation (docs/DESIGN.md §1.5 R2: the device-side detector "submits every drift patch...
    and makes no judgement, so it can never silently drop a real edit").

    Accounted for, not spooled: a change whose patch carries no content is withheld from the spool
    and named in `uncaptured`, which the cycle then treats as a capture failure. Either list is a
    place a path can legitimately land; *neither* list is not. Phrasing the invariant over the
    union is what keeps it a statement about never dropping a path, rather than a restatement of
    which branch the selector happened to take."""
    selection = select_spool_entries(changes)
    input_paths = {change.path for change in changes}
    accounted = [entry.path for entry in selection.entries] + list(selection.uncaptured)

    assert len(accounted) == len(changes), "no change should be merged away or duplicated"
    assert set(accounted) == input_paths, "every input path must be accounted for, and no other"
    assert len(set(accounted)) == len(accounted), "no path should appear more than once"
    assert not (set(selection.uncaptured) & {entry.path for entry in selection.entries}), (
        "a path is either spooled or uncaptured, never both"
    )
