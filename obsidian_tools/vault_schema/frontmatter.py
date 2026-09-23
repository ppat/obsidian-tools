"""The frontmatter dialect: a strict subset of YAML, parsed and emitted with the standard library.

## Why a subset, and why no YAML library

The runtime image is built for two architectures by copying one virtual environment into both
(`Dockerfile`, `pyproject.toml`), so no dependency with a compiled extension may be added — and the
mainstream Python YAML parsers ship one. The schema's own types need very little YAML anyway: text,
dates, one integer, and flat lists of those. Owning the subset also makes `frontmatter_unparseable`
mean something exact: *outside this dialect*, rather than whatever one library version happens to
tolerate.

## The dialect

The block is delimited exactly as Obsidian delimits it: the note's first line is `---`, and the
block ends at the first later line that is exactly `---`, each followed by `\\n`, `\\r\\n`, or (for
the closing one) the end of the note. Inside it, line by line:

| Line | Meaning |
| --- | --- |
| `key: value` at column 0 | A scalar — plain, `'single'`, or `"double"` quoted, all on that line |
| `key: [a, "b"]` | A flow list of scalars, on that line; `[]` is the empty list |
| `key:` followed by `- item` lines | A block list of scalars, every item at one indentation (0 or more spaces) |
| `key:` followed by no items | Empty — present, with no value |
| blank | Ignored |

A key is any text YAML reads unquoted (`due date`, `état`). Everything else is outside the dialect
and refused: comments, block scalars (`|`, `>`), values continued onto a second line, nested lists
or mappings, anchors, aliases and tags, flow mappings, quoted keys, tabs as indentation or as the
separator after `key:`, duplicate keys, and raw C0 control characters other than tab.

## The reference is YAML 1.2 as Obsidian writes and reads it

Obsidian is the vault's other reader and its only GUI writer. Obsidian 1.12.7 serialises
frontmatter through the `yaml` library (YAML 1.2) with `lineWidth: 0` (never fold a long value onto
a second line) and `nullStr: ""` (write an empty value as a bare `key:`): lists in block style at
two spaces, the empty list as `[]`, a scalar quoted only when it must be. Those options are read
from the application bundle's own `stringifyYaml`, not assumed. Every value it writes parses here
to the value it was given, with one exception: a value containing a line break, which it writes as
a block scalar. No field the schema declares holds one.

That makes the dialect a subset of YAML 1.2, not of YAML 1.1. The two differ on characters Obsidian
does write raw: DEL and the C1 controls, U+2028 and U+2029, and a tab inside a plain scalar are
text to YAML 1.2, but a YAML 1.1 reader such as PyYAML refuses them or reads NEL as a line break.
This module reads them as Obsidian does. Its emitter escapes them in every double-quoted scalar; a
plain or single-quoted scalar, which has no escapes, carries them raw, exactly as Obsidian wrote it.

## The model is lexical, on purpose

A value is kept as its text *and* its quoting style; nothing is resolved to a number, boolean or
date. `salience: 5` and `salience: "5"` are therefore different values — as they are to Obsidian,
whose YAML 1.2 reading makes the first an integer and the second text — and a caller judging a
field's type can see which it got. Resolving here instead would bake one YAML version's resolution
rules into every caller, and YAML 1.1 and 1.2 disagree on exactly the plain words (`yes`, `on`)
nobody expects.

## The emitter is canonical, and the round trip is the contract

`emit_note` writes one layout whatever the input's: `key: value`, lists in block style at two
spaces, `[]` for the empty list, a bare `key:` for empty, each scalar in the style it carries, keys
in the order given. `parse_note(emit_note(note)) == note` holds for every note this module can
represent — that is what lets a caller rewrite a note's frontmatter without changing any value in
it, which is the whole of what normalisation may do.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

# --- the model ------------------------------------------------------------------------------------


class Style(StrEnum):
    PLAIN = "plain"
    SINGLE = "single"
    DOUBLE = "double"


@dataclass(frozen=True, slots=True)
class Scalar:
    """One scalar value, as its text and the quoting style it is written in.

    A style that cannot carry the text — a plain scalar YAML would read as something else, a
    single-quoted one spanning a line break — is refused at construction, so every `Scalar` that
    exists can be emitted and read back unchanged.
    """

    text: str
    style: Style = Style.PLAIN

    def __post_init__(self) -> None:
        if self.style is Style.PLAIN and not is_plain_safe(self.text):
            raise ValueError(f"{self.text!r} cannot be written as a plain scalar")
        if self.style is Style.SINGLE and not _is_single_safe(self.text):
            raise ValueError(f"{self.text!r} cannot be written as a single-quoted scalar")
        if self.style is Style.DOUBLE and _SURROGATE.search(self.text) is not None:
            raise ValueError(f"{self.text!r} holds a lone surrogate, which no YAML document can carry")


type Value = Scalar | tuple[Scalar, ...] | None
"""A field's value: one scalar, a list of scalars, or `None` for present-and-empty (`key:`)."""


