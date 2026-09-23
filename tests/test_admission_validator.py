"""The admission validator's refused tier, as a table: one planted violation and one control per rule.

Policy with no independent oracle, so curated examples rather than properties
(`research-property-based-testing-practice`: "policy = this exact set of patterns" is where a
property degenerates into re-implementing the rule). Every violation is the admitted baseline note
with exactly one change, so a refusal can only be the planted change's doing, and every control is
the nearest note the rule must *not* refuse — the half that catches a rule grown too wide.
"""

from __future__ import annotations

import pytest

from obsidian_tools.admission.validator import ReasonCode, admit

_ABSENT = object()

_BASELINE: dict[str, str | None] = {
    "type": "note",
    "title": '"Kubernetes Ingress"',
    "source": "batch-processor",
    "authority": "import",
    "trigger": "event",
    "status": "inbox",
    "created": "2026-07-30",
    "updated": "2026-07-30",
    "reviewed": None,
    "tags": "[]",
    "confidence": "high",
    "related": "[]",
    "refs": "[]",
}
"""A note that passes every rule, in finance as everywhere: `authority: import`, a `confidence`
in vocabulary. `None` writes a bare `key:`."""

_MARKED_BODY = "## Summary\n\nThe fee is 40 USD (as of 2026-07, embassy.example.gov).\n"


def note(body: str = _MARKED_BODY, **fields: object) -> str:
    """The baseline note with `fields` overridden — a raw YAML value, `None` for `key:`, or
    `_ABSENT` to drop the key. A key not in the baseline is appended."""
    merged: dict[str, object] = {**_BASELINE, **fields}
    lines = [f"{key}:" if value is None else f"{key}: {value}" for key, value in merged.items() if value is not _ABSENT]
    return "---\n" + "".join(f"{line}\n" for line in lines) + "---\n" + body


_HOMELAB = "10-areas/homelab/ingress.md"
_FINANCE = "10-areas/finance/brokerage.md"


def test_the_baseline_is_admitted_in_both_curated_roots_and_in_finance() -> None:
    """The control every row below is measured against. Red if the baseline itself stops passing,
    at which point every "violation" below would be refused for the wrong reason."""
    for path in (_HOMELAB, "20-projects/cutover.md", _FINANCE):
        verdict = admit(path, note())
        assert verdict.crossing
        assert verdict.admitted, verdict.summary()


# --- the table: each row is (code, planted violation, control) ------------------------------------

