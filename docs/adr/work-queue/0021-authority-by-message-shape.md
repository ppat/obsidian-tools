# 0021. Enqueue authority follows message shape, carried by per-producer NATS credentials — never by network position

**Status:** Accepted ·
**Pillar:** [Authority is carried by capability](../../../DESIGN.md#authority-is-carried-by-capability-not-by-network-position) ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted)

## Context

The original rule was a flat allowlist: only the Coder workspace may enqueue. Correct for the batch
stream, but over-general as a principle — the escalation risk comes from the *processor's handle*,
not from queueing itself. And the moment NATS has an ingress (it must:
[`local-replicator`](../../../DESIGN.md#device-loop-and-read-path) publishes from off-cluster), a
NetworkPolicy stops distinguishing producers at all: it selects on pod and namespace, has no notion
of *stream*, and sees every ingress client identically.

## Decision

What a message **carries** decides who may enqueue it, and the decision is enforced by a distinct
NATS account per producer population, subject-scoped to the one stream it may publish
([the stream table in DESIGN §3](../../../DESIGN.md#3-the-write-path-end-to-end)):

- **Patch-carrying** (batch): content *and* destination — the enqueuer effectively writes with the
  processor's own handle, so exactly one producer holds the credential, and which client it is issued to — attended or
  not — is an issuance decision. Consequence stated plainly: **restructuring the vault is confined
  to that one credential's holder.**
- **Pointer-carrying** (promotion): confers nothing — *provided* the processor refuses any pointer
  naming a path outside the enqueuer's own scope. Without that check, a prompt-injected agent could
  point the widest handle at curated content; with it, every interactive agent may announce a write.
- **Content-carrying, fixed destination** (drift): confers nothing — the destination is the
  processor's, never the message's; sole producer by credential.

Credentials must exist **before** a stream is stood up, not as hardening afterwards; NetworkPolicy
remains defence in depth for in-cluster traffic, never the control.

## Alternatives considered

- The flat allowlist enforced by NetworkPolicy — correct only until the ingress exists; then
  structurally blind.
- Per-producer path scoping inside `batch-processor` — duplicates what path scope already does
  elsewhere.
- Per-producer streams — reintroduces the sharding already rejected on its own grounds
  ([ADR-0022](./0022-batch-stream-mechanics.md)).

## Consequences

The decisive acceptance test is **credential-shaped, not network-shaped**: a legitimately held
credential publishing to a subject outside its grant, refused — the only injection that
distinguishes real subject permissions from a NetworkPolicy-only implementation, because in-cluster
producers would be refused by the network anyway, for a reason unrelated to the control under test.
Credential-minting is separable from stream-standing, so those tests unlock before any processor
exists ([ROADMAP](../../../ROADMAP.md#orderings-that-would-guarantee-waste)).
