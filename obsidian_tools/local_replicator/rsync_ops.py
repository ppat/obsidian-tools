"""The two rsync invocations the replication cycle needs: a dry-run comparison, and the real publish.

Both read from the parked cache clone's checked-out working tree; neither ever writes to it. See
`cycle.py` for how these two calls are sequenced against the git pull and the capture step.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from obsidian_tools.local_replicator.exclude import rsync_exclude_args

logger = logging.getLogger(__name__)

# The itemized-output path field always starts at this column, for both an ordinary itemize line
# (an 11-character code, then one separator space) and a "*deleting " line (the literal marker
# padded to the same 11-character column width) — confirmed directly against rsync 3.2.7's actual
# output, not assumed from the man page's prose description.
_ITEMIZE_PATH_COLUMN = 12
_DELETING_PREFIX = "*deleting"


@dataclass(frozen=True, slots=True)
class DriftedPath:
    """One path rsync's dry run flagged as different between the parked baseline and iCloud.

    `kind` names what a real (non-dry-run) rsync would mechanically do about it — `copy` (the
    baseline has it, iCloud doesn't yet, or has different content) or `delete` (iCloud has it,
    the baseline doesn't). It is not a claim about human intent (e.g. `copy` does not mean
    "new upstream content" — since the comparison runs against the *pre-pull* baseline, a `copy`
    entry for a path the baseline already had almost always means a human deleted it from iCloud;
    see `cycle.py`). Capture logic does not branch on this field — it simply checks whether the
    path currently exists in iCloud — this is carried for observability only.
    """

    path: str
    kind: Literal["copy", "delete"]


class RsyncError(RuntimeError):
    """An rsync invocation exited non-zero."""


def _run_rsync(args: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["rsync", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RsyncError(f"rsync {' '.join(args)} exited {result.returncode}: {result.stderr.strip()}")
    return result


def compare_dry_run(baseline: Path, icloud_vault_dir: Path) -> list[DriftedPath]:
    """Enumerate paths that differ between `baseline` (the parked clone, checked out at
    LAST_CHECKOUT — see cycle.py) and `icloud_vault_dir`, without changing either.

    `-a --delete --dry-run` mirrors the real publish call exactly (same flags, same direction) so
    the enumeration reports precisely what that call would do — including files present only in
    iCloud, which only show up when `--delete` is part of the dry run too (docs/DESIGN.md §4 Plane
    B: "rsync -n -ai between the two enumerates which paths drifted").
    """
    args = [
        "-a",
        "--itemize-changes",
        "--delete",
        "--dry-run",
        *rsync_exclude_args(),
        f"{baseline}/",
        f"{icloud_vault_dir}/",
    ]
    result = _run_rsync(args)
    return _parse_itemize_output(result.stdout)


def publish(baseline: Path, icloud_vault_dir: Path, *, extra_excludes: list[str]) -> None:
    """Rsync `baseline`'s tree onto `icloud_vault_dir`, deleting extraneous destination files.

    `--delete` always runs, but plain `--delete` (never `--delete-excluded`) does not touch an
    excluded path — it is left exactly as it is on the destination side, neither overwritten nor
    removed. `extra_excludes` is how `cycle.py` protects a path whose capture failed this cycle (or
    all of `.obsidian/`, once already seeded): one mechanism, no separate conditional needed.
    Pairing this with `--delete-excluded` would defeat that protection outright — it would delete
    exactly the paths this function exists to leave alone.
    """
    args = ["-a", "--delete", *rsync_exclude_args(*extra_excludes), f"{baseline}/", f"{icloud_vault_dir}/"]
    _run_rsync(args)


def _parse_itemize_output(output: str) -> list[DriftedPath]:
    drifted: list[DriftedPath] = []
    for line in output.splitlines():
        if not line:
            continue
        if line.startswith(_DELETING_PREFIX):
            path = line[_ITEMIZE_PATH_COLUMN:]
            if path.endswith("/"):
                continue  # a directory becoming empty is structural, not a content drift
            drifted.append(DriftedPath(path=path, kind="delete"))
            continue

        code = line[:11]
        if len(code) < 11 or line[11:12] != " ":
            logger.warning(
                "unrecognised rsync itemize line, ignoring",
                extra={"event": "itemize_parse_skip", "line": line},
            )
            continue
        file_type = code[1]
        if file_type != "f":
            continue  # directories/symlinks are structural; only regular files are vault content
        path = line[_ITEMIZE_PATH_COLUMN:]
        drifted.append(DriftedPath(path=path, kind="copy"))
    return drifted
