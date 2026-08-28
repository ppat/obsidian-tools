# 0018. The lint pass's policy: normalisation in-pass and in-code, a mechanical/judgment auto-fix boundary, and the capped review digest

**Status:** Accepted ·
**Serves:** [S2](../../../USE_CASES.md#s2--sound) ·
**Unit:** [A5](../../../ROADMAP.md#group-a--pipeline-mechanisms) ·
**Ticket:** [ot#6](https://github.com/ppat/obsidian-tools/issues/6)

## Context

Vault rot — drift, staleness, broken structure — is the community pattern's single biggest failure
mode at scale, and the maintenance loop is described there as not optional. Three policy questions
shape it: when normalisation runs, what may be fixed automatically, and how findings reach a human
who lives on a phone.

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
  stamping, unambiguous dead links); anything requiring a judgment is flagged only (contradictions,
  stale claims, orphan disposition, near-duplicate merges, `trigger:`/`authority:` contradictions —
  which field is wrong is itself a judgment — and any promotion).
- **Findings surface in three tiers**: the full report in `_ops/lint/`; the **review digest** —
  ranked by damage (not recency), hard-capped at roughly seven items, pushed over WhatsApp with
  actionable replies (approve/skip/explain) that flow back through the ordinary gated path; and
  metrics. The cap and the push are the phone constraint taken seriously: the review loop closes
  with zero new components, arriving where the human already is, which is what makes the mandatory
  review gate something that actually happens.

## Alternatives considered

On-save normalisation via plugins (two owners of shape; no vault-wide command existed anyway);
uncapped reports (unread digests are the pattern's known death); auto-fixing content claims
(fail-loud posture forbids it).

## Consequences

The pass is also the only observer of GUI-exception writes
([ADR-0002](../write-model/0002-gui-exception-dormant-vnc.md)), and the natural emitter for the
vault-derived metrics riding [A5](../../../ROADMAP.md#group-a--pipeline-mechanisms) — a second
whole-vault reader would be two components disagreeing about the same numbers. Accepted cost of
in-pass normalisation: a note created directly in the GUI carries no `created:` until the next
pass fills it.
