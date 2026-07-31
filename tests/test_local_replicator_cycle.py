"""Tests for obsidian_tools/local_replicator/cycle.py — the full five-step cycle.

Real local git repos (bare "origin" plus a normal push-clone to seed commits) and a real rsync
against real temp directories throughout, exactly like the git committer's own test suite — the
risk here lives in git's and rsync's actual semantics, not in anything worth mocking.

Covers every scenario named in the brief for this component:
- the comparison runs against the pre-pull baseline, so an upstream change and a device edit in
  the same cycle are attributed correctly (this is the whole reason for the step ordering);
- a capture failure is injected (never merely omitted) and the publish skips exactly that path;
- LAST_CHECKOUT does not advance when a publish was skipped, and the next cycle retries;
- `--delete` never removes a path whose capture failed;
- `.obsidian/` is copied once, then left alone even when the device customises it, and a partial
  first copy is not mistaken for a complete one (see test_local_replicator_device_baseline.py for
  that property in isolation);
- the shared exclude list suppresses spurious drift end-to-end, not just at the rsync layer.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from conftest import run_git

from obsidian_tools.config import ReplicateConfig
from obsidian_tools.local_replicator.capture import CaptureError, CaptureSink
from obsidian_tools.local_replicator.cycle import run_cycle
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


def _recording_sink(record: list[tuple[str, bytes | None]]) -> CaptureSink:
    def sink(path: str, content: bytes | None) -> None:
        record.append((path, content))

    return sink


def _tag_sha(tmp_path: Path) -> str | None:
    runner = GitRunner(tmp_path / "cache-clone" / ".git", tmp_path / "cache-clone")
    return read_last_checkout(runner)


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

    captured: list[tuple[str, bytes | None]] = []
    result = run_cycle(config, capture_sink=_recording_sink(captured))

    # Only the human-edited path is drift. The upstream-changed path is not — it was never
    # different from the pre-pull baseline at compare time.
    assert result.drifted == ("10-areas/other.md",)
    assert captured == [("10-areas/other.md", b"typed on the phone\n")]
    # The upstream change still reaches the device, via the pull+publish, independent of drift
    # attribution.
    assert (icloud_dir / "00-index.md").read_text() == "# Home (agent update)\n"


def test_capture_failure_skips_publish_for_that_path_and_retries_next_cycle(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    config = _config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)  # cycle 1: bootstrap
    (icloud_dir / "00-index.md").write_text("human edit, capture about to fail\n")

    def failing_sink(path: str, content: bytes | None) -> None:
        del content
        if path == "00-index.md":
            raise CaptureError("simulated capture failure — injected, not a real I/O error")

    result = run_cycle(config, capture_sink=failing_sink)

    assert result.capture_failed == ("00-index.md",)
    # The publish must have skipped this path entirely: neither overwritten with the authoritative
    # content nor reset to anything else.
    assert (icloud_dir / "00-index.md").read_text() == "human edit, capture about to fail\n"

    # Next cycle, with capture succeeding (the default stub): the same drift is detected again
    # (never lost) and this time resolves.
    result_retry = run_cycle(config)

    assert result_retry.drifted == ("00-index.md",)
    assert result_retry.tag_advanced is True
    assert (icloud_dir / "00-index.md").read_text() == "# Home\n"


def test_tag_does_not_advance_when_a_publish_was_skipped(tmp_path: Path, seeded_origin: Path, icloud_dir: Path) -> None:
    config = _config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    checkout_after_bootstrap = _tag_sha(tmp_path)
    _push_commit(seeded_origin, tmp_path, {"00-index.md": "# Home (agent update)\n"}, "agent update")
    (icloud_dir / "00-index.md").write_text("uncaptured human edit\n")

    def always_fails(path: str, content: bytes | None) -> None:
        del path, content
        raise CaptureError("simulated")

    result = run_cycle(config, capture_sink=always_fails)

    assert result.tag_advanced is False
    assert _tag_sha(tmp_path) == checkout_after_bootstrap  # unmoved, not partially advanced

    # And once capture succeeds, it does advance — proving this isn't just permanently stuck.
    result_retry = run_cycle(config)
    assert result_retry.tag_advanced is True
    assert _tag_sha(tmp_path) != checkout_after_bootstrap


def test_delete_does_not_remove_a_path_whose_capture_failed(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    config = _config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    (icloud_dir / "typed-on-phone.md").write_text("a note created directly on the device\n")

    def always_fails(path: str, content: bytes | None) -> None:
        del path, content
        raise CaptureError("simulated")

    result = run_cycle(config, capture_sink=always_fails)

    assert "typed-on-phone.md" in result.capture_failed
    assert (icloud_dir / "typed-on-phone.md").exists()  # protected: not deleted by `--delete`

    # Once capture succeeds, Phase 2's stub discards the bytes (docs/DESIGN.md §2 item 10), so the
    # file — never part of the authoritative tree — is removed on the next successful cycle. This
    # is documented Phase 2 behaviour, not a bug: the capture attempt is real, its destination
    # isn't, yet.
    run_cycle(config)
    assert not (icloud_dir / "typed-on-phone.md").exists()


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


def test_shared_exclude_list_suppresses_spurious_drift_end_to_end(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    config = _config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    (icloud_dir / ".DS_Store").write_text("finder metadata\n")
    (icloud_dir / ".obsidian").mkdir()
    (icloud_dir / ".obsidian" / "workspace.json").write_text('{"instance": "device"}\n')
    (icloud_dir / "note.md.icloud").write_text("dataless placeholder stub\n")

    result = run_cycle(config)

    assert result.drifted == ()
    assert result.tag_advanced is True  # no false-positive drift ever blocks an ordinary advance
