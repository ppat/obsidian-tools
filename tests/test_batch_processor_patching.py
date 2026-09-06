"""Tests for `batch_processor/patching.py` — the unified diff, applied without git.

Two layers, and the second is the one that matters. The tables pin the shapes that silently corrupt
content when handled the obvious way (line splitting, the no-newline marker, a create's hunk
header). Underneath them sits an **oracle outside this file**: a real git repository stages a real
change, the *producer* turns it into a real chunk, and the applier's output is compared against what
git itself put on disk. That oracle is what stops these tests from being a restatement of the
implementation — nothing about the expected content is computed by the code under test.

The git calls that build each fixture are environment-scrubbed for the reason
`tests/test_batch_producer_generation.py` records: a machine with `core.autocrlf=input` in its
global config has `git add` clean CRLF into the blob, so a CRLF test would pass or fail according to
whose `~/.gitconfig` ran it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from obsidian_tools.batch_processor.patching import (
    PatchError,
    WriteKind,
    already_applied,
    apply_file_diff,
    parse_patch,
    plan_writes,
)
from obsidian_tools.batch_producer.chunk import Chunk
from obsidian_tools.batch_producer.chunking import build_chunks
from obsidian_tools.batch_producer.generation import collect_patch_units
from obsidian_tools.batch_producer.staleness import ChunkTarget, TargetOperation
from obsidian_tools.vault_git.runner import GitRunner

_SCRUBBED_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_ATTR_NOSYSTEM": "1",
}


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True, env=_SCRUBBED_GIT_ENV)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    work_tree = tmp_path / "work"
    work_tree.mkdir()
    _git("init", "-q", "--initial-branch=main", cwd=work_tree)
    _git("config", "user.email", "t@example.invalid", cwd=work_tree)
    _git("config", "user.name", "t", cwd=work_tree)
    return work_tree


def commit(repo: Path, files: dict[str, bytes]) -> None:
    for name, body in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_bytes(body)
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "seed", cwd=repo)


def stage(repo: Path, *, write: dict[str, bytes] | None = None, remove: tuple[str, ...] = ()) -> None:
    for name, body in (write or {}).items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_bytes(body)
    for name in remove:
        (repo / name).unlink()
    _git("add", "-A", cwd=repo)


def one_chunk(repo: Path) -> Chunk:
    """What the producer would enqueue for whatever is staged — the real thing, not a hand-written
    patch, so the applier is checked against the format it will actually receive."""
    units = collect_patch_units(GitRunner(repo / ".git", repo), base_rev="HEAD")
    chunks = build_chunks(units, batch_id="b1", produced_at="2026-09-05T12:00:00+00:00", max_patch_bytes=1 << 20)
    assert len(chunks) == 1
    return chunks[0]


def pre_images(repo: Path, chunk: Chunk) -> dict[str, str | None]:
    """Each target's content at HEAD — the pre-flight's own view, read straight out of git."""
    runner = GitRunner(repo / ".git", repo)
    images: dict[str, str | None] = {}
    for target in chunk.targets:
        if target.operation is TargetOperation.CREATE:
            images[target.path] = None
        else:
            images[target.path] = runner.blob_bytes("HEAD", target.path).decode("utf-8")
    return images


def applied(repo: Path) -> dict[str, str]:
    """What the producer's chunk, applied, should reproduce: the staged content, on disk."""
    return {
        write.path: (write.content or "")
        for write in plan_writes(one_chunk(repo), pre_images(repo, one_chunk(repo)))
        if write.kind is not WriteKind.DELETE
    }


def on_disk(repo: Path, path: str) -> str:
    return (repo / path).read_bytes().decode("utf-8")


# --- the oracle: what git staged is what the applier reproduces -----------------------------------


def test_a_modified_note_is_reproduced_byte_for_byte(repo: Path) -> None:
    """The central oracle. Red on any hunk-application defect at all — the expected content is what
    git has on disk, computed by nothing in this codebase."""
    commit(repo, {"10-areas/x.md": b"alpha\nbeta\ngamma\n"})
    stage(repo, write={"10-areas/x.md": b"alpha\nBETA\ngamma\ndelta\n"})

    assert applied(repo)["10-areas/x.md"] == on_disk(repo, "10-areas/x.md")


