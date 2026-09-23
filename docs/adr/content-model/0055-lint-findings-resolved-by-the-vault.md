# 0055. The vault system resolves its own lint findings; nothing is pushed to a human, and no finding waits on one

**Status:** Proposed (supersedes the judgement boundary and the review digest in [ADR-0018](./0018-lint-pass-policy.md)) ·
**Pillar:** [The vault system does its own job](../../../DESIGN.md#the-vault-system-does-its-own-job) ·
**Serves:** [S2](../../../USE_CASES.md#s2--sound) ·
**Unit:** [A5](../../../ROADMAP.md#group-a--pipeline-mechanisms) ·
**Tickets:** [ot#83](https://github.com/ppat/obsidian-tools/issues/83), [ot#177](https://github.com/ppat/obsidian-tools/issues/177)

## Context

The lint pass finds two sorts of problem ([ADR-0018](./0018-lint-pass-policy.md)):

- **Mechanical breakage**, which it fixes in code.
- **Judgement calls**: contradictions, stale claims, orphans, near-duplicates, ambiguous links,
  inconsistent provenance, and notes the admission validator has quarantined.

Two standing owner rulings bound who may decide a judgement call:

- The vault system does not rely on outside agents to do its job.
- Nothing is pushed to the owner, and nothing waits on him to resolve it.

A finding that only a human can close therefore never closes. A finding handed to a client rests
on a party the vault system does not know. And a finding that is recorded and then left is a sink,
not a resolution.

## Decision

**Every judged finding is resolved by the vault system itself. When it cannot be resolved yet, it
is recorded with its reason and retried by the system, never sent anywhere.**

- **The agent runtime judges** ([ADR-0054](../platform/0054-agent-runtime.md)). Each finding goes to
  a typed task, whose verdict is either one resolution from the finding's closed set below, or
  *unresolved* with a reason.
- **The component that owns the action applies it, under its own authority.**
  - The lint pass edits content in place through its ingestor handle, and every curated post-image
    goes through the admission validator ([ADR-0007](../write-model/0007-validation-placement.md)).
  - Retirement and merging belong to the roll-up pass
    ([ADR-0060](../work-queue/0060-roll-up-pass-owned-by-promotion-processor.md)). It reads the
    findings handed off to it from the lint pass's report.
- **A resolution never raises `confidence:` or `authority:`, and it destroys nothing.** It may:
  lower `confidence:`; move `authority:` to `agent`; annotate; stamp the vault's own `verified:`, and
  then only with its evidence recorded ([ADR-0061](./0061-freshness-is-the-vaults-own-verdict.md)); repair a link, a query or a
  vocabulary value; or hand a note on. It never rewrites or deletes the text of a claim, and never
  touches text that the sentinel markers reserve for a human
  ([ADR-0042](./0042-adopted-community-pattern.md)). Every resolution goes to the audit trail
  (`_ops/audit/`), and git keeps what came before.
- **The evidence decides, not what the note says about itself.** Contradiction and staleness
  resolutions apply whatever `authority:` the note declares. A self-reported `authority: human`
  unlocks nothing ([ADR-0010](./0010-authority-human-loses-privilege.md)).

| Finding | Resolutions the runtime may choose |
| --- | --- |
| Contradiction between notes | Annotate each note with a generated, sentinel-marked block naming the other note and the verdict; lower `confidence:` on the note the evidence supports less |
| Stale claim ([ADR-0061](./0061-freshness-is-the-vaults-own-verdict.md)) | Supported, with recorded evidence: stamp `verified:`. Contradicted: annotate and lower `confidence:`. A failure on the fetcher's side, or a transient one at the source: retried, nothing decays. No cited source, or a source dead across the area's dial: lower `confidence:` one step and annotate the reason |
| `trigger:`/`authority:` mismatch | Set `authority:` to `agent` where the runtime judges `human` unsupported by the note's history and the access record. Never set `human` or `import` |
| Dead link with more than one candidate target, a bare link to a stem more than one note carries, or a link to a merged source now in the archive | Repair it to the target the runtime judges intended — for a merged source, its survivor — qualified by path |
| Broken query | Repair it where the runtime judges the intended query unambiguous |
| A vocabulary value the schema lacks — and no release migration covers ([ADR-0063](./0063-schema-published-from-this-repository.md)); a renamed value is migrated, never repaired | Outside the trust fields: map it to the nearest value the schema has, keeping the original in an annotation and the audit trail. In a trust field (`authority:`, `confidence:`): set the lowest-trust value (`agent`, `speculation`), never the nearest, so repair can never raise trust. A value that should exist becomes a schema change, authored and released like any other ([ADR-0063](./0063-schema-published-from-this-repository.md)); nothing waits for it |
| A note in quarantine | Re-validate it whenever it or the schema changes. Otherwise, repair what makes it fail, where a fix in this table's sets applies — a missing required field derivable from the note, a vocabulary value. The lint pass records a note that then passes as re-admissible, and `promotion-processor` — relocation being its one job — moves it out of quarantine into its curated home through the validator ([ADR-0056](../work-queue/0056-inbox-is-promotions-work-list.md)). A note whose failure nothing in the sets can repair — a finance figure with no source — stays refused and counted: refusing it is the gate working, and it is re-validated whenever it changes |
| Orphan, near-duplicate | Hand the note to the roll-up pass, which judges retirement or a merge |
| A filename stem no longer matching `slug(title)` | Add `slug(title)` to `aliases:`, so links written to the current title resolve. The stem itself changes only through a rename, which is owner-authorised batch work ([ADR-0013](./0013-filename-slug-page-exists.md)) |

- **Resolutions are idempotent.** Each resolution is recorded against the finding's class, and the
  content hashes of the note and any counterpart, taken *after* the resolution's own writes. A
  finding whose key matches a recorded resolution is neither re-judged nor re-applied. Only a change
  made by something other than the lint pass invalidates the record, so a resolution never ratchets
  on its own output.
- **Unresolved is a retried state, not a queue.** A finding stays in the report under `_ops/lint/`
  with its reason, and it is judged again:
  - on the next pass, if the reason was that the runtime could not be reached;
  - otherwise, when its note, a note it depends on, or the schema changes.

  Nobody is asked about it.
- **The report, the audit trail and the metrics are the lint pass's whole output.** There is no
  digest, no push and no reply channel.

## Alternatives considered

- **A capped digest pushed to the owner, with replies acted on** — this makes the owner a work
  queue, and every finding he ignores stays open forever.
- **Handing findings to a client agent to resolve** — this rests resolution on a party the vault
  system does not know.
- **Flagging only, with nothing resolving the findings** — findings accumulate and the vault rots.
- **Resolutions that rewrite claims or raise trust** — the runtime would author or promote content
  on its own judgement.
- **Sparing notes that declare `authority: human`** — any writer could shield its claims by
  declaring that value.

## Consequences

- **What [ADR-0018](./0018-lint-pass-policy.md) keeps:** normalisation, the mechanical auto-fix
  side of its boundary, the audit trail, the report and the metrics. Its capped review digest is
  withdrawn.
- **A wrong verdict is bounded and reversible by the vault itself.** At worst it lowers confidence,
  adds an annotation, or repoints a link or a value — each audited, and each re-judged when the
  evidence changes. A retirement is reversed by the roll-up pass on new evidence
  ([ADR-0060](../work-queue/0060-roll-up-pass-owned-by-promotion-processor.md)).
- **The loop's health is falsifiable.** [S2](../../../USE_CASES.md#s2--sound) fails if the count
  of unresolved findings or quarantined notes grows across consecutive passes while the runtime is
  reachable and no new content arrives. It also fails if a finding goes un-retried after its note
  changes. Unresolved-finding age and count are emitted and queryable, and sent to no one.
- **With the model endpoint unreachable**, every judged finding is recorded as unresolved and
  retried on the next pass, while the mechanical half proceeds. The pass holds the endpoint's
  credential for its in-process runtime.
