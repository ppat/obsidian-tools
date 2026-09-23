# 0065. `local-replicator` converges on the deployed version by itself, verifies what it installs, and proves its own schedule

**Status:** Proposed ·
**Pillar:** [The vault system does its own job](../../../DESIGN.md#the-vault-system-does-its-own-job) ·
**Serves:** [O2](../../../USE_CASES.md#o2--survives-its-failure-modes) ·
**Unit:** [D8](../../../ROADMAP.md#group-d--operability) ·
**Ticket:** [ot#66](https://github.com/ppat/obsidian-tools/issues/66)

## Context

`local-replicator` runs under launchd on the operator's Mac, and its upgrades are installed by
hand. An upgrade also fails in a way that looks exactly like health: the install path is scoped to
the version while the launchd plist needs an absolute path, so an upgrade silently strands the
schedule ([ot#66](https://github.com/ppat/obsidian-tools/issues/66)).

Three owner rulings bound the answer: the owner does no manual steps; nothing reaches production
piecemeal; and each increment's production change goes through one merge, the clusters
repository's pin. The Mac is outside the cluster, and nothing in the cluster can reach it.

## Decision

**The Mac converges on the version the cluster runs, not on the latest release.**

- **It learns the deployed version from history it already pulls, trusting only the committer.**
  The committer runs the deployed version and records it as a trailer on its sync commits. When its
  own version changes, it makes a commit carrying the new trailer even if the vault has not changed,
  so a quiet vault does not hold the Mac back. The committer **signs** its commits with a key of its
  own. `local-replicator` trusts a trailer only on a commit signed by a committer key that its
  **trust list** names, and ignores every other commit. Other holders of push rights to the vault
  repository cannot move the Mac up or down. So the Mac moves with the same owner-merged pin as the
  cluster, lagging only by when it next wakes — the lag the design already accepts for device
  freshness.
- **Its trust is rooted in the release workflow, and the committer's key is a delegate.** The trust
  list — the committer keys currently trusted and every key ever revoked — ships inside each
  attested release artifact. So only this repository's release workflow can change it, and a stolen
  committer key cannot. On every cycle, independently of any trailer, the Mac reads the trust list
  of the newest attested release of this project, without installing that release. It **adds** that
  list's trusted keys to the set it trusts, and adds its revocations to a revocation set it keeps
  across versions and never shrinks. A key stays trusted until a release revokes it, so the key the
  deployed committer still signs with keeps counting through an ordinary rotation. Trust data is the
  one input the Mac reads ahead of the deploy: a revocation that waited on the pin merge would keep
  an exposed key's window open for as long as the merge took. Rotating the committer's key, ordinary or emergency, is therefore one
  agent-authored change the owner merges and a release carries. In an emergency, that release
  revokes the exposed key, and the committer switches to the new key once it is deployed.
- **It installs only a verified artifact.** It verifies the release artifact for that version
  against a provenance attestation bound to this repository's release workflow, which proves who
  built it and not merely that the bytes are intact. It installs at a **version-independent path**
  that the plist names.
- **It proves its own schedule.** After installing, it confirms that launchd's next run executed the
  new version and completed a cycle. If not, it rolls back to the previous version.

**These are requirements on the implementation, not present facts.** Today the release workflow
publishes no artifact and no attestation, the Mac installs from a git tag, and the committer does not
sign its commits [measured]. [D8](../../../ROADMAP.md#group-d--operability)
adds all of them: the artifact, its attestation and the trust list inside it, to the release
pipeline; and the committer's signing.

**The first install is the one hand act.** A launchd job on the owner's own Mac cannot be put
there by anything else. That happens once, and the trust list arrives inside the attested artifact
that install verifies. Every later version, and every later key, arrives by itself.

## Alternatives considered

- **Hand upgrades** — a manual step on every release, and it fails silently.
- **Upgrading to every release as it appears** — a standing staggered rollout: the drift publisher
  on the Mac and its consumer in the cluster would run different versions by construction.
- **A digest published beside the artifact** — proves the bytes are intact, not where they came
  from.
- **Rotating by an announcement the old key signs** — works for an ordinary rotation, but an exposed
  key can vouch for nothing. An emergency would then need the key re-pinned on the Mac by hand, and
  until then the Mac would keep trusting the exposed key.
- **Upgrades pushed from the cluster** — nothing in the cluster can reach the Mac.

## Consequences

- **An upgrade cannot look healthy while broken**, and the Mac never runs ahead of the cluster —
  with one intended exception. If the cluster is rolled back past an emergency rotation, the
  rolled-back committer signs with a revoked key, and the Mac holds its version until the committer
  signs with a key it trusts. Holding still is the intended behaviour, so a rollback onto an exposed
  key can never steer the Mac.
- **The committer gains two responsibilities:** a version trailer on its own commits, and signing
  them. It still authors no content.
- **A revocation reaches a Mac even while the exposed key is steering it.** The trust check reads the
  newest attested release directly, not through trailers, so trailers signed by the exposed key
  cannot hide the revocation. Once the Mac reads it, trailers signed by the revoked key are ignored
  for good. The revocation set persists across versions, so a later move to an older release cannot
  restore trust in that key.
- **The residue is a bounded window.** From the key's exposure until a release revoking it is
  published and the Mac next wakes, whoever holds the exposed key can steer the Mac among genuine,
  attested releases, older ones included — a downgrade to a genuine build of this project, never to
  anything else. The window closes with no hand act. Nothing in the cluster sees the Mac during it.
- **The cluster still cannot see the Mac.** A Mac that is asleep, switched off, or rolled back looks
  the same from the cluster. Device freshness has no in-cluster observer, and that is a stated
  residue ([ADR-0064](../operability/0064-operational-conditions-have-resolvers.md)).
