"""What to do about the local cached branch vs. `origin`'s branch, as a pure function over three
SHAs — no `GitRunner`, no `fetch`, no `update-ref`.

**Why this is split out of `provisioning.py` and kept pure.** Misclassifying divergence here is
silent and hard to notice: the wrong call either rewinds a commit this component itself took
(discarding history a maintainer would assume is safe because "git never force-pushes here"), or
lets two remotes fork without anyone comparing them — the next push to one succeeds, the next push
to the other is rejected non-fast-forward days later, far from the run that actually caused it. A
decision this consequential needs to be provable against every state the three SHAs can be in
without constructing a real repository in each state — see `baseline_selector.py` for the same
argument applied to a different decision in this codebase.

Kept deliberately *narrow*: this module answers "what should happen to the local ref," nothing
about which paths to fetch, how to run `update-ref`, or what to log. `provisioning.py` remains the
only place that touches git.
"""

from __future__ import annotations

from enum import Enum


class SyncAction(Enum):
    """What `_sync_branch_from_origin` should do, given the three SHAs below.

    `FAST_FORWARD` and `ROOT_LOCAL_FROM_REMOTE` both end up calling `update-ref` with the same
    target (`remote_sha`) — they are kept as distinct members rather than collapsed into one
    because the caller logs them differently ("branch created" vs. "branch fast-forwarded"), and
    that distinction is itself useful signal to an operator reading the log.
    """

    NO_REMOTE_HISTORY = "no_remote_history"  # origin has no ref for this branch yet
    ROOT_LOCAL_FROM_REMOTE = "root_local_from_remote"  # no local ref yet; adopt origin's outright
    UP_TO_DATE = "up_to_date"  # local and remote already match
    FAST_FORWARD = "fast_forward"  # local is behind remote (an ancestor of it): advance it
    LOCAL_AHEAD = "local_ahead"  # local is ahead of remote: leave it, a previous push failed
    DIVERGED = "diverged"  # neither is an ancestor of the other: refuse to guess


def classify_branch_sync(
    *,
    local_sha: str | None,
    remote_sha: str | None,
    merge_base: str | None,
) -> SyncAction:
    """Classify the relationship between `local_sha` (the cached branch's current tip, or `None` if
    the ref doesn't exist yet) and `remote_sha` (origin's tip for the same branch, or `None` if
    origin has no history for it yet).

    `merge_base` is `git merge-base local_sha remote_sha` — the caller only needs to compute it when
    both SHAs are present (it's meaningless otherwise, and this function never inspects it in that
    case). It being equal to `local_sha` means local is an ancestor of remote (behind); equal to
    `remote_sha` means remote is an ancestor of local (ahead); equal to neither — including `None`,
    the no-common-ancestor case — means the two have diverged.
    """
    if remote_sha is None:
        return SyncAction.NO_REMOTE_HISTORY
    if local_sha is None:
        return SyncAction.ROOT_LOCAL_FROM_REMOTE
    if local_sha == remote_sha:
        return SyncAction.UP_TO_DATE
    if merge_base == local_sha:
        return SyncAction.FAST_FORWARD
    if merge_base == remote_sha:
        return SyncAction.LOCAL_AHEAD
    return SyncAction.DIVERGED
