"""The tolerance line, proven row by row (plan row 7): one planted instance per row lands in its tier,
and the clean vault — its control — does not.

**The expected tier is written here, not read from `LINE`.** A test that looked the tier up in the
table it is testing would pass whatever the table said. Stated independently, moving a row's tier
in `line.py` without moving its behaviour turns that row red, and moving the behaviour without the
row turns it red too.

**A row with no fixture fails the suite**, except the not-checked rows, which by definition have
nothing to plant: silence there is not a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from lint_vault import ALPHA, BETA, DELTA, FINANCE_BODY, GAMMA, TODAY, fields, finance_fields, text, vault

from obsidian_tools.admission.validator import ReasonCode
from obsidian_tools.lint_pass.decide import Decision, decide
from obsidian_tools.lint_pass.line import LINE, TIER_OF, Check, Fix, NotChecked, RowName, Tier, Tolerated


@dataclass(frozen=True)
class Plant:
    tier: Tier
    path: str
    overrides: dict[str, str | None]


_ALPHA_BODY = "See [[beta]].\n"


def _alpha(body: str = _ALPHA_BODY, **changes: str | None) -> dict[str, str | None]:
    return {ALPHA: text(fields("Alpha", **changes), body)}


def _gamma(body: str = FINANCE_BODY, **changes: str | None) -> dict[str, str | None]:
    return {GAMMA: text(finance_fields(**changes), body)}


def _reordered(*keys: str) -> dict[str, str | None]:
    clean = dict(fields("Alpha"))
    order = [*keys, *(key for key in clean if key not in keys)]
    return {ALPHA: text(tuple((key, clean[key]) for key in order), _ALPHA_BODY)}


PLANTS: dict[RowName, Plant] = {
    # refused: a curated note already in a refused state is reported at the refused tier
    ReasonCode.FRONTMATTER_UNPARSEABLE: Plant(Tier.REFUSED, ALPHA, {ALPHA: "no frontmatter here\n[[beta]]\n"}),
    ReasonCode.TYPE_MISSING: Plant(Tier.REFUSED, ALPHA, _alpha(type=None)),
    ReasonCode.PROVENANCE_MISSING: Plant(Tier.REFUSED, ALPHA, _alpha(authority=None)),
    ReasonCode.DATE_UNPARSEABLE: Plant(Tier.REFUSED, ALPHA, _alpha(created="30/07/2026")),
    ReasonCode.WRONG_TYPE: Plant(Tier.REFUSED, ALPHA, _alpha(tags="foo")),
    ReasonCode.FINANCE_AUTHORITY: Plant(Tier.REFUSED, GAMMA, _gamma(authority="human")),
    ReasonCode.FINANCE_CONFIDENCE: Plant(Tier.REFUSED, GAMMA, _gamma(confidence="")),
    ReasonCode.FINANCE_PROVENANCE: Plant(Tier.REFUSED, GAMMA, _gamma(body="Balance 10 USD.\n")),
    # fixed
    Fix.KEY_ORDER: Plant(Tier.FIXED, ALPHA, _reordered("title", "type")),
    Fix.DATE_FORMAT: Plant(Tier.FIXED, ALPHA, _alpha(created="2026/09/01")),
    Fix.TAG_CASE: Plant(Tier.FIXED, ALPHA, _alpha(tags="[Homelab]")),
    Fix.KEY_INSERTED: Plant(Tier.FIXED, ALPHA, _alpha(refs=None)),
    Fix.DATE_STAMPED: Plant(Tier.FIXED, ALPHA, _alpha(created="")),
    # reported
    Check.TRIGGER_AUTHORITY: Plant(Tier.REPORTED, ALPHA, _alpha(trigger="schedule", authority="human")),
    Check.UNSTAMPED: Plant(Tier.REPORTED, ALPHA, _alpha(source="")),
    Check.CONFIDENCE_EMPTY: Plant(Tier.REPORTED, ALPHA, _alpha(confidence="")),
    Check.OUT_OF_VOCABULARY: Plant(Tier.REPORTED, ALPHA, _alpha(status="draft")),
    Check.SLUG_MISMATCH: Plant(Tier.REPORTED, ALPHA, _alpha(title='"Something Else"')),
    Check.TITLE_UNSLUGGABLE: Plant(Tier.REPORTED, ALPHA, _alpha(title='"!!!"')),
    Check.OPTIONAL_FIELD_EMPTY: Plant(Tier.REPORTED, ALPHA, _alpha(salience="")),
    Check.REMOVED_SINGULAR_KEY: Plant(Tier.REPORTED, ALPHA, _alpha(tag="homelab")),
    Check.BANNED_CHARACTER: Plant(Tier.REPORTED, ALPHA, _alpha(body="See [[beta]] \u2014 soon.\n")),
    Check.DANGLING_LINK: Plant(Tier.REPORTED, ALPHA, _alpha(body="See [[beta]] and [[nowhere]].\n")),
    Check.ORPHAN: Plant(Tier.REPORTED, ALPHA, {BETA: text(fields("Beta"), "See [[gamma]].\n")}),
    Check.STALE: Plant(Tier.REPORTED, ALPHA, _alpha(reviewed="2025-09-01")),
    Check.AGENT_ZONE_NONCONFORMING: Plant(
        Tier.REPORTED, DELTA, {DELTA: text(fields("Delta", status="inbox", type=None), "Captured.\n")}
    ),
    # tolerated: planted, and silent
    Tolerated.TAG_SHAPE: Plant(Tier.TOLERATED, ALPHA, _alpha(tags='["#half done"]')),
    Tolerated.SALIENCE_RANGE: Plant(Tier.TOLERATED, ALPHA, _alpha(salience="42")),
}


def _outcome(decision: Decision, name: RowName, path: str) -> dict[str, bool]:
    """Whether the row's class shows up at `path`, as a finding or as a planned or withheld change."""
    changes = [c for plan in decision.fixes if plan.path == path for c in plan.changes]
    changes += [c for held in decision.withheld if held.path == path for c in held.changes]
    return {
        "finding": any(f.check == name and f.path == path for f in decision.findings),
        "change": any(c.kind == name for c in changes),
        "anything": any(f.path == path for f in decision.findings) or bool(changes),
    }


