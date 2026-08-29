# 0024. Captured drift rides the drift stream — superseding MinIO, whose siting reasoning survives

**Status:** Accepted (supersedes an earlier MinIO decision; the gotcha it carried is retained) ·
**Serves:** [W6](../../../USE_CASES.md#axis-2--writers-connected) ·
**Ticket:** [ot#4](https://github.com/ppat/obsidian-tools/issues/4)

## Context

Captured device edits need durable storage between capture and dispatch. When the drift channel was
the only component needing that, MinIO on the second cluster was chosen — already running, already
ingressed, no new component. The work-queue redesign then stood up NATS JetStream for the batch and
promotion streams anyway.

## Decision

A third JetStream stream carries drift. Notes are kilobytes and the vault is markdown-only
([ADR-0014](../content-model/0014-markdown-only-vault.md)), so a captured edit fits in a message
payload — one durability mechanism instead of two. The drainer removes a spool entry **only after
JetStream acknowledges the publish**; a publish without an ack is not a durable handoff, and
treating one as success is the only remaining way to lose a human's edit on this path.

**The siting reasoning from the superseded decision carries forward unchanged, and is the gotcha
worth keeping:** the durable copy must be **cluster-side, never on the device**. If the device
fails or is lost it has already forfeited the divergence baseline; a safety copy on the same device
compounds one failure into two with the same cause. Device loss and capture loss must stay
independent events — true whatever technology holds the copy.

## Alternatives considered

MinIO (superseded as above); a purpose-built endpoint fronted by Tailscale (a new component to
build, secure and maintain for no gain); a Synology share over SMB/NFS (weakest auth story, and
awkward against the exactly-three-mounts discipline).

## Consequences

The Mac holds one manually-minted, manually-rotated `.creds` file — the one producer credential the
cluster's secret machinery cannot reach, custody stated in the design rather than left implicit. A
failed NATS publish pauses only the drain; capture's safety property was already satisfied by the
local spool write ([ADR-0025](../replication/0025-replication-cycle.md)).
