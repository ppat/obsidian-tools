"""Crash injection for `batch-processor`: a real broker, a real vault surface, and a process killed
between named steps of its own orchestration.

Same shape and same evidence as `test_local_replicator_crash_injection.py`: the crash granularity is
component-level — *between* `processor.py`'s named steps, never inside an HTTP call or a broker
round-trip — because Amazon's ShardStore paper found a coarse, component-level crash model catches
substantially the same bugs as an exhaustive syscall-level fault injector at a fraction of the cost,
and because this project does not mock, so a finer injector here would mean faking NATS and HTTP
internals.

**A killed process runs no `finally`, and modelling that is the whole point.** `_run` starts the
agent MCP instance again in a `finally`, so a stub that merely raises would model an *orderly*
shutdown: the instance would come back on its own and every watchdog assertion below would pass while
proving nothing. `_crash_at` therefore neutralises `end_run` as well, which is what makes the
instance stay stopped exactly as it would after a SIGKILL, an OOM kill, or a lost node.

**Two of the seams are inside the instance seam rather than the orchestration, and deliberately.**
ADR-0052 replaces atomicity with ordering: the lease is taken before the stop and the instance is
started before the release, so that every interruption leaves the instance *running*. The only
points where that claim could be false are between each ordered pair, so a kill is injected at
exactly those two points — `stop_instance` (after the lease is held, before anything is stopped) and
`release_lease` (after the instance is running again, before the lease is dropped). Injecting them
in `processor.py` instead would have meant restating the ordering there, which is to say keeping the
system's most load-bearing invariant in two places.

The `release_lease` seam is the one that must *not* neutralise `end_run`: the whole point is to reach
a kill on the second half of that call, so the first half has to really happen.

**Every invariant is read off the broker and the vault, never off the model.** The model tracks only
what cannot be re-derived: which chunks were published and what content each one would produce. What
is pending, what was dead-lettered, what the vault holds and whether the instance is stopped are all
read back live, each time.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,  # pyright: ignore[reportUnknownVariableType] -- hypothesis.stateful lacks full stubs
)
from nats_harness import running_broker
from test_batch_processor_stream import (
    SERVER_CONFIG,
    chunk_of,
    config_for,
    creating,
    dead_letter_count,
    modifying,
    note,
    publish,
)
from vault_stub import TOOL_WRITE, FakeVault, running_vault

from obsidian_tools.batch_processor import processor
from obsidian_tools.batch_processor.agent_instance import (
    AgentInstanceClient,
    build_instance_client,
    run_watchdog_once,
)
from obsidian_tools.batch_processor.watchdog import WatchdogVerdict
from obsidian_tools.config import BatchProcessorConfig

_PORT = 14224

# Where a kill is injected, and on what. A crash at each models a kill at that exact point:
# everything before it really happened, everything after it never runs at all. The first three are
# the named collaborators `processor.py` calls by their own module-level names; the last two are the
# two halves of ADR-0052's ordering, which live in the instance seam — see the module docstring.
_DRAIN_SEAMS = ("read_targets", "apply_writes", "settle")
_ORDERING_SEAMS = ("stop_instance", "release_lease")
_CRASH_SEAMS = (*_DRAIN_SEAMS, *_ORDERING_SEAMS)

# The one seam reached *through* `end_run`, so neutralising `end_run` would put it out of reach.
_SEAM_INSIDE_THE_EXIT_PATH = "release_lease"

_broker_url = ""


class _SimulatedCrash(RuntimeError):
    """Raised by a patched collaborator to model a process killed at that point. Never caught
    anywhere in `obsidian_tools`, only here."""


def _target(seam: str) -> object:
    """Where the seam lives. Ordering seams are methods on the instance client, drain seams are
    module-level names in `processor.py`."""
    return AgentInstanceClient if seam in _ORDERING_SEAMS else processor


def _crash_at(seam: str) -> tuple[object, object | None]:
    """Replace `seam` with a stub that raises before doing any real work, and — unless the seam is
    reached through it — neutralise `end_run`.

    Returning the `end_run` original rather than only the seam's is not tidiness: see the module
    docstring — leaving the real `end_run` in place would model a clean shutdown and quietly
    invalidate every watchdog assertion in this file.
    """
    target = _target(seam)
    original_seam = getattr(target, seam)

    def raise_crash(*_args: object, **_kwargs: object) -> None:
        raise _SimulatedCrash(seam)

    async def no_shutdown(*_args: object, **_kwargs: object) -> None:
        return None

    setattr(target, seam, raise_crash)
    if seam == _SEAM_INSIDE_THE_EXIT_PATH:
        return original_seam, None
    original_end = processor.end_run
    processor.end_run = no_shutdown  # type: ignore[assignment]
    return original_seam, original_end


def _restore(seam: str, originals: tuple[object, object | None]) -> None:
    setattr(_target(seam), seam, originals[0])
    if originals[1] is not None:
        processor.end_run = originals[1]  # type: ignore[assignment]


def run_until_crash(config: BatchProcessorConfig, seam: str) -> None:
    originals = _crash_at(seam)
    try:
        processor.run(config)
    except _SimulatedCrash:
        pass
    finally:
        _restore(seam, originals)


def pending_on_the_batch_stream(config: BatchProcessorConfig) -> int:
    """What the broker itself says is still owed to this consumer — delivered-but-unsettled
    included, which is exactly the state a killed processor leaves behind."""

    async def scenario() -> int:
        from nats.aio.client import Client
        from nats.js import JetStreamContext
        from nats.js.errors import NotFoundError

        client = Client()
        await client.connect(servers=config.nats_url, user=config.nats_user, password=config.nats_password)
        try:
            info = await JetStreamContext(client).consumer_info(config.stream, config.durable)
        except NotFoundError:
            # The consumer is created by the first run. Before one has happened, everything on the
            # stream is still owed — returning zero here would make the accounting invariant read a
            # published-but-not-yet-consumed chunk as having vanished.
            info = await JetStreamContext(client).stream_info(config.stream)
            return info.state.messages
        else:
            return (info.num_pending or 0) + (info.num_ack_pending or 0)
        finally:
            await client.close()

    return asyncio.run(scenario())


def watchdog_pass(config: BatchProcessorConfig, *, after_the_lease: bool = True) -> WatchdogVerdict:
    """One watchdog run. The clock is moved past the lease rather than the lease being shortened,
    so the thing under test is the decision rather than a configuration that made it inevitable."""
    offset = timedelta(seconds=config.lease_ttl_seconds + 1) if after_the_lease else timedelta(0)
    return run_watchdog_once(build_instance_client(config.agent_instance), datetime.now(tz=UTC) + offset)


# --- the deterministic catalogue row --------------------------------------------------------------


@pytest.fixture(scope="module")
def broker() -> Iterator[str]:
    with running_broker(SERVER_CONFIG, _PORT) as url:
        yield url


@pytest.fixture
def vault() -> Iterator[FakeVault]:
    with running_vault(FakeVault()) as state:
        yield state


@pytest.fixture
def tag(broker: str) -> str:
    from test_batch_processor_stream import create_streams

    marker = uuid.uuid4().hex[:8]
    asyncio.run(create_streams(broker, marker))
    return marker


@pytest.mark.parametrize("seam", _DRAIN_SEAMS)
def test_a_kill_mid_run_redelivers_the_chunk_and_the_watchdog_restarts_the_instance(
    broker: str, vault: FakeVault, tag: str, seam: str
) -> None:
    """`docs/VERIFICATIONS.md`'s pending A2+D4 row, injected: kill `batch-processor` mid-run, and
    both halves must hold — the chunk comes back, and the agent MCP instance comes back.

    Red in three separate ways, each of which is a different production incident:

    - if the killed run had acked or terminated the chunk before applying it, the work is gone and
      nothing says so;
    - if the instance were left stopped with no lease, the watchdog is required to read that as an
      operator's own hold and leave it, so every interactive write in the system stops indefinitely;
    - if the watchdog restarted without the lease having expired, it would open interactive writes
      in the middle of a live run.

    `ack_wait` is short here because it is the *only* thing that brings the chunk back: a killed
    process leaves the message delivered-but-unsettled, and the broker withholds it from every later
    run until that deadline passes. Worth knowing operationally — restarting `batch-processor` does
    not resume its interrupted chunk immediately, it resumes it one `ack_wait` later.
    """
    config = config_for(broker, vault, tag, ack_wait_seconds=1.0)
    publish(broker, tag, chunk_of(*creating("10-areas/x.md", note("one\n"))))

    run_until_crash(config, seam)

    assert vault.agent_replicas == 0, "a killed run leaves the agent instance stopped; that is the failure"
    assert watchdog_pass(config, after_the_lease=False) is WatchdogVerdict.PROCESSOR_HOLDS_THE_LEASE
    assert watchdog_pass(config) is WatchdogVerdict.RESTART_INSTANCE
    assert vault.agent_replicas == 1
    assert vault.lease == {}, "restarting the instance drops the lease with it"

    assert pending_on_the_batch_stream(config) == 1
    assert processor.run(config) == 0

    # Applied exactly once, whichever step the kill landed on — and the two routes there are
    # different, which is the point. A kill before the writes leaves the redelivery to apply the
    # chunk; a kill *after* them leaves a chunk whose notes already landed, so its redelivery is
    # refused by the pre-flight and then settled as already applied rather than replayed. Red if
    # either route produced a second write: that is the double-apply the whole-chunk pre-flight
    # exists to remove. Red on the exit code means a crash costs the *next* run its clean bill of
    # health, and the parked copy would block every later chunk that links to what it wrote.
    assert vault.written("10-areas/x.md") == note("one\n")
    assert [call for call in vault.calls if call[0] == TOOL_WRITE] == [(TOOL_WRITE, "10-areas/x.md")]
    assert dead_letter_count(broker, tag) == 0


def test_a_restart_inside_the_acknowledgement_window_says_the_stream_is_not_drained(
    broker: str, vault: FakeVault, tag: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A killed run leaves one chunk unsettled, and `max_ack_pending=1` then withholds every message
    behind it until `ack_wait` expires. The restarted run's fetch is therefore empty — and an empty
    fetch is also how a healthy run against a drained stream ends.

    Measured, before this said anything: the restart produced a log bit-identical to a healthy empty
    run — five INFO lines, every count zero, exit 0 — with the rest of the import still queued. A
    green that means "did not run" is the failure this repository's testing discipline refuses, so
    the run states what it left behind. Red if `left_pending` were absent or zero: nothing in the
    output distinguishes the two, and `ack_wait` defaults to 120 seconds, so every restart in the
    two minutes after a crash lands in this window.
    """
    config = config_for(broker, vault, tag, ack_wait_seconds=30.0)
    publish(broker, tag, chunk_of(*creating("10-areas/x.md", note("one\n"))))
    run_until_crash(config, "settle")

    with caplog.at_level(logging.INFO):
        assert processor.run(config) == 0

    assert [record for record in caplog.records if getattr(record, "event", None) == "batch_stream_not_drained"]
    [complete] = [record for record in caplog.records if getattr(record, "event", None) == "batch_run_complete"]
    assert getattr(complete, "applied", None) == 0
    assert getattr(complete, "left_pending", None) == 1