def test_every_row_that_can_be_planted_has_a_plant_and_no_plant_names_a_missing_row() -> None:
    plantable = {row.name for row in LINE if row.tier is not Tier.NOT_CHECKED}
    assert set(PLANTS) == plantable
    assert {row.name for row in LINE if row.tier is Tier.NOT_CHECKED} == set(NotChecked)


def test_row_names_are_distinct_across_every_class() -> None:
    """Every class is keyed by its string value; two enums sharing a value would share a tier."""
    names = [str(row.name) for row in LINE]
    assert len(names) == len(set(names))


def test_the_refused_rows_are_the_validators_codes_and_nothing_else() -> None:
    assert {row.name for row in LINE if row.tier is Tier.REFUSED} == set(ReasonCode)


def test_the_clean_vault_is_silent() -> None:
    """The control every row shares. Red if any check fires on conforming notes."""
    decision = decide(vault(), today=TODAY)
    assert decision.findings == ()
    assert decision.fixes == ()
    assert decision.withheld == ()


@pytest.mark.parametrize("name", list(PLANTS), ids=str)
def test_each_planted_row_lands_in_its_tier(name: RowName) -> None:
    plant = PLANTS[name]
    assert TIER_OF[name] is plant.tier, f"{name} is tiered {TIER_OF[name]} in the line, but planted as {plant.tier}"

    planted = _outcome(decide(vault(plant.overrides), today=TODAY), name, plant.path)
    control = _outcome(decide(vault(), today=TODAY), name, plant.path)

    match plant.tier:
        case Tier.REFUSED | Tier.REPORTED:
            assert planted["finding"], f"{name} planted at {plant.path} was not reported"
            assert not planted["change"]
        case Tier.FIXED:
            assert planted["change"], f"{name} planted at {plant.path} was not fixed"
            assert not planted["finding"]
        case Tier.TOLERATED:
            assert not planted["anything"], f"{name} is tolerated, but the pass said or did something about it"
        case Tier.NOT_CHECKED:
            pytest.fail("a not-checked row has nothing to plant")
    assert not control["finding"]
    assert not control["change"]
