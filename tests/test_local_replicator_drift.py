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
    SpoolSelection,
    StagedChange,
    UpstreamComparison,
    captures_content,
    decide_cycle_outcome,
    matches_upstream,
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


_BASELINE_SHA = "1111111111111111111111111111111111111111"
_UPSTREAM_SHA = "2222222222222222222222222222222222222222"


def _upstream(*differing_paths: str) -> UpstreamComparison:
    return UpstreamComparison(sha=_UPSTREAM_SHA, differing_paths=frozenset(differing_paths))


_ALL_MATCHING_UPSTREAM = _upstream()


def _select(
    changes: list[StagedChange],
    *,
    upstream: UpstreamComparison | None = _ALL_MATCHING_UPSTREAM,
    baseline_sha: str = _BASELINE_SHA,
) -> SpoolSelection:
    """`select_spool_entries` with the comparison context most tests below don't care about.

    The default `upstream` deliberately names *no* differing paths, which makes every change in
    those tests read as matching upstream -- the state that would be dropped if this component ever
    started suppressing rather than annotating. Every classification, ordering and accounting
    assertion below therefore doubles as a check that annotation changes nothing about which
    entries exist."""
    return select_spool_entries(changes, baseline_sha=baseline_sha, upstream=upstream)


def test_added_status_classifies_as_create() -> None:
    entries = _select([_change("A", "00-inbox/new-note.md")]).entries
    assert [e.kind for e in entries] == ["create"]
    assert entries[0].path == "00-inbox/new-note.md"
    assert entries[0].old_path is None


def test_deleted_status_classifies_as_delete() -> None:
    entries = _select([_change("D", "10-areas/gone.md")]).entries
    assert [e.kind for e in entries] == ["delete"]


def test_modified_status_classifies_as_modify() -> None:
    entries = _select([_change("M", "10-areas/tech/note.md")]).entries
    assert [e.kind for e in entries] == ["modify"]


def test_type_change_status_falls_back_to_modify() -> None:
    """`T` (a regular file replaced by a symlink or vice versa) isn't a status this module names
    explicitly -- the fallback is "modify", never silently dropped."""
    entries = _select([_change("T", "10-areas/weird.md")]).entries
    assert [e.kind for e in entries] == ["modify"]


def test_rename_status_classifies_as_rename_and_carries_old_path() -> None:
    entries = _select([_change("R100", "10-areas/renamed.md", old_path="10-areas/original.md")]).entries
    assert entries[0].kind == "rename"
    assert entries[0].old_path == "10-areas/original.md"


def test_copy_status_classifies_as_rename() -> None:
    """`C` (a detected copy) is folded into the same "rename" vocabulary as `R` -- both carry an
    `old_path`, and downstream (Phase 5) cares about "this path's history traces to that one", not
    about git's rename/copy distinction specifically."""
    entries = _select([_change("C087", "10-areas/copy.md", old_path="10-areas/source.md")]).entries
    assert entries[0].kind == "rename"


def test_low_similarity_rename_score_still_classifies_as_rename() -> None:
    entries = _select([_change("R051", "note-b.md", old_path="note-a.md")]).entries
    assert entries[0].kind == "rename"


def test_patch_text_is_carried_through_unmodified() -> None:
    # The `diff --git` header is part of the fixture for the same reason as `_MINIMAL_PATCH`'s: git
    # emits it ahead of the `---`/`+++` pair for every patch, and `captures_content` now requires it.
    patch = "diff --git a/note.md b/note.md\n--- a/note.md\n+++ b/note.md\n@@ -1 +1 @@\n-old\n+new\n"
    entries = _select([_change("M", "note.md", patch=patch)]).entries
    assert entries[0].patch == patch


# --- select_spool_entries: adversarial paths ----------------------------------------------------


def test_non_ascii_path_is_preserved_exactly() -> None:
    path = "40-journal/2026-07-31-日本語.md"  # "日本語"
    entries = _select([_change("M", path)]).entries
    assert entries[0].path == path


def test_path_with_embedded_quote_is_preserved_exactly() -> None:
    path = '10-areas/tech/note "with quotes".md'
    entries = _select([_change("A", path)]).entries
    assert entries[0].path == path


def test_patch_with_embedded_newlines_and_quotes_is_preserved_exactly() -> None:
    patch = 'diff --git a/n.md b/n.md\n+line with "quotes" and\nmultiple\nlines\n'
    entries = _select([_change("M", "n.md", patch=patch)]).entries
    assert entries[0].patch == patch


def test_path_at_odd_depth_is_preserved_exactly() -> None:
    path = "10-areas/tech/homelab/subsystem/deeply/nested/note.md"
    entries = _select([_change("M", path)]).entries
    assert entries[0].path == path


def test_root_level_path_is_preserved_exactly() -> None:
    entries = _select([_change("M", "log.md")]).entries
    assert entries[0].path == "log.md"


# --- select_spool_entries: ordering, and never dropping or duplicating -------------------------


