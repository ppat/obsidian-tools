"""One batch run, end to end. The impure shell: it owns the only `asyncio.run` in this package,
the clock, and the order the named steps happen in.

## The run, and why it is a run rather than a loop

A run stops the agent MCP instance, drains the batch stream until it is empty, and starts the
instance again. That shape is what makes ADR-0022's triggering policy expressible without any code
of its own: "at least once a day whenever non-empty, inside a permitted window" is a schedule, and
"only when the stream has work" falls out of an empty stream costing exactly one fetch before the
run exits. The one thing a schedule cannot express — that the instance must come back even if this
process does not — is the watchdog's job (`watchdog.py`), not this file's.

## Ordering, four places it is load-bearing

- **Both connections are opened before the instance goes down.** A broker or MCP outage then fails
  the run with agents still able to write, rather than blocking every interactive writer to
  discover the batch could not have run anyway.
- **Inside `begin_run` the lease is taken before the stop, and inside `end_run` the instance is
  started before the lease is released.** Both live in `agent_instance.py`, in one place rather than
  restated here, because that pair is the whole reason "stopped with no lease" stays an operator's
  own hold. A crash between either pair leaves the instance *running*.
- **The lease is renewed before each chunk, and while yielding.** Renewing after would let a long
  chunk look like a death; not renewing during a yield would let fairness itself trip the watchdog.
- **`end_run` is in a `finally`, and its own failure is logged rather than raised.** If the API
  server is unreachable at the end of a run, the instance stays stopped — and that is precisely the
  state the watchdog exists to recover, so the correct behaviour here is to say so loudly and let
  the lease expire, never to retry into the exit path.

## What settles a chunk, and why the four verdicts are not interchangeable

| Outcome | Settled as | Why |
| --- | --- | --- |
| Applied | `ack` | done |
| Already applied | `ack` | done, by an earlier delivery or an earlier run |
| Stale, raw-refused, undecodable, unapplicable, refused | dead-letter, then `term` | redelivery changes none of them |
| Dependent on a chunk that failed | dead-letter, then `term` | the work it needs is undone |
| Broker or MCP unavailable | `nak` with backoff, to `max_deliver` | the next delivery may well succeed |

**A chunk that failed part-way through applying is dead-lettered, not retried**, and this is
ADR-0048's stated consequence rather than a shortcut: its redelivery would meet content its own
earlier writes moved, so the pre-flight would reject it anyway. Recovery is producer regeneration —
loud, and it destroys nothing.

## A chunk whose work is already done is acked, and that is what makes a re-run converge

The pre-flight refuses a create whose target exists, and that rule is unchanged: it is what stops a
re-import overwriting an earlier one. But "the target holds exactly what this patch would write" and
"the target holds something else" are different facts, and only the second is a conflict. So before
a chunk is refused for any reason at all, one further question is asked — does every path it touches
already hold what applying it would leave there (`patching.already_applied`)? If so it is acked,
counted separately, and blocks nothing, because acking it leaves the vault in the same state
applying it would.

Without this, recovery is impossible rather than merely manual. A producer re-run republishes the
whole batch; the chunks already applied are refused as duplicates; each refusal blocks the paths it
would have created; and the run applies nothing at all — measured, three cycles deep, on the same
batch.

**The question beats *both* refusals, including the dependency one, and that ordering is load
bearing.** Asked only after the dependency check, a chunk whose work is entirely done is parked
without ever being asked whether it needs doing — measured against a vault already holding the whole
import, where not one of thirty fully-applied chunks was recognised, because one genuine conflict
early in the batch parked all of them. A settled chunk performs no write, so it cannot leave a link
pointing at nothing, and parking it withholds only work the vault already has. The price is that the
targets must be read before anything is decided, so a chunk that ends up parked now costs one read
per target; buying the answer is what those reads are for, and reads are what this component was
always going to spend on a chunk it applies.

## What a failed chunk blocks, and for how long

A dead-lettered chunk's `create` paths are remembered for the rest of the run, keyed by batch, and a
later chunk of that same batch whose patch links to one of them is parked rather than applied —
otherwise the relink that ADR-0022's ordering exists to sequence lands without the rename it was
sequenced behind, and a note is left pointing at a page nobody created. `dependency.py` decides;
this file owns the state, which is the run's alone: chunks of every other batch are untouched, and
the next run starts with nothing blocked because by then the producer has regenerated the batch or
has not.

**Why park at all, given what it costs.** Because the alternative is silently wrong content in
exactly the layer a bulk import mostly lands in. Applying a dependent writes a note whose link
points at a page this run has just failed to create, and `05-raw/` is write-once *and
validation-exempt* by design (ADR-0015) — the lint pass reporting nothing about the raw layer is it
working correctly, not failing. So "apply it and let the lint pass find the broken link" is false
precisely where most of the import goes. Parking turns an unreportable wrong page into an absent
one: countable, on the dead-letter stream, and the run exits non-zero.

**The blocking is one hop deep, deliberately.** A chunk parked *as a dependent* does not add its own
creates to the blocked set. Chained, the rule parks the whole tail of a link-dense batch: measured,
one refused write in chunk 5 of 36 parked 31 chunks and left 1,287 of 1,500 notes unwritten, where
the same corpus without wikilinks parked one. It compounds because the reference test is deliberately
an over-approximation (`dependency.py` matches case-insensitively, on stems, across the whole patch)
— sound applied once, and close to "park everything after the first failure" applied thirty times.
The second hop is where the argument for parking runs out: a chunk two hops down links to pages a
*parked* chunk would create, and parked work is regenerated by the producer and applied by the next
cycle, so that dangle closes on its own — whereas parking it costs a chunk's worth of notes on every
cycle until it does. One hop is still expensive, measured: at 45% link density one refused write
parks 24 of the 30 chunks behind it. That is the price of the mechanism, not a defect in it.

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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from obsidian_tools.batch_processor.agent_instance import (
    AgentInstanceClient,
    AgentInstanceError,
    build_instance_client,
)
from obsidian_tools.batch_processor.consumer import BatchConsumer, BatchConsumerError, DeliveredChunk
from obsidian_tools.batch_processor.dependency import created_paths, referenced_blocked_paths
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
from obsidian_tools.batch_processor.patching import PatchError, PlannedWrite, WriteKind, already_applied, plan_writes
from obsidian_tools.batch_processor.preflight import assess_chunk, observed_state
from obsidian_tools.batch_producer.chunk import Chunk, ChunkFormatError, chunk_id, decode_chunk
from obsidian_tools.config import BatchProcessorConfig
from obsidian_tools.logging_config import LOG_PATH_SAMPLE_LIMIT
from obsidian_tools.retry import backoff_delay

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1

_REASON_UNDECODABLE = "undecodable_chunk"
_REASON_UNAPPLICABLE = "unapplicable_patch"
_REASON_REFUSED = "mcp_refused"
_REASON_PARTIALLY_APPLIED = "partially_applied"
_REASON_REDELIVERY_EXHAUSTED = "redelivery_exhausted"
_REASON_DEPENDS_ON_FAILED = "depends_on_failed_chunk"


def run(config: BatchProcessorConfig) -> int:
    return asyncio.run(_run(config))


# --- the named steps a crash can be injected between --------------------------------------------


async def begin_run(instance: AgentInstanceClient, config: BatchProcessorConfig) -> None:
    await asyncio.to_thread(instance.begin_batch_run, datetime.now(tz=UTC), config.lease_ttl_seconds)


async def renew_run(instance: AgentInstanceClient, config: BatchProcessorConfig) -> None:
    await asyncio.to_thread(instance.renew_lease, datetime.now(tz=UTC))


async def end_run(instance: AgentInstanceClient) -> None:
    await asyncio.to_thread(instance.end_batch_run)


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
    instance = build_instance_client(config.agent_instance)
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
        await begin_run(instance, config)
        try:
            counts = await _drain(config, consumer, mcp, instance)
        finally:
            try:
                await end_run(instance)
            except AgentInstanceError:
                logger.exception(
                    "could not start the agent MCP instance; the watchdog's lease is now the only thing that will",
                    extra={"event": "agent_instance_stuck"},
                )
    logger.info("batch run complete", extra={"event": "batch_run_complete", **counts})
    return EXIT_FAILED if counts["dead_lettered"] else EXIT_OK


async def _drain(
    config: BatchProcessorConfig, consumer: BatchConsumer, mcp: McpClient, instance: AgentInstanceClient
) -> Counter[str]:
    counts: Counter[str] = Counter({"applied": 0, "already_applied": 0, "rejected": 0, "dead_lettered": 0, "yields": 0})
    consecutive_yields = 0
    # Batch id to the paths a dead-lettered chunk of that batch would have created; see "What a
    # failed chunk blocks" in the module docstring. Keyed by the subject's batch token rather than
    # the payload's, so that a chunk too malformed to decode is still filed under the same batch.
    blocked: dict[str, set[str]] = {}

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
                    # agent instance stopped indefinitely, which is the outage the watchdog exists
                    # to end, arrived at by this component's own patience.
                    logger.warning(
                        "ending the run: the promotion stream has not drained",
                        extra={"event": "fairness_gave_up", "promotion_depth": depth, "yields": consecutive_yields},
                    )
                    await _record_what_is_left(consumer, counts)
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
                await renew_run(instance, config)
                await asyncio.sleep(decision.delay_seconds)
                continue

        consecutive_yields = 0
        delivered = await consumer.next_chunk(config.idle_timeout_seconds)
        if delivered is None:
            await _record_what_is_left(consumer, counts)
            return counts
        await renew_run(instance, config)
        await _process(config, consumer, mcp, delivered, counts, blocked)


async def _record_what_is_left(consumer: BatchConsumer, counts: Counter[str]) -> None:
    """Say how much of the stream this run did not take, because an empty fetch does not mean empty.

    An idle fetch is the run's completion signal, and it fires for two states that are otherwise
    indistinguishable in the output: the stream is drained, or the broker is withholding everything
    behind a chunk another run left ack-pending. The second produces a log identical to a healthy
    nightly run against an empty queue — five INFO lines, every count zero, exit 0 — while a whole
    import sits queued, which is the "a green that means it did not run" failure this repository's
    testing discipline refuses. `left_pending` on `batch_run_complete` separates them, and it is
    absent only when the warning below says in so many words that it could not be read.

    Not an exit code and not an alert: chunks published *during* the run leave work pending too, and
    that is an ordinary Tuesday rather than a fault (O3 — no alert rules before AI triage). Being
    unable to answer is itself reported rather than either swallowed or raised: the drain is over
    and every chunk is settled, so failing a completed run over a diagnostic would trade work done
    for a number.
    """
    try:
        pending = await consumer.pending()
    except BatchConsumerError:
        logger.warning(
            "could not read how much of the batch stream is left, so this run cannot say whether it drained it",
            extra={"event": "batch_pending_unknown"},
        )
        return
    counts["left_pending"] = pending
    if pending:
        logger.warning(
            "the run ended with chunks still queued: nothing was deliverable before the idle timeout",
            extra={"event": "batch_stream_not_drained", "left_pending": pending},
        )


async def _process(
    config: BatchProcessorConfig,
    consumer: BatchConsumer,
    mcp: McpClient,
    delivered: DeliveredChunk,
    counts: Counter[str],
    blocked: dict[str, set[str]],
) -> None:
    try:
        chunk = decode_chunk(delivered.body)
    except ChunkFormatError as exc:
        # No chunk, so no target list, so nothing this batch can be known to owe a later chunk. The
        # producer and this consumer ship in one image, so an undecodable body is a corrupted
        # message rather than a version skew.
        await _dead_letter(config, consumer, delivered, None, _REASON_UNDECODABLE, str(exc), counts, blocked)
        return

    identity = chunk_id(chunk)
    try:
        contents = await read_targets(mcp, chunk)
    except McpUnavailableError as exc:
        await _retry_later(config, consumer, delivered, chunk, str(exc), counts, blocked)
        return
    except McpError as exc:
        detail = f"reading targets: {exc}"
        await _dead_letter(config, consumer, delivered, chunk, _REASON_REFUSED, detail, counts, blocked)
        return

    verdict = assess_chunk(
        chunk,
        {path: observed_state(content) for path, content in contents.items()},
        raw_layer_prefix=config.raw_layer_prefix,
    )
    depends_on = referenced_blocked_paths(chunk, blocked.get(_batch_id_of(delivered), set()))
    if depends_on or not verdict.accepted:
        # Every way this chunk can fail to apply, and the one way it needs no applying, decided off
        # a single reading of the vault. "Its work is already done" is asked first and beats both
        # refusals: settling writes nothing, so it can leave no link pointing at nothing, and
        # parking it instead would withhold a chunk whose notes are already in the vault.
        if _work_already_done(chunk, contents):
            await settle(consumer, delivered, acked=True, delay_seconds=0.0)
            counts["already_applied"] += 1
            logger.info(
                "chunk already applied: every path it touches already holds what it would have written",
                extra={
                    "event": "chunk_already_applied",
                    "chunk_id": identity,
                    "stream_sequence": delivered.stream_sequence,
                    "target_count": len(chunk.targets),
                    "paths": [target.path for target in chunk.targets][:LOG_PATH_SAMPLE_LIMIT],
                },
            )
            return
        if depends_on:
            # Ahead of the pre-flight's own verdict, because a chunk waiting on undone work is
            # fixed by finishing that work, whereas whatever the pre-flight has to say about a
            # vault the failed chunk never got to change is a fact about the wrong question.
            logger.warning(
                "chunk parked: it links to what an earlier failed chunk of its batch would have created",
                extra={"event": "chunk_depends_on_failed_chunk", "chunk_id": identity, "paths": list(depends_on)},
            )
            detail = f"links to {', '.join(depends_on)}, which an earlier chunk of this batch failed to create"
            await _dead_letter(config, consumer, delivered, chunk, _REASON_DEPENDS_ON_FAILED, detail, counts, blocked)
            return
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
        reason = str(verdict.reasons[0])
        await _dead_letter(config, consumer, delivered, chunk, reason, verdict.summary(), counts, blocked)
        return

    try:
        writes = plan_writes(chunk, contents)
    except PatchError as exc:
        await _dead_letter(config, consumer, delivered, chunk, _REASON_UNAPPLICABLE, str(exc), counts, blocked)
        return

    outcome = await apply_writes(mcp, writes)
    if outcome.failure is not None:
        if outcome.applied == 0 and isinstance(outcome.failure, McpUnavailableError):
            # Nothing landed, and nothing decided anything — an MCP rollout mid-run looks exactly
            # like this, and dead-lettering it would park work with nothing wrong with it. A write
            # that timed out may in fact have landed; the redelivery then rejects as stale, which is
            # loud and destroys nothing.
            await _retry_later(config, consumer, delivered, chunk, str(outcome.failure), counts, blocked)
            return
        # Whatever this chunk had already written stays written; ADR-0048's pre-flight is what makes
        # that safe to leave, because a redelivery would meet it and reject rather than double-apply.
        # Its creates are blocked whether or not some of them landed: which ones did is knowable
        # here, and blocking a path that exists only parks a chunk the producer will regenerate,
        # while missing one leaves the link this whole mechanism exists to prevent.
        reason = _REASON_PARTIALLY_APPLIED if outcome.applied else _REASON_REFUSED
        await _dead_letter(config, consumer, delivered, chunk, reason, str(outcome.failure), counts, blocked)
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
            # Which notes landed, sampled with the whole count beside it — the repository's
            # convention for path lists (`logging_config.LOG_PATH_SAMPLE_LIMIT`). Without this, a
            # run that stops half way through a batch leaves nobody able to say from the logs which
            # files are in the vault, and restaging the difference is the recovery for every partial
            # import: the paths have to be recoverable from the run's own output rather than by
            # decoding the stream or enumerating the vault afterwards.
            "paths": [write.path for write in writes][:LOG_PATH_SAMPLE_LIMIT],
        },
    )


def _work_already_done(chunk: Chunk, contents: Mapping[str, str | None]) -> bool:
    """`patching.already_applied`, with an unparseable patch answering no rather than raising.

    A patch that will not parse establishes nothing, and the verdict this question was asked about
    is already in hand: the chunk goes to the dead-letter path under it, exactly as it would were
    the question never asked. `plan_writes` raises the same error on the applying side.
    """
    try:
        return already_applied(chunk, contents)
    except PatchError:
        return False


async def _retry_later(
    config: BatchProcessorConfig,
    consumer: BatchConsumer,
    delivered: DeliveredChunk,
    chunk: Chunk,
    detail: str,
    counts: Counter[str],
    blocked: dict[str, set[str]],
) -> None:
    if exhausted_redelivery(delivered.delivery_count, max_deliver=config.max_deliver):
        await _dead_letter(config, consumer, delivered, chunk, _REASON_REDELIVERY_EXHAUSTED, detail, counts, blocked)
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
    chunk: Chunk | None,
    reason: str,
    detail: str,
    counts: Counter[str],
    blocked: dict[str, set[str]],
) -> None:
    """Republish, then terminate — in that order, and never the other way round.

    Terminating first would settle the message permanently before its copy existed, so a failure to
    publish would erase the chunk instead of parking it. `dead_letter` raising here therefore leaves
    the message unsettled, and the broker redelivers it: work stalls loudly rather than vanishing.

    Recording the parked chunk's creates happens here, and only here, because every way a chunk can
    fail terminally already funnels through this function — a second recording site would be a
    second place for one to be forgotten.
    """
    batch = _batch_id_of(delivered)
    await consumer.dead_letter(dead_letter_subject(config.dead_letter_subject_prefix, batch), delivered, reason, detail)
    await consumer.term(delivered)
    if chunk is not None and reason != _REASON_DEPENDS_ON_FAILED:
        # A chunk parked as a dependent contributes nothing, which is what keeps the blocking one
        # hop deep — see "What a failed chunk blocks" in the module docstring for the measurement
        # that decided it and the dead link it accepts in exchange.
        blocked.setdefault(batch, set()).update(created_paths(chunk))
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
