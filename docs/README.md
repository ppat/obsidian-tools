# Design canon

This directory is the versioned home of BRAIN's design of record. It replaces the GitHub issue attachments
these documents previously lived only as (`ppat/homelab-ops-kubernetes-apps#3439`), which was inconsistent
with the project's own durability constraint: the design of a durability-focused system shouldn't itself be
hostage to a third-party issue tracker's attachment storage.

Read `DESIGN.md` first, and read it as current. Read the other three only for the reasoning behind it.

## Reading order and authority

| Document | Role | Authority |
| --- | --- | --- |
| [`DESIGN.md`](./DESIGN.md) | The design itself: what BRAIN is, why it's shaped this way, what remains open | **Current design of record.** Supersedes CANON-1 and CANON-2 wherever they conflict with it. |
| [`FINDINGS-v1-source-review.md`](./FINDINGS-v1-source-review.md) | A primary-source review conducted to check claims made in CANON-1 and CANON-2 against upstream documentation and repositories | Corrects specific claims in the two canon documents below — see "Corrections to canon" in that file. Superseded, in turn, by anything `DESIGN.md` says explicitly. |
| [`CANON-1-designing-brain.md`](./CANON-1-designing-brain.md) | Research input: community practice for vaults written and organised by multiple agents | Research input, not current guidance on its own. Read through `DESIGN.md` and `FINDINGS-v1`, not standalone. |
| [`CANON-2-mcp-research.md`](./CANON-2-mcp-research.md) | Research input: selecting an Obsidian MCP server | Research input, not current guidance on its own. Read through `DESIGN.md` and `FINDINGS-v1`, not standalone. |

The dependency runs one way: `DESIGN.md` was written by drawing on `FINDINGS-v1`, which was itself written to
check claims made in `CANON-1` and `CANON-2`. Nothing later in that chain gets overridden by something earlier
in it. A claim in a CANON document that `FINDINGS-v1` corrects, or that `DESIGN.md` decides differently, is
superseded — the CANON documents are kept unedited as a historical record of what was researched, not as a
document to reconcile against current decisions.

## Also in this directory, and not canon

[`settings-lock.md`](./settings-lock.md) is a one-time operational procedure, not design:
the checklist a human works through at the headless Obsidian instance's GUI before any vault
content exists. It lives here rather than in the vault because it is executed once and is
then history, whereas the vault holds durable, regenerable content. It is copied into the
vault root transiently for the duration of the GUI session and removed afterwards; the file
itself explains that. It carries no authority over `DESIGN.md` and nothing in the table
above defers to it.

[`gui-access.md`](./gui-access.md) is likewise operational, not design: the runbook for
reaching that same GUI (P8) at all — connection steps, and the hazards hit the first time
it was actually used. It carries no authority over `DESIGN.md` either, and nothing in the
table above defers to it.

[`local-replicator.md`](./local-replicator.md) is the third document in this operational
tier: install/uninstall instructions and operator prerequisites for `local-replicator`, the
one component in the whole design that runs outside the cluster (a launchd job on the
operator's Mac, not a Flux-managed workload). Unlike `settings-lock.md`, it isn't a one-time
procedure executed once and then history — installing, uninstalling and resetting the
device `.obsidian/` baseline are all things an operator does again, on demand. It carries no
authority over `DESIGN.md` either.

## What FINDINGS-v1 corrects

`FINDINGS-v1-source-review.md` § "Corrections to canon" lists, with severity, every claim in CANON-1/CANON-2 it
found to be wrong or outdated against primary sources, including:

- **CANON-1's claim that a Claude Code `PostToolUse` hook blocks a write that fails validation is false** — the
  tool has already run by the time that hook fires; only `PreToolUse` can block. This is the highest-severity
  correction, and `DESIGN.md` §3 Gate 0 designs around it explicitly (a detective, not preventive, control at
  that point).
- CANON-2's claim that `shanehull/obsidian-remote` published no pullable image was outdated — a pullable image
  now exists — though `DESIGN.md` sets the project aside anyway, on different and stronger grounds (no GUI, own
  bundled MCP server).
- A discrepancy (not fully resolved) in CANON-2's claimed last-updated date for `StevenStavrakis/obsidian-mcp`
  against GitHub's own `pushed_at` timestamp.

Treat any claim from CANON-1 or CANON-2 that isn't repeated or endorsed in `DESIGN.md` as unverified at best,
and check `FINDINGS-v1` before relying on it.

## Status

No code in this repository exists yet — see the root [`README.md`](../README.md) and [`CLAUDE.md`](../CLAUDE.md)
for what's here today. These documents describe the target design that the rest of this repository, once
written, implements.
