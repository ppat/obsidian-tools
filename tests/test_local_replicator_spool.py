"""Tests for obsidian_tools/local_replicator/spool.py -- the atomic, durable local spool.

Real filesystem operations throughout (temp directories, real `os.replace`) -- the property under
test is the atomicity guarantee itself, which mocking the filesystem would assume rather than
prove.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from obsidian_tools.local_replicator.drift import SpoolEntry, SpoolEntryKind
from obsidian_tools.local_replicator.spool import (
    SpoolWriteError,
    list_spool_files,
    read_spool_entry,
    write_spool_entry,
)


def _entry(
    path: str = "note.md",
    kind: SpoolEntryKind = "modify",
    old_path: str | None = None,
    patch: str = "diff\n",
    baseline_sha: str | None = "1111111111111111111111111111111111111111",
    upstream_sha: str | None = "2222222222222222222222222222222222222222",
    matches_upstream: bool | None = False,
) -> SpoolEntry:
    return SpoolEntry(
        kind=kind,
        path=path,
        old_path=old_path,
        patch=patch,
        baseline_sha=baseline_sha,
        upstream_sha=upstream_sha,
        matches_upstream=matches_upstream,
    )


def test_write_then_read_round_trips_every_field(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"
    entry = _entry(path="10-areas/note.md", kind="rename", old_path="10-areas/old-note.md", patch="the patch\n")

    written = write_spool_entry(spool_dir, entry)

    assert written.parent == spool_dir
    assert read_spool_entry(written) == entry


@pytest.mark.parametrize(
    ("upstream_sha", "matches_upstream"),
    [
        ("2222222222222222222222222222222222222222", True),
        ("2222222222222222222222222222222222222222", False),
        # No upstream revision was known, so no observation was possible -- `None`, and it has to
        # survive as `None` rather than being flattened into `False` by the JSON round trip, since
        # the two mean different things to a Phase 5 reader (drift.py, `matches_upstream`).
        (None, None),
    ],
)
def test_every_observed_upstream_state_round_trips(
    tmp_path: Path, upstream_sha: str | None, matches_upstream: bool | None
) -> None:
    entry = _entry(upstream_sha=upstream_sha, matches_upstream=matches_upstream)

    written = write_spool_entry(tmp_path / "spool", entry)

    assert read_spool_entry(written) == entry


def test_write_creates_the_spool_directory_if_absent(tmp_path: Path) -> None:
    spool_dir = tmp_path / "does" / "not" / "exist" / "yet"

    write_spool_entry(spool_dir, _entry())

    assert spool_dir.is_dir()


def test_two_entries_written_in_sequence_get_distinct_files(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"

    write_spool_entry(spool_dir, _entry(path="a.md"))
    write_spool_entry(spool_dir, _entry(path="b.md"))

    files = list_spool_files(spool_dir)
    assert len(files) == 2
    assert {read_spool_entry(f).path for f in files} == {"a.md", "b.md"}


def test_no_temp_file_survives_a_successful_write(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"

    write_spool_entry(spool_dir, _entry())

    leftover_tmp_files = [p for p in spool_dir.iterdir() if p.suffix == ".tmp"]
    assert leftover_tmp_files == []


def test_list_spool_files_ignores_tmp_files(tmp_path: Path) -> None:
    """A `.tmp` file mid-write (or one left behind by a crash between mkstemp and replace) must
    never be picked up as a complete entry -- only files that made it through `os.replace` (the
    real `.json` suffix) count."""
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    (spool_dir / ".partial-write.tmp").write_text("not a complete entry")

    assert list_spool_files(spool_dir) == []


def test_list_spool_files_on_a_missing_directory_is_empty_not_an_error(tmp_path: Path) -> None:
    assert list_spool_files(tmp_path / "never-created") == []


def test_list_spool_files_is_sorted_oldest_write_first(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"
    write_spool_entry(spool_dir, _entry(path="first.md"))
    write_spool_entry(spool_dir, _entry(path="second.md"))
    write_spool_entry(spool_dir, _entry(path="third.md"))

    ordered_paths = [read_spool_entry(f).path for f in list_spool_files(spool_dir)]

    assert ordered_paths == ["first.md", "second.md", "third.md"]


def test_non_ascii_path_and_patch_round_trip_through_serialization(tmp_path: Path) -> None:
    """The json ensure_ascii=True guarantee this module's docstring describes -- a path/patch
    carrying a byte that was never valid UTF-8 comes back from GitRunner as a lone surrogate
    codepoint (PEP 383). This proves the round trip survives that, not just ordinary Unicode."""
    spool_dir = tmp_path / "spool"
    surrogate_laden_path = "40-journal/2026-07-31-\udcff-note.md"
    surrogate_laden_patch = "content with a lone surrogate: \udcfe\n"
    entry = _entry(path=surrogate_laden_path, patch=surrogate_laden_patch)

    written = write_spool_entry(spool_dir, entry)
    round_tripped = read_spool_entry(written)

    assert round_tripped.path == surrogate_laden_path
    assert round_tripped.patch == surrogate_laden_patch
    # And the file on disk is genuinely plain ASCII -- proves `ensure_ascii=True` actually fired,
    # not merely that some encoding round-tripped by luck.
    written.read_text(encoding="ascii")


def test_injected_write_failure_raises_spool_write_error_not_a_bare_oserror(tmp_path: Path) -> None:
    """The seam the Phase 2 acceptance test injects a failure into (docs/DESIGN.md §7 Phase 2:
    "Force the spool write to fail for a drift patch"). A real local disk write failure (full
    disk, permission error, anything `OSError`-shaped) must surface as `SpoolWriteError`, the type
    `cycle.py` catches to gate the cycle -- never an uncaught bare `OSError`."""
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    spool_dir.chmod(0o500)  # read+execute only: mkstemp inside it must fail with PermissionError
    try:
        with pytest.raises(SpoolWriteError):
            write_spool_entry(spool_dir, _entry())
    finally:
        spool_dir.chmod(0o700)


def test_failure_between_mkstemp_and_replace_leaves_no_temp_file_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The narrower failure window than "the directory is unwritable": the temp file was created
    successfully (so it exists on disk, mid-write), and only the final `os.replace` fails. This is
    the exact window atomicity has to survive -- proven here by injecting the failure precisely
    there (monkeypatching `os.replace`, not the filesystem's own semantics, which real-git/rsync
    tests elsewhere in this suite deliberately never mock) and asserting the temp file doesn't
    survive the raised `SpoolWriteError`."""
    spool_dir = tmp_path / "spool"

    def _failing_replace(_src: str, _dst: str) -> None:
        raise OSError("simulated failure between mkstemp and replace")

    monkeypatch.setattr(os, "replace", _failing_replace)

    with pytest.raises(SpoolWriteError):
        write_spool_entry(spool_dir, _entry())

    leftover = list(spool_dir.iterdir())
    assert leftover == [], f"expected no files left behind, found: {leftover}"


