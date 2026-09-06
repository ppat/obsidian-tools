# 0049. A processor whose run disables a gateway handle is scheduled, not supervised

**Status:** Proposed ·
**Pillar:** [Bulk and drift are feeds into the one write path](../../../DESIGN.md#bulk-and-drift-are-feeds-into-the-one-write-path-never-lanes-around-it) ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted) ·
**Unit:** [A2](../../../ROADMAP.md#group-a--pipeline-mechanisms) ·
**Tickets:** [ot#5](https://github.com/ppat/obsidian-tools/issues/5), [apps#3875](https://github.com/ppat/homelab-ops-kubernetes-apps/issues/3875)

## Context

`process-batch` is a run with a beginning and an end, and its first act has system-wide effect: it
disables the agent-facing gateway handle *before* it looks at the stream, drains what is there,
re-enables the handle, and exits. An empty stream costs one fetch and an idle timeout, so the
common run is short and the handle is down for its whole length.

Kubernetes offers two shapes for such a component — a controller that keeps a process running and
restarts it whenever it exits, or a schedule that starts a run and lets it finish. "A processor"
reads as the first, and the second is what the handle requires.

## Decision

`batch-processor` runs on a schedule, run-to-completion, with concurrent runs forbidden. Nothing
restarts it when it exits.

The deciding argument is what a restarting controller does to the handle. Under a controller, the
exit that ends a run is the event that starts the next one, and that next run's first act is to
take the handle down again. The handle would then be disabled essentially always: every
interactive agent write stops, indefinitely, and nothing reports it — the silent outage the
batch-mode watchdog exists to end ([ADR-0022](./0022-batch-stream-mechanics.md),
[D4](../../../ROADMAP.md#group-d--operability)), reproduced on a loop by the workload's own shape.

Two further properties follow only from a schedule. [ADR-0022](./0022-batch-stream-mechanics.md)'s
triggering policy — a run at least once a day whenever the stream is non-empty, and only inside a
permitted time window — has no expression in a controller, which has no vocabulary for a window.
And forbidding concurrency is what makes "runs alone" mechanical rather than incidental: a rolling
update of a controller runs two processes at once, both racing the same lease.

**This does not contradict [ADR-0023](./0023-streams-ship-with-processors.md).** That record says
`batch-processor` *runs alone*, and attaches the word Deployment to `promotion-processor` and
`drift-processor`, the two that share one. It never says it of `batch-processor`.

The property that decides the shape is the handle, not processor-ness. `promotion-processor` and
`drift-processor` disable nothing — promotion drains through the ingestor handle mid-batch by
design — so they take the shape ADR-0023 gives them.

## Alternatives considered

- **A controller that restarts the process.** Fails on the handle, above. It becomes available only
  if `process-batch` changes first, to a loop that leaves the handle up while there is no work —
  a decision about what the processor does, not about how it is run, and one that trades the
  window for a poll.
- **A single run-to-completion job, created once.** Does not reconverge: its pod template is
  immutable, so a definition change needs a new name, and a completed job does not run again when
  the next occasion for a run arrives.
- **A controller with the handle taken per chunk rather than per run.** Narrows the outage to one
  chunk's length, at the cost of what disabling the handle for a whole run buys: an interactive
  agent write may then land between two chunks of the same batch, against the paths a later chunk
  of that batch is about to touch. It also spends two gateway administrative round trips per chunk
  on a run measured in thousands of chunks.

## Consequences

- **The readiness assertion a supervised workload gets for free is unavailable.** A
  Deployment-Ready check has nothing to wait on for a run that has not fired, and this workload's
  dependencies — a broker on a load-balancer hostname, a gateway key held in the gateway's own
  store — are not ones a CI cluster can supply, so it cannot be made to run there either. That is
  the ceiling the committer's workload sits at, for the same reason
  ([ADR-0030](../replication/0030-committer-shape.md)): manifest-level CI can prove shape and
  environment, never behaviour. Behaviour is proven in this repository's own suite, against a real
  broker.
- **A failed run is not retried.** `process-batch` exits non-zero when it dead-lettered a chunk,
  which is a reported outcome rather than a transient fault: re-running takes the handle down again
  and changes nothing. The retry budget is therefore zero, and a run's outcome is read from its
  exit status and its counts.
- **A maximum run duration is not imposed by the schedule.** Bounding a run is a designed mechanism
  with its own behaviour on expiry — the handle must be restored, not merely abandoned — and a
  timeout set on the workload would be that mechanism, undocumented.
- **The tickets that scope the work are not the authority on the workload's shape.** Where a
  ticket and this record differ on it, this record governs.
