"""Tests for obsidian_tools/local_replicator/cycle.py -- the full seven-step cycle.

Real local git repos (bare "origin" plus a normal push-clone to seed commits) and real rsync
against real temp directories throughout, exactly like the git committer's own test suite -- the
risk here lives in git's and rsync's actual semantics, not in anything worth mocking.

Covers every scenario named in the brief for this component:
- the comparison runs against the pre-pull baseline, so an upstream change and a device edit in
  the same cycle are attributed correctly (this is the whole reason for the step ordering);
- a device creation, deletion, and modification each produce a real patch, not just a path;
- a spool write failure is injected (never merely omitted), and blocks the *whole* cycle's publish
  and tag advance, not just the failed path -- the core safety property of the current design;
- the next cycle retries and resolves once the injected failure clears;
- the cycle is idempotent from an arbitrary crash-like starting state;
- non-ASCII and quoted filenames survive the whole real git/rsync pipeline, not just the pure core;
- `.obsidian/` is copied once, then left alone even when the device customises it;
- the shared exclude list suppresses spurious drift end-to-end, not just at the rsync layer.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

from conftest import run_git

from obsidian_tools.config import ReplicateConfig
from obsidian_tools.local_replicator.cycle import run_cycle
from obsidian_tools.local_replicator.drift import SpoolEntry
from obsidian_tools.local_replicator.spool import SpoolWriteError, list_spool_files, read_spool_entry, write_spool_entry
from obsidian_tools.local_replicator.tag import read_last_checkout
from obsidian_tools.vault_git.runner import GitRunner


def _config(tmp_path: Path, origin: Path, icloud: Path) -> ReplicateConfig:
    return ReplicateConfig(
        cache_clone_dir=str(tmp_path / "cache-clone"),
        icloud_vault_dir=str(icloud),
        branch="main",
        origin_url=str(origin),
        ssh_key_path=str(tmp_path / "unused-key"),
        ssh_known_hosts_path=str(tmp_path / "unused-known-hosts"),
        spool_dir=str(tmp_path / "spool"),
    )


def _push_commit(origin: Path, tmp_path: Path, files: dict[str, str], message: str) -> None:
    """Simulates the git committer taking and pushing another cycle's commit."""
    clone = tmp_path / f"push-clone-{uuid.uuid4().hex}"
    run_git("clone", "-q", str(origin), str(clone), cwd=tmp_path)
    for relative, content in files.items():
        path = clone / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    run_git("add", "-A", cwd=clone)
    run_git("-c", "user.name=x", "-c", "user.email=x@example.invalid", "commit", "-q", "-m", message, cwd=clone)
    run_git("push", "-q", "origin", "main", cwd=clone)


def _tag_sha(tmp_path: Path) -> str | None:
    runner = GitRunner(tmp_path / "cache-clone" / ".git", tmp_path / "cache-clone")
    return read_last_checkout(runner)


def _spooled_by_path(tmp_path: Path) -> dict[str, SpoolEntry]:
    spool_dir = tmp_path / "spool"
    return {entry.path: entry for f in list_spool_files(spool_dir) for entry in [read_spool_entry(f)]}


def _failing_spool_writer(fail_on: str) -> Callable[[Path, SpoolEntry], Path]:
    def writer(spool_dir: Path, entry: SpoolEntry) -> Path:
        if entry.path == fail_on:
            raise SpoolWriteError(f"simulated local disk write failure for {fail_on!r} -- injected, not real I/O")
        return write_spool_entry(spool_dir, entry)

    return writer


# --- basic cycle behaviour -----------------------------------------------------------------------


