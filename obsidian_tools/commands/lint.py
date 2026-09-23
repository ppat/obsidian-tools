"""The `lint` subcommand: the lint pass (unit A5, ot#83).

Runs in-cluster on a schedule, with the vault volume mounted read-only and the ingestor door as its
only way to write. See `obsidian_tools/lint_pass/` for what one pass decides and does.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from obsidian_tools.batch_processor.mcp_client import McpClient, McpToolNames
from obsidian_tools.config import LintPassConfig
from obsidian_tools.lint_pass.shell import DigestHook, run_pass


def run(config: LintPassConfig) -> int:
    mcp = McpClient(
        base_url=config.mcp_url,
        api_key=config.mcp_api_key,
        tools=McpToolNames(read=config.mcp_tool_read, write=config.mcp_tool_write, append=config.mcp_tool_append),
        timeout_seconds=config.mcp_timeout_seconds,
        verify_tls=config.mcp_verify_tls,
        retries=config.mcp_retries,
        retry_base_delay_seconds=1.0,
        client_name="obsidian-tools lint-pass",
    )
    hook = DigestHook(config.digest_hook_url, config.digest_hook_token, config.digest_hook_timeout_seconds)
    return run_pass(
        root=Path(config.vault_dir), mcp=mcp, hook=hook, digest_limit=config.digest_max_items, now=datetime.now(UTC)
    )
