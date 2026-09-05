"""`batch-processor` against a real `nats-server` in a container and a real HTTP vault surface.

Everything asserted here is a property of the broker's own behaviour — FIFO delivery, what a nak
does, what `max_deliver` does, what `term` does — so a mocked consumer would only ever agree with
whatever `consumer.py` already believes about all four, which is the failure this repository's
testing rule names. The vault side is a real HTTP server (`vault_stub.py`) rather than a recorded
call list, so every invariant below is read off state the processor actually wrote.

Each test gets its own stream and its own subject prefix. A shared stream with a fresh durable
consumer would replay every earlier test's messages, and a shared consumer would carry an earlier
test's delivery counts — either of which makes a redelivery assertion mean something other than what
it says.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator

import pytest
from nats.aio.client import Client
from nats.js import JetStreamContext
from nats.js.api import ConsumerConfig, StreamConfig
from nats_harness import running_broker
from vault_stub import TOOL_DELETE, TOOL_READ, TOOL_WRITE, FakeVault, running_vault

from obsidian_tools.batch_processor import processor
from obsidian_tools.batch_producer.chunk import Chunk, decode_chunk, encode_chunk
from obsidian_tools.batch_producer.staleness import ChunkTarget, TargetOperation, content_sha256
from obsidian_tools.config import AgentHandleConfig, BatchProcessorConfig

_PORT = 14223
_USER = "streams"
_PASSWORD = "streams-pw"

SERVER_CONFIG = """
port: 4222
jetstream: { store_dir: "/tmp/js" }

accounts {
  STREAMS: {
    jetstream: enabled
    users: [ { user: "streams", password: "streams-pw" } ]
  }
}
"""


@pytest.fixture(scope="module")
def broker() -> Iterator[str]:
    with running_broker(SERVER_CONFIG, _PORT) as url:
        yield url


@pytest.fixture
def vault() -> Iterator[FakeVault]:
    with running_vault(FakeVault()) as state:
        yield state


@pytest.fixture
def tag() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture
def streams(broker: str, tag: str) -> str:
    """A batch stream, a dead-letter stream and a promotion stream with a consumer, all this
    test's own. Returns the tag every subject and stream name is built from."""
    asyncio.run(create_streams(broker, tag))
    return tag


async def _connect(broker: str) -> tuple[Client, JetStreamContext]:
    client = Client()
    await client.connect(servers=broker, user=_USER, password=_PASSWORD)
    return client, JetStreamContext(client)


async def create_streams(broker: str, tag: str) -> None:
    client, jetstream = await _connect(broker)
    try:
        for name, subject in ((f"batch{tag}", f"batch{tag}.>"), (f"dead{tag}", f"dead{tag}.>")):
            await jetstream.add_stream(StreamConfig(name=name, subjects=[subject]))  # type: ignore[reportUnknownMemberType]
        await jetstream.add_stream(StreamConfig(name=f"prom{tag}", subjects=[f"prom{tag}.>"]))  # type: ignore[reportUnknownMemberType]
        await jetstream.add_consumer(f"prom{tag}", ConsumerConfig(durable_name=f"promc{tag}"))  # type: ignore[reportUnknownMemberType]
    finally:
        await client.close()


