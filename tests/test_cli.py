"""Tests for the `obsidian-tools` console entry point's subcommand wiring (obsidian_tools/cli.py)."""

from __future__ import annotations

import os
import signal

import pytest

from obsidian_tools import cli
from obsidian_tools.cli import GracefulShutdown, build_parser, main


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


def test_installed_sigterm_handler_raises_graceful_shutdown() -> None:
    """CPython only installs a default handler for SIGINT; without an explicit one, SIGTERM keeps
    the platform default, which the kernel does not deliver at all to a PID-1 process (signal(7))
    — exactly the shape this container runs in when the manifest supplies the subcommand directly
    as container args. Installing this handler is what makes SIGTERM delivery work in the first
    place, independent of what owns PID 1; this test proves the handler itself is wired to raise,
    not the PID-1-specific kernel behaviour (which a non-PID-1 test process can't reproduce)."""
    previous = signal.getsignal(signal.SIGTERM)
    try:
        cli.install_signal_handlers()
        with pytest.raises(GracefulShutdown):
            os.kill(os.getpid(), signal.SIGTERM)
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_main_stops_gracefully_and_returns_143_on_sigterm(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_shutdown(_config: object) -> int:
        raise GracefulShutdown("received signal 15")

    monkeypatch.setattr(cli.commit_command, "run", _raise_shutdown)
    monkeypatch.setenv("GIT_REMOTE_ORIGIN_URL", "git@github.com:ppat/obsidian-vault.git")
    monkeypatch.setenv("GIT_REMOTE_NAS_URL", "git@nas:vault.git")

    assert main(["commit"]) == 143
