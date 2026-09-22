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

**Every chunk entering curated space crosses the admission validator** (ADR-0007, `admission/`),
called in `processor.py` between planning the writes and performing them. The check itself is not
this component's — it is one validator with three callers — but calling it is, because a batch
chunk carries the widest write scope in the system and nothing downstream of this processor judges
what it writes. What a refusal does to the chunk, and why it is judged after the settle question rather
than before it, is `processor.py`'s "Admission" section.
"""

from __future__ import annotations
