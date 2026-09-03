# 0046. Git through the real CLI, behind one seam

**Status:** Accepted

## Context

The components that turn the vault into history and replicate it are git plumbing at their core,
and there are two ways to drive git from Python: a library binding, or subprocess calls to the
real CLI. The case for a binding is real and is stated here so the decision can be re-argued
honestly: three bug classes actually suffered in this codebase are artifacts of the CLI —
`core.quotePath` C-quoting non-ASCII paths in command output (fed back to git, it wedges the
consuming operation); pathspec re-expansion of validated paths
([ADR-0043](./0043-git-pathspecs-literal.md)); and stale `*.lock` files after a SIGKILL, which
wedge every later run. A binding returns paths as values, takes paths as paths, and manages locks
internally.

## Decision

The real git CLI, invoked through one seam — `GitRunner` — that every git call crosses.

The deciding fact: this system is one node in a distributed git system. Every interop partner —
GitHub, the Mac's pull-only clone, the operator's own shell — runs real git, so a re-implementation
(libgit2) at one node creates a compatibility surface where none existed. A binding would also
quietly defeat the project's testing rule: the suite and production would agree with each other on
libgit2's behaviour while the systems that must interoperate run something else — internally
consistent, and not testing the thing that has to work. Two smaller reasons: a failing git command
can be copy-pasted into a shell by an operator, which a failing library call cannot; and `pygit2`
carries a native libgit2 build — a C dependency and multi-arch build complexity in an image that
otherwise has none.

The accepted cost is bounded and enforced centrally at the seam, never remembered per call site:
`-z`/NUL parsing wherever paths come back; `:(literal)` on every pathspec built from a real path
([ADR-0043](./0043-git-pathspecs-literal.md)); `errors="surrogateescape"` for filenames that are
not valid UTF-8; and explicit stale-lock clearing. The owner's trade in one line: the class of
bugs the CLI introduces is more predictable than the alternative.

## Alternatives considered

- **`pygit2`/libgit2** — removes all three suffered bug classes, at the compatibility and
  dependency price above.
- **`GitPython`** — itself a subprocess wrapper around the CLI: adds a dependency while removing
  none of the three bug classes. Recorded because it is the obvious first search result for
  "Python git library".

## Consequences

- The seam keeps the decision cheap to reverse: every git invocation goes through `GitRunner`, so
  swapping to a binding is a contained change, not a rewrite.
- A git call made around the seam forfeits the four disciplines with it.
