# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) and other coding agents working in this repository.

## Start here

- [README.md](./README.md) — what this repository is, what it will hold, and how to run it locally.
- [DESIGN.md](./DESIGN.md) — why this repository is shaped the way it is, and what each component must and
  must never do.
- [docs/README.md](./docs/README.md) — the design canon: `docs/DESIGN.md` is the current design of record for
  the whole BRAIN platform (not just this repository); the other three documents under `docs/` are research
  inputs, superseded wherever `docs/DESIGN.md` says otherwise. Read that file before treating anything in
  `docs/CANON-1-designing-brain.md` or `docs/CANON-2-mcp-research.md` as current.

## Status: this is a scaffold, not a working system

**No application code exists in this repository yet.** `obsidian_tools/__init__.py` is an empty package with a
version string. Every component named in `README.md` and `DESIGN.md` — the vault worker, git committer, batch
processor, drift-reconciliation channel, frontmatter validator, and Mac-side replication script — is a future
ticket, not something to start implementing speculatively. Check the epic
[`ppat/homelab-ops-kubernetes-apps#3439`](https://github.com/ppat/homelab-ops-kubernetes-apps/issues/3439) for
sequencing before starting work on any of them — several have hard dependencies on infrastructure (the headless
Obsidian image, the MCP server deployment, cluster storage) that doesn't exist yet either.

## Language and constraints

- **Runtime code is Python, managed with `uv`.** Bash is for CI and operational scripts only — never runtime
  logic. This is a firm project constraint, not a default; see `DESIGN.md` for why.
- Target the Python version pinned in `mise.toml`, not a version assumed from memory — check what's actually
  pinned, and when bumping it, verify against current upstream (python.org / astral-sh) rather than guessing.
- **Do not add a dependency for code that doesn't exist yet.** `pyproject.toml`'s `dependencies` list is
  intentionally empty. When a component's ticket lands, add exactly the dependencies that component needs, and
  verify the pinned version against the package's own release notes — don't copy a version from another repo
  on trust.
- **Don't copy a version number or config value from another repo on trust, even one that looks like a close
  analog.** A repo can look authoritative — matching CI shape, matching toolchain — while several of its pinned
  versions are simply stale because it never reached a released state. Treat any such repo as a source of
  *shape* only (file layout, how pieces relate); verify every version and config value against current upstream
  documentation independently, the same way this repository's own scaffold was verified rather than copied.
- **Exactly three processes may ever mount the vault volume** — see `DESIGN.md` "The volume mount contract" and
  `docs/DESIGN.md` §1.3/§2 for the full reasoning: headless Obsidian (read-write, content), the vault worker
  (read-only, content), and the git committer (read-only, content; write-only, `.git/`). The **batch processor
  takes no mount** — it reads the patch queue and writes only through the MCP gateway. When implementing any of
  these components' Job/CronJob/Deployment spec (in `ppat/homelab-ops-kubernetes-apps`, not here, but the code
  here assumes it), don't add a volume mount beyond what's listed above without revisiting that invariant first
  — an unlisted fourth mounter breaks it, not just violates a style preference.

## Working conventions

- **Conventional Commits**, enforced by commitlint (`commitlint.config.js`). Header max 120 chars. Scope must
  be one of the enum values in `commitlint.config.js`: the six components this repository builds
  (`worker`, `committer`, `processor`, `drift-channel`, `validator`, `replication`) plus the generic scopes
  shared across this ecosystem's repositories (`dev-tools`, `github-actions`, `renovate`, `release`, `deps`).
  Scope generally matches which component a change touches; use no scope (or `dev-tools`) for changes that
  aren't specific to one component (scaffold, CI, shared utilities).
- **Lint before pushing**: `pre-commit run --all-files` mirrors `.github/workflows/lint.yaml` and
  `.pre-commit-config.yaml`. Individual checks can be run standalone — see `README.md` "Development".
- **Property-based testing (`hypothesis`, in the `dev` dependency group) is for code with invariants that must
  hold across arbitrary valid inputs — not a default for every test.** Reach for it where structured input and
  a checkable invariant both exist; use ordinary example-based `pytest` tests everywhere else. The two candidates
  this repository is expected to have, once the corresponding component lands: the **frontmatter validator**
  (arbitrary frontmatter, valid or invalid, should round-trip through the JSON-Schema contract consistently —
  `docs/DESIGN.md` §3 "Schema enforcement") and the **batch processor's patch chunking** (a patch split into
  chunks, however the splits land, must always reassemble to the original patch — `docs/DESIGN.md` §3 "The batch
  lane"). Don't write property tests — or any tests — for a component that doesn't exist yet; add them when its
  ticket lands.
- **Releases** are independent and automatic via release-please (`release-please-config.json` +
  `.release-please-manifest.json`). Don't hand-edit `CHANGELOG.md`; it's generated when a release PR merges.
- **Dependency updates** are managed by Renovate (`.github/renovate.json`), extending the shared
  `ppat/renovate-presets`. Patch/minor auto-merge if CI passes; majors require review.
- **Comments in committed code**: a comment should tell a future maintainer (often another agent) something
  the code itself can't — a non-obvious constraint, a gotcha, a reason a workaround exists. Don't restate what
  a line does or narrate what changed and why it's better now; that belongs in the commit message, not the file.

## Where things are deployed

This repository holds code only — it has no cluster definitions and doesn't deploy anything itself. The
workloads built here are packaged (as container images, referenced from the sibling
`ppat/homelab-ops-kubernetes-apps` repository's Kubernetes manifests) and run in-cluster, except for the
replication script, which runs on the user's Mac outside the cluster entirely. See `DESIGN.md` "Architecture"
for which component runs where.
