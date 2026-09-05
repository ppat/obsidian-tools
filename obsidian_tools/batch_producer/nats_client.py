"""The one seam that talks to NATS. Every publish crosses `BatchPublisher`, for the reason
`GitRunner` exists (ADR-0046): the disciplines below are enforced once here rather than remembered
at each call site.

`asyncio` is confined to this module and the coroutine `producer.py` hands to `asyncio.run` —
nothing pure in this package is a coroutine.

## The four disciplines, each measured against a real broker

1. **A permissions violation never reaches the publish call.** `js.publish()` raises a timeout; the
   violation itself arrives asynchronously on the connection's error callback. Code that inspects
   only the publish exception reports "broker unreachable" for "not authorised" — and then retries,
   forever, a message it will never be permitted to enqueue. `publish_chunk` correlates the two and
   raises a distinct, named failure for each. This is the single most expensive thing to get wrong
   here, because both wrong answers look like an outage.

2. **`js.publish()`, never core `nc.publish()`.** Core publish returns without raising whether or
   not the message was refused, so it confirms nothing. Only the JetStream publish waits for a
   `PubAck`, and only the sequence in that ack is evidence the chunk is on the stream. This class
   exposes no other way to send, which is what makes "never core publish" a property rather than a
   review note.

3. **`inbox_prefix` is configuration that must match the credential's subscribe grant.** A
   JetStream publish is a request/reply: the client opens one reply inbox per connection and the
   acknowledgement returns on it. The batch credential is granted subscribe on `_INBOX_BATCH.*.*`
   and nothing else (ADR-0047), so a client left on the library's default `_INBOX` has its reply
   inbox *refused* — measured — and the publish then times out with nowhere for the ack to land.
   The coupling fails closed, which is right, but its symptom is a timeout that reads as "broker
   down"; `BatchInboxGrantError` is what turns that back into "these two settings disagree".

4. **Reconnection is bounded.** With `allow_reconnect` on, a client whose credential is wrong or
   rotated retries on a timer; the library's default of 60 attempts is a long, quiet spin rather
   than a failure an operator sees. Bounded low, a bad credential fails loudly and soon, which is
   the whole point of a producer that runs to completion and exits.

Never parse a subject out of an error string: the client lowercases the entire error payload, so a
subject recovered from it has lost its case. `violations.py` classifies by the *kind* of operation
refused, and is pure so that classification is testable against the exact strings a real broker
produced.
"""

from __future__ import annotations

import asyncio
import logging

import nats.errors
import nats.js.errors
from nats.aio.client import Client
from nats.js import JetStreamContext

from obsidian_tools.batch_producer.violations import ViolationKind, classify_violations

logger = logging.getLogger(__name__)

# How long `publish_chunk` waits, after a publish has already failed, for the error callback to
# deliver the violation that explains it. Not a guess about network latency: the server sends its
# `-ERR` promptly and the callback usually fires well before the publish's own timeout expires, so
# this only covers the race in which the two arrive the other way round. Bounded and small because
# it is paid once per failure, never on the success path.
_VIOLATION_SETTLE_SECONDS = 0.5

# `js.publish()` fails differently depending on whether the server answered at all, and every one of
# these can be the shape a permissions refusal takes. They are caught together and separated by what
# the error callback recorded, never by which exception arrived.
_PUBLISH_FAILURES = (
    nats.errors.TimeoutError,
    nats.errors.NoRespondersError,
    nats.js.errors.NoStreamResponseError,
    nats.js.errors.ServiceUnavailableError,
    asyncio.TimeoutError,
)


class BatchPublisherError(RuntimeError):
    """Any failure of this seam."""


class BatchConnectionError(BatchPublisherError):
    """The broker could not be reached, or refused the credential outright at connect time."""


class BatchAuthorizationError(BatchPublisherError):
    """The credential is not permitted to publish to this subject. Never retried: the grant is
    static server configuration, so the same message would be refused identically forever."""


class BatchInboxGrantError(BatchPublisherError):
    """The reply inbox was refused, so no acknowledgement could return. `inbox_prefix` here and the
    credential's subscribe grant on the broker disagree — see discipline 3 in the module docstring."""


class BatchPublishTimeoutError(BatchPublisherError):
    """The publish went unanswered and nothing on the error callback explains why. This is the
    failure the other three exist to *not* be mistaken for: it is the genuine "broker unreachable"."""


