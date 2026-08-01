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
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

from obsidian_tools.retry import retry_with_backoff
from obsidian_tools.vault_git.name_status import NameStatusEntry, parse_name_status, split_nul_terminated

# Re-exported: every existing call site in this codebase imports NameStatusEntry from here, and
# `vault_git/name_status.py` (the pure parsing module this runner delegates to — see
# `staged_name_status` below) is where it's actually defined.
__all__ = ["GitCommandError", "GitInvocationError", "GitRunner", "NameStatusEntry"]

# Prefix for the stand-in working directory `_spawn_with_cwd_fallback` creates when the OS refuses
# `work_tree` — named so an operator finding one stranded after a SIGKILL knows what left it.
_FALLBACK_CWD_PREFIX = "obsidian-tools-git-cwd-"

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

# `run()` below pins `cwd` to `work_tree`, not left as whatever directory launched this process:
# git's per-directory `.gitattributes` lookup does its own filesystem probe relative to the
# *process's* cwd, not to `--work-tree`, even though `--work-tree` is always given explicitly --
# verified directly (strace on a real invocation). `$GIT_DIR/info/attributes` and the system
# attributes file are unaffected (both opened by absolute path); this is specific to the
# per-directory stack.
#
# When that cwd-relative probe finds nothing at a given directory level -- the ordinary case, cwd
# unrelated to this repository -- git correctly falls back to that level's `.gitattributes` as
# recorded in the *index*. That fallback is not a degraded case here: every load-bearing diff in
# this codebase runs after `git add -A`, so the index already reflects anything the vault or the
# device just staged, and nothing this repository's own tracked `.gitattributes` says is lost by
# preferring it. But when the cwd probe *does* find a same-named file at the launching directory --
# an unrelated `.gitattributes` sitting wherever this process happened to start, matching only by
# name, never by content -- that file wins over the index fallback for that directory level, silently
# blanking or replacing whatever this repository's own tracked `.gitattributes` says there instead.
# That is a second, independent way the launching environment can reach a decision diff's output, on
# top of the ones `_GIT_ENV_OVERRIDES`/`_GIT_CONFIG_PINS`/`_DECISION_DIFF_FLAGS` already close --
# and unlike those, nothing above closes it, because none of them name the process's cwd. It is not
# hypothetical: it silently defeated the textconv tests in this project's own suite whenever pytest's
# cwd was this project's checkout root, which carries a `.gitattributes` of its own.
#
# Two other anchors were tried and rejected, both caught by this suite the moment they were tried
# rather than shipped -- recorded so a future reader doesn't reach for either again:
#
# - `git_dir` itself: always exists before this class's first call (`clone.py`/`provisioning.py`
#   create it first) and is never populated by the vault or a device, but its own top level *is* a
#   set of filenames git treats as revisions when they appear bare on the command line -- `HEAD`
#   above all, which every git directory contains unconditionally. `git reset --hard HEAD` run with
#   cwd equal to a directory containing a file literally named `HEAD` fails outright: "ambiguous
#   argument 'HEAD': both revision and filename."
# - a dedicated, permanently-empty subdirectory *inside* `git_dir` (avoiding the `HEAD` collision
#   above by construction): breaks something more basic than attributes. A relative pathspec --
#   `:(literal)<path>`, used everywhere in this codebase -- resolves against cwd *as a prefix within
#   the work tree*, not against the work tree's root, whenever cwd is a non-root descendant of the
#   work tree; local-replicator's `git_dir` sits inside its own `work_tree` (an ordinary clone, not
#   the committer's detached bare layout), so any cwd under it is such a descendant. Every diff and
#   `add -A` this codebase runs came back empty against a real, freshly-staged file -- silently, no
#   error, so this is the more dangerous of the two to have shipped.
#
# `work_tree` is the one directory that is simultaneously the correct root for both: pathspecs
# resolve correctly *because* cwd-as-prefix and work-tree-root coincide when cwd *is* the work-tree
# root, and it is what git's own per-directory `.gitattributes` docs assume "running from the
# repository" means. It reintroduces a narrower version of the `git_dir` collision above -- a vault
# or committer mount path literally named `HEAD` with no extension would make `reset --hard HEAD`
# ambiguous again -- checked directly against every other bare `HEAD` argument this codebase passes
# (`rev-parse --verify --quiet`, `ls-tree`, `read-tree`): none of the others are ambiguous, because
# none of those commands accept a pathspec in the same argument position, so `HEAD` cannot mean two
# things to them the way it can to `reset`/`checkout`/`diff`. Only `cycle.py`'s
# `reset -q --hard HEAD` needed the trailing `--` git's own error message recommends; see that call
# site.
#
# **`work_tree` is not guaranteed to exist, and pinning cwd to it unconditionally is therefore not
# safe on its own** -- `_FALLBACK_CWD_PREFIX`/`_spawn_with_cwd_fallback` below are what make it so.
# The committer's `work_tree` is `/vault/brain`, which is *not* its mount: the CronJob mounts the
# vault PVC at `/vault` and keeps the vault one directory down, because the mount root carries an
# ext4 `lost+found` that uid cannot read (`config.py`'s `OBSIDIAN_VAULT_DIR`, and the Obsidian
# Deployment for the same reason). `/vault/brain` is an ordinary directory *inside* a volume this
# workload mounts `readOnly: true`, so it cannot create it, and on a freshly provisioned or restored
# `vault-data` PVC it is simply absent. A `cwd` naming a directory that does not exist fails in
# `subprocess.run` -- an `OSError` raised *before* git is executed at all, which is neither a
# `GitCommandError` nor anything else this codebase's handlers name. Unpinned, that run degraded:
# every git call that does not need a work tree (all of provisioning) still succeeded, and the ones
# that do failed as ordinary non-zero git exits, retried and classified. Pinned naively, the very
# first call died instead -- raw traceback, no structured event, no push catch-up, every 15 minutes.
#
# So the pin is attempted, not asserted: if the OS refuses `work_tree` as a working directory --
# ENOENT, ENOTDIR, EACCES, or (this being a soft-mounted NFS export -- see `obsidian_tools/retry.py`)
# ETIMEDOUT/ESTALE/EIO -- the same invocation is retried once from a freshly created, empty,
# private temporary directory. Driven by the failure that actually occurred rather than by an
# `is_dir()` check beforehand: an existence check races the mount and covers only one errno, and
# `subprocess` reports the chdir failure before `exec`, so nothing has run and re-spawning is safe.
#
# An empty temporary directory is a *security-equivalent* anchor for the reason the pin exists at
# all, which is the only reason it may stand in: git's cwd-relative probe finds no `.gitattributes`
# there (it is freshly created and private, unlike the launching directory or any shared `/tmp`), so
# git falls back to the index's own copy -- verified directly to produce output identical to
# cwd=`work_tree` for a decision diff, and the hostile-file case still resolves to the launching
# directory's file when the pin is dropped. It is also outside the work tree, so relative pathspecs
# keep resolving against the work-tree root rather than picking up a prefix (the trap that sank the
# rejected `git_dir` subdirectory anchor above), and it holds no `HEAD`-shaped filename.


