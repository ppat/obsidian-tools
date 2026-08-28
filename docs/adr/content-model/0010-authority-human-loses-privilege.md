# 0010. `authority: human` unlocks nothing — the finance hard block keys on evidence, not claimant

**Status:** Accepted ·
**Serves:** [S2](../../../USE_CASES.md#s2--sound)

## Context

The finance overlay's hard block originally read `authority: human|import` — a human-attributed
claim treated as trustworthy as an imported one. That contradicted the provenance split's own
premise: `authority:` is self-reported, and the entire point of separating it from `trigger:` was to
make trust checkable rather than assumed. The flaw became reachable, not theoretical, once
`drift-processor` was specified to stamp `authority: human` on every dispatched device edit: a
credential held on a laptop, publishing over an ingress, could mint the system's highest-trust
provenance — and the one mechanical cross-check is inert on that path, since drift is legitimately
`trigger: event`, not the `schedule` combination the lint flags.

## Decision

**No gate anywhere treats `authority:` as sufficient by itself.** The finance overlay keys on
`authority: import` plus inline provenance plus `confidence:` — the fields that actually evidence a
number — never on who claims to have typed it. `authority: human` stays in the enum and stays
useful (the consistency lint reads it; drift classification reads it); it simply unlocks nothing.
The rule is vault-wide, not finance-specific: an agent's own claim never satisfies a requirement for
an asserted fact anywhere — a fabricated figure is as wrong in a homelab note as a financial one.
What varies by domain is only strictness dials (how much provenance, how fast staleness), which is
what the domain overlays are.

## Alternatives considered

- Keeping `human` in the gate — a hard block resting on a self-reported field; rejected as
  contradicting the split's design.
- Enforcing the anti-fabrication rule as a vault-wide hard block — overclaims: detecting "this
  sentence asserts a fact" in arbitrary prose is judgment, so outside the strict domain it is
  flag-only by the lint pass.

## Consequences

Accepted cost, stated: a human typing a figure directly with no source can no longer place it in
the strict domain without inline provenance either — close to hypothetical (the owner does not
author at the keyboard), and a hand-typed number with no source is exactly what the guardrail
exists to catch. The drift path did not create the flaw; it made it reachable — worth remembering
when a new producer path appears.
