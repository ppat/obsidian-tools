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
sole control, whether the platform actually enforces NetworkPolicy is load-bearing. The enforcement
dependency is packet-proven: another project on the same cluster runs a standing falsifiability
probe for its own purposes — default-deny with positive controls, blocked and reachable cases both
asserted, continuously — so a cluster-wide enforcement regression now surfaces on the probe's next
cycle rather than never [measured 2026-09-02]. What remains config-level is this namespace's own
policy objects staying correct (live policies selecting real pods, no replacement CNI owning
enforcement), backed by the refusals observed from a non-MCP pod at substrate acceptance. The
dedicated in-namespace two-pod test remains unrun. **Parked by standing decision, upheld on the new
evidence: the standing probe out-instruments a one-time test, and the residual is the smallest it
has been.**

## Alternatives considered

- Disabling the endpoint — does not exist as an option; verified against settings and upstream.
- A validating proxy in front of the REST surface — rejected for the write path generally
  ([ADR-0007](./0007-validation-placement.md)); would not remove this endpoint either.
- Treating the risk as tolerable without isolation — the endpoint is path-unscoped write-anywhere;
  not tolerable.

## Consequences

- There is **no fallback** behind this control. Cluster-wide enforcement loss is now watched-for —
  the standing probe surfaces it — but a mangled or unselected policy object in this namespace
  specifically remains invisible through the API: at that layer, no passive signal exists.
- CI cannot exercise this control at all, permanently: the CI cluster's CNI is not the platform's
  enforcement engine, so a passing kind packet test would validate the wrong machinery. This bounds
  the open CI-strategy decision
  ([apps#3440](https://github.com/ppat/homelab-ops-kubernetes-apps/issues/3440)).
- The same reasoning generalises: network position is never an authority model
  ([ADR-0021](../work-queue/0021-authority-by-message-shape.md)).
