# CLAUDE.md

Orientation for AI coding agents (and humans) starting cold on this repository: what is where, what
is true today, and the disciplines that prevent a newcomer's mistakes. This file points at
documents; it does not duplicate them — duplication is how two documents on this project once
disagreed for weeks.

## Start here — the document map

| Read | For |
| --- | --- |
| [`USE_CASES.md`](./USE_CASES.md) | What the platform is for: the outcomes on four axes, each with a falsifiable acceptance criterion |
| [`DESIGN.md`](./DESIGN.md) | The pillars and invariants — and the **Glossary**, the single home for vocabulary. If a term needs defining, it gets defined there, never locally |
| [`ROADMAP.md`](./ROADMAP.md) | All the work in one place: delivered, remaining, the delivery posture and value path, dependencies, open decisions. Supersedes every older phase numbering |
| [`docs/adr/README.md`](./docs/adr/README.md) | The decision records: index, record format, statuses, and the granularity rule (one decision per record, cut by the re-argue test) |
| [`docs/VERIFICATIONS.md`](./docs/VERIFICATIONS.md) | Every control's proving injection and answerable-by-doing check, past and pending |
| [`docs/README.md`](./docs/README.md) | The research canon's authority chain — read CANON-1/CANON-2 only through it |
| `docs/local-replicator.md` · `docs/settings-lock.md` · `docs/gui-access.md` | Operator runbooks: the Mac component, the one-time settings lock, reaching the GUI |

Design questions resolve there, in that order: outcome → pillar/glossary → decision record. The
stable documents cite decision records **by number, through the index only** — never deep-linked.

## What this repository is, and its state

One Python package holding every BRAIN component. Shipped and running: the **git committer**
(`obsidian_tools/commands/commit.py` — in-cluster CronJob) and **`local-replicator`**
(`replicate.py` + `drain.py` — launchd on the operator's Mac; the drainer discards by design until
the work queue exists). Also deployed: `batch-processor` and its watchdog (never yet
observed draining a batch) and the vault-loaded exporter. Built, not deployed: the admission
validator and the lint pass's first pass; the batch producer is released, and runs only when an
operator runs it. Specified but unbuilt: `promotion-processor` and its inbox sweep,
`drift-processor`, the agent runtime, `promotion-processor`'s roll-up pass, and the lint pass's
resolution of judged findings — see [`ROADMAP.md`](./ROADMAP.md), which carries the exact state and names each unit's
tickets.

