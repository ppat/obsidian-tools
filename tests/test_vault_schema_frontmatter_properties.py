"""Properties of the frontmatter dialect — the one place in this package with real oracles.

Three, each independent of the implementation's own logic:

- **Round trip.** `parse_note(emit_note(note)) == note` for every representable note. The inverse
  is the oracle: an emitter that drops an escape, or a parser that mis-reads one, breaks it.
- **Differential against PyYAML, over surface variants.** The same note written the ways its
  writers write it — flow or block lists, any indentation, extra separator spaces, trailing spaces,
  blank lines, CRLF — must read as that note here, *and* as the same values to PyYAML. PyYAML is a
  test-only dependency and a second, unrelated implementation of the grammar.
- **Soundness under hostile input.** Whenever this parser accepts a block built from YAML's
  syntax characters, PyYAML accepts it too and reads the same values: the dialect is never looser
  than YAML, whatever a writer throws at it.

PyYAML reads YAML 1.1; the dialect's reference is YAML 1.2, as Obsidian reads it
(`vault_schema/frontmatter.py`). The two differ on DEL, the C1 controls, U+2028/2029 and tabs
inside plain scalars, so the differential and soundness properties keep those out of unquoted and
single-quoted text — the only places they can appear raw. The round trip covers them, and the
examples pin Obsidian's reading of each.
"""

from __future__ import annotations

import yaml
from hypothesis import event, given
from hypothesis import strategies as st

from obsidian_tools.vault_schema.fields import FIELDS
from obsidian_tools.vault_schema.frontmatter import (
    Frontmatter,
    ParsedNote,
    Scalar,
    Style,
    Value,
    emit_note,
    emit_scalar,
    is_key_safe,
    is_plain_safe,
    parse_note,
)

# --- strategies ---------------------------------------------------------------------------------

_SYNTAX = " :-#'\"[]{},?&*!|>%@`\\~"
_ANY_CHARACTER = st.characters(exclude_categories=("Cs",))
_YAML_1_1_DIVERGENT = frozenset({chr(c) for c in range(0x7F, 0xA0)} | {"\t", "\U00002028", "\U00002029"})


def _texts(alphabet: st.SearchStrategy[str]) -> st.SearchStrategy[str]:
    # Mostly short words over the syntax characters, where the quoting decisions live; some
    # arbitrary text, where the escaping decisions live.
    return st.one_of(st.text(st.sampled_from("abcAB19 " + _SYNTAX), max_size=8), st.text(alphabet, max_size=8))


def _carried(text: str, *, shared: bool) -> list[Style]:
    styles: list[Style] = [Style.DOUBLE]
    if shared and any(c in _YAML_1_1_DIVERGENT for c in text):
        return styles
    for style in (Style.PLAIN, Style.SINGLE):
        try:
            Scalar(text, style)
        except ValueError:
            continue
        styles.append(style)
    return styles


def _scalars(*, shared: bool) -> st.SearchStrategy[Scalar]:
    return _texts(_ANY_CHARACTER).flatmap(
        lambda text: st.sampled_from(_carried(text, shared=shared)).map(lambda style: Scalar(text, style))
    )


def _values(*, shared: bool) -> st.SearchStrategy[Value]:
    scalars = _scalars(shared=shared)
    return st.one_of(st.none(), scalars, st.lists(scalars, max_size=4).map(tuple))


_KEYS = st.one_of(
    st.sampled_from([field.name for field in FIELDS] + ["Due Date", "\xe9tat", "TQ_explain", "null", "?x", "-x"]),
    st.text(st.sampled_from("abZ9_-. "), min_size=1, max_size=4).filter(is_key_safe),
)


def _notes(*, shared: bool) -> st.SearchStrategy[ParsedNote]:
    fields = st.lists(st.tuples(_KEYS, _values(shared=shared)), max_size=8, unique_by=lambda kv: kv[0])
    return st.builds(ParsedNote, fields.map(lambda f: Frontmatter(tuple(f))), st.text(max_size=20))


def _pyyaml(block: str) -> object:
    """PyYAML's reading of a block: BaseLoader constructs every scalar as its text, so this compares
    structure and text and never YAML 1.1's type resolution."""
    return yaml.load(block, Loader=yaml.BaseLoader) or {}


def _as_pyyaml_reads(fields: tuple[tuple[str, Value], ...]) -> dict[str, object]:
    """What PyYAML's BaseLoader — which constructs every scalar as its text — makes of each value."""
    return {
        key: "" if value is None else value.text if isinstance(value, Scalar) else [item.text for item in value]
        for key, value in fields
    }


