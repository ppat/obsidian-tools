"""Formatting the commit message, as a pure function over the staged change list — no `GitRunner`.

**Why this is split out of `commit.py` and kept pure.** `build_commit_message` reads
`runner.staged_name_status()` and immediately turns it into text; the actual formatting decisions
(how to summarise change types, how many paths to list before truncating, how to render a rename)
never touch git again once `entries` is in hand. Pulling that formatting out makes every hostile
input — non-ASCII paths, embedded quotes and newlines, an empty change set, more paths than the cap
— a literal `NameStatusEntry` list fed straight to the function, rather than a real staged commit
built to produce that exact text (see `baseline_selector.py` for the same argument elsewhere in this
codebase).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from obsidian_tools.vault_git.name_status import NameStatusEntry

_CHANGE_TYPE_LABELS = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "C": "copied"}
_MAX_LISTED_PATHS = 50


def _format_name_status_entry(entry: NameStatusEntry) -> str:
    if entry.old_path is not None:
        return f"{entry.status}\t{entry.old_path} -> {entry.path}"
    return f"{entry.status}\t{entry.path}"


def format_commit_message(entries: Sequence[NameStatusEntry], *, cycle_time: datetime) -> str:
    """Not Conventional Commits — see `vault_git/commit.py`'s module docstring for why the vault
    content repository's own history is deliberately freeform."""
    counts: dict[str, int] = {}
    for entry in entries:
        code = entry.status[:1]
        counts[code] = counts.get(code, 0) + 1
    summary = ", ".join(f"{counts[code]} {_CHANGE_TYPE_LABELS.get(code, code)}" for code in sorted(counts))
    summary = summary or "no path changes"

    header = f"vault sync {cycle_time.strftime('%Y-%m-%dT%H:%M:%SZ')}: {len(entries)} changed ({summary})"

    body_lines = [_format_name_status_entry(entry) for entry in entries[:_MAX_LISTED_PATHS]]
    if len(entries) > _MAX_LISTED_PATHS:
        body_lines.append(f"... and {len(entries) - _MAX_LISTED_PATHS} more")

    return header if not body_lines else f"{header}\n\n" + "\n".join(body_lines)
