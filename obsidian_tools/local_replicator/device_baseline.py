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

import logging
import os
import shutil
from pathlib import Path

from obsidian_tools.vault_git.baseline_selector import PathInfo, select_baseline_paths

logger = logging.getLogger(__name__)

OBSIDIAN_DIR = ".obsidian"
BASELINE_MARKER = ".local-replicator-baseline-complete"


def is_baselined(icloud_vault_dir: Path) -> bool:
    return (icloud_vault_dir / OBSIDIAN_DIR / BASELINE_MARKER).exists()


def _iter_obsidian_candidates(source: Path) -> list[PathInfo]:
    """Walk `source` (a parked clone's `.obsidian/`) into `PathInfo` candidates for
    `select_baseline_paths`, relative to `source` itself.

    `os.walk(..., followlinks=False)` is the standard-library equivalent of the non-descent
    guarantee `vault_git/baseline.py`'s own walker documents and hand-rolls with an explicit stack:
    it never recurses into a symlinked directory, so nothing beneath one — a symlinked plugin
    directory, say — is ever produced as a candidate in the first place. This walk only has to
    enumerate files to copy, not build `git` pathspecs or tolerate the retry/pathspec-magic concerns
    that walker also carries, so it leans on the library default rather than reusing that function.
    A directory this process can't read (a transient NFS/iCloud glitch) is skipped via `onerror`,
    same tolerance `vault_git/baseline.py` documents for the equivalent case.
    """
    candidates: list[PathInfo] = []
    for dirpath, _dirnames, filenames in os.walk(source, onerror=lambda _err: None, followlinks=False):
        current_directory = Path(dirpath)
        for filename in filenames:
            entry = current_directory / filename
            try:
                is_file = entry.is_file()
                is_symlink = entry.is_symlink()
            except OSError:
                continue
            candidates.append(
                PathInfo(relative_path=entry.relative_to(source).as_posix(), is_file=is_file, is_symlink=is_symlink)
            )
    return candidates


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
    copied = 0
    for relative_path in select_baseline_paths(_iter_obsidian_candidates(source)):
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative_path, target)
        copied += 1

    (destination / BASELINE_MARKER).write_text("seeded by obsidian-tools local-replicator\n")
    logger.info("seeded device .obsidian/ baseline", extra={"event": "device_baseline_seeded", "file_count": copied})
