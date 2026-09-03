"""End-to-end tests for the `commit` subcommand's orchestration (obsidian_tools/commands/commit.py):
provisioning, staging, committing, pushing, and the two failure-mode behaviours a CronJob run
depends on — a persistent read failure must fail the run without a partial commit, and a failed
push must fail the run while leaving the commit in place for the next one to carry.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from conftest import commit_count

import obsidian_tools.retry as retry_module
from obsidian_tools.commands import commit as commit_command
from obsidian_tools.commands.commit import (
    _STAGING_FAILURE_LOG,  # pyright: ignore[reportPrivateUsage]
    is_index_lock_error,
)
from obsidian_tools.config import CommitConfig
from obsidian_tools.retry import RetryExhaustedError
from obsidian_tools.vault_git.commit import DEFAULT_MAX_DELETION_FRACTION
from obsidian_tools.vault_git.git_errors import ErrorKind
from obsidian_tools.vault_git.known_hosts import KnownHostsError
from obsidian_tools.vault_git.runner import GitCommandError


def _config(
    git_dir: Path,
    vault_dir: Path,
    *,
    origin_url: str,
    max_deletion_fraction: float = DEFAULT_MAX_DELETION_FRACTION,
) -> CommitConfig:
    return CommitConfig(
        git_dir=str(git_dir),
        vault_dir=str(vault_dir),
        branch="main",
        author_name="test-committer",
        author_email="test-committer@example.invalid",
        origin_url=origin_url,
        # Local file-path remotes in these tests never actually shell out over SSH, so this path is
        # never opened as a key; it only needs to exist as a string for build_ssh_command to format.
        ssh_key_path="/dev/null",
        # A real path known_hosts.assemble_known_hosts can write to -- these tests' remotes are
        # local filesystem paths, never github.com, so no network fetch ever happens (see
        # test_vault_git_known_hosts.py for that seam); this only has to be writable.
        ssh_known_hosts_path=str(vault_dir.parent / "known_hosts"),
        ssh_known_hosts_extra="",
        max_deletion_fraction=max_deletion_fraction,
    )


def test_known_hosts_fetch_failure_fails_the_run_without_touching_git_at_all(
    tmp_path: Path,
    seeded_origin: Path,
    vault_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The behaviour this whole feature exists for, exercised at the same seam a CronJob run
    actually takes: a GitHub host-key fetch failure must fail the run loudly rather than proceed
    with an unverified connection (obsidian_tools/vault_git/known_hosts.py's module docstring).
    `test_vault_git_known_hosts.py` proves `assemble_known_hosts` itself raises and writes nothing;
    this proves `run()` catches that, logs it, and returns non-zero *before* provisioning ever
    touches git -- not merely that some later git call then fails."""
    git_dir = tmp_path / "git-dir"
    (vault_dir / "10-areas").mkdir()
    (vault_dir / "10-areas" / "note.md").write_text("# Note\n")
    commits_before = commit_count(seeded_origin)

    def _raise(**_kwargs: object) -> Path:
        raise KnownHostsError("simulated GitHub outage")

    monkeypatch.setattr(commit_command, "assemble_known_hosts", _raise)

    with caplog.at_level(logging.ERROR):
        exit_code = commit_command.run(_config(git_dir, vault_dir, origin_url=str(seeded_origin)))

    assert exit_code == 1
    assert commit_count(seeded_origin) == commits_before  # nothing pushed
    assert not git_dir.exists()  # provisioning never ran -- the git-dir cache was never even created
    events = [getattr(record, "event", None) for record in caplog.records]
    assert "known_hosts_failed" in events


def test_full_cycle_commits_and_pushes_to_origin(tmp_path: Path, seeded_origin: Path, vault_dir: Path) -> None:
    git_dir = tmp_path / "git-dir"
    (vault_dir / "10-areas").mkdir()
    (vault_dir / "10-areas" / "note.md").write_text("# Note\n")

    exit_code = commit_command.run(_config(git_dir, vault_dir, origin_url=str(seeded_origin)))

    assert exit_code == 0
    assert commit_count(seeded_origin) == 2


