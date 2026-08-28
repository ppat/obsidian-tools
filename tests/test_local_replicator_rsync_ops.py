"""Tests for obsidian_tools/local_replicator/rsync_ops.py.

Real `rsync` invocations against real temp directories throughout -- no mocking. The behaviour
under test here is rsync's own semantics (`--delete` vs `--delete-excluded`, exclude-pattern
matching, direction), which is exactly the risk mocking rsync would hide.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from obsidian_tools.local_replicator.exclude import OBSIDIAN_BASELINE_EXCLUDE
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
    """Deliberately no `.obsidian/`-relative path here. Both rsync calls exclude that directory
    wholesale, so an assertion naming one would pass whatever `SHARED_EXCLUDE_LIST` contained --
    the shared list's two workspace entries are pinned separately, below, in the one configuration
    that can still discriminate them."""
    baseline = tmp_path / "baseline"
    icloud = tmp_path / "icloud"
    baseline.mkdir()
    _write(icloud / ".DS_Store", "finder metadata\n")
    _write(icloud / "note.md.icloud", "dataless placeholder stub\n")

    overlay(icloud, baseline)

    assert not (baseline / ".DS_Store").exists()
    assert not (baseline / "note.md.icloud").exists()


def test_the_shared_lists_workspace_entries_suppress_them_without_the_wholesale_exclusion(
    tmp_path: Path,
) -> None:
    """The two `.obsidian/workspace*.json` entries in `SHARED_EXCLUDE_LIST` are unreachable at both
    of this module's call sites, which pass `OBSIDIAN_BASELINE_EXCLUDE` and so cover them by a wider
    rule. They are kept as the narrow rule that would still be right if the wholesale exclusion ever
    came off (`exclude.py`), and this is the configuration in which that claim is testable at all:
    without it nothing in the suite distinguishes the entries being present from their being deleted
    as dead, and the reversal they exist to protect would be quietly incomplete."""
    baseline = tmp_path / "baseline"
    icloud = tmp_path / "icloud"
    _write(baseline / ".obsidian" / "app.json", "a settings file the baseline does place\n")
    _write(baseline / ".obsidian" / "workspace.json", '{"instance": "cluster"}\n')
    _write(baseline / ".obsidian" / "workspaces.json", '{"instance": "cluster"}\n')
    icloud.mkdir()

    publish(baseline, icloud, extra_excludes=[])

    assert (icloud / ".obsidian" / "app.json").exists()
    assert not (icloud / ".obsidian" / "workspace.json").exists()
    assert not (icloud / ".obsidian" / "workspaces.json").exists()


def test_overlay_leaves_the_baselines_obsidian_directory_untouched(tmp_path: Path) -> None:
    """`.obsidian/` is a one-time seed, not synced content, so `publish` never writes it back to the
    device. An overlay that copied the device's copy in would therefore report drift this cycle has
    structurally decided never to resolve -- the same unchanged bytes read as fresh drift on every
    subsequent cycle, forever (ppat/obsidian-tools#46). Observation covers exactly what publication
    can act on, which means both directions exclude it."""
    baseline = tmp_path / "baseline"
    icloud = tmp_path / "icloud"
    _write(baseline / ".obsidian" / "app.json", "frozen baseline content\n")
    _write(icloud / ".obsidian" / "app.json", "device has since reconfigured this\n")
    _write(icloud / ".obsidian" / "plugins" / "dataview" / "data.json", "device-only plugin state\n")

    overlay(icloud, baseline)

    assert (baseline / ".obsidian" / "app.json").read_text() == "frozen baseline content\n"
    assert not (baseline / ".obsidian" / "plugins").exists()


def test_overlay_never_deletes_the_baselines_obsidian_when_the_device_has_none(tmp_path: Path) -> None:
    """The `--delete` half of the same exclude, and the state the operator reaches on purpose: the
    documented device reset deletes `.obsidian/` from the iCloud vault by hand. An unexcluded
    `--delete` strips the parked checkout's own baseline in response, staging the entire frozen
    `.obsidian/` set as device-side deletions on the very cycle that is about to re-seed it."""
    baseline = tmp_path / "baseline"
    icloud = tmp_path / "icloud"
    _write(baseline / ".obsidian" / "app.json", "frozen baseline content\n")
    _write(icloud / "note.md", "the vault the device still has\n")

    overlay(icloud, baseline)

    assert (baseline / ".obsidian" / "app.json").read_text() == "frozen baseline content\n"


@pytest.mark.parametrize("device_entry", ["symlink", "file"])
def test_overlay_holds_the_obsidian_exclusion_against_non_directory_entries(tmp_path: Path, device_entry: str) -> None:
    """The exclude pattern must match `.obsidian` whatever kind of entry it currently is. A
    trailing slash restricts an rsync filter rule to directories, so the two shapes that are not
    one are not covered by it at all: a symlink, and a plain *file* of that name. `--delete` then
    removes the parked baseline's real directory and puts the entry in its place, destroying the
    clone's settings baseline; in the symlink case `git add -A` additionally stages a mode-120000
    blob naming the target path (ppat/obsidian-tools#22, and `vault_git/baseline.py`'s ignore rule,
    which drops its own trailing slash for this reason). Both shapes, because `.git` one constant
    over meets the same trap and is parametrized over both -- a claim of "whatever kind of entry it
    is" that only ever ran the symlink is a claim wider than its evidence. Exercised through
    `overlay` rather than against the pattern string, because it is rsync's filter semantics that
    decide it."""
    baseline = tmp_path / "baseline"
    icloud = tmp_path / "icloud"
    outside_the_vault = tmp_path / "outside-the-vault"
    _write(baseline / ".obsidian" / "app.json", "frozen baseline content\n")
    _write(outside_the_vault / "app.json", "content of a directory the vault does not own\n")
    _write(icloud / "note.md", "the vault the device still has\n")
    if device_entry == "symlink":
        (icloud / ".obsidian").symlink_to(outside_the_vault)
    else:
        (icloud / ".obsidian").write_text("a plain file where the device's settings should be\n")

    overlay(icloud, baseline)

    assert not (baseline / ".obsidian").is_symlink()
    assert (baseline / ".obsidian").is_dir()
    assert (baseline / ".obsidian" / "app.json").read_text() == "frozen baseline content\n"


@pytest.mark.parametrize("device_entry", ["symlink", "file"])
def test_overlay_holds_the_git_metadata_exclusion_against_non_directory_entries(
    tmp_path: Path, device_entry: str
) -> None:
    """`.git` meets the same trap as `.obsidian`, one constant over, and the consequence is heavier:
    `--delete` replaces the parked clone's own repository with whatever the device holds -- silently,
    at exit 0 -- after which every `GitRunner` invocation of the next cycle addresses something else,
    or nothing. Two entry shapes defeat a directory-shaped rule, not one: a symlink, and a plain
    *file* named `.git`, which is exactly what `git worktree add` and a submodule produce.

    Nothing in this system puts either shape in the vault -- `vault_git/runner.py` is built on there
    never being a `.git` inside the vault directory at all -- so what the bare name buys is that the
    clone survives one arriving from outside it (ppat/obsidian-tools#73). The recovery is what makes
    that worth buying: clearing the clone is the documented remedy, and it leaves the next cycle with
    no `LAST_CHECKOUT`, which skips the drift capture entirely and then publishes with `--delete`."""
    baseline = tmp_path / "baseline"
    icloud = tmp_path / "icloud"
    outside_the_vault = tmp_path / "outside-the-vault"
    _write(baseline / ".git" / "HEAD", "ref: refs/heads/main\n")
    _write(baseline / "note.md", "the vault the device still has\n")
    _write(outside_the_vault / "HEAD", "ref: refs/heads/an-unrelated-repository\n")
    _write(icloud / "note.md", "the vault the device still has\n")
    if device_entry == "symlink":
        (icloud / ".git").symlink_to(outside_the_vault)
    else:
        (icloud / ".git").write_text("gitdir: /some/other/repository/.git\n")

    overlay(icloud, baseline)

    assert not (baseline / ".git").is_symlink()
    assert (baseline / ".git").is_dir()
    assert (baseline / ".git" / "HEAD").read_text() == "ref: refs/heads/main\n"


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
    """`.obsidian/`, once already seeded, is excluded wholesale -- the exclusion covers everything
    beneath the directory, not just an entry of that name."""
    baseline = tmp_path / "baseline"
    icloud = tmp_path / "icloud"
    _write(baseline / ".obsidian" / "app.json", "frozen baseline content\n")
    _write(icloud / ".obsidian" / "app.json", "device has since reconfigured this\n")
    _write(icloud / ".obsidian" / "community-plugins.json", "a plugin only the device knows about\n")

    publish(baseline, icloud, extra_excludes=[OBSIDIAN_BASELINE_EXCLUDE])

    assert (icloud / ".obsidian" / "app.json").read_text() == "device has since reconfigured this\n"
    assert (icloud / ".obsidian" / "community-plugins.json").exists()
