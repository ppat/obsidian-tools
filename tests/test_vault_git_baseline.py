"""Tests for obsidian_tools/vault_git/baseline.py.

The core property under test: `.gitignore` cannot make the `.obsidian/` freeze happen — only
`git update-index --skip-worktree`, reapplied every run, does. These tests exist specifically to
catch a regression back to `.gitignore`-only behaviour, which would pass every other test in this
suite while silently failing the one thing that matters (see the module's own docstring and
ppat/obsidian-tools#3, "empirically, not reasoned out in advance").
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from conftest import make_runner

from obsidian_tools.vault_git.baseline import ensure_obsidian_baseline
from obsidian_tools.vault_git.commit import create_commit, has_staged_changes, push_all, stage_all
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


def _write_obsidian_dir(vault_dir: Path) -> None:
    obsidian = vault_dir / ".obsidian"
    obsidian.mkdir()
    (obsidian / "app.json").write_text('{"legacyEditor": false}\n')
    (obsidian / "workspace.json").write_text('{"instance": "a"}\n')
    (obsidian / "workspaces.json").write_text('{"instance": "a"}\n')


def test_pathspec_excludes_workspace_state_files(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    ensure_obsidian_baseline(runner, vault_dir)

    staged = runner.run(["diff", "--cached", "--name-only"]).stdout.splitlines()
    assert ".obsidian/app.json" in staged
    assert ".obsidian/workspace.json" not in staged
    assert ".obsidian/workspaces.json" not in staged


def test_baseline_taken_once_then_frozen_even_after_a_later_edit(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    """The single most important behaviour: once the baseline is committed, a later change to a
    baselined file must NOT be re-staged by an ordinary `git add -A` — the property `.gitignore`
    was shown to be unable to provide."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)

    # Cycle 1: baseline captured and committed.
    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    took_baseline = ensure_obsidian_baseline(runner, vault_dir)
    stage_all(runner)
    assert took_baseline
    assert has_staged_changes(runner)
    create_commit(runner, cycle_time=datetime.now(UTC))

    committed_tree = set(runner.list_tree_paths("HEAD", ".obsidian"))
    assert ".obsidian/app.json" in committed_tree
    assert ".obsidian/workspace.json" not in committed_tree

    # A setting is changed at the cluster GUI: app.json is edited in place on the (simulated) volume.
    (vault_dir / ".obsidian" / "app.json").write_text('{"legacyEditor": true}\n')

    # Cycle 2: full provisioning re-run, as if against a fresh pod, then the ordinary staging path.
    runner_2 = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    took_baseline_again = ensure_obsidian_baseline(runner_2, vault_dir)
    stage_all(runner_2)

    assert not took_baseline_again
    assert not has_staged_changes(runner_2), (
        "the edited .obsidian/app.json was re-staged — the skip-worktree freeze did not survive, "
        "which is exactly the failure .gitignore alone produces"
    )


def test_baseline_survives_a_lost_git_dir_cache(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    """Skip-worktree bits are index-local and do not survive a wiped git-dir cache even though the
    baseline commit itself (fetched fresh from origin) does — provisioning must reapply the bits
    from HEAD's tree, and must NOT retake the baseline commit a second time."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    ensure_obsidian_baseline(runner, vault_dir)
    stage_all(runner)
    create_commit(runner, cycle_time=datetime.now(UTC))
    push_all(runner, branch="main")

    shutil.rmtree(git_dir)
    (vault_dir / ".obsidian" / "app.json").write_text('{"legacyEditor": true}\n')

    runner_2 = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    took_baseline_again = ensure_obsidian_baseline(runner_2, vault_dir)
    stage_all(runner_2)

    assert took_baseline_again is False, "the baseline must not be recaptured just because the cache was rebuilt"
    assert not has_staged_changes(runner_2)


def test_no_baseline_taken_when_obsidian_dir_absent(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    took_baseline = ensure_obsidian_baseline(runner, vault_dir)

    assert took_baseline is False
    assert not has_staged_changes(runner)
