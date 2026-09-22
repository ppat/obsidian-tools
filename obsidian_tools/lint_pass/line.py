"""The S2 tolerance line (unit A6, ot#84): every class of badness the schema file defines, and its tier.

S2's criterion is falsifiable only against a written statement of what is tolerated
(`USE_CASES.md`, S2). This table is that statement, beside the checks it scopes. The report's
header restates it in prose from here, so the two cannot drift.

The principle behind the tiers: **refuse what breaks the machine-readability of the frontmatter or
its provenance; accept deviations in vocabulary and style, and report the ones the schema file
names.** A field of the wrong type silently breaks every view over that property; a value outside
a vocabulary breaks nothing silently and can be found by a query.

- **refused** — a curated crossing carrying it is refused by the admission validator. A curated note
  already in that state is reported at the top rank: the pass cannot refuse what is already there.
- **fixed** — the pass rewrites the frontmatter, never overwriting an existing value, and logs the
  change to the audit trail.
- **reported** — accepted, and reported; it may reach the digest.
- **tolerated** — accepted silently: there is nothing to report against.
- **not checked** — outside this pass. Silence here is not a verdict.

**The refused rows are the validator's reason codes, imported rather than restated** — one home
per fact, and the direction ADR-0007 requires: the lint pass depends on the validator, never the
reverse. Adding a code there adds a row here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from obsidian_tools.admission.validator import ReasonCode


class Tier(StrEnum):
    REFUSED = "refused"
    FIXED = "fixed"
    REPORTED = "reported"
    TOLERATED = "tolerated"
    NOT_CHECKED = "not checked"


class Check(StrEnum):
    """The reported classes: what the pass reports that the validator does not refuse."""

    TRIGGER_AUTHORITY = "trigger_authority_contradiction"
    UNSTAMPED = "unstamped"
    CONFIDENCE_EMPTY = "confidence_empty"
    OUT_OF_VOCABULARY = "out_of_vocabulary"
    SLUG_MISMATCH = "slug_mismatch"
    TITLE_UNSLUGGABLE = "title_unsluggable"
    OPTIONAL_FIELD_EMPTY = "optional_field_empty"
    REMOVED_SINGULAR_KEY = "removed_singular_key"
    BANNED_CHARACTER = "banned_character"
    DANGLING_LINK = "dangling_link"
    ORPHAN = "orphan"
    STALE = "stale"
    AGENT_ZONE_NONCONFORMING = "agent_zone_nonconforming"


class Fix(StrEnum):
    """The fixed classes: each kind of change normalisation may make."""

    KEY_ORDER = "key_order"
    DATE_FORMAT = "date_format"
    TAG_CASE = "tag_case"
    KEY_INSERTED = "key_inserted"
    DATE_STAMPED = "date_stamped"


class Tolerated(StrEnum):
    TAG_SHAPE = "tag_shape"
    SALIENCE_RANGE = "salience_range"


class NotChecked(StrEnum):
    JUDGMENT = "judgment"
    REFS_STALENESS = "refs_staleness"
    NEAR_DUPLICATES = "near_duplicates"
    BROKEN_QUERIES = "broken_queries"
    TASKS_AND_MARKERS = "tasks_and_markers"
    NUMBERS_OUTSIDE_FINANCE = "numbers_outside_finance"
    BODY_FIXES = "body_fixes"


type FindingClass = ReasonCode | Check
"""What a finding is about: a refused-tier condition, or a reported class."""

type RowName = ReasonCode | Check | Fix | Tolerated | NotChecked


@dataclass(frozen=True, slots=True)
class Row:
    name: RowName
    tier: Tier
    statement: str


_REFUSED = tuple(Row(code, Tier.REFUSED, f"`{code}`, as the admission validator decides it") for code in ReasonCode)

_FIXED = (
    Row(Fix.KEY_ORDER, Tier.FIXED, "Keys out of the schema file's canonical order are reordered; unknown keys follow"),
    Row(Fix.DATE_FORMAT, Tier.FIXED, "A date in an accepted year-first form is rewritten as ISO `YYYY-MM-DD`"),
    Row(Fix.TAG_CASE, Tier.FIXED, "Tags are lowercased"),
    Row(
        Fix.KEY_INSERTED,
        Tier.FIXED,
        "A missing required key is inserted empty; `consolidated` and `salience` stay absent. Never filled "
        "with a value: `type`, `source`, `authority`, `trigger`, `confidence` are claims, not shapes",
    ),
    Row(
        Fix.DATE_STAMPED,
        Tier.FIXED,
        "A missing or empty `created:` or `updated:` is stamped with the file's modification date on the mount",
    ),
)

_REPORTED = (
    Row(
        Check.TRIGGER_AUTHORITY,
        Tier.REPORTED,
        "`trigger: schedule` with `authority: human` - which field is wrong is a judgment, so it is not fixed",
    ),
    Row(Check.UNSTAMPED, Tier.REPORTED, "An empty or absent `source:`: a note written at the GUI"),
    Row(Check.CONFIDENCE_EMPTY, Tier.REPORTED, "An empty or absent `confidence:`"),
    Row(
        Check.OUT_OF_VOCABULARY,
        Tier.REPORTED,
        "A `type`, `source`, `status` or `confidence` value outside its vocabulary (`confidence` outside finance, "
        "where it is refused)",
    ),
    Row(Check.SLUG_MISMATCH, Tier.REPORTED, "A filename stem that is not `slug(title)`. Never renamed"),
    Row(Check.TITLE_UNSLUGGABLE, Tier.REPORTED, "A `title` whose slug is empty, or no `title` at all"),
    Row(Check.OPTIONAL_FIELD_EMPTY, Tier.REPORTED, "`salience` or `consolidated` present but empty"),
    Row(Check.REMOVED_SINGULAR_KEY, Tier.REPORTED, "The removed singular keys `tag`, `alias`, `cssclass`"),
    Row(
        Check.BANNED_CHARACTER,
        Tier.REPORTED,
        "Smart quotes, en and em dashes, non-breaking and zero-width spaces, anywhere in the note",
    ),
    Row(
        Check.DANGLING_LINK,
        Tier.REPORTED,
        "A wikilink whose target matches no filename stem, vault path or `aliases:` entry, exactly",
    ),
    Row(Check.ORPHAN, Tier.REPORTED, "A curated note no other note links to"),
    Row(
        Check.STALE,
        Tier.REPORTED,
        "A curated note whose `reviewed:` age exceeds its area's dial; an empty `reviewed:` ages from `created:`",
    ),
    Row(
        Check.AGENT_ZONE_NONCONFORMING,
        Tier.REPORTED,
        "A refused-tier condition on a note outside curated space: reported, never refused (the agent zone is "
        "detective only)",
    ),
)

_TOLERATED = (
    Row(
        Tolerated.TAG_SHAPE,
        Tier.TOLERATED,
        "Tags malformed beyond case (spaces, a leading `#`): tags have no vocabulary",
    ),
    Row(
        Tolerated.SALIENCE_RANGE,
        Tier.TOLERATED,
        "A `salience` integer outside 1-10: the type is right, and the consolidation pass normalises within a batch",
    ),
)

_NOT_CHECKED = (
    Row(NotChecked.JUDGMENT, Tier.NOT_CHECKED, "Contradictions, and staleness judged by a model"),
    Row(NotChecked.REFS_STALENESS, Tier.NOT_CHECKED, "Push-based staleness over the `refs:` graph"),
    Row(NotChecked.NEAR_DUPLICATES, Tier.NOT_CHECKED, "Near-duplicates (deliberately not built)"),
    Row(NotChecked.BROKEN_QUERIES, Tier.NOT_CHECKED, "Broken queries"),
    Row(NotChecked.TASKS_AND_MARKERS, Tier.NOT_CHECKED, "Task bracket format, and sentinel-marker integrity"),
    Row(NotChecked.NUMBERS_OUTSIDE_FINANCE, Tier.NOT_CHECKED, "Recency markers and numbers outside finance"),
    Row(
        NotChecked.BODY_FIXES,
        Tier.NOT_CHECKED,
        "Body fixes (banned-character replacement, dead-link repair): both are reported instead, because content "
        "outside markers is human-owned",
    ),
)

LINE: tuple[Row, ...] = _REFUSED + _FIXED + _REPORTED + _TOLERATED + _NOT_CHECKED

TIER_OF: dict[RowName, Tier] = {row.name: row.tier for row in LINE}

EXEMPT_STATEMENT = (
    "Exempt, and silent: `05-raw/` (silence about raw is S2 working); `.obsidian/` and every other dot-folder; "
    "`_templates/`, `_attachments/`; the pass's own `_ops/lint/` and `_ops/audit/`; `_ops/quarantine/` (counted "
    "as depth, not re-judged); `90-archive/`; and the fixed root files. Every exempt file still counts as a link "
    "target."
)
