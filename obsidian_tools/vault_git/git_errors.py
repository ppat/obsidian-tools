"""Classifying a failed git invocation's `stderr`, as a pure function over the text itself — no
`GitCommandError`, no `RetryExhaustedError`, no exception unwrapping.

**Why this is split out and kept pure.** Misdiagnosing a staging failure has wedged this committer
twice, both times pointing the operator at the vault's NFS mount when the actual fault was the
git-dir cache volume itself (full, or newly read-only) — a human chasing the wrong storage system
wastes an incident, not just a test run. The previous check (`is_index_lock_error` alone) only ever
answered "is this a stale lock, yes or no," collapsing every other failure into one bucket
regardless of what actually went wrong. Splitting the *text classification* from the *exception
unwrapping* (still in `commands/commit.py`, since only the shell has an exception object to unwrap)
means every stderr shape below is a one-line table test against a literal string, not a fabricated
`CompletedProcess` — see `commands/commit.py`'s existing tests, which build one of those for every
case, as the thing this was worth separating from. See `baseline_selector.py` for the same argument
applied elsewhere in this codebase.

Every string below was captured from a real, reproduced git failure (a stale `index.lock`, a
work-tree file made unreadable, a read-only git-dir), not invented — see `test_vault_git_errors.py`.
"""

from __future__ import annotations

from enum import Enum


class ErrorKind(Enum):
    STALE_LOCK = "stale_lock"  # a leftover index.lock from a killed run; safe to clear and retry
    NO_SPACE = "no_space"  # the git-dir cache volume is full
    PERMISSION_DENIED = "permission_denied"  # the git-dir cache volume itself is unwritable
    VAULT_READ_FAILURE = "vault_read_failure"  # a work-tree file under the vault couldn't be read
    WORK_TREE_UNUSABLE = "work_tree_unusable"  # the work tree itself is absent or can't be entered
    # git never started at all — an OSError out of the invocation, not a git exit. Assigned by the
    # exception-unwrapping half in `commands/commit.py` (from the exception's own type, which is
    # `vault_git/runner.py`'s `GitInvocationError`), never by `classify_git_error` below: there is
    # no git stderr to classify when there was no git process.
    INVOCATION_FAILED = "invocation_failed"
    UNKNOWN = "unknown"


def classify_git_error(stderr: str) -> ErrorKind:
    """Classify one git invocation's `stderr`. Deliberately conservative: a string this function
    doesn't recognize returns `UNKNOWN` rather than guessing, which is what keeps `NO_SPACE` and
    `PERMISSION_DENIED` from ever being silently folded back into `VAULT_READ_FAILURE` the way the
    single-purpose lock check used to fold both into "not a lock"."""
    if "index.lock" in stderr:
        # The path git was trying to create when it failed -- distinguishing *why* it failed still
        # requires looking past that shared substring. "File exists" is what actually means a lock
        # is already there; the git-dir cache volume being full or read-only produces the identical
        # "Unable to create '.../index.lock'" prefix with a different OS-level cause after it.
        if "File exists" in stderr:
            return ErrorKind.STALE_LOCK
        if "No space left on device" in stderr:
            return ErrorKind.NO_SPACE
        if "Permission denied" in stderr:
            return ErrorKind.PERMISSION_DENIED
        return ErrorKind.UNKNOWN

    if "must be run in a work tree" in stderr:
        # git's own wording when it cannot use the work tree it was given *at all* -- measured
        # identical for a `--work-tree` that is absent, is a plain file, and is a directory this uid
        # cannot enter, so this kind covers the directory itself rather than any one errno. Distinct
        # from VAULT_READ_FAILURE below, which is a *file* under an otherwise-fine work tree: the
        # committer's work tree is `/vault/brain`, a directory *inside* the read-only vault volume
        # that this workload can never create, so on a freshly provisioned or restored PVC this is
        # what every staging attempt says until the vault directory appears.
        return ErrorKind.WORK_TREE_UNUSABLE

    if 'open("' in stderr and "Permission denied" in stderr:
        # git's own wording for a work-tree file `git add` couldn't read, e.g.
        # `error: open("10-areas/note.md"): Permission denied` -- the NFS-mounted vault, not the
        # git-dir cache, is what's actually implicated by this shape.
        return ErrorKind.VAULT_READ_FAILURE

    return ErrorKind.UNKNOWN
