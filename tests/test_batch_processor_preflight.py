"""Tables for `batch_processor/preflight.py` — ADR-0048's stale-reject and ADR-0015's write-once
raw layer.

Both are controls, so both are tested by injecting the violation and watching the verdict fire,
never by observing that a well-formed chunk is accepted. `docs/VERIFICATIONS.md` §4 names the raw
one specifically as **a code check, not an MCP refusal**, so it is tested here against this code
rather than against any response — a code regression and a misconfiguration are debugged
differently, and a test that could not tell them apart would prove neither.
"""

from __future__ import annotations

import pytest

from obsidian_tools.batch_processor.preflight import (
    RejectionReason,
    TargetState,
    assess_chunk,
    observed_state,
)
from obsidian_tools.batch_producer.chunk import Chunk
from obsidian_tools.batch_producer.staleness import ChunkTarget, TargetOperation, content_sha256

_HASH_A = content_sha256(b"content A\n")
_HASH_B = content_sha256(b"content B\n")


def a_chunk(*targets: ChunkTarget) -> Chunk:
    return Chunk(
        batch_id="b1",
        chunk_index=0,
        chunk_count=1,
        produced_at="2026-09-05T12:00:00+00:00",
        patch="diff --git a/x b/x\n",
        targets=targets,
    )


def present(sha: str) -> TargetState:
    return TargetState(exists=True, content_sha256=sha)


ABSENT = TargetState(exists=False, content_sha256=None)


# --- the staleness measure ------------------------------------------------------------------------


def test_a_modify_against_unchanged_content_is_accepted() -> None:
    """The positive control the rejections below are measured against. Red if the comparison ever
    stops matching identical content, at which point nothing could ever be applied."""
    chunk = a_chunk(ChunkTarget("10-areas/x.md", TargetOperation.MODIFY, _HASH_A))

    assert assess_chunk(chunk, {"10-areas/x.md": present(_HASH_A)}).accepted


def test_a_modify_whose_target_moved_rejects_as_stale() -> None:
    """ADR-0022's stale-reject, injected. Red if a moved file is applied anyway — which is the
    silent lost update the whole measure exists to prevent, and the point at which a three-way
    merge becomes the only way out."""
    chunk = a_chunk(ChunkTarget("10-areas/x.md", TargetOperation.MODIFY, _HASH_A))

    verdict = assess_chunk(chunk, {"10-areas/x.md": present(_HASH_B)})

    assert verdict.reasons == (RejectionReason.STALE,)
    assert _HASH_A in verdict.summary()
    assert _HASH_B in verdict.summary()


def test_one_moved_target_rejects_the_whole_chunk() -> None:
    """The chunk is the transaction unit (ADR-0048's stated cost, not an accident). Red if the
    verdict ever became per-target: a chunk could then half-apply and be redelivered onto content
    its own earlier writes moved."""
    chunk = a_chunk(
        ChunkTarget("10-areas/x.md", TargetOperation.MODIFY, _HASH_A),
        ChunkTarget("10-areas/y.md", TargetOperation.MODIFY, _HASH_A),
    )

    verdict = assess_chunk(chunk, {"10-areas/x.md": present(_HASH_A), "10-areas/y.md": present(_HASH_B)})

    assert not verdict.accepted


def test_a_delete_whose_target_moved_rejects_as_stale() -> None:
    """A delete carries a hash for the same reason a modify does. Red if deletes skipped the check
    — a note edited since the patch was generated would be destroyed on the strength of a stale
    view of it, which "fail loud, destroy nothing" forbids."""
    chunk = a_chunk(ChunkTarget("10-areas/x.md", TargetOperation.DELETE, _HASH_A))

    assert assess_chunk(chunk, {"10-areas/x.md": present(_HASH_B)}).reasons == (RejectionReason.STALE,)


