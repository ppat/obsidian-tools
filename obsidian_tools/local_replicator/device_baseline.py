"""`.obsidian/` on the device side: copy once, then hands off (docs/DESIGN.md §8a D3).

One rule covers bootstrap and steady state: if `.obsidian/` is absent at the destination, copy it;
if present, exclude it from that cycle's publish rsync entirely (`cycle.py` does the excluding —
this module only answers "has it been seeded" and performs the seed copy itself). A device that
already has `.obsidian/` keeps its own configuration indefinitely; a baseline change made later at
the cluster GUI does not reach it. The only reset path is deleting `.obsidian/` in the device's
iCloud vault directory by hand, which makes the presence check answer "no" again.

**Presence is decided by a completion marker, not by the directory existing.** If the first copy
dies partway through, `.obsidian/` would otherwise be left present-but-incomplete, and every later
run would skip it forever — a permanently half-configured vault that looks correct. Gating on a
marker written only after every file has copied means a partial attempt is retried wholesale on
the next cycle rather than mistaken for done; re-running the copy is idempotent (it only ever
touches files under `.obsidian/`), so retrying it costs nothing.

The marker lives inside `.obsidian/` itself, in iCloud — not in any local-replicator-side state on
the Mac — because presence is a fact about the shared iCloud vault, not about this one process's
own memory: Mac and iPhone open the *same* iCloud-synced `.obsidian/`, and local-replicator's own
state could be lost and rebuilt independently of whether the device copy was ever actually seeded.

**Divergence is reported, never reconciled, and the two halves share one set on purpose.** Nothing
publishes `.obsidian/` after the seed, and nothing here writes over a device's settings to bring
them back — that would need a third `.obsidian/` write path and would silently revert a device's own
theme choice, which is the seed-once rule reversed rather than repaired. What
`diverged_baseline_paths` adds is the observation the rsync exclusion would otherwise cost
(ppat/obsidian-tools#46): it enumerates through the *same* `select_baseline_paths` call `seed_baseline`
copies through, so it can never name a path no seed would place. Reporting a divergence the system
has no mechanism to act on is the failure this pairing exists to avoid, one level up from the
overlay/publish asymmetry that motivated it.

**What gets copied is decided by `vault_git/baseline_selector.py`, not by a rule re-derived here.**
That module is the one place every `.obsidian/` safety rule applies — an allowlist, not a denylist
— after three prior ad hoc rules in this repository each fixed one hole and left another (a plugin's
`data.json`/bearer token; `themes/`/`snippets/` as bare directory prefixes; a symlink followed on
one enumeration branch but not another — see that module's docstring). This module used to carry a
fourth such rule, a two-name denylist of workspace-state files, which was narrower than the
allowlist it duplicated in spirit and reintroduced exactly the "is a plugin's `data.json` on this
list" gap the selector exists to close. The walk below only has to build `PathInfo` candidates —
computing `is_symlink` is this module's job because it is the one with a filesystem to check it
against, per `PathInfo`'s own docstring — and hand them to the same selector `vault_git/baseline.py`
uses for the committer-side capture; deciding which paths are safe never happens here.
"""

from __future__ import annotations

import filecmp
import logging
import os
import shutil
from pathlib import Path
from stat import S_ISLNK, S_ISREG

from obsidian_tools.logging_config import LOG_PATH_SAMPLE_LIMIT
from obsidian_tools.vault_git.baseline_selector import PathInfo, select_baseline_paths

logger = logging.getLogger(__name__)

OBSIDIAN_DIR = ".obsidian"
BASELINE_MARKER = ".local-replicator-baseline-complete"


def is_baselined(icloud_vault_dir: Path) -> bool:
    return (icloud_vault_dir / OBSIDIAN_DIR / BASELINE_MARKER).exists()


def baseline_source_present(cache_clone_dir: Path) -> bool:
    """Whether the parked clone holds anything at `.obsidian` to seed a device from — `seed_baseline`'s
    own "nothing to seed from" test, exposed so a caller can tell a seed that was *withheld* from one
    there was never anything to perform. The committer not having taken its `.obsidian/` baseline
    commit yet is a routine state (docs/DESIGN.md §8a D3), and reporting an unconfigured device
    against it would name a cause no operator can act on."""
    return (cache_clone_dir / OBSIDIAN_DIR).is_dir()


