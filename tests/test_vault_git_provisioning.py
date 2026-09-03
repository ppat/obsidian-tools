"""Tests for obsidian_tools/vault_git/provisioning.py.

The critical property under test: the git-dir is a derivable cache, not durable state. A lost or
re-provisioned cache must recover by fetching origin's existing history, never by re-initialising a
fresh, divergent root commit — see the module's own docstring and ppat/obsidian-tools#3 for the
incident this guards against: a re-rooted cache would fail to push as non-fast-forward, while the
vault volume itself looked perfectly fine.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import commit_count, make_runner, rev_list_root, run_git

from obsidian_tools.vault_git.commit import PushResult, create_commit, has_staged_changes, push_all, stage_all
from obsidian_tools.vault_git.provisioning import GitDivergenceError, provision_repository
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


def _cycle(runner: GitRunner) -> list[PushResult]:
    stage_all(runner)
    if has_staged_changes(runner):
        create_commit(runner, cycle_time=datetime.now(UTC))
    return push_all(runner, branch="main")


def _add_vault_file(vault_dir: Path, relative: str, content: str) -> None:
    path = vault_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_provisioning_is_idempotent(tmp_path: Path, seeded_origin: Path, vault_dir: Path) -> None:
    git_dir = tmp_path / "git-dir"

    _provision(git_dir, vault_dir, origin_url=str(seeded_origin))
    _provision(git_dir, vault_dir, origin_url=str(seeded_origin))

    assert commit_count(git_dir) == 1  # only origin's pre-existing seed commit; provisioning alone commits nothing


def test_first_run_against_an_empty_origin_roots_cleanly(
    tmp_path: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    origin = make_bare_repo()  # genuinely empty, no commits at all
    git_dir = tmp_path / "git-dir"

    runner = _provision(git_dir, vault_dir, origin_url=str(origin))
    results = _cycle(runner)

    assert all(result.ok for result in results)
    assert commit_count(origin) == 1


def test_recovers_from_wiped_cache_by_fetching_not_reinitialising(
    tmp_path: Path, seeded_origin: Path, vault_dir: Path
) -> None:
    """The scenario the PVC-as-cache correction exists to prevent."""
    git_dir = tmp_path / "git-dir"
    original_root_sha = rev_list_root(seeded_origin)

    # Cycle 1: a normal run against a freshly-provisioned cache.
    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin))
    _add_vault_file(vault_dir, "10-areas/homelab.md", "# Homelab\n")
    results_1 = _cycle(runner)
    assert all(result.ok for result in results_1)
    assert commit_count(git_dir) == 2
    assert rev_list_root(git_dir) == original_root_sha

    # Simulate a lost/re-provisioned PVC: the git-dir cache vanishes entirely. The vault volume's
    # content is untouched, and origin still holds everything cycle 1 pushed.
    shutil.rmtree(git_dir)
    _add_vault_file(vault_dir, "10-areas/homelab2.md", "# Homelab 2\n")

    # Cycle 2: provisioning must recover by fetching origin's history, not by re-rooting.
    runner_2 = _provision(git_dir, vault_dir, origin_url=str(seeded_origin))
    results_2 = _cycle(runner_2)

    # A genuinely re-rooted cache would be rejected here as non-fast-forward. Succeeding is part
    # of what this test proves, not an incidental assertion.
    assert all(result.ok for result in results_2)

    assert rev_list_root(git_dir) == original_root_sha  # single lineage, never re-rooted
    assert commit_count(git_dir) == 3
    assert commit_count(seeded_origin) == 3  # origin received both cycles' commits, linearly


def test_a_stuck_unpushed_commit_is_not_discarded_on_the_next_provision(
    tmp_path: Path, seeded_origin: Path, vault_dir: Path
) -> None:
    """A previous run may have committed locally and then failed to push. The next provisioning
    pass must leave that local-only commit alone (never rewind it), so the next push catches up."""
    git_dir = tmp_path / "git-dir"

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin))
    _add_vault_file(vault_dir, "10-areas/homelab.md", "# Homelab\n")
    stage_all(runner)
    create_commit(runner, cycle_time=datetime.now(UTC))  # committed locally, deliberately not pushed

    local_sha_before = runner.rev_parse_or_none("refs/heads/main")

    runner_2 = _provision(git_dir, vault_dir, origin_url=str(seeded_origin))

    assert runner_2.rev_parse_or_none("refs/heads/main") == local_sha_before  # not rewound to origin
    assert commit_count(git_dir) == 2
    assert commit_count(seeded_origin) == 1  # origin never received it


def test_diverged_local_and_origin_history_raises(tmp_path: Path, seeded_origin: Path, vault_dir: Path) -> None:
    git_dir = tmp_path / "git-dir"

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin))
    _add_vault_file(vault_dir, "10-areas/homelab.md", "# Homelab\n")
    _cycle(runner)  # local now matches origin (pushed)

    # Something pushes directly to origin, outside this committer's control.
    other_clone = tmp_path / "other-clone"
    run_git("clone", "-q", str(seeded_origin), str(other_clone), cwd=tmp_path)
    (other_clone / "rogue.md").write_text("unexpected\n")
    run_git("add", "-A", cwd=other_clone)
    run_git(
        "-c",
        "user.name=rogue",
        "-c",
        "user.email=rogue@example.invalid",
        "commit",
        "-q",
        "-m",
        "rogue",
        cwd=other_clone,
    )
    run_git("push", "-q", "origin", "main", cwd=other_clone)

    # Meanwhile the committer separately takes its own local commit, never pushed.
    _add_vault_file(vault_dir, "10-areas/homelab2.md", "# Homelab 2\n")
    stage_all(runner)
    create_commit(runner, cycle_time=datetime.now(UTC))

    with pytest.raises(GitDivergenceError):
        _provision(git_dir, vault_dir, origin_url=str(seeded_origin))
