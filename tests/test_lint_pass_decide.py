"""The lint pass's decisions over planted vaults, beyond one-row-per-class (`test_lint_pass_line.py`).

Plan rows 8, 10, 11 and 13 in their pure half — the shell half is `test_lint_pass_shell.py` — plus
the rules the line depends on: which notes are judged at all, what a link may match, how staleness
ages a note nobody reviewed, and the digest's order.
"""

from __future__ import annotations

from datetime import date

import pytest
from lint_vault import ALPHA, BETA, DELTA, MODIFIED, TODAY, fields, text, vault

from obsidian_tools.admission.validator import ReasonCode, admit
from obsidian_tools.lint_pass.checks import Finding, Zone, link_targets, zone_of
from obsidian_tools.lint_pass.decide import VaultFile, decide, digest
from obsidian_tools.lint_pass.line import Check, Fix
from obsidian_tools.vault_schema.frontmatter import ParsedNote, parse_note


def _classes(path: str, files: list[VaultFile]) -> set[object]:
    return {f.check for f in decide(files, today=TODAY).findings if f.path == path}


# --- plan rows 8, 10, 11, 13 ----------------------------------------------------------------------


def test_row_8_a_schedule_trigger_with_human_authority_is_reported_and_neither_field_is_touched() -> None:
    """Which of the two fields is wrong is a judgment (ADR-0018). Red if the contradiction goes
    unreported, or if any fix is planned for the note — a planned fix is a rewrite of its block."""
    decision = decide(
        vault({ALPHA: text(fields("Alpha", trigger="schedule", authority="human"), "[[beta]]\n")}), today=TODAY
    )
    assert Check.TRIGGER_AUTHORITY in {f.check for f in decision.findings if f.path == ALPHA}
    assert all(plan.path != ALPHA for plan in decision.fixes)
    assert all(held.path != ALPHA for held in decision.withheld)


_GUI_TEMPLATE = (
    ("type", "note"),
    ("title", '"Alpha"'),
    ("source", ""),
    ("authority", "human"),
    ("trigger", "human"),
    ("status", "inbox"),
    ("created", ""),
    ("updated", ""),
    ("reviewed", ""),
    ("tags", "[]"),
    ("confidence", ""),
    ("related", "[]"),
    ("refs", "[]"),
)


def test_row_10_a_note_in_the_gui_templates_shape_is_reported_unstamped_and_empty_confidence() -> None:
    """The GUI exception's only visible signature, since a pass without history cannot see a GUI edit
    that leaves every field intact. Its empty dates are stamped; its empty claims are left empty."""
    decision = decide(vault({ALPHA: text(_GUI_TEMPLATE, "[[beta]]\n")}), today=TODAY)
    assert {Check.UNSTAMPED, Check.CONFIDENCE_EMPTY} <= {f.check for f in decision.findings if f.path == ALPHA}
    (plan,) = [p for p in decision.fixes if p.path == ALPHA]
    assert {(c.kind, c.key) for c in plan.changes} == {(Fix.DATE_STAMPED, "created"), (Fix.DATE_STAMPED, "updated")}
    after = parse_note(plan.post_image)
    assert isinstance(after, ParsedNote)
    values = after.frontmatter.as_dict()
    assert values["source"] is None
    assert values["confidence"] is None
    assert values["created"] is not None
    assert values["created"].text == MODIFIED.isoformat()  # type: ignore[union-attr]


def test_row_11_the_raw_layer_is_silent_whatever_it_holds_and_still_a_link_target() -> None:
    """Silence about raw is S2 working. Red if any finding or fix names a raw path — or if a link into
    raw reads as dangling, which would make raw's exemption leak into the notes that cite it."""
    raw = "05-raw/imported-thing.md"
    decision = decide(
        vault(
            {
                raw: "no frontmatter \u2014 [[nowhere]]\n",
                ALPHA: text(fields("Alpha"), "See [[beta]] and [[imported-thing]].\n"),
            }
        ),
        today=TODAY,
    )
    assert [f for f in decision.findings if f.path == raw] == []
    assert [p for p in decision.fixes if p.path == raw] == []
    assert decision.findings == ()


