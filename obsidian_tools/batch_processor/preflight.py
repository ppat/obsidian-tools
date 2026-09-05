"""Apply this chunk, or reject the whole of it. Pure — no MCP client, no sockets, no clock.

Two independent controls compose here, and ADR-0048 states the composition: **create resolves to an
existence check, modify and delete resolve to a hash check.** Both run over every target in the
chunk before the shell is allowed to write anything.

## Whole-chunk, and before any write — not per write

A chunk is the transaction and redelivery unit (DESIGN.md's Glossary, ADR-0022). Checking per write
would let a chunk half-apply and then be redelivered onto content its own earlier writes moved: the
second delivery would find the first delivery's output, disagree with the recorded hash for reasons
the producer never caused, and either reject something already correct or — worse, if the check ran
per write — apply the remainder on top of a partial state. Checking every target first turns that
whole family into one rejection, before the vault has changed at all.

The corollary is deliberate and recorded as a cost, not hidden: **one moved file rejects every edit
in the chunk**, and a chunk that crashed part-way through applying is rejected on redelivery rather
than replayed (ADR-0048's consequences). Recovery is producer regeneration, which is loud and
destroys nothing.

## The raw layer's rule is a code check here, and that placement is the decision

`05-raw/` is write-once and validation-exempt (ADR-0015). Nothing upstream can carry that rule:
Gate 2's path scope is path-granular only and cannot express "create yes, modify no"
(ADR-0005), and the admission validator sees notes entering *curated* space — the wrong
side of this boundary, which a modification to a raw note never crosses. So it is enforced in this
file, and its acceptance test targets this file rather than an MCP refusal, because a code
regression and a misconfiguration are debugged very differently.

An existing raw target is refused **regardless of hash**: it is not a staleness question, and
`RAW_LAYER_EXISTS` is kept distinct from the ordinary `ALREADY_EXISTS` for the same reason — the
roadmap makes raw-refusals a countable acceptance criterion on this unit, which a shared reason
code would make uncountable.

## What this module refuses to do

It never transforms a patch. The only outputs are "apply as given" and "reject the whole chunk";
any use of a recorded hash to reconcile rather than to gate is the merge engine this architecture
deleted, returning under a new name (ADR-0048).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from obsidian_tools.batch_producer.chunk import Chunk
from obsidian_tools.batch_producer.staleness import ChunkTarget, TargetOperation, content_sha256

# The write-once layer's prefix, as a plain string compared with `str.startswith` and deliberately
# never a glob or a `Path.match`: a vault path may legally contain `*`, `[` or `?`, and ADR-0043
# records what happens in this codebase when a real filename meets pattern-matching machinery. The
# trailing slash is what keeps a sibling directory named `05-rawer/` outside the rule.
DEFAULT_RAW_LAYER_PREFIX = "05-raw/"


class RejectionReason(StrEnum):
    """Why a chunk was not applied. Distinct values because each is a different thing to fix, and
    because the roadmap counts two of them (raw refusals, stale rejections) separately."""

    STALE = "stale"
    """The target's current content is not what the patch was generated against (ADR-0048)."""

    MISSING = "missing"
    """A modify or delete whose target is not there at all — nothing to compare and nothing to edit."""

    ALREADY_EXISTS = "already_exists"
    """A create whose target is already there. A create has no prior content to hash, so its whole
    pre-flight is absence; applying it anyway is the silent lost update Gate 3 exists to prevent."""

    RAW_LAYER_EXISTS = "raw_layer_exists"
    """A create into `05-raw/` whose target is already there — the write-once rule (ADR-0015)."""

    RAW_LAYER_NOT_CREATE = "raw_layer_not_create"
    """A modify or delete targeting `05-raw/` at all. Refused before any hash is looked at."""

    NOT_OBSERVED = "not_observed"
    """The caller did not read this target before asking. A bug in the shell, surfaced as a
    rejection rather than an exception so this function stays total over its inputs — a check that
    silently does not run is the failure this whole module exists to make impossible."""


@dataclass(frozen=True, slots=True)
class TargetState:
    """What the processor read back through the gated MCP path for one target path."""

    exists: bool
    content_sha256: str | None
    """`sha256` over the raw bytes read back, as `staleness.content_sha256` computes it. `None`
    exactly when `exists` is false."""


