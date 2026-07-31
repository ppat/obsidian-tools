"""The `.obsidian/` baseline allowlist, as a pure function over a description of what exists.

**Why this is split out of `baseline.py` and kept pure.** The question this module answers —
"given these paths, which are safe to capture?" — has been patched three times
(ppat/obsidian-tools#3, #22) and an independent review has found a new hole each time:

1. A denylist that would have committed a plugin's `data.json` (a bearer token).
2. `themes/`/`snippets/` named as bare directory prefixes, admitting a file of any name at any
   depth underneath them (`themes/deep/nested/inner/data.json`).
3. Symlinks excluded on two of three enumeration branches, but not the plugins branch — a bare
   `if file_path.is_file():` follows a symlink, so a symlinked plugin directory or a symlinked file
   inside a real plugin directory was still captured.

Three holes of the same *kind*, in the same function, is a design signal: the safety rules (is a
real file, is not a symlink, is an allowlisted name at the right depth) were being re-derived by
hand in three near-identical loops, so a fix to one loop had no way to reach the other two. Putting
`select_baseline_paths` here — pure, filesystem- and git-free, one copy of every rule — makes that
class of bug structurally impossible: there is no second loop left to fall out of sync with the
first. It also makes the rules cheap to test: a pure function over a list of descriptors can be fed
hundreds of adversarial cases in milliseconds, where the fused walk-and-stage version could only be
tested by building real directory trees one at a time.

Do not re-fuse this into a filesystem walk. If a future change needs `.obsidian/` capture to depend
on something only the filesystem knows, add that as another field on `PathInfo` and another rule
here — not as a fourth loop somewhere else that has to remember the existing three rules by hand.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

# What a device baseline actually needs: shared application config, never per-plugin state.
# Obsidian's own documentation names `workspace.json`/`workspaces.json` as per-instance state that
# updates every session — they are excluded from this baseline by never appearing on either list
# below, not by a denylist entry naming them specifically (see the module docstring for why that
# distinction is the whole point).
BASELINE_TOP_LEVEL_FILES = (
    "app.json",
    "appearance.json",
    "core-plugins.json",
    "community-plugins.json",
    "hotkeys.json",
    "types.json",
)

# Constrained to *.css (plus a theme's own manifest.json, below) — never the bare directory. A bare
# `snippets/`/`themes/` prefix admits every file of any name at any depth, sight unseen, which is
# the same allowlist-as-denylist mistake the plugin `data.json` exclusion exists to prevent, one
# level down: a probe against an earlier revision staged `.obsidian/themes/Minimal/data.json`,
# `.obsidian/themes/deep/nested/inner/data.json` and `.obsidian/snippets/sub/dir/creds.json`
# (ppat/obsidian-tools#3). Themes are third-party code installed through the ungated GUI path
# (docs/DESIGN.md Sec 1.3 P8), so "nothing secret would ever land under there" is not a claim this
# baseline gets to make.
BASELINE_DIRS = ("snippets", "themes")
_BASELINE_CSS_SUFFIX = ".css"

# A theme's own manifest, at exactly one level below its own directory (`themes/<name>/manifest.json`)
# — never at any depth via a recursive glob. `docs/settings-lock.md`'s own hand-run checklist command
# always named this depth explicitly (`.obsidian/themes/*/manifest.json`); an earlier revision of
# this module's *.rglob("manifest.json")* call let a manifest at any depth through, one narrowing
# looser than the checklist it's supposed to automate.
_THEME_MANIFEST_FILENAME = "manifest.json"

# A community plugin's own settings/state conventionally lives in `data.json` inside its plugin
# directory — the Local REST API plugin's bearer token included. Only a plugin's *code* is ever
# baselined, at exactly one level below its own directory (`plugins/<name>/<file>`); `data.json` is
# never on this list, for this plugin or any other, present or future.
PLUGIN_CODE_FILES = ("manifest.json", "main.js", "styles.css")


@dataclass(frozen=True, slots=True)
class PathInfo:
    """One filesystem entry under `.obsidian/`, described the way the selector needs to see it —
    nothing here is a `Path` or touches disk.

    `relative_path` is POSIX-style (`/`-separated) and relative to `.obsidian/` itself, e.g.
    `"plugins/obsidian-local-rest-api/manifest.json"` — never includes the `.obsidian/` prefix.

    `is_symlink` means "a symlink is involved anywhere in reaching this entry": either the leaf
    itself is a symlink, or some directory between `.obsidian/` and the leaf is. Collapsing both
    cases into one flag is deliberate — this function has no filesystem to walk back up and check
    ancestors itself, so whoever builds the candidate list (which does have the filesystem) is
    responsible for computing it. It is what lets one rule ("skip if `is_symlink`") cover both the
    symlinked-plugin-directory wedge and the symlinked-leaf-file security hole with the same check.
    """

    relative_path: str
    is_file: bool
    is_symlink: bool


def select_baseline_paths(candidates: Iterable[PathInfo]) -> list[str]:
    """The one place every `.obsidian/` baseline safety rule applies. Returns `relative_path` (never
    `.obsidian/`-prefixed) for every candidate that should be captured, sorted for determinism.

    No filesystem access, no git — a candidate not being real, being a directory, or being a
    symlink is trusted entirely from the fields on `PathInfo`, which is what makes this function
    testable against hand-built, adversarial candidate lists rather than only against real trees.
    """
    return sorted(
        candidate.relative_path
        for candidate in candidates
        if candidate.is_file and not candidate.is_symlink and _is_allowlisted(candidate.relative_path)
    )


def _is_allowlisted(relative_path: str) -> bool:
    parts = relative_path.split("/")

    if len(parts) == 1:
        return parts[0] in BASELINE_TOP_LEVEL_FILES

    if parts[0] in BASELINE_DIRS and parts[-1].endswith(_BASELINE_CSS_SUFFIX):
        return True

    if parts[0] == "themes" and len(parts) == 3 and parts[-1] == _THEME_MANIFEST_FILENAME:
        return True

    return bool(parts[0] == "plugins" and len(parts) == 3 and parts[-1] in PLUGIN_CODE_FILES)