def config_for(
    broker: str,
    vault: FakeVault,
    tag: str,
    *,
    promotion: bool = False,
    max_deliver: int = 3,
    max_consecutive_yields: int = 2,
    mcp_retries: int = 1,
    ack_wait_seconds: float = 10.0,
    idle_timeout_seconds: float = 1.0,
    dead_letter_prefix: str | None = None,
) -> BatchProcessorConfig:
    return BatchProcessorConfig(
        nats_url=broker,
        nats_user=_USER,
        nats_password=_PASSWORD,
        nats_inbox_prefix="_INBOX",
        stream=f"batch{tag}",
        durable=f"proc{tag}",
        subject_prefix=f"batch{tag}",
        dead_letter_subject_prefix=dead_letter_prefix or f"dead{tag}",
        max_deliver=max_deliver,
        ack_wait_seconds=ack_wait_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
        connect_timeout_seconds=5.0,
        promotion_stream=f"prom{tag}" if promotion else None,
        promotion_consumer=f"promc{tag}" if promotion else None,
        promotion_depth_threshold=1,
        backoff_base_delay_seconds=0.05,
        backoff_max_delay_seconds=0.2,
        backoff_jitter_fraction=0.0,
        max_consecutive_yields=max_consecutive_yields,
        mcp_url=vault.url,
        mcp_api_key="stub-key",
        mcp_tool_read=TOOL_READ,
        mcp_tool_write=TOOL_WRITE,
        mcp_tool_delete=TOOL_DELETE,
        mcp_path_argument="filepath",
        mcp_content_argument="content",
        mcp_timeout_seconds=5.0,
        mcp_verify_tls=False,
        mcp_retries=mcp_retries,
        raw_layer_prefix="05-raw/",
        lease_ttl_seconds=300.0,
        agent_handle=AgentHandleConfig(
            gateway_url=vault.url,
            gateway_admin_key="stub-admin",
            agent_handle_key="stub-handle",
            gateway_timeout_seconds=5.0,
            gateway_verify_tls=False,
        ),
    )


# --- building chunks the producer's format would produce ------------------------------------------


def creating(path: str, body: str) -> tuple[str, tuple[ChunkTarget, ...]]:
    lines = body.split("\n")[:-1]
    hunk = "".join(f"+{line}\n" for line in lines)
    patch = f"diff --git a/{path} b/{path}\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{hunk}"
    return patch, (ChunkTarget(path, TargetOperation.CREATE, None),)


def modifying(path: str, before: str, after: str) -> tuple[str, tuple[ChunkTarget, ...]]:
    old = before.split("\n")[:-1]
    new = after.split("\n")[:-1]
    body = "".join(f"-{line}\n" for line in old) + "".join(f"+{line}\n" for line in new)
    patch = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,{len(old)} +1,{len(new)} @@\n{body}"
    return patch, (ChunkTarget(path, TargetOperation.MODIFY, content_sha256(before.encode("utf-8"))),)


def chunk_of(patch: str, targets: tuple[ChunkTarget, ...], *, batch_id: str | None = None) -> Chunk:
    return Chunk(
        batch_id=batch_id or uuid.uuid4().hex,
        chunk_index=0,
        chunk_count=1,
        produced_at="2026-09-05T12:00:00+00:00",
        patch=patch,
        targets=targets,
    )


def publish(broker: str, tag: str, *chunks: Chunk) -> None:
    async def scenario() -> None:
        client, jetstream = await _connect(broker)
        try:
            for chunk in chunks:
                await jetstream.publish(f"batch{tag}.{chunk.batch_id}", encode_chunk(chunk))
        finally:
            await client.close()

    asyncio.run(scenario())


def dead_letters(broker: str, tag: str) -> list[tuple[bytes, dict[str, str]]]:
    async def scenario() -> list[tuple[bytes, dict[str, str]]]:
        client, jetstream = await _connect(broker)
        try:
            subscription = await jetstream.pull_subscribe(f"dead{tag}.>", durable=f"r{uuid.uuid4().hex[:8]}")
            try:
                messages = await subscription.fetch(10, timeout=2)
            except Exception:
                return []
            collected = [(m.data, dict(m.headers or {})) for m in messages]
            for message in messages:
                await message.ack()
            return collected
        finally:
            await client.close()

    return asyncio.run(scenario())


def dead_letter_count(broker: str, tag: str) -> int:
    """How many chunks are parked, from the stream's own state. Used where the count is checked
    repeatedly (the crash harness's invariants): reading them back would mint a consumer per check.
    """

    async def scenario() -> int:
        client, jetstream = await _connect(broker)
        try:
            return (await jetstream.stream_info(f"dead{tag}")).state.messages
        finally:
            await client.close()

    return asyncio.run(scenario())