def test_serialized_content_matches_json_schema(tmp_path: Path) -> None:
    """Not load-bearing for this codebase (nothing here parses the file except `read_spool_entry`),
    but pins the on-disk shape so a future Phase 5 consumer reading these files directly (before
    drift-processor exists) has something stable to target."""
    spool_dir = tmp_path / "spool"
    entry = _entry(path="note.md", kind="create", patch="whole file content\n")

    written = write_spool_entry(spool_dir, entry)
    data = json.loads(written.read_text())

    assert data == {
        "kind": "create",
        "path": "note.md",
        "old_path": None,
        "patch": "whole file content\n",
        "baseline_sha": "1111111111111111111111111111111111111111",
        "upstream_sha": "2222222222222222222222222222222222222222",
        "matches_upstream": False,
    }


def test_an_entry_written_before_the_provenance_fields_existed_still_reads_back(tmp_path: Path) -> None:
    """An operator upgrading local-replicator with a non-empty spool. The old entry has no
    provenance keys, and the drainer has to be able to send it on: indexing them would raise
    `KeyError` inside `drain_once`'s read, which happens *outside* its per-entry `try`, wedging
    every entry behind it. Absent reads back as `None` -- not recorded -- never as an invented sha
    or a `False` claiming an observation was made and came back negative."""
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    legacy = spool_dir / "00000000000000000000-legacy.json"
    legacy.write_text(json.dumps({"kind": "modify", "path": "note.md", "old_path": None, "patch": "diff\n"}))

    entry = read_spool_entry(legacy)

    assert entry.path == "note.md"
    assert entry.baseline_sha is None
    assert entry.upstream_sha is None
    assert entry.matches_upstream is None
