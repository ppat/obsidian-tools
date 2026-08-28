# 0026. What a spool entry records, and the one inference `matches_upstream: true` licenses

**Status:** Accepted ·
**Serves:** [W6](../../../USE_CASES.md#axis-2--writers-connected) ·
**Ticket:** [ot#4](https://github.com/ppat/obsidian-tools/issues/4)

## Context

A cycle that dies between publish and tag advance leaves the next cycle re-reading the system's own
published content as device drift ([ADR-0025](./0025-replication-cycle.md)). The device cannot
detect that state — nothing in the tree records whether the previous publish ran, and durable
state between cycles is exactly the mechanism already ruled out (a crash strands the marker in the
state it exists to describe). So the device records what only it can observe, and the verdict
belongs to the consumer holding upstream history.

## Decision

Each entry carries, beside the patch: `baseline_sha` (the tag the overlay was diffed against),
`upstream_sha` (the revision known *before* this cycle's fetch — measuring against the about-to-be-
fetched revision would report a human's edit as upstream content whenever an agent happened to
write the same text in between), and `matches_upstream` (whether every named path is byte-identical
to that revision's tree; **null** when no upstream revision was known — not determinable and
determined-to-differ must not be spelled the same way). These are observations, never judgements.

**`matches_upstream: true` licenses exactly one inference: these bytes are already upstream, so
admitting the edit adds nothing new.** It is *not* evidence of crash residue: residue needs three
facts (published, died before tag advance, path was in the publish) the entry evidences none of.
Nor does `baseline_sha != upstream_sha` rescue that reading — the fetch runs unconditionally while
publish is gated, so an ordinary *withheld* cycle (a pasted binary — the common case) produces that
state far more often than a crash does. The first real spool entry ever produced settled the
tempting misreading empirically: identical shas with `matches_upstream: false` — the field asserts
**content equality at the path**, not revision equality.

## Alternatives considered

- A field naming the previous cycle as incomplete — deliberately not recorded: the shas already say
  that much, and saying it once more, one interpretation further along, only makes the wrong
  reading easier to reach.
- Letting the consumer refetch and reconstruct — impossible: these are facts only observable at the
  one moment the device tree, its baseline and the known upstream coexist.

## Consequences

A classifier reading `true` as "already upstream, discard" is wrong about **deletions first** — a
modification needs a byte-for-byte coincidence (rare), a deletion only a *path* coincidence, which
an agent archiving upstream produces easily — and what it discards is a genuine human deletion. The
error runs the unsafe way, which is why the reconcile-against-history obligation sits on
`drift-processor` and the fields are inputs, never an answer.
