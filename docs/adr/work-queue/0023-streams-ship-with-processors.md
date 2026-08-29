# 0023. Streams ship with their processors and credential grants; the substrate alone carries no streams

**Status:** Accepted ·
**Unit:** [A1](../../../ROADMAP.md#group-a--pipeline-mechanisms) ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted)

## Context

"Stand up the queue" reads like one unit of work. The design refuses one specific state: a stream
**reachable by an interactive agent with no consumer and no credential control behind it** — a
durable place for messages to accumulate with nothing entitled to drain them and nothing scoping
who may fill them.

## Decision

Each stream is an attribute of its consuming processor, shipped *together with* that processor and
its subject grant — never created bare. What is genuinely separable is the layer underneath: a NATS
deployment with an ingress and account machinery but **zero streams** is reachable and grants
nothing, which is not the forbidden state. Hence
[A1](../../../ROADMAP.md#group-a--pipeline-mechanisms) is substrate-and-credentials only, and
[A2](../../../ROADMAP.md#group-a--pipeline-mechanisms)/[A3](../../../ROADMAP.md#group-a--pipeline-mechanisms)/[A7](../../../ROADMAP.md#group-a--pipeline-mechanisms)
each carry their own stream. One further split keeps it honest: the subject **grant** ships with
the stream; the **issuance** of a credential to a named producer is that producer's connection unit
([Group B](../../../ROADMAP.md#group-b--connection-work)).

## Alternatives considered

- "The queue" as one unit — crosses a seam the design treats as load-bearing, and re-creates the
  reachable-unconsumed-stream state for whichever stream finishes first.
- Deferring credentials to a hardening pass — the moment the queue is reachable, an unscoped
  credential is already the wrong shape ([ADR-0021](./0021-authority-by-message-shape.md)).

## Consequences

Deploy-side workloads split on the credential boundary, not on traffic volume: `batch-processor`
runs alone (its stream's producer credential is the one the design is most protective of);
`promotion-processor` and `drift-processor` share a Deployment (their streams confer nothing on
enqueuers). A drift stream standing before its dispatcher would only accumulate patches nothing can
dispatch — which is why the drainer discards until
[V5](../../../ROADMAP.md#v5--humans-on-devices).
