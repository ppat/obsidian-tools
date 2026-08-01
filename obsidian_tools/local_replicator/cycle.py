"""The replication cycle: park at the baseline, overlay the device tree, diff, spool, reset,
pull, publish, advance -- in that order, because the order is load-bearing (docs/DESIGN.md §2 item
10, §4 Plane B; ppat/obsidian-tools#3).

**Why comparison and capture precede the pull.** Whatever currently sits in iCloud was placed
there by the last successful publish -- exactly `LAST_CHECKOUT`. The human's edits were made
against *that*. The diff is only meaningful while it is still the live comparison: pulling first
would bring new upstream content in, and the subsequent overlay would then be comparing iCloud
against a baseline it was never actually checked out against, mixing upstream change and human
drift into one diff with no way to separate them.

**The one-parked-clone simplification.** A previous design sketch used a second local checkout
pinned at `LAST_CHECKOUT` purely to hold the comparison baseline, separate from the clone being
pulled forward. That component doesn't need to exist: if the single cache clone simply *stays
parked at LAST_CHECKOUT between cycles*, it already *is* the baseline -- compare iCloud against it,
spool the drift, pull it forward, publish, advance the tag, and it's parked at the new
`LAST_CHECKOUT`, ready for the next cycle. `clone.py` and `tag.py` exist to make exactly that true.

**Git is the drift engine, and the gate now sits on two conditions ahead of publish, not on publish
itself.** Checking out `LAST_CHECKOUT` and overlaying the device tree onto it turns "which paths
changed, and what do they now contain" into one `git diff` -- a patch for a path that already
existed at the baseline, and the whole file (once staged, since an unstaged `git diff` shows
nothing for an untracked path) for a path created fresh on the device. Each patch is spooled
atomically before anything below discards the overlay that produced it
(`obsidian_tools.local_replicator.spool`). Publish (step 6) and the tag advance (step 7) proceed
only once every staged change clears both: it made it into the spool durably, and its patch
actually captures what changed (`obsidian_tools.local_replicator.drift.captures_content`) -- not,
as an earlier reading of this cycle had it, gated *per path* on capturing that path's content
before an otherwise-unconditional publish. A single `LAST_CHECKOUT` ref cannot mean "this path at
the new commit, that path at the old one," so a cycle either spools every real change and proceeds,
or it doesn't proceed at all -- see `obsidian_tools.local_replicator.drift.decide_cycle_outcome` for
the gate itself, held as a value rather than enacted inline here.

**Steps 6 and 7 are not atomic, and the residue is recorded rather than suppressed.** A crash
between the publish and the tag advance leaves fresh upstream content sitting in iCloud while
`LAST_CHECKOUT` still names the pre-publish commit, so the next cycle's comparison reads every path
that commit touched as device-side drift (ppat/obsidian-tools#36). Two operations cannot be made
one, so the window cannot be closed -- and closing it by having *this* component decide such a path
is not really a human's edit is the wrong repair twice over: it is the judgement docs/DESIGN.md §1.5
R2 reserves for the server, and a human edit that happens to reproduce upstream byte-for-byte is
indistinguishable from residue here, so a suppressing device would silently drop real edits. What
step 3 does instead is *observe*: which baseline it compared against, which upstream revision the
clone knew at that moment, and whether each drifted path's content is byte-identical to it
(`_observe_upstream`, and `drift.SpoolEntry`'s own docstring). Every path is still spooled. This is
the moment -- the only moment -- at which the iCloud tree, its baseline, and the revision the clone
knew all exist together; nothing downstream can reconstruct it, which is why not recording it would
be the actual loss.

**Recording it is not the same as explaining it, and the gap matters.** These observations narrow
what Phase 5 has to guess at; they do not identify crash residue, and `SpoolEntry`'s docstring is
explicit that no combination of them does. The same "upstream revision ahead of the tag" state that
a step 6/7 crash produces is produced by every *withheld* cycle too -- step 5's fetch is
unconditional while step 6 is gated -- and a withheld cycle is the ordinary case, not the rare one.

**Idempotent from any starting state.** Step 1 does not trust that the parked clone is still
sitting at `LAST_CHECKOUT` just because the previous cycle *should* have left it there -- a crash at
any point in a prior cycle (mid-overlay, mid-stage, between reset and checkout) can leave the
working tree in an arbitrary state. Every cycle starts by forcing it back to `LAST_CHECKOUT`
(`checkout -f` plus `clean -fd`) rather than by bookkeeping how the previous cycle must have ended.
Discarding the overlay later in the same cycle uses `git reset --hard`, never `git stash`, for the
same reason: stash accumulates refs and would itself be one more piece of state a crash could
strand.

**What's unconditional versus gated, once the gate's two conditions have been evaluated.** Step 5
(reset, check out `main`, pull) always runs, whether or not this cycle's spool write succeeded and
whether or not every staged change's patch captured content -- the design doc lists it as a plain
step, not a conditional one, because bringing new upstream history into the local clone costs
nothing and a future cycle will need it regardless. Only step 6 (publish) and step 7 (tag advance)
are gated, on both conditions together. This is deliberately not "revert the tree to the previous
checkout on failure," the way an earlier implementation of this cycle worked: the *next* cycle's own
step 1 re-establishes `LAST_CHECKOUT` regardless of what ref this cycle's step 5 left the tree on,
so there is nothing to reconcile here -- idempotency at the top of the cycle is what makes
bookkeeping at the bottom unnecessary.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from obsidian_tools.config import ReplicateConfig
from obsidian_tools.local_replicator.clone import checkout_forward, ensure_cache_clone, fetch_origin
from obsidian_tools.local_replicator.device_baseline import is_baselined, seed_baseline
from obsidian_tools.local_replicator.drift import (
    SpoolEntry,
    StagedChange,
    UpstreamComparison,
    decide_cycle_outcome,
    select_spool_entries,
)
from obsidian_tools.local_replicator.rsync_ops import overlay, publish
from obsidian_tools.local_replicator.spool import SpoolWriteError, write_spool_entry
from obsidian_tools.local_replicator.tag import advance_last_checkout, read_last_checkout
from obsidian_tools.vault_git.runner import GitRunner
from obsidian_tools.vault_git.ssh import build_ssh_command

logger = logging.getLogger(__name__)

# The injectable seam the Phase 2 acceptance test uses to prove the ordering (docs/DESIGN.md §7
# Phase 2: "Force the spool write to fail for a drift patch, run the cycle"). A real failure here
# is a local disk write failing -- expected to be rare to the point of practically never, which is
# exactly why the gate can afford to sit on it (see this module's own docstring).
SpoolWriter = Callable[[Path, SpoolEntry], Path]


@dataclass(frozen=True, slots=True)
class CycleResult:
    drifted: tuple[str, ...]
    spooled: tuple[str, ...]
    spool_write_failed: bool
    uncaptured: tuple[str, ...]
    obsidian_seed_attempted: bool
    tag_advanced: bool
    checkout: str | None


def _staged_changes(runner: GitRunner) -> list[StagedChange]:
    """Every currently-staged change (the overlay's effect, after `git add -A`), each paired with
    its own patch text. `:(literal)` pathspec magic: these paths came from git's own `-z` output,
    not from a human, but a filename containing a glob metacharacter (`*`, `[`, `?`) would
    otherwise be re-interpreted by git as a pattern rather than matched as itself -- the same
    convention `vault_git/baseline.py` already uses for the same reason. For a rename/copy, both
    the old and new path are passed together so git's own diff machinery re-pairs them into one
    rename patch rather than two unrelated add/delete hunks.
    """
    changes: list[StagedChange] = []
    for status_entry in runner.staged_name_status():
        pathspecs = [f":(literal){status_entry.path}"]
        if status_entry.old_path is not None:
            pathspecs.append(f":(literal){status_entry.old_path}")
        patch = runner.staged_patch(*pathspecs)
        changes.append(
            StagedChange(
                status=status_entry.status,
                path=status_entry.path,
                old_path=status_entry.old_path,
                patch=patch,
            )
        )
    return changes


def _observe_upstream(runner: GitRunner, *, branch: str) -> UpstreamComparison | None:
    """The upstream revision this clone already knows, and which staged paths differ from it --
    read *before* the fetch, because that is both the only revision observable at comparison time
    and the correct one (see `UpstreamComparison`'s own docstring, and ppat/obsidian-tools#36).

    `None` when the clone knows no upstream revision at all: a freshly re-provisioned cache, or an
    origin the committer has not pushed to yet. Nothing is guessed in that case -- the entries
    record that no observation was possible.
    """
    sha = runner.rev_parse_or_none(f"refs/remotes/origin/{branch}")
    if sha is None:
        return None
    return UpstreamComparison(sha=sha, differing_paths=frozenset(runner.staged_paths_differing_from(sha)))


def run_cycle(config: ReplicateConfig, *, spool_writer: SpoolWriter = write_spool_entry) -> CycleResult:
    cache_clone_dir = Path(config.cache_clone_dir)
    icloud_vault_dir = Path(config.icloud_vault_dir)
    spool_dir = Path(config.spool_dir)
    icloud_vault_dir.mkdir(parents=True, exist_ok=True)

    ssh_command = build_ssh_command(config.ssh_key_path, config.ssh_known_hosts_path)
    runner = GitRunner(cache_clone_dir / ".git", cache_clone_dir, ssh_command=ssh_command)
    ensure_cache_clone(runner, branch=config.branch, origin_url=config.origin_url)

    previous_checkout = read_last_checkout(runner)

    drifted: list[str] = []
    spooled: list[str] = []
    uncaptured: list[str] = []
    spool_write_failed = False

    # A missing baseline (first run, or a lost/re-provisioned cache) means there is nothing
    # meaningful to diff against; re-baselining publishes everything once instead of reporting the
    # whole vault as drifted (docs/DESIGN.md §4 Plane B, "Losing the Mac clone loses the baseline").
    if previous_checkout is not None:
        # Step 1: idempotent re-park, regardless of what a prior crash left behind (module
        # docstring, "Idempotent from any starting state").
        runner.run(["checkout", "-q", "-f", previous_checkout])
        runner.run(["clean", "-q", "-fd"])

        # Step 2: overlay the device tree onto the parked baseline, in place.
        overlay(icloud_vault_dir, cache_clone_dir)

        # Step 3: git is the drift engine. Staging first (`add -A`) is what turns a device-created
        # file's empty, unstaged diff into a full "new file" patch (module docstring; see
        # `_staged_changes`).
        runner.run(["add", "-A"])
        changes = _staged_changes(runner)
        drifted = [change.path for change in changes]
        # Read while the overlay is still staged and before the fetch below moves the
        # remote-tracking ref: this is the one moment the iCloud tree, the baseline it was compared
        # against, and the upstream revision the clone knows all coexist (ppat/obsidian-tools#36).
        # Nothing downstream can reconstruct it, so it is recorded on every entry rather than acted
        # on here.
        upstream = _observe_upstream(runner, branch=config.branch)
        selection = select_spool_entries(changes, baseline_sha=previous_checkout, upstream=upstream)
        entries = selection.entries
        matching_upstream = [entry.path for entry in entries if entry.matches_upstream]
        if matching_upstream:
            # Not a warning, not a gate, and deliberately not phrased as a diagnosis: the entries
            # were spooled like any others, and what to make of them is Phase 5's call. An operator
            # reading this line is being told what was observed, not what happened -- the same
            # content arises from a crash in the step 6/7 window and from a wholly ordinary gated
            # cycle, and this component cannot tell them apart (`drift.SpoolEntry`).
            logger.info(
                "drift on paths whose content already matches the known upstream revision; spooled and annotated",
                extra={
                    "event": "drift_matches_upstream",
                    "paths": matching_upstream,
                    "baseline_sha": previous_checkout,
                    "upstream_sha": None if upstream is None else upstream.sha,
                },
            )
        uncaptured = list(selection.uncaptured)
        if uncaptured:
            logger.error(
                "drift detected on a path whose patch carries no content; this cycle will not publish",
                extra={"event": "drift_uncaptured", "paths": uncaptured},
            )

        # Step 4: publish each drift patch to the spool, atomically and durably, before anything
        # below discards the overlay that produced it. Stops at the first failure rather than
        # attempting the rest: this cycle's outcome is already decided at that point (see
        # `drift.decide_cycle_outcome`), and every entry actually written stays on disk regardless
        # -- durable, and picked up by the drainer on its own schedule independent of this cycle.
        for entry in entries:
            try:
                spool_writer(spool_dir, entry)
            except SpoolWriteError:
                logger.exception(
                    "spool write failed; this cycle's publish and tag advance will not run",
                    extra={"event": "spool_write_failed", "path": entry.path},
                )
                spool_write_failed = True
                break
            spooled.append(entry.path)

        # Step 5, part one: reset the tree -- `git reset --hard`, not a stash (module docstring).
        # Unconditional: whether or not the spool write above succeeded, the overlay must not
        # survive into the pull below.
        #
        # Trailing `--`: `vault_git/runner.py` now runs every git invocation with `cwd` pinned to
        # `work_tree` (see that module's comment on why), which means a vault note or folder
        # literally named `HEAD` -- no extension, unlikely but not disallowed -- would otherwise
        # make this ambiguous between the ref and a same-named path in cwd ("ambiguous argument
        # 'HEAD': both revision and filename"). The trailing `--` is git's own documented fix: it
        # says there are zero pathspecs, so everything before it is unambiguously a revision.
        runner.run(["reset", "-q", "--hard", "HEAD", "--"])
        runner.run(["clean", "-q", "-fd"])

    verdict = decide_cycle_outcome(spool_write_failed=spool_write_failed, uncaptured_paths=uncaptured)

    # Step 5, part two: check out main, and pull -- unconditional (module docstring, "What's
    # unconditional versus gated").
    fetched_sha = fetch_origin(runner, branch=config.branch)
    if fetched_sha is None:
        logger.info(
            "origin has no history for this branch yet; nothing to publish this cycle",
            extra={"event": "cycle_no_origin_history"},
        )
        return CycleResult(
            drifted=tuple(drifted),
            spooled=tuple(spooled),
            spool_write_failed=spool_write_failed,
            uncaptured=tuple(uncaptured),
            obsidian_seed_attempted=False,
            tag_advanced=False,
            checkout=previous_checkout,
        )
    checkout_forward(runner, branch=config.branch, sha=fetched_sha)

    obsidian_seed_attempted = False
    if verdict.should_publish:
        # Step 6: `.obsidian/` gets its own one-time seed copy (device_baseline.py) rather than
        # going through the ordinary drift/spool path, and is always excluded from this main sync
        # -- either because it was just seeded, or because it was already present (the "exclude
        # when present" rule).
        exclude_paths: list[str] = []
        if is_baselined(icloud_vault_dir):
            exclude_paths.append(".obsidian/")
        else:
            obsidian_seed_attempted = True
            seed_baseline(cache_clone_dir, icloud_vault_dir)
            exclude_paths.append(".obsidian/")

        publish(cache_clone_dir, icloud_vault_dir, extra_excludes=exclude_paths)

    tag_advanced = False
    checkout = previous_checkout
    if verdict.should_advance_tag:
        # Step 7: advance LAST_CHECKOUT only once step 6 has completed.
        advance_last_checkout(runner, fetched_sha)
        tag_advanced = True
        checkout = fetched_sha
        logger.info(
            "cycle complete, LAST_CHECKOUT advanced",
            extra={"event": "cycle_tag_advanced", "checkout": fetched_sha},
        )
    else:
        # Two independent reasons this branch is reachable, and they are not the same operator
        # problem: a spool write failure is a local disk fault (rare, see this module's own
        # docstring), while an uncaptured path is ordinary device behaviour meeting a file outside
        # the vault's markdown-only contract -- pasting an image into a note, most often
        # (drift.captures_content). A single hardcoded message here used to name the first cause
        # even when only the second applied, sending an operator to look at a failing disk while
        # the actual cause was a pasted image. Logged as two independent lines, not one merged
        # message, so a cycle where both occur names both rather than only whichever came first.
        if spool_write_failed:
            logger.info(
                "spool write failed for at least one drifted path this cycle; LAST_CHECKOUT not advanced",
                extra={"event": "cycle_tag_not_advanced", "reason": "spool_write_failed", "spooled": spooled},
            )
        if uncaptured:
            logger.info(
                "at least one drifted path's patch carried no content this cycle; LAST_CHECKOUT not advanced",
                extra={
                    "event": "cycle_tag_not_advanced",
                    "reason": "uncaptured",
                    "spooled": spooled,
                    "uncaptured": uncaptured,
                },
            )

    return CycleResult(
        drifted=tuple(drifted),
        spooled=tuple(spooled),
        spool_write_failed=spool_write_failed,
        uncaptured=tuple(uncaptured),
        obsidian_seed_attempted=obsidian_seed_attempted,
        tag_advanced=tag_advanced,
        checkout=checkout,
    )