def _iter_obsidian_candidates(source: Path) -> tuple[list[PathInfo], list[str]]:
    """Walk `source` (a parked clone's `.obsidian/`) into `PathInfo` candidates for
    `select_baseline_paths`, relative to `source` itself. Returns `(candidates, unreadable)` —
    `unreadable` names every directory `os.walk` could not enumerate, collected rather than
    swallowed (see `seed_baseline`, which refuses to call the result complete when this is non-empty).

    `os.walk(..., followlinks=False)` is the standard-library equivalent of the non-descent
    guarantee `vault_git/baseline.py`'s own walker documents and hand-rolls with an explicit stack:
    it never recurses into a symlinked directory, so nothing beneath one — a symlinked plugin
    directory, say — is ever produced as a candidate in the first place. This walk only has to
    enumerate files to copy, not build `git` pathspecs or tolerate the retry/pathspec-magic concerns
    that walker also carries, so it leans on the library default rather than reusing that function.

    A directory this process can't read (a transient NFS/iCloud glitch) is *not* silently skipped
    the way an earlier revision of this walker did (`onerror=lambda _err: None`, by analogy to the
    tolerance `vault_git/baseline.py`'s own walker states for the same kind of failure). The analogy
    does not hold *here*, specifically because of `seed_baseline`'s completion marker: a directory
    this walk fails to enumerate produces no candidates for anything beneath it, so the copy that
    follows can quietly finish "successfully" having skipped a whole plugin, and the marker — once
    written — is the only thing that ever stops this walk from running again. Swallowing the error
    does not degrade to "retry next cycle" here; it degrades to "permanently correct-looking and
    wrong" (independent review of this module, ppat/obsidian-tools). Reporting every failure instead
    lets `seed_baseline` withhold the marker and actually retry — see it for the rest of this
    argument, including what a withheld marker versus a fully-refused seed each cost the device.

    **`onerror` covers only the directories this walk cannot open; each entry is therefore stat'ed
    explicitly** (ppat/obsidian-tools#35). An earlier revision wrapped `is_file()`/`is_symlink()` in a
    `try`/`except OSError` that could never fire: those delegate to `os.path.isfile`/`islink`, which
    catch `OSError` themselves and return `False`. An entry that cannot be stat'ed therefore reached
    the selector as `is_file=False`, indistinguishable from a directory or a socket, and was dropped
    with `unreadable` left empty — so `seed_baseline` wrote the completion marker over the gap and
    `is_baselined` gated this walk off permanently. `onerror` does not fill it: that fires on
    `scandir`, and a directory with read but no execute permission enumerates perfectly well
    (`readdir` needs `r`) while every `stat` on its entries fails with `EACCES` (which needs `x`).
    One `lstat()`, which does raise, is what actually detects it, and it is one syscall rather than
    two. `is_file` for a symlink still follows the link (`PathInfo.is_file`'s documented meaning) via
    the swallowing predicate, deliberately: a dangling symlink is a legible state of the clone, not a
    read failure, and must not be able to withhold the marker forever.

    Note what `onerror` *does* still cover, so this is not mistaken for the wider hole it looks like:
    an unreadable subdirectory is still reported, because `readdir` answers "is this a directory"
    from `d_type` without a `stat`, so `os.walk` descends and fails at that directory's own
    `scandir` — verified, not assumed. The silent case was entries, not subtrees.
    """
    candidates: list[PathInfo] = []
    unreadable: list[str] = []

    def _record_unreadable(error: OSError) -> None:
        unreadable.append(error.filename if isinstance(error.filename, str) else str(error))

    for dirpath, _dirnames, filenames in os.walk(source, onerror=_record_unreadable, followlinks=False):
        current_directory = Path(dirpath)
        for filename in filenames:
            entry = current_directory / filename
            try:
                entry_stat = entry.lstat()
            except OSError:
                unreadable.append(str(entry))
                continue
            is_symlink = S_ISLNK(entry_stat.st_mode)
            is_file = entry.is_file() if is_symlink else S_ISREG(entry_stat.st_mode)
            candidates.append(
                PathInfo(relative_path=entry.relative_to(source).as_posix(), is_file=is_file, is_symlink=is_symlink)
            )
    return candidates, unreadable


