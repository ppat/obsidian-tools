"""Tests for `obsidian_tools/vault_git/branch_sync.py` — the pure local-vs-origin divergence
classification, tested as a pure function over three hand-picked SHA-shaped strings rather than
against a real repository in each state.

`test_vault_git_provisioning.py` keeps the real-git integration coverage (fetch, fast-forward,
divergence against an actually-pushed rogue commit); this file is where the exhaustive state-table
coverage lives, cheaply, including edge states a real-repo test would need a dedicated fixture for.
"""

from __future__ import annotations

from obsidian_tools.vault_git.branch_sync import SyncAction, classify_branch_sync

# Real commit objects have no bearing on this pure function -- it only ever compares these strings
# for equality -- so plain, distinguishable labels stand in for SHAs throughout.
_LOCAL = "local-sha"
_REMOTE = "remote-sha"
_OTHER = "some-other-sha"


def test_no_remote_ref_yet() -> None:
    """Origin has no history for this branch at all -- the first commit will root it."""
    assert classify_branch_sync(local_sha=None, remote_sha=None, merge_base=None) is SyncAction.NO_REMOTE_HISTORY
    assert classify_branch_sync(local_sha=_LOCAL, remote_sha=None, merge_base=None) is SyncAction.NO_REMOTE_HISTORY


def test_no_local_ref_yet_adopts_remote_outright() -> None:
    """No local ref, but origin has one: root the local branch from it. Checked before divergence
    can even be asked about -- there is nothing local to have diverged from yet."""
    assert (
        classify_branch_sync(local_sha=None, remote_sha=_REMOTE, merge_base=None) is SyncAction.ROOT_LOCAL_FROM_REMOTE
    )


def test_identical_shas_are_up_to_date() -> None:
    """Equal SHAs short-circuit before merge-base is even consulted -- a self-merge-base is always
    itself, so this is also what `merge_base == local_sha == remote_sha` would classify as, but the
    function never needs to look at `merge_base` to know it."""
    assert classify_branch_sync(local_sha=_LOCAL, remote_sha=_LOCAL, merge_base=None) is SyncAction.UP_TO_DATE
    assert classify_branch_sync(local_sha=_LOCAL, remote_sha=_LOCAL, merge_base=_LOCAL) is SyncAction.UP_TO_DATE


def test_merge_base_equal_to_local_is_a_fast_forward() -> None:
    """Local is an ancestor of remote (behind it): advance local to match."""
    assert classify_branch_sync(local_sha=_LOCAL, remote_sha=_REMOTE, merge_base=_LOCAL) is SyncAction.FAST_FORWARD


def test_merge_base_equal_to_remote_is_local_ahead() -> None:
    """Remote is an ancestor of local (local is ahead): a previous run committed but didn't push
    yet. Leave the local ref alone -- the push step catches it up."""
    assert classify_branch_sync(local_sha=_LOCAL, remote_sha=_REMOTE, merge_base=_REMOTE) is SyncAction.LOCAL_AHEAD


def test_merge_base_equal_to_neither_side_is_diverged() -> None:
    """A real common ancestor exists (some third commit), but it's neither tip -- classic
    divergence: both sides have commits the other lacks."""
    assert classify_branch_sync(local_sha=_LOCAL, remote_sha=_REMOTE, merge_base=_OTHER) is SyncAction.DIVERGED


def test_no_merge_base_at_all_is_diverged() -> None:
    """Unrelated histories (`git merge-base` exits 1, no common ancestor at all) is the most extreme
    form of divergence, not a special case -- `merge_base=None` here falls through the same "equal
    to neither side" branch as a real, unrelated third commit would."""
    assert classify_branch_sync(local_sha=_LOCAL, remote_sha=_REMOTE, merge_base=None) is SyncAction.DIVERGED
