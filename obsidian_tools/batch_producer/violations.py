"""Classifying what arrived on the NATS connection's error callback. Pure — no client, no sockets.

Split out and kept pure because of the single most misleading measured behaviour in this component:
**a permissions violation never reaches the publish call.** `js.publish()` raises a timeout, and the
violation itself arrives asynchronously on the connection's `error_cb` — so code that only inspects
the publish exception reports "broker unreachable" for "not authorised", and retries forever a
message it will never be permitted to enqueue. `nats_client.py` correlates the two; this module
decides what the correlated evidence means, from strings alone, so the decision is table-testable
against the exact text a real broker produced rather than only reachable through one.

Two measured facts shape the matching, and both are traps:

- **The client lowercases the whole error payload.** A subject is therefore not recoverable from
  the text with its case intact, so nothing here parses one out — classification is by the kind of
  operation refused, never by which subject it named.
- **A refused reply inbox and a refused publish are different failures with the same symptom.** The
  batch credential is granted subscribe on `_INBOX_BATCH.*.*` only, so a client left on the
  library's default `_INBOX` prefix has its reply inbox refused; a JetStream publish is a
  request/reply, so the acknowledgement then has nowhere to land and the publish times out
  identically to an unreachable broker. Distinguishing the two is the difference between "fix the
  credential's subject grant" and "fix `inbox_prefix` in this producer's configuration".
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum, auto

_PERMISSIONS_VIOLATION = "permissions violation"
_FOR_SUBSCRIPTION = "for subscription"
_FOR_PUBLISH = "for publish"


class ViolationKind(Enum):
    """What the error callback's accumulated messages amount to."""

    NONE = auto()
    """Nothing on the callback names a permissions violation — the failure is something else."""

    SUBSCRIPTION = auto()
    """The reply inbox was refused: `inbox_prefix` and the credential's subscribe grant disagree."""

    PUBLISH = auto()
    """The publish subject was refused: the credential may not enqueue here."""


def classify_violations(messages: Sequence[str]) -> ViolationKind:
    """What the error callback's messages say about why a publish did not complete.

    A subscription violation outranks a publish violation when both are present: the reply channel
    is what carries the acknowledgement, so with it refused the publish never had a verdict to
    report, and pointing an operator at the subject grant would send them to the wrong file.
    """
    lowered = [message.lower() for message in messages]
    violations = [message for message in lowered if _PERMISSIONS_VIOLATION in message]
    if any(_FOR_SUBSCRIPTION in message for message in violations):
        return ViolationKind.SUBSCRIPTION
    if any(_FOR_PUBLISH in message for message in violations):
        return ViolationKind.PUBLISH
    return ViolationKind.NONE
