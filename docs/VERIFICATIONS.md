# BRAIN — Verification catalogue

Every control's proving injection and every answerable-by-doing check, past and pending. The
project's acceptance standard is
[violation injection](../DESIGN.md#prove-controls-by-violation-injection-keep-claims-falsifiable):
a control is proven by deliberately creating the violation it exists to stop and watching it fire,
never by observing that nothing bad happened. This catalogue is those test plans in one place —
rekeyed from the retired phase numbering to the [roadmap's](../ROADMAP.md) units, so each pending
verification travels with the work that must satisfy it.

**What this document is, and is not.** It is the *test-plan* layer: more durable than any one
ticket (a recut ticket inherits its rows; a passed row keeps its evidence pointer), more fluid than
the design (rows are added as controls are added). It records **what must fire and what firing
proves** — the how-to detail of running one lives with the implementation. Status values: **proven**
(with the date and the record holding the evidence), **pending** (with the unit that delivers the
control), **parked** (deliberately not run; with the standing reason). A proven row is a claim
about that date — re-runs after relevant change are the drill's business
([D3](../ROADMAP.md#group-d--operability)), not this table's.

## 1. Proven — substrate and containment ([S1](../USE_CASES.md#s1--admitted))

Delivered with the substrate and content foundation; evidence in the closed records.

| Injection | Proves | Status |
| --- | --- | --- |
| Hit the REST API from a non-MCP pod → refused | Network isolation on the direct bearer-token path | Proven at substrate acceptance ([apps#3441](https://github.com/ppat/homelab-ops-kubernetes-apps/issues/3441)) |
| Call the built-in `/mcp/` endpoint from a non-MCP pod → refused by NetworkPolicy | The sole control on the undisableable endpoint (ADR-0006) — *as far as config-level evidence carries; see the parked packet test, §4* | Proven at substrate acceptance |
| Mount the volume from any pod other than the editor, lint, or the committer → blocked | The three-mount contract (ADR-0001) | Proven at substrate acceptance |
| Call MCP without a key → refused; reach MCP from a pod outside the gateway → refused | Gate 1's floor | Proven at substrate acceptance |
| Write attempt on a read-only key → refused | Agents read-only until write keys open | Proven at content-foundation acceptance ([obsidian-vault#2](https://github.com/ppat/obsidian-vault/issues/2)) |
| In-scope write to the inbox succeeds; the byte-identical call at the finance area → structured refusal (`path_forbidden`, echoing the active scope) | The write-path gate, **in both directions** — and the refusal arrives as HTTP 200 with the error in the envelope, the fact behind [D2](../ROADMAP.md#group-d--operability) | Proven 2026-07-30 (recorded in [apps#3439](https://github.com/ppat/homelab-ops-kubernetes-apps/issues/3439)) |
| Delete a note through the agent instance (21 bytes → 0), then withhold the tool at the gateway | The delete gap was real, then closed (ADR-0004) | Proven, then fixed, at content-foundation hardening |

## 2. Proven — replication and capture ([S4](../USE_CASES.md#s4--retrievable), [W6](../USE_CASES.md#axis-2--writers-connected) capture)

All run on real hardware, 2026-08-28; full evidence in
[ot#3](https://github.com/ppat/obsidian-tools/issues/3)'s closing record.

| Injection / check | Proves | Status |
| --- | --- | --- |
| Delete a curated note on the volume, run the cycle → deletion recorded as history; the pull-only clone and the device converge | The committer produces history, never mutates content (ADR-0030); publish converges end to end | Proven 2026-08-28 |
| Change an *allowlisted* setting on the device, run the cycle → device config survives untouched **and** the cycle reports the divergence, naming the file | The settings-baseline exclusion's two halves (ADR-0028). The allowlist qualifier is load-bearing: a plugin `data.json` change is invisible to the comparison *by construction*, and an injection against one would fail while the control worked | Proven 2026-08-28 |
| Delete the device's settings directory while a drifted path's capture is withheld → **no re-seed**, and the cycle says so | The seed sits behind the publish gate and reports rather than falling silent in exactly the no-baseline state | Proven 2026-08-28 |
| Force the spool write to fail (`chmod 500`) → publish withheld, tag not advanced, drifted line survives, captured next cycle | The capture gate's ordering, not merely that the spool fills (ADR-0025) | Proven 2026-08-28 |
| Paste a binary into a drifted path → content-free patch, publish and tag withheld, path reported; released cleanly on removal — a pause, not a wedge | The capture-completeness condition, independent of the spool-write condition | Proven 2026-08-28 |
| iCloud propagates rsync-written files | Answered **yes**, from a second device | Proven 2026-08-28 |
| "Optimize Mac Storage" off suffices against eviction | **Bounded answer only**: 45 files, zero dataless stubs, minutes after write — no eviction *in a short window*; cold-data behaviour needs elapsed time (§4) | Proven-as-bounded 2026-08-28 |
| First real spool entry: identical shas with `matches_upstream: false` | The field means content-equality at the path, not revision equality — the misreading that would discard human deletions, settled empirically (ADR-0026) | Observed 2026-08-28 |

## 3. Pending — keyed to the unit that delivers the control

### The work queue and its credentials ([A1](../ROADMAP.md#group-a--pipeline-mechanisms), [A2](../ROADMAP.md#group-a--pipeline-mechanisms), [B1](../ROADMAP.md#group-b--connection-work))

| Injection | Proves | Pending on |
| --- | --- | --- |
| Fire an interactive-agent write while the agent handle is disabled for a batch run → refused | Batch mode is exactly which handle is enabled | A2 |
| Enqueue a chunk whose base is no longer current → rejected back to the producer, never merged | Stale-reject; no merge engine returns (ADR-0022) | A2 |
| Enqueue a patch modifying an existing raw-layer file → refused by `batch-processor` | Create-only immutability — **a code check, not an MCP refusal**; a regression here is debugged as code, not config (ADR-0015) | A2 |
| Kill `batch-processor` mid-run → chunks redeliver; the agent handle comes back | Redelivery, and [D4](../ROADMAP.md#group-d--operability)'s watchdog — which must exist before any unattended run | A2 + D4 |
| **Publish to the batch subject with `local-replicator`'s own legitimate drift credential → refused by subject permissions** | **The decisive authority test**: a legitimately held credential at a subject outside its grant is the *only* injection distinguishing real per-subject permissions from a NetworkPolicy-only implementation — in-cluster producers would be refused by the network anyway, for a reason unrelated to the control under test. Needs only the *credentials*, which are separable from the streams (ADR-0021) | A1 (credential half) + B7's credential issuance |
| Publish to the promotion subject with the drift credential, and to the drift subject with a promotion credential → refused | The same check across every remaining pair, once all three streams and credentials are live | A1/A3/A7 |

### Admission and promotion ([A3](../ROADMAP.md#group-a--pipeline-mechanisms), [A4](../ROADMAP.md#group-a--pipeline-mechanisms))

| Injection | Proves | Pending on |
| --- | --- | --- |
| A note missing a required `type` → quarantined, machine-readable reason, counted | The validator's floor; fail loud, destroy nothing | A4 |
| A finance note with an unsourced number → quarantined | The evidence-keyed hard block (ADR-0010) | A4 |
| Enqueue a pointer naming a curated path onto the promotion stream → refused by the pointer-target check, never promoted or archived | The check that stops a prompt-injected agent borrowing the widest handle; its refusal count is A3's standing metric | A3 |

### Open writes ([B2](../ROADMAP.md#group-b--connection-work), [B4](../ROADMAP.md#group-b--connection-work), [B5](../ROADMAP.md#group-b--connection-work))

| Injection | Proves | Pending on |
| --- | --- | --- |
| An agent key writes to the finance area → refused by path scope | Gate 2 live per writer (already proven for the substrate's key shape; re-proven per connection) | each of B2/B4/B5 |
| The n8n key calls delete → tool absent | Per-client tool grants (ADR-0003, ADR-0004) | B5 |
| Two agents patch one file concurrently → no torn file; any lost update detected | Serialisation plus clobber self-detection | B2/B4/B5 |
| Re-send a modify with a stale version → precondition failure, not a silent apply | Optimistic concurrency actually wired | B2 |

### The lint pass and the provenance contract ([A5](../ROADMAP.md#group-a--pipeline-mechanisms))

| Injection | Proves | Pending on |
| --- | --- | --- |
| Write a note stamped `trigger: schedule` with `authority: human` → flagged by the next pass | The consistency check — **the entire return on splitting the provenance field**, proved by firing (ADR-0009) | A5 |
| Plant an orphan, a dangling link, a contradiction and a stale claim → all four appear; the mechanical two auto-fixed, the content two flagged only | The auto-fix boundary (ADR-0018) | A5 |
| Make a GUI edit → it appears in the next report, unstamped and unvalidated | The lint pass is the GUI exception's only observer (ADR-0002) | A5 |

### Content gate ([C3](../ROADMAP.md#group-c--content-work))

| Injection | Proves | Pending on |
| --- | --- | --- |
| Edit a human-marked block, force an agent regeneration, confirm the edit survives | The sentinel-marker convention (ADR-0042) — the round-trip test *is* this gate's injection | C3 |

### The recovery drill ([D3](../ROADMAP.md#group-d--operability))

| Injection | Proves | Pending on |
| --- | --- | --- |
| Delete a curated note; restore from a volume snapshot; independently restore the same note from git | Both recovery grounds actually work — a backup never restored from is an assumption, not a floor | D3 (runnable today; cheapest now) |
| Wedge the editor process → the probe restarts it | Liveness recovery for the known-wedging component | D3 |
| Force rapid pod-template churn on the editor's Deployment → no second container ever starts before the first is torn down | The single-writer window's observed limit (ADR-0033) — proven by exercising, not assumed closed by `Recreate` | D3 |

## 4. Parked, revisits, and answerable-by-doing

| Check | Standing |
| --- | --- |
| **The NetworkPolicy packet test** — confirm the enforcement dependency on the nodes, then a throwaway default-deny-plus-one-allow test between labelled and unlabelled pods, the positive case proving the negative means something | **Reopened on new evidence 2026-09-02; re-parked on a smaller residual.** The enforcement dependency is packet-proven: another project on the same cluster runs a standing falsifiability probe for its own purposes — default-deny with positive controls, blocked and reachable cases both asserted, every cycle — which also supplies the passive signal an unenforced policy previously lacked, and would surface a cluster-wide enforcement regression that a one-time test never could. Combined with §1's observed refusals in the vault namespace, the residual is config-level only: the vault namespace's specific policy objects staying correct. The in-namespace two-pod test stays unrun, and a CI variant is permanently off the table — the CI cluster's CNI is not the platform's enforcement engine, so a passing kind packet test would validate the wrong machinery (ADR-0006) |
| **Obsidian on iOS reads rsync-written files correctly** | Testable only at the app rollout's reset — [ot#69](https://github.com/ppat/obsidian-tools/issues/69), riding [B8](../ROADMAP.md#group-b--connection-work) |
| **Eviction over elapsed time** (cold data, days) | The bounded 2026-08-28 answer covers minutes; the steady-state launchd run is the instrument, nobody has read it over days yet |
| **Steady-state false drift** — does unchanged content ever report drift across many cycles? | Needs elapsed time, not action; informational, from the replication acceptance record |
| **Does the app spontaneously rewrite any allowlisted settings file** with no human touching a setting? | Decides whether the settings-baseline closure holds in practice (plugin bundles are allowlisted and auto-update); elapsed-time observation |
| **Whether the review digest renders actionably** over the chat surface | Answerable by doing at [A5](../ROADMAP.md#group-a--pipeline-mechanisms); on failure the loop degrades to reading the report in Obsidian |
| **Whether volume-snapshot granularity supports file-level restore at the needed cadence** | Answerable by doing at [D3](../ROADMAP.md#group-d--operability) |
| **Whether the runner's blocking pre-write hook replaces the detective one** | Recorded revisit, after [B2](../ROADMAP.md#group-b--connection-work) is implemented — the blocking property may be relocatable rather than lost |
| **The `salience:`/`confidence:` correlation audit at ~200 notes** | A scheduled decision, not a check — carried in [ROADMAP's open decisions](../ROADMAP.md#open-decisions) and ADR-0012; listed here because its instrument (the correlation measurement) must exist when the count arrives |