def test_a_created_note_is_reproduced_byte_for_byte(repo: Path) -> None:
    """A create's diff is `@@ -0,0 +1,n @@`, whose zero old count makes its start an index already.
    Red on the off-by-one that treats it as an index plus one — every created note would lose or
    duplicate its first line."""
    commit(repo, {"seed.md": b"seed\n"})
    stage(repo, write={"05-raw/new.md": b"one\ntwo\nthree\n"})

    assert applied(repo)["05-raw/new.md"] == on_disk(repo, "05-raw/new.md")


def test_a_note_without_a_trailing_newline_stays_without_one(repo: Path) -> None:
    """`\\ No newline at end of file`, in the direction that loses a byte. Red if the marker is
    ignored: every applied note would gain a trailing newline git never wrote, and the *next*
    chunk touching it would then reject as stale against a hash it invalidated itself."""
    commit(repo, {"x.md": b"alpha\nbeta\n"})
    stage(repo, write={"x.md": b"alpha\nBETA"})

    assert applied(repo)["x.md"] == "alpha\nBETA"


def test_a_note_that_gains_a_trailing_newline_gains_it(repo: Path) -> None:
    """The other direction, and the one a naive "strip if any marker is present" gets wrong: the
    marker here describes the *old* side only. Red if the output kept the old file's missing
    newline."""
    commit(repo, {"x.md": b"alpha\nbeta"})
    stage(repo, write={"x.md": b"alpha\nbeta\n"})

    assert applied(repo)["x.md"] == "alpha\nbeta\n"


def test_a_crlf_note_keeps_every_carriage_return(repo: Path) -> None:
    """`str.splitlines()` breaks on `\\r` as well as `\\n`. Red the moment it is used instead of
    `split("\\n")`: every line ending in the note would be silently rewritten, which is exactly the
    corruption the producer's binary git reads exist to prevent one component upstream."""
    commit(repo, {"x.md": b"alpha\r\nbeta\r\n"})
    stage(repo, write={"x.md": b"alpha\r\nBETA\r\n"})

    assert applied(repo)["x.md"] == "alpha\r\nBETA\r\n"
    assert on_disk(repo, "x.md") == "alpha\r\nBETA\r\n"


def test_a_note_holding_a_hunk_header_as_content_round_trips(repo: Path) -> None:
    """A markdown note may legitimately contain a line beginning `@@`, and in the diff it arrives as
    an ordinary body line with a one-character prefix in front of it. Red if a body line were ever
    classified by anything but its first character — a `line.strip().startswith("@@")` test, say —
    at which point this note's own content reads as a hunk header and the rest of its edit is
    dropped silently."""
    commit(repo, {"x.md": b"@@ -1 +1 @@\nkeep\ntail\n"})
    stage(repo, write={"x.md": b"@@ -1 +1 @@\nCHANGED\ntail\nmore\n"})

    assert applied(repo)["x.md"] == on_disk(repo, "x.md")


def test_a_bare_empty_line_inside_a_hunk_reads_as_context() -> None:
    """Git writes a blank context line as a single space, but a patch that has been through any
    whitespace-trimming pipeline arrives with it bare — and a bare line matches none of the four
    prefixes. Red if the lenience were removed: the line is refused outright, and a note containing
    a blank line becomes unapplicable. Red the other way too, if it were read as anything but
    context: every line after it would be matched against the wrong source line.

    Hand-written rather than generated, because git will not emit this shape on purpose — which is
    the whole reason the branch exists and the whole reason it needs a test of its own.
    """
    patch = "diff --git a/x.md b/x.md\n--- a/x.md\n+++ b/x.md\n@@ -1,3 +1,3 @@\n a\n\n-b\n+B\n"

    assert apply_file_diff("a\n\nb\n", parse_patch(patch)[0]) == "a\n\nB\n"


def test_a_note_with_a_blank_line_round_trips(repo: Path) -> None:
    """Git writes a blank context line as a single space, and an empty line inside a hunk still has
    to read as context. Red if a blank line desynchronised the walk — every line after it would be
    matched against the wrong source line."""
    commit(repo, {"x.md": b"a\n\nb\n"})
    stage(repo, write={"x.md": b"a\n\nB\n"})

    assert applied(repo)["x.md"] == "a\n\nB\n"


