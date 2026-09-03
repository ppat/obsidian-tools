# 0028. The device settings baseline is a frozen seed, captured once through an allowlist, excluded from observation

**Status:** Accepted ·
**Serves:** [S4](../../../USE_CASES.md#s4--retrievable), [R1](../../../USE_CASES.md#axis-3--readers-connected) ·
**Tickets:** [ot#47](https://github.com/ppat/obsidian-tools/issues/47), [ot#71](https://github.com/ppat/obsidian-tools/pull/71), [ot#72](https://github.com/ppat/obsidian-tools/issues/72)

## Context

A device must open the vault correctly configured, but `.obsidian/` is per-instance state that
multiple instances would fight over if continuously replicated — the vendor's own docs name the
workspace files as ones to gitignore because they churn with workspace state.

## Decision

The committed `.obsidian/` is a **seed, not a mirror**: captured once into git, then frozen; a
device is seeded from it once (gated on a completion marker, never on the directory's mere
presence, so an interrupted copy retries rather than reading as done), and thereafter owns its own
configuration. A cluster-side settings change does **not** reach devices; re-seeding is a
deliberate reset. Three mechanisms make the freeze real, found empirically — each covering a case
the others structurally cannot:

| Mechanism | The question it answers |
| --- | --- |
| A git-dir-local ignore rule (reapplied every run — the git dir is a derivable cache) | Does an ordinary add ever *newly track* a settings path? (ignore rules apply to untracked files only) |
| `skip-worktree` on each baselined file (per-clone, reapplied every run) | Does an ordinary add re-stage a *change* to an already-captured file? (git never consults ignore rules for tracked files) |
| The forced add's own **allowlist pathspec** | The one add that deliberately overrides every ignore rule is constrained by nothing else |

The mechanisms key on different things, deliberately, and the asymmetry is load-bearing: the
**allowlist** decides what enters git, once, at capture; the freeze answers a different question —
on every run the committer reapplies `skip-worktree` to **whatever `.obsidian/` files git already
tracks**, without asking how they got there or whether the allowlist would admit them; and the
device seed filters by the **allowlist again**. The
symmetric reading — protection scoped to the same list that decides capture — is the natural one
and is wrong (a pinned test predating the question holds the asymmetry). Both halves matter: the
freeze-on-tracked side means a hand-authored addition to the tracked baseline is protected like
any captured file — the one kind of vault-repo commit the sync does *not* revert
([ADR-0030](./0030-committer-shape.md)) — and the seed-on-allowlist side means a tracked file the
allowlist does not name never reaches a device, so a baseline correction is complete only when the
allowlist names it too.

**The allowlist is a security property, not tidiness.** A denylist would have committed the REST
API bearer token — the path-unscoped, write-anywhere credential — into permanent history on two
remotes and onto every device, because `data.json` is the conventional settings filename for
*every* plugin and the file's `0600` mode is no defence against a same-uid committer. Caught by
adversarial review before it ever executed, and verified never to have leaked. The same defect
recurs one level down: **a bare directory prefix inside an allowlist is a denylist wearing its
clothes** — admitting every future file underneath it sight unseen — so the allowlist is file-glob
and depth-bounded (a `.css` at any depth; every `.json`-admitting rule depth-bounded), and themes
are third-party code arriving through the ungated GUI, so "nothing secret would land there" is not
a claim available. The chain has a third link at the consuming step: the forced add's pathspecs are
re-interpreted by git as patterns unless literal-marked — the observed bypass and the codebase-wide
rule that closes it are [ADR-0043](../platform/0043-git-pathspecs-literal.md)'s.

**Observation is scoped to what publication can act on.** The overlay excludes `.obsidian/`
entirely ([ot#71](https://github.com/ppat/obsidian-tools/pull/71)): a directory no publish can
write back would otherwise re-drift identically every cycle, indefinitely — measured at ~96
patches/day per diverged path, with multi-megabyte plugin bundles in the tracked set. Divergence
from the baseline is still detected, against the same allowlist the seed places, and **reported on
its own per-cycle event, never spooled as drift** — a signal an operator's manual restore answers,
because nothing else can.

## Alternatives considered

Continuous replication (instances fight; churn); per-file excludes of just the workspace files (the
directory-wide rule follows the same reading one step further: a directory nothing publishes is a
directory nothing should observe); a boolean "seeded" flag on the directory's presence (a partial
copy then reads as done — the marker exists precisely to avoid that); server-side dropping of
settings drift from the stream (superseded: device-side exclusion, accepted because the fleet is
one Mac — re-run the argument if a second device ever runs the replicator).

## Consequences

- **The capture is refused outright when its walk is incomplete** — the completion signal *is* the
  commit and history cannot be un-taken, so a partial capture would be the permanent baseline,
  looking correct. Over-refusal is loud and repeats; under-capture is silent and permanent.
- **The deadline for the baseline being right is the device reset** — the moment it stops being
  inert and becomes real configuration
  ([ot#47](https://github.com/ppat/obsidian-tools/issues/47) still has 14 of 16 files unverified
  against the live instance; [ot#72](https://github.com/ppat/obsidian-tools/issues/72)'s seed
  symlink hazard is open). The baseline is *knowingly* not byte-faithful to the instance: it
  deliberately carries a date-format key the instance omits, because a requirement carried only by
  an application default breaks silently on any device whose defaults differ.
- The report cannot distinguish a device edit from a cluster-side baseline change after seeding —
  nothing records which revision a device was seeded from; accepted, recorded.
