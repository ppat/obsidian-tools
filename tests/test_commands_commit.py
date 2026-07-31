"""End-to-end tests for the `commit` subcommand's orchestration (obsidian_tools/commands/commit.py):
provisioning, staging, committing, pushing, and the two failure-mode behaviours a CronJob run
depends on — a persistent read failure must fail the run without a partial commit, and a push
failure on one remote must not block the other while still failing the run overall.
"""

from __future__ import annotations

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
from obsidian_tools.vault_git.runner import GitCommandError


def _config(git_dir: Path, vault_dir: Path, *, origin_url: str, nas_url: str) -> CommitConfig:
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
