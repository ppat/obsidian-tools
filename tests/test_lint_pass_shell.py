"""The lint pass end to end: a real directory as the mount, the vault stub as the door on a real
socket, and a stub hook endpoint for the digest.

Plan rows 9, 11, 13, 14 and 15 in their shell half, and the records. What the stub is and is not
evidence for is in `vault_stub.py`: it proves what the pass puts on the wire and what it refuses to
send, never what the deployed surface does with it — that is harness S9's.

**The mount and the door are seeded separately, on purpose.** They are two views of one vault, and
the pass's one shell-side judgment — do not write over a note that changed since it was read — is
exactly a disagreement between them. Seeding them identically is the ordinary case; seeding one
note differently is the violation injection.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from lint_vault import ALPHA, BETA, CLEAN, MODIFIED, fields, text
from vault_stub import TOOL_APPEND, TOOL_READ, TOOL_WRITE, FakeVault, running_vault

from obsidian_tools.admission.validator import admit
from obsidian_tools.batch_processor.mcp_client import McpClient, McpFailedError, McpToolNames
from obsidian_tools.lint_pass.shell import DIGEST_SOURCE, EXIT_FAILED, EXIT_OK, DigestHook, read_vault, run_pass
from obsidian_tools.vault_schema.frontmatter import ParsedNote, parse_note

NOW = datetime(2026, 9, 22, 5, 30, tzinfo=UTC)
REPORT = "_ops/lint/2026-09-22.md"
AUDIT = "_ops/audit/2026-09-22.md"
TOKEN = "stub-hooks-token"

GUI_NOTE = "10-areas/homelab/gui-edit.md"
CONTRADICTION = "10-areas/homelab/contradiction.md"
STALE = "10-areas/homelab/stale.md"
DANGLING = "10-areas/homelab/dangling.md"
WITHHELD = "10-areas/homelab/withheld.md"
RAW = "05-raw/imported.md"

_GUI_FIELDS = (
    ("type", "note"),
    ("title", '"GUI edit"'),
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

PLANTED: dict[str, str] = {
    **CLEAN,
    GUI_NOTE: text(_GUI_FIELDS, "Typed at the GUI. [[beta]]\n"),
    CONTRADICTION: text(fields("Contradiction", trigger="schedule", authority="human"), "[[beta]]\n"),
    STALE: text(fields("Stale", reviewed="2024-01-01"), "[[beta]]\n"),
    DANGLING: text(fields("Dangling"), "[[beta]] and [[nowhere]]\n"),
    WITHHELD: text((("source", "claude-code"), *fields("Withheld", type=None, source=None)), "[[beta]]\n"),
    RAW: "no frontmatter at all, [[nowhere-either]]\n",
}


# --- the hook ---------------------------------------------------------------------------------------


@dataclass
class FakeHook:
    status: int = 202
    url: str = ""
    posts: list[tuple[dict[str, str], dict[str, object]]] = field(
        default_factory=list[tuple[dict[str, str], dict[str, object]]]
    )


@contextmanager
def running_hook(hook: FakeHook) -> Generator[FakeHook]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            hook.posts.append((dict(self.headers.items()), json.loads(body)))
            self.send_response(hook.status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    hook.url = f"http://127.0.0.1:{server.server_port}/hooks/agent"
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        yield hook
    finally:
        server.shutdown()
        server.server_close()


# --- the pass ---------------------------------------------------------------------------------------


def _mount(root: Path, notes: dict[str, str]) -> Path:
    stamp = datetime(MODIFIED.year, MODIFIED.month, MODIFIED.day, 12, tzinfo=UTC).timestamp()
    for path, content in notes.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))
        os.utime(target, (stamp, stamp))
    return root


def _run(root: Path, door: FakeVault, hook: FakeHook) -> int:
    mcp = McpClient(
        base_url=door.url,
        api_key="stub-key",
        tools=McpToolNames(read=TOOL_READ, write=TOOL_WRITE, append=TOOL_APPEND),
        timeout_seconds=5.0,
        verify_tls=False,
        retries=1,
        retry_base_delay_seconds=0.0,
        client_name="obsidian-tools lint-pass",
    )
    return run_pass(root=root, mcp=mcp, hook=DigestHook(hook.url, TOKEN, 5.0), digest_limit=7, now=NOW)


def _pass(tmp_path: Path, mount: dict[str, str], door: dict[str, str] | None = None, *, hook_status: int = 202):
    root = _mount(tmp_path / "brain", mount)
    vault = FakeVault(notes=dict(mount if door is None else door))
    hook = FakeHook(status=hook_status)
    with running_vault(vault), running_hook(hook):
        code = _run(root, vault, hook)
    return code, vault, hook


def test_a_pass_over_a_planted_vault(tmp_path: Path) -> None:
    """Rows 9, 10, 11 and 13 through the door, and every record written once."""
    code, door, hook = _pass(tmp_path, PLANTED)
    assert code == EXIT_OK

    # Reported only, never modified: the contradiction, the stale note, the dangling link, the raw
    # note, and the note whose fix admission withheld.
    for path in (CONTRADICTION, STALE, DANGLING, RAW, WITHHELD, BETA, ALPHA):
        assert door.written(path) == PLANTED[path], f"{path} was changed"
    assert all(path != RAW for _, path in door.calls), "the pass touched the raw layer"

    # The GUI note's empty dates were stamped, through a whole-note overwrite the validator admits.
    fixed = door.written(GUI_NOTE)
    assert fixed is not None
    assert fixed != PLANTED[GUI_NOTE]
    assert admit(GUI_NOTE, fixed).admitted
    note = parse_note(fixed)
    assert isinstance(note, ParsedNote)
    assert note.body == "Typed at the GUI. [[beta]]\n"
    assert (GUI_NOTE, True) in door.write_arguments

    # Every write to a curated path is a post-image the validator admits; nothing is deleted.
    for tool, path in door.calls:
        assert tool in (TOOL_READ, TOOL_WRITE, TOOL_APPEND)
        if tool == TOOL_WRITE and path.startswith(("10-areas/", "20-projects/")):
            assert admit(path, door.notes[path]).admitted

    report = door.written(REPORT)
    assert report is not None
    parsed = parse_note(report)
    assert isinstance(parsed, ParsedNote)
    values = parsed.frontmatter.as_dict()
    stamped = [values[key] for key in ("source", "authority", "trigger")]
    assert [v.text if v is not None and not isinstance(v, tuple) else v for v in stamped] == [
        "vault-worker",
        "agent",
        "schedule",
    ]
    for path, cls in (
        (CONTRADICTION, "trigger_authority_contradiction"),
        (STALE, "stale"),
        (DANGLING, "dangling_link"),
        (GUI_NOTE, "unstamped"),
        (GUI_NOTE, "confidence_empty"),
        (WITHHELD, "type_missing"),
    ):
        assert f"| `{path}` | {cls} |" in report, f"{cls} on {path} is not in the report"
    assert f"`{WITHHELD}`: withheld by admission (type_missing)" in report
    assert RAW not in report
    assert "## The tolerance line" in report

    audit = door.written(AUDIT)
    assert audit is not None
    assert GUI_NOTE in audit
    assert (AUDIT, False) in door.write_arguments  # the day's first pass creates it, never clobbering

    log = door.written("log.md")
    assert log is not None
    assert log.startswith(CLEAN["log.md"])
    added = log.removeprefix(CLEAN["log.md"])
    assert added.count("\n") == 1
    assert added.startswith("- 2026-09-22 | vault-worker | lint pass: ")

    ((headers, body),) = hook.posts
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert body["source"] == DIGEST_SOURCE
    message = str(body["message"])
    lines = message.split("\n")
    assert 1 < len(lines) <= 8
    assert lines[1].endswith(WITHHELD)  # a curated note failing admission ranks first


def test_row_14_a_note_changed_through_the_door_since_the_mount_read_is_not_overwritten(tmp_path: Path) -> None:
    """Violation injection: the door holds different bytes for the note than the mount did. Red if
    the fix lands over them — a silent clobber of a concurrent edit."""
    mount = {**CLEAN, ALPHA: text(fields("Alpha", created="2026/09/01"), "See [[beta]].\n")}
    concurrent = text(fields("Alpha", created="2026/09/01"), "See [[beta]]. Edited meanwhile.\n")
    code, door, _ = _pass(tmp_path, mount, {**mount, ALPHA: concurrent})

    assert code == EXIT_OK
    assert door.written(ALPHA) == concurrent
    assert (ALPHA, True) not in door.write_arguments
    report = door.written(REPORT)
    assert report is not None
    assert f"`{ALPHA}`: skipped, changed since read" in report
    assert door.written(AUDIT) is None  # nothing applied, nothing to audit


def test_the_control_for_row_14_the_same_note_unchanged_is_fixed(tmp_path: Path) -> None:
    mount = {**CLEAN, ALPHA: text(fields("Alpha", created="2026/09/01"), "See [[beta]].\n")}
    code, door, _ = _pass(tmp_path, mount)
    assert code == EXIT_OK
    assert door.written(ALPHA) == CLEAN[ALPHA]


def test_row_15_no_findings_means_no_push(tmp_path: Path) -> None:
    code, door, hook = _pass(tmp_path, CLEAN)
    assert code == EXIT_OK
    assert hook.posts == []
    assert door.written(REPORT) is not None
    assert door.written(AUDIT) is None


def test_row_15_a_refused_push_fails_the_pass_and_nothing_else_is_skipped(tmp_path: Path) -> None:
    code, door, hook = _pass(tmp_path, PLANTED, hook_status=500)
    assert code == EXIT_FAILED
    assert len(hook.posts) == 1
    assert door.written(REPORT) is not None
    log = door.written("log.md")
    assert log is not None
    assert log != CLEAN["log.md"]


def test_a_refused_fix_fails_the_pass_and_the_records_are_still_written(tmp_path: Path) -> None:
    mount = {**CLEAN, ALPHA: text(fields("Alpha", created="2026/09/01"), "See [[beta]].\n")}
    root = _mount(tmp_path / "brain", mount)
    vault = FakeVault(notes=dict(mount), refuse_paths={ALPHA})
    hook = FakeHook()
    with running_vault(vault), running_hook(hook):
        code = _run(root, vault, hook)
    assert code == EXIT_FAILED
    report = vault.written(REPORT)
    assert report is not None
    assert f"`{ALPHA}`: failed:" in report


def test_a_later_pass_appends_to_the_days_audit_note_rather_than_rewriting_it(tmp_path: Path) -> None:
    earlier = "---\ntype: note\n---\n# Normalisation audit\n\n## Pass earlier\n"
    mount = {**CLEAN, AUDIT: earlier, ALPHA: text(fields("Alpha", created="2026/09/01"), "See [[beta]].\n")}
    code, door, _ = _pass(tmp_path, mount)
    assert code == EXIT_OK
    audit = door.written(AUDIT)
    assert audit is not None
    assert audit.startswith(earlier)
    assert ALPHA in audit
    assert (AUDIT, False) not in door.write_arguments
    assert (AUDIT, True) not in door.write_arguments


def test_an_absent_log_is_not_created_as_a_fragment(tmp_path: Path) -> None:
    mount = {path: content for path, content in CLEAN.items() if path != "log.md"}
    code, door, _ = _pass(tmp_path, mount)
    assert code == EXIT_FAILED
    assert door.written("log.md") is None


def test_a_log_without_a_final_line_break_gets_exactly_one_before_the_new_line(tmp_path: Path) -> None:
    """The append tool adds the missing line break itself (measured against the pinned image, and
    modelled by the stub). Red if the pass adds its own as well — a blank line in the log."""
    mount = {**CLEAN, "log.md": "# log\n\n- 2026-09-01 | claude-code | bootstrapped"}
    code, door, _ = _pass(tmp_path, mount)
    assert code == EXIT_OK
    log = door.written("log.md")
    assert log is not None
    assert "bootstrapped\n- 2026-09-22 | vault-worker | " in log


def test_the_summary_line_carries_the_counts(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="obsidian_tools.lint_pass.shell"):
        _pass(tmp_path, {**PLANTED, "00-inbox/one.md": "x\n", "_ops/quarantine/q.md": "x\n"})
    (record,) = [r for r in caplog.records if getattr(r, "event", None) == "lint_pass_complete"]
    summary = record.__dict__
    assert summary["outcome"] == "ok"
    assert summary["inbox_depth"] == 2
    assert summary["quarantine_depth"] == 1
    assert summary["findings_trigger_authority_contradiction"] == 1
    assert summary["findings_type_missing"] == 1
    assert summary["unstamped"] == summary["findings_unstamped"] >= 1
    assert summary["fixes_applied"] >= 1
    assert summary["fixes_withheld_admission"] == 1
    assert summary["fixes_changed_since_read"] == 0
    assert summary["digest_items_sent"] == 7
    assert summary["notes_raw"] == 1
    assert summary["nonconforming"] >= 3
    assert "duration_seconds" in summary


def test_read_vault_reads_markdown_skips_dot_folders_and_links_and_dates_by_mtime(tmp_path: Path) -> None:
    root = _mount(
        tmp_path / "brain", {"a.md": "a\n", ".obsidian/app.md": "x\n", ".trash/old.md": "x\n", "img.png": "p"}
    )
    (root / "link.md").symlink_to(root / "a.md")
    files = {f.path: f for f in read_vault(root)}
    assert set(files) == {"a.md", "img.png"}
    assert files["a.md"].content == b"a\n"
    assert files["img.png"].content is None
    assert files["a.md"].modified == MODIFIED


# --- the seam's additions -------------------------------------------------------------------------


def test_append_note_appends_at_the_end_through_the_target_envelope(tmp_path: Path) -> None:
    vault = FakeVault(notes={"log.md": "a\n"})
    with running_vault(vault):
        mcp = McpClient(
            base_url=vault.url,
            api_key="k",
            tools=McpToolNames(read=TOOL_READ, write=TOOL_WRITE, append=TOOL_APPEND),
            timeout_seconds=5.0,
            verify_tls=False,
            retries=1,
            retry_base_delay_seconds=0.0,
        )
        mcp.append_note("log.md", "b\n")
    assert vault.written("log.md") == "a\nb\n"
    assert vault.calls == [(TOOL_APPEND, "log.md")]


def test_a_client_holding_no_delete_tool_sends_nothing_when_asked_to_delete() -> None:
    """The lint key is granted no delete tool (ADR-0004); asking for one fails before any request,
    rather than guessing a name the gateway would refuse."""
    vault = FakeVault(notes={"x.md": "x\n"})
    with running_vault(vault):
        mcp = McpClient(
            base_url=vault.url,
            api_key="k",
            tools=McpToolNames(read=TOOL_READ, write=TOOL_WRITE, append=TOOL_APPEND),
            timeout_seconds=5.0,
            verify_tls=False,
            retries=1,
            retry_base_delay_seconds=0.0,
        )
        with pytest.raises(McpFailedError, match="no delete tool"):
            mcp.delete_note("x.md")
    assert vault.calls == []
    assert vault.written("x.md") == "x\n"
