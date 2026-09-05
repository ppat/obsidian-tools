"""Yield to the promotion stream, and give up on a chunk that will never apply. Pure — no broker.

## Fairness, not health

ADR-0022 keys backpressure on **promotion-stream depth**, deliberately not on MCP health, and the
difference is the whole point: health-based throttling reacts once the shared bottleneck is already
saturated — by which time the interactive paths a human is waiting on are already slow — while
depth-based throttling reacts the moment someone is waiting. The default threshold is therefore one
pending message, because "someone is waiting" is literally what depth ≥ 1 means.

**The asymmetry is one-way and has no counterpart anywhere in this package**: bulk yields to
interactive, never the reverse. There is no code path here by which a deep batch stream slows
promotion, and adding one would invert the decision, not tune it.

Backing off costs only time — unacked messages redeliver and nothing is lost — so the yield is
taken *before* fetching the next chunk rather than while holding one. Holding a chunk through a
long yield would let its ack deadline expire and have the broker redeliver work already in hand,
which converts patience into duplicate delivery.

The schedule itself is `retry.backoff_delay`, the one this repository already has; this module adds
no second one. Jitter is required here and defaulted off there: many processors backing off in
lockstep is a thundering herd, and this component is the only caller that can have more than one
instance in a backoff at once.

## The dead-letter path is deliberate construction, not a feature

ADR-0020 records the trade honestly: JetStream's dead-lettering is advisory-based rather than a
first-class exchange, so it has to be built. What is built here is the smallest thing that keeps
the promise: a chunk that has been delivered `max_deliver` times, or that has failed for a reason
redelivery cannot change, is published to a dead-letter subject and *terminated* — never nak'd
again. Terminating is what makes it a path rather than a loop; the copy on the dead-letter subject
is what makes it a path rather than a deletion.

**The dead-letter subject must not fall inside the batch stream's own subject list.** If it does,
every dead-lettered chunk is immediately re-consumed by the consumer that just gave up on it, at
which point the delivery counter restarts and the loop is infinite and silent. `subjects_overlap`
is checked at configuration time for exactly that.
"""

from __future__ import annotations

from dataclasses import dataclass

from obsidian_tools.retry import backoff_delay

# One pending promotion message is a human waiting; see the module docstring.
DEFAULT_PROMOTION_DEPTH_THRESHOLD = 1

# A quarter off the top, at most. Enough to break lockstep between processors without making the
# delay unrecognisable against the configured schedule when an operator reads it out of a log.
DEFAULT_JITTER_FRACTION = 0.25


@dataclass(frozen=True, slots=True)
class FairnessDecision:
    should_yield: bool
    delay_seconds: float
    """Zero when proceeding."""

    promotion_depth: int


def decide_fairness(
    promotion_depth: int,
    *,
    threshold: int = DEFAULT_PROMOTION_DEPTH_THRESHOLD,
    consecutive_yields: int,
    base_delay: float,
    max_delay: float,
    jitter_fraction: float = DEFAULT_JITTER_FRACTION,
    random_value: float,
) -> FairnessDecision:
    """Whether to take the next chunk, or wait for the promotion stream to drain first.

    `consecutive_yields` is how many times in a row this run has already yielded; it is what makes
    the wait exponential rather than a fixed poll. It resets on every chunk actually taken, so a
    batch interleaving with steady promotion traffic keeps checking often rather than drifting out
    to the ceiling and staying there.

    `random_value` is passed in rather than drawn so the decision is a function of its arguments
    and can be tabled exactly.
    """
    if promotion_depth < threshold:
        return FairnessDecision(should_yield=False, delay_seconds=0.0, promotion_depth=promotion_depth)
    delay = backoff_delay(
        consecutive_yields + 1,
        base_delay=base_delay,
        max_delay=max_delay,
        jitter_fraction=jitter_fraction,
        random_value=random_value,
    )
    return FairnessDecision(should_yield=True, delay_seconds=delay, promotion_depth=promotion_depth)


def exhausted_redelivery(delivery_count: int, *, max_deliver: int) -> bool:
    """Whether this delivery is the last one the broker will make.

    Compared against the delivery just made, not the next one: the consumer is configured with the
    same `max_deliver`, so after this delivery the broker stops on its own. Waiting for a delivery
    that never comes is how a chunk disappears instead of being dead-lettered.
    """
    return delivery_count >= max_deliver


def dead_letter_subject(prefix: str, batch_id: str) -> str:
    """One subject per batch under the dead-letter prefix, mirroring the batch stream's own scheme
    (ADR-0047) so an operator can select one batch's failures without parsing any payload."""
    return f"{prefix}.{batch_id}"


def subjects_overlap(batch_prefix: str, dead_letter_prefix: str) -> bool:
    """Whether a dead-lettered chunk would land back on the stream it was taken from.

    Token-wise rather than string-wise: NATS subjects are dot-separated, so `batch` and `batchx`
    share a string prefix and no subjects at all, while `batch` and `batch.dead` share every
    subject under the second.
    """
    left = batch_prefix.split(".")
    right = dead_letter_prefix.split(".")
    shared = min(len(left), len(right))
    return left[:shared] == right[:shared]
