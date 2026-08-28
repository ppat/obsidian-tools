# 0042. The adopted community pattern: an LLM-maintained wiki with three operations — plus the regeneration-safety conventions

**Status:** Accepted ·
**Serves:** [S2](../../../USE_CASES.md#s2--sound), [S3](../../../USE_CASES.md#s3--placed)

## Context

The platform did not invent its content model. The founding research identified a dominant,
well-attested pattern for exactly this use case — an agent-maintained wiki built from an immutable
raw layer, an agent-owned working layer, and a schema file, operated through three verbs —
**ingest, query, lint** — with practitioners at thousands of concepts reporting that drift is the
dominant failure mode and the lint pass is not optional. Folder-taxonomy debates (PARA, Johnny
Decimal, LYT, Zettelkasten) were found to be cosmetic next to five load-bearing principles.

## Decision

Adopt the pattern's principles, not any folder religion: a strict **ownership contract** per layer;
**enumerable, stable locations** encoded in the schema file; **atomicity** (atomic notes retrieve
better and bound an agent edit's blast radius); **machine-readable structure** (typed frontmatter —
the make-or-break variable at write volume); and a **promotion path** from raw toward curated. Two
navigation layers coexist deliberately: a hand-curated home note (human intent) and agent-maintained
indexes. Two conventions specifically protect content across regeneration, and the definition-of-done
gate's round-trip test exists to prove them by firing
([obsidian-vault#3](https://github.com/ppat/obsidian-vault/issues/3)):

- **Sentinel markers**: generated content lives inside marked blocks; human-marked blocks and
  anything outside markers survive a regeneration untouched — additive-only editing, never
  delete-or-merge by regeneration.
- **A page-level lease**, held only by regeneration and bulk jobs — leases exist for whole-page
  rewrites only; ordinary writes rely on the commuting primitives and optimistic concurrency
  instead. No general lock protocol exists (deleted with the multi-writer subsystem,
  [ADR-0001](../write-model/0001-single-writer-one-door.md)).

One more adopted mechanism: **`refs:` as directed dependencies** (distinct from associative
`related:`) supports a computed dependency graph for *push-based staleness* — when a source
changes, the notes depending on it are queryable as stale, rather than waiting for age thresholds.

## Alternatives considered

A PKM folder system adopted wholesale (the principles survive; the taxonomies are downstream);
skipping the lint verb (the pattern's own practitioners: not optional); free-form frontmatter
(the #1 reported cause of broken queries and unreliable retrieval). The pattern's write-time
*blocking* hook, as originally described, turned out not to exist on this runner — the available
hook fires after the write — which is why prevention lives at the curated boundary instead
([ADR-0007](../write-model/0007-validation-placement.md)).

## Consequences

The layered vault independently converged on the four-tier shape (raw → staging → wiki → schema)
other practitioners report as what scales — corroboration, not justification. The adopted
thresholds travel with the pattern: sustained inbox depth ~20 as the automation tripwire, and a
flat index no longer fitting in context as the retrieval threshold
([ADR-0038](./0038-search-and-dedup-not-built.md)).
