"""Tests for `obsidian_tools/batch_producer/nats_client.py` — against a real `nats-server` in a
container, never a mock.

Everything this seam exists for is a property of the broker's own behaviour: that a permissions
violation arrives on the error callback rather than at the publish call, that a JetStream publish
needs a permitted reply inbox, and that a refusal and an outage are indistinguishable until the two
are correlated. A mocked client would agree with whatever this module already believes about all
three, which is exactly the failure mode this repository's testing rule names.

The account topology below is ADR-0047's, cut down to what these tests decide: one account holding
the stream, one producer account importing `batch.>` as a *service* (so the PubAck can cross back),
and per-user publish allow-lists — the layer ADR-0047 records as load-bearing precisely because the
account boundary alone refuses silently.

Two users, differing only in their publish grant, are what make the authorisation test an injection
rather than an assertion: `narrow-producer` is a legitimately held, correctly-connected credential
publishing outside its own subject grant.

Set `OBSIDIAN_TOOLS_TEST_NATS_HOST`/`_PORT` when the Docker daemon is not local (a remote daemon
publishes the port on its own host, not on this one).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from nats_harness import HOST as _HOST
from nats_harness import running_broker

from obsidian_tools.batch_producer.chunk import Chunk, chunk_subject, decode_chunk, encode_chunk
from obsidian_tools.batch_producer.nats_client import (
    BatchAuthorizationError,
    BatchConnectionError,
    BatchInboxGrantError,
    BatchPublisher,
    BatchPublisherError,
    BatchPublishTimeoutError,
)
from obsidian_tools.batch_producer.producer import EXIT_OK
from obsidian_tools.batch_producer.staleness import ChunkTarget, TargetOperation
from obsidian_tools.commands import enqueue_batch
from obsidian_tools.config import BatchProducerConfig

_PORT = int(os.environ.get("OBSIDIAN_TOOLS_TEST_NATS_PORT", "14222"))
_INBOX = "_INBOX_BATCH"

_SERVER_CONFIG = """
port: 4222
jetstream: { store_dir: "/tmp/js" }

accounts {
  STREAMS: {
    jetstream: enabled
    users: [ { user: "streams", password: "streams-pw" } ]
    exports: [ { service: "batch.>", accounts: [BATCH] } ]
  }
  BATCH: {
    users: [
      {
        user: "batch-producer", password: "producer-pw",
        permissions: {
          publish:   { allow: ["batch.>"] }
          subscribe: { allow: ["_INBOX_BATCH.*.*"] }
        }
      },
      {
        user: "narrow-producer", password: "narrow-pw",
        permissions: {
          publish:   { allow: ["batch.permitted.>"] }
          subscribe: { allow: ["_INBOX_BATCH.*.*"] }
        }
      }
    ]
    imports: [ { service: { account: STREAMS, subject: "batch.>" } } ]
  }
}
"""


@pytest.fixture(scope="module")
def broker() -> Iterator[str]:
    """A real `nats-server` with JetStream and the account topology above, torn down afterwards.
    The container lifecycle lives in `nats_harness.py`, shared with the consumer's own broker
    tests; only the topology and the stream below are this module's."""
    with running_broker(_SERVER_CONFIG, _PORT) as url:
        asyncio.run(_create_stream())
        yield url


async def _create_stream() -> None:
    """Created through the streams account, which is the only one holding the JetStream API grant —
    no producer is given it (ADR-0047), so a producer could not create this even for a test."""
    from nats.aio.client import Client
    from nats.js import JetStreamContext
    from nats.js.api import StreamConfig

    client = Client()
    await client.connect(servers=f"nats://{_HOST}:{_PORT}", user="streams", password="streams-pw")
    try:
        # `add_stream` takes `**params` alongside its typed `config`, so the whole signature reads as
        # partially unknown to a strict type checker. Nothing the producer itself calls is untyped —
        # `nats_client.py` deliberately avoids the untyped shorthands (see its `connect`).
        await JetStreamContext(client).add_stream(StreamConfig(name="batch", subjects=["batch.>"]))  # type: ignore[reportUnknownMemberType]
    finally:
        await client.close()


