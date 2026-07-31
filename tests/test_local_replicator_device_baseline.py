"""Tests for obsidian_tools/local_replicator/device_baseline.py.

The property under test that matters most: a partial first copy must never be mistaken for a
complete one (docs/DESIGN.md §7 Phase 2, "the first copy has to be atomic"). Presence is decided by
a completion marker written only after every file has copied, not by the directory merely existing.
"""

from __future__ import annotations

from pathlib import Path

from obsidian_tools.local_replicator.device_baseline import BASELINE_MARKER, is_baselined, seed_baseline


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_not_baselined_when_obsidian_absent(tmp_path: Path) -> None:
    icloud = tmp_path / "icloud"
    icloud.mkdir()

    assert is_baselined(icloud) is False


def test_seed_copies_files_and_writes_completion_marker(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    icloud = tmp_path / "icloud"
    _write(clone / ".obsidian" / "app.json", '{"legacyEditor": false}\n')
    _write(clone / ".obsidian" / "types.json", "{}\n")
    icloud.mkdir()

    seed_baseline(clone, icloud)

    assert is_baselined(icloud) is True
    assert (icloud / ".obsidian" / "app.json").read_text() == '{"legacyEditor": false}\n'
    assert (icloud / ".obsidian" / "types.json").read_text() == "{}\n"


def test_seed_never_copies_workspace_state_files(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    icloud = tmp_path / "icloud"
    _write(clone / ".obsidian" / "app.json", "{}\n")
    _write(clone / ".obsidian" / "workspace.json", '{"frozen": "baseline copy, never wanted"}\n')
    _write(clone / ".obsidian" / "workspaces.json", '{"frozen": "baseline copy, never wanted"}\n')
    icloud.mkdir()

    seed_baseline(clone, icloud)

    assert not (icloud / ".obsidian" / "workspace.json").exists()
    assert not (icloud / ".obsidian" / "workspaces.json").exists()


def test_a_partial_prior_copy_is_not_mistaken_for_complete(tmp_path: Path) -> None:
    """Simulates a first sync that died partway: `.obsidian/` exists at the destination, with some
    baseline content, but no completion marker — because the process was killed before it could
    write one. `is_baselined` must say False, and re-running the seed must finish the job."""
    clone = tmp_path / "clone"
    icloud = tmp_path / "icloud"
    _write(clone / ".obsidian" / "app.json", "{}\n")
    _write(clone / ".obsidian" / "types.json", '{"salience": "number"}\n')
    # Simulate the interrupted run: only app.json made it across before the process died.
    _write(icloud / ".obsidian" / "app.json", "{}\n")

    assert is_baselined(icloud) is False

    seed_baseline(clone, icloud)

    assert is_baselined(icloud) is True
    assert (icloud / ".obsidian" / "types.json").read_text() == '{"salience": "number"}\n'


def test_seed_is_a_noop_when_no_source_obsidian_exists_yet(tmp_path: Path) -> None:
    """The committer hasn't taken its own .obsidian baseline commit yet (docs/DESIGN.md §8a D3) —
    nothing to seed from. Must not create a half-baseline or a marker with no content behind it."""
    clone = tmp_path / "clone"
    icloud = tmp_path / "icloud"
    clone.mkdir()
    icloud.mkdir()

    seed_baseline(clone, icloud)

    assert is_baselined(icloud) is False
    assert not (icloud / ".obsidian").exists()


def test_marker_file_name_is_not_mistaken_for_vault_content(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    icloud = tmp_path / "icloud"
    _write(clone / ".obsidian" / "app.json", "{}\n")
    icloud.mkdir()

    seed_baseline(clone, icloud)

    assert (icloud / ".obsidian" / BASELINE_MARKER).exists()
