# 0009. Provenance is three fields: `source:` / `authority:` / `trigger:`

**Status:** Accepted — `source:`'s verifiability superseded by [ADR-0058](./0058-source-values-open-for-outside-writers.md) (Proposed), marked where it stands ·
**Pillar:** [Provenance is three questions](../../../DESIGN.md#provenance-is-three-questions-and-self-report-never-unlocks-a-gate) ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted), [S2](../../../USE_CASES.md#s2--sound)

## Context

The original schema had one `source:` field whose enum mixed three unrelated things: process names
(`openclaw`, `n8n`, `claude-code`), claim-authority values (`human`, `import`), and a channel
(`home-assistant`) that is never mechanically the writer at all — its LLM hands off to OpenClaw. One
field carrying two questions forced paragraph-long reconciliations and made the field unverifiable.

## Decision

Three fields, one question each:

| Field | Question | Verifiability |
| --- | --- | --- |
| `source:` | Which process performed the write | Mechanical — the handle and key identify the caller |
| `authority:` | Whose claim the content is (`human`/`agent`/`import`) | Self-reported — trusted, never privileged |
| `trigger:` | What caused the write (`human`/`schedule`/`event`) | Mechanical — the invoker knows which applies |

*The `source:` row's verifiability superseded by [ADR-0058](./0058-source-values-open-for-outside-writers.md), pending its ratification.*

`trigger:` earns a field (not documentation) because it makes `authority:`'s trust *checkable*:
`trigger: schedule` with `authority: human` is a mechanical contradiction — a cron job cannot be
transcribing something a person just said — flagged by the lint pass in a few lines, vault-wide.
That check is also what catches a device edit landing on a note still stamped `authority: agent`,
which is the failure shape the human write path actually produces. Two precisions that go wrong in
a month if unstated: review does not transfer authority (`reviewed:` set by a human leaves the
claim the agent's), and `confidence:` and `authority:` are orthogonal, never merged. Relocation
rewrites none of the three — `source:` records who authored the content, not who last moved the
file, which is why `promotion-processor` carries no enum value (a flagged judgement call the owner
may reverse).

## Alternatives considered

- The single overloaded field — scored and rejected: each of the split fields means exactly what its
  name says, where the overload needed a reconciliation paragraph.
- A static lookup table mapping `source:` to allowed `trigger:` values — the same unenforced-
  convention shape the design rejects everywhere else; stamping the fact where it is known for free
  beats maintaining a table that drifts.

## Consequences

Two more required frontmatter fields on every note, bought back as a mechanically checkable
integrity constraint on the one field every anti-fabrication rule depends on. The gate implications
of `authority:` being self-reported are their own record
([ADR-0010](./0010-authority-human-loses-privilege.md)).
