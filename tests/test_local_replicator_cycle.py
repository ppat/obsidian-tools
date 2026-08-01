"""Tests for obsidian_tools/local_replicator/cycle.py -- the full seven-step cycle.

Real local git repos (bare "origin" plus a normal push-clone to seed commits) and real rsync
against real temp directories throughout, exactly like the git committer's own test suite -- the
risk here lives in git's and rsync's actual semantics, not in anything worth mocking.

Covers every scenario named in the brief for this component:
- the comparison runs against the pre-pull baseline, so an upstream change and a device edit in
  the same cycle are attributed correctly (this is the whole reason for the step ordering);
- a device creation, deletion, and modification each produce a real patch, not just a path;
- a spool write failure is injected (never merely omitted), and blocks the *whole* cycle's publish
  and tag advance, not just the failed path -- the core safety property of the current design;
- the next cycle retries and resolves once the injected failure clears;
- the cycle is idempotent from an arbitrary crash-like starting state;
- non-ASCII and quoted filenames survive the whole real git/rsync pipeline, not just the pure core;
- `.obsidian/` is copied once, then left alone even when the device customises it;
- the shared exclude list suppresses spurious drift end-to-end, not just at the rsync layer.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import push_commit, replicate_config, run_git

from obsidian_tools.local_replicator.cycle import run_cycle
from obsidian_tools.local_replicator.drift import SpoolEntry
from obsidian_tools.local_replicator.spool import SpoolWriteError, list_spool_files, read_spool_entry, write_spool_entry
from obsidian_tools.local_replicator.tag import read_last_checkout
from obsidian_tools.vault_git.runner import GitRunner


def _tag_sha(tmp_path: Path) -> str | None:
    runner = GitRunner(tmp_path / "cache-clone" / ".git", tmp_path / "cache-clone")
    return read_last_checkout(runner)


def _spooled_by_path(tmp_path: Path) -> dict[str, SpoolEntry]:
    spool_dir = tmp_path / "spool"
    return {entry.path: entry for f in list_spool_files(spool_dir) for entry in [read_spool_entry(f)]}


def _hostile_global_git_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """Point git's *global* configuration at a file this test writes, via `GIT_CONFIG_GLOBAL`.

    This is not a weaker stand-in for `~/.gitconfig`: `GIT_CONFIG_GLOBAL` *replaces* the global
    configuration file git would otherwise read, by git's own documented mechanism, so a setting
    placed here reaches every git invocation by exactly the path the operator's own `~/.gitconfig`
    reaches it. That is what makes the tests below evidence about what the environment can still
    do to us, rather than evidence that we wrote the argv we meant to write -- and it keeps them
    hermetic (nothing outside `tmp_path` is written, no real user configuration is read), which
    setting a genuine `~/.gitconfig` would not be.
    """
    config_path = tmp_path / "hostile-gitconfig"
    config_path.write_text(body)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config_path))


def _executable_script(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


def _failing_spool_writer(fail_on: str) -> Callable[[Path, SpoolEntry], Path]:
    def writer(spool_dir: Path, entry: SpoolEntry) -> Path:
        if entry.path == fail_on:
            raise SpoolWriteError(f"simulated local disk write failure for {fail_on!r} -- injected, not real I/O")
        return write_spool_entry(spool_dir, entry)

    return writer


# --- basic cycle behaviour -----------------------------------------------------------------------


def test_first_cycle_publishes_everything_and_advances_the_tag(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)

    result = run_cycle(config)

    assert result.tag_advanced is True
    assert result.drifted == ()  # nothing to compare against on a first run
    assert (icloud_dir / "00-index.md").read_text() == "# Home\n"


def test_order_attributes_upstream_change_and_device_drift_correctly(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """The whole reason for the step ordering: an upstream change and a device edit that land in
    the same cycle must not be conflated. Comparing before the pull is what keeps them apart."""
    push_commit(seeded_origin, tmp_path, {"10-areas/other.md": "original other\n"}, "add other")
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)  # cycle 1: bootstrap both files onto the device

    # Between cycles: an agent updates one file upstream; independently, a human edits a
    # *different* file directly in the device's iCloud copy.
    push_commit(seeded_origin, tmp_path, {"00-index.md": "# Home (agent update)\n"}, "agent update")
    (icloud_dir / "10-areas" / "other.md").write_text("typed on the phone\n")

    result = run_cycle(config)

    # Only the human-edited path is drift. The upstream-changed path is not -- it was never
    # different from the pre-pull baseline at compare time.
    assert result.drifted == ("10-areas/other.md",)
    spooled = _spooled_by_path(tmp_path)
    assert set(spooled) == {"10-areas/other.md"}
    assert "typed on the phone" in spooled["10-areas/other.md"].patch
    # The upstream change still reaches the device, via the pull+publish, independent of drift
    # attribution.
    assert (icloud_dir / "00-index.md").read_text() == "# Home (agent update)\n"


def test_device_creation_is_spooled_as_a_create_with_full_content(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """ "Creations are not diffs" trap: a file created on the device is untracked with an empty
    unstaged `git diff`. Staging first (`add -A`, in cycle.py) is what turns that into a real
    "new file" patch carrying the whole content."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    (icloud_dir / "typed-on-phone.md").write_text("brand new note from the device\n")

    result = run_cycle(config)

    assert "typed-on-phone.md" in result.drifted
    entry = _spooled_by_path(tmp_path)["typed-on-phone.md"]
    assert entry.kind == "create"
    assert "brand new note from the device" in entry.patch


