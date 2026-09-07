"""The `watch-agent-instance` subcommand: unit D4's non-deferrable half, the batch-mode watchdog.

One pass per invocation, on a schedule of its own, in its own process — a watchdog that shared a
process with `batch-processor` would die with it, which is the failure it exists to catch. It reads
the agent MCP instance's desired replica count and the batch lease beside it, and starts the
instance again when the batch run holding it stopped has stopped saying it is alive. Nothing else:
the maximum run duration and the pre-stop drain are ot#89.

See `batch_processor/watchdog.py` for why the liveness signal is a lease deadline rather than a
flag, why a stopped instance holding no lease is left exactly as found, and why the read is of
desired replicas rather than available ones.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from obsidian_tools.batch_processor.agent_instance import (
    AgentInstanceError,
    build_instance_client,
    run_watchdog_once,
)
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
        run_watchdog_once(build_instance_client(config.agent_instance), datetime.now(tz=UTC))
    except AgentInstanceError:
        # A watchdog that cannot read the instance has learned nothing, and must not guess: an
        # unreachable API server is not evidence that a run has died, and starting the instance on
        # that basis would open interactive writes in the middle of a live batch run.
        logger.exception("the watchdog could not read the agent MCP instance", extra={"event": "watchdog_failed"})
        return EXIT_FAILED
    return EXIT_OK
