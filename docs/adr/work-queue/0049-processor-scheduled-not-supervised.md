# 0049. A processor whose run stops the agent MCP instance is scheduled, not supervised

**Status:** Accepted ·
**Pillar:** [Bulk and drift are feeds into the one write path](../../../DESIGN.md#bulk-and-drift-are-feeds-into-the-one-write-path-never-lanes-around-it) ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted) ·
**Unit:** [A2](../../../ROADMAP.md#group-a--pipeline-mechanisms) ·
**Tickets:** [ot#5](https://github.com/ppat/obsidian-tools/issues/5), [apps#3875](https://github.com/ppat/homelab-ops-kubernetes-apps/issues/3875)

## Context

`process-batch` is a run with a beginning and an end, and its first act has system-wide effect: it
stops the agent MCP instance *before* it looks at the stream
([ADR-0052](./0052-batch-mode-stops-the-agent-instance.md)), drains what is there, starts the
instance again, and exits. An empty stream costs one fetch and an idle timeout, so the common run is
short and the interactive door is shut for its whole length.

Kubernetes offers two shapes for such a component — a controller that keeps a process running and
restarts it whenever it exits, or a schedule that starts a run and lets it finish. "A processor"
reads as the first, and the second is what a run-scoped outage requires.

## Decision

`batch-processor` runs on a schedule, run-to-completion, with concurrent runs forbidden. Nothing
restarts it when it exits.

The deciding argument is what a restarting controller does to the interactive door. Under a
controller, the exit that ends a run is the event that starts the next one, and that next run's
first act is to stop the agent instance again. The instance would then be down essentially always:
every interactive agent write stops, indefinitely, and nothing reports it — the silent outage the
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

The property that decides the shape is the run-scoped outage, not processor-ness.
`promotion-processor` and `drift-processor` stop nothing — promotion drains through the ingestor
instance mid-batch by design — so they take the shape ADR-0023 gives them.

## Alternatives considered

- **A controller that restarts the process.** Fails on the interactive door, above. It becomes
  available only if `process-batch` changes first, to a loop that leaves the agent instance up while
  there is no work — a decision about what the processor does, not about how it is run, and one that
  trades the window for a poll.
- **A single run-to-completion job, created once.** Does not reconverge: its pod template is
  immutable, so a definition change needs a new name, and a completed job does not run again when
  the next occasion for a run arrives.
- **A controller with the door shut per chunk rather than per run.** Narrows the outage to one
  chunk's length, at the cost of what shutting it for a whole run buys: an interactive agent write
  may then land between two chunks of the same batch, against the paths a later chunk of that batch
  is about to touch. It also spends a stop, a restart and a pod's startup latency per chunk on a run
  measured in thousands of chunks.

## Consequences

- **The readiness assertion a supervised workload gets for free is unavailable.** A
  Deployment-Ready check has nothing to wait on for a run that has not fired, and this workload's
  dependencies — a broker on a load-balancer hostname, a key held in the front's own
  store — are not ones a CI cluster can supply, so it cannot be made to run there either. That is
  the ceiling the committer's workload sits at, for the same reason
  ([ADR-0030](../replication/0030-committer-shape.md)): manifest-level CI can prove shape and
  environment, never behaviour. Behaviour is proven in this repository's own suite, against a real
  broker.
- **A failed run is not retried.** `process-batch` exits non-zero when it dead-lettered a chunk,
  which is a reported outcome rather than a transient fault: re-running stops the agent instance
  again and changes nothing. The retry budget is therefore zero, and a run's outcome is read from its
  exit status and its counts.
- **A maximum run duration is not imposed by the schedule.** Bounding a run is a designed mechanism
  with its own behaviour on expiry — the agent instance must be restored, not merely abandoned — and
  a timeout set on the workload would be that mechanism, undocumented.
- **The tickets that scope the work are not the authority on the workload's shape.** Where a
  ticket and this record differ on it, this record governs.
