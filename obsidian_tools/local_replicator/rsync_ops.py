"""The two rsync invocations the replication cycle needs: overlay the device tree onto the parked
baseline, and publish the baseline back out (ADR-0025).

Git is the drift engine now (see `obsidian_tools.local_replicator.drift`) -- these two calls are
pure tree mutation, no longer a comparison. This module used to also provide an `rsync -n -ai`
dry-run enumeration; that mechanism is gone. The third reading of this cycle collapsed "which paths
changed, and what do they now contain" into a single `git diff` over a checked-out baseline
(ADR-0025)
-- reintroducing an rsync-side enumeration here would resurrect exactly the two-mechanisms-for-one-job
shape that reading replaced.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from obsidian_tools.local_replicator.exclude import OBSIDIAN_BASELINE_EXCLUDE, rsync_exclude_args


class RsyncError(RuntimeError):
    """An rsync invocation exited non-zero."""


def _run_rsync(args: list[str]) -> None:
    result = subprocess.run(["rsync", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RsyncError(f"rsync {' '.join(args)} exited {result.returncode}: {result.stderr.strip()}")


# `--checksum`, on both calls below: rsync's default "quick check" skips transferring a file whose
# size *and* mtime already match the destination, without ever looking at content. `baseline_work_tree`
# is a git working tree -- `git checkout` stamps every file it writes with the checkout's own mtime,
# unrelated to the content's actual history -- so a same-second overlay or publish racing a file of
# coincidentally identical size is enough to trigger a false "unchanged, skip it" (found directly,
# by a same-length device edit landing in the same second as a checkout, in this module's own test
# suite -- not a theoretical risk). `--checksum` forces a real content comparison instead. The vault
# is markdown-only and stays small by design (ADR-0027), so the extra
# read-and-hash cost this adds is not a concern worth trading correctness against here.
_CHECKSUM_FLAG = "--checksum"


def overlay(icloud_vault_dir: Path, baseline_work_tree: Path) -> None:
    """Overlay `icloud_vault_dir` onto `baseline_work_tree` in place (step 2): rsync in, with
    `--delete`, excluding `.git`, `.obsidian` and the shared noise list -- bare names, and
    `exclude.py` carries why that is not a spelling choice.

    `--delete` is what makes a phone-side deletion visible to the `git diff` that follows at all --
    without it, a note removed on the device leaves the checkout's copy in place, and the deletion
    never registers as drift. Excluding `.git` is what keeps that same `--delete` from deleting the
    checkout's own repository, since the iCloud side never has one of its own to compare against
    and would otherwise look, to a plain `--delete`, like `.git/` had been removed on the device
    (ADR-0025; ppat/obsidian-tools#3, "Implementation traps").

    **`.obsidian/` is excluded here for the same reason `publish` excludes it, and the two must not
    disagree** (ppat/obsidian-tools#46). Observation covers exactly what publication can act on: an
    overlay that brought device settings in would stage them as ordinary drift, and the publish
    below cannot write them back, so the identical bytes are re-read as fresh drift on every
    subsequent cycle for as long as the divergence lasts. `--delete` makes the reverse direction
    worse still -- a device reset, which deletes `.obsidian/` from the iCloud vault by hand
    (docs/local-replicator.md, "Resetting a device"), would otherwise strip the parked checkout's
    own baseline and stage the entire frozen set as device-side deletions on the one cycle that
    exists to re-seed it. Divergence is detected instead by
    `device_baseline.diverged_baseline_paths`, against the same allowlist the seed places.
    """
    args = [
        "-a",
        _CHECKSUM_FLAG,
        "--delete",
        *rsync_exclude_args(OBSIDIAN_BASELINE_EXCLUDE),
        f"{icloud_vault_dir}/",
        f"{baseline_work_tree}/",
    ]
    _run_rsync(args)


def publish(baseline_work_tree: Path, icloud_vault_dir: Path, *, extra_excludes: list[str]) -> None:
    """Rsync `baseline_work_tree` onto `icloud_vault_dir`, deleting extraneous destination files
    (step 6).

    Unconditional once called -- no longer gated per path the way an earlier reading of this cycle
    ran it, excluding only the paths whose capture had failed that cycle (ADR-0025). `cycle.py`
    now decides whether to call this function
    *at all*, via `obsidian_tools.local_replicator.drift.decide_cycle_outcome`, rather than this
    function deciding per path which parts of an otherwise-unconditional run to skip.

    `extra_excludes` is `.obsidian/`'s own publish rule (device_baseline.py): excluded from this
    sync so a device's own configuration is never overwritten, once the one-time seed has placed it.
    `overlay` excludes the same directory, which is what keeps this exclusion from producing drift
    nothing can resolve -- see that function.
    Plain `--delete` (never `--delete-excluded`) does not touch an excluded path -- verified
    directly against a real rsync invocation, not assumed from the man page's prose (see
    tests/test_local_replicator_rsync_ops.py).
    """
    args = [
        "-a",
        _CHECKSUM_FLAG,
        "--delete",
        *rsync_exclude_args(*extra_excludes),
        f"{baseline_work_tree}/",
        f"{icloud_vault_dir}/",
    ]
    _run_rsync(args)
