"""Tests for obsidian_tools/vault_git/commit.py: staging, committing, and independent per-remote pushing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import commit_count, make_runner

from obsidian_tools.vault_git.commit import (
    MassDeletionError,
    check_for_mass_deletion,
    create_commit,
    has_staged_changes,
    push_all,
    stage_all,
)
from obsidian_tools.vault_git.provisioning import provision_repository
from obsidian_tools.vault_git.runner import GitRunner


def _provision(git_dir: Path, work_tree: Path, *, origin_url: str) -> GitRunner:
    runner = make_runner(git_dir, work_tree)
    provision_repository(
        runner,
        branch="main",
        author_name="test-committer",
        author_email="test-committer@example.invalid",
        origin_url=origin_url,
    )
    return runner


def test_nothing_staged_produces_no_commit(tmp_path: Path, seeded_origin: Path, vault_dir: Path) -> None:
    git_dir = tmp_path / "git-dir"
    before = commit_count(seeded_origin)

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin))
    stage_all(runner)  # vault_dir already matches what provisioning just synced from origin

    assert not has_staged_changes(runner)
    assert commit_count(git_dir) == before


def test_commit_message_names_the_cycle_and_change_counts(tmp_path: Path, seeded_origin: Path, vault_dir: Path) -> None:
    git_dir = tmp_path / "git-dir"

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin))
    (vault_dir / "10-areas").mkdir()
    (vault_dir / "10-areas" / "note.md").write_text("# Note\n")
    stage_all(runner)
    cycle_time = datetime(2026, 7, 31, 4, 0, 0, tzinfo=UTC)
    create_commit(runner, cycle_time=cycle_time)

    message = runner.run(["log", "-1", "--format=%B"]).stdout
    assert "2026-07-31T04:00:00Z" in message
    assert "1 added" in message
    # Not Conventional Commits: no `feat:`/`fix:`/`chore:` style prefix in this repo's history.
    assert not message.startswith(("feat", "fix", "chore", "docs", "refactor"))


def test_commit_message_survives_a_quotepath_hostile_path(tmp_path: Path, seeded_origin: Path, vault_dir: Path) -> None:
    """`build_commit_message` parses `git diff --cached --name-status`, one of the sites this
    codebase must never read in git's line-oriented, quoted form: `core.quotePath` defaults to
    true, so a non-ASCII path is C-quoted (surrounding quotes included), and a path containing a
    literal newline would otherwise land mid-record. `-z` is what keeps this from ever producing a
    corrupted or wrong-count commit message."""
    git_dir = tmp_path / "git-dir"

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin))
    (vault_dir / "10-areas").mkdir()
    (vault_dir / "10-areas" / "日本語.md").write_text("# Note\n")
    stage_all(runner)
    cycle_time = datetime(2026, 7, 31, 4, 0, 0, tzinfo=UTC)

    create_commit(runner, cycle_time=cycle_time)

    message = runner.run(["log", "-1", "--format=%B"]).stdout
    assert "1 added" in message
    assert "10-areas/日本語.md" in message
    assert '"' not in message  # no leftover C-quoting artifacts


def test_check_for_mass_deletion_trips_on_deletion_fraction(
    tmp_path: Path, seeded_origin: Path, vault_dir: Path
) -> None:
    git_dir = tmp_path / "git-dir"
    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin))
    (vault_dir / "10-areas").mkdir()
    for i in range(3):
        (vault_dir / "10-areas" / f"note-{i}.md").write_text(f"# Note {i}\n")
    stage_all(runner)
    create_commit(runner, cycle_time=datetime.now(UTC))
    # HEAD now tracks 00-index.md plus the 3 notes just added: 4 paths total.

    for note in (vault_dir / "10-areas").glob("*.md"):
        note.unlink()
    stage_all(runner)  # 3/4 tracked paths staged as deletions -- well over the 50% threshold

    with pytest.raises(MassDeletionError):
        check_for_mass_deletion(runner)


def test_check_for_mass_deletion_trips_on_zero_markdown_even_below_the_fraction_threshold(
    tmp_path: Path, seeded_origin: Path, vault_dir: Path
) -> None:
    """The fraction tripwire alone wouldn't catch this: losing the vault's only markdown note among
    many non-markdown attachments is a small fraction of tracked paths, but still means the vault
    that exists to hold notes now holds none."""
    git_dir = tmp_path / "git-dir"
    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin))
    (vault_dir / "_attachments").mkdir()
    for i in range(9):
        (vault_dir / "_attachments" / f"file-{i}.bin").write_bytes(b"x")
    stage_all(runner)
    create_commit(runner, cycle_time=datetime.now(UTC))
    # HEAD now tracks 00-index.md (the vault's only markdown note) plus 9 non-markdown attachments.

    (vault_dir / "00-index.md").unlink()
    stage_all(runner)  # only 1/10 tracked paths deleted

    with pytest.raises(MassDeletionError):
        check_for_mass_deletion(runner)


def test_unreadable_directories_do_not_false_trip_the_zero_markdown_tripwire(
    tmp_path: Path, seeded_origin: Path, vault_dir: Path
) -> None:
    """An unreadable directory must not manufacture a false "zero markdown" mass-deletion alarm.

    `Path.rglob` silently swallows `PermissionError`, so a filesystem-walk-based after-count sees
    zero notes under a directory it cannot list, even though those files are completely untouched
    and still tracked -- confusing "I can't see it" with "it's gone". `git add -A` compounds this:
    it succeeds against a directory it cannot open, leaving that directory's existing index entries
    exactly as they were rather than failing the add. This reproduces both halves at once: one
    ordinary, legitimate deletion (00-index.md, as if promoted elsewhere) plus a coincidental
    permission glitch on two unrelated directories whose notes are never actually touched. Deriving
    the after-state from git's own staged tree (`git write-tree`), rather than a filesystem walk,
    is what keeps that glitch from being amplified into a fabricated total-loss alarm."""
    git_dir = tmp_path / "git-dir"
    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin))
    for name in ("locked-a", "locked-b"):
        d = vault_dir / name
        d.mkdir()
        (d / "note.md").write_text(f"# {name}\n")
    stage_all(runner)
    create_commit(runner, cycle_time=datetime.now(UTC))
    # HEAD now tracks 00-index.md, locked-a/note.md and locked-b/note.md: 3 markdown notes.

    (vault_dir / "00-index.md").unlink()  # one ordinary, legitimate deletion
    for name in ("locked-a", "locked-b"):
        (vault_dir / name).chmod(0)  # unrelated permission glitch; content untouched, still tracked
    try:
        stage_all(runner)
        check_for_mass_deletion(runner)  # must not raise: only one note was actually deleted
    finally:
        for name in ("locked-a", "locked-b"):
            (vault_dir / name).chmod(0o750)


def test_max_deletion_fraction_at_or_above_one_disables_both_tripwires(
    tmp_path: Path, seeded_origin: Path, vault_dir: Path
) -> None:
    """The operator escape hatch for a genuine archive purge: `max_deletion_fraction` used to be a
    parameter no call site passed and no env var exposed, so a real purge had no way past this
    guard short of editing source. `>=1.0` must actually disable both tripwires, not just raise the
    fraction cap partway -- a full purge trips the zero-markdown tripwire regardless of fraction."""
    git_dir = tmp_path / "git-dir"
    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin))
    (vault_dir / "10-areas").mkdir()
    for i in range(3):
        (vault_dir / "10-areas" / f"note-{i}.md").write_text(f"# Note {i}\n")
    stage_all(runner)
    create_commit(runner, cycle_time=datetime.now(UTC))
    # HEAD now tracks 00-index.md plus 3 notes: 4 markdown paths total.

    (vault_dir / "00-index.md").unlink()
    for note in (vault_dir / "10-areas").glob("*.md"):
        note.unlink()
    stage_all(runner)  # every tracked markdown note deleted -- would trip both tripwires

    check_for_mass_deletion(runner, max_deletion_fraction=1.0)  # must not raise


def test_check_for_mass_deletion_allows_an_ordinary_partial_deletion(
    tmp_path: Path, seeded_origin: Path, vault_dir: Path
) -> None:
    """An ordinary edit -- deleting one note out of several, the shape a human archiving or
    correcting a slug actually produces -- must not be refused."""
    git_dir = tmp_path / "git-dir"
    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin))
    (vault_dir / "10-areas").mkdir()
    for i in range(5):
        (vault_dir / "10-areas" / f"note-{i}.md").write_text(f"# Note {i}\n")
    stage_all(runner)
    create_commit(runner, cycle_time=datetime.now(UTC))
    # HEAD now tracks 00-index.md plus 5 notes: 6 paths total.

    (vault_dir / "10-areas" / "note-0.md").unlink()
    stage_all(runner)  # 1/6 tracked paths deleted, and 5 markdown notes remain

    check_for_mass_deletion(runner)  # must not raise


def test_push_failure_on_one_remote_does_not_block_the_other(
    tmp_path: Path, seeded_origin: Path, vault_dir: Path
) -> None:
    """`push_all` takes the remote names to push to and must treat each independently: one remote
    failing has to be recorded as its own `PushResult` and leave the rest of the tuple attempted,
    not raise out of the loop. The deployed committer passes a single remote, so the second one
    here is added directly rather than through `provision_repository` -- this is a test of
    `push_all`'s contract over the tuple it is given, which is what `summarize_push_results`
    consumes, not of any remote the committer is configured with."""
    git_dir = tmp_path / "git-dir"

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin))
    broken_url = str(tmp_path / "does-not-exist.git")  # a genuinely unreachable remote, no mocking
    runner.run(["remote", "add", "broken", broken_url])
    (vault_dir / "10-areas").mkdir()
    (vault_dir / "10-areas" / "note.md").write_text("# Note\n")
    stage_all(runner)
    create_commit(runner, cycle_time=datetime.now(UTC))

    results = push_all(runner, branch="main", remotes=("origin", "broken"))
    by_remote = {result.remote: result for result in results}

    assert by_remote["origin"].ok is True  # the healthy remote still received the push
    assert by_remote["broken"].ok is False  # the broken remote's failure didn't stop the loop
    assert by_remote["broken"].error is not None
    assert commit_count(seeded_origin) == 2
