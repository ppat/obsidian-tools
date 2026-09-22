"""The lint pass's I/O: read the mount, write through the door, push the digest, log the summary.

Everything here does what `decide` already decided. The one judgment left to the shell is the one
only the shell can make, because it needs the vault as it is *now*:

**A fix is written only over the bytes it was computed from.** Before each write the note is read
back through the door and hashed; if it no longer matches what was read from the mount, the fix is
skipped as `changed_since_read`. No write tool takes a precondition (DESIGN.md §5), so this narrows
the lost-update window rather than closing it — but a silent clobber of a concurrent edit is
exactly what "fail loud, destroy nothing" forbids, and the pass would otherwise sit in that window
for its whole run rather than for one round trip.

**Every step is attempted, and any failure fails the pass.** A refused fix, the report, the audit
trail, the log line and the digest push are independent: one failing does not stop the others, and
any of them failing exits non-zero, read from the exit and the summary line rather than retried —
the reasoning ADR-0049 gives for a scheduled processor. A pass that merely has findings exits 0.

**The door's append creates an absent note** (ADR-0053's third ground), so the pass appends only to
a note the mount showed it: the log line to `log.md`, and a later pass's section to the day's audit
note. An absent `log.md` fails the pass rather than being created as a one-line fragment.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from obsidian_tools.batch_processor.mcp_client import McpClient, McpError
from obsidian_tools.batch_processor.transport import TransportUnreachableError, build_opener, send
from obsidian_tools.lint_pass.checks import Zone
from obsidian_tools.lint_pass.decide import FixPlan, VaultFile, decide, digest
from obsidian_tools.lint_pass.line import LINE, TIER_OF, Tier
from obsidian_tools.lint_pass.records import (
    FixOutcomes,
    audit_note,
    audit_path,
    audit_section,
    digest_message,
    log_line,
    report,
    report_path,
)
from obsidian_tools.logging_config import LOG_PATH_SAMPLE_LIMIT

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1

LOG_PATH = "log.md"
DIGEST_SOURCE = "obsidian-lint-pass"


class DigestPushError(RuntimeError):
    """The hook did not accept the digest."""


@dataclass(frozen=True, slots=True)
class DigestHook:
    """OpenClaw's `/hooks/agent`: the path n8n's notify workflow and the Alertmanager relay already
    use to reach WhatsApp, so the review loop needs no new component (ADR-0018). The token is a
    bearer credential, and like the gateway key it is never logged; redirects are refused for the
    same reason (`batch_processor/transport.py`)."""

    url: str
    token: str
    timeout_seconds: float

    def push(self, message: str) -> None:
        body = json.dumps({"source": DIGEST_SOURCE, "message": message}).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        try:
            response = send(
                build_opener(verify_tls=True),
                self.url,
                method="POST",
                headers=headers,
                body=body,
                timeout=self.timeout_seconds,
            )
        except TransportUnreachableError as exc:
            raise DigestPushError(str(exc)) from exc
        if not 200 <= response.status < 300:
            raise DigestPushError(f"POST {self.url} answered HTTP {response.status}")


def read_vault(root: Path) -> list[VaultFile]:
    """Every file under `root`, with each markdown note's bytes. Dot-folders are not walked —
    Obsidian indexes none of them, so none is a note or a link target — and neither is anything
    that is not a regular file: a symlink could point anywhere, and this reads through a mount."""
    files: list[VaultFile] = []
    for directory, subdirectories, names in os.walk(root):
        subdirectories[:] = sorted(d for d in subdirectories if not d.startswith("."))
        for name in sorted(names):
            if name.startswith("."):
                continue
            full = Path(directory) / name
            status = full.lstat()
            if not stat.S_ISREG(status.st_mode):
                continue
            modified = datetime.fromtimestamp(status.st_mtime, tz=UTC).date()
            content = full.read_bytes() if name.endswith(".md") else None
            files.append(VaultFile(full.relative_to(root).as_posix(), content, modified))
    return files


def apply_fixes(mcp: McpClient, plans: tuple[FixPlan, ...]) -> FixOutcomes:
    applied: list[FixPlan] = []
    changed: list[FixPlan] = []
    failed: list[tuple[FixPlan, str]] = []
    for plan in plans:
        try:
            current = mcp.read_note(plan.path)
            if current is None or hashlib.sha256(current.encode("utf-8")).hexdigest() != plan.pre_image_sha256:
                changed.append(plan)
                continue
            mcp.write_note(plan.path, plan.post_image, overwrite=True)
        except McpError as exc:
            failed.append((plan, str(exc)))
            continue
        applied.append(plan)
    return FixOutcomes(tuple(applied), tuple(changed), tuple(failed))


def run_pass(*, root: Path, mcp: McpClient, hook: DigestHook, digest_limit: int, now: datetime) -> int:
    started = time.monotonic()
    today = now.date()
    files = read_vault(root)
    decision = decide(files, today=today)
    items, digest_total = digest(decision.findings, limit=digest_limit)
    present = {f.path: f.content for f in files}
    failures: list[str] = []

    try:
        mcp.connect()
        outcomes = apply_fixes(mcp, decision.fixes)
    except McpError as exc:
        failures.append(f"connect: {exc}")
        outcomes = FixOutcomes((), (), tuple((plan, "the door was not reachable") for plan in decision.fixes))
    failures.extend(f"fix {plan.path}: {why}" for plan, why in outcomes.failed)

    def attempt(what: str, action: Callable[[], None]) -> None:
        try:
            action()
        except (McpError, DigestPushError) as exc:
            failures.append(f"{what}: {exc}")

    if outcomes.applied:
        path = audit_path(today)
        if path in present:
            attempt("audit trail", lambda: mcp.append_note(path, audit_section(now, outcomes.applied)))
        else:
            attempt(
                "audit trail", lambda: mcp.write_note(path, audit_note(today, now, outcomes.applied), overwrite=False)
            )
    attempt(
        "report",
        lambda: mcp.write_note(
            report_path(today), report(today, decision, outcomes, digest_total=digest_total), overwrite=True
        ),
    )
    log_content = present.get(LOG_PATH)
    if log_content is None:
        failures.append(f"log line: {LOG_PATH} is not on the mount, and appending would create it as a fragment")
    else:
        separator = "" if log_content.endswith(b"\n") or not log_content else "\n"
        attempt("log line", lambda: mcp.append_note(LOG_PATH, separator + log_line(today, decision, outcomes)))
    if items:
        attempt("digest", lambda: hook.push(digest_message(today, items, digest_total)))

    # Every zone, class and tier is emitted, zeros included: a count that is absent on a quiet day
    # makes a log query's series vanish instead of reading zero.
    by_class = Counter(str(f.check) for f in decision.findings)
    by_tier = Counter(str(TIER_OF[f.check]) for f in decision.findings)
    counted = [row for row in LINE if row.tier in (Tier.REFUSED, Tier.REPORTED)]
    logger.info(
        "lint pass complete",
        extra={
            "event": "lint_pass_complete",
            "outcome": "failed" if failures else "ok",
            **{f"notes_{zone}": decision.notes_by_zone.get(zone, 0) for zone in Zone},
            "inbox_depth": decision.inbox_depth,
            "quarantine_depth": decision.quarantine_depth,
            "findings": len(decision.findings),
            **{f"findings_{row.name}": by_class.get(str(row.name), 0) for row in counted},
            **{f"findings_tier_{tier}": by_tier.get(str(tier), 0) for tier in (Tier.REFUSED, Tier.REPORTED)},
            "nonconforming": decision.nonconforming,
            "unstamped": by_class.get("unstamped", 0),
            "fixes_applied": len(outcomes.applied),
            "fixes_withheld_admission": len(decision.withheld),
            "fixes_changed_since_read": len(outcomes.changed_since_read),
            "fixes_failed": len(outcomes.failed),
            "digest_items_sent": len(items) if items and not any(f.startswith("digest") for f in failures) else 0,
            "duration_seconds": round(time.monotonic() - started, 3),
            "failure_count": len(failures),
            "failures": failures[:LOG_PATH_SAMPLE_LIMIT],
        },
    )
    return EXIT_FAILED if failures else EXIT_OK
