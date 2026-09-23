# 0004. Delete withheld from the agent handle at the gateway; relocation writes before it deletes

**Status:** Accepted — where the withholding is enforced superseded by [ADR-0059](./0059-one-holder-per-credential-access-record.md) (Proposed), marked where it stands ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted)

## Context

The MCP tool set has **no move or rename primitive**: any relocation — promotion out of the inbox,
archiving a rolled-up source — is necessarily write-to-new-path followed by delete-at-old-path. So
the ingestor side genuinely needs delete. The agent instance has no legitimate use for it at all,
and it is also the instance exposed to untrusted input (chat captures, fetched web content) — delete
is exactly the tool prompt injection would want most. The MCP server cannot withhold it alone: its
read-only flag bundles delete with six other write tools, with no per-tool switch.

## Decision

`obsidian_delete_note` is withheld one layer up, at the gateway: per-tool `disallowed_tools` on the
agent MCP registration. This was a live gap before the fix, not a theoretical one — `tools/list`
against the agent instance advertised delete regardless, and a violation-injection test deleted a
note through it (21 bytes to 0) to prove the hole before closing it. *Where it is withheld superseded by [ADR-0059](./0059-one-holder-per-credential-access-record.md), pending its ratification.*

Where relocation happens, **order is load-bearing: write to the new path first, delete the source
second, never the reverse.** A crash between the two leaves a recoverable duplicate (the lint pass
already flags near-duplicates) rather than data loss —
[fail loud, destroy nothing](../../../DESIGN.md#fail-loud-destroy-nothing) applied to a two-step
operation.

## Alternatives considered

- Disabling delete server-side — costs the agent instance every write tool it needs (one bundled
  flag), or a fork of the server's tool index.
- Trusting path scope alone — path scope does not distinguish operations
  ([ADR-0005](./0005-path-scope-granularity.md)), so delete inside an allowed path would sail
  through.
- Delete-then-write relocation — strictly worse crash behaviour; rejected.

## Consequences

The restriction is per-handle, so it must be re-verified whenever gateway registrations change
(list tools *through* the gateway with a real key, not against the server directly). Deletes by the
ingestor side are currently uncounted anywhere — a known gap carried by
[D2](../../../ROADMAP.md#group-d--operability).
