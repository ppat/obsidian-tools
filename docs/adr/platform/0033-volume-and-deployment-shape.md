# 0033. Volume and deployment shape: RWX, one replica, `Recreate`, the vault in a subdirectory — and the manifest decisions riding with them

**Status:** Accepted ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted), [O2](../../../USE_CASES.md#o2--survives-its-failure-modes)

## Context

Three processes must mount one volume without co-scheduling onto one node
([ADR-0001](../write-model/0001-single-writer-one-door.md)); the single-writer property must
survive deploys; and the first GUI session produced a hard fact about mount roots.

## Decision

- **ReadWriteMany volume** (NFS-backed, exported by the storage layer's share-manager,
  soft-mounted): RWX is what lets three pods mount concurrently without node co-scheduling.
  Soft-mount semantics mean I/O errors instead of hangs — writes can fail under ordinary NFS
  trouble, so the committer and lint treat failure as expected and retryable.
- **One replica, `strategy: Recreate`** on the editor's Deployment: a rolling update would run two
  editors against the RWX volume on every deploy. **The enforcement is the Deployment's shape, not
  the storage layer** — RWX permits multi-attach by definition, and a live incident showed a second
  pod's volume attach *succeeding* two seconds after that pod was marked for deletion; no violation
  occurred only because the second container never started. The guarantee is timing-narrowed, not
  closed; rapid pod-template churn reopens it, and the recovery drill
  ([D3](../../../ROADMAP.md#group-d--operability)) exercises exactly that.
- **The vault lives in a subdirectory of the mount, never at its root.** The mount root carries the
  filesystem's own root-owned `lost+found`, and the editor establishes a watcher on every directory
  rather than degrading around one it cannot watch — so a vault at the root fails to open outright,
  while the pod stays Ready and the REST API keeps serving (the probe hits a different code path).
  Deleting the directory is not a fix: the CSI mounter's fsck recreates it on the next attach.
- **Manifest decisions riding with the shape:** the application's own state lives on an `emptyDir`,
  deliberately ephemeral — the entrypoint re-establishes vault registration and plugin trust
  idempotently on every boot, so the content volume holds content only (and is named for that:
  `vault-data`). Backup group membership is wired knowing the storage layer's recurring-job label
  **replaces** a storage class's group membership rather than adding to it — an override, not a
  union.

## Alternatives considered

RWO plus co-scheduling (couples three workloads' placement); enforcing single-writer at the storage
layer (not expressible without giving up the three-mount model); vault at the mount root (observed
failing); app state on the volume (braids content with per-instance state the seed model keeps
out — [ADR-0028](../replication/0028-settings-baseline-seed.md)).

## Consequences

inotify over NFS only observes same-client writes — ordinarily a gap, here structurally irrelevant
*because* no cross-client write exists; anything that ever breaks the single-writer invariant also
silently breaks indexing. The Ready-while-broken incident this shape surfaced is the standing
argument for the independent vault-loaded signal
([ADR-0037](../operability/0037-vault-loaded-exporter.md)).
