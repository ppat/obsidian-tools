"""Tests for the `obsidian-tools` console entry point's subcommand wiring (obsidian_tools/cli.py)."""

from __future__ import annotations

import pytest

from obsidian_tools.cli import build_parser, main


def test_commit_subcommand_is_registered() -> None:
    parser = build_parser()

    args = parser.parse_args(["commit"])

    assert args.subcommand == "commit"
    assert callable(args.handler)


def test_no_subcommand_is_an_error() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_main_returns_config_error_exit_code_when_required_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIT_REMOTE_ORIGIN_URL", raising=False)
    monkeypatch.delenv("GIT_REMOTE_NAS_URL", raising=False)

    assert main(["commit"]) == 2
