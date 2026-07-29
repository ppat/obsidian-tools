# FINDINGS v1 — Primary-Source Review behind [CANON-1](./CANON-1-designing-brain.md) / [CANON-2](./CANON-2-mcp-research.md) / [DESIGN-v2](./DESIGN.md)

**Access date for all sources: 2026-07-28.** Method: `gh api` (GitHub, authenticated) and WebFetch. WebSearch was unavailable (session budget exhausted); DuckDuckGo/Bing HTML search via WebFetch substituted where a search was needed.

**Scope discipline:** findings only. No design work, no analysis, no recommendations. Where a finding has design consequences, the finding is stated and stopped there.

---

## 1. Answers to open questions

### V7 — Can the Local REST API v4/v5 built-in MCP endpoint be disabled? — **ANSWERED: NO**

Source: `coddingtonbear/obsidian-local-rest-api` — `src/types.ts`, `src/main.ts` (read via `gh api`), `README.md`.

The plugin's settings interface (`LocalRestApiSettings`) contains fields only for `enableInsecureServer` / `enableSecureServer` (HTTP vs HTTPS), ports, and verbose logging. **Nothing scoped to MCP.** `main.ts` registers `/mcp/` unconditionally whenever the REST API server is enabled.

Issue [#271](https://github.com/coddingtonbear/obsidian-local-rest-api/issues/271) — *"Remove MCP from this plugin and place in separate plugin"*, opened 2026-06-04 — argued exactly this. Closed same-day with `state_reason: completed`, zero comments. Five releases later (5.0.2, 2026-07-24), MCP is still bundled with no disable switch.

**The built-in MCP endpoint cannot be disabled short of disabling the whole plugin.**

### V9 — Does Claude Code's PostToolUse / TaskCompleted hook actually block a write? — **ANSWERED: NO. This is a correction to [CANON-1](./CANON-1-designing-brain.md).**

Source: `code.claude.com/docs/en/hooks`.

The docs' own table: `PreToolUse` **can** block (exit 2). `PostToolUse` **cannot** — *"Shows stderr to Claude; the tool already ran."* `TaskCompleted` can block *task completion* only.

Corroborated by the exact reference project [CANON-1](./CANON-1-designing-brain.md) cites for this pattern. `eugeniughelbur/obsidian-second-brain`'s own `hooks/validate-ai-first.hook.yaml`:

> *"PostToolUse hook on Write and Edit... **Non-blocking: the file write still succeeds**; the agent sees the warning in stderr."*

[CANON-1](./CANON-1-designing-brain.md)'s claim that such a hook *"bounces the write if it fails"* and that *"nothing gets written without passing validation"* does not hold for PostToolUse.

### V6 — cyanheads/obsidian-mcp-server — **ANSWERED; fully confirms canon, no drift**

- Latest **v3.2.9** (2026-06-30), still latest as of access date. Digest `sha256:ae313e98…` confirmed exactly as canon reported.
- All six env vars real: `OBSIDIAN_READ_PATHS`, `OBSIDIAN_WRITE_PATHS`, `OBSIDIAN_READ_ONLY`, `OBSIDIAN_BASE_URL`, `MCP_AUTH_MODE`, `OBSIDIAN_ENABLE_COMMANDS`.
- **`WRITE_PATHS` is prefix-based with implicit recursion, not glob** — *"`projects/` matches `projects/a.md`, `projects/sub/b.md`"*.
- Path policy covers **all 14 tools**, including `obsidian_delete_note`.
- **New detail not in canon:** every mutating tool returns `previousSizeInBytes` / `currentSizeInBytes`, usable for clobber and concurrency self-detection.

### V4 — REST API / event-loop serialization — **New mechanism found; canon's caveat still holds**

Release **5.0.0** (2026-07-24 — postdates both canon docs) adds a real optimistic-concurrency primitive: the document map returns a content-hash `version`; pass it as `ifMatch` and a stale write fails with **`412 Precondition Failed`** instead of silently applying. Opt-in per request, not automatic.

This does not contradict canon's *"architectural side-effect, not a promised feature"* — but it is a genuinely new, documented primitive.

Separately: `bitbonsai/mcpvault` issue #109 received a **partial** fix (PR #138, merged) which only corrected a misleading *"No space left on device"* error message. The author states the underlying same-path race *"has no deterministic repro... tracked separately"* — **still open**.

### V1 — Does Obsidian continuously write per-instance state into `.obsidian/`? — **ANSWERED: YES, with named files**

Official Obsidian docs (`obsidianmd/obsidian-help`, "How Obsidian stores data"):

> *"If you use Git to manage your vault, you might want to add these files to `.gitignore`"*

naming **`.obsidian/workspace.json`** and **`.obsidian/workspaces.json`** specifically, *"because they update frequently based on current workspace state."*

### V2 — Read-only vault mode — **UNRESOLVED (absence of evidence, not evidence of absence)**

No official page mentions such a setting. The forum was not exhaustively searched. Reported as unresolved rather than confirmed negative.

### V3 / G1 — Can iOS Obsidian open a vault outside its own app container? — **ANSWERED: NO**

Official docs (`obsidian.md/help/sync-notes`): for iPhone/iPad, *"Recommended options: Obsidian Sync [and] iCloud"* — with iCloud requiring the vault at the specific path `iCloud Drive/Obsidian/[Vault Name]` that Obsidian's own app watches, **not an arbitrary external location**.

The same page lists **Dropbox, Google Drive, OneDrive and Syncthing as NOT officially supported on iOS**.

A forum thread (*"Possible way to sync IOS to External Vault?"*) shows a moderator rejecting a Shortcuts-based SMB workaround as too risky. **No source anywhere claims iOS Obsidian can open a vault in place from outside its sandbox.**

**New tool found, not in canon:** `psimaker/vaultsync` (132★, MPL-2.0, active) — syncs *into* Obsidian's iOS sandbox via Syncthing, not around it.

**Also newly notable:** iCloud is **Obsidian's own free, first-party recommendation** for iOS — not a third-party candidate on the same footing as SMB/rsync.

### V5 / G4 — Tasks-plugin inline field syntax — **ANSWERED**

- Emoji format: `📅 2023-04-16`
- Dataview format: `[due:: 2023-04-16]`, also `[priority:: high]`, `[repeat:: ...]`, `[dependsOn:: ...]`, `[onCompletion:: delete]`
- Bracket- or paren-wrapped `key:: value`.

### V10 — shanehull/obsidian-remote — **A pullable image NOW EXISTS (corrects canon)**

`ghcr.io/shanehull/obsidian-remote:v1.1.1` / `:latest`, published 2026-06-13. Corrects [CANON-2](./CANON-2-mcp-research.md)'s *"no published pull-able image"* claim. Memory and disk figures confirmed verbatim; no drift there.

### G2 — Do `linuxserver/obsidian` or `sytone/obsidian-remote` expose a human-usable GUI? — **ANSWERED: YES, both**

Via Selkies / KasmVNC remote desktop. linuxserver: ports 3000/3001. sytone: port 8080, page titled *"KasmVNC Client"*.

**New caveat:** linuxserver's GUI ships with **passwordless root/sudo** in its terminal — a real hardening consideration.

### V8 — LiteLLM MCP protocol — **CONFIRMED exactly, no drift**

*"Starting in LiteLLM v1.80.18, the LiteLLM MCP protocol version is `2025-11-25`."* Streamable HTTP / SSE / stdio all supported. Tool filtering confirmed server/namespace-level, **not** path-scoped.

### G3, G5, G6, G7, G8 — **NOT RESOLVABLE from primary sources**

These are deployment-specific questions with no externally citable claim.

---

## 2. New findings by source

### coddingtonbear/obsidian-local-rest-api

- Major rewrite to **5.0.2**: PATCH engine v1→v2 with JSON-object instructions; **DELETE now trashes by default** rather than permanently deleting.
- **Footgun:** 5.0.0 and 5.0.1 briefly required a Catalyst-only Obsidian pre-release. Relevant to anyone pinning `latest`.
- Periodic-note support split out into a separate companion plugin.

### Karpathy `llm-wiki` gist comment thread

- A **four-tier versioning model** — Raw → Staging → Mart/Wiki → Schema — beyond canon's two/three-layer summary.
- A specific **two-stage embedding + LLM dedup pipeline** reported reaching *"F1 to 1.000"*.
- A governance note: the schema should have **"one owner (or small council)"**, not open co-evolution.

### eugeniughelbur/obsidian-second-brain

- The hook script's **path-exclusion list** (`raw/`, `templates/`, `_export/`, `.obsidian/`, `.git/`, `boards/`, plus named operating files) is a concrete precedent for content-vs-operating-file separation.
- A **banned-Unicode-character mechanical rule** not mentioned in canon.

### codeculture.store / greg-asher/codex-obsidian

The repo is real (23★, active) but its own README explicitly states *"it does not bundle MCP servers or connector apps"* — contradicting the marketing article's "official Codex AI Agent plugin" framing and its specific behavioural claims. **Corroborates canon's fraud flag.**

### obsidianmd/obsidian-headless

A genuinely new **official npm CLI** for Obsidian Sync, supporting `--mode pull-only`. Named by neither canon doc. Requires paid Sync.

---

## 3. Corrections to canon

| # | Correction | Severity |
| --- | --- | --- |
| 1 | **[CANON-1](./CANON-1-designing-brain.md)'s "PostToolUse blocks writes" claim is false** — per Claude Code's own docs and per the reference project's own hook config (V9) | **Highest** |
| 2 | [CANON-2](./CANON-2-mcp-research.md)'s *"no published image"* for `shanehull/obsidian-remote` is outdated (V10) | Moderate |
| 3 | [CANON-2](./CANON-2-mcp-research.md)'s date for `StevenStavrakis/obsidian-mcp` (*"last updated 2026-02-19"*) does not match GitHub's `pushed_at` (**2025-06-23**) — a year earlier and far staler than stated. Flagged as a discrepancy, not resolved | Moderate |

---

## 4. Drift since late July 2026

- REST API plugin **4.x → 5.0.2**
- `obsidian-second-brain` **3,444 → 3,678★**
- `shanehull/obsidian-remote` image now pullable
- `sytone/obsidian-remote` ~9 months stale — **not flagged by canon**
- `MarkusPfundstein/mcp-obsidian` has **4,213★** — far more than cyanheads' 642 or mcpvault's 1,568, and **not starred at all in canon's account**
- mcpvault #109 partially fixed; race still open
- Dataview confirmed still dormant since 0.5.70

---

## 5. Sources not reached / partially reached

WebSearch unavailable for the whole session; DuckDuckGo/Bing via WebFetch substituted.

**Not independently verified:** Propsec · Obsidian Schema (`briansunter/obsidian-schema` 404'd — wrong owner/name, not chased) · Frontmatter Date Manager · Auto Note Mover · QuickAdd · Templater · OKF / `serradura/okf-gem` · `jlevere/obsidian-mcp-plugin` internals · `aaronsb/obsidian-mcp-plugin` · `ebullient/obsidian-vault-mcp` · both `second-brain-mcp` forks · `acuvity/mcp-server-obsidian` · official Bases/Properties docs (no `[V]` depended on them, and nothing encountered contradicted canon incidentally).

---

## 6. Source coverage

**Full read:** coddingtonbear/obsidian-local-rest-api · cyanheads/obsidian-mcp-server · shanehull/obsidian-remote · linuxserver/docker-obsidian · sytone/obsidian-remote · bitbonsai/mcpvault (+ issues #109, #138) · docs.litellm.ai (mcp + mcp_control) · Claude Code hooks docs · Karpathy gist + comment thread · eugeniughelbur/obsidian-second-brain (CLAUDE.md, hook config, hook script, ai-first-rules.md) · official Obsidian help pages (vault storage, config folder, iOS, sync-notes) · obsidianmd/obsidian-headless · obsidian-tasks docs · obsidian-community/obsidian-kanban · codeculture.store article · greg-asher/codex-obsidian · psimaker/vaultsync · the iOS forum thread.

**Metadata-only skims:** jacksteamdev/obsidian-mcp-tools · StevenStavrakis/obsidian-mcp · jlevere/obsidian-mcp-plugin · newtype-01/obsidian-mcp · MarkusPfundstein/mcp-obsidian · aleksakarac/obsidian-mcp · blacksmithgu/obsidian-dataview · platers/obsidian-linter.

**Not reached:** everything in §5.

---

## 7. Tally

**11 of 13** `[V]` / `[GAP]` items resolved: V7, V9, V6, V1, V3/G1, V5/G4, V10, G2, V8, V4 (partial).

**Unresolved:** V2 (read-only mode — absence not conclusively proven); G3, G5, G6, G7, G8 (deployment-specific, no citable primary source exists).
