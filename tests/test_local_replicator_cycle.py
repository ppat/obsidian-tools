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

import json
import logging
import shutil
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from conftest import push_commit, replicate_config, run_git, seed_obsidian_baseline_in_history
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import obsidian_tools.local_replicator.cycle as cycle_module
from obsidian_tools.local_replicator.cycle import run_cycle
from obsidian_tools.local_replicator.device_baseline import is_baselined
from obsidian_tools.local_replicator.drift import SpoolEntry
from obsidian_tools.local_replicator.spool import SpoolWriteError, list_spool_files, read_spool_entry, write_spool_entry
from obsidian_tools.local_replicator.tag import read_last_checkout
from obsidian_tools.logging_config import LOG_PATH_SAMPLE_LIMIT, JsonFormatter
from obsidian_tools.vault_git.runner import GitRunner


def _tag_sha(tmp_path: Path) -> str | None:
    runner = GitRunner(tmp_path / "cache-clone" / ".git", tmp_path / "cache-clone")
    return read_last_checkout(runner)


def _spooled_by_path(tmp_path: Path) -> dict[str, SpoolEntry]:
    spool_dir = tmp_path / "spool"
    return {entry.path: entry for f in list_spool_files(spool_dir) for entry in [read_spool_entry(f)]}


def _hostile_global_git_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """Point git's *global* configuration at a file this test writes, via `GIT_CONFIG_GLOBAL`.

    This is not a weaker stand-in for `~/.gitconfig`: `GIT_CONFIG_GLOBAL` *replaces* the global
    configuration file git would otherwise read, by git's own documented mechanism, so a setting
    placed here reaches every git invocation by exactly the path the operator's own `~/.gitconfig`
    reaches it. That is what makes the tests below evidence about what the environment can still
    do to us, rather than evidence that we wrote the argv we meant to write -- and it keeps them
    hermetic (nothing outside `tmp_path` is written, no real user configuration is read), which
    setting a genuine `~/.gitconfig` would not be.
    """
    config_path = tmp_path / "hostile-gitconfig"
    config_path.write_text(body)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config_path))


def _hostile_local_git_config(cache_clone_dir: Path, *pairs: tuple[str, str]) -> None:
    """The repository-local analogue of `_hostile_global_git_config`: `git config --local key
    value` against the cache clone's own `.git/config`, standing in for what an operator debugging
    a diff in that clone -- or a `.gitattributes` shipped inside the vault itself -- leaves behind.

    This is not the same route as the global helper above, and the distinction matters: repo-local
    configuration is deliberately left readable by `vault_git/runner.py`'s scrub (it is where this
    codebase's own pins live -- identity, `core.quotePath`, `core.fileMode`), so nothing about the
    scrub or the `-c` config pins helps here. Only the explicit flags on the decision diff
    invocations (`_DECISION_DIFF_FLAGS`) close this route, which is exactly why those flags are not
    redundant with the environment scrub.
    """
    for key, value in pairs:
        run_git("config", "--local", key, value, cwd=cache_clone_dir)


def _executable_script(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


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
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)

    result = run_cycle(config)

    assert result.tag_advanced is True
    assert result.drifted == ()  # nothing to compare against on a first run
    assert (icloud_dir / "00-index.md").read_text() == "# Home\n"


def test_order_attributes_upstream_change_and_device_drift_correctly(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """The whole reason for the step ordering: an upstream change and a device edit that land in
    the same cycle must not be conflated. Comparing before the pull is what keeps them apart."""
    push_commit(seeded_origin, tmp_path, {"10-areas/other.md": "original other\n"}, "add other")
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)  # cycle 1: bootstrap both files onto the device

    # Between cycles: an agent updates one file upstream; independently, a human edits a
    # *different* file directly in the device's iCloud copy.
    push_commit(seeded_origin, tmp_path, {"00-index.md": "# Home (agent update)\n"}, "agent update")
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
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    (icloud_dir / "typed-on-phone.md").write_text("brand new note from the device\n")

    result = run_cycle(config)

    assert "typed-on-phone.md" in result.drifted
    entry = _spooled_by_path(tmp_path)["typed-on-phone.md"]
    assert entry.kind == "create"
    assert "brand new note from the device" in entry.patch


def test_device_deletion_is_spooled_as_a_delete(tmp_path: Path, seeded_origin: Path, icloud_dir: Path) -> None:
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
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
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    checkout_after_bootstrap = _tag_sha(tmp_path)
    push_commit(seeded_origin, tmp_path, {"00-index.md": "# Home (agent update)\n"}, "agent update")
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
    push_commit(seeded_origin, tmp_path, {"10-areas/other.md": "original other\n"}, "add other")
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    push_commit(seeded_origin, tmp_path, {"00-index.md": "# Home (agent update)\n"}, "agent update")
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
    push_commit(seeded_origin, tmp_path, {"aaa-first.md": "first\n"}, "add first")
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    (icloud_dir / "aaa-first.md").write_text("edited first (sorts before the failing path)\n")

    run_cycle(config, spool_writer=_failing_spool_writer("zzz-does-not-exist.md"))

    # "aaa-first.md" sorts before the (nonexistent) failing path alphabetically, so
    # select_spool_entries's deterministic ordering means it was attempted, and written, first.
    spooled = _spooled_by_path(tmp_path)
    assert "aaa-first.md" in spooled
    assert "edited first" in spooled["aaa-first.md"].patch


# --- the tag-not-advanced log line states the actual cause, not just "spool write failed" --------


def _tag_not_advanced_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if getattr(r, "event", None) == "cycle_tag_not_advanced"]


