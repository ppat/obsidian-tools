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

# How many paths any path-list log field in this codebase carries, with the full count logged
# beside it. Every such list is built from whatever happens to be on a volume or a device, so no
# length is the emitting code's to choose. A log pipeline's line-size limit rejects an oversized
# line outright rather than truncating it (Loki's `max_line_size` does exactly this unless it is
# explicitly configured to truncate), which would lose the *count* along with the sample at the
# moment the list is most interesting. Lives here rather than beside any one emitter because the
# emitters are in three modules and the pipeline limit they are sized against is one fact. Each
# emitter pins the cap in its own test, and one cycle's whole record set is additionally swept
# generically (`_oversized_path_lists`, tests/test_local_replicator_cycle.py), so an emitter added
# to the cycle later inherits the invariant rather than having to remember it.
LOG_PATH_SAMPLE_LIMIT = 100

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