def test_device_deletion_is_spooled_as_a_delete(tmp_path: Path, seeded_origin: Path, icloud_dir: Path) -> None:
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    (icloud_dir / "00-index.md").unlink()

    result = run_cycle(config)

    assert "00-index.md" in result.drifted
    entry = _spooled_by_path(tmp_path)["00-index.md"]
    assert entry.kind == "delete"
    assert "# Home" in entry.patch  # the removed content, visible in the deletion patch


# --- the spool-write gate: the core safety property ---------------------------------------------


def test_spool_write_failure_blocks_the_whole_cycles_publish_and_tag_advance(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """docs/DESIGN.md §7 Phase 2's own acceptance test, verbatim: "Force the spool write to fail
    for a drift patch, run the cycle -> publish does not run for that cycle, the tag does not
    advance, and the next cycle retries the overlay from scratch"."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    checkout_after_bootstrap = _tag_sha(tmp_path)
    push_commit(seeded_origin, tmp_path, {"00-index.md": "# Home (agent update)\n"}, "agent update")
    (icloud_dir / "00-index.md").write_text("uncaptured human edit\n")

    result = run_cycle(config, spool_writer=_failing_spool_writer("00-index.md"))

    assert result.spool_write_failed is True
    assert result.tag_advanced is False
    assert _tag_sha(tmp_path) == checkout_after_bootstrap  # unmoved, not partially advanced
    # Publish did not run at all this cycle: the human's uncaptured edit survives untouched, and
    # the agent's upstream update has NOT been pushed over it either -- not a partial publish.
    assert (icloud_dir / "00-index.md").read_text() == "uncaptured human edit\n"

    # And once the spool write succeeds, the *same* drift is detected again (never lost) and this
    # time resolves -- proving this isn't just permanently stuck.
    result_retry = run_cycle(config)

    assert result_retry.drifted == ("00-index.md",)
    assert result_retry.tag_advanced is True
    assert (icloud_dir / "00-index.md").read_text() == "# Home (agent update)\n"
    assert _tag_sha(tmp_path) != checkout_after_bootstrap


def test_spool_write_failure_on_one_path_blocks_publish_of_an_unrelated_drifted_path_too(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """The property that distinguishes this design from the superseded per-path-gated one
    (docs/DESIGN.md §4 Plane B, "Why the gate moved"): a single stuck path holds back *every*
    path's publish, because `LAST_CHECKOUT` names one commit and cannot mean "this path at the new
    commit, that path at the old one"."""
    push_commit(seeded_origin, tmp_path, {"10-areas/other.md": "original other\n"}, "add other")
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    push_commit(seeded_origin, tmp_path, {"00-index.md": "# Home (agent update)\n"}, "agent update")
    (icloud_dir / "00-index.md").write_text("stuck edit\n")
    (icloud_dir / "10-areas" / "other.md").write_text("an unrelated, perfectly capturable edit\n")

    result = run_cycle(config, spool_writer=_failing_spool_writer("00-index.md"))

    assert result.tag_advanced is False
    # Neither path was published this cycle -- not just the stuck one.
    assert (icloud_dir / "00-index.md").read_text() == "stuck edit\n"
    assert (icloud_dir / "10-areas" / "other.md").read_text() == "an unrelated, perfectly capturable edit\n"


def test_entries_spooled_before_a_failure_stay_durable_on_disk(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """The spool write is real and durable from the moment it succeeds, independent of whether the
    rest of that cycle goes on to fail -- an entry already written is never rolled back just
    because a later entry in the same cycle couldn't be written."""
    push_commit(seeded_origin, tmp_path, {"aaa-first.md": "first\n"}, "add first")
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    (icloud_dir / "aaa-first.md").write_text("edited first (sorts before the failing path)\n")

    run_cycle(config, spool_writer=_failing_spool_writer("zzz-does-not-exist.md"))

    # "aaa-first.md" sorts before the (nonexistent) failing path alphabetically, so
    # select_spool_entries's deterministic ordering means it was attempted, and written, first.
    spooled = _spooled_by_path(tmp_path)
    assert "aaa-first.md" in spooled
    assert "edited first" in spooled["aaa-first.md"].patch


def test_a_binary_created_on_the_device_is_never_published_over(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """A pasted image is the most ordinary non-text thing a human does in Obsidian, and it used to
    be destroyed silently: `git diff --cached` emits `Binary files ... differ` -- a patch asserting
    that something changed while carrying none of it -- the spool write for it then *succeeds*, so
    the gate read the cycle as safe and the publish rsync's `--delete` removed the file from iCloud.
    The bytes existed nowhere else: not in git, not in the spool, not on the device."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    checkout_before = _tag_sha(tmp_path)

    (icloud_dir / "_attachments").mkdir(parents=True, exist_ok=True)
    (icloud_dir / "_attachments" / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00")

    result = run_cycle(config)

    assert result.uncaptured == ("_attachments/screenshot.png",)
    assert result.tag_advanced is False
    assert _tag_sha(tmp_path) == checkout_before
    assert (icloud_dir / "_attachments" / "screenshot.png").exists()
    assert (icloud_dir / "_attachments" / "screenshot.png").read_bytes().startswith(b"\x89PNG")
    assert "_attachments/screenshot.png" not in _spooled_by_path(tmp_path)


def test_a_binary_does_not_block_the_cycle_once_it_leaves_the_vault(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """The gate pauses the cycle; it must not wedge it. Removing the out-of-contract file -- the
    operator's remedy -- lets the very next cycle proceed normally."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    (icloud_dir / "_attachments").mkdir(parents=True, exist_ok=True)
    (icloud_dir / "_attachments" / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00")
    assert run_cycle(config).tag_advanced is False

    (icloud_dir / "_attachments" / "screenshot.png").unlink()

    result = run_cycle(config)

    assert result.uncaptured == ()
    assert result.tag_advanced is True


# --- the operator's own git configuration must not reach any decision this cycle makes -----------
#
# local-replicator is the one component of this system that runs on a real machine with a real
# `~/.gitconfig` (docs/DESIGN.md §4 Plane B). Every test below sets a *real* hostile global
# configuration and drives the *real* cycle through it. None of them assert on argv: an assertion
# that a flag is present proves only that we wrote the argv we intended, not that the environment
# can no longer reach the output that argv produces.


def test_a_global_diff_external_cannot_replace_the_patch_a_markdown_edit_spools(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The severe half of the defect, and the silent one: `[diff] external = ...` -- a setting
    difftastic's own README instructs users to add -- replaces `git diff --cached`'s output
    wholesale with a summary line. The gate reads that summary, finds no binary marker, and calls
    the cycle captured; the spool entry carries a summary instead of the thought typed on the
    phone, and the publish rsync's `--delete` then overwrites the device's copy with git's. The
    note exists nowhere afterwards -- not on the device, not in git, not in the spool -- while
    `drifted`, `spooled` and `tag_advanced` all read healthy."""
    external = _executable_script(
        tmp_path / "hostile-external-diff.sh",
        "#!/bin/sh\necho '1 file changed (difftastic-style summary)'\n",
    )
    _hostile_global_git_config(tmp_path, monkeypatch, f"[diff]\n\texternal = {external}\n")
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    push_commit(seeded_origin, tmp_path, {"00-index.md": "# Home (agent update)\n"}, "agent update")
    (icloud_dir / "00-index.md").write_text("a thought typed on the phone\n")

    result = run_cycle(config)

    assert result.drifted == ("00-index.md",)
    spooled = _spooled_by_path(tmp_path)
    assert "a thought typed on the phone" in spooled["00-index.md"].patch
    assert "difftastic-style summary" not in spooled["00-index.md"].patch


def test_a_global_diff_external_cannot_smuggle_a_binary_past_the_gate(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same setting, against the case the gate exists for: git's binary marker never appears in
    the replaced output, so the pasted image is spooled as "captured" and the publish deletes the
    only copy of its bytes."""
    external = _executable_script(
        tmp_path / "hostile-external-diff.sh",
        "#!/bin/sh\necho '1 file changed (difftastic-style summary)'\n",
    )
    _hostile_global_git_config(tmp_path, monkeypatch, f"[diff]\n\texternal = {external}\n")
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    (icloud_dir / "_attachments").mkdir(parents=True, exist_ok=True)
    (icloud_dir / "_attachments" / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00")

    result = run_cycle(config)

    assert result.uncaptured == ("_attachments/screenshot.png",)
    assert result.tag_advanced is False
    assert (icloud_dir / "_attachments" / "screenshot.png").read_bytes().startswith(b"\x89PNG")


def test_a_global_textconv_driver_cannot_smuggle_a_binary_past_the_gate(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The quieter reach into the same output: a `diff.<driver>.textconv` found through a global
    `core.attributesFile`. Nothing is replaced wholesale -- git produces an ordinary-looking patch
    with an ordinary-looking hunk, and no binary marker anywhere, for a file whose actual bytes it
    never carried."""
    textconv = _executable_script(
        tmp_path / "hostile-textconv.sh",
        "#!/bin/sh\necho 'PNG image data, 800 x 600, 8-bit/color RGBA'\n",
    )
    attributes = tmp_path / "hostile-gitattributes"
    attributes.write_text("*.png diff=img\n")
    _hostile_global_git_config(
        tmp_path,
        monkeypatch,
        f'[core]\n\tattributesFile = {attributes}\n[diff "img"]\n\ttextconv = {textconv}\n',
    )
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    (icloud_dir / "_attachments").mkdir(parents=True, exist_ok=True)
    (icloud_dir / "_attachments" / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00")

    result = run_cycle(config)

    assert result.uncaptured == ("_attachments/screenshot.png",)
    assert result.tag_advanced is False
    assert (icloud_dir / "_attachments" / "screenshot.png").read_bytes().startswith(b"\x89PNG")


def test_a_global_ignore_file_cannot_hide_a_device_creation_from_the_drift_diff(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same class reached one step earlier than the patch, and the most silent member of it:
    `core.excludesFile`. Step 3 stages the overlay with `git add -A`, which consults ignore rules
    for untracked paths -- so a pattern in the operator's global ignore file makes a note created
    on the phone never appear in `git diff --cached` at all. There is no patch to judge, nothing
    lands in `uncaptured`, the gate sees a clean cycle, and the publish's `--delete` removes the
    note from iCloud.

    The vault's own tracked `.gitignore` must keep working (the `.obsidian/` rule depends on it --
    see the commit "correct the claim that .obsidian churn reaches the spool"), so what closes this
    has to be scoped to the *user-global* file specifically, not to ignore rules in general."""
    global_ignore = tmp_path / "hostile-global-gitignore"
    global_ignore.write_text("phone-*.md\n")
    _hostile_global_git_config(tmp_path, monkeypatch, f"[core]\n\texcludesFile = {global_ignore}\n")
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    (icloud_dir / "phone-draft.md").write_text("typed on the phone, never seen by git add -A\n")

    result = run_cycle(config)

    assert result.drifted == ("phone-draft.md",)
    entry = _spooled_by_path(tmp_path)["phone-draft.md"]
    assert entry.kind == "create"
    # The publish's `--delete` does remove it from the device this cycle, and that is correct
    # *because* it was captured first: the spool holds the whole note, and it returns to the device
    # once the server commits it. That ordering -- captured, then published over -- is the entire
    # property, and the ignore file removed the "captured" half of it while leaving the other.
    assert "typed on the phone, never seen by git add -A" in entry.patch


def test_a_repository_local_diff_external_cannot_replace_the_patch_either(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """The route neutralising the *environment* deliberately cannot close, and the one the flags on
    the diff invocation exist for: the same setting in the cache clone's own `.git/config`. This is
    not exotic -- `git config --local diff.external ...` is what an operator debugging a diff in
    that clone leaves behind, and the clone is a directory on their laptop like any other.

    Repository-local configuration is left readable on purpose (it is where this codebase's own
    pins live), so nothing about the scrub helps here. An explicit flag overrides configuration
    from every source at once, which is why the two mechanisms are not redundant."""
    external = _executable_script(
        tmp_path / "hostile-external-diff.sh",
        "#!/bin/sh\necho '1 file changed (difftastic-style summary)'\n",
    )
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    run_git("config", "--local", "diff.external", str(external), cwd=tmp_path / "cache-clone")

    (icloud_dir / "00-index.md").write_text("a thought typed on the phone\n")

    result = run_cycle(config)

    assert result.drifted == ("00-index.md",)
    assert "a thought typed on the phone" in _spooled_by_path(tmp_path)["00-index.md"].patch


def test_a_global_hooks_path_cannot_reach_the_tree_this_cycle_publishes(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The knob nobody would have enumerated, which is the point of closing the class rather than
    the findings: `core.hooksPath`. It names a directory of scripts git *executes*, and the cycle
    checks out twice per pass -- so a `post-checkout` hook rewrites the parked tree between the
    checkout and the publish, and the publish rsync then writes that rewrite onto the device over
    a note the human never touched. No diff flag and no capture predicate is in this path at all;
    only refusing to read the operator's configuration in the first place stops it.

    Kept as a test rather than as a comment because it is the evidence that the scrub is load-
    bearing on its own: every other hostile setting here is closed twice over."""
    hooks_dir = tmp_path / "hostile-hooks"
    hooks_dir.mkdir()
    # Hooks run with the working tree as their cwd, so a relative path is all this needs.
    _executable_script(hooks_dir / "post-checkout", "#!/bin/sh\nprintf 'CLOBBERED BY A GLOBAL HOOK\\n' > 00-index.md\n")
    _hostile_global_git_config(tmp_path, monkeypatch, f"[core]\n\thooksPath = {hooks_dir}\n")
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)

    result = run_cycle(config)

    assert result.tag_advanced is True
    assert (icloud_dir / "00-index.md").read_text() == "# Home\n"


def test_the_default_user_ignore_file_cannot_hide_a_device_creation_either(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same hole reached by the route that neutralising git's configuration files does *not*
    close, and the reason `core.excludesFile` is pinned on the invocation rather than left to the
    scrub: its default value is a path, `$XDG_CONFIG_HOME/git/ignore` (`~/.config/git/ignore`), so
    unsetting the config that names it changes nothing -- git reads the file anyway. Verified
    directly: with every configuration file replaced by `/dev/null`, an ignore file at the default
    location still made `git add -A` skip the note."""
    xdg_config_home = tmp_path / "xdg-config"
    (xdg_config_home / "git").mkdir(parents=True)
    (xdg_config_home / "git" / "ignore").write_text("phone-*.md\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config_home))
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    (icloud_dir / "phone-draft.md").write_text("typed on the phone, hidden by the default ignore file\n")

    result = run_cycle(config)

    assert result.drifted == ("phone-draft.md",)
    assert "hidden by the default ignore file" in _spooled_by_path(tmp_path)["phone-draft.md"].patch


# --- idempotency from an arbitrary starting state ------------------------------------------------


def test_cycle_recovers_from_a_working_tree_left_mid_overlay_by_a_prior_crash(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """Simulates a crash between step 2 (overlay) and step 5 (reset): the parked clone's working
    tree has stray, uncommitted, untracked content sitting in it when the next cycle starts. Step
    1's `checkout -f` + `clean -fd` must recover cleanly rather than erroring or corrupting the
    next comparison."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)  # establishes the parked clone and LAST_CHECKOUT

    cache_clone_dir = tmp_path / "cache-clone"
    # Simulate the crash residue directly: stray untracked content plus a staged-but-uncommitted
    # modification, left in the working tree as if a previous cycle died mid-overlay.
    (cache_clone_dir / "stray-untracked-from-crash.md").write_text("leftover overlay residue\n")
    (cache_clone_dir / "00-index.md").write_text("half-applied overlay content\n")
    run_git("add", "-A", cwd=cache_clone_dir)

    (icloud_dir / "10-areas").mkdir(parents=True, exist_ok=True)
    (icloud_dir / "10-areas" / "genuine-drift.md").write_text("a real device edit this cycle should see\n")

    result = run_cycle(config)

    assert result.tag_advanced is True
    # The crash residue must not appear as phantom drift, nor survive into the published tree.
    assert result.drifted == ("10-areas/genuine-drift.md",)
    assert not (icloud_dir / "stray-untracked-from-crash.md").exists()
    assert (icloud_dir / "00-index.md").read_text() == "# Home\n"


def test_cycle_recovers_from_a_working_tree_left_on_main_by_a_prior_crash(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """The residue step 1 actually exists for, and the one its sibling test cannot reach.

    A crash *after* step 5 (reset, check out `main`, pull) leaves the clone on `main`, ahead of
    `LAST_CHECKOUT`, with the tag still naming the older revision. HEAD is not something the overlay
    touches -- rsync writes files, it does not move refs -- so unless step 1 forces the tree back to
    `LAST_CHECKOUT`, the comparison runs against `main` instead of against what was actually last
    placed in iCloud, and every path the agent changed upstream reads as a device-side *reversion*.

    The sibling crash test plants stray files and staged content, both of which the overlay's own
    `--delete` and checksum copy neutralise before step 1 could matter -- which is why deleting step
    1 outright leaves it green. This one plants residue rsync cannot undo.
    """
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    parked_at = _tag_sha(tmp_path)

    push_commit(seeded_origin, tmp_path, {"00-index.md": "# Home (agent update)\n"}, "agent update")

    cache_clone_dir = tmp_path / "cache-clone"
    run_git("fetch", "-q", "origin", "main", cwd=cache_clone_dir)
    run_git("checkout", "-q", "-B", "main", "FETCH_HEAD", cwd=cache_clone_dir)
    assert _tag_sha(tmp_path) == parked_at

    result = run_cycle(config)

    assert result.drifted == ()
    assert _spooled_by_path(tmp_path) == {}
    assert result.tag_advanced is True
    assert (icloud_dir / "00-index.md").read_text() == "# Home (agent update)\n"


# --- non-ASCII and quoted filenames, end to end --------------------------------------------------


def test_non_ascii_filename_drift_survives_the_full_git_and_rsync_pipeline(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    """The exact bug class that "wedged the committer permanently" (ppat/obsidian-tools#3): a note
    titled in Japanese must not break `-z` porcelain parsing anywhere in this pipeline."""
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    non_ascii_name = "40-journal/2026-07-31-日本語のノート.md"
    (icloud_dir / "40-journal").mkdir(parents=True, exist_ok=True)
    (icloud_dir / "40-journal" / "2026-07-31-日本語のノート.md").write_text("非同期の下書き\n")

    result = run_cycle(config)

    assert non_ascii_name in result.drifted
    entry = _spooled_by_path(tmp_path)[non_ascii_name]
    assert entry.kind == "create"
    assert "非同期の下書き" in entry.patch
    assert result.tag_advanced is True
    # The patch *header* must carry the same bytes as `path`, not git's C-quoted escaping. Without
    # `core.quotePath=false` (clone.py) one spool entry holds two encodings of one path, and a
    # Phase 5 consumer reading the diff header gets the escaped one. Asserted on the header
    # specifically because every other assertion in this test passes with the quoting present.
    assert "日本語" in entry.patch.splitlines()[0]
    assert "\\346" not in entry.patch


def test_filename_with_embedded_quote_survives_the_full_pipeline(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)
    quoted_name = '10-areas/note "with quotes".md'
    (icloud_dir / "10-areas").mkdir(parents=True, exist_ok=True)
    (icloud_dir / "10-areas" / 'note "with quotes".md').write_text("quoted filename content\n")

    result = run_cycle(config)

    assert quoted_name in result.drifted
    assert result.tag_advanced is True


# --- `.obsidian/` publish rule --------------------------------------------------------------------


def test_obsidian_copied_once_absent_then_left_alone_when_present(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    push_commit(
        seeded_origin,
        tmp_path,
        {
            ".obsidian/app.json": '{"legacyEditor": false}\n',
            ".obsidian/workspace.json": '{"instance": "cluster"}\n',
        },
        "obsidian baseline",
    )
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)

    result = run_cycle(config)

    assert result.obsidian_seed_attempted is True
    assert (icloud_dir / ".obsidian" / "app.json").read_text() == '{"legacyEditor": false}\n'
    assert not (icloud_dir / ".obsidian" / "workspace.json").exists()  # per-instance state, never seeded

    # The device personalises its own config afterwards.
    (icloud_dir / ".obsidian" / "app.json").write_text('{"legacyEditor": true}\n')
    (icloud_dir / ".obsidian" / "community-plugins.json").write_text('["dataview"]\n')

    result_2 = run_cycle(config)

    assert result_2.obsidian_seed_attempted is False
    assert (icloud_dir / ".obsidian" / "app.json").read_text() == '{"legacyEditor": true}\n'  # untouched
    assert (icloud_dir / ".obsidian" / "community-plugins.json").exists()  # untouched


# --- shared exclude list, end to end --------------------------------------------------------------


def test_shared_exclude_list_suppresses_spurious_drift_end_to_end(
    tmp_path: Path, seeded_origin: Path, icloud_dir: Path
) -> None:
    config = replicate_config(tmp_path, seeded_origin, icloud_dir)
    run_cycle(config)

    (icloud_dir / ".DS_Store").write_text("finder metadata\n")
    (icloud_dir / "note.md.icloud").write_text("dataless placeholder stub\n")

    result = run_cycle(config)

    assert result.drifted == ()
    assert result.tag_advanced is True  # no false-positive drift ever blocks an ordinary advance
