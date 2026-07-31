"""Capture: a stub with a real contract (docs/DESIGN.md §2 item 10, §7 Phase 2).

Capture is attempted for every drifted path, before the cache clone is pulled forward or anything
in iCloud is overwritten. In this phase it reads the path's current bytes out of iCloud and
discards them — functionally `/dev/null`. Phase 5 changes only the destination (the work queue's
drift stream) via `sink`; it does not change this function's signature, return semantics, or where
it sits in the cycle, which is the entire point of shipping the ordering now (see `cycle.py`).

Capture does not exist to support editing on devices — it exists so the read replica is
non-destructive. If editing in the native app becomes the actual draw, the design has failed on
its own terms; nothing here should be read as, or extended into, a device-authoring workflow.

**This module is the seam the Phase 2 acceptance test injects a failure into** (see
docs/DESIGN.md §7 Phase 2, "force local-replicator's capture step to fail for a drifted path").
`sink` is deliberately the injectable part: a test failure there proves the same skip-and-retry
path a real iCloud read failure would take, without needing to fake NFS/iCloud I/O semantics that
don't exist in a temp-directory test.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from obsidian_tools.local_replicator.rsync_ops import DriftedPath

logger = logging.getLogger(__name__)


class CaptureError(RuntimeError):
    """Raised by a `CaptureSink` to signal this path's capture must be treated as failed."""


# A named tuple, not an inline `except OSError, CaptureError:` — matches the convention already
# established by `commands/commit.py`'s `_STAGING_FAILURES`.
_CAPTURE_FAILURES = (OSError, CaptureError)


# `content` is `None` for a path the comparison flagged that no longer exists in iCloud (a human
# deleted it) — there is nothing to preserve, so a sink is never expected to fail on that case
# specifically, though nothing stops one from doing so to simulate a downstream failure regardless.
CaptureSink = Callable[[str, bytes | None], None]


def discard_sink(path: str, content: bytes | None) -> None:
    """The Phase 2 destination: read the bytes, throw them away. Phase 5 replaces this with a
    publish onto the work queue's drift stream (docs/DESIGN.md §7 Phase 5) — same signature."""
    del path, content


def capture_path(icloud_vault_dir: Path, drifted: DriftedPath, *, sink: CaptureSink = discard_sink) -> bool:
    """Attempt to capture one drifted path's current iCloud contents. Returns True on success.

    Never raises: a capture failure on one path must not abort the whole cycle — the caller treats
    a `False` return as "skip this path's publish, retry next cycle" (`cycle.py`), never as a
    reason to stop processing the rest of the drifted paths.
    """
    absolute = icloud_vault_dir / drifted.path
    try:
        content = absolute.read_bytes() if absolute.exists() else None
        sink(drifted.path, content)
    except _CAPTURE_FAILURES:
        logger.exception(
            "capture failed for drifted path; publish will skip it this cycle",
            extra={"event": "capture_failed", "path": drifted.path},
        )
        return False
    return True
