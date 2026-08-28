# 0017. The community plugin set is Tasks and Dataview only; no in-app validation plugin

**Status:** Accepted — supersedes the seven-plugin list in the pre-restructure design document ·
**Serves:** [S2](../../../USE_CASES.md#s2--sound), [S4](../../../USE_CASES.md#s4--retrievable)

## Context

The design inherited a seven-plugin community set (Tasks, Dataview, Templater, QuickAdd, Linter,
Frontmatter Date Manager, a validation plugin). Implementation research forced each through two
tests: does it load on the pinned stable app version, and does it hold a job nothing else owns.

## Decision

Two survive, each for one reason:

| Plugin | Verdict | Reason |
| --- | --- | --- |
| **Tasks** | Kept | Owns the task query layer |
| **Dataview** | Kept — dormant, every use removable without data loss | Bases cannot read inline checkbox lines at all (it reads cached metadata, not file bodies — confirmed structurally); the task layer depends on that one capability |
| Templater, QuickAdd | Dropped | Both declare a minimum app version above the pinned stable release ([ADR-0032](../platform/0032-obsidian-image.md)); tracking the beta channel for the component everything stands on was not worth two plugins |
| Linter, Frontmatter Date Manager | Dropped | Normalisation moved into the lint pass's own code ([ADR-0018](./0018-lint-pass-policy.md)) — Frontmatter Date Manager has no vault-wide command, so a scheduled pass could not drive it, and acting at file-creation time is exactly the on-save behaviour the one-authority rule forbids |
| Validation plugin | Dropped | The only viable candidate cannot express enums or lowercase-tag rules — precisely the schema parts most likely violated — so it would be a second, lossy source of truth competing with the authoritative validator; also a very small single-author project inside an image we maintain |
| Kanban | Already excluded | Post-1.9 breakage record, found worse than recorded (~26 months stale, repo transferred); board views come from Bases |

Core plugins are unchanged (Properties, Bases, Templates, Daily Notes, Backlinks, Outgoing Links).

## Consequences

A small plugin set is itself the design's stated aim (startup and conflict risk scale with count).
The committed settings baseline corroborates the set: exactly three community plugins ship on a
device seed — Tasks, Dataview, and the Local REST API. Frontmatter shape gets one owner in one
place, structural rather than dependent on plugin settings staying right.