_ROWS: list[tuple[ReasonCode, str, str, str, str]] = [
    # (code, path, violation, control path, control)
    (ReasonCode.FRONTMATTER_UNPARSEABLE, _HOMELAB, "# Kubernetes Ingress\n\nno frontmatter\n", _HOMELAB, note()),
    (ReasonCode.FRONTMATTER_UNPARSEABLE, _HOMELAB, note(tags="[a, [b]]"), _HOMELAB, note(tags="[a, b]")),
    (ReasonCode.TYPE_MISSING, _HOMELAB, note(type=_ABSENT), _HOMELAB, note(type="concept")),
    (ReasonCode.TYPE_MISSING, _HOMELAB, note(type=None), _HOMELAB, note(type="not-in-the-vocabulary")),
    (ReasonCode.TYPE_MISSING, _HOMELAB, note(type='""'), _HOMELAB, note(type='"note"')),
    (ReasonCode.TYPE_MISSING, _HOMELAB, note(type="null"), _HOMELAB, note(type='"null"')),
    (ReasonCode.PROVENANCE_MISSING, _HOMELAB, note(authority=_ABSENT), _HOMELAB, note(authority="agent")),
    (ReasonCode.PROVENANCE_MISSING, _HOMELAB, note(trigger=None), _HOMELAB, note(trigger="schedule")),
    (ReasonCode.DATE_UNPARSEABLE, _HOMELAB, note(created="30/07/2026"), _HOMELAB, note(created="2026/07/30")),
    (ReasonCode.DATE_UNPARSEABLE, _HOMELAB, note(updated="07/30/2026"), _HOMELAB, note(updated="2026.7.30")),
    (ReasonCode.DATE_UNPARSEABLE, _HOMELAB, note(reviewed="2026-02-30"), _HOMELAB, note(reviewed="2026-02-28")),
    (ReasonCode.DATE_UNPARSEABLE, _HOMELAB, note(consolidated="yesterday"), _HOMELAB, note(consolidated=None)),
    (ReasonCode.DATE_UNPARSEABLE, _HOMELAB, note(created="2026-07-30T10:00"), _HOMELAB, note(created=None)),
    (ReasonCode.WRONG_TYPE, _HOMELAB, note(tags="foo"), _HOMELAB, note(tags="[foo]")),
    (ReasonCode.WRONG_TYPE, _HOMELAB, note(aliases="Ingress"), _HOMELAB, note(aliases="[Ingress]")),
    (ReasonCode.WRONG_TYPE, _HOMELAB, note(title="[a, b]"), _HOMELAB, note(title='"[a, b]"')),
    (ReasonCode.WRONG_TYPE, _HOMELAB, note(created="[2026-07-30]"), _HOMELAB, note(created="2026-07-30")),
    (ReasonCode.WRONG_TYPE, _HOMELAB, note(salience='"5"'), _HOMELAB, note(salience="5")),
    (ReasonCode.WRONG_TYPE, _HOMELAB, note(salience="5.5"), _HOMELAB, note(salience="12")),
    (ReasonCode.FINANCE_AUTHORITY, _FINANCE, note(authority="human"), _HOMELAB, note(authority="human")),
    (ReasonCode.FINANCE_AUTHORITY, _FINANCE, note(authority="Import"), _FINANCE, note(authority="'import'")),
    (ReasonCode.FINANCE_CONFIDENCE, _FINANCE, note(confidence=None), _HOMELAB, note(confidence=None)),
    (ReasonCode.FINANCE_CONFIDENCE, _FINANCE, note(confidence="stated"), _FINANCE, note(confidence="speculation")),
    (
        ReasonCode.FINANCE_PROVENANCE,
        _FINANCE,
        note(body="The fee is 40 USD.\n"),
        _HOMELAB,
        note(body="The fee is 40 USD.\n"),
    ),
    (
        ReasonCode.FINANCE_PROVENANCE,
        _FINANCE,
        note(body="The fee is 40 USD (as of July 2026, embassy.example.gov).\n"),
        _FINANCE,
        note(body="The fee is 40 USD (as of 2026-07-30, embassy.example.gov).\n"),
    ),
    (
        ReasonCode.FINANCE_PROVENANCE,
        _FINANCE,
        note(body="Fee: 40 USD (as of 2026-07, ).\n"),
        _FINANCE,
        note(body="Fee: 40 USD (as of 2026-07, statement).\n"),
    ),
]


@pytest.mark.parametrize(("code", "path", "violation", "control_path", "control"), _ROWS)
def test_each_rule_fires_on_its_violation_and_only_that_rule(
    code: ReasonCode, path: str, violation: str, control_path: str, control: str
) -> None:
    """Red if the rule does not fire on its planted violation, or if anything else fires with it —
    the second half is what makes each row evidence about *this* rule rather than about the
    baseline."""
    verdict = admit(path, violation)

    assert verdict.crossing
    assert verdict.codes == (code,), verdict.summary()


@pytest.mark.parametrize(("code", "path", "violation", "control_path", "control"), _ROWS)
def test_each_rule_stays_silent_on_its_control(
    code: ReasonCode, path: str, violation: str, control_path: str, control: str
) -> None:
    """Red if the rule has grown to refuse the nearest note it must admit — an out-of-vocabulary
    `type`, a year-first date, `authority: human` outside finance, a quoted `"null"`."""
    verdict = admit(control_path, control)

    assert verdict.admitted, verdict.summary()


def test_every_reason_code_has_a_planted_violation() -> None:
    """A code with no row is a rule nobody has seen fire. Red when a code is added without one."""
    assert {row[0] for row in _ROWS} == set(ReasonCode)


# --- how rules compose --------------------------------------------------------------------------


def test_every_violation_is_reported_not_just_the_first() -> None:
    """One refused chunk should name every defect at once, rather than one per redelivery."""
    verdict = admit(_FINANCE, note(body="no marker\n", type=_ABSENT, trigger=_ABSENT, created="30/07/2026"))

    assert verdict.codes == (
        ReasonCode.TYPE_MISSING,
        ReasonCode.PROVENANCE_MISSING,
        ReasonCode.DATE_UNPARSEABLE,
        ReasonCode.FINANCE_PROVENANCE,
    )


