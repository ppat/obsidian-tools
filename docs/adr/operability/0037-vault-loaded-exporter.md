# 0037. The vault-loaded signal is an independent purpose-built exporter — not a probe, not the shared prober

**Status:** Accepted ·
**Serves:** [O1](../../../USE_CASES.md#o1--measured) ·
**Unit:** [D1](../../../ROADMAP.md#group-d--operability) ·
**Tickets:** [apps#3446](https://github.com/ppat/homelab-ops-kubernetes-apps/issues/3446), [apps#3484](https://github.com/ppat/homelab-ops-kubernetes-apps/issues/3484)

## Context

The editor has been observed `1/1 Ready`, zero restarts, REST API serving — and **unable to open
the vault at all** (a permissions condition at the volume mount root,
[ADR-0033](../platform/0033-volume-and-deployment-shape.md)). The readiness probe hits the REST
root, a different code path from the renderer's vault load, so every pod-level signal stayed green
through a total application failure. Only the GUI revealed it.

## Decision

A small exporter inside the vault namespace makes an authenticated call that **enumerates vault
content**, asserts the list is non-empty, and exposes a gauge plus a last-success timestamp. Sited
and labelled to ride the two NetworkPolicy exceptions the module already provisions — zero policy
changes. It is deliberately an **independent observer**: the signal's entire purpose is to
contradict the component's own account of itself, and a component cannot be made to emit "I am
wrong about myself."

## Alternatives considered

- **A vault-load readiness/liveness probe** — the strongest fold-in attempt, and it fails on what a
  probe *does*: a probe converts observation into restart, and the observed failure was one a
  restart does not fix — it would have produced a crashloop, not a signal. (A probe change may
  still be worth making; it is not a substitute.)
- **The shared blackbox prober** — structurally wrong for this check: it attaches credentials
  per-module while targets are per-probe, so anything able to create a probe could point the
  credentialed module at an arbitrary host and receive the vault's write-anywhere bearer token in
  the outbound header; it would also widen a NetworkPolicy documented as a sole control, and its
  "non-empty" assertion would be a body regex standing in for a real check.
- **A push-gateway-fronted CronJob** — a new dependency for one check.

## Consequences

The one observability build item that is **not** a guess to defer: its requirement was observed
against the deployed system, not predicted — so it is pullable forward at will within the
hardening band ([V6](../../../ROADMAP.md#v6--harden-and-tighten-from-experience)). Everything else
request-time-shaped accumulates in
[apps#3484](https://github.com/ppat/homelab-ops-kubernetes-apps/issues/3484) until production
experience says which requirements are real.
