# 0007. One shared admission validator, three callers, fired at every curated-boundary crossing

**Status:** Proposed — adopted by the top-level documents, awaiting owner ratification ·
**Serves:** [S2](../../../USE_CASES.md#s2--sound) ·
**Unit:** [A4](../../../ROADMAP.md#group-a--pipeline-mechanisms) ·
**Ticket:** [ot#6](https://github.com/ppat/obsidian-tools/issues/6)

## Context

Two texts disagreed about where validation lives. The pre-restructure design document's prose
described a scheduled-validator shape left over from a retired nightly worker, and never said
whether the check was shared; [ot#6](https://github.com/ppat/obsidian-tools/issues/6) — the newer
text, corrected the day after the work-queue redesign — states it plainly: one admission logic,
three callers. The owner posed the open question directly: where do validate/lint/digest kick in —
on arrival in the inbox and the raw layer? at promotion only? what about edits after promotion, and
the different arrival routes?

## Decision

**The admission validator is one shared piece of logic with three callers**
(`promotion-processor`, `batch-processor`, the lint pass), and it fires **at every write that
crosses the curated boundary, regardless of route or caller** — first arrival and every later edit
alike, since only ingestor-handle holders can touch curated space and all of them call it. Strength
differs by caller and domain: a hard mechanical block where the check is presence-based (the
finance overlay), flag-only where detection is judgment. Around that boundary:

| Zone | Posture |
| --- | --- |
| Agent zone (inbox, journal, agent scratch, log) | **Detective only** — agents must capture freely; the boundary is what is defended; the lint pass watches |
| Raw layer | **Exempt entirely** — immutable, unvalidated by design ([ADR-0015](../content-model/0015-raw-immutability.md)) |
| Curated space | **Preventive** — nothing enters or changes except through the validator |
| Direct agent writes | No admission question arises — containment (Gate 2) already denies them the curated boundary |

Resolving lint findings — the vault's own agentic workflow, run inside the lint pass
([ADR-0054](../content-model/0054-vault-owned-agentic-workflow.md)) — is downstream of the lint
pass's *output*, not a gate on content arrival; a resolution that writes curated space crosses the
boundary as the lint pass, one of the three callers, so no fourth caller arises.

## Alternatives considered (scored in the original enforcement analysis)

- **A validating proxy in front of the write path** — cannot compute post-write state for
  append/patch without applying them; true in-flight blocking is unachievable and claiming it would
  overclaim.
- **Write-to-inbox-and-promote with no path scoping** — "the agent zone" is not a boundary without
  path scope.
- **Prompt discipline alone** — instructions to a model are suggestions, not rules.
- **Per-caller validators** — under that reading, a batch chunk entering curated space passes
  through *no* admission gate at all while wielding the widest handle in the system; that hole is
  what the shared reading closes. This is also why
  [ot#6](https://github.com/ppat/obsidian-tools/issues/6)'s dependency line points *into* the
  validator: its callers depend on it, never the reverse.

## Consequences

- Failure behaviour is uniform: quarantine with a machine-readable reason, counted, never deleted.
- Drift intentionality stays a separate, upstream authority
  ([ADR-0008](./0008-drift-classification-separate-authority.md)) — one authority per question, not
  one authority total.
- Until the owner ratifies, this is the working answer the roadmap builds
  [A4](../../../ROADMAP.md#group-a--pipeline-mechanisms) against; ratification (or overrule) is an
  [open decision](../../../ROADMAP.md#open-decisions).
