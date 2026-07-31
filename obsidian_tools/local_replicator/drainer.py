"""The drainer: sends spooled drift patches onward (docs/DESIGN.md §2 item 10, §7 Phase 2).

Decoupled from the replication cycle's own step numbering by design -- it runs on its own
schedule, reading whatever the spool currently holds, independent of which cycle wrote any given
entry (docs/DESIGN.md §4 Plane B: "A separate drainer, decoupled from this cycle's own numbering,
sends the spool onward, with retry, onto the work queue's drift stream"). In Phase 2 there is
nowhere for it to send anything -- the work queue's drift stream and `drift-processor` don't exist
yet (ppat/obsidian-tools#4, Phase 5) -- so `discard_sink` *is* the destination: read the entry,
throw it away, remove it from the spool. Phase 5 replaces only the sink; draining, ordering, and
the spool format itself do not change (docs/DESIGN.md §7 Phase 5: "the drainer's stub destination
stops being discard and becomes the drift stream").

Draining still has to run in Phase 2, even though it discards everything -- an undrained spool
grows without bound, which is a real operational cost even for a stub.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from obsidian_tools.local_replicator.drift import SpoolEntry
from obsidian_tools.local_replicator.spool import list_spool_files, read_spool_entry

logger = logging.getLogger(__name__)

DrainSink = Callable[[SpoolEntry], None]


def discard_sink(entry: SpoolEntry) -> None:
    """The Phase 2 destination: read the entry, throw it away -- functionally `/dev/null`. Phase 5
    replaces this with a publish onto the work queue's drift stream (docs/DESIGN.md §7 Phase 5);
    same signature, so nothing about `drain_once` below has to change to accommodate it."""
    del entry


def drain_once(spool_dir: Path, *, sink: DrainSink = discard_sink) -> list[str]:
    """Drain every entry currently in the spool, in filename (write) order. Returns the vault path
    of every entry successfully drained.

    An entry the sink fails on is left in the spool -- undrained, not lost -- for a later drain to
    retry, and `drain_once` keeps going with the rest rather than stopping at the first failure:
    unlike the cycle's own spool-write gate (which is about *storing* every entry before publish
    proceeds, docs/DESIGN.md §4 Plane B), drained entries are independent of each other once
    written, so one stuck entry has no reason to hold up the others.
    """
    drained: list[str] = []
    for spool_file in list_spool_files(spool_dir):
        entry = read_spool_entry(spool_file)
        try:
            sink(entry)
        except Exception:
            logger.exception(
                "drain failed for spool entry; leaving it in the spool for the next drain",
                extra={"event": "drain_failed", "path": entry.path, "spool_file": str(spool_file)},
            )
            continue
        spool_file.unlink()
        drained.append(entry.path)
    return drained
