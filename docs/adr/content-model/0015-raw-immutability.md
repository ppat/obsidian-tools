# 0015. The raw layer is write-once, exempt from validation, enforced in `batch-processor`

**Status:** Accepted ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted), [S2](../../../USE_CASES.md#s2--sound) ·
**Tickets:** [ot#5](https://github.com/ppat/obsidian-tools/issues/5)

## Context

The bootstrap import lands thousands of documents at once. Validating them would either fail
thousands of times or force a schema laxity that poisons the baseline for everything else — so
`05-raw/` is immutable and unvalidated by design, and curated content is built *from* it
incrementally, preserving the judge-at-100–200-notes gate against a bootstrap of thousands. That
creates a rule no existing gate can carry: create permitted, modify and delete refused — an
*operation* rule, and path scope is path-granular only
([ADR-0005](../write-model/0005-path-scope-granularity.md)).

## Decision

`batch-processor` enforces it: before applying a chunk, reject any patch whose target already
exists under the raw layer. It is the only component positioned to — it writes raw during the
import and reads raw when curated content is built from the pile, so both sides of the boundary are
its own traffic. The admission validator sees notes entering *curated* space, the wrong side of
this boundary: a modification to a raw note never reaches it.

## Alternatives considered

- Enforcement at Gate 2 — cannot express "create yes, modify no".
- A dedicated raw-guard component — a component whose entire job is one conditional
  `batch-processor` already has the context to evaluate.
- Validating raw after all — poisons the baseline or fails en masse; rejected with the exemption
  that keeps [S2](../../../USE_CASES.md#s2--sound) falsifiable.

## Consequences

The acceptance test for this behaviour tests `batch-processor`'s own logic, not an MCP-layer
refusal — a code regression, not a misconfiguration, and the two are debugged differently. Known
accepted residue until a third MCP instance exists
([ADR-0003](../write-model/0003-two-instances-two-handles.md)): other ingestor-handle holders could
technically modify raw and are held back by scope discipline, not a backstop.
