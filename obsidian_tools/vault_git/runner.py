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
from obsidian_tools.vault_git.name_status import NameStatusEntry, parse_name_status, split_nul_terminated

# Re-exported: every existing call site in this codebase imports NameStatusEntry from here, and
# `vault_git/name_status.py` (the pure parsing module this runner delegates to — see
# `staged_name_status` below) is where it's actually defined.
__all__ = ["GitCommandError", "GitRunner", "NameStatusEntry"]

# --- keeping the environment out of git's output -------------------------------------------------
#
# `git diff`'s output is not a fixed format: it is a *configurable* one, and a caller that makes a
# decision from it is reading something the machine's owner can replace. local-replicator is the one
# component of this system that runs outside the cluster, on the operator's own MacBook with a real
# `~/.gitconfig` (docs/DESIGN.md §4 Plane B) — and a single `[diff] external = ...`, a line
# difftastic's README instructs users to add, replaces every patch this codebase reads with a
# summary line carrying none of the content the publish gate is there to protect
# (ppat/obsidian-tools#3).
#
# The narrow reading of that fact is what let it happen twice. `core.quotePath=false` was pinned on
# this same clone one commit earlier, on the finding that unpinned git configuration corrupts patch
# output — recorded as being about path quoting, when the general fact is that git's output is
# user-configurable in ways that can replace it wholesale. So this closes the *class*, not the two
# settings that were found: everything the operator's environment can say about what git prints,
# stages, or checks out is cut off here, once, for every invocation.

# Neutralise every git configuration file outside this repository. `GIT_CONFIG_GLOBAL` /
# `GIT_CONFIG_SYSTEM` replace `~/.gitconfig` (and its XDG location) and `/etc/gitconfig`;
# `GIT_CONFIG_NOSYSTEM` is the older mechanism for the latter, set alongside so this holds on a git
# predating the pair; `GIT_ATTR_NOSYSTEM` does the same for `/etc/gitattributes`. Repository-local
# config is deliberately untouched — it is where this codebase's own pins live (`core.quotePath`,
# the committer's identity and `core.fileMode`), and it is not something the environment supplies.
#
# `core.attributesFile` and `core.excludesFile` are pinned as command-line config rather than left
# to the scrub, because their *defaults* point into the operator's home directory
# (`~/.config/git/attributes` and `~/.config/git/ignore`): unsetting the config that names them does
# not stop git reading them. `-c` outranks every configuration file, including this repository's
# own, so the pin cannot be edited away. The excludes pin is the one that matters most and is the
# least obvious: `git add -A` consults ignore rules for untracked paths, so one pattern in a global
# ignore file makes a note created on the phone invisible to the drift diff entirely — no patch to
# judge, nothing in `uncaptured`, and the publish's `--delete` removes it. It is scoped to the
# user-global file specifically: the vault's own tracked `.gitignore` and `$GIT_DIR/info/exclude`
# still apply, and must — `.obsidian/` exclusion depends on them (see `vault_git/baseline.py`).
_GIT_ENV_OVERRIDES = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_ATTR_NOSYSTEM": "1",
}

# The same reach, through environment variables rather than configuration files, which the scrub
# above cannot cover because they are not configuration files: `GIT_EXTERNAL_DIFF` is `diff.external`
# by another name, `GIT_CONFIG_PARAMETERS` / `GIT_CONFIG_COUNT` inject arbitrary config into every
# invocation, `GIT_INDEX_FILE` redirects the very index `add -A` and `diff --cached` talk to, and the
# pathspec-magic variables reinterpret the `:(literal)` pathspecs this codebase builds from real
# filenames. `GIT_CONFIG_COUNT`'s numbered `GIT_CONFIG_KEY_<n>`/`GIT_CONFIG_VALUE_<n>` companions
# are inert once the count is gone.
_GIT_ENV_REMOVED = (
    "GIT_EXTERNAL_DIFF",
    "GIT_EXTERNAL_DIFF_TRUST_EXIT_CODE",
    "GIT_DIFF_OPTS",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_INDEX_FILE",
    "GIT_LITERAL_PATHSPECS",
    "GIT_GLOB_PATHSPECS",
    "GIT_NOGLOB_PATHSPECS",
    "GIT_ICASE_PATHSPECS",
)

_GIT_CONFIG_PINS = ("core.attributesFile", "core.excludesFile")

