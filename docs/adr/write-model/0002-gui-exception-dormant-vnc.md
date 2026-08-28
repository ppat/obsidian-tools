# 0002. The GUI exception: one pod, dormant VNC, port-forward only

**Status:** Accepted ·
**Pillar:** [Exceptions are declared, never discovered](../../../DESIGN2.md#exceptions-are-declared-never-discovered)

## Context

Configuring Obsidian itself — the settings lock, plugin trust, repair after an incident — requires
the application's own GUI; there is no sanctioned file-level alternative (hand-crafting
`.obsidian/*.json` is undocumented and easy to get subtly wrong on exactly the settings that must be
right early). So a human-usable GUI has to exist for the one in-cluster instance, and any GUI is by
nature an ungated write path: no MCP, no validation, no provenance stamping.

## Decision

The GUI ships inside the one image and one pod: `x11vnc` installed but **dormant**, attached on
demand to the Xvfb display the running Obsidian process already owns. It is reachable only by
`kubectl port-forward` from the operator's machine — no Ingress, no standing exposed surface. Its
use is bounded by purpose, not by mechanism: configuration and repair only. A write made there is
observed after the fact by the lint pass, which is the only component that ever sees one; a rising
count of such writes is treated as evidence the write model is wrong, never as a discipline failure.

## Alternatives considered

- **A second, GUI-bearing deployment against the same volume** — rejected: it gives an operator an
  accidental way to break the single-writer invariant, two Obsidian processes holding the vault
  read-write at once.
- **Ingress behind SSO** — considered only on the mistaken belief that `kubectl` authentication was
  unavailable from the operator's laptop; port-forward wins on having no standing surface and
  requiring a deliberate act from one machine.
- **No GUI at all** — not available: one of the rejected base images was set aside partly *because*
  it exposes no GUI (see [ADR-0032](../platform/0032-obsidian-image.md)).

## Consequences

- The headline invariant is stated with this exception at the top of the design rather than
  discovered later; the controls on it — documentation and discipline — are the weakest in the
  system, and that is stated plainly.
- The lint pass carries the detection duty; the pre-mortem tripwires
  ([apps#3448](https://github.com/ppat/homelab-ops-kubernetes-apps/issues/3448)) carry the
  rising-count signal.
- An accidental GUI edit has already destroyed content once (a stray click deleted a file's header,
  restored from git in seconds) — the incident that motivated server-side drift classification
  ([ADR-0008](./0008-drift-classification-separate-authority.md)) even though a GUI write never
  reaches that classifier: GUI writes land on the authoritative volume and flow outward, so they
  never appear as device drift.
