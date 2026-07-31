"""obsidian-tools: CLI entry point.

Subcommands share the same config/git helpers under `obsidian_tools/` — see
`obsidian_tools/commands/` for each subcommand's own orchestration. `commit` is the first;
`replicate` (the Mac-side `local-replicator`, tracked separately at ppat/obsidian-tools#3) is
expected to land beside it as another subparser here, over the same shared helpers.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from collections.abc import Callable, Sequence
from types import FrameType

from obsidian_tools.commands import commit as commit_command
from obsidian_tools.config import CommitConfig, ConfigError
from obsidian_tools.logging_config import configure_logging

logger = logging.getLogger(__name__)


# The CronJob pod's terminationGracePeriod stop is a SIGTERM; a graceful stop needs a handler
# installed for it. CPython's own default only installs one for SIGINT, so without this, SIGTERM
# keeps the platform default disposition — and when this process is PID 1 in-container (the
# manifest supplies the subcommand directly as container args, replacing whatever would otherwise
# own PID 1), the kernel does not deliver a default-disposition signal to PID 1 at all (signal(7)):
# SIGTERM is silently discarded and every stop escalates straight to SIGKILL after the grace
# period, which is exactly how a run gets killed mid `git add`/`commit`/`reset` and leaves a stale
# `index.lock` behind (see vault_git/provisioning.py's `_clear_stale_index_lock`, the recovery net
# for whatever this handler doesn't manage to unwind before the kernel kills it outright).
# Installing *any* handler restores normal delivery even for PID 1, independent of the container's
# entrypoint — this is a genuine fix on its own, not merely preparation for one made elsewhere.
class GracefulShutdown(RuntimeError):
    """Raised from the SIGTERM handler so a run can unwind and log instead of dying silently."""


def _handle_sigterm(signum: int, frame: FrameType | None) -> None:
    raise GracefulShutdown(f"received signal {signum}")


def install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _handle_sigterm)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="obsidian-tools")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    commit_parser = subparsers.add_parser("commit", help="stage, commit and push the vault's git history")
    commit_parser.set_defaults(handler=_run_commit)

    return parser


def _run_commit(_args: argparse.Namespace) -> int:
    try:
        config = CommitConfig.from_env()
    except ConfigError:
        logger.exception("invalid configuration", extra={"event": "config_error"})
        return 2
    return commit_command.run(config)


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    install_signal_handlers()
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.handler
    try:
        return handler(args)
    except GracefulShutdown:
        logger.warning("stopping on SIGTERM before completing this cycle", extra={"event": "sigterm_shutdown"})
        return 143  # 128 + SIGTERM(15), the conventional signal-exit convention


if __name__ == "__main__":
    sys.exit(main())
