# 0003. Two MCP instances × two gateway handles; a third instance deferred; credential choices

**Status:** Accepted ·
**Pillar:** [Authority is carried by capability](../../../DESIGN2.md#authority-is-carried-by-capability-not-by-network-position) ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted)

## Context

Two different questions need enforcing on every write: *who may call, with which tools* and *where
may a write land*. The gateway's tool filtering is server-level and confirmed **not** path-scoped,
so one layer cannot answer both. Path scope lives on the MCP server instance
(`OBSIDIAN_WRITE_PATHS`); tool visibility lives on the gateway handle.

## Decision

Handles and instances are separate axes, two of each:

| Axis | Narrow | Wide |
| --- | --- | --- |
| Gateway **handle** (who may call) | Agent handle: every interactive writer and `drift-processor` | Ingestor handle: `promotion-processor`, `batch-processor`, the lint pass |
| MCP **instance** (where a write lands) | Agent instance: the agent zone only | Ingestor instance: the processors' and lint's full reach |

Each distinct path scope needs its own instance, not merely its own handle: two handles onto one
instance would leak that instance's path scope to whoever held the other handle. Tool grants are
per client on the agent handle, and two are worth recording: **n8n gets a narrow write grant, not
read-only** — the founding research said read-only, but its daily-organise workflow needs writes,
resolved as frontmatter-status updates plus appends to the log and nothing else (no queue access,
and no write path to the global todo, which holds queries only); and **the human-facing browser
chat is read-only** — the human is not a writer
([the pillar](../../../DESIGN2.md#humans-originate-agents-act)). Batch runs disable
the agent handle only; the ingestor handle stays live, which is what lets promotion keep draining
mid-batch.

**A third, promotion-scoped instance is deferred deliberately.** It would be strictly
least-privilege for `promotion-processor` (which never creates in the raw layer or archives), but
every instance costs a full provisioning chain — its own Deployment, signing secret, pre-minted JWT,
gateway registration and secret-store entry — paid at setup and on every rotation. Three accepted
consequences are recorded rather than hidden: `promotion-processor` runs wider than it needs; batch
and promotion contend inside the shared instance (sharpening the case for backpressure,
[ADR-0022](../work-queue/0022-batch-stream-mechanics.md)); and the promotion enqueue surface fronts
the widest handle, bounded by the processor's own pointer check rather than instance scope. Revisit
on the first incident implicating the extra width, or when provisioning stops being a manual chain.

**Credential choices riding on the same provisioning chain:**

- **Tokens are deliberately non-expiring.** No revocation or denylist exists anywhere in this stack;
  only rotating the signing secret invalidates tokens, identically with or without `exp` — so expiry
  would add a deadline, not a capability, and its failure mode is a scheduled, silent, platform-wide
  outage. The honest counter-argument (a bearer token is more exposed than the secret, since it
  transits the gateway and gateways log requests) points at the real gap — no visibility into MCP
  auth failures — which needs closing either way
  ([D2](../../../ROADMAP.md#group-d--operability)). Rotation triggers on an event (exposure,
  re-scoping), never on a date.
- **Server-side scope checks are disabled** (`MCP_AUTH_DISABLE_SCOPE_CHECKS=true`): the gateway
  sends one static token per registered server, so a scope claim cannot distinguish callers —
  distinguishing callers is the handle's job. Signature, audience, issuer and expiry validation stay
  on.

## Alternatives considered

- One instance, two handles — leaks path scope across handles; rejected.
- Building the third instance now — rejected on provisioning cost before its consumer exists.
- Expiring tokens — rejected as above; recorded with its counter-argument.

## Consequences

Five secrets, not three (two of them pre-minted JWTs that are easy to forget); the widest handle in
the system is shared by three components, so its scope is part of three components' blast radius;
and the delete tool's placement becomes a per-handle decision
([ADR-0004](./0004-delete-withheld-relocation-order.md)).
