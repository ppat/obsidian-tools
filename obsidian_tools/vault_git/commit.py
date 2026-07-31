"""Staging, committing and pushing — the part of the cycle that actually produces history.

One commit per cycle, freeform message — **not** Conventional Commits. Commitlint and
release-please are deliberately absent from the vault content repository; machine commits there
are not release-relevant, and forcing them into this codebase's own commit convention would imply
a relationship between the two histories that doesn't exist.

Pushes to both remotes independently. A failure on one must never prevent the attempt on the
other — the NAS copy exists as independence insurance precisely so no single push target is
load-bearing. Nothing here treats "no new commit this cycle" as a reason to skip pushing: a commit
taken by a previous run whose push then failed is still sitting, unpushed, in the (cached) git-dir,
and a plain push to a remote that's already caught up is a harmless no-op — so pushing
unconditionally on every run is what lets a stuck backlog catch itself up, for free, on the next
scheduled run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from obsidian_tools.vault_git.commit_message import format_commit_message
from obsidian_tools.vault_git.deletion_assessment import DeletionVerdict, assess_deletion
from obsidian_tools.vault_git.runner import GitCommandError, GitRunner

logger = logging.getLogger(__name__)

# Above this fraction of HEAD's tracked paths staged as deletions in one cycle, refuse rather than
# commit: nothing in the design produces a legitimate single-cycle change anywhere near this large
# (the widest ordinary write is a promotion relocation or an archive roll-up, not a rewrite of the
# vault), so a fraction this high is a much better fit for "the volume came back blank" than for
# "a human deleted some notes." Picked well below "the entire vault" so a deletion doesn't have to
# be total to trip it. Overridable via GIT_COMMIT_MAX_DELETION_FRACTION (obsidian_tools.config) —
# a genuine archive purge has no other way past this guard than editing source, and content is
# never at risk here (git history holds it either way), only history production stops, so an
# operator escape hatch is the right shape. Setting it to `1.0` or above disables both tripwires
# below for that run: at that point every deletion up to and including the whole tree is already
# accepted, which subsumes the zero-markdown tripwire too.
DEFAULT_MAX_DELETION_FRACTION = 0.5


class MassDeletionError(RuntimeError):
    """Staged deletions look like data loss (an empty/reset volume), not an ordinary edit."""


def stage_all(runner: GitRunner) -> None:
    runner.run(["add", "--all"], retry=True)


def has_staged_changes(runner: GitRunner) -> bool:
    result = runner.run(["diff", "--cached", "--quiet"], check=False)
    return result.returncode != 0


def check_for_mass_deletion(runner: GitRunner, *, max_deletion_fraction: float = DEFAULT_MAX_DELETION_FRACTION) -> None:
    """Refuse (by raising) when the currently-staged change looks like the volume came back empty
    rather than like a human deleted a note — this component's whole job is durability, and
    `docs/DESIGN.md`'s "fail loud, destroy nothing" posture applies nowhere more than here.

    The verdict itself — which of the two tripwires (deletion fraction, zero markdown) fired, if
    either — is `assess_deletion` (`vault_git/deletion_assessment.py`), pure over the before/after
    path lists. This function's job is gathering those two lists and turning a non-`ALLOWED` verdict
    into `MassDeletionError`. `max_deletion_fraction >= 1.0` and an empty `HEAD` both short-circuit
    here, before either path list is even fetched — the former is redundant with `assess_deletion`'s
    own handling of it (kept there too, so that boundary is directly pure-testable), the latter
    because `assess_deletion` has nothing to evaluate without a tracked-before list to begin with.

    **The after-state is derived entirely from git's own staged tree (`git write-tree`), never by
    walking the work tree.** An earlier revision counted `work_tree.rglob("*.md")` directly, on the
    reasoning that `stage_all` already succeeded without raising, so nothing unreadable could remain
    by the time this runs. That reasoning was false on its own terms: `git add -A` succeeds against
    a directory it cannot open — it leaves that directory's existing index entries untouched rather
    than failing the whole add — so staging "succeeding" says nothing about whether the work tree is
    fully readable afterward. And `pathlib.Path.rglob` silently swallows `PermissionError` while
    walking, so a directory this process merely can't *list* contributes zero to a filesystem-based
    markdown count, indistinguishable from "these notes are gone" even though they're untouched and
    still tracked. Measured: with notes under two unreadable directories and nothing actually staged
    as a deletion, the old implementation still raised `MassDeletionError` — turning a transient
    permission or NFS glitch into a failed run and a false durability alarm, on the one component
    whose entire job is durability. `write-tree` reads only the index and the object store, so a
    permission glitch on a directory this run never touched can no longer manufacture that alarm.
    """
    if max_deletion_fraction >= 1.0:
        return

    head_sha = runner.rev_parse_or_none("HEAD")
    if head_sha is None:
        return  # no history yet to compare a deletion against

    tracked_before = runner.list_tree_paths("HEAD")
    if not tracked_before:
        return

    if not has_staged_changes(runner):
        return  # nothing staged: no deletion to evaluate, and no staged tree to derive one from

    tracked_after = runner.list_tree_paths(runner.write_staged_tree())

    assessment = assess_deletion(
        tracked_before=tracked_before, tracked_after=tracked_after, max_deletion_fraction=max_deletion_fraction
    )

    if assessment.verdict is DeletionVerdict.FRACTION_EXCEEDED:
        raise MassDeletionError(
            f"staged commit deletes {assessment.deleted_count}/{assessment.tracked_count} tracked paths "
            f"({assessment.deletion_fraction:.0%}), over the {max_deletion_fraction:.0%} threshold. If this is "
            "a deliberate archive purge, set GIT_COMMIT_MAX_DELETION_FRACTION>=1.0 and rerun to "
            "disable this guard for that run."
        )
    if assessment.verdict is DeletionVerdict.MARKDOWN_WIPED:
        raise MassDeletionError(
            f"HEAD tracked {assessment.markdown_before} markdown files; the staged tree now has none. If this "
            "is a deliberate archive purge, set GIT_COMMIT_MAX_DELETION_FRACTION>=1.0 and rerun to "
            "disable this guard for that run."
        )


def build_commit_message(runner: GitRunner, *, cycle_time: datetime) -> str:
    """Gathers the staged change list; `format_commit_message` (`vault_git/commit_message.py`) —
    pure over that list — decides what the message text actually says."""
    entries = runner.staged_name_status()
    return format_commit_message(entries, cycle_time=cycle_time)


def create_commit(runner: GitRunner, *, cycle_time: datetime) -> str:
    message = build_commit_message(runner, cycle_time=cycle_time)
    runner.run(["commit", "--quiet", "--message", message])
    sha = runner.rev_parse_or_none("HEAD")
    if sha is None:  # pragma: no cover - a commit was just taken; HEAD must resolve
        raise RuntimeError("commit succeeded but HEAD does not resolve")
    logger.info("committed vault changes", extra={"event": "commit_created", "commit": sha})
    return sha


@dataclass(frozen=True, slots=True)
class PushResult:
    remote: str
    ok: bool
    error: str | None = None


def push_all(runner: GitRunner, *, branch: str, remotes: tuple[str, ...] = ("origin", "nas")) -> list[PushResult]:
    """Push `branch` to every remote in `remotes`, independently and unconditionally."""
    results: list[PushResult] = []
    for remote in remotes:
        try:
            runner.run(["push", remote, f"{branch}:{branch}"])
        except GitCommandError as exc:
            logger.error("push failed", extra={"event": "push_failed", "remote": remote, "error": str(exc)})
            results.append(PushResult(remote=remote, ok=False, error=str(exc)))
        else:
            logger.info("pushed", extra={"event": "push_succeeded", "remote": remote})
            results.append(PushResult(remote=remote, ok=True))
    return results
