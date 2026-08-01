"""Guards the two Hypothesis settings this repo's CI machinery is built on, *in the environment
where they are actually used*.

Both `test.yaml` and `test-hypothesis-deep.yaml` attach an `actions/cache` step to
`.hypothesis/examples` so a counterexample found once is replayed on every later run instead of
being re-earned by luck. That only works if the loaded profile has a database at all, and the whole
point of not derandomizing (tests/conftest.py's long comment) is that derandomizing would take the
database away. Neither fact is observable from a laptop run: Hypothesis auto-loads its own built-in
"ci" profile -- `derandomize=True, database=None` -- whenever `CI`/`GITHUB_ACTIONS` is set, and an
omitted `parent=` on `register_profile` inherits from whatever profile is loaded at that moment. So
both settings silently flipped in CI, and only in CI, while every local run looked correct.

Hence the subprocesses: the profiles are registered at `tests.conftest` *import* time, so the only
way to observe what CI sees is to import it in a fresh interpreter with CI's environment. Asserting
on this test's own already-imported profile would assert on the laptop case -- the case that was
never broken.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

type Profiles = dict[str, dict[str, object]]

# Reads the profile as registered, rather than as loaded, so one subprocess covers all three.
_PROBE = """
import json, sys
from pathlib import Path
sys.path.insert(0, {root!r})
import tests.conftest  # noqa: F401  -- importing is what registers the profiles
from hypothesis import settings

print(json.dumps({{
    name: {{
        "derandomize": settings.get_profile(name).derandomize,
        "database": repr(settings.get_profile(name).database),
        "database_dir": (
            str(Path(str(getattr(settings.get_profile(name).database, "path", ""))).resolve())
            if getattr(settings.get_profile(name).database, "path", None) is not None
            else None
        ),
        "max_examples": settings.get_profile(name).max_examples,
        "print_blob": settings.get_profile(name).print_blob,
    }}
    for name in ("dev", "ci", "deep")
}}))
"""


@pytest.fixture
def ci_profiles() -> Profiles:
    """What GitHub Actions actually loads. `CI=true` is what `hypothesis.is_in_ci()` keys off; the
    real runner sets `GITHUB_ACTIONS=true` as well, and either alone is sufficient."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE.format(root=str(_REPO_ROOT))],
        capture_output=True,
        text=True,
        check=False,
        cwd=_REPO_ROOT,
        env={**os.environ, "CI": "true", "GITHUB_ACTIONS": "true"},
    )
    # Not `check=True`: conftest.py's own import-time guard raises on exactly the regression this
    # file is here to name, and `CalledProcessError` would report the exit status while dropping
    # the message that says which setting went wrong.
    assert result.returncode == 0, f"probing the profiles under CI failed:\n{result.stderr}"
    return json.loads(result.stdout)


@pytest.mark.parametrize("profile", ["dev", "ci", "deep"])
def test_no_profile_is_derandomized_in_ci(profile: str, ci_profiles: Profiles) -> None:
    """`derandomize=True` is what forces `database=None` (`settings.__init__`: "derandomize=True
    implies database=None"), so this is the upstream half of the assertion below."""
    assert ci_profiles[profile]["derandomize"] is False


@pytest.mark.parametrize("profile", ["dev", "ci", "deep"])
def test_every_profile_keeps_its_example_database_in_ci(profile: str, ci_profiles: Profiles) -> None:
    """`database=None` is the state in which the cache steps are decorative: nothing is written for
    the save to pick up, and nothing restored is ever read."""
    assert ci_profiles[profile]["database"] != repr(None), f"profile {profile!r} has no database"


@pytest.mark.parametrize("profile", ["dev", "ci", "deep"])
def test_every_profile_writes_to_the_directory_the_workflows_cache(profile: str, ci_profiles: Profiles) -> None:
    """A database *somewhere* is not the invariant the workflows rely on. Both cache
    `current/.hypothesis/examples`, which is the repository root's `.hypothesis/examples` only
    because they run pytest with `working-directory: ./current`. Asserting a substring would pass
    for any working directory; asserting the resolved path against this file's own location is what
    ties the two together. `None` here is also how an `InMemoryExampleDatabase` -- which
    `hypothesis.database._db_for_path` substitutes with only a warning when the directory is
    unusable -- fails rather than passing as "a database"."""
    expected = str((_REPO_ROOT / ".hypothesis" / "examples").resolve())
    assert ci_profiles[profile]["database_dir"] == expected


def test_deep_prints_blobs_in_ci(ci_profiles: Profiles) -> None:
    """A scheduled run nobody watches live is exactly the one whose counterexamples have to be
    legible from the log afterwards -- and "deep" previously got this only by inheriting it from
    Hypothesis's built-in "ci" profile, i.e. from the same accident this file guards against."""
    assert ci_profiles["deep"]["print_blob"] is True


def test_deep_searches_deeper_than_the_pr_gate(ci_profiles: Profiles) -> None:
    """The one thing the out-of-band schedule buys that the PR gate cannot."""
    deep = ci_profiles["deep"]["max_examples"]
    gate = ci_profiles["ci"]["max_examples"]
    assert isinstance(deep, int)
    assert isinstance(gate, int)
    assert deep > gate
