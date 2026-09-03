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
nothing to exclude; and no mount layering. The design also has the committer run
*around every batch run*, not only on schedule — that trigger waits on the queue existing and is
deliberately deferred; keeping a run a single idempotent command means only the invoker changes
when it arrives. The workload is a **CronJob**
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
[ADR-0028](./0028-settings-baseline-seed.md)); an `emptyDir` for the git dir instead of the
committer's own small PVC — correctness would be identical, since the directory is derivable
either way, but every scheduled run would then start by cloning the vault's entire history:
history grows without bound while a 15-minute delta stays tiny, so the PVC exists to make the
fetch incremental and for no other reason; and `--filter=blob:none` partial clones, which would
have made the `emptyDir` cheap — rejected because a partial clone fetches missing blobs lazily,
mid-command, so any git operation touching an unfetched blob becomes a network call that can fail
halfway through a run: a subtler dependency on the remote than one clean clone up front, taken on
for a git feature nothing else in the fleet uses.

## Consequences

The git dir is a **derivable cache, never durable state**: cloned from origin when missing, never
`git init` (an init after a lost PVC would re-root history and fork the remotes), with every
per-clone setting (`fileMode`, `skip-worktree`, the ignore rule, `core.quotePath`) reapplied
idempotently on every run. "It's a PVC, therefore it's state" is exactly the inference a reader
will draw, and it is wrong — everything on the volume can be rebuilt from the remote, and nothing
may be built that depends on it surviving. Every run pushes, even when nothing changed locally: if
a run commits and its push fails, that commit exists only in the git dir, and it is the *next*
run's unconditional push that delivers it (were the git dir lost too, the content still on the
vault volume means the next run simply takes an equivalent commit). Logic that skips the push
because the new run found nothing to commit would leave the remote silently lagging until the next
real content edit. Reads can fail transiently under NFS and are treated as expected and
retryable, not exceptional. Before this shipped, durability was luck — two GUI mishaps were
recoverable only because a manual seed happened to have run an hour earlier; the committer is what
turned "git has it" from coincidence into guarantee.

The one-way-ness cuts the other way too, and it has already bitten: **the repo the committer
pushes is derived, never an editing surface.** A change merged there by pull request touches
nothing on the volume, and the next sync re-records the volume's copy — silently reverting the
merge. Observed once: a schema ruling applied as a vault-repo pull request was reverted by the
sync two days later, leaving the repo carrying the rejected text again with no error anywhere.
Vault content changes only on the volume, through the doors (or the declared exceptions), and
reaches git exclusively as sync commits.
