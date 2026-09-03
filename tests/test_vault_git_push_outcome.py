"""Tests for `obsidian_tools/vault_git/push_outcome.py` — the pure push-outcome aggregation,
tested as a pure function over hand-built `PushResult` lists rather than real pushes.

The real-git integration coverage lives in two other files, one per level:
`test_vault_git_commit.py::test_push_failure_on_one_remote_does_not_block_the_other` hands `push_all`
a healthy remote alongside an unreachable URL and checks each is attempted and recorded on its own;
`test_commands_commit.py::test_push_failure_exits_nonzero_and_leaves_the_local_commit_for_the_next_run`
drives a whole run against a single remote whose git receiving end refuses the push. This file is
where the aggregation decision itself -- which remotes failed, and what that means for the run's
exit status -- is proven independently of performing any push at all.

The remote names below are arbitrary labels for `push_all`'s remote tuple, not a topology the
committer is deployed with -- it pushes to `origin` alone.
"""

from __future__ import annotations

from obsidian_tools.vault_git.push_outcome import PushOutcome, PushResult, summarize_push_results


def test_all_remotes_succeeding_is_not_a_failure() -> None:
    results = [PushResult(remote="origin", ok=True), PushResult(remote="mirror", ok=True)]
    assert summarize_push_results(results) == PushOutcome(failed_remotes=(), any_failed=False)


def test_one_remote_failing_is_a_failure_even_though_the_other_succeeded() -> None:
    """`summarize_push_results` aggregates without collapsing: one failure must never be masked by
    another remote's success."""
    results = [PushResult(remote="origin", ok=True), PushResult(remote="mirror", ok=False, error="connection refused")]
    outcome = summarize_push_results(results)
    assert outcome.any_failed is True
    assert outcome.failed_remotes == ("mirror",)


def test_every_remote_failing_lists_every_remote() -> None:
    results = [
        PushResult(remote="origin", ok=False, error="a"),
        PushResult(remote="mirror", ok=False, error="b"),
    ]
    outcome = summarize_push_results(results)
    assert outcome.any_failed is True
    assert outcome.failed_remotes == ("origin", "mirror")


def test_no_results_at_all_is_not_a_failure() -> None:
    """An empty `remotes` tuple is a real (if unusual) call shape for `push_all` -- nothing to fail
    means nothing failed."""
    assert summarize_push_results([]) == PushOutcome(failed_remotes=(), any_failed=False)


def test_failed_remotes_preserves_input_order() -> None:
    results = [
        PushResult(remote="mirror", ok=False, error="a"),
        PushResult(remote="origin", ok=False, error="b"),
    ]
    assert summarize_push_results(results).failed_remotes == ("mirror", "origin")
