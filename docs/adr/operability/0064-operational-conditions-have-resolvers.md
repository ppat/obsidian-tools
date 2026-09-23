# 0064. Every operational condition the vault system can observe has a resolver inside it, or is a stated residue with its consequence

**Status:** Proposed ·
**Pillar:** [The vault system does its own job](../../../DESIGN.md#the-vault-system-does-its-own-job) ·
**Serves:** [O2](../../../USE_CASES.md#o2--survives-its-failure-modes) ·
**Unit:** [D7](../../../ROADMAP.md#group-d--operability)

## Context

[ADR-0062](./0062-nothing-is-pushed-to-a-person.md) holds that anything the vault system notices has
an owner inside the vault system, and that a condition no component resolves is a gap in the design.
The content pipeline meets that standard. The operational signals need the same: each condition
either has a resolver, or is named as a residue together with what it costs.

Two constraints shape the resolver. Kubernetes RBAC scopes by resource, verb and name, never by
label, and a Deployment's pod names are generated. The precedent that does scope is ADR-0052's: the
`scale` subresource on one named Deployment ([ADR-0052](../work-queue/0052-batch-mode-stops-the-agent-instance.md)).

## Decision

**An operations pass — a scheduled component — reads the vault system's own metrics and the access
record, detects each condition below by rule, and acts only through one narrow authority: the
`scale` subresource on a named list of Deployments** — the editor and each access gate. Every
condition here can be decided by a rule, so no model is involved: a model would add a dependency on
the endpoint, an injection surface (the access record carries paths clients choose), and a failure
mode, and it would judge nothing a rule cannot.

**A restart is a scale to zero and back, made safe the way
[ADR-0052](../work-queue/0052-batch-mode-stops-the-agent-instance.md) makes the batch run safe.** The
two calls have a crash window between them, and the Deployment is the vault's only writer or a
gate. So, in this order:

1. the pass takes a **Lease** naming the Deployment, and renews it;
2. it scales the Deployment to zero;
3. it scales it back to one;
4. it releases the Lease — only once the scale back has succeeded. A failed scale back leaves the
   Lease to expire and is never released, so no partial run can leave a Deployment at zero with no
   Lease.

The **batch watchdog** (D4) — which already depends on nothing the processes it watches depend on
— extends its list to these Deployments. Any of them found at zero under an *expired* Lease is
restored. One found at zero with *no* Lease is left exactly as found and recorded. Nothing in the
design puts it there, so whatever did so is outside the design, and the watchdog does not undo it —
the same rule ADR-0052 sets for the agent instance. These Deployments' replica counts are runtime
state, left unowned by whatever reconciles declared state, exactly as the agent instance's is
(ADR-0052).
Each Deployment is restarted at most once per episode. The pass never edits a network policy, never
writes a Deployment's body, never deletes a pod, never touches vault content, and pushes nothing to
anyone.

**Required injection, for the implementation:** kill the operations pass between the scale to
zero and the scale back. The watchdog must then restore the Deployment within one watchdog period
of the Lease expiring, and must leave alone a Deployment at zero that no Lease covers.

| Condition | Resolution |
| --- | --- |
| The editor Ready while the vault-loaded signal says it cannot open the vault; or writes absent while calls reach the gate and the editor fails them | Restart the editor, once. If the condition survives the restart, it is a **residue**. **Consequence: the vault is down for writes until a code or manifest change lands.** Clients see their calls fail; nothing else reports it |
| An access gate down, or refusing every credential | Its own Deployment restarts a crashed pod. A gate that is up but refusing everything is restarted once; if that persists, a **residue** with the same consequence |
| No calls reaching the gates at all | Indistinguishable, from inside the vault, between no traffic and an outage upstream of the gate — at a front, or in the network. A **residue**: the vault cannot observe what never reaches it |
| The committer failing | Its CronJob retries every run, and every run pushes ([ADR-0030](../replication/0030-committer-shape.md)). A persistent failure is a **residue**. Consequence: history, device freshness and git recovery go stale while the volume stays authoritative |
| The broker down | Its Deployment restarts it; producers and processors retry. Persistent, a **residue**: queue-borne work waits |
| A scheduled component not running (lint, the sweep, the roll-up pass, the committer) | Detected from its last-success timestamp and recorded. The operations pass cannot run another job, so this is a **residue**: that job's work waits |
| The operations pass itself not running | Nothing observes it. A **residue** |
| An unreachable model endpoint | Its callers retry every judged item on the next pass (ADR-0055, ADR-0056) |
| The evidence fetcher down, or failing on its own side (every canary failing) | Its Deployment restarts a crashed pod. A failure on the fetcher's side that lasts longer than an area's dial is a **residue**. Consequence: freshness judgement halts — no `verified:` stamps and no decay — and stale-claim findings persist, counted as stalled by the fetcher ([ADR-0061](../content-model/0061-freshness-is-the-vaults-own-verdict.md)) |
| Promotion dead letters | The sweep re-enqueues, one pointer per note per interval (ADR-0056) |
| Batch dead letters | A **residue** until the producer regenerates. How that happens, and whether it waits on a merge, is part of the open bulk-run decision (ROADMAP) |
| The Mac asleep, off, or with a stranded schedule | Not observable from the cluster. The self-upgrade's proof and rollback resolve a stranded schedule on the Mac itself ([ADR-0065](../replication/0065-local-replicator-upgrades-itself.md)). Device freshness has no in-cluster observer: a **residue** |
| A front down | The front belongs to the installation, not the design; see "no calls reaching the gates" |
| An access-record write failing | The call is refused ([ADR-0059](../write-model/0059-one-holder-per-credential-access-record.md)), so no admitted write is unrecorded |
| Gate refusals | Counted. A refusal is the control working |

**Every residue is emitted with its consequence**, and none of them is sent to a person. The residues
describe what this design does not recover from by itself. They are not work for anyone to pick up.

## Alternatives considered

- **Alerting a person** — excluded (ADR-0062).
- **Deleting pods by label** — RBAC cannot scope that, so it would amount to deleting any pod in the
  namespace.
- **A restart that never passes through zero** — deleting the pod needs `delete pods`, which RBAC
  cannot confine to one Deployment's pods without an admission policy as well. A rollout restart
  needs `patch` on the Deployment's body, the authority ADR-0052 refuses because it could change the
  image or the write scope. Reusing the Lease and the watchdog adds no new kind of authority.
- **Model judgement over the signals** — nothing here needs it.
- **A liveness probe on the vault-loaded signal** — rejected by
  [ADR-0037](./0037-vault-loaded-exporter.md): a probe turns observation into an endless restart
  loop.

## Consequences

- **The only new operational authority is `scale` on named Deployments**, the shape ADR-0052
  already uses, plus Leases. It never includes the agent instance's Deployment, whose replica count
  is the batch run's and the watchdog's (ADR-0052). The watchdog's scale grant widens to the same
  named list.
- **A restart can no longer strand the only writer at zero**: its crash window is covered by an
  observer that depends on nothing the pass does.
- **The residues are the honest boundary of self-repair**, stated with their consequences, so a
  reader does not mistake them for resolved conditions.
