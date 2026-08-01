"""Shared fixtures and helpers for the git-committer test suite.

These tests exercise real local git repositories rather than mocking git itself — the risk this
component exists to manage lives entirely in git's own semantics (skip-worktree, pathspec
exclusions, fast-forward vs. divergence), so mocking git away would prove nothing about them.
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from obsidian_tools.config import ReplicateConfig
from obsidian_tools.vault_git.runner import GitRunner


def run_git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def rev_list_root(git_dir: Path) -> str:
    """The SHA of HEAD's root commit — used to prove a recovered cache didn't re-root history."""
    result = subprocess.run(
        ["git", f"--git-dir={git_dir}", "rev-list", "--max-parents=0", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def commit_count(git_dir: Path) -> int:
    result = subprocess.run(
        ["git", f"--git-dir={git_dir}", "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip())


def make_runner(git_dir: Path, work_tree: Path) -> GitRunner:
    return GitRunner(git_dir, work_tree)


@pytest.fixture
def make_bare_repo(tmp_path: Path) -> Callable[[], Path]:
    counter = iter(range(10_000))

    def _make() -> Path:
        path = tmp_path / f"bare-{next(counter)}.git"
        path.mkdir(parents=True)
        run_git("init", "--bare", "-q", "--initial-branch=main", cwd=path)
        return path

    return _make


@pytest.fixture
def seeded_origin(make_bare_repo: Callable[[], Path], tmp_path: Path) -> Path:
    """A bare 'origin' repo with one pre-existing commit, standing in for the Phase 1 vault seed."""
    origin = make_bare_repo()
    seed_clone = tmp_path / "seed-clone"
    run_git("clone", "-q", str(origin), str(seed_clone), cwd=tmp_path)
    (seed_clone / "00-index.md").write_text("# Home\n")
    run_git("add", "-A", cwd=seed_clone)
    run_git(
        "-c",
        "user.name=seed",
        "-c",
        "user.email=seed@example.invalid",
        "commit",
        "-q",
        "-m",
        "seed",
        cwd=seed_clone,
    )
    run_git("push", "-q", "origin", "main", cwd=seed_clone)
    return origin


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "00-index.md").write_text("# Home\n")
    return vault


@pytest.fixture
def icloud_dir(tmp_path: Path) -> Path:
    """Stands in for the device-facing `iCloud Drive/Obsidian/<Vault Name>` directory that
    local-replicator publishes into — see obsidian_tools/local_replicator/."""
    icloud = tmp_path / "icloud-vault"
    icloud.mkdir()
    return icloud


def replicate_config(tmp_path: Path, origin: Path, icloud: Path) -> ReplicateConfig:
    """Shared between `test_local_replicator_cycle.py` (exercises `run_cycle` directly) and
    `test_commands_replicate.py` (exercises the `replicate` subcommand's `run()` wrapper) — both
    need the same real git/rsync setup, just invoked at different layers."""
    return ReplicateConfig(
        cache_clone_dir=str(tmp_path / "cache-clone"),
        icloud_vault_dir=str(icloud),
        branch="main",
        origin_url=str(origin),
        ssh_key_path=str(tmp_path / "unused-key"),
        ssh_known_hosts_path=str(tmp_path / "unused-known-hosts"),
        spool_dir=str(tmp_path / "spool"),
    )


def push_commit(origin: Path, tmp_path: Path, files: dict[str, str], message: str) -> None:
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