async def _read_stream(subject: str, count: int) -> list[bytes]:
    """The messages on one subject, in stream order, read through the streams account.

    Filtered by subject rather than reading `batch.>`: the broker is shared by every test in this
    module, so an unfiltered read returns whatever earlier tests left on the stream. Each test
    generates its own batch id, and the batch id *is* the subject's trailing token, so filtering on
    the subject is what makes each test read only its own messages."""
    from nats.aio.client import Client
    from nats.js import JetStreamContext

    client = Client()
    await client.connect(servers=f"nats://{_HOST}:{_PORT}", user="streams", password="streams-pw")
    try:
        subscription = await JetStreamContext(client).pull_subscribe(subject, durable=f"r{uuid.uuid4().hex[:8]}")
        messages = await subscription.fetch(count, timeout=5)
        bodies = [message.data for message in messages]
        for message in messages:
            await message.ack()
        return bodies
    finally:
        await client.close()


def publisher(*, user: str = "batch-producer", password: str = "producer-pw", inbox: str = _INBOX) -> BatchPublisher:
    return BatchPublisher(
        servers=f"nats://{_HOST}:{_PORT}",
        user=user,
        password=password,
        inbox_prefix=inbox,
        connect_timeout_seconds=5.0,
        publish_timeout_seconds=3.0,
        max_reconnect_attempts=2,
    )


def a_chunk(batch_id: str, *, index: int = 0, count: int = 1, patch: str = "diff --git a/x.md b/x.md\n") -> Chunk:
    return Chunk(
        batch_id=batch_id,
        chunk_index=index,
        chunk_count=count,
        produced_at="2026-09-05T12:00:00+00:00",
        patch=patch,
        targets=(ChunkTarget(f"05-raw/{batch_id}-{index}.md", TargetOperation.CREATE, None),),
    )


def new_batch_id() -> str:
    return uuid.uuid4().hex


# --- the acknowledged publish ---------------------------------------------------------------------


def test_a_chunk_publishes_and_is_acknowledged(broker: str) -> None:
    """The positive control the three refusals below are measured against. Red if a publish ever
    returns without a `PubAck` — the producer would then treat "sent" as "enqueued", which is
    precisely what core `nc.publish()` would give it and why this seam exposes only `js.publish()`."""

    async def scenario() -> int:
        batch_id = new_batch_id()
        async with publisher() as client:
            return await client.publish_chunk(chunk_subject("batch", batch_id), encode_chunk(a_chunk(batch_id)))

    assert asyncio.run(scenario()) >= 1


def test_the_published_bytes_decode_back_to_the_same_chunk(broker: str) -> None:
    """The wire contract, proven across a real broker rather than only in memory. Red if anything
    between `encode_chunk` and what a consumer reads off the stream alters the payload."""
    batch_id = new_batch_id()
    chunk = a_chunk(batch_id, patch="diff --git a/café.md b/café.md\n+dé\r\n")

    async def scenario() -> list[bytes]:
        subject = chunk_subject("batch", batch_id)
        async with publisher() as client:
            await client.publish_chunk(subject, encode_chunk(chunk))
        return await _read_stream(subject, 1)

    bodies = asyncio.run(scenario())

    assert decode_chunk(bodies[0]) == chunk