def test_uncaptured_binary_logs_the_actual_cause_not_a_spool_write_failure(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """F3: this line used to hardcode "spool write failed for at least one drifted path" regardless
    of *why* `should_advance_tag` came back False -- reachable, and reached, by an uncaptured binary
    with no spool write failure anywhere in the cycle. An operator reading it would go looking at a
    failing disk while the actual cause was a pasted image. Asserted on the literal message text,
    not just the event name or a field, so a reversion to the old hardcoded string fails this test
    rather than passing it by satisfying a looser check."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    (icloud_dir / "_attachments").mkdir(parents=True, exist_ok=True)
    (icloud_dir / "_attachments" / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00")

    with caplog.at_level(logging.INFO):
        result = run_cycle(config)

    assert result.tag_advanced is False
    assert result.spool_write_failed is False
    messages = _tag_not_advanced_messages(caplog)
    assert len(messages) == 1
    assert "no content" in messages[0]
    assert "spool write failed" not in messages[0]


def test_spool_write_failure_log_still_names_a_real_spool_write_failure(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The sibling case: a genuine spool write failure must still be named as such -- this is not a
    swap of one hardcoded message for another that's now wrong the other way around."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    push_commit(seeded_origin, tmp_path, {"00-index.md": "# Home (agent update)\n"}, "agent update")
    (icloud_dir / "00-index.md").write_text("uncaptured human edit\n")

    with caplog.at_level(logging.INFO):
        result = run_cycle(config, spool_writer=_failing_spool_writer("00-index.md"))

    assert result.tag_advanced is False
    assert result.uncaptured == ()
    messages = _tag_not_advanced_messages(caplog)
    assert len(messages) == 1
    assert "spool write failed" in messages[0]
    assert "no content" not in messages[0]


def test_tag_not_advanced_log_names_both_causes_when_both_occur_in_one_cycle(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Both conditions can fail in the same cycle -- an unrelated spool write failure alongside an
    uncaptured binary -- and an operator needs both named, not just whichever the implementation
    happens to check first."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    push_commit(seeded_origin, tmp_path, {"00-index.md": "# Home (agent update)\n"}, "agent update")
    (icloud_dir / "00-index.md").write_text("uncaptured human edit\n")
    (icloud_dir / "_attachments").mkdir(parents=True, exist_ok=True)
    (icloud_dir / "_attachments" / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00")

    with caplog.at_level(logging.INFO):
        result = run_cycle(config, spool_writer=_failing_spool_writer("00-index.md"))

    assert result.tag_advanced is False
    assert result.spool_write_failed is True
    assert result.uncaptured == ("_attachments/screenshot.png",)
    messages = _tag_not_advanced_messages(caplog)
    assert len(messages) == 2
    assert any("spool write failed" in m for m in messages)
    assert any("no content" in m for m in messages)


def test_a_binary_created_on_the_device_is_never_published_over(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """A pasted image is the most ordinary non-text thing a human does in Obsidian, and it used to
    be destroyed silently: `git diff --cached` emits `Binary files ... differ` -- a patch asserting
    that something changed while carrying none of it -- the spool write for it then *succeeds*, so
    the gate read the cycle as safe and the publish rsync's `--delete` removed the file from iCloud.
    The bytes existed nowhere else: not in git, not in the spool, not on the device."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    checkout_before = _tag_sha(tmp_path)

    (icloud_dir / "_attachments").mkdir(parents=True, exist_ok=True)
    (icloud_dir / "_attachments" / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00")

    result = run_cycle(config)

    assert result.uncaptured == ("_attachments/screenshot.png",)
    assert result.tag_advanced is False
    assert _tag_sha(tmp_path) == checkout_before
    assert (icloud_dir / "_attachments" / "screenshot.png").exists()
    assert (icloud_dir / "_attachments" / "screenshot.png").read_bytes().startswith(b"\x89PNG")
    assert "_attachments/screenshot.png" not in _spooled_by_path(tmp_path)


_PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00original"


def test_deleting_a_tracked_binary_on_the_device_does_not_wedge_the_cycle(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """The gate pauses the cycle; it must not wedge it -- proved on the instance where it can fail.

    This test replaces one that planted an *untracked* binary and then deleted it. That case cannot
    wedge by construction: deleting an untracked file erases the drift itself, so the remedy works
    whatever the gate does, and the test was green through a defect that made the same claim false
    for every tracked file. A deletion is the case that matters, because `LAST_CHECKOUT` still names
    a commit containing the file and step 1 re-parks there every cycle -- so a withheld deletion is
    re-detected from scratch forever, and the only escape found was restoring the exact bytes the
    human had deliberately deleted. The trap closes behind the documented remedy too: for a tracked
    binary, "remove the file from the vault" is precisely what produces this state.

    Nothing is lost by capturing it: the pre-deletion bytes are in git at the baseline."""
    push_commit(seeded_origin, tmp_path, {}, "add diagram", binary_files={"_attachments/diagram.png": _PNG_BYTES})
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    assert (icloud_dir / "_attachments" / "diagram.png").read_bytes() == _PNG_BYTES

    (icloud_dir / "_attachments" / "diagram.png").unlink()  # deleted on the phone
    push_commit(seeded_origin, tmp_path, {"00-index.md": "# Home (agent update)\n"}, "agent update")

    result = run_cycle(config)

    assert result.uncaptured == ()
    assert result.tag_advanced is True
    entry = _spooled_by_path(tmp_path)["_attachments/diagram.png"]
    assert entry.kind == "delete"
    # The upstream edit reaches the device again -- what a wedge here holds back is not just this
    # path but every agent edit, cycle after cycle.
    assert (icloud_dir / "00-index.md").read_text() == "# Home (agent update)\n"

    # And it settles rather than repeating: the publish restored the file from git (it is still in
    # `main` until the server drains the spool), so the next cycle sees no drift at all.
    assert run_cycle(config).drifted == ()


def test_a_modified_tracked_binary_pauses_the_cycle_and_deleting_it_is_a_real_escape(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """A modified binary is withheld for a reason a deletion is not: its new bytes exist only on
    the device, and publishing would overwrite them with git's copy. So the pause is correct -- but
    it is only a pause if the operator's remedy actually resolves. It used to convert an `M` that
    regenerates every cycle into a `D` that regenerates every cycle."""
    push_commit(seeded_origin, tmp_path, {}, "add diagram", binary_files={"_attachments/diagram.png": _PNG_BYTES})
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    edited_on_the_phone = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00annotated-on-the-phone"
    (icloud_dir / "_attachments" / "diagram.png").write_bytes(edited_on_the_phone)

    paused = run_cycle(config)

    assert paused.uncaptured == ("_attachments/diagram.png",)
    assert paused.tag_advanced is False
    assert (icloud_dir / "_attachments" / "diagram.png").read_bytes() == edited_on_the_phone  # not published over

    (icloud_dir / "_attachments" / "diagram.png").unlink()  # the documented remedy

    resolved = run_cycle(config)

    assert resolved.uncaptured == ()
    assert resolved.tag_advanced is True
    assert _spooled_by_path(tmp_path)["_attachments/diagram.png"].kind == "delete"


# --- the operator's own git configuration must not reach any decision this cycle makes -----------
#
# local-replicator is the one component of this system that runs on a real machine with a real
# `~/.gitconfig` (docs/DESIGN.md §4 Plane B). Every test below sets a *real* hostile global
# configuration and drives the *real* cycle through it. None of them assert on argv: an assertion
# that a flag is present proves only that we wrote the argv we intended, not that the environment
# can no longer reach the output that argv produces.


def test_a_global_diff_external_cannot_replace_the_patch_a_markdown_edit_spools(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The severe half of the defect, and the silent one: `[diff] external = ...` -- a setting
    difftastic's own README instructs users to add -- replaces `git diff --cached`'s output
    wholesale with a summary line. The gate reads that summary, finds no binary marker, and calls
    the cycle captured; the spool entry carries a summary instead of the thought typed on the
    phone, and the publish rsync's `--delete` then overwrites the device's copy with git's. The
    note exists nowhere afterwards -- not on the device, not in git, not in the spool -- while
    `drifted`, `spooled` and `tag_advanced` all read healthy."""
    external = _executable_script(
        tmp_path / "hostile-external-diff.sh",
        "#!/bin/sh\necho '1 file changed (difftastic-style summary)'\n",
    )
    _hostile_global_git_config(tmp_path, monkeypatch, f"[diff]\n\texternal = {external}\n")
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    push_commit(seeded_origin, tmp_path, {"00-index.md": "# Home (agent update)\n"}, "agent update")
    (icloud_dir / "00-index.md").write_text("a thought typed on the phone\n")

    result = run_cycle(config)

    assert result.drifted == ("00-index.md",)
    spooled = _spooled_by_path(tmp_path)
    assert "a thought typed on the phone" in spooled["00-index.md"].patch
    assert "difftastic-style summary" not in spooled["00-index.md"].patch


def test_a_global_diff_external_cannot_smuggle_a_binary_past_the_gate(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same setting, against the case the gate exists for: git's binary marker never appears in
    the replaced output, so the pasted image is spooled as "captured" and the publish deletes the
    only copy of its bytes."""
    external = _executable_script(
        tmp_path / "hostile-external-diff.sh",
        "#!/bin/sh\necho '1 file changed (difftastic-style summary)'\n",
    )
    _hostile_global_git_config(tmp_path, monkeypatch, f"[diff]\n\texternal = {external}\n")
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    (icloud_dir / "_attachments").mkdir(parents=True, exist_ok=True)
    (icloud_dir / "_attachments" / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00")

    result = run_cycle(config)

    assert result.uncaptured == ("_attachments/screenshot.png",)
    assert result.tag_advanced is False
    assert (icloud_dir / "_attachments" / "screenshot.png").read_bytes().startswith(b"\x89PNG")


def test_a_global_textconv_driver_cannot_smuggle_a_binary_past_the_gate(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The quieter reach into the same output: a `diff.<driver>.textconv` found through a global
    `core.attributesFile`. Nothing is replaced wholesale -- git produces an ordinary-looking patch
    with an ordinary-looking hunk, and no binary marker anywhere, for a file whose actual bytes it
    never carried."""
    textconv = _executable_script(
        tmp_path / "hostile-textconv.sh",
        "#!/bin/sh\necho 'PNG image data, 800 x 600, 8-bit/color RGBA'\n",
    )
    attributes = tmp_path / "hostile-gitattributes"
    attributes.write_text("*.png diff=img\n")
    _hostile_global_git_config(
        tmp_path,
        monkeypatch,
        f'[core]\n\tattributesFile = {attributes}\n[diff "img"]\n\ttextconv = {textconv}\n',
    )
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    (icloud_dir / "_attachments").mkdir(parents=True, exist_ok=True)
    (icloud_dir / "_attachments" / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00")

    result = run_cycle(config)

    assert result.uncaptured == ("_attachments/screenshot.png",)
    assert result.tag_advanced is False
    assert (icloud_dir / "_attachments" / "screenshot.png").read_bytes().startswith(b"\x89PNG")


def test_a_global_ignore_file_cannot_hide_a_device_creation_from_the_drift_diff(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same class reached one step earlier than the patch, and the most silent member of it:
    `core.excludesFile`. Step 3 stages the overlay with `git add -A`, which consults ignore rules
    for untracked paths -- so a pattern in the operator's global ignore file makes a note created
    on the phone never appear in `git diff --cached` at all. There is no patch to judge, nothing
    lands in `uncaptured`, the gate sees a clean cycle, and the publish's `--delete` removes the
    note from iCloud.

    The vault's own tracked `.gitignore` must keep working, so what closes this has to be scoped to
    the *user-global* file specifically, not to ignore rules in general."""
    global_ignore = tmp_path / "hostile-global-gitignore"
    global_ignore.write_text("phone-*.md\n")
    _hostile_global_git_config(tmp_path, monkeypatch, f"[core]\n\texcludesFile = {global_ignore}\n")
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    (icloud_dir / "phone-draft.md").write_text("typed on the phone, never seen by git add -A\n")

    result = run_cycle(config)

    assert result.drifted == ("phone-draft.md",)
    entry = _spooled_by_path(tmp_path)["phone-draft.md"]
    assert entry.kind == "create"
    # The publish's `--delete` does remove it from the device this cycle, and that is correct
    # *because* it was captured first: the spool holds the whole note, and it returns to the device
    # once the server commits it. That ordering -- captured, then published over -- is the entire
    # property, and the ignore file removed the "captured" half of it while leaving the other.
    assert "typed on the phone, never seen by git add -A" in entry.patch


def test_a_repository_local_diff_external_cannot_replace_the_patch_either(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """The route neutralising the *environment* deliberately cannot close, and the one the flags on
    the diff invocation exist for: the same setting in the cache clone's own `.git/config`. This is
    not exotic -- `git config --local diff.external ...` is what an operator debugging a diff in
    that clone leaves behind, and the clone is a directory on their laptop like any other.

    Repository-local configuration is left readable on purpose (it is where this codebase's own
    pins live), so nothing about the scrub helps here. An explicit flag overrides configuration
    from every source at once, which is why the two mechanisms are not redundant."""
    external = _executable_script(
        tmp_path / "hostile-external-diff.sh",
        "#!/bin/sh\necho '1 file changed (difftastic-style summary)'\n",
    )
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    run_git("config", "--local", "diff.external", str(external), cwd=tmp_path / "cache-clone")

    (icloud_dir / "00-index.md").write_text("a thought typed on the phone\n")

    result = run_cycle(config)

    assert result.drifted == ("00-index.md",)
    assert "a thought typed on the phone" in _spooled_by_path(tmp_path)["00-index.md"].patch


def test_a_global_hooks_path_cannot_reach_the_tree_this_cycle_publishes(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The knob nobody would have enumerated, which is the point of closing the class rather than
    the findings: `core.hooksPath`. It names a directory of scripts git *executes*, and the cycle
    checks out twice per pass -- so a `post-checkout` hook rewrites the parked tree between the
    checkout and the publish, and the publish rsync then writes that rewrite onto the device over
    a note the human never touched. No diff flag and no capture predicate is in this path at all;
    only refusing to read the operator's configuration in the first place stops it.

    Kept as a test rather than as a comment because it is the evidence that the scrub is load-
    bearing on its own: every other hostile setting here is closed twice over."""
    hooks_dir = tmp_path / "hostile-hooks"
    hooks_dir.mkdir()
    # Hooks run with the working tree as their cwd, so a relative path is all this needs.
    _executable_script(hooks_dir / "post-checkout", "#!/bin/sh\nprintf 'CLOBBERED BY A GLOBAL HOOK\\n' > 00-index.md\n")
    _hostile_global_git_config(tmp_path, monkeypatch, f"[core]\n\thooksPath = {hooks_dir}\n")
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)

    result = run_cycle(config)

    assert result.tag_advanced is True
    assert (icloud_dir / "00-index.md").read_text() == "# Home\n"


def test_the_default_user_ignore_file_cannot_hide_a_device_creation_either(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same hole reached by the route that neutralising git's configuration files does *not*
    close, and the reason `core.excludesFile` is pinned on the invocation rather than left to the
    scrub: its default value is a path, `$XDG_CONFIG_HOME/git/ignore` (`~/.config/git/ignore`), so
    unsetting the config that names it changes nothing -- git reads the file anyway. Verified
    directly: with every configuration file replaced by `/dev/null`, an ignore file at the default
    location still made `git add -A` skip the note."""
    xdg_config_home = tmp_path / "xdg-config"
    (xdg_config_home / "git").mkdir(parents=True)
    (xdg_config_home / "git" / "ignore").write_text("phone-*.md\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config_home))
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    (icloud_dir / "phone-draft.md").write_text("typed on the phone, hidden by the default ignore file\n")

    result = run_cycle(config)

    assert result.drifted == ("phone-draft.md",)
    assert "hidden by the default ignore file" in _spooled_by_path(tmp_path)["phone-draft.md"].patch


def test_a_repository_local_color_config_cannot_corrupt_the_patch_header(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """`captures_content` now requires git's `diff --git ` header as *positive* evidence a patch
    arrived (see that predicate's own docstring, "the two guards before the marker"), which changed
    what `--no-color` is for: colour used to be a fidelity nuisance, now it is a hard reject. A
    repo-local `color.diff = always` -- a plausible operator leftover from debugging a diff by eye
    in that clone, exactly like the `diff.external` case above -- wraps the header in an ANSI escape
    (`\x1b[1mdiff --git a/x b/x\x1b[m`), so `captures_content` reads an ordinary markdown edit as
    `uncaptured`. And because the gate holds back the *whole* cycle on any uncaptured path, not just
    that one, this is a liveness failure rather than a fidelity one: the cycle never publishes, and
    the drift regenerates every cycle from a `LAST_CHECKOUT` that cannot move."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    _hostile_local_git_config(tmp_path / "cache-clone", ("color.diff", "always"))

    (icloud_dir / "00-index.md").write_text("a thought typed on the phone\n")

    result = run_cycle(config)

    assert result.drifted == ("00-index.md",)
    assert result.uncaptured == ()
    assert result.tag_advanced is True
    spooled = _spooled_by_path(tmp_path)
    assert "a thought typed on the phone" in spooled["00-index.md"].patch
    assert "\x1b[" not in spooled["00-index.md"].patch


def test_an_in_tree_textconv_driver_cannot_report_a_binary_modification_as_captured(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """The route neutralising the *environment* deliberately cannot close (see the section comment
    above `test_a_repository_local_diff_external_cannot_replace_the_patch_either`), applied to the
    quieter half of the same defect: a `.gitattributes` committed *into the vault itself*
    (`*.png diff=img`) naming a textconv driver defined in the cache clone's own `.git/config`. Both
    are read regardless of the environment scrub -- one is in-tree, the other repo-local -- so only
    `--no-textconv` on the decision diff invocations closes this. A driver that reports size rather
    than content (`PNG image data, %s bytes`) produces an ordinary-looking hunk with two different,
    plausible strings and no binary marker anywhere -- `captures_content` reads that as a genuine
    capture of a change that in fact carries none of the actual bytes pasted on the phone.

    (A driver that reports the *same* string for both sides, which is the more obvious thing to
    reach for, does not exercise this at all -- verified directly: git omits the diff entirely when
    the textconv'd sides are identical, and an empty patch is already rejected by
    `captures_content`'s header check regardless of `--no-textconv`. Content-dependent-but-lossy
    output is what actually needs the flag.)

    No `monkeypatch.chdir` here: earlier drafts needed one, because git's attribute lookup also
    consults a `.gitattributes` sitting in the *invoking process's* cwd, not just the ones under
    `--work-tree` (verified directly, including against this very suite's own repo root -- its
    top-level `.gitattributes`, for `linguist-detectable`, is unrelated to `*.png` but its mere
    presence was enough to make git fall back to binary detection regardless of `--no-textconv` or
    the `core.attributesFile` pin, silently defeating this test and the pre-existing global
    textconv test above it). `vault_git/runner.py`'s `GitRunner.run` now pins the subprocess's
    `cwd` to `work_tree`, which fixes that -- but does not, on its own, make *this* test evidence
    of the fix: with `--no-textconv` present, a same-directory-level `.gitattributes` collision
    just makes git fall back to ordinary binary detection instead of the vault's `diff=img`, which
    is indistinguishable from `--no-textconv` doing its own job (verified directly: this test
    stays green with the `cwd` pin removed, precisely because both routes land on the same
    `Binary files ... differ` output).
    `test_the_vaults_own_gitattributes_is_not_overridden_by_the_launching_directory`, below, is the
    one that actually isolates the `cwd` pin -- see its docstring for why it needed a mechanism
    `--no-textconv` cannot also explain away."""
    push_commit(
        seeded_origin,
        tmp_path,
        {".gitattributes": "*.png diff=img\n"},
        "add gitattributes",
        binary_files={"_attachments/diagram.png": _PNG_BYTES},
    )
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    textconv = _executable_script(
        tmp_path / "hostile-textconv.sh",
        '#!/bin/sh\nprintf \'PNG image data, %s bytes\\n\' "$(wc -c < "$1")"\n',
    )
    _hostile_local_git_config(tmp_path / "cache-clone", ("diff.img.textconv", str(textconv)))

    edited_on_the_phone = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00annotated-on-the-phone"
    (icloud_dir / "_attachments" / "diagram.png").write_bytes(edited_on_the_phone)

    result = run_cycle(config)

    assert result.uncaptured == ("_attachments/diagram.png",)
    assert result.tag_advanced is False
    assert (icloud_dir / "_attachments" / "diagram.png").read_bytes() == edited_on_the_phone


def test_the_vaults_own_gitattributes_is_not_overridden_by_the_launching_directory(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`vault_git/runner.py` pins `GitRunner.run`'s subprocess `cwd` to `work_tree` because git's
    per-directory `.gitattributes` lookup does a filesystem probe relative to the *launching
    process's* cwd, not to `--work-tree`, even though `--work-tree` is always given explicitly --
    verified directly (`strace` on a real invocation). Left unpinned, a `.gitattributes` sitting
    wherever this process happens to start -- matching only by name, never by content -- silently
    overrides whatever the vault's own tracked `.gitattributes` says for that directory level.

    A `diff=<driver>` assignment is the wrong mechanism to prove this with, and the test above this
    one is the record of finding that out: with `--no-textconv` present, an overridden `diff=img`
    just falls back to ordinary content-sniffed binary detection -- the *same* `Binary files ...
    differ` output `--no-textconv` itself produces for a real driver, so the two causes are
    indistinguishable from the outcome alone. What isolates the `cwd` pin is an attribute that
    changes *whether* something is treated as binary in the first place, with no driver involved at
    all: `*.md -diff` on an ordinary, non-binary markdown file. Correctly resolved (against the
    vault's own tracked `.gitattributes`), it forces git to treat prose as binary and withhold it
    (`captures_content`'s job, working as intended on content the vault itself opted out of
    diffing) -- overridden by an unrelated file at cwd, the assignment vanishes and the very same
    edit diffs normally instead. Two different, unambiguous outcomes for a plain text change, with
    every `_DECISION_DIFF_FLAGS` flag held constant -- nothing here depends on any of them.

    **`monkeypatch.chdir` into a directory this test creates, holding a `.gitattributes` this test
    writes to contradict the vault's.** An earlier revision took the opposite approach and ran under
    pytest's ordinary cwd, on the reasoning that this checkout's own top-level `.gitattributes` (an
    unrelated `linguist-detectable` rule) supplies the hostile file for free and that leaving cwd
    alone is therefore the more faithful reproduction. It is not, because that precondition is
    external and unasserted: measured with the `cwd` pin removed, this test failed when run from the
    checkout root and *passed* -- mutation undetected -- when run from a directory with no
    `.gitattributes`, so a one-line change to a file kept for an unrelated linguist tweak silently
    retires the only test isolating a production fix. The `chdir` the previous commit removed was
    one into a *safe* directory, dodging the bug; this one is into a deliberately *hostile* one,
    which is the direction that makes the outcome evidence rather than luck."""
    hostile_launch_directory = tmp_path / "hostile-launch-directory"
    hostile_launch_directory.mkdir()
    (hostile_launch_directory / ".gitattributes").write_text("*.md diff\n")
    monkeypatch.chdir(hostile_launch_directory)
    # The precondition, owned and asserted rather than inherited: an unrelated file, matching the
    # vault's own only by name, saying the opposite of what the vault says.
    assert (hostile_launch_directory / ".gitattributes").read_text() == "*.md diff\n"

    push_commit(seeded_origin, tmp_path, {".gitattributes": "*.md -diff\n"}, "opt markdown out of diffing")
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    (icloud_dir / "00-index.md").write_text("a thought typed on the phone\n")

    result = run_cycle(config)

    assert result.uncaptured == ("00-index.md",)
    assert result.tag_advanced is False
    assert (icloud_dir / "00-index.md").read_text() == "a thought typed on the phone\n"


def test_a_device_path_literally_named_head_does_not_break_the_reset_step(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """The one bit of collateral the `cwd` pin above reintroduces, one directory level up from
    where the identical shape of bug was first caught and rejected (see `vault_git/runner.py`'s
    comment: a *subdirectory* of `git_dir` was tried first and broke every pathspec-bearing call in
    this suite outright). `work_tree`'s own top level is no longer just where paths *live*, it is
    also cwd for every invocation -- so a device-created path with no extension that happens to be
    spelled exactly `HEAD` collides with the bare `HEAD` argument `cycle.py`'s step 5 passes to
    `git reset --hard`, the same "ambiguous argument 'HEAD': both revision and filename" this suite
    hit immediately while the `cwd` pin was still landing on `git_dir` itself. `cycle.py` closes it
    with the trailing `--` git's own error message recommends. Real device content, a real cycle,
    checking the cycle completes and captures the file rather than raising."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    (icloud_dir / "HEAD").write_text("a device-created path that happens to be spelled like a git ref\n")

    result = run_cycle(config)

    assert result.tag_advanced is True
    entry = _spooled_by_path(tmp_path)["HEAD"]
    assert entry.kind == "create"
    assert "a device-created path that happens to be spelled like a git ref" in entry.patch


def test_a_repository_local_diff_renames_config_falsely_withholds_a_renamed_binary(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """Rename detection is not cosmetic for a binary: `captures_content` deliberately passes a pure
    rename with no hunk at all ("the header describes the change completely" -- see that
    predicate's own docstring), which is exactly the shape that lets a renamed image through
    without needing to carry its bytes a second time. A repo-local `diff.renames = false` -- the
    config an operator debugging a large rename-heavy diff plausibly leaves behind -- is what
    `--find-renames` on the decision diff invocations exists to override.

    Without detection, `R100` decomposes into a `D` (old path) and an `A` (new path). The `D` is
    captured regardless -- a deletion always passes, since the pre-deletion bytes are still at
    `LAST_CHECKOUT` (`captures_content`'s docstring, "a deletion passes for the rename's reason").
    The `A` is a binary creation with no textual content, which `captures_content` correctly
    withholds -- exactly `test_a_binary_created_on_the_device_is_never_published_over`'s case,
    except nothing was actually created here: the same bytes just moved. So losing
    `--find-renames` does not lose any bytes for a *binary* rename -- it wrongly reports one as
    uncaptured and stalls the whole cycle behind it, a liveness failure rather than a data-loss
    one. (A *text* rename in the same scenario is not even wrong: the decomposed `A` still carries
    the new path's full text content in its patch, so `select_spool_entries` just spools two
    entries -- delete, then create -- in place of one rename. That is why this test targets a
    binary specifically, and asserts liveness rather than the R100/D+A distinction itself.)"""
    push_commit(seeded_origin, tmp_path, {}, "add diagram", binary_files={"_attachments/diagram.png": _PNG_BYTES})
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    _hostile_local_git_config(tmp_path / "cache-clone", ("diff.renames", "false"))

    (icloud_dir / "_attachments" / "diagram.png").rename(icloud_dir / "_attachments" / "diagram-renamed.png")

    result = run_cycle(config)

    assert result.uncaptured == ()
    assert result.tag_advanced is True
    entry = _spooled_by_path(tmp_path)["_attachments/diagram-renamed.png"]
    assert entry.kind == "rename"
    assert entry.old_path == "_attachments/diagram.png"
    assert "similarity index 100%" in entry.patch


def test_the_default_user_attributes_file_can_smuggle_a_content_filter_past_a_markdown_edit(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same hole `test_the_default_user_ignore_file_cannot_hide_a_device_creation_either` proves
    for `core.excludesFile`, which `core.attributesFile` shares the identical justification with
    (`vault_git/runner.py`'s own comment: both are pinned because their *defaults* point into the
    operator's home directory, and unsetting the config that names them does not stop git reading
    them) but had no test of its own.

    Not exercised through a textconv driver -- verified directly that a textconv-driver route here
    is already fully closed by `--no-textconv` regardless of whether this pin is present, since
    that flag disables textconv outright rather than only where the attribute assignment was
    found; a test built that way would not go red for removing this pin at all. What actually
    needs this pin specifically is a route none of the decision diff flags touch, because it does
    not run at diff time: a `clean` filter, which git applies while *staging* content
    (`git add -A`), before any diff is ever taken. A `.gitattributes` at the default
    `core.attributesFile` location (`$XDG_CONFIG_HOME/git/attributes`, unreached by the
    `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_NOSYSTEM` scrub for the same reason `core.excludesFile`'s
    default is) assigns a filter to `00-index.md`; the filter itself is defined repo-locally (a
    route left open on purpose). The result is a text-for-text substitution with no binary marker
    anywhere to catch, ordinary in every way `captures_content` checks -- the thought typed on the
    phone is replaced before git ever sees it, and the gate reports the cycle healthy."""
    xdg_config_home = tmp_path / "xdg-config"
    (xdg_config_home / "git").mkdir(parents=True)
    (xdg_config_home / "git" / "attributes").write_text("00-index.md filter=mangle\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config_home))
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    clean_filter = _executable_script(
        tmp_path / "hostile-clean-filter.sh",
        "#!/bin/sh\necho 'placeholder content, not the real edit'\n",
    )
    _hostile_local_git_config(tmp_path / "cache-clone", ("filter.mangle.clean", str(clean_filter)))

    (icloud_dir / "00-index.md").write_text("a thought typed on the phone\n")

    result = run_cycle(config)

    assert result.drifted == ("00-index.md",)
    assert result.uncaptured == ()
    assert result.tag_advanced is True
    spooled = _spooled_by_path(tmp_path)
    assert "a thought typed on the phone" in spooled["00-index.md"].patch
    assert "placeholder content, not the real edit" not in spooled["00-index.md"].patch


# --- idempotency from an arbitrary starting state ------------------------------------------------


def test_cycle_recovers_from_a_working_tree_left_mid_overlay_by_a_prior_crash(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """Simulates a crash between step 2 (overlay) and step 5 (reset): the parked clone's working
    tree has stray, uncommitted, untracked content sitting in it when the next cycle starts. Step
    1's `checkout -f` + `clean -fd` must recover cleanly rather than erroring or corrupting the
    next comparison."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
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


def test_cycle_recovers_from_a_working_tree_left_on_main_by_a_prior_crash(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """The residue step 1 actually exists for, and the one its sibling test cannot reach.

    A crash *after* step 5 (reset, check out `main`, pull) leaves the clone on `main`, ahead of
    `LAST_CHECKOUT`, with the tag still naming the older revision. HEAD is not something the overlay
    touches -- rsync writes files, it does not move refs -- so unless step 1 forces the tree back to
    `LAST_CHECKOUT`, the comparison runs against `main` instead of against what was actually last
    placed in iCloud, and every path the agent changed upstream reads as a device-side *reversion*.

    The sibling crash test plants stray files and staged content, both of which the overlay's own
    `--delete` and checksum copy neutralise before step 1 could matter -- which is why deleting step
    1 outright leaves it green. This one plants residue rsync cannot undo.
    """
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    parked_at = _tag_sha(tmp_path)

    push_commit(seeded_origin, tmp_path, {"00-index.md": "# Home (agent update)\n"}, "agent update")

    cache_clone_dir = tmp_path / "cache-clone"
    run_git("fetch", "-q", "origin", "main", cwd=cache_clone_dir)
    run_git("checkout", "-q", "-B", "main", "FETCH_HEAD", cwd=cache_clone_dir)
    assert _tag_sha(tmp_path) == parked_at

    result = run_cycle(config)

    assert result.drifted == ()
    assert _spooled_by_path(tmp_path) == {}
    assert result.tag_advanced is True
    assert (icloud_dir / "00-index.md").read_text() == "# Home (agent update)\n"


# --- idempotence property: arbitrary crash residue ------------------------------------------------
#
# The two tests above are one residue shape each -- stray files plus a staged modification; a clone
# left checked out on `main` ahead of the tag -- chosen by a human because they're the two crash
# points the design doc's own history called out (§4 Plane B, "Why the gate moved, not
# disappeared"). `cycle.py`'s own docstring commits to a broader claim: "idempotent from any
# starting state", recovered by step 1 forcing the tree back to `LAST_CHECKOUT` rather than by
# bookkeeping how a prior cycle ended. This property generalizes to a generated combination of both
# residue shapes (and their absence), rather than only the two hand-picked ones.
#
# **Why not "running the cycle twice equals running it once."** That statement is false on its own
# terms: a second run legitimately reports fresh drift the first didn't, if a device edit lands
# between the two -- equality of outcome was never the actual claim. What step 1 *is* a commitment
# to is narrower and does hold unconditionally: crash residue -- bytes nobody on the device ever
# typed, left behind by an interrupted prior cycle -- must never surface as spooled drift or reach
# the published iCloud tree, and a genuine, concurrent device edit must still be captured correctly
# no matter how much residue sits alongside it. That's the invariant checked below, phrased as a
# statement about the spool and iCloud's contents from outside cycle.py, not as a restatement of
# what checkout -f/clean -fd do.
#
# Every environment below is built fresh per generated example, not reused via a shared fixture:
# pytest resolves function-scoped fixtures once per test *invocation*, not once per Hypothesis
# draw, so a shared `tmp_path`/`seeded_origin` would let one example's residue bleed into the next.
# `_push_commit`, above, already relies on the same per-call uniqueness (`uuid.uuid4().hex`) for the
# same reason.

_RESIDUE_MARKER = "CRASH-RESIDUE-CONTENT-NEVER-TYPED-BY-A-HUMAN"

_residue_body = st.text(
    alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7A, exclude_characters="/\\"), min_size=1, max_size=24
)
_stray_name = st.text(alphabet="abcdefghijklmnopqrstuvwxyz-", min_size=3, max_size=10).map(
    lambda s: f"crash-residue-{s}.md"
)


@dataclass(frozen=True)
class _CrashResidue:
    # The residue shape of test_cycle_recovers_from_a_working_tree_left_on_main_by_a_prior_crash:
    # a crash after step 5 (reset, checkout main, pull) leaves the clone on `main`, not back on the
    # parked tag.
    left_on_main: bool
    # Whether a new upstream commit exists before the residue is applied -- without this,
    # `left_on_main` alone re-checks-out the same content the tag already names, and never actually
    # diverges from it.
    push_upstream_first: bool
    # The residue shape of test_cycle_recovers_from_a_working_tree_left_mid_overlay_by_a_prior_crash:
    # a staged, uncommitted mutation on a path that already exists at the baseline.
    tracked_mutation: str  # "none" | "modify" | "delete"
    # Stray untracked files, at paths a device edit could never produce by construction (see
    # `_stray_name`), standing in for whatever an interrupted overlay/stage left lying around.
    strays: tuple[tuple[str, str], ...]
    # A real, concurrent device edit, alongside whatever residue this example also generated --
    # the property's job is this combination, not residue in isolation (see this section's own
    # docstring, "the property's job is the space between and around them").
    genuine_device_edit: bool


@st.composite
def _crash_residue(draw: st.DrawFn) -> _CrashResidue:
    stray_count = draw(st.integers(min_value=0, max_value=2))
    names = draw(st.lists(_stray_name, min_size=stray_count, max_size=stray_count, unique=True))
    strays = tuple((name, draw(_residue_body)) for name in names)
    return _CrashResidue(
        left_on_main=draw(st.booleans()),
        push_upstream_first=draw(st.booleans()),
        tracked_mutation=draw(st.sampled_from(("none", "modify", "delete"))),
        strays=strays,
        genuine_device_edit=draw(st.booleans()),
    )


def _apply_crash_residue(cache_clone_dir: Path, residue: _CrashResidue) -> None:
    """Simulate the on-disk state a crash could leave, applied directly against the parked clone --
    the same way the two hand-written crash tests above do, generalized to an arbitrary
    combination."""
    if residue.left_on_main:
        run_git("fetch", "-q", "origin", "main", cwd=cache_clone_dir)
        run_git("checkout", "-q", "-B", "main", "FETCH_HEAD", cwd=cache_clone_dir)

    if residue.tracked_mutation == "modify":
        (cache_clone_dir / "00-index.md").write_text(f"{_RESIDUE_MARKER}\n")
    elif residue.tracked_mutation == "delete":
        (cache_clone_dir / "00-index.md").unlink(missing_ok=True)

    for name, body in residue.strays:
        (cache_clone_dir / name).write_text(f"{_RESIDUE_MARKER} {body}\n")

    if residue.tracked_mutation != "none" or residue.strays:
        run_git("add", "-A", cwd=cache_clone_dir)


@settings(deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(residue=_crash_residue())
def test_cycle_recovers_from_arbitrary_generated_crash_residue(tmp_path: Path, residue: _CrashResidue) -> None:
    unique = uuid.uuid4().hex
    origin = tmp_path / f"prop-origin-{unique}.git"
    origin.mkdir()
    run_git("init", "--bare", "-q", "--initial-branch=main", cwd=origin)
    seed_clone = tmp_path / f"prop-seed-{unique}"
    run_git("clone", "-q", str(origin), str(seed_clone), cwd=tmp_path)
    (seed_clone / "00-index.md").write_text("# Home\n")
    run_git("add", "-A", cwd=seed_clone)
    run_git(
        "-c", "user.name=seed", "-c", "user.email=seed@example.invalid", "commit", "-q", "-m", "seed", cwd=seed_clone
    )
    run_git("push", "-q", "origin", "main", cwd=seed_clone)

    icloud = tmp_path / f"prop-icloud-{unique}"
    icloud.mkdir()
    config = replicate_config(tmp_path / f"prop-work-{unique}", origin, icloud)

    run_cycle(config)  # bootstrap: establishes the parked clone and LAST_CHECKOUT

    if residue.push_upstream_first:
        push_commit(origin, tmp_path, {"00-index.md": "# Home (agent update)\n"}, "agent update")

    cache_clone_dir = Path(config.cache_clone_dir)
    _apply_crash_residue(cache_clone_dir, residue)

    if residue.genuine_device_edit:
        (icloud / "genuine-drift.md").write_text("a real device edit typed by a human\n")

    result = run_cycle(config)

    # The cycle completes normally regardless of the residue -- no wedge, whatever shape a crash
    # left behind.
    assert result.tag_advanced is True

    # Crash residue never surfaces as spooled drift, whatever shape it took -- only the genuine
    # device edit (if any) does.
    expected_drift = ("genuine-drift.md",) if residue.genuine_device_edit else ()
    assert result.drifted == expected_drift

    spool_dir = Path(config.spool_dir)
    spooled_text = "".join(f.read_text() for f in spool_dir.glob("*.json")) if spool_dir.is_dir() else ""
    assert _RESIDUE_MARKER not in spooled_text

    # The residue never reaches the working tree it was recovered into...
    for name, _body in residue.strays:
        assert not (cache_clone_dir / name).exists()
    assert (cache_clone_dir / "00-index.md").read_text() != f"{_RESIDUE_MARKER}\n"

    # ...nor the published iCloud tree.
    for name, _body in residue.strays:
        assert not (icloud / name).exists()
    expected_index = "# Home (agent update)\n" if residue.push_upstream_first else "# Home\n"
    assert (icloud / "00-index.md").read_text() == expected_index


# --- the crash window between publish and the tag advance: ppat/obsidian-tools#36 -----------------
#
# Steps 6 and 7 are two operations and cannot be made one, so a crash can always land between them:
# publish has placed the fresh upstream tree in iCloud, and `LAST_CHECKOUT` still names the
# pre-publish commit. The next cycle parks at that older tag, overlays an iCloud tree that has
# already moved past it, and reads every path the upstream commit touched as device-side drift.
#
# **The device does not suppress that, and must not.** Deciding a drifted path is not really a human
# edit is a judgement, and docs/DESIGN.md §1.5 R2 reserves every such judgement for the server: the
# device-side detector "submits every path the comparison flags, and makes no judgement, so it can
# never silently drop a real edit". What the device does instead is record what it *observed* --
# which baseline the comparison ran against, which upstream revision it knew at that moment, and
# whether this path's content is byte-identical to that revision -- so `drift-processor` (Phase 5)
# can refuse to stamp `authority: human` on content that demonstrably came from upstream. Facts on
# the record; the verdict stays server-side.


def _origin_head(origin: Path) -> str:
    return run_git("rev-parse", "main", cwd=origin).stdout.strip()


def _push_tree_change(origin: Path, tmp_path: Path, message: str, mutate: Callable[[Path], None]) -> str:
    """`conftest.push_commit` for changes that aren't "write these files": a deletion, a rename.
    Returns the pushed commit's SHA, which is what `upstream_sha` on a spool entry must name."""
    clone = tmp_path / f"push-clone-{uuid.uuid4().hex}"
    run_git("clone", "-q", str(origin), str(clone), cwd=tmp_path)
    mutate(clone)
    run_git("add", "-A", cwd=clone)
    run_git("-c", "user.name=x", "-c", "user.email=x@example.invalid", "commit", "-q", "-m", message, cwd=clone)
    run_git("push", "-q", "origin", "main", cwd=clone)
    return run_git("rev-parse", "HEAD", cwd=clone).stdout.strip()


def _crash_cycle_at(seam: str, config: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run one cycle that dies at `seam`, one of the collaborators `cycle.py` calls by name.
    Patched in `cycle.py`'s own module namespace, so everything ahead of the seam -- real git, real
    rsync -- has already happened and nothing after it runs at all. The same mechanism the
    crash-injection harness uses (`tests/test_local_replicator_crash_injection.py`), for the same
    reason: a stub that raises before doing any work models a killed process, where hand-writing the
    residue would only model our belief about what one leaves behind."""

    def _die(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(f"simulated crash at {seam}")

    monkeypatch.setattr(cycle_module, seam, _die)
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            run_cycle(config)  # pyright: ignore[reportArgumentType] -- ReplicateConfig, kept loose for the helper
    finally:
        monkeypatch.undo()


def _crash_between_publish_and_tag_advance(config: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """The #36 window itself: step 6 has placed the fresh tree in iCloud, step 7 never runs."""
    _crash_cycle_at("advance_last_checkout", config, monkeypatch)


def test_content_republished_by_a_crashed_cycle_is_still_spooled(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half of #36 that is deliberately *not* fixed. The device keeps submitting the path --
    dropping it would be exactly the silent judgement docs/DESIGN.md §1.5 R2 forbids, and the
    coincidence case (a human edit that happens to reproduce upstream byte-for-byte) is
    indistinguishable from crash residue, so a suppressing device would drop real edits."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    push_commit(seeded_origin, tmp_path, {"00-index.md": "# Home (agent update)\n"}, "agent update")

    _crash_between_publish_and_tag_advance(config, monkeypatch)

    result = run_cycle(config)

    assert result.drifted == ("00-index.md",)
    assert set(_spooled_by_path(tmp_path)) == {"00-index.md"}


def test_content_republished_by_a_crashed_cycle_is_marked_as_matching_upstream(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fix for #36: the entry carries the three facts that make it legible to Phase 5's
    `drift-processor` -- the baseline the comparison ran against, the upstream revision known at
    that moment, and that this path's content is byte-identical to it. `baseline_sha !=
    upstream_sha` is the crash's own signature: a cycle that completed leaves the two equal."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    baseline = _tag_sha(tmp_path)
    push_commit(seeded_origin, tmp_path, {"00-index.md": "# Home (agent update)\n"}, "agent update")
    upstream = _origin_head(seeded_origin)

    _crash_between_publish_and_tag_advance(config, monkeypatch)

    # The window this is all about: iCloud holds the new content, the tag still names the old commit.
    assert (icloud_dir / "00-index.md").read_text() == "# Home (agent update)\n"
    assert _tag_sha(tmp_path) == baseline

    run_cycle(config)

    entry = _spooled_by_path(tmp_path)["00-index.md"]
    assert entry.matches_upstream is True
    assert entry.baseline_sha == baseline
    assert entry.upstream_sha == upstream
    assert "# Home (agent update)" in entry.patch  # the patch itself is unchanged by the annotation


def test_a_genuine_device_edit_is_never_marked_as_matching_upstream(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """The direction that costs something if it is wrong. A human's edit marked as matching upstream
    is an edit Phase 5 could discard as crash residue -- so this is the assertion that stands
    between the annotation and the silent data loss the whole component exists to prevent."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    (icloud_dir / "00-index.md").write_text("typed on the phone\n")

    run_cycle(config)

    entry = _spooled_by_path(tmp_path)["00-index.md"]
    assert entry.matches_upstream is False
    # No crash, nothing new upstream: the two shas agree, which is what a healthy cycle looks like.
    assert entry.baseline_sha == entry.upstream_sha


def test_the_comparison_names_the_upstream_revision_known_before_this_cycles_fetch(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """Which revision `matches_upstream` is measured against, pinned. The drift enumeration runs
    *before* the pull (step 3 before step 5), so the only upstream revision that can be observed is
    the one the clone already had -- and that is the right one, because it is the revision whose
    tree the last publish placed in iCloud. Measuring against the revision this cycle is about to
    fetch would mark a human's edit as upstream content whenever an agent happened to write the same
    text upstream in the meantime, which is the one error direction that loses data."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    before_fetch = _origin_head(seeded_origin)

    # An agent writes upstream, and the human independently types the identical text on the phone,
    # both between cycles. Nothing has published that upstream commit to iCloud yet.
    push_commit(seeded_origin, tmp_path, {"00-index.md": "# Home (same text)\n"}, "agent update")
    (icloud_dir / "00-index.md").write_text("# Home (same text)\n")

    run_cycle(config)

    entry = _spooled_by_path(tmp_path)["00-index.md"]
    assert entry.upstream_sha == before_fetch
    assert entry.matches_upstream is False


def test_a_republished_upstream_deletion_is_marked_as_matching_upstream(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What "matches upstream" means for a path upstream deleted: absent there too. A deletion
    carries no content to compare, so the fact recorded is the one that is actually decidable --
    the baseline still has this path, iCloud no longer does, and neither does upstream."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    push_commit(seeded_origin, tmp_path, {"10-areas/doomed.md": "upstream content\n"}, "add doomed")
    run_cycle(config)
    assert (icloud_dir / "10-areas" / "doomed.md").exists()

    _push_tree_change(seeded_origin, tmp_path, "delete doomed", lambda clone: (clone / "10-areas/doomed.md").unlink())
    _crash_between_publish_and_tag_advance(config, monkeypatch)
    assert not (icloud_dir / "10-areas" / "doomed.md").exists()  # publish's `--delete` already ran

    run_cycle(config)

    entry = _spooled_by_path(tmp_path)["10-areas/doomed.md"]
    assert entry.kind == "delete"
    assert entry.matches_upstream is True


def test_a_device_deletion_of_a_path_upstream_still_has_is_not_marked_as_matching_upstream(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """The counterpart that keeps the deletion rule honest: a human deleting a note upstream still
    holds is a real device-side deletion, and must reach Phase 5 as one."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    (icloud_dir / "00-index.md").unlink()

    run_cycle(config)

    entry = _spooled_by_path(tmp_path)["00-index.md"]
    assert entry.kind == "delete"
    assert entry.matches_upstream is False


def test_a_republished_upstream_rename_is_marked_as_matching_upstream(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rename touches two paths, so the fact has to hold for both: the new path's content
    identical to upstream's, *and* the old path gone from upstream as well. Requiring both is what
    keeps a half-coincidence -- a device edit that happens to reproduce upstream's new file while
    upstream still holds the old one -- from being recorded as upstream content."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    push_commit(seeded_origin, tmp_path, {"10-areas/before.md": "a note with enough text to match\n"}, "add before")
    run_cycle(config)

    def _rename(clone: Path) -> None:
        (clone / "10-areas/after.md").write_text((clone / "10-areas/before.md").read_text())
        (clone / "10-areas/before.md").unlink()

    _push_tree_change(seeded_origin, tmp_path, "rename before to after", _rename)
    _crash_between_publish_and_tag_advance(config, monkeypatch)
    assert (icloud_dir / "10-areas" / "after.md").exists()

    run_cycle(config)

    entry = _spooled_by_path(tmp_path)["10-areas/after.md"]
    assert entry.kind == "rename"
    assert entry.old_path == "10-areas/before.md"
    assert entry.matches_upstream is True


def test_a_device_rename_is_not_marked_as_matching_upstream(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """The old path is what gives this away: upstream still has it, so the device's rename is a
    real one however closely the new path's content resembles something upstream holds."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    push_commit(seeded_origin, tmp_path, {"10-areas/before.md": "a note with enough text to match\n"}, "add before")
    run_cycle(config)

    (icloud_dir / "10-areas" / "after.md").write_text((icloud_dir / "10-areas" / "before.md").read_text())
    (icloud_dir / "10-areas" / "before.md").unlink()

    run_cycle(config)

    entry = _spooled_by_path(tmp_path)["10-areas/after.md"]
    assert entry.kind == "rename"
    assert entry.matches_upstream is False


def test_an_ordinary_gated_cycle_also_produces_matches_upstream_on_a_real_human_edit(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """**No crash anywhere in this test**, and the entry still comes out looking exactly like #36
    residue. This is the case that governs how `matches_upstream` may be read at Phase 5, and it is
    the reason the field licenses far less than an earlier reading of it claimed.

    `upstream_sha` is only "the revision whose tree the last publish placed in iCloud" when the
    previous cycle both fetched *and* published. Step 5 fetches unconditionally while step 6 is
    gated, so any withheld cycle -- a crash, a spool-write failure, or, as here, the wholly ordinary
    gate on a pasted image (`captures_content`) -- leaves the remote-tracking ref ahead of anything
    iCloud has ever seen, and it runs further ahead every cycle the gate persists.

    In that state a human's own deletion of a note upstream happens to have deleted too records
    `matches_upstream: true` with `baseline_sha != upstream_sha`: byte-for-byte the signature of
    republished upstream content. The observation is *correct* -- the path genuinely is absent from
    `upstream_sha` -- but a consumer treating that signature as proof of crash residue would discard
    a genuine human deletion. Note also that this needed no content coincidence at all, only a path
    coincidence, which is a far likelier thing for an agent reorganising upstream to produce."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    push_commit(seeded_origin, tmp_path, {"10-areas/note.md": "a note that exists in both places\n"}, "add note")
    run_cycle(config)
    baseline = _tag_sha(tmp_path)

    # An agent deletes the note upstream. Independently, the device acquires an out-of-contract
    # binary, which gates every subsequent cycle's publish and tag advance.
    _push_tree_change(seeded_origin, tmp_path, "delete note", lambda clone: (clone / "10-areas/note.md").unlink())
    (icloud_dir / "pasted.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03")

    gated = run_cycle(config)
    assert gated.uncaptured == ("pasted.png",)
    assert gated.tag_advanced is False
    assert _tag_sha(tmp_path) == baseline  # ...while the fetch inside the same cycle moved on

    # Now the human deletes the note on the device, for their own reasons.
    (icloud_dir / "10-areas" / "note.md").unlink()

    run_cycle(config)

    entry = _spooled_by_path(tmp_path)["10-areas/note.md"]
    assert entry.kind == "delete"
    assert entry.matches_upstream is True
    assert entry.baseline_sha != entry.upstream_sha


def test_a_device_deletion_is_not_hidden_by_an_unrelated_rename_pairing_against_upstream(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why the observation's own diff runs with rename detection *off*
    (`vault_git/runner.py`, `_IDENTITY_DIFF_FLAGS`), which nothing else here would catch.

    With detection on, `git diff --name-only` prints only a rename's *destination* -- the source
    path vanishes from the output. So a path the device deleted, which upstream still holds, can be
    silently paired with some unrelated path the index happens to hold and upstream does not, and
    then reads as "identical to upstream": a real human deletion recorded as republished upstream
    content, which is the direction that loses data.

    The pairing needs the baseline and the known upstream revision to disagree, which is exactly
    what a crash at the *publish* seam leaves behind -- the fetch moved the remote-tracking ref
    forward, the publish never ran, so iCloud still holds the pre-fetch tree.
    """
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    identical = "the same content in both notes, long enough to be paired as a rename\n"
    push_commit(seeded_origin, tmp_path, {"10-areas/p.md": identical, "10-areas/q.md": identical}, "add p and q")
    run_cycle(config)

    # Upstream deletes q.md. The crash at `publish` brings that commit into the clone (so the
    # comparison below runs against it) while leaving iCloud untouched, still holding both notes.
    _push_tree_change(seeded_origin, tmp_path, "delete q", lambda clone: (clone / "10-areas/q.md").unlink())
    _crash_cycle_at("publish", config, monkeypatch)
    assert (icloud_dir / "10-areas" / "q.md").exists()

    # A human deletes the *other* note -- a genuine device-side deletion of a path upstream still has.
    (icloud_dir / "10-areas" / "p.md").unlink()

    run_cycle(config)

    entry = _spooled_by_path(tmp_path)["10-areas/p.md"]
    assert entry.kind == "delete"
    assert entry.matches_upstream is False


# --- non-ASCII and quoted filenames, end to end --------------------------------------------------


def test_non_ascii_filename_drift_survives_the_full_git_and_rsync_pipeline(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """The exact bug class that "wedged the committer permanently" (ppat/obsidian-tools#3): a note
    titled in Japanese must not break `-z` porcelain parsing anywhere in this pipeline."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
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
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
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
    push_commit(
        seeded_origin,
        tmp_path,
        {
            ".obsidian/app.json": '{"legacyEditor": false}\n',
            ".obsidian/workspace.json": '{"instance": "cluster"}\n',
        },
        "obsidian baseline",
    )
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)

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


# --- `.obsidian/` is observed where it can be acted on, not where it can only be reported ---------


def test_a_device_edit_to_a_tracked_obsidian_file_is_never_spooled_as_drift(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """The defect confirmed on real hardware (ppat/obsidian-tools#46): a device edit to a tracked
    `.obsidian/` file spooled as drift, `publish` left the file untouched because it excludes
    `.obsidian/`, and the *next* cycle -- on identical, unchanged content -- spooled it again.

    The second cycle is the load-bearing assertion, not a repetition of the first: a component that
    merely failed to write the file back would still stop re-reporting once something reconciled it.
    Nothing ever does here, which is why re-running on unchanged content is what distinguishes
    "publication is deferred" from "this drift can never resolve"."""
    seed_obsidian_baseline_in_history(seeded_origin, tmp_path)
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    (icloud_dir / ".obsidian" / "app.json").write_text('{"legacyEditor": true}\n')

    first = run_cycle(config)
    second = run_cycle(config)

    assert first.drifted == ()
    assert second.drifted == ()
    assert _spooled_by_path(tmp_path) == {}
    assert first.tag_advanced is True
    assert second.tag_advanced is True
    assert (icloud_dir / ".obsidian" / "app.json").read_text() == '{"legacyEditor": true}\n'


def test_a_diverged_device_baseline_is_reported_on_every_cycle_it_lasts(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Detection is not given up, it moves: excluding `.obsidian/` from the overlay removes it from
    the spool, and the same allowlist `seed_baseline` places is compared against what the device
    actually holds. The report repeats every cycle deliberately -- the condition itself persists
    until an operator resolves it by hand, so a one-shot report would be visible only in whichever
    log window happened to contain the cycle the edit landed in."""
    seed_obsidian_baseline_in_history(seeded_origin, tmp_path)
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    baseline = _tag_sha(tmp_path)

    (icloud_dir / ".obsidian" / "app.json").write_text('{"legacyEditor": true}\n')

    with caplog.at_level(logging.INFO):
        first = run_cycle(config)
        second = run_cycle(config)

    assert first.obsidian_baseline_diverged == (".obsidian/app.json",)
    assert second.obsidian_baseline_diverged == (".obsidian/app.json",)
    events = [r for r in caplog.records if getattr(r, "event", None) == "obsidian_baseline_diverged"]
    assert len(events) == 2
    assert getattr(events[0], "paths", None) == [".obsidian/app.json"]
    # `warning`, not `info`: the level is the contested half of this decision (see the emitter's own
    # comment weighing the two), so it is the half a later edit must not be able to move quietly.
    assert events[0].levelno == logging.WARNING
    # Which baseline the device was found to differ from -- the only thing on this line that lets an
    # operator reproduce the comparison from the clone by hand.
    assert getattr(events[0], "baseline_sha", None) == baseline


def test_a_device_baseline_matching_the_parked_clone_reports_no_divergence(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The control that makes the report above readable at all: an ordinary seeded device, left
    alone, must stay silent. Without it, an implementation that reported every allowlisted path on
    every cycle would satisfy the divergence test and be indistinguishable from a working one."""
    seed_obsidian_baseline_in_history(seeded_origin, tmp_path)
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    with caplog.at_level(logging.INFO):
        result = run_cycle(config)

    assert result.obsidian_baseline_diverged == ()
    assert [r for r in caplog.records if getattr(r, "event", None) == "obsidian_baseline_diverged"] == []


def test_deleting_the_device_obsidian_directory_reseeds_without_spooling_the_baseline_as_deletions(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The documented device reset -- delete `.obsidian/` from the iCloud vault by hand and let the
    next cycle re-seed it -- run end to end. The overlay's `--delete` used to answer that empty
    directory by stripping the parked checkout's own baseline, so the cycle staged the whole frozen
    set as device-side deletions and spooled them as a human's edits, on the one cycle whose entire
    purpose is to put the baseline back."""
    seed_obsidian_baseline_in_history(seeded_origin, tmp_path)
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    assert (icloud_dir / ".obsidian" / "app.json").exists()

    shutil.rmtree(icloud_dir / ".obsidian")

    with caplog.at_level(logging.INFO):
        result = run_cycle(config)

    assert result.drifted == ()
    assert _spooled_by_path(tmp_path) == {}
    assert result.obsidian_seed_attempted is True
    # The control for the withheld-seed report: this cycle reaches the same unseeded device and
    # answers it, so the report must stay silent here or it says nothing anywhere.
    assert [r for r in caplog.records if getattr(r, "event", None) == "obsidian_baseline_unseeded"] == []
    # Bootstrap, not divergence: this cycle's device is missing every allowlisted path by
    # definition, and the re-seed below is what answers that. Reporting it as a diverged baseline
    # would make the signal fire loudest on precisely the cycle that resolves it.
    assert result.obsidian_baseline_diverged == ()
    assert (icloud_dir / ".obsidian" / "app.json").read_text() == '{"legacyEditor": false}\n'


def test_a_device_side_obsidian_symlink_is_never_spooled(tmp_path: Path, seeded_origin: Path, icloud_dir: Path) -> None:
    """The same exclusion, through the whole cycle, against the one entry shape that can defeat an
    rsync filter rule written for a directory. A `.obsidian` symlink that reaches the overlay is
    staged by `git add -A` as a mode-120000 blob whose content is its target path — publishing a
    path outside the vault into the drift stream, which is `ppat/obsidian-tools#22`'s blast radius —
    alongside a deletion of every baseline path the symlink displaced."""
    seed_obsidian_baseline_in_history(seeded_origin, tmp_path)
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    outside_the_vault = tmp_path / "outside-the-vault"
    (outside_the_vault).mkdir()
    (outside_the_vault / "app.json").write_text('{"legacyEditor": true}\n')
    shutil.rmtree(icloud_dir / ".obsidian")
    (icloud_dir / ".obsidian").symlink_to(outside_the_vault)

    result = run_cycle(config)

    assert result.drifted == ()
    assert _spooled_by_path(tmp_path) == {}


def test_untracked_obsidian_residue_in_the_parked_clone_is_pruned_before_it_is_compared(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """Excluding `.obsidian/` from the overlay removed the only thing that ever pruned the parked
    clone's copy of it: `git clean -fd` skips ignored paths and the vault's own `.gitignore` carries
    `.obsidian/` wholesale, `reset --hard` is tracked-only, and the overlay's `--delete` no longer
    reaches it. Anything a device deposited there before that -- an auto-updated community plugin's
    `main.js`, which the baseline allowlist admits -- would otherwise sit in the clone forever and be
    read by `diverged_baseline_paths` as the committed baseline, which it never was. The comparison
    walks the clone's working tree, so the working tree is what has to hold only what `HEAD` does."""
    seed_obsidian_baseline_in_history(seeded_origin, tmp_path)
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    residue = tmp_path / "cache-clone" / ".obsidian" / "plugins" / "device-only-plugin" / "main.js"
    residue.parent.mkdir(parents=True)
    residue.write_text("copied in from a device by an overlay that predates the exclusion\n")

    result = run_cycle(config)

    assert not residue.exists()
    assert result.obsidian_baseline_diverged == ()


def test_untracked_obsidian_residue_is_not_seeded_onto_a_device_being_reset(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """The other half, and the one that reaches the device: `seed_baseline` walks the same working
    tree, so a reset -- the documented remedy, which deletes `.obsidian/` from the iCloud vault by
    hand -- hands the device the committed baseline unioned with whatever a previous device left in
    the clone. Before the exclusion the overlay's own `--delete` wiped the residue earlier in the
    same cycle; nothing else ever did."""
    seed_obsidian_baseline_in_history(seeded_origin, tmp_path)
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    residue = tmp_path / "cache-clone" / ".obsidian" / "plugins" / "device-only-plugin" / "main.js"
    residue.parent.mkdir(parents=True)
    residue.write_text("copied in from a device by an overlay that predates the exclusion\n")
    shutil.rmtree(icloud_dir / ".obsidian")

    result = run_cycle(config)

    assert result.obsidian_seed_attempted is True
    assert (icloud_dir / ".obsidian" / "app.json").exists()  # the real baseline did arrive
    assert not (icloud_dir / ".obsidian" / "plugins" / "device-only-plugin").exists()


def test_a_large_divergence_set_is_sampled_with_its_full_count_beside_it(
    tmp_path: Path,
    seeded_origin: Path,
    icloud_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The list is bounded by the allowlist but not by anything small: eight top-level settings
    files, three per plugin, plus every `snippets/*.css` and `themes/*/*`. A cluster-side baseline
    change or an interrupted seed puts the whole set on one line, and a log pipeline's line-size
    limit drops an oversized line whole rather than truncating it -- taking the count with it, at
    the one moment the count is what an operator needs. Sampled with the count beside it, the same
    convention and the same limit `vault_git/baseline.py`'s walk fields use.

    The detector is stubbed rather than driven, because what is under test is the emitter: building
    150 real diverged paths would exercise `select_baseline_paths` again and pin nothing new."""
    seed_obsidian_baseline_in_history(seeded_origin, tmp_path)
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)  # bootstrap: seeds the device, so the divergence check is no longer gated off

    many = [f".obsidian/plugins/plugin-{n:03d}/main.js" for n in range(150)]

    def _many_diverged(_cache_clone_dir: Path, _icloud_vault_dir: Path) -> list[str]:
        return list(many)

    monkeypatch.setattr(cycle_module, "diverged_baseline_paths", _many_diverged)

    with caplog.at_level(logging.INFO):
        result = run_cycle(config)

    [record] = [r for r in caplog.records if getattr(r, "event", None) == "obsidian_baseline_diverged"]
    assert getattr(record, "path_count", None) == 150
    assert getattr(record, "paths", None) == many[:100]
    # The result itself is not sampled -- the cap is a property of the log line, and
    # `cycle_complete`'s count is taken from this.
    assert len(result.obsidian_baseline_diverged) == 150


def test_a_device_left_without_a_baseline_by_a_withheld_cycle_is_reported_rather_than_silent(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The state with the largest possible divergence is the one the divergence comparison cannot
    see. A device reset deletes `.obsidian/` by hand and the re-seed sits behind the publish gate,
    which an uncaptured path -- a pasted image, which `drift.captures_content` treats as the routine
    case -- holds shut for as long as it lasts. The device is then left with no settings lock, no
    plugins and no template folder, while `is_baselined()` is False so the comparison is skipped as
    bootstrap. Nothing else names the state: `cycle_complete` reads field for field as it does on a
    healthy seeded cycle, `obsidian_seed_attempted: false` included, and the two events a gated
    cycle does emit name the pasted image rather than `.obsidian/`."""
    seed_obsidian_baseline_in_history(seeded_origin, tmp_path)
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    shutil.rmtree(icloud_dir / ".obsidian")
    (icloud_dir / "pasted.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03")

    with caplog.at_level(logging.INFO):
        first = run_cycle(config)
        second = run_cycle(config)

    assert first.obsidian_seed_attempted is False
    assert second.obsidian_seed_attempted is False
    assert first.obsidian_baseline_diverged == ()
    assert not (icloud_dir / ".obsidian").exists()
    records = [r for r in caplog.records if getattr(r, "event", None) == "obsidian_baseline_unseeded"]
    assert len(records) == 2  # every cycle it lasts, for the same reason the divergence event repeats
    assert records[0].levelno == logging.WARNING


def test_untracked_obsidian_residue_is_not_seeded_after_the_last_checkout_tag_is_deleted(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """The runbook's other reset, and the one branch the re-park block never runs on. Deleting
    `LAST_CHECKOUT` without removing the rest of the clone is a documented whole-vault reset, and it
    makes `previous_checkout` None -- so `seed_baseline` walks a working tree nothing pruned this
    cycle. An operator reaches this state by escalating rather than by choosing it: the
    `.obsidian/`-only reset leaves a device unseeded for as long as the publish gate is shut, and
    dropping the tag is what the runbook offers next."""
    seed_obsidian_baseline_in_history(seeded_origin, tmp_path)
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    residue = tmp_path / "cache-clone" / ".obsidian" / "plugins" / "device-only-plugin" / "main.js"
    residue.parent.mkdir(parents=True)
    residue.write_text("copied in from a device by an overlay that predates the exclusion\n")
    shutil.rmtree(icloud_dir / ".obsidian")
    run_git("tag", "-d", "LAST_CHECKOUT", cwd=tmp_path / "cache-clone")

    result = run_cycle(config)

    assert result.obsidian_seed_attempted is True
    assert (icloud_dir / ".obsidian" / "app.json").exists()  # the real baseline did arrive
    assert not (icloud_dir / ".obsidian" / "plugins" / "device-only-plugin").exists()
    assert not residue.exists()


def test_obsidian_residue_carrying_its_own_git_directory_is_pruned_too(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`git clean` will not remove a directory that is itself a git repository unless force is given
    twice, and it declines at exit 0. A plugin installed from source or through BRAT is a git
    checkout, which makes the residue shape an operator is most likely to have produced by hand the
    one shape a single `-f` leaves in place permanently -- and leaves in place *quietly*, which the
    second assertion here is for: `obsidian_residue_prune_incomplete` is keyed on a non-zero exit,
    so it does not fire, and weakening the flag goes red on the first assertion with no log line
    anywhere naming what survived."""
    seed_obsidian_baseline_in_history(seeded_origin, tmp_path)
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    residue_dir = tmp_path / "cache-clone" / ".obsidian" / "plugins" / "developer-installed-plugin"
    residue_dir.parent.mkdir(parents=True)
    run_git("init", "-q", "-b", "main", str(residue_dir), cwd=tmp_path)
    (residue_dir / "main.js").write_text("a plugin checked out from source, not from the baseline\n")

    with caplog.at_level(logging.INFO):
        result = run_cycle(config)

    assert not residue_dir.exists()
    assert result.obsidian_baseline_diverged == ()
    assert [r for r in caplog.records if getattr(r, "event", None) == "obsidian_residue_prune_incomplete"] == []


def test_the_residue_prune_is_bounded_to_the_vault_roots_own_obsidian_directory(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """`-x` reaches ignored content, which is what the prune is for and equally why it must stay
    pathspec-scoped. The vault's own `.gitignore` carries `.obsidian/` with no leading slash, so git
    ignores a directory of that name at any depth, while a git pathspec is anchored at the work-tree
    root -- and the overlay excludes `.obsidian` at any depth too, so nothing would put back what an
    unscoped `-x` deleted. A vault holding a nested vault is the concrete case; the general one is
    that every ignored path in the clone outside the frozen baseline is content this statement has
    no business touching."""
    seed_obsidian_baseline_in_history(seeded_origin, tmp_path)
    push_commit(seeded_origin, tmp_path, {"10-areas/note.md": "a note in a real vault folder\n"}, "add a note")
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    nested = tmp_path / "cache-clone" / "10-areas" / ".obsidian" / "app.json"
    nested.parent.mkdir(parents=True)
    nested.write_text("a nested vault's own settings, which this cycle never seeds or compares\n")

    run_cycle(config)

    assert nested.exists()


def test_a_residue_prune_that_cannot_finish_does_not_stop_the_cycle(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`git clean` exits 1 on any entry it cannot remove and any directory it cannot read, and the
    prune runs ahead of the drift capture -- so treating that exit as fatal would let one permission
    fault inside a local cache directory stop the vault replicating at all, on this cycle and every
    subsequent one. `_iter_obsidian_candidates` names the identical condition on this same directory
    as expected and goes to length to degrade gracefully on it. What degrading costs is asserted
    here too, because it is real: the comparison then reads residue the prune could not remove."""
    seed_obsidian_baseline_in_history(seeded_origin, tmp_path)
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    residue_dir = tmp_path / "cache-clone" / ".obsidian" / "plugins" / "unremovable-plugin"
    residue_dir.mkdir(parents=True)
    (residue_dir / "main.js").write_text("residue the clean cannot reach\n")
    residue_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)  # readable and traversable, not writable
    (icloud_dir / "note-from-the-device.md").write_text("a real edit, made on the same cycle\n")

    try:
        with caplog.at_level(logging.INFO):
            result = run_cycle(config)
    finally:
        residue_dir.chmod(stat.S_IRWXU)  # tmp_path cleanup

    assert result.drifted == ("note-from-the-device.md",)  # the cycle carried on and did its job
    assert result.tag_advanced is True
    [record] = [r for r in caplog.records if getattr(r, "event", None) == "obsidian_residue_prune_incomplete"]
    assert record.levelno == logging.WARNING
    # git's own reason for the failure, carried through so the line names a directory to go and fix.
    assert any("Permission denied" in line for line in getattr(record, "detail", []))
    # The cost of degrading rather than failing, stated as an assertion: this path is in the clone
    # and not in `HEAD`, so the comparison names it exactly as the prune exists to prevent.
    assert ".obsidian/plugins/unremovable-plugin/main.js" in result.obsidian_baseline_diverged


def test_a_diverged_baseline_is_still_reported_when_origin_has_no_history_to_publish(
    tmp_path: Path,
    seeded_origin: Path,
    icloud_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The comparison runs in step 2 and the cycle can still return before step 6 -- an origin whose
    history this clone can no longer resolve returns early, ahead of every later step. What was
    observed before that point is a fact about the device either way, so it has to survive the early
    return rather than be discarded with the cycle that could not publish."""
    seed_obsidian_baseline_in_history(seeded_origin, tmp_path)
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    (icloud_dir / ".obsidian" / "app.json").write_text('{"legacyEditor": true}\n')

    def _no_origin_history(_runner: GitRunner, *, branch: str) -> str | None:
        return None

    monkeypatch.setattr(cycle_module, "fetch_origin", _no_origin_history)

    with caplog.at_level(logging.INFO):
        result = run_cycle(config)

    assert result.obsidian_baseline_diverged == (".obsidian/app.json",)
    assert result.tag_advanced is False
    assert [r for r in caplog.records if getattr(r, "event", None) == "cycle_no_origin_history"]


def test_no_unseeded_baseline_is_reported_before_the_committer_has_taken_one(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The withheld-seed report has to stay silent while there is nothing to seed. A vault whose
    history carries no `.obsidian/` baseline commit yet is a routine state (docs/DESIGN.md §8a D3),
    and a device is unconfigured there for a reason no gate produced and no operator can clear --
    naming the gate would point at a cause that isn't one and a remedy that doesn't exist."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    (icloud_dir / "pasted.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03")

    with caplog.at_level(logging.INFO):
        result = run_cycle(config)

    assert result.uncaptured == ("pasted.png",)  # the gate really is shut, so no seed ran
    assert result.obsidian_seed_attempted is False
    assert not is_baselined(icloud_dir)
    assert [r for r in caplog.records if getattr(r, "event", None) == "obsidian_baseline_unseeded"] == []


def test_a_healthy_seeded_cycle_never_reports_an_unseeded_baseline(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The direction the withheld-seed report fails in if its completion-marker term is ever
    dropped. Every cycle of a correctly seeded device reaches this report with no seed attempted
    (there is nothing to seed) and a baseline present in the clone, so that term is the only thing
    holding the line back: without it the event fires at `warning` on every cycle a healthy device
    ever runs -- 96 times a day at the installed interval -- telling an operator the device is not
    running the locked baseline while it is. A report that cannot stop firing is the inverse of the
    silence it was added to remove, and costs the same thing in the end."""
    seed_obsidian_baseline_in_history(seeded_origin, tmp_path)
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    with caplog.at_level(logging.INFO):
        result = run_cycle(config)

    assert is_baselined(icloud_dir)
    # The guard's other two terms are inert on this cycle, which is what makes it discriminating:
    # nothing was seeded, and the clone does hold a baseline to seed from.
    assert result.obsidian_seed_attempted is False
    assert [r for r in caplog.records if getattr(r, "event", None) == "obsidian_baseline_unseeded"] == []


def test_a_seed_that_was_attempted_and_withheld_is_not_also_reported_as_unseeded(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The report is keyed on the seed not having been *attempted*, not on the device ending up
    with a baseline: every way a seed can be attempted and not complete emits its own per-cycle
    event already, and this line would restate it while naming a cause that is not the one --
    "this cycle did not seed one", about a cycle that tried. The reachable instance is a clone
    directory the seed walk cannot enumerate, which withholds the completion marker deliberately so
    the next cycle tops up what is missing. Nothing else separates the two events here: the marker
    really is absent, and the clone really does hold a baseline."""
    seed_obsidian_baseline_in_history(seeded_origin, tmp_path)
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    shutil.rmtree(icloud_dir / ".obsidian")
    unreadable = tmp_path / "cache-clone" / ".obsidian" / "plugins" / "unreadable-plugin"
    unreadable.mkdir(parents=True)
    (unreadable / "main.js").write_text("a plugin directory the seed walk cannot enumerate\n")
    unreadable.chmod(stat.S_IWUSR | stat.S_IXUSR)  # traversable, not readable

    try:
        with caplog.at_level(logging.INFO):
            result = run_cycle(config)
    finally:
        unreadable.chmod(stat.S_IRWXU)  # tmp_path cleanup

    assert result.obsidian_seed_attempted is True
    assert not is_baselined(icloud_dir)  # the marker was withheld, so the device really is unseeded
    assert [r for r in caplog.records if getattr(r, "event", None) == "device_baseline_seed_incomplete"]
    assert [r for r in caplog.records if getattr(r, "event", None) == "obsidian_baseline_unseeded"] == []


def test_obsidian_seed_attempted_records_the_attempt_and_not_that_a_baseline_landed(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The flag is set before `seed_baseline` is called and is never revised by what that call
    finds, so it cannot carry the claim that a device now holds a baseline -- only that a cycle got
    as far as trying. The state that separates the two needs nothing exotic: a vault whose history
    carries no `.obsidian/` baseline commit yet (docs/DESIGN.md §8a D3) publishes normally, attempts
    the seed, and skips it. What confirms a baseline actually landed is `device_baseline_seeded`,
    and that is what docs/local-replicator.md sends an operator to look for; this pins the
    difference the runbook now turns on."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)

    with caplog.at_level(logging.INFO):
        result = run_cycle(config)

    assert result.tag_advanced is True  # nothing gated this cycle, so the seed really was reached
    assert result.obsidian_seed_attempted is True
    assert not (icloud_dir / ".obsidian").exists()  # and it placed nothing at all
    assert not is_baselined(icloud_dir)
    events = [getattr(record, "event", None) for record in caplog.records]
    assert "device_baseline_skip_no_source" in events
    assert "device_baseline_seeded" not in events


def test_a_device_holding_its_own_obsidian_but_no_completed_baseline_is_reported_unseeded(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """What this report's predicate is, and it is not the directory: the completion marker.
    Obsidian creates `.obsidian/` itself the first time it opens a vault, so a real directory full
    of the device's own settings with no baseline ever seeded into it is the *ordinary* state
    between a reset and the cycle that publishes -- the sibling test above reaches the same state
    with the directory absent, which is the narrower half of it. The event has to fire on both, and
    its message has to stay true of a device that visibly has plugins."""
    seed_obsidian_baseline_in_history(seeded_origin, tmp_path)
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    shutil.rmtree(icloud_dir / ".obsidian")
    device_own = icloud_dir / ".obsidian"
    (device_own / "plugins" / "device-installed-plugin").mkdir(parents=True)
    (device_own / "app.json").write_text('{"legacyEditor": true}\n')
    (device_own / "plugins" / "device-installed-plugin" / "main.js").write_text("installed on the device\n")
    (icloud_dir / "pasted.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03")

    with caplog.at_level(logging.INFO):
        result = run_cycle(config)

    assert result.uncaptured == ("pasted.png",)  # the gate is shut, so no seed ran
    assert not is_baselined(icloud_dir)
    assert result.obsidian_baseline_diverged == ()  # and the comparison is skipped as bootstrap
    [record] = [r for r in caplog.records if getattr(r, "event", None) == "obsidian_baseline_unseeded"]
    assert record.levelno == logging.WARNING
    # The seed is the only thing in the cycle that writes this directory, and the gate withheld it:
    # the device keeps its own settings, unaltered, rather than being half-configured while the
    # report says it is unconfigured. Not a statement about the excludes -- `publish` does not run
    # on a gated cycle and `overlay` only reads the device side, so nothing here can discriminate
    # them however they are spelled.
    assert (device_own / "app.json").read_text() == '{"legacyEditor": true}\n'


def test_a_device_left_unseeded_by_a_cycle_that_cannot_reach_origin_is_reported_too(
    tmp_path: Path,
    seeded_origin: Path,
    icloud_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other route that withholds the seed, and the one that returns before the publish gate is
    ever consulted. An origin whose branch this clone can no longer resolve leaves a reset device
    exactly as unconfigured as a shut gate does, so the state has to be reported on both routes or
    the report is conditional on which way the cycle happened to end."""
    seed_obsidian_baseline_in_history(seeded_origin, tmp_path)
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    shutil.rmtree(icloud_dir / ".obsidian")

    def _no_origin_history(_runner: GitRunner, *, branch: str) -> str | None:
        return None

    monkeypatch.setattr(cycle_module, "fetch_origin", _no_origin_history)

    with caplog.at_level(logging.INFO):
        result = run_cycle(config)

    assert result.obsidian_seed_attempted is False
    assert not (icloud_dir / ".obsidian").exists()
    [record] = [r for r in caplog.records if getattr(r, "event", None) == "obsidian_baseline_unseeded"]
    assert record.levelno == logging.WARNING


# --- every path list on every log line is bounded, not just the ones that were noticed ----------


def _oversized_path_lists(records: list[logging.LogRecord]) -> list[tuple[str, str, int]]:
    """Every `extra=`-supplied list of paths on `records` that exceeds the sample limit, as
    (event, field, length). A log pipeline rejects an oversized line outright rather than truncating
    it, so an uncapped list does not cost a few paths -- it costs the whole line, count included,
    at the moment the line is most interesting (`logging_config.LOG_PATH_SAMPLE_LIMIT`). Swept
    generically rather than field by field so an emitter added later is covered by construction,
    which is what the constant's comment claims of the codebase."""
    oversized: list[tuple[str, str, int]] = []
    formatter = JsonFormatter()
    for record in records:
        # Measured on the emitted line rather than on the record, because the limit is a property of
        # what reaches the pipeline: `extra=` fields are folded into the JSON object by the formatter,
        # and that object is what a line-size limit is applied to.
        payload: dict[str, object] = json.loads(formatter.format(record))
        for field, value in payload.items():
            if not isinstance(value, list):
                continue
            items = cast(list[object], value)
            if all(isinstance(item, str) for item in items) and len(items) > LOG_PATH_SAMPLE_LIMIT:
                oversized.append((str(payload.get("event", "?")), field, len(items)))
    return oversized


def test_a_gated_cycles_own_path_lists_are_sampled_with_their_counts_beside_them(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The gate's own two lines are the ones that must survive: they are the only place the paths
    that stopped this cycle publishing are named at all (`cycle_complete` carries counts and nothing
    else). A folder of images dropped into the vault -- one drag, and the routine way this gate is
    reached -- puts every one of them on both lines at once."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    (icloud_dir / "attachments").mkdir()
    for index in range(150):
        # A trailing NUL is what makes every one of these binary to git, rather than only the ones
        # whose index byte happens to fall outside printable ASCII.
        (icloud_dir / "attachments" / f"pasted-{index:03d}.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00" + bytes([index]))

    with caplog.at_level(logging.INFO):
        result = run_cycle(config)

    assert len(result.uncaptured) == 150  # the result itself is never sampled
    assert result.tag_advanced is False
    [uncaptured_record] = [r for r in caplog.records if getattr(r, "event", None) == "drift_uncaptured"]
    assert getattr(uncaptured_record, "path_count", None) == 150
    assert len(getattr(uncaptured_record, "paths", [])) == LOG_PATH_SAMPLE_LIMIT
    [gate_record] = [r for r in caplog.records if getattr(r, "event", None) == "cycle_tag_not_advanced"]
    assert getattr(gate_record, "uncaptured_count", None) == 150
    assert len(getattr(gate_record, "uncaptured", [])) == LOG_PATH_SAMPLE_LIMIT
    assert _oversized_path_lists(caplog.records) == []


def test_an_oversized_matching_upstream_set_is_sampled_with_its_full_count_beside_it(
    tmp_path: Path,
    seeded_origin: Path,
    icloud_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The same bound on the line whose size is set by *upstream*, not by the device: a crash in the
    step 6/7 window makes the next cycle read every path that commit touched as drift, so one agent
    commit reorganising the vault is enough to oversize it (ppat/obsidian-tools#36)."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    push_commit(
        seeded_origin,
        tmp_path,
        {f"10-areas/note-{index:03d}.md": f"upstream note {index}\n" for index in range(150)},
        "an agent reorganises the vault",
    )

    _crash_between_publish_and_tag_advance(config, monkeypatch)

    with caplog.at_level(logging.INFO):
        run_cycle(config)

    [record] = [r for r in caplog.records if getattr(r, "event", None) == "drift_matches_upstream"]
    assert getattr(record, "path_count", None) == 150
    assert len(getattr(record, "paths", [])) == LOG_PATH_SAMPLE_LIMIT
    assert _oversized_path_lists(caplog.records) == []


def test_an_oversized_spooled_set_is_sampled_on_the_spool_failure_line_too(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The third field on the same two lines, and the one whose length is set by how much the device
    changed before the disk failed rather than by anything this cycle chose."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    (icloud_dir / "10-areas").mkdir()
    for index in range(150):
        (icloud_dir / "10-areas" / f"note-{index:03d}.md").write_text(f"a device edit {index}\n")
    (icloud_dir / "10-areas" / "zzz-last.md").write_text("the one whose spool write fails\n")

    with caplog.at_level(logging.INFO):
        result = run_cycle(config, spool_writer=_failing_spool_writer("10-areas/zzz-last.md"))

    assert result.spool_write_failed is True
    assert len(result.spooled) == 150
    [record] = [r for r in caplog.records if getattr(r, "event", None) == "cycle_tag_not_advanced"]
    assert getattr(record, "spooled_count", None) == 150
    assert len(getattr(record, "spooled", [])) == LOG_PATH_SAMPLE_LIMIT
    assert _oversized_path_lists(caplog.records) == []


def test_an_oversized_spooled_set_is_sampled_on_the_uncaptured_gate_line_too(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The same field on the *other* of the two gate lines, and the one the generic sweep above
    cannot reach by construction: its scenario drops a folder of images, so every path is
    uncaptured and `spooled` is empty. Reaching this one needs a bulk edit and a pasted image in
    the same cycle -- one ordinary day, not a contrived state -- and the whole point of the line is
    that it is the only place the paths this cycle did manage to spool are named."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    (icloud_dir / "10-areas").mkdir()
    for index in range(150):
        (icloud_dir / "10-areas" / f"note-{index:03d}.md").write_text(f"a device edit {index}\n")
    (icloud_dir / "pasted.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03")

    with caplog.at_level(logging.INFO):
        result = run_cycle(config)

    assert len(result.spooled) == 150
    assert result.uncaptured == ("pasted.png",)
    [record] = [r for r in caplog.records if getattr(r, "event", None) == "cycle_tag_not_advanced"]
    assert getattr(record, "reason", None) == "uncaptured"
    assert getattr(record, "spooled_count", None) == 150
    assert len(getattr(record, "spooled", [])) == LOG_PATH_SAMPLE_LIMIT
    assert _oversized_path_lists(caplog.records) == []


def test_an_oversized_prune_failure_is_sampled_with_its_full_line_count_beside_it(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The list on this line is git's own output rather than a set of paths this cycle built, and
    git emits one warning per entry it could not deal with -- so its length is set by how much
    residue sits under a directory the prune cannot write to, and a plugin directory is exactly the
    shape that holds hundreds of files. The generic sweep does not reach this emitter either: a
    prune failure is not part of any scenario that produces a long list elsewhere."""
    seed_obsidian_baseline_in_history(seeded_origin, tmp_path)
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    residue_dir = tmp_path / "cache-clone" / ".obsidian" / "plugins" / "unremovable-plugin"
    residue_dir.mkdir(parents=True)
    for index in range(150):
        (residue_dir / f"chunk-{index:03d}.js").write_text(f"residue the clean cannot reach {index}\n")
    residue_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)  # readable and traversable, not writable

    try:
        with caplog.at_level(logging.INFO):
            run_cycle(config)
    finally:
        residue_dir.chmod(stat.S_IRWXU)  # tmp_path cleanup

    [record] = [r for r in caplog.records if getattr(r, "event", None) == "obsidian_residue_prune_incomplete"]
    assert getattr(record, "detail_line_count", None) == 150
    assert len(getattr(record, "detail", [])) == LOG_PATH_SAMPLE_LIMIT
    assert _oversized_path_lists(caplog.records) == []


# --- shared exclude list, end to end --------------------------------------------------------------


def test_shared_exclude_list_suppresses_spurious_drift_end_to_end(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    (icloud_dir / ".DS_Store").write_text("finder metadata\n")
    (icloud_dir / "note.md.icloud").write_text("dataless placeholder stub\n")

    result = run_cycle(config)

    assert result.drifted == ()
    assert result.tag_advanced is True  # no false-positive drift ever blocks an ordinary advance
