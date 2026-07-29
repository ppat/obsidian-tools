# CANON DOC 1 — Designing "Brain": A Multi-Domain Obsidian Vault Shared by a Human and Multiple LLM Agents

> User-supplied research. Treat as canon. Content/organisation layer. Explicitly scopes MCP server selection OUT.

## TL;DR

- **Adopt the "LLM Wiki" mental model, not a PKM folder religion:** treat Brain as a compounding, agent-maintained artifact governed by three things that actually matter at write-volume — an immutable-vs-mutable ownership contract, a machine-readable schema file (`AGENTS.md`/`CLAUDE.md`) that turns agents into disciplined maintainers, and a non-optional periodic "lint" pass. Folder names (PARA/Johnny Decimal/LYT) are cosmetic by comparison.
- **Use a single unified base schema with domain-specific overlays, not a different system per domain:** every note carries the same core frontmatter (`source`, `status`, `type`, `created`, `updated`, `reviewed`, `confidence`), and finance/portfolio notes add *stricter* rules (mandatory inline provenance, `confidence: stated`, no-speculation guardrails) while homelab notes stay looser. Enforce it mechanically (Linter + a schema-validation plugin + a write-time hook), because "tell the LLM not to hallucinate" is a suggestion, not a rule.
- **Validate the content design on a deliberately small vault before scaling agent writes:** the "definition of done" is that your triage flow, Dataview/Bases surfacing views, and lint pass all work on ~100–200 notes with consistent frontmatter, an inbox that empties, and zero broken queries — then open the write floodgates.

## Key Findings

1. **The dominant, well-sourced pattern for exactly this use case is Andrej Karpathy's "LLM Wiki."** Posted April 2026; per Starmorph's guide the post "went viral — 16+ million views — and the follow-up GitHub Gist hit 5,000+ stars within days." Maps ~1:1 onto Brain: raw/immutable sources, an LLM-owned wiki layer, and a schema file. Its three operations — **ingest, query, lint** — and explicit tips (fixed attachment folder, `index.md`, append-only `log.md`, Dataview frontmatter, git history) are the closest thing to an authoritative reference architecture. Practitioners running it at 4,000+ concepts report the **single biggest failure mode is drift** (agents under-updating cross-references so pages silently go stale) and that **"the lint pass is not optional."**
2. **"AI slop dilution" is real and has named mitigations:** read-only-first agents, write-time validation hooks that *block* non-conforming writes, additive-only editing with sentinel markers so regeneration never clobbers human edits, scheduled lint (orphan/contradiction/stale-claim detection), and keeping a separate curated layer that only reviewed content is promoted into. Recurring warning: skipping approval checkpoints causes agents' internal state to diverge from the vault, and "ungated agents fail quietly" because a wrong summary still reads cleanly.
3. **Frontmatter consistency is the make-or-break variable.** Inconsistent frontmatter is the #1 cause of broken Dataview/Bases queries and unreliable agent retrieval ("active" ≠ "Active" to Dataview; missing fields silently drop notes). Fix: documented schema, templates, the Linter plugin, periodic agent sweeps.
4. **The best actively-maintained end-to-end reference project is `eugeniughelbur/obsidian-second-brain`.** 3,444 GitHub stars; a 45-command cross-CLI skill (Claude Code, Codex, Gemini, OpenCode, Antigravity, Hermes, Pi), v0.6.0 wiring its "AI-first rule" into all commands on 2026-04-26, explicitly "an evolution of Karpathy's LLM Wiki pattern." Its "AI-first rule" is a concrete, copyable frontmatter/convention spec. By contrast `jacksteamdev/obsidian-mcp-tools` was **archived (read-only) May 13, 2026** and must not be treated as maintained; much of the widely-circulated "Codex auto-maintains your vault" narrative traces to AI-generated marketing rather than actual repositories.
5. **Core Bases is the right default for dashboards; Dataview remains best for text-embedded queries; the Kanban plugin is a maintenance risk.** Bases debuted in 1.9.0 early access (2025-05-21), available to everyone in 1.9.10 (2025-08-18); 1.9.10 dropped singular `tag`/`alias`/`cssclass` in favour of plural list forms. Kanban's README states "The Kanban plugin is looking for new maintainers," moved under `obsidian-community`, last release over a year old with post-1.9 breakage reports. Keep the plugin set small — large plugin counts cause slow startup and conflicts, and Obsidian ships in Restricted Mode because plugins run with full system access.

