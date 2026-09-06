# 0020. The work queue is NATS JetStream, chosen over RabbitMQ on operational weight

**Status:** Accepted ·
**Unit:** [A1](../../../ROADMAP.md#group-a--pipeline-mechanisms) ·
**Tickets:** [apps#3444](https://github.com/ppat/homelab-ops-kubernetes-apps/issues/3444), [ot#5](https://github.com/ppat/obsidian-tools/issues/5)

## Context

The design's requirement was always the behavioural contract, not a product: ordered consumers
(a rename and its link rewrites must apply in sequence), per-message acknowledgement, and a
dead-letter path for messages that fail repeatedly. The choice was deliberately deferred until the
batch-stream code needed unblocking.

## Decision

NATS JetStream, as a single small Go binary running as a single-replica Deployment with a volume.
The deciding ground is operational weight: the deployment repo has no StatefulSet anywhere, and
RabbitMQ effectively requires one plus an Erlang runtime — a heavy first for the sake of a queue.
JetStream supplies ordered consumers, per-message acks, and a max-delivery threshold feeding a
dead-letter path per stream.

## Alternatives considered

- **RabbitMQ** — richer, better-trodden dead-lettering (a first-class exchange versus JetStream's
  advisory-based equivalent), at the cost of the repo's first StatefulSet plus Erlang. The accepted
  trade is recorded honestly: **the dead-letter path here needs deliberate construction rather than
  coming free**, and if JetStream's proves fiddly, RabbitMQ is the named fallback at that cost.
- **No queue** (interactive-only writes) — leaves bulk work and drift capture with no durable,
  ordered transport; both need one.

## Consequences

Three streams ride one deployment ([ADR-0023](./0023-streams-ship-with-processors.md)); the ingress
that drift needs makes credential-carried authority mandatory
([ADR-0021](./0021-authority-by-message-shape.md)); per-stream depth/ack/nack/dead-letter metrics
are acceptance criteria on [A1](../../../ROADMAP.md#group-a--pipeline-mechanisms) because they are
queue-native facts no envelope parsing can miss.

**The construction the dead-letter path needs has one constraint that is easy to get wrong and
silent when it is.** Dead-lettering here is a republish: a chunk that has exhausted its delivery
budget, or failed for a reason redelivery cannot change, is published to a dead-letter subject and
then terminated — the terminate is what makes it a path rather than a loop, the copy is what makes
it a path rather than a deletion. **The dead-letter subject must therefore lie outside the source
stream's own subject list.** Inside it, the republish puts the chunk back on the stream it was just
taken from, the consumer that gave up on it receives it again with its delivery count reset, and
the loop is infinite, unbounded and indistinguishable from ordinary redelivery. The check is
token-wise rather than string-wise — `batch` and `batchx` share a string prefix and no subjects at
all, while `batch` and `batch.dead` share every subject under the second — and it is made when the
processor reads its configuration, so a stream and a dead-letter stream whose subjects overlap stop
the processor at startup instead of consuming each other's messages.
