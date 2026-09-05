"""Tests for `obsidian_tools/batch_producer/staleness.py` — ADR-0048's per-target hash payload, as
pure functions over literals.

`batch-processor` is built against these rules by a different agent, so what is asserted here is the
*contract*, not this producer's convenience: every case below is something the consumer is entitled
to rely on. The red condition for each is named in its own comment or docstring, because a test
whose red condition cannot be stated is not evidence.
"""

from __future__ import annotations

import hashlib

import pytest

from obsidian_tools.batch_producer.staleness import (
    ChunkTarget,
    ChunkTargetError,
    TargetOperation,
    TargetSpec,
    UnsupportedPatchStatusError,
    content_sha256,
    targets_for_status,
    validate_target,
)

_A_HASH = "a" * 64
_B_HASH = "b" * 64


# --- content_sha256: the definition both components must share ---------------------------------


def test_content_sha256_is_plain_sha256_over_the_bytes_as_given() -> None:
    """Red if the hash ever gains a prefix, a normalisation step, or a different algorithm — any of
    which silently stops matching what a consumer computes over the bytes it read back."""
    content = b"# a note\n\nwith a line\n"

    assert content_sha256(content) == hashlib.sha256(content).hexdigest()


def test_content_sha256_distinguishes_crlf_from_lf() -> None:
    """The reason `GitRunner.run_binary` exists. Red the moment the hash is computed over text that
    has been through universal-newline translation, because both inputs would then collide."""
    assert content_sha256(b"a\r\nb\r\n") != content_sha256(b"a\nb\n")


def test_content_sha256_distinguishes_a_trailing_newline() -> None:
    """Red if any trimming or normalisation is introduced. Obsidian's own writes differ by exactly
    this often enough that a hash that ignored it would pass a stale target as fresh."""
    assert content_sha256(b"text") != content_sha256(b"text\n")


def test_content_sha256_is_lowercase_hex_of_the_expected_width() -> None:
    """Red if the digest is ever emitted uppercase or in another encoding: the consumer compares
    strings, so a case change is a total, silent mismatch rather than a partial one."""
    digest = content_sha256(b"anything")

    assert len(digest) == 64
    assert digest == digest.lower()


# --- targets_for_status: git's name-status letters to a pre-flight -------------------------------


@pytest.mark.parametrize(
    ("status", "path", "old_path", "expected"),
    [
        ("A", "05-raw/new.md", None, (TargetSpec("05-raw/new.md", TargetOperation.CREATE),)),
        ("M", "10-areas/x.md", None, (TargetSpec("10-areas/x.md", TargetOperation.MODIFY),)),
        ("T", "10-areas/x.md", None, (TargetSpec("10-areas/x.md", TargetOperation.MODIFY),)),
        ("D", "10-areas/x.md", None, (TargetSpec("10-areas/x.md", TargetOperation.DELETE),)),
        (
            "R100",
            "10-areas/new.md",
            "10-areas/old.md",
            (
                TargetSpec("10-areas/old.md", TargetOperation.DELETE),
                TargetSpec("10-areas/new.md", TargetOperation.CREATE),
            ),
        ),
        (
            "R087",
            "10-areas/new.md",
            "10-areas/old.md",
            (
                TargetSpec("10-areas/old.md", TargetOperation.DELETE),
                TargetSpec("10-areas/new.md", TargetOperation.CREATE),
            ),
        ),
    ],
)
def test_status_maps_to_its_pre_flight(
    status: str, path: str, old_path: str | None, expected: tuple[TargetSpec, ...]
) -> None:
    """Red if any status's pre-flight changes shape — in particular if a rename ever stops
    producing two targets, which would leave one of its two paths unchecked entirely."""
    assert targets_for_status(status, path, old_path) == expected


def test_a_renames_source_is_checked_before_its_destination() -> None:
    """Order is asserted deliberately: the source carries the hash and the destination carries the
    existence check, and a consumer reading these in order sees the hash check first. Red if the
    pair is ever emitted the other way round."""
    specs = targets_for_status("R100", "new.md", "old.md")

    assert specs[0].operation is TargetOperation.DELETE
    assert specs[1].operation is TargetOperation.CREATE


def test_a_type_change_is_a_modify_not_a_create() -> None:
    """Red if `T` were ever mapped to `create`: the path already exists, so the create branch's
    existence check would refuse every legitimate type change, and no hash would be carried."""
    assert targets_for_status("T", "x.md", None)[0].operation is TargetOperation.MODIFY


