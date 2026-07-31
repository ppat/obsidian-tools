"""Tests for obsidian_tools/local_replicator/capture.py.

The property under test that matters most for the rest of the cycle: a capture failure must be
reported (`False`), never raised past `capture_path` — and the failure must be injectable without
faking iCloud/NFS I/O semantics, since that's the seam the Phase 2 acceptance test uses (see the
module's own docstring and docs/DESIGN.md §7 Phase 2).
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from obsidian_tools.local_replicator.capture import CaptureError, CaptureSink, capture_path, discard_sink
from obsidian_tools.local_replicator.rsync_ops import DriftedPath


def _recording_sink(record: list[tuple[str, bytes | None]]) -> CaptureSink:
    def sink(path: str, content: bytes | None) -> None:
        record.append((path, content))

    return sink


def test_discard_sink_accepts_bytes_and_none_without_error() -> None:
    discard_sink("note.md", b"content")
    discard_sink("deleted.md", None)


def test_successful_capture_reads_the_current_bytes(tmp_path: Path) -> None:
    (tmp_path / "note.md").write_text("human's edit\n")
    captured: list[tuple[str, bytes | None]] = []

    ok = capture_path(tmp_path, DriftedPath(path="note.md", kind="copy"), sink=_recording_sink(captured))

    assert ok is True
    assert captured == [("note.md", b"human's edit\n")]


def test_capture_of_a_deleted_path_succeeds_with_no_content(tmp_path: Path) -> None:
    """A path the comparison flagged that no longer exists in iCloud (a human deleted it) has
    nothing to preserve — capture succeeds trivially rather than treating a missing file as an
    error."""
    captured: list[tuple[str, bytes | None]] = []

    ok = capture_path(tmp_path, DriftedPath(path="gone.md", kind="copy"), sink=_recording_sink(captured))

    assert ok is True
    assert captured == [("gone.md", None)]


def test_injected_sink_failure_is_reported_not_raised(tmp_path: Path) -> None:
    """The genuine seam: a test (or, in Phase 5, a real drift-stream publish) can fail capture
    without faking a filesystem-level read error."""
    (tmp_path / "note.md").write_text("content\n")

    def failing_sink(path: str, content: bytes | None) -> None:
        del path, content
        raise CaptureError("simulated Phase 5 publish failure")

    ok = capture_path(tmp_path, DriftedPath(path="note.md", kind="copy"), sink=failing_sink)

    assert ok is False


def test_unreadable_file_capture_fails_without_raising(tmp_path: Path) -> None:
    """A real, unmocked read failure — not a stand-in for NFS/iCloud semantics specifically, but a
    faithful demonstration that `capture_path` never lets an OSError escape."""
    target = tmp_path / "note.md"
    target.write_text("content\n")
    target.chmod(0)
    try:
        try:
            target.read_bytes()
        except PermissionError:
            pass
        else:
            pytest.skip("running as a user that bypasses file permissions (e.g. root); can't force a real EACCES")

        ok = capture_path(tmp_path, DriftedPath(path="note.md", kind="copy"))

        assert ok is False
    finally:
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)
