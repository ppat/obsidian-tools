"""Tables for `batch_processor/fairness.py` — ADR-0022's backpressure, dead-letter and the subject
check that keeps the dead-letter path from being a loop.

Every delay below is asserted exactly, which is what the injected `random_value` is for: a decision
that drew from the global RNG could only be asserted as a range, and a range assertion cannot tell
"the schedule is exponential" from "the schedule is constant with wide jitter".
"""

from __future__ import annotations

import pytest

from obsidian_tools.batch_processor.fairness import (
    FairnessDecision,
    dead_letter_subject,
    decide_fairness,
    exhausted_redelivery,
    subjects_overlap,
)
from obsidian_tools.retry import backoff_delay


def decide(depth: int, *, yields: int = 0, threshold: int = 1, random_value: float = 0.0) -> FairnessDecision:
    return decide_fairness(
        depth,
        threshold=threshold,
        consecutive_yields=yields,
        base_delay=1.0,
        max_delay=60.0,
        jitter_fraction=0.0,
        random_value=random_value,
    )


# --- fairness keys on depth, and reacts the moment someone is waiting -----------------------------


def test_an_empty_promotion_stream_does_not_yield() -> None:
    """The positive control. Red if a batch yielded with nobody waiting — the run would never
    finish and the agent handle would stay down for the whole of it."""
    assert decide(0).should_yield is False


def test_one_waiting_promotion_message_is_enough_to_yield() -> None:
    """ "Fairness reacts when someone is waiting" is ADR-0022's whole distinction from health-based
    throttling. Red if the default threshold drifted above one: the batch would keep the shared
    editor busy while a human's capture sat in the queue, which is the starvation the rule exists
    to prevent."""
    assert decide(1).should_yield is True


def test_the_threshold_is_configurable_upward_but_not_silently() -> None:
    """Red if the threshold were ignored: an operator's decision to tolerate a shallow promotion
    queue would have no effect at all."""
    assert decide(2, threshold=5).should_yield is False
    assert decide(5, threshold=5).should_yield is True


def test_a_deep_promotion_stream_keeps_yielding_however_long_it_has_already_waited() -> None:
    """The asymmetry is one-way (ADR-0022): patience never runs out inside this decision, so no
    amount of accumulated batch backlog buys the batch its turn. Red if the yield ever expired on
    its own — bulk would resume against a queue that is still full, which is the reverse of the
    rule. (The run's own bound on consecutive yields is a separate mechanism in `processor.py`, and
    it ends the run rather than proceeding through it.)"""
    assert decide(9, yields=100).should_yield is True


# --- the schedule ---------------------------------------------------------------------------------


@pytest.mark.parametrize(("yields", "expected"), [(0, 1.0), (1, 2.0), (2, 4.0), (3, 8.0), (10, 60.0)])
def test_the_wait_doubles_and_then_stops_at_the_ceiling(yields: int, expected: float) -> None:
    """Red if the schedule were linear or unbounded: a linear one hammers the broker while a human
    waits, and an unbounded one blows past the ceiling an operator configured."""
    assert decide(1, yields=yields).delay_seconds == expected


def test_jitter_only_ever_shortens_the_wait() -> None:
    """Subtractive on purpose, so `max_delay` stays a real ceiling. Red if jitter were additive —
    the configured maximum would become a midpoint, and the run's worst case would silently
    double."""
    longest = decide_fairness(
        1, threshold=1, consecutive_yields=0, base_delay=8.0, max_delay=8.0, jitter_fraction=0.5, random_value=0.0
    )
    shortest = decide_fairness(
        1, threshold=1, consecutive_yields=0, base_delay=8.0, max_delay=8.0, jitter_fraction=0.5, random_value=1.0
    )

    assert longest.delay_seconds == 8.0
    assert shortest.delay_seconds == 4.0


def test_a_zero_jitter_fraction_reproduces_the_schedule_exactly() -> None:
    """Red if jitter were applied when a caller asked for none."""
    assert decide(1, yields=2, random_value=0.9).delay_seconds == 4.0


@pytest.mark.parametrize(("attempt", "expected"), [(1, 1.0), (2, 2.0), (3, 4.0), (9, 20.0)])
def test_the_shared_schedule_is_unjittered_unless_a_caller_asks(attempt: int, expected: float) -> None:
    """`retry.backoff_delay` grew jitter for `batch-processor` and must default it off for every
    caller that predates it — `GitRunner`'s NFS retries never asked for randomness and their delays
    are asserted exactly elsewhere. Red if the default changed: this checks the *default*, which a
    test passing `jitter_fraction=0.0` explicitly can never do."""
    assert backoff_delay(attempt, base_delay=1.0, max_delay=20.0) == expected


# --- the dead-letter path -------------------------------------------------------------------------


@pytest.mark.parametrize(("delivered", "expected"), [(1, False), (4, False), (5, True), (6, True)])
def test_the_last_delivery_the_broker_will_make_is_the_one_that_dead_letters(delivered: int, expected: bool) -> None:
    """The consumer carries the same `max_deliver`, so after this delivery the broker stops on its
    own. Red if the comparison were off by one in either direction: too early throws away a
    delivery that would have succeeded, too late waits for a redelivery that never comes and the
    chunk vanishes rather than being parked."""
    assert exhausted_redelivery(delivered, max_deliver=5) is expected


def test_the_dead_letter_subject_carries_the_batch_id_as_its_own_token() -> None:
    """Mirrors the batch stream's own scheme (ADR-0047). Red if the id were folded into the payload
    instead — an operator could no longer select one batch's failures without parsing every
    message."""
    assert dead_letter_subject("batch-dead-letter", "9f2c") == "batch-dead-letter.9f2c"


@pytest.mark.parametrize(
    ("batch", "dead_letter", "overlaps"),
    [
        ("batch", "batch", True),
        ("batch", "batch.dead", True),
        ("batch.dead", "batch", True),
        ("batch", "batch-dead-letter", False),
        ("batch", "dead-letter", False),
        ("batch", "batchx", False),
    ],
)
def test_a_dead_letter_subject_inside_the_batch_stream_is_detected(
    batch: str, dead_letter: str, overlaps: bool
) -> None:
    """The check that keeps the dead-letter path from being an infinite loop: a chunk republished
    onto the stream it was taken from is immediately re-consumed with a fresh delivery count. Red
    on either mistake — missing `batch.dead` builds the loop, and flagging `batchx` refuses a
    configuration that is fine, because subjects are token-wise and `batch` and `batchx` share no
    subject at all."""
    assert subjects_overlap(batch, dead_letter) is overlaps
