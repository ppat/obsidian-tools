# Design canon and research inputs

This directory holds the research record behind BRAIN's design, the decision records, the
verification catalogue, and the operational runbooks. **The design of record lives at the
repository root**: [`DESIGN.md`](../DESIGN.md) (the pillars and invariants, with the settled
Glossary), [`USE_CASES.md`](../USE_CASES.md) (the outcomes and their acceptance criteria), and
[`ROADMAP.md`](../ROADMAP.md) (the work, its state, and the mapping between the two). The three
documents that previously carried the design are retired; their content lives in that triad, in the
decision records, and in the verification catalogue.

## Reading order and authority

| Document | Role | Authority |
| --- | --- | --- |
| [`../DESIGN.md`](../DESIGN.md) + [`adr/`](./adr/README.md) | The design: invariants in the design document, every reversible decision as a record | **Current design of record.** Supersedes the canon documents wherever they conflict |
| [`VERIFICATIONS.md`](./VERIFICATIONS.md) | Every control's proving injection and answerable-by-doing check, past and pending | The acceptance layer; status per row |
| [`FINDINGS-v1-source-review.md`](./FINDINGS-v1-source-review.md) | A primary-source review checking CANON-1/CANON-2 against upstream documentation and repositories | Corrects specific canon claims; superseded in turn by anything the design decides explicitly |
| [`CANON-1-designing-brain.md`](./CANON-1-designing-brain.md) | Research input: community practice for vaults written and organised by multiple agents | Research input, not current guidance. Read through the design and FINDINGS, never standalone |
| [`CANON-2-mcp-research.md`](./CANON-2-mcp-research.md) | Research input: selecting an Obsidian MCP server | Research input, not current guidance. Read through the design and FINDINGS, never standalone |

The dependency runs one way: the design drew on `FINDINGS-v1`, which was written to check claims in
`CANON-1` and `CANON-2`. Nothing later in that chain is overridden by something earlier in it. The
canon documents are kept unedited as a historical record of what was researched (their internal
links are maintained mechanically when files move, nothing more); a canon claim that FINDINGS
corrects, or that the design decides differently, is superseded.

## Also in this directory, and not canon

[`settings-lock.md`](./settings-lock.md) is a one-time operational procedure, not design:
the checklist a human works through at the headless Obsidian instance's GUI before any vault
content exists. It lives here rather than in the vault because it is executed once and is
then history, whereas the vault holds durable, regenerable content. It is copied into the
vault root transiently for the duration of the GUI session and removed afterwards; the file
itself explains that. It carries no authority over `DESIGN.md` and nothing in the table
above defers to it.

[`gui-access.md`](./gui-access.md) is likewise operational, not design: the runbook for
reaching that same GUI (P8) at all — connection steps, and the hazards hit the first time
it was actually used. It carries no authority over `DESIGN.md` either, and nothing in the
table above defers to it.

[`local-replicator.md`](./local-replicator.md) is the third document in this operational
tier: install/uninstall instructions and operator prerequisites for `local-replicator`, the
one component in the whole design that runs outside the cluster (a launchd job on the
operator's Mac, not a Flux-managed workload). Unlike `settings-lock.md`, it isn't a one-time
procedure executed once and then history — installing, uninstalling and resetting the
device `.obsidian/` baseline are all things an operator does again, on demand. It carries no
authority over `DESIGN.md` either.

## What FINDINGS-v1 corrects

`FINDINGS-v1-source-review.md` § "Corrections to canon" lists, with severity, every claim in CANON-1/CANON-2 it
found to be wrong or outdated against primary sources, including:

- **CANON-1's claim that a Claude Code `PostToolUse` hook blocks a write that fails validation is false** — the
  tool has already run by the time that hook fires; only `PreToolUse` can block. This is the highest-severity
  correction. The design goes further than the correction: any runner hook belongs to a client the vault system
  does not know, so no control of the vault's rests on one, and prevention lives at the curated boundary
  ([ADR-0007](./adr/write-model/0007-validation-placement.md)).
- CANON-2's claim that `shanehull/obsidian-remote` published no pullable image was outdated — a pullable image
  now exists — though the design sets the project aside anyway, on different and stronger grounds (no GUI, own
  bundled MCP server; [ADR-0041](./adr/platform/0041-mcp-stack-selection.md)).
- A discrepancy (not fully resolved) in CANON-2's claimed last-updated date for `StevenStavrakis/obsidian-mcp`
  against GitHub's own `pushed_at` timestamp.

Treat any claim from CANON-1 or CANON-2 that isn't repeated or endorsed by the current design as
unverified at best, and check `FINDINGS-v1` before relying on it.
