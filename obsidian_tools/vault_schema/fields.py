"""The schema file's §3 table: every declared frontmatter field, its type, and its vocabulary.

`FIELDS` is in canonical key order — the table's row order, with the two optional lists `aliases`
and `cssclasses` directly after `title`, as §3 places them. A key not declared here is not this
module's to judge; whether it belongs on a note is the per-type block rule (§3.1), which is lint
policy, not schema fact.

One field means one type, vault-wide (§3): a field is declared once, with one `FieldType`, and
nothing here varies by `type:` or by area.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FieldType(StrEnum):
    TEXT = "text"
    DATE = "date"
    INTEGER = "integer"
    """§3 says "number, integer 1 to 10". The range is not part of the type: a `salience:` of 12
    is the right type and the wrong value, and the consolidation pass normalises within a batch
    anyway (ADR-0012)."""
    LIST = "list"


@dataclass(frozen=True, slots=True)
class Field:
    name: str
    type: FieldType
    required: bool
    """Whether §3 marks it required. `reviewed` is required-present, may-be-empty; `consolidated`
    and `salience` are the two fields left absent, never written empty."""
    vocabulary: frozenset[str] | None = None
    """The closed set of values §3 lists, where it lists one."""


CONFIDENCE_VOCABULARY = frozenset({"high", "medium", "speculation"})
"""ADR-0011: purely epistemic, three levels."""

FIELDS: tuple[Field, ...] = (
    Field(
        "type",
        FieldType.TEXT,
        required=True,
        vocabulary=frozenset(
            {"note", "source", "entity", "concept", "project", "task", "decision"}
            | {"devlog", "meeting", "research", "moc"}
        ),
    ),
    Field("title", FieldType.TEXT, required=True),
    Field("aliases", FieldType.LIST, required=False),
    Field("cssclasses", FieldType.LIST, required=False),
    Field(
        "source",
        FieldType.TEXT,
        required=True,
        vocabulary=frozenset({"openclaw", "n8n", "claude-code", "vault-worker", "batch-processor", "drift-channel"}),
    ),
    Field("authority", FieldType.TEXT, required=True, vocabulary=frozenset({"human", "agent", "import"})),
    Field("trigger", FieldType.TEXT, required=True, vocabulary=frozenset({"human", "schedule", "event"})),
    Field(
        "status", FieldType.TEXT, required=True, vocabulary=frozenset({"inbox", "processed", "evergreen", "archived"})
    ),
    Field("created", FieldType.DATE, required=True),
    Field("updated", FieldType.DATE, required=True),
    Field("reviewed", FieldType.DATE, required=True),
    Field("tags", FieldType.LIST, required=True),
    Field("confidence", FieldType.TEXT, required=True, vocabulary=CONFIDENCE_VOCABULARY),
    Field("related", FieldType.LIST, required=True),
    Field("refs", FieldType.LIST, required=True),
    Field("consolidated", FieldType.DATE, required=False),
    Field("salience", FieldType.INTEGER, required=False),
)

FIELD_BY_NAME: dict[str, Field] = {field.name: field for field in FIELDS}