def seed_baseline(cache_clone_dir: Path, icloud_vault_dir: Path) -> None:
    """Copy the frozen `.obsidian/` baseline from the parked clone into the iCloud vault, then
    write the completion marker. Idempotent and safe to re-run after a partial prior attempt."""
    source = cache_clone_dir / OBSIDIAN_DIR
    if source.is_symlink():
        # Checked before `is_dir()` below, and separately from it, for the same reason
        # `vault_git/baseline.py`'s own root guard exists (ppat/obsidian-tools#22): `is_dir()`
        # resolves symlinks, so it would read a symlinked `.obsidian` as "present" and hand this
        # walk a source it doesn't actually control. That module's copy of this check exists to
        # stop a symlinked `.obsidian` from wedging `git add --force`; this one exists to stop this
        # module from copying an unknown target's contents into iCloud, where they replicate to the
        # phone and to Apple's servers — a wider blast radius than a failed git command, which is
        # why this logs at `warning` rather than the `info` the routine "not written yet" skips
        # below use. The committer's own invariants (`ensure_ignore_rule`, docs/DESIGN.md §7 Phase
        # 2) mean `.obsidian` should never be anything but an ordinary directory in a clone pulled
        # from history it produced; seeing a symlink here means one of those invariants didn't hold,
        # which is worth a human noticing rather than a silent skip identical to bootstrap-not-done.
        # The marker stays unwritten, so the next cycle retries rather than treating a symlinked
        # `.obsidian` as permanently unseeded.
        logger.warning(
            "refusing to seed device .obsidian/ baseline: .obsidian/ in the parked clone is a "
            "symlink, not a real directory",
            extra={"event": "device_baseline_skip_symlinked_source"},
        )
        return

    if not source.is_dir():
        # The committer hasn't taken its own .obsidian baseline commit yet (docs/DESIGN.md §8a D3)
        # — nothing to seed from. Not an error: the marker stays unwritten, and the next cycle
        # tries again once the clone has pulled a commit that includes it.
        logger.info(
            "no .obsidian/ in the parked clone yet; skipping device baseline seed this cycle",
            extra={"event": "device_baseline_skip_no_source"},
        )
        return

    destination = icloud_vault_dir / OBSIDIAN_DIR
    destination.mkdir(parents=True, exist_ok=True)
    candidates, unreadable = _iter_obsidian_candidates(source)
    copied = 0
    for relative_path in select_baseline_paths(candidates):
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative_path, target)
        copied += 1

    if unreadable:
        # Copy what was reachable — it is additive and idempotent (never deletes, per this
        # module's own docstring), so a device that gets some of its config now is strictly better
        # off than one that gets none, and nothing here is destroyed by a later cycle finishing the
        # job. What must NOT happen is the marker: writing it here would make this cycle's gap in
        # `.obsidian/` — whatever sat under the directory that failed to enumerate — permanent,
        # since the marker is the only thing that ever stops this walk from running again (module
        # docstring, "Presence is decided by a completion marker"). Leaving it unwritten means the
        # next cycle re-walks from scratch and tops up anything still missing once the read error
        # clears, the same "retried wholesale" property the marker exists to give a crash-interrupted
        # copy — this is that same property, applied to a walk that finished but wasn't complete.
        logger.warning(
            "device .obsidian/ baseline seed incomplete: part of .obsidian/ could not be read this "
            "cycle; marker withheld so the next cycle retries and tops up what's missing",
            extra={
                "event": "device_baseline_seed_incomplete",
                "file_count": copied,
                "unreadable_count": len(unreadable),
                "unreadable_paths": unreadable[:LOG_PATH_SAMPLE_LIMIT],
            },
        )
        return

    (destination / BASELINE_MARKER).write_text("seeded by obsidian-tools local-replicator\n")
    logger.info("seeded device .obsidian/ baseline", extra={"event": "device_baseline_seeded", "file_count": copied})


