# 0059. Every credential has one holder, and the vault's own access gate verifies it, enforces its tool grant and records the call

**Status:** Proposed (supersedes the disabled scope checks and the no-revocation token premise in [ADR-0003](./0003-two-instances-two-handles.md); where [ADR-0004](./0004-delete-withheld-relocation-order.md) withholds delete; where [ADR-0041](../platform/0041-mcp-stack-selection.md) enforces tool grants; who holds the patch-shape credential in [ADR-0021](../work-queue/0021-authority-by-message-shape.md); and one shared credential per shape in [ADR-0047](../work-queue/0047-subject-scheme-and-account-topology.md)) ·
**Pillar:** [Clients are known by credential, never by name](../../../DESIGN.md#clients-are-known-by-credential-never-by-name) ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted) ·
**Unit:** [A10](../../../ROADMAP.md#group-a--pipeline-mechanisms)

## Context

[S1](../../../USE_CASES.md#s1--admitted) requires every admitted write to be attributable to the
authority that made it. The vault system knows a client only by its credential
([ADR-0057](./0057-grants-by-kind-of-access.md)). The owner has ruled that a credential is defined
by its type, and that which concrete client holds it is a question of issuance, not of the
substrate. Attribution therefore needs two things: each credential names exactly one holder, and
something the design *requires* records which credential made each call.

The design must work with or without an LLM gateway in front of the vault. Four facts constrain it:

- **The MCP server cannot withhold one tool alone** ([ADR-0004](./0004-delete-withheld-relocation-order.md),
  measured). A per-credential tool grant cannot live in the server.
- **This installation's gateway presents one static token per registration**
  ([ADR-0003](./0003-two-instances-two-handles.md)). Its keys are issued through infrastructure code
  ([terraform#338](https://github.com/ppat/homelab-ops-terraform/pull/338)), and a key is
  restricted to named tools on its handle's registration [measured]. Per-holder confinement is
  unmeasured.
- **Tokens signed with one shared secret, with no denylist, can only be revoked all at once**
  (ADR-0003).
- **Kubernetes RBAC and the network do not identify callers.** Only the credential does.

## Decision

**One holder per credential.** A kind of access, or a message shape, is the *grant* a credential
carries, and each holder is issued its own credential with that grant. On the queue, each holder is
its own user in its shape's account; accounts stay cut by shape
([ADR-0047](../work-queue/0047-subject-scheme-and-account-topology.md)). Each of a component's roles
holds its own credential. The patch-shape credential is a component credential that only the batch
producer presents, in runs the owner authorised.

**The credential registry names no client.** It maps an opaque credential identifier, assigned at
issuance, to its grant and to whether it is active or revoked. Which client holds which identifier
exists only in the installation's issuance code, outside the vault system. The access record names
identifiers, never clients.

**The access gate is a vault component in front of each MCP instance**, and every call to the
instance passes through it. For each call, the gate:

1. verifies the credential against the registry;
2. enforces its tool grant — on the agent instance, delete is withheld from every kind;
3. writes the call durably to the **access record** before forwarding it: identifier, tool, target
   path, time, and afterwards the instance's answer, refusals included. If that write fails, the
   call is refused.

The gate never decides *where* a write may land; the instance's path scope keeps that (Gate 2).
Each instance accepts calls only from its gate.

**Revocation is per holder.** Removing an identifier from the registry refuses that holder at once
and affects no one else. **Issuance runs through infrastructure code**, authored by an agent and
landed by the owner's merge: minting, storing and registering follow from the merged change with no
manual step.

**How calls reach the gate.**

- **The gate's own endpoint.** Each gate is a cluster Service that terminates TLS with a certificate
  from the cluster's issuer, since a credential is presented to it. A network policy admits the
  namespaces carrying a label applied at issuance — so not even the vault's manifests list
  clients — and the cluster ingress, with TLS, for clients outside the
  cluster. That policy is defence in depth: the credential is the authority.
- **Topologies:**

  | Topology | How the holder's own credential reaches the gate |
  | --- | --- |
  | No front | The client calls the gate's endpoint and presents its vault credential |
  | A front that passes each request's vault credential through unchanged | The client calls the front with its vault credential; the front forwards it. No credential gathers in the front |

- **This installation's gateway** is measured when [A10](../../../ROADMAP.md#group-a--pipeline-mechanisms)
  is built. If it can pass the credential through, clients keep their one gateway endpoint. If it
  cannot, vault calls do not go through it: clients call the gates' endpoint directly, configured as
  a second endpoint beside the gateway, which stays in place for everything else. Both outcomes are
  what "works with or without LiteLLM" means here: the vault never depends on the gateway, and the
  gateway, where present, never stands between a holder and its own credential.
- **Through a front, attribution is only as good as the front's forwarding.** A front that swapped
  one caller's credential for another's would misattribute. That residue is stated, not closed.

## Alternatives considered

- **Enforcement inside the MCP server** — the server has no per-tool switch.
- **Requiring a separate front** — the design must work without one.
- **A registry that names clients** — the vault system would then know its clients.
- **One shared credential per kind, or a front holding a single credential for everyone** — the
  gate could not tell holders apart.
- **A front holding one registration per holder, with every holder's credential** — it works, but
  it gathers every credential in one component and adds a mapping that can be wrong. Pass-through,
  with a direct endpoint as the fallback, covers the same need without either.
- **Revoking by rotating the signing secret** — every holder loses access to revoke one.
- **Path rules in the gate** — "where may a write land" has one authority, the instance's scope.

## Consequences

- **The access gate is a component**, and Gate 1's enforcement point.
- **Refusals that come back as HTTP 200 become countable.** The gate records the instance's answer,
  which serves [D2](../../../ROADMAP.md#group-d--operability).
- **An admitted write whose credential the access record cannot name falsifies S1.** The record is
  written durably, before the call it describes, and is kept for the whole window
  [O1](../../../USE_CASES.md#o1--measured) answers for.
- **The gate is on every call's path.** Its failure modes have resolvers
  ([ADR-0064](../operability/0064-operational-conditions-have-resolvers.md)).
