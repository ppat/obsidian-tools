"""Tests for obsidian_tools/local_replicator/device_baseline.py.

The property under test that matters most: a partial first copy must never be mistaken for a
complete one. The copy itself is an ordinary per-file loop and is deliberately not made atomic
(ADR-0028) — what removes the hazard is the gate, a completion marker written only
after every file has copied, so presence is never decided by the directory merely existing.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

import pytest

from obsidian_tools.local_replicator.device_baseline import (
    BASELINE_MARKER,
    diverged_baseline_paths,
    is_baselined,
    seed_baseline,
)
from obsidian_tools.logging_config import LOG_PATH_SAMPLE_LIMIT


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_not_baselined_when_obsidian_absent(tmp_path: Path) -> None:
    icloud = tmp_path / "icloud"
    icloud.mkdir()

    assert is_baselined(icloud) is False


def test_seed_copies_files_and_writes_completion_marker(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    clone = tmp_path / "clone"
    icloud = tmp_path / "icloud"
    _write(clone / ".obsidian" / "app.json", '{"legacyEditor": false}\n')
    _write(clone / ".obsidian" / "types.json", "{}\n")
    icloud.mkdir()

    with caplog.at_level(logging.INFO):
        seed_baseline(clone, icloud)

    assert is_baselined(icloud) is True
    # The event is emitted exactly where the marker is written, and nowhere else in this module:
    # docs/local-replicator.md sends an operator to it as the one confirmation that a device now
    # holds the baseline, precisely because `obsidian_seed_attempted` cannot carry that claim.
    [record] = [r for r in caplog.records if getattr(r, "event", None) == "device_baseline_seeded"]
    assert getattr(record, "file_count", None) == 2
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
    """The committer hasn't taken its own .obsidian baseline commit yet (ADR-0028) —
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


def test_an_unreadable_subdirectory_withholds_the_marker_but_keeps_what_was_reachable(tmp_path: Path) -> None:
    """The completion marker exists so a copy that dies partway is retried wholesale rather than
    mistaken for done (module docstring) — but a swallowed `os.walk` error used to let a copy that
    *finished cleanly* still be incomplete: a transient unreadable plugin directory produced no
    candidates for anything beneath it, the rest of `.obsidian/` copied fine, and the marker got
    written anyway (independent review of this branch: reproduced `.local-replicator-baseline-complete`
    written with the plugin's files missing, permanently, since the marker is the only thing that
    ever triggers another attempt).

    Fixed by reporting every walk failure back to `seed_baseline` instead of swallowing it: what
    *is* reachable this cycle is still copied (additive, idempotent, so a partly-configured device
    now is strictly better than an unconfigured one, and nothing here is ever destroyed by a later
    cycle finishing the job) — but the marker is withheld, so `is_baselined` keeps reporting False
    and the next cycle re-walks and tops up whatever was missing once the read error clears.
    """
    clone = tmp_path / "clone"
    icloud = tmp_path / "icloud"
    _write(clone / ".obsidian" / "app.json", "{}\n")
    broken_plugin_dir = clone / ".obsidian" / "plugins" / "broken-plugin"
    _write(broken_plugin_dir / "main.js", "// plugin code, unreachable while the directory is denied\n")
    broken_plugin_dir.chmod(0)
    icloud.mkdir()

    try:
        seed_baseline(clone, icloud)

        assert is_baselined(icloud) is False
        assert (icloud / ".obsidian" / "app.json").exists()  # still copied -- reachable, additive
        assert not (icloud / ".obsidian" / "plugins" / "broken-plugin" / "main.js").exists()

        # The read error clears (an operator fixes the transient permission glitch); the next cycle
        # tops up the file the first cycle could not reach and only now completes the baseline.
        broken_plugin_dir.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        seed_baseline(clone, icloud)

        assert is_baselined(icloud) is True
        assert (icloud / ".obsidian" / "plugins" / "broken-plugin" / "main.js").exists()
    finally:
        broken_plugin_dir.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)  # tmp_path cleanup


