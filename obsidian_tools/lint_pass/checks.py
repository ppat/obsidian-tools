"""The reported checks, and the zone each note is judged in. Pure — no I/O, no clock.

Every check here is mechanical: a note either has the property or it does not, and nothing is
decided about meaning (ADR-0018 draws the auto-fix boundary at judgment; this pass stops short of
judgment altogether). The refused-tier conditions are not here — they are the validator's, read
through `admission.validator` so no rule has two homes.

## Zones

- **curated** (`10-areas/`, `20-projects/`): every check, the refused tier read through `admit`.
- **agent** (`00-inbox/`, `40-journal/`, `_ops/agent/`): every check but orphan and stale; the
  refused tier is reported, never refused.
- **elsewhere** (a stray root note, an unmapped folder): as the agent zone — outside curated space,
  so detective only.
- **raw** (`05-raw/`): nothing. Silence about raw is S2 working.
- **exempt** (dot-folders, `_templates/`, `_attachments/`, `_ops/lint/`, `_ops/audit/`,
  `_ops/quarantine/`, `90-archive/`, the fixed root files): nothing.

Every note in every zone is still a link target, and every note except the pass's own records is a
link source: a link into raw or to the index is not dangling, and a curated note linked only from
the inbox is not an orphan.

## Links

A wikilink's target is matched **exactly** against a filename stem, a vault path without `.md`, a
file's own name, or an `aliases:` entry (schema file §7.2, §8). Obsidian resolves case- and
separator-insensitively; the schema file names that a trap, because nothing else that reads the
vault does. Links inside fenced code and inline code are text, not links, as Obsidian renders them.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import PurePosixPath

from obsidian_tools.lint_pass.line import Check, FindingClass
from obsidian_tools.vault_schema.dates import parse_date
from obsidian_tools.vault_schema.fields import FIELD_BY_NAME
from obsidian_tools.vault_schema.frontmatter import Frontmatter, Scalar, Value, is_empty
from obsidian_tools.vault_schema.slug import slug
from obsidian_tools.vault_schema.zones import (
    AGENT_ZONE_PREFIXES,
    CURATED_PREFIXES,
    FINANCE_PREFIX,
    FIXED_ADDRESS_PREFIXES,
    FIXED_ROOT_FILES,
    RAW_PREFIX,
    STALENESS_DIALS,
)


class Zone(StrEnum):
    CURATED = "curated"
    AGENT = "agent"
    ELSEWHERE = "elsewhere"
    RAW = "raw"
    EXEMPT = "exempt"


_EXEMPT_PREFIXES = ("_templates/", "_attachments/", "_ops/lint/", "_ops/audit/", "_ops/quarantine/", "90-archive/")
RECORD_PREFIXES = ("_ops/lint/", "_ops/audit/")
"""The pass's own output. Never judged, and never a link source: a report naming a note must not
be what stops that note being an orphan the next day."""


def zone_of(path: str) -> Zone:
    if path.startswith(CURATED_PREFIXES):
        return Zone.CURATED
    if path.startswith(RAW_PREFIX):
        return Zone.RAW
    if path in FIXED_ROOT_FILES or path.startswith(_EXEMPT_PREFIXES) or _hidden(path):
        return Zone.EXEMPT
    if path.startswith(AGENT_ZONE_PREFIXES):
        return Zone.AGENT
    return Zone.ELSEWHERE


def _hidden(path: str) -> bool:
    return any(part.startswith(".") for part in PurePosixPath(path).parts)


@dataclass(frozen=True, slots=True)
class Finding:
    check: FindingClass
    path: str
    detail: str


# --- frontmatter checks ---------------------------------------------------------------------------

_VOCABULARY_FIELDS = ("type", "source", "status", "confidence")
_REMOVED_SINGULAR = ("tag", "alias", "cssclass")
_OPTIONAL_NEVER_EMPTY = ("consolidated", "salience")

# Schema file §9: the curly quotes (single and double, low-9 and reversed), the en and em dashes, the
# non-breaking spaces, and the zero-width characters (space, non-joiner, joiner, word joiner, and the
# byte-order mark used as one). Written as escapes: literally, half of them are invisible in review
# and the rest look like their ASCII neighbours — and the repository's formatters rewrite them.
BANNED_CHARACTERS = frozenset(
    "\u2018\u2019\u201a\u201b\u201c\u201d\u201e\u201f\u2013\u2014\u00a0\u202f\u2007\u200b\u200c\u200d\u2060\ufeff"
)


def frontmatter_findings(path: str, frontmatter: Frontmatter) -> list[Finding]:
    """The reported checks that read one note's parsed frontmatter and its own path."""
    values = frontmatter.as_dict()
    findings: list[Finding] = []

    if _text(values.get("trigger")) == "schedule" and _text(values.get("authority")) == "human":
        findings.append(Finding(Check.TRIGGER_AUTHORITY, path, "`trigger: schedule` with `authority: human`"))
    if is_empty(values.get("source")):
        findings.append(Finding(Check.UNSTAMPED, path, f"`source:` is {_absent_or_empty(values, 'source')}"))
    if is_empty(values.get("confidence")):
        findings.append(
            Finding(Check.CONFIDENCE_EMPTY, path, f"`confidence:` is {_absent_or_empty(values, 'confidence')}")
        )

    finance = path.startswith(FINANCE_PREFIX)
    for name in _VOCABULARY_FIELDS:
        text = _text(values.get(name))
        vocabulary = FIELD_BY_NAME[name].vocabulary or frozenset()
        if text is None or text in vocabulary or (finance and name == "confidence"):
            continue
        findings.append(
            Finding(Check.OUT_OF_VOCABULARY, path, f"`{name}: {quote(text)}` is not in {sorted(vocabulary)}")
        )

    if not path.startswith(FIXED_ADDRESS_PREFIXES):
        stem = PurePosixPath(path).stem
        title = _text(values.get("title"))
        expected = slug(title) if title is not None else ""
        if not expected:
            detail = "no `title:`" if title is None else f"`title: {quote(title)}` slugs to nothing"
            findings.append(Finding(Check.TITLE_UNSLUGGABLE, path, detail))
        elif expected != stem:
            findings.append(
                Finding(Check.SLUG_MISMATCH, path, f"the stem is `{quote(stem)}`; `slug(title)` is `{expected}`")
            )

    for name in _OPTIONAL_NEVER_EMPTY:
        if name in values and is_empty(values[name]):
            findings.append(Finding(Check.OPTIONAL_FIELD_EMPTY, path, f"`{name}:` is present but empty"))
    for name in _REMOVED_SINGULAR:
        if name in values:
            findings.append(
                Finding(Check.REMOVED_SINGULAR_KEY, path, f"`{name}:` (removed; the plural list replaces it)")
            )
    return findings