class BatchPublisher:
    """A connected JetStream publisher for one batch run."""

    def __init__(
        self,
        *,
        servers: str,
        user: str,
        password: str,
        inbox_prefix: str,
        connect_timeout_seconds: float,
        publish_timeout_seconds: float,
        max_reconnect_attempts: int,
        violation_settle_seconds: float = _VIOLATION_SETTLE_SECONDS,
    ) -> None:
        self._servers = servers
        self._user = user
        self._password = password
        self._inbox_prefix = inbox_prefix
        self._connect_timeout_seconds = connect_timeout_seconds
        self._publish_timeout_seconds = publish_timeout_seconds
        self._max_reconnect_attempts = max_reconnect_attempts
        self._violation_settle_seconds = violation_settle_seconds
        self._jetstream: JetStreamContext | None = None
        self._client: Client | None = None
        self._errors: list[str] = []

    async def _on_error(self, error: Exception) -> None:
        """The connection's error callback: where a permissions violation actually arrives."""
        message = str(error)
        self._errors.append(message)
        logger.warning("nats connection reported an error", extra={"event": "nats_error", "error": message})

    async def connect(self) -> None:
        # `Client().connect(...)` rather than the `nats.connect(...)` shorthand: the shorthand takes
        # its options as untyped `**kwargs`, so a misspelled option name would be accepted silently
        # and simply not applied -- and two of the options below (`inbox_prefix`,
        # `max_reconnect_attempts`) are the ones whose absence this module exists to prevent.
        client = Client()
        try:
            await client.connect(
                servers=self._servers,
                user=self._user,
                password=self._password,
                error_cb=self._on_error,
                inbox_prefix=self._inbox_prefix,
                connect_timeout=int(self._connect_timeout_seconds),
                max_reconnect_attempts=self._max_reconnect_attempts,
            )
        except nats.errors.AuthorizationError as exc:
            raise BatchAuthorizationError(f"the broker refused this credential at connect: {exc}") from exc
        except (nats.errors.Error, OSError) as exc:
            raise BatchConnectionError(f"could not connect to {self._servers}: {exc}") from exc
        self._client = client
        # Constructing the context does not reach the JetStream API subject space, and this producer
        # is deliberately granted no access to it (ADR-0047: an acknowledged publish never touches
        # `$JS.API`, and granting it would hand every producer stream creation, deletion and the
        # power to widen its own stream's subject list). The `prefix` default is left alone because
        # nothing here ever sends to it -- only `publish` is used.
        self._jetstream = JetStreamContext(client)
        logger.info(
            "connected to the batch stream broker",
            extra={"event": "nats_connected", "servers": self._servers, "inbox_prefix": self._inbox_prefix},
        )

    async def publish_chunk(self, subject: str, payload: bytes) -> int:
        """Publish one chunk and return the stream sequence its `PubAck` reports.

        Returns only on an acknowledged publish, so a caller that gets a sequence back has evidence
        the chunk is on the stream — the property that lets `producer.py` treat "published" as a
        fact rather than an assumption.
        """
        jetstream = self._require_jetstream()
        try:
            ack = await jetstream.publish(subject, payload, timeout=self._publish_timeout_seconds)
        except _PUBLISH_FAILURES as exc:
            raise await self._explain_publish_failure(subject, exc) from exc
        return ack.seq

    async def _explain_publish_failure(self, subject: str, exc: Exception) -> BatchPublisherError:
        """Turn a bare publish failure into the named failure the error callback says it really is."""
        await asyncio.sleep(self._violation_settle_seconds)
        kind = classify_violations(self._errors)
        self._errors.clear()
        if kind is ViolationKind.SUBSCRIPTION:
            return BatchInboxGrantError(
                f"the broker refused this client's reply inbox under prefix {self._inbox_prefix!r}, so the "
                f"acknowledgement for {subject!r} had nowhere to land: inbox_prefix and the credential's "
                f"subscribe grant disagree ({exc})"
            )
        if kind is ViolationKind.PUBLISH:
            return BatchAuthorizationError(
                f"this credential is not permitted to publish to {subject!r}; the broker refused it ({exc})"
            )
        return BatchPublishTimeoutError(f"publishing to {subject!r} went unanswered: {exc}")

    def _require_jetstream(self) -> JetStreamContext:
        if self._jetstream is None:
            raise BatchPublisherError("publish attempted before connect")
        return self._jetstream

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
            self._jetstream = None

    async def __aenter__(self) -> BatchPublisher:
        await self.connect()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.close()
