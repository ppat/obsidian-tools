"""One batch run, end to end: read the index, chunk it, enqueue every chunk in order.

The impure shell. It owns the only `asyncio.run` in the package, the batch id, and the clock —
everything it decides with is a pure function over data it gathered first.

**Chunks are published one at a time, each acknowledged before the next is sent.** ADR-0022 chose
one FIFO stream so a producer can express dependency by ordering (rename in chunk N, relink in
chunk N+1); the stream preserves the order it *receives*, so serialising the publishes is what makes
the producer's order and the stream's order the same order. Publishing concurrently would let two
chunks be sequenced by whichever acknowledgement raced, which is not a throughput optimisation with
a small ordering cost — it silently discards the guarantee the single stream was chosen to provide.

**A failed publish stops the batch where it is.** The alternative — skip and continue — would leave
a gap in a sequence whose whole meaning is that later chunks may depend on earlier ones. Stopping
leaves a batch cut short, which ADR-0048 characterises as an edit not yet made rather than a wrong
one, and the chunk that failed is still on the producer to regenerate. SIGTERM lands the same way,
through `cli.py`'s handler, and needs nothing of its own here.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from obsidian_tools.batch_producer.chunk import Chunk, chunk_id, chunk_subject, encode_chunk, validate_batch
from obsidian_tools.batch_producer.chunking import build_chunks
from obsidian_tools.batch_producer.generation import collect_patch_units
from obsidian_tools.batch_producer.nats_client import BatchPublisher, BatchPublisherError
from obsidian_tools.config import BatchProducerConfig
from obsidian_tools.vault_git.runner import GitRunner

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1


class BatchValidationError(RuntimeError):
    """The chunks this producer built are not a well-formed batch. Raised before anything is
    published, because a batch that violates ADR-0048's one-chunk-per-path invariant would reject
    itself downstream and the producer is the only place that can still see why."""


def new_batch_id() -> str:
    """A batch id is also a NATS subject token (`chunk_subject`), so it is generated in the alphabet
    that is legal there — `uuid4().hex` is already lowercase hex and needs no escaping."""
    return uuid4().hex


def run(config: BatchProducerConfig) -> int:
    runner = GitRunner(Path(config.git_dir), Path(config.work_tree))
    units = collect_patch_units(runner, base_rev=config.base_rev)
    if not units:
        logger.info("nothing is staged; no batch to enqueue", extra={"event": "batch_empty"})
        return EXIT_OK

    batch_id = new_batch_id()
    chunks = build_chunks(
        units,
        batch_id=batch_id,
        produced_at=datetime.now(tz=UTC).isoformat(),
        max_patch_bytes=config.max_chunk_patch_bytes,
    )
    problems = validate_batch(chunks)
    if problems:
        raise BatchValidationError(f"refusing to enqueue a malformed batch: {'; '.join(problems)}")

    logger.info(
        "enqueueing batch",
        extra={
            "event": "batch_starting",
            "batch_id": batch_id,
            "chunk_count": len(chunks),
            "target_count": sum(len(chunk.targets) for chunk in chunks),
        },
    )
    return asyncio.run(_publish_all(config, chunks))


async def _publish_all(config: BatchProducerConfig, chunks: tuple[Chunk, ...]) -> int:
    publisher = BatchPublisher(
        servers=config.nats_url,
        user=config.nats_user,
        password=config.nats_password,
        inbox_prefix=config.nats_inbox_prefix,
        connect_timeout_seconds=config.connect_timeout_seconds,
        publish_timeout_seconds=config.publish_timeout_seconds,
        max_reconnect_attempts=config.max_reconnect_attempts,
    )
    published = 0
    async with publisher:
        for chunk in chunks:
            subject = chunk_subject(config.subject_prefix, chunk.batch_id)
            try:
                sequence = await publisher.publish_chunk(subject, encode_chunk(chunk))
            except BatchPublisherError:
                logger.exception(
                    "stopping the batch at the chunk that could not be enqueued",
                    extra={
                        "event": "chunk_publish_failed",
                        "chunk_id": chunk_id(chunk),
                        "subject": subject,
                        "chunks_published": published,
                        "chunk_count": len(chunks),
                    },
                )
                return EXIT_FAILED
            published += 1
            logger.info(
                "chunk enqueued",
                extra={
                    "event": "chunk_published",
                    "chunk_id": chunk_id(chunk),
                    "subject": subject,
                    "stream_sequence": sequence,
                    "target_count": len(chunk.targets),
                },
            )

    logger.info(
        "batch enqueued",
        extra={"event": "batch_complete", "batch_id": chunks[0].batch_id, "chunks_published": published},
    )
    return EXIT_OK
