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
        # Staging touches the read-only NFS work tree; `stage_all`/`ensure_obsidian_baseline`
        # already retried transient failures, so reaching here means something never recovered.
        # A stale `index.lock` and a persistent read failure point a human at completely different
        # fixes, so tell them apart here rather than blaming NFS for both (provisioning already
        # clears a stale lock before this point — see vault_git/provisioning.py — so seeing one
        # here means something recreated it after that, not the ordinary case this misattributed).
        # No index reset here: the next run's stage_all() re-runs `git add -A` in full regardless
        # of what a prior partial stage left behind, since -A reconciles the whole index against
        # the current work tree rather than applying incrementally. Still give a previous run's
        # stuck-unpushed commit a chance to catch up before failing.
        if is_index_lock_error(exc):
            logger.exception("staging failed: the git-dir is locked", extra={"event": "stage_failed_locked"})
        else:
            logger.exception("staging failed, likely a persistent vault read error", extra={"event": "stage_failed"})
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


def is_index_lock_error(exc: BaseException) -> bool:
    """True only for the specific "the lock file is already there" failure — git's own message when
    it can't create an `index.lock` because one already exists.

    The actual classification (stale lock vs. no space vs. permission denied vs. a vault read
    failure) lives in `classify_git_error` (`vault_git/git_errors.py`) — pure, over the stderr text
    alone. This function's job is just gathering that text out of whatever exception `commit.py`'s
    `_STAGING_FAILURES` handling caught: either a bare `GitCommandError`, or a `RetryExhaustedError`
    chaining one as `__cause__` once `stage_all`'s retried `git add` has exhausted every attempt.
    """
    git_error = exc if isinstance(exc, GitCommandError) else exc.__cause__
    if not isinstance(git_error, GitCommandError):
        return False
    stderr = git_error.result.stderr or ""
    return classify_git_error(stderr) is ErrorKind.STALE_LOCK