def test_a_note_whose_name_is_not_ascii_is_applied_at_its_real_path(repo: Path) -> None:
    """**Measured, and the reason this reader decodes C-quoting at all**: a real producer chunk for
    `café.md` carries `"a/caf\\303\\251.md"`, because `core.quotePath` defaults to on and the
    producer's git seam scrubs the operator's config rather than pinning it off. Red if the quoted
    form were used as-is or refused — every accented note in the vault would be written to a
    mangled path, or would never be written at all."""
    commit(repo, {"café.md": "über\nnaïve\n".encode()})
    stage(repo, write={"café.md": "über\nNAÏVE\n".encode()})

    chunk = one_chunk(repo)

    assert '"a/caf\\303\\251.md"' in chunk.patch
    assert applied(repo)["café.md"] == "über\nNAÏVE\n"


def test_a_deleted_note_is_planned_as_a_delete_and_nothing_else(repo: Path) -> None:
    """Red if a deletion produced a write of empty content: the note would be emptied rather than
    removed, and the vault would fill with zero-byte husks that every later read still finds."""
    commit(repo, {"x.md": b"gone\n", "keep.md": b"keep\n"})
    stage(repo, remove=("x.md",))

    chunk = one_chunk(repo)
    writes = plan_writes(chunk, pre_images(repo, chunk))

    assert [(w.path, w.kind, w.content) for w in writes] == [("x.md", WriteKind.DELETE, None)]


def test_a_rename_creates_the_new_path_before_deleting_the_old_one(repo: Path) -> None:
    """ "Fail loud, destroy nothing" decides this order, not the patch format. Red if the delete came
    first: a crash between the two halves would leave the note at neither path instead of at both."""
    commit(repo, {"old.md": b"body\n"})
    stage(repo, write={"new.md": b"body\n"}, remove=("old.md",))

    chunk = one_chunk(repo)
    writes = plan_writes(chunk, pre_images(repo, chunk))

    assert [(w.path, w.kind) for w in writes] == [("new.md", WriteKind.CREATE), ("old.md", WriteKind.DELETE)]
    assert writes[0].content == "body\n"


def test_a_rename_that_also_edits_carries_the_edited_content_to_the_new_path(repo: Path) -> None:
    """Red if a rename's hunks were applied to the destination's (absent) pre-image rather than the
    source's — the new note would be written as if created from nothing, losing every unchanged
    line."""
    commit(repo, {"old.md": b"one\ntwo\nthree\n"})
    stage(repo, write={"new.md": b"one\nTWO\nthree\n"}, remove=("old.md",))

    chunk = one_chunk(repo)
    writes = plan_writes(chunk, pre_images(repo, chunk))

    assert writes[0].path == "new.md"
    assert writes[0].content == "one\nTWO\nthree\n"


def test_several_files_in_one_chunk_all_reproduce(repo: Path) -> None:
    """Red if a multi-file patch's second file were applied against the first file's pre-image, or
    if the file boundary were mis-detected at all."""
    commit(repo, {"a.md": b"a1\na2\n", "b.md": b"b1\nb2\n"})
    stage(repo, write={"a.md": b"a1\nA2\n", "b.md": b"b1\nB2\nb3\n"})

    result = applied(repo)

    assert result == {"a.md": "a1\nA2\n", "b.md": "b1\nB2\nb3\n"}


# --- the property, against the same oracle --------------------------------------------------------


_LINE = st.text(
    # No surrogates (unencodable), no `\n`/`\r` (line structure rather than line content), and no
    # NUL — git calls any file containing one binary and emits a content-free placeholder instead
    # of a diff, which `parse_patch` refuses by design. Generating one would test the refusal, not
    # this property.
    alphabet=st.characters(exclude_categories=("Cs",), exclude_characters="\n\r\x00"),
    max_size=6,
)


