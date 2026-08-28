# 0013. One subject, one page, at one computed name: `slug(title)` plus the page-exists check

**Status:** Accepted ·
**Serves:** [S2](../../../USE_CASES.md#s2--sound), [S3](../../../USE_CASES.md#s3--placed)

## Context

Multiple agents naming the same subject must not produce multiple files. Filenames also replicate
to case-insensitive filesystems (macOS, iOS), where `Backup.md` and `backup.md` in one vault is not
untidy but *unrepresentable* — and git on macOS sets `core.ignoreCase=true`, so fixing casing later
does not even register as a rename without force.

## Decision

`title:` is free human-facing text; the filename stem is derived mechanically: NFKC-normalise,
collapse whitespace, lowercase, spaces to hyphens, delete anything outside `a-z0-9-`, collapse and
trim hyphen runs. Deletion — not substitution — for stripped characters (`Node.js` → `nodejs`,
`C++` → `c`), matching how conventional slugs read. Lowercase is load-bearing, not style. The rule
closes exactly one failure: two agents, one title, two files. It does nothing about one subject
under two legitimate titles (singular/plural, acronym/expansion, synonyms) — that residue is owned
by the page-exists check (an index lookup before creating: exact title, singular/plural,
acronym/expansion, likely aliases) plus an `aliases:` entry on the surviving page. In-app wikilink
resolution being case-insensitive is reader-side convenience in one client, not a property the
filesystem or git shares.

## Alternatives considered

- Free filenames with link-time resolution — leaves the duplicate-page failure to agent judgment
  alone.
- Substitution slugs (`node-js`) — trailing-separator garbage; deletion matches convention.
- A mechanical dedup pipeline now — deliberately not built
  ([ADR-0038](./0038-search-and-dedup-not-built.md)); a two-stage embedding-plus-LLM technique is
  named there for the day naming discipline proves insufficient.

## Consequences

A slug-conformance lint check can only catch a stem that no longer matches its title — never two
working titles for one subject; the two defences are not redundant and must not be collapsed. A
rename, when discipline fails anyway, is structural work and goes through the batch stream as a
patch, never performed by the lint pass.
