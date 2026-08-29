# 0038. Search at scale and near-duplicate detection are deliberately not built

**Status:** Accepted ·
**Serves:** [S4](../../../USE_CASES.md#s4--retrievable), [S2](../../../USE_CASES.md#s2--sound)

## Context

Retrieval currently works from a flat index that fits in an agent's context, and duplicate
prevention rests on the filename rule and the page-exists check
([ADR-0013](./0013-filename-slug-page-exists.md)). Both have obvious "more infrastructure"
answers — hybrid retrieval, embedding pipelines — that are tempting to pre-build.

## Decision

Neither is built, and both carry a **named threshold and a named technique** so the day they are
needed nothing is rediscovered from scratch:

| Capability | Build when | Technique named |
| --- | --- | --- |
| Search at scale | A flat index file stops fitting in context | Hybrid retrieval: BM25 plus local embeddings |
| Near-duplicate detection | Naming discipline plus the page-exists check prove insufficient (page count growing faster than distinct concepts) | Two-stage embedding-plus-LLM dedup, reported by practitioners of this pattern at very high precision |

Filling either gap now would be a regression, not progress — an absence by design, with the
threshold as the tripwire.

## Alternatives considered

Pre-building either — rejected as infrastructure ahead of need, contrary to the delivery posture
([ROADMAP](../../../ROADMAP.md#delivery-posture)); a vector store was reached for once in early
drafting and removed by the dependency audit.

## Consequences

The pre-mortem's near-duplicate risk keeps its fallback documented; retrieval cost appearing in
practice has a stated answer rather than an open investigation. Revisit on threshold, not on
calendar.