def diverged_baseline_paths(cache_clone_dir: Path, icloud_vault_dir: Path) -> list[str]:
    """Every allowlisted `.obsidian/` path whose device copy differs from the parked clone's, as
    vault-relative paths (`.obsidian/app.json`, not `app.json`) so a log line reads the same way
    `CycleResult.drifted` does. Sorted, and empty when the clone holds no baseline to compare
    against — the same "nothing to seed from" state `seed_baseline` treats as routine.

    **The comparison source is `seed_baseline`'s own source, and the set is `seed_baseline`'s own
    set.** That is the property worth preserving over any refinement: this reports a divergence
    exactly when a re-seed of that device would place different bytes than it currently holds, so
    every path it names is one a documented recovery can actually resolve.

    **It does not distinguish a device edit from a baseline the cluster changed after the device
    was seeded.** Both are genuine divergence from the committed baseline and neither is reconciled
    by any publish, but only the first is a settings-lock violation. The device cannot tell them
    apart: nothing records which revision a device was seeded from, and the completion marker is a
    presence flag, not a provenance one. Deliberately not solved by making the marker carry a SHA —
    that would make a device's seed provenance state this component has to keep true across a
    partial copy, a hand-edit and a manual reset, to sharpen a log line that already points at the
    right file.

    A device copy that cannot be read at all counts as diverged, since what it holds is exactly what
    could not be established. Two conditions take the opposite answer and are reported on their own
    event rather than named here, because in both of them nothing about the device was established
    and a zero would be indistinguishable from a healthy one: a clone subdirectory that fails to
    enumerate (it produces no candidates at all, so there is no path to name), and a device copy
    iCloud has evicted to a dataless placeholder. `filecmp.cmp(shallow=False)` compares sizes before
    contents, so the multi-megabyte minified plugin `main.js` files this allowlist admits are only
    read in full when they are already known to be the same length.
    """
    source = cache_clone_dir / OBSIDIAN_DIR
    if source.is_symlink() or not source.is_dir():
        return []

    destination = icloud_vault_dir / OBSIDIAN_DIR
    if destination.is_symlink():
        # The read-side twin of `seed_baseline`'s source guard, and reachable for the reason
        # `is_baselined` is not itself a guard: that check follows the link, so a device whose
        # `.obsidian` points elsewhere answers "seeded" and arrives here. `is_file()` and
        # `filecmp.cmp` follow it too, so every allowlisted path would be read from outside the
        # vault and reported under an `.obsidian/`-relative name — a comparison against a tree this
        # component does not control, presented as a fact about the device. Returning silently would
        # be this module's own named worst case (a zero that is a fact about the read rather than
        # about the device), so the condition is reported. The write-side half of this gap — the
        # seed following the same symlink — is ppat/obsidian-tools#72.
        #
        # This guard is the directory's, and only the directory's. A symlink at an individual
        # allowlisted path inside a real `.obsidian/` is still followed by both sides: `is_file()`
        # and `filecmp.cmp` below resolve it, so a target outside the vault whose content happens to
        # match reads as "not diverged", and `seed_baseline`'s `copy2` writes through it. Measured,
        # not inferred, and folded into #72 — a per-path shape needs a per-path guard, which is not
        # something a check on `destination` can be widened into.
        logger.warning(
            "refusing to compare the device .obsidian/ baseline: .obsidian in the device's iCloud "
            "vault is a symlink, not a real directory, so every allowlisted path would be read from "
            "outside the vault",
            extra={"event": "obsidian_baseline_skip_symlinked_device"},
        )
        return []

    candidates, unreadable = _iter_obsidian_candidates(source)
    diverged: list[str] = []
    dataless: list[str] = []
    for relative_path in select_baseline_paths(candidates):
        device_copy = destination / relative_path
        if not device_copy.exists() and (device_copy.parent / f".{device_copy.name}.icloud").exists():
            # iCloud evicts a file's content by leaving a `.<name>.icloud` placeholder in its place
            # — the same shape `exclude.py`'s `SHARED_EXCLUDE_LIST` carries defensively, and the
            # largest allowlisted files (a minified plugin `main.js`) are the first candidates for
            # it. The real path is then simply absent, which is byte-for-byte what a setting deleted
            # on the device looks like, while the diagnosis this comparison's own event hands an
            # operator names neither cause.
            dataless.append(f"{OBSIDIAN_DIR}/{relative_path}")
            continue
        try:
            same = device_copy.is_file() and filecmp.cmp(source / relative_path, device_copy, shallow=False)
        except OSError:
            same = False
        if not same:
            diverged.append(f"{OBSIDIAN_DIR}/{relative_path}")

    if unreadable or dataless:
        # Two lists of paths the comparison could not reach, from opposite sides of it. Unlike
        # `seed_baseline` there is no marker to withhold here, and refusing outright would suppress
        # the divergence that *was* established, so saying so is the whole of what this caller can
        # do about it — and it has to be said even when `diverged` is empty, since that is precisely
        # the case in which an unreachable path and a healthy device are otherwise the same zero.
        logger.warning(
            "part of this cycle's .obsidian/ baseline comparison could not be made: paths under a "
            "clone directory that could not be read, and paths whose device copy iCloud has evicted "
            "to a dataless placeholder, are absent from the result rather than known to match",
            extra={
                "event": "obsidian_baseline_comparison_incomplete",
                "unreadable_count": len(unreadable),
                "unreadable_paths": unreadable[:LOG_PATH_SAMPLE_LIMIT],
                "dataless_count": len(dataless),
                "dataless_paths": dataless[:LOG_PATH_SAMPLE_LIMIT],
            },
        )
    return diverged
