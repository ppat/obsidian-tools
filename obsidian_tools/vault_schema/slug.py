"""`slug(title)` — the filename stem a title must live at (schema file §7.2; ADR-0013).

The eight steps are the schema file's, in its order; none may be skipped or reordered. NFKC runs
first so a full-width or ligature character folds to ASCII rather than being deleted, and step 6
deletes rather than substitutes, so `Node.js` is `nodejs` and `C++` is `c`. An empty result means
the title is unusable — the caller reports it; nothing here invents a name.
"""

from __future__ import annotations

import re
import unicodedata

_WHITESPACE_RUN = re.compile(r"\s+")
_OUTSIDE_ALPHABET = re.compile(r"[^a-z0-9-]")
_HYPHEN_RUN = re.compile(r"-{2,}")


def slug(title: str) -> str:
    text = unicodedata.normalize("NFKC", title)  # 1
    text = text.strip()  # 2
    text = _WHITESPACE_RUN.sub(" ", text)  # 3
    text = text.lower()  # 4
    text = text.replace(" ", "-")  # 5
    text = _OUTSIDE_ALPHABET.sub("", text)  # 6
    text = _HYPHEN_RUN.sub("-", text)  # 7
    return text.strip("-")  # 8
