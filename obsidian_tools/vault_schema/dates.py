"""Which spellings of a date are dates at all.

The schema file's form is ISO `YYYY-MM-DD` (§3, §9). Beyond it, a closed set of **year-first**
spellings is still a date — one reading only, so a normaliser can rewrite it to ISO without
guessing: `2026/07/30`, `2026.7.30`, `2026-7-3`. Day-first and month-first spellings are not dates
here, because `03/04/2026` has two readings and choosing one is a guess; so is anything with a time
of day, which a date field has nowhere to keep.
"""

from __future__ import annotations

import re
from datetime import date

_YEAR_FIRST = re.compile(r"(?P<year>[0-9]{4})(?P<sep>[-/.])(?P<month>[0-9]{1,2})(?P=sep)(?P<day>[0-9]{1,2})")


def parse_date(text: str) -> date | None:
    """The calendar date `text` spells, if it is ISO or one of the year-first forms and names a day
    that exists; otherwise `None`."""
    match = _YEAR_FIRST.fullmatch(text)
    if match is None:
        return None
    try:
        return date(int(match["year"]), int(match["month"]), int(match["day"]))
    except ValueError:
        return None
