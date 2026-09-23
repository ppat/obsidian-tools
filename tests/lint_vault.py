"""Planted vaults for the lint pass's tests. Not a test module.

One clean curated pair — two homelab notes linking each other, so neither is an orphan — and a clean
finance note and inbox note beside them. Each test plants one violation by varying one note's
frontmatter or body, and keeps the clean vault as its control: a check that fires on the clean vault
is as wrong as one that stays silent on the planted one.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from obsidian_tools.lint_pass.decide import VaultFile

TODAY = date(2026, 9, 22)
MODIFIED = date(2026, 9, 20)

ALPHA = "10-areas/homelab/alpha.md"
BETA = "10-areas/homelab/beta.md"
GAMMA = "10-areas/finance/gamma.md"
DELTA = "00-inbox/delta.md"

type Fields = tuple[tuple[str, str], ...]


def fields(title: str, /, **changes: str | None) -> Fields:
    """A clean note's frontmatter lines as `(key, raw value)`, in canonical order, with `changes`
    applied: a string replaces or appends a key's raw value, `None` removes the key."""
    base: dict[str, str] = {
        "type": "note",
        "title": f'"{title}"',
        "source": "claude-code",
        "authority": "agent",
        "trigger": "human",
        "status": "processed",
        "created": "2026-09-01",
        "updated": "2026-09-01",
        "reviewed": "2026-09-01",
        "tags": "[]",
        "confidence": "high",
        "related": "[]",
        "refs": "[]",
    }
    for key, value in changes.items():
        if value is None:
            base.pop(key, None)
        else:
            base[key] = value
    return tuple(base.items())


def text(frontmatter: Fields, body: str) -> str:
    lines = "".join(f"{key}: {value}\n" if value else f"{key}:\n" for key, value in frontmatter)
    return f"---\n{lines}---\n{body}"


def finance_fields(**changes: str | None) -> Fields:
    return fields("Gamma", **({"authority": "import"} | changes))


FINANCE_BODY = "Balance 10 USD (as of 2026-09, bank statement).\n"

CLEAN: dict[str, str] = {
    ALPHA: text(fields("Alpha"), "See [[beta]].\n"),
    BETA: text(fields("Beta"), "See [[alpha]] and [[gamma]].\n"),
    GAMMA: text(finance_fields(), FINANCE_BODY),
    DELTA: text(fields("Delta", status="inbox"), "Captured.\n"),
    "log.md": "# log\n\n- 2026-09-01 | claude-code | bootstrapped\n",
}


def vault(overrides: Mapping[str, str | None] | None = None) -> list[VaultFile]:
    """The clean vault with `overrides` applied: a string replaces or adds a note, `None` removes it."""
    notes = dict(CLEAN)
    for path, content in (overrides or {}).items():
        if content is None:
            notes.pop(path, None)
        else:
            notes[path] = content
    return [VaultFile(path, content.encode("utf-8"), MODIFIED) for path, content in sorted(notes.items())]
