# 0025. The replication cycle: one parked clone, overlay-and-diff, spool-gated publish, tag advance

**Status:** Accepted ·
**Pillar:** [The device loop is non-destructive by ordering](../../../DESIGN2.md#the-device-loop-is-non-destructive-by-ordering-not-by-hope) ·
**Serves:** [S4](../../../USE_CASES.md#s4--retrievable), [W6](../../../USE_CASES.md#axis-2--writers-connected) capture ·
**Ticket:** [ot#3](https://github.com/ppat/obsidian-tools/issues/3) (closed; the shipped shape)

## Context

The device-facing iCloud copy must be kept current from git without ever destroying an edit a human
made there. The design went through **three readings of the cycle**, each correction kept rather
than silently replaced — the sequence is the reasoning.

## Decision

One cache clone, **parked at the `LAST_CHECKOUT` tag between cycles, is itself the baseline** — no
second checkout. The ordering (diagrammed in
[DESIGN2](../../../DESIGN2.md#the-device-loop-is-non-destructive-by-ordering-not-by-hope)): park and
prune → overlay the device tree onto the baseline (rsync in, `--delete`, excluding `.git` and
`.obsidian` by **bare name** — a trailing slash is inert against a symlink or plain file, after
which `--delete` destroys the real directory) → `git diff` *is* the drift enumeration, yielding
patches → spool every patch durably to local disk → reset, pull, publish the working tree out →
advance the tag. Publish waits on **two independent conditions**: every patch spooled, and every
patch *actually captured its content* — a binary yields a content-free patch (`Binary files ...
differ`) and withholds the whole cycle; a pure rename or deletion passes, because its header
describes the change completely. The tag advances only after a completed publish, because the tag's
definition is "byte-identical to what was last placed there".

## Alternatives considered — the three readings

1. **Pull before comparing** — wrong: the human's edits were made against what was last *placed* in
   iCloud; comparing after the pull mixes upstream change and human drift inseparably.
2. **Per-path publish gating** (capture as a separate read step; failed paths excluded while the
   rest publishes) — wrong twice: one tag structurally cannot mean "this path at the new commit,
   that path at the old one", so partial publish makes the next cycle read already-published paths
   as fresh drift (and, once dispatch exists, stamp the system's own content as human); and the
   whole-publish variant paused the direction the owner depends on to protect the direction that is
   rare by design.
3. **The shipped shape** — the gate moved to the *spool write* (local disk, whose failure is rare)
   rather than a network publish (whose unavailability is this component's normal condition). The
   safety property survived; the pause-generator did not.

Also rejected: `git stash` for cycle reset (accumulates refs a crash can strand; `reset --hard`
keeps the cycle idempotent from any crash state), and a per-file hash manifest instead of a tag (a
manifest can describe a tree partly new and partly old; a single ref structurally cannot — which is
exactly what reading 2's failure turned on).

## Consequences

- Drift is a **patch against a known baseline**, not a snapshot — an append and a rewrite look
  different before content is even read, which is most of what the intentionality classifier needs
  ([ADR-0008](../write-model/0008-drift-classification-separate-authority.md)).
- The ordering was built at first ship with the drainer stubbed to discard, because retrofitting a
  gate later would *invert* a working publish path's control flow rather than insert a step — the
  skeleton never has to move; dispatch only changes where the drainer sends
  ([ADR-0024](../work-queue/0024-drift-destination-stream-not-minio.md)).
- The window between publish and tag advance cannot be closed (two operations on two substrates); a
  cycle dying between them makes the next cycle read the system's own published tree as drift —
  the reason the spool records upstream observations
  ([ADR-0026](./0026-spool-upstream-observations.md)) and the classifier must reconcile against
  history before stamping `authority: human`.
- Losing the clone loses the baseline: the first overlay after a rebuild reads the whole vault as
  drifted; re-baselining is publish-then-tag, skipping one enumeration.
