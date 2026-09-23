# obsidian-tools

Code for **BRAIN**: a git-backed Obsidian vault used as a shared brain — the store of the owner's
knowledge and ideas, written mostly by AI agents, read by the same agents and a human. The design of
record lives beside this file: [`USE_CASES.md`](./USE_CASES.md) (the outcomes),
[`DESIGN.md`](./DESIGN.md) (the pillars, invariants and glossary), and [`ROADMAP.md`](./ROADMAP.md)
(the work and its state), with the decision records under [`docs/adr/`](./docs/adr/README.md).

## Status

Two components have run in production longest — the others' state, and the gap between what is
released and what the cluster runs, are in [`ROADMAP.md`](./ROADMAP.md):

- **The git committer** (`obsidian-tools commit`) — runs in-cluster on a schedule, turning the vault
  volume into git history pushed to its one remote, GitHub. It never authors content.
- **`local-replicator`** (`obsidian-tools replicate` + `obsidian-tools drain`) — runs on the
  operator's Mac under launchd, keeping the device-facing iCloud vault current from git, one-way and
  non-destructively: device-side drift is captured to a durable local spool before anything is
  overwritten. The drainer's destination is a deliberate stub (discard) until the drift stream exists.

The other components, each specified in the design and tracked on the roadmap with its exact state:
the work queue and its three processors (`batch-processor`, `promotion-processor`,
`drift-processor`), the batch producer, the admission validator, the lint pass, and the agent
runtime — the vault system's own agentic workflow, so that nothing it does rests on an outside
agent.

## Documentation

| Where | What |
| --- | --- |
| [`USE_CASES.md`](./USE_CASES.md) · [`DESIGN.md`](./DESIGN.md) · [`ROADMAP.md`](./ROADMAP.md) | Outcomes and acceptance criteria · pillars, invariants and the settled glossary · all the work in one place |
| [`docs/adr/`](./docs/adr/README.md) | One decision per record: context, decision, alternatives, consequences |
| [`docs/VERIFICATIONS.md`](./docs/VERIFICATIONS.md) | Every control's proving injection and answerable-by-doing check, past and pending |
| [`docs/`](./docs/README.md) | The research canon behind the design, and the operator runbooks (`local-replicator.md`, `settings-lock.md`, `gui-access.md`) |

## Related repositories

- [`ppat/obsidian-vault`](https://github.com/ppat/obsidian-vault) — the vault content itself (private).
- [`ppat/homelab-ops-kubernetes-apps`](https://github.com/ppat/homelab-ops-kubernetes-apps) — the
  deployment manifests (module `apps-obsidian-vault`) for the in-cluster workloads built here.
- [`ppat/homelab-ops-kubernetes-clusters`](https://github.com/ppat/homelab-ops-kubernetes-clusters) —
  composes those modules onto the real clusters at pinned release tags.

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

CI runs the same checks plus an offline link-and-anchor check — see
[`.github/workflows/lint.yaml`](./.github/workflows/lint.yaml) and
[`.github/workflows/test.yaml`](./.github/workflows/test.yaml).

## Releases

Versioned via [release-please](https://github.com/googleapis/release-please) — merging a release PR
cuts a tagged release and updates `CHANGELOG.md` automatically. Don't hand-edit `CHANGELOG.md`.

## Conventions

Runtime code is Python, managed with `uv`; shell is for CI/ops only, never runtime. The full working
conventions for this repository — an agent's orientation included — are in
[`CLAUDE.md`](./CLAUDE.md).
