# 0050. Streams are created by a scheduled provisioner holding stream creation but not stream update

**Status:** Proposed ·
**Pillar:** [Authority is carried by capability](../../../DESIGN.md#authority-is-carried-by-capability-not-by-network-position) ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted) ·
**Unit:** [A2](../../../ROADMAP.md#group-a--pipeline-mechanisms) ·
**Ticket:** [apps#3875](https://github.com/ppat/homelab-ops-kubernetes-apps/issues/3875)

## Context

[ADR-0023](./0023-streams-ship-with-processors.md) makes each stream an attribute of its consuming
processor and leaves the substrate underneath carrying none. It fixes *when* a stream comes into
existence relative to its processor, and says nothing about *by what* — which is not a detail,
because NATS has no declarative answer. A stream cannot be expressed in server configuration; it is
created over the wire by a client holding the JetStream API for it. Something has to hold that
authority and run.

The authority is not small. The JetStream API subjects for a stream cover its update as well as its
creation, and a stream's configuration includes its subject list — so a client that may update a
stream may widen the set of subjects that stream accepts.
[ADR-0047](./0047-subject-scheme-and-account-topology.md) withholds the JetStream API from every
producer for exactly that reason, and the reasoning does not stop at producers.

## Decision

Streams are created by a **scheduled provisioner that reconciles**: it runs on a short fixed
interval, creates each stream it owns if that stream is absent, and leaves it alone if it is
present. It holds the consuming processor's own credential rather than the stream-holding account's
administrative one, and that credential is granted stream **creation** and deliberately **not**
stream **update**.

Idempotence is the broker's property here, not the provisioner's code. Creating a stream that
already exists with an identical configuration succeeds; creating one that already exists with a
changed configuration is refused, with the existing stream's configuration unchanged afterwards.
Without the update grant there is no second path to the same effect — an in-place edit is refused
as a publish violation naming the stream-update subject. The workload therefore cannot widen its
own stream's subject list, and it cannot do so by mistake either.

Holding the processor's credential rather than an administrative one is what keeps the blast radius
at one processor's streams. The administrative identity is unrestricted inside the account that
holds *every* stream, so a provisioner carrying it could purge or reshape the promotion and drift
streams, which have nothing to do with it. The processor's credential names its own streams and its
own consumer literally, so the provisioner cannot address any other stream at all — and it
introduces no credential the processor does not already need.

## Alternatives considered

- **An init container on the processor's pod.** Couples the stream's existence to the processor's
  schedule. A producer may enqueue at any time, and an enqueue before the processor has ever run
  finds no stream and fails with no responders. A stream that arrives *with* its processor
  ([ADR-0023](./0023-streams-ship-with-processors.md)) is not a stream created *by* it.
- **A single job, run once.** Creates the stream and never runs again. A lost JetStream volume takes
  the stream with it and nothing re-creates it; the queue is then absent, and the first symptom is
  a producer's publish failing.
- **Folding provisioning into the batch-mode watchdog.** The watchdog's entire value is that it
  depends on nothing `batch-processor` depends on — it restores the agent handle when the processor
  has died, so a watchdog that needs the broker fails in the conditions it exists to survive. A
  NATS dependency there removes the property [D4](../../../ROADMAP.md#group-d--operability) is for.
- **Granting the provisioner stream update, to reconcile stream configuration the way manifests
  reconcile everything else.** It buys convergence on a change that is rare and deliberate, and it
  costs the single largest authority in the write path: the component with the widest write scope
  in the system would hold the power to widen its own subject list to everything — the same power
  [ADR-0047](./0047-subject-scheme-and-account-topology.md) withholds from every producer, granted
  instead to the workload that applies the patches.
- **An administrative credential.** Works on the first run and every run after, and removes the
  boundary between one processor's provisioning and the other streams in the account.

## Consequences

- **A stream-definition change fails loudly rather than converging.** Changing a stream's declared
  configuration produces a failing provisioner run at every interval until an operator applies the
  change with the administrative credential. A stream's shape is thus an operator action by
  construction, and the failing run is the notification.
- **The dead-letter stream is created by the same provisioner as the stream it serves.**
  Dead-lettering is a republish, so it needs a stream covering the dead-letter subject
  ([ADR-0020](./0020-nats-jetstream.md)); a dead-letter publish with no stream behind it fails with
  no responders, and a chunk that cannot be dead-lettered cannot be settled at all. The two streams
  are one provisioner's responsibility and come into existence together.
- **Each stream a processor owns is named literally in its credential's grant**, so adding a stream
  is a reviewed grant change rather than something an existing grant already covers.
- **Convergence has an interval rather than a trigger.** A stream absent for any reason — a first
  run, a restored volume, a manual deletion — returns without human action within one interval, and
  that is the whole of the recovery procedure.
