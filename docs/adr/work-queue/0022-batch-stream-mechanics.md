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
  interactive door is shut for a run, every agent write stops silently and indefinitely — far worse
  than a slow batch. What a run shuts, and what the watchdog reopens, is
  [ADR-0052](./0052-batch-mode-stops-the-agent-instance.md). The smallest shape ships (a dumb
  restore); the maximum-window and post-disable drain are refinements
  ([D4](../../../ROADMAP.md#group-d--operability)). This
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
mode" reduces to which of the two MCP instances is running: the agent instance is stopped for the
run's duration and the ingestor instance is not, which is what lets promotion keep draining
mid-batch ([ADR-0052](./0052-batch-mode-stops-the-agent-instance.md)). Quiescing the editor during a
batch is not merely rejected but *unavailable* — batch writes travel through it.

**A failed chunk stops itself and its direct dependents, and nothing else.** The run continues past
a dead-lettered chunk — a bulk import is thousands of chunks, and one bad file must not discard the
rest — and chunks of every other batch are unaffected in every case. What ordering buys has to be
paid for explicitly here: a later chunk of the *same* batch that links to a page the failed chunk
would have created is parked with its own dead-letter reason rather than applied, because applying
it is precisely how the relink lands without its rename and a note is left pointing at a page nobody
created. Only a create can be depended on this way, the new name of a rename included; a path the
batch merely modified or deleted cannot be, because a batch touches each path at most once
([ADR-0048](./0048-batch-staleness-per-file-hash.md)).

**Parking is expensive, and it is still right, because the raw layer is where the alternative fails
silently.** Applying a dependent writes a note whose link points at a page the run has just failed to
create. `05-raw/` is write-once *and* validation-exempt
([ADR-0015](../content-model/0015-raw-immutability.md)), and the lint pass reporting nothing about
that layer is it working correctly — so the obvious defence, "apply it and let the lint pass find
the broken link", is false exactly where a bulk import puts the majority of its content. Parking
converts a wrong page nothing will ever report into an absent one: counted, copied to the dead-letter
stream, and the run exits non-zero. That is the whole justification for the mechanism, and its price
is a chunk's worth of notes deferred to the next cycle.

**The parking is one hop deep, and the second hop is where that justification runs out.** A chunk
parked as a dependent contributes nothing further to what is blocked. Chained instead, the rule parks
the whole tail of a link-dense batch — measured, one refused write parked 31 of 36 chunks and left
1,287 of 1,500 notes unwritten, against one parked for the same corpus with its links removed —
because the reference test is deliberately an over-approximation, sound applied once and close to
"discard everything after the first failure" applied thirty times. What a second-hop chunk links to
is a page a *parked* chunk would create, and parked work is not lost: the producer regenerates the
batch and the next cycle applies it, so that dangle closes on its own, where parking would cost a
chunk's worth of notes on every cycle until it did. One hop remains expensive — at 45% link density
one refused write parks 24 of the 30 chunks behind it, deferring 1,068 of 1,500 notes to the cycle
that resolves the failure. That is the cost of the guarantee, stated rather than discovered.

**Regeneration is what recovers a partial batch, and it converges where nothing genuinely
conflicts.** Nothing consumes the dead-letter stream, so failed work stays undone until the producer
emits the batch again — the whole of it, most of it already applied. A chunk whose every path already
holds exactly what applying it would leave there is settled rather than parked, and that question is
asked before any refusal, including the dependency one. Measured: two cycles take a batch killed at a
third of the way through to complete, where before it was a fixed point at zero progress.

Where a target *has* drifted, cycles stop short of complete and further cycles change nothing — the
conflicting chunk is refused on its merits, its direct dependents are parked, and everything else
settles. **That is a pause, not a loss, and the distinction is measured:** resolving the one
conflicting note and regenerating completes the import on the very next cycle. So the residue is
what a genuine conflict is supposed to cost — a human decision, which no number of further cycles
can substitute for — and the run names it by exiting non-zero with the conflicting path in the
rejection.
