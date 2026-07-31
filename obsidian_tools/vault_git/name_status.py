"""Parsing git's `-z` porcelain output, as pure functions over the decoded text — no `GitRunner`,
no `subprocess`.

**Why this is split out of `runner.py` and kept pure.** This is the one place in this codebase with
a demonstrated defect *class*, not just a single bug: `core.quotePath` C-quoting non-ASCII paths in
the line-oriented form (the reason `-z` is used at every call site that lists git paths, see
`split_nul_terminated` below), the `R100\0old\0new` three-field rename/copy shape being easy to get
wrong, and non-UTF-8 filenames needing `surrogateescape` decoding upstream rather than raising. Three
different ways to get this wrong is the same signal `baseline_selector.py`'s docstring describes for
a different module: the parsing rules need exactly one place to live, testable against adversarial
byte/character sequences directly, not only by way of a real git process and a real filesystem.

Operates on `str`, not `bytes`: `GitRunner.run` already decodes subprocess output with
`encoding="utf-8", errors="surrogateescape"` (PEP 383) before anything downstream sees it, so an
undecodable byte has already become a lone surrogate codepoint by the time it reaches this module —
there is no separate bytes-handling path to keep in sync with that decoding.
"""

from __future__ import annotations

from dataclasses import dataclass


def split_nul_terminated(output: str) -> list[str]:
    """Split `-z`-terminated git output into entries.

    `core.quotePath` defaults to true, so the ordinary line-oriented form of every git command that
    lists paths (`ls-tree --name-only`, `diff --name-only`, `diff --name-status`) C-quotes any path
    containing a non-ASCII byte, a literal quote, a backslash, or a control character — including a
    literal newline, which would otherwise land mid-record and desync a line-based split entirely.
    The quoted form also wraps the whole path in `"..."`, and those quote characters are part of the
    string `splitlines()` would hand back — passing that straight to another git invocation
    (`update-index --skip-worktree --`, in this codebase) fails with `fatal: Unable to mark file`
    because the quoted string no longer names a real path. `-z` sidesteps all of it: entries come
    back NUL-delimited and completely unquoted, so every call site that lists git paths in this
    codebase uses it exclusively, never the line-oriented form.
    """
    return [entry for entry in output.split("\0") if entry]


@dataclass(frozen=True, slots=True)
class NameStatusEntry:
    """One record from `git diff --cached --name-status -z`.

    `old_path` is set only for a detected rename/copy (status `R*`/`C*`), where git reports the
    source path in addition to the (always-present) current path.
    """

    status: str
    path: str
    old_path: str | None = None


def parse_name_status(output: str) -> list[NameStatusEntry]:
    """Parse `git diff --cached --name-status -z`'s raw stdout into structured records.

    A rename/copy record is three NUL-delimited fields (status, old path, new path) rather than the
    two every other status uses, which this walks explicitly by inspecting each status code's first
    character (`R100`, `C087`, ... carry a similarity percentage after the letter) rather than
    assuming every record is the same shape.
    """
    fields = split_nul_terminated(output)
    entries: list[NameStatusEntry] = []
    i = 0
    while i < len(fields):
        status = fields[i]
        if status[:1] in ("R", "C"):
            entries.append(NameStatusEntry(status=status, path=fields[i + 2], old_path=fields[i + 1]))
            i += 3
        else:
            entries.append(NameStatusEntry(status=status, path=fields[i + 1]))
            i += 2
    return entries