def test_row_13_a_fix_whose_post_image_admission_refuses_is_withheld_not_planned() -> None:
    """Key order is fixable, but the note lacks `type`, so the fixed note would still be refused at the
    curated boundary. Red if the fix is planned: the pass would write a post-image the validator
    never admitted."""
    clean = dict(fields("Alpha", type=None))
    reordered = (("source", clean.pop("source")), *clean.items())
    decision = decide(vault({ALPHA: text(reordered, "[[beta]]\n")}), today=TODAY)

    assert all(plan.path != ALPHA for plan in decision.fixes)
    (held,) = decision.withheld
    assert held.path == ALPHA
    assert held.codes == (ReasonCode.TYPE_MISSING,)
    assert Fix.KEY_ORDER in {c.kind for c in held.changes}
    assert ReasonCode.TYPE_MISSING in _classes(ALPHA, vault({ALPHA: text(reordered, "[[beta]]\n")}))


def test_every_planned_fix_is_a_post_image_the_validator_admits() -> None:
    decision = decide(vault({ALPHA: text(_GUI_TEMPLATE, "[[beta]]\n")}), today=TODAY)
    assert decision.fixes
    assert all(admit(plan.path, plan.post_image).admitted for plan in decision.fixes)


def test_a_fix_outside_curated_space_is_not_withheld_by_admission() -> None:
    """The agent zone is detective only: its fixes are not crossings, so admission never withholds
    them, even on a note admission would refuse."""
    note = text((("title", '"Delta"'), ("type", "note")), "Captured.\n")
    decision = decide(vault({DELTA: note}), today=TODAY)
    assert [p.path for p in decision.fixes] == [DELTA]
    assert decision.withheld == ()


def test_each_fix_carries_the_hash_of_the_bytes_it_was_computed_from() -> None:
    import hashlib

    content = text(fields("Alpha", created="2026/09/01"), "[[beta]]\n")
    (plan,) = decide(vault({ALPHA: content}), today=TODAY).fixes
    assert plan.pre_image_sha256 == hashlib.sha256(content.encode("utf-8")).hexdigest()


# --- zones ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "zone"),
    [
        ("10-areas/tech/x.md", Zone.CURATED),
        ("20-projects/x.md", Zone.CURATED),
        ("00-inbox/x.md", Zone.AGENT),
        ("40-journal/2026-09-22.md", Zone.AGENT),
        ("_ops/agent/x.md", Zone.AGENT),
        ("05-raw/x.md", Zone.RAW),
        ("log.md", Zone.EXEMPT),
        ("CLAUDE.md", Zone.EXEMPT),
        ("_templates/note.md", Zone.EXEMPT),
        ("_ops/lint/2026-09-22.md", Zone.EXEMPT),
        ("_ops/audit/2026-09-22.md", Zone.EXEMPT),
        ("_ops/quarantine/x.md", Zone.EXEMPT),
        ("90-archive/x.md", Zone.EXEMPT),
        (".trash/x.md", Zone.EXEMPT),
        ("10-areas-old/x.md", Zone.ELSEWHERE),
        ("stray.md", Zone.ELSEWHERE),
    ],
)
def test_zone_of(path: str, zone: Zone) -> None:
    assert zone_of(path) is zone


def test_exempt_notes_are_never_judged() -> None:
    bad = "no frontmatter \u201csmart\u201d [[nowhere]]\n"
    exempt = ["_templates/x.md", "_ops/quarantine/x.md", "90-archive/x.md", "CLAUDE.md", "_ops/lint/x.md"]
    decision = decide(vault(dict.fromkeys(exempt, bad)), today=TODAY)
    assert decision.findings == ()
    assert decision.fixes == ()


def test_the_passs_own_records_are_not_link_sources() -> None:
    """A report naming a note must not be what keeps it from being an orphan the next day."""
    files = vault({BETA: text(fields("Beta"), "See [[gamma]].\n"), "_ops/lint/2026-09-21.md": "[[alpha]]\n"})
    assert Check.ORPHAN in _classes(ALPHA, files)


