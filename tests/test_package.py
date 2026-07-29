"""Smoke test proving the package is importable and the test harness is wired up.

No application code exists yet (see ppat/obsidian-tools#1) — this exists so `uv run pytest` and CI have
something real to run rather than exiting non-zero on "no tests collected."
"""

import obsidian_tools


def test_version_is_a_string() -> None:
    assert isinstance(obsidian_tools.__version__, str)
    assert obsidian_tools.__version__
