"""Frontmatter normalisation: the fixed tier of the tolerance line. Pure — no I/O, no clock.

The lint pass is the vault's one authority on frontmatter shape, exercised here in the scheduled
pass and never on save, so the validator's approval cannot be silently reshaped afterwards
(ADR-0018, DESIGN.md's one-authority table). What it may do is the schema file's §5 list, and what
it may never do is the half that matters:

- **Never overwrite an existing value.** Two transforms rewrite a value's spelling without changing
  what it says — a year-first date to its ISO form, a tag to lower case. Everything else a pass
  writes goes into a key that was absent or empty. "Overwriting a value a human set is the fastest
  way to lose trust in the whole pass" (ADR-0018).
- **Never fill a claim.** A missing `type`, `source`, `authority`, `trigger` or `confidence` is
  inserted *empty*: each is a claim about the note, not a shape (schema file §5), and an empty
  claim is reported, where an invented one would be believed.
- **Never touch the body.** The body rides through untouched; `emit_note` writes it byte for byte.

A value this cannot read as its declared shape is left exactly as found. A mistyped field is the
validator's to refuse and the report's to show, not this module's to reinterpret.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date

from obsidian_tools.lint_pass.line import Fix
from obsidian_tools.vault_schema.dates import parse_date
from obsidian_tools.vault_schema.fields import FIELDS, FieldType
from obsidian_tools.vault_schema.frontmatter import Frontmatter, Scalar, Value, is_empty

_CANONICAL_INDEX = {field.name: index for index, field in enumerate(FIELDS)}
_DATE_FIELDS = tuple(field.name for field in FIELDS if field.type is FieldType.DATE)
_STAMPED = ("created", "updated")
"""The two dates a pass without history can still state truthfully: the file's modification date is
the closest fact it holds (plan §3). `reviewed:` is a human's act and is never stamped."""

_INSERTED = tuple(field.name for field in FIELDS if field.required and field.name not in _STAMPED)


@dataclass(frozen=True, slots=True)
class Change:
    kind: Fix
    key: str | None
    """The frontmatter key changed; `None` for a reordering, which is about the block."""
    before: Value
    after: Value
    """The value before and after, `None` meaning absent or empty. For `KEY_ORDER`, the keys
    themselves, in their order before and after."""


def normalise(frontmatter: Frontmatter, *, modified: date) -> tuple[Frontmatter, tuple[Change, ...]]:
    """The normalised block and every change made to reach it. No change means nothing to write."""
    fields = dict(frontmatter.fields)
    changes: list[Change] = []

    for name in _DATE_FIELDS:
        value = fields.get(name)
        if isinstance(value, Scalar) and not is_empty(value):
            parsed = parse_date(value.text)
            if parsed is not None and parsed.isoformat() != value.text:
                fields[name] = Scalar(parsed.isoformat())
                changes.append(Change(Fix.DATE_FORMAT, name, value, fields[name]))

    tags = fields.get("tags")
    if isinstance(tags, tuple):
        lowered = tuple(_lowered(tag) for tag in tags)
        if lowered != tags:
            fields["tags"] = lowered
            changes.append(Change(Fix.TAG_CASE, "tags", tags, lowered))

    for name in _STAMPED:
        value = fields.get(name)
        if is_empty(value) and not isinstance(value, tuple):
            fields[name] = Scalar(modified.isoformat())
            changes.append(Change(Fix.DATE_STAMPED, name, value, fields[name]))

    for name in _INSERTED:
        if name not in fields:
            fields[name] = None
            changes.append(Change(Fix.KEY_INSERTED, name, None, None))

    original = tuple(key for key, _ in frontmatter.fields)
    order = _order_key(original)
    if list(original) != sorted(original, key=order):
        reordered = sorted(original, key=order)
        changes.append(Change(Fix.KEY_ORDER, None, _keys(original), _keys(reordered)))
    normalised = Frontmatter(tuple((key, fields[key]) for key in sorted(fields, key=order)))
    return normalised, tuple(changes)


def _keys(keys: Iterable[str]) -> tuple[Scalar, ...]:
    # A key the dialect can hold is plain-safe by construction (`Frontmatter` refuses any other).
    return tuple(Scalar(key) for key in keys)


def _lowered(tag: Scalar) -> Scalar:
    """The tag in lower case, in the style it was written in — or unchanged, if lower case cannot
    be carried in that style (a character whose lower-case form YAML reads differently)."""
    try:
        return Scalar(tag.text.lower(), tag.style)
    except ValueError:
        return tag


def _order_key(original: tuple[str, ...]) -> Callable[[str], tuple[int, int]]:
    position = {key: index for index, key in enumerate(original)}

    def key(name: str) -> tuple[int, int]:
        canonical = _CANONICAL_INDEX.get(name)
        # Known keys in the schema file's order; every other key after them, in the order found.
        return (0, canonical) if canonical is not None else (1, position[name])

    return key
