# BRAIN — Design

The high-level design of BRAIN: the pillars and invariants that hold the platform together, and the
reasoning behind them. It states what the system *is*; it deliberately does not re-argue every
decision that had alternatives. Those live as decision records under `docs/adr/` — the split is that
this document holds **what would still be true if any individual reversible decision had gone the
other way**, and an ADR holds one such decision: its context, alternatives, and consequences.
Together, this document and the ADR set are the entire design — the parts already built and
running as much as the parts still to come. Build state is a roadmap fact, not a design fact: a
pillar binds identically whether its mechanisms are live or unbuilt, and the component table below
marks state only so a reader knows what exists.

Companions: [`USE_CASES.md`](./USE_CASES.md) — the outcomes this design exists to deliver, with
their acceptance criteria; [`ROADMAP.md`](./ROADMAP.md) — the work, its state, and the mapping from
work to outcomes and to this design. Terms used throughout are defined in the [Glossary](#glossary)
at the end; a cold reader should be able to resolve any term in the three documents, a ticket, or an
ADR from there without guessing.

## 1. What the system is

BRAIN is a git-backed Obsidian vault — markdown notes with YAML frontmatter — used as a shared brain
by one human and several AI agents. The vault lives on a Kubernetes volume; exactly one process ever
touches its files; every writer reaches that process through narrow, permission-scoped doors;
content flows through a pipeline (admitted → sound → placed → retrievable) into curated areas; and
read replicas carry the content out to the places the owner actually reads — a chat surface that is
always fresh, and native Obsidian on devices via a one-way replication chain.

### Components, one job each

| Component | One job | Runs | State |
| --- | --- | --- | --- |
| **Vault volume** (`vault-data`, mounted at `/vault`, vault at `/vault/brain`) | Be the authoritative bytes | Cluster (namespace `obsidian-vault`) | Live |
| **Headless Obsidian** + Local REST API | Be the only process that mutates vault content | Cluster | Live |
| **MCP servers, two instances** (agent, ingestor) | Be the only scoped doors into the vault; each instance decides *where* a write may land | Cluster | Live |
| **LiteLLM gateway, two handles** (agent, ingestor) | Decide *who* may call and which tools they see | Cluster | Live |
| **Git committer** | Turn the vault volume into git history, pushed to GitHub and the NAS; never author content | Cluster (CronJob, every 15 min) | Live |
| **`local-replicator`** (+ its spool and drainer) | Keep the device-facing iCloud vault current from git, one-way and non-destructively; capture device-side drift before overwriting it | The operator's Mac (launchd, every 15 min) | Live |
| **The work queue** — NATS JetStream; batch, promotion and drift streams | Carry deferred work to its processor, with per-producer credentials scoping who may publish where | Cluster | Unbuilt |
| **`batch-processor`** | Apply patch-carrying bulk work through the gated write path; enforce the raw layer's create-only rule | Cluster | Unbuilt |
| **`promotion-processor`** | Relocate notes out of the inbox into curated homes, in real time, gated by the admission validator | Cluster | Unbuilt |
| **`drift-processor`** | Classify captured device edits (intentional or not), reconcile them against upstream history, and dispatch survivors into the funnel as ordinary ingest | Cluster | Unbuilt |
| **The admission validator** | Decide whether content meets the schema and provenance bar at the curated boundary; quarantine, never delete | Library, called by the three processors above and lint | Unbuilt |
| **The lint pass** | Walk the whole vault on a schedule: conformance, hygiene, normalisation, and the review digest | Cluster (CronJob) | Unbuilt |
| **Observability** | Make behaviour answerable from stored metrics and logs | Cluster | Groundwork only |
| **ExternalSecrets / Bitwarden** | Custody of every in-cluster credential | Cluster | Live |

The vault content itself lives in `ppat/obsidian-vault`; the components' code lives in
`ppat/obsidian-tools`; their deployment manifests live in `ppat/homelab-ops-kubernetes-apps`
(module `apps-ai`), composed onto clusters by `ppat/homelab-ops-kubernetes-clusters`.

## 2. The pillars

Everything else in this document is a consequence of these. Each is stated with the reasoning that
holds it up; the decisions that *implement* each one, and their alternatives, are ADR material.

### One writer, one door

Exactly one process ever mutates vault content: the headless, in-cluster Obsidian instance. Every
writer — every agent, every processor, lint — is a client of that process through a permission-
scoped MCP door, never a second filesystem writer. Exactly three processes mount the volume at all,
on disjoint or read-only slices: headless Obsidian (read-write on content), lint (read-only on
content, for whole-vault visibility), and the committer (read-only on content, write-only on git's
own metadata — held on a separate volume, so the vault never even grows a `.git`).

Why: a single writer deletes entire subsystems — conflict resolution, merge engines, lock protocols,
human-vs-agent collision handling — rather than mitigating them. Most of what looks like a
multi-writer problem dissolves structurally; the three residues that survive (lost updates on
read-modify-write, device replica divergence, bulk work too large for tool-by-tool traffic) are each
individually mechanised (see "Bulk and drift are feeds into the one write path" and "The device
loop is non-destructive by ordering", below).

Known limit, stated rather than hidden: the single-writer property is enforced by the Deployment's
shape (one replica, `Recreate` strategy), not by the storage layer — the volume is ReadWriteMany
precisely so three processes can mount it. Rapid pod-template churn reopens a timing window a live
incident has already demonstrated; the recovery drill exercises it deliberately.

### Exceptions are declared, never discovered

Two paths bypass the gates, both on purpose, both named at the top rather than found later:

- **The GUI exception.** A human at the headless instance's own GUI (reached only by
  `kubectl port-forward`, dormant VNC) writes directly, with no validation and no provenance
  stamping — because configuring Obsidian itself requires a GUI, and there is no other sanctioned
  way. It is for configuration and repair; the lint pass is the only thing that ever sees such a
  write, after the fact. Rising use of this path is treated as evidence the write model is wrong,
  not as a discipline failure.
- **The recovery exception.** Operator-triggered restore — from a volume snapshot or from git —
  bypasses everything, rarely, as disaster recovery. It is outside the gate system and outside
  ordinary operation.

### The volume is authoritative; git is derived history; no merge engine anywhere

The one directory on the one volume is the authoritative copy. Git records and distributes — history
outward to GitHub and the NAS, content onward to devices — and never writes back: there is no
git-to-volume path anywhere in the system. Nothing anywhere merges: a stale batch patch is rejected
back to its producer to regenerate; a divergent device edit is captured and re-enters as new input.
Every mechanism that would have needed a merge engine was deleted or reshaped so it doesn't.

### Humans are sources and readers, not writers

The human feeds requests in at the top (through agents) and consumes curated content at the bottom.
Direct human writes are rare edits, almost never creation — an owner ruling, load-bearing: any
reasoning that assumes meaningful human-originated content volume is wrong. A device edit is not
prevented and not discarded; it is captured as drift and re-enters the funnel as an ordinary ingest
event, stamped as human-authored. The devices are read replicas plus a capture surface — if editing
on a device becomes the draw, the design has failed on its own terms.

### Layered content, one ownership contract

The vault is layered, and each layer has exactly one owner and one mutability rule:

| Layer | Owner | Mutability |
| --- | --- | --- |
| `05-raw/` — imported sources | ingest | Write-once, immutable; **exempt from validation** by design |
| `00-inbox/`, `40-journal/`, `_ops/agent/`, `log.md` — the agent zone | agents | Freely mutable, path-scoped |
| `10-areas/`, `20-projects/` — curated space | the admission validator | Mutable only through the gate |
| `10-areas/finance/` | validator + finance overlay | Strictest |
| `90-archive/` | lint | Terminal |
| `.obsidian/` — application settings | each instance, locally | Outside the content contract |

The raw layer's exemption is load-bearing, not an oversight: validating thousands of imported
documents would either fail thousands of times or force a schema laxity that poisons the baseline
for everything else. Curated content is built *from* raw, incrementally. The vault accepts markdown
only; conversion from any other format happens outside the vault boundary, in the systems that own
the capture channel.

`CLAUDE.md` at the vault root is the schema file governing all of it — the ownership contract, the
folder map, the frontmatter schema and vocabularies, the overlays, and the write discipline every
agent must follow. It has one owner: the human.

### Provenance is three questions, and self-report never unlocks a gate

Every note carries three provenance fields because they answer three different questions:

- **`source:`** — which process performed the write. Mechanically attributable.
- **`authority:`** — whose claim the content is (`human` | `agent` | `import`). Self-reported.
- **`trigger:`** — what caused the write (`human` | `schedule` | `event`). Mechanically attributable.

The split exists so trust is *checkable*: `trigger: schedule` with `authority: human` is a
mechanical contradiction the lint pass flags, and it is the check that catches a device edit landing
on a note still stamped `authority: agent`. Because `authority:` is self-reported, **no gate treats
it as sufficient by itself**: the finance overlay's hard block keys on `authority: import` plus
inline provenance plus `confidence:` — the fields that actually evidence a number — never on who
claims to have typed it. `confidence:` (how sure the claim is) and `authority:` (whose claim it is)
are orthogonal and never merged. Relocation does not rewrite provenance: `source:` records who
authored the content, not who last moved the file.

### Authority is carried by capability, not by network position

Who may do what is decided by the credential in hand, at every layer:

- **Handles and instances are separate axes.** A gateway *handle* decides who may call and which
  tools they see; an MCP *instance* decides where a write may land (`OBSIDIAN_WRITE_PATHS`). Two
  instances exist — agent (narrow: the agent zone) and ingestor (wide: everything the processors and
  lint legitimately touch) — because two handles onto one instance would leak that instance's path
  scope to whoever held the other handle.
- **Queue enqueue authority follows message shape, carried by per-producer NATS credentials.** A
  *patch-carrying* message (batch stream) confers the processor's own write scope on its enqueuer,
  so exactly one producer — the operator's workspace — may publish there: no unattended agent can
  restructure the vault. A *pointer-carrying* message (promotion stream) confers nothing, provided
  the processor refuses any pointer naming a path outside the enqueuer's own scope — so every
  interactive agent may announce what it wrote. A *content-carrying, fixed-destination* message
  (drift stream) confers nothing either; its sole producer is `local-replicator`, by credential.
- **Network position is not an authority model.** The moment the queue is reachable off-cluster (it
  must be, for the Mac), a NetworkPolicy cannot distinguish producers or streams. NetworkPolicy
  remains as in-cluster defence in depth — and, in one place, as a sole control: the REST API's
  built-in second MCP endpoint cannot be disabled, so the policy admitting only the MCP pods to
  Obsidian is load-bearing, which is why whether the platform actually enforces NetworkPolicy is a
  named open verification (§5).

### One authority per question

Every question about content has exactly one component entitled to answer it, and no component
answers two:

| Question | Authority |
| --- | --- |
| Who may call, with which tools | the gateway handle |
| Where a write may land | the MCP instance's path scope |
| Does content meet the schema and provenance bar | the admission validator — asked of *everything* entering curated space, whoever carries it |
| Did a human *mean* to make this device edit | `drift-processor`'s classifier — asked only of drift, upstream of the validator, never merged with it |
| What shape frontmatter takes | the lint pass's normalisation — in the scheduled pass, never on save, so the validator's approval cannot be silently reshaped afterwards |
| Whether a promotion happens | the validator admits; agents and n8n only propose |

The corollary that resolves where validation "kicks in": **the admission validator fires at the
curated boundary, on every write that crosses it, regardless of route or caller** — promotion out of
the inbox, a batch chunk targeting curated space, or a lint auto-fix. It is one shared piece of
admission logic with three callers, applied at different strengths (a hard mechanical block in the
strictest domain; flag-only where detection is judgment). Inside the agent zone the posture is
detective, not preventive — agents must capture freely, and the boundary is what is defended. The
raw layer is exempt (the ownership contract above). Edits *after* promotion cross the same boundary — only ingestor-handle
holders can make them, and all of them call the same validator.

### Fail loud, destroy nothing

A note failing validation is quarantined with a machine-readable reason, never deleted. A device
edit is spooled durably before anything overwrites it, and a cycle that cannot capture everything
publishes nothing. A stale patch is rejected, not merged. A relocation writes the new path before
deleting the old, so a crash leaves a recoverable duplicate rather than a loss. Every component
defaults to this posture when it meets something it cannot reconcile.

### Bulk and drift are feeds into the one write path, never lanes around it

Bulk work arrives as git patches on the batch stream and is applied by `batch-processor` *through
the same MCP path and controls as ordinary ingest* — "batch mode" reduces to which gateway handle is
enabled. Captured device edits arrive on the drift stream and are dispatched by `drift-processor`
through the narrow agent-scoped handle into the inbox, like any other capture. The accepted cost is
throughput (a bulk import takes hours, once); the purchased property is that there is no second
write mode, no second operating state, and no direct filesystem write anywhere. Batch mode's safety
mechanisms — a watchdog that re-enables the agent handle if the processor dies, a maximum window,
a post-disable drain — must exist before the batch stream ever runs unattended, because a processor
crash with the agent handle disabled silently stops every agent write.

### The device loop is non-destructive by ordering, not by hope

`local-replicator`'s cycle is capture-then-publish against a byte-exact baseline (a parked git
checkout at the `LAST_CHECKOUT` tag): overlay the device tree onto the baseline, let `git diff`
enumerate drift as patches, spool every patch durably to local disk, and only then publish upstream
content over the device copy and advance the tag. Publish is gated on the spool write succeeding
*and* on every patch actually capturing what changed (a pasted binary produces a content-free patch
and withholds the cycle). The ordering was built on day one, with the spool's consumer stubbed,
because retrofitting a gate later would invert a working publish path rather than insert a step.
The device-side detector is deliberately dumb — it submits everything the comparison flags and
judges nothing — so it can never silently drop a real edit; judgement is server-side, where upstream
history is in hand. Device settings (`.obsidian/`) are outside the loop entirely: seeded once from a
committed baseline that is deliberately a **seed, not a mirror**, then owned by the device, with
divergence reported but never spooled as drift.

