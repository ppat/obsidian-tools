"""The `drain` subcommand: one run of the spool drainer (docs/DESIGN.md §2 item 10, §7 Phase 2).

Decoupled from `replicate`'s own cycle, invoked on its own schedule — see
`obsidian_tools.local_replicator.drainer`. Phase 2's sink discards everything it drains; Phase 5
(ppat/obsidian-tools#4) is the only thing that changes here, by swapping the sink.
"""

from __future__ import annotations

import logging
from pathlib import Path

from obsidian_tools.config import DrainConfig
from obsidian_tools.local_replicator.drainer import drain_once

logger = logging.getLogger(__name__)


def run(config: DrainConfig) -> int:
    drained = drain_once(Path(config.spool_dir))
    logger.info("drain complete", extra={"event": "drain_complete", "drained": len(drained)})
    return 0