def test_a_missing_authority_in_finance_counts_against_both_rules_it_breaks() -> None:
    """Each rule is counted alone (the codes are the O1 dimension); neither hides the other."""
    verdict = admit(_FINANCE, note(authority=_ABSENT))

    assert verdict.codes == (ReasonCode.PROVENANCE_MISSING, ReasonCode.FINANCE_AUTHORITY)
    assert {r.field for r in verdict.refusals} == {"authority"}


def test_a_mistyped_field_is_judged_by_wrong_type_alone() -> None:
    """A list has no reading as a date or as `import`; refusing it again under those codes would
    count one defect three times."""
    verdict = admit(_FINANCE, note(created="[2026-07-30]", authority="[import]"))

    assert verdict.codes == (ReasonCode.WRONG_TYPE,)
    assert {r.field for r in verdict.refusals} == {"created", "authority"}


def test_the_finance_block_is_presence_based_at_note_level() -> None:
    """The stated limit of ADR-0010's evidence-keyed block, pinned so it is not "fixed" into a
    detector of fact-asserting prose: one marked figure and one unmarked figure pass together."""
    body = "The fee is 40 USD (as of 2026-07, embassy.example.gov). The deposit is 200 USD.\n"

    assert admit(_FINANCE, note(body=body)).admitted


def test_the_gui_templates_shape_is_admitted_because_its_gaps_are_the_lint_passes_to_report() -> None:
    """A note in `_templates/`' own shape — empty `source`, `confidence`, `created`, `updated` — is
    within the refused tier's tolerance. Red if the validator starts refusing what the tolerance
    line puts in the reported tier, which would turn every GUI-shaped note into a refusal."""
    template = note(
        title='"Untitled"',
        source=None,
        authority="human",
        trigger="human",
        created=None,
        updated=None,
        confidence=None,
    )

    assert admit(_HOMELAB, template).admitted


# --- zones --------------------------------------------------------------------------------------

_UNPARSEABLE = "not a note at all"


@pytest.mark.parametrize(
    "path",
    [
        "00-inbox/capture.md",
        "05-raw/import/statement.md",
        "40-journal/2026-07-30.md",
        "_ops/agent/scratch.md",
        "log.md",
        "90-archive/old.md",
        "10-areas-old/x.md",
        "10-areas",
    ],
)
def test_a_path_outside_curated_space_is_not_a_crossing_whatever_its_content(path: str) -> None:
    """The agent zone is detective only and the raw layer exempt (ADR-0007, ADR-0015). Red if the
    validator judges outside curated space — or, via `10-areas-old/`, matches a prefix without
    its slash."""
    verdict = admit(path, _UNPARSEABLE)

    assert not verdict.crossing
    assert verdict.admitted
    assert verdict.refusals == ()


@pytest.mark.parametrize(
    ("path", "resolved"),
    [
        ("10-areas/homelab/x.md", "10-areas/homelab/x.md"),
        ("/10-areas/homelab/x.md", "10-areas/homelab/x.md"),
        ("./20-projects/x.md", "20-projects/x.md"),
        ("00-inbox/../10-areas/x.md", "10-areas/x.md"),
        ("../10-areas/x.md", "10-areas/x.md"),
        ("a/../../../20-projects/x.md", "20-projects/x.md"),
        ("10-areas//finance/./x.md", "10-areas/finance/x.md"),
    ],
)
def test_no_spelling_of_a_curated_path_escapes_the_crossing(path: str, resolved: str) -> None:
    """Red if a path a door could resolve into curated space is read as outside it — the one way a
    caller could route a write around this gate without knowing it had."""
    verdict = admit(path, _UNPARSEABLE)

    assert verdict.crossing
    assert verdict.path == resolved
    assert verdict.codes == (ReasonCode.FRONTMATTER_UNPARSEABLE,)


def test_a_finance_rule_follows_the_resolved_path() -> None:
    verdict = admit("10-areas/homelab/../finance/x.md", note(authority="human"))

    assert verdict.codes == (ReasonCode.FINANCE_AUTHORITY,)
