"""obsidian-tools: CLI entry point.

Subcommands share the same config/git helpers under `obsidian_tools/` — see
`obsidian_tools/commands/` for each subcommand's own orchestration. `commit` is the first;
`replicate` (the Mac-side `local-replicator`, tracked separately at ppat/obsidian-tools#3) is
expected to land beside it as another subparser here, over the same shared helpers.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence

from obsidian_tools.commands import commit as commit_command
from obsidian_tools.config import CommitConfig, ConfigError
from obsidian_tools.logging_config import configure_logging

logger = logging.getLogger(__name__)


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
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.handler
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
