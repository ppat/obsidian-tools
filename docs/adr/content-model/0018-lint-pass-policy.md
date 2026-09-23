# 0018. The lint pass's policy: normalisation in-pass and in-code, a mechanical/judgment auto-fix boundary, and judgment findings resolved by the vault's agentic workflow, never pushed to the owner

**Status:** Accepted ·
**Serves:** [S2](../../../USE_CASES.md#s2--sound) ·
**Unit:** [A5](../../../ROADMAP.md#group-a--pipeline-mechanisms) ·
**Ticket:** [ot#6](https://github.com/ppat/obsidian-tools/issues/6)

## Context

Vault rot — drift, staleness, broken structure — is the community pattern's single biggest failure
mode at scale, and the maintenance loop is described there as not optional. Three policy questions
shape it: when normalisation runs, what may be fixed automatically, and who resolves the findings
that need judgment.

## Decision

- **Normalisation runs in the scheduled pass, never on save — and in the pass's own code, not
  in-app plugins.** On-save normalisation would let a plugin silently reshape frontmatter *after*
  the validator approved it, giving frontmatter shape two owners; and the plugins that would have
  done it were dropped ([ADR-0017](./0017-plugin-set-tasks-dataview.md)), which makes the
  one-authority rule structural rather than dependent on plugin configuration. Missing required
  fields are filled, existing values never overwritten, every change logged — overwriting a value a
  human set is the fastest way to lose trust in the whole pass.
- **The auto-fix boundary is drawn at judgment about meaning, not at ease of automation.**
  Mechanical breakage is fixed (key order, ISO dates, lowercase tags, banned characters, `updated:`
  stamping, unambiguous dead links); anything requiring a judgment is never fixed in code (contradictions,
  stale claims — via `reviewed:` age and the `refs:` staleness graph — orphan disposition,
  near-duplicate merges, broken queries, a filename stem no longer matching its slug,
  `trigger:`/`authority:` contradictions — which field is wrong is itself a judgment): it is handed
  to the vault's own agentic workflow ([ADR-0054](./0054-vault-owned-agentic-workflow.md)), except promotion, which stays the promotion path's.
  Deterministic checks run in code; contradiction and stale-claim detection is judgment and runs in
  that workflow, against whatever model endpoint the installation provides.
- **Every normalisation change is logged to the audit trail** (`_ops/audit/`) — the third
  granularity of history beside git and the append-only log.
- **Findings surface in two tiers** — the full report in `_ops/lint/` (plus a one-line append to
  the log) and metrics — and **nothing is pushed to the owner.** Judgment findings are resolved by
  the vault's own agentic workflow through the pass's own gated write path, every change validated
  at the curated boundary and logged to the audit trail; a finding it cannot resolve stays recorded
  in the report. The review loop closes with agents, not with the owner.

## Alternatives considered

On-save normalisation via plugins (two owners of shape; no vault-wide command existed anyway);
uncapped reports (unread digests are the pattern's known death); auto-fixing content claims *in
code* (fail-loud posture forbids it — judgment belongs to the agentic workflow, whose writes are
gated and audited); a capped review digest pushed to the owner for approve/skip replies (rejected
by owner ruling: nothing is pushed to the owner, and resolving findings is agents' work); routing
findings to a client agent (rejected: the vault does not rely on outside agents for its own job).

## Consequences

The pass is also the only observer of GUI-exception writes
([ADR-0002](../write-model/0002-gui-exception-dormant-vnc.md)), and the natural emitter for the
vault-derived metrics riding [A5](../../../ROADMAP.md#group-a--pipeline-mechanisms) — a second
whole-vault reader would be two components disagreeing about the same numbers. Accepted cost of
in-pass normalisation: a note created directly in the GUI carries no `created:` until the next
pass fills it.
