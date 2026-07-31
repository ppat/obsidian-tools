"""The `commit` subcommand: one idempotent run — provision, stage, commit if needed, push, exit.

Invoked by a Kubernetes CronJob (ppat/obsidian-tools#3, homelab-ops-kubernetes-apps#3443): the run
is the unit of work. No daemon, no sleep loop, no internal scheduling — see the module docstrings
under `obsidian_tools/vault_git/` for why each step is shaped the way it is.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from obsidian_tools.config import CommitConfig
from obsidian_tools.retry import RetryExhaustedError
from obsidian_tools.vault_git.baseline import ensure_obsidian_baseline
from obsidian_tools.vault_git.commit import (
    MassDeletionError,
    check_for_mass_deletion,
    create_commit,
    has_staged_changes,
    push_all,
    stage_all,
)
from obsidian_tools.vault_git.git_errors import ErrorKind, classify_git_error
from obsidian_tools.vault_git.provisioning import GitDivergenceError, provision_repository
from obsidian_tools.vault_git.push_outcome import summarize_push_results
from obsidian_tools.vault_git.runner import GitCommandError, GitRunner
from obsidian_tools.vault_git.ssh import build_ssh_command

# Both raised by staging: GitCommandError from a single failed git invocation (check=True, no
# retry), RetryExhaustedError once stage_all's/ensure_obsidian_baseline's own retried git add has
# exhausted every attempt against a persistently-failing read.
_STAGING_FAILURES = (GitCommandError, RetryExhaustedError)

logger = logging.getLogger(__name__)


def run(config: CommitConfig) -> int:
    git_dir = Path(config.git_dir)
    work_tree = Path(config.vault_dir)
    ssh_command = build_ssh_command(config.ssh_key_path, config.ssh_known_hosts_path)
    runner = GitRunner(git_dir, work_tree, ssh_command=ssh_command)

    try:
        provision_repository(
            runner,
            branch=config.branch,
            author_name=config.author_name,
            author_email=config.author_email,
            origin_url=config.origin_url,
            nas_url=config.nas_url,
        )
    except GitDivergenceError:
        logger.exception(
            "local and origin history have diverged; refusing to guess", extra={"event": "provision_failed"}
        )
        return 1
    except GitCommandError:
        logger.exception("provisioning failed", extra={"event": "provision_failed"})
        return 1

    try:
        ensure_obsidian_baseline(runner, work_tree)
        stage_all(runner)
    except _STAGING_FAILURES as exc:
        # Staging touches the read-only NFS work tree, but the git-dir cache volume is a completely
        # separate failure domain (a different PVC, mounted read-write) — a stale `index.lock`, a
        # full git-dir volume, a read-only git-dir volume, and an actual unreadable vault file all
        # surface here, and each points a human at a different subsystem to fix. Collapsing them
        # into one message has already sent an operator chasing NFS/the vault mount twice for a
        # git-dir volume problem (see vault_git/git_errors.py's module docstring), so every kind
        # `classify_git_error` distinguishes gets its own, actionable log line here rather than
        # folding back into a single generic one (provisioning already clears a stale lock before
        # this point — see vault_git/provisioning.py — so seeing one here means something recreated
        # it after that, not the ordinary case this misattributed).
        # No index reset here: the next run's stage_all() re-runs `git add -A` in full regardless
        # of what a prior partial stage left behind, since -A reconciles the whole index against
        # the current work tree rather than applying incrementally. Still give a previous run's
        # stuck-unpushed commit a chance to catch up before failing.
        _log_staging_failure(exc)
        push_all(runner, branch=config.branch)
        return 1

    try:
        check_for_mass_deletion(runner, max_deletion_fraction=config.max_deletion_fraction)
    except MassDeletionError:
        logger.exception(
            "refusing to commit: staged deletions look like data loss, not an edit",
            extra={"event": "mass_deletion_refused"},
        )
        push_all(runner, branch=config.branch)
        return 1

    cycle_time = datetime.now(UTC)
    committed = False
    if has_staged_changes(runner):
        try:
            create_commit(runner, cycle_time=cycle_time)
        except GitCommandError:
            # Unlike staging, this call was previously unguarded: a stranded HEAD.lock or
            # refs/heads/<branch>.lock (the same SIGKILL-mid-write failure mode index.lock is
            # cleared for, see vault_git/provisioning.py's _clear_stale_locks) would otherwise
            # surface as a bare, uncaught traceback rather than a logged, attributable event.
            logger.exception(
                "commit failed; the git-dir may be locked or otherwise wedged", extra={"event": "commit_failed"}
            )
            push_all(runner, branch=config.branch)  # still let a prior run's stuck commit catch up
            return 1
        committed = True
    else:
        logger.info("nothing to commit this cycle", extra={"event": "nothing_to_commit"})

    push_results = push_all(runner, branch=config.branch)
    outcome = summarize_push_results(push_results)

    logger.info(
        "commit cycle complete",
        extra={"event": "cycle_complete", "committed": committed, "push_failed": outcome.any_failed},
    )
    return 1 if outcome.any_failed else 0


def _staging_error_kind(exc: BaseException) -> ErrorKind:
    """Classify whatever exception `commit.py`'s `_STAGING_FAILURES` handling caught: either a bare
    `GitCommandError`, or a `RetryExhaustedError` chaining one as `__cause__` once `stage_all`'s
    retried `git add` has exhausted every attempt. This function's job is just gathering the stderr
    text out of that exception; the actual classification (stale lock vs. no space vs. permission
    denied vs. a vault read failure) lives in `classify_git_error` (`vault_git/git_errors.py`) —
    pure, over the stderr text alone."""
    git_error = exc if isinstance(exc, GitCommandError) else exc.__cause__
    if not isinstance(git_error, GitCommandError):
        return ErrorKind.UNKNOWN
    stderr = git_error.result.stderr or ""
    return classify_git_error(stderr)


def is_index_lock_error(exc: BaseException) -> bool:
    """True only for the specific "the lock file is already there" failure — git's own message when
    it can't create an `index.lock` because one already exists."""
    return _staging_error_kind(exc) is ErrorKind.STALE_LOCK


# One actionable log line per `ErrorKind`, each naming the subsystem an operator should actually
# look at rather than restating the error — see the `_STAGING_FAILURES` handling in `run()` above
# for why these must stay distinct instead of collapsing back into one generic message.
_STAGING_FAILURE_LOG: dict[ErrorKind, tuple[str, str]] = {
    ErrorKind.STALE_LOCK: (
        "staging failed: the git-dir is locked",
        "stage_failed_locked",
    ),
    ErrorKind.NO_SPACE: (
        "staging failed: the git-dir cache volume is full — check that PVC's free space, not the vault NFS mount",
        "stage_failed_no_space",
    ),
    ErrorKind.PERMISSION_DENIED: (
        "staging failed: the git-dir cache volume is read-only — check that PVC's mount/permissions, not the "
        "vault NFS mount",
        "stage_failed_permission_denied",
    ),
    ErrorKind.VAULT_READ_FAILURE: (
        "staging failed: a vault file could not be read — check the NFS mount and share-manager, not the "
        "git-dir cache volume",
        "stage_failed_vault_read_failure",
    ),
    ErrorKind.UNKNOWN: (
        "staging failed for an unrecognized reason; see the exception traceback for the underlying git stderr",
        "stage_failed_unknown",
    ),
}


def _log_staging_failure(exc: BaseException) -> None:
    message, event = _STAGING_FAILURE_LOG[_staging_error_kind(exc)]
    logger.exception(message, extra={"event": event})