# --- round trip ---------------------------------------------------------------------------------


@given(_notes(shared=False))
def test_emit_then_parse_is_the_identity(note: ParsedNote) -> None:
    """Red if any value, style, key order or body byte changes across a write and a read — the
    whole of what a normalising writer is allowed to rely on."""
    assert parse_note(emit_note(note)) == note


# --- differential, over the ways a note is actually written -----------------------------------


@st.composite
def _written_variously(draw: st.DrawFn) -> tuple[ParsedNote, str, str]:
    note = draw(_notes(shared=True))
    newline = draw(st.sampled_from(["\n", "\r\n"]))
    lines: list[str] = []
    for key, value in note.frontmatter.fields:
        separator = " " * draw(st.integers(1, 3))
        if isinstance(value, tuple) and value and draw(st.booleans()) and all(_flow_writable(i) for i in value):
            lines.append(f"{key}:{separator}[" + ", ".join(emit_scalar(item) for item in value) + "]")
        elif isinstance(value, tuple) and value:
            indent = " " * draw(st.sampled_from([0, 2, 4]))
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"{indent}-{' ' * draw(st.integers(1, 2))}{emit_scalar(item)}")
                if draw(st.booleans()):
                    lines.append("")
        elif isinstance(value, tuple):
            lines.append(f"{key}:{separator}[]")
        elif value is None:
            lines.append(f"{key}:")
        else:
            lines.append(f"{key}:{separator}{emit_scalar(value)}")
    lines = [line + " " * draw(st.integers(0, 2)) for line in lines]
    block = "".join(line + newline for line in lines)
    return note, block, f"---{newline}{block}---{newline}{note.body}"


def _flow_writable(item: Scalar) -> bool:
    return item.style is not Style.PLAIN or is_plain_safe(item.text, flow=True)


@given(_written_variously())
def test_every_way_of_writing_a_note_reads_as_that_note_here_and_to_pyyaml(case: tuple[ParsedNote, str, str]) -> None:
    """Red if a layout the dialect admits reads differently here than the canonical one does, or if
    PyYAML reads any value differently — a disagreement between two independent readers of the
    same bytes, which is exactly what a caller's verdict must never rest on."""
    note, block, written = case

    assert parse_note(written) == note
    assert _pyyaml(block) == _as_pyyaml_reads(note.frontmatter.fields)


# --- soundness under hostile input ------------------------------------------------------------

_HOSTILE = st.sampled_from(list("ab1 " + _SYNTAX))
_HOSTILE_TEXT = st.text(_HOSTILE, max_size=6)
_HOSTILE_KEY = st.text(st.sampled_from(list("ab.-_ " + _SYNTAX)), min_size=1, max_size=4)


def _key_value_line(key: str, value: str) -> str:
    return f"{key}: {value}"


def _key_line(key: str) -> str:
    return f"{key}:"


def _item_line(indent: int, value: str) -> str:
    return " " * indent + "- " + value


_HOSTILE_LINE = st.one_of(
    _HOSTILE_TEXT,
    st.builds(_key_value_line, _HOSTILE_KEY, _HOSTILE_TEXT),
    st.builds(_key_line, _HOSTILE_KEY),
    st.builds(_item_line, st.integers(0, 3), _HOSTILE_TEXT),
)


@st.composite
def _blocks_with_one_hostile_line(draw: st.DrawFn) -> str:
    """A valid context — plain `key: value` lines, and a `key:` opening a list — with one hostile
    line inserted anywhere in it. One hostile line per block keeps a fair share of blocks accepted
    (a property that only ever sees refusals checks nothing): measured at about one in five."""
    values = draw(st.lists(st.text(st.sampled_from("ab1_."), max_size=5), max_size=4))
    lines = [f"k{index}: {value}" if value else f"k{index}:" for index, value in enumerate(values)]
    lines.insert(draw(st.integers(0, len(lines))), draw(_HOSTILE_LINE))
    return "".join(f"{line}\n" for line in lines if line.rstrip(" ") != "---")


@given(_blocks_with_one_hostile_line())
def test_whatever_this_parser_accepts_pyyaml_reads_the_same(block: str) -> None:
    """Red if the dialect is ever looser than YAML: a block accepted here that PyYAML refuses, or
    reads as other values. The case this exists for is the one nobody writes as an example — a
    syntax character in a position the hand-written tables never put it."""
    parsed = parse_note(f"---\n{block}---\n")

    if not isinstance(parsed, ParsedNote):
        event("refused")
        return
    event("accepted")
    assert _pyyaml(block) == _as_pyyaml_reads(parsed.frontmatter.fields)
