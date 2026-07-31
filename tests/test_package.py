"""Smoke test proving the package is importable and the test harness is wired up.

See tests/test_commands_commit.py and tests/vault_git_* for the `commit` subcommand's own coverage;
this file is deliberately minimal.
"""

import obsidian_tools


def test_version_is_a_string() -> None:
    assert isinstance(obsidian_tools.__version__, str)
    assert obsidian_tools.__version__
