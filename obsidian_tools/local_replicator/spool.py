"""The local spool: durable, atomic storage for drift patches, written before anything overwrites
the iCloud copy that produced them (ADR-0025).

**Why atomic write-then-rename, not a plain write.** A crash mid-write must never look like a
successful spool entry -- `cycle.py`'s publish gate (`obsidian_tools.local_replicator.drift`'s
`decide_cycle_outcome`) trusts that once a spool file exists at its final name, it holds a
complete, durable entry, and that trust is exactly what "the gate moved, not disappeared" (see
drift.py) rests on. Writing to a temp file in the spool directory itself, fsyncing it, then
`os.replace`-ing it into place is what makes that true: `os.replace` is atomic on the same
filesystem (POSIX `rename(2)`), so a reader -- or a crash -- never observes a partially-written
file at the final path. It either isn't there yet, or it's complete.

**Why an opaque filename, not one derived from the vault path.** A vault path can contain almost
anything a filesystem allows (non-ASCII/quoting hazards apply here too, and this is exactly the
class of bug that "wedged the committer permanently" once already --
ppat/obsidian-tools#3). Turning an arbitrary vault path into a filesystem-safe spool filename would
be its own small parser, with its own escaping bugs to get wrong. Each entry gets a
collision-resistant, content-independent filename instead; the vault path lives inside the entry's
own serialized content, never in how it is named on disk.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

from obsidian_tools.local_replicator.drift import SpoolEntry

_SPOOL_SUFFIX = ".json"


class SpoolWriteError(RuntimeError):
    """A spool entry could not be written durably. `cycle.py` catches exactly this type to gate
    the cycle's publish and tag advance -- see `obsidian_tools.local_replicator.drift`'s
    `decide_cycle_outcome`, and ADR-0025: a laptop off the network is normal; a local write
    failing is not."""


def _entry_filename() -> str:
    # A time-ordered prefix purely so a human `ls`-ing the spool directory sees write order; the
    # uuid suffix is what actually guarantees uniqueness -- two entries written within the same
    # nanosecond-resolution tick (unlikely but not impossible on fast hardware) must still never
    # collide and silently overwrite one another.
    return f"{time.time_ns():020d}-{uuid.uuid4().hex}{_SPOOL_SUFFIX}"


def _serialize(entry: SpoolEntry) -> str:
    # `ensure_ascii=True` -- json's own default, kept explicit here because it is load-bearing, not
    # incidental. A path or patch containing a byte that was not valid UTF-8 comes back from
    # GitRunner as a lone surrogate codepoint (PEP 383 surrogateescape -- see
    # vault_git/runner.py's own `_split_nul`/`_invoke` docstrings). `ensure_ascii` \u-escapes every
    # non-ASCII codepoint, surrogates included, into plain ASCII text -- which is what makes
    # `.encode("ascii")` below safe unconditionally. A lone surrogate cannot be encoded to UTF-8
    # directly; only its \uXXXX text escape can be, and `json.loads` reconstructs the same lone
    # surrogate character from that escape on the way back out (`read_spool_entry`).
    return json.dumps(
        {
            "kind": entry.kind,
            "path": entry.path,
            "old_path": entry.old_path,
            "patch": entry.patch,
            # The three provenance observations (ppat/obsidian-tools#36) -- see `SpoolEntry`'s own
            # docstring for what each one means and why none of them is a verdict. Serialized as
            # plain, separate keys rather than folded into a nested object or a derived summary:
            # the reader is a Phase 5 `drift-processor` that does not exist yet, so there is nothing
            # to test a clever encoding against.
            "baseline_sha": entry.baseline_sha,
            "upstream_sha": entry.upstream_sha,
            "matches_upstream": entry.matches_upstream,
        },
        ensure_ascii=True,
    )


def write_spool_entry(spool_dir: Path, entry: SpoolEntry) -> Path:
    """Write one drift patch to the spool, atomically and durably. Raises `SpoolWriteError` (never
    a bare `OSError`) on any failure -- `cycle.py` catches exactly this type to gate the cycle."""
    tmp_name: str | None = None
    try:
        spool_dir.mkdir(parents=True, exist_ok=True)
        final_path = spool_dir / _entry_filename()
        fd, tmp_name = tempfile.mkstemp(dir=spool_dir, prefix=".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(_serialize(entry))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, final_path)
        tmp_name = None
    except OSError as exc:
        raise SpoolWriteError(f"failed to write spool entry for {entry.path!r}: {exc}") from exc
    finally:
        if tmp_name is not None:
            # Best-effort cleanup of the temp file; the real failure is already raised above.
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
    return final_path


def list_spool_files(spool_dir: Path) -> list[Path]:
    """Every complete spool entry currently on disk, oldest-write-first (filename-sortable by
    construction -- see `_entry_filename`). A `.tmp` file mid-write never carries the final
    `.json` suffix, so it is never listed here even if this runs concurrently with a write."""
    if not spool_dir.is_dir():
        return []
    return sorted(path for path in spool_dir.iterdir() if path.is_file() and path.suffix == _SPOOL_SUFFIX)


def read_spool_entry(path: Path) -> SpoolEntry:
    """Read one entry back. The three provenance keys are read with `.get`, not indexed: an entry
    already sitting in the spool when local-replicator is upgraded was written before those keys
    existed, and the drainer must be able to send it on rather than dying on a `KeyError` and
    wedging every entry behind it. Absent reads back as `None` -- "not recorded", which is what is
    actually true of it -- never as a fabricated sha or a `False` that would claim an observation
    was made and came back negative. Everything this version writes records all three.
    """
    data = json.loads(path.read_text(encoding="ascii"))
    return SpoolEntry(
        kind=data["kind"],
        path=data["path"],
        old_path=data["old_path"],
        patch=data["patch"],
        baseline_sha=data.get("baseline_sha"),
        upstream_sha=data.get("upstream_sha"),
        matches_upstream=data.get("matches_upstream"),
    )
