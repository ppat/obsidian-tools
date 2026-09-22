"""`admit(path, content) -> Verdict`: whether a note's post-image may enter curated space.

## What is refused, and the line it enforces

The refused tier of the S2 tolerance line (A6, ot#84), and nothing else. The principle: **refuse
what breaks the machine-readability of the frontmatter or its provenance; accept deviations in
vocabulary and style** — those are the lint pass's to report. One reason code per rule, because the
codes are the counted O1 dimension:

- `frontmatter_unparseable` — no frontmatter block, or one outside the vault's dialect
  (`vault_schema/frontmatter.py`). Every other rule presupposes a readable block.
- `type_missing` — `type` absent or empty. S2's falsifier: a note missing `type` is not admitted.
- `provenance_missing` — `authority` or `trigger` absent or empty, one refusal per field. S1's
  falsifier: no write lands without `authority:`/`trigger:`.
- `date_unparseable` — a date field holds a value that is not a date (`vault_schema/dates.py`).
  S2's scope note: an unparseable date is not accepted.
- `wrong_type` — a declared field holds the wrong shape: a list where the schema declares a scalar,
  a scalar where it declares a list, a `salience` that is not an unquoted integer. Schema file §3:
  one field means one type, and a mistyped field silently breaks every view over it.
- `finance_authority` — under `10-areas/finance/`, `authority` is not exactly `import` (ADR-0010:
  `authority: human` unlocks nothing).
- `finance_confidence` — under `10-areas/finance/`, `confidence` is absent, empty, or outside its
  vocabulary (ADR-0010, ADR-0011).
- `finance_provenance` — under `10-areas/finance/`, the body carries no inline recency marker of
  schema file §6's form, `(as of YYYY-MM[-DD], <source>)` (ADR-0010's inline provenance).

Each rule stands alone and is counted alone: a finance note with no `authority` is refused as both
`provenance_missing` and `finance_authority`. The one ordering is that a field refused as
`wrong_type` is judged by nothing else, since its value has no reading left to judge.

**Deliberately not refused:** a `type`, `source`, `status` or `confidence` outside its vocabulary
(outside finance), malformed tags, a missing `title`, `created` or `updated`, a filename that is not
`slug(title)`. The lint pass reports them, and fixes the fixable ones; refusing them would turn
style into a gate.

## The finance block is presence-based, at note level

It checks that the three kinds of evidence are *present on the note*. It does not decide which
sentences assert a figure — ADR-0010 rejects a hard block that depends on detecting fact-asserting
prose, because that is judgment. The limit, stated so it is not mistaken for a bug: a finance note
with one marked figure and one unmarked figure is admitted.

Where the schema file's finance row says `authority: human` or `import`, ADR-0010 (Accepted)
governs: only `import` passes.

## Zones

Only a write whose path resolves into curated space (`10-areas/`, `20-projects/`) is a crossing.
Anything else — the agent zone, which is detective only, and the raw layer, which is exempt
(ADR-0007, ADR-0015) — returns a verdict saying so, rather than leaving each caller to remember the
zone rule. The path is resolved the way a door might resolve it (a leading `/` dropped, `.` and `..`
collapsed, `..` above the root clamped to it), so no spelling of a curated path reads as outside
curated space.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from enum import StrEnum

from obsidian_tools.vault_schema.dates import parse_date
from obsidian_tools.vault_schema.fields import CONFIDENCE_VOCABULARY, FIELD_BY_NAME, FIELDS, FieldType
from obsidian_tools.vault_schema.frontmatter import Scalar, Style, Unparseable, Value, is_empty, parse_note
from obsidian_tools.vault_schema.zones import CURATED_PREFIXES, FINANCE_PREFIX


class ReasonCode(StrEnum):
    FRONTMATTER_UNPARSEABLE = "frontmatter_unparseable"
    TYPE_MISSING = "type_missing"
    PROVENANCE_MISSING = "provenance_missing"
    DATE_UNPARSEABLE = "date_unparseable"
    WRONG_TYPE = "wrong_type"
    FINANCE_AUTHORITY = "finance_authority"
    FINANCE_CONFIDENCE = "finance_confidence"
    FINANCE_PROVENANCE = "finance_provenance"


@dataclass(frozen=True, slots=True)
class Refusal:
    code: ReasonCode
    field: str | None
    """The frontmatter key the rule judged, or `None` for a rule about the whole note."""
    detail: str


@dataclass(frozen=True, slots=True)
class Verdict:
    path: str
    """The path as resolved for the zone decision — what the verdict is about."""
    crossing: bool
    """Whether this write crosses into curated space at all. `False` means admission was never the
    question, and `refusals` is empty."""
    refusals: tuple[Refusal, ...]

    @property
    def admitted(self) -> bool:
        """Whether admission stands in the way of this write. True for every non-crossing."""
        return not self.refusals

    @property
    def codes(self) -> tuple[ReasonCode, ...]:
        """The distinct reason codes, in first-seen order — what a log line and a counter carry."""
        return tuple(dict.fromkeys(refusal.code for refusal in self.refusals))

    def summary(self) -> str:
        return "; ".join(
            f"{r.code}: {r.detail}" if r.field is None else f"{r.code} ({r.field}): {r.detail}" for r in self.refusals
        )


# Schema file §6, exactly: "(as of YYYY-MM[-DD], <source>)", with a real month and day-of-month and a
# source that is not blank.
_RECENCY_MARKER = re.compile(
    r"\(as of [0-9]{4}-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12][0-9]|3[01]))?, [^()\n]*[^()\s][^()\n]*\)"
)

# A YAML 1.2 core-schema integer, in decimal, unquoted: a quoted "5" is text to every YAML reader,
# and so to every Obsidian view over the property.
_INTEGER = re.compile(r"[-+]?[0-9]+")

_REQUIRED = (
    ("type", ReasonCode.TYPE_MISSING),
    ("authority", ReasonCode.PROVENANCE_MISSING),
    ("trigger", ReasonCode.PROVENANCE_MISSING),
)
_DATE_FIELDS = tuple(field.name for field in FIELDS if field.type is FieldType.DATE)


def admit(path: str, content: str) -> Verdict:
    """Whether `content`, written at `path`, may enter curated space. Pure and total."""
    resolved = _resolve(path)
    if not resolved.startswith(CURATED_PREFIXES):
        return Verdict(resolved, crossing=False, refusals=())

    parsed = parse_note(content)
    if isinstance(parsed, Unparseable):
        refusal = Refusal(ReasonCode.FRONTMATTER_UNPARSEABLE, None, parsed.describe())
        return Verdict(resolved, crossing=True, refusals=(refusal,))

    values = parsed.frontmatter.as_dict()
    refusals = _wrong_types(values)
    mistyped = {refusal.field for refusal in refusals}

    for name, code in _REQUIRED:
        if name not in mistyped and is_empty(values.get(name)):
            refusals.append(Refusal(code, name, f"is {_described(values, name)}"))

    for name in _DATE_FIELDS:
        text = None if name in mistyped else _text(values.get(name))
        if text is not None and parse_date(text) is None:
            refusals.append(Refusal(ReasonCode.DATE_UNPARSEABLE, name, f"{text!r} is not an ISO or year-first date"))

    if resolved.startswith(FINANCE_PREFIX):
        refusals.extend(_finance(values, mistyped, parsed.body))
    return Verdict(resolved, crossing=True, refusals=tuple(refusals))


def _finance(values: dict[str, Value], mistyped: set[str | None], body: str) -> list[Refusal]:
    refusals: list[Refusal] = []
    if "authority" not in mistyped and _text(values.get("authority")) != "import":
        detail = f"is {_described(values, 'authority')}; a finance note needs exactly `import`"
        refusals.append(Refusal(ReasonCode.FINANCE_AUTHORITY, "authority", detail))
    if "confidence" not in mistyped and _text(values.get("confidence")) not in CONFIDENCE_VOCABULARY:
        detail = f"is {_described(values, 'confidence')}; a finance note needs one of {sorted(CONFIDENCE_VOCABULARY)}"
        refusals.append(Refusal(ReasonCode.FINANCE_CONFIDENCE, "confidence", detail))
    if _RECENCY_MARKER.search(body) is None:
        detail = "the body carries no inline `(as of YYYY-MM[-DD], <source>)` marker"
        refusals.append(Refusal(ReasonCode.FINANCE_PROVENANCE, None, detail))
    return refusals


def _resolve(path: str) -> str:
    resolved = posixpath.normpath(path.lstrip("/"))
    while resolved == ".." or resolved.startswith("../"):
        resolved = resolved[3:]
    return resolved


def _wrong_types(values: dict[str, Value]) -> list[Refusal]:
    refusals: list[Refusal] = []
    for name, value in values.items():
        field = FIELD_BY_NAME.get(name)
        if field is None or value is None:
            continue
        if field.type is FieldType.LIST:
            if isinstance(value, Scalar):
                detail = "holds one value where the schema declares a list"
                refusals.append(Refusal(ReasonCode.WRONG_TYPE, name, detail))
        elif isinstance(value, tuple):
            detail = f"holds a list where the schema declares {field.type}"
            refusals.append(Refusal(ReasonCode.WRONG_TYPE, name, detail))
        elif field.type is FieldType.INTEGER and not is_empty(value) and not _is_integer(value):
            detail = f"{value.text!r} is not an unquoted integer"
            refusals.append(Refusal(ReasonCode.WRONG_TYPE, name, detail))
    return refusals


def _is_integer(value: Scalar) -> bool:
    return value.style is Style.PLAIN and _INTEGER.fullmatch(value.text) is not None


def _text(value: Value) -> str | None:
    """A non-empty scalar's text; `None` for anything that says nothing, or is a list."""
    return value.text if isinstance(value, Scalar) and not is_empty(value) else None


def _described(values: dict[str, Value], name: str) -> str:
    if name not in values:
        return "absent"
    text = _text(values[name])
    return "empty" if text is None else repr(text)
