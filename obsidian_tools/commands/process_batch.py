"""The `process-batch` subcommand: `batch-processor` (unit A2, ot#5).

Runs in-cluster. One invocation is one batch run — the agent MCP instance is stopped, the batch
stream is drained in strict FIFO, the instance is started again. See `obsidian_tools/batch_processor/`
for the five obligations that shape it, and `chunk.py` for the wire contract it consumes.
"""

from __future__ import annotations

import logging

from obsidian_tools.batch_processor.agent_instance import AgentInstanceError
from obsidian_tools.batch_processor.consumer import BatchConsumerError
from obsidian_tools.batch_processor.mcp_client import McpError
from obsidian_tools.batch_processor.processor import EXIT_FAILED
from obsidian_tools.batch_processor.processor import run as run_processor
from obsidian_tools.config import BatchProcessorConfig

logger = logging.getLogger(__name__)

# Every way a run can fail before or while draining. Caught as one group and logged once: they
# differ in what an operator fixes, not in what this function does about them, and the distinction
# survives in the message. A failure here leaves the agent MCP instance to the watchdog's lease,
# which is the whole reason the watchdog is a separate process.
_RUN_FAILURES = (AgentInstanceError, BatchConsumerError, McpError)


def run(config: BatchProcessorConfig) -> int:
    try:
        return run_processor(config)
    except _RUN_FAILURES:
        logger.exception("batch run failed", extra={"event": "batch_run_failed"})
        return EXIT_FAILED
