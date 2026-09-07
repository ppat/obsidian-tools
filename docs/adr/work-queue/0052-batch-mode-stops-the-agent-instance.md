# 0052. Batch mode stops the agent MCP instance itself, scaled to zero, with the lease taken before the stop

**Status:** Proposed ·
**Pillar:** [Bulk and drift are feeds into the one write path](../../../DESIGN.md#bulk-and-drift-are-feeds-into-the-one-write-path-never-lanes-around-it) ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted) ·
**Units:** [A2](../../../ROADMAP.md#group-a--pipeline-mechanisms), [D4](../../../ROADMAP.md#group-d--operability) ·
**Tickets:** [ot#5](https://github.com/ppat/obsidian-tools/issues/5), [apps#3875](https://github.com/ppat/homelab-ops-kubernetes-apps/issues/3875)

## Context

A batch run applies bulk work through the same gated path as ordinary ingest, so interactive
writers and the run share one editor and one event loop. For the duration of a run, interactive
vault writes are unavailable and bulk writes are not
([ADR-0022](./0022-batch-stream-mechanics.md)). Three things constrain what may make them
unavailable.

**The stopped state must be readable and reversible by something that is not the run.** The
disaster here is not a slow batch: it is a processor that dies mid-run and leaves every interactive
write stopped, silently and indefinitely, until a human notices that capture has quietly ceased.
The batch watchdog is a scheduled convergence pass whose entire job is to end that state, so the
mechanism has to leave evidence a separate process can read, judge and reverse — and has to leave
it in a form a *dead* processor stops producing, since a flag the processor clears is destroyed by
exactly the failure it would have to report.

**The mechanism must be a property of the vault's own components.** The design's write path is the
two MCP instances and the headless editor behind them. *Something fronting the instances decides who
may call and with which tools* is a role the design names; which component occupies that role is a
deployment choice, and an installation may have none. A mechanism that lives in the occupant is one
that some installations cannot have, and a control the design cannot rely on everywhere is not a
control the design may specify.

**Stopping the wrong thing is worse than stopping nothing.** The ingestor instance is the door bulk
work writes through, and promotion drains through it mid-run by design. Anything that takes the
ingestor path down takes the run down with it.

The doors are the two MCP instances: the **agent instance**, narrow, carrying every interactive
writer and drift dispatch, and the **ingestor instance**, wide, carrying the processors and the lint
pass.

## Decision

**A batch run stops the agent MCP instance for the run's duration by scaling its Deployment to zero,
and starts it again when the run ends. The ingestor instance is untouched.** The door is shut; no
credential is withheld, revoked or altered, and no caller's identity changes. The agent instance
becomes unreachable to every caller at once — those the design knows about, those a particular
installation adds, and an operator's own port-forward alike — because there is nothing left to
reach.

### The authority this may hold, and the authority it may not

Deciding this mechanism is deciding what `batch-processor` and the batch watchdog are permitted to
do to the cluster, and that is the argument that settles it rather than a consideration alongside
it. **A workload may not hold authority to edit the controls that contain it.** The policy in front
of the editor's REST API is a sole control, not defence in depth — the REST API exposes an
undisableable, unscoped write endpoint and that policy is the only thing standing in front of it
([ADR-0006](../write-model/0006-networkpolicy-sole-control.md)) — so a workload able to edit network
policy in that namespace is a workload able to open the whole vault. That authority is not grantable
here at any price in convenience, which removes network refusal from consideration before its
ergonomics are reached at all.

Scaling requires authority too, and a wide scaling grant would undercut the same reasoning. The
grant is therefore constrained in three ways, each load-bearing:

| Constraint | What it prevents |
| --- | --- |
| The **`scale` subresource**, never the Deployment object | A caller that can write the object body can change the image it runs and the write scope it enforces — and that write scope is Gate 2, one of the design's named controls |
| **Named to the agent instance's Deployment alone** | Scaling the editor down stops the only process that writes vault content; scaling the ingestor instance down cuts the run's own write path and promotion's |
| Held by **`batch-processor` and the batch watchdog only** | Every additional holder is another workload whose compromise stops interactive writes |

This constraint is a consequence of the decision rather than a decision of its own: it exists only
because the mechanism is scaling, it has no life if the mechanism changes, and reversing either
forces re-arguing the other. By the re-argue test it belongs in this record.

### The lease, and why ordering replaces atomicity

The watchdog needs two facts: whether the door is shut, and whether a live run is holding it shut.
The first is the Deployment's **desired** replica count — desired rather than available, because an
instance that is crashlooping is a health question with a different owner, and answering it here
would make an ordinary crash look like a batch run and an ordinary batch run look healthy.

The second is a **Lease** beside the Deployment: the object whose semantics are exactly *a holder
renews a deadline*, naming the run that holds it and carrying an expiry it must keep pushing
forward. It cannot ride on the Deployment as an annotation, because writing the Deployment's body is
the authority refused above; an annotation elsewhere, or a timestamp in a ConfigMap, would be a
lease wearing another object's clothes.

Two objects mean two writes, and the ordering is what makes every interruption legible:

| Step order | What a crash between the two steps leaves |
| --- | --- |
| A run **takes the lease, then scales to zero** | Instance up, lease held and expiring. Nothing is stopped; the watchdog drops an expired lease it finds on a running instance |
| A run **scales back to one, then releases the lease** | Instance up, stale lease. The same harmless state, cleared the same way |

So *stopped with no lease* is unreachable by any partial run, which is what lets the watchdog keep
reading that state as an operator's own hold and leave it exactly as found. Ordering buys here what
a single atomic write bought before, without the authority a single write would have required — the
same shape the device loop uses, where a durable record is written before the destructive act rather
than beside it.

### What makes it provable

The control fires observably in both directions, and every step is a violation someone can create:

- **Stop the instance and issue an interactive write** → it fails, while a bulk write through the
  ingestor instance and a promotion drain both succeed in the same window. Stopping *only* the agent
  door is half the claim, and the ingestor half is what distinguishes this control from an outage.
- **Kill a run mid-batch, running no cleanup** → the door stays shut, the lease stops being renewed,
  and the next watchdog pass after expiry starts the instance again. The kill must run no exit
  handler, or the mechanism tidies itself up on the way out and the injection proves nothing.
- **Stop the instance by hand, holding no lease** → the watchdog reports it and changes nothing.
- **Issue, from the processor's own identity, a scale of the ingestor instance and a write to the
  agent instance's Deployment body** → both refused. The grant's narrowness is a claim about what
  the API server does, and reading the role definition is not the same as being refused by it.
- **Reconcile the declared state while a run holds the door shut** → the instance stays at zero. The
  replica count is runtime state, not declared configuration, and any reconciler or autoscaler that
  owns that field will restore the door mid-run. That is the mechanism's sharpest failure mode and
  it is silent in the direction that matters: writes work, so nothing looks wrong, while the batch
  and interactive writers race exactly as the mechanism exists to prevent.

**The mechanism is judged broken** if any of: an interactive write succeeds while the instance is
stopped; the instance is still stopped one watchdog period after a killed run's lease expires; a
watchdog pass starts an instance that no lease covers; or the processor's identity is permitted to
scale anything but the agent instance.

## Alternatives considered

- **Withholding the credential that interactive writers present to whatever fronts the instances.**
  It requires such a front to exist, and requires the front to hold exactly one credential that
  every interactive writer presents and no bulk writer does. Neither is a property of the design:
  the first is a deployment choice, the second is a fact about how a particular installation issued
  its credentials. *[Installation fact, offered as evidence rather than as the argument: in this
  one, each interactive consumer holds its own credential and no shared one exists, so the mechanism
  would have to withhold several — and each withholding stops that consumer's entire access to the
  front, not merely its vault writes.]* Withholding several also multiplies the state the watchdog
  must converge: a partial failure leaves some withheld and some not, and the "no lease means an
  operator's hold" rule has to hold separately over each. And administering credentials at a shared
  front means holding administrative authority over that front's whole credential store — authority
  over consumers with nothing to do with the vault. Finally the state is invisible: a withheld
  credential appears in no ordinary inventory of the running system, so the failure this mechanism
  exists to end stays exactly as silent as it was.
- **Refusing the agent instance at the network for the run's duration.** Rejected on the authority
  above, before any other property is weighed. Three further properties would have counted against
  it had the authority been grantable: the refusal must enumerate the callers to refuse, so it needs
  editing whenever the caller set changes and is unenumerable where callers are off-cluster; network
  isolation is already the sole control on a different door in the same namespace, so batch mode and
  vault containment would ride one mechanism and a mistake in either becomes a mistake in both; and
  a port-forward is served into the pod's own network namespace and never transits the pod network,
  so the door would have a documented bypass.
- **Narrowing the agent instance's write scope for the run instead of stopping it.** Attractive
  because callers would get a structured refusal naming a path scope rather than a transport
  failure, and reads would keep working. Rejected on its restore path: the restore has to write a
  *value* back rather than a count, so a failed or corrupted restore leaves the instance running
  with a scope nobody chose — and the failure class shifts from "writes stopped" (loud, and the
  watchdog's whole subject) to "writes permitted where they must not land", which no watchdog can
  detect because the instance looks healthy. It also requires writing the Deployment body, refused
  above.
- **A single atomic write carrying both the stop and the lease.** The strongest argument for the
  Deployment body as the lease's home, since one write can never be half-done. Rejected because
  ordering makes the half-done states harmless anyway, so atomicity would be bought with the one
  grant this decision most needs to withhold.

## Consequences

- **Two workloads stop holding administrative authority over a shared front, and hold a
  single-integer grant instead.** The blast radius of a compromised `batch-processor` or watchdog
  narrows from "everything that front can be told to do" to "the agent instance runs, or it does
  not".
- **Batch mode becomes visible in the cluster's ordinary inventory.** A Deployment at `0/0` shows up
  in any listing of the namespace and in the replica metrics a cluster already collects, so the
  watchdog is no longer the only thing that can notice a stuck door — and where the watchdog itself
  is dead, the state is at least discoverable rather than invisible. No alert rule follows from this
  ([ADR-0036](../operability/0036-alerting-deferred-emission-first.md)); emission and queryability
  are the whole of it.
- **A caller cannot tell a batch run from an outage, and that is not fixed here.** Interactive
  callers meet a connection failure with no explanation in it. What distinguishes the two lives in
  the cluster, one command away, and the lease names which run is holding the door — an operator's
  answer, not the caller's.
- **Everything onto the agent instance stops, reads included.** The instance is a coarser object
  than a credential: stopping it stops read-only callers of that instance as well as writers, so
  conversational reads through it are unavailable for the run's duration. This is a cost, not a
  side-benefit, and it sharpens two existing decisions rather than reopening them — the permitted
  time window ([ADR-0022](./0022-batch-stream-mechanics.md)) is what keeps the outage where nobody
  is waiting, and the deferred read-scoped third instance
  ([ADR-0003](../write-model/0003-two-instances-two-handles.md)) is the remedy if it ever bites.
- **The agent instance's replica count is runtime state and must be left unowned by whatever
  reconciles the declared state.** A reconciler that owns the field restores the door mid-run, and a
  horizontal autoscaler pointed at that Deployment does the same. The failure is silent in the
  dangerous direction.
- **Stopping the instance is not a drain.** A write in flight when the door shuts is cut at the
  transport, so its caller cannot tell whether it landed — the pod's termination grace period is the
  only thing bounding it. ADR-0022 already names the post-disable drain as a deferred refinement
  ([ot#89](https://github.com/ppat/obsidian-tools/issues/89)); this mechanism makes it sharper, not
  optional, because a stopped process is more abrupt than a refusal.
- **The watchdog acquires a dependency on the cluster's own API and loses one on the front.** It is
  still true that it must not need the broker, the queue credential or any vault write credential to
  start; what it must now reach is the API that reports and sets the instance's replica count.
- **Bulk work no longer needs the front to be present in order to stop interactive writes**, which
  removes one of the write path's dependencies on a component the design does not require. The
  remaining ones — how a processor addresses an MCP instance, and with what credential — are not
  settled here.
