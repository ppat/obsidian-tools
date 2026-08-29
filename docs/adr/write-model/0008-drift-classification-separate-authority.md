# 0008. Drift intentionality is a separate authority, upstream of the validator — merging them was tried and reverted

**Status:** Accepted ·
**Serves:** [W6](../../../USE_CASES.md#axis-2--writers-connected) ·
**Unit:** [A7](../../../ROADMAP.md#group-a--pipeline-mechanisms) ·
**Ticket:** [ot#4](https://github.com/ppat/obsidian-tools/issues/4)

## Context

A captured device edit raises a question nothing else in the system asks: *did a human mean to do
this* — or is it a slipped keystroke, autocorrect damage, or the system's own upstream content
re-read after a crash? An earlier draft merged this classification into the admission validator to
avoid "two gates disagreeing about the same file".

## Decision

Classification is a stage inside `drift-processor`, **upstream of the common ingest pipeline, and
never a call into the validator**. The two answer different questions: *did a human mean this* is
asked only of drift; *does this content meet the schema and provenance bar* is asked of everything,
and stays the validator's sole job. The invariant is
[one authority per question](../../../DESIGN2.md#one-authority-per-question) — "one authority
total" was the wrong generalisation, and it would have made the drift path depend on a validator
that does not exist until [A4](../../../ROADMAP.md#group-a--pipeline-mechanisms) ships. The
device-side detector stays dumb by contract: it submits every path the comparison flags and judges
nothing, so it can never silently drop a real edit. Classification is server-side, using signals
decidable without guessing intent: location (drift on a fixed-address contract file — the log, the
global todo, the schema file and its pointer, the templates and ops areas — is presumed
unintentional, since the folder map already says humans don't author there), schema damage (an edit
that breaks validation is presumed unintentional anywhere), shape (whitespace-only, mid-word
insertions, uncompensated truncation — legible because drift is a diff against a known baseline,
not a snapshot), and contract violation (prose in a queries-only file). **The home note
(`00-index.md`) is deliberately carved out of the fixed-address list**: it is the one hand-curated
root file (and carries the salience-audit checklist), so drift on it classifies like an ordinary
content note — and the carve-out closes what would otherwise be that file's *only* mutation route,
since it sits outside both MCP instances' write scopes and only drift or the GUI can ever change
it. Once an edit survives, dispatch is dumb — it joins the ordinary ingest path and faces the
validator like any other write.

## Alternatives considered

- Classification inside the validator — reverted: braids two questions, and adds a hard dependency
  the drift path does not need.
- Device-side filtering — rejected: policy would live on a fleet of devices, and a judging detector
  can silently drop a real edit.

## Consequences

- `drift-processor` carries an obligation the classifier must honour before stamping
  `authority: human`: reconcile the captured edit against upstream history
  ([ADR-0026](../replication/0026-spool-upstream-observations.md)) — drift traffic is not reliably
  human in origin.
- The classifier must not be calibrated before the overlay fix reaches the Mac
  ([ROADMAP, dependencies](../../../ROADMAP.md#dependencies)): a diverged settings path used to
  re-drift every cycle, and a filter tuned on that distribution is tuned on an artifact.
