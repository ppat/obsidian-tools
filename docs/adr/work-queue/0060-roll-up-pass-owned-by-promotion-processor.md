# 0060. The roll-up pass is `promotion-processor`'s: salience sets prominence, and a merge copies its sources verbatim rather than rewriting them

**Status:** Proposed (supersedes agent-maintained indexes and the page-level lease convention in [ADR-0042](../content-model/0042-adopted-community-pattern.md)) ·
**Pillar:** [The vault system does its own job](../../../DESIGN.md#the-vault-system-does-its-own-job) ·
**Serves:** [S3](../../../USE_CASES.md#s3--placed) ·
**Unit:** [A8](../../../ROADMAP.md#group-a--pipeline-mechanisms) ·
**Ticket:** [ot#85](https://github.com/ppat/obsidian-tools/issues/85)

## Context

[S3](../../../USE_CASES.md#s3--placed)'s advanced half is where the platform's purpose lands:
important ideas rise to where agents act on them, less important ones sink, and notes about one
subject are merged, with a record kept either way. Its first pass is part of project done. The agent
runtime ([ADR-0054](../platform/0054-agent-runtime.md)) supplies the judgement: `salience:` scores
([ADR-0012](../content-model/0012-salience-consolidated-fields.md)), merge proposals, and retirement
verdicts.

Six constraints bound who acts on that judgement, and how:

- **Salience is a ranking within a batch, never an absolute threshold** (ADR-0012). Moving notes
  because they "cross a threshold" would therefore archive every batch's bottom, whatever it is worth.
- **Generated content is additive only, never a delete or merge by regeneration**
  ([ADR-0042](../content-model/0042-adopted-community-pattern.md)). A resolution never rewrites a
  claim ([ADR-0055](../content-model/0055-lint-findings-resolved-by-the-vault.md)).
- **No unattended process restructures the vault.** A rename is structural batch work
  ([ADR-0013](../content-model/0013-filename-slug-page-exists.md)).
- **Curated writes go through the ingestor handle and the validator's three callers**
  ([ADR-0007](../write-model/0007-validation-placement.md)).
- **Agent readers read raw markdown.** An embed they cannot expand does not put content in front of
  them.
- **A verdict can be wrong, and nothing waits on a human to undo it.**

## Decision

**The roll-up pass is a scheduled entrypoint of `promotion-processor`**, whose one job is relocation.
It writes through that component's ingestor handle, sends every curated post-image through the
validator, and takes no mount.

**Candidates.** A note is a candidate when:

- its `updated:` is later than its `consolidated:`;
- it has never been scored;
- the lint pass handed it off in its latest report (orphans, near-duplicates);
- it is an archived *retired* note that a curated note now links to.

**Salience sets prominence, not location.** The pass writes `salience:`, and the vault's indexes
are queries ordered by it — computed, never agent-written rows. Promotion and demotion in S3's advanced half are a note rising or
falling there; the file does not move.

**Only curated notes merge.** Every source has already passed the validator. An inbox note never
merges directly: if its curated path is taken, promotion places it beside the note already there
([ADR-0056](./0056-inbox-is-promotions-work-list.md)), and it can become a merge candidate later,
like any curated note.

**A note leaves curated space in two ways only.**

- **Retirement.** The runtime judges a note superseded or obsolete, with its grounds. Salience rank
  alone never retires a note. A retired note moves to the archive, and it is restored when new
  curated content links to it and the runtime judges the retirement no longer holds. A wrong
  retirement is therefore undone by the vault itself.
- **Merge.** The runtime judges two or more curated notes to be one subject and names the survivor.
  The pass then:
  1. records the merge in progress under `_ops/`: the survivor and the sources;
  2. appends to the survivor one generated block per source, inside sentinel markers. Each block is
     a **verbatim copy** of the source's body — no model text — and its marker carries the source's
     path and provenance (`source:`, `authority:`, `confidence:`);
  3. sends the survivor's post-image through the validator, which judges the copied content with
     it;
  4. moves each source to the archive;
  5. sets the survivor's `consolidated:`, and clears the in-progress record.

  A crash leaves the in-progress record, and the next pass completes the merge from it. A merge the
  validator refuses — for example a finance source whose evidence fields the survivor's note-level
  fields cannot carry — is recorded as *not merged*, with the refusal as its reason, and is not
  proposed again until one of the notes changes. A decision
  *not* to merge is recorded too. Links to a merged source land on its archived copy, which names the
  survivor, and the lint pass repoints them to the survivor.

**The archive.** A move goes to `90-archive/<date>/<original path>`, so archived notes never share a
path. A merged source's archive entry is permanent, since its content now lives in the survivor. A
retired note's entry stands until a restoration reverses it.

**Provenance of mixed content.** Note-level fields describe the note's own authored content and are
never changed by the pass. A copied block is attributed by its marker, which carries the source's
provenance with it.

**The pass's own writes do not re-qualify a note.** It sets `consolidated:` at the time of its own
write.

**No page-level lease.** The pass only appends, never rewrites a whole page. Whole-note rewrites
happen only in batch runs, under the run's own lease on the stopped agent instance
([ADR-0052](./0052-batch-mode-stops-the-agent-instance.md)) and per-file staleness
([ADR-0048](./0048-batch-staleness-per-file-hash.md)). Ordinary writes rely on the commuting
primitives and on after-the-fact clobber detection, because no write tool takes a precondition that
would make optimistic concurrency possible.

**Nothing is renamed.**

## Alternatives considered

- **Moving notes when normalised salience crosses a threshold** — archives each batch's bottom.
- **A merge that writes composed, model-authored text** — it rewrites claims through the runtime's
  judgement.
- **A merge that transcludes archived sources** — agent readers see the embed syntax, not the
  content, and the validator never judges the transcluded content.
- **Roll-ups as batch patches** — this would put the batch credential in an unattended process.
- **A terminal archive for retirements** — only a human could undo a wrong verdict.
- **A separate roll-up component, or the lint pass** — the first adds a fourth holder of the widest
  handle; the second braids placement into maintenance.

## Consequences

- **The survivor carries its sources' content in curated space**, readable by every reader and
  judged by the validator.
- **The definition-of-done round-trip test can fail.** A human-marked passage in a source must
  survive in the survivor byte for byte; a mangled copy fails it.
- **`promotion-processor` holds one credential per role:** its consumer identity, the sweep's
  pointer credential, the ingestor handle, and the model endpoint.
- **The archive's only writer is the roll-up pass.**
