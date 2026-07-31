"""End-to-end tests for the `commit` subcommand's orchestration (obsidian_tools/commands/commit.py):
provisioning, staging, committing, pushing, and the two failure-mode behaviours a CronJob run
depends on — a persistent read failure must fail the run without a partial commit, and a push
failure on one remote must not block the other while still failing the run overall.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import commit_count

import obsidian_tools.retry as retry_module
from obsidian_tools.commands import commit as commit_command
from obsidian_tools.commands.commit import is_index_lock_error
from obsidian_tools.config import CommitConfig
from obsidian_tools.retry import RetryExhaustedError
from obsidian_tools.vault_git.commit import DEFAULT_MAX_DELETION_FRACTION
from obsidian_tools.vault_git.runner import GitCommandError


def _config(
    git_dir: Path,
    vault_dir: Path,
    *,
    origin_url: str,
    nas_url: str,
    max_deletion_fraction: float = DEFAULT_MAX_DELETION_FRACTION,
) -> CommitConfig:
    return CommitConfig(
        git_dir=str(git_dir),
        vault_dir=str(vault_dir),
        branch="main",
        author_name="test-committer",
        author_email="test-committer@example.invalid",
        origin_url=origin_url,
        nas_url=nas_url,
        # Local file-path remotes in these tests never actually shell out over SSH, so these paths
        # are never opened; they only need to exist as strings for build_ssh_command to format.
        ssh_key_path="/dev/null",
        ssh_known_hosts_path="/dev/null",
        max_deletion_fraction=max_deletion_fraction,
    )


def test_full_cycle_commits_and_pushes_to_both_remotes(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    (vault_dir / "10-areas").mkdir()
    (vault_dir / "10-areas" / "note.md").write_text("# Note\n")

    exit_code = commit_command.run(_config(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas)))

    assert exit_code == 0
    assert commit_count(seeded_origin) == 2
    assert commit_count(nas) == 2


def test_repeated_runs_with_no_new_content_stay_exit_zero_with_no_empty_commits(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    config = _config(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))

    assert commit_command.run(config) == 0
    commits_after_first_run = commit_count(seeded_origin)

    assert commit_command.run(config) == 0  # nothing changed on the volume between runs
    assert commit_count(seeded_origin) == commits_after_first_run


def test_persistent_read_failure_exits_nonzero_without_partial_commit(
    tmp_path: Path,
    seeded_origin: Path,
    make_bare_repo: Callable[[], Path],
    vault_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real defaults would make this test wait tens of seconds on backoff; turning the tuning down
    # still exercises real retry-then-fail behaviour against a real, persistent filesystem denial.
    monkeypatch.setattr(retry_module, "DEFAULT_RETRIES", 2)
    monkeypatch.setattr(retry_module, "DEFAULT_BASE_DELAY_SECONDS", 0.01)

    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    before = commit_count(seeded_origin)

    (vault_dir / "10-areas").mkdir()
    unreadable = vault_dir / "10-areas" / "unreadable.md"
    unreadable.write_text("# Unreadable\n")
    unreadable.chmod(0)  # a real, persistent read failure — not mocked; this uid owns but can't read it

    try:
        exit_code = commit_command.run(_config(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas)))
    finally:
        unreadable.chmod(stat.S_IRUSR | stat.S_IWUSR)  # restore so tmp_path cleanup can remove it

    assert exit_code == 1
    assert commit_count(seeded_origin) == before  # no partial commit reached either remote
    assert commit_count(git_dir) == before  # and none sits stranded locally either


def test_transient_read_failure_recovers_and_still_commits(
    tmp_path: Path,
    seeded_origin: Path,
    make_bare_repo: Callable[[], Path],
    vault_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read failure that clears up before retries are exhausted must not fail the run."""
    monkeypatch.setattr(retry_module, "DEFAULT_RETRIES", 5)
    monkeypatch.setattr(retry_module, "DEFAULT_BASE_DELAY_SECONDS", 0.01)

    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    (vault_dir / "10-areas").mkdir()
    flaky = vault_dir / "10-areas" / "flaky.md"
    flaky.write_text("# Flaky\n")
    flaky.chmod(0)

    real_sleep = __import__("time").sleep

    def fix_permissions_then_sleep(seconds: float) -> None:
        flaky.chmod(stat.S_IRUSR | stat.S_IWUSR)
        real_sleep(seconds)

    monkeypatch.setattr(retry_module.time, "sleep", fix_permissions_then_sleep)

    exit_code = commit_command.run(_config(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas)))

    assert exit_code == 0
    assert commit_count(seeded_origin) == 2


