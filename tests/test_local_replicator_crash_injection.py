"""The crash-injection stateful harness for local-replicator (docs/DESIGN.md §2 item 10, §4 Plane
B): the highest-value deferred testing mechanism named in
/home/coder/.claude/tmp/obsidian-brain/notes/decision-testing-strategy-for-obsidian-tools.md,
implemented here.

**The shape, and why it's coarse.** A synthetic *crash* action is inserted into the operation
alphabet alongside the real ones (a device-side edit, an upstream commit landing on origin, a
cycle, a drain), so Hypothesis can place it anywhere in a generated sequence -- this is what makes
the harness "crash-injection" rather than just "more integration tests." The crash granularity is
component-level -- *between* `cycle.py`'s named steps (before the overlay, before the fetch, before
the checkout-forward, before the publish, before the tag advance), never inside a git or rsync
invocation itself. `research-property-based-testing-practice.md` §4 records the evidenced reason:
Amazon's ShardStore paper found a coarse, component-level crash model catches substantially the
same bugs as an exhaustive syscall-level fault injector, at a fraction of the cost -- and this
project does not mock, so a syscall-level injector here would mean faking git and rsync internals,
exactly the kind of belief-encoding the doctrine rules out.

**How a crash is injected, concretely.** `cycle.py` calls its collaborators (`overlay`, `publish`,
`fetch_origin`, `checkout_forward`, `advance_last_checkout`) by name, imported into its own module
namespace -- already a clean seam, the same one the existing `spool_writer` injection point in
`cycle.py` uses for the same purpose. `_crash_at` replaces one of those names with a stub that
raises before doing any real work, runs exactly one `run_cycle` call, and restores the original
regardless of outcome. Everything cycle.py did *before* the patched call already ran for real,
against real git and real rsync; everything after it never runs at all -- an authentic model of a
process killed at that exact point, not a hand-simulated approximation of one (contrast the
crash-residue property in `test_local_replicator_cycle.py`, which *does* hand-simulate residue,
because that property is about arbitrary starting states rather than the specific control-flow
points a real crash could land on -- the two are complementary, not redundant).

**The invariants, and how they're checked.** All of them are read off real disk state after every
step, never off the model's own bookkeeping of what "should" have happened -- the model exists only
to track which *markers* (unique, greppable content strings) are still owed durability, because
that single fact cannot be re-derived from disk once a marker has been legitimately drained
(docs/DESIGN.md §7 Phase 2: the drainer's stub sink discards on purpose, standing in for a
downstream consumer that doesn't exist yet). Everything else -- whether a path currently matches
the tag, whether a marker appears in the spool -- is read directly off git and the filesystem each
time, the same principle the crash-residue property applies.

**One invariant this harness used to carry was false by design, and its replacement is an extra
obligation rather than a weaker one.** `upstream_content_never_misattributed` asserted that content
which reached iCloud via `publish` is never spooled as drift. That is not achievable and was never
the right claim: steps 6 and 7 cannot be made atomic, so a crash always can leave published content
looking like drift -- and a device that *suppressed* it would be making the judgement
docs/DESIGN.md §1.5 R2 reserves for the server, on a signal (content identical to upstream) that a
human's own edit can produce (ppat/obsidian-tools#36). What replaced it is split in two, so that
retiring the false half cannot quietly take the true half with it:

- **The loss invariant is untouched and absolute** (`durable_or_recoverable`): nothing a human typed
  is ever absent from both iCloud and the spool. It never depended on attribution, and it was
  already a separate `@invariant()` here rather than fused into the misattribution check -- checked
  before writing this, because a loss claim entangled with an attribution claim is exactly how
  retiring the second would have silently retired the first.
- **An observation-completeness invariant is new and additive**
  (`every_spool_entry_records_what_it_observed_about_upstream`): every entry's recorded observation
  must be *correct*, in both directions -- content this harness knows came from upstream must be
  recorded as matching upstream, and content this harness knows a device typed must be recorded as
  not matching. Correctness, established from the harness's own marker vocabulary, not presence of
  the field: "the mark is there" would hold even if the mark were wrong, which is the failure mode
  worth guarding. The two shas each entry carries are checked the same way, against the refs read
  off real git immediately before the cycle that wrote it.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path

import pytest
from conftest import run_git
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,  # pyright: ignore[reportUnknownVariableType] -- hypothesis.stateful lacks full stubs
)

import obsidian_tools.local_replicator.cycle as cycle_module
from obsidian_tools.config import ReplicateConfig
from obsidian_tools.local_replicator.cycle import CycleResult, run_cycle
from obsidian_tools.local_replicator.drainer import drain_once
from obsidian_tools.local_replicator.drift import SpoolEntry
from obsidian_tools.local_replicator.spool import list_spool_files, read_spool_entry, write_spool_entry
from obsidian_tools.local_replicator.tag import read_last_checkout
from obsidian_tools.vault_git.runner import GitRunner

# Two fixed, named paths rather than arbitrary generated ones: the point of a stateful harness is
# exploring *sequence* space (docs/DESIGN.md and the doctrine both call this out as what
# property-based testing earns its keep on), not path-name space -- that's already covered by the
# adversarial-path property in test_local_replicator_drift.py. A small, fixed alphabet keeps every
# generated sequence's outcome tractable to reason about by hand when a failure needs to be read.
_PATHS = ("alpha.md", "beta.md")

# The component-level crash points this harness can land on -- named collaborators cycle.py calls
# by their own module-level names (module docstring, above). `None` (no crash) is a valid draw too,
# handled by the rule itself rather than listed here.
_CRASH_SEAMS = ("overlay", "fetch_origin", "checkout_forward", "publish", "advance_last_checkout")


class _SimulatedCrash(RuntimeError):
    """Raised by a patched collaborator to model a process killed at that exact point -- never
    caught anywhere in `obsidian_tools` itself, only by this harness."""


def _crash_at(seam: str) -> object:
    """Replace `seam` (a name cycle.py imported into its own namespace) with a stub that raises
    before doing any real work. Returns the original callable, which the caller must restore."""
    original = getattr(cycle_module, seam)

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise _SimulatedCrash(seam)

    setattr(cycle_module, seam, _raise)
    return original


def _restore(seam: str, original: object) -> None:
    setattr(cycle_module, seam, original)


def _marker(kind: str) -> str:
    # Distinct, greppable prefixes per origin -- a human-typed edit and an upstream commit must
    # never be confusable with one another when scanning disk for which invariant a piece of
    # content is standing in for.
    return f"{kind}-{uuid.uuid4().hex}"


class CrashInjectionMachine(RuleBasedStateMachine):
    """See this module's own docstring for the full design. One instance = one generated sequence
    (Hypothesis constructs a fresh instance per example, so `__init__` is the right place to build
    a fresh, real git origin and iCloud directory -- the same per-example-isolation the crash-
    residue property needs and gets by building fresh state inside the test body instead of
    relying on a function-scoped fixture, since here there is no pytest fixture injection at all)."""

    def __init__(self) -> None:
        super().__init__()
        self._root = Path(tempfile.mkdtemp(prefix="lr-crash-"))
        self.origin = self._root / "origin.git"
        self.icloud = self._root / "icloud"
        self.icloud.mkdir(parents=True)
        cache_clone_dir = self._root / "cache-clone"

        self.origin.mkdir(parents=True)
        run_git("init", "--bare", "-q", "--initial-branch=main", cwd=self.origin)
        seed_clone = self._root / "seed-clone"
        run_git("clone", "-q", str(self.origin), str(seed_clone), cwd=self._root)
        (seed_clone / "alpha.md").write_text("seed alpha\n")
        (seed_clone / "beta.md").write_text("seed beta\n")
        run_git("add", "-A", cwd=seed_clone)
        run_git(
            "-c",
            "user.name=seed",
            "-c",
            "user.email=seed@example.invalid",
            "commit",
            "-q",
            "-m",
            "seed",
            cwd=seed_clone,
        )
        run_git("push", "-q", "origin", "main", cwd=seed_clone)

        self.config = ReplicateConfig(
            cache_clone_dir=str(cache_clone_dir),
            icloud_vault_dir=str(self.icloud),
            branch="main",
            origin_url=str(self.origin),
            ssh_key_path=str(self._root / "unused-key"),
            ssh_known_hosts_path=str(self._root / "unused-known-hosts"),
            spool_dir=str(self._root / "spool"),
        )
        self.cache_clone_dir = cache_clone_dir
        self.spool_dir = self._root / "spool"
        self.runner = GitRunner(cache_clone_dir / ".git", cache_clone_dir)

        # Model state -- see the module docstring, "The invariants, and how they're checked": only
        # tracks what cannot be re-derived from disk (which markers are still owed durability, and
        # what the most recent marker written to each path was, purely so a later overwrite can
        # tell whether it's superseding a still-raw edit or an already-resolved one).
        self.open_markers: set[str] = set()
        # Every marker a device action has *ever* minted, monotonically growing -- unlike
        # `open_markers` (which shrinks once a marker is superseded or drained), this never
        # forgets one, because `upstream_content_never_misattributed` needs to recognize a device's
        # own handiwork inside an *already-spooled, possibly long-drained* patch (below).
        self._device_markers: set[str] = set()
        self.upstream_markers: set[str] = set()
        # Upstream markers this instance has watched a real `publish` place in the iCloud tree
        # (recorded after every real cycle, below). This is the harness's own ground truth for "this
        # content came from upstream, not from a device", and it is what
        # `every_spool_entry_records_what_it_observed_about_upstream` measures the recorded
        # observation against. Per-marker, not time-windowed: every marker is a fresh uuid
        # (`_marker`, above), so membership is unambiguous however many cycles follow.
        #
        # It replaces a narrower predecessor -- markers seen escaping via the `advance_last_checkout`
        # seam specifically -- which existed only to explain away a defect this harness now
        # positively asserts about. The general fact turned out to be the simpler one to track.
        self._published_upstream_markers: set[str] = set()
        self.last_written_marker: dict[str, str | None] = dict.fromkeys(_PATHS)
        # A path whose content was removed on the device but not yet captured by a comparison --
        # tracked separately from `last_written_marker` because a deletion has no marker text of
        # its own to look for; without this, `tag_never_names_unpublished_content` cannot tell an
        # uncaptured device-side deletion apart from a real ceiling violation (both look like "the
        # tag still names old content that iCloud no longer has").
        self.pending_delete: set[str] = set()

        # Bootstrap: exactly what every real deployment and every existing cycle.py test does
        # before any device activity -- establishes the parked clone and LAST_CHECKOUT. Never
        # crashed: a lost/uninitialized cache re-baselines by design (cycle.py's own docstring,
        # "A missing baseline... re-baselining publishes everything once instead of reporting the
        # whole vault as drifted") -- pre-bootstrap device edits are explicitly out of contract for
        # durability, so keeping bootstrap crash-free avoids conflating that accepted, documented
        # trade-off with a real finding.
        run_cycle(self.config)

    def teardown(self) -> None:
        shutil.rmtree(self._root, ignore_errors=True)

    # --- helpers ------------------------------------------------------------------------------

    def _icloud_content(self, path: str) -> str | None:
        target = self.icloud / path
        return target.read_text() if target.exists() else None

    def _tag_content(self, path: str) -> str | None:
        tag_sha = read_last_checkout(self.runner)
        if tag_sha is None:
            return None
        result = self.runner.run(["show", f"{tag_sha}:{path}"], check=False)
        return result.stdout if result.returncode == 0 else None

    def _spool_text(self) -> str:
        if not self.spool_dir.is_dir():
            return ""
        return "".join(f.read_text() for f in self.spool_dir.glob("*.json"))

    def _record_published_upstream_markers(self) -> None:
        """Whatever upstream content is live in iCloud right now got there via a real `publish` --
        nothing else in this machine's rule set ever writes upstream content to the device
        (`device_write`/`device_rename` always mint a fresh `HUMAN-` marker of their own). Called
        after every real `run_cycle`, crashed or clean, so the record is taken while it is still
        observable: the next publish's `--delete` overwrites it."""
        icloud_text = "".join((self.icloud / p).read_text() for p in _PATHS if (self.icloud / p).exists())
        self._published_upstream_markers |= {m for m in self.upstream_markers if m in icloud_text}

    def _pre_cycle_refs(self) -> tuple[str | None, str | None]:
        """`LAST_CHECKOUT` and the remote-tracking ref as real git holds them *right now* -- read
        immediately before a cycle so the entries it writes can be checked against them
        afterwards. The upstream half is the one that matters: `cycle.py` must record the revision
        it knew going in, not the one its own fetch is about to bring back."""
        return read_last_checkout(self.runner), self.runner.rev_parse_or_none("refs/remotes/origin/main")

    def _assert_entries_written_since(
        self, known_files: set[Path], baseline_before: str | None, upstream_before: str | None
    ) -> None:
        for spool_file in list_spool_files(self.spool_dir):
            if spool_file in known_files:
                continue
            entry = read_spool_entry(spool_file)
            assert entry.baseline_sha == baseline_before, (
                f"entry for {entry.path!r} records baseline {entry.baseline_sha!r}, but the cycle that "
                f"wrote it compared against {baseline_before!r}"
            )
            assert entry.upstream_sha == upstream_before, (
                f"entry for {entry.path!r} records upstream {entry.upstream_sha!r}, but the revision this "
                f"clone knew when the comparison ran was {upstream_before!r} -- recording the post-fetch "
                "revision instead would mark a human's edit as upstream content whenever an agent wrote "
                "the same text upstream in between"
            )

    def _supersede_if_still_raw(self, path: str) -> None:
        """Called before overwriting `path` on the device. If the marker most recently written
        there is still the literal, untouched content sitting in iCloud, no cycle ever compared
        against it -- it never became capturable drift, so overwriting it now is exactly what a
        human editing twice before the Mac wakes does, not a durability violation (see this
        module's docstring and the crash-residue property's own note on why "run twice equals run
        once" is the wrong frame). If iCloud's content has since moved on, the previous marker was
        already resolved through the ordinary capture path and is tracked (or already forgotten)
        independently of this path's current content."""
        marker = self.last_written_marker.get(path)
        if marker is None:
            return
        current = self._icloud_content(path)
        if current is not None and marker in current:
            self.open_markers.discard(marker)

    # --- rules: device-side activity -----------------------------------------------------------

    @rule(path=st.sampled_from(_PATHS), body=st.text(min_size=0, max_size=20))
    def device_write(self, path: str, body: str) -> None:
        self._supersede_if_still_raw(path)
        marker = _marker("HUMAN")
        (self.icloud / path).write_text(f"{marker} {body}\n")
        self.last_written_marker[path] = marker
        self.open_markers.add(marker)
        self._device_markers.add(marker)
        self.pending_delete.discard(path)  # a fresh write supersedes any uncaptured deletion too

    @rule(path=st.sampled_from(_PATHS))
    def device_delete(self, path: str) -> None:
        self._supersede_if_still_raw(path)
        existed = (self.icloud / path).exists()
        (self.icloud / path).unlink(missing_ok=True)
        self.last_written_marker[path] = None
        if existed:
            self.pending_delete.add(path)

    @rule()
    def device_rename(self) -> None:
        src, dst = _PATHS
        source = self.icloud / src
        if not source.exists():
            # Nothing renamed, nothing mutated -- must not call `_supersede_if_still_raw` below for
            # either path: doing so unconditionally (before knowing whether a rename will actually
            # happen) was an earlier, wrong version of this rule -- it superseded `dst`'s still-open
            # marker on a call that never touched `dst` at all, since the check ran before the
            # existence guard rather than only immediately before an actual mutation.
            return
        self._supersede_if_still_raw(src)
        self._supersede_if_still_raw(dst)
        marker = _marker("HUMAN")
        content = f"{marker} {source.read_text()}"
        source.unlink()
        (self.icloud / dst).write_text(content)
        self.last_written_marker[src] = None
        self.pending_delete.add(src)
        self.last_written_marker[dst] = marker
        self.open_markers.add(marker)
        self._device_markers.add(marker)
        self.pending_delete.discard(dst)

    # --- rules: upstream activity (the git-committer / an agent pushing to origin) --------------

    @rule(path=st.sampled_from(_PATHS))
    def upstream_commit(self, path: str) -> None:
        marker = _marker("UPSTREAM")
        clone = self._root / f"upstream-clone-{uuid.uuid4().hex}"
        run_git("clone", "-q", str(self.origin), str(clone), cwd=self._root)
        (clone / path).write_text(f"{marker}\n")
        run_git("add", "-A", cwd=clone)
        run_git(
            "-c", "user.name=agent", "-c", "user.email=agent@example.invalid", "commit", "-q", "-m", "update", cwd=clone
        )
        run_git("push", "-q", "origin", "main", cwd=clone)
        self.upstream_markers.add(marker)

    # --- rules: the cycle itself, clean and crashed ----------------------------------------------

    @rule()
    def run_cycle_clean(self) -> None:
        known_files = set(list_spool_files(self.spool_dir))
        baseline_before, upstream_before = self._pre_cycle_refs()
        result = run_cycle(self.config)
        self._record_published_upstream_markers()
        self._assert_entries_written_since(known_files, baseline_before, upstream_before)
        self._resolve_captured_markers(tag_advanced=result.tag_advanced)

    @rule(seam=st.sampled_from(_CRASH_SEAMS))
    def run_cycle_crashed(self, seam: str) -> None:
        """Ask for a crash at `seam`. Two outcomes are both legitimate, and the model must treat
        both as real transitions with their own consequences -- not assume the first and fail the
        harness itself when the second happens: the seam is reached and raises `_SimulatedCrash`
        (`crashed` below), or the whole-cycle uncaptured-content gate (`drift.decide_cycle_outcome`)
        withholds the cycle *before* the seam is ever reached, so the patched collaborator is never
        called and `run_cycle` returns normally instead. The latter is not a failure to inject: a
        NUL byte in `device_write`'s generated text makes git treat the change as binary, and the
        gate withholding a binary drift patch is exactly what it exists to do (the same gate a
        pasted image trips for real). Only `publish`/`advance_last_checkout` sit behind that gate
        (cycle.py's own step order: `overlay`/`fetch_origin`/`checkout_forward` are unconditional),
        so the gate-withheld outcome is only ever legitimate for those two seams, and only when the
        gate's own stated reason -- `result.uncaptured` non-empty -- is actually present. Anything
        else not raising is still a genuine disagreement between this harness and the code, and
        still fails loudly."""
        known_files = set(list_spool_files(self.spool_dir))
        baseline_before, upstream_before = self._pre_cycle_refs()
        original = _crash_at(seam)
        crashed = False
        result: CycleResult | None = None
        try:
            try:
                result = run_cycle(self.config)
            except _SimulatedCrash:
                crashed = True
        finally:
            _restore(seam, original)

        # Taken before the recovery cycle below, while it is still observable: a crash at
        # `advance_last_checkout` means `publish` (step 6, strictly earlier in cycle.py's own step
        # order) already ran for real, so whatever upstream content sits in iCloud now was placed
        # there by that publish. The recovery cycle's own publish would overwrite the evidence.
        self._record_published_upstream_markers()
        self._assert_entries_written_since(known_files, baseline_before, upstream_before)

        if crashed:
            # A crash always means the tag never advances: `_crash_at` replaces the collaborator
            # with a stub that raises before doing any real work, including
            # `advance_last_checkout`'s own -- never `tag_advanced=True` for a call that raised
            # `_SimulatedCrash` before completing.
            self._resolve_captured_markers(tag_advanced=False)
        else:
            assert seam in ("publish", "advance_last_checkout"), (
                f"expected a crash at {seam!r}, but the cycle completed normally instead -- this "
                "seam sits ahead of the uncaptured-content gate (cycle.py's own step order), so "
                "nothing legitimate should ever let it go unreached"
            )
            assert result is not None  # `run_cycle` always returns or raises; never both omitted
            assert result.uncaptured, (
                f"expected a crash at {seam!r}, but the cycle completed normally without the one "
                "legitimate reason that seam can go unreached -- drift.decide_cycle_outcome "
                f"withholding the cycle on uncaptured content: result={result!r}"
            )
            # The gate withheld the whole cycle -- real disk state (`_resolve_captured_markers`'s
            # own per-path loop) and `result.tag_advanced` (necessarily `False`, since the gate
            # ties `should_publish`/`should_advance_tag` together -- see `decide_cycle_outcome`)
            # already reflect that nothing was published and the tag did not move, so this needs
            # no different handling from an ordinary gated `run_cycle_clean`.
            self._resolve_captured_markers(tag_advanced=result.tag_advanced)

        # Invariant 3, checked here rather than as a standalone @invariant: "the next cycle after
        # any crash-or-withheld cycle completes normally" -- a gate that pauses the cycle must
        # never wedge it (docs/DESIGN.md's own framing, carried into this harness's brief).
        recovery_known_files = set(list_spool_files(self.spool_dir))
        recovery_baseline, recovery_upstream = self._pre_cycle_refs()
        recovery_result = run_cycle(self.config)
        self._record_published_upstream_markers()
        self._assert_entries_written_since(recovery_known_files, recovery_baseline, recovery_upstream)
        self._resolve_captured_markers(tag_advanced=recovery_result.tag_advanced)

    def _resolve_captured_markers(self, *, tag_advanced: bool) -> None:
        """After any real `run_cycle` call (crashed or not), a marker that was raw-in-iCloud and
        is no longer the live content at its path has been captured -- it now lives in the spool
        (or, if publish also ran, only in the spool, since publish overwrites iCloud from `main`,
        which never includes device edits in Phase 2 -- see cycle.py's own module docstring). This
        does not need to know *why* -- INV1 below checks that capture actually landed somewhere,
        directly off disk; this only stops treating a path as "still raw" once it demonstrably
        isn't, so `_supersede_if_still_raw` doesn't misfire on a future edit to the same path. Real
        disk content, not the cycle's own outcome, is the right signal for this half: a path is
        free for a fresh edit the moment its raw content is gone, whether that's because publish
        overwrote it or because a comparison captured it into the spool -- durability is INV1's job,
        not this bookkeeping's.

        A pending deletion has no marker text to look for, so it can't be resolved the same way --
        and, unlike the loop above, resolving it needs the cycle's own outcome, not just disk
        content: `tag_never_names_unpublished_content` treats `pending_delete` as "the tag hasn't
        caught up to this deletion yet," and the tag only catches up when this cycle actually
        advanced it. A comparison can capture a deletion into the spool -- durably, real content
        gone from iCloud -- while the *whole-cycle* gate (`drift.decide_cycle_outcome`) still
        withholds publish and the tag advance over an unrelated path's uncaptured content; the
        deletion is durable, but the tag still names the pre-deletion commit, so it is still
        "pending" by this invariant's own definition. Clearing on anything less than
        `tag_advanced` was the bug: a comparison happening is necessary but not sufficient for the
        tag to have caught up."""
        for path in _PATHS:
            marker = self.last_written_marker.get(path)
            if marker is None:
                continue
            current = self._icloud_content(path)
            if current is None or marker not in current:
                self.last_written_marker[path] = None
        if tag_advanced:
            self.pending_delete.clear()

    # --- rules: the drainer -----------------------------------------------------------------------

    @rule()
    def drain(self) -> None:
        # Phase 2's discard sink is a real, permanent forgetting (docs/DESIGN.md §7 Phase 2) --
        # scan what's about to be discarded *before* draining, so open_markers can be resolved for
        # exactly the markers this call actually consumes, not guessed at.
        spool_text_before = self._spool_text()
        drained_markers = {m for m in self.open_markers if m in spool_text_before}
        drain_once(self.spool_dir)
        self.open_markers -= drained_markers

    # --- invariants, checked after every rule, always off real disk state -----------------------

    @invariant()
    def durable_or_recoverable(self) -> None:
        """The component's entire reason to exist (docs/DESIGN.md §4 Plane B): anything a human
        typed is present in the iCloud tree or recoverable from the spool -- never absent from
        both."""
        icloud_text = "".join((self.icloud / p).read_text() for p in _PATHS if (self.icloud / p).exists())
        spool_text = self._spool_text()
        for marker in self.open_markers:
            assert marker in icloud_text or marker in spool_text, (
                f"{marker!r} is durable nowhere: not in iCloud, not in the spool"
            )

    @invariant()
    def every_spool_entry_records_what_it_observed_about_upstream(self) -> None:
        """Every spooled entry's recorded observation is *correct* -- in both directions, measured
        against this harness's own ground truth rather than against the entry's own say-so
        (ppat/obsidian-tools#36; see this module's docstring for why this replaced an invariant
        that was false by design, and why it is an added obligation rather than a relaxed one).

        **Direction one: content a publish placed there is recorded as matching upstream.** Nothing
        in this machine's rule set writes upstream content into iCloud except `publish`
        (`_record_published_upstream_markers`), so a marker in that set, appearing in a spooled
        patch that carries no device marker of its own, is upstream content the device re-detected
        as drift. The entry must say so, because that observation is the only thing standing
        between it and Phase 5 stamping `authority: human` on an agent's own words. The entry still
        *exists* -- that is the point, and the previous version of this invariant forbade it.

        **Direction two, and the expensive one to get wrong: a device's own content is never
        recorded as matching upstream.** Every device-authored create, modify and rename writes a
        fresh `HUMAN-` uuid into its content (`_marker`), which no upstream commit can contain, so
        such an entry can never be byte-identical to upstream and must never be recorded as if it
        were -- an entry wrongly carrying that observation is an edit Phase 5 could discard as
        crash residue. `_device_markers` (every `HUMAN-` marker ever minted) rather than
        `open_markers`, which shrinks once a marker is superseded or drained and would stop
        recognizing a device's own already-drained handiwork. Note the harness-specific step: it is
        the *uniqueness of the markers* that makes "device-authored" imply "differs from upstream"
        here. In the real world a human can reproduce upstream byte-for-byte by coincidence, and
        `matches_upstream: true` would then be a correct observation of that.

        A delete entry's patch shows whatever content the path *had*, not what a device wrote, so
        direction one cannot read it: it is exempt structurally rather than by inference, because
        this rule set never gives upstream content a way to be deleted (`upstream_commit` only ever
        writes), so a delete entry can only originate from `device_delete`/`device_rename`.
        Direction two still applies to it, and is where a device's deletion of a note upstream
        still holds would be caught being recorded as upstream residue."""
        for spool_file in list_spool_files(self.spool_dir):
            entry = read_spool_entry(spool_file)
            assert entry.baseline_sha is not None, f"entry for {entry.path!r} records no baseline it compared against"
            assert entry.upstream_sha is not None, (
                f"entry for {entry.path!r} records no upstream revision, but this machine's clone has known one "
                "since bootstrap"
            )
            if any(device_marker in entry.patch for device_marker in self._device_markers):
                assert entry.matches_upstream is False, (
                    f"entry for {entry.path!r} (kind={entry.kind!r}) carries a device-authored marker in "
                    f"its own patch, so its content cannot be upstream's -- yet it records "
                    f"matches_upstream={entry.matches_upstream!r}, which Phase 5 could act on by "
                    "discarding a real human edit"
                )
                continue
            if entry.kind == "delete":
                continue
            for marker in self._published_upstream_markers:
                if marker not in entry.patch:
                    continue
                assert entry.matches_upstream is True, (
                    f"upstream content {marker!r} -- placed in iCloud by a real publish -- was spooled as "
                    f"drift (entry: {entry.path!r}, kind={entry.kind!r}) recording "
                    f"matches_upstream={entry.matches_upstream!r}. Spooling it is correct; failing to "
                    "record the one observation that lets drift-processor refuse it `authority: human` "
                    "is ppat/obsidian-tools#36"
                )

    @invariant()
    def tag_never_names_unpublished_content(self) -> None:
        """`LAST_CHECKOUT` never names a commit whose content was not actually published
        (docs/DESIGN.md §4 Plane B: the tag is defined as "byte-identical to what was last placed
        in iCloud"). A mismatch between the tag's tree and iCloud's current content is only
        legitimate when it's explained by a device edit that hasn't been captured by a cycle yet --
        never by the tag racing ahead of what publish actually wrote."""
        for path in _PATHS:
            tag_content = self._tag_content(path)
            icloud_content = self._icloud_content(path)
            if tag_content == icloud_content:
                continue
            marker = self.last_written_marker.get(path)
            explained = (marker is not None and marker in (icloud_content or "") and marker in self.open_markers) or (
                icloud_content is None and path in self.pending_delete
            )
            assert explained, (
                f"LAST_CHECKOUT content for {path!r} does not match iCloud, and isn't explained by "
                f"an uncaptured device edit -- tag={tag_content!r} icloud={icloud_content!r}"
            )


