"""Tables for `batch_processor/watchdog.py` — unit D4's non-deferrable half.

Five states, exhaustively: the agent MCP instance is running or stopped, and either way it may carry
a live lease, an expired one, or none. Every one has a different correct action, and four of the
five are wrong in a way that is silent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from obsidian_tools.batch_processor.watchdog import (
    AgentInstanceStatus,
    WatchdogVerdict,
    decide,
    lease_deadline,
)

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def test_a_running_instance_holding_no_lease_is_left_alone() -> None:
    """The steady state. Red if the watchdog ever wrote to a healthy instance: every scheduled pass
    would be a scale patch against the cluster, for nothing."""
    assert decide(AgentInstanceStatus(running=True, lease_expires_at=None), NOW) is WatchdogVerdict.INSTANCE_IS_RUNNING


def test_a_running_instance_under_a_live_lease_is_left_alone() -> None:
    """The window a run is in between taking the lease and stopping the instance — legitimately
    reached on every single run. Red if this were treated as debris and the lease dropped: the run
    about to stop the instance would lose the only thing saying the stop was its doing, and its own
    crash a moment later would strand the instance at zero with no lease, forever."""
    status = AgentInstanceStatus(running=True, lease_expires_at=NOW + timedelta(seconds=30))

    assert decide(status, NOW) is WatchdogVerdict.INSTANCE_IS_RUNNING


def test_a_stopped_instance_under_a_live_lease_means_a_run_is_in_progress() -> None:
    """Red if a live lease were restarted: interactive writes would open in the middle of a batch
    run, putting an interactive writer and the batch through the editor's single event loop at once
    — precisely the state stopping the instance exists to prevent."""
    status = AgentInstanceStatus(running=False, lease_expires_at=NOW + timedelta(seconds=30))

    assert decide(status, NOW) is WatchdogVerdict.PROCESSOR_HOLDS_THE_LEASE


def test_a_stopped_instance_under_an_expired_lease_is_restarted() -> None:
    """The whole point of the component. Red if an expired lease were left alone: every interactive
    write in the system stops, silently and indefinitely, until a human notices capture has gone
    quiet."""
    status = AgentInstanceStatus(running=False, lease_expires_at=NOW - timedelta(seconds=1))

    assert decide(status, NOW) is WatchdogVerdict.RESTART_INSTANCE


def test_a_lease_exactly_at_its_deadline_counts_as_expired() -> None:
    """The comparison has to fall on one side. Red if it fell the other way: the worst case flips
    from restarting a run one heartbeat early to leaving interactive writers blocked on an exact
    tie."""
    status = AgentInstanceStatus(running=False, lease_expires_at=NOW)

    assert decide(status, NOW) is WatchdogVerdict.RESTART_INSTANCE


def test_an_instance_stopped_with_no_lease_is_reported_and_left_alone() -> None:
    """An operator holding writes during an incident leaves exactly this state, and ADR-0052's
    ordering is what guarantees no partial run can. Red if the watchdog restarted it — it would
    silently undo a deliberate human action, on a schedule, every time."""
    status = AgentInstanceStatus(running=False, lease_expires_at=None)

    assert decide(status, NOW) is WatchdogVerdict.STOPPED_BY_SOMEONE_ELSE


def test_a_running_instance_carrying_an_expired_lease_has_the_lease_dropped() -> None:
    """The debris both harmless crash states leave: a run killed between taking the lease and
    stopping, or between starting and releasing.

    Red in two directions. If the verdict were `INSTANCE_IS_RUNNING`, the debris would survive until
    the next operator hand-stop, which would then be read as "stopped, with a lease" and undone by
    the very watchdog that exists to respect it. If the verdict restarted or stopped anything, the
    watchdog would be acting on the instance because of a fact about a run that has already
    finished."""
    status = AgentInstanceStatus(running=True, lease_expires_at=NOW - timedelta(hours=1))

    assert decide(status, NOW) is WatchdogVerdict.RELEASE_STALE_LEASE


@pytest.mark.parametrize("ttl", [1.0, 300.0])
def test_the_lease_deadline_is_the_duration_after_the_renewal(ttl: float) -> None:
    """Red if the deadline were computed from anything but the last renewal — a deadline derived
    from the run's start would turn the liveness lease into a maximum run duration, which is a
    different mechanism in a different ticket (ot#89)."""
    assert lease_deadline(NOW, ttl) == NOW + timedelta(seconds=ttl)


@pytest.mark.parametrize(
    ("renewed_at", "duration"),
    [(None, 300.0), (NOW, None), (None, None)],
)
def test_half_a_lease_covers_nothing(renewed_at: datetime | None, duration: float | None) -> None:
    """A `Lease` whose spec ships empty in git, and one mid-release, both look like this. Red if a
    missing duration were defaulted: the watchdog would invent the deadline it exists to read, and
    the invented value would decide whether a live run is restarted out from under itself."""
    assert lease_deadline(renewed_at, duration) is None
