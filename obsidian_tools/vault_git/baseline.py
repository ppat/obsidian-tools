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
under `.obsidian/` that was never captured — `workspace.json`/`workspaces.json` forever, or any new
file a later plugin drops in `.obsidian/` — because those are untracked, and untracked paths are
exactly what ignore rules (not skip-worktree) govern. Both mechanisms are required, and each covers
the files the other cannot: ignore rules for what git has never tracked, skip-worktree for what it
already has.
"""

from __future__ import annotations

import logging
from pathlib import Path

from obsidian_tools.vault_git.runner import GitRunner

logger = logging.getLogger(__name__)

OBSIDIAN_DIR = ".obsidian"

# Obsidian's own documentation names these two files specifically as per-instance workspace state
# that updates on every session and must never be shared across devices — exactly the files the
# baseline's forced add must never (re)capture.
WORKSPACE_STATE_FILES = (".obsidian/workspace.json", ".obsidian/workspaces.json")

_IGNORE_RULE_CONTENTS = """\
# Managed by obsidian-tools' git committer (obsidian_tools/vault_git/baseline.py) — do not edit.
#
# The vault volume is mounted read-only, so a tracked .gitignore cannot live in the work tree;
# this git-dir-local exclude file is the only place this rule can live. It stops `git add -A`
# from ever tracking a NEW path under .obsidian/ (including workspace.json/workspaces.json,
# forever, and anything a later plugin drops in there). It does NOT freeze a path the baseline
# commit already captured — git only consults ignore rules for untracked paths — so it does not
# substitute for the `update-index --skip-worktree` bits this module also sets; the two mechanisms
# cover disjoint sets of files.
.obsidian/
"""


def ensure_ignore_rule(git_dir: Path) -> None:
    """Idempotent: (re)writes the git-dir-local exclude file with fixed, deterministic contents."""
    exclude_path = git_dir / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    exclude_path.write_text(_IGNORE_RULE_CONTENTS)


def ensure_obsidian_baseline(runner: GitRunner, work_tree: Path) -> bool:
    """Idempotent baseline step. Returns True if this call staged the (one-time) baseline capture."""
    if runner.rev_parse_or_none("HEAD") is not None and runner.path_exists_at("HEAD", OBSIDIAN_DIR):
        for path in runner.list_tree_paths("HEAD", OBSIDIAN_DIR):
            if path not in WORKSPACE_STATE_FILES:
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

    # `--force` overrides *every* ignore rule, including a specific one — so once `.gitignore`
    # excludes `.obsidian/` wholesale (needed, since per-file ignores don't survive the freeze
    # either), a bare `--force .obsidian/` would recapture exactly the two per-instance files this
    # baseline exists to protect against. The exclusion has to live in this add's own pathspec.
    runner.run(
        [
            "add",
            "--force",
            "--",
            f"{OBSIDIAN_DIR}/",
            ":!.obsidian/workspace.json",
            ":!.obsidian/workspaces.json",
        ],
        retry=True,
    )
    staged_paths = runner.run(["diff", "--cached", "--name-only", "--", f"{OBSIDIAN_DIR}/"]).stdout.splitlines()
    for path in staged_paths:
        if path not in WORKSPACE_STATE_FILES:
            runner.run(["update-index", "--skip-worktree", "--", path])

    logger.info(
        "captured .obsidian/ baseline",
        extra={"event": "baseline_captured", "file_count": len(staged_paths)},
    )
    return True
