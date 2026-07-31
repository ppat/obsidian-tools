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
from pathlib import Path

from obsidian_tools.vault_git.runner import GitCommandError, GitRunner

logger = logging.getLogger(__name__)

_CHANGE_TYPE_LABELS = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "C": "copied"}
_MAX_LISTED_PATHS = 50

# Above this fraction of HEAD's tracked paths staged as deletions in one cycle, refuse rather than
# commit: nothing in the design produces a legitimate single-cycle change anywhere near this large
# (the widest ordinary write is a promotion relocation or an archive roll-up, not a rewrite of the
# vault), so a fraction this high is a much better fit for "the volume came back blank" than for
# "a human deleted some notes." Picked well below "the entire vault" so a deletion doesn't have to
# be total to trip it.
_MAX_DELETION_FRACTION = 0.5


class MassDeletionError(RuntimeError):
    """Staged deletions look like data loss (an empty/reset volume), not an ordinary edit."""


def stage_all(runner: GitRunner) -> None:
    runner.run(["add", "--all"], retry=True)


def has_staged_changes(runner: GitRunner) -> bool:
    result = runner.run(["diff", "--cached", "--quiet"], check=False)
    return result.returncode != 0


def check_for_mass_deletion(
    runner: GitRunner, work_tree: Path, *, max_deletion_fraction: float = _MAX_DELETION_FRACTION
) -> None:
    """Refuse (by raising) when the currently-staged change looks like the volume came back empty
    rather than like a human deleted a note — this component's whole job is durability, and
    `docs/DESIGN.md`'s "fail loud, destroy nothing" posture applies nowhere more than here.

    Two independent tripwires, either sufficient on its own:
    - staged deletions exceed `max_deletion_fraction` of what HEAD had tracked, or
    - HEAD tracked markdown notes and the work tree now has none at all.

    Only ever called after `stage_all` has already succeeded without raising, so an unreadable
    directory (which fails staging outright and is handled well before this point) can never reach
    here and never trips this check — the hazard this guards against is specific to a work tree
    that is genuinely, readably empty.
    """
    head_sha = runner.rev_parse_or_none("HEAD")
    if head_sha is None:
        return  # no history yet to compare a deletion against

    tracked_before = runner.run(["ls-tree", "-r", "--name-only", "HEAD"]).stdout.splitlines()
    if not tracked_before:
        return

    status_lines = runner.run(["diff", "--cached", "--name-status"]).stdout.splitlines()
    deleted_count = sum(1 for line in status_lines if line[:1] == "D")
    deletion_fraction = deleted_count / len(tracked_before)

    markdown_before = sum(1 for path in tracked_before if path.endswith(".md"))
    markdown_now = sum(1 for _ in work_tree.rglob("*.md"))

    if deletion_fraction > max_deletion_fraction:
        raise MassDeletionError(
            f"staged commit deletes {deleted_count}/{len(tracked_before)} tracked paths "
            f"({deletion_fraction:.0%}), over the {max_deletion_fraction:.0%} threshold"
        )
    if markdown_before > 0 and markdown_now == 0:
        raise MassDeletionError(f"HEAD tracked {markdown_before} markdown files; the work tree now has none")


def build_commit_message(runner: GitRunner, *, cycle_time: datetime) -> str:
    status_lines = runner.run(["diff", "--cached", "--name-status"]).stdout.splitlines()

    counts: dict[str, int] = {}
    for line in status_lines:
        code = line[:1]
        counts[code] = counts.get(code, 0) + 1
    summary = ", ".join(f"{counts[code]} {_CHANGE_TYPE_LABELS.get(code, code)}" for code in sorted(counts))
    summary = summary or "no path changes"

    header = f"vault sync {cycle_time.strftime('%Y-%m-%dT%H:%M:%SZ')}: {len(status_lines)} changed ({summary})"

    body_lines = status_lines[:_MAX_LISTED_PATHS]
    if len(status_lines) > _MAX_LISTED_PATHS:
        body_lines = [*body_lines, f"... and {len(status_lines) - _MAX_LISTED_PATHS} more"]

    return header if not body_lines else f"{header}\n\n" + "\n".join(body_lines)


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
