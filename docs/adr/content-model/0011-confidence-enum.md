# 0011. `confidence:` is a three-level enum — `stated` removed, no float added

**Status:** Accepted ·
**Serves:** [S2](../../../USE_CASES.md#s2--sound)

## Context

The inherited enum was `stated|high|medium|speculation`. `stated` was never a confidence level — it
meant "this is what the source says", a provenance claim, and provenance has three fields of its
own. Separately, a numeric confidence (0–1 float) was considered, and the schema rule that one
field means one type vault-wide makes that a now-or-never choice.

## Decision

`confidence:` is purely epistemic: `high|medium|speculation`. "What the source says" lives in
`authority: import`, or in the sentence carrying an inline citation. No float: no vault, plugin or
documented markdown-KB schema using numeric confidence could be found; LLM verbalised numeric
confidence is badly calibrated (systematically overconfident, round-number clustering, degraded
further by RLHF relative to raw token probabilities); and where coarse-versus-fine has been tested,
coarse does as well or better. A single-shot `0.83` carries no more information than `high` — it
only looks like it does. Two tradecraft conventions draw the same axis line the removal of `stated`
draws: the Admiralty Code grades source reliability and information credibility independently, and
ICD 203 forbids combining a confidence term with a likelihood term in one expression.

## Alternatives considered

- Keeping `stated` — braids provenance into an epistemic field.
- A float — rejected on the calibration evidence above; revisit only when a scoring algorithm
  exists to define what the number would be calibrated against.

## Consequences

Coarse loses granularity that can never be recaptured for old notes — accepted knowingly under the
one-field-one-type rule. `confidence:` and `salience:` are elicited in separate model calls so two
ratings do not anchor on each other ([ADR-0012](./0012-salience-consolidated-fields.md)).
