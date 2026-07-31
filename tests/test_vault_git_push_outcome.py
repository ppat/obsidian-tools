"""Tests for `obsidian_tools/vault_git/push_outcome.py` — the pure push-outcome aggregation,
tested as a pure function over hand-built `PushResult` lists rather than real pushes.

`test_vault_git_commit.py::test_push_failure_on_one_remote_does_not_block_the_other` and
`test_commands_commit.py::test_push_failure_on_one_remote_still_attempts_the_other_and_run_exits_nonzero`
keep the real-git integration coverage (an actually broken remote URL, an actual push attempt to
the healthy one); this file is where the aggregation decision itself -- which remotes failed, and
what that means for the run's exit status -- is proven independently of performing any push at all.
"""

from __future__ import annotations

from obsidian_tools.vault_git.push_outcome import PushOutcome, PushResult, summarize_push_results


def test_all_remotes_succeeding_is_not_a_failure() -> None:
    results = [PushResult(remote="origin", ok=True), PushResult(remote="nas", ok=True)]
    assert summarize_push_results(results) == PushOutcome(failed_remotes=(), any_failed=False)


def test_one_remote_failing_is_a_failure_even_though_the_other_succeeded() -> None:
    """The whole point of pushing to two remotes independently: one failure must never be masked by
    the other's success."""
    results = [PushResult(remote="origin", ok=True), PushResult(remote="nas", ok=False, error="connection refused")]
    outcome = summarize_push_results(results)
    assert outcome.any_failed is True
    assert outcome.failed_remotes == ("nas",)


def test_every_remote_failing_lists_every_remote() -> None:
    results = [
        PushResult(remote="origin", ok=False, error="a"),
        PushResult(remote="nas", ok=False, error="b"),
    ]
    outcome = summarize_push_results(results)
    assert outcome.any_failed is True
    assert outcome.failed_remotes == ("origin", "nas")


def test_no_results_at_all_is_not_a_failure() -> None:
    """An empty `remotes` tuple is a real (if unusual) call shape for `push_all` -- nothing to fail
    means nothing failed."""
    assert summarize_push_results([]) == PushOutcome(failed_remotes=(), any_failed=False)


def test_failed_remotes_preserves_input_order() -> None:
    results = [
        PushResult(remote="nas", ok=False, error="a"),
        PushResult(remote="origin", ok=False, error="b"),
    ]
    assert summarize_push_results(results).failed_remotes == ("nas", "origin")
