"""The two small schema facts with one right answer each: which spellings are dates, and `slug()`.

`slug()`'s examples are the schema file's own §7.2 table, verbatim — an oracle written before this
code existed. Its properties are safety invariants on the output rather than a restatement of the
eight steps: whatever the title, the stem is a legal, stable filename.
"""

from __future__ import annotations

from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from obsidian_tools.vault_schema.dates import parse_date
from obsidian_tools.vault_schema.slug import slug

# --- dates --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["2026-07-30", "2026/07/30", "2026.07.30", "2026-7-30", "2026/7/3", "2024-02-29"],
)
def test_iso_and_year_first_spellings_are_dates(text: str) -> None:
    assert parse_date(text) is not None


def test_year_first_spellings_have_the_one_reading() -> None:
    assert parse_date("2026/7/3") == parse_date("2026-07-03") == date(2026, 7, 3)


@pytest.mark.parametrize(
    "text",
    [
        "30/07/2026",  # day-first
        "07/30/2026",  # month-first
        "03/04/2026",  # both readings exist — the reason neither is accepted
        "2026-07-30T10:00",  # a time of day, which a date field has nowhere to keep
        "2026-07-30 10:00",
        "2026-07/30",  # mixed separators
        "2026-02-30",  # no such day
        "2025-02-29",
        "2026-13-01",
        "20260730",
        "26-07-30",
        "July 30, 2026",
        "\u0662\u0660\u0662\u0666-\u0660\u0667-\u0663\u0660",  # Arabic-Indic digits: `\\d` would take them
        "",
    ],
)
def test_anything_else_is_not_a_date(text: str) -> None:
    assert parse_date(text) is None


# --- slug ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "stem"),
    [
        ("Kubernetes Ingress", "kubernetes-ingress"),
        ("K3s / Longhorn notes", "k3s-longhorn-notes"),
        ("Node.js", "nodejs"),
        ("C++", "c"),
        ("  Two   spaces  ", "two-spaces"),
        ("2026-07-30", "2026-07-30"),
    ],
)
def test_the_schema_files_own_examples(title: str, stem: str) -> None:
    assert slug(title) == stem


@pytest.mark.parametrize(
    ("title", "stem"),
    [
        ("\uff2b\uff13\uff53", "k3s"),  # full-width folds at NFKC rather than being deleted at step 6
        ("\ufb01le", "file"),  # a ligature folds too
        ("a\u00a0b", "a-b"),  # a non-breaking space is whitespace at step 3
        ("?!:", ""),  # unusable: the caller reports it, nothing here invents a name
    ],
)
def test_nfkc_runs_before_deletion(title: str, stem: str) -> None:
    assert slug(title) == stem


@given(st.text())
def test_a_stem_is_always_a_legal_stable_filename(title: str) -> None:
    """Red if any title yields a stem with a character outside `a-z0-9-` (so one a case-insensitive
    device or iCloud could fork or refuse), an edge or doubled hyphen, or a stem whose own slug
    differs — which would make the lint's stem check flag a file named by this very function."""
    stem = slug(title)

    assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in stem)
    assert not stem.startswith("-")
    assert not stem.endswith("-")
    assert "--" not in stem
    assert slug(stem) == stem
