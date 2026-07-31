"""`.obsidian/` on the device side: copy once, then hands off (docs/DESIGN.md §8a D3).

One rule covers bootstrap and steady state: if `.obsidian/` is absent at the destination, copy it;
if present, exclude it from that cycle's publish rsync entirely (`cycle.py` does the excluding —
this module only answers "has it been seeded" and performs the seed copy itself). A device that
already has `.obsidian/` keeps its own configuration indefinitely; a baseline change made later at
the cluster GUI does not reach it. The only reset path is deleting `.obsidian/` in the device's
iCloud vault directory by hand, which makes the presence check answer "no" again.

**Presence is decided by a completion marker, not by the directory existing.** If the first copy
dies partway through, `.obsidian/` would otherwise be left present-but-incomplete, and every later
run would skip it forever — a permanently half-configured vault that looks correct. Gating on a
marker written only after every file has copied means a partial attempt is retried wholesale on
the next cycle rather than mistaken for done; re-running the copy is idempotent (it only ever
touches files under `.obsidian/`), so retrying it costs nothing.

The marker lives inside `.obsidian/` itself, in iCloud — not in any local-replicator-side state on
the Mac — because presence is a fact about the shared iCloud vault, not about this one process's
own memory: Mac and iPhone open the *same* iCloud-synced `.obsidian/`, and local-replicator's own
state could be lost and rebuilt independently of whether the device copy was ever actually seeded.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

OBSIDIAN_DIR = ".obsidian"
BASELINE_MARKER = ".local-replicator-baseline-complete"

# Never copy per-instance workspace state into a device baseline — same two files the shared
# exclude list (exclude.py) protects on every ordinary cycle, named because Obsidian's own
# documentation calls them out as ones to ignore ("they update frequently based on current
# workspace state"). A fresh device generates its own from a blank slate.
_WORKSPACE_STATE_FILES = frozenset({"workspace.json", "workspaces.json"})


def is_baselined(icloud_vault_dir: Path) -> bool:
    return (icloud_vault_dir / OBSIDIAN_DIR / BASELINE_MARKER).exists()


def seed_baseline(cache_clone_dir: Path, icloud_vault_dir: Path) -> None:
    """Copy the frozen `.obsidian/` baseline from the parked clone into the iCloud vault, then
    write the completion marker. Idempotent and safe to re-run after a partial prior attempt."""
    source = cache_clone_dir / OBSIDIAN_DIR
    if not source.is_dir():
        # The committer hasn't taken its own .obsidian baseline commit yet (docs/DESIGN.md §8a D3)
        # — nothing to seed from. Not an error: the marker stays unwritten, and the next cycle
        # tries again once the clone has pulled a commit that includes it.
        logger.info(
            "no .obsidian/ in the parked clone yet; skipping device baseline seed this cycle",
            extra={"event": "device_baseline_skip_no_source"},
        )
        return

    destination = icloud_vault_dir / OBSIDIAN_DIR
    destination.mkdir(parents=True, exist_ok=True)
    copied = 0
    for item in source.rglob("*"):
        if item.is_dir():
            continue
        relative = item.relative_to(source)
        if relative.name in _WORKSPACE_STATE_FILES:
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        copied += 1

    (destination / BASELINE_MARKER).write_text("seeded by obsidian-tools local-replicator\n")
    logger.info("seeded device .obsidian/ baseline", extra={"event": "device_baseline_seeded", "file_count": copied})