### Prove controls by violation injection; keep claims falsifiable

A control is proven by making it fire — writing where writing is forbidden, publishing to a subject
the credential must not reach, planting the defect the linter must flag — never by observing that
nothing bad happened. Several controls that "looked correct" here were only found wrong this way.
Two companion disciplines: **measured versus inferred** are always distinguished, and *authored*,
*merged*, *released*, and *deployed-and-observed* are four different states never collapsed — this
project's dominant failure mode has been unmeasured claims hardening into established fact.

### Instrument early, alert never (until AI triage exists)

Emission is the irreversible half of observability: an uninstrumented window is gone and cannot be
backfilled, while dashboards and alert rules over existing metrics are cheap configuration. So
metrics and logging move to the front of the work, and alerting is an explicit non-goal until an
AI triage layer exists to filter noise for a single operator. Two hard-won signal rules: watch the
**absence** of writes, not only errors (the editor can wedge silently); and a signal whose purpose
is to **contradict a component's own account of itself** (the pod that was Ready while unable to
open its vault) must come from an independent observer — it cannot be that component's own probe,
because a probe converts observation into restart, and the observed failure was one a restart does
not fix.

## 3. The write path, end to end

A write from an interactive agent passes, in order: the gateway handle (who may call, which tools —
delete is withheld from the agent handle entirely); the MCP instance's path scope (where it may
land — prefix-based, path-granular); the editing primitives (append/patch preferred, optimistic
concurrency on modify); serialisation at the editor's single event loop; and — for anything crossing
into curated space — the admission validator. Path scope is *path*-granular only: it does not
distinguish create from overwrite, so per-operation rules (the raw layer's create-only, the log's
append-only) are enforced by the component positioned to see both sides — `batch-processor` for
raw — or held by scope discipline where no backstop exists yet.

