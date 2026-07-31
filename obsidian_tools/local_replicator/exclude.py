"""The exclude list shared by the drift comparison and the publish rsync.

Deliberately two separate constants, not one, because they answer different questions and the
design doc (docs/DESIGN.md §4 Plane B, "Constraints") keeps them distinct:

- `GIT_METADATA_EXCLUDE` is structural, not about spurious drift: git metadata must never enter
  the iCloud-synced folder at all (iCloud resolves conflicts by *renaming*, which applied to
  `.git/refs/heads/main` produces `main 2`, a ref git cannot parse). The cache clone is an
  ordinary checked-out working tree, so it physically contains `.git/` — this exclusion is what
  keeps it out of what actually gets published.
- `SHARED_EXCLUDE_LIST` is the "don't report/ship noise" list: `.DS_Store`, the two per-instance
  Obsidian workspace files (named by Obsidian's own documentation as ones to ignore, "because they
  update frequently based on current workspace state"), and iCloud's dataless-placeholder stubs.
  Without these, every cycle would report drift that isn't drift.
"""

from __future__ import annotations

GIT_METADATA_EXCLUDE = ".git/"

SHARED_EXCLUDE_LIST: tuple[str, ...] = (
    ".DS_Store",
    ".obsidian/workspace.json",
    ".obsidian/workspaces.json",
    # iCloud's dataless-placeholder naming convention for evicted file content (only produced when
    # "Optimize Mac Storage" is on, which the install docs require turning off — see docs/). Still
    # excluded defensively: rsync's `*` matches a leading dot, so this also protects against a stub
    # appearing transiently during a storage-optimization race.
    "*.icloud",
)


def rsync_exclude_args(*extra: str) -> list[str]:
    """Build `--exclude` arguments covering git metadata, the shared list, and any per-cycle extras
    (e.g. a path whose capture failed this cycle, or `.obsidian/` once already seeded)."""
    args: list[str] = []
    for pattern in (GIT_METADATA_EXCLUDE, *SHARED_EXCLUDE_LIST, *extra):
        args += ["--exclude", pattern]
    return args
