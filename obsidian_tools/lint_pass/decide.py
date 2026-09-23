"""One pass's decisions, from one snapshot of the vault. Pure — no I/O, no clock.

`decide` takes what the shell read from the mount and returns everything the pass will do and say:
the findings, the fixes it may write, the fixes admission withheld, and the counts the summary line
carries. The shell only reads, writes and reports; every policy question is answered here, so the
whole of it is testable with literals.

**Every fix to a curated note goes through `admit` before it is planned.** A fix is a curated
crossing like any other (ADR-0007), and the post-image `admit` judges is the exact text the shell
will write — a whole note, not a frontmatter edit the door would re-serialise — so the bytes that
land are the bytes the validator saw (DESIGN.md's one-authority table; ADR-0053). A refused
post-image is not written: it is recorded as withheld, and the note is already reported at the top
rank for the same refusal, because normalisation can only remove a refused condition, never add one.

**Each fix carries the hash of the bytes it was computed from**, so the shell can refuse to write
over a note that changed between the mount read and the write (ADR-0048's measure, reused).
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from obsidian_tools.admission.validator import ReasonCode, admit, refusals_for
from obsidian_tools.lint_pass.checks import (
    RECORD_PREFIXES,
    Finding,
    Zone,
    aliases_of,
    banned_character_findings,
    frontmatter_findings,
    link_names,
    link_targets,
    quote,
    stale_finding,
    zone_of,
)
from obsidian_tools.lint_pass.line import Check, FindingClass
from obsidian_tools.lint_pass.normalise import Change, normalise
from obsidian_tools.vault_schema.frontmatter import Frontmatter, ParsedNote, Unparseable, emit_note, parse_note
from obsidian_tools.vault_schema.zones import FINANCE_PREFIX, INBOX_PREFIX, QUARANTINE_PREFIX

JUDGED = frozenset({Zone.CURATED, Zone.AGENT, Zone.ELSEWHERE})

# Findings about a note's place in the graph or its age, rather than its conformance to the schema.
# Excluded from the non-conforming count — ROADMAP D6's drift rate is schema drift.
_NOT_CONFORMANCE = frozenset({Check.DANGLING_LINK, Check.ORPHAN, Check.STALE})


@dataclass(frozen=True, slots=True)
class VaultFile:
    path: str
    """Vault-relative, `/`-separated."""
    content: bytes | None
    """The file's bytes for a markdown note; `None` for any other file, which is only ever a link
    target."""
    modified: date
    """The file's modification date on the mount, in UTC."""


@dataclass(frozen=True, slots=True)
class FixPlan:
    path: str
    pre_image_sha256: str
    """Over the mount's bytes, exactly as read — the same definition `batch-processor` hashes with."""
    post_image: str
    changes: tuple[Change, ...]


@dataclass(frozen=True, slots=True)
class Withheld:
    """A fix `admit` refused: never written."""

    path: str
    changes: tuple[Change, ...]
    codes: tuple[ReasonCode, ...]


@dataclass(frozen=True, slots=True)
class DigestItem:
    rank: int
    classes: tuple[FindingClass, ...]
    path: str


@dataclass(frozen=True, slots=True)
class Decision:
    findings: tuple[Finding, ...]
    fixes: tuple[FixPlan, ...]
    withheld: tuple[Withheld, ...]
    notes_by_zone: dict[Zone, int]
    inbox_depth: int
    quarantine_depth: int
    nonconforming: int
    """Judged notes with a schema finding or a fix to make: the pass's drift rate."""


def decide(files: Sequence[VaultFile], *, today: date) -> Decision:
    notes = {f.path: f for f in files if f.content is not None}
    texts = {path: _decoded(f.content) for path, f in notes.items() if f.content is not None}
    parsed = {path: parse_note(text) for path, text in texts.items() if text is not None}
    frontmatter = {path: p.frontmatter for path, p in parsed.items() if isinstance(p, ParsedNote)}
    zones = {f.path: zone_of(f.path) for f in files}

    findings: list[Finding] = []
    inbound = _link_graph(files, texts, frontmatter, zones, findings)

    fixes: list[FixPlan] = []
    withheld: list[Withheld] = []
    for path in sorted(notes):
        zone = zones[path]
        if zone not in JUDGED:
            continue
        text = texts.get(path)
        if zone is Zone.CURATED and not inbound.get(path):
            findings.append(Finding(Check.ORPHAN, path, "no other note links here"))
        if text is None:
            findings.append(_refused(zone, path, ReasonCode.FRONTMATTER_UNPARSEABLE, "the note is not valid UTF-8"))
            continue
        findings.extend(_refused_tier(path, text))
        findings.extend(banned_character_findings(path, text))
        note = parsed[path]
        if isinstance(note, Unparseable):
            continue
        findings.extend(frontmatter_findings(path, note.frontmatter))
        if zone is Zone.CURATED and (stale := stale_finding(path, note.frontmatter, today)) is not None:
            findings.append(stale)

        normalised, changes = normalise(note.frontmatter, modified=notes[path].modified)
        if not changes:
            continue
        post_image = emit_note(ParsedNote(normalised, note.body))
        verdict = admit(path, post_image)
        if verdict.admitted:
            pre = hashlib.sha256(notes[path].content or b"").hexdigest()
            fixes.append(FixPlan(path, pre, post_image, changes))
        else:
            withheld.append(Withheld(path, changes, verdict.codes))

    findings.sort(key=lambda f: (f.path, str(f.check)))
    nonconforming = {f.path for f in findings if f.check not in _NOT_CONFORMANCE}
    nonconforming |= {fix.path for fix in fixes} | {w.path for w in withheld}
    return Decision(
        findings=tuple(findings),
        fixes=tuple(fixes),
        withheld=tuple(withheld),
        notes_by_zone=dict(Counter(zones[path] for path in notes)),
        inbox_depth=sum(1 for path in notes if path.startswith(INBOX_PREFIX)),
        quarantine_depth=sum(1 for path in notes if path.startswith(QUARANTINE_PREFIX)),
        nonconforming=len(nonconforming),
    )


