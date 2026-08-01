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


def test_seed_applies_the_shared_baseline_allowlist_not_a_denylist(tmp_path: Path) -> None:
    """The seed used to exclude by name (two workspace-state filenames) rather than include by
    allowlist (`vault_git/baseline_selector.py`) — the same shape of bug an independent review found
    three times on the committer side (ppat/obsidian-tools#3, #22): a denylist only ever knows about
    the holes someone already noticed. This plants exactly those shapes under a fresh `.obsidian/`
    and asserts none of them reach the device copy, even though none is named
    `workspace.json`/`workspaces.json`:

    - a plugin's `data.json`, which for the Local REST API plugin holds a bearer token;
    - a file at an unexpected depth under `themes/` (`themes/deep/nested/inner/data.json`), which a
      bare directory-prefix rule would admit at any depth;
    - a symlinked plugin file (`plugins/.../main.js`, an otherwise-allowlisted name) pointing outside
      `.obsidian/` entirely.

    Every one of these was captured by the old two-name denylist (nothing here is named
    workspace.json/workspaces.json), so this test fails against it — see PR description for the
    reproduced failure — and only passes once seeding goes through the shared allowlist selector.
    """
    clone = tmp_path / "clone"
    icloud = tmp_path / "icloud"
    _write(clone / ".obsidian" / "app.json", "{}\n")
    _write(
        clone / ".obsidian" / "plugins" / "obsidian-local-rest-api" / "data.json",
        '{"apiKey": "not-a-real-token-but-shaped-like-one"}\n',
    )
    _write(clone / ".obsidian" / "themes" / "deep" / "nested" / "inner" / "data.json", '{"leaked": true}\n')
    outside_secret = tmp_path / "outside-obsidian-secret.txt"
    outside_secret.write_text("must never reach iCloud\n")
    symlinked_plugin_file = clone / ".obsidian" / "plugins" / "obsidian-local-rest-api" / "main.js"
    symlinked_plugin_file.parent.mkdir(parents=True, exist_ok=True)
    symlinked_plugin_file.symlink_to(outside_secret)
    icloud.mkdir()

    seed_baseline(clone, icloud)

    assert (icloud / ".obsidian" / "app.json").exists()
    assert not (icloud / ".obsidian" / "plugins" / "obsidian-local-rest-api" / "data.json").exists()
    assert not (icloud / ".obsidian" / "themes" / "deep" / "nested" / "inner" / "data.json").exists()
    assert not (icloud / ".obsidian" / "plugins" / "obsidian-local-rest-api" / "main.js").exists()


def test_seed_refuses_a_symlinked_source_obsidian_dir(tmp_path: Path) -> None:
    """The per-path allowlist (above) only ever sees entries *inside* `.obsidian/` — nothing
    produces a `PathInfo` for the root itself, so it cannot protect against `.obsidian` in the
    parked clone being a symlink rather than a real directory (`Path.is_dir()` resolves symlinks,
    so the existing `if not source.is_dir()` guard reads a symlinked `.obsidian` as "present" and
    happily walks through it). `vault_git/baseline.py` guards this exact case on the committer side
    (ppat/obsidian-tools#22); this is the same rule, one level up, on the replicator side.

    Plants a symlinked `.obsidian` pointing at a directory holding both an allowlisted-shaped file
    (`app.json`) and a plugin `data.json`, and asserts *nothing* reaches the device copy — not even
    `app.json`, which the per-path selector would happily allow if it ever saw it. That's what makes
    this red on the per-path fix alone: `data.json` is already blocked by the allowlist regardless of
    the root, but `app.json` is not, so its absence is the only signal that proves the root guard
    fired rather than the per-path selector doing unrelated work.
    """
    clone = tmp_path / "clone"
    clone.mkdir()
    icloud = tmp_path / "icloud"
    icloud.mkdir()
    outside_target = tmp_path / "outside-obsidian"
    _write(outside_target / "app.json", "{}\n")
    _write(outside_target / "data.json", '{"apiKey": "must never reach iCloud"}\n')
    (clone / ".obsidian").symlink_to(outside_target)

    seed_baseline(clone, icloud)

    assert is_baselined(icloud) is False
    assert not (icloud / ".obsidian").exists()
