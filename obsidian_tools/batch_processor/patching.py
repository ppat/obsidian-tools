"""The chunk's patch, applied. Pure — no git, no filesystem, no MCP.

The processor has no git and no mount (DESIGN.md's "one writer, one door"), so `git apply` is not
available to it even in principle: the only bytes it can reach are the ones it read back through
the gated MCP path, and the only way it can write them back is a whole-note write through the same
door. Applying the unified diff is therefore this component's own arithmetic, and it lives here as
a pure function so that `tests/test_batch_processor_patching.py` can check it against real `git
apply` on real repositories — an oracle outside this file, rather than a restatement of it.

## Three details that silently corrupt content if handled the obvious way

- **Split on `"\\n"`, never `str.splitlines()`.** `splitlines()` also breaks on `\\r`, `\\x0b`,
  `\\x1c`, `\\x85` and `\\u2028`, every one of which is legal inside a markdown line. A CRLF note run
  through it comes back with every line ending rewritten, which is the same class of silent damage
  the producer's binary git reads exist to avoid one component upstream (`GitRunner.run_binary`) —
  and the diff's own line separator really is `\\n` regardless of what the content's is, so a `\\r`
  belongs to the line, not to the split.
- **`\\ No newline at end of file` binds to a side, not to a file.** The marker describes whichever
  side the line before it belongs to: after a `-` line it is a fact about the old content only,
  after a `+` line about the new content only, after a context line about both. Applying it to the
  output unconditionally strips a trailing newline the patch never removed.
- **A create's hunk header is `@@ -0,0 +1,n @@`, and so is an insertion's.** With a zero old count,
  the old start is the line *after which* the insertion lands rather than the first line replaced,
  so it is an index already, not an index plus one. Off by one here inserts every added block one
  line late, everywhere, quietly.

## Ordering: everything that creates content runs before anything that removes it

`plan_writes` emits creates and modifies first and deletes last. A crash between the two halves of
a rename then leaves the note at both paths — visible, repairable, and caught by the next chunk's
own pre-flight — rather than at neither. "Fail loud, destroy nothing" decides the order; nothing
about the patch format requires it.

## `targets` is the pre-flight, and the patch is the instruction — they must agree

`plan_writes` refuses a chunk whose patch writes a path the chunk's `targets` never declared, or
declares a target the patch never writes. The two are generated together by the producer, so a
disagreement is a defect on one side or the other; letting it through would mean applying a write
whose staleness was never checked, which is exactly the hole the pre-flight exists to close.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from obsidian_tools.batch_producer.chunk import Chunk
from obsidian_tools.batch_producer.staleness import TargetOperation

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

_DEV_NULL = "/dev/null"

# git's own C-style escapes, everything outside them being an octal byte. Bytes rather than
# characters throughout: `\303\251` is one UTF-8 character in two escapes, so decoding per character
# would produce mojibake for every accented note in the vault.
_C_ESCAPES = {"a": 0x07, "b": 0x08, "f": 0x0C, "n": 0x0A, "r": 0x0D, "t": 0x09, "v": 0x0B, "\\": 0x5C, '"': 0x22}

# Header lines a git patch carries that say nothing about content. Listed rather than skipped by
# "anything before the first @@": a line this module does not recognise inside a file's header is a
# patch shape it has never seen, and guessing at it is how a mode change or a binary body gets
# treated as an ordinary edit.
_IGNORED_HEADER_PREFIXES = (
    "index ",
    "old mode ",
    "new mode ",
    "new file mode ",
    "deleted file mode ",
    "similarity index ",
    "dissimilarity index ",
)


class PatchError(ValueError):
    """The patch cannot be applied as given. Always names the file and what disagreed."""


class WriteKind(StrEnum):
    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[tuple[str, str], ...]
    """`(kind, text)` per line, where kind is one of `' '`, `'-'`, `'+'`, `'\\'`."""


@dataclass(frozen=True, slots=True)
class FileDiff:
    old_path: str | None
    """`None` for a create — the diff's old side is `/dev/null`."""

    new_path: str | None
    """`None` for a delete."""

    hunks: tuple[Hunk, ...]


