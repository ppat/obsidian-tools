"""Shared fixtures and helpers for the git-committer test suite.

These tests exercise real local git repositories rather than mocking git itself — the risk this
component exists to manage lives entirely in git's own semantics (skip-worktree, pathspec
exclusions, fast-forward vs. divergence), so mocking git away would prove nothing about them.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings

from obsidian_tools.config import ReplicateConfig
from obsidian_tools.vault_git.runner import GitRunner

# --- Hypothesis profiles ---------------------------------------------------------------------
#
# Three CI-discipline mitigations, all agreed rather than optional (see
# /home/coder/.claude/tmp/obsidian-brain/notes/decision-testing-strategy-for-obsidian-tools.md):
# a run must be reproducible so a property finding a real bug at random on an unrelated PR doesn't
# read as CI flakiness; a case discovered once must be remembered rather than re-earned by luck on
# every later run; and deep search belongs out-of-band, not on every PR's critical path.
#
# **Deliberately NOT `derandomize=True`, NOT `@seed(...)`, NOT `--hypothesis-seed`.** All three were
# tried, in that order, and all three turned out to disable exactly the database persistence the
# second mitigation needs -- not documented as a shared consequence anywhere obvious, so this is
# recorded here rather than left to be rediscovered:
#   - `derandomize=True` -- `Settings.__init__` (hypothesis/_settings.py) hard-codes "derandomize=True
#     implies database=None": passing both raises `InvalidArgument`, and omitting `database` just
#     sets it to `None` silently.
#   - `@seed(N)` -- `hypothesis.core.seed()`'s own `accept()` closure does the identical thing by
#     hand: `test._hypothesis_internal_use_settings = Settings(current_settings, database=None)`.
#   - `--hypothesis-seed` (equivalently `core.global_force_seed`) -- a third, independent path to
#     the same outcome: `core.py`'s `run_engine` computes `database_key = None` whenever
#     `global_force_seed is not None`, and `ConjectureRunner.save_choices` silently no-ops whenever
#     its `database_key` is `None` (`internal/conjecture/engine.py`) -- confirmed by tracing
#     `save_choices` directly, since nothing in the settings repr surfaces this path the way the
#     first two do.
# All three were verified empirically, not just read off the source: with any one of them active,
# a real, reproduced failure on this property (see the crash-residue property in
# tests/test_local_replicator_cycle.py) never created `.hypothesis/examples/` at all; with none of
# them active, it did, every time. Fixing the *generation seed* and persisting *found examples* are
# mutually exclusive levers in this Hypothesis version -- not a bug, evidently deliberate (three
# independent code paths agree), but exactly the kind of composition the doctrine warns against
# assuming rather than checking.
#
# Given that, "an unrelated PR does not go red on a fresh random draw" is bought by the database
# instead of by fixing the seed: once any run, with any seed, finds a failing example, Hypothesis's
# own "reuse" phase replays it first on every later run regardless of that run's own seed -- so a
# bug found once reproduces on every subsequent run deterministically, without needing the whole
# run's generation pinned. This is Hypothesis's own documented shape for CI, not an improvisation
# here: `hypothesis.database`'s own `MultiplexedDatabase` docstring gives
# `settings.load_profile("ci" if os.environ.get("CI") else "dev")` with the *database*, not the
# seed, as the thing that differs between CI and a laptop. `max_examples` stays modest in the "ci"
# profile below for a different, complementary reason: it bounds how much of the input space any
# one run explores fresh, which is what actually limits how often a genuinely new (not-yet-in-the-
# database) failure can surface for the first time on an unrelated PR.
#
# The example database itself is left at its default (`.hypothesis/examples`, relative to the
# working directory `uv run pytest` runs from -- `current/` in every workflow that uses
# `setup-repository-tools`, per that action's own `path: current` checkout step) in every profile
# below, including "deep": a case the scheduled deep run turns up is exactly the kind of thing an
# ordinary PR run should get for free from the database, rather than needing its own rediscovery.
# `.github/workflows/test.yaml` restores/saves that same path across runs with `actions/cache` --
# see the comment there for how the restore was proven to actually take effect, not just configured.
settings.register_profile("dev", max_examples=25, deadline=None)
settings.register_profile(
    "ci",
    max_examples=25,
    deadline=None,
    print_blob=True,  # a failure's minimal example lands directly in the CI log, not just the database
    suppress_health_check=[HealthCheck.too_slow],
)
# Pushed out-of-band (a scheduled workflow, not a PR gate) precisely because it's expensive on
# purpose: an order of magnitude more examples than any PR run should pay for, in exchange for
# reaching further into the interaction/ordering space the doctrine says property tests earn their
# keep in. Findings still land in the same shared database every other profile reads from (above).
settings.register_profile("deep", max_examples=500, deadline=None, suppress_health_check=[HealthCheck.too_slow])

settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


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


def push_commit(
    origin: Path,
    tmp_path: Path,
    files: dict[str, str],
    message: str,
    *,
    binary_files: dict[str, bytes] | None = None,
) -> None:
    """Simulates the git committer taking and pushing another cycle's commit.

    `binary_files` is how a test gets a *tracked* binary into history -- the state in which the
    capture gate's failure modes are reachable at all, since an untracked one can be made to
    disappear by deleting it, and a tracked one cannot."""
    clone = tmp_path / f"push-clone-{uuid.uuid4().hex}"
    run_git("clone", "-q", str(origin), str(clone), cwd=tmp_path)
    for relative, content in files.items():
        path = clone / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for relative, blob in (binary_files or {}).items():
        path = clone / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
    run_git("add", "-A", cwd=clone)
    run_git("-c", "user.name=x", "-c", "user.email=x@example.invalid", "commit", "-q", "-m", message, cwd=clone)
    run_git("push", "-q", "origin", "main", cwd=clone)
