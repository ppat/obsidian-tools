"""The `LAST_CHECKOUT` tag: the parked clone's own record of "content byte-identical to what was
last placed in iCloud" (ADR-0025).

A tag was chosen over a manifest of file hashes because it reuses machinery already present (the
clone is a git repository regardless) and can't drift from what was actually checked out — the
working tree and the tag either name the same commit or they don't; there's no third state to
reconcile. Advancing it is `cycle.py`'s job, gated on whether every drifted path published
successfully this cycle — this module only reads and writes the ref itself.
"""

from __future__ import annotations

from obsidian_tools.vault_git.runner import GitRunner

LAST_CHECKOUT_TAG = "LAST_CHECKOUT"


def read_last_checkout(runner: GitRunner) -> str | None:
    return runner.rev_parse_or_none(LAST_CHECKOUT_TAG)


def advance_last_checkout(runner: GitRunner, sha: str) -> None:
    runner.run(["tag", "-f", LAST_CHECKOUT_TAG, sha])