Deferred work rides the work queue: three streams, one per processor, each shipped *together with*
its consumer and its credential grant so no stream is ever reachable with no consumer and no
credential control behind it. The batch stream is FIFO, unsharded, and rejects stale patches;
`batch-processor` yields to the promotion stream's depth (fairness to the paths where a human is
waiting), not merely to health signals.

The lint pass walks the whole vault on a schedule, from a read-only mount (whole-vault reads through
the gateway would contend with the write path and muddy the write-absence signal), and writes only
through the ingestor handle. It normalises mechanically (key order, dates, casing, `updated:`
stamping, filling — never overwriting — missing required fields, all logged), flags judgment calls
(contradictions, staleness, near-duplicates, provenance inconsistencies), and is the only thing that
ever sees a GUI-exception write. Normalisation lives in the lint pass's own code — the in-app
Linter/Frontmatter-style plugins were dropped, and the community plugin set is deliberately minimal
(Tasks and Dataview only), so frontmatter shape has one owner in one place.

Its findings surface in three tiers: a full report in `_ops/lint/`; the **review digest** — ranked,
hard-capped at roughly seven items, pushed over WhatsApp with actionable replies — which is the
human review loop, arriving where the human already is; and metrics.

## 4. The read path

Two planes, deliberately asymmetric:

