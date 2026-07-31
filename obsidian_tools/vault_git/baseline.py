"""The `.obsidian/` baseline: captured once, then frozen.

See docs/DESIGN.md §7 Phase 2 / §8a D3 and ppat/obsidian-tools#3 for the full reasoning; the
short version: `.obsidian/` exists only to get Mac/iOS clients configured quickly with a common
baseline, and a setting changed later at the cluster GUI must not reach devices.

**`.gitignore` cannot achieve that freeze — this was found empirically, not reasoned out in
advance.** Git only consults ignore rules for *untracked* paths. Once the baseline commit has
captured a file, a later change to that same file is still staged by a plain `git add -A`
regardless of what `.gitignore` says — the ignore entry is silently inert for exactly the files it
was added to protect. `git update-index --skip-worktree` is the actual mechanism.

Two independent facts matter here and must not be conflated:

1. **Has the baseline commit ever been taken?** Asked against `HEAD`'s tree — durable history
   fetched fresh from `origin` on every run (see vault_git/provisioning.py) — never against a
   marker file or index state living on the git-dir cache. That cache is itself lost-and-recovered;
   a marker file on the very volume that can vanish would desynchronize from what's actually in
   history, either re-triggering a capture that already happened or skipping one that hasn't.
2. **Are the skip-worktree bits currently set on the local index?** This genuinely is per-index,
   per-clone state that does not survive a lost/rebuilt cache even when the underlying commit does
   (a fresh `read-tree HEAD` populates the index with tracked paths, but skip-worktree is an index
   flag, not part of the tree object, so it comes back cleared). It is therefore reapplied
   unconditionally on every run to every baselined path found in `HEAD`'s tree — reapplying an
   already-set bit is a harmless no-op, which is what makes doing this unconditionally both correct
   and simple.

A third mechanism, `ensure_ignore_rule`, is a *separate* concern from both of the above and easy to
mistake for redundant with skip-worktree — it isn't. Skip-worktree only stops `git add -A` from
re-staging a *change* to a path already captured in the baseline commit. It does nothing for a path
under `.obsidian/` that was never captured — any file outside the allowlist below, forever — because
those are untracked, and untracked paths are exactly what ignore rules (not skip-worktree) govern.
Both mechanisms are required, and each covers the files the other cannot: ignore rules for what git
has never tracked, skip-worktree for what it already has.

**The capture itself is an allowlist, never a denylist — see `baseline_selector.py`.** That module
holds the actual list of what gets captured (and why) plus `select_baseline_paths`, the pure
function every safety rule is checked in exactly once. This module is the thin shell around it: walk
`.obsidian/` into `PathInfo` candidates, hand them to the selector, and turn what comes back into
git calls. It intentionally does no allowlist-shaped reasoning of its own — see `baseline_selector.py`'s
module docstring for why three prior patches each fixed the rule in one loop and missed another, and
why that means the walk-and-decide logic must never be fused back together.
"""

from __future__ import annotations

import logging
from pathlib import Path

from obsidian_tools.vault_git.baseline_selector import PathInfo, select_baseline_paths
from obsidian_tools.vault_git.runner import GitRunner

logger = logging.getLogger(__name__)

OBSIDIAN_DIR = ".obsidian"

_IGNORE_RULE_CONTENTS = """\
# Managed by obsidian-tools' git committer (obsidian_tools/vault_git/baseline.py) — do not edit.
#
# ppat/obsidian-vault's own tracked .gitignore already lists .obsidian/ wholesale, but this
# component cannot depend on that alone: the vault volume is mounted read-only, so if that tracked
# file were ever missing, renamed, or edited to drop the line, this committer has no way to create
# or repair one in the work tree, and no way to notice. This git-dir-local exclude file is the copy
# of the rule this component actually controls. It stops `git add -A` from ever tracking a NEW path
# under .obsidian/ (anything outside the baseline allowlist below, forever). It does NOT freeze a
# path the baseline commit already captured — git only consults ignore rules for untracked paths —
# so it does not substitute for the `update-index --skip-worktree` bits this module also sets; the
# two mechanisms cover disjoint sets of files.
.obsidian/
"""


def ensure_ignore_rule(git_dir: Path) -> None:
    """Idempotent: (re)writes the git-dir-local exclude file with fixed, deterministic contents."""
    exclude_path = git_dir / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    exclude_path.write_text(_IGNORE_RULE_CONTENTS)


