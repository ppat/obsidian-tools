# 0054. The vault system's agentic work runs in one agent runtime of its own, which judges and never writes

**Status:** Proposed ·
**Pillar:** [The vault system does its own job](../../../DESIGN.md#the-vault-system-does-its-own-job) ·
**Serves:** [S2](../../../USE_CASES.md#s2--sound) ·
**Unit:** [A9](../../../ROADMAP.md#group-a--pipeline-mechanisms)

## Context

Several of the vault system's own mechanisms need a model's judgement rather than a rule:

| Caller | The judgement |
| --- | --- |
| The lint pass | Contradictions and claims a model would judge stale, and which resolution each judged finding gets ([ADR-0055](../content-model/0055-lint-findings-resolved-by-the-vault.md)) |
| `promotion-processor`'s roll-up pass ([ADR-0060](../work-queue/0060-roll-up-pass-owned-by-promotion-processor.md)) | `salience:` scores ([ADR-0012](../content-model/0012-salience-consolidated-fields.md)), merge proposals, and retirement verdicts |
| `promotion-processor` | A note's curated home, where the note's own frontmatter does not settle it |

Three standing owner rulings bound where that judgement may come from. The vault system does not rely
on outside agents to do its job — agentic work it needs is done by its own agentic workflow. And no
LLM gateway is part of the design: whatever fronts the MCP instances is a role whose occupant is a
deployment choice, and an installation may occupy it with something that has no models at all, so
judgement cannot be a service of that front. And nothing waits on a human: a judgement the vault
system needs cannot be handed to a person either.

## Decision

**One agent runtime, a library inside this package, is the only code in the vault system that calls a
model.** It is called in-process by the component that owns the question — the same shape as the
admission validator, one shared piece with several callers ([ADR-0007](../write-model/0007-validation-placement.md)).

- **Tasks are typed.** Each task declares its input, the schema its output must satisfy, and the
  tools it may use. Every model output is validated against the task's schema before the caller sees
  it; an output that fails validation is a failed task, never a partial verdict.
- **It holds no vault credential and no mount.** A tool a task uses is a read function its caller
  supplies: either over the caller's own read access, or — for evidence — what the credential-less
  evidence fetcher returned for the sources a note itself cites
  ([ADR-0061](../content-model/0061-freshness-is-the-vaults-own-verdict.md)). The three-mount rule and the credential set are
  untouched ([ADR-0001](../write-model/0001-single-writer-one-door.md)).
- **It never writes, and its verdict never admits.** A verdict is an input to the calling component,
  which acts or declines under its own handle, its own path scope and the admission validator. The
  validator's decision stays deterministic; model judgement is flag-shaped or proposes an action a
  deterministic owner then takes, which is the auto-fix boundary
  ([ADR-0018](../content-model/0018-lint-pass-policy.md)) applied to every caller.
- **The model endpoint is configuration.** A provider's API directly, or through any front an
  installation runs; the runtime works identically either way. Because the runtime runs in-process,
  each calling component holds its own model-endpoint credential.
- **Where a note's curated home needs judgement, the runtime proposes it and the validator still
  admits.** Promotion settles a destination from the note's own frontmatter wherever that suffices.
  Where it does not, the destination is the runtime's verdict. The relocation it leads to crosses
  the curated boundary like any other, through the admission validator. Without the runtime, such a note
  stays in the inbox, counted, and is swept again
  ([ADR-0056](../work-queue/0056-inbox-is-promotions-work-list.md)). It is never quarantined for
  lack of a verdict.
- **Every task call is emitted**: task, model, latency, token use, and schema failures — so what the
  judgement half costs and how often it fails is answerable after the fact
  ([O1](../../../USE_CASES.md#o1--measured)).

**The framework is not chosen here.** What it must satisfy is: output validated against a declared
schema; provider-agnostic model configuration; tools expressed as plain typed functions; a test model
that lets every caller's suite run without a live endpoint; and a dependency footprint compatible
with the one-package, one-image build ([ADR-0035](./0035-tooling-python-one-repo.md)). PydanticAI is
the owner's named example and is evaluated first. The choice is made and recorded by the unit that
builds the runtime ([A9](../../../ROADMAP.md#group-a--pipeline-mechanisms)): the repository's rule is
that a dependency is added when a component genuinely needs one, verified against its own releases,
and nothing in the design turns on which framework satisfies the list.

## Alternatives considered

- **Handing the judgement to outside agents** — a conversational agent resolving lint findings, a
  workflow engine organising the vault. Ruled out by the owner, and unsound on its own terms: an
  outside agent's presence, behaviour and continued existence are facts the vault system cannot know,
  so an outcome resting on one cannot be falsified by anything the vault system controls.
- **Model calls made wherever a component needs one** — each caller with its own client, prompt
  handling, output parsing, provider configuration and metrics. Easy at the first call site and
  complected by the third: validation, budgets and emission would be re-decided per component and
  drift apart. The validator was made one shared piece for the same reason.
- **A standalone agent service** with its own deployment and API. It would need a vault read
  credential of its own to fetch the context its callers already hold, and a network hop between a
  scheduled job and its own judgement. Nothing any caller needs requires a long-running process;
  revisit when one does.
- **An agent with write authority** — an autonomous maintainer acting on its own verdicts. Braids
  judgement with authority: containment in this design rests on credentials, path scope and the
  validator, and a model deciding writes would sit across all three.

## Consequences

- **Model availability becomes a dependency of the judgement halves only.** With the endpoint
  unreachable, judgement tasks fail and are reported; normalisation, the mechanical checks,
  admission, and the relocation of notes whose destination is settled proceed.
  [Fail loud, destroy nothing](../../../DESIGN.md#fail-loud-destroy-nothing) applies unchanged.
- **Prompt injection is bounded by construction, not by prompting.** Tasks read note content, and any
  note may carry injected instructions. Because the runtime cannot write and its outputs are
  validated into declared schemas — closed vocabularies wherever the caller acts on them — an
  injection can at worst yield a wrong verdict inside the caller's own authority and its closed set
  of actions: a lowered confidence, a misplaced annotation, a declined proposal. Never a write
  outside it.
- **The drift classifier stays deterministic** ([ADR-0008](../write-model/0008-drift-classification-separate-authority.md)):
  its signals are decidable without guessing intent. The runtime is available to it if a judgement
  signal is ever added; nothing here adds one.
- **A caller whose judgement half does not run reports it as not checked** — the tolerance line's
  "not checked" tier — rather than as a clean result.
- **The framework is a third-party runtime dependency**, added under the repository's rule for
  dependencies and nowhere else in the package.
