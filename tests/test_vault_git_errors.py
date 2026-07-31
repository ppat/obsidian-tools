"""Tests for `obsidian_tools/vault_git/git_errors.py` — the pure git-stderr classifier.

Every stderr string below was captured from a real, locally reproduced git failure (commands run
directly against a throwaway `--git-dir`/`--work-tree` pair: a stale `index.lock` left in place, a
work-tree file `chmod 0`'d, and a read-only git-dir), not invented -- table-testing against
fabricated text is exactly the weakness this module exists to end (see the module docstring and
`test_commands_commit.py`'s existing `_git_command_error` helper, which builds a `CompletedProcess`
around one of these same strings for every case -- the exception-object plumbing this module was
split away from). `NO_SPACE`'s string is `ENOSPC`'s standard kernel wording, reproduced verbatim
in `vault_git/commit.py`'s own docstring; genuinely filling a disk in a test fixture isn't practical,
so this one case is transcribed rather than freshly captured.
"""

from __future__ import annotations

from obsidian_tools.vault_git.git_errors import ErrorKind, classify_git_error

# --- real, captured stderr strings ------------------------------------------------------------

_STALE_LOCK_STDERR = (
    "fatal: Unable to create '/git/vault.git/index.lock': File exists.\n\n"
    "Another git process seems to be running in this repository, e.g.\n"
    "an editor opened by 'git commit'. Please make sure all processes\n"
    "are terminated then try again. If it still fails, a git process\n"
    "may have crashed in this repository earlier:\n"
    "remove the file manually to continue.\n"
)

_NO_SPACE_STDERR = "fatal: Unable to create '/git/vault.git/index.lock': No space left on device"

_PERMISSION_DENIED_LOCK_STDERR = "fatal: Unable to create '/git/vault.git/index.lock': Permission denied"

_VAULT_READ_FAILURE_STDERR = (
    'error: open("10-areas/note.md"): Permission denied\n'
    "error: unable to index file '10-areas/note.md'\n"
    "fatal: adding files failed\n"
)


def test_stale_lock() -> None:
    assert classify_git_error(_STALE_LOCK_STDERR) is ErrorKind.STALE_LOCK


def test_no_space() -> None:
    assert classify_git_error(_NO_SPACE_STDERR) is ErrorKind.NO_SPACE


def test_permission_denied_on_the_git_dir_itself() -> None:
    """The git-dir cache volume being read-only -- not the vault -- despite also saying
    "Permission denied"; see `test_vault_read_failure_is_not_confused_with_a_locked_git_dir`."""
    assert classify_git_error(_PERMISSION_DENIED_LOCK_STDERR) is ErrorKind.PERMISSION_DENIED


def test_vault_read_failure() -> None:
    assert classify_git_error(_VAULT_READ_FAILURE_STDERR) is ErrorKind.VAULT_READ_FAILURE


def test_unrecognized_text_is_unknown_not_guessed() -> None:
    """A string this function has never seen before must not be forced into one of the known
    buckets -- guessing wrong here is exactly what misattributed two real incidents to NFS."""
    assert classify_git_error("fatal: unable to access 'https://example.invalid/repo.git/'") is ErrorKind.UNKNOWN
    assert classify_git_error("") is ErrorKind.UNKNOWN


def test_vault_read_failure_is_not_confused_with_a_locked_git_dir() -> None:
    """Both `_PERMISSION_DENIED_LOCK_STDERR` and `_VAULT_READ_FAILURE_STDERR` contain the literal
    substring "Permission denied" -- one about the git-dir cache volume, the other about a vault
    work-tree file. Classifying by that substring alone would conflate two failures with completely
    different fixes; this is the regression case for exactly that mistake."""
    assert classify_git_error(_PERMISSION_DENIED_LOCK_STDERR) is not ErrorKind.VAULT_READ_FAILURE
    assert classify_git_error(_VAULT_READ_FAILURE_STDERR) is not ErrorKind.PERMISSION_DENIED


def test_permission_denied_without_index_lock_context_is_not_misread_as_a_lock() -> None:
    """ "Permission denied" naming a work-tree path, with no "index.lock" anywhere in the message,
    must not fall into the git-dir-volume buckets -- those are gated on "index.lock" appearing at
    all, not merely on the words "Permission denied"."""
    assert classify_git_error(_VAULT_READ_FAILURE_STDERR) is not ErrorKind.STALE_LOCK
    assert classify_git_error(_VAULT_READ_FAILURE_STDERR) is not ErrorKind.NO_SPACE