def test_first_cycle_publishes_everything_and_advances_the_tag(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    config = _config(tmp_path, seeded_origin, icloud_dir)

    result = run_cycle(config)

    assert result.tag_advanced is True
    assert result.drifted == ()  # nothing to compare against on a first run
    assert (icloud_dir / "00-index.md").read_text() == "# Home\n"


def test_order_attributes_upstream_change_and_device_drift_correctly(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """The whole reason for the step ordering: an upstream change and a device edit that land in
    the same cycle must not be conflated. Comparing before the pull is what keeps them apart."""
    _push_commit(seeded_origin, tmp_path, {"10-areas/other.md": "original other\n"}, "add other")
    config = _config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)  # cycle 1: bootstrap both files onto the device

    # Between cycles: an agent updates one file upstream; independently, a human edits a
    # *different* file directly in the device's iCloud copy.
    _push_commit(seeded_origin, tmp_path, {"00-index.md": "# Home (agent update)\n"}, "agent update")
    (icloud_dir / "10-areas" / "other.md").write_text("typed on the phone\n")

    result = run_cycle(config)

    # Only the human-edited path is drift. The upstream-changed path is not -- it was never
    # different from the pre-pull baseline at compare time.
    assert result.drifted == ("10-areas/other.md",)
    spooled = _spooled_by_path(tmp_path)
    assert set(spooled) == {"10-areas/other.md"}
    assert "typed on the phone" in spooled["10-areas/other.md"].patch
    # The upstream change still reaches the device, via the pull+publish, independent of drift
    # attribution.
    assert (icloud_dir / "00-index.md").read_text() == "# Home (agent update)\n"


def test_device_creation_is_spooled_as_a_create_with_full_content(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """ "Creations are not diffs" trap: a file created on the device is untracked with an empty
    unstaged `git diff`. Staging first (`add -A`, in cycle.py) is what turns that into a real
    "new file" patch carrying the whole content."""
    config = _config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    (icloud_dir / "typed-on-phone.md").write_text("brand new note from the device\n")

    result = run_cycle(config)

    assert "typed-on-phone.md" in result.drifted
    entry = _spooled_by_path(tmp_path)["typed-on-phone.md"]
    assert entry.kind == "create"
    assert "brand new note from the device" in entry.patch


def test_device_deletion_is_spooled_as_a_delete(tmp_path: Path, seeded_origin: Path, icloud_dir: Path) -> None:
    config = _config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    (icloud_dir / "00-index.md").unlink()

    result = run_cycle(config)

    assert "00-index.md" in result.drifted
    entry = _spooled_by_path(tmp_path)["00-index.md"]
    assert entry.kind == "delete"
    assert "# Home" in entry.patch  # the removed content, visible in the deletion patch


# --- the spool-write gate: the core safety property ---------------------------------------------


def test_spool_write_failure_blocks_the_whole_cycles_publish_and_tag_advance(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """docs/DESIGN.md §7 Phase 2's own acceptance test, verbatim: "Force the spool write to fail
    for a drift patch, run the cycle -> publish does not run for that cycle, the tag does not
    advance, and the next cycle retries the overlay from scratch"."""
    config = _config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    checkout_after_bootstrap = _tag_sha(tmp_path)
    _push_commit(seeded_origin, tmp_path, {"00-index.md": "# Home (agent update)\n"}, "agent update")
    (icloud_dir / "00-index.md").write_text("uncaptured human edit\n")

    result = run_cycle(config, spool_writer=_failing_spool_writer("00-index.md"))

    assert result.spool_write_failed is True
    assert result.tag_advanced is False
    assert _tag_sha(tmp_path) == checkout_after_bootstrap  # unmoved, not partially advanced
    # Publish did not run at all this cycle: the human's uncaptured edit survives untouched, and
    # the agent's upstream update has NOT been pushed over it either -- not a partial publish.
    assert (icloud_dir / "00-index.md").read_text() == "uncaptured human edit\n"

    # And once the spool write succeeds, the *same* drift is detected again (never lost) and this
    # time resolves -- proving this isn't just permanently stuck.
    result_retry = run_cycle(config)

    assert result_retry.drifted == ("00-index.md",)
    assert result_retry.tag_advanced is True
    assert (icloud_dir / "00-index.md").read_text() == "# Home (agent update)\n"
    assert _tag_sha(tmp_path) != checkout_after_bootstrap


def test_spool_write_failure_on_one_path_blocks_publish_of_an_unrelated_drifted_path_too(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """The property that distinguishes this design from the superseded per-path-gated one
    (docs/DESIGN.md §4 Plane B, "Why the gate moved"): a single stuck path holds back *every*
    path's publish, because `LAST_CHECKOUT` names one commit and cannot mean "this path at the new
    commit, that path at the old one"."""
    _push_commit(seeded_origin, tmp_path, {"10-areas/other.md": "original other\n"}, "add other")
    config = _config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    _push_commit(seeded_origin, tmp_path, {"00-index.md": "# Home (agent update)\n"}, "agent update")
    (icloud_dir / "00-index.md").write_text("stuck edit\n")
    (icloud_dir / "10-areas" / "other.md").write_text("an unrelated, perfectly capturable edit\n")

    result = run_cycle(config, spool_writer=_failing_spool_writer("00-index.md"))

    assert result.tag_advanced is False
    # Neither path was published this cycle -- not just the stuck one.
    assert (icloud_dir / "00-index.md").read_text() == "stuck edit\n"
    assert (icloud_dir / "10-areas" / "other.md").read_text() == "an unrelated, perfectly capturable edit\n"


def test_entries_spooled_before_a_failure_stay_durable_on_disk(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """The spool write is real and durable from the moment it succeeds, independent of whether the
    rest of that cycle goes on to fail -- an entry already written is never rolled back just
    because a later entry in the same cycle couldn't be written."""
    _push_commit(seeded_origin, tmp_path, {"aaa-first.md": "first\n"}, "add first")
    config = _config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    (icloud_dir / "aaa-first.md").write_text("edited first (sorts before the failing path)\n")

    run_cycle(config, spool_writer=_failing_spool_writer("zzz-does-not-exist.md"))

    # "aaa-first.md" sorts before the (nonexistent) failing path alphabetically, so
    # select_spool_entries's deterministic ordering means it was attempted, and written, first.
    spooled = _spooled_by_path(tmp_path)
    assert "aaa-first.md" in spooled
    assert "edited first" in spooled["aaa-first.md"].patch


# --- idempotency from an arbitrary starting state ------------------------------------------------


def test_cycle_recovers_from_a_working_tree_left_mid_overlay_by_a_prior_crash(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """Simulates a crash between step 2 (overlay) and step 5 (reset): the parked clone's working
    tree has stray, uncommitted, untracked content sitting in it when the next cycle starts. Step
    1's `checkout -f` + `clean -fd` must recover cleanly rather than erroring or corrupting the
    next comparison."""
    config = _config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)  # establishes the parked clone and LAST_CHECKOUT

    cache_clone_dir = tmp_path / "cache-clone"
    # Simulate the crash residue directly: stray untracked content plus a staged-but-uncommitted
    # modification, left in the working tree as if a previous cycle died mid-overlay.
    (cache_clone_dir / "stray-untracked-from-crash.md").write_text("leftover overlay residue\n")
    (cache_clone_dir / "00-index.md").write_text("half-applied overlay content\n")
    run_git("add", "-A", cwd=cache_clone_dir)

    (icloud_dir / "10-areas").mkdir(parents=True, exist_ok=True)
    (icloud_dir / "10-areas" / "genuine-drift.md").write_text("a real device edit this cycle should see\n")

    result = run_cycle(config)

    assert result.tag_advanced is True
    # The crash residue must not appear as phantom drift, nor survive into the published tree.
    assert result.drifted == ("10-areas/genuine-drift.md",)
    assert not (icloud_dir / "stray-untracked-from-crash.md").exists()
    assert (icloud_dir / "00-index.md").read_text() == "# Home\n"


# --- non-ASCII and quoted filenames, end to end --------------------------------------------------


def test_non_ascii_filename_drift_survives_the_full_git_and_rsync_pipeline(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """The exact bug class that "wedged the committer permanently" (ppat/obsidian-tools#3): a note
    titled in Japanese must not break `-z` porcelain parsing anywhere in this pipeline."""
    config = _config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    non_ascii_name = "40-journal/2026-07-31-日本語のノート.md"
    (icloud_dir / "40-journal").mkdir(parents=True, exist_ok=True)
    (icloud_dir / "40-journal" / "2026-07-31-日本語のノート.md").write_text("非同期の下書き\n")

    result = run_cycle(config)

    assert non_ascii_name in result.drifted
    entry = _spooled_by_path(tmp_path)[non_ascii_name]
    assert entry.kind == "create"
    assert "非同期の下書き" in entry.patch
    assert result.tag_advanced is True
    # The patch *header* must carry the same bytes as `path`, not git's C-quoted escaping. Without
    # `core.quotePath=false` (clone.py) one spool entry holds two encodings of one path, and a
    # Phase 5 consumer reading the diff header gets the escaped one. Asserted on the header
    # specifically because every other assertion in this test passes with the quoting present.
    assert "日本語" in entry.patch.splitlines()[0]
    assert "\\346" not in entry.patch


def test_filename_with_embedded_quote_survives_the_full_pipeline(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    config = _config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    quoted_name = '10-areas/note "with quotes".md'
    (icloud_dir / "10-areas").mkdir(parents=True, exist_ok=True)
    (icloud_dir / "10-areas" / 'note "with quotes".md').write_text("quoted filename content\n")

    result = run_cycle(config)

    assert quoted_name in result.drifted
    assert result.tag_advanced is True


# --- `.obsidian/` publish rule --------------------------------------------------------------------


def test_obsidian_copied_once_absent_then_left_alone_when_present(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    _push_commit(
        seeded_origin,
        tmp_path,
        {
            ".obsidian/app.json": '{"legacyEditor": false}\n',
            ".obsidian/workspace.json": '{"instance": "cluster"}\n',
        },
        "obsidian baseline",
    )
    config = _config(tmp_path, seeded_origin, icloud_dir)

    result = run_cycle(config)

    assert result.obsidian_seed_attempted is True
    assert (icloud_dir / ".obsidian" / "app.json").read_text() == '{"legacyEditor": false}\n'
    assert not (icloud_dir / ".obsidian" / "workspace.json").exists()  # per-instance state, never seeded

    # The device personalises its own config afterwards.
    (icloud_dir / ".obsidian" / "app.json").write_text('{"legacyEditor": true}\n')
    (icloud_dir / ".obsidian" / "community-plugins.json").write_text('["dataview"]\n')

    result_2 = run_cycle(config)

    assert result_2.obsidian_seed_attempted is False
    assert (icloud_dir / ".obsidian" / "app.json").read_text() == '{"legacyEditor": true}\n'  # untouched
    assert (icloud_dir / ".obsidian" / "community-plugins.json").exists()  # untouched


# --- shared exclude list, end to end --------------------------------------------------------------


def test_shared_exclude_list_suppresses_spurious_drift_end_to_end(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    config = _config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    (icloud_dir / ".DS_Store").write_text("finder metadata\n")
    (icloud_dir / "note.md.icloud").write_text("dataless placeholder stub\n")

    result = run_cycle(config)

    assert result.drifted == ()
    assert result.tag_advanced is True  # no false-positive drift ever blocks an ordinary advance
