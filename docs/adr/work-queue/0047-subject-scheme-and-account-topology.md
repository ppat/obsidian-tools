# 0047. One subject namespace and one NATS account per message shape, with private service imports and no JetStream API for any producer

**Status:** Accepted — the pointer check's formulation superseded by [ADR-0056](./0056-inbox-is-promotions-work-list.md), and one shared credential per shape by [ADR-0059](../write-model/0059-one-holder-per-credential-access-record.md) (both Proposed), marked where they stand ·
**Pillar:** [Authority is carried by capability](../../../DESIGN.md#authority-is-carried-by-capability-not-by-network-position) ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted)

## Context

[ADR-0021](./0021-authority-by-message-shape.md) establishes that enqueue authority follows what a
message carries, enforced by a distinct NATS account per producer population, subject-scoped to the
one stream it may publish to. It does not fix the subjects themselves or the arrangement of the
accounts. Those are independently reversible from the principle — the concrete namespace and account
shapes could change entirely while "authority follows message shape" stands — so they take their own
record.

## Decision

One top-level subject token per stream, and one account per **message shape**:

| Account | Message shape | What enqueueing confers | Publishes to |
| --- | --- | --- | --- |
| patch-carrying | a git patch — content *and* destination | the consuming processor's own write scope, the widest in the system | `batch.>` |
| pointer-carrying | a pointer to something already written in the inbox | nothing, given the processor refuses a pointer outside the enqueuer's scope | `promotion.>` |

*The pointer check's formulation superseded by [ADR-0056](./0056-inbox-is-promotions-work-list.md), pending its ratification.*
| content-carrying, destination fixed by the processor | content whose destination is not the message's to choose | nothing | `drift.>` |

The account is cut by what the message carries, never by who holds the credential. A new holder is
therefore never a reason to mint an account: a holder of an existing shape takes that shape's
credential, and issuance to a named holder is a connection question rather than a substrate one.
*One shared credential per shape superseded by [ADR-0059](../write-model/0059-one-holder-per-credential-access-record.md), pending its ratification.*

Streams live in one account holding every stream and every consuming processor; no producer is a
member of it. Each producer account imports exactly one subject **as a service** — service rather
than stream, because a JetStream publish is request/reply and the PubAck must cross the account
boundary back. Exports name their importing account, which makes them private: only that account may
import, and that is a scoping layer a mistake in a user's permission block cannot undo.

**No producer is granted access to the JetStream API subject space.** An acknowledged publish never
touches it, and granting it would hand every producer stream creation, update, deletion and purge
across the account — including the power to widen its own stream's subject list to everything.

Two findings, both measured, decide the shape rather than merely decorating it:

1. **The account boundary and the per-user publish allow-list fail differently, and only one fails
   audibly.** A user with no permission block, sitting in a correctly-importing account, is still
   isolated from subjects its account never imported — but the refusal is a silent drop: the client
   sees a no-response error, nothing reaches its error callback, and the server logs nothing at all.
   That is indistinguishable from a missing stream or a crashed broker. A per-user publish allow-list
   turns the same refusal into a `Publish Violation` naming account, user, connection id and subject.
   A project whose acceptance standard is proving a control by making it fire cannot rest on a
   control that fires silently, so the permission layer is load-bearing for the verification
   catalogue rather than defence in depth over the account boundary.

2. **Withholding the JetStream API is what makes the subject grant hold.** With only its publish
   grant, a credential publishes successfully and receives a real acknowledgement, while an attempt
   to create a stream subscribing to everything is refused and the existing stream's subject list is
   unchanged afterwards.

## Alternatives considered

- **One account with a user per message shape.** Measured to produce identical client-visible
  behaviour on every decisive case: the account boundary changes nothing an authorised client can
  observe. The difference is entirely in how a mistake fails — with one account, per-user permissions
  are the sole control, and a typo in one user's block is unguarded. With an account per shape, a
  private import sits underneath it.
- **A shared subject prefix**, `brain.batch.>` and so on. Costs a token on every subject and buys
  nothing: nothing else occupies the root namespace, and the JetStream API, system and inbox subject
  spaces are all reserved-prefixed, so a bare top-level token cannot collide with them.
- **`batch.*` rather than `batch.>`.** Strictly tighter, and correct if the token count under a
  stream's prefix is ever fixed. `>` is chosen to leave the trailing token free for routing, and
  narrowing later is a one-word change to the credential and the stream's subject list together.
- **The decentralized JWT model**, which the upstream documentation reaches for first. Rejected on a
  build consequence rather than an authority one: client-side nonce signing pulls a compiled
  cryptography dependency into a package whose container image builds for two architectures from a
  single builder stage whose virtual environment is copied into both, invalidating that assumption.
  The static model installs with no transitive dependencies and preserves it. The authority
  properties are equivalent.

## Consequences

- **The decisive injection is observable at the broker.** A legitimately held credential publishing
  outside its own subject produces a `Publish Violation` log line naming account, user, connection id
  and subject — a queue-native fact obtainable without instrumenting any producer. This is unlike the
  write gate, whose refusals arrive as HTTP 200 with the error inside the JSON-RPC envelope and are
  invisible to any HTTP-level metric. The queue and the gate need different observation strategies
  for the same class of claim.

- **An acknowledged publish requires that the client have a reply channel, and that grant is a client
  mechanism rather than an authority.** A JetStream publish is a request; the client opens one reply
  inbox per connection and the acknowledgement returns on it, so a credential permitted to subscribe
  nowhere cannot receive its own acknowledgement — the publish then fails with a *subscription*
  violation and a client-side timeout, which reads as "broker unreachable" to whoever debugs it. The
  grant is therefore scoped per credential, to a reply-inbox prefix that credential's client is
  configured to use, so that one credential's acknowledgements are not readable by another's. Its
  floor is per-credential and not per-connection: the connection identifier inside the inbox subject
  is generated by the client, and no static server configuration can pin it. Two connections
  presenting the same credential share a prefix — a property of a shared credential, not of the
  grant.

- **Two spellings of that grant read as tightening and are the widest possible permission**, both
  measured: omitting the subscribe block entirely leaves subscribe unrestricted, and an allow-list
  present but empty means no restriction rather than no subscription. A credential in either state
  was measured subscribing to the batch subject and receiving another producer's patch bytes live.
  The spelling that grants no subscription at all is an explicit deny of everything.

- **Permission changes apply to already-connected clients on a reload signal**, without a restart or
  a client reconnect, so revocation is a configuration change plus a signal rather than a
  redeployment.

- **The account holding the streams also holds consumer identities, and the table above does not
  describe them.** The table cuts accounts by what a message carries, which is a property producers
  have and consumers do not. A processor draining a stream holds its own user inside the
  stream-holding account, granted by literal subject and never administratively
  ([ADR-0051](./0051-consumer-identity-in-stream-account.md)).

- **The client port terminates TLS**, and this follows from two things already decided here rather
  than being a choice of its own. Authentication is static-account, so a credential's password
  crosses the connection on CONNECT; and the broker is reachable from outside the cluster, because
  `local-replicator` publishes drift from a device that is not in it
  ([ADR-0021](./0021-authority-by-message-shape.md)). Those two together put the whole authority
  model in clear text on a network the cluster does not own. This is not a point of difference
  against the decentralized JWT model rejected above — a signed nonce protects the credential but
  not the traffic, so that model carries the identical obligation. One consequence of the
  consequence is worth stating because it is invisible until it is noisy: a health probe that only
  opens a socket against a TLS port completes a connection it never negotiates, and the broker logs
  a handshake failure on every probe forever, so probes go to the broker's HTTP monitoring endpoint
  instead.
