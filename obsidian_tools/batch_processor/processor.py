"""One batch run, end to end. The impure shell: it owns the only `asyncio.run` in this package,
the clock, and the order the named steps happen in.

## The run, and why it is a run rather than a loop

A run disables the agent handle, drains the batch stream until it is empty, and re-enables the
handle. That shape is what makes ADR-0022's triggering policy expressible without any code of its
own: "at least once a day whenever non-empty, inside a permitted window" is a schedule, and "only
when the stream has work" falls out of an empty stream costing exactly one fetch before the run
exits. The one thing a schedule cannot express — that the handle must come back even if this
process does not — is the watchdog's job (`watchdog.py`), not this file's.

## Ordering, three places it is load-bearing

- **Both connections are opened before the handle goes down.** A broker or MCP outage then fails
  the run with agents still able to write, rather than blocking every interactive writer to
  discover the batch could not have run anyway.
- **The lease is renewed before each chunk, and while yielding.** Renewing after would let a long
  chunk look like a death; not renewing during a yield would let fairness itself trip the watchdog.
- **`end_run` is in a `finally`, and its own failure is logged rather than raised.** If the gateway
  is unreachable at the end of a run, the handle stays down — and that is precisely the state the
  watchdog exists to recover, so the correct behaviour here is to say so loudly and let the lease
  expire, never to retry into the exit path.

## What settles a chunk, and why the three verdicts are not interchangeable

| Outcome | Settled as | Why |
| --- | --- | --- |
| Applied | `ack` | done |
| Stale, raw-refused, undecodable, unapplicable, refused | dead-letter, then `term` | redelivery changes none of them |
| Broker or MCP unavailable | `nak` with backoff, to `max_deliver` | the next delivery may well succeed |

**A chunk that failed part-way through applying is dead-lettered, not retried**, and this is
ADR-0048's stated consequence rather than a shortcut: its redelivery would meet content its own
earlier writes moved, so the pre-flight would reject it anyway. Recovery is producer regeneration —
loud, and it destroys nothing.

## The crash-injection seams

`read_targets`, `apply_writes`, `settle`, `begin_run` and `end_run` are module-level functions
called by their own names, exactly as `local_replicator/cycle.py` calls its collaborators, so the
stateful harness can replace one with a stub that raises and model a process killed at that point.
That is the only reason they are not methods.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

from obsidian_tools.batch_processor.agent_handle import AgentHandleClient, AgentHandleError, build_handle_client
from obsidian_tools.batch_processor.consumer import BatchConsumer, DeliveredChunk
from obsidian_tools.batch_processor.fairness import (
    dead_letter_subject,
    decide_fairness,
    exhausted_redelivery,
)
from obsidian_tools.batch_processor.mcp_client import (
    McpClient,
    McpError,
    McpToolNames,
    McpUnavailableError,
)
from obsidian_tools.batch_processor.patching import PatchError, PlannedWrite, WriteKind, plan_writes
from obsidian_tools.batch_processor.preflight import assess_chunk, observed_state
from obsidian_tools.batch_processor.watchdog import lease_expiry
from obsidian_tools.batch_producer.chunk import Chunk, ChunkFormatError, chunk_id, decode_chunk
from obsidian_tools.config import BatchProcessorConfig
from obsidian_tools.retry import backoff_delay

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1

_REASON_UNDECODABLE = "undecodable_chunk"
_REASON_UNAPPLICABLE = "unapplicable_patch"
_REASON_REFUSED = "mcp_refused"
_REASON_PARTIALLY_APPLIED = "partially_applied"
_REASON_REDELIVERY_EXHAUSTED = "redelivery_exhausted"


def run(config: BatchProcessorConfig) -> int:
    return asyncio.run(_run(config))


# --- the named steps a crash can be injected between --------------------------------------------


async def begin_run(handle: AgentHandleClient, config: BatchProcessorConfig) -> None:
    await asyncio.to_thread(handle.begin_batch_run, lease_expiry(datetime.now(tz=UTC), config.lease_ttl_seconds))


async def renew_run(handle: AgentHandleClient, config: BatchProcessorConfig) -> None:
    await asyncio.to_thread(handle.renew_lease, lease_expiry(datetime.now(tz=UTC), config.lease_ttl_seconds))


async def end_run(handle: AgentHandleClient) -> None:
    await asyncio.to_thread(handle.end_batch_run)


async def read_targets(mcp: McpClient, chunk: Chunk) -> dict[str, str | None]:
    """Every target's current content, read back through the one door this component has.

    Read in the chunk's own target order and all of them before anything is written — the
    whole-chunk, before-any-write pre-flight ADR-0048 specifies is only true if this returns
    before `apply_writes` begins.
    """
    contents: dict[str, str | None] = {}
    for target in chunk.targets:
        contents[target.path] = await asyncio.to_thread(mcp.read_note, target.path)
    return contents


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    """How far the chunk got. `applied` is what decides whether it may be retried at all."""

    applied: int
    failure: McpError | None


async def apply_writes(mcp: McpClient, writes: tuple[PlannedWrite, ...]) -> WriteOutcome:
    """Perform the chunk's writes in order, stopping at the first that fails.

    Returns rather than raises, because *how far it got* is the deciding fact and an exception
    carries only what went wrong. A chunk that failed on its first write has changed nothing and
    can be redelivered; one that failed later is genuinely half-applied, and its redelivery would
    meet content its own earlier writes moved.
    """
    applied = 0
    for write in writes:
        try:
            if write.kind is WriteKind.DELETE:
                await asyncio.to_thread(mcp.delete_note, write.path)
            else:
                await asyncio.to_thread(mcp.write_note, write.path, write.content or "")
        except McpError as exc:
            return WriteOutcome(applied, exc)
        applied += 1
    return WriteOutcome(applied, None)


async def settle(consumer: BatchConsumer, delivered: DeliveredChunk, *, acked: bool, delay_seconds: float) -> None:
    if acked:
        await consumer.ack(delivered)
    else:
        await consumer.nak(delivered, delay_seconds)


# --- the run ------------------------------------------------------------------------------------


async def _run(config: BatchProcessorConfig) -> int:
    mcp = McpClient(
        base_url=config.mcp_url,
        api_key=config.mcp_api_key,
        tools=McpToolNames(
            read=config.mcp_tool_read,
            write=config.mcp_tool_write,
            delete=config.mcp_tool_delete,
            path_argument=config.mcp_path_argument,
            content_argument=config.mcp_content_argument,
        ),
        timeout_seconds=config.mcp_timeout_seconds,
        verify_tls=config.mcp_verify_tls,
        retries=config.mcp_retries,
        retry_base_delay_seconds=config.backoff_base_delay_seconds,
    )
    handle = build_handle_client(config.agent_handle)
    consumer = BatchConsumer(
        servers=config.nats_url,
        user=config.nats_user,
        password=config.nats_password,
        inbox_prefix=config.nats_inbox_prefix,
        stream=config.stream,
        durable=config.durable,
        filter_subject=f"{config.subject_prefix}.>",
        max_deliver=config.max_deliver,
        ack_wait_seconds=config.ack_wait_seconds,
        connect_timeout_seconds=config.connect_timeout_seconds,
        promotion_stream=config.promotion_stream,
        promotion_consumer=config.promotion_consumer,
    )

    # Both connections first: see "Ordering" in the module docstring.
    await asyncio.to_thread(mcp.connect)
    async with consumer:
        await begin_run(handle, config)
        try:
            counts = await _drain(config, consumer, mcp, handle)
        finally:
            try:
                await end_run(handle)
            except AgentHandleError:
                logger.exception(
                    "could not re-enable the agent handle; the watchdog's lease is now the only thing that will",
                    extra={"event": "agent_handle_stuck"},
                )
    logger.info("batch run complete", extra={"event": "batch_run_complete", **counts})
    return EXIT_FAILED if counts["dead_lettered"] else EXIT_OK


async def _drain(
    config: BatchProcessorConfig, consumer: BatchConsumer, mcp: McpClient, handle: AgentHandleClient
) -> Counter[str]:
    counts: Counter[str] = Counter({"applied": 0, "rejected": 0, "dead_lettered": 0, "yields": 0})
    consecutive_yields = 0

    while True:
        depth = await consumer.promotion_depth()
        if depth is None and consecutive_yields == 0 and counts["applied"] == 0:
            logger.warning(
                "no promotion stream is configured, so this run does not yield to interactive work",
                extra={"event": "fairness_unconfigured"},
            )
        if depth is not None:
            decision = decide_fairness(
                depth,
                threshold=config.promotion_depth_threshold,
                consecutive_yields=consecutive_yields,
                base_delay=config.backoff_base_delay_seconds,
                max_delay=config.backoff_max_delay_seconds,
                jitter_fraction=config.backoff_jitter_fraction,
                random_value=random.random(),
            )
            if decision.should_yield:
                consecutive_yields += 1
                counts["yields"] += 1
                if consecutive_yields > config.max_consecutive_yields:
                    # Not ADR-0022's maximum run duration (ot#89): this bounds one specific wait,
                    # for one specific condition that is not clearing. Unbounded, a promotion
                    # stream that never drains — because its own processor is down — would hold the
                    # agent handle disabled indefinitely, which is the outage the watchdog exists
                    # to end, arrived at by this component's own patience.
                    logger.warning(
                        "ending the run: the promotion stream has not drained",
                        extra={"event": "fairness_gave_up", "promotion_depth": depth, "yields": consecutive_yields},
                    )
                    return counts
                logger.info(
                    "yielding to the promotion stream",
                    extra={
                        "event": "backpressure_yield",
                        "promotion_depth": depth,
                        "delay_seconds": round(decision.delay_seconds, 3),
                        "consecutive_yields": consecutive_yields,
                    },
                )
                await renew_run(handle, config)
                await asyncio.sleep(decision.delay_seconds)
                continue

        consecutive_yields = 0
        delivered = await consumer.next_chunk(config.idle_timeout_seconds)
        if delivered is None:
            return counts
        await renew_run(handle, config)
        await _process(config, consumer, mcp, delivered, counts)


async def _process(
    config: BatchProcessorConfig,
    consumer: BatchConsumer,
    mcp: McpClient,
    delivered: DeliveredChunk,
    counts: Counter[str],
) -> None:
    try:
        chunk = decode_chunk(delivered.body)
    except ChunkFormatError as exc:
        await _dead_letter(config, consumer, delivered, _REASON_UNDECODABLE, str(exc), counts)
        return

    identity = chunk_id(chunk)
    try:
        contents = await read_targets(mcp, chunk)
    except McpUnavailableError as exc:
        await _retry_later(config, consumer, delivered, str(exc), counts)
        return
    except McpError as exc:
        await _dead_letter(config, consumer, delivered, _REASON_REFUSED, f"reading targets: {exc}", counts)
        return

    verdict = assess_chunk(
        chunk,
        {path: observed_state(content) for path, content in contents.items()},
        raw_layer_prefix=config.raw_layer_prefix,
    )
    if not verdict.accepted:
        counts["rejected"] += 1
        for reason in verdict.reasons:
            counts[f"rejected_{reason}"] += 1
        logger.warning(
            "chunk rejected with nothing applied",
            extra={
                "event": "chunk_rejected",
                "chunk_id": identity,
                "reasons": [str(reason) for reason in verdict.reasons],
                "detail": verdict.summary(),
            },
        )
        await _dead_letter(config, consumer, delivered, str(verdict.reasons[0]), verdict.summary(), counts)
        return

    try:
        writes = plan_writes(chunk, contents)
    except PatchError as exc:
        await _dead_letter(config, consumer, delivered, _REASON_UNAPPLICABLE, str(exc), counts)
        return

    outcome = await apply_writes(mcp, writes)
    if outcome.failure is not None:
        if outcome.applied == 0 and isinstance(outcome.failure, McpUnavailableError):
            # Nothing landed, and nothing decided anything — an MCP rollout mid-run looks exactly
            # like this, and dead-lettering it would park work with nothing wrong with it. A write
            # that timed out may in fact have landed; the redelivery then rejects as stale, which is
            # loud and destroys nothing.
            await _retry_later(config, consumer, delivered, str(outcome.failure), counts)
            return
        # Whatever this chunk had already written stays written; ADR-0048's pre-flight is what makes
        # that safe to leave, because a redelivery would meet it and reject rather than double-apply.
        reason = _REASON_PARTIALLY_APPLIED if outcome.applied else _REASON_REFUSED
        await _dead_letter(config, consumer, delivered, reason, str(outcome.failure), counts)
        return

    await settle(consumer, delivered, acked=True, delay_seconds=0.0)
    counts["applied"] += 1
    logger.info(
        "chunk applied",
        extra={
            "event": "chunk_applied",
            "chunk_id": identity,
            "stream_sequence": delivered.stream_sequence,
            "write_count": len(writes),
        },
    )


async def _retry_later(
    config: BatchProcessorConfig,
    consumer: BatchConsumer,
    delivered: DeliveredChunk,
    detail: str,
    counts: Counter[str],
) -> None:
    if exhausted_redelivery(delivered.delivery_count, max_deliver=config.max_deliver):
        await _dead_letter(config, consumer, delivered, _REASON_REDELIVERY_EXHAUSTED, detail, counts)
        return
    delay = backoff_delay(
        delivered.delivery_count,
        base_delay=config.backoff_base_delay_seconds,
        max_delay=config.backoff_max_delay_seconds,
        jitter_fraction=config.backoff_jitter_fraction,
    )
    logger.warning(
        "returning the chunk for redelivery",
        extra={
            "event": "chunk_redelivery",
            "stream_sequence": delivered.stream_sequence,
            "delivery_count": delivered.delivery_count,
            "delay_seconds": round(delay, 3),
            "detail": detail,
        },
    )
    await settle(consumer, delivered, acked=False, delay_seconds=delay)


async def _dead_letter(
    config: BatchProcessorConfig,
    consumer: BatchConsumer,
    delivered: DeliveredChunk,
    reason: str,
    detail: str,
    counts: Counter[str],
) -> None:
    """Republish, then terminate — in that order, and never the other way round.

    Terminating first would settle the message permanently before its copy existed, so a failure to
    publish would erase the chunk instead of parking it. `dead_letter` raising here therefore leaves
    the message unsettled, and the broker redelivers it: work stalls loudly rather than vanishing.
    """
    subject = dead_letter_subject(config.dead_letter_subject_prefix, _batch_id_of(delivered))
    await consumer.dead_letter(subject, delivered, reason, detail)
    await consumer.term(delivered)
    counts["dead_lettered"] += 1
    counts[f"dead_lettered_{reason}"] += 1


def _batch_id_of(delivered: DeliveredChunk) -> str:
    """The batch id from the message's own subject, which is where it also lives (ADR-0047).

    Taken from the subject rather than the payload on purpose: an undecodable chunk still has to be
    dead-lettered somewhere, and the subject is the one part of it the broker guarantees is
    well-formed.
    """
    _, _, trailing = delivered.subject.rpartition(".")
    return trailing or "unknown"