**The vault system does not know its clients, and pushes nothing to a person.** Agents consume its
APIs; it knows each only by the credential it presents and that credential's kind of access, and
relies on none of them to do its own job — its agentic work is its own agent runtime's, and the
problems it finds it resolves itself rather than handing to a human. Products named in the documents are examples.
Never wire a component to a named client, and never add a client's name to a grant, a stream's
producer set, a vocabulary or an acceptance criterion ([`DESIGN.md`](./DESIGN.md#clients-are-known-by-credential-never-by-name)).

**`main` is not deployed state, in either direction.** A change reaches the cluster only after
release-please cuts a tag *and* the apps/clusters repos bump their pins; the Mac is upgraded by
hand today, and by design converges on the cluster's pinned version by itself (unit D8). `main` can also *understate* what exists (work sitting on open PR branches). Distinguish
*authored → merged → released → deployed-and-observed* in every status claim, and tag claims
**[measured]** or **[inferred]**.

## Repository layout

```
USE_CASES.md · DESIGN.md · ROADMAP.md   — the top-level triad (outcomes / design / work)
docs/
  adr/                                  — decision records in six themed folders; README.md is the index
  VERIFICATIONS.md                      — the verification catalogue
  README.md                             — canon authority chain; CANON-1, CANON-2, FINDINGS-v1 (research inputs)
  local-replicator.md · settings-lock.md · gui-access.md   — operator runbooks
obsidian_tools/
  cli.py · config.py · logging_config.py · retry.py
  commands/                             — the three entrypoints: commit, replicate, drain
  vault_git/                            — the committer's engine: provisioning, runner, ssh/known-hosts,
                                          commit + message building, the settings-baseline allowlist
                                          (baseline_selector.py is the executable copy of ADR-0028's list)
  local_replicator/                     — the replication cycle: clone/tag/exclude, overlay+drift, spool,
                                          drainer, device_baseline (the seed), rsync_ops
packaging/launchd/                      — the two Mac plist templates (replicate, drain)
tests/                                  — pytest + hypothesis, incl. the crash-injection harness
Dockerfile                              — the committer's container image
.github/workflows/                      — lint (incl. the links job), test, test-hypothesis-deep (scheduled),
                                          build-image, release, renovate
lychee.toml · mise.toml · pyproject.toml · commitlint.config.js · release-please-config.json
```

## Language and constraints

- **Runtime code is Python, managed with `uv`; Bash is CI/ops only, never runtime** (ADR-0035 via
  [the index](./docs/adr/README.md)). Target the Python pinned in `mise.toml` — check it, don't
  assume.
- **`pyproject.toml`'s `dependencies` list is empty on purpose and stays that way until a component
  genuinely needs one** — the shipped components run on the standard library alone. When a new
  component needs a dependency, add exactly that, verified against the package's own releases.
- **Never copy a version or config value from another repo on trust**, however close the analog
  looks — verify against current upstream, the way this scaffold was built.
- **Exactly three processes may ever mount the vault volume** (headless Obsidian read-write on
  content; the lint pass read-only; the committer read-only on content with git metadata on its own
  volume). The processors take **no mount** — they write only through the gated MCP path — and the
  agent runtime takes neither a mount nor a vault credential. A fourth
  mounter is a design change to ADR-0001, not a manifest detail — see
  [`DESIGN.md`](./DESIGN.md#one-writer-one-door).

## Working in this repository

- **Conventional Commits**, enforced by commitlint on branch commits and CI. Header ≤ 120 chars.
  The scope enum is closed (`commitlint.config.js`): component scopes `committer`, `replication`,
  `processor`, `validator`, `worker`, `drift-channel`, plus `deps`, `dev-tools`, `github-actions`,
  `renovate`, `release`, and empty. Two scope names predate the component renames — `worker` is the
  retired vault worker (today's lint pass), `drift-channel` is `drift-processor`'s ancestor — kept
  in the enum for history; prefer the scope matching what the diff touches, empty scope for
  cross-cutting changes.
- **Releases are release-please**; never hand-edit `CHANGELOG.md`. **`docs` is a visible release
  type here — a documentation PR proposes a release when merged. Expected, not accidental.**
- **Lint before pushing**: `pre-commit run --all-files` mirrors CI. Tests: `pytest`, with
  **hypothesis property tests only where structured input and a checkable invariant meet** — the PR
  gate runs the fast profile; the deep profile runs on a schedule
  (`test-hypothesis-deep.yaml`) against a shared example corpus built by `main` and scheduled runs
  (a PR's own saves are discarded).
- **The links job** checks every internal link *and anchor* offline on each PR; external links are
  checked weekly, not per-PR (`lychee.toml` — links into the private vault repo are excluded
  because an unauthenticated checker cannot tell "moved" from "private").
- **Renovate** auto-merges patch/minor on green CI; majors need review.
- **Comments in committed code** earn their place: non-obvious constraints, gotchas, reasons a
  workaround exists — never restating the line or narrating the diff.

## Standing disciplines

- **Prove controls by violation injection** — create the violation and watch the control fire;
  "nothing bad happened" proves nothing. The catalogue of proven and pending injections is
  [`docs/VERIFICATIONS.md`](./docs/VERIFICATIONS.md); a new control lands with its injection row.
- **Claims are falsifiable** — an acceptance criterion that cannot fail is not one.
- **One decision per record** in `docs/adr/`, merged only when reversing one would force re-arguing
  the others; superseding a decision mints a new number, never an edit-in-place.
- **Nothing is pushed to a person, ever** — propose instrumentation and queryable metrics, never
  alert rules, digests or notifications ([O3](./USE_CASES.md#o3--alerting) is excluded, not
  deferred), and never design a finding that only a human can clear.

## Gotchas that cost real effort

- **A green `build-image` run proves nothing about the runtime path** — Xvfb, CDP auto-trust and
  REST binding are exercised only by a real deployment, never by the image build.
- **A write-gate refusal is HTTP 200** with the error inside the JSON-RPC envelope — no HTTP-level
  metric can ever observe the gate; parse the envelope or use queue-native facts.
- **A `local-replicator` upgrade fails indistinguishably from healthy**
  ([ot#66](https://github.com/ppat/obsidian-tools/issues/66)): the install path is version-scoped
  while the launchd plist needs an absolute path, so an upgrade silently strands the schedule. The
  design's answer is a self-upgrade to a version-independent path that proves its own schedule
  (unit D8, ADR-0065 via the index); until it exists, verify after upgrading, not just after
  installing.
- **The committer's git dir is a derivable cache, never durable state**: cloned when missing (never
  `git init` — that would re-root history), with every per-clone setting (`fileMode`,
  `skip-worktree`, the ignore rule, `quotePath`) reapplied idempotently on every run.

## Where things are deployed

This repository holds code only. The in-cluster workloads are container images referenced by
`ppat/homelab-ops-kubernetes-apps` (module `apps-obsidian-vault`) and pinned onto clusters by
`ppat/homelab-ops-kubernetes-clusters`; `local-replicator` is installed by hand on the operator's
Mac per `docs/local-replicator.md` — the first install stays a hand act by design; later versions do not. Which component runs where: the component table in
[`DESIGN.md`](./DESIGN.md#components-one-job-each).