class GitCommandError(RuntimeError):
    """A git invocation exited non-zero."""

    def __init__(
        self, git_args: Sequence[str], result: subprocess.CompletedProcess[str], *, message: str | None = None
    ) -> None:
        # Not named `self.args` — BaseException already defines that attribute (as a tuple), and
        # shadowing it with a list trips strict type checking for no benefit.
        self.git_args = list(git_args)
        self.result = result
        if message is None:
            detail = result.stderr.strip() or result.stdout.strip()
            message = f"git {' '.join(git_args)} exited {result.returncode}: {detail}"
        # `message` exists for `GitInvocationError` below, whose whole point is that git never ran:
        # the default wording asserts an exit status there is none of.
        super().__init__(message)


class GitInvocationError(GitCommandError):
    """git never ran: the OS refused the invocation itself (an `OSError` out of `subprocess.run`,
    raised before `exec`) rather than git starting and exiting non-zero.

    **A subclass of `GitCommandError` deliberately, not a sibling.** Every handler in this codebase
    is written against `GitCommandError` — `run`'s own `retry_on`, `commands/commit.py`'s
    `_STAGING_FAILURES`, its provisioning/commit `except` clauses, `vault_git/commit.py`'s
    `push_all` — and an `OSError` escaping all of them, unretried and unclassified, is exactly the
    hole this exists to close. Subclassing closes it at every one of those sites at once, including
    ones added later, rather than by enumerating them here and hoping the list stays complete.

    `result` is synthesized so the attribute stays present and typed for the one consumer that
    reads it (`commands/commit.py`'s `_staging_error_kind`, which short-circuits on this type
    before it gets there). `returncode` is `-1`: there is no exit status, because there was no
    process. The errno and the directory the OS refused are on `os_error`/`cwd` and in the message.
    """

    def __init__(self, git_args: Sequence[str], cwd: Path, error: OSError) -> None:
        self.cwd = cwd
        self.os_error = error
        super().__init__(
            git_args,
            subprocess.CompletedProcess(args=["git", *git_args], returncode=-1, stdout="", stderr=str(error)),
            message=f"git {' '.join(git_args)} could not be started in {cwd}: {error}",
        )


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
            result = self._spawn_with_cwd_fallback(args, command, env)
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

    def _spawn(self, command: Sequence[str], env: dict[str, str], cwd: Path | str) -> subprocess.CompletedProcess[str]:
        # `encoding="utf-8", errors="surrogateescape"` rather than the plain `text=True` this used
        # to be: a filename that is not valid UTF-8 (`b"caf\xe9.md"`, legal on the volume) is staged
        # fine by `git add -A` — the failure was always in *decoding it back*, in every call site
        # downstream that lists paths (`ls-tree`, `diff --name-status`, `diff --name-only`). Strict
        # decoding raised a bare `UnicodeDecodeError` there — not a `GitCommandError`, so nothing in
        # commands/commit.py's exception handling ever caught it, and the same file wedges every
        # later run identically since nothing removes it. `surrogateescape` (PEP 383) round-trips an
        # undecodable byte through a lone surrogate codepoint losslessly; passing that same string
        # back into a later argv (e.g. `update-index --skip-worktree --`) re-encodes it via the
        # identical mechanism, since subprocess already encodes str argv elements with `os.fsencode`
        # (also surrogateescape on POSIX) independent of this method's stdout/stderr decoding.
        return subprocess.run(
            command,
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
            env=env,
            check=False,
            cwd=cwd,
        )

    def _spawn_with_cwd_fallback(
        self, args: Sequence[str], command: Sequence[str], env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        """Run `command` with cwd pinned to `work_tree`, falling back to an empty private directory
        if the OS refuses that one — see the `work_tree`-is-not-guaranteed section of this module's
        header comment for why both halves are load-bearing.

        Any `OSError` that survives both attempts is translated into `GitInvocationError`, so it
        joins the classified vocabulary the rest of this codebase already handles instead of
        escaping every handler as a raw `OSError`. Translated rather than swallowed: it is a real
        failure of this invocation, just not one git itself produced."""
        try:
            return self._spawn(command, env, self.work_tree)
        except OSError as work_tree_error:
            try:
                # `mkdtemp`, not a fixed path under the system temp directory: that directory is
                # world-writable, and a `.gitattributes` planted in it by anything else on the host
                # would reopen the very hole the cwd pin closes. `mkdtemp` creates a fresh 0700
                # directory nobody else can write into or predict.
                fallback_cwd = tempfile.mkdtemp(prefix=_FALLBACK_CWD_PREFIX)
            except OSError as fallback_error:
                raise GitInvocationError(args, self.work_tree, work_tree_error) from fallback_error
            try:
                return self._spawn(command, env, fallback_cwd)
            except OSError as fallback_error:
                # Not the work tree's fault at this point (a plain `git --version` would fail the
                # same way): the executable or the process environment itself is what the OS
                # refused. Chained to the original so the traceback still names the work tree.
                raise GitInvocationError(args, Path(fallback_cwd), fallback_error) from work_tree_error
            finally:
                shutil.rmtree(fallback_cwd, ignore_errors=True)

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
