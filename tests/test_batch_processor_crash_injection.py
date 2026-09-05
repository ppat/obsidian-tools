"""Crash injection for `batch-processor`: a real broker, a real vault surface, and a process killed
between named steps of its own orchestration.

Same shape and same evidence as `test_local_replicator_crash_injection.py`: the crash granularity is
component-level — *between* `processor.py`'s named steps, never inside an HTTP call or a broker
round-trip — because Amazon's ShardStore paper found a coarse, component-level crash model catches
substantially the same bugs as an exhaustive syscall-level fault injector at a fraction of the cost,
and because this project does not mock, so a finer injector here would mean faking NATS and HTTP
internals.

**A killed process runs no `finally`, and modelling that is the whole point.** `_run` re-enables the
agent handle in a `finally`, so a stub that merely raises would model an *orderly* shutdown: the
handle would come back on its own and every watchdog assertion below would pass while proving
nothing. `_crash_at` therefore neutralises `end_run` as well, which is what makes the handle stay
down exactly as it would after a SIGKILL, an OOM kill, or a lost node.

**Every invariant is read off the broker and the vault, never off the model.** The model tracks only
what cannot be re-derived: which chunks were published and what content each one would produce. What
is pending, what was dead-lettered, what the vault holds and whether the handle is down are all read
back live, each time.
"""

from __future__ import annotations

import asyncio
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
    publish,
)
from vault_stub import TOOL_WRITE, FakeVault, running_vault

from obsidian_tools.batch_processor import processor
from obsidian_tools.batch_processor.agent_handle import build_handle_client, run_watchdog_once
from obsidian_tools.batch_processor.watchdog import WatchdogVerdict
from obsidian_tools.config import BatchProcessorConfig

_PORT = 14224

# The named collaborators `processor.py` calls by their own module-level names. A crash at each
# models a kill at that exact point: everything before it really happened, everything after it never
# runs at all.
_CRASH_SEAMS = ("read_targets", "apply_writes", "settle")

_broker_url = ""


class _SimulatedCrash(RuntimeError):
    """Raised by a patched collaborator to model a process killed at that point. Never caught
    anywhere in `obsidian_tools`, only here."""


def _crash_at(seam: str) -> tuple[object, object]:
    """Replace `seam` with a stub that raises before doing any real work, and neutralise `end_run`.

    Returning both originals rather than one is not tidiness: see the module docstring — leaving the
    real `end_run` in place would model a clean shutdown and quietly invalidate every watchdog
    assertion in this file.
    """
    original_seam = getattr(processor, seam)
    original_end = processor.end_run

    def raise_crash(*_args: object, **_kwargs: object) -> None:
        raise _SimulatedCrash(seam)

    async def no_shutdown(*_args: object, **_kwargs: object) -> None:
        return None

    setattr(processor, seam, raise_crash)
    processor.end_run = no_shutdown  # type: ignore[assignment]
    return original_seam, original_end


def _restore(seam: str, originals: tuple[object, object]) -> None:
    setattr(processor, seam, originals[0])
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
    return run_watchdog_once(build_handle_client(config.agent_handle), datetime.now(tz=UTC) + offset)


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