def _iter_obsidian_candidates(directory: Path, prefix: str = "") -> list[PathInfo]:
    """Walk `.obsidian/` once into `PathInfo` candidates, relative to `.obsidian/` itself.

    Mirrors `os.walk(..., followlinks=False)`: never descends into a symlinked directory, so no
    candidate for anything beneath one is ever produced in the first place. That is the actual
    mechanism that keeps a symlinked plugin directory (the standard local plugin-development layout,
    `.obsidian/plugins/my-plugin -> ~/dev/my-plugin`) from ever reaching `git add` as a pathspec
    "beyond a symbolic link" — see `test_symlinked_plugin_directory_does_not_wedge_the_committer`.
    `select_baseline_paths` also rejects any candidate with `is_symlink` set, as a second,
    independent check — a mistake here should not be the only thing standing between a symlink and
    permanent history.

    A directory this process can't read (a transient NFS glitch, matching the tolerance
    `check_for_mass_deletion` already documents for the same volume) is skipped rather than raising:
    the next scheduled run tries again, same as every other "not there yet" case this module treats
    as routine rather than fatal.
    """
    candidates: list[PathInfo] = []
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return candidates

    for entry in entries:
        relative_path = f"{prefix}{entry.name}"
        try:
            is_symlink = entry.is_symlink()
            is_dir = entry.is_dir()
            is_file = entry.is_file()
        except OSError:
            continue

        if is_dir and not is_symlink:
            candidates.extend(_iter_obsidian_candidates(entry, f"{relative_path}/"))
            continue

        candidates.append(PathInfo(relative_path=relative_path, is_file=is_file, is_symlink=is_symlink))

    return candidates


def _baseline_paths(work_tree: Path) -> list[str]:
    """Enumerate the allowlisted `.obsidian/` paths actually present, `.obsidian/`-prefixed and
    relative to `work_tree` — the walk-and-decide split described in the module docstring.

    Every returned path existed at enumeration time, so it was a valid pathspec then — but this
    process does not hold any lock on the (read-only, NFS-backed) work tree between here and the
    `git add --force --` call that actually consumes these as pathspecs, so a path can still vanish
    in that window. That's a real, if narrow, TOCTOU gap — not something this list can rule out by
    construction — and it self-heals on the next run either way, since `git add -A`/`stage_all`
    reconciles the whole index against the current work tree regardless of what a prior cycle saw.
    Each pathspec also carries the `:(literal)` pathspec magic prefix (applied where the paths are
    actually passed to git, not here) specifically so a filename containing a glob metacharacter
    (`*`, `[`, `?`) is matched as itself rather than re-interpreted as a pattern.
    """
    obsidian_dir = work_tree / OBSIDIAN_DIR
    candidates = _iter_obsidian_candidates(obsidian_dir)
    return [f"{OBSIDIAN_DIR}/{path}" for path in select_baseline_paths(candidates)]


def ensure_obsidian_baseline(runner: GitRunner, work_tree: Path) -> bool:
    """Idempotent baseline step. Returns True if this call staged the (one-time) baseline capture."""
    if runner.rev_parse_or_none("HEAD") is not None and runner.path_exists_at("HEAD", OBSIDIAN_DIR):
        for path in runner.list_tree_paths("HEAD", OBSIDIAN_DIR):
            runner.run(["update-index", "--skip-worktree", "--", path])
        return False

    if not (work_tree / OBSIDIAN_DIR).is_dir():
        # Nothing to baseline yet — e.g. the committer's very first run, before headless Obsidian
        # has created .obsidian/ on the volume at all. Not an error: the next run tries again.
        logger.info(
            "obsidian baseline not taken yet, and .obsidian/ is absent from the work tree; skipping this cycle",
            extra={"event": "baseline_skip_no_obsidian_dir"},
        )
        return False

    baseline_paths = _baseline_paths(work_tree)
    if not baseline_paths:
        # .obsidian/ exists but nothing on the allowlist has landed in it yet (e.g. plugins haven't
        # finished seeding). Not an error: the next run tries again once something allowlisted shows up.
        logger.info(
            "obsidian baseline not taken yet, and no allowlisted .obsidian/ paths are present; skipping this cycle",
            extra={"event": "baseline_skip_no_allowlisted_paths"},
        )
        return False

    # `:(literal)` pathspec magic: these paths came from a real directory listing, not a human, but
    # a filename containing a glob metacharacter (`*`, `[a-z]`, `?`) would otherwise be
    # re-interpreted by git as a pattern rather than matched as itself — `:(literal)` pins each
    # pathspec to exactly the path it names.
    literal_pathspecs = [f":(literal){path}" for path in baseline_paths]
    runner.run(["add", "--force", "--", *literal_pathspecs], retry=True)
    staged_paths = runner.staged_paths(f"{OBSIDIAN_DIR}/")
    for path in staged_paths:
        runner.run(["update-index", "--skip-worktree", "--", path])

    logger.info(
        "captured .obsidian/ baseline",
        extra={"event": "baseline_captured", "file_count": len(staged_paths)},
    )
    return True
