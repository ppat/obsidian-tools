"""Pure decisions for local-replicator's replication cycle (docs/DESIGN.md §2 item 10, §4 Plane
B; ppat/obsidian-tools#3): what a staged git change becomes as a spool entry, and whether a
cycle's publish and tag advance may proceed. No filesystem, no git, no rsync -- `cycle.py` gathers
the state (git plumbing, rsync) and calls these; every safety rule lives here exactly once, the
same split `vault_git/baseline_selector.py` uses over `baseline.py`, and for the same reason -- see
that module's docstring for what happens when a safety rule instead lives inline in the code that
produces the data it judges.

**Why this exists as its own module, not inlined in cycle.py.** The third reading of this cycle
(docs/DESIGN.md §4 Plane B, "Why the gate moved, not disappeared") replaced two intertwined
mechanisms -- a path enumeration and a separate per-path content read, gated per path -- with one:
checking out the baseline and overlaying the device tree turns "which paths changed, and what do
they now contain" into a single `git diff`. Once the comparison collapses to one mechanism, the
decisions riding on it collapse too, to two pure questions this module answers: given a staged git
change, what goes in the spool (`select_spool_entries`); and given whether every entry made it into
the spool durably, does this cycle publish and advance the tag (`decide_cycle_outcome`). Neither
question needs a filesystem to answer, so neither gets one -- which is what makes both cheap to test
against hand-built, adversarial input rather than only against real git repositories one case at a
time.
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
    `git diff` shows nothing at all for an untracked path (docs/DESIGN.md §4 Plane B,
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
class SpoolEntry:
    """One drift patch, ready to be written to the local spool (spool.py) -- the unit
    docs/DESIGN.md §2 item 10 step 4 calls "each drift patch." `kind` restates `StagedChange`'s raw
    git status in a stable, small vocabulary a downstream consumer can classify on without knowing
    git's status-letter conventions (docs/DESIGN.md §1.5 R2's *shape* heuristic -- an append reads
    differently from a rewrite -- needs exactly this plus the patch text itself, once a real
    consumer exists at Phase 5)."""

    kind: SpoolEntryKind
    path: str
    old_path: str | None
    patch: str


# `git diff` emits this line *instead of* a hunk body when either side of a change is binary, so
# the resulting patch describes that something changed without carrying a single byte of what.
# Matched as a substring of the patch rather than by sniffing the path's extension: what makes a
# capture incomplete is what git actually produced, not what the filename suggests it should have.
_BINARY_PATCH_MARKER = "\nBinary files "


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
    no hunk body either, but the header describes the change completely, so nothing is lost.
    Keying on git's binary marker rather than on "has a hunk" is what keeps that case captured.

    **This is not a new policy call.** The vault receives markdown only, images are deferred past
    the first pass, and `_attachments/` is a committed placeholder with nothing in it yet
    (docs/DESIGN.md Sec 5, Sec 8b G5) -- so a binary here is out of contract. What to do about
    out-of-contract input is likewise already settled, by DESIGN.md's "Fail loud, destroy nothing":
    every component defaults to that posture when it meets something it cannot reconcile. Silently
    deleting the bytes is the one response that policy rules out.
    """
    return _BINARY_PATCH_MARKER not in change.patch


@dataclass(frozen=True, slots=True)
class SpoolSelection:
    """What a cycle's staged changes decompose into: entries safe to spool, and paths whose
    capture came back without content.

    Two lists rather than a filtered one, because dropping the second silently is exactly what
    docs/DESIGN.md Sec 1.5 R2 forbids -- "it submits every drift patch... and makes no judgement, so
    it can never silently drop a real edit." `uncaptured` is not a judgement about whether the
    edit mattered; it is a statement that this component could not capture it, which the cycle
    then treats as a capture failure rather than as permission to proceed.
    """

    entries: list[SpoolEntry]
    uncaptured: list[str]


def select_spool_entries(changes: Sequence[StagedChange]) -> SpoolSelection:
    """The one place a staged git change becomes a spool entry.

    One entry per input change -- nothing is merged, split, or dropped. That is what lets the
    device-side detector stay "dumb" (docs/DESIGN.md §1.5 R2: "it submits every drift patch...
    and makes no judgement, so it can never silently drop a real edit"): filtering anything out
    here -- `.obsidian/` drift included -- would be a device-side judgement call this design
    deliberately reserves for Phase 5's server-side classifier, not for this component.

    Sorted by path for deterministic spool ordering -- the spool itself has no other concept of
    "this cycle's batch" once entries are written (spool.py), so a stable order is what makes a
    written spool directory reproducible from the same drift, run to run.
    """
    entries = [
        SpoolEntry(kind=_classify(change.status), path=change.path, old_path=change.old_path, patch=change.patch)
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
    """Whether this cycle's publish (docs/DESIGN.md §2 item 10 step 6) and tag advance (step 7)
    may proceed.

    Two fields, not one, even though they currently always agree: the design doc states them as
    two separate gates in sequence -- "Only once step 4 has spooled every drift patch for this
    cycle" gates step 6, and "Only once step 6's publish has completed" gates step 7 -- and keeping
    them as two fields keeps that sequencing visible in the type rather than collapsing it into a
    single boolean a future change could pull back apart incorrectly.
    """

    should_publish: bool
    should_advance_tag: bool


def decide_cycle_outcome(*, spool_write_failed: bool, uncaptured_paths: Sequence[str] = ()) -> CycleVerdict:
    """The gate, as a value, not a side effect (docs/DESIGN.md §4 Plane B, "Why the gate moved,
    not disappeared").

    Not per-path. The second, superseded reading of this cycle gated publish per drifted path,
    excluding only the stuck ones from that cycle's rsync while the rest of the tree published
    regardless -- found to be incoherent, because `LAST_CHECKOUT` names one commit, and a single
    ref cannot mean "this path at the new commit, that path at the old one." The third reading
    gates the *whole* cycle on the *whole* spool write instead: if durably writing even one entry
    to local disk fails, this cycle's publish does not run and the tag does not advance -- the next
    cycle's overlay-and-diff reproduces the same drift from scratch, against the same,
    still-unmoved `LAST_CHECKOUT`, and retries. A laptop being off the network is this component's
    normal condition; a local disk write failing is not -- so this gate is a correctness guarantee
    that is expected to survive without ever actually firing in ordinary operation.
    """
    if spool_write_failed or uncaptured_paths:
        return CycleVerdict(should_publish=False, should_advance_tag=False)
    return CycleVerdict(should_publish=True, should_advance_tag=True)