@dataclass(frozen=True, slots=True)
class Rejection:
    reason: RejectionReason
    path: str
    detail: str


@dataclass(frozen=True, slots=True)
class PreflightVerdict:
    """Every reason the chunk may not be applied. Empty means apply it as given."""

    rejections: tuple[Rejection, ...]

    @property
    def accepted(self) -> bool:
        return not self.rejections

    @property
    def reasons(self) -> tuple[RejectionReason, ...]:
        """The distinct reason codes, in first-seen order — what a log line and a counter carry."""
        seen: list[RejectionReason] = []
        for rejection in self.rejections:
            if rejection.reason not in seen:
                seen.append(rejection.reason)
        return tuple(seen)

    def summary(self) -> str:
        return "; ".join(f"{r.path!r}: {r.detail}" for r in self.rejections)


def is_raw_layer(path: str, raw_layer_prefix: str) -> bool:
    return path.startswith(raw_layer_prefix)


def observed_state(content: str | None) -> TargetState:
    """The state of a target, from what the gated MCP path returned for it.

    **The two sides must hash the same bytes for the comparison to mean anything.** The producer
    hashes the file's stored bytes; this hashes the UTF-8 encoding of the text the MCP surface
    returned. They agree exactly when the note round-trips through that surface unchanged, which is
    the assumption the whole staleness check rests on — and it is an assumption, not a proof: a
    surface that normalised line endings or re-encoded on read would make every modify look stale.
    The failure is loud (a rejection naming both hashes), never silent, which is what makes it
    tolerable to rest on.
    """
    if content is None:
        return TargetState(exists=False, content_sha256=None)
    return TargetState(exists=True, content_sha256=content_sha256(content.encode("utf-8")))


def assess_chunk(
    chunk: Chunk,
    observed: Mapping[str, TargetState],
    *,
    raw_layer_prefix: str = DEFAULT_RAW_LAYER_PREFIX,
) -> PreflightVerdict:
    """Whether every target of `chunk` still stands where its patch was generated against.

    `observed` is what the shell read back for each target path, keyed by path. Every reason is
    collected rather than the first, so one rejected chunk produces one log line naming every file
    that moved instead of one per redelivery.
    """
    return PreflightVerdict(
        tuple(
            rejection
            for target in chunk.targets
            for rejection in _assess_target(target, observed.get(target.path), raw_layer_prefix)
        )
    )


def _assess_target(target: ChunkTarget, state: TargetState | None, raw_layer_prefix: str) -> tuple[Rejection, ...]:
    if state is None:
        return (Rejection(RejectionReason.NOT_OBSERVED, target.path, "was never read before the chunk was assessed"),)

    if is_raw_layer(target.path, raw_layer_prefix):
        # Ordered before every other check on purpose: an existing raw target is a refusal
        # whatever its hash says, so reaching the staleness comparison at all would report a
        # write-once violation as a stale patch and send the operator to regenerate a batch that
        # was never permitted.
        if target.operation is not TargetOperation.CREATE:
            return (
                Rejection(
                    RejectionReason.RAW_LAYER_NOT_CREATE,
                    target.path,
                    f"the raw layer is write-once, so a {target.operation} of it is refused (ADR-0015)",
                ),
            )
        if state.exists:
            return (
                Rejection(
                    RejectionReason.RAW_LAYER_EXISTS,
                    target.path,
                    "already exists under the write-once raw layer, so this create is refused (ADR-0015)",
                ),
            )
        return ()

    if target.operation is TargetOperation.CREATE:
        if state.exists:
            return (
                Rejection(
                    RejectionReason.ALREADY_EXISTS,
                    target.path,
                    "already exists, but the chunk carries it as a create",
                ),
            )
        return ()

    if not state.exists:
        return (
            Rejection(
                RejectionReason.MISSING,
                target.path,
                f"is absent, but the chunk carries a {target.operation} of it",
            ),
        )
    if state.content_sha256 != target.base_sha256:
        return (
            Rejection(
                RejectionReason.STALE,
                target.path,
                f"holds {state.content_sha256}, but the patch was generated against {target.base_sha256}",
            ),
        )
    return ()
