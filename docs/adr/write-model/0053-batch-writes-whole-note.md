# 0053. A chunk applies as whole-note writes, the create/modify distinction carried by the write's own anti-clobber flag

**Status:** Proposed ·
**Pillar:** [The volume is authoritative; git is derived history; no merge engine anywhere](../../../DESIGN.md#the-volume-is-authoritative-git-is-derived-history-no-merge-engine-anywhere) ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted) ·
**Unit:** [A2](../../../ROADMAP.md#group-a--pipeline-mechanisms) ·
**Ticket:** [ot#5](https://github.com/ppat/obsidian-tools/issues/5)

## Context

A batch chunk carries a git patch ([ADR-0022](../work-queue/0022-batch-stream-mechanics.md)).
`batch-processor` holds no mount and no git — it is not one of the three processes permitted to
touch the volume ([the pillar](../../../DESIGN.md#one-writer-one-door)) — so the only bytes it can
reach for a target are the ones it reads back through the gated MCP path, and applying the diff to
them is its own arithmetic. What it holds per target path when it comes to write is therefore a
whole post-image, not an edit.

The tool surface offers four ways to change a note. `obsidian_write_note` replaces a whole file,
or, given a `section`, one heading/block/frontmatter region of it. `obsidian_patch_note` appends
to, prepends to, or replaces such a region. `obsidian_append_to_note` appends to a region or to the
end of the file. `obsidian_replace_in_note` runs an ordered list of literal or regex substitutions
over the body. Three of the four address a *structural* target — a heading name, a block
reference, a frontmatter key — and the fourth addresses a textual match. None takes a diff.

[Gate 3](../../../DESIGN.md#3-the-write-path-end-to-end) names "append/patch preferred" among the
editing primitives, and the two senses of *patch* are different objects: the surface's patch is a
structural edit to a named region, a chunk's patch is a unified diff. Which one the gate means for
this component is what this record settles.

## Decision

Every write a chunk performs is a whole-note write of the post-image its patch produces, and the
operation the chunk declares decides the anti-clobber flag rather than the tool:

| The chunk declares | The call | The flag |
| --- | --- | --- |
| create | whole-file `obsidian_write_note` | `overwrite: false` — an existing target refuses the call |
| modify | whole-file `obsidian_write_note` | `overwrite: true` |
| delete | `obsidian_delete_note` | — |

Reads take the `content` projection — the raw markdown body, which is the bytes the staleness hash
is computed over ([ADR-0048](../work-queue/0048-batch-staleness-per-file-hash.md)) — and nothing
richer. The projections carrying parsed frontmatter, file metadata or a structural map answer
questions this component does not ask, on every read of a bulk import.

Three grounds, in order of weight:

- **The structural primitives cannot accept a whole-note post-image.** A section-addressed call
  needs a region name and that region's new body. A diff that spans a heading boundary, touches
  frontmatter and body together, or rewrites a heading line itself has no single region to name at
  all; deriving one means recomputing structure from the post-image and the pre-image it was built
  from. That is a transformation of the patch's product, which is the reconciliation
  [ADR-0048](../work-queue/0048-batch-staleness-per-file-hash.md) forbids by name — the merge
  engine returning as a tool argument rather than as a subsystem.
- **The substitution primitive is fuzzy patch application at the far end of the door.**
  `obsidian_replace_in_note` matches by content: replacements run in array order over the evolving
  body, `replaceAll` defaults to true, and the result reports how many substitutions landed, never
  whether they landed where they were meant to. A search string occurring twice in a note is
  applied twice. Applying the diff in the processor instead matches against an exact pre-image and
  refuses when context disagrees; handing substitution pairs to the surface trades that exactness
  for a content match against whatever the note holds at the moment of the call.
- **The append primitive turns a modify into a create when its target is absent.**
  `obsidian_append_to_note` without a section appends to the end of the file *or creates the file
  when it does not exist, with the appended content becoming the whole file*. A modify whose target
  went away between the pre-flight read and the write then lands as a note holding a fragment, and
  reports success — indistinguishable from a correct append, and destroying precisely what the
  missing-target rejection exists to catch.

`overwrite` is not a version precondition and is no substitute for one. It asserts the target's
*absence*, so it constrains the create half and says nothing about the modify half. What the
surface offers there is post-write self-report — a write returns whether it created the note, and
the note's size before and after — which is detective.
[ADR-0048](../work-queue/0048-batch-staleness-per-file-hash.md) carries what that leaves open.

## Alternatives considered

- **`obsidian_patch_note` per structural target**, on the reading that Gate 3's "patch preferred"
  names this tool. It needs a region-shaped edit the chunk does not carry, and its
  `createTargetIfMissing` flag converts a region that does not match into an invented heading
  rather than a refusal — the mismatch stops being visible exactly where it matters.
- **Section-targeted `obsidian_write_note`.** The same region-naming problem, without even the
  append/prepend/replace vocabulary that would make the derivation expressible.
- **`obsidian_replace_in_note` with pairs derived from the diff's hunks.** The second ground above.
  It is the closest thing on the surface to applying a diff, and that is the objection: it applies
  something diff-shaped without the diff's exactness, and reports a count instead of a verdict.
- **`obsidian_append_to_note` for the additive subset of a diff.** The third ground above, and a
  diff that only adds lines is not thereby an append: added lines land at their hunk positions,
  which is the end of the file only by coincidence.
- **`overwrite: true` on every write, leaving create-vs-modify entirely to the pre-flight.** One
  branch simpler, and it discards the only server-side assertion the surface offers — see the
  consequence below on what that assertion actually backstops.
- **Refusing to write through this surface at all** until it carries a diff-shaped primitive.
  Rejected because the primitive would not help: the processor has already applied the diff by the
  time it writes, and moving that application behind the door would put patch arithmetic inside the
  one process that mutates vault content.

## Consequences

**The raw layer's create-only rule keeps exactly one enforcer, and gains an independent trip on one
of its two halves.** [ADR-0015](../content-model/0015-raw-immutability.md)'s check is path-scoped
and covers both halves: a modify or delete of a raw path is refused before any hash is considered,
and a create over an existing raw note is refused whatever its content. `overwrite: false` is
path-blind — it cannot see the raw layer, and it cannot refuse a raw modify, which travels as an
ordinary `overwrite: true` write like any other. So this is not one rule enforced twice. It is one
rule's named enforcer, plus a general anti-clobber assertion on every create in the system, whose
overlap with the rule is a single case.

**The redundancy that does exist is worth having, because the two rest on different things and fail
independently.** The pre-flight's create branch turns on the target reading as absent, and the tool
surface reports absence as an error carrying a structured reason of its own — a fact about the
vault, told apart from a refusal about authority without either one's message being read. Absence
is recognised from that structured reason alone: the one error the surface emits without structure
is its SDK's own, which says "not found" about a *tool*, so reading absence from a message could
only misread it. `overwrite: false` rests on something different
again: the server's own existence test, at the moment of the write. A determination of absence that
went wrong in the unsafe direction would put a create in front of a note that exists, and the flag
refuses it. Which of the two is the backstop is
[ADR-0005](./0005-path-scope-granularity.md)'s discipline, and the answer is unchanged: the rule's
enforcer is the code check, the flag is never claimed as one, and the acceptance test for the rule
targets the code.

**The two refuse at different points, and the order is load-bearing.** The pre-flight refuses
before anything is written, carrying the reason code that makes raw refusals countable. The
server's refusal arrives mid-chunk, inside a JSON-RPC envelope on an HTTP 200, after earlier writes
of the same chunk have already landed — a half-applied chunk, recovered by producer regeneration.
Reaching it at all means the pre-flight's view of the vault was wrong, which is both why it is
worth having and why it must stay second.

**Convergence is unaffected.** A redelivered chunk whose every path already holds what applying it
would leave there is settled before any write
([ADR-0022](../work-queue/0022-batch-stream-mechanics.md)), so `overwrite: false` never meets a
create that a re-run should have applied again; a partially applied chunk is rejected by the
pre-flight's own existence check, not by the flag.

**A modify's blast radius is the whole note, bounded by the pre-image rather than by the
primitive.** The post-image is the pre-image plus the diff, and the pre-image is the content whose
hash the pre-flight has just matched, so nothing outside the diff changes. What the choice gives up
is commutativity: a concurrent writer editing a different region of the same note between the
pre-flight read and the write loses its edit, where a region-targeted call would have kept it. That
window is [ADR-0048](../work-queue/0048-batch-staleness-per-file-hash.md)'s, narrowed by a batch
run stopping the agent instance ([ADR-0052](../work-queue/0052-batch-mode-stops-the-agent-instance.md))
and not closed by it.

**"Append/patch preferred" keeps its force where a writer holds a region-shaped edit** — the
interactive path, where content originates as an addition to a named place, and where the commuting
primitives are the reason a concurrent edit elsewhere in the note survives. It does not reach a
writer holding a whole-note post-image, because there is no region-shaped edit to prefer.

**What would show this wrong.** Three observations, each pointing at a different part of the
choice:

| Observation | What it would mean |
| --- | --- |
| A note's content after a write differing from the post-image sent, in a way the diff does not account for | The surface does not write the bytes it is given, and whole-note writing is not the safe primitive it is chosen for being. The write's own before/after sizes are the instrument |
| Lost updates concentrated on notes a batch run touched, where the lost edit and the chunk's edit are in different regions | Commutativity is worth more than this record prices it at, and a region-targeted write becomes right for the subset of diffs that do have a single region target |
| A post-image too large to carry in one call | Whole-note writing stops being expressible, and the answer becomes a chunking one rather than a primitive one |
