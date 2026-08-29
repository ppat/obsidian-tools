# 0031. The Obsidian apps are staged out of replication entirely; installing them is a deliberate day-one reset

**Status:** Accepted ·
**Serves:** [R1](../../../USE_CASES.md#axis-3--readers-connected) ·
**Unit:** [B8](../../../ROADMAP.md#group-b--connection-work) ·
**Tickets:** [ot#69](https://github.com/ppat/obsidian-tools/issues/69), [ot#47](https://github.com/ppat/obsidian-tools/issues/47), [ot#72](https://github.com/ppat/obsidian-tools/issues/72)

## Context

The replication cycle shipped and was verified with **no Obsidian app installed anywhere**: content
arrives through agent paths, the devices are for reading, and there was not yet enough content to
read. Any editor writing into the iCloud directory exercises the drift path, so nothing in the
cycle's acceptance needed an app.

## Decision

The apps wait until they are wanted ("enough content to read" — the owner's criterion). When they
go on, the device vault is **reset to day one: the iCloud copy is dropped *and* the baseline tag
with it** — with the tag left in place, the next cycle would overlay an empty directory and read
the entire vault as a device-side deletion; with the tag gone it takes the no-baseline path,
publish-then-tag, skipping one drift enumeration. The one acceptance criterion that genuinely needs
an app — does Obsidian on iOS read rsync-written files correctly — was **relocated to the rollout,
not waived** ([ot#69](https://github.com/ppat/obsidian-tools/issues/69)): it tests device-side app
compatibility, which only signifies once devices are read from, and deferring costs nothing because
the app is not a write path.

## Alternatives considered

Installing the apps at replication-ship time (tests nothing the cycle needed, and starts the
irreversibility clock early); folding the iOS criterion into a settings ticket (different question
— baseline *value* correctness versus app compatibility).

## Consequences

**The reset is the deadline, and it is irreversible**: the settings baseline stops being inert and
becomes a device's actual configuration at that moment
([ADR-0028](./0028-settings-baseline-seed.md)). Its preconditions —
[ot#47](https://github.com/ppat/obsidian-tools/issues/47) (values verified three-way) and
[ot#72](https://github.com/ppat/obsidian-tools/issues/72) (the seed's symlink hazard) — are
structural edges on [B8](../../../ROADMAP.md#group-b--connection-work), and doing the rollout
before they close fails silently and permanently, this project's characteristic failure shape.
