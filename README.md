# obsidian-tools

Code for **BRAIN**: a git-backed Obsidian vault that a human reads and multiple AI agents write to. See
[`docs/`](./docs/) for the full design of record and [`DESIGN.md`](./DESIGN.md) for this repository's part in it.

## Status

**No application code exists yet.** This repository currently holds only the scaffold (CI, linting, release
automation, dependency conventions) and the design canon under `docs/`. Everything below describes the target
shape, not what's implemented today — see [`CLAUDE.md`](./CLAUDE.md) for what that means when working here.

## What this repository will hold

BRAIN has exactly one process that ever writes the vault's files — a headless, in-cluster Obsidian instance,
reached only through a permission-scoped MCP server (see [`DESIGN.md`](./DESIGN.md) for why). Every other
component in this repository is a *client* of that one door:

- **Vault worker** — ingest/promote, lint, and publish entrypoints, run as a scheduled job.
- **Git committer** — turns the vault volume into git history; never writes vault content itself.
- **Batch processor** — applies git patches from a queue through the MCP server, for bulk work too large to
  run tool-call-by-tool-call.
- **Drift-reconciliation channel** — dispatches device-side edits (typed directly into Obsidian on Mac/iOS)
  back into the system as ordinary agent writes.
- **Frontmatter validator** — a JSON-Schema validator enforcing the vault's schema at the promotion gate.
- **Replication script** — runs on the user's Mac, not in the cluster; orchestrates `rsync` and `git` to keep
  the iCloud-synced Obsidian vault on Mac/iOS current from the authoritative cluster copy.

None of these exist yet. Each lands as its own ticket — see the epic
[`ppat/homelab-ops-kubernetes-apps#3439`](https://github.com/ppat/homelab-ops-kubernetes-apps/issues/3439) for
sequencing, and [`docs/`](./docs/) for the design each of them implements.

## Related repositories

- [`ppat/obsidian-vault`](https://github.com/ppat/obsidian-vault) — the vault content itself (the markdown, not
  the code that writes it).
- [`ppat/homelab-ops-kubernetes-apps`](https://github.com/ppat/homelab-ops-kubernetes-apps) — deploys the
  workloads this repository's code runs as, alongside the rest of the homelab.

## Development

Requires [`mise`](https://mise.jdx.dev/) (pins exact tool versions — see [`mise.toml`](./mise.toml)) and
[`uv`](https://docs.astral.sh/uv/) for Python dependency management.

```bash
mise install
mise exec -- uv sync --all-extras
mise exec -- pre-commit install --install-hooks
```

Run the checks locally:

```bash
mise exec -- uv run ruff check .
mise exec -- uv run ruff format --check .
mise exec -- uv run pyright
mise exec -- uv run pytest
pre-commit run --all-files
```

CI runs the same checks — see [`.github/workflows/lint.yaml`](./.github/workflows/lint.yaml) and
[`.github/workflows/test.yaml`](./.github/workflows/test.yaml).

## Releases

Versioned independently via [release-please](https://github.com/googleapis/release-please) — merging a release
PR cuts a tagged release and updates `CHANGELOG.md` automatically. Don't hand-edit `CHANGELOG.md`.

## Language and tooling conventions

Runtime code is Python, managed with `uv`; shell is for CI/ops only, never runtime — see
[`CLAUDE.md`](./CLAUDE.md) for the full set of working conventions for this repository.
