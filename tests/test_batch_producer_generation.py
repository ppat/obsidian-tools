"""Tests for `obsidian_tools/batch_producer/generation.py` — reading the producer's staged index
through `GitRunner`.

Real git repositories, never a mocked runner: the questions here are all about git's own semantics
(what `--name-status` reports for a rename, what `cat-file blob` returns, what a diff's bytes look
like for a CRLF file), and a mock would only ever agree with whatever this module already believes.
That is the same reasoning `tests/conftest.py` records for the committer's own suite.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from obsidian_tools.batch_producer.generation import PatchEncodingError, collect_patch_units
from obsidian_tools.batch_producer.staleness import TargetOperation
from obsidian_tools.vault_git.runner import GitCommandError, GitRunner

# The setup calls below build the repository these tests then read through `GitRunner`, so they must
# run under the same conditions `GitRunner` reads it under -- otherwise the fixture is written by a
# differently-configured git than the one under test. This bit for real: a developer machine with
# `core.autocrlf=input` in its global config has `git add` clean CRLF to LF *into the blob*, so the
# CRLF tests below passed or failed according to whose machine ran them, and what they appeared to
# prove about this codebase was actually a property of the operator's `~/.gitconfig`. `GitRunner`
# neutralises exactly this (`_GIT_ENV_OVERRIDES`); these calls do the same by hand.
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
    """A repository with one commit, standing in for the workspace checkout a batch is generated
    from. Written with bytes throughout so a test can put a CRLF or non-UTF-8 file in it."""
    work_tree = tmp_path / "work"
    work_tree.mkdir()
    _git("init", "-q", "--initial-branch=main", cwd=work_tree)
    _git("config", "user.email", "t@example.invalid", cwd=work_tree)
    _git("config", "user.name", "t", cwd=work_tree)
    (work_tree / "existing.md").write_bytes(b"first line\nsecond line\n")
    (work_tree / "doomed.md").write_bytes(b"to be deleted\n")
    _git("add", "-A", cwd=work_tree)
    _git("commit", "-qm", "seed", cwd=work_tree)
    return work_tree


def runner_for(work_tree: Path) -> GitRunner:
    return GitRunner(work_tree / ".git", work_tree)


# --- what each staged change produces ------------------------------------------------------------


def test_a_staged_create_carries_no_hash(repo: Path) -> None:
    """ADR-0048's composition: a create has no prior content to hash, so the consumer runs an
    existence check instead. Red if a hash is invented for a path that did not exist — the consumer
    would compare against content that was never there and reject every legitimate create."""
    (repo / "new.md").write_bytes(b"brand new\n")
    _git("add", "-A", cwd=repo)

    units = collect_patch_units(runner_for(repo), base_rev="HEAD")

    (target,) = units[0].targets
    assert (target.path, target.operation, target.base_sha256) == ("new.md", TargetOperation.CREATE, None)


def test_a_staged_modify_carries_the_hash_of_the_pre_image(repo: Path) -> None:
    """The measure itself. Red if the hash is ever taken over the *new* content — the pre-flight
    would then pass only when the vault already holds the change the patch is trying to make."""
    (repo / "existing.md").write_bytes(b"first line\nchanged\n")
    _git("add", "-A", cwd=repo)

    units = collect_patch_units(runner_for(repo), base_rev="HEAD")

    (target,) = units[0].targets
    assert target.operation is TargetOperation.MODIFY
    assert target.base_sha256 == hashlib.sha256(b"first line\nsecond line\n").hexdigest()


def test_a_staged_delete_carries_the_hash_of_what_is_being_deleted(repo: Path) -> None:
    """Red if a delete carried no hash: a file changed by someone else since the patch was generated
    would be deleted anyway, which is the silent lost update ADR-0048's alternatives section names."""
    _git("rm", "-q", "doomed.md", cwd=repo)

    units = collect_patch_units(runner_for(repo), base_rev="HEAD")

    (target,) = units[0].targets
    assert target.operation is TargetOperation.DELETE
    assert target.base_sha256 == hashlib.sha256(b"to be deleted\n").hexdigest()


def test_a_staged_rename_becomes_one_unit_with_two_targets(repo: Path) -> None:
    """Red if a rename ever produces two units. Split across chunks, the destination could be
    created before the source is checked, and neither half would carry the other's pre-flight."""
    _git("mv", "existing.md", "renamed.md", cwd=repo)

    units = collect_patch_units(runner_for(repo), base_rev="HEAD")

    assert len(units) == 1
    assert [(t.path, t.operation) for t in units[0].targets] == [
        ("existing.md", TargetOperation.DELETE),
        ("renamed.md", TargetOperation.CREATE),
    ]
    assert units[0].targets[0].base_sha256 == hashlib.sha256(b"first line\nsecond line\n").hexdigest()


def test_a_rename_is_diffed_as_one_rename_not_an_add_and_a_delete(repo: Path) -> None:
    """Both paths go to one diff invocation so git re-pairs them. Red if they are diffed separately:
    the patch would carry the whole file twice rather than a rename header."""
    _git("mv", "existing.md", "renamed.md", cwd=repo)

    units = collect_patch_units(runner_for(repo), base_rev="HEAD")

    assert "rename from existing.md" in units[0].patch
    assert "rename to renamed.md" in units[0].patch


