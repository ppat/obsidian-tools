# 0006. NetworkPolicy as the sole control on the undisableable second MCP endpoint

**Status:** Accepted — with a named, deliberately parked verification ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted)

## Context

The Local REST API plugin bundles its own MCP endpoint, registered unconditionally whenever the
server is enabled. It cannot be disabled: no setting exists, and the upstream issue asking for a
split was closed the same day, unchanged. That endpoint is path-unscoped — a second, wide door to
the vault that the chosen access model never uses.

## Decision

Close it by network isolation alone, stated as a **sole control rather than defence in depth**: the
REST service is ClusterIP-only with no Ingress, inside a default-deny namespace, with an allow rule
admitting only the MCP pods. The bearer token lives only in the MCP pods' secret. Because this is a
sole control, whether the platform actually enforces NetworkPolicy is load-bearing: config-level
evidence against the live cluster reads as enforced at moderate-to-high confidence (the disable
flag absent from every node's recorded arguments, no replacement CNI owning enforcement, live
policies selecting real pods), but the enforcement component fails *silently* if a node dependency
is missing — leaving policies that exist and enforce nothing, indistinguishable through the API.
Only a packet test settles it, and it has never been run. **Parked by standing decision; reopen only
on new evidence.**

## Alternatives considered

- Disabling the endpoint — does not exist as an option; verified against settings and upstream.
- A validating proxy in front of the REST surface — rejected for the write path generally
  ([ADR-0007](./0007-validation-placement.md)); would not remove this endpoint either.
- Treating the risk as tolerable without isolation — the endpoint is path-unscoped write-anywhere;
  not tolerable.

## Consequences

- There is **no fallback** if enforcement turns out to be off — the one risk in the register whose
  mitigation is "test before relying on it" rather than "watch for the symptom". Passive monitoring
  cannot surface an unenforced policy.
- CI cannot exercise this control at all (the test cluster's CNI enforces nothing), which is part of
  the open CI-strategy decision
  ([apps#3440](https://github.com/ppat/homelab-ops-kubernetes-apps/issues/3440)).
- The same reasoning generalises: network position is never an authority model
  ([ADR-0021](../work-queue/0021-authority-by-message-shape.md)).
