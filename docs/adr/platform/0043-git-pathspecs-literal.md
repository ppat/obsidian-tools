# 0043. Paths handed to git are always literal-marked pathspecs

**Status:** Accepted

## Context

Every component that touches git does so through one shared runner, handing it filesystem paths
constantly — staging enumerated files, diffing specific paths, resetting them. At those argument
positions git does not take *paths*; it takes **pathspecs**, a pattern language. A filename
containing glob metacharacters therefore makes the argument match files other than itself.

That turns a common, safe-looking shape into a bypass: enumerate the filesystem applying safety
rules — is this a regular file, is it a symlink, is its name allowlisted — then pass the surviving
strings to `git add`. The validation does not travel with the value. Observed against real git
[measured 2026-07-31]: an allowlisted regular file named `custom[1].css` produced a pathspec that
also matched an unrelated `custom1.css` **symlink**, staging exactly what the enumeration's symlink
exclusion existed to keep out — not through a hole in the check, but through the check being
applied to the wrong thing. The instance was the settings baseline's allowlisted forced add
([ADR-0028](../replication/0028-settings-baseline-seed.md)), where what the allowlist withholds
includes credential-bearing files.

## Decision

Every pathspec built from a real path is wrapped in git's `:(literal)` magic — everywhere, enforced
at the shared runner that every git invocation crosses, never remembered per call site. Two
supporting facts make the rule complete:

- The runner strips git's pathspec-magic environment variables from every invocation: an inherited
  `GIT_LITERAL_PATHSPECS=1` would make git read the `:(literal)` prefix itself as part of a literal
  filename, silently breaking every pathspec this rule produces.
- The prefix also resolves an unrelated ambiguity for free: a vault file literally named like a
  revision (`HEAD`) stays a path, never a ref.

## Alternatives considered

- **`--pathspec-file-nul`** — the same guarantee, workable; costlier here, since every call site
  would grow a temp-file or stdin protocol where the prefix is one function applied to an
  already-assembled argument list.
- **Call-site vigilance** (escape or quote where it seems to matter) — rejected on the mechanism
  itself: the defect exists precisely because upstream validation does not travel with the value,
  and a discipline that must be remembered at each site is the shape that already failed once.

## Consequences

- **The control is provable only by the right injection.** A test using a `*`-named file passes
  even with the fix removed — git glob-expands a pathspec only when no file matches it literally,
  so the star matches its own file and stops — while a bracket pattern matches its literal file
  *and* independently fnmatches siblings. The proving test therefore uses a bracket-named file, and
  was confirmed to go red with the fix reverted.
- A component inherits the rule by going through the shared runner; a git call made around the
  runner forfeits it.
- The observed bypass is the third link in ADR-0028's defect chain (a denylist would have committed
  credentials; a directory-prefixed allowlist is a denylist in disguise; an unmarked pathspec voids
  the allowlist's checks at the consuming step). The rule lives here; that instance's stakes live
  there.