@pytest.mark.slow  # real git/rsync per step; see .github/workflows/test.yaml for where this runs
def test_crash_injection_state_machine() -> None:
    """No `xfail` wrapper any more, and its removal is deliberate rather than incidental. The
    machinery it used -- `KnownAdvanceLastCheckoutCrashDefect`, a type-keyed classifier, and the
    per-marker bookkeeping that fed it -- existed for exactly one defect
    (ppat/obsidian-tools#36), and that defect's *symptom* is now asserted about rather than
    tolerated: the residue is still spooled, and the observation that makes it harmless is now
    required to be present and correct. A suppression left standing here would catch a regression
    of that same defect and report it as an expected failure, which is the one outcome worse than
    not testing for it at all."""
    run_state_machine_as_test(
        CrashInjectionMachine,
        settings=hypothesis_settings(deadline=None, stateful_step_count=12),
    )


# --- proofs that the observation invariant is real, in both of its directions ---------------------
#
# The first drives `CrashInjectionMachine`'s own real rules -- the actual #36 sequence, not a
# hand-simulation of it -- and is the regression test for the fix. The other two inject a violation
# directly into the spool the invariant reads, because neither direction can be provoked through the
# rules against correct code: an invariant that has never been watched fail is an invariant nobody
# has evidence about (the same reason the mutation table in this change's PR body exists).