def banned_character_findings(path: str, text: str) -> list[Finding]:
    found = sorted({ch for ch in text if ch in BANNED_CHARACTERS})
    if not found:
        return []
    names = ", ".join(f"U+{ord(ch):04X} {unicodedata.name(ch, '?')}" for ch in found)
    return [Finding(Check.BANNED_CHARACTER, path, names)]


def stale_finding(path: str, frontmatter: Frontmatter, today: date) -> Finding | None:
    """Past its area's `reviewed:` dial. An empty `reviewed:` ages from `created:`: without that,
    the check would say nothing about exactly the notes nobody has vetted."""
    dial = next((days for prefix, days in STALENESS_DIALS if path.startswith(prefix)), None)
    if dial is None:
        return None
    values = frontmatter.as_dict()
    reviewed = values.get("reviewed")
    field = "created" if is_empty(reviewed) else "reviewed"
    text = _text(values.get(field))
    since = parse_date(text) if text is not None else None
    if since is None:
        return None  # an unreadable date is its own finding; there is no age to state
    age = (today - since).days
    if age <= dial:
        return None
    return Finding(Check.STALE, path, f"`{field}: {since.isoformat()}` is {age} days old; the dial here is {dial}")


# --- links ----------------------------------------------------------------------------------------

_WIKILINK = re.compile(r"\[\[([^\[\]\n]+?)\]\]")
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_FENCE = re.compile(r" {0,3}(```|~~~)")


def link_targets(text: str) -> tuple[str, ...]:
    """Every wikilink target in `text`, as written, with any `|display`, `#heading` or `^block`
    part removed. A link to a heading of the same note (`[[#h]]`) names no target and is skipped."""
    targets: list[str] = []
    fenced = False
    for line in text.split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        for match in _WIKILINK.finditer(_INLINE_CODE.sub("", line)):
            target = re.split(r"[|#^]", match.group(1), maxsplit=1)[0].strip()
            if target:
                targets.append(target)
    return tuple(targets)


def link_names(path: str, aliases: tuple[str, ...]) -> frozenset[str]:
    """Every exact spelling a link may use to reach the file at `path`."""
    pure = PurePosixPath(path)
    names = {pure.name, path, *aliases}
    if pure.suffix == ".md":
        names |= {pure.stem, path.removesuffix(".md")}
    return frozenset(names)


def aliases_of(frontmatter: Frontmatter | None) -> tuple[str, ...]:
    value = frontmatter.as_dict().get("aliases") if frontmatter is not None else None
    return tuple(item.text for item in value) if isinstance(value, tuple) else ()


# --- helpers --------------------------------------------------------------------------------------


def _text(value: Value) -> str | None:
    return value.text if isinstance(value, Scalar) and not is_empty(value) else None


def _absent_or_empty(values: dict[str, Value], name: str) -> str:
    return "absent" if name not in values else "empty"


def quote(text: str) -> str:
    """Text from a note, made safe to quote in the report and the digest: ASCII only, with every
    other character written as its escape, and no character that would end a table cell or a code
    span. The report must not carry the banned characters it reports, nor a link it quotes."""
    escaped = text.encode("ascii", "backslashreplace").decode("ascii")
    return escaped.replace("|", "\\|").replace("`", "'").replace("[[", "[ [")
