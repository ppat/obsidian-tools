# 0062. Nothing is pushed to a person: alerting is excluded, not deferred

**Status:** Proposed (supersedes the "until AI triage exists" condition in [ADR-0036](./0036-alerting-deferred-emission-first.md)) ·
**Pillar:** [Instrument early, alert never](../../../DESIGN.md#instrument-early-alert-never) ·
**Serves:** [O3](../../../USE_CASES.md#o3--alerting)

## Context

[ADR-0036](./0036-alerting-deferred-emission-first.md) deferred alert rules "until an AI triage layer
exists to filter noise before anything reaches the human". Read that way, the deferral names a
future condition under which pushes to the operator become acceptable.

The owner has since ruled that nothing is ever pushed to him, and that nothing waits on him to
resolve it. The platform exists so that agents do his work, not to give him work.

## Decision

**No component of the vault system pushes anything to a person, at any stage.** That covers alerts,
digests and notifications alike. Alerting is excluded from the design outright: it is not waiting
on a precondition.

Any triage of the vault system's own signals is the vault system's own: by rule where a rule
decides ([ADR-0064](./0064-operational-conditions-have-resolvers.md)), through its own runtime
(ADR-0054) where judgement is needed. Either way, its output is a resolution or a record, never a
message to a person. Emission stays
front-loaded exactly as ADR-0036 decides, and its signal rules stand. The metrics and logs are there
to be queried, not sent.

## Alternatives considered

- **Alerting once an AI triage layer filters the noise** — this still pushes to a person, and the
  ruling excludes that.
- **A pull-only dashboard as the "alerting"** — it is not excluded, because a person may choose to
  read a dashboard ([D6](../../../ROADMAP.md#group-d--operability)). But nothing depends on anyone
  reading it.

## Consequences

- **[O3](../../../USE_CASES.md#o3--alerting) is a non-outcome for good**, not for now.
- **Everything the vault system notices has an owner inside the vault system.** A condition no
  component resolves is a gap in the design, and a push to a person does not close it.
