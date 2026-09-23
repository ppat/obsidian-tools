# 0001. One writer, one door — and two-way device sync deleted rather than mitigated

**Status:** Accepted — the count of declared bypasses superseded by [ADR-0063](../content-model/0063-schema-published-from-this-repository.md) (Proposed), marked where it stands ·
**Pillar:** [One writer, one door](../../../DESIGN.md#one-writer-one-door) ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted)

## Context

The vault is written by several agents and read by a human and agents, across a cluster and two
Apple devices. The obvious architectures — multiple writers with a merge/conflict layer, or two-way
device sync — each require a reconciliation subsystem: conflict resolution, vector clocks or a lock
protocol, a quarantine flow for divergent copies, and a merge authority. An early design draft had
exactly that shape, built around two-way device sync, and the device leg was ungateable in
principle: an edit landing on a synced copy reaches the authoritative bytes with no control point in
between.

## Decision

Exactly one process ever mutates vault content: a headless, in-cluster Obsidian instance. Every
writer is a client of that process through permission-scoped MCP doors — never a second filesystem
writer. Devices are read replicas plus a capture surface; nothing written on a device flows back in
place (see [ADR-0025](../replication/0025-replication-cycle.md)). Exactly three processes mount the
volume at all, on disjoint or read-only slices: headless Obsidian (read-write on content), the lint
pass (read-only, whole-vault visibility), the committer (read-only content, git metadata on its own
volume). Two paths bypass the door, both declared: the GUI exception
([ADR-0002](./0002-gui-exception-dormant-vnc.md)) and operator-triggered disaster-recovery restore —
rare, outside ordinary operation, exercised only at the recovery drill
([D3](../../../ROADMAP.md#group-d--operability)). *"Two paths" superseded by [ADR-0063](../content-model/0063-schema-published-from-this-repository.md), pending its ratification.*

## Alternatives considered

- **Two-way device sync with conflict handling** — deleted, not mitigated. Removing it removed the
  whole reconciliation subsystem and the one genuinely ungateable path.
- **Multiple writers with a merge engine** — rejected everywhere it re-appeared (stale batch
  patches, device drift). No merge engine exists anywhere, and mechanisms are shaped so none creeps
  back in.
- **Filesystem-level write confinement per writer** — superseded with the same early draft;
  per-writer confinement is done with MCP path scopes instead
  ([ADR-0005](./0005-path-scope-granularity.md)).

## Consequences

- Most multi-writer problems dissolve structurally. Three residues survive, each individually
  mechanised: lost updates on read-modify-write (anti-clobber primitives, optimistic concurrency),
  device replica divergence (capture-and-dispatch, [ADR-0025](../replication/0025-replication-cycle.md)),
  and bulk work too large for tool-by-tool traffic (the batch stream,
  [ADR-0022](../work-queue/0022-batch-stream-mechanics.md)).
- Throughput is capped by one single-threaded event loop; a bulk import takes hours, once —
  accepted, and the reason no fast lane exists.
- The invariant is enforced by the Deployment's shape, not the storage layer — see
  [ADR-0033](../platform/0033-volume-and-deployment-shape.md) for the known timing window.
- Anything that would add a fourth mount or a second content writer is a design change to this
  record, not an implementation detail.
