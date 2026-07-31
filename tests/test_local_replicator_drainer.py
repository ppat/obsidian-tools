"""Tests for obsidian_tools/local_replicator/drainer.py -- the Phase 2 spool drainer.

Real spool files on a real temp filesystem (via spool.py's own write path) throughout.
"""

from __future__ import annotations

from pathlib import Path

from obsidian_tools.local_replicator.drainer import DrainSink, discard_sink, drain_once
from obsidian_tools.local_replicator.drift import SpoolEntry
from obsidian_tools.local_replicator.spool import list_spool_files, write_spool_entry


def _entry(path: str) -> SpoolEntry:
    return SpoolEntry(kind="modify", path=path, old_path=None, patch="diff\n")


def _recording_sink(record: list[str]) -> DrainSink:
    def sink(entry: SpoolEntry) -> None:
        record.append(entry.path)

    return sink


def test_discard_sink_accepts_an_entry_without_error() -> None:
    discard_sink(_entry("note.md"))


def test_drain_once_on_an_empty_spool_is_a_noop(tmp_path: Path) -> None:
    assert drain_once(tmp_path / "spool") == []


def test_drain_once_calls_sink_for_every_entry_and_removes_them(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"
    write_spool_entry(spool_dir, _entry("a.md"))
    write_spool_entry(spool_dir, _entry("b.md"))
    drained_into: list[str] = []

    result = drain_once(spool_dir, sink=_recording_sink(drained_into))

    assert sorted(result) == ["a.md", "b.md"]
    assert sorted(drained_into) == ["a.md", "b.md"]
    assert list_spool_files(spool_dir) == []


def test_default_sink_discards_and_still_empties_the_spool(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"
    write_spool_entry(spool_dir, _entry("note.md"))

    result = drain_once(spool_dir)

    assert result == ["note.md"]
    assert list_spool_files(spool_dir) == []


def test_a_sink_failure_leaves_that_entry_in_the_spool_for_the_next_drain(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"
    write_spool_entry(spool_dir, _entry("stuck.md"))
    write_spool_entry(spool_dir, _entry("fine.md"))

    def failing_sink(entry: SpoolEntry) -> None:
        if entry.path == "stuck.md":
            raise RuntimeError("simulated Phase 5 dispatch failure")

    result = drain_once(spool_dir, sink=failing_sink)

    assert result == ["fine.md"]
    assert len(list_spool_files(spool_dir)) == 1  # only the failed entry's file survives

    # And it drains successfully on a later retry, once the failure clears.
    retry_result = drain_once(spool_dir)
    assert retry_result == ["stuck.md"]
    assert list_spool_files(spool_dir) == []
