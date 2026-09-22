"""Normalisation never overwrites a value (plan row 12) — the one lint mechanism with a real oracle.

The property is phrased as what must never happen, not as a second copy of the rules: whatever the
input, every key that was there is still there; every value that said something still says it —
the only permitted rewrites are a date's spelling and a tag's case, and each is checked against its
own meaning, not against the code's; a key that was absent is either left absent or inserted empty,
except `created`/`updated`, which may be stamped with the file's date; the body is byte-identical;
and a second pass over the output changes nothing.

The examples beside it pin the transforms the property only bounds.
"""

from __future__ import annotations

from datetime import date

from hypothesis import event, example, given
from hypothesis import strategies as st

from obsidian_tools.lint_pass.line import Fix
from obsidian_tools.lint_pass.normalise import normalise
from obsidian_tools.vault_schema.dates import parse_date
from obsidian_tools.vault_schema.fields import FIELDS, FieldType
from obsidian_tools.vault_schema.frontmatter import (
    Frontmatter,
    ParsedNote,
    Scalar,
    Style,
    Value,
    emit_note,
    is_empty,
    is_key_safe,
    parse_note,
)

_DATE_FIELDS = {field.name for field in FIELDS if field.type is FieldType.DATE}
_STAMPED = {"created", "updated"}
_NEVER_INSERTED = {"consolidated", "salience", "aliases", "cssclasses"}


def _scalar(text: str) -> st.SearchStrategy[Scalar]:
    styles = [style for style in Style if _carries(text, style)]
    return st.sampled_from(styles).map(lambda style: Scalar(text, style))


def _carries(text: str, style: Style) -> bool:
    try:
        Scalar(text, style)
    except ValueError:
        return False
    return True


_WORDS = st.text(st.sampled_from("abcXYZ-# 19:/.\u0130\u00df"), min_size=0, max_size=8)
_DATE_TEXTS = st.one_of(
    st.dates(min_value=date(1900, 1, 1), max_value=date(2999, 12, 31)).flatmap(
        lambda d: st.sampled_from(
            [
                d.isoformat(),
                f"{d.year}/{d.month}/{d.day}",
                f"{d.year}.{d.month:02}.{d.day}",
                f"{d.day}/{d.month}/{d.year}",
            ]
        )
    ),
    _WORDS,
)
_LISTS = st.lists(_WORDS.flatmap(_scalar), max_size=3).map(tuple)


def _value_for(key: str) -> st.SearchStrategy[Value]:
    # Weighted towards each field's own shape, so the permitted rewrites — a date's spelling, a tag's
    # case — are reached at the CI profile's example count, not only at depth; a property that never
    # meets the transforms it bounds proves nothing about them.
    if key in _DATE_FIELDS:
        shaped = _DATE_TEXTS.flatmap(_scalar)
    elif key == "tags":
        shaped = st.lists(
            st.text(st.sampled_from("aBC-# \u0130"), min_size=1, max_size=5).flatmap(_scalar), min_size=1, max_size=3
        ).map(tuple)
    else:
        shaped = _WORDS.flatmap(_scalar)
    return st.one_of(*[shaped] * 4, st.none(), _WORDS.flatmap(_scalar), _LISTS)


_NAMES = [field.name for field in FIELDS] + ["tag", "Due Date", "zz"]


@st.composite
def _frontmatter(draw: st.DrawFn) -> Frontmatter:
    """The schema's keys and a few others, some dropped, in canonical order or shuffled."""
    kept = [name for name in _NAMES if draw(st.booleans())]
    extra = draw(st.lists(st.text(st.sampled_from("abq_"), min_size=1, max_size=3).filter(is_key_safe), max_size=2))
    keys = list(dict.fromkeys([*kept, *extra]))
    if draw(st.booleans()):
        keys = draw(st.permutations(keys))
    return Frontmatter(tuple((key, draw(_value_for(key))) for key in keys))


def _says_something(value: Value) -> bool:
    return isinstance(value, tuple) or not is_empty(value)


# Always evaluated, whatever the random search reaches: each permitted rewrite, an empty stampable
# date, a claim to insert, a reordering, and an unknown key riding along.
_EVERY_TRANSFORM = Frontmatter(
    (
        ("title", Scalar("T", Style.DOUBLE)),
        ("type", Scalar("note")),
        ("created", Scalar("2026/7/3")),
        ("updated", None),
        ("reviewed", Scalar("3/7/2026")),
        ("tags", (Scalar("Home"), Scalar("x\u0130", Style.SINGLE))),
        ("Due Date", Scalar("Friday")),
    )
)