def test_a_crash_between_publish_and_the_tag_advance_leaves_correctly_annotated_drift() -> None:
    """The #36 regression test, at the harness's own level. The republished content is spooled --
    the device drops nothing -- and it carries the observation that lets `drift-processor` refuse to
    stamp it `authority: human`."""
    machine = CrashInjectionMachine()
    try:
        machine.upstream_commit("alpha.md")
        marker = next(iter(machine.upstream_markers))
        machine.run_cycle_crashed("advance_last_checkout")

        machine.every_spool_entry_records_what_it_observed_about_upstream()
        machine.durable_or_recoverable()

        annotated = [
            entry
            for spool_file in list_spool_files(machine.spool_dir)
            for entry in [read_spool_entry(spool_file)]
            if marker in entry.patch
        ]
        assert annotated, "the republished content must still be submitted, not suppressed"
        assert all(entry.matches_upstream is True for entry in annotated)
        assert all(entry.baseline_sha != entry.upstream_sha for entry in annotated)
    finally:
        machine.teardown()


def test_the_observation_invariant_fails_when_upstream_content_is_recorded_as_a_device_edit() -> None:
    """Direction one, watched failing. An entry carrying content a publish placed in iCloud, but
    recording `matches_upstream=False`, is precisely the pre-fix behaviour, and the invariant must
    go red on it rather than accept the entry because a mark of *some* value is present."""
    machine = CrashInjectionMachine()
    try:
        machine.upstream_commit("alpha.md")
        marker = next(iter(machine.upstream_markers))
        machine.run_cycle_crashed("advance_last_checkout")
        assert marker in machine._published_upstream_markers  # pyright: ignore[reportPrivateUsage]

        _replace_spool_with(machine, _entry_carrying(marker, matches_upstream=False))

        with pytest.raises(AssertionError, match="ppat/obsidian-tools#36"):
            machine.every_spool_entry_records_what_it_observed_about_upstream()
    finally:
        machine.teardown()


