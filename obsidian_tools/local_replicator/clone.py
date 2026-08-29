"""Provisioning and pull-forward for the single parked cache clone.

Reuses `GitRunner` (`obsidian_tools/vault_git/runner.py`) — the generic detached
`--git-dir`/`--work-tree` wrapper the git committer's own PR called out as expected to be reused
here — rather than growing a second git wrapper. What's genuinely different from the committer's
own provisioning (`obsidian_tools/vault_git/provisioning.py`) is the git topology: the committer's
repository is bare, headless, and never checked out; this one is an ordinary working tree, because
`rsync_ops.publish` reads its files directly off disk. That difference is what a checked-out clone
needs (`checkout_forward`) that a bare repo never does, so this module is its own, small,
committer-independent thing rather than importing the committer's private helpers.

**Fetching and checking out forward are deliberately two separate functions, not one.** `cycle.py`
always fetches and always checks out forward onto `main` (ADR-0025 runs
unconditionally) — but whether the *publish* that follows actually reaches iCloud, and whether
`LAST_CHECKOUT` advances, is gated separately, on the spool write, not on this step (see
`obsidian_tools.local_replicator.drift.decide_cycle_outcome`). Idempotency across a partial cycle
comes from the *next* cycle re-parking at `LAST_CHECKOUT` at its own step 1, not from this module
reverting anything here.
"""

from __future__ import annotations

import logging

from obsidian_tools.vault_git.runner import GitRunner

logger = logging.getLogger(__name__)


def ensure_cache_clone(runner: GitRunner, *, branch: str, origin_url: str) -> None:
    """Idempotent: safe to call every cycle, whether the clone already exists or was just lost and
    re-provisioned (ADR-0025)."""
    runner.work_tree.mkdir(parents=True, exist_ok=True)
    runner.git_dir.mkdir(parents=True, exist_ok=True)
    runner.run(["init", "-q", f"--initial-branch={branch}"])
    # `core.quotePath=false` is the second half of the fix the committer applied as `-z` everywhere
    # it lists paths (ppat/obsidian-tools#3: C-quoted non-ASCII paths are what wedged its baseline).
    # `-z` closes it wherever a NUL-separated form exists -- and `git diff`'s *patch* output has no
    # such form, so a spool entry's `patch` header would otherwise carry an escaped path while its
    # own `path` field carries the real bytes: one entry, two encodings, for any note titled in a
    # non-Latin script. Set here rather than per-call because it must hold for every git invocation
    # against this clone, and this is the one place the clone is provisioned.
    runner.run(["config", "--local", "core.quotePath", "false"])
    _ensure_remote(runner, "origin", origin_url)


def _ensure_remote(runner: GitRunner, name: str, url: str) -> None:
    # `--local --get`, not `git remote`: scoped to this repository's own config file, so a remote
    # section merged in from global config (with no URL of its own) can't misreport as present.
    result = runner.run(["config", "--local", "--get", f"remote.{name}.url"], check=False)
    if result.returncode == 0:
        runner.run(["remote", "set-url", name, url])
    else:
        runner.run(["remote", "add", name, url])


def fetch_origin(runner: GitRunner, *, branch: str) -> str | None:
    """Fetch `origin`'s `branch` into the local git-dir. Returns the fetched commit SHA, or `None`
    if origin has no history for that branch yet (the committer hasn't taken its first commit).
    Never touches the working tree — see `checkout_forward` for that."""
    runner.run(["fetch", "-q", "origin", branch], retry=True)
    return runner.rev_parse_or_none(f"refs/remotes/origin/{branch}")


def checkout_forward(runner: GitRunner, *, branch: str, sha: str) -> None:
    """Move the parked clone's local branch and working tree to `sha`. Callers decide when it is
    safe to call this — it performs no gating of its own, and is also how `cycle.py` reverts the
    clone back to the previous `LAST_CHECKOUT` when a cycle's publish had to skip a path."""
    runner.run(["checkout", "-q", "-B", branch, sha])
