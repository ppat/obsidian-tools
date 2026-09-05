"""The watchdog's one decision: re-enable the agent handle, or leave it alone. Pure — no gateway.

## The failure this closes

A batch run is nothing more than which gateway handle is enabled: the agent-facing handle is
disabled for the run's duration, the ingestor handle stays live so promotion keeps draining
(ADR-0003, ADR-0022). If `batch-processor` dies while the agent handle is disabled, **every agent
write stops, silently and indefinitely** — far worse than a slow batch, and invisible until a human
notices that WhatsApp capture has quietly stopped working. That is why unit D4's watchdog is not
deferrable and must exist before the stream ever runs unattended.

## Why the signal is a deadline and not a flag

A flag saying "a run is in progress" is set by the processor and cleared by the processor — so a
processor that dies leaves it set, and the watchdog's only evidence that anything is wrong is
exactly the evidence that has been destroyed. The signal has to be one that a dead processor stops
producing. A lease does that: the processor stamps an expiry when it disables the handle and pushes
it forward as it works, and a processor that is gone stops pushing.

**A lease is not the maximum run duration**, and the two must not be conflated: this deadline moves
forward for as long as the processor is alive and says nothing about how long a run may last. A cap
on the run itself, and draining the agent handle before disabling it, are the other two batch-mode
safety mechanisms and are deliberately *not* here (ot#89, D4's deferred half).

## Why the watchdog re-enables only what the processor disabled

An operator disabling the agent handle by hand — to hold writes during an incident, say — leaves it
disabled with no lease. A watchdog that re-enabled anything it found disabled would fight that
operator, silently, every time it ran. So an absent lease means "not ours", and the handle is left
exactly as found and reported. The cost is that a processor killed between disabling the handle and
stamping the lease would not be recovered, which is why the seam does both in one call
(`agent_handle.py`): the two facts are written together or not at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto


class WatchdogVerdict(Enum):
    HANDLE_IS_ENABLED = auto()
    """Nothing to do — agents can write."""

    PROCESSOR_HOLDS_THE_LEASE = auto()
    """Disabled, and a live processor said so recently. A batch run is in progress."""

    RE_ENABLE = auto()
    """Disabled with an expired lease: whoever disabled it is gone. Turn it back on."""

    DISABLED_BY_SOMEONE_ELSE = auto()
    """Disabled with no lease at all. Not this system's doing; report it and leave it."""


@dataclass(frozen=True, slots=True)
class AgentHandleStatus:
    """What the gateway says about the agent handle right now."""

    enabled: bool
    lease_expires_at: datetime | None
    """The deadline the processor stamped when it disabled the handle, timezone-aware. `None` when
    the handle carries no lease — which includes an enabled handle and an operator's own hold."""


def decide(status: AgentHandleStatus, now: datetime) -> WatchdogVerdict:
    """What the watchdog should do about `status` at `now`.

    A lease exactly at its deadline counts as expired: the comparison has to fall on one side, and
    falling on the side that re-enables means the worst case is re-enabling a run a heartbeat
    early, against a worst case on the other side of leaving agents blocked forever.
    """
    if status.enabled:
        return WatchdogVerdict.HANDLE_IS_ENABLED
    if status.lease_expires_at is None:
        return WatchdogVerdict.DISABLED_BY_SOMEONE_ELSE
    if now >= status.lease_expires_at:
        return WatchdogVerdict.RE_ENABLE
    return WatchdogVerdict.PROCESSOR_HOLDS_THE_LEASE


def lease_expiry(now: datetime, ttl_seconds: float) -> datetime:
    """The deadline a processor stamps at `now`.

    The TTL is the whole tuning surface: too short and an ordinary pause between chunks reads as a
    death, too long and the outage the watchdog exists to end lasts that much longer. It is sized
    against how often the processor renews, never against how long a chunk takes.
    """
    return now + timedelta(seconds=ttl_seconds)
