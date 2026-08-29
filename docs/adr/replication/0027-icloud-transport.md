# 0027. Devices are fed via iCloud from a pull-only git clone; git metadata never enters iCloud; Obsidian Sync rejected

**Status:** Accepted ·
**Serves:** [S4](../../../USE_CASES.md#s4--retrievable), [R1](../../../USE_CASES.md#axis-3--readers-connected)

## Context

iOS Obsidian cannot open a vault outside its own app container — no source claims otherwise, and
the vendor's docs list every third-party sync tool as unsupported on iOS. The vendor's own free,
first-party recommendation is iCloud at `iCloud Drive/Obsidian/<Vault Name>`. So the device-side
vault *is* the iCloud copy, opened by both the Mac and the phone, and the question is only how
content reaches that directory.

## Decision

A **pull-only git clone** on the Mac feeds the iCloud directory by rsync of the working tree —
never `.git`. Git wins as transport because the vault already needs git for history (replication
rides an existing concern), it works offline and transfers incrementally, and it is the only option
supplying a reproducible byte-exact baseline — exactly what drift detection requires
([ADR-0025](./0025-replication-cycle.md)). Git metadata stays outside iCloud because iCloud
resolves conflicts by *renaming*: applied to a ref that produces a file git cannot parse,
corrupting the repository; keeping only the working tree in iCloud removes that mechanism entirely.
"Optimize Mac Storage" is off (eliminates the dataless-stub eviction class); the two residual
iCloud behaviours (silent POSIX reverts, content-file conflict renames) are cheap *because* the
clone is pull-only — nothing originates there, so repair is always a re-checkout.

**Obsidian Sync — the paid first-party alternative — was rejected** on three grounds: dependence on
a corporation with no recourse if the product degrades or dies; ongoing cost for what the
architecture gets free; and third-party custody of vault content. iCloud is not exempt from the
first and third in the abstract, but it is the platform the devices already run on, costs nothing
further, and is not a business built on monetising the content passing through it.

## Alternatives considered

rsync from the NAS (no baseline, hence no divergence detection); SMB/NFS mounts (fail off-network;
likely index-thrash; unsupported on iOS anyway); the community Syncthing-into-sandbox tool (noted,
not needed — iCloud closes the gap free); Obsidian Sync (above; its official headless CLI, which
ships a pull-only mode, is the named trigger to revisit if the free path sours).

## Consequences

Device freshness is gated on the Mac waking — only the Mac can write its own iCloud directory, and
nothing in the cluster can push to iOS. That is accepted *because* the conversational surface
([R5](../../../USE_CASES.md#axis-3--readers-connected)) is the primary, always-fresh phone reader;
native reading is the rich, laggier complement. The whole tree is carried (markdown-only keeps the
payload small — [ADR-0014](../content-model/0014-markdown-only-vault.md)), so no curated-subset
replication is needed.