@dataclass(frozen=True, slots=True)
class PlannedWrite:
    """One call the processor will make on the gated MCP path."""

    path: str
    kind: WriteKind
    content: str | None
    """The whole note, post-application. `None` exactly for a delete."""


def parse_patch(patch: str) -> tuple[FileDiff, ...]:
    """Split a unified diff into one `FileDiff` per file it touches."""
    lines = patch.split("\n")
    diffs: list[FileDiff] = []
    index = 0
    while index < len(lines):
        if not lines[index].startswith("diff --git "):
            index += 1
            continue
        diff, index = _parse_file_diff(lines, index)
        diffs.append(diff)
    if not diffs:
        raise PatchError("the patch contains no file diffs")
    return tuple(diffs)


def _parse_file_diff(lines: list[str], start: int) -> tuple[FileDiff, int]:
    header = lines[start]
    index = start + 1
    old_path: str | None = None
    new_path: str | None = None
    saw_paths = False
    rename_from: str | None = None
    rename_to: str | None = None

    while index < len(lines):
        line = lines[index]
        if line.startswith("diff --git ") or line.startswith("@@"):
            break
        if line.startswith("--- "):
            old_path, saw_paths = _header_path(line[4:], "a/", header), True
        elif line.startswith("+++ "):
            new_path, saw_paths = _header_path(line[4:], "b/", header), True
        elif line.startswith("rename from "):
            rename_from = _unquoted(line.removeprefix("rename from "), header)
        elif line.startswith("rename to "):
            rename_to = _unquoted(line.removeprefix("rename to "), header)
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            # The producer refuses a diff that is not valid UTF-8, but a binary file staged in a
            # repository configured to diff it textually still arrives here as this content-free
            # placeholder. Applying it would write the placeholder's absence as an empty note.
            raise PatchError(f"{header!r}: the patch carries a binary body, which cannot be applied through MCP")
        elif line.startswith("copy from ") or line.startswith("copy to "):
            # `--find-renames` is on and copy detection is off at the producer's git seam, so a copy
            # header means the diff was not generated by the path this contract assumes.
            raise PatchError(f"{header!r}: copy detection is not part of the chunk contract")
        elif not (line == "" or line.startswith(_IGNORED_HEADER_PREFIXES)):
            raise PatchError(f"{header!r}: unrecognised header line {line!r}")
        index += 1

    if not saw_paths:
        # A pure rename with no content change carries no `---`/`+++` pair at all.
        if rename_from is None or rename_to is None:
            raise PatchError(f"{header!r}: the file diff names neither a path pair nor a rename")
        old_path, new_path = rename_from, rename_to

    hunks: list[Hunk] = []
    while index < len(lines) and lines[index].startswith("@@"):
        hunk, index = _parse_hunk(lines, index, header)
        hunks.append(hunk)

    if old_path is None and new_path is None:
        raise PatchError(f"{header!r}: the file diff has no path on either side")
    return FileDiff(old_path=old_path, new_path=new_path, hunks=tuple(hunks)), index


def _header_path(raw: str, prefix: str, header: str) -> str | None:
    # git appends an optional tab-separated timestamp to the `---`/`+++` paths; a path that itself
    # contains a tab is C-quoted, so the tab inside it is `\t` and never a real one.
    candidate = raw.split("\t", 1)[0]
    if candidate == _DEV_NULL:
        return None
    # Exactly one prefix, and the one belonging to this side: stripping both in turn would take
    # `a/b/note.md` down to `note.md`, silently relocating every note in a directory called `b`.
    return _unquoted(candidate, header).removeprefix(prefix)