def _link_graph(
    files: Sequence[VaultFile],
    texts: dict[str, str | None],
    frontmatter: dict[str, Frontmatter],
    zones: dict[str, Zone],
    findings: list[Finding],
) -> dict[str, set[str]]:
    """Who links to whom, reporting each dangling link from a judged note on the way."""
    index: dict[str, set[str]] = {}
    for f in files:
        for name in link_names(f.path, aliases_of(frontmatter.get(f.path))):
            index.setdefault(name, set()).add(f.path)

    inbound: dict[str, set[str]] = {}
    for source, text in sorted(texts.items()):
        if text is None or source.startswith(RECORD_PREFIXES):
            continue
        dangling: list[str] = []
        for target in link_targets(text):
            matched = index.get(target)
            if matched is None:
                dangling.append(target)
                continue
            for path in matched - {source}:
                inbound.setdefault(path, set()).add(source)
        if zones[source] in JUDGED:
            for target in dict.fromkeys(dangling):
                findings.append(Finding(Check.DANGLING_LINK, source, f"`{quote(target)}` matches no note"))
    return inbound


def _refused_tier(path: str, text: str) -> list[Finding]:
    """The validator's own reading. In curated space it is the gate's verdict; anywhere else the same
    rules are read without the zone decision and reported, because the agent zone is detective only."""
    verdict = admit(path, text)
    if verdict.crossing:
        return [
            Finding(r.code, path, quote(r.detail) if r.field is None else f"`{r.field}` {quote(r.detail)}")
            for r in verdict.refusals
        ]
    refusals = refusals_for(text, finance=False)
    if not refusals:
        return []
    codes = ", ".join(dict.fromkeys(r.code for r in refusals))
    return [Finding(Check.AGENT_ZONE_NONCONFORMING, path, codes)]


def _refused(zone: Zone, path: str, code: ReasonCode, detail: str) -> Finding:
    if zone is Zone.CURATED:
        return Finding(code, path, detail)
    return Finding(Check.AGENT_ZONE_NONCONFORMING, path, f"{code}: {detail}")


def _decoded(content: bytes) -> str | None:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


# --- the digest -----------------------------------------------------------------------------------

_RANK_OF: dict[FindingClass, int] = {
    **dict.fromkeys(ReasonCode, 1),
    Check.AGENT_ZONE_NONCONFORMING: 1,
    Check.TRIGGER_AUTHORITY: 2,
    Check.DANGLING_LINK: 3,
    Check.UNSTAMPED: 4,
    Check.STALE: 5,
}
"""Rank by damage (ADR-0018): a curated note failing the admission bar first, then the provenance
contradiction, dangling links, unstamped notes, staleness, then everything else. A first pass —
changing the order is a change to this table."""

_EVERYTHING_ELSE = 6
_OUTSIDE_CURATED = 6
"""Added to every finding about a note outside curated space, so all of them rank below every
curated one."""


def digest(findings: Iterable[Finding], *, limit: int) -> tuple[tuple[DigestItem, ...], int]:
    """The digest's items, best first and capped at `limit`, and how many there were before the cap.

    One item per note per rank, carrying every class the note has at that rank, so one bad note
    spends one line of a seven-line message rather than four.
    """
    grouped: dict[tuple[int, str], list[FindingClass]] = {}
    for finding in findings:
        rank = _RANK_OF.get(finding.check, _EVERYTHING_ELSE)
        if zone_of(finding.path) is not Zone.CURATED:
            rank += _OUTSIDE_CURATED
        classes = grouped.setdefault((rank, finding.path), [])
        if finding.check not in classes:
            classes.append(finding.check)
    ordered = sorted(grouped, key=lambda key: (key[0], not key[1].startswith(FINANCE_PREFIX), key[1]))
    items = tuple(DigestItem(rank, tuple(grouped[rank, path]), path) for rank, path in ordered)
    return items[:limit], len(items)
