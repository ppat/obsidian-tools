"""The watchdog's one decision: start the agent MCP instance, drop a stale lease, or leave both
alone. Pure — nothing here talks to the cluster.

## The failure this closes

A batch run stops the agent MCP instance for its duration by scaling that Deployment to zero, and
starts it again at the end; the ingestor instance is untouched, which is what lets promotion keep
draining (ADR-0052, ADR-0022, ADR-0003). If `batch-processor` dies while the instance is stopped,
**every interactive write stops, silently and indefinitely** — far worse than a slow batch, and
invisible until a human notices that WhatsApp capture has quietly stopped working. That is why unit
D4's watchdog is not deferrable and must exist before the stream ever runs unattended.

## Why the instance's *desired* replica count, and never its availability

`AgentInstanceStatus.running` is read from the Deployment's `spec.replicas`, not from how many pods
answer. Availability is a health question with a different owner: keyed on it, an ordinary
crashloop would present as a batch run holding the door — and a batch run whose pod happened to
linger would present as healthy. Desired replicas is the only reading that says what somebody
*asked for*, which is the fact this component acts on.

## Why the signal is a deadline and not a flag

A flag saying "a run is in progress" is set by the processor and cleared by the processor — so a
processor that dies leaves it set, and the watchdog's only evidence that anything is wrong is
exactly the evidence that has been destroyed. The signal has to be one that a dead processor stops
producing. A `Lease` does that natively: its holder pushes `renewTime` forward for as long as it is
alive, and a processor that is gone stops pushing.

**A lease is not the maximum run duration**, and the two must not be conflated: this deadline moves
forward for as long as the processor is alive and says nothing about how long a run may last. A cap
on the run itself, and draining the agent instance before stopping it, are the other two batch-mode
safety mechanisms and are deliberately *not* here (ot#89, D4's deferred half).

## Why the watchdog starts only what a run stopped, and how ordering makes that sound

An operator stopping the agent instance by hand — to hold writes during an incident, say — leaves it
at zero replicas with no lease. A watchdog that started anything it found stopped would fight that
operator, silently, every time it ran. So an absent lease means "not ours", and the instance is left
exactly as found and reported.

That rule is only *correct* because no partial run can reach the same state. The lease is taken
before the instance is stopped and the instance is started before the lease is released
(`agent_instance.py`), so a crash between any two writes leaves the instance **running** — never
stopped without a lease. Ordering buys what a single atomic write would have bought, without the
authority to write the Deployment body that a single write would have required.

## Why an expired lease on a *running* instance is dropped rather than ignored

Both harmless crash states leave debris: a live-then-expiring lease on a running instance. Leaving
it there is harmless only until the next thing that happens — an operator stopping the instance by
hand would then be met by "stopped, with a lease", and the watchdog would start the instance back up
against a deliberate human action. Dropping the debris is what makes the two states actually
converge rather than merely be survivable, and it is the one verdict that writes to a running
instance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto


class WatchdogVerdict(Enum):
    INSTANCE_IS_RUNNING = auto()
    """Nothing to do — interactive writers can reach the agent instance."""

    PROCESSOR_HOLDS_THE_LEASE = auto()
    """Stopped, and a live processor said so recently. A batch run is in progress."""

    RESTART_INSTANCE = auto()
    """Stopped with an expired lease: whoever stopped it is gone. Start it again."""

    STOPPED_BY_SOMEONE_ELSE = auto()
    """Stopped with no lease at all. Not this system's doing; report it and leave it."""

    RELEASE_STALE_LEASE = auto()
    """Running, carrying an expired lease: the debris a crashed run's ordering leaves behind."""


@dataclass(frozen=True, slots=True)
class AgentInstanceStatus:
    """What the cluster says about the agent MCP instance right now."""

    running: bool
    """`spec.replicas > 0` on the instance's Deployment — desired, never available. See above."""

    lease_expires_at: datetime | None
    """`renewTime + leaseDurationSeconds` from the batch lease, timezone-aware. `None` when the
    lease is unheld — which is both an idle system and an operator's own hold."""


def decide(status: AgentInstanceStatus, now: datetime) -> WatchdogVerdict:
    """What the watchdog should do about `status` at `now`.

    A lease exactly at its deadline counts as expired: the comparison has to fall on one side, and
    falling on the side that acts means the worst case is starting an instance a heartbeat early,
    against a worst case on the other side of leaving interactive writers blocked forever.
    """
    if status.running:
        if status.lease_expires_at is not None and now >= status.lease_expires_at:
            return WatchdogVerdict.RELEASE_STALE_LEASE
        return WatchdogVerdict.INSTANCE_IS_RUNNING
    if status.lease_expires_at is None:
        return WatchdogVerdict.STOPPED_BY_SOMEONE_ELSE
    if now >= status.lease_expires_at:
        return WatchdogVerdict.RESTART_INSTANCE
    return WatchdogVerdict.PROCESSOR_HOLDS_THE_LEASE


def lease_deadline(renewed_at: datetime | None, duration_seconds: float | None) -> datetime | None:
    """When a lease renewed at `renewed_at` for `duration_seconds` stops covering its holder.

    The deadline is *derived* on read rather than stamped on write, which is what makes the lease a
    `Lease` rather than a timestamp in a `Lease`'s clothing: a holder renews by moving `renewTime`
    to the moment of renewal, so the deadline is always measured from when the processor last said
    it was alive. Deriving it from the run's start instead would turn a liveness signal into a
    maximum run duration, which is a different mechanism in a different ticket (ot#89).

    `None` when either half is absent: a lease carrying a holder but no duration covers nothing, and
    a watchdog that guessed a duration would be inventing the trigger it exists to read.
    """
    if renewed_at is None or duration_seconds is None:
        return None
    return renewed_at + timedelta(seconds=duration_seconds)
