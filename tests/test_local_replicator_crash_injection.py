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

**The invariants, and how they're checked.** All three are read off real disk state after every
step, never off the model's own bookkeeping of what "should" have happened -- the model exists only
to track which *markers* (unique, greppable content strings) are still owed durability, because
that single fact cannot be re-derived from disk once a marker has been legitimately drained
(docs/DESIGN.md §7 Phase 2: the drainer's stub sink discards on purpose, standing in for a
downstream consumer that doesn't exist yet). Everything else -- whether a path currently matches
the tag, whether a marker appears in the spool -- is read directly off git and the filesystem each
time, the same principle the crash-residue property applies.
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


class KnownAdvanceLastCheckoutCrashDefect(Exception):
    """Raised only for the one known, unfixed defect this harness reproduces --
    ppat/obsidian-tools#36: a crash landing between `publish` (step 6) and `advance_last_checkout`
    (step 7) leaves already-published upstream content sitting in iCloud while `LAST_CHECKOUT`
    still names the pre-publish commit, so the next cycle's comparison reads that content as fresh
    device drift and spools it, misattributed.

    Deliberately a distinct exception type -- not a plain `AssertionError` caught downstream and
    pattern-matched on its message text -- and deliberately *not* a subclass of `AssertionError`,
    so the two can never be confused by an `isinstance`/`except` check anywhere, including in this
    module's own tests. `upstream_content_never_misattributed` (below) raises this only for a
    marker it can trace, via `CrashInjectionMachine._advance_crash_explained_markers`, to a real
    crash at the `advance_last_checkout` seam -- every other misattribution it finds raises a plain
    `AssertionError` instead, which `test_crash_injection_state_machine`'s own classifier
    (`_is_only_the_known_advance_last_checkout_crash_defect`) does not match, so it fails the suite
    rather than being silently absorbed alongside this one. See
    `test_a_different_misattribution_is_not_absorbed_by_the_known_defect_xfail` and
    `test_the_known_advance_last_checkout_crash_defect_is_classified_not_generic`, below, for the
    two-sided proof that this actually holds -- an earlier version of this harness classified on a
    substring of the assertion message alone (`"was spooled as device drift"`), which a genuinely
    different defect tripping the same invariant would have matched too, silently xfailing
    alongside the known one. Found during a rebase's own mutation-testing pass, not by design."""


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
        self.upstream_markers: set[str] = set()
        # Upstream markers this instance has *itself* watched reach iCloud via a real crash at the
        # `advance_last_checkout` seam (populated in `run_cycle_crashed`, below) -- the only markers
        # `upstream_content_never_misattributed` is allowed to explain away as the known defect
        # (ppat/obsidian-tools#36) rather than fail loudly on. Per-marker, not time-windowed: every
        # marker is a fresh uuid (`_marker`, above), so membership here is unambiguous regardless of
        # how many crashes or cycles happen afterward.
        self._advance_crash_explained_markers: set[str] = set()
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
        result = run_cycle(self.config)
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

        if crashed:
            if seam == "advance_last_checkout":
                # `publish` (step 6) already ran for real before this crash -- it's strictly
                # earlier in cycle.py's own step order than `advance_last_checkout` (step 7), so
                # everything it wrote to iCloud is real, not simulated. Whatever upstream content
                # is live in iCloud right now got there via that publish, not via a device edit --
                # record it so `upstream_content_never_misattributed` can trace a later
                # misattribution of it back to this exact, known defect (ppat/obsidian-tools#36)
                # rather than treat it as a new one.
                icloud_text = "".join((self.icloud / p).read_text() for p in _PATHS if (self.icloud / p).exists())
                self._advance_crash_explained_markers |= {m for m in self.upstream_markers if m in icloud_text}

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
        recovery_result = run_cycle(self.config)
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
    def upstream_content_never_misattributed(self) -> None:
        """A narrower, harness-specific corollary of the tag/publish ordering
        (docs/DESIGN.md §4 Plane B, "Why the gate moved, not disappeared"): content that reached
        iCloud purely via `publish` -- never typed on a device -- must never be spooled as if a
        human wrote it. Present in iCloud is fine and expected (that's what publish is for); present
        in the *spool* would mean a later cycle re-diffed already-published content as fresh
        device drift.

        Raises `KnownAdvanceLastCheckoutCrashDefect`, not a plain `AssertionError`, when (and only
        when) the misattributed marker is one this instance itself watched reach iCloud via a real
        crash at the `advance_last_checkout` seam (`_advance_crash_explained_markers`, populated in
        `run_cycle_crashed`) -- ppat/obsidian-tools#36, the one known, unfixed defect this harness
        reproduces. Any *other* misattribution -- a marker never seen leaving via that seam -- is a
        different defect and raises a plain `AssertionError`, which
        `test_crash_injection_state_machine`'s classifier does not match, so it fails the suite
        rather than xfailing."""
        spool_text = self._spool_text()
        for marker in self.upstream_markers:
            if marker not in spool_text:
                continue
            if marker in self._advance_crash_explained_markers:
                raise KnownAdvanceLastCheckoutCrashDefect(
                    f"upstream content {marker!r} was spooled as device drift -- explained by "
                    "ppat/obsidian-tools#36 (a crash at the advance_last_checkout seam left this "
                    "marker published in iCloud while LAST_CHECKOUT still named the pre-publish "
                    "commit)"
                )
            raise AssertionError(
                f"upstream content {marker!r} was spooled as device drift, and is NOT explained by "
                "the known advance_last_checkout-seam crash defect (ppat/obsidian-tools#36) -- this "
                "is a different, previously-unseen defect"
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


def _is_only_the_known_advance_last_checkout_crash_defect(exc: BaseException) -> bool:
    """Classifies on exception *type* (`KnownAdvanceLastCheckoutCrashDefect`,
    ppat/obsidian-tools#36), not on a substring of the assertion message. A prior version of this
    function matched any exception whose message contained `"was spooled as device drift"` --
    that string is `upstream_content_never_misattributed`'s own wording for *any* misattribution,
    known cause or not, so a genuinely different defect tripping the same invariant would have
    matched it too and been silently xfailed alongside the known one. Type-based dispatch can't
    make that mistake: `upstream_content_never_misattributed` (above) only ever constructs
    `KnownAdvanceLastCheckoutCrashDefect` for a marker it can trace to a real crash at the
    `advance_last_checkout` seam; everything else it raises is a plain `AssertionError`, which this
    function does not match. See `test_a_different_misattribution_is_not_absorbed_by_the_known_defect_xfail`
    and `test_the_known_advance_last_checkout_crash_defect_is_classified_not_generic`, below, for
    the two-sided proof."""
    # Bare `ExceptionGroup` (no type parameter) narrows `.exceptions` to `tuple[Unknown, ...]`
    # under strict pyright -- there's no narrower type to give it here, since Hypothesis's own
    # multi-bug reporting groups arbitrary, heterogeneous exception types together.
    if isinstance(exc, ExceptionGroup):
        return all(
            _is_only_the_known_advance_last_checkout_crash_defect(sub)  # pyright: ignore[reportUnknownArgumentType]
            for sub in exc.exceptions  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        )
    return isinstance(exc, KnownAdvanceLastCheckoutCrashDefect)


@pytest.mark.slow  # real git/rsync per step; see .github/workflows/test.yaml for where this runs
def test_crash_injection_state_machine() -> None:
    try:
        run_state_machine_as_test(
            CrashInjectionMachine,
            settings=hypothesis_settings(deadline=None, stateful_step_count=12),
        )
    except BaseException as exc:  # deliberately broad; re-raised below unless matched
        if _is_only_the_known_advance_last_checkout_crash_defect(exc):
            pytest.xfail(f"known, unfixed defect, ppat/obsidian-tools#36: {exc}")
        raise


# --- the two-sided proof that the classification above doesn't over- or under-generalize --------
#
# Both drive `CrashInjectionMachine`'s own real rules and invariant directly (not a reimplementation
# of the classification logic, and not the full Hypothesis search, which would make either an
# unreliable, slow way to pin down one specific case) -- constructed by hand precisely because each
# is checking a single, specific scenario, the same reason the two crash-recovery examples in
# `test_local_replicator_cycle.py` predate that property's own generalization.


def test_the_known_advance_last_checkout_crash_defect_is_classified_not_generic() -> None:
    """The positive half: a misattribution actually caused by a real crash at the
    `advance_last_checkout` seam -- driven through the harness's own `run_cycle_crashed` rule, not
    hand-simulated -- is classified as `KnownAdvanceLastCheckoutCrashDefect` (ppat/obsidian-tools#36),
    not a plain `AssertionError`."""
    machine = CrashInjectionMachine()
    try:
        machine.upstream_commit("alpha.md")
        machine.run_cycle_crashed("advance_last_checkout")

        with pytest.raises(KnownAdvanceLastCheckoutCrashDefect):
            machine.upstream_content_never_misattributed()
    finally:
        machine.teardown()


def test_a_different_misattribution_is_not_absorbed_by_the_known_defect_xfail() -> None:
    """The negative half, and the one that actually matters: a misattribution caused by something
    *other* than a crash at the `advance_last_checkout` seam must not be classified as the known
    defect, so `test_crash_injection_state_machine`'s own except-clause would re-raise it rather
    than xfail it.

    Simulates a hypothetical, different bug -- upstream content reaching iCloud by some path this
    harness's sanctioned crash mechanism never touched -- by writing the upstream commit's own
    content directly into iCloud, bypassing `run_cycle_crashed` entirely. `run_cycle_clean` then
    diffs it as ordinary drift and spools it, exactly like the real defect's symptom, but reached
    by a different route that `_advance_crash_explained_markers` never recorded."""
    machine = CrashInjectionMachine()
    try:
        machine.upstream_commit("alpha.md")
        marker = next(iter(machine.upstream_markers))
        explained = machine._advance_crash_explained_markers  # pyright: ignore[reportPrivateUsage]
        assert marker not in explained  # never crashed at that seam yet

        (machine.icloud / "alpha.md").write_text(f"{marker}\n")
        machine.run_cycle_clean()

        with pytest.raises(AssertionError) as excinfo:
            machine.upstream_content_never_misattributed()
        assert not isinstance(excinfo.value, KnownAdvanceLastCheckoutCrashDefect)
        assert not _is_only_the_known_advance_last_checkout_crash_defect(excinfo.value)
    finally:
        machine.teardown()
