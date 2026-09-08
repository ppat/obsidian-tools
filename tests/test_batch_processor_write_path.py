"""The write path's two halves, over a real socket and against a surface that enforces the flag.

`test_batch_processor_http_seams.py` proves what one call puts on the wire. This module proves the
step above it: that `processor.apply_writes` derives the anti-clobber flag from the chunk's own
declared operation (ADR-0053), and that a surface acting on `overwrite: false` stops a create that
the pre-flight cleared and reality then contradicted.

**Why the injection is here and not beside the run tests.** It needs no broker: the control being
proven is a refusal arriving mid-chunk from the surface, and `apply_writes` is exactly the step
that meets one. What a refused write does to the *chunk* — dead-lettered, not replayed — is proven
where chunks live.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from hypothesis import given
from hypothesis import strategies as st
from vault_stub import TOOL_DELETE, TOOL_READ, TOOL_WRITE, FakeVault, running_vault

from obsidian_tools.batch_processor.mcp_client import McpClient, McpRefusedError, McpToolNames
from obsidian_tools.batch_processor.patching import PlannedWrite, WriteKind
from obsidian_tools.batch_processor.processor import apply_writes


def client_for(vault: FakeVault) -> McpClient:
    return McpClient(
        base_url=vault.url,
        api_key="stub-key",
        tools=McpToolNames(read=TOOL_READ, write=TOOL_WRITE, delete=TOOL_DELETE),
        timeout_seconds=5.0,
        verify_tls=False,
        retries=1,
        retry_base_delay_seconds=0.0,
    )


def test_a_create_and_a_modify_are_told_apart_by_the_flag_apply_writes_sends() -> None:
    """The flag is read off the planned write's kind, and the two travel differently. Red if
    `apply_writes` sent one value for both, or read the flag from anywhere but the chunk's declared
    operation — a create would stop asserting the target's absence, or a modify would be refused
    for the note it exists to replace."""
    vault = FakeVault(notes={"10-areas/x.md": "before\n"})
    writes = (
        PlannedWrite("05-raw/new.md", WriteKind.CREATE, "raw\n"),
        PlannedWrite("10-areas/x.md", WriteKind.MODIFY, "after\n"),
    )

    with running_vault(vault):
        outcome = asyncio.run(apply_writes(client_for(vault), writes))

    assert outcome.failure is None
    assert vault.write_arguments == [("05-raw/new.md", False), ("10-areas/x.md", True)]
    assert vault.written("10-areas/x.md") == "after\n"


def test_a_create_whose_target_appears_after_the_preflight_is_refused_by_the_surface() -> None:
    """**Violation injection.** A writer wins the race between the pre-flight's read and the write:
    the processor has established that `05-raw/imported.md` is absent, and by the time it writes,
    it is not. Nothing in this repository can see that happen — the pre-flight already ran and was
    right when it ran — so the only thing standing between the chunk and a whole-note overwrite of
    somebody else's note is the server's own existence test.

    It sits beside ADR-0015's pre-flight check rather than replacing it, and the two are not the
    same control: the pre-flight is path-scoped to the raw layer and refuses a raw *modify* as
    well, which travels as an ordinary `overwrite: true` write this flag cannot see; the flag is
    path-blind and catches the case the pre-flight's view of the vault got wrong. Red if the flag
    stops being sent — the racing writer's note is overwritten, the chunk reports success, and
    nothing anywhere records that a write-once note was replaced.

    What this cannot prove is that the *deployed* surface honours the flag: a stub refusing because
    a test told it to refuse is evidence about the client (`docs/VERIFICATIONS.md` §5)."""
    vault = FakeVault(appear_before_first_write={"05-raw/imported.md": "the racing writer got here first\n"})
    writes = (
        PlannedWrite("05-raw/imported.md", WriteKind.CREATE, "what the chunk would have written\n"),
        PlannedWrite("10-areas/later.md", WriteKind.CREATE, "never reached\n"),
    )

    with running_vault(vault):
        outcome = asyncio.run(apply_writes(client_for(vault), writes))

    assert isinstance(outcome.failure, McpRefusedError)
    assert "file_exists" in str(outcome.failure)
    assert outcome.applied == 0
    assert vault.written("05-raw/imported.md") == "the racing writer got here first\n"
    assert vault.written("10-areas/later.md") is None


@dataclass
class RecordingClient:
    """Every call `apply_writes` makes, in order, with the flag as it was passed."""

    writes: list[tuple[str, bool]] = field(default_factory=list[tuple[str, bool]])
    deletes: list[str] = field(default_factory=list[str])

    def write_note(self, path: str, content: str, *, overwrite: bool) -> None:
        del content
        self.writes.append((path, overwrite))

    def delete_note(self, path: str) -> None:
        self.deletes.append(path)


@given(
    st.lists(
        st.tuples(st.sampled_from(WriteKind), st.text(min_size=1, max_size=8)),
        min_size=1,
        max_size=8,
    )
)
def test_every_write_carries_the_flag_its_declared_operation_implies(plan: list[tuple[WriteKind, str]]) -> None:
    """The mapping, over every sequence of operations a chunk can plan rather than over one
    example. Red if any kind other than a create ever asserted absence, if a create ever failed to,
    or if a write went out with the flag unset — the last of which the surface would answer by
    defaulting it to `false` and refusing every modify in the batch."""
    writes = tuple(
        PlannedWrite(f"{index}-{name}.md", kind, None if kind is WriteKind.DELETE else "body\n")
        for index, (kind, name) in enumerate(plan)
    )
    client = RecordingClient()

    asyncio.run(apply_writes(client, writes))  # pyright: ignore[reportArgumentType]

    expected = [
        (write.path, write.kind is not WriteKind.CREATE) for write in writes if write.kind is not WriteKind.DELETE
    ]
    assert client.writes == expected
    assert client.deletes == [write.path for write in writes if write.kind is WriteKind.DELETE]
