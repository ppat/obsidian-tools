"""The two gauges, and the one piece of mutable state behind them.

`render_prometheus_text` is pure: a `MetricsSnapshot` in, exposition-format bytes out. It never
imports sockets or threads, so it's table-testable the same way `enumeration.py` is.

`VaultMetricsState` is the one seam holding real mutable state in this package -- read by every
`/metrics` request, written by every poll cycle (`server.py`). It is intentionally *not* reset on a
failed or unhealthy poll: a gauge line is only ever written from a poll that asserted
`file_count >= 1`, so `obsidian_vault_files_total` and `obsidian_vault_enumeration_success_timestamp_
seconds` both hold whatever they last held on success, and freeze there through any number of
subsequent failures. That's deliberate, not an oversight -- see the module docstring's staleness
argument in `obsidian_tools/config.py`'s `VaultExporterConfig` and the two HELP strings below: a
frozen `..._files_total` next to a frozen, aging `..._success_timestamp_seconds` is what makes a
stuck exporter look different from a healthy one to whoever queries it, without this code ever
having to decide what "too stale" means -- that's a query-time judgment, not an emission-time one
(DESIGN.md: "Instrument early, alert never until AI triage exists").

Before the first successful poll, both gauges are omitted from the exposition text entirely, rather
than defaulting to `0`. Emitting `0` by default would be indistinguishable from a real, healthy
enumeration that happened to return a zero count on a technicality this module doesn't allow (an
empty `files` array is itself classified unhealthy, see `enumeration.py`) -- omission is the only
representation that can't be confused with a value.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

_FILES_TOTAL_HELP = (
    "# HELP obsidian_vault_files_total Top-level vault entries returned by the most recent "
    "successful enumeration (a healthy result requires this to be >= 1; see ADR-0037)."
)
_FILES_TOTAL_TYPE = "# TYPE obsidian_vault_files_total gauge"
_SUCCESS_TIMESTAMP_HELP = (
    "# HELP obsidian_vault_enumeration_success_timestamp_seconds Unix time of the most recent "
    "successful (non-empty) vault enumeration; absent or stale means the vault has not been "
    "confirmed open recently."
)
_SUCCESS_TIMESTAMP_TYPE = "# TYPE obsidian_vault_enumeration_success_timestamp_seconds gauge"


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    """An immutable read of `VaultMetricsState` at one instant -- what `render_prometheus_text`
    actually renders. `None` in either field means "no successful poll yet", not "zero"."""

    file_count: int | None
    success_timestamp: float | None


def render_prometheus_text(snapshot: MetricsSnapshot) -> bytes:
    """Pure: a snapshot in, Prometheus text-exposition-format bytes out (HELP/TYPE lines always
    present so the metric is discoverable even before it has a value; the value line itself only
    when the snapshot actually has one)."""
    lines = [_FILES_TOTAL_HELP, _FILES_TOTAL_TYPE]
    if snapshot.file_count is not None:
        lines.append(f"obsidian_vault_files_total {snapshot.file_count}")
    lines += [_SUCCESS_TIMESTAMP_HELP, _SUCCESS_TIMESTAMP_TYPE]
    if snapshot.success_timestamp is not None:
        lines.append(f"obsidian_vault_enumeration_success_timestamp_seconds {snapshot.success_timestamp}")
    return ("\n".join(lines) + "\n").encode("ascii")


class VaultMetricsState:
    """Thread-safe holder for the exporter's current view. `server.py`'s poll loop and its HTTP
    handler run on different threads (a background poll thread and the request-handling thread(s)
    of a `ThreadingHTTPServer`), so every read and write goes through the same lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._file_count: int | None = None
        self._success_timestamp: float | None = None

    def record_success(self, file_count: int, when: float) -> None:
        with self._lock:
            self._file_count = file_count
            self._success_timestamp = when

    def snapshot(self) -> MetricsSnapshot:
        with self._lock:
            return MetricsSnapshot(file_count=self._file_count, success_timestamp=self._success_timestamp)