## AREA 1 — Organizing a multi-domain human+multi-agent vault

### (a) How PARA / Zettelkasten / Johnny Decimal / LYT adapt when agents are the primary writers

The folder-naming debate is mostly a distraction. What survives are the *underlying principles*:

- **From Johnny Decimal — permanent addresses and shallow structure.** Every location has a stable unchanging ID so links don't rot when things get reorganised; forces a shallow enumerable structure. For agents: a **canonical, enumerable set of locations is something you can encode in the schema file**.
- **From PARA — actionability-based separation.** Sorting by how actionable something is maps onto the agent-writable vs curated split and onto a `status` lifecycle. You need its "archive is a first-class destination" idea so low-value notes have somewhere to go besides deletion.
- **From Zettelkasten — atomicity.** The one principle *more* important with agents: atomic notes embed and retrieve far better than 3,000-word brain dumps, and make agent edits lower-blast-radius. Unique-ID filenames optional; atomicity is not.
- **From LYT / MOCs — curated hubs over auto-generated indexes.** MOCs are the human-facing navigation layer. Key adaptation: mix a hand-curated MOC/Home note (human intent) with automated indexes the agent keeps current. Karpathy's `index.md` is essentially an agent-maintained MOC.

**Principles that matter at agent write-volume:** (1) a strict *ownership contract* — which layers are immutable, agent-owned, human-curated; (2) *enumerable, stable locations* encoded in a schema file; (3) *atomicity*; (4) *machine-readable structure* (typed frontmatter, predictable sections) because the agent is a primary consumer; (5) a *promotion path* from raw → reviewed. Folder taxonomy is downstream of these.

### (b) Preventing "AI slop dilution"

- **Read-first, write-later.** Start agents read-only to validate retrieval and structure before granting writes. This is how team "company brain" deployments stage rollout.
- **Write-time validation hooks that block, not warn.** Highest-leverage anti-slop mechanism: make the layer contract *physically unviolable* via tooling — a `PostToolUse`/`TaskCompleted` hook that validates format, completeness and cross-references and **bounces the write if it fails**. "Nothing gets written without passing validation." One implementer's key finding: "the biggest drift reducer for us wasn't better prompts — it was making the layer contract physically unviolable via tool boundaries."
- **Additive-only editing with sentinel markers.** `obsidian-second-brain` writes generated content inside `<!-- @generated -->` blocks and leaves `<!-- @user -->` blocks and anything outside markers untouched on re-runs — "It only adds or updates - never deletes, archives, or merges."
- **Non-destructive frontmatter passes.** Fill missing required fields with defaults, *never overwrite existing values*, log every change to a separate audit file — "Overwriting fields the developer set manually is the fastest way to lose trust in the automation and turn it off."
- **Scheduled lint as the anti-staleness engine.** Orphan detection, contradiction flagging, stale-claim checks on a timer, surfaced as a *reviewed* change list — never auto-applied for content claims.
- **Sample-review a small fraction of agent output** to catch the "reads cleanly but says the wrong thing" failure. Enterprise guidance suggests 1–5%; for a personal vault, weekly inbox review plus a lint report is the practical equivalent.

### (c) Inbox-triage workflow patterns and supporting metadata

1. **Capture** lands in `00-inbox/` with `status: inbox` and `source:` set automatically.
2. **Triage** (human weekly review or supervised agent pass): read → *promote / merge / archive / delete*. A Dataview query surfaces unprocessed items: `TABLE file.ctime AS Captured FROM "00-inbox" SORT file.ctime DESC`.
3. **Promote** to a curated `10-areas/*` folder, upgrade `status` to `processed`/`evergreen`, add wikilinks and MOC membership.
4. **Archive** low-value notes rather than leaving them to dilute retrieval.

