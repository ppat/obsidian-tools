# 0045. Write-scope composition: enumerated allowlists, entries mirroring grants, the schema file outside every scope

**Status:** Accepted — the client grant `log.md` mirrored superseded by [ADR-0057](./0057-grants-by-kind-of-access.md), and the count of paths to the excluded files by [ADR-0063](../content-model/0063-schema-published-from-this-repository.md) (both Proposed), marked where they stand ·
**Pillar:** [Layered content, one ownership contract](../../../DESIGN.md#layered-content-one-ownership-contract) ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted)

## Context

Each MCP instance carries a write scope — `OBSIDIAN_WRITE_PATHS`, a list of path prefixes; a write
to any path not under one of them is refused (Gate 2 in the design's gate ordering).
[ADR-0005](./0005-path-scope-granularity.md) records that gate's *contract*: path-granular
prefixes, where a bare filename is a valid prefix matching only itself. This record is about the
scopes' *composition* — which prefixes each of the two instances carries, and which vault files
deliberately sit under none of them.

The vault contains files whose ownership contract admits no standing writer of any kind: the
schema file (`CLAUDE.md` at the vault root — the ownership contract, folder map, frontmatter
schema, and agent write discipline, with one owner, the human —
[ADR-0039](../content-model/0039-schema-file-and-agents-pointer.md)), the vault's index note, the
templates directory (`_templates/`), and Obsidian's own configuration directory (`.obsidian/`).

## Decision

Both write scopes are explicit enumerations, and neither is ever left unset:

| Instance | `OBSIDIAN_WRITE_PATHS` |
| --- | --- |
| agent | `00-inbox/`, `40-journal/`, `_ops/agent/`, `log.md` |
| ingestor | `00-inbox/`, `05-raw/`, `10-areas/`, `20-projects/`, `40-journal/`, `90-archive/`, `_ops/`, `log.md` |

Two properties are the point of the enumeration:

- **What is excluded.** The schema file, the index note, `_templates/`, and `.obsidian/` are under
  no prefix in either scope — no gateway credential, narrow or wide, can write them — so the
  schema's single-owner rule is enforced by omission rather than by convention. The only paths to
  those files are the two declared exceptions to the gate system: a human at the headless
  instance's own GUI (the GUI exception,
  [ADR-0002](./0002-gui-exception-dormant-vnc.md)), and operator-triggered restore (the recovery
  exception). *"The only paths … the two declared exceptions" superseded by
  [ADR-0063](../content-model/0063-schema-published-from-this-repository.md), pending its
  ratification.*
- **Entries mirror grants.** `log.md` appears in the *agent* scope as a bare filename — a prefix
  matching only itself — because the gateway grants n8n "append to `log.md`, and nothing else"
  ([ADR-0003](./0003-two-instances-two-handles.md)), and a gateway grant is exercisable only if
  the instance's path scope also admits the path: who-may-call (the handle) and
  where-a-write-may-land (the scope) are separate layers, and a write needs both to say yes.
  A scope entry that looks stray may therefore be load-bearing for a grant recorded elsewhere;
  removing it disables that grant silently. *The named client's grant this entry mirrored superseded by [ADR-0057](./0057-grants-by-kind-of-access.md), pending its ratification.*

## Alternatives considered

- **Leaving the ingestor scope unset**, since it is the wide one anyway — an unset scope admits
  the whole vault, which would place the schema file and the application configuration inside the
  blast radius of the widest credential in the system.
- **Expressing per-writer path scopes at the gateway instead** — the gateway's tool filtering is
  server-level and not path-scoped; that limitation is why the instance layer exists at all
  ([ADR-0003](./0003-two-instances-two-handles.md)).

## Consequences

- A new or changed gateway grant that writes to a new path must add the matching scope entry in
  the same change — the composition-side mirror of
  [ADR-0005](./0005-path-scope-granularity.md)'s rule that every per-operation rule names its
  enforcing component.
- The index note and the global todo sit outside both scopes deliberately: they hold queries over
  vault content, never written rows — the standing rule that no note is a materialised cache of
  something computed elsewhere.
