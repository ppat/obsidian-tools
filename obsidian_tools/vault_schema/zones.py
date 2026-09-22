"""The folder map's prefixes that carry rules (schema file §1, §2; DESIGN.md's ownership table).

Plain vault-relative prefixes compared with `str.startswith`, never globs — a vault path may
legally contain `*`, `[` or `?` (ADR-0043). The trailing slash is what keeps a sibling folder such
as `10-areas-old/` outside the rule.
"""

from __future__ import annotations

CURATED_PREFIXES = ("10-areas/", "20-projects/")
"""Curated space: mutable only through the admission validator (ADR-0007)."""

FINANCE_PREFIX = "10-areas/finance/"
"""The strictest overlay: the evidence-keyed hard block (ADR-0010)."""
