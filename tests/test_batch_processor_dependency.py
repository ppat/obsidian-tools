"""Tables for `batch_processor/dependency.py` — which later chunks a failed chunk takes with it.

The decision is a control, so the tables inject the thing it exists to catch: a patch that links to
a page an earlier chunk of the same batch failed to create. The reference forms come from the
vault's schema file (link target is the filename stem, `#heading`/`#^block`/`|display` suffixes,
wikilinks inside `refs:` and `related:`), not from what this module happens to accept — a table
written from the implementation would agree with any syntax it invented.

Every ambiguity in the module resolves toward matching, so the rows below assert the direction as
well as the outcome: over-matching parks a chunk the producer regenerates, under-matching writes a
link to a page nobody created.
"""

from __future__ import annotations

import pytest

from obsidian_tools.batch_processor.dependency import created_paths, referenced_blocked_paths
from obsidian_tools.batch_producer.chunk import Chunk
from obsidian_tools.batch_producer.staleness import ChunkTarget, TargetOperation, content_sha256

_HASH = content_sha256(b"whatever\n")
_BLOCKED = "10-areas/tech/kubernetes-ingress.md"


def a_chunk(patch: str, *targets: ChunkTarget) -> Chunk:
    return Chunk(
        batch_id="b1",
        chunk_index=1,
        chunk_count=2,
        produced_at="2026-09-05T12:00:00+00:00",
        patch=patch,
        targets=targets or (ChunkTarget("10-areas/tech/other.md", TargetOperation.MODIFY, _HASH),),
    )


def relinking(body: str) -> Chunk:
    """A chunk whose patch adds one line of `body` to an unrelated note."""
    return a_chunk(f"diff --git a/10-areas/tech/other.md b/10-areas/tech/other.md\n@@ -1,0 +1,1 @@\n+{body}\n")


# --- what a failed chunk blocks -------------------------------------------------------------------


def test_only_the_paths_a_chunk_would_have_created_are_blocked() -> None:
    """Nothing a chunk modified or deleted can be depended on, because a batch touches each path at
    most once (ADR-0048) — so a later chunk can never name one as a target. Red if modifies or
    deletes were blocked too: the old name of every rename would park the relink that removes the
    last reference to it, which is the one chunk that repairs the batch."""
    chunk = a_chunk(
        "diff --git a/x b/x\n",
        ChunkTarget("10-areas/tech/old-name.md", TargetOperation.DELETE, _HASH),
        ChunkTarget("10-areas/tech/new-name.md", TargetOperation.CREATE, None),
        ChunkTarget("10-areas/tech/edited.md", TargetOperation.MODIFY, _HASH),
    )

    assert created_paths(chunk) == frozenset({"10-areas/tech/new-name.md"})


def test_a_renames_new_name_is_blocked_because_the_format_models_it_as_a_create() -> None:
    """The dependency ADR-0022 names by example. Red if the rename's destination were treated as
    anything but a create — the relink chunk would sail through and leave the link dangling, which
    is the whole failure."""
    chunk = a_chunk(
        "diff --git a/x b/y\n",
        ChunkTarget("10-areas/tech/old-name.md", TargetOperation.DELETE, _HASH),
        ChunkTarget("10-areas/tech/new-name.md", TargetOperation.CREATE, None),
    )

    assert "10-areas/tech/new-name.md" in created_paths(chunk)


