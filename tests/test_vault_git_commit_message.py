"""Tests for `obsidian_tools/vault_git/commit_message.py` — the pure commit-message formatter,
tested as a pure function over hand-built `NameStatusEntry` lists rather than a real staged commit.

`test_vault_git_commit.py` keeps the real-git integration coverage (message text read back via
`git log`, including the quotePath-hostile-path regression); this file is where hostile inputs
become literals rather than fixtures.
"""

from __future__ import annotations

from datetime import UTC, datetime

from obsidian_tools.vault_git.commit_message import format_commit_message
from obsidian_tools.vault_git.name_status import NameStatusEntry

_CYCLE_TIME = datetime(2026, 7, 31, 4, 0, 0, tzinfo=UTC)


def test_empty_change_set() -> None:
    message = format_commit_message([], cycle_time=_CYCLE_TIME)
    assert message == "vault sync 2026-07-31T04:00:00Z: 0 changed (no path changes)"
    assert "\n" not in message  # no body at all when there's nothing to list


def test_single_added_file() -> None:
    entries = [NameStatusEntry(status="A", path="note.md")]
    message = format_commit_message(entries, cycle_time=_CYCLE_TIME)
    assert message == "vault sync 2026-07-31T04:00:00Z: 1 changed (1 added)\n\nA\tnote.md"


def test_mixed_change_types_are_summarised_alphabetically_by_status_code() -> None:
    entries = [
        NameStatusEntry(status="M", path="edited.md"),
        NameStatusEntry(status="A", path="added.md"),
        NameStatusEntry(status="D", path="deleted.md"),
    ]
    message = format_commit_message(entries, cycle_time=_CYCLE_TIME)
    header, _, body = message.partition("\n\n")
    assert header == "vault sync 2026-07-31T04:00:00Z: 3 changed (1 added, 1 deleted, 1 modified)"
    assert body == "M\tedited.md\nA\tadded.md\nD\tdeleted.md"


def test_rename_is_rendered_as_old_arrow_new() -> None:
    entries = [NameStatusEntry(status="R100", path="new.md", old_path="old.md")]
    message = format_commit_message(entries, cycle_time=_CYCLE_TIME)
    assert "R100\told.md -> new.md" in message
    assert "1 renamed" in message


def test_copy_is_rendered_as_source_arrow_copy() -> None:
    entries = [NameStatusEntry(status="C100", path="copy.md", old_path="source.md")]
    message = format_commit_message(entries, cycle_time=_CYCLE_TIME)
    assert "C100\tsource.md -> copy.md" in message
    assert "1 copied" in message


def test_unrecognized_status_code_falls_back_to_the_raw_code_in_the_summary() -> None:
    """Defensive: a status code this codebase's label table doesn't know about must still produce a
    readable summary rather than raising a KeyError."""
    entries = [NameStatusEntry(status="T", path="typechange.md")]
    message = format_commit_message(entries, cycle_time=_CYCLE_TIME)
    assert "1 T" in message


def test_non_ascii_paths_pass_through_unescaped() -> None:
    entries = [NameStatusEntry(status="A", path="10-areas/日本語.md")]
    message = format_commit_message(entries, cycle_time=_CYCLE_TIME)
    assert "10-areas/日本語.md" in message
    assert '"' not in message  # no C-quoting artifacts


def test_embedded_quotes_and_newlines_in_a_path_are_preserved_literally() -> None:
    hostile_path = 'weird\nname "with quotes".md'
    entries = [NameStatusEntry(status="A", path=hostile_path)]
    message = format_commit_message(entries, cycle_time=_CYCLE_TIME)
    assert f"A\t{hostile_path}" in message


def test_change_set_exactly_at_the_cap_lists_every_path_with_no_truncation_note() -> None:
    entries = [NameStatusEntry(status="A", path=f"note-{i}.md") for i in range(50)]
    message = format_commit_message(entries, cycle_time=_CYCLE_TIME)
    assert "... and" not in message
    assert message.count("\n") == 1 + 50  # header, blank line folded into the "\n\n" join, 50 body lines


def test_change_set_over_the_cap_is_truncated_with_a_remainder_count() -> None:
    entries = [NameStatusEntry(status="A", path=f"note-{i}.md") for i in range(63)]
    message = format_commit_message(entries, cycle_time=_CYCLE_TIME)
    lines = message.splitlines()
    assert lines[0] == "vault sync 2026-07-31T04:00:00Z: 63 changed (63 added)"
    assert "note-49.md" in message
    assert "note-50.md" not in message  # the 51st entry is past the 50-path cap
    assert lines[-1] == "... and 13 more"
