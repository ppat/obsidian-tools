# 0044. The vault has one git remote: GitHub

**Status:** Accepted (supersedes [ADR-0029](./0029-vault-remotes.md)) ·
**Serves:** [S4](../../../USE_CASES.md#s4--retrievable), [O2](../../../USE_CASES.md#o2--survives-its-failure-modes) ·
**Ticket:** [ot#104](https://github.com/ppat/obsidian-tools/issues/104)

## Context

A cluster-reachable git remote necessarily exists (the cluster is GitOps-managed). Durability and
recovery stand on two independent grounds: the vault volume's storage-layer snapshots and
off-cluster backups, and the git remote's full history with rollback. A git clone is complete and
portable — the entire vault, content and history, is obtainable from the private remote with
nothing but git, which exists on essentially any machine — so content on a hosted remote is not
hostage to the host.

## Decision

The committer pushes exactly one remote: the private `ppat/obsidian-vault` repository on GitHub —
the vault's history and its rollback ground.

The only vault copies outside the cluster are the device-facing replicas, and they serve the
devices: the iCloud vault directory is the vault native Obsidian on macOS and iOS actually opens
(Plane B), fed by the Mac's pull-only clone. Their only upstream consumer is the replication cycle
— its baseline, its capture, and drift published to the work queue. No recovery or tooling path
treats either as a source.

A tension is recorded rather than resolved: the paid-sync rejection
([ADR-0027](./0027-icloud-transport.md)) leans partly on not handing vault content to a third
party — and the GitHub remote holds the finance area. Either that reasoning extends to GitHub too,
or it is narrower than written. Named fallback if it bites: exclude the finance overlay's area
from the pushed tree.

## Alternatives considered

Additional remotes or off-cluster copies, on any host — more copies add surface (push targets,
credentials, host keys, state that can silently diverge) without adding a property: clone
portability already makes one remote sufficient for independence, and the volume's snapshots and
backups already carry durability.

## Consequences

Recovery has two grounds: a curated note must be restorable from a volume snapshot and,
independently, from the GitHub remote
([O2](../../../USE_CASES.md#o2--survives-its-failure-modes)). No single service is load-bearing.
