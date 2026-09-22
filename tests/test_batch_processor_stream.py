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
import logging
import uuid
from collections.abc import Iterator

import pytest
from nats.aio.client import Client
from nats.js import JetStreamContext
from nats.js.api import ConsumerConfig, StreamConfig
from nats_harness import running_broker
from vault_stub import (
    DEPLOYMENT,
    LEASE,
    LEASE_PATH,
    NAMESPACE,
    SCALE_PATH,
    TOKEN_PATH,
    TOOL_DELETE,
    TOOL_READ,
    TOOL_WRITE,
    FakeVault,
    running_vault,
)

from obsidian_tools.batch_processor import processor
from obsidian_tools.batch_producer.chunk import Chunk, decode_chunk, encode_chunk
from obsidian_tools.batch_producer.staleness import ChunkTarget, TargetOperation, content_sha256
from obsidian_tools.config import AgentInstanceConfig, BatchProcessorConfig

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
        mcp_timeout_seconds=5.0,
        mcp_verify_tls=False,
        mcp_retries=mcp_retries,
        raw_layer_prefix="05-raw/",
        lease_ttl_seconds=300.0,
        agent_instance=AgentInstanceConfig(
            api_url=vault.url,
            namespace=NAMESPACE,
            deployment=DEPLOYMENT,
            lease_name=LEASE,
            holder_identity=f"stub-run-{tag}",
            token_path=TOKEN_PATH,
            ca_path=None,
            timeout_seconds=5.0,
            verify_tls=False,
        ),
    )


# --- building chunks the producer's format would produce ------------------------------------------

ADMISSIBLE_FRONTMATTER = "---\ntype: note\nauthority: agent\ntrigger: schedule\n---\n"


def note(body: str) -> str:
    """`body` as a note curated space admits: the three fields the admission bar requires, and no
    others. Every curated fixture in a test about some other mechanism is built with this, so that
    test's outcome turns on its own mechanism and never on where admission sits in the order."""
    return ADMISSIBLE_FRONTMATTER + body


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


def renaming(old: str, new: str, body: str) -> tuple[str, tuple[ChunkTarget, ...]]:
    """A pure rename, which the chunk format carries as a delete of the old path plus a create of
    the new one — the shape ADR-0022 uses to describe dependency by ordering."""
    patch = f"diff --git a/{old} b/{new}\nsimilarity index 100%\nrename from {old}\nrename to {new}\n"
    return patch, (
        ChunkTarget(old, TargetOperation.DELETE, content_sha256(body.encode("utf-8"))),
        ChunkTarget(new, TargetOperation.CREATE, None),
    )


def chunk_of(
    patch: str, targets: tuple[ChunkTarget, ...], *, batch_id: str | None = None, index: int = 0, count: int = 1
) -> Chunk:
    return Chunk(
        batch_id=batch_id or uuid.uuid4().hex,
        chunk_index=index,
        chunk_count=count,
        produced_at="2026-09-05T12:00:00+00:00",
        patch=patch,
        targets=targets,
    )


