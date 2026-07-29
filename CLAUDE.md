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
- **`/home/coder/code/dependency-migrator`** (the owner's other Python/uv repo, if present in your environment)
  is a source of *shape* only — file layout, how pieces relate — never of proven configuration. It never
  reached a released state (no CI beyond lint/test, no `LICENSE`, no release-please wiring). Several of its
  pinned tool versions were stale when this repository was scaffolded from it. If you're tempted to copy a
  version number or a config value from it, verify it against upstream documentation first.

## Working conventions

- **Conventional Commits**, enforced by commitlint (`commitlint.config.js`). Header max 120 chars. Scope must
  be one of the enum values in `commitlint.config.js`: the six components this repository builds
  (`worker`, `committer`, `processor`, `drift-channel`, `validator`, `replication`) plus the generic scopes
  shared across this ecosystem's repositories (`dev-tools`, `github-actions`, `renovate`, `release`, `deps`).
  Scope generally matches which component a change touches; use no scope (or `dev-tools`) for changes that
  aren't specific to one component (scaffold, CI, shared utilities).
- **Lint before pushing**: `pre-commit run --all-files` mirrors `.github/workflows/lint.yaml` and
  `.pre-commit-config.yaml`. Individual checks can be run standalone — see `README.md` "Development".
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
