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

from obsidian_tools.vault_git.runner import GitCommandError, GitRunner

logger = logging.getLogger(__name__)

_CHANGE_TYPE_LABELS = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "C": "copied"}
_MAX_LISTED_PATHS = 50


def stage_all(runner: GitRunner) -> None:
    runner.run(["add", "--all"], retry=True)


def has_staged_changes(runner: GitRunner) -> bool:
    result = runner.run(["diff", "--cached", "--quiet"], check=False)
    return result.returncode != 0


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
