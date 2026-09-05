"""The one seam onto JetStream: take chunks off the batch stream, settle them, and dead-letter the
ones that will never apply.

`asyncio` is confined to this module and the coroutine `processor.py` hands to `asyncio.run` —
nothing pure in this package is a coroutine.

## Strict FIFO is `max_ack_pending=1`, and that is not a throughput knob

ADR-0022 chose one unsharded FIFO stream so a producer can express dependency by ordering: rename in
chunk N, relink in chunk N+1. The stream delivers in order, but a consumer permitted more than one
outstanding message would still *apply* out of order the moment one chunk is nak'd and redelivered
while a later one is already in flight. With one outstanding message the broker cannot deliver the
next chunk until this one is settled, so a redelivery is retried before anything after it — which is
what makes "the stream's order is the apply order" true rather than merely likely. It also happens
to be the cap on in-flight requests ADR-0022 asks for, from the same setting.

`ack_wait` is the other half. A chunk held longer than it — through a long yield, or a slow MCP
call — is redelivered while still being worked on, which turns patience into duplicate delivery. The
run therefore takes its fairness yield *before* fetching (see `fairness.py`), never while holding a
message.

## The dead-letter path, built rather than configured

ADR-0020 records that JetStream's dead-lettering is advisory-based and needs deliberate
construction. This is that construction, and it is deliberately not an advisory consumer: the
processor already knows, at the moment it gives up, both why it gave up and what the chunk was.
`dead_letter` republishes the chunk's own bytes to a separate top-level subject with the reason in
the message headers, and the caller then `term`s the original so the broker stops redelivering it.
Terminate is what makes it a path instead of a loop; the republished copy is what makes it a path
instead of a deletion.

## Where the promotion depth comes from

`num_pending` on the promotion stream's own consumer — a queue-native fact, not an inference from
anything this processor can observe about the MCP surface. ADR-0020 makes those per-stream numbers
acceptance criteria on A1 for the same reason: nothing an envelope parser sees can substitute.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import nats.errors
import nats.js.errors
from nats.aio.client import Client
from nats.aio.msg import Msg
from nats.js import JetStreamContext
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy, ReplayPolicy

logger = logging.getLogger(__name__)


class BatchConsumerError(RuntimeError):
    """Any failure of this seam."""


class BatchConnectionError(BatchConsumerError):
    """The broker could not be reached, or refused the credential."""


@dataclass(frozen=True, slots=True)
class DeliveredChunk:
    """One message off the batch stream, with the two broker facts the run decides with."""

    message: Msg
    body: bytes
    subject: str
    stream_sequence: int
    delivery_count: int
    """How many times the broker has delivered this message, this delivery included."""


class BatchConsumer:
    """A connected pull consumer on the batch stream, for one run."""

    def __init__(
        self,
        *,
        servers: str,
        user: str,
        password: str,
        inbox_prefix: str,
        stream: str,
        durable: str,
        filter_subject: str,
        max_deliver: int,
        ack_wait_seconds: float,
        connect_timeout_seconds: float,
        promotion_stream: str | None,
        promotion_consumer: str | None,
    ) -> None:
        self._servers = servers
        self._user = user
        self._password = password
        self._inbox_prefix = inbox_prefix
        self._stream = stream
        self._durable = durable
        self._filter_subject = filter_subject
        self._max_deliver = max_deliver
        self._ack_wait_seconds = ack_wait_seconds
        self._connect_timeout_seconds = connect_timeout_seconds
        self._promotion_stream = promotion_stream
        self._promotion_consumer = promotion_consumer
        self._client: Client | None = None
        self._jetstream: JetStreamContext | None = None
        self._subscription: JetStreamContext.PullSubscription | None = None

    async def connect(self) -> None:
        # `Client().connect(...)` rather than the `nats.connect(...)` shorthand, for the reason the
        # producer's seam gives: the shorthand takes untyped `**kwargs`, so a misspelled option is
        # accepted silently and simply not applied.
        client = Client()
        try:
            await client.connect(
                servers=self._servers,
                user=self._user,
                password=self._password,
                inbox_prefix=self._inbox_prefix,
                connect_timeout=int(self._connect_timeout_seconds),
            )
        except nats.errors.AuthorizationError as exc:
            raise BatchConnectionError(f"the broker refused this credential at connect: {exc}") from exc
        except (nats.errors.Error, OSError) as exc:
            raise BatchConnectionError(f"could not connect to {self._servers}: {exc}") from exc

        self._client = client
        self._jetstream = JetStreamContext(client)
        config = ConsumerConfig(
            durable_name=self._durable,
            filter_subject=self._filter_subject,
            ack_policy=AckPolicy.EXPLICIT,
            deliver_policy=DeliverPolicy.ALL,
            replay_policy=ReplayPolicy.INSTANT,
            # See the module docstring: this is what makes the stream's order the apply order.
            max_ack_pending=1,
            max_deliver=self._max_deliver,
            ack_wait=self._ack_wait_seconds,
        )
        try:
            # `add_consumer` takes `**params` alongside its typed `config`, so its whole signature
            # reads as partially unknown to a strict type checker. Everything this seam passes is
            # in the typed `ConsumerConfig`; nothing rides on the untyped half.
            await self._jetstream.add_consumer(self._stream, config)  # type: ignore[reportUnknownMemberType]
            self._subscription = await self._jetstream.pull_subscribe_bind(self._durable, stream=self._stream)
        except (nats.errors.Error, nats.js.errors.Error) as exc:
            raise BatchConsumerError(f"could not bind a consumer on stream {self._stream!r}: {exc}") from exc
        logger.info(
            "consuming the batch stream",
            extra={
                "event": "batch_consumer_ready",
                "stream": self._stream,
                "durable": self._durable,
                "max_deliver": self._max_deliver,
            },
        )

    async def next_chunk(self, timeout_seconds: float) -> DeliveredChunk | None:
        """The next chunk, or `None` when nothing arrived within `timeout_seconds`.

        An empty fetch is the run's own completion signal — the stream has nothing left, so a run
        triggered on a schedule against an empty stream costs one fetch and exits.
        """
        subscription = self._require_subscription()
        try:
            messages = await subscription.fetch(1, timeout=timeout_seconds)
        except TimeoutError, nats.errors.TimeoutError:
            return None
        if not messages:
            return None
        message = messages[0]
        metadata = message.metadata
        return DeliveredChunk(
            message=message,
            body=message.data,
            subject=message.subject,
            stream_sequence=metadata.sequence.stream,
            delivery_count=metadata.num_delivered,
        )

    async def ack(self, chunk: DeliveredChunk) -> None:
        await chunk.message.ack()

    async def nak(self, chunk: DeliveredChunk, delay_seconds: float) -> None:
        """Give the chunk back for redelivery after `delay_seconds`.

        The delay is the broker's, not a local sleep: a local sleep would hold the message past its
        ack deadline and have it redelivered anyway, at which point the wait bought nothing.
        """
        await chunk.message.nak(delay=delay_seconds)

    async def term(self, chunk: DeliveredChunk) -> None:
        """Settle the chunk permanently — no redelivery, ever. Always paired with `dead_letter`."""
        await chunk.message.term()

    async def dead_letter(self, subject: str, chunk: DeliveredChunk, reason: str, detail: str) -> int:
        """Republish the chunk's own bytes to the dead-letter subject and return its sequence.

        The bytes are republished unchanged so a dead-lettered chunk is still a decodable chunk: an
        operator can read it with the same `decode_chunk` the processor uses, and a re-enqueue is a
        copy rather than a reconstruction. Why it died travels in the headers, where it does not
        alter the payload's hash or its format.
        """
        jetstream = self._require_jetstream()
        try:
            ack = await jetstream.publish(
                subject,
                chunk.body,
                headers={
                    "Obsidian-Batch-Reason": reason,
                    "Obsidian-Batch-Detail": detail[:1024],
                    "Obsidian-Batch-Origin-Subject": chunk.subject,
                    "Obsidian-Batch-Delivery-Count": str(chunk.delivery_count),
                },
            )
        except (nats.errors.Error, nats.js.errors.Error) as exc:
            # Loud rather than swallowed: a chunk terminated without its dead-letter copy landing is
            # work that has silently disappeared, which is the one outcome this path exists to
            # prevent. The caller must not terminate the message if this raises.
            raise BatchConsumerError(f"could not dead-letter to {subject!r}: {exc}") from exc
        logger.warning(
            "chunk dead-lettered",
            extra={
                "event": "chunk_dead_lettered",
                "subject": subject,
                "reason": reason,
                "detail": detail,
                "origin_subject": chunk.subject,
                "stream_sequence": chunk.stream_sequence,
                "delivery_count": chunk.delivery_count,
            },
        )
        return ack.seq

    async def promotion_depth(self) -> int | None:
        """How many messages are waiting on the promotion stream, or `None` when no promotion
        stream is configured — which is a declared state, not a failure. A configured stream that
        cannot be read *is* a failure and raises: proceeding blind is exactly the starvation the
        fairness rule exists to prevent."""
        if self._promotion_stream is None or self._promotion_consumer is None:
            return None
        jetstream = self._require_jetstream()
        try:
            info = await jetstream.consumer_info(self._promotion_stream, self._promotion_consumer)
        except (nats.errors.Error, nats.js.errors.Error) as exc:
            raise BatchConsumerError(
                f"could not read the depth of {self._promotion_stream}/{self._promotion_consumer}: {exc}"
            ) from exc
        return info.num_pending

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
            self._jetstream = None
            self._subscription = None

    def _require_jetstream(self) -> JetStreamContext:
        if self._jetstream is None:
            raise BatchConsumerError("the consumer was used before connect")
        return self._jetstream

    def _require_subscription(self) -> JetStreamContext.PullSubscription:
        if self._subscription is None:
            raise BatchConsumerError("the consumer was used before connect")
        return self._subscription

    async def __aenter__(self) -> BatchConsumer:
        await self.connect()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.close()