def test_the_observation_invariant_fails_when_a_device_edit_is_recorded_as_upstream_content() -> None:
    """Direction two, watched failing -- the direction that loses data if it is ever wrong, since a
    device edit recorded as matching upstream is one Phase 5 could discard as crash residue."""
    machine = CrashInjectionMachine()
    try:
        machine.device_write("alpha.md", "typed by a human")
        marker = next(iter(machine._device_markers))  # pyright: ignore[reportPrivateUsage]

        _replace_spool_with(machine, _entry_carrying(marker, matches_upstream=True))

        with pytest.raises(AssertionError, match="discarding a real human edit"):
            machine.every_spool_entry_records_what_it_observed_about_upstream()
    finally:
        machine.teardown()


def _entry_carrying(marker: str, *, matches_upstream: bool) -> SpoolEntry:
    return SpoolEntry(
        kind="modify",
        path="alpha.md",
        old_path=None,
        patch=f"diff --git a/alpha.md b/alpha.md\n@@ -1 +1 @@\n-old\n+{marker}\n",
        baseline_sha="1111111111111111111111111111111111111111",
        upstream_sha="2222222222222222222222222222222222222222",
        matches_upstream=matches_upstream,
    )


def _replace_spool_with(machine: CrashInjectionMachine, entry: SpoolEntry) -> None:
    """Clear whatever the rules above legitimately spooled and leave exactly the injected entry, so
    the invariant's verdict is unambiguously about it."""
    for spool_file in list_spool_files(machine.spool_dir):
        spool_file.unlink()
    write_spool_entry(machine.spool_dir, entry)
