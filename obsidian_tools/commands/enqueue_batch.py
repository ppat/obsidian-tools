"""The `enqueue-batch` subcommand: the batch stream's producer (unit B1, ot#125).

Runs in the Coder workspace, not in-cluster, against the workspace's own git checkout — see
`obsidian_tools/batch_producer/` for the message format and the ordering guarantee, and ADR-0047 for
why this is the only credential permitted to enqueue patch-carrying work.
"""

from __future__ import annotations

import logging

from obsidian_tools.batch_producer.chunking import ChunkTooLargeError
from obsidian_tools.batch_producer.generation import PatchEncodingError
from obsidian_tools.batch_producer.nats_client import BatchPublisherError
from obsidian_tools.batch_producer.producer import EXIT_FAILED, BatchValidationError
from obsidian_tools.batch_producer.producer import run as run_batch
from obsidian_tools.batch_producer.staleness import ChunkTargetError, UnsupportedPatchStatusError
from obsidian_tools.config import BatchProducerConfig
from obsidian_tools.vault_git.runner import GitCommandError

logger = logging.getLogger(__name__)

# Every way a batch can be refused before or during enqueueing, each of which already names the file
# or the setting responsible. Caught as one group and logged once: they differ in what an operator
# fixes, not in what this function does about them, and the distinction survives in the message.
_BATCH_REFUSALS = (
    BatchPublisherError,
    BatchValidationError,
    ChunkTargetError,
    ChunkTooLargeError,
    GitCommandError,
    PatchEncodingError,
    UnsupportedPatchStatusError,
)


def run(config: BatchProducerConfig) -> int:
    try:
        return run_batch(config)
    except _BATCH_REFUSALS:
        logger.exception("batch not enqueued", extra={"event": "batch_failed"})
        return EXIT_FAILED
