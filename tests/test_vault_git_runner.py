"""`GitRunner`'s invocation boundary: what happens when the OS refuses the call before git runs.

Every other test file in this suite exercises git's own behaviour — exit statuses, pathspecs,
attributes. These cover the layer underneath that, where there is no git process at all: `GitRunner`
pins each subprocess's `cwd` to `work_tree` (see `vault_git/runner.py`'s header comment for why),
and `work_tree` is not something this codebase can assume exists. The committer's is `/vault/brain`,
a directory *inside* a `readOnly: true` volume mount, absent on a freshly provisioned or restored
PVC; the vault's own volume is a soft-mounted NFS export that can return ETIMEDOUT/ESTALE/EIO for
any path on it at any moment (`obsidian_tools/retry.py`). A `cwd` the OS refuses raises `OSError`
out of `subprocess.run` *before* `exec` — not a `GitCommandError`, so unhandled it is retried by
nothing and classified as nothing.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

import pytest

import obsidian_tools.retry as retry_module
from obsidian_tools.commands.commit import _staging_error_kind  # pyright: ignore[reportPrivateUsage]
from obsidian_tools.retry import RetryExhaustedError
from obsidian_tools.vault_git.git_errors import ErrorKind, classify_git_error
from obsidian_tools.vault_git.runner import GitCommandError, GitInvocationError, GitRunner


def _bare_runner(tmp_path: Path, work_tree: Path) -> GitRunner:
    """A runner over a real, initialised git-dir in the committer's detached bare layout, whose
    work tree is whatever `work_tree` names — including something that isn't a usable directory."""
    git_dir = tmp_path / "git-dir"
    git_dir.mkdir()
    runner = GitRunner(git_dir, work_tree)
    runner.run(["init", "--bare", "-q", "--initial-branch=main"], include_work_tree=False)
    runner.run(["config", "user.name", "test-committer"])
    runner.run(["config", "user.email", "test-committer@example.invalid"])
    return runner


def _unusable_work_tree(tmp_path: Path, kind: str) -> Path:
    """One of the three ways the OS refuses a directory as `cwd`, as a real filesystem state rather
    than a patched exception: ENOENT, ENOTDIR, EACCES. The soft-mount's own ETIMEDOUT/ESTALE/EIO
    cannot be staged locally, but they arrive at the same `subprocess.run` call as the same
    `OSError`."""
    path = tmp_path / kind
    if kind == "not_a_directory":
        path.write_text("the vault path is a file, not a directory\n")
    elif kind == "unenterable":
        path.mkdir()
        path.chmod(0)
    return path


def test_a_git_call_that_needs_no_work_tree_still_runs_when_the_work_tree_is_absent(tmp_path: Path) -> None:
    """The behaviour the whole graceful-degradation path rests on. Provisioning — `init --bare`,
    `config`, `fetch`, `update-ref`, `read-tree` — touches the git-dir cache volume only and has no
    use for the work tree at all, so a missing vault directory must not stop it; the run needs the
    cache recovered and needs to reach `push_all` regardless. Pinning `cwd` to the work tree without
    a fallback made the *first* of those calls die before git started."""
    runner = _bare_runner(tmp_path, tmp_path / "absent")

    result = runner.run(["config", "--local", "--get", "user.name"])

    assert result.returncode == 0
    assert result.stdout.strip() == "test-committer"


@pytest.mark.parametrize("kind", ["absent", "not_a_directory", "unenterable"])
def test_a_work_tree_the_os_refuses_still_reaches_git_and_fails_as_a_classified_git_error(
    tmp_path: Path, kind: str
) -> None:
    """A call that genuinely needs the work tree must fail the way git fails — a `GitCommandError`
    carrying git's own stderr, which `classify_git_error` can then attribute to the right subsystem
    — rather than as an `OSError` raised before git ran, which nothing in this codebase catches.
    All three refusals are real filesystem states, and git's own message is identical for each."""
    work_tree = _unusable_work_tree(tmp_path, kind)

    try:
        runner = _bare_runner(tmp_path, work_tree)
        with pytest.raises(GitCommandError) as caught:
            runner.run(["add", "--all"])
    finally:
        if work_tree.is_dir():
            work_tree.chmod(0o700)  # restore so tmp_path cleanup can remove it, however this exits

    assert not isinstance(caught.value, GitInvocationError)  # git ran and exited; it was not refused
    assert classify_git_error(caught.value.result.stderr) is ErrorKind.WORK_TREE_UNUSABLE


