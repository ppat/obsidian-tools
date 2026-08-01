"""Tests for the `replicate` subcommand's orchestration (obsidian_tools/commands/replicate.py):
`run()` itself, not `run_cycle` -- `test_local_replicator_cycle.py` already covers the cycle's own
git/rsync semantics in depth. What's untested before this file is the contract `run()` adds on top:
a cycle that *completed but was gated* (a spool write failure, or a path whose patch carried no
content) exits 0 -- the design working, not a run failure -- while a cycle that *could not complete
at all* (`GitCommandError`, `RsyncError`) exits 1. Getting the first case wrong would make launchd
report a failing job on every cycle a human happens to paste an image, teaching the operator to
ignore the one signal that means "your edit is being held" (docs/DESIGN.md §2 item 10).

The `extra={...}` log payload is checked by field name against `CycleResult`, not spot-checked --
docs/DESIGN.md §2 item 10 states local-replicator is "watched by nothing" that reaches a Mac-side
process, so this log line is the entire operational surface, not decoration.

Real local git repos and real rsync throughout, same as `test_local_replicator_cycle.py` -- the
`GitCommandError` and `RsyncError` cases are caused by real, uncontrived failures (a corrupted
object store; a source file this process's own uid cannot open) rather than raised by the test,
because the risk actually worth proving is that `run()`'s exception handling and logging fire on a
genuine failure, not merely that it fires when told to.
"""

from __future__ import annotations

import logging
import shutil
import stat
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import push_commit, replicate_config

from obsidian_tools.commands import replicate as replicate_command
from obsidian_tools.config import ReplicateConfig
from obsidian_tools.local_replicator.cycle import CycleResult, run_cycle
from obsidian_tools.local_replicator.drift import SpoolEntry
from obsidian_tools.local_replicator.spool import SpoolWriteError, write_spool_entry


def _failing_spool_writer(fail_on: str) -> Callable[[Path, SpoolEntry], Path]:
    """Same shape as `test_local_replicator_cycle.py`'s own helper -- kept local rather than moved
    to `conftest.py` since, unlike `replicate_config`/`push_commit`, only one test in this file
    needs it, and the seam it exercises (`run_cycle`'s `spool_writer` parameter) is `run_cycle`'s
    own, not something this file and that one both call into independently."""

    def writer(spool_dir: Path, entry: SpoolEntry) -> Path:
        if entry.path == fail_on:
            raise SpoolWriteError(f"simulated local disk write failure for {fail_on!r} -- injected, not real I/O")
        return write_spool_entry(spool_dir, entry)

    return writer


def _corrupt_git_objects(cache_clone_dir: Path) -> None:
    """Deletes every loose object from the parked clone's object store, leaving `objects/pack/` and
    `objects/info/` alone. `LAST_CHECKOUT` (a lightweight tag) still resolves to a SHA afterward --
    `git rev-parse --verify` only dereferences the ref, it never checks the object store -- so the
    next cycle's step 1 `checkout -f <that SHA>` genuinely fails: `fatal: reference is not a tree`,
    a real `GitCommandError` raised by git itself, not one this test raises by hand. Modest stand-in
    for the git-dir cache volume corruption `vault_git/git_errors.py` already treats as a real,
    distinct failure domain for the committer side of this codebase."""
    objects_dir = cache_clone_dir / ".git" / "objects"
    for entry in objects_dir.iterdir():
        if entry.name in {"info", "pack"}:
            continue
        shutil.rmtree(entry)


# --- ordinary success -----------------------------------------------------------------------------


def test_successful_cycle_exits_zero(tmp_path: Path, seeded_origin: Path, icloud_dir: Path) -> None:
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)

    exit_code = replicate_command.run(config)

    assert exit_code == 0
    assert (icloud_dir / "00-index.md").read_text() == "# Home\n"