@given(original=st.lists(_LINE, max_size=8), edited=st.lists(_LINE, max_size=8))
def test_applying_gits_own_diff_reproduces_what_git_staged(original: list[str], edited: list[str]) -> None:
    """The oracle as a property: for any pair of line lists, git's diff applied to the first
    reproduces the second. Not a restatement of the applier — the expected value is produced by
    `git diff`/`git add`, which the applier never calls and never sees.

    Red on any hunk shape the tables happen not to cover: interleaved additions and deletions,
    edits at either boundary, a file that becomes empty, a file that starts empty.
    """
    import tempfile

    with tempfile.TemporaryDirectory(prefix="bp-patch-") as raw:
        work = Path(raw)
        _git("init", "-q", "--initial-branch=main", cwd=work)
        _git("config", "user.email", "t@example.invalid", cwd=work)
        _git("config", "user.name", "t", cwd=work)
        before = "".join(f"{line}\n" for line in original)
        after = "".join(f"{line}\n" for line in edited)
        (work / "x.md").write_text(before, encoding="utf-8")
        (work / "anchor.md").write_text("anchor\n", encoding="utf-8")
        _git("add", "-A", cwd=work)
        _git("commit", "-qm", "seed", cwd=work)
        if before == after:
            return
        (work / "x.md").write_text(after, encoding="utf-8")
        _git("add", "-A", cwd=work)

        chunk = one_chunk(work)
        writes = plan_writes(chunk, pre_images(work, chunk))

        if after == "":
            # git models "every line removed" as an empty file, not a deletion; the write is still a
            # modify, and its content is the empty string.
            assert [(w.path, w.kind, w.content) for w in writes] == [("x.md", WriteKind.MODIFY, "")]
        else:
            assert [(w.path, w.content) for w in writes] == [("x.md", after)]


# --- the refusals: what the applier will not guess at ---------------------------------------------


def test_a_patch_writing_a_path_the_targets_never_declared_is_refused() -> None:
    """`targets` is the pre-flight and the patch is the instruction; a disagreement means a write
    whose staleness nobody measured. Red if the two were not cross-checked — the extra path would
    be written having skipped ADR-0048 entirely."""
    chunk = Chunk(
        batch_id="b1",
        chunk_index=0,
        chunk_count=1,
        produced_at="2026-09-05T12:00:00+00:00",
        patch="diff --git a/a.md b/a.md\n--- /dev/null\n+++ b/a.md\n@@ -0,0 +1 @@\n+a\n",
        targets=(),
    )

    with pytest.raises(PatchError, match="disagree"):
        plan_writes(chunk, {})


def test_a_patch_whose_operation_contradicts_its_target_is_refused() -> None:
    """Red if the operation were taken from the patch alone: a create declared as a modify would
    skip the existence check and take the hash branch, which for a create has no hash to take."""
    from obsidian_tools.batch_producer.staleness import ChunkTarget

    chunk = Chunk(
        batch_id="b1",
        chunk_index=0,
        chunk_count=1,
        produced_at="2026-09-05T12:00:00+00:00",
        patch="diff --git a/a.md b/a.md\n--- /dev/null\n+++ b/a.md\n@@ -0,0 +1 @@\n+a\n",
        targets=(ChunkTarget("a.md", TargetOperation.MODIFY, "0" * 64),),
    )

    with pytest.raises(PatchError, match="declares it a modify"):
        plan_writes(chunk, {"a.md": "x\n"})


def test_a_binary_body_is_refused_rather_than_written_as_its_placeholder() -> None:
    """Red if `Binary files ... differ` fell through to the ordinary path: the note would be written
    as the empty result of a patch with no hunks, destroying whatever was there."""
    with pytest.raises(PatchError, match="binary"):
        parse_patch("diff --git a/x.bin b/x.bin\nindex 1..2 100644\nBinary files a/x.bin and b/x.bin differ\n")


def test_a_quoted_path_holding_an_unknown_escape_is_refused() -> None:
    """Decoding stops at anything git would not have written. Red if unknown escapes were passed
    through: the write would land at a path with literal backslashes in its name, which is a
    different note from the one the chunk declared."""
    with pytest.raises(PatchError, match="unknown escape"):
        parse_patch('diff --git a/x b/x\n--- "a/x\\qy.md"\n+++ b/x\n')


def test_a_quoted_path_with_a_truncated_octal_escape_is_refused() -> None:
    """Red if a short octal run were accepted: `\\30` would decode as some other byte entirely, and
    the resulting path would be a plausible-looking wrong one."""
    with pytest.raises(PatchError, match="octal"):
        parse_patch('diff --git a/x b/x\n--- "a/x\\30.md"\n+++ b/x\n')


def test_a_directory_named_b_is_not_stripped_from_the_old_side() -> None:
    """`a/b/note.md` is a note inside a directory called `b`. Red if both prefixes were stripped in
    turn — every note under a top-level `b/` would be written one directory up."""
    diffs = parse_patch("diff --git a/b/n.md b/b/n.md\n--- a/b/n.md\n+++ b/b/n.md\n@@ -1 +1 @@\n-x\n+y\n")

    assert (diffs[0].old_path, diffs[0].new_path) == ("b/n.md", "b/n.md")