def test_entries_are_sorted_by_path_regardless_of_input_order() -> None:
    entries = _select([_change("M", "z-note.md"), _change("A", "a-note.md"), _change("D", "m-note.md")]).entries
    assert [e.path for e in entries] == ["a-note.md", "m-note.md", "z-note.md"]


def test_empty_input_produces_empty_output() -> None:
    assert _select([]).entries == []


def test_mixed_creation_deletion_rename_modification_in_one_call() -> None:
    changes = [
        _change("A", "created.md"),
        _change("D", "deleted.md"),
        _change("M", "modified.md"),
        _change("R100", "renamed-to.md", old_path="renamed-from.md"),
    ]
    entries = _select(changes).entries
    by_path = {e.path: e for e in entries}
    assert len(entries) == 4
    assert by_path["created.md"].kind == "create"
    assert by_path["deleted.md"].kind == "delete"
    assert by_path["modified.md"].kind == "modify"
    assert by_path["renamed-to.md"].kind == "rename"
    assert by_path["renamed-to.md"].old_path == "renamed-from.md"


# --- matches_upstream: the observation each entry carries, and what it must never become --------
#
# The whole of ppat/obsidian-tools#36's fix lives here. What is being tested is that the entry
# records *what was observed* -- never that the observation causes anything, because on this side of
# the system it must not: every case below asserts the entry still exists.


def test_content_identical_to_upstream_is_recorded_and_still_spooled() -> None:
    """The #36 case: a path republished by a crashed cycle. The entry is annotated, not dropped --
    dropping is the judgement ADR-0008 reserves for the server, and it is
    indistinguishable from a human edit that reproduced upstream byte-for-byte."""
    selection = _select([_change("M", "00-index.md")], upstream=_upstream())

    assert [e.path for e in selection.entries] == ["00-index.md"]
    assert selection.entries[0].matches_upstream is True
    assert selection.entries[0].upstream_sha == _UPSTREAM_SHA
    assert selection.entries[0].baseline_sha == _BASELINE_SHA


def test_content_differing_from_upstream_is_recorded_as_differing() -> None:
    entries = _select([_change("M", "00-index.md")], upstream=_upstream("00-index.md")).entries
    assert entries[0].matches_upstream is False


def test_one_paths_observation_does_not_leak_onto_another() -> None:
    """Per path, not per cycle. The rule the owner's own correction turned on: a cycle in which a
    human edits one file while a crash republished another must not resolve to a single verdict for
    the whole cycle -- each entry carries its own observation."""
    entries = _select(
        [_change("M", "republished.md"), _change("M", "typed-on-the-phone.md")],
        upstream=_upstream("typed-on-the-phone.md"),
    ).entries
    by_path = {e.path: e for e in entries}
    assert by_path["republished.md"].matches_upstream is True
    assert by_path["typed-on-the-phone.md"].matches_upstream is False


def test_a_deletion_matches_upstream_when_the_path_is_gone_upstream_too() -> None:
    """ "Matches upstream" for a path upstream deleted: absent on both sides. A deletion has no
    content to compare, and `differing_paths` lists a path present on exactly one side, so this
    needs no special case -- the path simply never appears there."""
    assert matches_upstream(_change("D", "10-areas/gone.md"), _upstream()) is True


def test_a_deletion_of_a_path_upstream_still_has_does_not_match_upstream() -> None:
    assert matches_upstream(_change("D", "10-areas/gone.md"), _upstream("10-areas/gone.md")) is False


def test_a_rename_matches_upstream_only_when_both_of_its_paths_do() -> None:
    """The case a per-`path` rule gets wrong. An upstream rename leaves the new path identical
    *and* the old path gone; a device rename onto content that coincidentally matches upstream
    leaves the old path still sitting there. Only requiring both halves separates them."""
    rename = _change("R100", "after.md", old_path="before.md")

    assert matches_upstream(rename, _upstream()) is True
    assert matches_upstream(rename, _upstream("before.md")) is False  # upstream still has the old path
    assert matches_upstream(rename, _upstream("after.md")) is False  # the new content isn't upstream's
    assert matches_upstream(rename, _upstream("before.md", "after.md")) is False


def test_no_known_upstream_revision_records_unknown_rather_than_differing() -> None:
    """A re-provisioned cache has no upstream revision to measure against. `None` -- "no
    observation was possible" -- and specifically not `False`, which would tell a Phase 5 reader
    that this whole first drift enumeration was positively established as device-authored."""
    selection = _select([_change("A", "note.md")], upstream=None)

    assert selection.entries[0].matches_upstream is None
    assert selection.entries[0].upstream_sha is None
    assert selection.entries[0].baseline_sha == _BASELINE_SHA  # the baseline is still known


def test_an_uncaptured_path_is_still_uncaptured_regardless_of_the_observation() -> None:
    """The observation and the publish gate are separate mechanisms and must stay that way: a
    binary's patch carries nothing whether or not its bytes match upstream, so it stays in
    `uncaptured` and keeps withholding the cycle. Annotating is not a way around the gate."""
    binary = _change("A", "_attachments/x.png", patch="diff --git a/x.png b/x.png\nBinary files a and b differ\n")

    selection = _select([binary], upstream=_upstream())

    assert selection.entries == []
    assert selection.uncaptured == ["_attachments/x.png"]


