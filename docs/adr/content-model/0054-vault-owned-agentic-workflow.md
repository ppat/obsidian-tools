# 0054. The vault does its own agentic work: a vault-owned workflow, run inside the vault's existing jobs, writing only through their gated paths

**Status:** Proposed ·
**Serves:** [S2](../../../USE_CASES.md#s2--sound), and [S3](../../../USE_CASES.md#s3--placed) through A8's roll-up pass, when built ·
**Unit:** [A5](../../../ROADMAP.md#group-a--pipeline-mechanisms), and [A8](../../../ROADMAP.md#group-a--pipeline-mechanisms) when built ·
**Ticket:** [ot#177](https://github.com/ppat/obsidian-tools/issues/177)

## Context

Some of the vault's own jobs need judgment, not code: the lint pass's contradiction and
model-judged staleness checks, and resolving the findings it cannot fix mechanically
([ADR-0018](./0018-lint-pass-policy.md)); and A8's roll-up pass, when built, needs model-elicited scores
([ADR-0011](./0011-confidence-enum.md), [ADR-0012](./0012-salience-consolidated-fields.md)).

Two facts close off the obvious places to send that work. The vault does not know its clients: its
design and behaviour depend only on the credential a writer presents, never on which agent holds it
or whether that agent runs batch work at all — so it cannot hand its own work to one. And nothing is pushed to the owner for
action — so it cannot hand the work to him.

## Decision

The vault runs its own agentic workflow for exactly that work, as a stage inside the host job — a
library, like the admission validator — not a component with a door of its own.

- **May:** read what the host job already reads; decide judgment findings; write resolutions
  through the host job's existing handle, each curated-boundary crossing through the admission
  validator and each change in the audit trail; leave a finding it cannot resolve recorded in the
  report. Each resolution applies only to the note or notes its finding names, through the host job's
  existing write path and checks.
- **May not:** hold any mount, handle, credential, stream grant or path scope of its own; delete
  (quarantine and write-before-delete stand); edit human-marked sentinel blocks; promote (that is
  the promotion path's); enqueue batch work; call or depend on any client agent; push to the owner;
  reach anything outside the vault except its configured model endpoint.
- **Model access** is an installation-configured model endpoint; an LLM gateway may front it or not.
- **Provenance:** notes and records it creates carry the host job's provenance — the host job's
  `source:`, `authority: agent`, `trigger: schedule`; an edit to an existing note leaves that
  note's provenance to the ordinary rules
  ([ADR-0009](./0009-three-field-provenance-split.md),
  [the pillar](../../../DESIGN.md#humans-originate-agents-act)).

## Alternatives considered

- **Push findings to the owner for action** — rejected by owner ruling: nothing is pushed to him,
  and resolving what the vault finds is agents' work.
- **Route findings to a client agent** — rejected: the vault does not rely on outside agents to do
  its own job, and does not know which agents exist.
- **A standalone agent component with its own handle** — a new writer and a new door, for no job
  that needs one.

## Consequences

- The three-mounter rule ([the pillar](../../../DESIGN.md#one-writer-one-door)) and the
  three-caller shape of the admission validator
  ([ADR-0007](../write-model/0007-validation-placement.md)) are unchanged: the workflow reads
  and writes only as its host job does, and crosses the curated boundary as that job.
- The agent framework is not fixed by this record; whichever is chosen enters `pyproject.toml` as a
  dependency of the component that needs it.
- Resolved and unresolved finding counts, and unresolved-finding age, are vault-derived metrics
  ([O1](../../../USE_CASES.md#o1--measured)), emitted by the host job like its others.
- The workflow reads content other agents wrote and writes through the ingestor handle, the
  widest in the system; its "May not" list is therefore enforced by the host job's code, not by a
  gate, and each item on it is proven by violation injection, catalogued in the verification
  catalogue like every other control.
