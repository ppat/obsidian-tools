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

Build our own: Debian slim, multi-stage, digest-pinned; Obsidian and a fixed, minimally selective
plugin set baked in as a seed; Xvfb;
`x11vnc` installed but dormant ([ADR-0002](../write-model/0002-gui-exception-dormant-vnc.md));
non-root throughout (uid 1000 from boot, `tini` as PID 1, no supervisor, no sudo — no
root-then-drop pass to narrow later, no `PUID`/`PGID` because there is no handoff to align).
The plugin set is the REST API plugin by necessity (it cannot be installed through a gateway that
does not exist yet, and serves only the headless instance) and
[ADR-0017](../content-model/0017-plugin-set-tasks-dataview.md)'s Tasks and Dataview by choice —
Renovate-tracked versions, disaster recovery reproducing the set, one less step on an already-long
manual checklist — cut to what the headless instance needs while remaining a useful first seed for
the device apps; a device may layer further plugins locally for the human consumption path, its
`.obsidian/` being its own after seeding
([ADR-0028](../replication/0028-settings-baseline-seed.md)).
"Auto-trusted" required real engineering, not a file drop: the Restricted Mode flag lives in the
Electron renderer's localStorage, keyed per vault install, so trust is established at boot over
Chrome DevTools Protocol — the same runtime API the UI's own toggle calls. **The app version is
pinned to stable** (which is what forced two plugins out of the set —
[ADR-0017](../content-model/0017-plugin-set-tasks-dataview.md)); verified safe against silent
drift: the vendor's release repo publishes no pre-releases, so automation cannot walk the pin onto
a beta without a deliberate act. Everything is digest-pinned — the REST plugin's own history
(a point release that briefly required a pre-release app build) is the standing illustration of
why — and an app or plugin upgrade is canaried in the headless instance before any device sees it,
since a silently-empty view is the failure shape upgrades produce. The tag adds nothing to that
pin: it tracks the upstream Obsidian version 1:1 — the shape Renovate's regex manager can follow —
and is reused on every rebuild, so the digest is the only identity a build has. A rebuild has
already exercised the distinction: baking the plugins republished the same tag under a new digest
while the consuming module still pinned the old one, and deploying that would have brought the
image up without Tasks and Dataview, silently, since plugin enablement fires only on the first
seed of each plugin directory (Renovate's digest pinning closes such drift only on its own
schedule, possibly after a deploy). Immutable per-build tags exist and are deliberately unused:
not semver, they would need custom Renovate versioning that fights the regex manager, for no gain
the digest does not already give.

## Alternatives considered

Deriving from the community bases (false premise, sudo-in-GUI, or no GUI + competing MCP server);
the filesystem-native MCP fallback without Electron
([ADR-0041](./0041-mcp-stack-selection.md) carries that fork); accepting the ~2GB/300–500MB
Electron runtime figures — accepted knowingly, with the fallback named rather than hedged.

## Consequences

We own the dependency set the community images would have documented for us. Baking carries a
two-sided entrypoint discipline: plugin code is force-copied on every start — the files are build
artifacts behind a Renovate pin, and copy-if-absent would strand a version bump on any vault that
had already booted — while enablement is ensured every start only for the load-bearing REST
plugin; the community plugins enable on first seed alone, so a deliberate disable at the GUI is
never silently reverted by a restart. A green image build
proves nothing about the runtime path (Xvfb, CDP attach, REST binding) — a recorded gap that shaped
the CI-strategy question ([apps#3440](https://github.com/ppat/homelab-ops-kubernetes-apps/issues/3440)).
The GUI exists at zero standing cost, attached on demand to the display the running process already
owns.
