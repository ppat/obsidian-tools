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

**The capture itself is an allowlist, never a denylist.** An earlier version of this module forced
`.obsidian/` wholesale minus the two known workspace-state files, which would have committed
`.obsidian/plugins/obsidian-local-rest-api/data.json` — the file holding the vault's Local REST API
bearer token (docs/DESIGN.md §2 item 5) — into permanent history on both remotes, pulled to the Mac
clone, and published into iCloud and onto the phone (caught by review before it ever ran; see
ppat/obsidian-tools#3). `data.json` is the conventional filename for *every* Obsidian plugin's
settings, not just this one, so a denylist would have to enumerate every current and future
secret-bearing file to stay safe — the next plugin that stores a credential there reintroduces the
same leak silently. An allowlist of what a device baseline actually needs doesn't have that failure
mode: a new plugin's `data.json` is excluded by never appearing on the list, not by someone
remembering to add it to an exclusion.
"""

from __future__ import annotations

import logging
from pathlib import Path

from obsidian_tools.vault_git.runner import GitRunner

logger = logging.getLogger(__name__)

OBSIDIAN_DIR = ".obsidian"

# Obsidian's own documentation names these two files specifically as per-instance workspace state
# that updates on every session and must never be shared across devices — exactly the files the
# baseline's forced add must never (re)capture. Never on either allowlist below, so a denylist
# check against them here is now redundant, not load-bearing — kept for now, unconditional
# skip-worktree reapplication for tracked-but-hand-seeded workspace files is a separate fix.
WORKSPACE_STATE_FILES = (".obsidian/workspace.json", ".obsidian/workspaces.json")

# What a device baseline actually needs: shared application config, never per-plugin state.
_BASELINE_TOP_LEVEL_FILES = (
    "app.json",
    "appearance.json",
    "core-plugins.json",
    "community-plugins.json",
    "hotkeys.json",
    "types.json",
)
_BASELINE_DIRS = ("snippets", "themes")

# A community plugin's own settings/state conventionally lives in `data.json` inside its plugin
# directory — the REST API plugin's bearer token included. Only a plugin's *code* is ever
# baselined; `data.json` is never on this list, for this plugin or any other, present or future.
_PLUGIN_CODE_FILES = ("manifest.json", "main.js", "styles.css")

_IGNORE_RULE_CONTENTS = """\
# Managed by obsidian-tools' git committer (obsidian_tools/vault_git/baseline.py) — do not edit.
#
# The vault volume is mounted read-only, so a tracked .gitignore cannot live in the work tree;
# this git-dir-local exclude file is the only place this rule can live. It stops `git add -A`
# from ever tracking a NEW path under .obsidian/ (anything outside the baseline allowlist below,
# forever). It does NOT freeze a path the baseline commit already captured — git only consults
# ignore rules for untracked paths — so it does not substitute for the `update-index
# --skip-worktree` bits this module also sets; the two mechanisms cover disjoint sets of files.
.obsidian/
"""


def ensure_ignore_rule(git_dir: Path) -> None:
    """Idempotent: (re)writes the git-dir-local exclude file with fixed, deterministic contents."""
    exclude_path = git_dir / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    exclude_path.write_text(_IGNORE_RULE_CONTENTS)


def _baseline_paths(work_tree: Path) -> list[str]:
    """Enumerate the allowlisted `.obsidian/` paths actually present, relative to `work_tree`.

    Only paths that exist are returned, so the resulting list is safe to pass straight to
    `git add --force --`: every entry is a real, existing pathspec, never one that would make the
    add fail with "did not match any files" because a given device baseline happens not to need it
    (e.g. no snippets yet, or a plugin that ships without a stylesheet).
    """
    obsidian_dir = work_tree / OBSIDIAN_DIR
    paths: list[str] = []

    for name in _BASELINE_TOP_LEVEL_FILES:
        if (obsidian_dir / name).is_file():
            paths.append(f"{OBSIDIAN_DIR}/{name}")

    for dirname in _BASELINE_DIRS:
        directory = obsidian_dir / dirname
        if directory.is_dir():
            paths.extend(
                str(file_path.relative_to(work_tree))
                for file_path in sorted(directory.rglob("*"))
                if file_path.is_file()
            )

    plugins_dir = obsidian_dir / "plugins"
    if plugins_dir.is_dir():
        for plugin_dir in sorted(p for p in plugins_dir.iterdir() if p.is_dir()):
            for filename in _PLUGIN_CODE_FILES:
                file_path = plugin_dir / filename
                if file_path.is_file():
                    paths.append(str(file_path.relative_to(work_tree)))

    return paths


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

    baseline_paths = _baseline_paths(work_tree)
    if not baseline_paths:
        # .obsidian/ exists but nothing on the allowlist has landed in it yet (e.g. plugins haven't
        # finished seeding). Not an error: the next run tries again once something allowlisted shows up.
        logger.info(
            "obsidian baseline not taken yet, and no allowlisted .obsidian/ paths are present; skipping this cycle",
            extra={"event": "baseline_skip_no_allowlisted_paths"},
        )
        return False

    runner.run(["add", "--force", "--", *baseline_paths], retry=True)
    staged_paths = runner.run(["diff", "--cached", "--name-only", "--", f"{OBSIDIAN_DIR}/"]).stdout.splitlines()
    for path in staged_paths:
        if path not in WORKSPACE_STATE_FILES:
            runner.run(["update-index", "--skip-worktree", "--", path])

    logger.info(
        "captured .obsidian/ baseline",
        extra={"event": "baseline_captured", "file_count": len(staged_paths)},
    )
    return True
