"""Structured logging, established here as this repository's first logging convention.

Every log line is a single JSON object on stdout, one per line. A CronJob has no scrape target of
its own, so clear, structured logs are the primary observability signal for a component like the
git committer, not metrics — see the `commit` subcommand's PR description for why instrumentation
was judged not to earn its cost here. Any field passed via ``extra=`` on a log call is folded into
the emitted object, so callers can attach structured context (a remote name, a commit SHA, a retry
count) without composing it into the message string themselves.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime

# What a bare `logging.LogRecord` already carries, plus the two attributes `Formatter.format`
# synthesizes (`message`, and `asctime` if a date format is used) — anything else on a record came
# from a caller's `extra=`, and that's what gets folded into the JSON payload below.
_RESERVED_LOG_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()) | {
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_LOG_RECORD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str | None = None) -> None:
    """Configure the root logger to emit one JSON object per line to stdout.

    Called once, at process start, before any subcommand runs.
    """
    resolved_level = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(resolved_level)
