# 0039. One schema file, one owner: `CLAUDE.md` at the vault root, with `AGENTS.md` as a one-line plain-text pointer

**Status:** Accepted ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted), [S2](../../../USE_CASES.md#s2--sound)

## Context

Every agent, whatever filename convention its runner looks for, must read one schema contract: the
ownership contract, the folder map with stable addresses, the frontmatter schema and vocabularies,
the domain overlays, the page-exists check, wikilink and sentinel-marker discipline,
anti-fabrication rules, and the write discipline (only through MCP, only in your zone, prefer
append/patch, never touch settings, never write raw, declare `source:` truthfully, bulk work goes
to the queue). Two filename conventions exist in the wild (`CLAUDE.md`, `AGENTS.md`); the vault
also replicates through rsync and iCloud to devices.

## Decision

`CLAUDE.md` is the schema file — the single most important file in the vault — and `AGENTS.md` is a
**plain-text file containing exactly one line, `@CLAUDE.md`**, which agent runners resolve as a
reference. Not a symlink: a symlink would have to survive the rsync/iCloud replication path, and
whether iCloud carries one, or rsync preserves it as a link rather than following it, depends on
flags nobody should depend on for the schema file — the project's symlink record
([ot#22](https://github.com/ppat/obsidian-tools/issues/22),
[ot#72](https://github.com/ppat/obsidian-tools/issues/72)) is a history of exactly such hazards.
The file has one owner: the human. Open co-evolution of a schema file invites the drift the whole
design exists to avoid; for a single-operator vault the council is trivially one person.

## Alternatives considered

A symlink (replication-fragile); duplicating the contract into both files (two copies drift — the
failure this project has repeatedly paid for); per-agent prompt injection of the rules (prompt
discipline scores lowest of the enforcement options; the file is the durable, versioned form).

## Consequences

Every schema change is one file, one review; the property types the schema declares are locked
day one at the settings lock ([ADR-0040](../platform/0040-first-seed-and-settings-lock.md)) because
retrofitting a required field once notes exist means backfilling every note — and `authority:` is
the worst possible field to backfill, since the information needed to answer it is gone by then.
