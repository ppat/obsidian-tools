"""The text of everything the pass writes and sends. Pure — the shell supplies the date and outcomes.

- **The report**, `_ops/lint/YYYY-MM-DD.md`: rewritten whole each pass. Its header restates the
  tolerance line.
- **The audit trail**, `_ops/audit/YYYY-MM-DD.md`: one section per pass that changed anything,
  appended and never rewritten, because it is history — the third granularity beside git and the
  log (DESIGN.md).
- **The log line**, in `log.md`: one per pass, in the file's own format.
- **The digest**, to OpenClaw's hook: only when there are items. A daily "nothing found" is noise
  to a single operator.

The report and audit notes carry schema-conforming frontmatter stamped as what wrote them —
`source: vault-worker` (the schema file's name for this process), `authority: agent`,
`trigger: schedule` — so the pass stays inside the contract it enforces.

Paths and values quoted from notes are escaped to ASCII and never written as wikilinks: the report
must not carry the characters it reports, and a note named in a report must not stop being an
orphan because the report named it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from obsidian_tools.lint_pass.checks import Finding, quote
from obsidian_tools.lint_pass.decide import JUDGED, Decision, DigestItem, FixPlan
from obsidian_tools.lint_pass.line import EXEMPT_STATEMENT, LINE, TIER_OF, Tier
from obsidian_tools.lint_pass.normalise import Change
from obsidian_tools.vault_schema.frontmatter import (
    Frontmatter,
    ParsedNote,
    Scalar,
    Style,
    Value,
    emit_note,
    emit_scalar,
)

SOURCE = "vault-worker"
"""The schema file's vocabulary name for this process (§1, §5)."""


def report_path(day: date) -> str:
    return f"_ops/lint/{day.isoformat()}.md"


def audit_path(day: date) -> str:
    return f"_ops/audit/{day.isoformat()}.md"


@dataclass(frozen=True, slots=True)
class FixOutcomes:
    """What became of each planned fix once the shell tried it."""

    applied: tuple[FixPlan, ...]
    changed_since_read: tuple[FixPlan, ...]
    failed: tuple[tuple[FixPlan, str], ...]


# --- the report -----------------------------------------------------------------------------------


def report(day: date, decision: Decision, outcomes: FixOutcomes, *, digest_total: int) -> str:
    lines = [
        f"# Lint report {day.isoformat()}",
        "",
        "## The tolerance line",
        "",
        "What this pass refuses, fixes, reports and tolerates, and what it does not check. Silence about "
        "a class it checks is a verdict; silence about one it does not check is not.",
        "",
    ]
    refused = ", ".join(f"`{row.name}`" for row in LINE if row.tier is Tier.REFUSED)
    lines += [
        f"**Refused** at a curated crossing by the admission validator, and reported at the top rank for a "
        f"curated note already in that state: {refused}.",
        "",
    ]
    for tier in (Tier.FIXED, Tier.REPORTED, Tier.TOLERATED, Tier.NOT_CHECKED):
        lines += [f"**{tier.capitalize()}.**", ""]
        lines.extend(f"- {row.statement}" for row in LINE if row.tier is tier)
        lines.append("")
    lines += [f"**Scope.** {EXEMPT_STATEMENT}", ""]

    zones = ", ".join(f"{zone} {count}" for zone, count in sorted(decision.notes_by_zone.items()))
    lines += [
        "## Summary",
        "",
        f"- Notes: {zones or 'none'}",
        f"- Inbox depth {decision.inbox_depth}; quarantine depth {decision.quarantine_depth}",
        f"- Findings: {len(decision.findings)}, on {len({f.path for f in decision.findings})} notes; "
        f"{digest_total} digest items before the cap",
        f"- Non-conforming notes: {decision.nonconforming}",
        f"- Fixes: {len(outcomes.applied)} applied, {len(decision.withheld)} withheld by admission, "
        f"{len(outcomes.changed_since_read)} skipped as changed since read, {len(outcomes.failed)} failed",
        "",
        "## Findings",
        "",
    ]
    if decision.findings:
        lines += ["| Path | Class | Tier | Detail |", "| --- | --- | --- | --- |"]
        lines.extend(_finding_row(f) for f in decision.findings)
    else:
        lines.append("None.")
    lines += ["", "## Fixes not applied", ""]
    not_applied = [
        *(f"- `{quote(w.path)}`: withheld by admission ({', '.join(w.codes)})" for w in decision.withheld),
        *(f"- `{quote(p.path)}`: skipped, changed since read" for p in outcomes.changed_since_read),
        *(f"- `{quote(p.path)}`: failed: {quote(why)}" for p, why in outcomes.failed),
    ]
    lines += not_applied or ["None."]
    lines.append("")
    return _note(f"Lint report {day.isoformat()}", day, "\n".join(lines))


