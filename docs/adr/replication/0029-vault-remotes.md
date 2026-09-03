# 0029. The vault's remotes: GitHub primary, a NAS bare repo as independence insurance — and the separate publish job deleted

**Status:** Superseded — **Superseded by:** [ADR-0044](./0044-vault-remote-github.md) ·
**Serves:** [S4](../../../USE_CASES.md#s4--retrievable), [O2](../../../USE_CASES.md#o2--survives-its-failure-modes)

## Context

A cluster-reachable git remote necessarily exists (the cluster is GitOps-managed); whether personal
vault content belongs on a hosted service was the policy question. Separately, an earlier design
had a scheduled `publish` job rsyncing a working tree to the NAS — a second component with its own
schedule and failure modes, for a property expressible more cheaply.

## Decision

The committer pushes **two remotes**: the private `ppat/obsidian-vault` repository on GitHub
(exists already, private already, no new infrastructure; the Mac clone pulls from it), and a **bare
repository on the NAS** as independence insurance — a plain-markdown copy a human can clone and
grep with zero tooling, no cluster, no third party. The separate publish job is **deleted, not
folded**: what the NAS copy protects against never depended on *how* it got there, and a second
push target on the existing committer is one fewer scheduled component that can fail silently.

**A tension is recorded rather than resolved:** the paid-sync rejection
([ADR-0027](./0027-icloud-transport.md)) leans partly on not handing vault content to a third
party — and the GitHub remote holds the finance area. Either that reasoning extends to GitHub too,
or it is narrower than written; the decision was made with the tension stated. **Named fallback if
it bites:** make the NAS primary and GitHub secondary, or exclude the finance overlay's area from
the pushed tree.

## Alternatives considered

A bare repo on the NAS alone (new infrastructure to stand up and maintain, and it would gate device
freshness on the Mac being on-network rather than merely awake); keeping the rsync publish job
(second failure surface for no added property).

## Consequences

Recovery has two independent grounds (either remote, plus volume snapshots —
[D3](../../../ROADMAP.md#group-d--operability)); no single service is load-bearing. The committer's
first push is also what captures the settings baseline
([ADR-0028](./0028-settings-baseline-seed.md)), which is why that capture could not happen at the
settings lock itself.