def test_a_modify_of_an_absent_target_rejects_as_missing_rather_than_stale() -> None:
    """Red if absence collapsed into staleness: the two are fixed differently — a stale patch is
    regenerated, an absent target means the batch was built against a different vault."""
    chunk = a_chunk(ChunkTarget("10-areas/x.md", TargetOperation.MODIFY, _HASH_A))

    assert assess_chunk(chunk, {"10-areas/x.md": ABSENT}).reasons == (RejectionReason.MISSING,)


def test_a_create_whose_target_already_exists_is_refused() -> None:
    """A create's whole pre-flight is absence (ADR-0048: a create has no prior content to hash).
    Red if an existing target were overwritten — Gate 3's anti-clobber create, in this processor's
    own code."""
    chunk = a_chunk(ChunkTarget("10-areas/new.md", TargetOperation.CREATE, None))

    assert assess_chunk(chunk, {"10-areas/new.md": present(_HASH_A)}).reasons == (RejectionReason.ALREADY_EXISTS,)


def test_a_target_the_caller_never_read_is_rejected_rather_than_skipped() -> None:
    """The failure this whole module exists to make impossible is a check that silently does not
    run. Red if an unobserved target were treated as fine — the chunk would apply a write whose
    staleness nobody ever measured."""
    chunk = a_chunk(ChunkTarget("10-areas/x.md", TargetOperation.MODIFY, _HASH_A))

    assert assess_chunk(chunk, {}).reasons == (RejectionReason.NOT_OBSERVED,)


# --- the raw layer's write-once rule, in this component's own code ---------------------------------


def test_a_create_into_an_empty_raw_path_is_accepted() -> None:
    """The bootstrap import's entire shape. Red if creates into the raw layer were ever refused,
    at which point the one thing this component was built for cannot happen."""
    chunk = a_chunk(ChunkTarget("05-raw/imported.md", TargetOperation.CREATE, None))

    assert assess_chunk(chunk, {"05-raw/imported.md": ABSENT}).accepted


def test_a_create_over_an_existing_raw_note_is_refused_by_its_own_reason() -> None:
    """ADR-0015's control, injected. Red if it were reported as a plain `ALREADY_EXISTS`: the
    roadmap makes raw refusals a countable acceptance criterion on this unit, and a shared reason
    code makes them uncountable."""
    chunk = a_chunk(ChunkTarget("05-raw/imported.md", TargetOperation.CREATE, None))

    assert assess_chunk(chunk, {"05-raw/imported.md": present(_HASH_A)}).reasons == (RejectionReason.RAW_LAYER_EXISTS,)


@pytest.mark.parametrize("operation", [TargetOperation.MODIFY, TargetOperation.DELETE])
def test_a_modify_or_delete_of_a_raw_note_is_refused_outright(operation: TargetOperation) -> None:
    """ "Create yes, modify no" is an *operation* rule, which no path scope can express (ADR-0005),
    so it is refused here. Red if either operation reached the vault — the write-once layer would
    stop being write-once, and the validation exemption that rests on it would poison the
    baseline."""
    chunk = a_chunk(ChunkTarget("05-raw/imported.md", operation, _HASH_A))

    assert assess_chunk(chunk, {"05-raw/imported.md": present(_HASH_A)}).reasons == (
        RejectionReason.RAW_LAYER_NOT_CREATE,
    )


def test_a_raw_refusal_outranks_the_staleness_comparison() -> None:
    """ "An existing target is a refusal regardless of hash" (ADR-0048's consequences). Red if the
    hash check ran first: a raw modify whose hash happened to match would be applied, and one that
    did not would be reported as a stale patch — sending an operator to regenerate a batch that was
    never permitted in the first place."""
    chunk = a_chunk(ChunkTarget("05-raw/imported.md", TargetOperation.MODIFY, _HASH_A))

    assert assess_chunk(chunk, {"05-raw/imported.md": present(_HASH_B)}).reasons == (
        RejectionReason.RAW_LAYER_NOT_CREATE,
    )