def test_nothing_staged_produces_no_units(repo: Path) -> None:
    """Red if an unstaged working tree produces a batch — the producer would enqueue an empty run
    every time it was invoked."""
    (repo / "unstaged.md").write_bytes(b"not added\n")

    assert collect_patch_units(runner_for(repo), base_rev="HEAD") == []


def test_each_staged_file_becomes_its_own_unit_in_gits_order(repo: Path) -> None:
    """Red if units are merged or reordered: the packer relies on receiving one atom per file, in
    the order the producer intends, because that order is what carries dependency."""
    (repo / "a.md").write_bytes(b"a\n")
    (repo / "b.md").write_bytes(b"b\n")
    _git("add", "-A", cwd=repo)

    units = collect_patch_units(runner_for(repo), base_rev="HEAD")

    assert [t.path for u in units for t in u.targets] == ["a.md", "b.md"]


# --- the bytes, exactly ---------------------------------------------------------------------------


def test_a_crlf_files_hash_is_over_its_real_bytes(repo: Path) -> None:
    """The defect `GitRunner.run_binary` exists to prevent. Text-mode subprocess collapses `\\r\\n`
    to `\\n` (measured), so a hash taken through it would be of content that was never on disk and
    would mismatch every read the consumer performs. Red the moment the hash is computed from
    decoded text rather than raw bytes."""
    crlf = b"windows line\r\nsecond\r\n"
    (repo / "crlf.md").write_bytes(crlf)
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "add crlf", cwd=repo)
    (repo / "crlf.md").write_bytes(b"windows line\r\nchanged\r\n")
    _git("add", "-A", cwd=repo)

    units = collect_patch_units(runner_for(repo), base_rev="HEAD")

    (target,) = units[0].targets
    assert target.base_sha256 == hashlib.sha256(crlf).hexdigest()


def test_a_crlf_files_patch_keeps_its_carriage_returns(repo: Path) -> None:
    """The same defect on the patch rather than the hash. Red if the patch is read through text
    mode: the chunk would carry a diff that also silently rewrites the file's line endings."""
    (repo / "crlf.md").write_bytes(b"windows line\r\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "add crlf", cwd=repo)
    (repo / "crlf.md").write_bytes(b"windows line\r\nadded\r\n")
    _git("add", "-A", cwd=repo)

    units = collect_patch_units(runner_for(repo), base_rev="HEAD")

    assert "+added\r\n" in units[0].patch


def test_a_non_ascii_path_and_body_round_trip(repo: Path) -> None:
    """Red if non-ASCII content is mangled anywhere between git and the chunk. `core.quotePath`
    C-quoting is the specific failure this guards, and it is on by default."""
    (repo / "café.md").write_bytes("dé\n".encode())
    _git("add", "-A", cwd=repo)

    units = collect_patch_units(runner_for(repo), base_rev="HEAD")

    assert units[0].targets[0].path == "café.md"
    assert "+dé" in units[0].patch


def test_a_path_holding_a_glob_metacharacter_is_diffed_as_itself(repo: Path) -> None:
    """ADR-0043's case: without `:(literal)`, `star*.md` is a pathspec pattern rather than a
    filename. Red if the pathspec is built without it — the diff would silently cover the wrong set
    of files, and the chunk's targets would not describe what its patch writes."""
    (repo / "star*.md").write_bytes(b"literal\n")
    (repo / "starOTHER.md").write_bytes(b"should not be here\n")
    _git("add", "-A", cwd=repo)

    units = collect_patch_units(runner_for(repo), base_rev="HEAD")

    by_path = {u.targets[0].path: u for u in units}
    assert "should not be here" not in by_path["star*.md"].patch


# --- refusals --------------------------------------------------------------------------------------


def test_a_non_utf8_diff_is_refused_naming_the_file(repo: Path) -> None:
    """The chunk is JSON and the gated MCP path is JSON, so the whole downstream is UTF-8-only.
    Red if such a file is carried anyway — `json.dumps` would raise against the whole message and
    name nothing, leaving an operator to find the file by bisection."""
    (repo / "latin.md").write_bytes(b"caf\xe9 not utf-8\n")
    _git("add", "-A", cwd=repo)

    with pytest.raises(PatchEncodingError, match=r"latin\.md"):
        collect_patch_units(runner_for(repo), base_rev="HEAD")


def test_a_base_rev_that_lacks_the_modified_path_is_a_loud_failure(repo: Path) -> None:
    """A pre-image read against the wrong tree is a wrong assumption, not a missing file. Red if it
    were silently treated as "no prior content", which would turn a modify into a create and skip
    the staleness check entirely."""
    (repo / "existing.md").write_bytes(b"changed\n")
    _git("add", "-A", cwd=repo)
    empty_tree = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

    with pytest.raises(GitCommandError):
        collect_patch_units(runner_for(repo), base_rev=empty_tree)