def fill_promotion(broker: str, tag: str, count: int) -> None:
    async def scenario() -> None:
        client, jetstream = await _connect(broker)
        try:
            for index in range(count):
                await jetstream.publish(f"prom{tag}.x", f"{index}".encode())
        finally:
            await client.close()

    asyncio.run(scenario())


# --- ordering, and that a run is a run ------------------------------------------------------------


def test_chunks_apply_in_the_order_the_stream_delivered_them(broker: str, vault: FakeVault, streams: str) -> None:
    """ADR-0022's dependency-by-ordering, end to end: the second chunk's patch only applies to the
    first chunk's output. Red if anything let the consumer run more than one chunk in flight — the
    second would meet the original content, fail its context check, and be dead-lettered, which is
    exactly how a relink applied before its rename presents."""
    first = chunk_of(*creating("10-areas/x.md", "one\n"))
    second = chunk_of(*modifying("10-areas/x.md", "one\n", "two\n"))
    publish(broker, streams, first, second)

    assert processor.run(config_for(broker, vault, streams)) == 0
    assert vault.written("10-areas/x.md") == "two\n"
    assert dead_letters(broker, streams) == []


def test_the_agent_handle_is_down_for_every_write_and_back_up_afterwards(
    broker: str, vault: FakeVault, streams: str
) -> None:
    """ "Batch mode is exactly which handle is enabled" (ADR-0022), injected: the handle's state is
    sampled *at* each write rather than after the run. Red if the handle were never disabled — bulk
    would run alongside the interactive writers it exists to exclude — and equally red if it were
    left down, which is the silent, indefinite outage unit D4's watchdog exists to end."""
    publish(broker, streams, chunk_of(*creating("10-areas/x.md", "one\n")))

    processor.run(config_for(broker, vault, streams))

    assert vault.handle_state_during_writes == [True]
    assert vault.handle_blocked is False


def test_an_empty_stream_applies_nothing_and_restores_the_handle(broker: str, vault: FakeVault, streams: str) -> None:
    """ADR-0022's triggering policy is a schedule plus this. Red if an empty stream cost anything
    more than one fetch, or left the handle down: a nightly run against an empty queue would block
    every agent write for as long as it took to notice."""
    assert processor.run(config_for(broker, vault, streams)) == 0

    assert vault.calls == []
    assert vault.handle_blocked is False


def test_an_applied_chunk_is_not_redelivered_to_a_later_run(broker: str, vault: FakeVault, streams: str) -> None:
    """Red if the ack were never sent, or sent before the writes: every run would reapply the whole
    stream, and the second application would meet its own output and reject as stale."""
    publish(broker, streams, chunk_of(*creating("10-areas/x.md", "one\n")))
    processor.run(config_for(broker, vault, streams))
    vault.calls.clear()

    processor.run(config_for(broker, vault, streams))

    assert vault.calls == []


# --- the two refusals, injected end to end ---------------------------------------------------------


def test_a_stale_chunk_is_dead_lettered_with_nothing_applied(broker: str, vault: FakeVault, streams: str) -> None:
    """ADR-0022's stale-reject, over a real broker. Red if a stale chunk were applied (the silent
    lost update), or nak'd (a permanent condition retried forever), or terminated without a
    dead-letter copy (work that vanishes rather than parks)."""
    vault.notes["10-areas/x.md"] = "something else\n"
    publish(broker, streams, chunk_of(*modifying("10-areas/x.md", "one\n", "two\n")))

    processor.run(config_for(broker, vault, streams))

    assert vault.written("10-areas/x.md") == "something else\n"
    parked = dead_letters(broker, streams)
    assert len(parked) == 1
    assert parked[0][1]["Obsidian-Batch-Reason"] == "stale"


def test_a_create_over_an_existing_raw_note_is_dead_lettered(broker: str, vault: FakeVault, streams: str) -> None:
    """ADR-0015's write-once rule, injected end to end — and the refusal comes from this
    processor's own code, not from the vault surface, which here would happily have accepted the
    write. Red if the existing raw note were overwritten: the layer stops being write-once and the
    validation exemption resting on it poisons the baseline."""
    vault.notes["05-raw/imported.md"] = "the original import\n"
    publish(broker, streams, chunk_of(*creating("05-raw/imported.md", "a replacement\n")))

    processor.run(config_for(broker, vault, streams))

    assert vault.written("05-raw/imported.md") == "the original import\n"
    parked = dead_letters(broker, streams)
    assert parked[0][1]["Obsidian-Batch-Reason"] == "raw_layer_exists"


