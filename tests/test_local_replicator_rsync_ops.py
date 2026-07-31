"""Tests for obsidian_tools/local_replicator/rsync_ops.py.

Real `rsync` invocations against real temp directories throughout — no mocking. The behaviour
under test here is rsync's own semantics (itemize output format, `--delete` vs `--delete-excluded`,
exclude-pattern matching), which is exactly the risk mocking rsync would hide.
"""

from __future__ import annotations

from pathlib import Path

from obsidian_tools.local_replicator.rsync_ops import compare_dry_run, publish


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_compare_reports_changed_added_and_deleted_paths(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    icloud = tmp_path / "icloud"
    _write(baseline / "unchanged.md", "same\n")
    _write(icloud / "unchanged.md", "same\n")
    _write(baseline / "edited.md", "original\n")
    _write(icloud / "edited.md", "human edited this\n")
    _write(baseline / "deleted-by-human.md", "still in baseline\n")  # absent from icloud
    _write(icloud / "created-by-human.md", "new note typed on the phone\n")  # absent from baseline

    drifted = {item.path: item.kind for item in compare_dry_run(baseline, icloud)}

    assert "unchanged.md" not in drifted
    assert drifted["edited.md"] == "copy"
    assert drifted["deleted-by-human.md"] == "copy"  # baseline has it, icloud doesn't: needs copying
    assert drifted["created-by-human.md"] == "delete"  # icloud has it, baseline doesn't: extraneous


def test_compare_does_not_report_directories(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    icloud = tmp_path / "icloud"
    _write(baseline / "10-areas" / "new-subdir" / "note.md", "content\n")
    icloud.mkdir()

    drifted = [item.path for item in compare_dry_run(baseline, icloud)]

    assert drifted == ["10-areas/new-subdir/note.md"]


def test_shared_exclude_list_suppresses_spurious_drift(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    icloud = tmp_path / "icloud"
    baseline.mkdir()
    _write(icloud / ".DS_Store", "finder metadata\n")
    _write(icloud / ".obsidian" / "workspace.json", '{"instance": "device"}\n')
    _write(icloud / ".obsidian" / "workspaces.json", '{"instance": "device"}\n')
    _write(icloud / "note.md.icloud", "dataless placeholder stub\n")

    drifted = compare_dry_run(baseline, icloud)

    assert drifted == []


def test_compare_excludes_git_metadata(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    icloud = tmp_path / "icloud"
    _write(baseline / ".git" / "config", "[core]\n")  # the parked clone's own git-dir
    icloud.mkdir()

    drifted = compare_dry_run(baseline, icloud)

    assert drifted == []


def test_publish_copies_tree_and_deletes_extraneous_files(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    icloud = tmp_path / "icloud"
    _write(baseline / "note.md", "authoritative content\n")
    _write(icloud / "stale-orphan.md", "upstream deleted this a while ago\n")

    publish(baseline, icloud, extra_excludes=[])

    assert (icloud / "note.md").read_text() == "authoritative content\n"
    assert not (icloud / "stale-orphan.md").exists()


def test_publish_never_copies_git_metadata(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    icloud = tmp_path / "icloud"
    _write(baseline / ".git" / "config", "[core]\n")
    _write(baseline / "note.md", "content\n")

    publish(baseline, icloud, extra_excludes=[])

    assert not (icloud / ".git").exists()
    assert (icloud / "note.md").exists()


def test_publish_leaves_an_excluded_path_untouched_by_content_and_by_delete(tmp_path: Path) -> None:
    """The `--delete-excluded` trap: plain `--delete` must never remove — nor overwrite — a path
    named in `extra_excludes`, which is how cycle.py protects a path whose capture failed."""
    baseline = tmp_path / "baseline"
    icloud = tmp_path / "icloud"
    _write(baseline / "protected.md", "new upstream content that must not land yet\n")
    _write(icloud / "protected.md", "the human's uncaptured edit\n")
    _write(baseline / "ordinary.md", "ordinary content\n")

    publish(baseline, icloud, extra_excludes=["protected.md"])

    assert (icloud / "protected.md").read_text() == "the human's uncaptured edit\n"
    assert (icloud / "ordinary.md").read_text() == "ordinary content\n"


def test_publish_leaves_an_excluded_directory_untouched(tmp_path: Path) -> None:
    """`.obsidian/`, once already seeded, is excluded wholesale — proves a directory-shaped
    exclude protects everything beneath it, not just a bare filename."""
    baseline = tmp_path / "baseline"
    icloud = tmp_path / "icloud"
    _write(baseline / ".obsidian" / "app.json", "frozen baseline content\n")
    _write(icloud / ".obsidian" / "app.json", "device has since reconfigured this\n")
    _write(icloud / ".obsidian" / "community-plugins.json", "a plugin only the device knows about\n")

    publish(baseline, icloud, extra_excludes=[".obsidian/"])

    assert (icloud / ".obsidian" / "app.json").read_text() == "device has since reconfigured this\n"
    assert (icloud / ".obsidian" / "community-plugins.json").exists()
