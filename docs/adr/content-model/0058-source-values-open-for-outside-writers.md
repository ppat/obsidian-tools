# 0058. `source:` is stamped by the vault system's own authoring components and declared by outside writers

**Status:** Proposed (supersedes `source:`'s verifiability in [ADR-0009](./0009-three-field-provenance-split.md)) ·
**Pillar:** [Clients are known by credential, never by name](../../../DESIGN.md#clients-are-known-by-credential-never-by-name) ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted), [S2](../../../USE_CASES.md#s2--sound)

## Context

`source:` answers which process performed a write ([ADR-0009](./0009-three-field-provenance-split.md)).
Its vocabulary lives in the vault's schema file, and this package's shared schema core mirrors it.

A vocabulary that lists named outside clients alongside the vault system's own components is the
vault enumerating its clients. Two things follow. Every new client becomes a schema change, and the
schema is owned by the human ([ADR-0039](./0039-schema-file-and-agents-pointer.md)). And a client
missing from the list has its writes flagged for being unknown rather than for being wrong.

Nothing in the write path writes `source:` for a caller. An outside writer's value is whatever that
writer puts in its frontmatter. So "the handle and key identify the caller" is true of the
credential, not of the field.

## Decision

**The field has two regimes.**

| Who writes | `source:` | How far it can be trusted |
| --- | --- | --- |
| A vault component that *creates* a note — the lint pass's own reports and records | A reserved value, stamped by that component's own code | **Mechanical** |
| An outside writer | An open value the writer declares: a lowercase slug, by convention the name its credential was issued under | **Declared**. It is exactly as trustworthy as whoever declared it. Mechanical attribution for these writes is the access record ([ADR-0059](../write-model/0059-one-holder-per-credential-access-record.md)) |

The validator and the lint pass check an outside value's form, never whether it is in a list of
clients.

**A block a component appends to someone else's note is attributed by its sentinel marker, which
names the component, and never by the note's `source:`**. A roll-up merge's verbatim-copy block is
the case in point: its marker carries the source's own provenance
([ADR-0060](../work-queue/0060-roll-up-pass-owned-by-promotion-processor.md)).
Note-level fields describe the note's authored content and stay its author's.

**Components that only relocate or apply content stamp nothing.** Promotion moves notes, and
`batch-processor` applies patches someone else authored. `source:` records who authored the content,
not who moved or applied it ([ADR-0009](./0009-three-field-provenance-split.md)).

No gate keys on `source:`, which matches the posture [ADR-0010](./0010-authority-human-loses-privilege.md)
gives `authority:`.

## Alternatives considered

- **A closed roster of clients** — ruled out by the owner's standing ruling that the vault system
  does not know its clients.
- **One generic value for every outside writer** — the field would stop answering its own question.
  Which process wrote a note becomes unanswerable exactly where more than one client writes.
- **Stamping every value inside the write path** — a component rewriting frontmatter between the
  caller and the editor. That is the validating proxy rejected for the write path
  ([ADR-0007](../write-model/0007-validation-placement.md)).

## Consequences

- **The schema file's vocabulary changes, and that change is the owner's to make** (ADR-0039). The
  package's schema core follows it.
- **No existing note needs rewriting.** A `source:` value naming a client is valid under the open
  form.
- **An outside writer can declare a reserved value or another writer's value.** Nothing in the note
  shows it; comparing the note with the access record for its write does.
