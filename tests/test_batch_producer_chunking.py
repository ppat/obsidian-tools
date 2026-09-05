"""Tests for `obsidian_tools/batch_producer/chunking.py` — splitting one batch's patch into chunks.

Table tests for the packing rules, plus two hypothesis properties. The properties are here because a
genuine oracle exists for each and neither restates the implementation: reassembling the chunks must
reproduce the whole patch (an identity the packer never computes), and no unit may be dropped,
duplicated or reordered (a permutation check the packer never performs).
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from obsidian_tools.batch_producer.chunking import (
    ChunkTooLargeError,
    PatchUnit,
    build_chunks,
    pack_units,
    patch_size_bytes,
)
from obsidian_tools.batch_producer.staleness import ChunkTarget, TargetOperation

_BATCH = "0123456789abcdef0123456789abcdef"


def unit(name: str, size: int) -> PatchUnit:
    """A unit whose patch is exactly `size` ASCII bytes, so the budget arithmetic in each test is
    readable rather than incidental."""
    return PatchUnit(
        patch=(name * size)[:size],
        targets=(ChunkTarget(f"05-raw/{name}.md", TargetOperation.CREATE, None),),
    )


def paths_of(groups: tuple[tuple[PatchUnit, ...], ...]) -> list[list[str]]:
    return [[target.path for u in group for target in u.targets] for group in groups]


# --- packing ------------------------------------------------------------------------------------


def test_units_that_all_fit_become_one_chunk() -> None:
    """Red if the packer splits when it need not — every extra chunk is an extra message and an
    extra independent rejection unit for no gain."""
    groups = pack_units([unit("a", 10), unit("b", 10)], max_patch_bytes=100)

    assert paths_of(groups) == [["05-raw/a.md", "05-raw/b.md"]]


def test_a_unit_that_would_overflow_starts_a_new_chunk() -> None:
    """Red if the budget is not enforced: an oversized message is refused by the broker rather than
    by anything that can name the file responsible."""
    groups = pack_units([unit("a", 60), unit("b", 60), unit("c", 10)], max_patch_bytes=100)

    assert paths_of(groups) == [["05-raw/a.md"], ["05-raw/b.md", "05-raw/c.md"]]


def test_packing_never_reorders_units() -> None:
    """ADR-0022's dependency-by-ordering is the whole reason the stream is FIFO, so a packer that
    reordered to fill chunks better would be wrong, not clever. Red if order is ever not preserved."""
    units = [unit("a", 60), unit("b", 10), unit("c", 60)]

    groups = pack_units(units, max_patch_bytes=100)

    assert [u.patch for group in groups for u in group] == [u.patch for u in units]


def test_a_unit_exactly_at_the_budget_is_placed_alone_rather_than_refused() -> None:
    """The boundary. Red on an off-by-one that turns a legal chunk into a refusal an operator cannot
    act on, because nothing about the file is actually wrong."""
    groups = pack_units([unit("a", 100)], max_patch_bytes=100)

    assert paths_of(groups) == [["05-raw/a.md"]]


def test_a_single_unit_over_the_budget_is_refused_naming_its_paths() -> None:
    """A file's diff is the indivisible atom, so an oversized one cannot be placed at all. Red if it
    is emitted anyway — the broker would refuse the message, and the failure would name a subject
    rather than the file that caused it."""
    with pytest.raises(ChunkTooLargeError, match=r"05-raw/big\.md"):
        pack_units([unit("a", 10), unit("big", 200)], max_patch_bytes=100)


def test_no_units_pack_into_no_groups() -> None:
    """Red if an empty input produces one empty chunk, which would then fail validation downstream
    for a reason that has nothing to do with the real situation (nothing was staged)."""
    assert pack_units([], max_patch_bytes=100) == ()


def test_the_budget_counts_encoded_bytes_not_characters() -> None:
    """`max_payload` is a byte limit and non-ASCII vault content differs from its character count by
    up to a factor of four. Red if the budget is measured in characters, which would let a chunk of
    accented notes exceed the broker's limit while appearing to fit."""
    assert patch_size_bytes("é" * 10) == 20


def test_a_multibyte_unit_is_budgeted_by_its_byte_length() -> None:
    """The same fact, exercised through the packer rather than the measurement. Red if two units
    that fit by character count but not by byte count are packed together."""
    accented = PatchUnit(patch="é" * 30, targets=(ChunkTarget("a.md", TargetOperation.CREATE, None),))
    other = PatchUnit(patch="é" * 30, targets=(ChunkTarget("b.md", TargetOperation.CREATE, None),))

    assert len(pack_units([accented, other], max_patch_bytes=100)) == 2


# --- build_chunks -------------------------------------------------------------------------------


def test_built_chunks_are_indexed_and_counted_consistently() -> None:
    """Red if index or count is ever wrong: the count is a consumer's only way to notice a batch cut
    short, and the index is its only way to audit that FIFO order held."""
    chunks = build_chunks(
        [unit("a", 60), unit("b", 60), unit("c", 60)],
        batch_id=_BATCH,
        produced_at="2026-09-05T12:00:00+00:00",
        max_patch_bytes=100,
    )

    assert [(c.chunk_index, c.chunk_count) for c in chunks] == [(0, 3), (1, 3), (2, 3)]