@given(_frontmatter(), st.text(max_size=30), st.dates())
@example(_EVERY_TRANSFORM, "body\r\n---\n", date(2026, 9, 20))
def test_normalisation_never_overwrites_a_value_never_touches_the_body_and_is_idempotent(
    frontmatter: Frontmatter, body: str, modified: date
) -> None:
    before = frontmatter.as_dict()
    normalised, changes = normalise(frontmatter, modified=modified)
    after = normalised.as_dict()

    for key, old in before.items():
        assert key in after, f"{key} was removed"
        new = after[key]
        if not _says_something(old):
            assert new == old or (key in _STAMPED and new == Scalar(modified.isoformat())), f"{key} was filled"
            continue
        if new == old:
            continue
        # The two spellings that may change, each checked against what it means:
        if key in _DATE_FIELDS and isinstance(old, Scalar) and isinstance(new, Scalar):
            event("a date respelled")
            assert parse_date(old.text) is not None
            assert parse_date(new.text) == parse_date(old.text)
            assert new.text == parse_date(new.text).isoformat()  # type: ignore[union-attr]
            continue
        if key == "tags" and isinstance(old, tuple) and isinstance(new, tuple):
            event("a tag recased")
            assert len(new) == len(old)
            for o, n in zip(old, new, strict=True):
                assert n.style is o.style
                assert n.text in (o.text, o.text.lower())
            continue
        raise AssertionError(f"{key}: {old!r} was overwritten with {new!r}")

    for key in after.keys() - before.keys():
        event("a key inserted")
        assert key not in _NEVER_INSERTED
        assert after[key] is None or (key in _STAMPED and after[key] == Scalar(modified.isoformat()))

    if [k for k, _ in normalised.fields] != [k for k, _ in frontmatter.fields]:
        event("keys reordered")

    written = emit_note(ParsedNote(normalised, body))
    assert parse_note(written) == ParsedNote(normalised, body), "the post-image does not read back as planned"

    assert (changes == ()) == (normalised == frontmatter)
    assert normalise(normalised, modified=modified)[1] == ()


# --- the transforms, pinned -----------------------------------------------------------------------


def _fm(*fields: tuple[str, Value]) -> Frontmatter:
    return Frontmatter(fields)


_MODIFIED = date(2026, 9, 20)


def test_a_year_first_date_is_respelled_iso_and_a_day_first_one_is_left_alone() -> None:
    fm = _fm(("created", Scalar("2026/7/3")), ("updated", Scalar("03/07/2026")))
    normalised, _ = normalise(fm, modified=_MODIFIED)
    assert normalised.as_dict()["created"] == Scalar("2026-07-03")
    assert normalised.as_dict()["updated"] == Scalar("03/07/2026")


def test_tags_are_lowercased_in_their_own_style() -> None:
    fm = _fm(("tags", (Scalar("Home Lab", Style.DOUBLE), Scalar("K8S"))))
    normalised, changes = normalise(fm, modified=_MODIFIED)
    assert normalised.as_dict()["tags"] == (Scalar("home lab", Style.DOUBLE), Scalar("k8s"))
    assert Fix.TAG_CASE in [c.kind for c in changes]


def test_created_and_updated_are_stamped_only_when_missing_or_empty() -> None:
    fm = _fm(("created", None), ("updated", Scalar("2026-01-01")))
    normalised, _ = normalise(fm, modified=_MODIFIED)
    assert normalised.as_dict()["created"] == Scalar("2026-09-20")
    assert normalised.as_dict()["updated"] == Scalar("2026-01-01")
    absent, _ = normalise(_fm(), modified=_MODIFIED)
    assert absent.as_dict()["created"] == Scalar("2026-09-20")


def test_claims_are_inserted_empty_and_the_optional_pair_stays_absent() -> None:
    normalised, _ = normalise(_fm(), modified=_MODIFIED)
    values = normalised.as_dict()
    for claim in ("type", "source", "authority", "trigger", "confidence", "title", "status", "reviewed"):
        assert values[claim] is None
    for absent in _NEVER_INSERTED:
        assert absent not in values


def test_keys_take_the_canonical_order_and_unknown_keys_follow_in_their_own_order() -> None:
    fm = _fm(("zz", Scalar("1")), ("title", Scalar("T")), ("aa", Scalar("2")), ("type", Scalar("note")))
    normalised, changes = normalise(fm, modified=_MODIFIED)
    keys = [k for k, _ in normalised.fields]
    assert keys[:2] == ["type", "title"]
    assert keys[-2:] == ["zz", "aa"]
    assert Fix.KEY_ORDER in [c.kind for c in changes]


def test_a_mistyped_date_is_not_reinterpreted() -> None:
    fm = _fm(("created", (Scalar("2026/7/3"),)))
    normalised, _ = normalise(fm, modified=_MODIFIED)
    assert normalised.as_dict()["created"] == (Scalar("2026/7/3"),)
