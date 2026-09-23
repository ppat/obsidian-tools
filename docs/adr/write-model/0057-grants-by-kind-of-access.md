# 0057. Grants on the handles are cut by kind of access, never by named client

**Status:** Proposed (supersedes the per-client tool grants in [ADR-0003](./0003-two-instances-two-handles.md), the client grant `log.md` mirrored in [ADR-0045](./0045-write-scope-composition.md), the log row's tool-scope holding in [ADR-0005](./0005-path-scope-granularity.md), and issuance as a named producer's connection unit in [ADR-0023](../work-queue/0023-streams-ship-with-processors.md)) ·
**Pillar:** [Clients are known by credential, never by name](../../../DESIGN.md#clients-are-known-by-credential-never-by-name) ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted)

## Context

[ADR-0003](./0003-two-instances-two-handles.md) separates the handles (who may call, with which
tools) from the instances (where a write may land). What remains is how tool grants on a handle are
cut. The owner's standing ruling is that the vault system does not know its clients, nor which of
them run batch jobs, so a grant cannot be a statement about who a client is or what workflow it runs.
The queue answers the same question for its producers: NATS accounts are cut by message shape, and a
new holder is never a reason to mint one
([ADR-0047](../work-queue/0047-subject-scheme-and-account-topology.md)).

## Decision

**A credential on a handle is issued as one of a fixed set of kinds, each defined by what it can do.**
On the handles, clients receive only these kinds on the agent handle. The third client kind,
announce, is a queue credential ([ADR-0047](../work-queue/0047-subject-scheme-and-account-topology.md)).

| Kind | Handle | Tools | Where a write may land |
| --- | --- | --- | --- |
| **Read** | agent | read and search only | nowhere |
| **Interactive write** | agent | read, search, and the create/append/patch/write primitives; never delete, never command execution | the agent zone — the agent instance's scope ([ADR-0045](./0045-write-scope-composition.md)) |

The ingestor handle is never issued to a client: it carries the vault system's own components,
which hold their credentials as components, not as kinds.

- **Which client holds which kind is issuance** — a change to the installation's infrastructure code,
  authored by an agent and landed by the owner's merge, outside the design's grants, like issuing a
  queue credential of a given shape. Each holder gets its own credential of its kind, never a
  shared one ([ADR-0059](./0059-one-holder-per-credential-access-record.md)). A new client never adds a kind; a new kind is a design change,
  recorded here.
- **The narrow status-and-log grant is not a kind.** A client that appends to the log or updates a
  status field inside the agent zone holds interactive write, whose reach already covers both.
- **A surface serving a human directly being read-only** is a property of how that surface's
  credential was issued, not a rule the design can state about a client it does not know.

## Alternatives considered

- **Per-client grants** — for example a workflow-automation client holding a narrow write grant for
  status updates and log appends because its daily-organise workflow needs exactly those, and a
  human-facing browser chat held read-only. A roster: every new client is a design change, and the
  grant encodes a workflow the vault system does not depend on and cannot know exists.
- **One kind for every client** — collapses read-only into write, giving every reader a write grant
  it has no use for.
- **A catalogue of narrow per-purpose kinds** (status-only, log-only, …) — a roster by another name:
  each kind exists because some particular client wanted exactly it.

## Consequences

- **[ADR-0003](./0003-two-instances-two-handles.md)'s instance, handle and credential decisions are
  untouched**; this record decides only how grants on a handle are cut.
- **The log's agent-scope entry mirrors a kind, not a client.** `log.md` sits in the agent instance's
  scope because the agent zone includes the log, and interactive write reaches the whole agent zone
  ([ADR-0045](./0045-write-scope-composition.md)).
- **Delete stays withheld from the agent handle for every kind**
  ([ADR-0004](./0004-delete-withheld-relocation-order.md)); a violation injection proving it is keyed
  to a kind, never to a client.