@pytest.mark.parametrize("path", ["05-rawer/x.md", "05-raw.md", "10-areas/05-raw/x.md", "0", "05-ra"])
def test_a_path_that_merely_resembles_the_raw_layer_is_not_in_it(path: str) -> None:
    """The trailing slash is the whole boundary. Red if any of these were governed by the raw rule:
    an ordinary curated note would become undeletable and unmodifiable, with a refusal citing a
    decision record that does not apply to it."""
    chunk = a_chunk(ChunkTarget(path, TargetOperation.MODIFY, _HASH_A))

    assert assess_chunk(chunk, {path: present(_HASH_A)}).accepted


def test_a_glob_metacharacter_in_a_path_is_matched_literally() -> None:
    """ADR-0043's lesson, in this component's shape: a vault path may legally contain `*` or `[`.
    Red if the prefix test ever became a glob or a `Path.match` — `05-raw/*` would then match
    nothing or everything depending on the machinery, and neither is the rule."""
    chunk = a_chunk(ChunkTarget("05-raw/[weird]*.md", TargetOperation.MODIFY, _HASH_A))

    assert assess_chunk(chunk, {"05-raw/[weird]*.md": present(_HASH_A)}).reasons == (
        RejectionReason.RAW_LAYER_NOT_CREATE,
    )


def test_the_raw_prefix_is_configurable_and_the_rule_follows_it() -> None:
    """Red if the prefix were hard-coded: the vault's layer naming and this component's enforcement
    of it could then only be changed by a release, in two places, in some order."""
    chunk = a_chunk(ChunkTarget("archive/x.md", TargetOperation.MODIFY, _HASH_A))

    verdict = assess_chunk(chunk, {"archive/x.md": present(_HASH_A)}, raw_layer_prefix="archive/")

    assert verdict.reasons == (RejectionReason.RAW_LAYER_NOT_CREATE,)


# --- how the observed state is built ---------------------------------------------------------------


def test_the_hash_is_taken_over_the_utf8_bytes_the_producer_hashed() -> None:
    """The two sides must hash the same bytes or the comparison means nothing. Red if either side
    normalised: this is the assertion tying `observed_state` to the producer's own
    `content_sha256`, and its failure mode is every modify in the system rejecting as stale."""
    assert observed_state("content A\n") == TargetState(exists=True, content_sha256=_HASH_A)


def test_a_crlf_note_hashes_over_its_carriage_returns() -> None:
    """Red if line endings were ever normalised on the way in — a CRLF note would hash to its LF
    twin, and a patch generated against the real bytes would be applied to content that is not
    those bytes."""
    assert observed_state("a\r\nb\r\n").content_sha256 == content_sha256(b"a\r\nb\r\n")


def test_an_absent_note_carries_no_hash() -> None:
    """Red if absence produced the hash of the empty string: an empty note and a missing note would
    become indistinguishable, and a create over an empty note would be permitted."""
    assert observed_state(None) == TargetState(exists=False, content_sha256=None)


def test_every_rejection_is_reported_rather_than_only_the_first() -> None:
    """Red if the verdict short-circuited: an operator would fix one moved file per redelivery and
    learn about the next only after the next rejection."""
    chunk = a_chunk(
        ChunkTarget("10-areas/x.md", TargetOperation.MODIFY, _HASH_A),
        ChunkTarget("10-areas/y.md", TargetOperation.MODIFY, _HASH_A),
        ChunkTarget("05-raw/z.md", TargetOperation.MODIFY, _HASH_A),
    )

    verdict = assess_chunk(
        chunk,
        {
            "10-areas/x.md": present(_HASH_B),
            "10-areas/y.md": present(_HASH_B),
            "05-raw/z.md": present(_HASH_A),
        },
    )

    assert len(verdict.rejections) == 3
    assert verdict.reasons == (RejectionReason.STALE, RejectionReason.RAW_LAYER_NOT_CREATE)
