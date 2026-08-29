# 0016. Task metadata uses the Tasks plugin's bracket format, settled vault-wide at Phase 1

**Status:** Accepted — supersedes the "deferred to implementation" stance in the pre-restructure design document ·
**Serves:** [S4](../../../USE_CASES.md#s4--retrievable)

## Context

Task metadata (`due`, `priority`, `repeat`, …) has two candidate syntaxes: emoji markers and the
bracket inline-field form (`[due:: 2026-07-29]`). Changing later rewrites every task in the vault,
so this is settle-now-or-rewrite-everything. The earlier design deferred the choice to an
implementation-time research pass; the research happened, and its central premise inverted:
everyone had assumed the bracket format was *Dataview's* syntax, inheriting Dataview's dormancy
risk. **It is not** — it is the Tasks plugin's own ASCII format, verified in Tasks' source with no
import of and no API call into Dataview.

## Decision

The bracket format, vault-wide, permanent. The deciding argument is failure shape, not popularity:
the emoji format encodes priority as codepoint identity with visually adjacent neighbours, so a
near-miss produces a *valid-but-wrong* value no lint can detect. A malformed bracket field is
simply **absent** — detectable. For a vault whose writers are LLMs and whose dominant failure mode
is content that reads cleanly and says the wrong thing, silently-valid loses to visibly-missing.

## Alternatives considered

Emoji format — rejected on failure shape; the (false) Dataview-dormancy argument against brackets
dissolved on source inspection.

## Consequences

The global todo stays a query over checklist items that live once, in the note that owns the work —
no materialised task list is ever written. A formatter mangling bracket syntax is a known open
risk; there is no converter, and mixed formats would be the symptom to watch for.
