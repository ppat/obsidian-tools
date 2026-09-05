"""Tables for `batch_processor/watchdog.py` — unit D4's non-deferrable half.

Four states, exhaustively: the handle is enabled or not, and if not, it carries a live lease, an
expired one, or none. Every one of them has a different correct action, and three of the four are
wrong in a way that is silent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from obsidian_tools.batch_processor.watchdog import (
    AgentHandleStatus,
    WatchdogVerdict,
    decide,
    lease_expiry,
)

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def test_an_enabled_handle_is_left_alone() -> None:
    """Red if the watchdog ever wrote to a healthy handle: every scheduled pass would be a
    configuration change against the gateway, for nothing."""
    assert decide(AgentHandleStatus(enabled=True, lease_expires_at=None), NOW) is WatchdogVerdict.HANDLE_IS_ENABLED


def test_a_live_lease_means_a_run_is_in_progress() -> None:
    """Red if a live lease were re-enabled: agent writes would open in the middle of a batch run,
    putting an interactive writer and the batch through the editor at once — precisely the state
    disabling the handle exists to prevent."""
    status = AgentHandleStatus(enabled=False, lease_expires_at=NOW + timedelta(seconds=30))

    assert decide(status, NOW) is WatchdogVerdict.PROCESSOR_HOLDS_THE_LEASE


def test_an_expired_lease_re_enables_the_handle() -> None:
    """The whole point of the component. Red if an expired lease were left alone: every agent write
    in the system stops, silently and indefinitely, until a human notices capture has gone quiet."""
    status = AgentHandleStatus(enabled=False, lease_expires_at=NOW - timedelta(seconds=1))

    assert decide(status, NOW) is WatchdogVerdict.RE_ENABLE


def test_a_lease_exactly_at_its_deadline_counts_as_expired() -> None:
    """The comparison has to fall on one side. Red if it fell the other way: the worst case flips
    from re-enabling a run one heartbeat early to leaving agents blocked on an exact tie."""
    status = AgentHandleStatus(enabled=False, lease_expires_at=NOW)

    assert decide(status, NOW) is WatchdogVerdict.RE_ENABLE


def test_a_handle_disabled_with_no_lease_is_reported_and_left_alone() -> None:
    """An operator holding writes during an incident leaves exactly this state. Red if the watchdog
    re-enabled it — it would silently undo a deliberate human action, on a schedule, every time."""
    status = AgentHandleStatus(enabled=False, lease_expires_at=None)

    assert decide(status, NOW) is WatchdogVerdict.DISABLED_BY_SOMEONE_ELSE


def test_an_enabled_handle_carrying_a_stale_lease_is_still_left_alone() -> None:
    """A lease left behind by a run that ended untidily. Red if the lease were consulted before the
    handle's own state: the watchdog would act on a run that has already finished, and its verdict
    would depend on debris rather than on whether agents can write."""
    status = AgentHandleStatus(enabled=True, lease_expires_at=NOW - timedelta(hours=1))

    assert decide(status, NOW) is WatchdogVerdict.HANDLE_IS_ENABLED


@pytest.mark.parametrize("ttl", [1.0, 300.0])
def test_the_lease_deadline_is_the_ttl_after_now(ttl: float) -> None:
    """Red if the deadline were computed from anything but the moment of stamping — a deadline
    derived from the run's start would turn the liveness lease into a maximum run duration, which
    is a different mechanism in a different ticket (ot#89)."""
    assert lease_expiry(NOW, ttl) == NOW + timedelta(seconds=ttl)