def test_a_link_from_any_other_note_counts_even_from_the_inbox() -> None:
    files = vault({BETA: text(fields("Beta"), "See [[gamma]].\n"), "00-inbox/x.md": "[[alpha]]\n"})
    assert Check.ORPHAN not in _classes(ALPHA, files)


def test_a_self_link_does_not_make_a_note_linked() -> None:
    files = vault(
        {BETA: text(fields("Beta"), "See [[gamma]].\n"), ALPHA: text(fields("Alpha"), "[[alpha]] [[beta]]\n")}
    )
    assert Check.ORPHAN in _classes(ALPHA, files)


def test_counts_depths_and_nonconforming() -> None:
    files = vault(
        {
            "00-inbox/a.md": "x\n",
            "_ops/quarantine/q.md": "x\n",
            BETA: text(fields("Beta"), "See [[gamma]].\n"),  # ALPHA becomes an orphan: not a schema finding
        }
    )
    decision = decide(files, today=TODAY)
    assert decision.inbox_depth == 2
    assert decision.quarantine_depth == 1
    assert decision.notes_by_zone[Zone.CURATED] == 3
    assert decision.nonconforming == 1  # 00-inbox/a.md; the orphan is not drift from the schema


# --- links ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "targets"),
    [
        ("[[a]] and [[b|shown]] and [[c#heading]] and [[d^block]] and ![[e]]", ("a", "b", "c", "d", "e")),
        ("[[#only a heading]]", ()),
        ("`[[in code]]` and [[out]]", ("out",)),
        ("```\n[[fenced]]\n```\n[[after]]", ("after",)),
        ("~~~\n[[fenced]]\n~~~", ()),
    ],
)
def test_link_targets(body: str, targets: tuple[str, ...]) -> None:
    assert link_targets(body) == targets


@pytest.mark.parametrize(
    ("link", "dangling"),
    [
        ("[[beta]]", False),
        ("[[10-areas/homelab/beta]]", False),
        ("[[beta.md]]", False),
        ("[[Beta Alias]]", False),
        ("[[Beta]]", True),  # exact: Obsidian's case-insensitive resolution is the named trap
        ("[[beta-alias]]", True),
        ("[[CLAUDE]]", False),  # exempt notes are still link targets
    ],
)
def test_links_match_exactly_a_stem_a_path_a_filename_or_an_alias(link: str, dangling: bool) -> None:
    beta = text((*fields("Beta"), ("aliases", '["Beta Alias"]')), "See [[alpha]].\n")
    files = vault({BETA: beta, ALPHA: text(fields("Alpha"), f"See {link}.\n"), "CLAUDE.md": "# schema\n"})
    assert (Check.DANGLING_LINK in _classes(ALPHA, files)) is dangling


# --- staleness ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "stale"),
    [
        ({"reviewed": "2025-09-22"}, False),  # exactly the homelab dial, 365 days
        ({"reviewed": "2025-09-21"}, True),
        ({"reviewed": "", "created": "2025-01-01"}, True),  # nobody reviewed it: it ages from created
        ({"reviewed": "", "created": "2026-09-01"}, False),
    ],
)
def test_staleness_against_the_areas_dial(changes: dict[str, str], stale: bool) -> None:
    files = vault({ALPHA: text(fields("Alpha", **changes), "See [[beta]].\n")})
    assert (Check.STALE in _classes(ALPHA, files)) is stale


def test_finance_ages_on_its_own_dial() -> None:
    gamma = "10-areas/finance/gamma.md"
    body = "Balance 10 USD (as of 2026-09, bank statement).\n"
    files = vault({gamma: text(fields("Gamma", authority="import", reviewed="2026-08-22"), body)})
    assert Check.STALE in _classes(gamma, files)  # 31 days, past finance's 30


def test_a_project_has_no_dial_and_is_never_stale() -> None:
    path = "20-projects/old.md"
    files = vault(
        {
            path: text(fields("Old", reviewed="2000-01-01"), "[[beta]]\n"),
            BETA: text(fields("Beta"), "[[old]] [[alpha]]\n"),
        }
    )
    assert Check.STALE not in _classes(path, files)


# --- the digest -----------------------------------------------------------------------------------