def _unquoted(candidate: str, header: str) -> str:
    """A patch path, with git's C-style quoting undone if it is present.

    **Every non-ASCII path in a real chunk arrives quoted, measured.** `core.quotePath` defaults to
    on, and the producer's git seam scrubs the operator's global and system config precisely so its
    output does not depend on that config — which leaves the built-in default in force. So a note
    named `café.md` reaches this parser as `"a/caf\\303\\251.md"`, and a reader that refused quoted
    paths would refuse every accented note in the vault.

    Decoding a path that is about to become a write target is the part worth being nervous about,
    and it is checked rather than trusted: `plan_writes` requires the decoded path to be one the
    chunk's `targets` already declared, and those come from git's `-z` output, which is never
    quoted. A wrong decode therefore fails loudly as a target disagreement instead of writing to a
    path nobody named.
    """
    if not candidate.startswith('"'):
        return candidate
    if not candidate.endswith('"') or len(candidate) < 2:
        raise PatchError(f"{header!r}: the quoted path {candidate} is not closed")
    decoded = bytearray()
    body = candidate[1:-1]
    index = 0
    while index < len(body):
        character = body[index]
        if character != "\\":
            decoded.extend(character.encode("utf-8"))
            index += 1
            continue
        index += 1
        if index >= len(body):
            raise PatchError(f"{header!r}: the quoted path {candidate} ends in an escape")
        escape = body[index]
        if escape in _C_ESCAPES:
            decoded.append(_C_ESCAPES[escape])
            index += 1
        elif escape in "01234567":
            digits = body[index : index + 3]
            if len(digits) < 3 or any(digit not in "01234567" for digit in digits):
                raise PatchError(f"{header!r}: the quoted path {candidate} holds a truncated octal escape")
            decoded.append(int(digits, 8))
            index += 3
        else:
            raise PatchError(f"{header!r}: the quoted path {candidate} holds an unknown escape {escape!r}")
    try:
        return decoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A path that is not UTF-8 cannot cross the JSON wire the chunk arrived on, so this is
        # unreachable from a well-formed chunk — and loud rather than replaced, because a path with
        # replacement characters in it is a write to the wrong note.
        raise PatchError(f"{header!r}: the quoted path {candidate} does not decode as UTF-8: {exc}") from exc


def _parse_hunk(lines: list[str], start: int, header: str) -> tuple[Hunk, int]:
    match = _HUNK_HEADER.match(lines[start])
    if match is None:
        raise PatchError(f"{header!r}: malformed hunk header {lines[start]!r}")
    old_start, old_raw, new_start, new_raw = match.groups()
    old_count = 1 if old_raw is None else int(old_raw)
    new_count = 1 if new_raw is None else int(new_raw)

    body: list[tuple[str, str]] = []
    old_seen = 0
    new_seen = 0
    index = start + 1
    # Driven by the header's own counts rather than by "until the next `@@`": a context line whose
    # content happens to begin with `@@` is ordinary markdown, and stopping on it would truncate the
    # hunk and silently drop the rest of the file's edit.
    while index < len(lines) and (old_seen < old_count or new_seen < new_count):
        line = lines[index]
        if line == "":
            # git writes an empty context line as a single space, but a patch that has been through
            # a whitespace-trimming pipeline arrives with it bare. Reading it as anything but
            # context would desynchronise every line after it.
            body.append((" ", ""))
            old_seen += 1
            new_seen += 1
        elif line.startswith("\\"):
            body.append(("\\", line))
        else:
            kind, text = line[0], line[1:]
            if kind == " ":
                old_seen += 1
                new_seen += 1
            elif kind == "-":
                old_seen += 1
            elif kind == "+":
                new_seen += 1
            else:
                raise PatchError(f"{header!r}: unrecognised line {line!r} inside a hunk")
            body.append((kind, text))
        index += 1

    if old_seen != old_count or new_seen != new_count:
        raise PatchError(
            f"{header!r}: hunk at -{old_start} declares {old_count}/{new_count} lines but carries {old_seen}/{new_seen}"
        )
    # A trailing `\ No newline at end of file` sits after the last counted line, so the count-driven
    # loop above stops just short of it.
    if index < len(lines) and lines[index].startswith("\\"):
        body.append(("\\", lines[index]))
        index += 1
    return Hunk(int(old_start), old_count, int(new_start), new_count, tuple(body)), index


