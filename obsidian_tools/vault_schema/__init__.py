"""The vault's schema file, executable: what `CLAUDE.md` at the vault root declares, as code.

In the way `vault_git/baseline_selector.py` is the executable copy of ADR-0028's allowlist, this
package is the executable copy of the schema file's frontmatter contract. It decides nothing about
admission or lint policy — those are its callers' (`admission/`, the lint pass). It answers only
"what does the schema file say", so that every caller answers that question the same way.

| Module | Holds | Schema file section |
| --- | --- | --- |
| `frontmatter.py` | The frontmatter dialect: a strict YAML subset, parsed, and emitted canonically | §3 |
| `fields.py` | Every declared field's type, vocabulary and requiredness, in canonical key order | §3 table |
| `dates.py` | Which date spellings are dates at all | §3, §9 |
| `slug.py` | `slug(title)` — the filename rule | §7.2 |
| `zones.py` | The folder map's prefixes that carry rules, and the §6 staleness dials | §1, §2, §6, §7.2 |

Where the schema file and an accepted decision record disagree, the record governs, and this
package follows the record (ADR-0010 on finance `authority:`; ADR-0019 removed the entity kind
tag, so nothing here knows of one).

Pure throughout: no I/O, no clock. The runtime carries no YAML dependency — see `frontmatter.py`.
"""