def test_stale_index_lock_from_a_killed_run_is_cleared_and_the_next_run_recovers(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    """A run that gets SIGKILLed mid `add`/`commit`/`reset` leaves `$GIT_DIR/index.lock` behind
    (see obsidian_tools/vault_git/provisioning.py). With `concurrencyPolicy: Forbid` and a
    single-writer RWO cache PVC, that lock can only be a corpse — the next run must clear it and
    proceed rather than wedging forever on "Unable to create '.../index.lock': File exists"."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    git_dir.mkdir(parents=True)
    (git_dir / "index.lock").write_text("")  # simulates a run killed mid write, before cleaning up

    (vault_dir / "10-areas").mkdir()
    (vault_dir / "10-areas" / "note.md").write_text("# Note\n")

    exit_code = commit_command.run(_config(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas)))

    assert exit_code == 0
    assert not (git_dir / "index.lock").exists()
    assert commit_count(seeded_origin) == 2


def test_stale_config_head_and_branch_locks_from_a_killed_run_are_all_cleared(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    """A SIGKILL can strand more than `index.lock`: `config.lock`, `HEAD.lock` and
    `refs/heads/<branch>.lock` are each left behind by the same failure mode, at whatever git
    invocation was in flight when the kill landed. Measured: `config.lock` alone wedges the next
    run's provisioning with exit 1, and `HEAD.lock`/`refs/heads/main.lock` wedge it with an
    uncaught `GitCommandError` traceback. Clearing only `index.lock`, as an earlier revision did,
    leaves the other three to wedge every later run permanently."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    git_dir.mkdir(parents=True)
    (git_dir / "index.lock").write_text("")
    (git_dir / "config.lock").write_text("")
    (git_dir / "HEAD.lock").write_text("")
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "refs" / "heads" / "main.lock").write_text("")

    (vault_dir / "10-areas").mkdir()
    (vault_dir / "10-areas" / "note.md").write_text("# Note\n")

    exit_code = commit_command.run(_config(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas)))

    assert exit_code == 0
    assert not (git_dir / "index.lock").exists()
    assert not (git_dir / "config.lock").exists()
    assert not (git_dir / "HEAD.lock").exists()
    assert not (git_dir / "refs" / "heads" / "main.lock").exists()
    assert commit_count(seeded_origin) == 2


def test_commit_failure_is_caught_and_logged_rather_than_propagating(
    tmp_path: Path,
    seeded_origin: Path,
    make_bare_repo: Callable[[], Path],
    vault_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stranded `HEAD.lock` or `refs/heads/<branch>.lock` -- the same SIGKILL-mid-write failure
    mode `index.lock` is cleared for -- used to have no exception handling around the commit call
    at all: `create_commit`'s `GitCommandError` propagated straight out of `run()` as a bare,
    uncaught traceback instead of a logged, attributable event. The commit path must catch it, log
    it, still let a prior run's stuck-unpushed commit's push catch up, and fail the run cleanly."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    (vault_dir / "10-areas").mkdir()
    (vault_dir / "10-areas" / "note.md").write_text("# Note\n")

    def _raise_locked(*_args: object, **_kwargs: object) -> str:
        result = subprocess.CompletedProcess(
            args=["git", "commit"],
            returncode=128,
            stdout="",
            stderr="fatal: Unable to create '/git/vault.git/refs/heads/main.lock': File exists.",
        )
        raise GitCommandError(["commit"], result)

    monkeypatch.setattr(commit_command, "create_commit", _raise_locked)

    exit_code = commit_command.run(_config(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas)))

    assert exit_code == 1
    assert commit_count(seeded_origin) == 1  # only the pre-existing seed commit; nothing landed


