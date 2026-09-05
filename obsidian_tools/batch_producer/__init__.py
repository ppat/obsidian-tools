"""The batch stream's producer (unit B1, ot#125): the only credential in the system permitted to
enqueue patch-carrying work (ADR-0047).

The whole job: read what is staged in the producer's own git repository, split it into chunks, and
enqueue each chunk on the batch stream in order. Nothing here mounts the vault, and nothing here
applies anything — `batch-processor` is a separate component that consumes what this one emits.

**The message format is the deliverable.** `chunk.py` and `staleness.py` are the interface between
the two components; the processor imports those definitions rather than parsing the payload itself,
so the wire contract has one home. Read `chunk.py`'s docstring first.

Split the way `local_replicator/drift.py` is split from `cycle.py`, and for the same reason:

| Pure — no I/O, no coroutines | Impure shell |
| --- | --- |
| `staleness.py` — ADR-0048's per-target hash payload | `generation.py` — the index, read via `GitRunner` |
| `chunk.py` — the wire format, its invariants, codec | `nats_client.py` — the one seam that publishes |
| `chunking.py` — splitting a patch, order-preserving | `producer.py` — the run, and its only `asyncio.run` |
| `violations.py` — what the NATS error callback means | |
"""

from __future__ import annotations
