"""Thin wrapper around the detached ``--git-dir``/``--work-tree`` invocation this project requires.

The vault volume is mounted read-only and git's own metadata lives on a separate writable volume,
so every git call in this codebase names both paths explicitly rather than relying on `cwd`-based
discovery. Never `chdir` into the work tree and rely on git to find a `.git` by walking up — there
must never be one inside the vault directory at all: headless Obsidian watches every directory in
the vault it's given, and the Mac-side replication script's rsync into iCloud has to exclude
whatever git metadata exists, so keeping it structurally outside the vault removes both problems at
once rather than working around them (see docs/DESIGN.md §1.3 P4 and ppat/obsidian-tools#3).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from obsidian_tools.retry import retry_with_backoff


def _split_nul(output: str) -> list[str]:
    """Split ``-z``-terminated git output into entries.

    ``core.quotePath`` defaults to true, so the ordinary line-oriented form of every git command
    that lists paths (``ls-tree --name-only``, ``diff --name-only``, ``diff --name-status``)
    C-quotes any path containing a non-ASCII byte, a literal quote, a backslash, or a control
    character — including a literal newline, which would otherwise land mid-record and desync a
    line-based split entirely. The quoted form also wraps the whole path in `"..."`, and those
    quote characters are part of the string `splitlines()` would hand back — passing that straight
    to another git invocation (`update-index --skip-worktree --`, in this codebase) fails with
    `fatal: Unable to mark file` because the quoted string no longer names a real path. ``-z``
    sidesteps all of it: entries come back NUL-delimited and completely unquoted, so every call
    site that lists git paths in this codebase uses it exclusively, never the line-oriented form.
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


class GitCommandError(RuntimeError):
    """A git invocation exited non-zero."""

    def __init__(self, git_args: Sequence[str], result: subprocess.CompletedProcess[str]) -> None:
        # Not named `self.args` — BaseException already defines that attribute (as a tuple), and
        # shadowing it with a list trips strict type checking for no benefit.
        self.git_args = list(git_args)
        self.result = result
        detail = result.stderr.strip() or result.stdout.strip()
        super().__init__(f"git {' '.join(git_args)} exited {result.returncode}: {detail}")


class GitRunner:
    """Runs git against one detached (git-dir, work-tree) pair, optionally over SSH."""

    def __init__(self, git_dir: Path, work_tree: Path, *, ssh_command: str | None = None) -> None:
        self.git_dir = git_dir
        self.work_tree = work_tree
        self._ssh_command = ssh_command

    def run(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        retry: bool = False,
        retries: int | None = None,
        base_delay: float | None = None,
        include_work_tree: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = ["git", f"--git-dir={self.git_dir}"]
        if include_work_tree:
            command.append(f"--work-tree={self.work_tree}")
        command.extend(args)
        env: dict[str, str] | None = None
        if self._ssh_command:
            env = {**os.environ, "GIT_SSH_COMMAND": self._ssh_command}

        def _invoke() -> subprocess.CompletedProcess[str]:
            # `encoding="utf-8", errors="surrogateescape"` rather than the plain `text=True` this
            # used to be: a filename that is not valid UTF-8 (`b"caf\xe9.md"`, legal on the volume)
            # is staged fine by `git add -A` — the failure was always in *decoding it back*, in
            # every call site downstream that lists paths (`ls-tree`, `diff --name-status`,
            # `diff --name-only`). Strict decoding raised a bare `UnicodeDecodeError` there — not a
            # `GitCommandError`, so nothing in commands/commit.py's exception handling ever caught
            # it, and the same file wedges every later run identically since nothing removes it.
            # `surrogateescape` (PEP 383) round-trips an undecodable byte through a lone surrogate
            # codepoint losslessly; passing that same string back into a later argv (e.g.
            # `update-index --skip-worktree --`) re-encodes it via the identical mechanism, since
            # subprocess already encodes str argv elements with `os.fsencode` (also surrogateescape
            # on POSIX) independent of this method's stdout/stderr decoding.
            result = subprocess.run(
                command, capture_output=True, encoding="utf-8", errors="surrogateescape", env=env, check=False
            )
            if check and result.returncode != 0:
                raise GitCommandError(args, result)
            return result

        if retry:
            # `retries`/`base_delay` are resolved inside retry_with_backoff from
            # obsidian_tools.retry's module attributes when left as None here — deliberately not
            # baked in as this method's own default argument values, so tests can turn retry
            # tuning down globally (monkeypatching those attributes) without every call site along
            # the way needing its own retry-tuning parameters.
            return retry_with_backoff(
                _invoke,
                description=f"git {' '.join(args)}",
                retries=retries,
                base_delay=base_delay,
                retry_on=(GitCommandError,),
            )
        return _invoke()

    def rev_parse_or_none(self, ref: str) -> str | None:
        result = self.run(["rev-parse", "--verify", "--quiet", ref], check=False)
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = self.run(["merge-base", "--is-ancestor", ancestor, descendant], check=False)
        return result.returncode == 0

    def path_exists_at(self, ref: str, path: str) -> bool:
        result = self.run(["cat-file", "-e", f"{ref}:{path}"], check=False)
        return result.returncode == 0

    def list_tree_paths(self, tree_ish: str, path: str | None = None) -> list[str]:
        """Every path git tracks under `tree_ish` (a ref, SHA, or any other tree-ish), optionally
        scoped to `path`. `-z`: see `_split_nul`."""
        args = ["ls-tree", "-r", "--name-only", "-z", tree_ish]
        if path is not None:
            args.extend(["--", path])
        result = self.run(args)
        return _split_nul(result.stdout)

    def staged_paths(self, pathspec: str | None = None) -> list[str]:
        """Paths currently staged relative to HEAD (`git diff --cached --name-only -z`), optionally
        scoped to `pathspec`. `-z`: see `_split_nul`."""
        args = ["diff", "--cached", "--name-only", "-z"]
        if pathspec is not None:
            args.extend(["--", pathspec])
        result = self.run(args)
        return _split_nul(result.stdout)

    def staged_name_status(self) -> list[NameStatusEntry]:
        """`git diff --cached --name-status -z`, parsed into structured records. `-z`: see
        `_split_nul` — a rename/copy record is three NUL-delimited fields (status, old path, new
        path) rather than two, which this parses explicitly rather than assuming every record is
        the same shape."""
        result = self.run(["diff", "--cached", "--name-status", "-z"])
        fields = _split_nul(result.stdout)
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

    def write_staged_tree(self) -> str:
        """Write the tree object the current index would produce if committed right now, without
        actually committing (`git write-tree`). Reads only the index and the object store, never
        the work tree — used to derive the after-state of a staged change from git's own
        bookkeeping rather than by re-walking a work tree that may be partially unreadable."""
        return self.run(["write-tree"]).stdout.strip()

    def index_file_exists(self) -> bool:
        return (self.git_dir / "index").exists()
