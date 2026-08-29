# 0030. The committer: a CronJob with a detached git dir, structurally incapable of writing content

**Status:** Accepted ·
**Serves:** [S4](../../../USE_CASES.md#s4--retrievable), [O2](../../../USE_CASES.md#o2--survives-its-failure-modes) ·
**Ticket:** [ot#3](https://github.com/ppat/obsidian-tools/issues/3) (closed; the shipped shape)

## Context

The committer must turn the volume into history without ever being able to author content — the
requirement is *structural incapability*, not discipline. The naive reading of "mounts content
read-only plus `.git/`" does not work as stated: a mount point cannot be created on a read-only
mount, so `.git` would need pre-provisioning on the one volume the committer must not write.

## Decision

Git's own separation of concerns, used natively: `git --git-dir=<own PVC> --work-tree=<vault,
read-only>`. The vault PVC mounts read-only (content unwritable by construction); a small
committer-owned PVC holds the repository. Three properties ride free: git's index and lock files
stay off the soft-mounted NFS volume (the index is the file most sensitive to a failed write); the
vault never grows a `.git` at all — so the editor never watches one and the device rsync has
nothing to exclude; and no mount layering. The workload is a **CronJob**
(`concurrencyPolicy: Forbid`), not a long-lived Deployment — Jobs do not roll and there is nothing
to evict between runs, which *dissolved* two open manifest questions (rollout strategy, eviction
annotation) rather than answering them, and narrows the RWX multi-attach exposure to the moments a
commit is actually being taken. `core.fileMode=false` is set on real grounds: the kubelet's
fsGroup mechanism re-applies group permissions at mount time, so mode bits can differ from what git
recorded regardless of what wrote the files.

## Alternatives considered

A `.git` on the vault volume (unprovisionable read-only, and a second thing the editor watches);
a long-lived Deployment (a sleep loop reimplemented in-process, two reopened manifest questions,
and a third permanent volume attachment); `.gitignore` in the work tree for exclusions (unwritable
— the vault is read-only — hence the git-dir-local rule in
[ADR-0028](./0028-settings-baseline-seed.md)).

## Consequences

The git dir is a **derivable cache, never durable state**: cloned from origin when missing, never
`git init` (an init after a lost PVC would re-root history and fork the remotes), with every
per-clone setting (`fileMode`, `skip-worktree`, the ignore rule, `core.quotePath`) reapplied
idempotently on every run. Reads can fail transiently under NFS and are treated as expected and
retryable, not exceptional. Before this shipped, durability was luck — two GUI mishaps were
recoverable only because a manual seed happened to have run an hour earlier; the committer is what
turned "git has it" from coincidence into guarantee.