def a_linked_batch(batch_id: str) -> tuple[Chunk, ...]:
    """Four creates of one batch in a chain: each links to the note the chunk before it creates.

    The shape a bulk import of a linked vault really has, at the smallest size that can tell a
    direct dependent from a second-hop one. `10-areas/blocked.md` is the one a test refuses.
    """
    bodies = {
        "10-areas/base.md": "base\n",
        "10-areas/blocked.md": "see [[base]]\n",
        "10-areas/direct.md": "see [[blocked]]\n",
        "10-areas/second-hop.md": "see [[direct]]\n",
    }
    return tuple(
        chunk_of(*creating(path, note(body)), batch_id=batch_id, index=index, count=len(bodies))
        for index, (path, body) in enumerate(bodies.items())
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


def publish_raw(broker: str, tag: str, batch_id: str, body: bytes) -> None:
    """A message on a batch's subject that never went through `encode_chunk` — the only way to put
    a body the consumer cannot decode onto the stream."""

    async def scenario() -> None:
        client, jetstream = await _connect(broker)
        try:
            await jetstream.publish(f"batch{tag}.{batch_id}", body)
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
    first = chunk_of(*creating("10-areas/x.md", note("one\n")))
    second = chunk_of(*modifying("10-areas/x.md", note("one\n"), note("two\n")))
    publish(broker, streams, first, second)

    assert processor.run(config_for(broker, vault, streams)) == 0
    assert vault.written("10-areas/x.md") == note("two\n")
    assert dead_letters(broker, streams) == []


def test_the_agent_instance_is_stopped_for_every_write_and_running_afterwards(
    broker: str, vault: FakeVault, streams: str
) -> None:
    """ "Batch mode is which of the two instances is running" (ADR-0052), injected: the instance's
    desired replica count is sampled *at* each write rather than after the run. Red if the instance
    were never stopped — bulk would run alongside the interactive writers it exists to exclude — and
    equally red if it were left stopped, which is the silent, indefinite outage unit D4's watchdog
    exists to end."""
    publish(broker, streams, chunk_of(*creating("10-areas/x.md", note("one\n"))))

    processor.run(config_for(broker, vault, streams))

    assert vault.agent_replicas_during_writes == [0]
    assert vault.agent_replicas == 1


def test_a_run_takes_the_lease_before_stopping_and_starts_before_releasing(
    broker: str, vault: FakeVault, streams: str
) -> None:
    """ADR-0052's ordering, read off the wire rather than off the code: the whole reason "stopped
    with no lease" may be treated as an operator's own hold is that no partial run can produce it.

    Red if either pair were reversed. Reversing the first leaves a run killed between its two writes
    with the instance at zero and no lease, which the watchdog is required to leave exactly as found
    — every interactive write in the system stops, permanently. Reversing the second leaves a run
    killed at the end in the same state. The crash-injection harness proves the same claim by
    actually being killed there; this one proves it in the ordinary, uninterrupted path, where the
    mistake would otherwise be invisible because everything still works."""
    publish(broker, streams, chunk_of(*creating("10-areas/x.md", note("one\n"))))

    processor.run(config_for(broker, vault, streams))

    patched = [path for method, path in vault.kube_calls if method == "PATCH"]
    assert patched[0] == LEASE_PATH, "the lease is taken before the instance is stopped"
    assert patched[1] == SCALE_PATH
    assert patched[-2] == SCALE_PATH, "the instance is started before the lease is released"
    assert patched[-1] == LEASE_PATH
    assert vault.lease == {}, "a finished run leaves no lease behind"


def test_an_empty_stream_applies_nothing_and_restarts_the_instance(broker: str, vault: FakeVault, streams: str) -> None:
    """ADR-0022's triggering policy is a schedule plus this. Red if an empty stream cost anything
    more than one fetch, or left the instance stopped: a nightly run against an empty queue would
    block every interactive write for as long as it took to notice."""
    assert processor.run(config_for(broker, vault, streams)) == 0

    assert vault.calls == []
    assert vault.agent_replicas == 1


def test_an_applied_chunk_is_not_redelivered_to_a_later_run(broker: str, vault: FakeVault, streams: str) -> None:
    """Red if the ack were never sent, or sent before the writes: every run would reapply the whole
    stream, and the second application would meet its own output and reject as stale."""
    publish(broker, streams, chunk_of(*creating("10-areas/x.md", note("one\n"))))
    processor.run(config_for(broker, vault, streams))
    vault.calls.clear()

    processor.run(config_for(broker, vault, streams))

    assert vault.calls == []


# --- the two refusals, injected end to end ---------------------------------------------------------


def test_a_stale_chunk_is_dead_lettered_with_nothing_applied(broker: str, vault: FakeVault, streams: str) -> None:
    """ADR-0022's stale-reject, over a real broker. Red if a stale chunk were applied (the silent
    lost update), or nak'd (a permanent condition retried forever), or terminated without a
    dead-letter copy (work that vanishes rather than parks)."""
    vault.notes["10-areas/x.md"] = note("something else\n")
    publish(broker, streams, chunk_of(*modifying("10-areas/x.md", note("one\n"), note("two\n"))))

    processor.run(config_for(broker, vault, streams))

    assert vault.written("10-areas/x.md") == note("something else\n")
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


def test_a_failed_rename_parks_its_relink_and_nothing_else(broker: str, vault: FakeVault, streams: str) -> None:
    """ADR-0022's own example of dependency by ordering, injected end to end: the rename fails, so
    the chunk that repoints another note's link at the new name must not apply.

    Three claims, and each fails differently. Red on `10-areas/other.md` means the relink landed and
    the vault now holds a link to a page nobody created — the defect itself, and indistinguishable
    afterwards from an ordinary dead link. Red on `10-areas/sibling.md` or `10-areas/elsewhere.md`
    means one bad chunk discarded work that had nothing to do with it, in the same batch and in
    another, which for a bulk import of thousands of chunks is the worse failure of the two.
    """
    batch = uuid.uuid4().hex
    vault.notes["10-areas/old-name.md"] = note("someone else got here first\n")
    vault.notes["10-areas/other.md"] = note("see [[old-name]]\n")
    rename = chunk_of(
        *renaming("10-areas/old-name.md", "10-areas/new-name.md", note("the page\n")),
        batch_id=batch,
        index=0,
        count=3,
    )
    relink = chunk_of(
        *modifying("10-areas/other.md", note("see [[old-name]]\n"), note("see [[new-name]]\n")),
        batch_id=batch,
        index=1,
        count=3,
    )
    sibling = chunk_of(*creating("10-areas/sibling.md", note("unrelated\n")), batch_id=batch, index=2, count=3)
    # Deliberately links to the same page the rename failed to create: the blocking is keyed by
    # batch, and a chunk another producer emitted is not this batch's dependent whatever it says.
    elsewhere = chunk_of(*creating("10-areas/elsewhere.md", note("see [[new-name]]\n")))
    publish(broker, streams, rename, relink, sibling, elsewhere)

    processor.run(config_for(broker, vault, streams))

    assert vault.written("10-areas/other.md") == note("see [[old-name]]\n")
    assert vault.written("10-areas/new-name.md") is None
    assert vault.written("10-areas/sibling.md") == note("unrelated\n")
    assert vault.written("10-areas/elsewhere.md") == note("see [[new-name]]\n")
    # The parked chunk *is* read before it is parked, and that cost is deliberate: parking is the
    # only verdict that can be wrong about work already done, so the question has to be answerable.
    assert (TOOL_READ, "10-areas/other.md") in vault.calls
    reasons = [headers["Obsidian-Batch-Reason"] for _, headers in dead_letters(broker, streams)]
    assert reasons == ["stale", "depends_on_failed_chunk"]


def test_a_chunk_parked_as_a_dependent_does_not_park_what_links_to_it(
    broker: str, vault: FakeVault, streams: str
) -> None:
    """The blocking is one hop deep. One write is refused in a batch of four linked creates, so the
    chunk linking to it is parked — and the chunk linking to *that* one still applies.

    Red on `10-areas/second-hop.md` is the cascade: chained, the rule parks the whole tail of a
    link-dense batch, and it was measured doing exactly that — one refused write in chunk 5 of 36
    parked 31 chunks and left 1,287 of 1,500 notes unwritten, against one parked for the same corpus
    with its wikilinks removed. Red on `10-areas/direct.md` is the opposite failure and the reason
    the first hop is kept: a note written pointing at a page nobody created.
    """
    batch = uuid.uuid4().hex
    vault.refuse_paths = {"10-areas/blocked.md"}
    publish(broker, streams, *a_linked_batch(batch))

    processor.run(config_for(broker, vault, streams))

    assert vault.written("10-areas/base.md") == note("base\n")
    assert vault.written("10-areas/blocked.md") is None
    assert vault.written("10-areas/direct.md") is None
    assert vault.written("10-areas/second-hop.md") == note("see [[direct]]\n")
    reasons = [headers["Obsidian-Batch-Reason"] for _, headers in dead_letters(broker, streams)]
    assert reasons == ["mcp_refused", "depends_on_failed_chunk"]


def test_a_chunk_whose_work_is_done_is_acked_even_when_it_links_to_a_failed_chunk(
    broker: str, vault: FakeVault, streams: str, caplog: pytest.LogCaptureFixture
) -> None:
    """ "Is this work already done?" is asked before *both* refusals, the dependency one included.

    The chunk here links to a page the failed chunk would have created — the exact shape that gets
    parked — but its own note is already in the vault, byte for byte. Settling it writes nothing, so
    it cannot leave a link pointing at nothing; parking it would only withhold a chunk the vault
    already has.

    Red if the dependency check ran first, and it is not a small red: measured against a vault
    holding a complete import, not one of thirty fully-applied chunks was recognised, because a
    single genuine conflict early in the batch parked every one of them. Every retry after any drift
    then reports a batch's worth of dead letters against a vault that is already correct.
    """
    batch = uuid.uuid4().hex
    vault.refuse_paths = {"10-areas/base.md"}
    vault.notes["10-areas/done.md"] = note("see [[base]]\n")
    base = chunk_of(*creating("10-areas/base.md", note("base\n")), batch_id=batch, index=0, count=3)
    done = chunk_of(*creating("10-areas/done.md", note("see [[base]]\n")), batch_id=batch, index=1, count=3)
    fresh = chunk_of(*creating("10-areas/fresh.md", note("see [[base]]\n")), batch_id=batch, index=2, count=3)
    publish(broker, streams, base, done, fresh)

    with caplog.at_level(logging.INFO):
        processor.run(config_for(broker, vault, streams))

    [settled] = [record for record in caplog.records if getattr(record, "event", None) == "chunk_already_applied"]
    assert getattr(settled, "paths", None) == ["10-areas/done.md"]
    # Only the genuinely failed chunk and the genuinely undone dependent are parked.
    reasons = [headers["Obsidian-Batch-Reason"] for _, headers in dead_letters(broker, streams)]
    assert reasons == ["mcp_refused", "depends_on_failed_chunk"]
    assert vault.written("10-areas/done.md") == note("see [[base]]\n")
    assert vault.written("10-areas/fresh.md") is None


def test_a_re_run_of_the_same_batch_applies_what_the_first_run_could_not(
    broker: str, vault: FakeVault, streams: str
) -> None:
    """Convergence, which is the property that makes any of this recoverable: run the batch, fix
    what was wrong, run it again, and the vault ends up holding all of it.

    The second run is what a producer re-run really is — the whole batch republished under a new
    identity, most of it already applied. Red on the notes means the re-run made no progress, which
    is measured behaviour without this: the already-applied chunks are refused as duplicates, each
    refusal blocks the paths it would have created, and three consecutive cycles applied zero chunks
    while the vault stayed frozen at a third of the import. Red on the write list means an
    already-applied chunk was written a second time rather than acked — the same content, so the
    vault would not show it, and only the call list can.
    """
    vault.refuse_paths = {"10-areas/blocked.md"}
    publish(broker, streams, *a_linked_batch(uuid.uuid4().hex))
    processor.run(config_for(broker, vault, streams))

    vault.refuse_paths = set()
    vault.calls.clear()
    publish(broker, streams, *a_linked_batch(uuid.uuid4().hex))

    assert processor.run(config_for(broker, vault, streams)) == 0

    assert vault.written("10-areas/blocked.md") == note("see [[base]]\n")
    assert vault.written("10-areas/direct.md") == note("see [[blocked]]\n")
    assert [path for tool, path in vault.calls if tool == TOOL_WRITE] == [
        "10-areas/blocked.md",
        "10-areas/direct.md",
    ]


def test_a_re_import_over_a_differing_raw_note_is_still_refused(broker: str, vault: FakeVault, streams: str) -> None:
    """The control for the test above, and the line the convergence fix must not cross. Same shape —
    a create whose target already exists — and the opposite outcome, because the content differs.

    Red here is the failure the write-once layer exists to prevent: a second import silently
    replacing a first one's note. Green here alongside a red `already_applied` row would mean the
    two cases had been collapsed into one, which is how "settle what is done" turns into "overwrite
    what is not".
    """
    vault.notes["05-raw/imported.md"] = "one\ntwo\n"
    identical = chunk_of(*creating("05-raw/imported.md", "one\ntwo\n"))
    differing = chunk_of(*creating("05-raw/other.md", "the replacement\n"))
    vault.notes["05-raw/other.md"] = "the original import\n"
    publish(broker, streams, identical, differing)

    assert processor.run(config_for(broker, vault, streams)) == 1

    assert vault.written("05-raw/other.md") == "the original import\n"
    reasons = [headers["Obsidian-Batch-Reason"] for _, headers in dead_letters(broker, streams)]
    assert reasons == ["raw_layer_exists"]


def test_an_applied_chunk_names_the_notes_it_wrote(
    broker: str, vault: FakeVault, streams: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Recovering a partial import means restaging exactly the paths that did not land, and that is
    only possible if the ones that did are in the run's own output. Red if `chunk_applied` carried
    only a count: after a run that stopped half way, nobody can say from the logs which notes are in
    the vault, and the repair needs the stream decoded or the vault enumerated to find out."""
    first_patch, first_targets = creating("10-areas/a.md", note("a\n"))
    second_patch, second_targets = creating("10-areas/b.md", note("b\n"))
    publish(broker, streams, chunk_of(first_patch + second_patch, first_targets + second_targets))

    with caplog.at_level(logging.INFO):
        processor.run(config_for(broker, vault, streams))

    [applied] = [record for record in caplog.records if getattr(record, "event", None) == "chunk_applied"]
    assert getattr(applied, "paths", None) == ["10-areas/a.md", "10-areas/b.md"]
    assert getattr(applied, "write_count", None) == 2


def test_an_undecodable_chunk_blocks_nothing_in_its_batch(broker: str, vault: FakeVault, streams: str) -> None:
    """The stated limit of the mechanism, pinned rather than left to be rediscovered: a body that
    will not decode carries no target list, so which paths its batch now owes is unknowable and
    nothing is blocked. Red if the batch were parked wholesale on it — a corrupted message would
    then discard every chunk behind it, which is the blast radius the run-continues rule refuses."""
    batch = uuid.uuid4().hex
    publish_raw(broker, streams, batch, b"{not a chunk")
    publish(broker, streams, chunk_of(*creating("10-areas/x.md", note("one\n")), batch_id=batch, index=1, count=2))

    processor.run(config_for(broker, vault, streams))

    assert vault.written("10-areas/x.md") == note("one\n")
    reasons = [headers["Obsidian-Batch-Reason"] for _, headers in dead_letters(broker, streams)]
    assert reasons == ["undecodable_chunk"]


def test_a_dead_lettered_chunk_is_still_a_decodable_chunk(broker: str, vault: FakeVault, streams: str) -> None:
    """The bytes are republished unchanged, with the reason in the headers. Red if the reason were
    folded into the payload: a parked chunk would no longer decode, and re-enqueueing one would be a
    reconstruction rather than a copy."""
    original = chunk_of(*modifying("10-areas/x.md", note("one\n"), note("two\n")))
    vault.notes["10-areas/x.md"] = note("moved on\n")
    publish(broker, streams, original)

    processor.run(config_for(broker, vault, streams))

    body, _ = dead_letters(broker, streams)[0]
    assert decode_chunk(body) == original


# --- admission at the curated boundary --------------------------------------------------------------
#
# ADR-0007's gate on the batch path. What each rule refuses is `admission/`'s and proven in its own
# tests; what is proven here is the caller: that every curated post-image is asked, that a refusal
# withholds the whole chunk and travels the dead-letter path with its reasons, and that nothing
# outside curated space — nor any delete — is asked at all.

FINANCE = (
    "---\ntype: note\nauthority: import\ntrigger: schedule\nconfidence: high\n---\n"
    "Balance 1,200 (as of 2026-07-30, bank statement)\n"
)


def deleting(path: str, body: str) -> tuple[str, tuple[ChunkTarget, ...]]:
    old = body.split("\n")[:-1]
    removed = "".join(f"-{line}\n" for line in old)
    patch = (
        f"diff --git a/{path} b/{path}\ndeleted file mode 100644\n--- a/{path}\n+++ /dev/null\n"
        f"@@ -1,{len(old)} +0,0 @@\n{removed}"
    )
    return patch, (ChunkTarget(path, TargetOperation.DELETE, content_sha256(body.encode("utf-8"))),)


def one_chunk(*parts: tuple[str, tuple[ChunkTarget, ...]], batch_id: str | None = None, index: int = 0) -> Chunk:
    """Several files' diffs as one chunk — one transaction."""
    patch = "".join(patch for patch, _ in parts)
    targets = tuple(target for _, targets in parts for target in targets)
    return chunk_of(patch, targets, batch_id=batch_id, index=index)


def the_run_summary(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    [complete] = [record for record in caplog.records if getattr(record, "event", None) == "batch_run_complete"]
    return complete


def test_a_curated_note_missing_type_is_refused_and_its_admissible_twin_applies(
    broker: str, vault: FakeVault, streams: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The gate on the batch path, injected: S2's own falsifier, a note with no `type` offered to
    curated space. It is dead-lettered with nothing written, its reason on the header and counted
    by rule, and the run exits non-zero; the same note carrying `type` applies.

    **This is the mutation check for the wiring as a whole**: delete the `admit` call and the
    untyped note lands. Red the other way — the typed control refused — means the gate is refusing
    something the bar admits, and every curated chunk in a bulk import would park."""
    untyped = "---\nauthority: agent\ntrigger: schedule\n---\nbody\n"
    refused = chunk_of(*creating("10-areas/homelab/untyped.md", untyped))
    control = chunk_of(*creating("10-areas/homelab/typed.md", note("body\n")))
    publish(broker, streams, refused, control)

    with caplog.at_level(logging.INFO):
        assert processor.run(config_for(broker, vault, streams)) == 1

    assert vault.written("10-areas/homelab/untyped.md") is None
    assert vault.written("10-areas/homelab/typed.md") == note("body\n")
    [(body, headers)] = dead_letters(broker, streams)
    assert decode_chunk(body) == refused
    assert headers["Obsidian-Batch-Reason"] == "admission_refused"
    assert headers["Obsidian-Batch-Detail"] == "10-areas/homelab/untyped.md: type_missing (type)"
    summary = the_run_summary(caplog)
    assert getattr(summary, "admission_refused_type_missing", None) == 1
    assert getattr(summary, "dead_lettered_admission_refused", None) == 1


@pytest.mark.parametrize(
    ("content", "refusal"),
    [
        pytest.param(FINANCE, None, id="evidenced-applies"),
        pytest.param(
            FINANCE.replace(" (as of 2026-07-30, bank statement)", ""), "finance_provenance", id="unmarked-refused"
        ),
        pytest.param(
            FINANCE.replace("authority: import", "authority: human"),
            "finance_authority (authority)",
            id="claimed-by-a-human-refused",
        ),
    ],
)
def test_the_finance_block_keys_on_evidence_at_the_batch_boundary(
    broker: str, vault: FakeVault, streams: str, content: str, refusal: str | None
) -> None:
    """ADR-0010's hard block, reached through a chunk: a finance note carrying `authority: import`,
    `confidence:` and an inline recency marker applies; the same note without the marker is refused,
    and so is the same note claimed by a human. The last is the case that separates a block keyed on
    evidence from one keyed on who claims to have typed it. Red on any row in either direction."""
    path = "10-areas/finance/balance.md"
    publish(broker, streams, chunk_of(*creating(path, content)))

    exit_code = processor.run(config_for(broker, vault, streams))

    if refusal is None:
        assert (exit_code, vault.written(path)) == (0, content)
        assert dead_letters(broker, streams) == []
    else:
        assert (exit_code, vault.written(path)) == (1, None)
        [(_, headers)] = dead_letters(broker, streams)
        assert headers["Obsidian-Batch-Detail"] == f"{path}: {refusal}"


def test_one_refused_note_withholds_every_write_in_its_chunk(
    broker: str, vault: FakeVault, streams: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The chunk is the transaction unit, so admission is judged over all of it before anything is
    written. One chunk carries an admissible curated note, an inbox capture that alone would apply,
    and two notes curated space refuses for different reasons — one in each curated root.

    Red if refusal were per file: the admissible note and the capture would land, and the chunk's
    regeneration would then meet its own earlier writes and reject as stale — a half-applied chunk,
    manufactured on purpose. The detail names every refused note, and each is counted under its own
    rule."""
    chunk = one_chunk(
        creating("10-areas/fine.md", note("fine\n")),
        creating("00-inbox/capture.md", "a capture\n"),
        creating("20-projects/bare.md", "no frontmatter\n"),
        creating("10-areas/untyped.md", "---\nauthority: agent\ntrigger: schedule\n---\nbody\n"),
    )
    publish(broker, streams, chunk)

    with caplog.at_level(logging.INFO):
        assert processor.run(config_for(broker, vault, streams)) == 1

    assert [call for call in vault.calls if call[0] == TOOL_WRITE] == []
    assert vault.notes == {}
    [(_, headers)] = dead_letters(broker, streams)
    assert headers["Obsidian-Batch-Detail"] == (
        "20-projects/bare.md: frontmatter_unparseable; 10-areas/untyped.md: type_missing (type)"
    )
    summary = the_run_summary(caplog)
    assert getattr(summary, "admission_refused_frontmatter_unparseable", None) == 1
    assert getattr(summary, "admission_refused_type_missing", None) == 1
    assert getattr(summary, "dead_lettered_admission_refused", None) == 1


def test_a_modify_that_strips_provenance_from_a_curated_note_is_refused(
    broker: str, vault: FakeVault, streams: str
) -> None:
    """ADR-0007's gate fires on every later edit, not only on first arrival: a modify whose
    post-image drops `authority:` and `trigger:` from a note already in curated space is refused, and
    the note keeps what it held. Red if only creates were judged — the one-line diff that strips a
    note's provenance is the easiest way past a gate that watches arrivals alone."""
    path = "10-areas/x.md"
    vault.notes[path] = note("body\n")
    publish(broker, streams, chunk_of(*modifying(path, note("body\n"), "---\ntype: note\n---\nbody\n")))

    assert processor.run(config_for(broker, vault, streams)) == 1

    assert vault.written(path) == note("body\n")
    [(_, headers)] = dead_letters(broker, streams)
    assert headers["Obsidian-Batch-Detail"] == f"{path}: provenance_missing (authority), provenance_missing (trigger)"


def test_a_rename_into_curated_space_is_judged_and_its_source_kept_when_refused(
    broker: str, vault: FakeVault, streams: str
) -> None:
    """A rename is a create of the new path plus a delete of the old, so a bare capture renamed out
    of the inbox into curated space is judged as the create it is — and when it is refused, the
    delete is withheld with it. Red on the curated path means a rename is a way around the gate;
    red on the inbox path means the refusal destroyed the only copy."""
    capture = "a bare capture\n"
    vault.notes["00-inbox/capture.md"] = capture
    publish(broker, streams, chunk_of(*renaming("00-inbox/capture.md", "10-areas/capture.md", capture)))

    assert processor.run(config_for(broker, vault, streams)) == 1

    assert vault.written("10-areas/capture.md") is None
    assert vault.written("00-inbox/capture.md") == capture
    [(_, headers)] = dead_letters(broker, streams)
    assert headers["Obsidian-Batch-Detail"] == "10-areas/capture.md: frontmatter_unparseable"


def test_writes_outside_curated_space_and_curated_deletes_are_never_judged(
    broker: str, vault: FakeVault, streams: str
) -> None:
    """The gate's edges. A bare note into the raw layer (exempt, ADR-0015) and into the inbox (the
    agent zone, detective only) applies, and so does deleting a curated note that would itself fail
    the bar — a delete carries no content to judge, and removing a note adds nothing to curated
    space. Red on any of the three means the gate is judging where it has no authority, which in the
    raw layer is exactly the mass refusal ADR-0015's exemption exists to prevent."""
    legacy = "a curated note with no frontmatter\n"
    vault.notes["10-areas/legacy.md"] = legacy
    chunk = one_chunk(
        creating("05-raw/import.md", "imported as is\n"),
        creating("00-inbox/capture.md", "a capture\n"),
        deleting("10-areas/legacy.md", legacy),
    )
    publish(broker, streams, chunk)

    assert processor.run(config_for(broker, vault, streams)) == 0

    assert vault.written("05-raw/import.md") == "imported as is\n"
    assert vault.written("00-inbox/capture.md") == "a capture\n"
    assert vault.written("10-areas/legacy.md") is None
    assert dead_letters(broker, streams) == []


def test_a_refused_note_parks_what_links_to_it_and_the_fixed_batch_converges(
    broker: str, vault: FakeVault, streams: str
) -> None:
    """A refusal is a failed chunk like any other, so ADR-0022's parking follows from it with no
    code of its own: the chunk linking to the refused note is parked, the one linking to *that*
    applies, and the rest of the batch is untouched. Then the producer fixes the note and republishes
    the whole batch, and the vault converges — a pause, not a loss.

    Red on `direct.md` after the first run means a note now links to a page admission refused, which
    in curated space is a dead link written on purpose. Red on the second run means a refusal left
    residue a regenerated batch cannot get past."""

    def batch(refused_body: str) -> tuple[Chunk, ...]:
        batch_id = uuid.uuid4().hex
        bodies = {
            "10-areas/base.md": note("base\n"),
            "10-areas/refused.md": refused_body,
            "10-areas/direct.md": note("see [[refused]]\n"),
            "10-areas/second-hop.md": note("see [[direct]]\n"),
        }
        return tuple(
            chunk_of(*creating(path, body), batch_id=batch_id, index=index, count=len(bodies))
            for index, (path, body) in enumerate(bodies.items())
        )

    publish(broker, streams, *batch("see [[base]]\n"))
    assert processor.run(config_for(broker, vault, streams)) == 1

    assert vault.written("10-areas/base.md") == note("base\n")
    assert vault.written("10-areas/refused.md") is None
    assert vault.written("10-areas/direct.md") is None
    assert vault.written("10-areas/second-hop.md") == note("see [[direct]]\n")
    reasons = [headers["Obsidian-Batch-Reason"] for _, headers in dead_letters(broker, streams)]
    assert reasons == ["admission_refused", "depends_on_failed_chunk"]

    vault.calls.clear()
    publish(broker, streams, *batch(note("see [[base]]\n")))
    assert processor.run(config_for(broker, vault, streams)) == 0

    assert vault.written("10-areas/refused.md") == note("see [[base]]\n")
    assert vault.written("10-areas/direct.md") == note("see [[refused]]\n")
    assert [path for tool, path in vault.calls if tool == TOOL_WRITE] == ["10-areas/refused.md", "10-areas/direct.md"]


def test_a_chunk_whose_refusable_note_is_already_in_the_vault_is_settled_not_refused(
    broker: str, vault: FakeVault, streams: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Admission is asked after "is this work already done?", as every refusal is (ADR-0022). The
    vault already holds, byte for byte, a curated note the bar refuses — it arrived by a path this
    gate never saw — and a chunk creating exactly that note is settled: no write, no dead-letter,
    exit 0.

    Nothing reaches curated space this way: the chunk writes nothing, and a refused chunk writes
    nothing either, so its own redelivery can never be what put such bytes there; the note itself is
    the lint pass's to report. Red means admission had moved ahead of the settle question, and a
    re-run of a batch would park work the vault holds whatever this run decides."""
    present = "a note that bypassed the gate\n"
    vault.notes["10-areas/present.md"] = present
    publish(broker, streams, chunk_of(*creating("10-areas/present.md", present)))

    with caplog.at_level(logging.INFO):
        assert processor.run(config_for(broker, vault, streams)) == 0

    assert [call for call in vault.calls if call[0] == TOOL_WRITE] == []
    assert dead_letters(broker, streams) == []
    assert getattr(the_run_summary(caplog), "already_applied", None) == 1


# --- redelivery ------------------------------------------------------------------------------------


def test_a_transient_write_failure_is_redelivered_and_then_applies(broker: str, vault: FakeVault, streams: str) -> None:
    """Red if a 503 dead-lettered the chunk: an ordinary MCP rollout would park every chunk in
    flight, and an operator would have to re-enqueue work that nothing was actually wrong with."""
    vault.unavailable_writes = 1
    publish(broker, streams, chunk_of(*creating("10-areas/x.md", note("one\n"))))

    processor.run(config_for(broker, vault, streams, max_deliver=3))

    assert vault.written("10-areas/x.md") == note("one\n")
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
        chunk_of(*creating("10-areas/x.md", note("one\n"))),
        chunk_of(*modifying("10-areas/x.md", note("one\n"), note("two\n"))),
    )

    processor.run(config_for(broker, vault, streams, max_deliver=3))

    assert vault.written("10-areas/x.md") == note("two\n")
    assert dead_letters(broker, streams) == []


def test_a_chunk_that_keeps_failing_is_dead_lettered_rather_than_redelivered_forever(
    broker: str, vault: FakeVault, streams: str
) -> None:
    """ADR-0020 records that JetStream's dead-letter path needs building; this is that path firing.
    Red if it were absent — the consumer's `max_ack_pending=1` means one unbounded chunk blocks the
    whole FIFO stream behind it, so "redelivered forever" is also "nothing else ever runs"."""
    vault.unavailable_writes = 99
    publish(broker, streams, chunk_of(*creating("10-areas/x.md", note("one\n"))))

    processor.run(config_for(broker, vault, streams, max_deliver=2))

    parked = dead_letters(broker, streams)
    assert len(parked) == 1
    assert parked[0][1]["Obsidian-Batch-Reason"] == "redelivery_exhausted"
    assert parked[0][1]["Obsidian-Batch-Delivery-Count"] == "2"


def test_a_duplicate_delivery_never_applies_its_writes_twice(broker: str, vault: FakeVault, streams: str) -> None:
    """The idempotence oracle, and the one property redelivery actually gives: applying a chunk a
    second time cannot change the vault, because the pre-flight measures against the first
    application's own output. Red if the second copy applied — a redelivered chunk would overwrite
    whatever had happened in between, which is the silent lost update ADR-0048 exists to prevent.

    The copy is *acked*, not parked, and the exit code is the visible half of that: its target holds
    exactly what it would have written, so the work is done rather than refused. Red on either —
    a dead-letter or a non-zero exit — and a producer re-run reports a failed import of work that is
    entirely present, then parks everything the duplicates were supposed to have blocked.
    """
    chunk = chunk_of(*creating("10-areas/x.md", note("one\n")))
    publish(broker, streams, chunk, chunk)

    assert processor.run(config_for(broker, vault, streams)) == 0

    assert vault.written("10-areas/x.md") == note("one\n")
    assert [call for call in vault.calls if call[0] == TOOL_WRITE] == [(TOOL_WRITE, "10-areas/x.md")]
    assert dead_letters(broker, streams) == []


def test_a_chunk_that_failed_part_way_through_is_parked_rather_than_replayed(
    broker: str, vault: FakeVault, streams: str
) -> None:
    """ADR-0048's stated consequence, injected: the second write of a two-file chunk is refused, so
    the chunk half-applied. Red if it were nak'd instead — its redelivery would meet content its own
    earlier write moved, and replaying it is the double-apply the whole-chunk pre-flight exists to
    make impossible."""
    first_patch, first_targets = creating("10-areas/a.md", note("a\n"))
    second_patch, second_targets = creating("10-areas/b.md", note("b\n"))
    vault.fail_after_writes = 1
    publish(broker, streams, chunk_of(first_patch + second_patch, first_targets + second_targets))

    processor.run(config_for(broker, vault, streams))

    assert vault.written("10-areas/a.md") == note("a\n")
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
    vault.notes["10-areas/x.md"] = note("moved on\n")
    publish(broker, streams, chunk_of(*modifying("10-areas/x.md", note("one\n"), note("two\n"))))
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
    yielding forever, so an undrained promotion stream cannot hold the agent instance stopped.
    """
    fill_promotion(broker, streams, 3)
    publish(broker, streams, chunk_of(*creating("10-areas/x.md", note("one\n"))))

    processor.run(config_for(broker, vault, streams, promotion=True, max_consecutive_yields=1))

    assert vault.calls == []
    assert vault.agent_replicas == 1


def test_the_batch_proceeds_once_the_promotion_stream_has_drained(broker: str, vault: FakeVault, streams: str) -> None:
    """The control for the test above: red if the yield were unconditional, which would make the
    previous test pass for the wrong reason and stop batches running at all."""
    publish(broker, streams, chunk_of(*creating("10-areas/x.md", note("one\n"))))

    processor.run(config_for(broker, vault, streams, promotion=True, max_consecutive_yields=1))

    assert vault.written("10-areas/x.md") == note("one\n")