- **Plane A — conversational.** WhatsApp ↔ OpenClaw, and Open WebUI in a browser, reading the
  authoritative volume live through read-only handles. Always fresh, works anywhere, independent of
  any device being awake. This is the primary phone surface, and it pushes (digests, due-today)
  rather than waiting to be opened.
- **Plane B — native Obsidian.** One-way chain: volume → committer → GitHub → `local-replicator` →
  iCloud → the Mac and iOS apps. Rich (backlinks, graph, offline), and laggier: device freshness is
  gated on the Mac waking, because only the Mac can write its own iCloud folder. The iCloud
  directory holds the actual vault both devices open; the Mac's clone is only the replication
  source, and git's own metadata stays outside iCloud (its conflict handling corrupts refs).

The NAS holds a bare git repository as a second push target — independence insurance: a
plain-markdown copy readable with zero tooling, no cluster, no third party. Because replicas are
one-way and lag, the vault must stay readable without any query engine — plain markdown, plain YAML,
and no note is ever a materialised cache of something computed elsewhere (the global todo is a
query, never copied rows; task metadata uses the Tasks plugin's bracket format, settled vault-wide).

## 5. Known limits and open verifications

Held here so they are not rediscovered; the roadmap carries their disposition.

- **NetworkPolicy enforcement on the platform has config-level evidence, not packet-level proof** —
  and it is a sole control (see "Authority is carried by capability"). The definitive two-pod test has never been run. Parked by decision;
  reopen only on new evidence.
- **Single-writer is guaranteed by Deployment shape, with an observed timing window** under rapid
  pod-template churn (see "One writer, one door"). The recovery drill covers it.
- **Batch staleness measurement** (patch base commit vs per-file content hash) is flagged, not
  settled — decided at implementation when real commit cadence and batch sizes are visible.
- **`salience:` may prove redundant with `confidence:`** — at roughly 200 notes, their correlation
  is measured, and if they track, `salience:` is removed. The audit is a scheduled decision, not a
  hope.
- **The W2 (NAS drop) writer has no place in the current authority model** — its content routes to
  bulk import, whose stream is closed to all but the operator's workspace. Connecting it is a design
  decision (see `ROADMAP.md`).
- **Search at scale and near-duplicate detection are deliberately not built.** Named techniques
  exist for the day the thresholds are hit; pre-building them would be a regression.

## Glossary

One entry per term as the three documents, the tickets, and the ADRs use it. Where sources used a
different name, the retired synonym is noted.

### System and repos

- **BRAIN** — the project codename for the whole platform. Infrastructure is named by function, not
  codename: the namespace is `obsidian-vault`, the volume `vault-data`.
- **The vault** — the Obsidian vault itself: markdown notes with YAML frontmatter at `/vault/brain`
  on the vault volume; the authoritative copy. Its content is committed to `ppat/obsidian-vault`.
- **`ppat/obsidian-tools`** — the code repository for every BRAIN component (one Python package,
  versioned as a whole). **`ppat/homelab-ops-kubernetes-apps`** — the deployment manifests (module
  `apps-ai`). **`ppat/homelab-ops-kubernetes-clusters`** — composes modules onto the real clusters;
  a change reaches a cluster only after a release is cut *and* that repo bumps its pinned tag.
- **The Coder workspace** — the operator's development environment, itself a pod in the cluster; the
  home of writer W1 and the only holder of the batch stream's producer credential.

### Vault areas and content

- **The agent zone** — the paths interactive agents may write: `00-inbox/`, `40-journal/`,
  `_ops/agent/`, `log.md`.
- **The inbox** (`00-inbox/`) — agent-writable staging; content lands here and is later promoted.
- **The raw layer** (`05-raw/`) — write-once immutable imported sources; exempt from validation;
  create-only, enforced by `batch-processor`.
- **Curated space** — `10-areas/` and `20-projects/`; mutable only through the admission gate.
- **The finance overlay** — the strictest per-domain rules (`10-areas/finance/`): a numeric claim
  needs `authority: import`, inline provenance, and `confidence:`. Overlays are strictness dials on
  shared axes (provenance demanded, staleness cadence), not different rules per domain.
- **The archive** (`90-archive/`) — terminal destination for rolled-up or retired notes; lint owns it.
- **Promotion** — relocating a note out of the inbox into its curated home upon admission. There is
  no move primitive: promotion is write-to-new-path then delete-at-old-path, in that order.
- **The schema file** — `CLAUDE.md` at the vault root (with `AGENTS.md` as a one-line pointer to
  it): the ownership contract, folder map, frontmatter schema, and agent write discipline.
- **Frontmatter provenance fields** — `source:` (which process wrote; mechanical), `authority:`
  (whose claim: `human`/`agent`/`import`; self-reported, never sufficient alone), `trigger:` (what
  caused the write: `human`/`schedule`/`event`; mechanical).
- **`confidence:`** — how sure a claim is (`high`/`medium`/`speculation`); orthogonal to
  `authority:`.
- **`salience:`** — an integer 1–10 an automated pass scores for roll-up ranking; normalised within
  a batch, never thresholded on the absolute number. **`consolidated:`** — the date a note was last
  folded into a roll-up; compared against `updated:` to re-qualify re-edited notes.
- **The tolerance line** — the written statement, inside the linter, of what badness S2 tolerates;
  what makes S2's acceptance falsifiable.

### Write path

- **Headless Obsidian** — the single in-cluster Obsidian process; the only writer of vault content.
- **The handles** — LiteLLM gateway registrations deciding who may call and with which tools: the
  **agent handle** (every interactive writer and `drift-processor`) and the **ingestor handle**
  (`promotion-processor`, `batch-processor`, lint). Batch runs disable the agent handle only.
- **The instances** — the two MCP server deployments deciding where a write may land: the **agent
  instance** (scoped to the agent zone) and the **ingestor instance** (the wide scope). Handles and
  instances are separate axes.
- **The gates** — the ordered controls on a write: Gate 0 (runner pre-write hook, detective,
  Claude Code only), Gate 1 (handle: tool visibility), Gate 2 (instance: path scope), Gate 3
  (editing primitives, anti-clobber, optimistic concurrency), Gate 4 (event-loop serialisation),
  **Gate 5 — the admission validator, the decisive gate**, Gate 6 (network isolation of the REST
  surface).
- **The admission validator** (Gate 5) — the one shared admission check at the curated boundary;
  quarantine-never-delete. *Retired synonyms: "promotion validator", "frontmatter validator" — same
  artifact, named in older sources by one of its callers.*
- **The lint pass** — the scheduled whole-vault maintenance CronJob: conformance checks,
  additive-only normalisation, flag-vs-autofix boundary, and the only observer of GUI-exception
  writes. *Retired synonyms: "the maintenance pass", "the vault worker" (a dead component name whose
  other entrypoints became `promotion-processor` and the committer's second remote).*
- **The review digest** — the lint pass's ranked, hard-capped (~7 items) findings pushed over
  WhatsApp with actionable replies (approve/skip/explain); the human review loop. This is the
  "digest" wherever older material pairs "lint/digest".
- **Quarantine** (`_ops/quarantine/`) — where a note failing validation goes, with a
  machine-readable reason; never deleted.
- **The work queue** — three NATS JetStream streams, one per processor, each shipped with its
  consumer and credential grant: the **batch stream** (patch-carrying; sole producer: the Coder
  workspace) drained by `batch-processor`; the **promotion stream** (pointer-carrying; producers:
  OpenClaw, n8n, Claude Code) drained by `promotion-processor`; the **drift stream**
  (content-carrying, fixed destination; sole producer: `local-replicator`) drained by
  `drift-processor`.
- **The batch watchdog** — the mechanism that re-enables the agent handle if `batch-processor` dies
  mid-run; with the maximum window and post-disable drain, it must exist before the batch stream
  runs unattended.

### Device loop and read path

- **Plane A / Plane B** — the two read planes: conversational (always fresh, primary on the phone)
  and native Obsidian on devices (rich, laggier, Mac-gated freshness).
- **The committer** — the CronJob committing the vault volume into git and pushing both remotes; it
  produces history and never authors content.
- **`local-replicator`** — the Mac-side launchd job running the replication cycle; the only
  component outside the cluster, watched by nothing.
- **The overlay** — the cycle step that copies the device-facing iCloud tree onto the parked
  baseline checkout so `git diff` can enumerate drift.
- **Drift** — a change in the device-facing copy relative to the baseline. Not necessarily human:
  the stream carries the system's own re-read content after a crash between publish and tag advance,
  and anything any editor wrote into the iCloud directory.
- **The baseline / `LAST_CHECKOUT`** — the git tag naming content byte-identical to what was last
  published to the device; the clone stays parked there between cycles and is the comparison
  baseline.
- **The spool** — the local directory where drift patches are written atomically before anything
  overwrites the device copy. **The drainer** — the separate job that sends spool entries onward
  (today: discards them; at dispatch: publishes to the drift stream, removing an entry only on
  JetStream ack).
- **The capture gate** — the rule that a cycle publishes nothing (and advances no tag) unless every
  drift patch spooled *and* actually captured what changed; a content-free patch (a pasted binary)
  withholds the cycle.
- **Capture / dispatch** — W6's two halves: recording a device edit non-destructively (delivered)
  versus adjudicating it back into the vault as an ingest event (unbuilt: the drift stream,
  `drift-processor`'s classifier, and the reconcile-against-upstream obligation before stamping
  `authority: human`).
- **The settings lock** — the one-time human checklist, performed at the GUI, locking the Obsidian
  settings that are prohibitively expensive to retrofit (property types, new-note location,
  attachment and template folders, daily-note format). The settings baseline captures its results
  into git.
- **The settings baseline** — the one-time, allowlist-constrained commit of `.obsidian/`
  configuration from which a device is seeded; deliberately a **seed, not a mirror** — never
  re-tracked, never reconciled, divergence reported but unresolvable by any publish.
- **The seed** — the one-time copy of the settings baseline onto a device, gated on a completion
  marker; deleting the device's `.obsidian/` (with the tag) is the deliberate reset that re-runs it.
- **The app rollout** — the future act of installing Obsidian on the Mac and iPhone, which requires
  resetting the device vault to day one; the point where the settings baseline stops being inert and
  becomes real device configuration, irreversibly.

### Exceptions, operability, doctrine

- **The GUI exception (P8)** — the ungated human path at the headless instance's own GUI;
  configuration and repair only; observed only by the lint pass.
- **The recovery exception (P9)** — operator-triggered snapshot or git restore; disaster recovery
  only, outside the gate system.
- **Violation injection** — proving a control by deliberately creating the violation it exists to
  stop and confirming it fires; this project's acceptance standard throughout.
- **Measured / inferred** — every status claim is one or the other, tagged; and *authored*,
  *merged*, *released*, *deployed-and-observed* are four states never collapsed.
- **AI triage** — the not-yet-built capability for an agent to interpret alerts; its absence is why
  alerting (O3) is a non-outcome.
- **Outcome identifiers** — S1–S4 (pipeline), W1–W6 (writers), R1–R5 (readers), O1–O3
  (operability): defined in [`USE_CASES.md`](./USE_CASES.md) and used as the coordinate system in
  [`ROADMAP.md`](./ROADMAP.md) and the tickets.