def apply_file_diff(pre_image: str | None, diff: FileDiff) -> str | None:
    """The note's content after `diff` is applied to `pre_image`. `None` means the note is deleted.

    Raises `PatchError` whenever the patch's context does not match `pre_image` — which, given the
    pre-flight has already confirmed the content hash the patch was generated against, means the
    patch itself disagrees with what the producer claimed to have generated it from.
    """
    if diff.new_path is None:
        return None

    source, source_ends_with_newline = _to_lines(pre_image or "")
    out: list[str] = []
    cursor = 0
    tail_touched = False
    new_side_unterminated = False

    for hunk in diff.hunks:
        start = hunk.old_start if hunk.old_count == 0 else hunk.old_start - 1
        if not cursor <= start <= len(source):
            raise PatchError(
                f"{diff.new_path!r}: hunk at -{hunk.old_start} starts outside the note's {len(source)} lines"
            )
        out.extend(source[cursor:start])
        cursor = start
        previous_kind = ""
        for kind, text in hunk.lines:
            if kind == "\\":
                if previous_kind in (" ", "+"):
                    new_side_unterminated = True
                continue
            if kind in (" ", "-"):
                if cursor >= len(source) or source[cursor] != text:
                    found = source[cursor] if cursor < len(source) else "<end of note>"
                    raise PatchError(
                        f"{diff.new_path!r}: line {cursor + 1} is {found!r}, but the patch expected {text!r}"
                    )
                cursor += 1
            if kind in (" ", "+"):
                out.append(text)
            previous_kind = kind
        if cursor >= len(source):
            tail_touched = True

    out.extend(source[cursor:])
    ends_with_newline = (not new_side_unterminated) if tail_touched else source_ends_with_newline
    return _from_lines(out, ends_with_newline)


def plan_writes(chunk: Chunk, pre_images: Mapping[str, str | None]) -> tuple[PlannedWrite, ...]:
    """Every write the chunk's patch performs, in the order the processor should perform them.

    `pre_images` is the content read back for each target path — `None` for a path that is absent,
    which is what a create's pre-flight has just established.
    """
    declared = {target.path: target.operation for target in chunk.targets}
    writes: list[PlannedWrite] = []
    deletes: list[PlannedWrite] = []

    for diff in parse_patch(chunk.patch):
        if diff.new_path is not None:
            kind = WriteKind.CREATE if diff.old_path is None or diff.old_path != diff.new_path else WriteKind.MODIFY
            content = apply_file_diff(pre_images.get(diff.old_path or diff.new_path), diff)
            writes.append(PlannedWrite(diff.new_path, kind, content))
        if diff.old_path is not None and diff.old_path != diff.new_path:
            deletes.append(PlannedWrite(diff.old_path, WriteKind.DELETE, None))

    planned = tuple(writes + deletes)
    _check_against_targets(planned, declared)
    return planned


def _check_against_targets(planned: tuple[PlannedWrite, ...], declared: Mapping[str, TargetOperation]) -> None:
    expected = {
        WriteKind.CREATE: TargetOperation.CREATE,
        WriteKind.MODIFY: TargetOperation.MODIFY,
        WriteKind.DELETE: TargetOperation.DELETE,
    }
    written = {write.path for write in planned}
    if written != set(declared):
        raise PatchError(
            "the patch and the chunk's targets disagree about which paths are written: "
            f"only in the patch {sorted(written - set(declared))}, only in targets {sorted(set(declared) - written)}"
        )
    for write in planned:
        if declared[write.path] is not expected[write.kind]:
            raise PatchError(
                f"{write.path!r}: the patch performs a {write.kind} but the chunk declares it a {declared[write.path]}"
            )


def _to_lines(text: str) -> tuple[list[str], bool]:
    """`text` as lines with their terminators stripped, plus whether it ended with one.

    Split on `"\\n"` alone — see this module's docstring for why `str.splitlines()` is wrong here.
    """
    if text == "":
        return [], True
    parts = text.split("\n")
    if parts[-1] == "":
        return parts[:-1], True
    return parts, False


def _from_lines(lines: list[str], ends_with_newline: bool) -> str:
    if not lines:
        return ""
    return "\n".join(lines) + ("\n" if ends_with_newline else "")