def test_repeated_runs_with_no_new_content_stay_exit_zero_with_no_empty_commits(
    tmp_path: Path, seeded_origin: Path, vault_dir: Path
) -> None:
    git_dir = tmp_path / "git-dir"
    config = _config(git_dir, vault_dir, origin_url=str(seeded_origin))

    assert commit_command.run(config) == 0
    commits_after_first_run = commit_count(seeded_origin)

    assert commit_command.run(config) == 0  # nothing changed on the volume between runs
    assert commit_count(seeded_origin) == commits_after_first_run


def test_persistent_read_failure_exits_nonzero_without_partial_commit(
    tmp_path: Path,
    seeded_origin: Path,
    vault_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real defaults would make this test wait tens of seconds on backoff; turning the tuning down
    # still exercises real retry-then-fail behaviour against a real, persistent filesystem denial.
    monkeypatch.setattr(retry_module, "DEFAULT_RETRIES", 2)
    monkeypatch.setattr(retry_module, "DEFAULT_BASE_DELAY_SECONDS", 0.01)

    git_dir = tmp_path / "git-dir"
    before = commit_count(seeded_origin)

    (vault_dir / "10-areas").mkdir()
    unreadable = vault_dir / "10-areas" / "unreadable.md"
    unreadable.write_text("# Unreadable\n")
    unreadable.chmod(0)  # a real, persistent read failure — not mocked; this uid owns but can't read it

    try:
        exit_code = commit_command.run(_config(git_dir, vault_dir, origin_url=str(seeded_origin)))
    finally:
        unreadable.chmod(stat.S_IRUSR | stat.S_IWUSR)  # restore so tmp_path cleanup can remove it

    assert exit_code == 1
    assert commit_count(seeded_origin) == before  # no partial commit reached either remote
    assert commit_count(git_dir) == before  # and none sits stranded locally either


def test_persistent_read_failure_is_logged_as_a_vault_problem_not_a_lock(
    tmp_path: Path,
    seeded_origin: Path,
    vault_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Wire test for `classify_git_error` (`vault_git/git_errors.py`) actually reaching the log line
    `run()` emits — not just the classifier in isolation (already table-tested in
    `test_vault_git_errors.py`) and not just `is_index_lock_error` distinguishing True/False
    (`test_is_index_lock_error_tells_a_lock_apart_from_an_ordinary_read_failure` below). Before this
    was wired up, this exact scenario -- a real, unreadable vault file, reached through the real
    retry-then-fail path -- was logged as "staging failed, likely a persistent vault read error",
    which is the one case here where that message happens to be right; the bug this test (together
    with the two immediately below, for the git-dir-volume cases) guards is the *other* three kinds
    all being folded into that same sentence regardless of which subsystem actually failed."""
    monkeypatch.setattr(retry_module, "DEFAULT_RETRIES", 2)
    monkeypatch.setattr(retry_module, "DEFAULT_BASE_DELAY_SECONDS", 0.01)

    git_dir = tmp_path / "git-dir"

    (vault_dir / "10-areas").mkdir()
    unreadable = vault_dir / "10-areas" / "unreadable.md"
    unreadable.write_text("# Unreadable\n")
    unreadable.chmod(0)  # a real, persistent read failure — not mocked; this uid owns but can't read it

    try:
        with caplog.at_level(logging.ERROR):
            exit_code = commit_command.run(_config(git_dir, vault_dir, origin_url=str(seeded_origin)))
    finally:
        unreadable.chmod(stat.S_IRUSR | stat.S_IWUSR)  # restore so tmp_path cleanup can remove it

    assert exit_code == 1
    events = [getattr(record, "event", None) for record in caplog.records]
    assert "stage_failed_vault_read_failure" in events
    assert "stage_failed_locked" not in events
    assert "stage_failed" not in events  # the old, undifferentiated event name must not reappear

    [record] = [r for r in caplog.records if getattr(r, "event", None) == "stage_failed_vault_read_failure"]
    message = record.getMessage().lower()
    assert message.startswith("staging failed: a vault file could not be read")
    assert "the git-dir is locked" not in message  # must not be conflated with a stale lock


def test_transient_read_failure_recovers_and_still_commits(
    tmp_path: Path,
    seeded_origin: Path,
    vault_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read failure that clears up before retries are exhausted must not fail the run."""
    monkeypatch.setattr(retry_module, "DEFAULT_RETRIES", 5)
    monkeypatch.setattr(retry_module, "DEFAULT_BASE_DELAY_SECONDS", 0.01)

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

    exit_code = commit_command.run(_config(git_dir, vault_dir, origin_url=str(seeded_origin)))

    assert exit_code == 0
    assert commit_count(seeded_origin) == 2


def test_a_vault_directory_absent_from_the_volume_degrades_instead_of_dying_at_the_first_git_call(
    tmp_path: Path,
    seeded_origin: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The committer's work tree is `/vault/brain` (`config.py`), which is *not* the mount: the
    CronJob mounts the vault PVC at `/vault` and keeps the vault one directory down, because the
    mount root carries an ext4 `lost+found` this uid cannot read. `/vault/brain` is therefore an
    ordinary directory *inside* a volume this workload mounts `readOnly: true` — it can never
    create it, and on a freshly provisioned or restored `vault-data` PVC (disaster recovery, a new
    cluster, the window before Obsidian has seeded the volume) it simply isn't there yet.

    That must degrade, not wedge, because the run repeats every 15 minutes and self-heals the
    moment the directory appears. Every behaviour asserted below is one the run had before
    `GitRunner` pinned its subprocess `cwd` to the work tree, and each is load-bearing on its own:
    provisioning still completes (so the git-dir cache is recovered/fetched rather than left
    half-built), the baseline step reaches its own "not there yet" branch, staging fails as a
    *classified* staging failure after real retries rather than as a raw traceback, and `push_all`
    still runs so a previous run's stuck-unpushed commit catches up regardless.
    """
    monkeypatch.setattr(retry_module, "DEFAULT_RETRIES", 2)
    monkeypatch.setattr(retry_module, "DEFAULT_BASE_DELAY_SECONDS", 0.01)

    git_dir = tmp_path / "git-dir"
    mount_root = tmp_path / "vault-mount"  # stands in for /vault, the volume mount itself
    mount_root.mkdir()
    vault_dir = mount_root / "brain"  # deliberately never created: the vault directory isn't there yet

    with caplog.at_level(logging.INFO):
        exit_code = commit_command.run(_config(git_dir, vault_dir, origin_url=str(seeded_origin)))

    assert exit_code == 1
    events = [getattr(record, "event", None) for record in caplog.records]

    # Provisioning ran to completion: origin's history is in the recovered cache.
    assert commit_count(git_dir) == 1
    assert "provision_failed" not in events

    assert "baseline_skip_no_obsidian_dir" in events

    # A classified staging failure, reached through the real retry-then-fail path -- not an
    # unhandled exception, and not a generic "unrecognized" line either.
    assert "retry" in events
    assert [event for event in events if event is not None and event.startswith("stage_failed_")] == [
        "stage_failed_work_tree_unusable"
    ]
    [record] = [r for r in caplog.records if getattr(r, "event", None) == "stage_failed_work_tree_unusable"]
    assert "git-dir cache volume" not in record.getMessage().split("not the")[0]  # points at the vault volume

    # push_all still ran.
    assert [getattr(r, "remote", None) for r in caplog.records if getattr(r, "event", None) == "push_succeeded"] == [
        "origin"
    ]


def test_stale_index_lock_from_a_killed_run_is_cleared_and_the_next_run_recovers(
    tmp_path: Path, seeded_origin: Path, vault_dir: Path
) -> None:
    """A run that gets SIGKILLed mid `add`/`commit`/`reset` leaves `$GIT_DIR/index.lock` behind
    (see obsidian_tools/vault_git/provisioning.py). With `concurrencyPolicy: Forbid` and a
    single-writer RWO cache PVC, that lock can only be a corpse — the next run must clear it and
    proceed rather than wedging forever on "Unable to create '.../index.lock': File exists"."""
    git_dir = tmp_path / "git-dir"
    git_dir.mkdir(parents=True)
    (git_dir / "index.lock").write_text("")  # simulates a run killed mid write, before cleaning up

    (vault_dir / "10-areas").mkdir()
    (vault_dir / "10-areas" / "note.md").write_text("# Note\n")

    exit_code = commit_command.run(_config(git_dir, vault_dir, origin_url=str(seeded_origin)))

    assert exit_code == 0
    assert not (git_dir / "index.lock").exists()
    assert commit_count(seeded_origin) == 2


def test_stale_config_head_and_branch_locks_from_a_killed_run_are_all_cleared(
    tmp_path: Path, seeded_origin: Path, vault_dir: Path
) -> None:
    """A SIGKILL can strand more than `index.lock`: `config.lock`, `HEAD.lock` and
    `refs/heads/<branch>.lock` are each left behind by the same failure mode, at whatever git
    invocation was in flight when the kill landed. Measured: `config.lock` alone wedges the next
    run's provisioning with exit 1, and `HEAD.lock`/`refs/heads/main.lock` wedge it with an
    uncaught `GitCommandError` traceback. Clearing only `index.lock`, as an earlier revision did,
    leaves the other three to wedge every later run permanently."""
    git_dir = tmp_path / "git-dir"
    git_dir.mkdir(parents=True)
    (git_dir / "index.lock").write_text("")
    (git_dir / "config.lock").write_text("")
    (git_dir / "HEAD.lock").write_text("")
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "refs" / "heads" / "main.lock").write_text("")

    (vault_dir / "10-areas").mkdir()
    (vault_dir / "10-areas" / "note.md").write_text("# Note\n")

    exit_code = commit_command.run(_config(git_dir, vault_dir, origin_url=str(seeded_origin)))

    assert exit_code == 0
    assert not (git_dir / "index.lock").exists()
    assert not (git_dir / "config.lock").exists()
    assert not (git_dir / "HEAD.lock").exists()
    assert not (git_dir / "refs" / "heads" / "main.lock").exists()
    assert commit_count(seeded_origin) == 2


def test_commit_failure_is_caught_and_logged_rather_than_propagating(
    tmp_path: Path,
    seeded_origin: Path,
    vault_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stranded `HEAD.lock` or `refs/heads/<branch>.lock` -- the same SIGKILL-mid-write failure
    mode `index.lock` is cleared for -- used to have no exception handling around the commit call
    at all: `create_commit`'s `GitCommandError` propagated straight out of `run()` as a bare,
    uncaught traceback instead of a logged, attributable event. The commit path must catch it, log
    it, still let a prior run's stuck-unpushed commit's push catch up, and fail the run cleanly."""
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

    exit_code = commit_command.run(_config(git_dir, vault_dir, origin_url=str(seeded_origin)))

    assert exit_code == 1
    assert commit_count(seeded_origin) == 1  # only the pre-existing seed commit; nothing landed


def test_emptied_vault_refuses_to_commit_a_mass_deletion(tmp_path: Path, seeded_origin: Path, vault_dir: Path) -> None:
    """The volume coming back genuinely empty (a re-provisioned or blank-restored PVC, a mis-set
    OBSIDIAN_VAULT_DIR, running before the volume is seeded) must not be committed and pushed as a
    wholesale deletion of the vault's history — `DESIGN.md`'s "fail loud, destroy nothing"
    applies nowhere more than to the one component whose entire job is durability. Content stays
    recoverable in git history either way; the point is that this run must not push the deletion."""
    git_dir = tmp_path / "git-dir"
    config = _config(git_dir, vault_dir, origin_url=str(seeded_origin))

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


def test_every_error_kind_has_its_own_staging_log_line() -> None:
    """`_log_staging_failure` indexes `_STAGING_FAILURE_LOG` directly, so a kind added to
    `ErrorKind` without an entry here raises `KeyError` *inside the handler that exists to keep a
    failed run legible* — turning an attributable failure back into the bare traceback the whole
    split exists to prevent, and only on the failure path, where nothing else would notice. Assert
    totality rather than trusting that whoever grows the enum next also grows the table."""
    assert set(_STAGING_FAILURE_LOG) == set(ErrorKind)
    events = [event for _message, event in _STAGING_FAILURE_LOG.values()]
    assert len(set(events)) == len(events)  # and each kind is distinguishable in the log


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


@pytest.mark.parametrize(
    ("stderr", "expected_event"),
    [
        pytest.param(
            "fatal: Unable to create '/git/vault.git/index.lock': No space left on device",
            "stage_failed_no_space",
            id="full-git-dir-volume",
        ),
        pytest.param(
            "fatal: Unable to create '/git/vault.git/index.lock': Permission denied",
            "stage_failed_permission_denied",
            id="read-only-git-dir-volume",
        ),
    ],
)
def test_a_full_or_read_only_git_dir_volume_is_logged_distinctly_from_a_vault_read_failure(
    tmp_path: Path,
    seeded_origin: Path,
    vault_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    stderr: str,
    expected_event: str,
) -> None:
    """Wire test, the git-dir-volume half of the classifier `run()` must act on -- the companion
    case to `test_persistent_read_failure_is_logged_as_a_vault_problem_not_a_lock`'s real vault
    read failure. A full or read-only git-dir cache PVC is a different failure domain from the
    vault's NFS mount (a different volume, mounted read-write); misdiagnosing it as "a persistent
    vault read error" has already sent an operator chasing NFS twice for what was actually the
    git-dir volume (see `vault_git/git_errors.py`'s module docstring). Neither failure is
    practical to reproduce with real I/O in a test this uid doesn't own the mount for -- unlike the
    vault-read case above, which is real chmod(0) against a real file -- so this drives `run()`'s
    real exception-handling and logging path by raising a `GitCommandError` built from
    `classify_git_error`'s own captured-from-real-git stderr text (the same strings
    `test_vault_git_errors.py` uses) at the seam a fake disk can't reach: `stage_all` itself.
    Matches this file's existing precedent for testing this path
    (`test_commit_failure_is_caught_and_logged_rather_than_propagating` monkeypatches
    `create_commit` the same way, for the analogous HEAD.lock/refs-lock case)."""
    git_dir = tmp_path / "git-dir"
    (vault_dir / "10-areas").mkdir()
    (vault_dir / "10-areas" / "note.md").write_text("# Note\n")

    git_error = _git_command_error(stderr)

    def _raise_git_dir_volume_error(*_args: object, **_kwargs: object) -> None:
        raise git_error

    monkeypatch.setattr(commit_command, "stage_all", _raise_git_dir_volume_error)

    with caplog.at_level(logging.ERROR):
        exit_code = commit_command.run(_config(git_dir, vault_dir, origin_url=str(seeded_origin)))

    assert exit_code == 1
    events = [getattr(record, "event", None) for record in caplog.records]
    assert expected_event in events
    assert "stage_failed" not in events  # the old, undifferentiated event name must not reappear

    [record] = [r for r in caplog.records if getattr(r, "event", None) == expected_event]
    message = record.getMessage().lower()
    assert message.startswith("staging failed: the git-dir cache volume")
    # Regression guard: this exact sentence is what misattributed a git-dir-volume failure to the
    # vault's NFS mount twice before the classifier was wired up (vault_git/git_errors.py).
    assert "persistent vault read error" not in message


def test_push_failure_exits_nonzero_and_leaves_the_local_commit_for_the_next_run(
    tmp_path: Path, seeded_origin: Path, vault_dir: Path
) -> None:
    """A push that fails must not be swallowed: the run exits non-zero, and the commit it already
    took stays in the git-dir cache so the next scheduled run pushes it (`vault_git/commit.py` --
    pushing is unconditional precisely so a stuck backlog catches itself up).

    The failure is real git, not a mock: origin here is an ordinary checked-out clone rather than a
    bare repository, so `git fetch` during provisioning succeeds while `git push` is refused by
    `receive.denyCurrentBranch`. That reaches the push failure through the same code path a genuine
    one would, after provisioning and committing have both succeeded."""
    git_dir = tmp_path / "git-dir"
    non_bare_origin = tmp_path / "non-bare-origin"
    subprocess.run(["git", "clone", "-q", str(seeded_origin), str(non_bare_origin)], check=True)
    # Pinned rather than inherited: `refuse` is git's default, but it resolves through system and
    # global config too, and an override there would make the push succeed -- leaving this test red
    # for a reason unrelated to anything it covers.
    subprocess.run(
        ["git", "-C", str(non_bare_origin), "config", "receive.denyCurrentBranch", "refuse"],
        check=True,
    )

    (vault_dir / "10-areas").mkdir()
    (vault_dir / "10-areas" / "note.md").write_text("# Note\n")

    exit_code = commit_command.run(_config(git_dir, vault_dir, origin_url=str(non_bare_origin)))

    assert exit_code == 1
    assert commit_count(git_dir) == 2  # the commit was taken and is still there for the next run
    assert commit_count(non_bare_origin / ".git") == 1  # and nothing reached the push target


def test_configured_remotes_are_logged_plainly_not_as_a_degraded_warning(
    tmp_path: Path,
    seeded_origin: Path,
    vault_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Running with only `origin` configured is the ordinary shape, not a fallback -- the log line
    must be a plain INFO statement of what's configured, not a WARNING about degraded operation."""
    git_dir = tmp_path / "git-dir"
    (vault_dir / "10-areas").mkdir()
    (vault_dir / "10-areas" / "note.md").write_text("# Note\n")

    with caplog.at_level(logging.INFO):
        commit_command.run(_config(git_dir, vault_dir, origin_url=str(seeded_origin)))

    [record] = [r for r in caplog.records if getattr(r, "event", None) == "remotes_configured"]
    assert record.levelno == logging.INFO
    assert tuple(record.remotes) == ("origin",)  # type: ignore[attr-defined]
    assert "degraded" not in record.getMessage().lower()
    assert "warn" not in record.getMessage().lower()


def test_non_utf8_filename_does_not_wedge_the_committer(tmp_path: Path, seeded_origin: Path, vault_dir: Path) -> None:
    """LATENT (`#22`): `b"caf\\xe9.md"` is a legal filename on a POSIX filesystem but not valid
    UTF-8. `GitRunner.run` used to decode subprocess output with plain `text=True` (strict UTF-8),
    so `git ls-tree -z`/`git diff --cached --name-status -z` -- called from
    `check_for_mass_deletion`'s `list_tree_paths` and (on a later cycle) `build_commit_message` --
    raised a bare `UnicodeDecodeError`. That's neither `GitCommandError` nor `MassDeletionError`, so
    nothing in commands/commit.py's exception handling ever caught it: an uncaught traceback,
    recurring identically every cycle since the file persists on the volume -- the same
    permanent-wedge shape as the symlinked-plugin-directory bug, just triggered by content instead
    of by structure."""
    git_dir = tmp_path / "git-dir"
    (vault_dir / "10-areas").mkdir()
    (vault_dir / "10-areas" / "note.md").write_text("# Note\n")
    non_utf8_path = os.fsencode(str(vault_dir / "10-areas")) + b"/caf\xe9.md"
    fd = os.open(non_utf8_path, os.O_CREAT | os.O_WRONLY, 0o644)
    os.write(fd, "# Café\n".encode())
    os.close(fd)

    exit_code = commit_command.run(_config(git_dir, vault_dir, origin_url=str(seeded_origin)))

    assert exit_code == 0
    assert commit_count(seeded_origin) == 2


def test_max_deletion_fraction_env_var_actually_reaches_the_mass_deletion_guard(
    tmp_path: Path,
    seeded_origin: Path,
    vault_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test gap (`#22`): `commands/commit.py` builds `CommitConfig` from the environment and calls
    `check_for_mass_deletion`, but nothing end to end proved the value actually travels from one to
    the other -- `test_config.py` proves env reaches `CommitConfig.max_deletion_fraction`,
    `test_vault_git_commit.py::test_max_deletion_fraction_at_or_above_one_disables_both_tripwires`
    proves the guard honours a value it's given directly, but severing the
    `max_deletion_fraction=config.max_deletion_fraction` kwarg in `commands/commit.py::run` left
    every one of those 53 tests passing. Confirmed (not assumed) the kwarg is actually wired today
    by reading `commands/commit.py` directly before writing this test. This test goes through
    `CommitConfig.from_env()` and `commit_command.run` -- the actual wire -- with the escape-hatch
    env var set to disable the guard for a genuine archive purge; it fails if that kwarg is ever
    severed again, because a severed wire silently falls back to the guard's own default (0.5),
    which would refuse this exact deletion."""
    git_dir = tmp_path / "git-dir"
    (vault_dir / "10-areas").mkdir()
    for i in range(4):
        (vault_dir / "10-areas" / f"note-{i}.md").write_text(f"# Note {i}\n")
    monkeypatch.setenv("OBSIDIAN_GIT_DIR", str(git_dir))
    monkeypatch.setenv("OBSIDIAN_VAULT_DIR", str(vault_dir))
    monkeypatch.setenv("GIT_COMMIT_BRANCH", "main")
    monkeypatch.setenv("GIT_REMOTE_ORIGIN_URL", str(seeded_origin))
    monkeypatch.setenv("GIT_COMMIT_MAX_DELETION_FRACTION", "1.0")  # the operator escape hatch
    # CommitConfig.ssh_known_hosts_path defaults to ~/.ssh/known_hosts -- pointed at tmp_path here
    # so this test (going through the real CommitConfig.from_env(), not the _config() helper above)
    # never writes into this machine's actual SSH known_hosts file.
    monkeypatch.setenv("GIT_SSH_KNOWN_HOSTS_PATH", str(tmp_path / "known_hosts"))

    assert commit_command.run(CommitConfig.from_env()) == 0
    commits_before = commit_count(seeded_origin)

    # The volume comes back blank -- every markdown note deleted, which trips both tripwires at
    # their default threshold. Only the env var reaching the guard through `commands/commit.py`
    # keeps this from being refused.
    for path in vault_dir.iterdir():
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()

    exit_code = commit_command.run(CommitConfig.from_env())

    assert exit_code == 0
    assert commit_count(seeded_origin) == commits_before + 1


def test_an_unreadable_obsidian_subtree_defers_the_baseline_without_failing_the_run(
    tmp_path: Path,
    seeded_origin: Path,
    vault_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """BROKEN, reproduced (`ppat/obsidian-tools#35`), at the seam a CronJob run actually takes: an
    unreadable directory under `.obsidian/` used to be dropped from the walk silently, and this
    run committed and pushed the rest as the baseline -- after which
    `ensure_obsidian_baseline`'s `path_exists_at("HEAD", ".obsidian")` guard is true forever and the
    omission can never be retried.

    It also pins the two orchestration decisions the refusal makes, neither of which
    `test_vault_git_baseline.py` can see, since both are about the *rest* of the run:

    - **The exit code does not change.** The run's job is committing vault content, and it did that.
      A non-zero exit marks the Job failed and buys a `backoffLimit` retry of a cycle that succeeded
      at everything it was for; the deferred baseline is retried by the next scheduled run anyway,
      and soft-mount I/O failure is the documented *expected* condition on this volume
      (ADR-0033), not a run failure.
    - **Nothing else in the run is skipped.** A `.obsidian/` problem stopping vault content being
      committed is precisely the wedge `ppat/obsidian-tools#22` cost twice.

    The warning is the only channel that carries the deferral, which is why its content is asserted
    here rather than just its presence."""
    git_dir = tmp_path / "git-dir"
    obsidian = vault_dir / ".obsidian"
    obsidian.mkdir()
    (obsidian / "app.json").write_text('{"legacyEditor": false}\n')
    denied = obsidian / "plugins" / "dataview"
    denied.mkdir(parents=True)
    (denied / "manifest.json").write_text('{"id": "dataview"}\n')
    (vault_dir / "10-areas").mkdir()
    (vault_dir / "10-areas" / "note.md").write_text("# Note\n")
    denied.chmod(0)

    try:
        with caplog.at_level(logging.INFO):
            exit_code = commit_command.run(_config(git_dir, vault_dir, origin_url=str(seeded_origin)))

        assert exit_code == 0
        assert commit_count(seeded_origin) == 2  # the vault content still reached origin
        committed = subprocess.run(
            ["git", f"--git-dir={git_dir}", "ls-tree", "-r", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        assert "10-areas/note.md" in committed
        assert not any(path.startswith(".obsidian") for path in committed), (
            "a partial .obsidian/ baseline reached history; the HEAD guard now freezes it forever"
        )
        refusals = [
            record for record in caplog.records if getattr(record, "event", None) == "baseline_refused_incomplete_walk"
        ]
        assert len(refusals) == 1
        assert refusals[0].levelno == logging.WARNING
        assert getattr(refusals[0], "unreadable_paths") == [str(denied)]  # noqa: B009 -- LogRecord attr
    finally:
        denied.chmod(stat.S_IRWXU)  # tmp_path cleanup