Supporting frontmatter: `status:` (pipeline trigger — `inbox | processed | evergreen`, add `archived`); `reviewed:` date plus `review-flag`/`needs-review: true` separating "an agent wrote this" from "a human vetted it"; staleness tracking — Obsidian tracks mtime but there is *no native "last reviewed" field*, you add it. The **Frontmatter Date Manager** plugin can auto-stamp `created`/`updated`/`viewed` and bulk-populate from filesystem dates. Staleness presets seen in the wild: realtime(30m)/active(1h)/recent(24h)/weekly(7d)/monthly(30d)/quarterly(90d).

### (d) Domain separation: unified schema with domain overlays (recommended)

**One unified *base* schema, with stricter *overlays* per domain — not different systems.** A single schema keeps queries and agent conventions coherent; per-domain systems fragment both.

- **Finance / portfolio (strictest):** mandatory inline provenance with recency markers (`(as of 2026-07, source.com)`), `source:` must be `human`/`import` for any figure (agents may draft but not assert numbers), `confidence:` required defaulting to `stated`, explicit **no-speculation guardrail**. Mirrors enterprise financial-agent guidance: a guardrail that "blocks any output containing a number that's not in the source," provenance/lineage labels on AI-generated content, "distinguish evidence from speculation." One illustrative deployment cut factual error rate from ~4–6% to ~1% within a week (illustrative pattern, not a guarantee).
- **Homelab / infrastructure (freest):** agents write and self-edit freely; lightest gate. A production ops team ran this at 4,000+ interlinked concepts.
- **Travel research (medium):** provenance for facts (prices, hours, visa rules go stale fast → recency markers), freer synthesis.
- **Global todo/task list (cross-cutting):** this is a *view*, not a domain — a `TODO.md` with a Tasks query plus a Bases view over `status`/`due`, cutting across all domains.

Encode overlays as sections in the schema file and, where enforceable, as path-scoped validation rules.

### (e) Linking discipline when agents create links

- **Prefer contextual `[[wikilinks]]` written inline** — `obsidian-second-brain`'s AI-first rule requires a wikilink "for every person, project, idea, decision, and concept referenced."
- **Guard against over-linking and link rot** with a *page-exists check before creating a new page*: *new page* only for "a distinct entity/concept you'd link to from elsewhere," otherwise *edit in place* — keyed to a canonical naming convention, enforced by the schema, not agent judgment. Single best defense against near-duplicate pages.
- **Automated MOCs for known structure, hand MOCs for thinking.**
- **Lint for orphans and dangling links** on a schedule.
- Consider **declaring dependencies in frontmatter** (`refs: [page-x]`) for a computed dependency graph, so when a source changes you can query which downstream pages went stale (push-based staleness).

### (f) Concrete frontmatter schema recommendation

```yaml
---
type: note            # required; enum: note|source|entity|concept|project|person|task|decision|devlog|meeting|research|moc
title: "…"
source: human         # required; human|openclaw|n8n|claude-code|home-assistant|import
status: inbox         # required; inbox|processed|evergreen|archived
created: 2026-07-28   # ISO YYYY-MM-DD always
updated: 2026-07-28   # auto-stamped
reviewed:             # empty until a human vets it
tags: []              # lowercase values
confidence: stated    # stated|high|medium|speculation (required in finance)
related: []           # wikilinks / refs for computed dependency graph
---
```

