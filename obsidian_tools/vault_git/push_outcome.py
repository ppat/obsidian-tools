"""Deciding what a set of per-remote push results means for the run's exit status, as a pure
function over `PushResult` values — no `GitRunner`, no actual pushing.

**Why this is split out and kept pure.** `push_all` performs the pushes; "which remotes failed, and
does that mean this run should exit non-zero" is a separate question that never touches git again
once every `PushResult` is in hand — it was previously answered inline in
`commands/commit.py::run` (`any(not result.ok for result in push_results)`) rather than named and
tested on its own. `PushResult` itself lives here too: it's plain, git-free data describing the
outcome of one push, not something that needs `vault_git/commit.py`'s `GitRunner` dependency to
define.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PushResult:
    remote: str
    ok: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PushOutcome:
    failed_remotes: tuple[str, ...]
    any_failed: bool


def summarize_push_results(results: Sequence[PushResult]) -> PushOutcome:
    """A failure on one remote must never be masked by success on another — `any_failed` is true if
    even one remote in `results` failed, regardless of how many succeeded."""
    failed_remotes = tuple(result.remote for result in results if not result.ok)
    return PushOutcome(failed_remotes=failed_remotes, any_failed=bool(failed_remotes))