# --- decide_cycle_outcome: the publish/tag-advance gate -----------------------------------------


def test_no_spool_failure_publishes_and_advances() -> None:
    assert decide_cycle_outcome(spool_write_failed=False) == CycleVerdict(should_publish=True, should_advance_tag=True)


def test_spool_failure_withholds_both_publish_and_tag_advance() -> None:
    """The core safety property (ADR-0025): a single failed spool write blocks the *whole*
    cycle's publish, not merely the
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


def test_a_deleted_binary_captures_content_because_the_bytes_are_still_in_history() -> None:
    """The third case the binary rule reasoned about only two of. A deletion's patch needs to carry
    no content for exactly the reason a pure rename's doesn't: the header describes the change
    completely, and the pre-deletion bytes are in git at `LAST_CHECKOUT`, which is the commit the
    next cycle re-parks at. Nothing is lost, so nothing is withheld.

    Withholding it is not a pause but a permanent wedge -- the baseline cannot move, so the same
    deletion is re-detected from scratch every cycle, forever, and every upstream edit stops
    reaching the device behind it."""
    patch = "diff --git a/x.png b/x.png\ndeleted file mode 100644\nBinary files a/x.png and /dev/null differ\n"
    assert captures_content(_change("D", "_attachments/x.png", patch=patch)) is True


def test_a_modified_binary_still_does_not_capture_content() -> None:
    """The case that looks like the deletion above and is genuinely different: the *new* bytes
    exist only on the device, so the patch describes nothing that could reconstruct them and
    publishing would overwrite the device's copy with git's. It stays withheld -- and it is a pause,
    not a wedge, precisely because the deletion above is now captured: removing the file from the
    vault is a remedy that resolves on the next cycle instead of converting one stuck state into
    another."""
    patch = "diff --git a/x.png b/x.png\nindex fbf5707..33c2b0b 100644\nBinary files a/x.png and b/x.png differ\n"
    assert captures_content(_change("M", "_attachments/x.png", patch=patch)) is False


def test_a_binary_rename_that_also_changed_the_bytes_does_not_capture_content() -> None:
    """The rename carve-out is about a *pure* rename, where the header is the whole change. Below
    100% similarity the header is not, and for a binary there is no hunk to carry the rest."""
    patch = (
        "diff --git a/a.png b/b.png\nsimilarity index 87%\nrename from a.png\nrename to b.png\n"
        "Binary files a/a.png and b/b.png differ\n"
    )
    assert captures_content(_change("R087", "b.png", old_path="a.png", patch=patch)) is False


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
    implementation (ADR-0008: the device-side detector "submits every drift patch...
    and makes no judgement, so it can never silently drop a real edit").

    Accounted for, not spooled: a change whose patch carries no content is withheld from the spool
    and named in `uncaptured`, which the cycle then treats as a capture failure. Either list is a
    place a path can legitimately land; *neither* list is not. Phrasing the invariant over the
    union is what keeps it a statement about never dropping a path, rather than a restatement of
    which branch the selector happened to take."""
    selection = _select(changes)
    input_paths = {change.path for change in changes}
    accounted = [entry.path for entry in selection.entries] + list(selection.uncaptured)

    assert len(accounted) == len(changes), "no change should be merged away or duplicated"
    assert set(accounted) == input_paths, "every input path must be accounted for, and no other"
    assert len(set(accounted)) == len(accounted), "no path should appear more than once"
    assert not (set(selection.uncaptured) & {entry.path for entry in selection.entries}), (
        "a path is either spooled or uncaptured, never both"
    )


@given(_staged_changes())
def test_the_upstream_observation_never_changes_which_entries_exist(changes: list[StagedChange]) -> None:
    """The line between an observation and a judgement, stated as a property (ppat/obsidian-tools#36).

    Whatever the comparison against upstream came back with -- nothing observable at all, every path
    identical, every path different -- the entries `select_spool_entries` produces must be the same
    entries, carrying the same patches, with the same paths landing in `uncaptured`. The moment that
    stops holding, the device has started deciding rather than recording, which is exactly the
    failure ADR-0008 forbids.

    Deliberately *not* a restatement of `matches_upstream`'s own formula over generated input --
    that would only assert the implementation equals itself. This asserts the thing the formula must
    never touch."""
    every_path = sorted({change.path for change in changes} | {c.old_path for c in changes if c.old_path is not None})
    observations = (None, _upstream(), _upstream(*every_path))

    shapes = [
        (
            [(e.kind, e.path, e.old_path, e.patch) for e in _select(changes, upstream=u).entries],
            _select(changes, upstream=u).uncaptured,
        )
        for u in observations
    ]
    assert shapes.count(shapes[0]) == len(shapes)
