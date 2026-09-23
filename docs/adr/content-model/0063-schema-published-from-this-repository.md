# 0063. The schema's source is this repository; a deployed release delivers it whole, with its migration

**Status:** Proposed (supersedes the schema file as the schema's only home, and property types set only at the settings lock, in [ADR-0039](./0039-schema-file-and-agents-pointer.md) and [ADR-0040](../platform/0040-first-seed-and-settings-lock.md); and the count of declared bypasses in [ADR-0001](../write-model/0001-single-writer-one-door.md) and [ADR-0045](../write-model/0045-write-scope-composition.md)) ·
**Pillar:** [The vault system does its own job](../../../DESIGN.md#the-vault-system-does-its-own-job) ·
**Serves:** [S2](../../../USE_CASES.md#s2--sound) ·
**Unit:** [A5](../../../ROADMAP.md#group-a--pipeline-mechanisms)

## Context

The schema lives in three places:

- the vault's schema file, `CLAUDE.md`, with `AGENTS.md` pointing to it;
- this package's schema core, which the validator and the lint pass enforce;
- Obsidian's property types, `.obsidian/types.json` — set by hand at the GUI during the settings
  lock, and the one type check the GUI exception relies on.

The schema file and `.obsidian/` sit under no write scope ([ADR-0045](../write-model/0045-write-scope-composition.md)),
and nothing flows from git back to the volume. So every schema change is a manual act at the GUI,
repeated in three places. Two homes for one fact is this project's best-documented failure mode;
this is three.

A schema change can also invalidate existing content: a renamed value, a removed value, a newly
required field. Repairing each such note as a "value the schema lacks" would be wrong — for a trust
field, repair sets the lowest value, so renaming a `confidence:` value would downgrade every note
that carries it.

Two owner rulings apply: the owner does no manual steps, and nothing reaches production piecemeal.

## Decision

**The schema's canonical source is this repository.** Each release produces one **schema bundle**:

- the schema file's text (`CLAUDE.md`, `AGENTS.md`);
- the property types for `.obsidian/types.json`;
- the schema core the validator enforces;
- the **migration chain**: one step per schema version this project has ever released, so a deploy
  can migrate from whatever version the vault is at, however many releases it skipped.

A schema change is a pull request here, authored by an agent and landed by the owner's merge — his
ownership of the schema, exercised through his landing role.

**A deployed release delivers the bundle, and a release is deployed only through the owner's
merge of the pin.** The schema therefore moves with the rest of the increment, never alone. **Route.** The deployed release's
bundle is rendered into a ConfigMap by the deployment module, as part of the same pin, and is
mounted read-only into the editor's own container. Before Obsidian opens the vault, that
container's entrypoint copies the schema file from the mount and merges the declared property types
into `.obsidian/types.json` — the entrypoint already re-establishes plugin state on every start. No
init container and no other workload mounts the vault volume, so the three mounters stay three
([ADR-0001](../write-model/0001-single-writer-one-door.md)). There is no network fetch at start, and
the editor image changes once, to learn this copy, rather than on every release of this project.

This is a **declared bypass of the door**, the third beside the GUI and recovery exceptions. It is
narrow in every dimension: it writes only those three files (and, when it preserves a GUI edit, a
copy under `_ops/schema/`), only at pod start, only from the
deployed release, and only before the one writer is running. A schema file that differs from the
previously delivered one — changed at the GUI exception — is first copied to `_ops/schema/`, so the
edit is preserved rather than destroyed. ADR-0045's exclusions stand: the
schema file and `.obsidian/` remain under no write scope, so no credential can write them.

**A release that changes existing vocabulary or required fields ships its migration.** A
migration is a declared, deterministic mapping — old value to new value, and a default or
derivation for each newly required field. **Ordering:**

- The vault records the schema version it is migrated to, in a marker under `_ops/`. Only the lint
  pass writes it by design. Nothing can enforce that, since `_ops/` is in the ingestor scope, so the
  lint pass trusts a marker only when the audit trail holds its own record of every step up to that
  version. A marker that is absent, or not backed by the trail, is read as the chain's first
  version. This check guards against accidental writes only: the audit trail is just as writable by
  any ingestor-handle holder, and such a holder could do worse than forge a marker.
- **Every step is idempotent by construction** — a value mapping finds nothing to map a second time,
  and a derivation fills only what is missing — so applying the chain from its start, or twice, is
  harmless.
- After a deploy, the lint pass's migration entrypoint runs at once, as a job of the deploy, once
  the editor is up. It also runs first in every lint pass until the marker matches the deployed
  bundle. It applies the chain from the marker's version to the bundle's, through its ingestor handle
  and the validator. The migration job takes **no mount**: it reads through its handle, so the vault
  keeps exactly three mounting processes ([ADR-0001](../write-model/0001-single-writer-one-door.md)).
  The price is a whole-vault read through the MCP instance on each deploy, which contends with the
  write path and blurs the write-absence signal for the length of the job.
  The job and a lint pass never migrate at once: both take one Lease before applying the chain.
- **Until the marker matches, every validator — in the lint pass, `promotion-processor` and
  `batch-processor` — also accepts a value the pending chain would migrate.** So no note is refused
  for carrying a value the schema has only just renamed. A client that read the old schema file
  before the editor restarted writes old values, and they are accepted and then migrated.

Rules for migrations:

- **A renamed value is a migration, never a "value the schema lacks".** Vocabulary repair
  ([ADR-0055](./0055-lint-findings-resolved-by-the-vault.md)) applies only to values no migration
  covers.
- **A migration never raises a trust field**, and it maps a trust field only as the release
  declares, never to its floor by default.
- **A newly required field that no migration can derive enters as optional**, and becomes required
  only in a later release, once the lint pass reports it present everywhere.
- **A chain never reuses a value it has retired**, so every value a pending chain would migrate
  means one thing. The bundle's build refuses a chain that reintroduces a retired value.

**The vault's copy is never edited in place.** A change made at the GUI exception is reported by
the lint pass, preserved under `_ops/schema/`, and replaced at the next pod start.

## Alternatives considered

- **Editing the schema, and declaring types, at the GUI** — a manual act, three times over, on
  every change.
- **Publishing through the ingestor instance** — puts the schema file inside the widest write scope,
  and needs a path rule in the access gate that only Gate 2 should own.
- **A dedicated schema instance** — a whole provisioning chain for three files.
- **Publishing on release rather than on deploy** — the schema would reach production ahead of the
  code that enforces it, piecemeal.
- **No migrations, with repair left to find the fallout** — a rename becomes a vault-wide
  downgrade.

## Consequences

- **A schema change needs no manual step.** An agent authors it, the owner's merges land and deploy
  it, the pod start delivers it, and the lint pass migrates the vault.
- **The validator, the schema file and the property types can disagree only within a bounded
  window after a deploy.** That window runs from the first workload starting on the new release to
  the migration marker matching. Its only consequence is that old values are accepted and then
  migrated. It closes on the migration job, not on the daily lint schedule.
- **Delivery is bound to the editor's restart.** A deploy restarts the pod anyway, and the schema
  never changes while the editor is running.
