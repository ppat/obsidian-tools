# 0032. A purpose-built headless Obsidian image — not derived from any community base

**Status:** Accepted ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted), [S4](../../../USE_CASES.md#s4--retrievable)

## Context

The research recommendation was to derive from a community base with the REST API plugin
"pre-installed and auto-trusted". Checked against the actual Dockerfiles, that premise was false:
the leading base downloads and extracts the application and stops — no plugins, no vault, nothing
to inherit — and its GUI ships passwordless sudo inside the container that would hold the
read-write mount of the authoritative vault. The all-in-one alternative exposes no GUI at all
(unusable for the settings lock) and bundles its own MCP server, which would displace the chosen
one and forfeit path-scoped writes.

## Decision

Build our own: Debian slim, multi-stage, digest-pinned; Obsidian and the REST API plugin baked in
as a seed; Xvfb; `x11vnc` installed but dormant ([ADR-0002](../write-model/0002-gui-exception-dormant-vnc.md));
non-root throughout (uid 1000 from boot, `tini` as PID 1, no supervisor, no sudo — no
root-then-drop pass to narrow later, no `PUID`/`PGID` because there is no handoff to align).
"Auto-trusted" required real engineering, not a file drop: the Restricted Mode flag lives in the
Electron renderer's localStorage, keyed per vault install, so trust is established at boot over
Chrome DevTools Protocol — the same runtime API the UI's own toggle calls. **The app version is
pinned to stable** (which is what forced two plugins out of the set —
[ADR-0017](../content-model/0017-plugin-set-tasks-dataview.md)); verified safe against silent
drift: the vendor's release repo publishes no pre-releases, so automation cannot walk the pin onto
a beta without a deliberate act. Everything is digest-pinned — the REST plugin's own history
(a point release that briefly required a pre-release app build) is the standing illustration of
why — and an app or plugin upgrade is canaried in the headless instance before any device sees it,
since a silently-empty view is the failure shape upgrades produce.

## Alternatives considered

Deriving from the community bases (false premise, sudo-in-GUI, or no GUI + competing MCP server);
the filesystem-native MCP fallback without Electron
([ADR-0041](./0041-mcp-stack-selection.md) carries that fork); accepting the ~2GB/300–500MB
Electron runtime figures — accepted knowingly, with the fallback named rather than hedged.

## Consequences

We own the dependency set the community images would have documented for us. A green image build
proves nothing about the runtime path (Xvfb, CDP attach, REST binding) — a recorded gap that shaped
the CI-strategy question ([apps#3440](https://github.com/ppat/homelab-ops-kubernetes-apps/issues/3440)).
The GUI exists at zero standing cost, attached on demand to the display the running process already
owns.