def test_the_stand_in_working_directory_does_not_reopen_the_hole_the_cwd_pin_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback is only allowed to stand in for `work_tree` if it is equivalent *for the reason
    the pin exists*: git's per-directory `.gitattributes` probe is relative to the launching
    process's cwd, so a same-named file sitting there overrides what the repository itself says
    (`vault_git/runner.py`). `diff --cached` is precisely the call that both needs no work tree and
    resolves attributes, so it is the one that runs on the fallback path *and* can be corrupted by
    it — a fallback of "leave cwd alone" would restore the graceful degradation and silently give
    the hostile file back its win.

    An empty, private, freshly created directory has no `.gitattributes` of its own, so git falls
    back to the index's copy, which is what the pin was protecting all along. Asserted here with the
    hostility owned by this test — a `.gitattributes` it creates and points cwd at deliberately —
    rather than inherited from wherever pytest happens to be running."""
    work_tree = tmp_path / "vault"
    work_tree.mkdir()
    runner = _bare_runner(tmp_path, work_tree)
    (work_tree / ".gitattributes").write_text("*.md -diff\n")  # the vault opts markdown out of diffing
    (work_tree / "note.md").write_text("hello prose\n")
    runner.run(["add", "--all"])
    runner.run(["commit", "--quiet", "--message", "seed"])
    (work_tree / "note.md").write_text("changed prose\n")
    runner.run(["add", "--all"])

    shutil.rmtree(work_tree)  # the vault volume goes away between staging and reading the patch

    hostile = tmp_path / "hostile-launch-directory"
    hostile.mkdir()
    (hostile / ".gitattributes").write_text("*.md diff\n")  # contradicts the vault's own rule
    monkeypatch.chdir(hostile)

    patch = runner.staged_patch(":(literal)note.md")

    assert "Binary files" in patch  # the vault's `-diff`, honoured from the index
    assert "changed prose" not in patch  # the hostile file at cwd did not get to re-enable diffing


def test_an_os_refusal_the_fallback_cannot_fix_is_retried_and_classified_instead_of_escaping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`run`'s retry envelope covers `GitCommandError` only, and `subprocess.run`'s own `OSError`
    is raised outside it — so before this, an ENOENT/ETIMEDOUT/ESTALE on the work tree was neither
    retried nor classified nor caught anywhere, on a mount whose module docstring calls exactly that
    class of failure "expected, not exceptional".

    Both failures here are real, not patched exceptions: the work tree is genuinely absent, and the
    system temp directory is pointed somewhere genuinely absent too, so the stand-in directory
    cannot be created either. What survives that is translated into `GitInvocationError` — a
    `GitCommandError` subclass, which is what puts it inside the retry envelope and inside every
    `except` clause this codebase already has — and classified from its own type, since there is no
    git stderr when there was no git."""
    monkeypatch.setattr(retry_module, "DEFAULT_RETRIES", 3)
    monkeypatch.setattr(retry_module, "DEFAULT_BASE_DELAY_SECONDS", 0.01)
    runner = _bare_runner(tmp_path, tmp_path / "absent")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "no-temp-directory-here"))

    with caplog.at_level(logging.WARNING), pytest.raises(RetryExhaustedError) as caught:
        runner.run(["add", "--all"], retry=True)

    invocation_error = caught.value.__cause__
    assert isinstance(invocation_error, GitInvocationError)
    assert isinstance(invocation_error, GitCommandError)  # so every existing handler catches it
    assert invocation_error.cwd == tmp_path / "absent"  # names the work tree, not the stand-in
    assert isinstance(invocation_error.os_error, FileNotFoundError)
    assert "exited" not in str(invocation_error)  # there was no process, so no exit status to claim

    assert [getattr(record, "event", None) for record in caplog.records].count("retry") == 2  # 3 attempts
    assert _staging_error_kind(caught.value) is ErrorKind.INVOCATION_FAILED
