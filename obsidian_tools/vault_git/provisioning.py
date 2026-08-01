"""Idempotent provisioning of the committer's detached git repository.

**The PVC backing `git_dir` is a derivable cache, not durable state.** It exists purely so a
scheduled run can fetch incrementally instead of re-cloning the vault's full history every time —
that is the only reason it is a PVC rather than an `emptyDir`. Nothing here may assume it survives
between runs: it can be lost or re-provisioned at any point, and a lost cache must be *recovered by
fetching origin's existing history*, never by re-initialising a fresh root commit. Re-rooting would
silently fork history — the next push would either be rejected as non-fast-forward, or, worse,
succeed against a NAS remote that happened to accept an unrelated root and leave the two remotes
permanently divergent while the vault volume itself looked perfectly fine. "It's a PVC, therefore
it's state" is exactly the assumption a future maintainer will otherwise make here — it's wrong.

Every step below must converge correctly whether `git_dir` is a fresh, empty directory or one
carrying a previous run's state:

- `git init --bare` against an already-initialised repository is a documented no-op (it fills in
  anything missing without touching existing refs or objects), so it runs on every call rather
  than being gated on "does the cache already exist."
- Remote URLs are set idempotently (`set-url` if the remote exists, `add` if it doesn't).
- The local branch is fast-forwarded to match `origin` before anything stages a change — never
  rewound, and never force-reset — so a commit taken by a previous run whose push then failed
  (and which is therefore *ahead* of origin, not behind it) is left alone rather than discarded.
"""

from __future__ import annotations

import logging
from pathlib import Path

from obsidian_tools.vault_git.baseline import ensure_ignore_rule
from obsidian_tools.vault_git.branch_sync import SyncAction, classify_branch_sync
from obsidian_tools.vault_git.runner import GitRunner

logger = logging.getLogger(__name__)


class GitDivergenceError(RuntimeError):
    """The local cached branch and origin's branch have diverged (neither is an ancestor of the other).

    Under this design's single-writer invariant, this committer is the only process that ever
    pushes to origin, so this should never happen in ordinary operation — it would mean something
    else pushed to the vault content repository outside this component's control. Refusing to
    guess which side wins (never force-pushing, never force-resetting local state) is deliberate:
    a wrong guess here would silently discard history on one side or the other.
    """


def provision_repository(
    runner: GitRunner,
    *,
    branch: str,
    author_name: str,
    author_email: str,
    origin_url: str,
    nas_url: str | None,
) -> None:
    """Bring `runner`'s git-dir to a valid, up-to-date-with-origin state. Safe to call every run."""
    runner.git_dir.mkdir(parents=True, exist_ok=True)
    _clear_stale_locks(runner.git_dir)
    index_existed_before = runner.index_file_exists()

    # `git init --bare` rejects an explicit `--work-tree` outright ("not allowed without
    # specifying GIT_DIR", even though GIT_DIR *is* set) — bare and work-treed are contradictory
    # to `init` specifically, unlike every other command this module runs against the same
    # git-dir. Every later invocation still passes `--work-tree` normally.
    runner.run(["init", "--bare", "-q", f"--initial-branch={branch}"], include_work_tree=False)

    # See obsidian_tools/vault_git/baseline.py: this is the layer that keeps ordinary `git add -A`
    # from ever tracking a new file under .obsidian/ in the first place. It's a distinct concern
    # from the skip-worktree bits `ensure_obsidian_baseline` applies to already-tracked paths.
    ensure_ignore_rule(runner.git_dir)

    # Bounds the cost of the kubelet's fsGroup mechanism, which re-applies group permissions across
    # the volume at mount time independent of what wrote the files — without this, every run would
    # record spurious mode-only diffs as real content changes. This setting lives on this
    # repository only; it never touches the Mac clone or the iCloud copy, which are separate
    # repositories entirely (docs/DESIGN.md §2 item 5, ppat/obsidian-tools#3).
    runner.run(["config", "core.fileMode", "false"])
    runner.run(["config", "user.name", author_name])
    runner.run(["config", "user.email", author_email])

    _ensure_remote(runner, "origin", origin_url)
    if nas_url is not None:  # the NAS remote is optional (config.py's CommitConfig.nas_url)
        _ensure_remote(runner, "nas", nas_url)

    ref_advanced = _sync_branch_from_origin(runner, branch)

    head_sha = runner.rev_parse_or_none("HEAD")
    if head_sha and (ref_advanced or not index_existed_before):
        # The index is not implicitly kept in sync with HEAD in this detached, no-checkout layout:
        # after `git init --bare` it doesn't exist yet, and after recovering a wiped cache it
        # reflects nothing until explicitly resynced. Without this, the next `git add -A` would
        # compare the work tree against an empty or stale index rather than against HEAD, staging
        # every already-committed file as though it were new.
        runner.run(["read-tree", "HEAD"])


