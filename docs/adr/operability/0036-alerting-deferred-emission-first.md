# 0036. Alerting is deferred until AI triage exists; emission is front-loaded — the asymmetry is reversibility

**Status:** Accepted — the "until AI triage exists" condition superseded by [ADR-0062](./0062-nothing-is-pushed-to-a-person.md) (Proposed), marked where it stands ·
**Pillar:** [Instrument early, alert never](../../../DESIGN.md#instrument-early-alert-never) ·
**Serves:** [O1](../../../USE_CASES.md#o1--measured), [O3](../../../USE_CASES.md#o3--alerting)

## Context

One operator, no team: an alert nobody can triage is negative value, and the platform's alerting
substrate deliberately carries nothing that pages. Meanwhile an uninstrumented window is gone
forever — metrics cannot be backfilled, while rules over existing metrics are switchable any day.

## Decision

**No alert rules until an AI triage layer exists** to filter noise before anything reaches the
human — [O3](../../../USE_CASES.md#o3--alerting) is an explicit non-outcome, recorded so nobody
"fixes" the absence. *The "until" condition superseded by [ADR-0062](./0062-nothing-is-pushed-to-a-person.md), pending its ratification.* **Emission moves to the front**: the owner's own correction — metrics and
logging up front, maybe dashboards; rules can wait till the end — reversed an earlier
observability-last ordering. Signals ride as acceptance criteria on the units able to produce them
([Group D's preamble](../../../ROADMAP.md#group-d--operability): criteria distribute, shared
mechanisms do not); dashboards wait until the questions are real, because one built before that
displays the wrong things and is cheap to rebuild later.

Four hard-won signal rules travel with this record: **watch the absence of writes, not only
errors** — the editor can wedge without raising one, and silence is the dangerous state;
**watch image age, not "a newer version exists"** — the risk is maintenance lapsing entirely, and
"newer exists" is permanently true and low-signal; **watch unreviewed-finding age, not finding
count** — the review loop's death shows as findings growing old, not numerous; and **watch read
volume, not write volume alone** — reading is agent-heavy by design, so the signal is the whole
read side (the agent readers and the conversational surface alike) declining while writes
continue: the vault becoming a write-only landfill, the pre-mortem's highest-probability,
highest-impact failure, and silent in every other signal here. A fifth is structural: a write-gate refusal
returns HTTP 200 with the error inside the envelope, so **no HTTP-level metric can ever observe
the write gate** — queue metrics cover stream-borne traffic as queue-native facts; the direct
write path needs its own instrument ([D2](../../../ROADMAP.md#group-d--operability)).

## Alternatives considered

Alerting now with tuned thresholds (noise for one person; the tuning has no data yet); deferring
emission along with alerting (irreversible loss — the two halves have opposite reversibility);
alerting phrased into the design's older prose ("alert when inbox > 20") — read as signal-naming,
superseded by the instrumentation-only scope.

## Consequences

Every window since Phase 0 is answerable or lost forever depending on whether its emitters shipped
with their units — the reason [O1](../../../USE_CASES.md#o1--measured) criteria are non-deferrable
under the delivery posture while every rule on top can wait indefinitely.
