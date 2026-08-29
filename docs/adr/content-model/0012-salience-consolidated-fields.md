# 0012. `salience:` (integer 1–10) and `consolidated:` (date) exist for the roll-up pass — with a scheduled audit that can remove one of them

**Status:** Accepted ·
**Serves:** [S3](../../../USE_CASES.md#s3--placed) ·
**Unit:** [A8](../../../ROADMAP.md#group-a--pipeline-mechanisms)

## Context

The platform's purpose includes important ideas bubbling up for agents to work on
([USE_CASES](../../../USE_CASES.md#the-system-in-one-paragraph)). An automated consolidation pass
needs two inputs nothing else stores: a relevance ordering (the future query is unknown at write
time), and a way to tell a note it already folded from one it has not.

## Decision

- **`salience:` — integer 1–10, absent until scored.** An admitted invention: no Obsidian
  convention, PARA/Zettelkasten/LYT practice, or published vault defines an equivalent (checked,
  not assumed). Named to avoid `priority` and `weight`, both typed inconsistently in the ecosystem
  with inverted polarity in places. 1–10 because rank-correlation against human judgement peaked
  there (stars .339, 1–5 float .413, **1–10 .428**, 1–100 .383); a float mostly creates ties; a
  coarser enum destroys an ordering that cannot be recaptured later. Values are **never thresholded
  absolutely** — the pass min-max normalises within each candidate batch, so miscalibration is
  harmless and the failure that matters is discrimination collapse, which the pole-anchored rubric
  defends against. Recomputable (only when `updated:` moved), elicited in its own model call.
- **`consolidated:` — a date, not a boolean.** Comparing against `updated:` makes a re-edited note
  eligible again automatically; `true` would strand a note after its first fold. Not part of
  `status:` (a note can be evergreen *and* consolidated); distinct from `reviewed:` (different
  actors).
- **The audit, kept from the strongest counter-argument:** field studies of two-axis rating show
  ~87% of ratings collapse onto the diagonal despite instructions to rate independently — so
  `salience:` may end up tracking `confidence:` and cost a model call for a column the vault
  already had. **At roughly 200 notes, measure their correlation; if they track, `salience:` is
  removed.** Recorded as an [open decision](../../../ROADMAP.md#open-decisions) so it happens
  rather than being remembered.

## Alternatives considered

Floats and coarser enums (above); deriving salience from existing fields (nothing stored encodes
"will this matter later"); `priority`/`weight` naming (ecosystem collision).

## Consequences

Treated as a coarse ordering within one batch it earns its place; treated as a measurement it
misleads — no study validates LLM importance ratings for memory items against human judgement, and
human inter-annotator agreement on the construct is only moderate. Neither field is ever written
empty to hold a slot: an empty value is indistinguishable from a real low one.