# Flags for the two diff invocations whose *output is read as evidence* (`staged_name_status`,
# `staged_patch`), as opposed to the environment scrub's blanket cover. They are not redundant with
# it: an in-tree `.gitattributes` — which the vault could grow, or a device could create — reaches
# diff drivers by a path no environment variable controls, and only these flags close it.
#
# --no-ext-diff/--no-textconv: the two ways a driver substitutes its own text for git's patch.
#   textconv is the quiet one: it produces a plausible hunk with no binary marker for a file whose
#   bytes git never carried.
# --no-color: an ANSI-coloured patch is not what a Phase 5 consumer will parse, and git colours the
#   metadata lines the gate's marker sits among.
# --find-renames: rename detection is load-bearing, not cosmetic — a pure rename is captured
#   *because* its header describes the change completely (see `drift.captures_content`). With
#   detection off, a renamed binary decomposes into an add that carries nothing, and the cycle
#   pauses on drift that lost nothing.
_DECISION_DIFF_FLAGS = ("--no-ext-diff", "--no-textconv", "--no-color", "--find-renames")


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
        for key in _GIT_CONFIG_PINS:
            command.extend(["-c", f"{key}={os.devnull}"])
        command.extend(args)
        env = {key: value for key, value in os.environ.items() if key not in _GIT_ENV_REMOVED}
        env.update(_GIT_ENV_OVERRIDES)
        if self._ssh_command:
            env["GIT_SSH_COMMAND"] = self._ssh_command

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

    def merge_base_or_none(self, a: str, b: str) -> str | None:
        """The best common ancestor of `a` and `b`, or `None` if they share no history at all
        (`git merge-base` exits 1 with empty stdout in that case)."""
        result = self.run(["merge-base", a, b], check=False)
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def path_exists_at(self, ref: str, path: str) -> bool:
        result = self.run(["cat-file", "-e", f"{ref}:{path}"], check=False)
        return result.returncode == 0

    def list_tree_paths(self, tree_ish: str, path: str | None = None) -> list[str]:
        """Every path git tracks under `tree_ish` (a ref, SHA, or any other tree-ish), optionally
        scoped to `path`. `-z`: see `split_nul_terminated`."""
        args = ["ls-tree", "-r", "--name-only", "-z", tree_ish]
        if path is not None:
            args.extend(["--", path])
        result = self.run(args)
        return split_nul_terminated(result.stdout)

    def staged_paths(self, pathspec: str | None = None) -> list[str]:
        """Paths currently staged relative to HEAD (`git diff --cached --name-only -z`), optionally
        scoped to `pathspec`. `-z`: see `split_nul_terminated`."""
        args = ["diff", "--cached", "--name-only", "-z"]
        if pathspec is not None:
            args.extend(["--", pathspec])
        result = self.run(args)
        return split_nul_terminated(result.stdout)

    def staged_name_status(self) -> list[NameStatusEntry]:
        """`git diff --cached --name-status -z`, parsed into structured records by
        `vault_git/name_status.py`'s pure `parse_name_status` — this method's only job is running
        the git command and handing its raw stdout over.

        `_DECISION_DIFF_FLAGS`: this output and `staged_patch`'s must describe the same set of
        changes, so both are pinned identically — with rename detection settled differently between
        them, a status line claiming `R100` would be paired with a patch showing an unrelated
        add/delete pair."""
        result = self.run(["diff", "--cached", *_DECISION_DIFF_FLAGS, "--name-status", "-z"])
        return parse_name_status(result.stdout)

    def staged_patch(self, *pathspecs: str) -> str:
        """`git diff --cached` restricted to one or more pathspecs, as raw patch text.

        Added for `obsidian_tools/local_replicator/`'s replication cycle (ppat/obsidian-tools#3):
        its drift comparison stages the device overlay with `add -A` and then needs each staged
        path's content as a patch, not just its name from `staged_name_status` -- this is that
        primitive, kept generic here rather than grown as a one-off in the caller, since it is
        exactly as git-plumbing-shaped as `staged_paths`/`staged_name_status` already are. Pass
        both a rename's old and new path together so git's own diff machinery re-pairs them into
        one rename patch instead of two unrelated add/delete hunks. A caller building a pathspec
        from a real path should prefix it with `:(literal)` -- the same convention
        `vault_git/baseline.py` already uses -- so a filename containing a pathspec metacharacter
        (`*`, `[`, `?`) is matched as itself rather than reinterpreted as a pattern.

        `_DECISION_DIFF_FLAGS` is what makes this output *evidence*: the publish gate
        (`local_replicator/drift.py`) decides from this text whether a device-side edit was actually
        captured, and without these flags that text is whatever the machine's git configuration says
        it is. Do not drop them as noise — see the flags' own comment for what each one closes.
        """
        return self.run(["diff", "--cached", *_DECISION_DIFF_FLAGS, "--", *pathspecs]).stdout

    def write_staged_tree(self) -> str:
        """Write the tree object the current index would produce if committed right now, without
        actually committing (`git write-tree`). Reads only the index and the object store, never
        the work tree — used to derive the after-state of a staged change from git's own
        bookkeeping rather than by re-walking a work tree that may be partially unreadable."""
        return self.run(["write-tree"]).stdout.strip()

    def index_file_exists(self) -> bool:
        return (self.git_dir / "index").exists()
