# 0022. Batch mechanics: strict FIFO, unsharded, stale-reject, fairness backpressure, watchdog before unattended

**Status:** Accepted ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted) ·
**Unit:** [A2](../../../ROADMAP.md#group-a--pipeline-mechanisms) ·
**Ticket:** [ot#5](https://github.com/ppat/obsidian-tools/issues/5)

## Context

Bulk work — the bootstrap import, later structural refactors — is too large for tool-by-tool
traffic but must not become a second writer or a second operating state
([the pillar](../../../DESIGN.md#bulk-and-drift-are-feeds-into-the-one-write-path-never-lanes-around-it)).
Producers emit git patches; chunks are the transaction and redelivery unit; `batch-processor`
applies each through the same gated MCP path as ordinary ingest.

## Decision

- **Stale patches are rejected back to the producer, never merged.** A patch records its base; if
  the vault moved, the producer regenerates. The tempting 3-way merge would reintroduce exactly the
  subsystem this architecture deleted. What "moved" is measured against is
  [ADR-0048](./0048-batch-staleness-per-file-hash.md).
- **One FIFO stream, not prefix-sharded parallel streams.** Renames rewrite wikilinks in arbitrary
  other notes (the ordinary case, not the exotic one), which breaks sharding's disjointness
  assumption; sharding removes cross-stream ordering ("rename in X, then relink in Y" becomes
  inexpressible); the bootstrap gains nothing (it lands in one write-once layer); and decisively,
  the processor tier is not the bottleneck — one MCP, one single-threaded editor behind everything.
  FIFO lets a producer express dependency by ordering — a guarantee of chunk self-containedness
  nobody could actually make — and keeps the staleness question cheap; a messy staleness answer is
  what invites the merge engine back.
- **Backpressure keys on promotion-stream depth, not MCP health.** What an unthrottled batch costs
  is starving the paths where a human is waiting; health-based throttling reacts only once the
  shared bottleneck is already saturated, fairness-based reacts when someone is waiting. Backoff
  costs only time — unacked messages redeliver, nothing is lost. Exponential backoff with jitter, a
  cap on in-flight requests (likely one), a dead-letter path instead of infinite redelivery. The
  asymmetry is deliberate and one-way: bulk yields to interactive, never the reverse.
- **Triggering policy:** a run starts when the stream exceeds a size threshold, or at least once a
  day whenever non-empty — and only inside a permitted time window.
- **The watchdog exists before the stream ever runs unattended.** If the processor dies while the
  agent handle is disabled for a run, every agent write stops silently and indefinitely — far worse
  than a slow batch. The smallest shape ships (a dumb re-enable); the maximum-window and
  post-disable drain are refinements ([D4](../../../ROADMAP.md#group-d--operability)). This
  placement is the arbitration of a contradiction the older records carried
  ([ROADMAP, supersessions](../../../ROADMAP.md#records-this-roadmap-supersedes-or-arbitrates)).

## Alternatives considered

3-way merge (deleted subsystem returns); sharded streams (four grounds above); a dedicated fast
lane for the import (complexity that never earns itself back — the import is hours, once,
resumable; and it runs as sole writer by circumstance anyway, before any write scope opens); an
exclusive maintenance window with direct filesystem writes (the superseded design this replaced —
eliminated, not softened).

## Consequences

Accepted cost is throughput: thousands of sequential round-trips through one event loop. "Batch
mode" reduces to which gateway handle is enabled; promotion keeps draining mid-batch because it
shares the ingestor handle. Quiescing the editor during a batch is not merely rejected but
*unavailable* — batch writes travel through it.
