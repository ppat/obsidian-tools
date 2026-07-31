"""Tests for obsidian_tools/local_replicator/rsync_ops.py.

Real `rsync` invocations against real temp directories throughout -- no mocking. The behaviour
under test here is rsync's own semantics (`--delete` vs `--delete-excluded`, exclude-pattern
matching, direction), which is exactly the risk mocking rsync would hide.
"""

from __future__ import annotations

import os
from pathlib import Path

from obsidian_tools.local_replicator.rsync_ops import overlay, publish


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# --- overlay (step 2: device tree onto the parked baseline) ------------------------------------


def test_overlay_copies_device_content_onto_the_baseline(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    icloud = tmp_path / "icloud"
    _write(baseline / "note.md", "baseline content\n")
    _write(icloud / "note.md", "human edited this on the device\n")

    overlay(icloud, baseline)

    assert (baseline / "note.md").read_text() == "human edited this on the device\n"


def test_overlay_brings_a_device_created_file_into_the_baseline(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    icloud = tmp_path / "icloud"
    baseline.mkdir()
    _write(icloud / "typed-on-phone.md", "new note\n")

    overlay(icloud, baseline)

    assert (baseline / "typed-on-phone.md").read_text() == "new note\n"


def test_overlay_deletes_a_baseline_path_the_device_no_longer_has(tmp_path: Path) -> None:
    """The `--delete` half of the "needs --delete AND must exclude .git" trap: without it, a note
    deleted on the phone would leave the baseline copy in place and the deletion would never
    register as drift once the caller diffs the overlaid tree against git's HEAD."""
    baseline = tmp_path / "baseline"
    icloud = tmp_path / "icloud"
    _write(baseline / "deleted-by-human.md", "still here in the baseline\n")
    icloud.mkdir()

    overlay(icloud, baseline)

    assert not (baseline / "deleted-by-human.md").exists()


def test_overlay_never_deletes_the_baselines_own_git_directory(tmp_path: Path) -> None:
    """The `.git` half of the same trap: the iCloud side never has a `.git` of its own, so an
    unexcluded `--delete` would otherwise remove the checkout's own repository."""
    baseline = tmp_path / "baseline"
    icloud = tmp_path / "icloud"
    _write(baseline / ".git" / "config", "[core]\n")
    _write(baseline / ".git" / "HEAD", "ref: refs/heads/main\n")
    icloud.mkdir()

    overlay(icloud, baseline)

    assert (baseline / ".git" / "config").exists()
    assert (baseline / ".git" / "HEAD").exists()


def test_overlay_applies_a_same_size_same_mtime_edit(tmp_path: Path) -> None:
    """The same rsync quick-check trap `test_publish_overwrites_a_same_size_same_mtime_file_with_different_content`
    proves for `publish`, on `overlay`'s reversed direction -- a device edit the same length as the
    baseline's content, landing at the same mtime, must still register."""
    baseline = tmp_path / "baseline"
    icloud = tmp_path / "icloud"
    same_length_old = "baseline content \n"
    same_length_new = "device-typed edit\n"
    assert len(same_length_old) == len(same_length_new)
    _write(baseline / "note.md", same_length_old)
    _write(icloud / "note.md", same_length_new)
    matched_mtime = (baseline / "note.md").stat().st_mtime
    os.utime(icloud / "note.md", (matched_mtime, matched_mtime))

    overlay(icloud, baseline)

    assert (baseline / "note.md").read_text() == same_length_new


def test_overlay_suppresses_the_shared_exclude_list(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    icloud = tmp_path / "icloud"
    baseline.mkdir()
    _write(icloud / ".DS_Store", "finder metadata\n")
    _write(icloud / ".obsidian" / "workspace.json", '{"instance": "device"}\n')
    _write(icloud / ".obsidian" / "workspaces.json", '{"instance": "device"}\n')
    _write(icloud / "note.md.icloud", "dataless placeholder stub\n")

    overlay(icloud, baseline)

    assert not (baseline / ".DS_Store").exists()
    assert not (baseline / ".obsidian" / "workspace.json").exists()
    assert not (baseline / ".obsidian" / "workspaces.json").exists()
    assert not (baseline / "note.md.icloud").exists()


# --- publish (step 6: baseline back out to iCloud) ----------------------------------------------


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
    """The `--delete-excluded` trap: plain `--delete` must never remove -- nor overwrite -- a path
    named in `extra_excludes`."""
    baseline = tmp_path / "baseline"
    icloud = tmp_path / "icloud"
    _write(baseline / "protected.md", "new upstream content that must not land yet\n")
    _write(icloud / "protected.md", "the human's uncaptured edit\n")
    _write(baseline / "ordinary.md", "ordinary content\n")

    publish(baseline, icloud, extra_excludes=["protected.md"])

    assert (icloud / "protected.md").read_text() == "the human's uncaptured edit\n"
    assert (icloud / "ordinary.md").read_text() == "ordinary content\n"


def test_publish_overwrites_a_same_size_same_mtime_file_with_different_content(tmp_path: Path) -> None:
    """rsync's default "quick check" skips a file whose size *and* mtime already match the
    destination without ever comparing content -- found directly, not by inspection, when a
    same-length device edit landing in the same second as a `git checkout` silently survived a
    publish that should have overwritten it. `--checksum` is what this function relies on to force
    a real content comparison; this test fails immediately if that flag is ever dropped."""
    baseline = tmp_path / "baseline"
    icloud = tmp_path / "icloud"
    same_length_old = "uncaptured human edit\n"  # 22 bytes
    same_length_new = "# Home (agent update)\n"  # 22 bytes
    assert len(same_length_old) == len(same_length_new)
    _write(baseline / "note.md", same_length_new)
    _write(icloud / "note.md", same_length_old)
    # Force matching mtimes -- the exact condition that let the stale content survive.
    matched_mtime = (baseline / "note.md").stat().st_mtime
    os.utime(icloud / "note.md", (matched_mtime, matched_mtime))

    publish(baseline, icloud, extra_excludes=[])

    assert (icloud / "note.md").read_text() == same_length_new


def test_publish_leaves_an_excluded_directory_untouched(tmp_path: Path) -> None:
    """`.obsidian/`, once already seeded, is excluded wholesale -- proves a directory-shaped
    exclude protects everything beneath it, not just a bare filename."""
    baseline = tmp_path / "baseline"
    icloud = tmp_path / "icloud"
    _write(baseline / ".obsidian" / "app.json", "frozen baseline content\n")
    _write(icloud / ".obsidian" / "app.json", "device has since reconfigured this\n")
    _write(icloud / ".obsidian" / "community-plugins.json", "a plugin only the device knows about\n")

    publish(baseline, icloud, extra_excludes=[".obsidian/"])

    assert (icloud / ".obsidian" / "app.json").read_text() == "device has since reconfigured this\n"
    assert (icloud / ".obsidian" / "community-plugins.json").exists()