@pytest.mark.parametrize("status", ["C087", "U", "X", "B", ""])
def test_an_unhandled_status_is_refused_by_name(status: str) -> None:
    """Red if an unknown status ever produces targets instead of raising. Guessing would yield a
    chunk whose staleness check covers fewer paths than its patch writes — a check that silently
    does not run, which is worse than one that fails."""
    with pytest.raises(UnsupportedPatchStatusError):
        targets_for_status(status, "x.md", None)


def test_a_rename_without_a_source_path_is_refused() -> None:
    """Red if a malformed rename entry produced a one-sided target set rather than raising."""
    with pytest.raises(UnsupportedPatchStatusError):
        targets_for_status("R100", "new.md", None)


# --- the operation/hash pairing, enforced at construction ----------------------------------------


def test_a_create_carrying_a_hash_cannot_be_constructed() -> None:
    """The pairing ADR-0048 composes as create -> existence check. Red if a create is ever allowed
    to carry a hash, which would make `base_sha256 is None` stop meaning "this is a create"."""
    with pytest.raises(ChunkTargetError):
        ChunkTarget(path="05-raw/x.md", operation=TargetOperation.CREATE, base_sha256=_A_HASH)


@pytest.mark.parametrize("operation", [TargetOperation.MODIFY, TargetOperation.DELETE])
def test_a_modify_or_delete_without_a_hash_cannot_be_constructed(operation: TargetOperation) -> None:
    """Red if a hash-checked operation is ever allowed through without its hash — the exact shape of
    a staleness check that is present in the payload and absent in effect."""
    with pytest.raises(ChunkTargetError):
        ChunkTarget(path="10-areas/x.md", operation=operation, base_sha256=None)


@pytest.mark.parametrize("bad_hash", ["", "abc", "A" * 64, "z" * 64, "a" * 63, "a" * 65, " " + "a" * 63])
def test_a_malformed_hash_cannot_be_constructed(bad_hash: str) -> None:
    """Red if anything that is not 64 lowercase hex characters is accepted. Uppercase is included
    because a consumer comparing strings would treat it as a total mismatch, not a formatting nit."""
    with pytest.raises(ChunkTargetError):
        ChunkTarget(path="10-areas/x.md", operation=TargetOperation.MODIFY, base_sha256=bad_hash)


def test_a_well_formed_target_validates_clean() -> None:
    """The positive control: red if the rules above ever reject a legitimate target, which would
    make every other refusal above unfalsifiable."""
    assert validate_target(ChunkTarget("10-areas/x.md", TargetOperation.MODIFY, _B_HASH)) == ()


# --- target paths name a write, so they are constrained here -------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../outside.md",
        "10-areas/../../outside.md",
        "./x.md",
        "10-areas//x.md",
        "10-areas/",
        "",
        "x\0.md",
    ],
)
def test_a_path_that_could_escape_the_vault_is_refused(path: str) -> None:
    """Every target path is a write the processor will perform, so traversal and absolute paths are
    refused at the producer rather than trusted downstream. Red if any of these is ever accepted —
    each one is a write outside the tree the batch was authorised for."""
    with pytest.raises(ChunkTargetError):
        ChunkTarget(path=path, operation=TargetOperation.CREATE, base_sha256=None)


def test_a_path_carrying_an_undecodable_byte_is_refused_naming_the_path() -> None:
    """A path read out of git with `errors="surrogateescape"` (PEP 383) holds a lone surrogate,
    which has no UTF-8 encoding and cannot cross the JSON wire. Red if this is deferred to
    `json.dumps`, which raises against the whole message and names no path at all."""
    with pytest.raises(ChunkTargetError, match="UTF-8"):
        ChunkTarget(path="caf\udce9.md", operation=TargetOperation.CREATE, base_sha256=None)


@pytest.mark.parametrize("path", ["05-raw/note.md", "a.md", "10-areas/sub/deep note.md", "café.md", "star*.md"])
def test_an_ordinary_vault_path_is_accepted(path: str) -> None:
    """The positive control for the refusals above, including the two shapes most at risk of being
    over-rejected: non-ASCII names, and names holding a glob metacharacter (ADR-0043's case). Red if
    the path rules ever tighten onto legitimate vault content."""
    assert validate_target(ChunkTarget(path, TargetOperation.CREATE, None)) == ()
