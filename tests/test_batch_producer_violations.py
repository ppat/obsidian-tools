"""Tests for `obsidian_tools/batch_producer/violations.py` — what the NATS error callback's
messages mean.

The two multi-line strings below are **verbatim** `str(error)` values captured from a real
`nats-server` 2.14 with the shipped account topology, via `nats-py` 2.15.0's `error_cb`. They are
literals here rather than regenerated, because the point of a pure classifier is that it can be
tested against the exact text a broker produced without needing one — the broker's own behaviour is
proven separately, in `tests/test_batch_producer_nats.py`.
"""

from __future__ import annotations

import pytest

from obsidian_tools.batch_producer.violations import ViolationKind, classify_violations

# Measured: the batch credential connected with the library's default `_INBOX` prefix against a
# grant scoped to `_INBOX_BATCH.*.*`. Note the lowercase `_inbox` — the client lowercases the whole
# error payload, which is why nothing parses a subject out of these strings.
_SUBSCRIPTION_VIOLATION = 'nats: permissions violation for subscription to "_inbox.oldztqigkyqmcxqhxxxog4.*"'

# Measured: a legitimately held credential publishing outside its own subject grant — ADR-0047's
# decisive injection.
_PUBLISH_VIOLATION = 'nats: permissions violation for publish to "batch.aaaa1111"'


def test_a_publish_violation_is_recognised() -> None:
    """Red if a refused publish is ever classified as anything else: the caller would raise a
    timeout, and a caller that sees a timeout retries a message the broker will never accept."""
    assert classify_violations([_PUBLISH_VIOLATION]) is ViolationKind.PUBLISH


def test_a_subscription_violation_is_recognised() -> None:
    """Red if a refused reply inbox is ever classified as anything else. It presents identically to
    an unreachable broker, so this classification is the only thing that sends an operator to the
    `inbox_prefix` setting rather than to the network."""
    assert classify_violations([_SUBSCRIPTION_VIOLATION]) is ViolationKind.SUBSCRIPTION


def test_a_subscription_violation_outranks_a_publish_violation() -> None:
    """With the reply channel refused, the publish never had a verdict to report, so the subject
    grant is not what an operator should be sent to first. Red if the precedence inverts."""
    assert classify_violations([_PUBLISH_VIOLATION, _SUBSCRIPTION_VIOLATION]) is ViolationKind.SUBSCRIPTION
    assert classify_violations([_SUBSCRIPTION_VIOLATION, _PUBLISH_VIOLATION]) is ViolationKind.SUBSCRIPTION


@pytest.mark.parametrize(
    "messages",
    [
        [],
        ["nats: connection closed"],
        ["nats: stale connection"],
        ["nats: outbound buffer limit exceeded"],
        ["nats: slow consumer, messages dropped"],
    ],
)
def test_an_error_that_is_not_a_permissions_violation_classifies_as_none(messages: list[str]) -> None:
    """Red if any ordinary connection error is misread as an authorisation failure. That direction
    is the more damaging one: a genuine outage would be reported as a permanent, unretryable
    credential problem, and the run would stop rather than being retried."""
    assert classify_violations(messages) is ViolationKind.NONE


def test_classification_is_case_insensitive() -> None:
    """The client lowercases the payload today, so this guards the assumption rather than a
    behaviour. Red if a future client version stops lowercasing and the matching silently misses —
    which would present as every refusal becoming a bare timeout."""
    assert classify_violations([_PUBLISH_VIOLATION.upper()]) is ViolationKind.PUBLISH


def test_a_violation_among_unrelated_errors_is_still_found() -> None:
    """A reconnect cycle puts several messages on the callback before the decisive one. Red if only
    the first or last message is inspected."""
    messages = ["nats: connection closed", _PUBLISH_VIOLATION, "nats: stale connection"]

    assert classify_violations(messages) is ViolationKind.PUBLISH


@pytest.mark.parametrize(
    "message",
    [
        'nats: no responders available for publish to "batch.x"',
        'nats: no responders available for subscription to "_inbox.x.*"',
        "nats: invalid subscription",
    ],
)
def test_naming_an_operation_without_a_permissions_violation_is_not_one(message: str) -> None:
    """Classification keys on the *conjunction* — a permissions violation, and which operation it
    refused — never on the operation word alone. Red if the permissions-violation requirement is
    dropped: the first two messages name an operation in the same phrasing a real violation uses,
    and would then be reported as a credential problem when the actual condition is that nothing is
    listening. The third case alone did not prove this, and a mutation check is what showed it."""
    assert classify_violations([message]) is ViolationKind.NONE
