"""The `replicate` subcommand: one run of local-replicator's five-step cycle.

Invoked by a launchd job on the operator's Mac (see docs/ for the plist and install
instructions) — the run is the unit of work, exactly like `commit`'s CronJob invocation. See
`obsidian_tools/local_replicator/cycle.py` for the cycle itself and the ordering rationale.
"""

from __future__ import annotations

import logging

from obsidian_tools.config import ReplicateConfig
from obsidian_tools.local_replicator.cycle import run_cycle
from obsidian_tools.local_replicator.rsync_ops import RsyncError
from obsidian_tools.vault_git.runner import GitCommandError

logger = logging.getLogger(__name__)

# A named tuple, not an inline `except GitCommandError, RsyncError:` — matches the convention
# already established by `commands/commit.py`'s `_STAGING_FAILURES`.
_CYCLE_FAILURES = (GitCommandError, RsyncError)


def run(config: ReplicateConfig) -> int:
    try:
        result = run_cycle(config)
    except _CYCLE_FAILURES:
        logger.exception("replication cycle failed", extra={"event": "cycle_failed"})
        return 1

    logger.info(
        "replication cycle complete",
        extra={
            "event": "cycle_complete",
            "drifted": len(result.drifted),
            "captured": len(result.captured),
            "capture_failed": len(result.capture_failed),
            "obsidian_seed_attempted": result.obsidian_seed_attempted,
            "tag_advanced": result.tag_advanced,
            "checkout": result.checkout,
        },
    )
    # Capture failures are retried next cycle by design (docs/DESIGN.md §2 item 10) — not a run
    # failure. A non-zero exit here is reserved for a cycle that couldn't complete at all.
    return 0
