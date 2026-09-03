# Architecture decision records

One decision per record: its context, the decision, the alternatives rejected, and the
consequences. The split with the top-level design is deliberate:
[`DESIGN.md`](../../DESIGN.md) holds what would still be true if any individual reversible
decision had gone the other way; each record here holds one such decision. Together they capture
the entire design — implemented parts as much as future ones. The vocabulary is
[`DESIGN.md`'s Glossary](../../DESIGN.md#glossary); outcome identifiers are defined in
[`USE_CASES.md`](../../USE_CASES.md), work units and value increments in
[`ROADMAP.md`](../../ROADMAP.md).

## Record format

The convention is Nygard-derived, with two deliberate additions and one deliberate absence:

- **Filename `NNNN-slug.md`.** The number is global, stable, and never reused; the folder is theme
  navigation only and a record may move folders without renumbering. Prose cites records by number
  ("ADR-0007"), resolved through this index — the number is the durable handle, the path is not.
- **An H1 stating the decision as a claim** (`# NNNN. <decision>`), then a header line carrying the
  record's metadata as visible, linked text: `**Status:** … · **Serves:** …` with outcome/unit
  identifiers and tickets linked per the repo's link standard. This line is the *single* home for a
  record's metadata; the tables below mirror status for scanning, and the record wins on
  disagreement.
- **Fixed sections: Context, Decision, Alternatives considered, Consequences** — Alternatives is
  mandatory, because a decision whose alternatives are unstated cannot be re-argued honestly.
  Optional extra sections (history, gotchas) only where they earn their place.
- **No YAML frontmatter, deliberately.** A machine-readable metadata block would be a second home
  for facts the header line already states — two homes for one fact is this project's
  best-documented failure mode — and frontmatter cannot carry the links the standard requires. The
  readers here (humans and LLMs) read text; this index is the queryable view.
- **Statuses:** **Proposed** (adopted by the documents, awaiting owner ratification) → **Accepted**
  → **Superseded** (header gains `**Superseded by:** ADR-NNNN`; the replacement is a new number,
  never an edit-in-place of the old decision). A superseded decision from *before* this set exists
  only inside the record that replaced it, and only where it carries a gotcha that would otherwise
  be repeated — this initial set deliberately contains no standalone superseded records.
- **One decision per record — where "one decision" is cut by the re-argue test.** Decisions merge
  into a single record when they share one review context and would be re-argued together: reversing
  one forces re-arguing the others (the batch stream's FIFO, backpressure and watchdog live in one
  record; the credential choices live with the instances-and-handles decision they ride on). They
  stay separate records when independently reversible — one can flip while its neighbours stand.
  A record found to be carrying two separable decisions is split at the next substantive touch,
  each half keeping or taking a number per the rule above; two records found to always travel
  together merge the same way.

Records state decisions; the [roadmap](../../ROADMAP.md) tracks what is built versus pending —
build state never lives here.

## Write model — `write-model/`

| # | Record | Status |
| --- | --- | --- |
| 0001 | [One writer, one door; two-way device sync deleted](./write-model/0001-single-writer-one-door.md) | Accepted |
| 0002 | [The GUI exception: one pod, dormant VNC, port-forward only](./write-model/0002-gui-exception-dormant-vnc.md) | Accepted |
| 0003 | [Two MCP instances × two handles; third instance deferred; credential choices](./write-model/0003-two-instances-two-handles.md) | Accepted |
| 0004 | [Delete withheld at the gateway; relocation writes before it deletes](./write-model/0004-delete-withheld-relocation-order.md) | Accepted |
| 0005 | [Path scope is path-granular only; per-operation rules get named backstops](./write-model/0005-path-scope-granularity.md) | Accepted |
| 0006 | [NetworkPolicy as the sole control on the undisableable second MCP endpoint](./write-model/0006-networkpolicy-sole-control.md) | Accepted |
| 0007 | [One shared admission validator, three callers, fired at every curated-boundary crossing](./write-model/0007-validation-placement.md) | **Proposed** |
| 0008 | [Drift intentionality is a separate authority, upstream of the validator](./write-model/0008-drift-classification-separate-authority.md) | Accepted |

## Content model — `content-model/`

| # | Record | Status |
| --- | --- | --- |
| 0009 | [Provenance is three fields: `source:` / `authority:` / `trigger:`](./content-model/0009-three-field-provenance-split.md) | Accepted |
| 0010 | [`authority: human` unlocks nothing; the finance block keys on evidence](./content-model/0010-authority-human-loses-privilege.md) | Accepted |
| 0011 | [`confidence:` stays a three-level enum; `stated` removed; no float](./content-model/0011-confidence-enum.md) | Accepted |
| 0012 | [`salience:` and `consolidated:` for the roll-up pass, with the removal audit](./content-model/0012-salience-consolidated-fields.md) | Accepted |
| 0013 | [`slug(title)` filenames plus the page-exists check](./content-model/0013-filename-slug-page-exists.md) | Accepted |
| 0014 | [Markdown only; conversion outside the vault boundary](./content-model/0014-markdown-only-vault.md) | Accepted |
| 0015 | [The raw layer: write-once, validation-exempt, enforced in `batch-processor`](./content-model/0015-raw-immutability.md) | Accepted |
| 0016 | [Task metadata: the bracket format, settled vault-wide](./content-model/0016-task-metadata-bracket-format.md) | Accepted |
| 0017 | [Community plugins: Tasks and Dataview only; no in-app validation plugin](./content-model/0017-plugin-set-tasks-dataview.md) | Accepted |
| 0018 | [Lint policy: in-pass in-code normalisation, the auto-fix boundary, the capped digest](./content-model/0018-lint-pass-policy.md) | Accepted |
| 0019 | [Vocabulary rulings: a person is an `entity`; dining takes travel's overlay](./content-model/0019-type-and-overlay-vocabulary.md) | Accepted |
| 0038 | [Search at scale and near-duplicate detection deliberately not built](./content-model/0038-search-and-dedup-not-built.md) | Accepted |
| 0039 | [One schema file, one owner: `CLAUDE.md`, with `AGENTS.md` a plain-text pointer](./content-model/0039-schema-file-and-agents-pointer.md) | Accepted |
| 0042 | [The adopted community pattern, and the regeneration-safety conventions](./content-model/0042-adopted-community-pattern.md) | Accepted |

## The work queue — `work-queue/`

| # | Record | Status |
| --- | --- | --- |
| 0020 | [NATS JetStream over RabbitMQ, on operational weight](./work-queue/0020-nats-jetstream.md) | Accepted |
| 0021 | [Enqueue authority follows message shape, carried by per-producer credentials](./work-queue/0021-authority-by-message-shape.md) | Accepted |
| 0022 | [Batch mechanics: FIFO, unsharded, stale-reject, fairness backpressure, watchdog](./work-queue/0022-batch-stream-mechanics.md) | Accepted |
| 0023 | [Streams ship with their processors; the substrate alone carries no streams](./work-queue/0023-streams-ship-with-processors.md) | Accepted |
| 0024 | [Captured drift rides the drift stream — superseding MinIO, keeping its siting gotcha](./work-queue/0024-drift-destination-stream-not-minio.md) | Accepted |

## Replication and devices — `replication/`

| # | Record | Status |
| --- | --- | --- |
| 0025 | [The replication cycle: parked clone, overlay-and-diff, spool-gated publish](./replication/0025-replication-cycle.md) | Accepted |
| 0026 | [Spool upstream observations, and the one inference `matches_upstream` licenses](./replication/0026-spool-upstream-observations.md) | Accepted |
| 0027 | [iCloud transport from a pull-only clone; git metadata outside iCloud; Sync rejected](./replication/0027-icloud-transport.md) | Accepted |
| 0028 | [The settings baseline: a frozen seed, allowlist-captured, excluded from observation](./replication/0028-settings-baseline-seed.md) | Accepted |
| 0029 | [Vault remotes: GitHub primary, NAS bare repo as independence insurance](./replication/0029-vault-remotes.md) | Accepted |
| 0030 | [The committer: a CronJob with a detached git dir, structurally unable to author](./replication/0030-committer-shape.md) | Accepted |
| 0031 | [The apps staged out of replication; installing them is a deliberate day-one reset](./replication/0031-staged-app-rollout.md) | Accepted |

## Platform — `platform/`

| # | Record | Status |
| --- | --- | --- |
| 0032 | [A purpose-built headless Obsidian image](./platform/0032-obsidian-image.md) | Accepted |
| 0033 | [Volume and deployment shape: RWX, one replica, `Recreate`, vault in a subdirectory](./platform/0033-volume-and-deployment-shape.md) | Accepted |
| 0034 | [Infrastructure named by function, never by codename](./platform/0034-naming-function-over-codename.md) | Accepted |
| 0035 | [Tooling: Python with uv; one repository, one package](./platform/0035-tooling-python-one-repo.md) | Accepted |
| 0040 | [Content foundation provisioning: manual seed, mode discipline, day-one settings lock](./platform/0040-first-seed-and-settings-lock.md) | Accepted |
| 0041 | [The MCP stack selection, and the REST-bridge/filesystem-native fork it resolved](./platform/0041-mcp-stack-selection.md) | Accepted |
| 0043 | [Paths handed to git are always literal-marked pathspecs](./platform/0043-git-pathspecs-literal.md) | Accepted |

## Operability — `operability/`

| # | Record | Status |
| --- | --- | --- |
| 0036 | [Alerting deferred until AI triage; emission front-loaded](./operability/0036-alerting-deferred-emission-first.md) | Accepted |
| 0037 | [The vault-loaded signal: an independent exporter, not a probe](./operability/0037-vault-loaded-exporter.md) | Accepted |
