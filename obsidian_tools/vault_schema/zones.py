"""The folder map's prefixes that carry rules (schema file §1, §2, §6, §7.2; DESIGN.md's ownership table).

Plain vault-relative prefixes compared with `str.startswith`, never globs — a vault path may
legally contain `*`, `[` or `?` (ADR-0043). The trailing slash is what keeps a sibling folder such
as `10-areas-old/` outside the rule.
"""

from __future__ import annotations

CURATED_PREFIXES = ("10-areas/", "20-projects/")
"""Curated space: mutable only through the admission validator (ADR-0007)."""

FINANCE_PREFIX = "10-areas/finance/"
"""The strictest overlay: the evidence-keyed hard block (ADR-0010)."""

RAW_PREFIX = "05-raw/"
"""The raw layer: write-once and exempt from validation (ADR-0015)."""

AGENT_ZONE_PREFIXES = ("00-inbox/", "40-journal/", "_ops/agent/")
"""The agent zone's folders (schema file §1). `log.md`, the zone's fourth member, is a fixed root
file and is in `FIXED_ROOT_FILES` instead."""

INBOX_PREFIX = "00-inbox/"
QUARANTINE_PREFIX = "_ops/quarantine/"

FIXED_ROOT_FILES = frozenset({"CLAUDE.md", "AGENTS.md", "README.md", "00-index.md", "log.md", "TODO.md"})
"""The root files §2 names. Their addresses are the contract (§7.2)."""

FIXED_ADDRESS_PREFIXES = ("_ops/", "_templates/")
"""Folders whose filenames are exempt from the slug rule, with the root files above (§7.2)."""

STALENESS_DIALS: tuple[tuple[str, int], ...] = (
    ("10-areas/finance/", 30),
    ("10-areas/travel/", 90),
    ("10-areas/dining/", 90),
    ("10-areas/tech/", 180),
    ("10-areas/homelab/", 365),
)
"""§6: the `reviewed:` age, in days, past which a note in each area is stale. Dining takes travel's
dial (ADR-0019). An area with no row — `20-projects/`, a new folder under `10-areas/` — has no dial,
and adding one is the owner's overlay assignment, not a default."""
