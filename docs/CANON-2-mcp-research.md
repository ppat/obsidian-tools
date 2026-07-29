# CANON DOC 2 — Choosing an Obsidian MCP Server for a Flux-Managed Kubernetes Homelab

> User-supplied research. Treat as canon. MCP/technical layer. Does NOT determine the device-sync topology.

## TL;DR

- **Primary recommendation: `cyanheads/obsidian-mcp-server` fronted by a headless Obsidian + Local REST API container** (`shanehull/obsidian-remote` as reference architecture, or a self-built `linuxserver/obsidian` sidecar). It is the only actively maintained candidate that natively ships path-scoped read/write permissions (`OBSIDIAN_READ_PATHS`/`OBSIDIAN_WRITE_PATHS`/`OBSIDIAN_READ_ONLY`), Streamable HTTP transport, an official multi-arch GHCR Docker image (Apache-2.0, latest 3.2.9), surgical/additive editing, and wikilink+tag+frontmatter+Dataview-DQL awareness.
- **The hard architectural fork:** the most capable servers (cyanheads, the built-in Local REST API v4 MCP) require the Obsidian app (Electron) running, while the truly app-free servers (`bitbonsai/mcpvault`, `StevenStavrakis/obsidian-mcp`) are filesystem-native but weaker on path-scoping, transport, and concurrency. You resolve this by running **headless Obsidian inside the cluster** so "requires the app" no longer means "requires a device awake."
- **No candidate natively enforces a `source:`/schema frontmatter contract or safe concurrent multi-client writes.** Schema enforcement must be layered in (init/sidecar validator, or `jlevere/obsidian-mcp-plugin`'s JSON-schema tool), and write serialization must come from confining all writes through a single MCP→single Obsidian instance plus git-based recovery.

## Key Findings

**The landscape splits into two families.** Filesystem-native servers read/write `.md` files directly and need no Obsidian app; REST-bridge servers proxy to the `coddingtonbear/obsidian-local-rest-api` plugin, which runs inside Obsidian. For a Kubernetes deployment with no device guaranteed awake this distinction is the pivot of the whole decision — but it is dissolvable by running Obsidian headless in the cluster.

**`jacksteamdev/obsidian-mcp-tools` is confirmed disqualified.** Archived read-only May 13, 2026; author no longer uses Obsidian. Last release 0.2.31 (April 2026). Reference-only.

**`cyanheads/obsidian-mcp-server` is the strongest maintained REST-bridge server.** 14 tools, STDIO and Streamable HTTP transports, JWT/OAuth auth on HTTP, official Apache-2.0 multi-arch (amd64+arm64) image on GHCR — `latest` → **3.2.9** (~17 days before report; digest `sha256:ae313e98…`), 3.2.8 prior (~27 days), README badge lags at 3.1.9. ~633 stars, 233 commits. Native folder-scoped permissions via env vars. Requires the Local REST API plugin, but `OBSIDIAN_BASE_URL` is fully configurable and can point at a remote/in-cluster REST API host.

**`shanehull/obsidian-remote` directly resolves the headless tension** — bundles headless Obsidian (Electron/Chromium under Xvfb), auto-trusted Local REST API plugin, and a Go MCP server with Streamable HTTP (`/mcp`) and SSE (`/sse`) plus RFC 9728 OAuth/JWT in one container. But: very new, single-maintainer (1 star, GPL-3.0, v1.1.1 June 13 2026), documented for `docker compose up --build` rather than a published image, Chromium-heavy — README states "Disk usage includes the Docker image (~2 GB base) plus vault storage. Typical runtime memory sits around 300–500 MB depending on vault size." Best treated as a **reference architecture**, not a turnkey dependency.

**The Local REST API plugin itself now ships a built-in MCP server (v4.0+).** Renamed "Local REST API with MCP"; exposes MCP at `https://127.0.0.1:27124/mcp/` over Streamable HTTP with Bearer auth, with direct access to live vault metadata, active file, command palette. Its README: "Several third-party MCP servers for Obsidian exist, but they are no longer necessary — this plugin ships a built-in MCP server that runs inside Obsidian and has direct access to your vault's live metadata, active file, and command palette." Partly obsoletes the "plugin + generic REST wrapper" path — but still requires Obsidian running and offers **no path-scoped write permissions or schema enforcement**.

**`bitbonsai/mcpvault` is the strongest app-free option but has confirmed concurrency gaps.** Actively maintained (1.6k stars, MIT, 243 commits, renamed to `@bitbonsai/mcpvault` on npm "at Obsidian's request" in v0.9.0, March 2026), pure Node/TypeScript, no plugin needed, 14 tools including `patch_note`, `update_frontmatter`, `manage_tags`, `.base`/`.canvas` awareness, and a `wiki_link` tool (PR #101, July 23 2026) resolving `[[Note]]`, `[[Note|Display]]`, `[[folder/Note]]`. But **stdio-only** (no HTTP/SSE — a blocker for cluster-hosted-behind-Ingress), and open issue #109 (v0.11.0) documents concurrent same-path writes hitting an unhandled race with "no per-path serialization" — no atomic temp-file rename, no advisory locks.

**No candidate enforces the frontmatter schema contract natively.** The one server built around schema validation is `jlevere/obsidian-mcp-plugin` ("Vault MCP"), which generates MCP tools from user-defined JSON-Schema-draft-07 (written in YAML) and validates structured writes — but it is an in-Obsidian Electron plugin (SSE/StreamHTTP, runs in the renderer), not a standalone container.

## Candidate comparison against the 10 criteria

✅ native/strong · ⚠️ partial/needs work · ❌ absent/blocker

| # | Criterion | cyanheads | shanehull/obsidian-remote | bitbonsai/mcpvault | Local REST API built-in MCP (v4) | StevenStavrakis | jlevere Vault MCP |
|---|---|---|---|---|---|---|---|
| 1 | Active maintenance | ✅ 233 commits, v3.2.9 | ⚠️ active but 1★, single dev | ✅ 1.6k★, 243 commits | ✅ core plugin, active | ⚠️ 628★, last updated 2026-02-19 | ⚠️ niche, active |
| 2 | Headless / no Electron | ❌ needs Obsidian+plugin (can be in-cluster headless) | ✅ bundles headless Obsidian | ✅ pure filesystem | ❌ runs inside Obsidian | ✅ pure filesystem | ❌ runs inside Obsidian |
| 3 | Path-scoped writes | ✅ `OBSIDIAN_READ_PATHS`/`WRITE_PATHS`/`READ_ONLY` | ⚠️ inherits REST API; scoping if cyanheads layered | ❌ traversal guard only, no allow-list | ❌ none | ❌ none | ⚠️ schema-driven, not folder allow-list |
| 4 | Wikilinks + tag/frontmatter search | ✅ outgoing links, tags, DQL, JSONLogic, frontmatter | ✅ via REST API | ✅ `wiki_link` tool, frontmatter/BM25 search | ✅ live metadata, JsonLogic | ⚠️ basic tags/search | ✅ structured queries |
| 5 | Dataview/Bases awareness | ✅ markdown+YAML; DQL search when plugin reachable | ✅ via REST API | ✅ `.base`/`.canvas` support | ✅ native | ⚠️ plain markdown | ⚠️ schema focus |
| 6 | Frontmatter schema enforcement | ❌ manages frontmatter, no validation gate | ❌ | ⚠️ prevents YAML corruption, no schema gate | ❌ | ❌ | ✅ JSON-Schema validation tool |
| 7 | Non-destructive/additive editing | ✅ append/patch/prepend/section, anti-clobber default | ✅ via REST API PATCH | ✅ append/prepend/patch_note | ✅ PATCH by heading/block/frontmatter | ⚠️ basic edit | ✅ diff-based edits + rollback |
| 8 | Packaging | ✅ official GHCR multi-arch; Node/Bun | ⚠️ build-from-source; Go | ⚠️ npm/npx, no official image | ⚠️ ships with plugin | ⚠️ npm/npx | ⚠️ Obsidian plugin |
| 9 | Multi-client concurrency safety | ⚠️ no explicit locks; implicit serialization via single Obsidian instance | ⚠️ same model | ❌ documented race, no locking (#109) | ⚠️ single Obsidian event loop | ❌ no handling | ⚠️ single Electron instance |
| 10 | HTTP/SSE transport | ✅ Streamable HTTP + STDIO | ✅ HTTP + SSE | ❌ stdio only | ✅ Streamable HTTP | ❌ stdio only | ✅ SSE + StreamHTTP |

## On criterion 5 (Dataview/Bases)

Treating the vault as "plain markdown + YAML frontmatter" and letting Dataview/Bases render client-side is correct and sufficient. Dataview stores queries as inert code blocks; results render at view-time and are never written. Bases (core since 1.9.10) stores views as `.base` files backed by frontmatter properties. An MCP server needs only to not corrupt code blocks or `.base` files. cyanheads is Dataview-DQL-aware for *search*; mcpvault treats `.base`/`.canvas` as first-class. The risk is servers doing naive whole-file rewrites that mangle multi-line YAML — which mcpvault's gray-matter AST-aware handling and cyanheads' surgical PATCH are designed to avoid. (Dataview has been effectively dormant since v0.5.70, April 2025; Bases is the recommended successor — another reason to keep the MCP server format-agnostic.)

## On criterion 6 (schema enforcement) — nobody does this the way you want

Blocking a write unless it carries a valid `source:` plus the broader schema is not natively satisfied by any container-deployable server. Options in order of robustness:

1. **Sidecar/init-container validator:** front the MCP server with a small validation proxy, or use cyanheads' write-path scoping to force all writes through an inbox then have a Flux-managed CronJob/controller validate and promote. Keeps the MCP server agnostic.
2. **`jlevere/obsidian-mcp-plugin` (Vault MCP):** the only server with built-in JSON-Schema validation of structured writes. Runs inside Obsidian's Electron renderer, so it only helps if already running headless Obsidian in-cluster, and it changes the tool surface agents see.
3. **System-prompt discipline + git-diff gate:** weakest; relies on the agent and on detect-and-escalate to catch violations after the fact. Given writes are agent-heavy, do not rely on this alone for curated zones.

## On criterion 9 (concurrency) — the real risk, and how to neutralize it

- **mcpvault:** confirmed unsafe for concurrent same-path writes — issue #109 shows whole-file rewrites with "no per-path serialization," and stdio-only means each client spawns its own process with zero shared locking (worst case).
- **cyanheads / any REST-bridge / headless-Obsidian setup:** no documented locking guarantee, but all writes funnel over HTTP to a single Local REST API instance inside one effectively single-threaded Obsidian process, so writes are *implicitly serialized* at the Obsidian layer. An architectural side-effect, not a promised feature — but materially safer than N independent filesystem writers.
- **Mitigation regardless of choice:** confine agent writes to additive zones using `OBSIDIAN_WRITE_PATHS`; prefer append/patch over overwrite; lean on git as merge authority (detect-and-escalate). That combination makes residual concurrency risk tolerable.

## LiteLLM integration reality check

LiteLLM's MCP Gateway filters access **at the server (namespace) level per virtual key/team**, and supports `allowed_tools`/`disallowed_tools` and `allowed_params` (docs.litellm.ai/docs/mcp_control). It namespaces tools by prefixing the server name; "Starting in LiteLLM v1.80.18, the LiteLLM MCP protocol version is 2025-11-25." Virtual keys mapping clients to MCP/tool subsets is supported: n8n a key scoped to read tools only, Claude Code/OpenClaw keys with write tools. Documented caveat: LiteLLM's native filtering is server/namespace-level with per-tool `allowed_tools` on top; it is **not** a substitute for *path*-scoping inside the Obsidian MCP server. Use both layers: LiteLLM decides *which tools* a client sees; `WRITE_PATHS` decides *which folders* a write can touch.

## Servers considered and set aside

- **`StevenStavrakis/obsidian-mcp`** (628★, MIT, TS, last updated 2026-02-19, 0 releases): filesystem-native, read+write, stdio-only, "in active development" with explicit backup warnings, no path-scoping or HTTP transport. Fine for a laptop; not for a cluster.
- **`newtype-01/obsidian-mcp`** (306★): has Dockerfile and docker-compose, dual REST+filesystem strategy, but oriented around `OBSIDIAN_API_TOKEN` + plugin, JavaScript, smaller community, no path-scoped writes.
- **`MarkusPfundstein/mcp-obsidian`** (`mcp/obsidian` image): classic REST-plugin bridge; solid tool set (patch/append/delete) but no path-scoping and requires the app. Acuvity publishes a hardened Kubernetes-oriented image (`acuvity/mcp-server-obsidian`, HTTP/SSE on port 8000).
- **`aleksakarac/obsidian-mcp`**: 45-tool hybrid (33 filesystem + 12 API) fork, Python/uv, offline backlinks/tags/broken-link analysis. Interesting for read-heavy analytics but heavier, less proven, no official image.
- **`CoMfUcIoS/second-brain-mcp`**, **`noesskeetit/second-brain-mcp`**: read-only/semantic-memory servers (SQLite/bge-m3, human-in-the-loop writes). Good inspiration for additive-write and read-only patterns, not general read+write servers.
- **`aaronsb/obsidian-mcp-plugin`**, **`ebullient/obsidian-vault-mcp`**, MCP Connector: in-Obsidian plugins exposing HTTP MCP with permissions/path allow-lists and Dataview/Bases support — strong feature sets, but require Obsidian running and are plugin-form, not containers.

## Documentation-vs-reality flags

- AI-generated "best Obsidian MCP" blog content frequently conflates the filesystem and REST-bridge families and misattributes HTTP transport or path-scoping to servers that lack them. Verified: mcpvault is stdio-only; StevenStavrakis is stdio-only; cyanheads' path-scoping and HTTP are real.
- shanehull/obsidian-remote's README claims are accurate but its 1-star, single-maintainer, build-from-source status means its "all-in-one, high-performance" framing is aspirational, not battle-tested.
- The Local REST API v4 "you no longer need third-party MCP servers" claim is true for feature coverage but omits that it offers no path-scoped writes or schema enforcement.

## Recommendations

**Stage 1 — Stand up the substrate (now).** Deploy the vault PVC read-write and a **headless Obsidian + Local REST API** workload in the cluster. Use `shanehull/obsidian-remote` as the reference blueprint but, given its maturity and GPL-3.0/build-from-source status, prefer building your own image from the well-worn `linuxserver/obsidian` (or `sytone/obsidian-remote`, 2.6k★, MIT) base with the Local REST API plugin pre-installed and auto-trusted. Pin the image, manage via Flux.

**Stage 2 — Deploy `cyanheads/obsidian-mcp-server` as the MCP workload (now).** Pull the official `ghcr.io/cyanheads/obsidian-mcp-server` image (`latest`→3.2.9; pin the digest not the moving tag), run in HTTP transport mode behind Ingress, point `OBSIDIAN_BASE_URL` at the in-cluster REST API service, set `MCP_AUTH_MODE=jwt` (or `oauth`) since the listener binds `0.0.0.0`. Configure `OBSIDIAN_WRITE_PATHS=00-inbox/,40-journal/,_ops/agent/` to enforce the agent-writable-zone boundary at the server, leaving `10-areas/finance/` and other curated areas out of write scope. Satisfies criteria 1,2 (via headless substrate),3,4,5,7,8,10 and gives implicit serialization for 9.

**Stage 3 — Wire LiteLLM virtual keys (now).** Register the cyanheads MCP server in LiteLLM's gateway. Per-client virtual keys: n8n → read/search tools only; Claude Code and OpenClaw → those plus append/patch/frontmatter/tags. Keep `obsidian_delete_note` and `obsidian_execute_command` off agent keys (the latter is opt-in via `OBSIDIAN_ENABLE_COMMANDS` anyway).

**Stage 4 — Add schema enforcement (next).** No server enforces the `source:`/schema contract: add an init/sidecar validator that rejects or quarantines writes to `00-inbox/` lacking required frontmatter, or a Flux-managed controller that validates and promotes. Encode the `source:` enum and required keys there. Treat system-prompt discipline as a first line, never the only line.

**Stage 5 — Harden concurrency & recovery (next).** Confirm all clients only ever write through the single MCP→single headless-Obsidian path (never a second filesystem writer). Prefer append/patch over overwrite in agent prompts. Rely on git detect-and-escalate for conflict recovery. Add liveness/readiness probes on the REST API and MCP endpoints so Flux restarts a wedged Electron process.

**Benchmarks that would change this recommendation:**
- If Electron/Chromium in-cluster is intolerable (memory, flakiness), **flip to `bitbonsai/mcpvault`** and accept: (a) an HTTP-transport shim in front of it or per-client sidecars since it's stdio-only, and (b) hard single-writer discipline, because its concurrent-write race is real. Gains "no Electron," loses path-scoped writes and native HTTP.
- If `shanehull/obsidian-remote` gains adoption, releases a pull-able image, and matures, it becomes the turnkey Stage-1+2 combo, collapsing two workloads into one.
- If Obsidian ships/expands its official headless client for automation, revisit whether the REST-bridge stack can be simplified.

## Caveats

- **"Requires the Obsidian app" is reframed, not eliminated.** The recommendation depends on running headless Obsidian in the cluster: a heavier runtime (Electron/Chromium, ~300–500 MB RAM) and a moving part that can wedge. Budget for probes, restarts, and image maintenance as Obsidian updates. If unacceptable, the filesystem-native path (mcpvault) is the fallback despite weaker features.
- **Version/tag volatility:** cyanheads releases frequently (3.2.8→3.2.9 within ~10 days; README badge lagged at 3.1.9). Pin a digest in Flux rather than tracking `latest`.
- **shanehull/obsidian-remote maturity risk:** 1 star, single maintainer, GPL-3.0, build-from-source, ~6-week-old latest release. Blueprint, not production dependency, unless prepared to own the build.
- **mcpvault concurrency race is documented, not theoretical** (issue #109, v0.11.0). Only choose it with strict single-writer enforcement.
- **Schema enforcement and concurrency safety are gaps you own, not gaps a server closes for you.** The chosen server gets path-scoping, transport, and safe editing primitives; the `source:`/schema contract and true write-safety come from the sidecar/validator, LiteLLM tool-filtering, write-path confinement, and git authority working together.
- **Star counts, versions, dates reflect repositories as observed late July 2026** and will drift; re-verify at deploy time.
