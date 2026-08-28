# 0005. Path scope is path-granular only; per-operation rules get named backstops

**Status:** Accepted ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted)

## Context

Gate 2 (`OBSIDIAN_WRITE_PATHS`) is prefix-based with implicit recursion, applied across all
fourteen tools — including delete. Established directly against the live agent instance: it gates
*where* a call may write, never *what kind* of write it is. `obsidian_write_note` advertises an
`overwrite` flag with no server-side restriction distinguishing create from overwrite at a given
path. A bare filename is a valid degenerate prefix (matches only itself), which is how a root-level
file's grant is expressed without a special case.

## Decision

Accept path granularity as the gate's contract and give every per-operation rule a **named
backstop** owned by the component positioned to enforce it — never an implied property of Gate 2:

| Per-operation rule | Where it is actually enforced |
| --- | --- |
| Raw layer create-only | `batch-processor` ([ADR-0015](../content-model/0015-raw-immutability.md)) — the only component on both sides of that boundary |
| Curated-space content bar | The admission validator ([ADR-0007](./0007-validation-placement.md)) |
| Append-only log | **No backstop exists today** — held by tool scope mostly lacking overwrite, recorded honestly rather than claimed |

An earlier revision of the design claimed whole-file overwrite was "on no agent key"; that
overclaimed what is enforced, and the correction is recorded rather than silently fixed — the
decisive control was always Gate 5 downstream, not Gate 2.

## Alternatives considered

- Per-operation enforcement at the MCP layer — the layer has no such switch; would mean forking the
  server.
- Treating conventions as enforced because they usually hold — exactly the overclaim this record
  corrects.

## Consequences

Anyone adding a "create-only" or "append-only" rule must name its enforcing component in the same
change; a rule without one is a convention, and the design says so out loud. The log's missing
backstop is a known, accepted residue.
