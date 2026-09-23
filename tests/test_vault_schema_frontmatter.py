"""The frontmatter dialect by example: what it reads, what it refuses, and where it says so.

The accepted table is the shapes the vault's writers actually produce — Obsidian's own serialiser
(`yaml` with `lineWidth: 0`, `nullStr: ""`), the schema file's per-type blocks, and the hand-written
forms an agent reaches for — each with the exact value it must read as. The refused table is one
row per construct outside the dialect, each naming the line at fault. The properties in
`test_vault_schema_frontmatter_properties.py` cover the space between the examples.
"""

from __future__ import annotations

import pytest

from obsidian_tools.vault_schema.frontmatter import (
    Frontmatter,
    ParsedNote,
    Scalar,
    Style,
    Unparseable,
    Value,
    emit_note,
    is_empty,
    parse_note,
)

P, S, D = Style.PLAIN, Style.SINGLE, Style.DOUBLE


def fields_of(content: str) -> tuple[tuple[str, Value], ...]:
    parsed = parse_note(content)
    assert isinstance(parsed, ParsedNote), parsed
    return parsed.frontmatter.fields


# --- accepted -----------------------------------------------------------------------------------

_GUI_TEMPLATE_INSTANTIATED = """---
type: note
title: "Untitled"
source:
authority: human
trigger: human
status: inbox
created:
updated:
reviewed:
tags: []
confidence:
related: []
refs: []
---

## Summary
"""


def test_the_gui_template_shape_reads_field_for_field() -> None:
    """The shape Obsidian's Templates plugin writes from `_templates/note.md`. Red if a bare
    `key:` stops reading as empty, or `[]` as the empty list."""
    assert fields_of(_GUI_TEMPLATE_INSTANTIATED) == (
        ("type", Scalar("note")),
        ("title", Scalar("Untitled", D)),
        ("source", None),
        ("authority", Scalar("human")),
        ("trigger", Scalar("human")),
        ("status", Scalar("inbox")),
        ("created", None),
        ("updated", None),
        ("reviewed", None),
        ("tags", ()),
        ("confidence", None),
        ("related", ()),
        ("refs", ()),
    )


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        # Obsidian's list layout, and the schema file's own.
        ("tags:\n  - moc\n  - home-lab\n", ("tags", (Scalar("moc"), Scalar("home-lab")))),
        # Block lists at column 0 and at four spaces are YAML too.
        ("tags:\n- a\n- b\n", ("tags", (Scalar("a"), Scalar("b")))),
        ("tags:\n    - a\n", ("tags", (Scalar("a"),))),
        # A blank line inside a block list does not end it.
        ("tags:\n  - a\n\n  - b\n", ("tags", (Scalar("a"), Scalar("b")))),
        # Flow lists, as agents write them.
        ("tags: [a, b]\n", ("tags", (Scalar("a"), Scalar("b")))),
        ("tags: [ a ,b ]\n", ("tags", (Scalar("a"), Scalar("b")))),
        ("tags: [ ]\n", ("tags", ())),
        ("related: [\"[[Home]]\", '[[log]]']\n", ("related", (Scalar("[[Home]]", D), Scalar("[[log]]", S)))),
        ('tags: ["a, b", c]\n', ("tags", (Scalar("a, b", D), Scalar("c")))),
        # Wikilinks in a block list, quoted as Obsidian quotes them.
        ('related:\n  - "[[Home]]"\n', ("related", (Scalar("[[Home]]", D),))),
        # Quoting: both styles, and their escapes.
        ("title: 'It''s \"fine\"'\n", ("title", Scalar('It\'s "fine"', S))),
        (
            'title: "a \\"b\\" \\\\ \\t \\x41 \\u00e9 \\U0001F600 \\N"\n',
            ("title", Scalar('a "b" \\ \t A \xe9 \U0001f600 \x85', D)),
        ),
        ('title: ""\n', ("title", Scalar("", D))),
        # Plain scalars YAML allows that a stricter reader might not.
        ("title: a:b c#d [x] {y} it's\n", ("title", Scalar("a:b c#d [x] {y} it's"))),
        ("title: -5\n", ("title", Scalar("-5"))),
        ("title: ?why\n", ("title", Scalar("?why"))),
        ("title: C++ / K3s\n", ("title", Scalar("C++ / K3s"))),
        ("title: a\tb\n", ("title", Scalar("a\tb"))),
        ("title: trailing   \n", ("title", Scalar("trailing"))),
        ("title:    spaced\n", ("title", Scalar("spaced"))),
        # Characters Obsidian writes raw, and reads as themselves (YAML 1.2).
        ("title: a\x85b\U00002028c\x7f\ufeff\n", ("title", Scalar("a\x85b\U00002028c\x7f\ufeff"))),
        # Keys are any plain text: Obsidian lets a property be called anything.
        ("Due Date: 2026-07-30\n", ("Due Date", Scalar("2026-07-30"))),
        ("\xe9tat: x\n", ("\xe9tat", Scalar("x"))),
        ("TQ_explain: true\n", ("TQ_explain", Scalar("true"))),
        # Lexical: numbers, booleans and null words stay text-with-a-style.
        ("salience: 5\n", ("salience", Scalar("5"))),
        ('salience: "5"\n', ("salience", Scalar("5", D))),
        ("reviewed: ~\n", ("reviewed", Scalar("~"))),
    ],
)
def test_an_accepted_shape_reads_as_exactly_its_value(block: str, expected: tuple[str, Value]) -> None:
    assert fields_of(f"---\n{block}---\n") == (expected,)


