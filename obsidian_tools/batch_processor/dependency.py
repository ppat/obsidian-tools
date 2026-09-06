"""Which later chunks a failed chunk takes with it. Pure — no broker, no MCP, no clock.

ADR-0022 orders the batch stream so that a producer can express dependency by ordering: the rename
in one chunk, and in a later chunk the edit that repoints another note's link at the new name. A
failed chunk does not end the run — a bulk import is thousands of chunks and one bad file must not
discard the rest — so the ordering guarantee has to be kept explicitly here, by refusing the
dependent chunk rather than by stopping the drain.

## Only a create can be depended on

A path is touched by at most one chunk of a batch (ADR-0048), so nothing a failed chunk would have
*modified* or *deleted* can be named by a later chunk of the same batch as a write target. What a
later chunk can name is a page the failed chunk would have brought into existence — including the
new name of a rename, which the chunk format models as a delete of the old path plus a create of
the new one (`batch_producer/staleness.py`). The blocked set is therefore exactly the failed
chunk's `create` targets, and the question asked of every later chunk is whether its patch links to
one of them.

## What "refers to" means here

The vault's schema file is the authority on link syntax, and it is narrow: a link target is the
filename stem, which is `slug(title)` (ADR-0013), optionally carrying `#heading`, `#^block` or
`|display text`. `refs:` and `related:` hold wikilinks too, so one wikilink match covers frontmatter
and prose alike. Markdown link targets are matched as well, because `05-raw/` holds imported
markdown that the vault's own "use wikilinks" setting never governed.

## Every ambiguity is resolved toward matching, and the asymmetry is the reason

A false match parks a chunk that would have applied cleanly, and the producer regenerates it — the
same recovery every other rejection in this component already has. A missed match applies the chunk
and leaves a note linking to a page that was never created, which nothing downstream distinguishes
from an ordinary dead link. So:

- the whole patch is scanned, added, removed and context lines alike;
- matching is case-insensitive, because Obsidian resolves a mis-cased target to the same page;
- a blocked path matches both on its stem and on the whole path, since "shortest path when
  possible" spells an unambiguous target as the bare stem and an ambiguous one as a path — which
  also means two paths sharing a stem (`40-journal/` and `_ops/lint/` both hold `YYYY-MM-DD.md`)
  match each other.

The one place ambiguity is *not* resolved toward matching: a stem counts only inside link syntax,
never as free text. ADR-0013's slug deletes rather than substitutes stripped characters, so `C++`
slugs to `c`, and a one-character stem matched as free text would park every remaining chunk of the
batch.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from urllib.parse import unquote

from obsidian_tools.batch_producer.chunk import Chunk
from obsidian_tools.batch_producer.staleness import TargetOperation

_WIKILINK = re.compile(r"\[\[([^\[\]]+)\]\]")
"""`[[target]]`, and the inner half of an embed's `![[target]]`. Nested brackets are excluded from
the target rather than matched non-greedily: a target cannot contain `[` or `]` at all, because the
slug rule deletes both for colliding with this very syntax."""

_MARKDOWN_LINK_TARGET = re.compile(r"\]\(\s*<?([^)>\s]+)")
"""The destination of `[text](target)`, stopping before an optional link title."""

_TARGET_TAIL = re.compile(r"[#|]")
"""What separates a link's target from its heading, block or display text."""


def created_paths(chunk: Chunk) -> frozenset[str]:
    """The paths `chunk` would have brought into existence — all that a later chunk of the same
    batch can depend on."""
    return frozenset(target.path for target in chunk.targets if target.operation is TargetOperation.CREATE)


def referenced_blocked_paths(chunk: Chunk, blocked: Collection[str]) -> tuple[str, ...]:
    """Which of `blocked` this chunk's patch links to, sorted. Empty means apply the chunk normally.

    Returns the matched paths rather than a bare verdict so that the chunk parked on this decision
    says in its dead-letter record which undone work it was waiting on.
    """
    # A short-circuit with no verdict of its own — the comprehension below already returns nothing
    # for an empty `blocked`. It is here because a healthy run has nothing blocked for every chunk
    # of the batch, and scanning each patch to conclude that is work with no possible answer.
    if not blocked:
        return ()
    linked = _link_targets(chunk.patch)
    return tuple(sorted(path for path in blocked if _spellings(path) & linked))


def _spellings(path: str) -> frozenset[str]:
    """The two ways a link can name `path`: the whole path, and the filename stem alone."""
    without_extension = path.casefold().removesuffix(".md")
    return frozenset({without_extension, without_extension.rpartition("/")[2]})


def _link_targets(patch: str) -> frozenset[str]:
    """Every link destination in `patch`, keyed for comparison.

    An external URL needs no exclusion here, and adding one would be dead code: the whole
    destination becomes the key, scheme and host included, and no key carrying those can equal a
    vault path.
    """
    wikilinks = (match.group(1) for match in _WIKILINK.finditer(patch))
    markdown = (unquote(match.group(1)) for match in _MARKDOWN_LINK_TARGET.finditer(patch))
    return frozenset(_target_key(raw) for raw in (*wikilinks, *markdown))


def _target_key(raw: str) -> str:
    """A link's target, reduced to the form `_spellings` renders a path in."""
    target = _TARGET_TAIL.split(raw, maxsplit=1)[0].strip().casefold()
    return target.removeprefix("./").removeprefix("/").removesuffix(".md")