def test_a_dead_lettered_chunk_is_still_a_decodable_chunk(broker: str, vault: FakeVault, streams: str) -> None:
    """The bytes are republished unchanged, with the reason in the headers. Red if the reason were
    folded into the payload: a parked chunk would no longer decode, and re-enqueueing one would be a
    reconstruction rather than a copy."""
    original = chunk_of(*modifying("10-areas/x.md", "one\n", "two\n"))
    vault.notes["10-areas/x.md"] = "moved on\n"
    publish(broker, streams, original)

    processor.run(config_for(broker, vault, streams))

    body, _ = dead_letters(broker, streams)[0]
    assert decode_chunk(body) == original


# --- redelivery ------------------------------------------------------------------------------------


def test_a_transient_write_failure_is_redelivered_and_then_applies(broker: str, vault: FakeVault, streams: str) -> None:
    """Red if a 503 dead-lettered the chunk: an ordinary MCP rollout would park every chunk in
    flight, and an operator would have to re-enqueue work that nothing was actually wrong with."""
    vault.unavailable_writes = 1
    publish(broker, streams, chunk_of(*creating("10-areas/x.md", "one\n")))

    processor.run(config_for(broker, vault, streams, max_deliver=3))

    assert vault.written("10-areas/x.md") == "one\n"
    assert dead_letters(broker, streams) == []


def test_a_redelivered_chunk_is_reapplied_before_the_chunk_that_depends_on_it(
    broker: str, vault: FakeVault, streams: str
) -> None:
    """The sharp edge of ADR-0022's dependency-by-ordering: a chunk given back for redelivery must
    come back *before* the newer chunk that was built on top of it. The second chunk here only
    applies to the first one's output, so if the broker served it during the first one's backoff it
    would be rejected as missing and parked — a relink applied before its rename, exactly the
    failure one FIFO stream was chosen to make impossible."""
    vault.unavailable_writes = 1
    publish(
        broker,
        streams,
        chunk_of(*creating("10-areas/x.md", "one\n")),
        chunk_of(*modifying("10-areas/x.md", "one\n", "two\n")),
    )

    processor.run(config_for(broker, vault, streams, max_deliver=3))

    assert vault.written("10-areas/x.md") == "two\n"
    assert dead_letters(broker, streams) == []


def test_a_chunk_that_keeps_failing_is_dead_lettered_rather_than_redelivered_forever(
    broker: str, vault: FakeVault, streams: str
) -> None:
    """ADR-0020 records that JetStream's dead-letter path needs building; this is that path firing.
    Red if it were absent — the consumer's `max_ack_pending=1` means one unbounded chunk blocks the
    whole FIFO stream behind it, so "redelivered forever" is also "nothing else ever runs"."""
    vault.unavailable_writes = 99
    publish(broker, streams, chunk_of(*creating("10-areas/x.md", "one\n")))

    processor.run(config_for(broker, vault, streams, max_deliver=2))

    parked = dead_letters(broker, streams)
    assert len(parked) == 1
    assert parked[0][1]["Obsidian-Batch-Reason"] == "redelivery_exhausted"
    assert parked[0][1]["Obsidian-Batch-Delivery-Count"] == "2"


def test_a_duplicate_delivery_never_applies_its_writes_twice(broker: str, vault: FakeVault, streams: str) -> None:
    """The idempotence oracle, and the one property redelivery actually gives: applying a chunk a
    second time cannot change the vault, because the pre-flight now measures against the first
    application's own output. Red if the second copy applied — a redelivered chunk would overwrite
    whatever had happened in between, which is the silent lost update ADR-0048 exists to prevent."""
    chunk = chunk_of(*creating("10-areas/x.md", "one\n"))
    publish(broker, streams, chunk, chunk)

    processor.run(config_for(broker, vault, streams))

    assert vault.written("10-areas/x.md") == "one\n"
    assert [call for call in vault.calls if call[0] == TOOL_WRITE] == [(TOOL_WRITE, "10-areas/x.md")]
    assert dead_letters(broker, streams)[0][1]["Obsidian-Batch-Reason"] == "already_exists"