@pytest.mark.parametrize("seam", _CRASH_SEAMS)
def test_a_kill_mid_run_redelivers_the_chunk_and_the_watchdog_reopens_the_handle(
    broker: str, vault: FakeVault, tag: str, seam: str
) -> None:
    """`docs/VERIFICATIONS.md`'s pending A2+D4 row, injected: kill `batch-processor` mid-run, and
    both halves must hold — the chunk comes back, and the agent handle comes back.

    Red in three separate ways, each of which is a different production incident:

    - if the killed run had acked or terminated the chunk before applying it, the work is gone and
      nothing says so;
    - if the handle were left down with no lease, the watchdog is required to read that as an
      operator's own hold and leave it, so every agent write in the system stops indefinitely;
    - if the watchdog re-enabled without the lease having expired, it would open agent writes in the
      middle of a live run.

    `ack_wait` is short here because it is the *only* thing that brings the chunk back: a killed
    process leaves the message delivered-but-unsettled, and the broker withholds it from every later
    run until that deadline passes. Worth knowing operationally — restarting `batch-processor` does
    not resume its interrupted chunk immediately, it resumes it one `ack_wait` later.
    """
    config = config_for(broker, vault, tag, ack_wait_seconds=1.0)
    publish(broker, tag, chunk_of(*creating("10-areas/x.md", "one\n")))

    run_until_crash(config, seam)

    assert vault.handle_blocked is True, "a killed run leaves the agent handle down; that is the failure"
    assert watchdog_pass(config, after_the_lease=False) is WatchdogVerdict.PROCESSOR_HOLDS_THE_LEASE
    assert watchdog_pass(config) is WatchdogVerdict.RE_ENABLE
    assert vault.handle_blocked is False

    assert pending_on_the_batch_stream(config) == 1
    processor.run(config)

    # Applied exactly once, whichever step the kill landed on — and the two routes there are
    # different, which is the point. A kill before the writes leaves the redelivery to apply the
    # chunk; a kill *after* them leaves a chunk that already landed, and its redelivery is rejected
    # by the pre-flight rather than replayed (ADR-0048's stated consequence). Red if either route
    # produced a second write: that is the double-apply the whole-chunk pre-flight exists to remove.
    assert vault.written("10-areas/x.md") == "one\n"
    assert [call for call in vault.calls if call[0] == TOOL_WRITE] == [(TOOL_WRITE, "10-areas/x.md")]


def test_a_kill_before_the_writes_leaves_the_vault_untouched(broker: str, vault: FakeVault, tag: str) -> None:
    """The pre-flight reads happen before any write, so a kill between them changes nothing. Red if
    reads and writes were ever interleaved per target — the chunk could then half-apply and be
    redelivered onto content its own earlier writes moved, which is the case ADR-0048's whole-chunk
    ordering exists to remove."""
    config = config_for(broker, vault, tag, ack_wait_seconds=1.0)
    publish(broker, tag, chunk_of(*creating("10-areas/x.md", "one\n")))

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
        # whether the handle is down -- is read back off the broker and the stub each time.
        self.legal_contents: dict[str, set[str]] = {}
        self.published = 0

    def teardown(self) -> None:
        self._vault_context.__exit__(None, None, None)

    def _note(self, path: str, content: str) -> None:
        self.legal_contents.setdefault(path, set()).add(content)

    # --- rules ------------------------------------------------------------------------------------

    @rule(path=st.sampled_from(("10-areas/a.md", "05-raw/b.md")), body=st.text(alphabet="abc", min_size=1, max_size=3))
    def enqueue_create(self, path: str, body: str) -> None:
        content = f"{body}\n"
        publish(_broker_url, self.tag, chunk_of(*creating(path, content)))
        self._note(path, content)
        self.published += 1

    @rule(body=st.text(alphabet="xyz", min_size=1, max_size=3))
    def enqueue_modify(self, body: str) -> None:
        """A modify against whatever the vault currently holds — a chunk the producer could really
        have generated. Against any other pre-image it would simply reject as stale, which is
        correct but tests only one branch."""
        path = "10-areas/a.md"
        before = self.vault.written(path)
        if before is None:
            return
        after = f"{body}\n"
        publish(_broker_url, self.tag, chunk_of(*modifying(path, before, after)))
        self._note(path, after)
        self.published += 1

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
        """The vault only ever holds something a chunk actually asked for. Red on any partial or
        garbled application — a truncated patch, a hunk applied to the wrong pre-image, a write of
        the empty string where a delete belonged."""
        for path, content in self.vault.notes.items():
            assert content in self.legal_contents.get(path, set()), (
                f"{path!r} holds {content!r}, which no published chunk ever declared"
            )

    @invariant()
    def a_watchdog_pass_never_leaves_the_handle_down(self) -> None:
        """Unit D4's guarantee, checked after every step rather than only after a crash: run the
        watchdog with the clock past any lease, and agent writes must be open. Red if any sequence
        of crashes reaches a state the watchdog cannot recover from — which is the silent,
        indefinite outage the component exists to make impossible."""
        watchdog_pass(self.config)
        assert self.vault.handle_blocked is False

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