Conventions: **ISO dates everywhere**; **lowercase tag values**; **double-quote strings when in doubt**; one field = one type vault-wide (never mix a date and a string in `due`); `type` non-empty on every note (OKF's single hard rule). Finance adds required `provenance:`/inline `(as of …, source)` markers and default `confidence: stated`. Note the Obsidian 1.9.10 breaking change: singular `tag`/`alias`/`cssclass` dropped in favour of plural list forms.

## AREA 2 — Getting-started guidance (content side only)

### (a) Starter skeleton and day-one `.obsidian` config worth committing

- **A `README.md` / `00-index.md` Home note** at vault root — hand-curated MOC linking each domain's MOC and the schema doc. Human entry point and agent orientation page.
- **A schema file the agents read** — `AGENTS.md` and/or `CLAUDE.md`. *The most important file in the repo* per every LLM-Wiki practitioner: "It defines the wiki's structure, naming conventions, page templates, and operational workflows. It transforms a generic LLM into a disciplined knowledge worker." Encodes the ownership contract, folder map, frontmatter schema, per-domain rules, and ingest/query/lint workflows.
- **A `log.md`** (append-only, ISO-dated) so agents and git both record what changed.
- **A `_templates/` folder** with one template per note `type`, pre-populating correct frontmatter.
- **A frontmatter-schema note** documenting the schema for humans, mirroring `AGENTS.md`.
- **`.obsidian` config to commit:** enabled community-plugin list + settings, hotkeys, templates-folder pointer, daily-notes settings, Files & Links settings. Committing it means the vault opens configured on any device rather than blank.

### (b) Settings painful to change retroactively

- **Default location for new notes** → `00-inbox/`.
- **Default attachment folder** → "In the folder specified below," a fixed path. Default behaviour dumps attachments in the vault root and creates chaos at scale.
- **Daily-note date format = `YYYY-MM-DD`** and daily-note folder fixed. Most-cited "get this right early" convention because agents and Dataview parse ISO reliably.
- **New-note template / template folder** wired so every new note is born with correct frontmatter.
- **Property types** — declare `created`/`updated`/`reviewed` as Date, `tags` as List, `status`/`type`/`source` as Text, so the Properties UI and Bases treat them correctly from the start.

### (c) One-time human setup checklist

```
[ ] Enable core plugins: Properties, Bases, Templates, Daily Notes, Backlinks, Outgoing Links
[ ] Enable community plugins (small set): Tasks, Dataview, Templater, QuickAdd, Linter,
    + one schema-validation plugin (Propsec or Obsidian Schema), + Frontmatter Date Manager
[ ] Files & Links: default new-note location = 00-inbox/; attachment folder = fixed path
[ ] Daily Notes: date format YYYY-MM-DD; folder = 40-journal/
[ ] Create _templates/ with one template per note type
[ ] Write AGENTS.md + CLAUDE.md schema file
[ ] Write the human-facing frontmatter-schema note
[ ] Create 00-index.md Home MOC + one MOC per domain
[ ] Create log.md (append-only) and TODO.md (Tasks query + Bases view)
[ ] Configure Linter rules (YAML key order, insert missing fields, ISO dates, lowercase tags)
[ ] Configure schema-validation rules per path (stricter for finance/)
[ ] Add a write-time validation hook if your agent runner supports it (block non-conforming writes)
[ ] Commit .obsidian config to git
[ ] Seed 10–20 real notes by hand to exercise every template and query
```

### (d) Definition of done before scaling agent writes

Validate on ~100–200 notes:

- **Every note passes schema validation** (no missing required fields, no type mismatches) — zero errors.
- **All Dataview/Bases views render with no empty/broken tables.**
- **The inbox actually empties** through triage at least once end-to-end.
- **The lint pass runs and produces a reviewable report** (orphans, contradictions, stale claims) — and you've acted on it once.
- **The surfacing loop works:** `TODO.md` and dashboard views show the right tasks and recent notes.
- **A sample of agent-written notes reads correctly *and* is factually grounded**, with finance notes carrying provenance.
- **Round-trip test:** an agent creates a note, you edit a `<!-- @user -->` block, the agent re-runs, and your edit survives.

## AREA 3 — Ecosystem tools/plugins/patterns worth adopting

### (a) Inbox processing / triage automation

- **QuickAdd** — capture, templates, routing on one hotkey. Note: does **not** watch folders; explicit, hotkey-driven. Actively maintained.
- **Templater** — dynamic templates with variables/dates/JS; the power layer for auto-populating frontmatter. Pair QuickAdd (capture) + Templater (structure).
- **Auto Note Mover** — rule-based moving of notes out of the inbox by tag/frontmatter (one rule per tag limitation).
- **AI-native options:** *Vault Operator* (agentic operating layer, BYOK+MCP, "every write needs your approval, every change is undoable," free/OSS, local-first) and *Copilot for Obsidian*; the *okb / obsidian-kb* plugin adds local semantic search + MCP aligned to the LLM-Wiki pattern (desktop-only). Since agents mostly write via external CLIs/MCP, treat these as optional human-side conveniences.

### (b) Dashboards / surfacing loop beyond core Bases

- **Bases (core, available to all since 1.9.10)** — right default for filtered/sorted/grouped dashboards over frontmatter; native, fast even on 20,000 notes, readable without a plugin dependency.
- **Dataview** — best for **text-embedded, inline queries** and DataviewJS dashboards ("notes not reviewed in 90 days"). Complementary to Bases, not either/or.
- **Charts** for quantitative surfacing.
- **Kanban** — **maintenance risk**: "looking for new maintainers," latest release over a year old, ~533 open issues, post-1.9 breakage. Prefer Bases board-style views or a Dataview-driven board.

### (c) Frontmatter linting / schema validation

- **Linter (`platers/obsidian-linter`)** — actively maintained; workhorse for *formatting* enforcement (YAML key order, ISO dates, lowercase tags, spacing). Does **not** natively insert absent frontmatter keys — pair with templates.
- **Propsec** — validates frontmatter against user-defined schemas *without modifying notes*; path/tag/property-scoped schemas, required/warn/unique flags, union types, cross-field constraints, conditional validation. Closest native fit for **per-domain overlays**.
- **Obsidian Schema** (`briansunter`) — JSON-Schema validation with tag-driven selectors, results sidebar, inline errors; rules only fire on matching notes. Pure-function validators can be driven from a Bun script to audit the vault in CI. No autofix.
- **External CI option:** validate YAML frontmatter against JSON Schema in a git pre-commit/CI step.

### (d) End-to-end "Obsidian vault as AI-agent KB" reference projects

- **`eugeniughelbur/obsidian-second-brain`** (3,444 stars, active mid-2026, MIT) — **the strongest reference.** Cross-CLI skill, explicitly "an evolution of Karpathy's LLM Wiki pattern: a vault that rewrites itself." Its CLAUDE.md states every note MUST follow `references/ai-first-rules.md` and notes "Vault notes are designed for future-Claude retrieval, not human reading." Publishes a concrete **"AI-first rule"**, sentinel-marker non-destructive editing, and a `validate-ai-first.sh` write hook.
- **Karpathy `llm-wiki` gist** — the pattern itself (raw/wiki/schema; ingest/query/lint). Read the gist *and* its comment thread (production war-stories on drift, dedup, team sharing).
- **`serradura/okf-gem` + Google's Open Knowledge Format (OKF)** — vendor-neutral markdown-KB spec (Google Cloud, ~June 2026); one hard rule: "YAML frontmatter with a non-empty `type`." Companion plugins (`obsidian-okf`, "OKF Enforcer") validate `type`/`index.md`/`log.md` on save. Useful as a minimal portable baseline. (OKF is Google's spec, not Karpathy's — a common misattribution.)
- **MCP-side reference servers** (architecture context only): `MCPVault`/`@bitbonsai/mcpvault` (filesystem, active); REST-API-based servers require the Local REST API plugin. **`jacksteamdev/obsidian-mcp-tools` is archived/read-only as of May 13, 2026** — do not adopt as a live dependency.
- **Caution:** the widely-cited "Codex CLI auto-triages your inbox and enforces frontmatter, 32 commits/week" story and some vendor blogs describe features **not present in the actual repos** they cite. Treat as unverified.

`obsidian-second-brain`'s published "AI-first rule" (copyable): a `## For future Claude` preamble on every note; rich frontmatter (`type`, `date`, `tags`, `ai-first: true`, plus type-specific fields); `[[wikilinks]]` for every entity; recency markers `(as of 2026-04, source.com)`; confidence levels `stated | high | medium | speculation`; hard anti-fabrication rules ("never invent facts, never claim absence without an exhaustive search, mark unknowns as TBD").

### (e) Multi-writer pitfalls (content/organisation, not sync)

- **Frontmatter schema drift** — the #1 killer. Mixed `tags:` YAML vs inline `#tags`, mixed date formats, notes with no frontmatter → Dataview returns empty tables and agents get unreliable metadata.
- **Dataview query breakage from malformed notes** — case sensitivity ("Active" ≠ "active"), unquoted folder paths, mixed field types, missing fields silently dropping notes. Scope queries with `FROM "folder"`/`#tag` and `LIMIT`.
- **Near-duplicate pages / over-linking** — agents spawning a new page instead of editing an existing entity. Fix with the page-exists check + canonical naming.
- **Silent drift / "reads clean but wrong"** — ungated agents fail quietly. Fix with approval gates and sampling.
- **Plugin bloat and conflicts** — large plugin counts slow startup and cause conflicts; every Obsidian update risks breaking a plugin. Standardise on ~8–12 vetted plugins. Security note: community plugins run with full system access.
- **Two agents editing the same page** — at multi-agent scale use page-level locks/leases and a review gate before pages land; a lint bot may auto-fix mechanical breakage (dead links) but never content claims.

## Recommendations

**Stage 1 — Foundation (before any agent writes):**

1. Write `AGENTS.md` + `CLAUDE.md` schema file first. Highest-leverage artifact.
2. Adopt the base frontmatter schema; commit `_templates/`, `00-index.md` Home MOC, `log.md`, `TODO.md`.
3. Lock the retroactively-painful settings.
4. Install a *small* plugin set: Tasks, Dataview, Templater, QuickAdd, Linter, one validation plugin (**Propsec** for path-scoped rules, or **Obsidian Schema** for JSON-Schema-in-CI), Frontmatter Date Manager. Commit `.obsidian`.

**Stage 2 — Validate on a small vault:**
5. Seed ~100–200 notes; run agents **read-only** first. Confirm retrieval quality against real questions.
6. Hit the definition of done.

**Stage 3 — Scale writes with gates:**
7. Enable agent writes, only *inside agent-writable zones*, only through a **write-time validation hook that blocks non-conforming writes** where the runner supports it. Additive-only editing with sentinel markers for regeneration.
8. Put lint on a schedule; review weekly alongside the inbox. Enforce the finance overlay.

**Thresholds that should change the approach:**

- **Vault > ~500 notes with 5–10 captures/day and inbox regularly > 20** → automated maintenance pays for itself; formalise scheduled lint/triage.
- **Flat `index.md` stops fitting in context (low thousands of pages)** → add hybrid search (BM25 + local embeddings); a retrieval concern, not a reorg.
- **Near-duplicate pages appearing** → tighten the page-exists check and canonical naming before adding more agents.
- **Startup time climbs or a plugin breaks after an update** → prune plugins; prefer core Bases.

## Caveats

- **Recency/quality of sources:** the LLM-Wiki ecosystem exploded April–July 2026, so much of the best material is very recent blog posts and GitHub threads, not battle-tested documentation. Several frequently-cited write-ups are AI-generated or marketing content that misattributes features to real repos — rely on the Karpathy gist, `obsidian-second-brain`, and official Obsidian docs as primary.
- **The "42% workflow retraction" and "4–6%→1% error-rate" figures are illustrative** from vendor/analyst sources, not controlled studies.
- **Kanban plugin** is functional today but actively seeking maintainers with post-1.9 breakage reports.
- **Bases is still young** (stable since 1.9.10 but evolving); 1.9.10 introduced a breaking change to singular property keys.
- **MCP server selection is explicitly out of scope** for this document.
- **Provenance enforcement is only as strong as your hook layer.** Prompt-level "don't speculate" instructions are suggestions, not rules — the finance guardrail only holds if enforced mechanically at write time.
