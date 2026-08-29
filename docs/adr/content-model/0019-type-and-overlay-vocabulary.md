# 0019. Vocabulary rulings: a person is an `entity` (no kind vocabulary), and dining takes travel's overlay

**Status:** Accepted ·
**Serves:** [S2](../../../USE_CASES.md#s2--sound)

## Context

Two small vocabulary decisions with non-obvious mechanisms behind them, recorded so neither is
re-invented.

## Decision

- **`type:` has no `person` value — a person is an `entity`, and `entity` carries no required kind
  vocabulary.** The alternative (a closed `person|org|tool|place` kind expressed as a required tag)
  was removed because the *mechanism* was wrong whatever the values: a required, exactly-one,
  closed-enum value living inside a free-form tag list is a rule nothing enforces — an agent can
  write zero or two kind tags and every structural check passes, and a tag edit can silently drop
  the one that was there. A dedicated `kind:` field would have fixed the mechanism and was rejected
  on a narrower ground: nothing queries an entity by kind, and a field should exist to be queried,
  not to satisfy the schema's appetite for completeness.
- **`10-areas/dining/` takes travel's overlay, not homelab's.** A restaurant's hours, menu and
  prices are perishable the way a destination's facts are, so dining sits at the
  recency-markers-on-perishable-facts strictness. Domain overlays are strictness dials on two
  shared axes (provenance demanded, staleness cadence) — an overlay *assignment* is the decision a
  new area needs, not a new rule.

## Consequences

Adding an area means choosing its overlay position explicitly; adding a schema rule means naming
the mechanism that enforces it — a required value inside a free-form list is the named
anti-pattern.