def _clear_stale_locks(git_dir: Path) -> None:
    """Remove every leftover `*.lock` file under `$GIT_DIR` from a run that was killed mid-write.

    A killed run can strand more than `index.lock`: `config.lock` (from the `git config` calls
    below), `HEAD.lock` and `refs/heads/<branch>.lock` (from `update-ref` during a branch sync, or
    from `git commit`) are each left behind by the same failure mode, at whichever git invocation
    was in flight when the kill landed — measured directly: `config.lock` wedges the next run's
    provisioning with exit 1, and `HEAD.lock`/`refs/heads/<branch>.lock` wedge it with an uncaught
    `GitCommandError` traceback, since the commit path has its own, separate exception handling
    (see `obsidian_tools/commands/commit.py`). Clearing only `index.lock`, as an earlier revision
    of this function did, leaves the other three to wedge every later run permanently, one
    directory removal short of the fix `index.lock` already got.

    Unconditional, with no age check or "was it really this process" guess, for every lock found:
    this CronJob runs with `concurrencyPolicy: Forbid` against its own single-writer RWO cache PVC,
    so at most one committer process ever holds this git-dir at a time — a lock file found here can
    only be a corpse left by a previous run that didn't get to clean up after itself (the platform
    delivers SIGKILL, not SIGTERM, once the termination grace period elapses; see
    `obsidian_tools/cli.py`'s SIGTERM handler for the other half of this). Under *ordinary*
    operation there is nothing to guess about.

    One residual this does not solve, worth stating rather than leaving implicit: a manually
    created Job (`kubectl create job --from=cronjob/...`) is not managed by the CronJob controller
    and bypasses `concurrencyPolicy` entirely. Under that kind of operator-initiated concurrency —
    not ordinary operation, but a real possibility on a disaster-recovery-adjacent tool like this
    one — an unconditional unlink here would remove a lock a second, genuinely concurrent writer
    still holds.
    """
    for lock_path in sorted(git_dir.rglob("*.lock")):
        if not lock_path.is_file():
            continue
        logger.warning(
            "clearing stale lock file left by a previous run",
            extra={"event": "stale_lock_cleared", "path": str(lock_path)},
        )
        lock_path.unlink()


def _ensure_remote(runner: GitRunner, name: str, url: str) -> None:
    # Deliberately `git config --local --get`, not `git remote` (which also surfaces remote
    # sections merged in from global/system config with no URL of their own — e.g. a stray
    # `[remote "origin"] prune = true` in a user's ~/.gitconfig — and would misreport a remote as
    # already present here). `set-url` requires `remote.<name>.url` specifically, so that's what
    # this checks, scoped to this repository's own config file only.
    result = runner.run(["config", "--local", "--get", f"remote.{name}.url"], check=False)
    if result.returncode == 0:
        runner.run(["remote", "set-url", name, url])
    else:
        runner.run(["remote", "add", name, url])


def _sync_branch_from_origin(runner: GitRunner, branch: str) -> bool:
    """Fetch `origin`, classify the local branch's relationship to it (`branch_sync.py` — pure,
    given the three SHAs below), and act on that classification. Returns True if the local ref was
    created or advanced. Never rewinds a local ref that is ahead of origin."""
    runner.run(["fetch", "origin"])  # a GitHub fetch failure is a real provisioning failure, not
    # an NFS read — not wrapped in retry; the run fails loud and the next scheduled run tries again.

    remote_ref = f"refs/remotes/origin/{branch}"
    local_ref = f"refs/heads/{branch}"

    remote_sha = runner.rev_parse_or_none(remote_ref)
    local_sha = runner.rev_parse_or_none(local_ref)
    # Meaningless (and not computed) unless both refs already exist — `classify_branch_sync` never
    # consults it in any other case.
    merge_base = None
    if local_sha is not None and remote_sha is not None:
        merge_base = runner.merge_base_or_none(local_sha, remote_sha)

    action = classify_branch_sync(local_sha=local_sha, remote_sha=remote_sha, merge_base=merge_base)

    if action in (SyncAction.NO_REMOTE_HISTORY, SyncAction.UP_TO_DATE, SyncAction.LOCAL_AHEAD):
        # NO_REMOTE_HISTORY: origin has no history for this branch yet; the first commit roots it.
        # LOCAL_AHEAD: a previous run committed but its push failed. Leave it — the push step
        # retries against both remotes on every run regardless of whether this cycle produced a new
        # commit, so a stuck local-only commit catches up on its own.
        return False

    if action is SyncAction.ROOT_LOCAL_FROM_REMOTE:
        assert remote_sha is not None  # guaranteed by classify_branch_sync's NO_REMOTE_HISTORY check
        runner.run(["update-ref", local_ref, remote_sha])
        logger.info("branch created from origin", extra={"event": "branch_synced", "branch": branch, "sha": remote_sha})
        return True

    if action is SyncAction.FAST_FORWARD:
        assert remote_sha is not None  # guaranteed by classify_branch_sync's NO_REMOTE_HISTORY check
        runner.run(["update-ref", local_ref, remote_sha])
        logger.info(
            "branch fast-forwarded from origin",
            extra={"event": "branch_synced", "branch": branch, "from": local_sha, "to": remote_sha},
        )
        return True

    raise GitDivergenceError(
        f"local {branch} ({local_sha}) and origin/{branch} ({remote_sha}) have diverged; "
        "refusing to guess which side wins"
    )