def test_crlf_line_endings_read_as_lf_and_the_body_is_kept_byte_for_byte() -> None:
    """Obsidian's delimiter accepts `\\r\\n`; the body is never the parser's to change."""
    parsed = parse_note("---\r\ntype: note\r\ntags:\r\n  - a\r\n---\r\nbody\r\n")

    assert isinstance(parsed, ParsedNote)
    assert parsed.frontmatter.fields == (("type", Scalar("note")), ("tags", (Scalar("a"),)))
    assert parsed.body == "body\r\n"


@pytest.mark.parametrize(
    ("content", "body"),
    [
        ("---\n---\n", ""),
        ("---\n---", ""),
        ("---\ntype: note\n---", ""),
        ("---\ntype: note\n---\n---\nmore\n", "---\nmore\n"),
        ("---\ntype: a---\n---\nbody", "body"),
        ("---\ntype: note\n----\n---\n", None),
    ],
)
def test_the_block_ends_where_obsidian_ends_it(content: str, body: str | None) -> None:
    """The first later line that is exactly `---`, followed by a line break or the end of the note
    — not a `---` inside a line, and not `----`."""
    parsed = parse_note(content)
    if body is None:
        assert isinstance(parsed, Unparseable)
    else:
        assert isinstance(parsed, ParsedNote), parsed
        assert parsed.body == body


# --- refused ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "line"),
    [
        ("# A heading\n\nno frontmatter\n", None),
        ("\n---\ntype: note\n---\n", None),
        ("--- \ntype: note\n---\n", None),
        ("---\ntype: note\n", None),
        ("---\ntype: note\n--- \n", None),
        ("---\n# a comment\ntype: note\n---\n", 2),
        ("---\ntype: note # a comment\n---\n", 2),
        ("---\ntitle: |\n  two\n  lines\n---\n", 2),
        ("---\ntitle: >-\n  folded\n---\n", 2),
        ("---\ntitle: a long title\n  continued\n---\n", 3),
        ('---\ntitle: "unclosed\n  quote"\n---\n', 2),
        ("---\ntags:\n  - a\n  -   \n---\n", 4),
        ("---\ntags:\n  - a\n  -\n---\n", 4),
        ("---\ntags:\n  - a\n    - b\n---\n", 4),
        ("---\ntags:\n  - - nested\n---\n", 3),
        ("---\ntags:\n  - [nested]\n---\n", 3),
        ("---\ntags:\n  - key: value\n---\n", 3),
        ("---\ntags: [a, [b]]\n---\n", 2),
        ("---\ntags: [a, b\n---\n", 2),
        ("---\ntags: [a, ]\n---\n", 2),
        ("---\ntags: [a,, b]\n---\n", 2),
        ("---\ntags: [a] b\n---\n", 2),
        ('---\ntags: ["a" "b"]\n---\n', 2),
        ("---\ntags: [a:b]\n---\n", 2),
        ("---\nmeta: {a: 1}\n---\n", 2),
        ("---\nmeta:\n  nested: 1\n---\n", 3),
        ("---\n- a\n---\n", 2),
        ("---\ntype: note\n- a\n---\n", 3),
        ("---\ntype: note\ntype: moc\n---\n", 3),
        ('---\n"type": note\n---\n', 2),
        ("---\ntype:note\n---\n", 2),
        ("---\ntype:\tnote\n---\n", 2),
        ("---\n\ttype: note\n---\n", 2),
        ("---\n  type: note\n---\n", 2),
        ("---\ntitle: Node.js: the runtime\n---\n", 2),
        ("---\ntitle: ends with a colon:\n---\n", 2),
        ("---\ntitle: &anchor x\n---\n", 2),
        ("---\ntitle: *alias\n---\n", 2),
        ("---\ntitle: !tag x\n---\n", 2),
        ("---\ntitle: @at\n---\n", 2),
        ("---\ntitle: `tick\n---\n", 2),
        ('---\ntitle: "a" b\n---\n', 2),
        ("---\ntitle: 'a' b\n---\n", 2),
        ('---\ntitle: "bad \\q escape"\n---\n', 2),
        ('---\ntitle: "\\ud800 surrogate"\n---\n', 2),
        ("---\ntitle: a\x07b\n---\n", 2),
        ("---\ntitle: a\rb\n---\n", 2),
        ("---\ntype: note\n...\n---\n", 3),
        ("---\n--- x: note\n---\n", 2),
        ("---\n... x: note\n---\n", 2),
    ],
)
def test_a_construct_outside_the_dialect_is_refused_at_its_line(content: str, line: int | None) -> None:
    """Red if the parser reads something outside the dialect — and so hands a caller a value no
    other reader of the vault would agree with — or blames the wrong line."""
    parsed = parse_note(content)

    assert isinstance(parsed, Unparseable), parsed
    assert parsed.line == line, parsed.describe()