def test_log_payload_carries_every_cycle_result_field(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Checked by attribute name against every `CycleResult` field, not by re-typing the dict, so a
    field `run()` forgets to carry into `extra={...}` fails this test with an `AttributeError`
    rather than the assertion silently passing an incomplete record."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)

    with caplog.at_level(logging.INFO):
        assert replicate_command.run(config) == 0

    [record] = [r for r in caplog.records if getattr(r, "event", None) == "cycle_complete"]
    assert record.drifted == 0  # type: ignore[attr-defined]
    assert record.spooled == 0  # type: ignore[attr-defined]
    assert record.spool_write_failed is False  # type: ignore[attr-defined]
    assert record.uncaptured == 0  # type: ignore[attr-defined]
    # True, not False: `seeded_origin` never commits a `.obsidian/` baseline, so
    # `device_baseline.is_baselined` never becomes True and every cycle attempts (a no-op) seed --
    # see device_baseline.py's own module docstring, "Presence is decided by a completion marker".
    assert record.obsidian_seed_attempted is True  # type: ignore[attr-defined]
    assert record.tag_advanced is True  # type: ignore[attr-defined]
    assert record.checkout is not None  # type: ignore[attr-defined]


# --- gated cycles: completed, but publish/tag-advance withheld -- not a run failure ----------------


def test_spool_write_failure_gates_the_cycle_but_the_run_still_exits_zero(
    tmp_path: Path,
    seeded_origin: Path,
    icloud_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`run()` has no parameter of its own for reaching `run_cycle`'s injectable `spool_writer`
    seam -- that seam exists on `run_cycle` (cycle.py) for `test_local_replicator_cycle.py`'s own
    acceptance test, and `run()` never threads it through. Reached here by monkeypatching the
    module-level `run_cycle` name `commands/replicate.py` calls, to a wrapper that still runs the
    real `run_cycle` (real git, real rsync) with that same real spool-writer failure bound in --
    nothing about the cycle's own behaviour is faked, only the one parameter `run()` itself has no
    way to pass through. If `run()` grows a `spool_writer` parameter of its own later, this
    monkeypatch becomes unnecessary and should be replaced with passing it directly."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    assert replicate_command.run(config) == 0  # bootstrap
    push_commit(seeded_origin, tmp_path, {"00-index.md": "# Home (agent update)\n"}, "agent update")
    (icloud_dir / "00-index.md").write_text("uncaptured human edit\n")

    failing_writer = _failing_spool_writer("00-index.md")

    def _run_cycle_with_failing_writer(cfg: ReplicateConfig) -> CycleResult:
        return run_cycle(cfg, spool_writer=failing_writer)

    monkeypatch.setattr(replicate_command, "run_cycle", _run_cycle_with_failing_writer)
    caplog.clear()  # drop the bootstrap cycle's own "cycle_complete" record above

    with caplog.at_level(logging.INFO):
        exit_code = replicate_command.run(config)

    assert exit_code == 0  # a gated cycle is not a run failure -- the design working, not broken
    events = [getattr(record, "event", None) for record in caplog.records]
    assert "spool_write_failed" in events  # cycle.py's own record of *why* this cycle gated

    [record] = [r for r in caplog.records if getattr(r, "event", None) == "cycle_complete"]
    assert record.spool_write_failed is True  # type: ignore[attr-defined]
    assert record.tag_advanced is False  # type: ignore[attr-defined]

    # Publish never ran this cycle: the human's uncaptured edit survives untouched.
    assert (icloud_dir / "00-index.md").read_text() == "uncaptured human edit\n"


def test_uncaptured_binary_gates_the_cycle_but_the_run_still_exits_zero(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No seam needed here, unlike the spool-write gate above -- an uncaptured path
    (`select_spool_entries` over the empty patch a binary's `git diff` produces) is a property of
    real drift, plantable directly, exactly like `test_local_replicator_cycle.py`'s
    `test_a_binary_created_on_the_device_is_never_published_over`. Doubles as proof that
    `uncaptured` in the log payload reflects a real count, not just presence -- the sibling test
    above only proves it exists and reads 0."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    assert replicate_command.run(config) == 0
    (icloud_dir / "_attachments").mkdir(parents=True, exist_ok=True)
    (icloud_dir / "_attachments" / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00")
    caplog.clear()  # drop the bootstrap cycle's own "cycle_complete" record above

    with caplog.at_level(logging.INFO):
        exit_code = replicate_command.run(config)

    assert exit_code == 0
    [record] = [r for r in caplog.records if getattr(r, "event", None) == "cycle_complete"]
    assert record.uncaptured == 1  # type: ignore[attr-defined]
    assert record.tag_advanced is False  # type: ignore[attr-defined]
    assert (icloud_dir / "_attachments" / "screenshot.png").exists()  # never destroyed by --delete


# --- cycles that could not complete at all -- these are run failures -------------------------------


def test_git_command_error_fails_the_run(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    assert replicate_command.run(config) == 0  # bootstrap, establishes LAST_CHECKOUT
    _corrupt_git_objects(tmp_path / "cache-clone")

    with caplog.at_level(logging.ERROR):
        exit_code = replicate_command.run(config)

    assert exit_code == 1
    events = [getattr(record, "event", None) for record in caplog.records]
    assert "cycle_failed" in events


def test_rsync_error_fails_the_run(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`overlay()` (step 2) reads the device tree as its rsync *source* -- a file this process's own
    uid cannot open makes rsync's sender fail for real (`Permission denied`, exit 23), unlike the
    destination-write direction: rsync silently regains write access to a directory it already owns
    before publishing into it, so a chmod on the iCloud side alone does not reproduce a failure --
    measured directly against a real rsync invocation while writing this test, not assumed."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    assert replicate_command.run(config) == 0  # bootstrap
    unreadable = icloud_dir / "unreadable-note.md"
    unreadable.write_text("a file rsync's sender genuinely cannot open this cycle\n")
    unreadable.chmod(0)
    try:
        with caplog.at_level(logging.ERROR):
            exit_code = replicate_command.run(config)
    finally:
        unreadable.chmod(stat.S_IRUSR | stat.S_IWUSR)  # restore so tmp_path cleanup can remove it

    assert exit_code == 1
    events = [getattr(record, "event", None) for record in caplog.records]
    assert "cycle_failed" in events
