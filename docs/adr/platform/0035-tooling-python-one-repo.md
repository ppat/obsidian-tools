# 0035. Tooling: Python managed with uv, one repository, one package versioned as a whole

**Status:** Accepted ·
**Ticket:** [ot#1](https://github.com/ppat/obsidian-tools/issues/1) (closed; the founding record)

## Context

Every component is glue — schema validation, protocol calls, YAML frontmatter, a markdown link
graph, git plumbing, model prompting — maintained largely by LLM agents in short bursts across many
sessions, for years. Nothing anywhere is performance-bound: the accepted bottleneck is one
single-threaded Electron event loop, and a bulk import taking hours, once, is in the design.

## Decision

Python, with uv. Iteration speed dominates for LLM-maintained code — a wrong guess costs a re-run,
not a rebuild — and a compiled language would have won only on packaging size, which constrains
nothing here. Bash is confined to CI and operational scripts, never runtime logic. **One
repository, one package, versioned as a whole**: every component shares the same MCP client path,
the same schema, the same notion of a valid write — splitting them would mean duplicating that
contract or versioning it apart from the code depending on it, for components with no reuse outside
this project.

## Alternatives considered

A compiled language (wins only where nothing is constrained); per-component repositories (contract
duplication); copying toolchain config from the nearest-shaped sibling repo on trust — explicitly
rejected: that repo never reached production, so it is a source of *shape* only, every version and
config verified independently against upstream.

## Consequences

Real git via the CLI is preferred over library bindings (this system is one node in a distributed
git system; the bindings would remove some bug classes while forking behaviour from every other
node's git). The version string reported by the installed package does not track the release tag —
deployment records the tag out of band
([ot#66](https://github.com/ppat/obsidian-tools/issues/66) carries the consequences).
