"""`batch-processor` (unit A2, ot#5): the batch stream's consumer.

One job: take chunks off the batch stream in strict FIFO and apply each through the same gated MCP
path as ordinary interactive ingest. It mounts nothing — DESIGN.md's "one writer, one door" gives
the processors no mount at all, so every fact it holds about the vault's current bytes is something
it read back through that same door.

`batch_producer/chunk.py` and `staleness.py` are the wire contract, imported rather than
re-derived. Read `chunk.py`'s docstring before anything here.

Four obligations are this component's alone, each because it is the only thing standing where the
decision can be made:

| Obligation | Record | Lives in |
| --- | --- | --- |
| Stale-reject, whole chunk, before any write lands | ADR-0048 | `preflight.py` |
| `05-raw/` create-only, in this processor's own code | ADR-0015 | `preflight.py` |
| Backpressure keyed on promotion-stream depth, not MCP health | ADR-0022 | `fairness.py` |
| Start the agent MCP instance if this processor dies mid-run | ADR-0052, unit D4 | `watchdog.py` |

Split pure/impure the way `local_replicator/drift.py` is split from `cycle.py`:

| Pure — no I/O, no coroutines | Impure shell |
| --- | --- |
| `envelope.py` — what an MCP response means | `mcp_client.py` — the one seam onto the gated path |
| `preflight.py` — apply-or-reject, per chunk | `consumer.py` — the one seam onto JetStream |
| `patching.py` — the diff, applied; the writes, planned | `agent_instance.py` — the one seam onto the cluster |
| `fairness.py` — yield-or-proceed, dead-letter-or-redeliver | `processor.py` — one batch run |
| `watchdog.py` — start, release or leave alone | |

**The admission validator is a named absence, not an oversight.** A chunk entering curated space
should cross ADR-0007's schema and provenance gate, and the roadmap records that dependency
explicitly (A2 depends on A4); the validator is unbuilt (ot#6), and the bootstrap import this
component exists for lands entirely in the validation-exempt raw layer. When it ships, its call
belongs between the pre-flight and the writes in `processor.py`.
"""

from __future__ import annotations
