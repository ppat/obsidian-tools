# 0040. Content foundation provisioning: a one-time manual seed, the file-mode discipline, and the day-one settings lock

**Status:** Accepted — property types declared after day one superseded by [ADR-0063](../content-model/0063-schema-published-from-this-repository.md) (Proposed), marked where it stands ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted), [S2](../../../USE_CASES.md#s2--sound)

## Context

There is **no git-to-volume path anywhere in the design** — the volume is authoritative, git is
derived history flowing outward, the committer mounts read-only, the device clone is pull-only. So
the vault's *first* content structurally cannot arrive through git; and Obsidian settings that are
prohibitively expensive to retrofit have to be right before content exists.

## Decision

- **The skeleton is seeded manually, once, directly onto the volume** — extracted from a git
  archive (structural `.git/` exclusion, not a flag someone can forget), into the already-running
  editor's pod, never via a delete-style sync (the entrypoint's own `.obsidian/` already lives on
  the volume and a `--delete` sync would destroy the plugin install and the API key). Provisioning,
  not contention: it runs before any write scope opens, sole writer by circumstance.
- **Files land `0640`, directories `0750`, under `umask 0027` — group-readable, never
  group-writable.** This is the mode discipline the three-mount model rests on: a same-group
  sidecar (committer, lint) can read and commit but never author. The kubelet's fsGroup mechanism
  re-loosens group bits at mount time (bounded by `OnRootMismatch`, and the entrypoint re-tightens
  only the API-key file), which is precisely why the committer sets `fileMode=false`
  ([ADR-0030](../replication/0030-committer-shape.md)).
- **The settings lock is a one-time human checklist at the GUI**, covering exactly the
  retroactively-painful set: new-note location, fixed attachment folder, daily-note format and
  folder, template folder, trash behaviour, and the **property types declared before any note
  exists** — adding a required field once notes exist means backfilling every one, and
  `authority:` is the worst possible field to backfill, since the information needed to answer it
  is gone by then. *Declaring later property types at the GUI superseded by [ADR-0063](../content-model/0063-schema-published-from-this-repository.md), pending its ratification.*

## Alternatives considered

Seeding through git (no such path exists, by design); hand-crafting settings JSON instead of the
GUI (undocumented, subtly wrong exactly where it must be right); locking settings lazily
(retrofit-cost test fails).

## Consequences

One display artefact worth knowing: a directory holding only a `.gitkeep` is *omitted* by the
note-listing tool (dotfiles are ignored, so the directory reads as empty and is dropped, not
reported empty) — the folder map is the schema file's contract regardless of what the listing
shows, and writes into such directories succeed.

One defect the record keeps for its lesson: the daily-note format key was **never actually
written** — the checklist item was half-performed because the GUI already displayed the right
default, so the requirement was satisfied by coincidence, invisible to inspection. That is why the
settings *baseline* deliberately carries the key the instance omits
([ADR-0028](../replication/0028-settings-baseline-seed.md)), and why baseline *values* get
three-way verification ([ot#47](https://github.com/ppat/obsidian-tools/issues/47)) before any
device is seeded for real.