def test_a_chunk_carries_every_target_of_every_unit_it_holds() -> None:
    """Red if a target is ever dropped while its patch is carried — the pre-flight would then check
    fewer paths than the chunk writes, which is a staleness check that silently does not run."""
    rename = PatchUnit(
        patch="diff --git a/old.md b/new.md\n",
        targets=(
            ChunkTarget("old.md", TargetOperation.DELETE, "a" * 64),
            ChunkTarget("new.md", TargetOperation.CREATE, None),
        ),
    )

    chunks = build_chunks([rename], batch_id=_BATCH, produced_at="2026-09-05T12:00:00+00:00", max_patch_bytes=1000)

    assert [t.path for t in chunks[0].targets] == ["old.md", "new.md"]


def test_a_renames_two_halves_are_never_separated() -> None:
    """A rename is one unit, so no budget can split it. Red if the packer ever divides a unit —
    the relink would land in a different chunk from the rename it depends on."""
    rename = PatchUnit(
        patch="x" * 100,
        targets=(
            ChunkTarget("old.md", TargetOperation.DELETE, "a" * 64),
            ChunkTarget("new.md", TargetOperation.CREATE, None),
        ),
    )

    chunks = build_chunks([rename], batch_id=_BATCH, produced_at="2026-09-05T12:00:00+00:00", max_patch_bytes=100)

    assert len(chunks) == 1
    assert len(chunks[0].targets) == 2


def test_no_units_build_no_chunks() -> None:
    """Red if an empty staging area produces a batch at all."""
    assert build_chunks([], batch_id=_BATCH, produced_at="2026-09-05T12:00:00+00:00", max_patch_bytes=100) == ()


# --- properties ----------------------------------------------------------------------------------


@st.composite
def patch_units(draw: st.DrawFn) -> PatchUnit:
    """Units with distinct paths, since a batch's paths must be unique — generating colliding paths
    would test a precondition the producer never creates rather than the packer's behaviour.

    Surrogates are excluded for the same reason, and it is not cosmetic: `generation.py` refuses a
    diff that is not UTF-8-encodable before any unit reaches the packer, so a lone surrogate here is
    an input the producer cannot construct. Left in, the strategy eventually draws one and
    `patch_size_bytes` raises `UnicodeEncodeError` — a failure of the generator's own contract
    rather than of the packer.
    """
    body = draw(st.text(alphabet=st.characters(min_codepoint=1, exclude_categories=("Cs",)), min_size=1, max_size=40))
    name = draw(st.uuids()).hex
    return PatchUnit(patch=body, targets=(ChunkTarget(f"05-raw/{name}.md", TargetOperation.CREATE, None),))


@given(units=st.lists(patch_units(), max_size=25), budget=st.integers(min_value=200, max_value=2000))
def test_reassembling_the_chunks_reproduces_the_whole_patch(units: list[PatchUnit], budget: int) -> None:
    """The oracle: concatenating the chunks' patches in order must equal the whole patch, because
    splitting and reassembling is an identity. Goes red the moment any generated input makes the
    packer drop, duplicate, truncate or reorder patch text — none of which the packer itself
    computes, so this is not the implementation restated.

    The budget's lower bound exceeds the maximum generated unit size, so `ChunkTooLargeError` is out
    of scope here and has its own table test above.
    """
    chunks = build_chunks(units, batch_id=_BATCH, produced_at="2026-09-05T12:00:00+00:00", max_patch_bytes=budget)

    assert "".join(chunk.patch for chunk in chunks) == "".join(u.patch for u in units)


@given(units=st.lists(patch_units(), max_size=25), budget=st.integers(min_value=200, max_value=2000))
def test_every_target_appears_in_exactly_one_chunk(units: list[PatchUnit], budget: int) -> None:
    """ADR-0048's invariant, as a property rather than a fixed case: goes red if any generated input
    produces a batch where a path is carried by two chunks or by none. A duplicate would make the
    batch invalidate its own later chunk; a drop would leave a write with no pre-flight at all."""
    chunks = build_chunks(units, batch_id=_BATCH, produced_at="2026-09-05T12:00:00+00:00", max_patch_bytes=budget)

    emitted = [target.path for chunk in chunks for target in chunk.targets]
    expected = [target.path for u in units for target in u.targets]
    assert sorted(emitted) == sorted(expected)
    assert len(set(emitted)) == len(emitted)


@given(units=st.lists(patch_units(), min_size=1, max_size=25), budget=st.integers(min_value=200, max_value=2000))
def test_every_chunk_fits_the_budget(units: list[PatchUnit], budget: int) -> None:
    """Goes red if any generated input produces a chunk the broker would refuse. The packer decides
    placement one unit at a time; this checks the emitted result, which it never does."""
    chunks = build_chunks(units, batch_id=_BATCH, produced_at="2026-09-05T12:00:00+00:00", max_patch_bytes=budget)

    assert all(patch_size_bytes(chunk.patch) <= budget for chunk in chunks)