def test_a_hunk_whose_context_does_not_match_is_refused_naming_the_line() -> None:
    """The last line of defence behind the pre-flight. Red if context were assumed rather than
    checked: a patch generated against different content would be applied anyway, which is the
    silent lost update the staleness measure exists to prevent."""
    diffs = parse_patch("diff --git a/x.md b/x.md\n--- a/x.md\n+++ b/x.md\n@@ -1,1 +1,1 @@\n-expected\n+new\n")

    with pytest.raises(PatchError, match="line 1"):
        apply_file_diff("something else\n", diffs[0])


def test_a_hunk_declaring_more_lines_than_it_carries_is_refused() -> None:
    """A truncated chunk. Red if the declared counts were ignored — the applier would silently
    write whatever fraction of the edit survived."""
    with pytest.raises(PatchError, match="declares"):
        parse_patch("diff --git a/x.md b/x.md\n--- a/x.md\n+++ b/x.md\n@@ -1,3 +1,3 @@\n a\n")


def test_an_unrecognised_header_line_is_refused_rather_than_skipped() -> None:
    """Red if unknown headers were skipped to the first `@@`: a patch shape this reader has never
    seen would be applied on the assumption it is an ordinary edit."""
    with pytest.raises(PatchError, match="unrecognised header"):
        parse_patch("diff --git a/x.md b/x.md\nGIT-LFS pointer\n--- a/x.md\n+++ b/x.md\n@@ -1 +1 @@\n-a\n+b\n")


def test_a_patch_with_no_file_diffs_is_refused() -> None:
    """Red if an empty patch produced an empty write plan: the chunk would be acked as applied
    having written nothing, and the work would be gone."""
    with pytest.raises(PatchError, match="no file diffs"):
        parse_patch("")


# --- already applied: the chunk whose work is done ------------------------------------------------
#
# The same oracle as above, asked the other way round. What the vault is claimed to hold is read off
# git's own working tree, so nothing about "exactly what this patch would produce" is computed by
# the function under test. The direction of every row is the point: `True` settles a chunk without
# writing, so a wrong `True` is a create silently skipped over content nobody compared.


def test_a_create_whose_target_already_holds_what_git_staged_is_already_applied(repo: Path) -> None:
    """The convergence property, at its root: a re-imported create whose note is already there,
    byte for byte, is work already done. Red if this were false — a producer re-run would park every
    chunk it had already applied, and each parked chunk seeds the paths that park the next."""
    commit(repo, {"seed.md": b"seed\n"})
    stage(repo, write={"05-raw/imported.md": b"one\ntwo\nthree\n"})
    chunk = one_chunk(repo)

    assert already_applied(chunk, {"05-raw/imported.md": on_disk(repo, "05-raw/imported.md")})


def test_a_create_whose_target_holds_anything_else_is_not_already_applied(repo: Path) -> None:
    """The create-only rule, undiminished. Red here is the whole danger of this mechanism: a second
    import would be acked over a first one's differing note, which is the silent lost update
    ADR-0015's write-once layer and ADR-0048's existence check both exist to refuse."""
    commit(repo, {"seed.md": b"seed\n"})
    stage(repo, write={"05-raw/imported.md": b"one\ntwo\nthree\n"})
    chunk = one_chunk(repo)

    assert not already_applied(chunk, {"05-raw/imported.md": "somebody else's import\n"})


def test_a_create_whose_target_differs_only_in_its_trailing_newline_is_not_already_applied(repo: Path) -> None:
    """ "Exactly" is byte-exact, and a trailing newline is the difference most likely to be waved
    through. Red if content were compared loosely — a chunk would be acked while the vault holds
    something the patch did not produce, and nothing downstream would ever say so."""
    commit(repo, {"seed.md": b"seed\n"})
    stage(repo, write={"10-areas/x.md": b"one\ntwo\n"})
    chunk = one_chunk(repo)

    assert not already_applied(chunk, {"10-areas/x.md": "one\ntwo"})


def test_a_chunk_only_half_of_whose_creates_are_present_is_not_already_applied(repo: Path) -> None:
    """Partly done is not done. The chunk is the transaction unit, and half its notes in the vault
    is a state this component settles by refusing rather than by completing: the rest would be
    written against a pre-flight that had already been overruled once. Red if the check were per
    target — a half-applied chunk would be acked and its missing notes owed by nobody."""
    commit(repo, {"seed.md": b"seed\n"})
    stage(repo, write={"10-areas/a.md": b"a\n", "10-areas/b.md": b"b\n"})
    chunk = one_chunk(repo)

    assert not already_applied(chunk, {"10-areas/a.md": on_disk(repo, "10-areas/a.md"), "10-areas/b.md": None})


