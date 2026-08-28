"""The exclude list shared by the drift comparison and the publish rsync.

Deliberately two separate constants, not one, because they answer different questions and the
design doc (docs/DESIGN.md §4 Plane B, "Constraints") keeps them distinct:

- `GIT_METADATA_EXCLUDE` is structural, not about spurious drift: git metadata must never enter
  the iCloud-synced folder at all (iCloud resolves conflicts by *renaming*, which applied to
  `.git/refs/heads/main` produces `main 2`, a ref git cannot parse). The cache clone is an
  ordinary checked-out working tree, so it physically contains `.git/` — this exclusion is what
  keeps it out of what actually gets published, and what keeps the overlay's `--delete` from
  removing it.
- `SHARED_EXCLUDE_LIST` is the "don't report/ship noise" list: `.DS_Store`, the two per-instance
  Obsidian workspace files (named by Obsidian's own documentation as ones to ignore, "because they
  update frequently based on current workspace state"), and iCloud's dataless-placeholder stubs.
  Without these, every cycle would report drift that isn't drift.
- `OBSIDIAN_BASELINE_EXCLUDE` is the settings baseline, and it is the one entry both rsync calls
  must agree on. `.obsidian/` is seeded onto a device once and never published again, so a cycle
  that observed it would report drift its own publish has structurally decided never to write back
  — the same unchanged bytes read as fresh drift every cycle, indefinitely, with no path to
  resolution short of an operator's manual copy (ppat/obsidian-tools#46). Divergence is still
  detected, by `device_baseline.diverged_baseline_paths`, which compares against the same allowlist
  the seed places rather than against everything the directory happens to contain.
"""

from __future__ import annotations

# No trailing slash, for the reason `OBSIDIAN_BASELINE_EXCLUDE` below drops its own and one
# consequence heavier. A directory-shaped filter rule is inert against a `.git` that is not a
# directory, and two shapes are: a symlink, and a plain *file* named `.git` — which is exactly what
# `git worktree add` and a submodule produce. Either one on the device side leaves the overlay's
# `--delete` free to remove the parked clone's real repository and put that entry in its place,
# silently, at exit 0 — after which every `GitRunner` invocation of the next cycle addresses an
# unrelated repository, or nothing (ppat/obsidian-tools#73). Nothing in this system creates either
# shape in the vault (`vault_git/runner.py` is built on there never being a `.git` inside the vault
# directory at all), so this is what the clone survives one arriving from outside it; the recovery
# is what makes that worth buying, since clearing the clone leaves the next cycle with no
# `LAST_CHECKOUT`, which skips the drift capture and then publishes with `--delete`. Verified
# against real rsync for both shapes, in both patterns
# (`test_overlay_holds_the_git_metadata_exclusion_against_non_directory_entries`).
GIT_METADATA_EXCLUDE = ".git"

SHARED_EXCLUDE_LIST: tuple[str, ...] = (
    ".DS_Store",
    # Unreachable rather than load-bearing while both rsync calls exclude `.obsidian` wholesale
    # (below). Kept because they name what Obsidian's own documentation asks every consumer to
    # ignore, and because they are the narrow rule that would still be right if the wholesale
    # exclusion ever came off — deleting them as dead would quietly make that reversal incomplete.
    # Pinned by a test that omits the wholesale exclusion, since every call site that applies it
    # would pass with these entries deleted
    # (`test_the_shared_lists_workspace_entries_suppress_them_without_the_wholesale_exclusion`).
    ".obsidian/workspace.json",
    ".obsidian/workspaces.json",
    # iCloud's dataless-placeholder naming convention for evicted file content (only produced when
    # "Optimize Mac Storage" is on, which the install docs require turning off — see docs/). Still
    # excluded defensively: rsync's `*` matches a leading dot, so this also protects against a stub
    # appearing transiently during a storage-optimization race.
    "*.icloud",
)

# No trailing slash, deliberately, and for the reason `vault_git/baseline.py`'s ignore rule drops
# its own: an rsync filter rule ending in `/` matches only a directory, so it is inert against a
# `.obsidian` that is a symlink, and equally against a plain *file* of that name. `--delete` then
# removes the parked baseline's real directory and puts the entry in its place, destroying the
# settings baseline the comparison and the re-seed both read; in the symlink case `git add -A`
# additionally stages a mode-120000 blob whose content is the target path — publishing a path
# outside the vault (ppat/obsidian-tools#22). The bare name matches `.obsidian` whatever kind of
# entry it is, and still covers the whole subtree beneath it when it is an ordinary directory; both
# halves verified against real rsync rather than read off the man page, and the first over both
# non-directory shapes rather than only the one that motivated it
# (`test_overlay_holds_the_obsidian_exclusion_against_non_directory_entries`,
# `test_overlay_leaves_the_baselines_obsidian_directory_untouched`).
#
# Its own constant rather than a fourth entry in `SHARED_EXCLUDE_LIST`, for the reason this module
# is split into separate constants at all: that list answers "what would be reported as drift
# without being drift", and `.obsidian/` is not noise — it is real content held deliberately outside
# this cycle's write path. It is therefore named at both call sites rather than inherited silently
# at one, which is what makes their agreement legible; each function's docstring points at the
# other, and both directions are pinned by their own tests, because a change that takes it off one
# side alone is precisely the defect that produced it.
OBSIDIAN_BASELINE_EXCLUDE = ".obsidian"


def rsync_exclude_args(*extra: str) -> list[str]:
    """Build `--exclude` arguments covering git metadata, the shared list, and any per-cycle extras
    (currently just `OBSIDIAN_BASELINE_EXCLUDE`, which both callers pass — see rsync_ops.py)."""
    args: list[str] = []
    for pattern in (GIT_METADATA_EXCLUDE, *SHARED_EXCLUDE_LIST, *extra):
        args += ["--exclude", pattern]
    return args