# --- the model's own guarantees ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "style"),
    [("", P), (" lead", P), ("a: b", P), ("- a", P), ("#x", P), ("a\nb", S), ("a\nb", P), ("\ud800", D)],
)
def test_a_scalar_its_style_cannot_carry_is_refused_at_construction(text: str, style: Style) -> None:
    """So every `Scalar` that exists can be emitted and read back unchanged."""
    with pytest.raises(ValueError, match=r"cannot|surrogate"):
        Scalar(text, style)


@pytest.mark.parametrize(
    "fields", [(("a", None), ("a", None)), (("a: b", None),), ((" a", None),), (("--- a", None),), (("...", None),)]
)
def test_a_frontmatter_the_dialect_cannot_write_is_refused_at_construction(
    fields: tuple[tuple[str, Value], ...],
) -> None:
    with pytest.raises(ValueError, match=r"duplicate|key"):
        Frontmatter(fields)


def test_the_emitters_layout_is_canonical() -> None:
    """One layout, whatever the input's: the round trip is what the properties prove; this pins the
    bytes a normalising writer will put in the vault."""
    parsed = parse_note("---\ntype:   note\ntags: [a, 'b']\nrelated: []\nreviewed:\ntitle: 'it''s \"y\"'\n---\nbody\n")
    assert isinstance(parsed, ParsedNote)

    assert emit_note(parsed) == (
        "---\ntype: note\ntags:\n  - a\n  - 'b'\nrelated: []\nreviewed:\ntitle: 'it''s \"y\"'\n---\nbody\n"
    )


def test_the_emitter_escapes_what_only_yaml_1_2_reads_raw() -> None:
    """Double-quoted output escapes the characters YAML 1.1 reads differently, so what this module
    writes reads the same to every YAML reader."""
    note = ParsedNote(Frontmatter((("title", Scalar('a\x85b\U00002028c\x7f\ufeff\x00"\\', D)),)), "")

    assert emit_note(note) == '---\ntitle: "a\\Nb\\Lc\\x7f\\ufeff\\0\\"\\\\"\n---\n'


@pytest.mark.parametrize(
    ("value", "empty"),
    [
        (None, True),
        ((), True),
        (Scalar("", D), True),
        (Scalar("null"), True),
        (Scalar("NULL"), True),
        (Scalar("~"), True),
        (Scalar("null", D), False),
        (Scalar("nil"), False),
        ((Scalar("a"),), False),
        (Scalar("0"), False),
    ],
)
def test_empty_means_says_nothing_to_any_reader(value: Value, empty: bool) -> None:
    assert is_empty(value) is empty
