"""Pure decisions for local-replicator's replication cycle (ADR-0025; ppat/obsidian-tools#3):
what a staged git change becomes as a spool entry, and whether a
cycle's publish and tag advance may proceed. No filesystem, no git, no rsync -- `cycle.py` gathers
the state (git plumbing, rsync) and calls these; every safety rule lives here exactly once, the
same split `vault_git/baseline_selector.py` uses over `baseline.py`, and for the same reason -- see
that module's docstring for what happens when a safety rule instead lives inline in the code that
produces the data it judges.

**Why this exists as its own module, not inlined in cycle.py.** The third reading of this cycle
(ADR-0025) replaced two intertwined
mechanisms -- a path enumeration and a separate per-path content read, gated per path -- with one:
checking out the baseline and overlaying the device tree turns "which paths changed, and what do
they now contain" into a single `git diff`. Once the comparison collapses to one mechanism, the
decisions riding on it collapse too, to two pure questions this module answers: given a staged git
change, what goes in the spool (`select_spool_entries`); and given whether every entry made it into
the spool durably, does this cycle publish and advance the tag (`decide_cycle_outcome`). Neither
question needs a filesystem to answer, so neither gets one -- which is what makes both cheap to test
against hand-built, adversarial input rather than only against real git repositories one case at a
time.

**Decisions, and one observation that is deliberately not one.** `matches_upstream` (below) is the
third thing here and the odd one out: it answers no question about what this component should *do*.
It restates, per entry, a fact `cycle.py` read off git -- these bytes are the bytes the clone's
known upstream revision already holds -- so that a Phase 5 consumer can decide something this
component must not (ppat/obsidian-tools#36, and `SpoolEntry`'s own docstring for the full argument).
It lives here because it is pure and belongs with the record it annotates, not because it is a
judgement.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

SpoolEntryKind = Literal["create", "modify", "delete", "rename"]


@dataclass(frozen=True, slots=True)
class StagedChange:
    """One record from `git diff --cached --name-status -z`, taken after the device overlay has
    been staged with `git add -A` (cycle.py), paired with the patch text `git diff --cached`
    produces for that same path.

    `patch` is never empty for a real change: a deletion's patch removes every line, and a
    creation's patch -- only possible *because* the path was staged first, since an unstaged
    `git diff` shows nothing at all for an untracked path (ADR-0025,
    "Constraints") -- adds every line, from `/dev/null`. That is what "the whole file, once
    staged" means in the design doc: one mechanism produces both a modification's diff and a
    creation's full content, so there is no separate content-read step for creations.

    `status` is git's raw code (`A`, `M`, `D`, or an `R`/`C`-prefixed similarity score) -- kept
    verbatim rather than pre-classified, so `select_spool_entries`/`_classify` is the one place
    that interprets it.
    """

    status: str
    path: str
    old_path: str | None
    patch: str


@dataclass(frozen=True, slots=True)
class UpstreamComparison:
    """What the cycle knew about upstream at the moment it enumerated drift -- gathered by
    `cycle.py` from git, judged nowhere.

    `sha` is `refs/remotes/origin/<branch>` as the parked clone held it **before this cycle's
    fetch**, and that is the load-bearing choice. The drift enumeration runs ahead of the pull
    (ADR-0026), so this is the only upstream revision
    observable at that moment. Measuring against the revision this cycle is *about* to fetch would
    report a human's edit as upstream content whenever an agent happened to write the same text
    upstream in between, which loses an edit.

    **It is not "the revision whose tree the last publish placed in iCloud", and an earlier version
    of this docstring said so wrongly.** That holds only when the previous cycle both fetched and
    published. Step 5 fetches unconditionally while step 6 is gated, so *every* withheld cycle --
    a crash in the step 6/7 window, a spool-write failure, and, far more commonly, an ordinary gate
    on a pasted image (`captures_content`) -- leaves this ref ahead of anything iCloud has seen,
    and it runs further ahead every cycle the gate persists. All this field can honestly claim is
    the revision the clone *knew*. What follows from that for `matches_upstream` is spelled out on
    `SpoolEntry`, because the consequence is a real one, not a caveat.

    `differing_paths` is `git diff --cached --name-only` against `sha`
    (`GitRunner.staged_paths_differing_from`): every path where the staged overlay is not
    byte-identical to that revision. Membership is the raw fact; `matches_upstream` below is its
    per-entry restatement, and nothing here decides what either means.
    """

    sha: str
    differing_paths: frozenset[str]


@dataclass(frozen=True, slots=True)
class SpoolEntry:
    """One drift patch, ready to be written to the local spool (spool.py) -- the unit
    ADR-0025 calls "each drift patch." `kind` restates `StagedChange`'s raw
    git status in a stable, small vocabulary a downstream consumer can classify on without knowing
    git's status-letter conventions (ADR-0008's *shape* heuristic -- an append reads
    differently from a rewrite -- needs exactly this plus the patch text itself, once a real
    consumer exists at Phase 5). It already distinguishes a path created on the device from one
    modified in place, so nothing else here needs to restate that.

    **The three provenance fields carry observations, never verdicts** (ppat/obsidian-tools#36).
    Steps 6 and 7 are two operations that cannot be made one, so a crash can always leave published
    upstream content in iCloud while `LAST_CHECKOUT` still names the pre-publish commit; the next
    cycle then reads that content as device-side drift. The device does not suppress it -- deciding
    a drifted path is not a human's edit is a judgement, and ADR-0008 reserves those
    for the server ("it submits every path the comparison flags, and makes no judgement, so it can
    never silently drop a real edit"). It records instead what only it can see: the iCloud tree at
    the one moment before publish's `--delete` overwrites it, measured against the baseline the
    comparison actually ran on. A `drift-processor` receiving the patch minutes later cannot
    reconstruct any of that, and none of it is recoverable afterwards -- which is why withholding it
    would be the real failure.

    - `baseline_sha` -- the `LAST_CHECKOUT` commit this cycle parked at and compared against. Every
      entry `select_spool_entries` builds records one (a comparison cannot happen without a
      baseline); it is optional only so `spool.read_spool_entry` can read back an entry written by a
      version predating these fields without inventing a value for it.
    - `upstream_sha` -- the upstream revision the clone knew at that moment (`UpstreamComparison`),
      or `None` when it knew none (a re-provisioned cache, an origin with no history yet).
    - `matches_upstream` -- whether **every** path this entry names is byte-identical to
      `upstream_sha`'s tree: a create's or modification's content equal to upstream's there, a
      deletion's path absent from upstream too, and a rename's *both* halves -- new path identical,
      old path likewise gone. `None`, never `False`, when there was no upstream revision to measure
      against: "not determinable" and "determined to differ" are different facts and must not be
      spelled the same way.

    **What `matches_upstream: true` licenses, and what it does not.** It licenses exactly one
    inference: these bytes are bytes `upstream_sha` already holds, so admitting this edit adds
    nothing that is not already upstream. **It is not evidence of crash residue**, and reading it
    that way discards real human edits. Three things have to be true at once for it to be residue
    -- the previous cycle published, it then died before advancing the tag, and this path was in
    what it published -- and the entry evidences none of them.

    Nor does `baseline_sha != upstream_sha` rescue the inference, though it is tempting: it says
    only that a previous cycle fetched without advancing the tag, which a gated cycle does just as
    readily as a crashed one, and gated cycles are the common case (`captures_content`'s own
    docstring calls a pasted image ordinary Tuesday behaviour). The pair `matches_upstream: true`
    **and** `baseline_sha != upstream_sha` is reachable with no crash anywhere -- see
    `test_an_ordinary_gated_cycle_also_produces_matches_upstream_on_a_real_human_edit`, where a
    human deletes a note an agent had independently deleted upstream, and the entry is
    indistinguishable from residue.

    **Deletions and renames are where this bites hardest, and the error runs the *unsafe* way.**
    For a modification the observation needs a byte-for-byte content coincidence, which is rare.
    For a deletion it needs only a *path* coincidence -- the human deleted a note, upstream also no
    longer has it -- which an agent reorganising, renaming or archiving upstream produces easily.
    So `matches_upstream: true` on a `delete` or a `rename` carries markedly less information than
    on a `modify`, and a consumer that treats them alike will be wrong about deletions first.

    **Can the device tell the two states apart? No, and that is worth stating plainly** rather than
    leaving Phase 5 to assume otherwise. The distinguishing physical fact is whether the previous
    cycle's publish ran, and nothing in the tree records it: the same cycle re-derives its whole
    world from `LAST_CHECKOUT`, iCloud and origin every time, by design. Recovering it would need
    durable state carried between cycles -- the objection that already ruled out an intent marker
    and `git stash` here, since a crash can strand it, and a stranded marker would mislead in
    exactly the state it exists to describe. One cheap heuristic does exist and is deliberately not
    recorded as a field: in a cycle that follows a real publish, every path that differs from
    `upstream_sha` is also a drifted path, whereas after a *withheld* publish the paths upstream
    changed are still sitting at the baseline's version in iCloud and so differ without drifting.
    It is a strong hint, not a decision procedure -- it collapses when the human happened to touch
    every path upstream changed -- so it belongs with the Phase 5 consumer that can validate it, not
    guessed at here ahead of one. It is at least uncontaminated: `.obsidian/` is excluded from the
    overlay as well as from the publish (`exclude.py`), so no path this heuristic sees is one the
    publish was structurally unable to place. The argument is recorded in ppat/obsidian-tools#42.

    Deliberately not recorded, likewise: a `previous_cycle_incomplete` field. `baseline_sha !=
    upstream_sha` already says that much, and saying it again one interpretation further along
    would only make the wrong reading above easier to reach.
    """

    kind: SpoolEntryKind
    path: str
    old_path: str | None
    patch: str
    baseline_sha: str | None
    upstream_sha: str | None
    matches_upstream: bool | None


# `git diff` emits this line *instead of* a hunk body when either side of a change is binary, so
# the resulting patch describes that something changed without carrying a single byte of what.
# Matched as a substring of the patch rather than by sniffing the path's extension: what makes a
# capture incomplete is what git actually produced, not what the filename suggests it should have.
_BINARY_PATCH_MARKER = "\nBinary files "

# Every patch `git diff` produces opens with this, whatever the change is -- a creation, a deletion,
# a mode-only change, a rename with no hunk at all, a non-ASCII or quote-bearing filename, a binary.
# Requiring it is a *positive* check that what arrived is git's own patch format, standing behind
# the flags and scrubbed environment that keep the operator's git configuration from replacing that
# format (`vault_git/runner.py`). A predicate phrased only as "the binary marker is absent" reads
# every possible non-patch -- an empty string most of all -- as a healthy capture.
_PATCH_HEADER_PREFIX = "diff --git "


def captures_content(change: StagedChange) -> bool:
    """Whether this change's patch actually carries what changed, as opposed to merely asserting
    that something did.

    **This is the predicate the publish gate's safety property rests on**, and it was assumed
    rather than checked. `StagedChange`'s docstring claims "`patch` is never empty for a real
    change... a creation's patch adds every line" -- true for text, false for binary. Paste an
    image into a note on the phone and `git diff --cached` produces
    `Binary files /dev/null and b/_attachments/x.png differ`: a patch with no content. The spool
    write for it then *succeeds*, so the gate reports the cycle safe, and the publish rsync's
    `--delete` removes the file from iCloud -- destroying the only copy of those bytes anywhere.

    A pure rename deliberately passes: `similarity index 100%` / `rename from` / `rename to` has
    no hunk body either, but the header describes the change completely, so nothing is lost. A
    mode-only change (`old mode` / `new mode`) is the same shape for the same reason. Keying on
    git's binary marker rather than on "has a hunk" is what keeps both captured -- and a
    hunk-demanding predicate would not merely be wrong about them, it would *wedge* the cycle,
    since a mode change persists in the tree and regenerates as the same drift every cycle.

    **A deletion passes for the rename's reason, and withholding one wedges the cycle
    permanently.** `Binary files a/x.png and /dev/null differ` carries nothing, and needs to carry
    nothing: the pre-deletion bytes are in git at `LAST_CHECKOUT`, which is the very commit step 1
    re-parks at every cycle. That last fact is also why getting this wrong is not a pause. A
    withheld *creation* stops the cycle until the operator removes the file, and removing it ends
    the drift. A withheld deletion cannot be ended that way: the baseline still contains the file
    and cannot advance, so the same deletion is re-detected from scratch on every cycle, forever,
    holding back every upstream edit behind it -- and for a tracked file, "remove it from the
    vault" is not the escape from that state, it is the entrance to it. Measured over eight
    consecutive cycles, three of them after the documented remedy.

    A *modification* is genuinely different and stays withheld: its new bytes exist only on the
    device, so publishing over it destroys them. That is a pause, and it is one only because the
    deletion above is captured -- deleting the file is then a remedy that resolves on the next
    cycle rather than trading one stuck state for another.

    **What it is not enough to check.** The two guards before the marker are not defensive
    padding. An empty patch is the strongest possible signal that nothing was carried, and
    "the binary marker is absent" reads it as healthy; so does a summary line from an external
    diff tool that replaced git's output entirely. `vault_git/runner.py` is what stops the
    environment reaching this text at all -- this is the check that the text is what that
    invocation was supposed to produce.

    **This is not a new policy call.** The vault receives markdown only, images are deferred past
    the first pass, and `_attachments/` is a committed placeholder with nothing in it yet
    (ADR-0014) -- so a binary here is out of contract. What to do about
    out-of-contract input is likewise already settled, by DESIGN.md's "Fail loud, destroy nothing":
    every component defaults to that posture when it meets something it cannot reconcile. Silently
    deleting the bytes is the one response that policy rules out.
    """
    if not change.patch.startswith(_PATCH_HEADER_PREFIX):
        return False
    if _classify(change.status) == "delete":
        return True
    return _BINARY_PATCH_MARKER not in change.patch


@dataclass(frozen=True, slots=True)
class SpoolSelection:
    """What a cycle's staged changes decompose into: entries safe to spool, and paths whose
    capture came back without content.

    Two lists rather than a filtered one, because dropping the second silently is exactly what
    ADR-0008 forbids -- "it submits every drift patch... and makes no judgement, so
    it can never silently drop a real edit." `uncaptured` is not a judgement about whether the
    edit mattered; it is a statement that this component could not capture it, which the cycle
    then treats as a capture failure rather than as permission to proceed.
    """

    entries: list[SpoolEntry]
    uncaptured: list[str]


def matches_upstream(change: StagedChange, upstream: UpstreamComparison | None) -> bool | None:
    """Whether every path `change` names is byte-identical to `upstream`'s tree -- the one
    observation `SpoolEntry` carries that the device is uniquely positioned to make, and the one a
    Phase 5 consumer cannot reconstruct from the patch alone (ppat/obsidian-tools#36).

    An *observation*, not a verdict: it says these bytes are the bytes upstream already holds. It
    does not say who typed them, and deliberately cannot -- a human edit that reproduces upstream is
    indistinguishable from crash residue by construction, and nothing on the device can tell them
    apart. `SpoolEntry`'s docstring states exactly what a `True` here licenses downstream and what
    it emphatically does not; read that before building anything on this value, because the
    plausible reading of it is the wrong one.

    Every path, not just `change.path`, and that is what makes a rename honest: upstream renaming
    `a.md` to `b.md` leaves `b.md` identical *and* `a.md` gone, while a device rename onto a path
    that coincidentally matches upstream leaves `a.md` still sitting upstream. Requiring both halves
    is what keeps the second from being recorded as the first. A deletion falls out of the same
    rule with no special case: `differing_paths` lists a path present on exactly one side, so a path
    absent from both the overlay and upstream simply never appears there -- but note that this makes
    a deletion the *cheapest* shape for `True` to arise on, since it needs only a path coincidence
    where a modification needs a byte-for-byte one.

    `None` rather than `False` when there is no upstream revision to measure against: "no
    observation was possible" is a different fact from "observed to differ", and a consumer that
    cannot tell them apart would read a re-provisioned cache's whole first drift enumeration as
    positively established device authorship.
    """
    if upstream is None:
        return None
    paths = {change.path} if change.old_path is None else {change.path, change.old_path}
    return not (paths & upstream.differing_paths)


def select_spool_entries(
    changes: Sequence[StagedChange], *, baseline_sha: str, upstream: UpstreamComparison | None
) -> SpoolSelection:
    """The one place a staged git change becomes a spool entry.

    One entry per input change -- nothing is merged, split, or dropped. That is what lets the
    device-side detector stay "dumb" (ADR-0008: "it submits every drift patch...
    and makes no judgement, so it can never silently drop a real edit"): filtering anything out
    here -- `.obsidian/` drift included, and content this cycle observes to be identical to
    upstream most of all -- would be a device-side judgement call this design deliberately reserves
    for Phase 5's server-side classifier, not for this component. `baseline_sha`/`upstream` are
    therefore inputs to what each entry *records*, never to which entries exist.

    Sorted by path for deterministic spool ordering -- the spool itself has no other concept of
    "this cycle's batch" once entries are written (spool.py), so a stable order is what makes a
    written spool directory reproducible from the same drift, run to run.
    """
    entries = [
        SpoolEntry(
            kind=_classify(change.status),
            path=change.path,
            old_path=change.old_path,
            patch=change.patch,
            baseline_sha=baseline_sha,
            upstream_sha=None if upstream is None else upstream.sha,
            matches_upstream=matches_upstream(change, upstream),
        )
        for change in changes
        if captures_content(change)
    ]
    uncaptured = [change.path for change in changes if not captures_content(change)]
    return SpoolSelection(
        entries=sorted(entries, key=lambda entry: entry.path),
        uncaptured=sorted(uncaptured),
    )


def _classify(status: str) -> SpoolEntryKind:
    code = status[:1]
    if code == "A":
        return "create"
    if code == "D":
        return "delete"
    if code in ("R", "C"):
        return "rename"
    return "modify"


@dataclass(frozen=True, slots=True)
class CycleVerdict:
    """Whether this cycle's publish (ADR-0025) and tag advance (step 7)
    may proceed.

    Two fields, not one, even though they currently always agree: the design doc states them as
    two separate gates in sequence -- "Only once step 4 has spooled every drift patch for this
    cycle, and every one of those patches actually captured what changed" gates step 6, and "Only
    once step 6's publish has completed" gates step 7 -- and keeping them as two fields keeps that
    sequencing visible in the type rather than collapsing it into a single boolean a future change
    could pull back apart incorrectly.
    """

    should_publish: bool
    should_advance_tag: bool


def decide_cycle_outcome(*, spool_write_failed: bool, uncaptured_paths: Sequence[str] = ()) -> CycleVerdict:
    """The gate, as a value, not a side effect (ADR-0025, "Why the gate moved,
    not disappeared").

    Not per-path. The second, superseded reading of this cycle gated publish per drifted path,
    excluding only the stuck ones from that cycle's rsync while the rest of the tree published
    regardless -- found to be incoherent, because `LAST_CHECKOUT` names one commit, and a single
    ref cannot mean "this path at the new commit, that path at the old one." The third reading
    gates the *whole* cycle on the *whole* spool write instead: if durably writing even one entry
    to local disk fails, this cycle's publish does not run and the tag does not advance -- the next
    cycle's overlay-and-diff reproduces the same drift from scratch, against the same,
    still-unmoved `LAST_CHECKOUT`, and retries.

    **Two conditions, not one, and they fail for different reasons.** `spool_write_failed` is a
    local disk write failing: a laptop being off the network is this component's normal condition,
    a local disk write failing is not, so this half of the gate is a correctness guarantee expected
    to survive without ever actually firing in ordinary operation. `uncaptured_paths` is
    `select_spool_entries`'s report of every staged change whose patch did not actually carry what
    changed (`captures_content`), and that is not rare at all -- it is what happens the first time a
    human pastes an image into a note on the phone, which is ordinary Tuesday behaviour, not a
    fault. Both hold the cycle back identically -- no publish, no tag advance, the same drift
    reproduced and retried next cycle -- because both mean the same thing from the gate's point of
    view: something this cycle needs to place in iCloud is not durably and completely captured yet.
    """
    if spool_write_failed or uncaptured_paths:
        return CycleVerdict(should_publish=False, should_advance_tag=False)
    return CycleVerdict(should_publish=True, should_advance_tag=True)
