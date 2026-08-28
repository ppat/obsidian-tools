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
# **`parent=_HYPOTHESIS_DEFAULTS` below is load-bearing, and is a fourth path to the same outcome --
# the one that actually bit.** Not writing `derandomize=True` is not the same as not getting it.
# Hypothesis ships its own built-in profile *named* "ci" (`hypothesis/_settings.py`, bottom:
# `CI = settings(derandomize=True, deadline=None, database=None, print_blob=True, ...)`) and
# auto-loads it whenever `is_in_ci()` sees `CI`/`GITHUB_ACTIONS` in the environment -- which is
# every GitHub Actions run. `settings.__init__` then resolves an omitted argument from
# `parent or settings.default`, and `settings.default` is *the currently loaded profile*, so every
# `register_profile(...)` call in this file without an explicit `parent` silently inherited
# `derandomize=True` and `database=None` from Hypothesis's built-in "ci" profile -- in CI only,
# invisibly on a laptop. Registering a profile named "ci" here also *replaces* the built-in one and,
# because it is the loaded profile, immediately reloads it, so "deep" (registered afterwards)
# inherited the same two values transitively.
#
# The observed consequence, both jobs, in production: `hypothesis profile 'ci' -> database=None,
# ..., derandomize=True` (run 30703556140) and `hypothesis profile 'deep' -> database=None, ...,
# derandomize=True` (run 30703589512). Both workflows' `actions/cache` steps had nothing to save and
# nothing to restore, and no counterexample either job found was ever remembered -- the exact
# failure the cache steps exist to prevent, one layer up. Pinning `parent` to Hypothesis's *default*
# profile (never auto-loaded, never replaced) is what makes the three bullets above true again,
# and is why `print_blob=True` is now stated explicitly on "deep": it too was only ever arriving by
# that accidental inheritance.
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
# `.github/workflows/test.yaml` and `.github/workflows/test-hypothesis-deep.yaml` restore/save that
# same path across runs with `actions/cache`. `database` is deliberately *not* passed explicitly
# below even though `derandomize` now is: naming a path here would re-implement Hypothesis's own
# default resolution (and could drift from it), whereas leaving it unset now genuinely resolves to
# that default, because the parent is pinned to a profile that never had it overridden.
#
# Captured *before* any registration below, since registering "ci" replaces (and, in CI, immediately
# reloads) the profile `settings.default` would otherwise point at.
_HYPOTHESIS_DEFAULTS = settings.get_profile("default")

settings.register_profile("dev", parent=_HYPOTHESIS_DEFAULTS, derandomize=False, max_examples=25, deadline=None)
settings.register_profile(
    "ci",
    parent=_HYPOTHESIS_DEFAULTS,
    derandomize=False,
    max_examples=25,
    deadline=None,
    print_blob=True,  # a failure's minimal example lands directly in the CI log, not just the database
    suppress_health_check=[HealthCheck.too_slow],
)
# Pushed out-of-band (a scheduled workflow, not a PR gate) precisely because it's expensive on
# purpose: an order of magnitude more examples than any PR run should pay for, in exchange for
# reaching further into the interaction/ordering space the doctrine says property tests earn their
# keep in. Findings still land in the same shared database every other profile reads from (above).
#
# Un-derandomized on purpose, and the schedule is the reason: a fixed seed makes every weekly run
# draw the identical 500 examples, so after the first run the schedule can only ever re-find what
# that run already found. Deep search's whole job is to reach inputs the 25-example PR gate cannot;
# a run that cannot explore anything new is a cron job that burns an hour to reprint last week's
# output. Reproducibility is bought by the database instead (see above) -- which derandomizing would
# also have disabled.
settings.register_profile(
    "deep",
    parent=_HYPOTHESIS_DEFAULTS,
    derandomize=False,
    max_examples=500,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)

settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))

# The loaded profile, checked at import time in whatever environment actually runs -- not the
# registered one, and not a simulation of CI. `tests/test_hypothesis_profiles.py` covers all three
# profiles under a synthetic `GITHUB_ACTIONS=true`, which is what makes the regression visible from
# a laptop; this is the other half, and the half that would have caught the original bug: it runs
# inside the real job, on the profile that job actually loaded. Both settings failed silently there
# for weeks, and the only symptom in a green job was one warning line from the `actions/cache` post
# step ("Path(s) specified in the action for caching do(es) not exist"). Raising here converts that
# into a collection error nobody can miss.
_loaded = settings.default
assert _loaded is not None  # always set: `load_profile` above assigns it
# `settings`' own properties are untyped, so strict pyright sees `Unknown | bool` here.
_loaded_derandomize: object = _loaded.derandomize  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
_loaded_database: object = _loaded.database  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
if _loaded_derandomize is not False or _loaded_database is None:
    raise RuntimeError(
        f"Hypothesis profile {settings.get_current_profile_name()!r} resolved to "
        f"derandomize={_loaded_derandomize!r}, database={_loaded_database!r} -- both workflows' "
        "`actions/cache` steps are inert in that state, and no counterexample found by any run is "
        "remembered by the next. See this file's `parent=_HYPOTHESIS_DEFAULTS` comment."
    )


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


def seed_obsidian_baseline_in_history(origin: Path, tmp_path: Path) -> None:
    """Two commits, in this order, because one cannot express it: `git add -A` consults ignore rules
    for untracked paths, so committing the ignore rule alongside the file it names would leave the
    file untracked. The real vault reaches the same state by the committer's own `git add --force`
    (`vault_git/baseline.py`) -- what matters downstream is only that `.obsidian/app.json` ends up
    tracked *and* covered by a tracked `.gitignore`, which is the shape a device actually sees."""
    push_commit(origin, tmp_path, {".obsidian/app.json": '{"legacyEditor": false}\n'}, "obsidian baseline")
    push_commit(origin, tmp_path, {".gitignore": ".obsidian/\n"}, "ignore .obsidian")