def test_emptied_vault_refuses_to_commit_a_mass_deletion(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    """The volume coming back genuinely empty (a re-provisioned or blank-restored PVC, a mis-set
    OBSIDIAN_VAULT_DIR, running before the volume is seeded) must not be committed and pushed as a
    wholesale deletion of the vault's history — `docs/DESIGN.md`'s "fail loud, destroy nothing"
    applies nowhere more than to the one component whose entire job is durability. Content stays
    recoverable in git history either way; the point is that this run must not push the deletion."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    config = _config(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))

    (vault_dir / "10-areas").mkdir()
    for i in range(4):
        (vault_dir / "10-areas" / f"note-{i}.md").write_text(f"# Note {i}\n")
    assert commit_command.run(config) == 0
    commits_before = commit_count(seeded_origin)

    # The volume comes back blank -- nothing distinguishes this from "a human deleted one note".
    for path in vault_dir.iterdir():
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()

    exit_code = commit_command.run(config)

    assert exit_code == 1
    assert commit_count(seeded_origin) == commits_before  # the deletion was never pushed
    assert commit_count(git_dir) == commits_before  # nor committed locally


def _git_command_error(stderr: str) -> GitCommandError:
    result = subprocess.CompletedProcess(args=["git", "add"], returncode=128, stdout="", stderr=stderr)
    return GitCommandError(["add", "-A"], result)


def test_is_index_lock_error_tells_a_lock_apart_from_an_ordinary_read_failure() -> None:
    """A stale `index.lock` and a persistent NFS read failure must not be logged as the same thing
    — they point a human at completely different fixes. Regression test for the `except` clause
    that used to lump both under "likely a persistent vault read error"."""
    lock_error = _git_command_error("fatal: Unable to create '/git/vault.git/index.lock': File exists.")
    assert is_index_lock_error(lock_error) is True

    read_error = _git_command_error('error: open("10-areas/note.md"): Permission denied')
    assert is_index_lock_error(read_error) is False

    # RetryExhaustedError chains the underlying GitCommandError as __cause__ (see obsidian_tools/retry.py) —
    # the classification has to unwrap it, not just check the retry wrapper's own message.
    wrapped_lock_error = RetryExhaustedError("git add -A failed after 5 attempts")
    wrapped_lock_error.__cause__ = lock_error
    assert is_index_lock_error(wrapped_lock_error) is True


def test_is_index_lock_error_does_not_misattribute_a_full_or_read_only_git_dir() -> None:
    """A full or read-only git-dir PVC fails with `index.lock` in the message too -- it's the path
    git was trying to create -- but with `No space left on device` or `Permission denied` instead
    of `File exists`. Matching the lock path alone points a human at "the git-dir is locked" when
    the actual problem is the volume; `File exists` is what actually distinguishes the two."""
    disk_full_error = _git_command_error("fatal: Unable to create '/git/vault.git/index.lock': No space left on device")
    assert is_index_lock_error(disk_full_error) is False

    read_only_error = _git_command_error("fatal: Unable to create '/git/vault.git/index.lock': Permission denied")
    assert is_index_lock_error(read_only_error) is False


def test_push_failure_on_one_remote_still_attempts_the_other_and_run_exits_nonzero(
    tmp_path: Path, seeded_origin: Path, vault_dir: Path
) -> None:
    git_dir = tmp_path / "git-dir"
    broken_nas_url = str(tmp_path / "does-not-exist.git")

    (vault_dir / "10-areas").mkdir()
    (vault_dir / "10-areas" / "note.md").write_text("# Note\n")

    exit_code = commit_command.run(_config(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=broken_nas_url))

    assert exit_code == 1
    assert commit_count(seeded_origin) == 2  # origin still received the commit despite nas failing


def test_non_utf8_filename_does_not_wedge_the_committer(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    """LATENT (`#22`): `b"caf\\xe9.md"` is a legal filename on a POSIX filesystem but not valid
    UTF-8. `GitRunner.run` used to decode subprocess output with plain `text=True` (strict UTF-8),
    so `git ls-tree -z`/`git diff --cached --name-status -z` -- called from
    `check_for_mass_deletion`'s `list_tree_paths` and (on a later cycle) `build_commit_message` --
    raised a bare `UnicodeDecodeError`. That's neither `GitCommandError` nor `MassDeletionError`, so
    nothing in commands/commit.py's exception handling ever caught it: an uncaught traceback,
    recurring identically every cycle since the file persists on the volume -- the same
    permanent-wedge shape as the symlinked-plugin-directory bug, just triggered by content instead
    of by structure."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    (vault_dir / "10-areas").mkdir()
    (vault_dir / "10-areas" / "note.md").write_text("# Note\n")
    non_utf8_path = os.fsencode(str(vault_dir / "10-areas")) + b"/caf\xe9.md"
    fd = os.open(non_utf8_path, os.O_CREAT | os.O_WRONLY, 0o644)
    os.write(fd, "# Café\n".encode())
    os.close(fd)

    exit_code = commit_command.run(_config(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas)))

    assert exit_code == 0
    assert commit_count(seeded_origin) == 2