@dataclass(frozen=True, slots=True)
class Frontmatter:
    fields: tuple[tuple[str, Value], ...]
    """Every key and its value, in the order they appear."""

    def __post_init__(self) -> None:
        keys = [key for key, _ in self.fields]
        if len(set(keys)) != len(keys):
            raise ValueError(f"duplicate keys in {keys!r}")
        for key in keys:
            if not is_key_safe(key):
                raise ValueError(f"{key!r} is not a key this dialect can write unquoted")

    def as_dict(self) -> dict[str, Value]:
        return dict(self.fields)


@dataclass(frozen=True, slots=True)
class ParsedNote:
    frontmatter: Frontmatter
    body: str
    """Everything after the closing delimiter's line, byte for byte."""


@dataclass(frozen=True, slots=True)
class Unparseable:
    """Why a note's frontmatter is outside the dialect. `line` is 1-based within the whole note,
    or `None` when the problem is the block's absence or delimiting rather than one line."""

    reason: str
    line: int | None

    def describe(self) -> str:
        return self.reason if self.line is None else f"line {self.line}: {self.reason}"


def is_empty(value: Value) -> bool:
    """Whether a value says nothing: `key:`, an empty string, a YAML null word, or an empty list."""
    if value is None:
        return True
    if isinstance(value, tuple):
        return not value
    if value.text == "":
        return True
    return value.style is Style.PLAIN and value.text in _NULL_WORDS


# The null words both YAML 1.1 and 1.2 agree on. A plain scalar spelled this way is empty to every
# reader of the vault, so it is empty here too, whatever its lexical form.
_NULL_WORDS = frozenset({"null", "Null", "NULL", "~"})

# --- characters -----------------------------------------------------------------------------------

# The raw characters no line of the block may hold: the C0 controls other than tab (a line break
# ends the line; a raw control is a control nobody meant) and lone surrogates, which no encoding
# carries. Everything else is read as itself, because Obsidian both writes and reads it so -
# including DEL, the C1 controls, U+2028/2029 and the byte-order mark, which Obsidian's YAML 1.2
# serialiser leaves raw (measured) and a YAML 1.1 reader would refuse or fold.
_FORBIDDEN_RAW = re.compile("[\x00-\x08\x0a-\x1f\ud800-\udfff]")

# What the emitter escapes inside double quotes: everything outside YAML 1.2's printable set, plus
# the characters YAML 1.1 reads as line breaks and the byte-order mark. The parser accepts them raw;
# a double-quoted scalar this module writes never carries them raw, so it reads the same under either
# YAML version.
_ESCAPED_ON_EMIT = re.compile("[^\t\x20-\x7e\xa0-\u2027\u202a-\ud7ff\ue000-\ufefe\uff00-\ufffd\U00010000-\U0010ffff]")
_SURROGATE = re.compile("[\ud800-\udfff]")

# A plain scalar may not start with any of these; YAML reads each as the start of other syntax. `-`,
# and outside a flow list `?` and `:`, may start one when a non-space follows (`-5`, `?x`), as YAML
# itself allows and Obsidian writes.
_PLAIN_FORBIDDEN_FIRST = frozenset("-?:,[]{}#&*!|>'\"%@`")
_FLOW_FORBIDDEN = frozenset(",[]{}:?")


def is_plain_safe(text: str, *, flow: bool = False) -> bool:
    """Whether `text` can be written unquoted and read back as exactly this text.

    YAML 1.2's plain-scalar rule, made stricter in one place and never looser: inside a flow list, no
    `:` or `?` at all, since YAML 1.1 readers end a plain scalar there and no writer of this vault
    puts either in a flow list.
    """
    if not text or text[0] in " \t" or text[-1] in " \t" or _FORBIDDEN_RAW.search(text):
        return False
    first = text[0]
    if first in _PLAIN_FORBIDDEN_FIRST:
        followed = len(text) > 1 and text[1] not in " \t"
        if not (followed and (first == "-" or (first in "?:" and not flow))):
            return False
    if any(indicator in text for indicator in (": ", ":\t", " #", "\t#")) or text.endswith(":"):
        return False
    return not (flow and any(c in _FLOW_FORBIDDEN for c in text))


def is_key_safe(text: str) -> bool:
    """Whether `text` can be a top-level key: plain-safe, and not starting with `---` or `...`,
    which at the start of a line YAML reads as a document marker rather than as text."""
    return is_plain_safe(text) and not text.startswith(("---", "..."))


def _is_single_safe(text: str) -> bool:
    return _FORBIDDEN_RAW.search(text) is None


# --- parsing --------------------------------------------------------------------------------------

