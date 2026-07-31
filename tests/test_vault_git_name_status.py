"""Tests for `obsidian_tools/vault_git/name_status.py` — the pure `-z` porcelain parser, tested as
a pure function over hand-built NUL-delimited strings rather than by shelling out to git.

This is the one module in this codebase with a demonstrated defect *class*: `core.quotePath`
C-quoting, the three-field rename/copy shape, and non-UTF-8 filenames arriving as surrogate-escaped
`str` (see the module docstring). `test_vault_git_commit.py::test_commit_message_survives_a_quotepath_hostile_path`
and `test_commands_commit.py::test_non_utf8_filename_does_not_wedge_the_committer` keep the
real-git, real-subprocess integration coverage for the same defect class; this file is where the
adversarial character-level cases live, cheaply.
"""

from __future__ import annotations

from obsidian_tools.vault_git.name_status import NameStatusEntry, parse_name_status, split_nul_terminated

# --- split_nul_terminated ----------------------------------------------------------------------


def test_splits_on_nul_and_drops_the_trailing_empty_field() -> None:
    """Real `-z` output always ends with a NUL, so a plain `str.split("\\0")` would otherwise hand
    back one spurious empty trailing entry every time."""
    assert split_nul_terminated("a\0b\0c\0") == ["a", "b", "c"]


def test_empty_output_splits_to_no_entries() -> None:
    assert split_nul_terminated("") == []


def test_never_splits_on_a_literal_newline() -> None:
    """The entire reason `-z` is used instead of the line-oriented form: a filename containing a
    literal newline must stay one field, not desync the split."""
    assert split_nul_terminated("weird\nname.md\0other.md\0") == ["weird\nname.md", "other.md"]


def test_never_unescapes_quotepath_style_quoting() -> None:
    """`-z` output is never C-quoted by `core.quotePath` in the first place, so a literal quote or
    backslash in a filename must survive completely untouched -- this function has no unescaping
    logic to accidentally apply to it."""
    assert split_nul_terminated('a "quoted" file.md\0back\\slash.md\0') == ['a "quoted" file.md', "back\\slash.md"]


def test_non_ascii_and_surrogate_escaped_content_passes_through_unchanged() -> None:
    """Non-ASCII text, and a lone surrogate codepoint standing in for an undecodable byte (PEP 383
    `surrogateescape`, applied upstream by `GitRunner.run` before this function ever sees the
    string) -- both must round-trip exactly as given."""
    non_utf8_stand_in = "caf\udce9.md"  # what b"caf\xe9.md" decodes to under surrogateescape
    assert split_nul_terminated(f"日本語.md\0{non_utf8_stand_in}\0") == ["日本語.md", non_utf8_stand_in]


# --- parse_name_status -------------------------------------------------------------------------


def test_empty_output_parses_to_no_entries() -> None:
    assert parse_name_status("") == []


def test_ordinary_two_field_statuses() -> None:
    for status in ("A", "M", "D"):
        assert parse_name_status(f"{status}\0note.md\0") == [NameStatusEntry(status=status, path="note.md")]


def test_rename_is_the_three_field_r100_old_new_shape() -> None:
    assert parse_name_status("R100\0old.md\0new.md\0") == [
        NameStatusEntry(status="R100", path="new.md", old_path="old.md")
    ]


def test_rename_similarity_percentage_other_than_100_is_still_recognized() -> None:
    """Only the leading `R`/`C` letter is significant -- the similarity index after it (`R087`,
    not just `R100`) must not need special-casing for the three-field shape to apply."""
    assert parse_name_status("R087\0old.md\0new.md\0") == [
        NameStatusEntry(status="R087", path="new.md", old_path="old.md")
    ]


def test_copy_is_also_the_three_field_shape() -> None:
    assert parse_name_status("C100\0source.md\0copy.md\0") == [
        NameStatusEntry(status="C100", path="copy.md", old_path="source.md")
    ]


def test_mixed_sequence_of_two_and_three_field_records_stays_aligned() -> None:
    """A rename record consuming the wrong number of fields would desync every record after it --
    this proves a full realistic sequence stays aligned end to end."""
    output = "A\0added.md\0R095\0old.md\0renamed.md\0D\0deleted.md\0M\0edited.md\0"
    assert parse_name_status(output) == [
        NameStatusEntry(status="A", path="added.md"),
        NameStatusEntry(status="R095", path="renamed.md", old_path="old.md"),
        NameStatusEntry(status="D", path="deleted.md"),
        NameStatusEntry(status="M", path="edited.md"),
    ]


def test_paths_with_embedded_newlines_quotes_and_non_ascii_are_preserved_verbatim() -> None:
    output = 'M\0weird\nname "with quotes" 日本語.md\0'
    assert parse_name_status(output) == [NameStatusEntry(status="M", path='weird\nname "with quotes" 日本語.md')]


def test_rename_with_a_hostile_new_path_still_lands_in_path_not_old_path() -> None:
    output = 'R100\0plain-old.md\0new "hostile" \n名前.md\0'
    assert parse_name_status(output) == [
        NameStatusEntry(status="R100", path='new "hostile" \n名前.md', old_path="plain-old.md")
    ]