def test_a_chunk_that_failed_part_way_through_is_parked_rather_than_replayed(
    broker: str, vault: FakeVault, streams: str
) -> None:
    """ADR-0048's stated consequence, injected: the second write of a two-file chunk is refused, so
    the chunk half-applied. Red if it were nak'd instead — its redelivery would meet content its own
    earlier write moved, and replaying it is the double-apply the whole-chunk pre-flight exists to
    make impossible."""
    first_patch, first_targets = creating("10-areas/a.md", "a\n")
    second_patch, second_targets = creating("10-areas/b.md", "b\n")
    vault.fail_after_writes = 1
    publish(broker, streams, chunk_of(first_patch + second_patch, first_targets + second_targets))

    processor.run(config_for(broker, vault, streams))

    assert vault.written("10-areas/a.md") == "a\n"
    assert vault.written("10-areas/b.md") is None
    assert dead_letters(broker, streams)[0][1]["Obsidian-Batch-Reason"] == "partially_applied"


def test_a_chunk_is_not_terminated_when_its_dead_letter_copy_cannot_land(
    broker: str, vault: FakeVault, streams: str
) -> None:
    """Republish *then* terminate, and never the other way round: terminating first settles the
    message permanently before its copy exists, so a dead-letter stream that is missing,
    misconfigured or full would erase the chunk instead of parking it. Injected by pointing the
    dead-letter subject at a prefix no stream serves. Red if the order were swapped — the chunk
    would be gone from the broker with nothing anywhere to show for it."""
    vault.notes["10-areas/x.md"] = "moved on\n"
    publish(broker, streams, chunk_of(*modifying("10-areas/x.md", "one\n", "two\n")))
    config = config_for(broker, vault, streams, dead_letter_prefix=f"nowhere{streams}")

    with pytest.raises(Exception, match="could not dead-letter"):
        processor.run(config)

    assert pending_after(broker, config) == 1


def pending_after(broker: str, config: BatchProcessorConfig) -> int:
    async def scenario() -> int:
        client, jetstream = await _connect(broker)
        try:
            info = await jetstream.consumer_info(config.stream, config.durable)
            return (info.num_pending or 0) + (info.num_ack_pending or 0)
        finally:
            await client.close()

    return asyncio.run(scenario())


# --- fairness --------------------------------------------------------------------------------------


def test_a_deep_promotion_stream_stops_the_batch_before_it_takes_a_chunk(
    broker: str, vault: FakeVault, streams: str
) -> None:
    """ADR-0022's one-way asymmetry, against a real queue depth. Red if the batch proceeded while
    promotion had work pending: bulk would be taking the shared editor's time while a human waited,
    which is the starvation the fairness rule — not any health signal — exists to prevent.

    Red in the other direction too, and that half matters as much: the run *ends* rather than
    yielding forever, so an undrained promotion stream cannot hold the agent handle down.
    """
    fill_promotion(broker, streams, 3)
    publish(broker, streams, chunk_of(*creating("10-areas/x.md", "one\n")))

    processor.run(config_for(broker, vault, streams, promotion=True, max_consecutive_yields=1))

    assert vault.calls == []
    assert vault.handle_blocked is False


def test_the_batch_proceeds_once_the_promotion_stream_has_drained(broker: str, vault: FakeVault, streams: str) -> None:
    """The control for the test above: red if the yield were unconditional, which would make the
    previous test pass for the wrong reason and stop batches running at all."""
    publish(broker, streams, chunk_of(*creating("10-areas/x.md", "one\n")))

    processor.run(config_for(broker, vault, streams, promotion=True, max_consecutive_yields=1))

    assert vault.written("10-areas/x.md") == "one\n"
