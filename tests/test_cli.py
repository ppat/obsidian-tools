"""Tests for the `obsidian-tools` console entry point's subcommand wiring (obsidian_tools/cli.py)."""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path

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


def test_replicate_subcommand_is_registered() -> None:
    parser = build_parser()

    args = parser.parse_args(["replicate"])

    assert args.subcommand == "replicate"
    assert callable(args.handler)


def test_main_returns_config_error_exit_code_for_replicate_when_required_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ICLOUD_VAULT_DIR", raising=False)
    monkeypatch.delenv("GIT_REMOTE_ORIGIN_URL", raising=False)

    assert main(["replicate"]) == 2


def test_drain_subcommand_is_registered() -> None:
    parser = build_parser()

    args = parser.parse_args(["drain"])

    assert args.subcommand == "drain"
    assert callable(args.handler)


def test_main_runs_drain_with_no_required_env_at_all(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Unlike `replicate`, `drain` has no required environment variable at all -- `DrainConfig`
    only ever reads `LOCAL_REPLICATOR_SPOOL_DIR`, which has a default (config.py's `DrainConfig`
    docstring: deliberately decoupled from `ReplicateConfig`'s `ICLOUD_VAULT_DIR`/
    `GIT_REMOTE_ORIGIN_URL`). Points the spool dir at a real temp directory so this exercises the
    actual drain, not just argument parsing."""
    monkeypatch.setenv("LOCAL_REPLICATOR_SPOOL_DIR", str(tmp_path))

    assert main(["drain"]) == 0


def test_export_metrics_subcommand_is_registered() -> None:
    parser = build_parser()

    args = parser.parse_args(["export-metrics"])

    assert args.subcommand == "export-metrics"
    assert callable(args.handler)


def test_main_returns_config_error_exit_code_for_export_metrics_when_required_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the config step runs here -- `main(["export-metrics"])` would otherwise block forever
    serving `/metrics` (`export_metrics_command.run` never returns under normal operation), so this
    test only ever exercises the code path that returns before `run()` is reached."""
    monkeypatch.delenv("OBSIDIAN_API_KEY", raising=False)

    assert main(["export-metrics"]) == 2


def test_installed_sigterm_handler_raises_graceful_shutdown() -> None:
    """CPython only installs its own handler for SIGINT; every other signal, SIGTERM included,
    keeps the interpreter's default disposition, and the OS default action for SIGTERM is
    immediate termination — no exception, no unwind. Installing this handler is what makes CPython
    raise `GracefulShutdown` instead.

    Runs in a subprocess, deliberately: sending SIGTERM straight to the pytest process would kill
    the *runner* outright if the handler were ever missing (default disposition is unconditional
    termination), producing a bare exit 143 with no `FAILED` line rather than a legible test
    failure — this was the previous shape of this test. Isolating the signal to a child process
    means a regression here shows up as an ordinary, readable assertion failure instead of taking
    the whole test run down with it."""
    script = (
        "import os, signal\n"
        "from obsidian_tools import cli\n"
        "cli.install_signal_handlers()\n"
        "os.kill(os.getpid(), signal.SIGTERM)\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10)

    # A working handler turns the signal into an uncaught GracefulShutdown in the child, which
    # Python reports as a traceback on stderr and a non-signal exit. Without the handler, the
    # child dies directly by the signal (a negative returncode, per subprocess's convention) with
    # nothing on stderr at all — this assertion is what makes that difference legible.
    assert "GracefulShutdown" in result.stderr, (
        f"expected a GracefulShutdown traceback from the child process; "
        f"got returncode={result.returncode!r} stderr={result.stderr!r}"
    )


def test_main_stops_gracefully_and_returns_143_on_sigterm(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main()` installs a real, process-wide SIGTERM handler as a side effect
    (`install_signal_handlers`) — must be restored afterward, or a later test in the same pytest
    process (or an actual SIGTERM delivered to the test runner itself, e.g. on CI cancellation)
    inherits a handler this test installed for an unrelated reason."""

    def _raise_shutdown(_config: object) -> int:
        raise GracefulShutdown("received signal 15")

    monkeypatch.setattr(cli.commit_command, "run", _raise_shutdown)
    monkeypatch.setenv("GIT_REMOTE_ORIGIN_URL", "git@github.com:ppat/obsidian-vault.git")
    monkeypatch.setenv("GIT_REMOTE_NAS_URL", "git@nas:vault.git")

    previous = signal.getsignal(signal.SIGTERM)
    try:
        assert main(["commit"]) == 143
    finally:
        signal.signal(signal.SIGTERM, previous)
