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
from pathlib import Path

from obsidian_tools.retry import retry_with_backoff


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
            result = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
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

    def list_tree_paths(self, ref: str, path: str) -> list[str]:
        result = self.run(["ls-tree", "-r", "--name-only", ref, "--", path])
        return [line for line in result.stdout.splitlines() if line]

    def index_file_exists(self) -> bool:
        return (self.git_dir / "index").exists()