def test_a_drained_stream_reports_nothing_left(
    broker: str, vault: FakeVault, tag: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The control for the test above, and what stops the warning becoming noise every run learns to
    ignore. Red if a run that really did drain the stream still reported work left: the signal would
    fire on every healthy nightly run and would then mean nothing at all."""
    publish(broker, tag, chunk_of(*creating("10-areas/x.md", note("one\n"))))

    with caplog.at_level(logging.INFO):
        processor.run(config_for(broker, vault, tag))

    assert [record for record in caplog.records if getattr(record, "event", None) == "batch_stream_not_drained"] == []
    [complete] = [record for record in caplog.records if getattr(record, "event", None) == "batch_run_complete"]
    assert getattr(complete, "left_pending", None) == 0


def test_a_kill_between_taking_the_lease_and_stopping_leaves_the_instance_running(
    broker: str, vault: FakeVault, tag: str
) -> None:
    """ADR-0052's ordering, injected at the one point it could be false. The lease is taken first
    precisely so that a kill in this window leaves nothing stopped.

    Red if the two writes were reversed. The instance would sit at zero replicas holding no lease —
    the state the watchdog is required to read as an operator's own hold and leave exactly as found,
    forever. Every interactive write in the system would stop, and no scheduled thing in the system
    would ever notice. That is *worse* than the outage the watchdog exists to end, because it is the
    one the watchdog is designed not to touch.

    The debris left behind is the live lease, which the next pass past its deadline drops.
    """
    config = config_for(broker, vault, tag, ack_wait_seconds=1.0)
    publish(broker, tag, chunk_of(*creating("10-areas/x.md", note("one\n"))))

    run_until_crash(config, "stop_instance")

    assert vault.agent_replicas == 1, "the lease is taken before the stop, so a kill here stops nothing"
    assert vault.lease.get("holderIdentity") is not None
    assert watchdog_pass(config) is WatchdogVerdict.RELEASE_STALE_LEASE
    assert vault.agent_replicas == 1
    assert vault.lease == {}
    assert vault.notes == {}, "the run never reached the stream"


def test_a_kill_between_starting_the_instance_and_releasing_the_lease_leaves_it_running(
    broker: str, vault: FakeVault, tag: str
) -> None:
    """The same claim at the other end of the run, and the only crash seam deliberately reached
    *through* `end_run` rather than around it — the start has to really happen for the kill to land
    where it matters.

    Red if the two writes were reversed: a kill in that window ends the run with the instance still
    at zero and its lease already gone, which is again the operator's-hold state nothing recovers
    from. Red equally if the chunk's own work were lost — the crash is in the exit path, after the
    stream was drained.
    """
    config = config_for(broker, vault, tag, ack_wait_seconds=1.0)
    publish(broker, tag, chunk_of(*creating("10-areas/x.md", note("one\n"))))

    run_until_crash(config, "release_lease")

    assert vault.agent_replicas == 1, "the instance is started before the lease is released"
    assert vault.written("10-areas/x.md") == note("one\n")
    assert watchdog_pass(config) is WatchdogVerdict.RELEASE_STALE_LEASE
    assert vault.lease == {}


def test_a_kill_before_the_writes_leaves_the_vault_untouched(broker: str, vault: FakeVault, tag: str) -> None:
    """The pre-flight reads happen before any write, so a kill between them changes nothing. Red if
    reads and writes were ever interleaved per target — the chunk could then half-apply and be
    redelivered onto content its own earlier writes moved, which is the case ADR-0048's whole-chunk
    ordering exists to remove."""
    config = config_for(broker, vault, tag, ack_wait_seconds=1.0)
    publish(broker, tag, chunk_of(*creating("10-areas/x.md", note("one\n"))))

    run_until_crash(config, "apply_writes")

    assert vault.notes == {}


# --- the stateful harness ---------------------------------------------------------------------------


class CrashInjectionMachine(RuleBasedStateMachine):
    """One instance is one generated sequence. Streams and a vault are built fresh per example; the
    broker container is shared, which is why every name below carries this instance's own tag."""

    def __init__(self) -> None:
        super().__init__()
        from test_batch_processor_stream import create_streams

        self.tag = uuid.uuid4().hex[:8]
        asyncio.run(create_streams(_broker_url, self.tag))
        self._vault_context = running_vault(FakeVault())
        self.vault = self._vault_context.__enter__()
        # A short `ack_wait` for the reason the deterministic test above records: a crashed run's
        # chunk is invisible to every later run until that deadline passes, so a long one would make
        # most generated sequences unable to observe a redelivery at all.
        self.config = config_for(_broker_url, self.vault, self.tag, ack_wait_seconds=1.0, idle_timeout_seconds=0.3)

        # Model state, and deliberately only this: what content each published chunk would produce
        # if applied. Everything else -- what is pending, what was parked, what the vault holds, and
        # whether the instance is stopped -- is read back off the broker and the stub each time.
        self.legal_contents: dict[str, set[str]] = {}
        self.published = 0

    def teardown(self) -> None:
        self._vault_context.__exit__(None, None, None)

    def _note(self, path: str, content: str) -> None:
        self.legal_contents.setdefault(path, set()).add(content)

    # --- rules ------------------------------------------------------------------------------------

    @rule(
        path=st.sampled_from(("10-areas/a.md", "05-raw/b.md")),
        body=st.text(alphabet="abc", min_size=1, max_size=3),
        admissible=st.booleans(),
    )
    def enqueue_create(self, path: str, body: str, admissible: bool) -> None:
        # The serial keeps every generated chunk's content distinct, and that is a constraint of the
        # accounting invariant rather than a choice about coverage: a create whose note already
        # holds exactly its content is *settled by an ack*, contributing to neither the writes nor
        # the parked copies the invariant counts, so a duplicate would read there as a chunk that
        # vanished. That case is proven deterministically instead — see the re-run and duplicate
        # delivery tests in `test_batch_processor_stream.py`.
        content = self._content(f"{body}-{self.published}\n", admissible)
        publish(_broker_url, self.tag, chunk_of(*creating(path, content)))
        self._declare(path, content, admissible)
        self.published += 1

    @rule(body=st.text(alphabet="xyz", min_size=1, max_size=3), admissible=st.booleans())
    def enqueue_modify(self, body: str, admissible: bool) -> None:
        """A modify against whatever the vault currently holds — a chunk the producer could really
        have generated. Against any other pre-image it would simply reject as stale, which is
        correct but tests only one branch."""
        path = "10-areas/a.md"
        before = self.vault.written(path)
        if before is None:
            return
        after = self._content(f"{body}\n", admissible)
        publish(_broker_url, self.tag, chunk_of(*modifying(path, before, after)))
        self._declare(path, after, admissible)
        self.published += 1

    @staticmethod
    def _content(body: str, admissible: bool) -> str:
        """An admissible note, or the bare body — which curated space refuses and the raw layer,
        being exempt, takes as it is."""
        return note(body) if admissible else body

    def _declare(self, path: str, content: str, admissible: bool) -> None:
        """Record `content` as legal at `path` only if the path may ever hold it. A refused
        post-image is never declared, so the invariant below reads one landing — under any sequence
        of crashes, redeliveries and re-runs — as content no chunk was entitled to write."""
        if admissible or not path.startswith(("10-areas/", "20-projects/")):
            self._note(path, content)

    @rule()
    def run_cleanly(self) -> None:
        processor.run(self.config)

    @rule(seam=st.sampled_from(_CRASH_SEAMS))
    def run_crashed(self, seam: str) -> None:
        run_until_crash(self.config, seam)

    @rule()
    def run_watchdog(self) -> None:
        watchdog_pass(self.config)

    # --- invariants, all read off real state ------------------------------------------------------

    @invariant()
    def no_note_holds_content_no_chunk_ever_declared(self) -> None:
        """The vault only ever holds something a chunk actually asked for and was entitled to write.
        Red on any partial or garbled application — a truncated patch, a hunk applied to the wrong
        pre-image, a write of the empty string where a delete belonged — and on any post-image the
        admission bar refuses reaching curated space by any route."""
        for path, content in self.vault.notes.items():
            assert content in self.legal_contents.get(path, set()), (
                f"{path!r} holds {content!r}, which no published chunk ever declared"
            )

    @invariant()
    def a_watchdog_pass_never_leaves_the_instance_stopped(self) -> None:
        """Unit D4's guarantee, checked after every step rather than only after a crash: run the
        watchdog with the clock past any lease, and interactive writes must be open. Red if any
        sequence of crashes reaches a state the watchdog cannot recover from — which is the silent,
        indefinite outage the component exists to make impossible.

        This is also where the ordering claim is checked against *sequences* rather than against one
        kill: a crash between either ordered pair leaves debris, and a later crash on top of that
        debris must still converge here."""
        watchdog_pass(self.config)
        assert self.vault.agent_replicas > 0

    @invariant()
    def no_published_chunk_is_ever_unaccounted_for(self) -> None:
        """Nothing vanishes: every chunk is applied, parked on the dead-letter stream, or still owed
        by the broker. Counted from the broker and the stub, never from the model's own tally of
        what it thinks happened. Red if a chunk were ever terminated without its dead-letter copy
        landing — work that disappears with no trace is the one outcome the dead-letter path exists
        to prevent."""
        parked = dead_letter_count(_broker_url, self.tag)
        pending = pending_on_the_batch_stream(self.config)
        applied = self.vault.writes
        assert parked + pending + applied >= self.published, (
            f"{self.published} chunks published, but only {applied} applied, {parked} parked and "
            f"{pending} still owed by the broker"
        )


@pytest.mark.slow  # a real broker round-trip and a real HTTP surface per step, like the replicator's
def test_crash_injection_state_machine(broker: str) -> None:
    """Sequence space, which the deterministic tests above deliberately do not explore: crashes at
    different seams interleaved with enqueues, clean runs and watchdog passes."""
    global _broker_url
    _broker_url = broker
    run_state_machine_as_test(
        CrashInjectionMachine,
        settings=hypothesis_settings(deadline=None, stateful_step_count=6),
    )