def test_chunks_arrive_on_the_stream_in_the_order_they_were_published(broker: str) -> None:
    """ADR-0022's dependency-by-ordering, end to end. Red if the producer ever publishes
    concurrently: two chunks would then be sequenced by whichever acknowledgement raced, and a
    relink could be applied before the rename it depends on."""
    batch_id = new_batch_id()
    chunks = [a_chunk(batch_id, index=index, count=3, patch=f"patch-{index}\n") for index in range(3)]

    async def scenario() -> list[bytes]:
        subject = chunk_subject("batch", batch_id)
        async with publisher() as client:
            for chunk in chunks:
                await client.publish_chunk(subject, encode_chunk(chunk))
        return await _read_stream(subject, 3)

    bodies = asyncio.run(scenario())

    assert [decode_chunk(body).chunk_index for body in bodies] == [0, 1, 2]
    assert [json.loads(body)["patch"] for body in bodies] == ["patch-0\n", "patch-1\n", "patch-2\n"]


def test_the_acknowledged_sequence_increases_with_each_chunk(broker: str) -> None:
    """The acknowledgement carries the stream position, which is what makes "published" a fact.
    Red if the sequence is ever fabricated rather than read from the `PubAck`."""

    async def scenario() -> list[int]:
        batch_id = new_batch_id()
        async with publisher() as client:
            subject = chunk_subject("batch", batch_id)
            return [
                await client.publish_chunk(subject, encode_chunk(a_chunk(batch_id, index=i, count=2))) for i in range(2)
            ]

    first, second = asyncio.run(scenario())
    assert second == first + 1


# --- the three refusals, each injected against a real broker --------------------------------------


def test_a_credential_publishing_outside_its_grant_raises_an_authorization_error(broker: str) -> None:
    """ADR-0047's decisive injection: a legitimately held credential publishing outside its own
    subject grant. The publish itself raises only `nats: timeout` — the violation arrives on the
    error callback — so this goes red the moment that correlation is removed, and the failure
    reverts to a timeout that a caller would retry forever against a permanent refusal."""
    batch_id = new_batch_id()

    async def scenario() -> None:
        async with publisher(user="narrow-producer", password="narrow-pw") as client:
            await client.publish_chunk(chunk_subject("batch", batch_id), encode_chunk(a_chunk(batch_id)))

    with pytest.raises(BatchAuthorizationError):
        asyncio.run(scenario())


def test_the_same_credential_succeeds_inside_its_grant(broker: str) -> None:
    """The control that makes the injection above evidence rather than a coincidence: the identical
    credential, connection and code path, differing only in the subject. Red if `narrow-producer`
    fails everywhere, which would mean the previous test proved nothing about the subject grant."""

    async def scenario() -> int:
        async with publisher(user="narrow-producer", password="narrow-pw") as client:
            batch_id = new_batch_id()
            return await client.publish_chunk(f"batch.permitted.{batch_id}", encode_chunk(a_chunk(batch_id)))

    assert asyncio.run(scenario()) >= 1


def test_the_wrong_inbox_prefix_fails_closed_as_a_named_grant_error(broker: str) -> None:
    """The library's default `_INBOX` against a grant scoped to `_INBOX_BATCH.*.*`. The reply inbox
    is refused, so the acknowledgement has nowhere to land and the publish times out exactly like an
    unreachable broker. Red if this ever surfaces as `BatchPublishTimeoutError`, which would send an
    operator to the network instead of to two settings that disagree."""
    batch_id = new_batch_id()

    async def scenario() -> None:
        async with publisher(inbox="_INBOX") as client:
            await client.publish_chunk(chunk_subject("batch", batch_id), encode_chunk(a_chunk(batch_id)))

    with pytest.raises(BatchInboxGrantError):
        asyncio.run(scenario())


def test_a_wrong_password_fails_at_connect_rather_than_spinning(broker: str) -> None:
    """Bounded reconnection. Red if a rotated credential retries indefinitely instead of failing:
    this producer runs to completion and exits, so an unbounded retry is a job that never finishes
    and never reports why."""

    async def scenario() -> None:
        async with publisher(password="wrong-pw"):
            pass

    started = time.monotonic()
    with pytest.raises((BatchAuthorizationError, BatchConnectionError)):
        asyncio.run(scenario())
    assert time.monotonic() - started < 30


