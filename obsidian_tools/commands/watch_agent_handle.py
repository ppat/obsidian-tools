"""The `watch-agent-handle` subcommand: unit D4's non-deferrable half, the batch-mode watchdog.

One pass per invocation, on a schedule of its own, in its own process — a watchdog that shared a
process with `batch-processor` would die with it, which is the failure it exists to catch. It reads
the agent handle, and re-enables it when the batch run holding it down has stopped saying it is
alive. Nothing else: the maximum run duration and the post-disable drain are ot#89.

See `batch_processor/watchdog.py` for why the liveness signal is a lease deadline rather than a
flag, and why an unleased disabled handle is left exactly as found.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from obsidian_tools.batch_processor.agent_handle import AgentHandleError, build_handle_client, run_watchdog_once
from obsidian_tools.config import WatchdogConfig

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1


def run(config: WatchdogConfig) -> int:
    """Exit zero for every verdict, including the ones that report a problem.

    A watchdog's exit code answers "did the check run", never "was everything healthy" — a
    scheduled pass that exits non-zero because it found and fixed the very condition it exists for
    would present as a broken watchdog. What it found is in its log line.
    """
    try:
        run_watchdog_once(build_handle_client(config.agent_handle), datetime.now(tz=UTC))
    except AgentHandleError:
        # A watchdog that cannot read the handle has learned nothing, and must not guess: an
        # unreachable gateway is not evidence that a run has died, and re-enabling on that basis
        # would open agent writes in the middle of a live batch run.
        logger.exception("the watchdog could not read the agent handle", extra={"event": "watchdog_failed"})
        return EXIT_FAILED
    return EXIT_OK
