"""The `export-metrics` subcommand: ADR-0037's independent vault-loaded exporter (unit D1, ot#121).

Unlike `commit`/`replicate`/`drain`, this one never returns under normal operation -- it blocks
serving `/metrics` until SIGTERM, per `vault_exporter/server.py`'s `serve()`. See that module for
the poll-loop/HTTP-server split and what a clean shutdown means for a long-running subcommand.
"""

from __future__ import annotations

import logging

from obsidian_tools.config import VaultExporterConfig
from obsidian_tools.vault_exporter.server import serve

logger = logging.getLogger(__name__)


def run(config: VaultExporterConfig) -> int:
    logger.info(
        "starting vault metrics exporter",
        extra={
            "event": "vault_exporter_starting",
            "host": config.listen_host,
            "port": config.listen_port,
            "poll_interval_seconds": config.poll_interval_seconds,
        },
    )
    return serve(config)