def test_an_unreachable_broker_is_a_connection_error_not_an_authorization_one(broker: str) -> None:
    """The distinction the whole seam exists to preserve, from the other side. Red if a genuine
    outage is ever reported as a credential problem — the run would stop permanently on a condition
    that is temporary."""
    unreachable = BatchPublisher(
        servers="nats://127.0.0.1:1",
        user="batch-producer",
        password="producer-pw",
        inbox_prefix=_INBOX,
        connect_timeout_seconds=2.0,
        publish_timeout_seconds=2.0,
        max_reconnect_attempts=1,
    )

    with pytest.raises(BatchConnectionError):
        asyncio.run(unreachable.connect())


def test_publishing_before_connect_is_refused(broker: str) -> None:
    """Red if a publish before `connect()` raises an `AttributeError` from inside the library rather
    than this seam's own error type."""
    with pytest.raises(BatchPublisherError):
        asyncio.run(publisher().publish_chunk("batch.x", b"{}"))


# --- the whole unit, end to end --------------------------------------------------------------------


def test_a_staged_repository_becomes_chunks_on_the_stream(broker: str, tmp_path: Path) -> None:
    """The whole of B1: a real git repository, staged, through `enqueue-batch`, onto a real stream —
    and read back as chunks carrying the staleness payload ADR-0048 specifies.

    Red on any break anywhere in the chain, which is what makes it worth having alongside the
    per-module tests: each of those holds one seam still while testing another, and none of them
    would notice the two being wired together wrongly.
    """
    scrubbed = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, check=True, env=scrubbed)

    git("init", "-q", "--initial-branch=main")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    (tmp_path / "curated.md").write_bytes(b"original\n")
    git("add", "-A")
    git("commit", "-qm", "seed")
    (tmp_path / "05-raw-note.md").write_bytes(b"imported\n")
    (tmp_path / "curated.md").write_bytes(b"revised\n")
    git("add", "-A")

    subject_prefix = f"batch.e2e{uuid.uuid4().hex[:8]}"
    config = BatchProducerConfig(
        git_dir=str(tmp_path / ".git"),
        work_tree=str(tmp_path),
        base_rev="HEAD",
        nats_url=f"nats://{_HOST}:{_PORT}",
        # `batch-producer`, whose grant is `batch.>` — so the run below is authorised exactly the way
        # the deployed credential is, rather than by a permission this test invented for itself.
        nats_user="batch-producer",
        nats_password="producer-pw",
        nats_inbox_prefix=_INBOX,
        subject_prefix=subject_prefix,
        max_chunk_patch_bytes=262144,
        connect_timeout_seconds=5.0,
        publish_timeout_seconds=5.0,
        max_reconnect_attempts=2,
    )

    assert enqueue_batch.run(config) == EXIT_OK

    bodies = asyncio.run(_read_stream(f"{subject_prefix}.*", 1))
    chunk = decode_chunk(bodies[0])
    assert (chunk.chunk_index, chunk.chunk_count) == (0, 1)
    assert {(t.path, t.operation, t.base_sha256) for t in chunk.targets} == {
        ("05-raw-note.md", TargetOperation.CREATE, None),
        ("curated.md", TargetOperation.MODIFY, hashlib.sha256(b"original\n").hexdigest()),
    }
    assert "+revised" in chunk.patch
    assert "+imported" in chunk.patch


def test_a_publish_timeout_error_type_exists_for_the_unexplained_case(broker: str) -> None:
    """Guards the classifier's default branch, which the three injections above never reach: a
    failure the error callback does not explain must stay retryable rather than being reported as an
    authorisation problem. Red if `BatchPublishTimeoutError` is ever folded into another type."""
    assert issubclass(BatchPublishTimeoutError, BatchPublisherError)
    assert not issubclass(BatchPublishTimeoutError, BatchAuthorizationError)
