"""The `.obsidian/` baseline: captured once, then frozen.

See ADR-0028 and ppat/obsidian-tools#3 for the full reasoning; the
short version: `.obsidian/` exists only to get Mac/iOS clients configured quickly with a common
baseline, and a setting changed later at the cluster GUI must not reach devices.

**`.gitignore` cannot achieve that freeze — this was found empirically, not reasoned out in
advance.** Git only consults ignore rules for *untracked* paths. Once the baseline commit has
captured a file, a later change to that same file is still staged by a plain `git add -A`
regardless of what `.gitignore` says — the ignore entry is silently inert for exactly the files it
was added to protect. `git update-index --skip-worktree` is the actual mechanism.

Two independent facts matter here and must not be conflated:

1. **Has the baseline commit ever been taken?** Asked against `HEAD`'s tree — durable history
   fetched fresh from `origin` on every run (see vault_git/provisioning.py) — never against a
   marker file or index state living on the git-dir cache. That cache is itself lost-and-recovered;
   a marker file on the very volume that can vanish would desynchronize from what's actually in
   history, either re-triggering a capture that already happened or skipping one that hasn't.
2. **Are the skip-worktree bits currently set on the local index?** This genuinely is per-index,
   per-clone state that does not survive a lost/rebuilt cache even when the underlying commit does
   (a fresh `read-tree HEAD` populates the index with tracked paths, but skip-worktree is an index
   flag, not part of the tree object, so it comes back cleared). It is therefore reapplied
   unconditionally on every run to every baselined path found in `HEAD`'s tree — reapplying an
   already-set bit is a harmless no-op, which is what makes doing this unconditionally both correct
   and simple.

A third mechanism, `ensure_ignore_rule`, is a *separate* concern from both of the above and easy to
mistake for redundant with skip-worktree — it isn't. Skip-worktree only stops `git add -A` from
re-staging a *change* to a path already captured in the baseline commit. It does nothing for a path
under `.obsidian/` that was never captured — any file outside the allowlist below, forever — because
those are untracked, and untracked paths are exactly what ignore rules (not skip-worktree) govern.
Both mechanisms are required, and each covers the files the other cannot: ignore rules for what git
has never tracked, skip-worktree for what it already has.

**The capture itself is an allowlist, never a denylist — see `baseline_selector.py`.** That module
holds the actual list of what gets captured (and why) plus `select_baseline_paths`, the pure
function every safety rule is checked in exactly once. This module is the thin shell around it: walk
`.obsidian/` into `PathInfo` candidates, hand them to the selector, and turn what comes back into
git calls. It intentionally does no allowlist-shaped reasoning of its own — see `baseline_selector.py`'s
module docstring for why three prior patches each fixed the rule in one loop and missed another, and
why that means the walk-and-decide logic must never be fused back together.

**The capture is all-or-nothing: a walk that could not read part of `.obsidian/` takes no baseline at
all** (ppat/obsidian-tools#35). This follows directly from fact 1 above rather than being a separate
policy. Because "has the baseline been taken" is asked of `HEAD`'s tree, it goes true the instant
*anything* lands under `.obsidian/`, complete or not, and can never be un-taken — so the only moment
a partial capture can be stopped is before the commit exists. The sibling walker in
`local_replicator/device_baseline.py` reaches the opposite behaviour from the same principle (copy
what is reachable, withhold the completion marker so the next cycle tops it up) purely because it has
a marker of its own to withhold; deliberately having none here is what makes refusal the equivalent
move, not a stricter one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISDIR, S_ISLNK, S_ISREG

from obsidian_tools.logging_config import LOG_PATH_SAMPLE_LIMIT
from obsidian_tools.vault_git.baseline_selector import PathInfo, select_baseline_paths
from obsidian_tools.vault_git.runner import GitRunner

logger = logging.getLogger(__name__)

OBSIDIAN_DIR = ".obsidian"

_IGNORE_RULE_CONTENTS = """\
# Managed by obsidian-tools' git committer (obsidian_tools/vault_git/baseline.py) — do not edit.
#
# ppat/obsidian-vault's own tracked .gitignore already lists .obsidian/ wholesale, but this
# component cannot depend on that alone: the vault volume is mounted read-only, so if that tracked
# file were ever missing, renamed, or edited to drop the line, this committer has no way to create
# or repair one in the work tree, and no way to notice. This git-dir-local exclude file is the copy
# of the rule this component actually controls. It stops `git add -A` from ever tracking a NEW path
# under .obsidian/ (anything outside the baseline allowlist below, forever). It does NOT freeze a
# path the baseline commit already captured — git only consults ignore rules for untracked paths —
# so it does not substitute for the `update-index --skip-worktree` bits this module also sets; the
# two mechanisms cover disjoint sets of files.
#
# No trailing slash, deliberately: a trailing slash only matches a directory, so it is inert against
# `.obsidian` if the directory is ever replaced by a symlink after a baseline already exists (git
# treats a symlink as a file, not a directory, however it names an actual directory) — `git add -A`
# would then stage the symlink itself as a new mode-120000 entry, publishing its target path as a
# blob (ppat/obsidian-tools#22). Dropping the slash matches the name `.obsidian` regardless of what
# kind of entry it currently is, so it excludes the ordinary directory case exactly as before *and*
# a symlink standing in for it — verified both ways (`test_ignore_rule_still_excludes_the_directory`,
# `test_ignore_rule_excludes_a_symlink_replacing_the_directory`).
.obsidian
"""


def ensure_ignore_rule(git_dir: Path) -> None:
    """Idempotent: (re)writes the git-dir-local exclude file with fixed, deterministic contents."""
    exclude_path = git_dir / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    exclude_path.write_text(_IGNORE_RULE_CONTENTS)


@dataclass(frozen=True, slots=True)
class _ObsidianWalk:
    """Everything one walk of `.obsidian/` observed — including what it could *not* observe.

    `unreadable` is the field that makes this a dataclass rather than a bare list of pathspecs. A
    walk that skipped something is not a shorter answer to the same question, it is an answer to a
    different one, and `ensure_obsidian_baseline` must be able to tell the two apart — see its own
    refusal branch for why nothing partial may ever be captured here.
    """

    # `.obsidian/`-prefixed, work-tree-relative — ready to hand to `git add` as pathspecs.
    selected: list[str]
    # `.obsidian/`-relative (the form `baseline_selector.py`'s allowlist itself is written in, which
    # is what makes this list directly comparable to it), sorted.
    unselected: list[str]
    # Real filesystem paths, in walk order — an operator reading this is going to go and look at the
    # thing, so this is the one list that names where it actually is rather than what it is called.
    unreadable: list[str]


def _iter_obsidian_candidates(directory: Path, prefix: str = "") -> tuple[list[PathInfo], list[str]]:
    """Walk `.obsidian/` once into `(candidates, unreadable)`, relative to `.obsidian/` itself.

    Mirrors `os.walk(..., followlinks=False)`: never descends into a symlinked directory, so no
    candidate for anything beneath one is ever produced in the first place. That is the actual
    mechanism that keeps a symlinked plugin directory (the standard local plugin-development layout,
    `.obsidian/plugins/my-plugin -> ~/dev/my-plugin`) from ever reaching `git add` as a pathspec
    "beyond a symbolic link" — see `test_symlinked_plugin_directory_does_not_wedge_the_committer`.
    `select_baseline_paths` also rejects any candidate with `is_symlink` set, as a second,
    independent check — a mistake here should not be the only thing standing between a symlink and
    permanent history.

    **Anything this walk could not read is reported, never swallowed** (ppat/obsidian-tools#35). An
    earlier revision skipped an unreadable directory and carried on, reasoning that a transient NFS
    glitch is routine on this volume and "the next scheduled run tries again". The first half of that
    is true — `/vault/brain` is a soft-mounted Longhorn NFS export whose documented normal failure
    mode is I/O returning an error rather than hanging (ADR-0033) — and the second half
    is false *here* in a way it is not for the sibling walker in
    `local_replicator/device_baseline.py`: `ensure_obsidian_baseline` only ever runs this walk until
    something lands under `.obsidian/` in history, so a capture that skipped a directory is frozen by
    the very guard that stops the walk repeating. Swallowing the error does not degrade to "retry
    next cycle"; it degrades to "permanently correct-looking and wrong".

    **Each entry is stat'ed explicitly, once, rather than asked three predicates.** `Path.is_file()`
    and `Path.is_dir()` delegate to `os.path.isfile`/`isdir`, and `Path.is_symlink()` to
    `os.path.islink`; all three catch `OSError` internally and return `False`. An entry that cannot
    be stat'ed — a `readdir` that succeeds while `stat` fails is exactly what an `ESTALE`/`EACCES` on
    an individual entry looks like, and is directly reproducible with a directory carrying read but
    not execute permission — therefore reaches the selector as `is_file=False`, indistinguishable
    from a socket or a directory, and is dropped with no exception raised anywhere. So the `except
    OSError` those three calls used to sit inside was dead code, and the failure it was supposed to
    catch was the *silent* one. One `lstat()`, which does raise, is both the fix and one syscall
    instead of three: `is_symlink`/`is_dir` come straight off its mode. `is_file` for a symlink still
    follows the link (`PathInfo.is_file`'s documented meaning), and deliberately keeps using the
    swallowing predicate — a dangling symlink is a legible state of the vault, not a read failure,
    and must not be able to refuse the baseline.

    Deliberately an explicit stack, not recursion: the pre-refactor `rglob` this module replaced was
    iterative, and a recursive descent here raises `RecursionError` at a depth (~992 levels, well
    under `PATH_MAX`) neither this function's own `OSError` handling nor the caller's
    `_STAGING_FAILURES` tuple catches — `RecursionError` is not an `OSError`, and it is neither a
    `GitCommandError` nor a `RetryExhaustedError` — so it used to surface as an uncaught traceback
    with the same permanent-wedge shape as the symlinked-root case above (ppat/obsidian-tools#22).
    """
    candidates: list[PathInfo] = []
    unreadable: list[str] = []
    stack: list[tuple[Path, str]] = [(directory, prefix)]

    while stack:
        current_directory, current_prefix = stack.pop()
        try:
            entries = sorted(current_directory.iterdir())
        except OSError:
            # Everything beneath this directory is lost with it — nothing under an unenumerated
            # directory ever reaches the stack — which is why one entry here can withhold the whole
            # baseline.
            unreadable.append(str(current_directory))
            continue

        for entry in entries:
            relative_path = f"{current_prefix}{entry.name}"
            try:
                entry_stat = entry.lstat()
            except OSError:
                unreadable.append(str(entry))
                continue

            is_symlink = S_ISLNK(entry_stat.st_mode)
            is_dir = S_ISDIR(entry_stat.st_mode)
            is_file = entry.is_file() if is_symlink else S_ISREG(entry_stat.st_mode)

            # `is_dir` now comes from `lstat`, so it is already False for a symlink to a directory
            # and the second half of this condition can no longer fire. It stays anyway: the
            # non-descent guarantee above is load-bearing enough that it should not rest on a reader
            # tracing which stat call produced the flag (ppat/obsidian-tools#22).
            if is_dir and not is_symlink:
                stack.append((entry, f"{relative_path}/"))
                continue

            candidates.append(PathInfo(relative_path=relative_path, is_file=is_file, is_symlink=is_symlink))

    return candidates, unreadable


def _walk_obsidian(work_tree: Path) -> _ObsidianWalk:
    """One walk of `.obsidian/`, split into what the allowlist takes, what it leaves, and what could
    not be read — the walk-and-decide split described in the module docstring.

    `unselected` is computed as the set difference against `select_baseline_paths`' own output, never
    by asking "what would the allowlist have taken" a second time here. That is the same rule the
    selector's docstring states for the capture itself, and for the same reason: a second copy of the
    allowlist logic is a copy that can fall out of sync. It follows that `unselected` mixes entries
    the allowlist simply does not name with entries excluded for safety (a symlink, a directory that
    is not a plain file) — telling those apart would need exactly the re-derivation this avoids.

    Every path in `selected` existed at enumeration time, so it was a valid pathspec then — but this
    process does not hold any lock on the (read-only, NFS-backed) work tree between here and the
    `git add --force --` call that actually consumes these as pathspecs, so a path can still vanish
    in that window. That's a real, if narrow, TOCTOU gap — not something this list can rule out by
    construction — and it self-heals on the next run either way, since `git add -A`/`stage_all`
    reconciles the whole index against the current work tree regardless of what a prior cycle saw.
    Each pathspec also carries the `:(literal)` pathspec magic prefix (applied where the paths are
    actually passed to git, not here) specifically so a filename containing a glob metacharacter
    (`*`, `[`, `?`) is matched as itself rather than re-interpreted as a pattern.
    """
    obsidian_dir = work_tree / OBSIDIAN_DIR
    candidates, unreadable = _iter_obsidian_candidates(obsidian_dir)
    selected = select_baseline_paths(candidates)
    selected_paths = set(selected)
    return _ObsidianWalk(
        selected=[f"{OBSIDIAN_DIR}/{path}" for path in selected],
        unselected=sorted(
            candidate.relative_path for candidate in candidates if candidate.relative_path not in selected_paths
        ),
        unreadable=unreadable,
    )


def _walk_log_fields(walk: _ObsidianWalk) -> dict[str, object]:
    """The complete account of one walk, as structured log fields — used by *every* line this module
    emits about a walk, so no caller can report a subset by omission.

    Built in one place deliberately. The first revision of the diagnostic below took the whole
    `_ObsidianWalk` and logged only `unselected` from it, which on a baselined vault (the reapply
    branch, the only branch that ever runs there) made an unreadable subtree read exactly like a
    healthy one — the same "complete-looking answer over dropped data" shape this module's refusal
    exists to prevent, reintroduced in the mechanism meant to reveal it. A caller that cannot choose
    which fields to include cannot make that mistake again.

    Both of this module's samples are capped here, and this is the only place either cap is applied;
    the limit itself is one number shared with every other path-list field (`logging_config.py`).
    """
    return {
        "unselected_count": len(walk.unselected),
        "unselected_paths": walk.unselected[:LOG_PATH_SAMPLE_LIMIT],
        "unreadable_count": len(walk.unreadable),
        "unreadable_paths": walk.unreadable[:LOG_PATH_SAMPLE_LIMIT],
    }


def _log_walk_observations(walk: _ObsidianWalk) -> None:
    """Report what the walk enumerated and the allowlist did not take — the direction the allowlist
    has never been validated in — together with anything the walk could not read at all.

    **`warning` when the walk was incomplete, `info` otherwise**, with the same event and the same
    fields either way. The level tracks the data, never which branch called this: on a vault that
    already has a baseline the capture branch never runs again, so `baseline_refused_incomplete_walk`
    can never fire and this line is the *only* signal that will ever exist for an unreadable
    `.obsidian/` — `git add -A` never descends into `.obsidian` either, since the git-dir exclude
    rule prunes it, so nothing downstream reports it as a vault read failure. An operator scanning
    for something wrong must not have to read every `info` line to find that.

    `baseline_selector.py`'s allowlist was arrived at by reasoning about what a device baseline
    needs, never by inspecting what is actually on the PVC. That validates it one way only: a file
    the allowlist names and the vault lacks would have been noticed, but a file that *is* there,
    *does* matter, and nobody thought of is dropped with no error and no warning. This line is the
    other direction, answerable from the committer's own logs with no cluster access.

    Emitted on every run rather than only when a capture is attempted, and that is the whole point:
    the capture branch runs only until a baseline exists, so for any vault that already has one —
    every deployed vault — a diagnostic gated on it would never emit again, and could never answer
    the question for the deployment that has it. Running it unconditionally also means a plugin
    installed a year from now shows up in the next run's logs by itself. The cost is one line and a
    few dozen `stat`s per 15-minute run, against a run that already walks the entire vault through
    `git add -A`; `info` rather than `warning` because an unselected path is the ordinary, expected
    state of most of `.obsidian/` (`workspace.json` is on this list every single run, correctly) —
    an unselected path on its own is the ordinary, expected state of most of `.obsidian/`
    (`workspace.json` is on this list every single run, correctly) — that is a question an operator
    comes to the logs to ask, not an event that should interrupt one.
    """
    if walk.unreadable:
        logger.warning(
            "part of .obsidian/ could not be read this cycle, so this enumeration is incomplete and the baseline "
            "may be missing whatever sits under the unreadable paths",
            extra={"event": "baseline_unselected_paths", **_walk_log_fields(walk)},
        )
        return

    logger.info(
        "enumerated .obsidian/ entries the baseline allowlist did not select",
        extra={"event": "baseline_unselected_paths", **_walk_log_fields(walk)},
    )


def ensure_obsidian_baseline(runner: GitRunner, work_tree: Path) -> bool:
    """Idempotent baseline step. Returns True if this call staged the (one-time) baseline capture."""
    obsidian_path = work_tree / OBSIDIAN_DIR

    if runner.rev_parse_or_none("HEAD") is not None and runner.path_exists_at("HEAD", OBSIDIAN_DIR):
        for path in runner.list_tree_paths("HEAD", OBSIDIAN_DIR):
            runner.run(["update-index", "--skip-worktree", "--", path])
        # Diagnostic only, and deliberately after the reapply loop above: the freeze is this
        # branch's actual job, and nothing added for observability should be able to run before it.
        # Guarded exactly as the capture branch below guards its own walk — without the symlink
        # check, `.obsidian -> /somewhere/else` would make this enumerate an arbitrary external
        # directory and print its contents into the committer's logs (ppat/obsidian-tools#22).
        if not obsidian_path.is_symlink() and obsidian_path.is_dir():
            _log_walk_observations(_walk_obsidian(work_tree))
        return False

    if obsidian_path.is_symlink():
        # Checked before `is_dir()` below, and separately from it, because `is_dir()` follows
        # symlinks and would otherwise read a symlinked `.obsidian` as "present" — this is the one
        # place the *root* itself is guarded. Every candidate downstream (the walker, the selector)
        # describes an entry *inside* `.obsidian/`; nothing ever produces a `PathInfo` for the root,
        # so neither the walker's non-descent into symlinked directories nor the selector's own
        # `is_symlink` rule ever gets a chance to apply to it. Without this check, `_walk_obsidian`
        # would build pathspecs like `.obsidian/app.json` that walk *through* the symlink, and
        # `git add --force` fails outright on those (`fatal: pathspec '...' is beyond a symbolic
        # link`) — which then wedges every future run permanently, since the baseline is only ever
        # skipped once HEAD already carries `.obsidian/`, which this failure prevents from ever
        # happening (ppat/obsidian-tools#22).
        logger.info(
            "obsidian baseline not taken yet, and .obsidian/ is a symlink rather than a real directory; "
            "skipping this cycle",
            extra={"event": "baseline_skip_symlinked_obsidian_dir"},
        )
        return False

    if not obsidian_path.is_dir():
        # Nothing to baseline yet — e.g. the committer's very first run, before headless Obsidian
        # has created .obsidian/ on the volume at all. Not an error: the next run tries again.
        logger.info(
            "obsidian baseline not taken yet, and .obsidian/ is absent from the work tree; skipping this cycle",
            extra={"event": "baseline_skip_no_obsidian_dir"},
        )
        return False

    walk = _walk_obsidian(work_tree)
    _log_walk_observations(walk)

    if walk.unreadable:
        # **The capture is all-or-nothing** (ppat/obsidian-tools#35). Anything staged here becomes a
        # commit, and the guard at the top of this function goes true the instant *anything* lands
        # under `.obsidian/` in history — so a capture taken while part of the walk was unreadable is
        # not "most of the baseline, topped up next cycle", it is the permanent baseline, missing
        # whatever happened to be unreachable during one 15-minute tick, and looking correct.
        #
        # `local_replicator/device_baseline.py` faced the same walk and chose the opposite trade —
        # copy what is reachable, withhold the completion marker so the next cycle tops it up. That
        # shape needs a marker to withhold, and this module deliberately has none: its completion
        # signal *is* git history (see the module docstring on why a marker file on the git-dir cache
        # volume was rejected), and history cannot be un-taken. Refusing the whole capture is the
        # same property — nothing partial is ever treated as done — expressed the only way a
        # history-shaped marker allows.
        #
        # Refusing on *any* unreadable path, including one under a subtree that could not have held
        # anything allowlisted, is deliberate: deciding otherwise means re-deriving the allowlist's
        # depth rules inside this walker, which is precisely what `baseline_selector.py`'s docstring
        # forbids. The two errors are not symmetric — over-refusal is loud, names the path, repeats
        # every run and destroys nothing, while under-capture is silent and permanent.
        #
        # `warning`, not `error` and not the `info` the "not there yet" skips above use: nothing has
        # failed (a transient read error is this volume's documented normal condition —
        # ADR-0033 — and the next run retries by itself), but unlike those skips this
        # one can also be a persistent fault that no run will ever clear on its own, and the only
        # thing that would ever tell a human so is this line.
        logger.warning(
            "refusing to capture the .obsidian/ baseline: part of .obsidian/ could not be read this cycle, so the "
            "capture would be silently incomplete and permanent; nothing staged, the next run retries the whole walk",
            extra={
                "event": "baseline_refused_incomplete_walk",
                **_walk_log_fields(walk),
                "readable_selected_count": len(walk.selected),
            },
        )
        return False

    if not walk.selected:
        # .obsidian/ exists but nothing on the allowlist has landed in it yet (e.g. plugins haven't
        # finished seeding). Not an error: the next run tries again once something allowlisted shows up.
        logger.info(
            "obsidian baseline not taken yet, and no allowlisted .obsidian/ paths are present; skipping this cycle",
            extra={"event": "baseline_skip_no_allowlisted_paths"},
        )
        return False

    # `:(literal)` pathspec magic: these paths came from a real directory listing, not a human, but
    # a filename containing a glob metacharacter (`*`, `[a-z]`, `?`) would otherwise be
    # re-interpreted by git as a pattern rather than matched as itself — `:(literal)` pins each
    # pathspec to exactly the path it names.
    literal_pathspecs = [f":(literal){path}" for path in walk.selected]
    runner.run(["add", "--force", "--", *literal_pathspecs], retry=True)
    staged_paths = runner.staged_paths(f"{OBSIDIAN_DIR}/")
    for path in staged_paths:
        runner.run(["update-index", "--skip-worktree", "--", path])

    logger.info(
        "captured .obsidian/ baseline",
        extra={"event": "baseline_captured", "file_count": len(staged_paths)},
    )
    return True
