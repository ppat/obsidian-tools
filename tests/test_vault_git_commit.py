"""Tests for obsidian_tools/vault_git/commit.py: staging, committing, and independent two-remote pushing."""

from __future__ import annotations

from collections.abc import Callable
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


def _provision(git_dir: Path, work_tree: Path, *, origin_url: str, nas_url: str) -> GitRunner:
    runner = make_runner(git_dir, work_tree)
    provision_repository(
        runner,
        branch="main",
        author_name="test-committer",
        author_email="test-committer@example.invalid",
        origin_url=origin_url,
        nas_url=nas_url,
    )
    return runner


def test_nothing_staged_produces_no_commit(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    before = commit_count(seeded_origin)

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    stage_all(runner)  # vault_dir already matches what provisioning just synced from origin

    assert not has_staged_changes(runner)
    assert commit_count(git_dir) == before


def test_commit_message_names_the_cycle_and_change_counts(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
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


def test_check_for_mass_deletion_trips_on_deletion_fraction(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
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
        check_for_mass_deletion(runner, vault_dir)


def test_check_for_mass_deletion_trips_on_zero_markdown_even_below_the_fraction_threshold(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    """The fraction tripwire alone wouldn't catch this: losing the vault's only markdown note among
    many non-markdown attachments is a small fraction of tracked paths, but still means the vault
    that exists to hold notes now holds none."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    (vault_dir / "_attachments").mkdir()
    for i in range(9):
        (vault_dir / "_attachments" / f"file-{i}.bin").write_bytes(b"x")
    stage_all(runner)
    create_commit(runner, cycle_time=datetime.now(UTC))
    # HEAD now tracks 00-index.md (the vault's only markdown note) plus 9 non-markdown attachments.

    (vault_dir / "00-index.md").unlink()
    stage_all(runner)  # only 1/10 tracked paths deleted

    with pytest.raises(MassDeletionError):
        check_for_mass_deletion(runner, vault_dir)


def test_check_for_mass_deletion_allows_an_ordinary_partial_deletion(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    """An ordinary edit -- deleting one note out of several, the shape a human archiving or
    correcting a slug actually produces -- must not be refused."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    (vault_dir / "10-areas").mkdir()
    for i in range(5):
        (vault_dir / "10-areas" / f"note-{i}.md").write_text(f"# Note {i}\n")
    stage_all(runner)
    create_commit(runner, cycle_time=datetime.now(UTC))
    # HEAD now tracks 00-index.md plus 5 notes: 6 paths total.

    (vault_dir / "10-areas" / "note-0.md").unlink()
    stage_all(runner)  # 1/6 tracked paths deleted, and 5 markdown notes remain

    check_for_mass_deletion(runner, vault_dir)  # must not raise


def test_push_failure_on_one_remote_does_not_block_the_other(
    tmp_path: Path, seeded_origin: Path, vault_dir: Path
) -> None:
    git_dir = tmp_path / "git-dir"
    broken_nas_url = str(tmp_path / "does-not-exist.git")  # a genuinely unreachable remote, no mocking

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=broken_nas_url)
    (vault_dir / "10-areas").mkdir()
    (vault_dir / "10-areas" / "note.md").write_text("# Note\n")
    stage_all(runner)
    create_commit(runner, cycle_time=datetime.now(UTC))

    results = push_all(runner, branch="main")
    by_remote = {result.remote: result for result in results}

    assert by_remote["origin"].ok is True  # the healthy remote still received the push
    assert by_remote["nas"].ok is False  # the broken remote's failure didn't stop the loop
    assert by_remote["nas"].error is not None
    assert commit_count(seeded_origin) == 2