# Obsidian's own delimiter expressions, translated: an opening `---` on the note's first line, and a
# closing `---` that starts a line and ends at a line break or at the end of the note.
_OPEN = re.compile(r"---\r?\n")
_CLOSE = re.compile(r"---(?:\r?\n|\Z)")

_ITEM_LINE = re.compile(r"(?P<indent> *)-(?:(?P<sep> +)(?P<rest>.*))?")

_ESCAPES = {
    "0": "\x00",
    "a": "\x07",
    "b": "\x08",
    "t": "\t",
    "\t": "\t",
    "n": "\n",
    "v": "\x0b",
    "f": "\x0c",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
    "N": "\x85",
    "_": "\xa0",
    "L": "\u2028",
    "P": "\u2029",
}
_HEX_ESCAPES = {"x": 2, "u": 4, "U": 8}


class _Refused(Exception):
    """Internal to the parser: unwinds to `parse_note`, which returns it as an `Unparseable`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.line: int | None = None


def parse_note(content: str) -> ParsedNote | Unparseable:
    """The note's frontmatter and body, or why its frontmatter is outside the dialect. Total: never
    raises on any input."""
    opening = _OPEN.match(content)
    if opening is None:
        return Unparseable("the note does not open with a `---` frontmatter delimiter", None)
    start = opening.end()
    position = start
    while True:
        closing = _CLOSE.search(content, position)
        if closing is None:
            return Unparseable("the frontmatter block has no closing `---` line", None)
        if content[closing.start() - 1] == "\n":
            break
        position = closing.end()

    lines = content[start : closing.start()].split("\n")[:-1]
    try:
        fields = _parse_lines(lines)
    except _Refused as refused:
        return Unparseable(refused.reason, refused.line)
    return ParsedNote(Frontmatter(fields), content[closing.end() :])


def _parse_lines(lines: list[str]) -> tuple[tuple[str, Value], ...]:
    fields: list[tuple[str, Value]] = []
    seen: set[str] = set()
    open_list: tuple[str, list[Scalar]] | None = None
    item_indent: int | None = None
    for index, raw in enumerate(lines):
        try:
            open_list, item_indent = _parse_line(raw.removesuffix("\r"), fields, seen, open_list, item_indent)
        except _Refused as refused:
            refused.line = index + 2  # the opening delimiter is line 1
            raise
    if open_list is not None:
        key, items = open_list
        fields.append((key, tuple(items) if items else None))
    return tuple(fields)


def _parse_line(
    line: str,
    fields: list[tuple[str, Value]],
    seen: set[str],
    open_list: tuple[str, list[Scalar]] | None,
    item_indent: int | None,
) -> tuple[tuple[str, list[Scalar]] | None, int | None]:
    """Consume one line into `fields`; returns the list still open under an empty key (and the
    indentation its items use), which only the next line can close."""
    if _FORBIDDEN_RAW.search(line):
        raise _Refused("holds a control character or lone surrogate, unescaped")
    if not line.strip(" "):
        return open_list, item_indent

    item = _ITEM_LINE.fullmatch(line)
    if item is not None:
        if open_list is None:
            raise _Refused("a list item with no empty `key:` line above it")
        indent = len(item["indent"])
        if item_indent is not None and indent != item_indent:
            raise _Refused("list items at inconsistent indentation")
        open_list[1].append(_scalar((item["rest"] or "").rstrip(" "), flow=False))
        return open_list, indent

    key, rest = _split_key_line(line)
    if open_list is not None:
        list_key, items = open_list
        fields.append((list_key, tuple(items) if items else None))
    if key in seen:
        raise _Refused(f"the key {key!r} appears twice")
    seen.add(key)
    rest = rest.strip(" ")
    if not rest:
        return (key, []), None
    fields.append((key, _flow_list(rest) if rest.startswith("[") else _scalar(rest, flow=False)))
    return None, None


def _split_key_line(line: str) -> tuple[str, str]:
    """A top-level `key: value` line's key, and everything after the `: ` separating it.

    A key is anything YAML reads unquoted as that exact text — the same rule as a plain scalar, so
    a property named `due date` or `état` is as readable here as it is to Obsidian. The first `: `
    ends it; a quoted key is outside the dialect.
    """
    separator = line.find(": ")
    if separator == -1:
        if not line.endswith(":"):
            raise _Refused("neither a `key: value` line nor a list item under one")
        key, rest = line[:-1], ""
    else:
        key, rest = line[:separator], line[separator + 2 :]
    if not is_key_safe(key):
        raise _Refused(f"{key!r} is not a key this dialect reads unquoted")
    return key, rest


def _scalar(text: str, *, flow: bool) -> Scalar:
    """One complete scalar occupying all of `text` (already stripped of trailing spaces)."""
    if text.startswith('"'):
        value, end = _double_quoted(text, 0)
        if end != len(text):
            raise _Refused("text after a closing double quote")
        return Scalar(value, Style.DOUBLE)
    if text.startswith("'"):
        value, end = _single_quoted(text, 0)
        if end != len(text):
            raise _Refused("text after a closing single quote")
        return Scalar(value, Style.SINGLE)
    if not is_plain_safe(text, flow=flow):
        raise _Refused(f"{text!r} is not a scalar this dialect reads unquoted")
    return Scalar(text, Style.PLAIN)


def _double_quoted(text: str, start: int) -> tuple[str, int]:
    """The value of the double-quoted scalar opening at `text[start]`, and the index after it."""
    out: list[str] = []
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == '"':
            return "".join(out), i + 1
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= len(text):
            break
        code = text[i + 1]
        if code in _ESCAPES:
            out.append(_ESCAPES[code])
            i += 2
            continue
        width = _HEX_ESCAPES.get(code)
        digits = text[i + 2 : i + 2 + width] if width else ""
        if width is None or len(digits) != width or not all(c in "0123456789abcdefABCDEF" for c in digits):
            raise _Refused(f"an unknown escape `\\{code}` in a double-quoted scalar")
        point = int(digits, 16)
        if point > 0x10FFFF or 0xD800 <= point <= 0xDFFF:
            raise _Refused(f"the escape `\\{code}{digits}` names no character")
        out.append(chr(point))
        i += 2 + width
    raise _Refused("a double-quoted scalar is not closed on its line")


def _single_quoted(text: str, start: int) -> tuple[str, int]:
    out: list[str] = []
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == "'":
            if text[i + 1 : i + 2] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    raise _Refused("a single-quoted scalar is not closed on its line")


def _flow_list(text: str) -> tuple[Scalar, ...]:
    """A `[...]` list occupying all of `text`. Items are scalars; nothing nests."""
    items: list[Scalar] = []
    i = 1
    while True:
        while i < len(text) and text[i] == " ":
            i += 1
        if i >= len(text):
            raise _Refused("a flow list is not closed on its line")
        if text[i] == "]" and not items:
            i += 1
            break
        if text[i] == '"':
            value, i = _double_quoted(text, i)
            items.append(Scalar(value, Style.DOUBLE))
        elif text[i] == "'":
            value, i = _single_quoted(text, i)
            items.append(Scalar(value, Style.SINGLE))
        else:
            end = i
            while end < len(text) and text[end] not in ",]":
                end += 1
            items.append(_scalar(text[i:end].rstrip(" "), flow=True))
            i = end
        while i < len(text) and text[i] == " ":
            i += 1
        if i >= len(text):
            raise _Refused("a flow list is not closed on its line")
        if text[i] == "]":
            i += 1
            break
        if text[i] != ",":
            raise _Refused("flow list items must be separated by `,`")
        i += 1
    if i != len(text):
        raise _Refused("text after a flow list's closing `]`")
    return tuple(items)


# --- emitting -------------------------------------------------------------------------------------

_NAMED_OUT = {
    "\x00": "\\0",
    "\x07": "\\a",
    "\x08": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\x0b": "\\v",
    "\x0c": "\\f",
    "\r": "\\r",
    "\x1b": "\\e",
    "\x85": "\\N",
    "\u2028": "\\L",
    "\u2029": "\\P",
    '"': '\\"',
    "\\": "\\\\",
}


def emit_scalar(scalar: Scalar) -> str:
    match scalar.style:
        case Style.PLAIN:
            return scalar.text
        case Style.SINGLE:
            return "'" + scalar.text.replace("'", "''") + "'"
        case Style.DOUBLE:
            return '"' + "".join(_escape(ch) for ch in scalar.text) + '"'


def _escape(ch: str) -> str:
    named = _NAMED_OUT.get(ch)
    if named is not None:
        return named
    if _ESCAPED_ON_EMIT.match(ch) is None:
        return ch
    point = ord(ch)
    return f"\\x{point:02x}" if point <= 0xFF else f"\\u{point:04x}" if point <= 0xFFFF else f"\\U{point:08x}"


def emit_frontmatter(frontmatter: Frontmatter) -> str:
    """The block's lines, without the delimiters, each ending in `\\n`."""
    lines: list[str] = []
    for key, value in frontmatter.fields:
        if value is None:
            lines.append(f"{key}:")
        elif isinstance(value, Scalar):
            lines.append(f"{key}: {emit_scalar(value)}")
        elif not value:
            lines.append(f"{key}: []")
        else:
            lines.append(f"{key}:")
            lines.extend(f"  - {emit_scalar(item)}" for item in value)
    return "".join(f"{line}\n" for line in lines)


def emit_note(note: ParsedNote) -> str:
    return f"---\n{emit_frontmatter(note.frontmatter)}---\n{note.body}"
