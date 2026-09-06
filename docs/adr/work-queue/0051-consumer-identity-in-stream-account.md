# 0051. The consuming processor holds its own identity inside the stream-holding account

**Status:** Proposed ·
**Pillar:** [Authority is carried by capability](../../../DESIGN.md#authority-is-carried-by-capability-not-by-network-position) ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted) ·
**Unit:** [A2](../../../ROADMAP.md#group-a--pipeline-mechanisms) ·
**Tickets:** [ot#5](https://github.com/ppat/obsidian-tools/issues/5), [apps#3875](https://github.com/ppat/homelab-ops-kubernetes-apps/issues/3875)

## Context

[ADR-0047](./0047-subject-scheme-and-account-topology.md) cuts NATS accounts by message shape, and
its table is a *producer* table: what a message carries decides which account may publish it. It
places every stream in one account of which no producer is a member, and it leaves the other half
of the picture unstated — what identity the processor that drains a stream holds inside that
account.

None of the producer identities can serve. A producer's account imports one publish subject as a
service and cannot see a stream at all, so a producer credential has nothing to consume from. That
leaves the account's administrative identity, which is unrestricted across every stream in the
account — more authority than draining one stream needs, held by the workload that already writes
with the widest scope in the system.

## Decision

Each consuming processor holds its own user inside the stream-holding account, granted narrowly and
by literal subject: the streams it owns, its own durable consumer, the acknowledgement subject for
that consumer, and the dead-letter subject it publishes to. Stream update, deletion and purge are
withheld ([ADR-0050](./0050-stream-provisioner-create-not-update.md)), as is every subject naming
another processor's stream or consumer.

Three properties of that grant are not derivable from documentation and are the reason it is
enumerated rather than wildcarded:

- **The client library creates a named durable on the legacy consumer-create subject.** A grant
  naming only the modern `CONSUMER.CREATE.<stream>.<durable>` spelling is refused: the request goes
  to `CONSUMER.DURABLE.CREATE.<stream>.<durable>`, the broker logs a publish violation naming that
  subject, and the create call times out. Both spellings are granted, and the legacy one carries
  the traffic.
- **The subscribe grant needs two inbox forms, and granting one of them fails in a way that does
  not read as a permissions failure.** A pull subscription's own delivery subject is a two-token
  inbox; the request/reply inboxes JetStream's API answers on are three tokens. With only the
  three-token form the subscription itself is refused. With only the two-token form — the shape
  ADR-0047 gives producers — fetch, acknowledge and terminate all keep working while *every*
  JetStream API request and every dead-letter publish times out, with nothing on the client's error
  callback. That presents as an unreachable broker, not as a missing permission. Both forms are
  granted.
- **The account's JetStream info subject is granted so that violations stay meaningful.** The CLI
  the provisioner runs probes that subject on every invocation; without the grant the operation
  still succeeds and the broker logs a publish violation every run. A violation log line
  is the observable this design's authority boundaries are proven by
  ([ADR-0047](./0047-subject-scheme-and-account-topology.md)), and a routine violation on a
  schedule buries the ones that mean something.

The provisioner of [ADR-0050](./0050-stream-provisioner-create-not-update.md) holds this same
credential: one identity covers both the consumer's own traffic and the stream creation, and the
stream-creation subjects are in the grant for that reason.

## Alternatives considered

- **A producer credential.** Structurally unavailable rather than merely wide: producer accounts are
  not members of the account holding the streams, and an account cannot consume from a stream it
  cannot see.
- **The account's administrative identity.** Works immediately and removes the boundary that keeps
  one processor away from the other two streams. It is also the credential a stream-definition
  change requires, and keeping it out of every workload is what makes such a change an operator
  action ([ADR-0050](./0050-stream-provisioner-create-not-update.md)).
- **A wildcard over the JetStream API, scoped by stream name rather than by operation.** Shorter to
  write and re-admits precisely what is being withheld: update, deletion and purge are siblings of
  creation under the same prefix, so any wildcard broad enough to permit creating a stream permits
  destroying it. Enumeration is what makes the withheld subjects withheld.
- **A separate, narrower identity for the provisioner.** Strictly tighter for the consumer, which
  needs no stream creation of its own. It costs a second credential and a second stored plaintext,
  and it leaves the property that carries the authority argument — create but not update —
  unchanged, since the provisioner's grant would have to be the same either way.

## Consequences

- **The stream-holding account carries one consumer identity per consuming processor.** ADR-0047's
  table stays a producer table; a consuming processor's user is added to the account alongside the
  streams it drains, and `promotion-processor` and `drift-processor` each bring their own, naming
  their own streams and consumers.
- **A JetStream timeout in this system is a permissions hypothesis before it is a broker
  hypothesis.** A missing subscribe permission removes the channel a reply would return on, so the
  caller times out with nothing on its error callback — client-side indistinguishable from the
  silent drop ADR-0047 records for a missing account import, and from a broker that is genuinely
  down. Unlike the account-boundary refusal, this one leaves a server-side trace: the violation log
  is where the distinction lives, and it is the first thing to read.
- **Renaming a durable consumer or adding a stream is a grant change.** Every subject in the grant
  names its stream and its consumer literally, so nothing survives a rename by accident. A grant
  that survived a rename would equally survive a typo.
