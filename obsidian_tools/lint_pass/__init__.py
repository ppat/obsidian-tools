"""The lint pass (unit A5, ot#83) and the S2 tolerance line it carries (unit A6, ot#84).

The scheduled whole-vault pass: conformance and hygiene checks, additive-only frontmatter
normalisation, the report, the audit trail, and the review digest (ADR-0018). It is one of exactly
three processes that mount the vault volume, and it mounts it read-only (ADR-0001); every write it
makes goes through the ingestor door, like any other writer's.

| Module | Holds | Pure |
| --- | --- | --- |
| `line.py` | The tolerance line: every class of badness and its tier | yes |
| `checks.py` | The zones, and the reported checks | yes |
| `normalise.py` | The fixed tier: frontmatter normalisation that never overwrites a value | yes |
| `decide.py` | One snapshot in; findings, admitted fix plans and the ranked digest out | yes |
| `records.py` | The text of the report, the audit trail, the log line and the digest | yes |
| `shell.py` | The mount read, the door writes with their hash re-check, the digest push, the summary line | no |

What it never does: relocate, rename, delete or quarantine a note (a relocation is a rename, and a
rename is structural work for the batch stream — schema file §5, §7.2; ADR-0013); edit a body;
judge meaning. A curated note failing admission is reported at the top rank, not moved.
"""