# --- the reference forms the vault actually uses ---------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("see [[kubernetes-ingress]] for the detail", id="bare stem"),
        pytest.param("see [[kubernetes-ingress|Kubernetes Ingress]]", id="display text"),
        pytest.param("see [[kubernetes-ingress#Routing]]", id="heading"),
        pytest.param("see [[kubernetes-ingress#^a1b2c3]]", id="block reference"),
        pytest.param("![[kubernetes-ingress]]", id="embed"),
        pytest.param("see [[10-areas/tech/kubernetes-ingress]]", id="path, as an ambiguous stem is written"),
        pytest.param("see [[kubernetes-ingress.md]]", id="stem carrying the extension"),
        pytest.param('refs: ["[[kubernetes-ingress]]"]', id="frontmatter refs"),
        pytest.param('related: ["[[kubernetes-ingress]]"]', id="frontmatter related"),
        pytest.param("see [Kubernetes Ingress](10-areas/tech/kubernetes-ingress.md)", id="markdown link"),
        pytest.param("see [KI](kubernetes-ingress.md#routing)", id="markdown link with a fragment"),
        pytest.param("see [KI](./kubernetes-ingress.md)", id="markdown link written relative"),
        pytest.param("see [KI](/10-areas/tech/kubernetes-ingress.md)", id="markdown link rooted at the vault"),
        pytest.param("see [[ kubernetes-ingress ]]", id="padded target, which Obsidian trims"),
        pytest.param("see [[Kubernetes-Ingress]]", id="mis-cased, which Obsidian resolves anyway"),
    ],
)
def test_a_link_to_a_blocked_path_parks_the_chunk(body: str) -> None:
    """Each row is a form the vault's schema file sanctions or Obsidian resolves. Red for any row
    means that spelling of a link slips through: the chunk applies, and the note it writes points at
    a page the failed chunk never created."""
    assert referenced_blocked_paths(relinking(body), [_BLOCKED]) == (_BLOCKED,)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("see [[kubernetes-service]] instead", id="a different note"),
        pytest.param("kubernetes-ingress is the subject of this sentence", id="the stem as free text"),
        pytest.param("see [the docs](https://example.com/kubernetes-ingress.md)", id="an external url"),
        pytest.param("see [[#kubernetes-ingress]]", id="a heading in this same note"),
        pytest.param("a `kubernetes-ingress` code span", id="the stem in a code span"),
    ],
)
def test_text_that_is_not_a_link_to_the_blocked_path_leaves_the_chunk_alone(body: str) -> None:
    """The other direction, and it has to hold or the mechanism is a batch-wide stop with extra
    steps. The free-text row is the sharp one: ADR-0013's slug deletes stripped characters, so
    `C++` becomes `c`, and matching bare words would park every chunk mentioning the letter."""
    assert referenced_blocked_paths(relinking(body), [_BLOCKED]) == ()


# --- scope: the blocked set, and everything outside it ---------------------------------------------


def test_nothing_blocked_means_nothing_to_decide() -> None:
    """The positive control for an ordinary run. Red if a chunk could be parked with no earlier
    failure at all, which would stop a healthy bulk import dead."""
    assert referenced_blocked_paths(relinking("see [[kubernetes-ingress]]"), []) == ()


def test_every_blocked_path_the_chunk_links_to_is_named() -> None:
    """The detail a parked chunk carries has to name the undone work, not merely assert that some
    exists. Red if only the first match were reported: an operator would regenerate one rename and
    find the chunk parked again on the next."""
    second = "20-projects/migration.md"
    chunk = relinking("see [[kubernetes-ingress]] and [[migration]]")

    assert referenced_blocked_paths(chunk, [_BLOCKED, second]) == (_BLOCKED, second)


def test_a_link_the_chunk_is_removing_still_parks_it() -> None:
    """Deliberate over-matching: the whole patch is scanned, removed lines included. Red if the
    scan were narrowed to added lines — which is defensible in isolation, and is exactly the kind
    of narrowing that turns a missed match into a dangling link when a producer's diff shape is not
    what the narrowing assumed."""
    chunk = a_chunk(
        "diff --git a/10-areas/tech/other.md b/10-areas/tech/other.md\n"
        "@@ -1,1 +1,1 @@\n"
        "-see [[kubernetes-ingress]]\n"
        "+see nothing\n"
    )

    assert referenced_blocked_paths(chunk, [_BLOCKED]) == (_BLOCKED,)


def test_a_percent_encoded_markdown_link_is_matched() -> None:
    """`05-raw/` keeps whatever an import named its files, spaces included, and a markdown link to
    such a path has to encode them. Red if the destination were compared raw: a dependency on an
    imported page would be missed wherever its name is not already a slug."""
    chunk = relinking("see [the import](05-raw/Imported%20Note.md)")

    assert referenced_blocked_paths(chunk, ["05-raw/Imported Note.md"]) == ("05-raw/Imported Note.md",)


def test_an_upper_case_blocked_path_is_matched_by_the_lower_case_link_to_it() -> None:
    """`05-raw/` is write-once and unvalidated, so the slug rule that makes every curated stem
    lowercase (ADR-0013) never applied to what an import named. Red if only the link side were
    folded: every dependency on an imported page would be missed, which is the direction that
    leaves a dangling link rather than the one that parks a chunk."""
    chunk = relinking("see [[imported-note]]")

    assert referenced_blocked_paths(chunk, ["05-raw/Imported-Note.md"]) == ("05-raw/Imported-Note.md",)


def test_two_paths_sharing_a_stem_match_each_other() -> None:
    """`40-journal/` and `_ops/lint/` both hold `YYYY-MM-DD.md`, and a bare stem cannot tell them
    apart. The over-match is chosen: it parks a chunk the producer regenerates, where resolving the
    stem against a directory this component cannot see would guess. Red if this ever returned
    nothing — that would mean the stem stopped being matched at all, and stems are how almost every
    real link is written."""
    chunk = relinking("see [[2026-07-30]]")

    assert referenced_blocked_paths(chunk, ["_ops/lint/2026-07-30.md"]) == ("_ops/lint/2026-07-30.md",)
