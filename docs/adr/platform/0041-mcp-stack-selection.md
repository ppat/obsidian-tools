# 0041. The MCP stack: Local REST API + the path-scoping MCP server + the gateway — and the fork it resolved

**Status:** Accepted — where tool grants are enforced superseded by [ADR-0059](../write-model/0059-one-holder-per-credential-access-record.md) (Proposed), marked where it stands ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted)

## Context

The server landscape splits into two families: **REST-bridge** servers (proxy to the Local REST API
plugin running inside Obsidian — most capable, but "requires the app") and **filesystem-native**
servers (no app needed — but weaker on path scoping, transport, and with a documented concurrent
same-path write race in the strongest candidate). For a cluster where no device is guaranteed
awake, that fork was the pivot of the whole selection.

## Decision

Dissolve the fork by running **Obsidian headless in the cluster** ([ADR-0032](./0032-obsidian-image.md)),
then take the strongest maintained REST-bridge server — the only candidate natively shipping
**path-scoped write permissions**, streamable HTTP transport, an official multi-arch image, and
surgical/additive editing primitives — digest-pinned, one deployment per path scope
([ADR-0003](../write-model/0003-two-instances-two-handles.md)). The gateway (LiteLLM) fronts it
with per-client virtual keys and per-tool allow/deny — its filtering is server-level and confirmed
**not** path-scoped, which is exactly why both layers exist: the gateway decides which tools a
client sees, the server's path scope decides where a write may land. `obsidian_execute_command`
sits on no agent key; delete is withheld at the gateway
([ADR-0004](../write-model/0004-delete-withheld-relocation-order.md)). *Where tool grants are enforced superseded by [ADR-0059](../write-model/0059-one-holder-per-credential-access-record.md), pending its ratification.*

## Alternatives considered

- **The filesystem-native candidate** — stdio-only (an HTTP shim would be ours to build), no path
  allow-list, and a documented, still-open concurrent-write race. **It remains the named fallback**
  if Electron-in-cluster proves intolerable, at the cost of rebuilding the path boundary by hand.
- **The all-in-one headless bundle** — no GUI (unusable for the settings lock) and a competing
  bundled MCP server that would forfeit path scoping; set aside even after it published a pullable
  image.
- **The REST plugin's own built-in MCP endpoint** — no path scoping at all, and undisableable;
  it is the surface [ADR-0006](../write-model/0006-networkpolicy-sole-control.md) exists to close.
- **The in-app JSON-Schema-validating server** — validation in the Electron renderer, changing the
  tool surface; schema enforcement went to the admission validator instead
  ([ADR-0007](../write-model/0007-validation-placement.md)).
- An archived formerly-popular server — reference only; and the research's headline star-counts
  were found unreliable more than once, which is why every load-bearing claim here was re-verified
  against sources at selection time.

## Consequences

"Requires the app" is reframed, not eliminated: an Electron process that can wedge, budgeted for
with probes, digest pins, and the wedge drill. Two selection-time caveats stand as permanent
posture: **schema enforcement and concurrency safety are gaps the platform owns, not gaps any
server closes** — they live in the validator and the single-writer funnel. Revisit trigger, named:
the vendor now ships an official headless CLI for its paid sync; if the free stack sours, that and
the filesystem-native fallback are the two doors.