def test_entries_that_cannot_be_stat_ed_withhold_the_marker_too(tmp_path: Path) -> None:
    """BROKEN, reproduced: the same dead `except OSError` this repository's committer-side walker
    carried (ppat/obsidian-tools#35), and here it defeats the completion marker rather than merely
    losing a log field.

    `Path.is_file()` and `Path.is_dir()` delegate to `os.path.isfile`/`isdir`, and
    `Path.is_symlink()` to `os.path.islink`; all three catch `OSError` themselves and return False.
    So the `try`/`except OSError` those calls sat inside could never fire, and an entry that cannot
    be stat'ed reached the selector as `is_file=False` — dropped as though it were a directory or a
    socket, with `unreadable` left empty. `os.walk`'s `onerror` does not cover it either: that fires
    on `scandir`, and a directory with read but no execute permission enumerates perfectly well
    (`readdir` needs `r`) while every `stat` on its entries fails with `EACCES` (which needs `x`) —
    the shape an NFS/iCloud `ESTALE` on individual entries takes.

    The consequence here is worse than on the committer side, because this module's `unreadable` list
    is *consumed*: with it empty, `seed_baseline` writes `.local-replicator-baseline-complete`,
    `is_baselined()` returns True forever after, and the walk never runs again. Measured against the
    pre-fix module with `.obsidian/plugins/dataview` at mode 0600: the device received `app.json` and
    the marker, nothing else, permanently. Silent and permanent — exactly what the marker exists to
    prevent.
    """
    clone = tmp_path / "clone"
    icloud = tmp_path / "icloud"
    _write(clone / ".obsidian" / "app.json", "{}\n")
    plugin_dir = clone / ".obsidian" / "plugins" / "dataview"
    _write(plugin_dir / "main.js", "// plugin code\n")
    _write(plugin_dir / "manifest.json", '{"id": "dataview"}\n')
    plugin_dir.chmod(stat.S_IRUSR | stat.S_IWUSR)  # listable, but its entries cannot be stat'ed
    icloud.mkdir()

    try:
        seed_baseline(clone, icloud)

        assert is_baselined(icloud) is False, (
            "the marker was written over an incomplete copy; the device keeps this gap forever"
        )
        assert (icloud / ".obsidian" / "app.json").exists()  # still copied -- reachable, additive
        assert not (icloud / ".obsidian" / "plugins" / "dataview" / "main.js").exists()

        # The read error clears; the next cycle re-walks and only now completes the baseline.
        plugin_dir.chmod(stat.S_IRWXU)
        seed_baseline(clone, icloud)

        assert is_baselined(icloud) is True
        assert (icloud / ".obsidian" / "plugins" / "dataview" / "main.js").exists()
        assert (icloud / ".obsidian" / "plugins" / "dataview" / "manifest.json").exists()
    finally:
        plugin_dir.chmod(stat.S_IRWXU)  # tmp_path cleanup


def test_a_dangling_symlink_does_not_withhold_the_marker(tmp_path: Path) -> None:
    """The risk the explicit stat introduces, pinned so a later "tighten it" change cannot reopen it.
    A symlink whose target is gone is a legible state of the parked clone, not an I/O failure, and it
    is already handled by being excluded from the copy. Had the walk stat'ed *through* each link
    (`stat()` rather than `lstat()`), a dangling one would raise `ENOENT`, land in `unreadable`, and
    withhold the completion marker forever on a condition no cycle can clear — a device stuck
    unseeded, which is strictly worse than the partial copy the marker exists to prevent."""
    clone = tmp_path / "clone"
    icloud = tmp_path / "icloud"
    _write(clone / ".obsidian" / "app.json", "{}\n")
    plugin_dir = clone / ".obsidian" / "plugins" / "dataview"
    _write(plugin_dir / "manifest.json", '{"id": "dataview"}\n')
    (plugin_dir / "main.js").symlink_to(tmp_path / "never-existed")
    icloud.mkdir()

    seed_baseline(clone, icloud)

    assert is_baselined(icloud) is True
    assert (icloud / ".obsidian" / "app.json").exists()
    assert (icloud / ".obsidian" / "plugins" / "dataview" / "manifest.json").exists()
    assert not (icloud / ".obsidian" / "plugins" / "dataview" / "main.js").exists()


# --- divergence of a seeded device from the baseline it was seeded from ---------------------------


