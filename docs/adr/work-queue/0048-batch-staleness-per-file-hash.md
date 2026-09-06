# 0048. Batch staleness is measured per file, by content hash, never against repo head

**Status:** Accepted ·
**Pillar:** [The volume is authoritative; git is derived history; no merge engine anywhere](../../../DESIGN.md#the-volume-is-authoritative-git-is-derived-history-no-merge-engine-anywhere) ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted) ·
**Unit:** [A2](../../../ROADMAP.md#group-a--pipeline-mechanisms) ·
**Ticket:** [ot#5](https://github.com/ppat/obsidian-tools/issues/5)

## Context

A stale patch is rejected back to its producer and never merged
([ADR-0022](./0022-batch-stream-mechanics.md)) — a three-way merge would return the subsystem this
architecture deleted. That leaves the reference open: what "moved" is measured against. The choice
decides which concurrent changes count as a conflict and, therefore, how often a producer is told
to regenerate work it has already done.

Three facts of this system constrain the answer:

- **Git is derived history, not the authority.** The committer runs on a schedule, so repo head
  lags the volume and moves for reasons unrelated to any batch.
- **The check has to be explicit, in the processor.** A chunk reaches the vault as writes through
  the gated MCP path; the only view of current content anything on that path has is what it reads
  back through the same door. Nothing between producer and volume compares a patch's assumptions
  against the vault's bytes unless the processor does it.
- **[ADR-0022](./0022-batch-stream-mechanics.md) chose one FIFO stream so a producer can express
  dependency by ordering** — rename in one chunk, relink in the next. A staleness reference that
  invalidates a batch's later chunks against its own earlier ones does not merely inconvenience
  that guarantee; it retracts it.

## Decision

**Staleness is per file, by content hash, over the paths a chunk touches.** A chunk records, per
target path, the hash of the content its patch was generated against. Before any write in the chunk
lands, `batch-processor` reads each target and compares. Any mismatch rejects the whole chunk to
the producer with nothing applied; all matching applies the chunk, each write carrying that hash as
a write-time precondition wherever the tool surface accepts one — the optimistic-concurrency
primitive Gate 3 names ([the gate table](../../../DESIGN.md#3-the-write-path-end-to-end), and
[FINDINGS-v1](../../FINDINGS-v1-source-review.md) for the REST-level `ifMatch`/`version` pair it
rests on). The pre-flight read establishes the measure; the precondition, where available, closes
the window between checking and writing rather than assuming it away.

Four properties are the point:

- **Orthogonal to commit cadence.** The measure never consults git, so the committer's schedule
  cannot invalidate a patch. Only a change to a file the chunk actually touches can.
- **Whole chunk, before any write.** A chunk is the transaction and redelivery unit
  ([Glossary](../../../DESIGN.md#glossary)); checking per write instead would let a chunk
  half-apply and then be redelivered onto content its own earlier writes moved. Checking first
  makes that a rejection rather than a double-apply.
- **It detects any change to a touched file, whoever made it** — batch, promotion, lint, or a human
  at the GUI exception. Authorship is irrelevant to the comparison.
- **It does not detect a conflict that is semantic and spans files.** A note renamed by one write
  while another file's link to it is rewritten by another: each file individually matches its
  recorded hash, and jointly they are inconsistent. No per-file measure can see this, because the
  property violated is a relation between files rather than a property of any one of them.

FIFO covers exactly half of that residue, and it is worth being precise about which half.
Within one batch, every chunk comes from one producer against one snapshot, and strict FIFO
delivers them in that producer's order — so a pair of interdependent edits is ordered by the only
party that knows they are interdependent, and there is no interleaving at which one applies out of
order relative to the other. The failure mode that remains inside a batch is a batch cut short,
which leaves an edit not yet made rather than a wrong one. What FIFO cannot cover is the other
half: it orders the batch stream against itself and never against another writer. A semantic pair
split between a batch chunk and a writer that is not on the batch stream is undetected here. The
batch window narrows that set — the agent handle is disabled for a run — without emptying it:
`promotion-processor` keeps draining on the shared ingestor handle, the lint pass is scheduled, and
the GUI exception is ungated by construction. That residue is caught downstream and after the fact
by the lint pass, which repairs unambiguous dead links and flags the ambiguous ones
([ADR-0018](../content-model/0018-lint-pass-policy.md)) — detective, the posture this design
already assigns to what prevention cannot reach.

**What would show this wrong.** The choice rests on one empirical claim: that the file sets touched
by different chunks of one batch are largely disjoint. Stale rejections are counted and attributed,
alongside the raw-refusal and backpressure counters [A2](../../../ROADMAP.md#group-a--pipeline-mechanisms)
already carries, and three observations falsify the choice:

| Observation | What it would mean |
| --- | --- |
| Rejections dominated by files an earlier chunk of the *same* batch changed | Per-file hashing self-invalidates too — the disjointness claim is false, and this measure fails on the ground that disqualified repo head |
| Lint-pass dead-link findings rising after batch runs while stale rejections stay near zero | The cross-file residue is material, not theoretical: chunks are passing that should have been stopped |
| A clobber observed between the pre-flight check and the write | The pre-flight read alone is insufficient and the write-time precondition has to be mandatory rather than opportunistic |

## Alternatives considered

- **The patch's base commit against repo head.** Detects strictly more, the cross-file semantic
  case included — this is a trade of detection strength for usability, not a dominated option.
  Rejected because the extra detection is bought almost entirely in false conflicts: head moves on
  a schedule for reasons unrelated to any batch, so a batch spanning a commit interval invalidates
  its own tail. That retracts the dependency-by-ordering guarantee FIFO exists to provide, and
  presents to an operator as "every chunk after the first fails" — indistinguishable from a broken
  processor.
- **A three-way merge.** Not rejected for difficulty: it reintroduces the merge engine the
  architecture deleted, and no staleness reference can be chosen on grounds that make it
  reachable again.
- **Applying chunks unconditionally.** Naming why this is unacceptable is what makes any measure
  meaningful: a patch generated against content that has since changed would overwrite the
  intervening change with no signal — a silent lost update, which is precisely what Gate 3 exists
  to prevent and what [fail loud, destroy nothing](../../../DESIGN.md#fail-loud-destroy-nothing)
  forbids.
- **Quiescing every other writer for the duration of a batch**, so no staleness can arise. Two
  independent refusals: bulk yields to interactive and never the reverse, so promotion must keep
  draining mid-batch; and quiescing the editor is not merely rejected but unavailable, because
  batch writes travel through it ([ADR-0022](./0022-batch-stream-mechanics.md)).

## Consequences

- **A path may be touched by at most one chunk of a batch**, and a producer that emits otherwise is
  malformed rather than merely inefficient. A later chunk records the pre-image hash of a path an
  earlier chunk of its own batch has already moved, so it rejects against a hash the batch itself
  invalidated — which is the first falsifying observation named above, manufactured by the producer
  rather than discovered in production. The constraint binds any producer, so it is stated here
  rather than left to whichever one is written first. It is reachable in ordinary work: a rename
  chain or swap staged together (`A` to `B`, `C` to `A`) writes `A` twice, once as the source of one
  rename and once as the destination of another.

- **Rejection granularity is the chunk.** One moved file rejects every edit in the chunk. This
  keeps the chunk the transaction unit and keeps within-chunk ordering meaningful.
- **A redelivered chunk that had begun to apply fails the check and is rejected rather than
  replayed** — loud and non-destructive, at the cost that recovery from a crash mid-chunk becomes
  producer regeneration. A chunk whose work is *entirely* done does not pay that cost: it is settled
  rather than parked, since acknowledging it leaves the vault exactly where applying it would. That
  needs no post-apply hash for a create, whose post-image its own patch spells out in full; a modify
  computes its post-image from a pre-image its application replaced, so a fully-applied chunk
  carrying one is still rejected, and recording a post-apply hash per target remains the named
  refinement for that half — a change to what is compared, not to the reference this record fixes.
- **The raw layer's rule is untouched and stronger where it applies.** `05-raw/` is create-only,
  enforced in the same processor ([ADR-0015](../content-model/0015-raw-immutability.md)): an
  existing target is a refusal regardless of hash, and a create has no prior content to hash at
  all. The two compose as create → existence check, modify → hash check. The bootstrap import lands
  entirely in the write-once layer, so it is governed by the existence check and never by this one;
  this measure governs the later structural refactors that modify curated content.
- **It cannot become a merge by another name.** The comparison's only outputs are apply-as-given,
  reject-whole-chunk, and — where every path already holds what the patch would produce — write
  nothing at all; no comparison result is ever used to transform a patch. Any future use of the
  recorded hash to reconcile rather than to gate is the deleted subsystem returning under a new
  name.