def _f(check: object, path: str) -> Finding:
    return Finding(check, path, "")  # type: ignore[arg-type]


def test_the_digest_ranks_by_damage_caps_at_the_limit_and_keeps_curated_above_the_agent_zone() -> None:
    """Plan row 15's order: admission (finance first), contradiction, dangling, unstamped, stale,
    everything else, then every finding outside curated space. Twelve items planted, seven sent."""
    findings = [
        _f(Check.ORPHAN, "10-areas/tech/other.md"),
        _f(Check.AGENT_ZONE_NONCONFORMING, "00-inbox/a.md"),
        _f(Check.STALE, "10-areas/tech/stale.md"),
        _f(Check.UNSTAMPED, "10-areas/tech/unstamped.md"),
        _f(Check.DANGLING_LINK, "10-areas/tech/dangling.md"),
        _f(Check.TRIGGER_AUTHORITY, "10-areas/tech/contradiction.md"),
        _f(ReasonCode.TYPE_MISSING, "10-areas/dining/typeless.md"),
        _f(ReasonCode.WRONG_TYPE, "10-areas/dining/typeless.md"),
        _f(ReasonCode.FINANCE_PROVENANCE, "10-areas/finance/unsourced.md"),
        _f(Check.UNSTAMPED, "00-inbox/b.md"),
        _f(Check.DANGLING_LINK, "40-journal/2026-09-22.md"),
        _f(Check.BANNED_CHARACTER, "_ops/agent/c.md"),
        _f(Check.OUT_OF_VOCABULARY, "stray.md"),
    ]
    items, total = digest(findings, limit=7)
    assert total == 12
    assert [(i.rank, i.path) for i in items] == [
        (1, "10-areas/finance/unsourced.md"),
        (1, "10-areas/dining/typeless.md"),  # sorts before finance by path; finance ranks first anyway
        (2, "10-areas/tech/contradiction.md"),
        (3, "10-areas/tech/dangling.md"),
        (4, "10-areas/tech/unstamped.md"),
        (5, "10-areas/tech/stale.md"),
        (6, "10-areas/tech/other.md"),
    ]
    assert items[1].classes == (ReasonCode.TYPE_MISSING, ReasonCode.WRONG_TYPE)


def test_the_digest_is_empty_when_there_are_no_findings() -> None:
    assert digest([], limit=7) == ((), 0)


def test_agent_zone_findings_follow_the_same_order_below_curated() -> None:
    items, _ = digest(
        [_f(Check.UNSTAMPED, "00-inbox/b.md"), _f(Check.AGENT_ZONE_NONCONFORMING, "00-inbox/a.md")], limit=7
    )
    assert [(i.rank, i.path) for i in items] == [(7, "00-inbox/a.md"), (10, "00-inbox/b.md")]


def test_an_undecodable_note_is_reported_not_crashed_on() -> None:
    files = [*vault(), VaultFile("10-areas/tech/bytes.md", b"---\ntitle: \xff\n---\n", date(2026, 9, 1))]
    classes = _classes("10-areas/tech/bytes.md", files)
    assert ReasonCode.FRONTMATTER_UNPARSEABLE in classes


def test_fixed_address_folders_are_exempt_from_the_slug_rule() -> None:
    """Schema file §7.2: everything in `_ops/` and `_templates/` keeps its address whatever its title."""
    path = "_ops/agent/scratch.md"
    files = vault({path: text(fields("A Readable Title", status="inbox"), "x\n")})
    assert _classes(path, files) == set()


def test_a_finance_confidence_outside_the_vocabulary_is_refused_not_also_reported() -> None:
    """In finance the vocabulary is the hard block's; reporting it again as a vocabulary finding would
    count one defect twice in the numbers the digest ranks by."""
    gamma = "10-areas/finance/gamma.md"
    files = vault(
        {gamma: text(fields("Gamma", authority="import", confidence="sure"), "Balance (as of 2026-09, bank).\n")}
    )
    classes = _classes(gamma, files)
    assert ReasonCode.FINANCE_CONFIDENCE in classes
    assert Check.OUT_OF_VOCABULARY not in classes