def _finding_row(finding: Finding) -> str:
    return f"| `{quote(finding.path)}` | {finding.check} | {TIER_OF[finding.check]} | {finding.detail} |"


# --- the audit trail ------------------------------------------------------------------------------


def audit_note(day: date, run_started: datetime, applied: Sequence[FixPlan]) -> str:
    """The day's audit note, as first written: its frontmatter, its heading and this pass's section."""
    body = f"# Normalisation audit {day.isoformat()}\n\nEvery frontmatter change the lint pass made.\n\n"
    return _note(f"Normalisation audit {day.isoformat()}", day, body + audit_section(run_started, applied))


def audit_section(run_started: datetime, applied: Sequence[FixPlan]) -> str:
    """One pass's changes: appended to the day's audit note, never rewritten over an earlier pass's."""
    lines = [f"## Pass {run_started.isoformat(timespec='seconds')}", "", "| Path | Change | Key | Before | After |"]
    lines.append("| --- | --- | --- | --- | --- |")
    for plan in applied:
        lines.extend(_change_row(plan.path, change) for change in plan.changes)
    return "\n".join(lines) + "\n\n"


def _change_row(path: str, change: Change) -> str:
    key = f"`{change.key}`" if change.key is not None else "-"
    before, after = _shown(change.before), _shown(change.after)
    return f"| `{quote(path)}` | {change.kind} | {key} | {before} | {after} |"


def _shown(value: Value) -> str:
    if value is None:
        return "(empty)"
    if isinstance(value, Scalar):
        return f"`{quote(emit_scalar(value))}`"
    return "`[" + quote(", ".join(emit_scalar(item) for item in value)) + "]`"


# --- the log line and the digest ------------------------------------------------------------------


def log_line(day: date, decision: Decision, outcomes: FixOutcomes) -> str:
    """One line in `log.md`'s own format, ending in a line break."""
    judged = sum(count for zone, count in decision.notes_by_zone.items() if zone in JUDGED)
    return (
        f"- {day.isoformat()} | {SOURCE} | lint pass: {judged} notes judged, {len(decision.findings)} findings, "
        f"{len(outcomes.applied)} fixes applied, {len(decision.withheld)} withheld by admission, "
        f"{len(outcomes.changed_since_read)} skipped as changed since read; report {report_path(day)}\n"
    )


def digest_message(day: date, items: Sequence[DigestItem], total: int) -> str:
    """The digest as sent: a header, then one line per item — rank, class, path."""
    header = (
        f"BRAIN lint {day.isoformat()}: {total} items to review, top {len(items)} by damage. Report: {report_path(day)}"
    )
    lines = [f"{n}. [{item.rank}] {', '.join(item.classes)} - {quote(item.path)}" for n, item in enumerate(items, 1)]
    return "\n".join([header, *lines])


# --- shared ---------------------------------------------------------------------------------------


def _note(title: str, day: date, body: str) -> str:
    stamp = Scalar(day.isoformat())
    frontmatter = Frontmatter(
        (
            ("type", Scalar("note")),
            ("title", Scalar(title, Style.DOUBLE)),
            ("source", Scalar(SOURCE)),
            ("authority", Scalar("agent")),
            ("trigger", Scalar("schedule")),
            ("status", Scalar("processed")),
            ("created", stamp),
            ("updated", stamp),
            ("reviewed", None),
            ("tags", ()),
            ("confidence", Scalar("high")),
            ("related", ()),
            ("refs", ()),
        )
    )
    return emit_note(ParsedNote(frontmatter, "\n" + body))
