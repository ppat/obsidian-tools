"""obsidian-tools: CLI entry point.

Subcommands share the same config/logging helpers under `obsidian_tools/` — see
`obsidian_tools/commands/` for each subcommand's own orchestration. `commit` is the in-cluster git
committer; `replicate` is `local-replicator`'s replication cycle, and `drain` is its spool drainer,
run on a separate schedule (ppat/obsidian-tools#3, ADR-0025) — those three are
clients of the same shared `GitRunner`/config/retry/logging helpers, not a parallel set each.
`export-metrics` is ADR-0037's independent vault-loaded exporter (unit D1, ot#121): it shares
config/logging conventions with the other three but never touches `GitRunner` or `retry` — its only
I/O is an HTTP call to Obsidian's Local REST API, and unlike the other three it never returns under
normal operation (it serves `/metrics` until SIGTERM). `enqueue-batch` is the batch stream's
producer (unit B1, ot#125): it runs in the Coder workspace rather than in-cluster, is a client of
`GitRunner` like the first three, and is the only subcommand that publishes to NATS.
`process-batch` is that stream's consumer, `batch-processor` (unit A2, ot#5): in-cluster, no git
and no mount at all, reading and writing the vault only through the gated MCP path.
`watch-agent-instance` is unit D4's watchdog — deliberately a *separate* subcommand on a separate
schedule, because a watchdog sharing a process with `batch-processor` would die with it, which is
the failure it exists to catch.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from collections.abc import Callable, Sequence
from types import FrameType

from obsidian_tools.commands import commit as commit_command
from obsidian_tools.commands import drain as drain_command
from obsidian_tools.commands import enqueue_batch as enqueue_batch_command
from obsidian_tools.commands import export_metrics as export_metrics_command
from obsidian_tools.commands import process_batch as process_batch_command
from obsidian_tools.commands import replicate as replicate_command
from obsidian_tools.commands import watch_agent_instance as watch_agent_instance_command
from obsidian_tools.config import (
    BatchProcessorConfig,
    BatchProducerConfig,
    CommitConfig,
    ConfigError,
    DrainConfig,
    ReplicateConfig,
    VaultExporterConfig,
    WatchdogConfig,
)
from obsidian_tools.logging_config import configure_logging

logger = logging.getLogger(__name__)


# The CronJob pod's terminationGracePeriod stop is a SIGTERM, and a graceful stop needs a handler
# installed for it — but not for the reason an earlier version of this comment gave. This process
# is NOT PID 1: the Dockerfile's ENTRYPOINT is `tini`, and the manifest supplies this subcommand
# only as container `args`, which Docker/Kubernetes map onto CMD (appended after ENTRYPOINT)
# precisely so `tini` keeps PID 1 and this process runs underneath it as an ordinary child — `args`
# does not replace ENTRYPOINT, an earlier version of this comment had that backwards. `tini`
# forwards SIGTERM to this process normally; the PID-1 signal-delivery quirk in signal(7) (the
# kernel not delivering a default-disposition signal to PID 1 at all) never applies here, because
# this process is never PID 1.
#
# The handler is needed anyway, for an unrelated reason: CPython only installs its own handler for
# SIGINT (raising KeyboardInterrupt); every other signal, SIGTERM included, keeps the interpreter's
# default disposition, and the OS default action for SIGTERM is immediate termination — no
# exception, no unwind, no `finally`. Without a handler here, a SIGTERM delivered mid
# `git add`/`commit`/`reset` kills the process at that exact instant, which is exactly how a stale
# `*.lock` file gets left behind (see vault_git/provisioning.py's `_clear_stale_locks`, the
# recovery net for whatever this handler doesn't manage to unwind in time). Installing a handler
# makes CPython raise `GracefulShutdown` instead of taking SIGTERM's default action, giving a run
# the chance to unwind and log before exiting.
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

    replicate_parser = subparsers.add_parser(
        "replicate", help="run one local-replicator cycle: overlay, diff, spool, pull, publish, advance"
    )
    replicate_parser.set_defaults(handler=_run_replicate)

    drain_parser = subparsers.add_parser(
        "drain", help="drain the local spool: send each drift patch onward (Phase 2: discard)"
    )
    drain_parser.set_defaults(handler=_run_drain)

    export_metrics_parser = subparsers.add_parser(
        "export-metrics",
        help="serve Prometheus metrics for the independent vault-loaded health signal (ADR-0037)",
    )
    export_metrics_parser.set_defaults(handler=_run_export_metrics)

    enqueue_batch_parser = subparsers.add_parser(
        "enqueue-batch",
        help="split what is staged into chunks and enqueue them on the batch stream (ADR-0022, ADR-0048)",
    )
    enqueue_batch_parser.set_defaults(handler=_run_enqueue_batch)

    process_batch_parser = subparsers.add_parser(
        "process-batch",
        help="drain the batch stream in strict FIFO, applying each chunk through the gated MCP path",
    )
    process_batch_parser.set_defaults(handler=_run_process_batch)

    watch_agent_instance_parser = subparsers.add_parser(
        "watch-agent-instance",
        help="start the agent MCP instance if the batch run holding it stopped has died (ADR-0052, unit D4)",
    )
    watch_agent_instance_parser.set_defaults(handler=_run_watch_agent_instance)

    return parser


def _run_commit(_args: argparse.Namespace) -> int:
    try:
        config = CommitConfig.from_env()
    except ConfigError:
        logger.exception("invalid configuration", extra={"event": "config_error"})
        return 2
    return commit_command.run(config)


def _run_replicate(_args: argparse.Namespace) -> int:
    try:
        config = ReplicateConfig.from_env()
    except ConfigError:
        logger.exception("invalid configuration", extra={"event": "config_error"})
        return 2
    return replicate_command.run(config)


def _run_drain(_args: argparse.Namespace) -> int:
    try:
        config = DrainConfig.from_env()
    except ConfigError:
        logger.exception("invalid configuration", extra={"event": "config_error"})
        return 2
    return drain_command.run(config)


def _run_export_metrics(_args: argparse.Namespace) -> int:
    try:
        config = VaultExporterConfig.from_env()
    except ConfigError:
        logger.exception("invalid configuration", extra={"event": "config_error"})
        return 2
    return export_metrics_command.run(config)


def _run_enqueue_batch(_args: argparse.Namespace) -> int:
    try:
        config = BatchProducerConfig.from_env()
    except ConfigError:
        logger.exception("invalid configuration", extra={"event": "config_error"})
        return 2
    return enqueue_batch_command.run(config)


def _run_process_batch(_args: argparse.Namespace) -> int:
    try:
        config = BatchProcessorConfig.from_env()
    except ConfigError:
        logger.exception("invalid configuration", extra={"event": "config_error"})
        return 2
    return process_batch_command.run(config)


def _run_watch_agent_instance(_args: argparse.Namespace) -> int:
    try:
        config = WatchdogConfig.from_env()
    except ConfigError:
        logger.exception("invalid configuration", extra={"event": "config_error"})
        return 2
    return watch_agent_instance_command.run(config)


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