def test_no_divergence_when_the_device_matches_the_parked_baseline(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    icloud = tmp_path / "icloud"
    _write(clone / ".obsidian" / "app.json", '{"legacyEditor": false}\n')
    _write(icloud / ".obsidian" / "app.json", '{"legacyEditor": false}\n')

    assert diverged_baseline_paths(clone, icloud) == []


def test_a_changed_device_copy_is_reported_vault_relative(tmp_path: Path) -> None:
    """Vault-relative, like every other path `CycleResult` carries: `select_baseline_paths` answers
    in paths relative to `.obsidian/` itself, and an operator reading `app.json` in a log line has
    no way to tell which of several plausible directories it names."""
    clone = tmp_path / "clone"
    icloud = tmp_path / "icloud"
    _write(clone / ".obsidian" / "app.json", '{"legacyEditor": false}\n')
    _write(icloud / ".obsidian" / "app.json", '{"legacyEditor": true}\n')

    assert diverged_baseline_paths(clone, icloud) == [".obsidian/app.json"]


def test_a_same_length_device_edit_at_a_matching_mtime_is_still_reported(tmp_path: Path) -> None:
    """Content, not the stat signature. A settings file is small JSON, so a device edit that swaps
    one value for another of the same width is the *likely* shape of one rather than an adversarial
    case, and `git checkout` stamps every file it writes with the checkout's own mtime — the same
    pair of conditions that made `--checksum` mandatory on both rsync calls (`rsync_ops.py`). A
    size-and-mtime comparison reports this file as identical."""
    clone = tmp_path / "clone"
    icloud = tmp_path / "icloud"
    _write(clone / ".obsidian" / "app.json", '{"a": "1111"}\n')
    _write(icloud / ".obsidian" / "app.json", '{"a": "2222"}\n')
    matched_mtime = (clone / ".obsidian" / "app.json").stat().st_mtime
    os.utime(icloud / ".obsidian" / "app.json", (matched_mtime, matched_mtime))

    assert diverged_baseline_paths(clone, icloud) == [".obsidian/app.json"]


def test_a_baseline_path_the_device_never_received_is_reported(tmp_path: Path) -> None:
    """Absent is diverged: a device missing a locked setting runs that plugin at Obsidian's own
    defaults, which is the state the settings lock exists to prevent -- and it is reachable without
    anyone editing anything, by a seed that was interrupted before this path or by a baseline the
    cluster gained after the device was seeded."""
    clone = tmp_path / "clone"
    icloud = tmp_path / "icloud"
    _write(clone / ".obsidian" / "app.json", "{}\n")
    _write(clone / ".obsidian" / "daily-notes.json", '{"folder": "00-daily"}\n')
    _write(icloud / ".obsidian" / "app.json", "{}\n")

    assert diverged_baseline_paths(clone, icloud) == [".obsidian/daily-notes.json"]


def test_a_path_the_allowlist_would_never_seed_is_never_reported(tmp_path: Path) -> None:
    """Keyed on the allowlist, which is the same set `seed_baseline` places -- so this can never
    report a divergence no publication path could act on. A plugin's `data.json` (the Local REST
    API plugin's bearer token, among others) and Obsidian's per-instance workspace state are both
    expected to differ on a live device, permanently and correctly."""
    clone = tmp_path / "clone"
    icloud = tmp_path / "icloud"
    _write(clone / ".obsidian" / "workspace.json", '{"instance": "cluster"}\n')
    _write(clone / ".obsidian" / "plugins" / "obsidian-local-rest-api" / "data.json", '{"apiKey": "cluster"}\n')
    _write(icloud / ".obsidian" / "workspace.json", '{"instance": "device"}\n')
    _write(icloud / ".obsidian" / "plugins" / "obsidian-local-rest-api" / "data.json", '{"apiKey": "device"}\n')

    assert diverged_baseline_paths(clone, icloud) == []


def test_nothing_is_reported_when_the_parked_clone_holds_no_baseline(tmp_path: Path) -> None:
    """The committer has not taken its `.obsidian/` baseline commit yet -- the same state
    `seed_baseline` treats as "nothing to seed from", not as an error. There is no baseline to
    diverge from, so reporting every path the device holds would be a claim about a comparison that
    was never made."""
    clone = tmp_path / "clone"
    icloud = tmp_path / "icloud"
    clone.mkdir()
    _write(icloud / ".obsidian" / "app.json", '{"legacyEditor": true}\n')

    assert diverged_baseline_paths(clone, icloud) == []


def test_a_symlinked_source_obsidian_is_never_compared_through(tmp_path: Path) -> None:
    """The same guard `seed_baseline` carries, for the same reason and against the same check
    order: `is_dir()` resolves symlinks, so it reads a symlinked `.obsidian` as present and hands
    this walk a source the clone does not control (ppat/obsidian-tools#22). The seed's copy of the
    guard stops an unknown target's contents reaching iCloud; this one stops an unknown target's
    contents becoming the baseline every path is *judged against* -- a comparison whose source is
    not the committed baseline reports divergence that is a fact about the symlink, and the runbook's
    remedy then copies that target's bytes onto the device by hand."""
    clone = tmp_path / "clone"
    icloud = tmp_path / "icloud"
    outside_the_clone = tmp_path / "outside-the-clone"
    clone.mkdir()
    _write(outside_the_clone / "app.json", '{"legacyEditor": true}\n')
    _write(icloud / ".obsidian" / "app.json", '{"legacyEditor": false}\n')
    (clone / ".obsidian").symlink_to(outside_the_clone)

    assert diverged_baseline_paths(clone, icloud) == []


def test_a_device_copy_that_cannot_be_read_is_reported_as_diverged(tmp_path: Path) -> None:
    """Identical bytes on both sides, and the device's copy unreadable: what it holds is exactly
    what could not be established, so the only safe answer is "diverged". Counting an unreadable
    copy as *identical* is this module's own named worst case -- a silent, permanently
    correct-looking wrong answer, which is the failure `_iter_obsidian_candidates` refuses on the
    source side and the reason `seed_baseline` withholds its marker. Identical content is what makes
    the assertion discriminating: any comparison that reached the bytes would answer "same"."""
    clone = tmp_path / "clone"
    icloud = tmp_path / "icloud"
    _write(clone / ".obsidian" / "app.json", '{"legacyEditor": false}\n')
    device_copy = icloud / ".obsidian" / "app.json"
    _write(device_copy, '{"legacyEditor": false}\n')
    device_copy.chmod(0)

    try:
        assert diverged_baseline_paths(clone, icloud) == [".obsidian/app.json"]
    finally:
        device_copy.chmod(stat.S_IRUSR | stat.S_IWUSR)  # tmp_path cleanup


def test_a_clone_subtree_that_cannot_be_enumerated_is_reported_rather_than_read_as_no_divergence(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A directory the walk cannot enumerate produces no candidates, so every path beneath it is
    silently answered "not diverged" -- a zero that is a fact about the read, not about the device.
    `_iter_obsidian_candidates` collects those failures precisely so a caller cannot drop them
    (`seed_baseline` withholds its marker on them); this caller has no marker to withhold and must
    not refuse either, because a refusal would suppress the divergence it *did* establish. Saying so
    is what is left, and it has to be said even when the returned list is empty -- that is the case
    where an unreadable subtree and a healthy device are otherwise indistinguishable."""
    clone = tmp_path / "clone"
    icloud = tmp_path / "icloud"
    _write(clone / ".obsidian" / "app.json", "{}\n")
    _write(icloud / ".obsidian" / "app.json", "{}\n")
    denied = clone / ".obsidian" / "plugins" / "dataview"
    _write(denied / "main.js", "plugin code\n")
    denied.chmod(0)

    try:
        with caplog.at_level(logging.INFO):
            assert diverged_baseline_paths(clone, icloud) == []
    finally:
        denied.chmod(stat.S_IRWXU)  # tmp_path cleanup

    [record] = [r for r in caplog.records if getattr(r, "event", None) == "obsidian_baseline_comparison_incomplete"]
    assert record.levelno == logging.WARNING
    assert str(denied) in getattr(record, "unreadable_paths", [])
    assert getattr(record, "unreadable_count", None) == 1


def test_a_symlinked_device_obsidian_is_never_compared_through(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The read-side twin of `seed_baseline`'s source guard, on the destination this function adds.
    `is_baselined()` follows a symlink, so a device whose `.obsidian` points elsewhere answers
    "seeded" and reaches this comparison; `Path.is_file` and `filecmp.cmp` both follow it too, so
    every allowlisted path would be read from outside the vault and reported under an
    `.obsidian/`-relative name. The answer is not "no divergence" either -- that is the silent,
    permanently correct-looking wrong answer this module refuses everywhere else -- so the condition
    is reported instead."""
    clone = tmp_path / "clone"
    icloud = tmp_path / "icloud"
    outside_the_vault = tmp_path / "outside-the-vault"
    _write(clone / ".obsidian" / "app.json", '{"legacyEditor": false}\n')
    _write(outside_the_vault / "app.json", '{"legacyEditor": true}\n')
    _write(outside_the_vault / BASELINE_MARKER, "seeded by something that is not this component\n")
    icloud.mkdir()
    (icloud / ".obsidian").symlink_to(outside_the_vault)

    with caplog.at_level(logging.INFO):
        assert is_baselined(icloud) is True  # the symlink is what makes this comparison reachable
        assert diverged_baseline_paths(clone, icloud) == []

    [record] = [r for r in caplog.records if getattr(r, "event", None) == "obsidian_baseline_skip_symlinked_device"]
    assert record.levelno == logging.WARNING


def test_a_device_copy_evicted_to_a_dataless_placeholder_is_not_reported_as_a_changed_setting(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """iCloud evicts a file's content by replacing it with a `.<name>.icloud` placeholder, and the
    largest thing under `.obsidian/` by a wide margin -- a minified plugin `main.js` -- is the first
    candidate for eviction. The real path is then simply absent, which is indistinguishable from a
    setting the device dropped, and the diagnosis this event hands an operator ("a setting was
    changed on the device or the baseline changed at the cluster") names neither cause. It is not
    divergence: nothing was established either way, which is what the comparison-incomplete event
    already means."""
    clone = tmp_path / "clone"
    icloud = tmp_path / "icloud"
    _write(clone / ".obsidian" / "app.json", "{}\n")
    _write(icloud / ".obsidian" / "app.json", "{}\n")
    _write(clone / ".obsidian" / "plugins" / "dataview" / "main.js", "a megabyte of minified plugin\n")
    _write(icloud / ".obsidian" / "plugins" / "dataview" / ".main.js.icloud", "")

    with caplog.at_level(logging.INFO):
        assert diverged_baseline_paths(clone, icloud) == []

    [record] = [r for r in caplog.records if getattr(r, "event", None) == "obsidian_baseline_comparison_incomplete"]
    assert record.levelno == logging.WARNING
    assert getattr(record, "dataless_paths", None) == [".obsidian/plugins/dataview/main.js"]
    assert getattr(record, "dataless_count", None) == 1


def test_an_oversized_unreadable_set_is_sampled_with_its_full_count_beside_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The seed's own list is bounded by nothing: it names one entry per directory the walk could not
    enumerate, and a permission or iCloud fault applies to a whole subtree at once rather than to one
    directory. An oversized line is dropped whole by the pipeline, taking the count with it, so the
    operator loses even the fact that a seed was incomplete."""
    clone = tmp_path / "clone"
    icloud = tmp_path / "icloud"
    _write(clone / ".obsidian" / "app.json", "{}\n")
    plugins = clone / ".obsidian" / "plugins"
    denied = [plugins / f"plugin-{index:03d}" for index in range(101)]
    for directory in denied:
        directory.mkdir(parents=True)
        directory.chmod(0)

    try:
        with caplog.at_level(logging.INFO):
            seed_baseline(clone, icloud)
    finally:
        for directory in denied:
            directory.chmod(stat.S_IRWXU)  # tmp_path cleanup

    assert not is_baselined(icloud)  # the marker is still withheld, whatever the line carries
    [record] = [r for r in caplog.records if getattr(r, "event", None) == "device_baseline_seed_incomplete"]
    assert getattr(record, "unreadable_count", None) == 101
    assert len(getattr(record, "unreadable_paths", [])) == LOG_PATH_SAMPLE_LIMIT


def test_both_comparison_incomplete_lists_are_sampled_with_their_counts_beside_them(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """This event carries two independently unbounded lists, from opposite sides of the comparison,
    and either can oversize the line on its own: a permission or iCloud fault applies to a whole
    subtree at once, and a storage-optimization pass evicts many files in one go."""
    clone = tmp_path / "clone"
    icloud = tmp_path / "icloud"
    _write(clone / ".obsidian" / "app.json", "{}\n")
    _write(icloud / ".obsidian" / "app.json", "{}\n")
    denied = [clone / ".obsidian" / "themes" / f"theme-{index:03d}" for index in range(101)]
    for directory in denied:
        directory.mkdir(parents=True)
        directory.chmod(0)
    for index in range(101):
        _write(clone / ".obsidian" / "plugins" / f"plugin-{index:03d}" / "main.js", f"plugin {index}\n")
        _write(icloud / ".obsidian" / "plugins" / f"plugin-{index:03d}" / ".main.js.icloud", "")

    try:
        with caplog.at_level(logging.INFO):
            assert diverged_baseline_paths(clone, icloud) == []
    finally:
        for directory in denied:
            directory.chmod(stat.S_IRWXU)  # tmp_path cleanup

    [record] = [r for r in caplog.records if getattr(r, "event", None) == "obsidian_baseline_comparison_incomplete"]
    assert getattr(record, "unreadable_count", None) == 101
    assert len(getattr(record, "unreadable_paths", [])) == LOG_PATH_SAMPLE_LIMIT
    assert getattr(record, "dataless_count", None) == 101
    assert len(getattr(record, "dataless_paths", [])) == LOG_PATH_SAMPLE_LIMIT