def test_a_chunk_whose_delete_still_has_its_note_is_not_already_applied(repo: Path) -> None:
    """A delete's post-state is absence, and it is checked. Red if only creates were looked at: a
    chunk that removed a note would be acked with the note still in the vault, and the removal is
    then owed by nobody."""
    commit(repo, {"gone.md": b"gone\n", "keep.md": b"keep\n"})
    stage(repo, remove=("gone.md",))
    chunk = one_chunk(repo)

    assert not already_applied(chunk, {"gone.md": "gone\n"})
    assert already_applied(chunk, {"gone.md": None})


def test_a_rename_is_never_already_applied_even_when_it_looks_done(repo: Path) -> None:
    """A rename's new side is built from the old path's content, which a rename that already
    happened no longer has, so the patch alone does not state what it would produce. Answering from
    the destination's *current* content instead would be inverting the patch against the vault —
    reconciliation, which ADR-0048 removed. Red if this returned true: a rename would be acked
    against a destination nothing had compared."""
    commit(repo, {"old.md": b"one\ntwo\nthree\n"})
    stage(repo, write={"new.md": b"one\nTWO\nthree\n"}, remove=("old.md",))
    chunk = one_chunk(repo)

    assert not already_applied(chunk, {"new.md": "one\nTWO\nthree\n", "old.md": None})


def test_a_modify_is_never_already_applied_even_when_the_note_holds_the_post_image(repo: Path) -> None:
    """The same line, drawn on the same principle: a modify's output is the pre-image plus the
    patch, and the pre-image is what its own application replaced. The cost is stated here rather
    than discovered later — a fully-applied chunk carrying a modify is still rejected on redelivery.
    Red if it returned true, which would mean the post-image had been guessed at from what is
    there now."""
    commit(repo, {"x.md": b"alpha\nbeta\n"})
    stage(repo, write={"x.md": b"alpha\nBETA\n"})
    chunk = one_chunk(repo)

    assert not already_applied(chunk, {"x.md": "alpha\nBETA\n"})


def test_a_target_that_was_never_read_is_not_already_applied(repo: Path) -> None:
    """Nothing is known about a path nobody read, so a question about it cannot answer yes.

    A *delete* is where this is the only thing standing in the way, and why the row is written
    against one: absence is what a done delete looks like, and an unread path is absent from the
    mapping in exactly the same way. Red if a missing key were read as absence — a caller that
    forgot to read a target would have its chunk acked on the strength of a check that never ran,
    and the note it was supposed to remove would stay.
    """
    commit(repo, {"gone.md": b"gone\n", "keep.md": b"keep\n"})
    stage(repo, remove=("gone.md",))
    chunk = one_chunk(repo)

    assert not already_applied(chunk, {})


def test_a_chunk_whose_patch_and_targets_disagree_is_not_already_applied() -> None:
    """`plan_writes` names this disagreement and refuses it; here it simply cannot be a settled
    chunk. Two directions, and the second is the sharp one. A path the targets declare and the patch
    never writes has no produced content to compare against at all. A path the patch creates and the
    targets call a modify would otherwise fall through to the delete branch — absent, therefore
    done — and be acked while nothing was ever written.
    """
    header = "diff --git a/a.md b/a.md\n--- /dev/null\n+++ b/a.md\n@@ -0,0 +1 @@\n+a\n"
    unwritten = Chunk(
        batch_id="b1",
        chunk_index=0,
        chunk_count=1,
        produced_at="2026-09-05T12:00:00+00:00",
        patch=header,
        targets=(ChunkTarget("a.md", TargetOperation.CREATE, None), ChunkTarget("b.md", TargetOperation.CREATE, None)),
    )
    miscalled = Chunk(
        batch_id="b1",
        chunk_index=0,
        chunk_count=1,
        produced_at="2026-09-05T12:00:00+00:00",
        patch=header,
        targets=(ChunkTarget("a.md", TargetOperation.MODIFY, "0" * 64),),
    )

    assert not already_applied(unwritten, {"a.md": "a\n", "b.md": None})
    assert not already_applied(miscalled, {"a.md": None})
