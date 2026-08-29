"""Tests for obsidian_tools/vault_git/baseline.py.

Two core properties under test:

- `.gitignore` cannot make the `.obsidian/` freeze happen — only `git update-index
  --skip-worktree`, reapplied every run, does. These tests exist specifically to catch a
  regression back to `.gitignore`-only behaviour, which would pass every other test in this suite
  while silently failing the one thing that matters (see the module's own docstring and
  ppat/obsidian-tools#3, "empirically, not reasoned out in advance").
- The baseline capture is an allowlist, never a denylist: a plugin's `data.json` (settings/state,
  potentially secret-bearing — the Local REST API plugin's bearer token lives at exactly this kind
  of path) must never be captured, no matter what the plugin is named. `test_plugin_data_json_is_never_captured`
  is the regression test for the vulnerability caught before it ever ran (ppat/obsidian-tools#3):
  it fails against the old denylist-shaped implementation.

The volume of "which paths does the allowlist match" adversarial cases lives in
`test_vault_git_baseline_selector.py` now, against the pure `select_baseline_paths` directly — no
real filesystem or git repo needed for those. What stays here is what only a real filesystem and a
real git repository can prove: that the walker in this module actually produces the candidates the
selector needs, that a symlinked *directory* doesn't reach `git add` as a pathspec "beyond a
symbolic link" (`ppat/obsidian-tools#22`), and that nothing ever lands in the index as a symlink
blob (mode `120000`) end to end.
"""

from __future__ import annotations

import logging
import shutil
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import make_runner, run_git

from obsidian_tools.logging_config import LOG_PATH_SAMPLE_LIMIT
from obsidian_tools.vault_git.baseline import ensure_obsidian_baseline
from obsidian_tools.vault_git.commit import create_commit, has_staged_changes, push_all, stage_all
from obsidian_tools.vault_git.provisioning import provision_repository
from obsidian_tools.vault_git.runner import GitRunner


def _provision(git_dir: Path, work_tree: Path, *, origin_url: str, nas_url: str) -> GitRunner:
    runner = make_runner(git_dir, work_tree)
    provision_repository(
        runner,
        branch="main",
        author_name="test-committer",
        author_email="test-committer@example.invalid",
        origin_url=origin_url,
        nas_url=nas_url,
    )
    return runner


def _write_obsidian_dir(vault_dir: Path) -> None:
    obsidian = vault_dir / ".obsidian"
    obsidian.mkdir()
    (obsidian / "app.json").write_text('{"legacyEditor": false}\n')
    (obsidian / "workspace.json").write_text('{"instance": "a"}\n')
    (obsidian / "workspaces.json").write_text('{"instance": "a"}\n')


def _write_plugin(obsidian_dir: Path, name: str) -> Path:
    plugin_dir = obsidian_dir / "plugins" / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "manifest.json").write_text(f'{{"id": "{name}"}}\n')
    (plugin_dir / "main.js").write_text("// plugin code\n")
    return plugin_dir


def _records_with_event(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if getattr(record, "event", None) == event]


def _one_record_with_event(caplog: pytest.LogCaptureFixture, event: str) -> logging.LogRecord:
    records = _records_with_event(caplog, event)
    assert len(records) == 1, f"expected exactly one {event!r} record, got {len(records)}"
    return records[0]


def test_pathspec_excludes_workspace_state_files(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    ensure_obsidian_baseline(runner, vault_dir)

    staged = runner.run(["diff", "--cached", "--name-only"]).stdout.splitlines()
    assert ".obsidian/app.json" in staged
    assert ".obsidian/workspace.json" not in staged
    assert ".obsidian/workspaces.json" not in staged


def test_plugin_data_json_is_never_captured(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    """The regression test for the vulnerability caught before it ever ran (ppat/obsidian-tools#3):
    a denylist-shaped baseline (force-add `.obsidian/` minus the two workspace files) would commit
    a plugin's `data.json` — the file the Local REST API plugin stores its bearer token in — to
    permanent history on both remotes. Fails against that old implementation; passes only because
    the baseline is an allowlist of plugin *code* filenames that `data.json` is never on."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)
    plugin_dir = vault_dir / ".obsidian" / "plugins" / "obsidian-local-rest-api"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "manifest.json").write_text('{"id": "obsidian-local-rest-api"}\n')
    (plugin_dir / "main.js").write_text("// plugin code\n")
    (plugin_dir / "data.json").write_text('{"apiKey": "super-secret-bearer-token"}\n')

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    ensure_obsidian_baseline(runner, vault_dir)

    staged = runner.run(["diff", "--cached", "--name-only"]).stdout.splitlines()
    assert ".obsidian/plugins/obsidian-local-rest-api/manifest.json" in staged
    assert ".obsidian/plugins/obsidian-local-rest-api/main.js" in staged
    assert ".obsidian/plugins/obsidian-local-rest-api/data.json" not in staged
    for path in staged:
        assert not path.endswith("data.json"), f"plugin state file {path} must never be baselined"


def test_baseline_taken_once_then_frozen_even_after_a_later_edit(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    """The single most important behaviour: once the baseline is committed, a later change to a
    baselined file must NOT be re-staged by an ordinary `git add -A` — the property `.gitignore`
    was shown to be unable to provide."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)

    # Cycle 1: baseline captured and committed.
    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    took_baseline = ensure_obsidian_baseline(runner, vault_dir)
    stage_all(runner)
    assert took_baseline
    assert has_staged_changes(runner)
    create_commit(runner, cycle_time=datetime.now(UTC))

    committed_tree = set(runner.list_tree_paths("HEAD", ".obsidian"))
    assert ".obsidian/app.json" in committed_tree
    assert ".obsidian/workspace.json" not in committed_tree

    # A setting is changed at the cluster GUI: app.json is edited in place on the (simulated) volume.
    (vault_dir / ".obsidian" / "app.json").write_text('{"legacyEditor": true}\n')

    # Cycle 2: full provisioning re-run, as if against a fresh pod, then the ordinary staging path.
    runner_2 = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    took_baseline_again = ensure_obsidian_baseline(runner_2, vault_dir)
    stage_all(runner_2)

    assert not took_baseline_again
    assert not has_staged_changes(runner_2), (
        "the edited .obsidian/app.json was re-staged — the skip-worktree freeze did not survive, "
        "which is exactly the failure .gitignore alone produces"
    )


def test_baseline_survives_a_lost_git_dir_cache(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    """Skip-worktree bits are index-local and do not survive a wiped git-dir cache even though the
    baseline commit itself (fetched fresh from origin) does — provisioning must reapply the bits
    from HEAD's tree, and must NOT retake the baseline commit a second time."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    ensure_obsidian_baseline(runner, vault_dir)
    stage_all(runner)
    create_commit(runner, cycle_time=datetime.now(UTC))
    push_all(runner, branch="main")

    shutil.rmtree(git_dir)
    (vault_dir / ".obsidian" / "app.json").write_text('{"legacyEditor": true}\n')

    runner_2 = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    took_baseline_again = ensure_obsidian_baseline(runner_2, vault_dir)
    stage_all(runner_2)

    assert took_baseline_again is False, "the baseline must not be recaptured just because the cache was rebuilt"
    assert not has_staged_changes(runner_2)


def test_tracked_workspace_file_is_frozen_by_the_reapply_loop_too(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    """`ensure_obsidian_baseline`'s own forced add never captures workspace.json/workspaces.json —
    they're never on the allowlist. But if one is ever hand-seeded directly into history, bypassing
    this module entirely (an operator committing it by hand — not reachable through this module's
    own code path, but reachable the moment anyone does it), the *reapply* branch must still apply
    skip-worktree to it unconditionally, with no exception for the workspace files: that filter is
    correct in the capture branch (don't take them into the baseline) and backwards in the reapply
    branch, where excluding them left a tracked workspace file frozen by neither mechanism."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"

    hand_seed_clone = tmp_path / "hand-seed-clone"
    run_git("clone", "-q", str(seeded_origin), str(hand_seed_clone), cwd=tmp_path)
    (hand_seed_clone / ".obsidian").mkdir()
    (hand_seed_clone / ".obsidian" / "workspace.json").write_text('{"instance": "a"}\n')
    run_git("add", "-A", cwd=hand_seed_clone)
    run_git(
        "-c",
        "user.name=seed",
        "-c",
        "user.email=seed@example.invalid",
        "commit",
        "-q",
        "-m",
        "hand-seed workspace.json",
        cwd=hand_seed_clone,
    )
    run_git("push", "-q", "origin", "main", cwd=hand_seed_clone)

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    (vault_dir / ".obsidian").mkdir()
    (vault_dir / ".obsidian" / "workspace.json").write_text('{"instance": "a"}\n')
    took_baseline = ensure_obsidian_baseline(runner, vault_dir)
    assert took_baseline is False  # HEAD already carries .obsidian/, so this exercises the reapply branch

    # A device (or a human at the cluster GUI) edits the tracked workspace file afterward.
    (vault_dir / ".obsidian" / "workspace.json").write_text('{"instance": "b"}\n')
    stage_all(runner)

    assert not has_staged_changes(runner), "a tracked workspace.json must be frozen by the reapply loop too"


def test_no_baseline_taken_when_obsidian_dir_absent(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    took_baseline = ensure_obsidian_baseline(runner, vault_dir)

    assert took_baseline is False
    assert not has_staged_changes(runner)


@pytest.mark.parametrize(
    "filename",
    [
        "日本語.css",  # non-ASCII bytes
        'quote".css',  # a literal double-quote
        "line\nbreak.css",  # a literal newline
    ],
    ids=["non-ascii", "literal-quote", "embedded-newline"],
)
def test_baseline_survives_quotepath_hostile_filenames(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path, filename: str
) -> None:
    """`core.quotePath` defaults to true, so git C-quotes any of these filenames -- including the
    surrounding quote characters -- in the line-oriented form of `ls-tree --name-only` and
    `diff --cached --name-only`. Feeding that quoted string straight into
    `git update-index --skip-worktree --` fails with `fatal: Unable to mark file`, which the
    staging-failure handler in obsidian_tools/commands/commit.py then misattributes as "likely a
    persistent vault read error" -- the exact misattribution the lock/read-failure disambiguation
    fix there existed to remove. A filename containing a newline used to crash the baseline
    outright. `-z` (NUL-delimited, unquoted output) is what fixes all three at every site that
    parses git path output: baseline.py's capture branch, its reapply branch, and
    `GitRunner.list_tree_paths` itself."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)
    (vault_dir / ".obsidian" / "snippets").mkdir()
    (vault_dir / ".obsidian" / "snippets" / filename).write_text("body {}\n")

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    took_baseline = ensure_obsidian_baseline(runner, vault_dir)
    stage_all(runner)
    assert took_baseline
    create_commit(runner, cycle_time=datetime.now(UTC))

    committed_tree = set(runner.list_tree_paths("HEAD", ".obsidian"))
    assert f".obsidian/snippets/{filename}" in committed_tree

    # The reapply branch (a later run, against a fresh provisioning pass) must also survive this
    # filename without raising -- the second of the "three sites" this fix covers.
    runner_2 = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    took_baseline_again = ensure_obsidian_baseline(runner_2, vault_dir)
    assert took_baseline_again is False

    # And a later edit to the baselined file must stay frozen, exactly like any other baselined path.
    (vault_dir / ".obsidian" / "snippets" / filename).write_text("body { color: red; }\n")
    stage_all(runner_2)
    assert not has_staged_changes(runner_2)


def _write_theme(obsidian_dir: Path, name: str) -> None:
    theme_dir = obsidian_dir / "themes" / name
    theme_dir.mkdir(parents=True)
    (theme_dir / "theme.css").write_text("/* theme */\n")
    (theme_dir / "manifest.json").write_text(f'{{"name": "{name}"}}\n')


def test_themes_and_snippets_are_constrained_to_css_and_theme_manifest(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    """A bare `snippets/`/`themes/` directory prefix admits every file of any name at any depth --
    the same allowlist-as-denylist mistake the plugin `data.json` exclusion exists to prevent, one
    level down (ppat/obsidian-tools#3). Regression test for a probe that staged
    `.obsidian/themes/Minimal/data.json`, `.obsidian/themes/deep/nested/inner/data.json` and
    `.obsidian/snippets/sub/dir/creds.json` against the old bare-directory implementation. Themes
    are third-party code installed through the ungated GUI path (ADR-0028), so
    "nothing secret would ever land there" is not a claim this baseline gets to make."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)
    obsidian = vault_dir / ".obsidian"

    (obsidian / "snippets").mkdir()
    (obsidian / "snippets" / "custom.css").write_text("body {}\n")
    (obsidian / "snippets" / "sub" / "dir").mkdir(parents=True)
    (obsidian / "snippets" / "sub" / "dir" / "creds.json").write_text('{"token": "leak"}\n')

    _write_theme(obsidian, "Minimal")
    (obsidian / "themes" / "Minimal" / "data.json").write_text('{"secret": "leak"}\n')
    (obsidian / "themes" / "deep" / "nested" / "inner").mkdir(parents=True)
    (obsidian / "themes" / "deep" / "nested" / "inner" / "data.json").write_text('{"secret": "leak"}\n')

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    ensure_obsidian_baseline(runner, vault_dir)

    staged = runner.run(["diff", "--cached", "--name-only"]).stdout.splitlines()

    assert ".obsidian/snippets/custom.css" in staged
    assert ".obsidian/themes/Minimal/theme.css" in staged
    assert ".obsidian/themes/Minimal/manifest.json" in staged
    assert ".obsidian/snippets/sub/dir/creds.json" not in staged
    assert ".obsidian/themes/Minimal/data.json" not in staged
    assert ".obsidian/themes/deep/nested/inner/data.json" not in staged
    for path in staged:
        assert not path.endswith("data.json"), f"plugin/theme state file {path} must never be baselined"


def test_symlinks_under_themes_and_snippets_are_never_captured(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    """A symlink under `snippets/` or `themes/` stores only its target *path* as a git blob -- the
    target's content is never leaked through git itself -- but that target string re-resolves
    against whatever filesystem later checks the clone out, the Mac clone's iCloud copy included.
    `snippets/escape.css -> /etc/passwd` would publish a live pointer outside the vault entirely
    onto every replica, so symlinks are excluded from the baseline outright."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)
    obsidian = vault_dir / ".obsidian"
    (obsidian / "snippets").mkdir()
    (obsidian / "snippets" / "escape.css").symlink_to("/etc/passwd")
    _write_theme(obsidian, "Minimal")
    (obsidian / "themes" / "Minimal" / "manifest.json").unlink()
    (obsidian / "themes" / "Minimal" / "manifest.json").symlink_to("/etc/hostname")

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    ensure_obsidian_baseline(runner, vault_dir)

    staged = runner.run(["diff", "--cached", "--name-only"]).stdout.splitlines()
    assert ".obsidian/snippets/escape.css" not in staged
    assert ".obsidian/themes/Minimal/manifest.json" not in staged
    assert ".obsidian/themes/Minimal/theme.css" in staged  # the ordinary, non-symlink file is unaffected


def test_symlinked_plugin_directory_does_not_wedge_the_committer(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    """BROKEN, reproduced (`ppat/obsidian-tools#22`): `.obsidian/plugins/my-plugin -> ~/dev/my-plugin`
    is the standard local plugin-development layout. The plugins loop used to build a pathspec
    straight through the symlinked directory (`file_path.is_file()` alone, no `is_symlink` check
    anywhere in that loop, unlike the themes/snippets loops), and `git add --force` on a pathspec
    that walks through a symlink fails outright: `fatal: pathspec '...' is beyond a symbolic link`.
    `ensure_obsidian_baseline` propagated that as an uncaught `GitCommandError` -- and because the
    baseline branch is only skipped once `HEAD` already carries `.obsidian/`, which then never
    happens, every future run hit the same failure: no vault content ever committed again, on every
    cycle, forever, logged as \"staging failed, likely a persistent vault read error\"."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)
    external_plugin = tmp_path / "dev" / "my-plugin"
    external_plugin.mkdir(parents=True)
    (external_plugin / "manifest.json").write_text('{"id": "my-plugin"}\n')
    (external_plugin / "main.js").write_text("// plugin code\n")
    plugins_dir = vault_dir / ".obsidian" / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "my-plugin").symlink_to(external_plugin, target_is_directory=True)
    # An ordinary, real plugin alongside the symlinked one -- proving the symlinked directory is
    # skipped specifically, not that the whole plugins branch silently stopped working.
    real_plugin = plugins_dir / "obsidian-local-rest-api"
    real_plugin.mkdir()
    (real_plugin / "manifest.json").write_text('{"id": "obsidian-local-rest-api"}\n')

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    took_baseline = ensure_obsidian_baseline(runner, vault_dir)  # must not raise GitCommandError

    assert took_baseline
    staged = runner.run(["diff", "--cached", "--name-only"]).stdout.splitlines()
    assert not any(path.startswith(".obsidian/plugins/my-plugin/") for path in staged)
    assert ".obsidian/plugins/obsidian-local-rest-api/manifest.json" in staged


def test_symlinked_obsidian_root_does_not_wedge_the_committer(
    tmp_path: Path,
    seeded_origin: Path,
    make_bare_repo: Callable[[], Path],
    vault_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """BROKEN, reproduced (`ppat/obsidian-tools#22`): nothing ever produces a `PathInfo` for
    `.obsidian/` *itself* -- only for entries *inside* it -- so neither the walker's non-descent
    into a symlinked directory nor the selector's `is_symlink` rule ever applies to the root. The
    only check on the root used to be `(work_tree / OBSIDIAN_DIR).is_dir()`, and `is_dir()` follows
    symlinks, so a symlinked `.obsidian` root read as "present": the walk built a pathspec
    like `.obsidian/app.json` that walks *through* the symlink, `git add --force` failed with
    `fatal: pathspec '...' is beyond a symbolic link`, the retry wrapping that call exhausted, and
    the uncaught `RetryExhaustedError` wedged every future run -- the baseline branch is only ever
    skipped once `HEAD` already carries `.obsidian/`, which this failure prevents from ever
    happening -- so no vault content was ever committed again, on any cycle, logged as "staging
    failed, likely a persistent vault read error". Reproduced over three consecutive cycles below,
    with new content arriving each time, matching the reported "every cycle, forever" shape."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    external_target = tmp_path / "not-really-obsidian"
    external_target.mkdir()
    (external_target / "app.json").write_text('{"legacyEditor": false}\n')
    (vault_dir / ".obsidian").symlink_to(external_target, target_is_directory=True)

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    with caplog.at_level(logging.INFO):
        took_baseline = ensure_obsidian_baseline(runner, vault_dir)  # must not raise

    assert took_baseline is False
    events = [getattr(record, "event", None) for record in caplog.records]
    assert "baseline_skip_symlinked_obsidian_dir" in events
    # Distinct from the absent-.obsidian event -- an operator diagnosing this needs to be able to
    # tell "nothing there yet" apart from "there's something there, but it's the wrong shape".
    assert "baseline_skip_no_obsidian_dir" not in events

    # Vault content still gets committed even though the (permanently unbaselined) symlink sits
    # there -- new content, since vault_dir's seeded 00-index.md already matches what provisioning
    # just pulled from origin and so wouldn't stage anything on its own.
    (vault_dir / "new-note-0.md").write_text("# cycle 0\n")
    stage_all(runner)
    assert has_staged_changes(runner)
    create_commit(runner, cycle_time=datetime.now(UTC))
    committed_tree = set(runner.list_tree_paths("HEAD"))
    assert "new-note-0.md" in committed_tree
    assert not any(path.startswith(".obsidian") for path in committed_tree)

    # And the wedge doesn't reappear on a later cycle -- the reported failure mode was permanent,
    # so proving it clears once isn't enough.
    for cycle in range(1, 3):
        (vault_dir / f"new-note-{cycle}.md").write_text(f"# cycle {cycle}\n")
        runner_n = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
        took_baseline_n = ensure_obsidian_baseline(runner_n, vault_dir)  # must not raise
        assert took_baseline_n is False
        stage_all(runner_n)
        assert has_staged_changes(runner_n)
        create_commit(runner_n, cycle_time=datetime.now(UTC))


def test_ignore_rule_still_excludes_the_directory(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    """Guard for the obvious way to break `test_ignore_rule_excludes_a_symlink_replacing_the_directory`
    below: dropping the exclude rule's trailing slash to cover the symlink case must not stop it from
    covering the ordinary, much more common directory case -- an untouched `.obsidian/` directory
    (nothing on the baseline allowlist yet) must never itself appear as a staged path."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    (vault_dir / ".obsidian").mkdir()
    (vault_dir / ".obsidian" / "workspace.json").write_text('{"instance": "a"}\n')  # never allowlisted

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    ensure_obsidian_baseline(runner, vault_dir)
    stage_all(runner)

    staged = runner.run(["diff", "--cached", "--name-only"]).stdout.splitlines()
    assert not any(path == ".obsidian" or path.startswith(".obsidian/") for path in staged)


def test_ignore_rule_excludes_a_symlink_replacing_the_directory(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    """Corollary to the symlinked-root wedge (`ppat/obsidian-tools#22`): the git-dir exclude rule was
    `.obsidian/` -- trailing slash, directory-only in gitignore semantics -- so it was inert against
    a symlink of the same name. Measured against the pre-fix rule: baseline taken while `.obsidian`
    was a real directory; `.obsidian` later replaced by a symlink; `ensure_obsidian_baseline` takes
    the reapply branch (`HEAD` already carries `.obsidian/`) and does not raise; `stage_all`'s
    `git add -A` then staged `.obsidian` itself as a mode `120000` blob whose contents are the
    external target path -- published to both remotes, the Mac clone, iCloud and the phone -- and
    dropped the baselined `.obsidian/app.json` from the index in the same diff."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)

    # Cycle 1: baseline captured normally, while .obsidian is a real directory.
    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    took_baseline = ensure_obsidian_baseline(runner, vault_dir)
    stage_all(runner)
    assert took_baseline
    create_commit(runner, cycle_time=datetime.now(UTC))

    # An operator (or a device) later replaces .obsidian with a symlink -- e.g. pointing it at a
    # shared config directory outside the vault.
    shutil.rmtree(vault_dir / ".obsidian")
    external_target = tmp_path / "elsewhere"
    external_target.mkdir()
    (vault_dir / ".obsidian").symlink_to(external_target, target_is_directory=True)

    runner_2 = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    took_baseline_again = ensure_obsidian_baseline(runner_2, vault_dir)  # reapply branch; must not raise
    assert took_baseline_again is False
    stage_all(runner_2)

    assert not has_staged_changes(runner_2), (
        "the .obsidian symlink was staged (as a mode 120000 blob) instead of being excluded, and/or "
        "the baselined .obsidian/app.json was dropped from the index"
    )


def _staged_mode(runner: GitRunner, path: str) -> str:
    """The index mode git currently has staged for `path` (`100644` ordinary, `120000` symlink)."""
    raw = runner.run(["diff", "--cached", "--raw", "--", path]).stdout
    assert raw, f"{path} is not staged at all"
    # `:<old-mode> <new-mode> <old-sha> <new-sha> <status>\t<path>` -- see `git-diff-index(1)`.
    return raw.split()[1]


def test_symlinked_leaf_files_are_never_staged_as_symlink_blobs(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    """BROKEN (latent, security-shaped), reproduced (`ppat/obsidian-tools#22`): a symlink git stages
    is committed as a mode `120000` blob whose *contents* are the target path string, published
    verbatim to both remotes, the Mac clone, iCloud and the phone. Measured against the pre-fix
    plugins loop: `manifest.json -> /etc/passwd` staged clean, mode `120000`, blob contents
    `/etc/passwd`. Exercises the top-level and plugins branches --
    `test_symlinks_under_themes_and_snippets_are_never_captured` already covers themes/snippets --
    so all three prior "which branch has the symlink check" variants are covered by an actual test,
    not by inspection of which loop looks right."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)
    obsidian = vault_dir / ".obsidian"
    (obsidian / "app.json").unlink()
    (obsidian / "app.json").symlink_to("/etc/hostname")
    plugin_dir = obsidian / "plugins" / "obsidian-local-rest-api"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "manifest.json").symlink_to("/etc/passwd")
    (plugin_dir / "main.js").write_text("// plugin code\n")  # ordinary sibling, unaffected

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    ensure_obsidian_baseline(runner, vault_dir)

    staged = runner.run(["diff", "--cached", "--name-only"]).stdout.splitlines()
    assert ".obsidian/app.json" not in staged
    assert ".obsidian/plugins/obsidian-local-rest-api/manifest.json" not in staged
    assert ".obsidian/plugins/obsidian-local-rest-api/main.js" in staged
    assert _staged_mode(runner, ".obsidian/plugins/obsidian-local-rest-api/main.js") == "100644"


def test_theme_manifest_deeper_than_its_own_directory_is_not_captured(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    """LOW (`#3`/`docs/settings-lock.md`'s own hand-run checklist command names this depth
    explicitly: `.obsidian/themes/*/manifest.json`, never a recursive search). An earlier revision's
    `directory.rglob("manifest.json")` let a manifest at any depth through -- one narrowing looser
    than the checklist it's meant to automate."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)
    obsidian = vault_dir / ".obsidian"
    _write_theme(obsidian, "Minimal")
    nested = obsidian / "themes" / "Minimal" / "nested"
    nested.mkdir()
    (nested / "manifest.json").write_text('{"name": "not a theme manifest"}\n')

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    ensure_obsidian_baseline(runner, vault_dir)

    staged = runner.run(["diff", "--cached", "--name-only"]).stdout.splitlines()
    assert ".obsidian/themes/Minimal/manifest.json" in staged  # depth 1: still captured
    assert ".obsidian/themes/Minimal/nested/manifest.json" not in staged  # depth 2: excluded


def test_glob_metacharacter_filename_does_not_sweep_in_a_differently_named_symlink(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    """LOW (`#22`): the walk's own docstring used to claim every path it returns is safe to hand
    straight to `git add --force --`. That's true of *existence* (mostly -- see the TOCTOU note in
    that docstring now), but was never true of *interpretation*: without pathspec magic, a filename
    containing `[...]` is a glob pattern to git, not a literal name. Measured against the pre-fix
    call: with both `custom[1].css` (the real, allowlisted snippet) and a *differently-named*
    `custom1.css -> /etc/passwd` symlink present, `git add --force -- .obsidian/snippets/custom[1].css`
    staged **both** -- the exact literal file, and the symlink, via `[1]` fnmatching the single
    character `1`. That's the selector's `is_symlink` exclusion bypassed entirely: the symlink was
    never in `baseline_paths` (the selector rejected it), but git's own glob interpretation of the
    *other* pathspec swept it in anyway. `:(literal)` closes this by pinning each pathspec to
    exactly the path it names, no fnmatch involved."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)
    snippets = vault_dir / ".obsidian" / "snippets"
    snippets.mkdir()
    (snippets / "custom[1].css").write_text("body {}\n")
    (snippets / "custom1.css").symlink_to("/etc/passwd")  # never selected; must never be staged either

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    ensure_obsidian_baseline(runner, vault_dir)

    staged = set(runner.run(["diff", "--cached", "--name-only"]).stdout.splitlines())
    assert ".obsidian/snippets/custom[1].css" in staged  # the real, literal, allowlisted file
    assert ".obsidian/snippets/custom1.css" not in staged  # swept in by fnmatch pre-fix
    assert _staged_mode(runner, ".obsidian/snippets/custom[1].css") == "100644"


def test_walker_handles_deeply_nested_snippet_directories(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    """The walker (`baseline._iter_obsidian_candidates`) used to be recursive -- one Python call per
    directory level -- and bisection against an isolated reproduction found it raises
    `RecursionError` at ~992 levels (fine at 991), well under filesystem `PATH_MAX`. Neither this
    module's own `OSError` handling nor `commands/commit.py`'s `_STAGING_FAILURES` tuple catches
    `RecursionError` (it is not an `OSError`, and it is neither `GitCommandError` nor
    `RetryExhaustedError`), so it used to surface as an uncaught traceback with the same
    permanent-wedge shape as the symlinked-root case (`ppat/obsidian-tools#22`); no designed vault
    layout nests anywhere near that deep.

    This test is deliberately modest-depth (50 levels), not pinned to the bisected ~992 threshold:
    where exactly Python's own recursion limit bites depends on how much of the call stack is
    already spent by the test runner (pytest/coverage/hypothesis frames), which makes an
    exact-threshold test environment-fragile rather than a stable regression guard, and creating
    ~1000 real nested directories per test run buys little beyond what code review already gives:
    the rewritten `_iter_obsidian_candidates` has no recursive self-call left in it at all (an
    explicit stack instead), which is what actually rules out `RecursionError` at any depth, not a
    specific number. What this test proves is that the rewrite still walks and selects correctly
    through a multi-level directory chain deeper than every other test in this suite uses -- a
    smoke test for the rewrite's correctness, not a repro of the crash itself. (Verified separately,
    by literal revert-and-run against the pre-fix recursive implementation at depths beyond the
    interpreter's recursion limit, that the crash this module's docstring describes is real and that
    the rewrite no longer reproduces it -- see the PR description.)"""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)
    depth = 50
    nested = vault_dir / ".obsidian" / "snippets"
    for level in range(depth):
        nested = nested / f"d{level}"
    nested.mkdir(parents=True)
    (nested / "deep.css").write_text("body {}\n")

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    ensure_obsidian_baseline(runner, vault_dir)  # must not raise RecursionError

    staged = runner.run(["diff", "--cached", "--name-only"]).stdout.splitlines()
    expected = ".obsidian/snippets/" + "/".join(f"d{level}" for level in range(depth)) + "/deep.css"
    assert expected in staged


# --- the walk must be complete, or no baseline is taken at all (ppat/obsidian-tools#35) -----------
#
# Unlike `local_replicator/device_baseline.py`, which copies what it could reach and withholds its
# own completion marker so the next cycle tops the copy up, this module has no marker to withhold:
# its "has the baseline been taken" signal *is* git history (`path_exists_at("HEAD", ".obsidian")`),
# which goes true the instant anything lands under `.obsidian/` and can never be un-taken. So the
# only place a partial capture can be stopped is before the commit exists at all.


def test_an_unreadable_directory_refuses_the_baseline_until_the_read_error_clears(
    tmp_path: Path,
    seeded_origin: Path,
    make_bare_repo: Callable[[], Path],
    vault_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """BROKEN, reproduced (`ppat/obsidian-tools#35`): the walker swallowed an `OSError` from
    `iterdir()` and carried on, so a directory unreadable for one 15-minute tick produced no
    candidates for anything beneath it and the capture committed whatever else it happened to see.
    `ensure_obsidian_baseline`'s own guard then went true against that partial tree, and every later
    run took the reapply branch and never re-walked -- so the omission was permanent even after the
    read error cleared. `/vault/brain` is a soft-mounted (`softerr`) Longhorn NFS export whose
    documented normal failure mode is exactly this: I/O returns an error rather than hanging
    (ADR-0033).

    `chmod 0` stands in for that transient read error, the same way
    `tests/test_local_replicator_device_baseline.py` reproduces the sibling module's version of this
    bug class. The fix refuses the capture outright while anything was unreadable, so nothing partial
    ever enters history and the next run retries against a clean walk."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)
    obsidian = vault_dir / ".obsidian"
    _write_plugin(obsidian, "plugin-a")
    denied = _write_plugin(obsidian, "plugin-b")
    denied.chmod(0)

    try:
        runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
        with caplog.at_level(logging.INFO):
            took_baseline = ensure_obsidian_baseline(runner, vault_dir)

        assert took_baseline is False
        staged = runner.run(["diff", "--cached", "--name-only"]).stdout.splitlines()
        assert not any(path.startswith(".obsidian/") for path in staged), (
            "a partial .obsidian/ capture was staged; committing it freezes the omission permanently"
        )

        refusal = _one_record_with_event(caplog, "baseline_refused_incomplete_walk")
        assert refusal.levelno == logging.WARNING
        assert getattr(refusal, "unreadable_paths") == [str(denied)]  # noqa: B009 -- LogRecord attr

        # The rest of the run is unaffected: refusing the baseline must not stop ordinary vault
        # content being committed, which is the wedge shape ppat/obsidian-tools#22 already cost.
        (vault_dir / "new-note.md").write_text("# note\n")
        stage_all(runner)
        assert has_staged_changes(runner)
        create_commit(runner, cycle_time=datetime.now(UTC))
        push_all(runner, branch="main")
        committed_tree = set(runner.list_tree_paths("HEAD"))
        assert "new-note.md" in committed_tree
        assert not any(path.startswith(".obsidian") for path in committed_tree)

        # The transient read error clears. The next run re-walks from scratch (the guard is still
        # false, because nothing partial was ever committed) and captures the whole thing.
        denied.chmod(stat.S_IRWXU)
        runner_2 = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
        took_baseline_again = ensure_obsidian_baseline(runner_2, vault_dir)
        stage_all(runner_2)
        create_commit(runner_2, cycle_time=datetime.now(UTC))

        assert took_baseline_again is True
        assert set(runner_2.list_tree_paths("HEAD", ".obsidian")) == {
            ".obsidian/app.json",
            ".obsidian/plugins/plugin-a/main.js",
            ".obsidian/plugins/plugin-a/manifest.json",
            ".obsidian/plugins/plugin-b/main.js",
            ".obsidian/plugins/plugin-b/manifest.json",
        }
    finally:
        denied.chmod(stat.S_IRWXU)  # tmp_path cleanup


def test_entries_that_cannot_be_stat_ed_refuse_the_baseline_too(
    tmp_path: Path,
    seeded_origin: Path,
    make_bare_repo: Callable[[], Path],
    vault_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The second, subtler half of `#35`, and the one a naive fix misses. `#35` names the walker's
    per-entry `except OSError` around `is_symlink()`/`is_dir()`/`is_file()` as the second swallow
    site -- but that handler is *dead code* on this interpreter: `Path.is_file()`/`is_dir()` delegate
    to `os.path.isfile`/`isdir`, and `Path.is_symlink()` to `os.path.islink`, all three of which
    catch `OSError` themselves and return **False**. An entry that cannot be stat'ed therefore
    reaches the selector as `is_file=False` -- indistinguishable from a directory or a socket -- and
    is dropped with no exception raised anywhere, so collecting the unreadable set in that `except`
    clause would report nothing at all here.

    A directory with read but no execute permission (`0o600`) is the real-filesystem form of this:
    `iterdir()` succeeds (readdir needs `r`), every `stat()` on the entries fails with `EACCES`
    (which needs `x`) -- the same shape an NFS `ESTALE`/`EIO` on individual entries takes. Verified
    directly: under `0o600` all three predicates return False and raise nothing, while `lstat()`
    raises `PermissionError`, which is why the walker now stats each entry explicitly instead of
    asking three predicates that cannot fail."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)
    obsidian = vault_dir / ".obsidian"
    _write_plugin(obsidian, "plugin-a")
    denied = _write_plugin(obsidian, "plugin-b")
    denied.chmod(stat.S_IRUSR | stat.S_IWUSR)  # listable, but its entries cannot be stat'ed

    try:
        runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
        with caplog.at_level(logging.INFO):
            took_baseline = ensure_obsidian_baseline(runner, vault_dir)

        assert took_baseline is False
        staged = runner.run(["diff", "--cached", "--name-only"]).stdout.splitlines()
        assert not any(path.startswith(".obsidian/") for path in staged)

        refusal = _one_record_with_event(caplog, "baseline_refused_incomplete_walk")
        assert refusal.levelno == logging.WARNING
        assert set(getattr(refusal, "unreadable_paths")) == {  # noqa: B009 -- LogRecord attr
            str(denied / "main.js"),
            str(denied / "manifest.json"),
        }
    finally:
        denied.chmod(stat.S_IRWXU)  # tmp_path cleanup


def test_a_persistently_unreadable_directory_never_wedges_the_rest_of_the_run(
    tmp_path: Path, seeded_origin: Path, make_bare_repo: Callable[[], Path], vault_dir: Path
) -> None:
    """The refusal's own risk, tested rather than argued: refusing on *any* unreadable path means a
    permanently unreadable subtree under `.obsidian/` (one carrying nothing the allowlist would take,
    even) blocks the baseline forever. That is deliberate -- deciding an unreadable directory
    "couldn't have held anything allowlisted anyway" would mean re-deriving the allowlist's depth
    rules inside the walker, the exact re-derivation `baseline_selector.py`'s docstring forbids, and
    the two failure modes are asymmetric: over-refusal is loud (a warning naming the path, every
    run) and destroys nothing, while under-capture is silent and permanent. What must *not* happen
    is the #22-shaped wedge, where a `.obsidian/` problem stops vault content being committed at
    all: three consecutive cycles, new content each time, all committed."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)
    denied = _write_plugin(vault_dir / ".obsidian", "plugin-b")
    denied.chmod(0)

    try:
        for cycle in range(3):
            (vault_dir / f"new-note-{cycle}.md").write_text(f"# cycle {cycle}\n")
            runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
            assert ensure_obsidian_baseline(runner, vault_dir) is False
            stage_all(runner)
            assert has_staged_changes(runner)
            create_commit(runner, cycle_time=datetime.now(UTC))
            push_all(runner, branch="main")

        final_runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
        committed_tree = set(final_runner.list_tree_paths("HEAD"))
        assert {"new-note-0.md", "new-note-1.md", "new-note-2.md"} <= committed_tree
        assert not any(path.startswith(".obsidian") for path in committed_tree)
    finally:
        denied.chmod(stat.S_IRWXU)  # tmp_path cleanup


# --- the dropped-set diagnostic -------------------------------------------------------------------
#
# The allowlist in `baseline_selector.py` was arrived at by reasoning about what a device baseline
# needs, never by inspecting what is actually on the PVC — validated in one direction only. An
# expected file going missing would have been noticed; a file that *is* there, *does* matter and
# nobody thought of is dropped with no error and no warning. These tests pin the other direction.


def test_the_walk_logs_every_enumerated_path_the_allowlist_did_not_select(
    tmp_path: Path,
    seeded_origin: Path,
    make_bare_repo: Callable[[], Path],
    vault_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The dropped set is reported as the set difference against `select_baseline_paths`' own
    output, never by re-deriving "what would the allowlist have taken" here -- so it cannot drift
    from the selector, and it deliberately does not separate an allowlist miss from a safety
    exclusion (a symlink), since telling those apart would need exactly that re-derivation."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)  # app.json (selected) + workspace.json/workspaces.json (not)
    obsidian = vault_dir / ".obsidian"
    plugin_dir = _write_plugin(obsidian, "obsidian-local-rest-api")
    (plugin_dir / "data.json").write_text('{"apiKey": "never baselined"}\n')

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    with caplog.at_level(logging.INFO):
        took_baseline = ensure_obsidian_baseline(runner, vault_dir)

    assert took_baseline is True
    record = _one_record_with_event(caplog, "baseline_unselected_paths")
    assert set(getattr(record, "unselected_paths")) == {  # noqa: B009 -- LogRecord attr
        "workspace.json",
        "workspaces.json",
        "plugins/obsidian-local-rest-api/data.json",
    }
    assert getattr(record, "unselected_count") == 3  # noqa: B009 -- LogRecord attr


def test_the_dropped_set_is_still_reported_after_the_baseline_has_been_taken(
    tmp_path: Path,
    seeded_origin: Path,
    make_bare_repo: Callable[[], Path],
    vault_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole point of the diagnostic, and the reason it does not live inside the capture branch:
    the capture branch runs only until a baseline exists, which for the live vault already happened
    -- a diagnostic gated on it would never emit again, and could never answer the question for the
    deployment that actually has the question. Enumerating on every run instead means a plugin
    installed a year from now shows up in the next run's own logs, with no cluster access needed."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)
    obsidian = vault_dir / ".obsidian"

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    assert ensure_obsidian_baseline(runner, vault_dir) is True
    stage_all(runner)
    create_commit(runner, cycle_time=datetime.now(UTC))

    # A plugin is installed later, through the GUI, carrying a file nobody writing the allowlist
    # thought about.
    late_plugin = _write_plugin(obsidian, "some-new-plugin")
    (late_plugin / "keybindings.json").write_text('{"binding": "value"}\n')

    caplog.clear()
    runner_2 = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    with caplog.at_level(logging.INFO):
        took_baseline_again = ensure_obsidian_baseline(runner_2, vault_dir)

    assert took_baseline_again is False  # the reapply branch, i.e. the guard is already satisfied
    record = _one_record_with_event(caplog, "baseline_unselected_paths")
    assert "plugins/some-new-plugin/keybindings.json" in getattr(record, "unselected_paths")  # noqa: B009


def test_the_diagnostic_never_enumerates_through_a_symlinked_obsidian_root(
    tmp_path: Path,
    seeded_origin: Path,
    make_bare_repo: Callable[[], Path],
    vault_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The diagnostic runs on the reapply branch too, which is the one branch that previously never
    touched the work tree at all -- so it needs the same root guard the capture branch has
    (`ppat/obsidian-tools#22`). Without it, `.obsidian -> /somewhere/else` would make this walk
    enumerate an arbitrary external directory and print its contents into the committer's logs."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    assert ensure_obsidian_baseline(runner, vault_dir) is True
    stage_all(runner)
    create_commit(runner, cycle_time=datetime.now(UTC))

    shutil.rmtree(vault_dir / ".obsidian")
    external_target = tmp_path / "elsewhere"
    external_target.mkdir()
    (external_target / "private-outside-the-vault.json").write_text('{"secret": "never logged"}\n')
    (vault_dir / ".obsidian").symlink_to(external_target, target_is_directory=True)

    caplog.clear()
    runner_2 = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    with caplog.at_level(logging.INFO):
        assert ensure_obsidian_baseline(runner_2, vault_dir) is False

    assert _records_with_event(caplog, "baseline_unselected_paths") == []
    for record in caplog.records:
        assert "private-outside-the-vault.json" not in str(record.__dict__)


def test_the_logged_path_lists_are_capped(
    tmp_path: Path,
    seeded_origin: Path,
    make_bare_repo: Callable[[], Path],
    vault_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The list is built from whatever is on the volume, so its length is not this code's to choose.
    Loki drops a log line over its max line size outright rather than truncating it, which would
    lose the count as well as the sample exactly when the dropped set is most interesting -- so the
    sample is capped and the full count is carried separately."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)  # workspace.json + workspaces.json are unselected too
    extra = LOG_PATH_SAMPLE_LIMIT + 20
    for index in range(extra):
        (vault_dir / ".obsidian" / f"unknown-{index:04d}.json").write_text("{}\n")

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    with caplog.at_level(logging.INFO):
        ensure_obsidian_baseline(runner, vault_dir)

    record = _one_record_with_event(caplog, "baseline_unselected_paths")
    assert getattr(record, "unselected_count") == extra + 2  # noqa: B009 -- LogRecord attr
    assert len(getattr(record, "unselected_paths")) == LOG_PATH_SAMPLE_LIMIT  # noqa: B009


def test_an_unreadable_walk_is_reported_as_such_even_when_nothing_allowlisted_was_found(
    tmp_path: Path,
    seeded_origin: Path,
    make_bare_repo: Callable[[], Path],
    vault_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pins the order the two skips are checked in. "No allowlisted paths are present" and "part of
    the walk failed" produce the same empty-handed outcome, and the second is frequently the *cause*
    of the first -- the allowlisted paths may be sitting in the directory that could not be read. An
    operator told "nothing allowlisted has landed yet" goes and waits for Obsidian to seed plugins;
    the same operator told a path could not be read goes and looks at the NFS mount. The unreadable
    check therefore comes first, and the routine skip means what it says: the walk was complete and
    found nothing."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    obsidian = vault_dir / ".obsidian"
    obsidian.mkdir()
    denied = _write_plugin(obsidian, "plugin-b")  # the only allowlistable content, and unreadable
    denied.chmod(0)

    try:
        runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
        with caplog.at_level(logging.INFO):
            assert ensure_obsidian_baseline(runner, vault_dir) is False

        events = [getattr(record, "event", None) for record in caplog.records]
        assert "baseline_refused_incomplete_walk" in events
        assert "baseline_skip_no_allowlisted_paths" not in events
    finally:
        denied.chmod(stat.S_IRWXU)  # tmp_path cleanup


def test_the_unreadable_path_list_is_capped_too(
    tmp_path: Path,
    seeded_origin: Path,
    make_bare_repo: Callable[[], Path],
    vault_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The same cap, on the other list, tested separately because the two reach it by different
    routes: one unreadable *directory* contributes one entry, but a directory whose entries cannot
    be stat'ed individually contributes one per entry, so the refusal's own list is just as
    unbounded as the diagnostic's."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)
    denied = vault_dir / ".obsidian" / "plugins" / "plugin-b"
    denied.mkdir(parents=True)
    entries = LOG_PATH_SAMPLE_LIMIT + 20
    for index in range(entries):
        (denied / f"chunk-{index:04d}.js").write_text("// plugin code\n")
    denied.chmod(stat.S_IRUSR | stat.S_IWUSR)  # listable, but its entries cannot be stat'ed

    try:
        runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
        with caplog.at_level(logging.INFO):
            assert ensure_obsidian_baseline(runner, vault_dir) is False

        refusal = _one_record_with_event(caplog, "baseline_refused_incomplete_walk")
        assert getattr(refusal, "unreadable_count") == entries  # noqa: B009 -- LogRecord attr
        assert len(getattr(refusal, "unreadable_paths")) == LOG_PATH_SAMPLE_LIMIT  # noqa: B009
    finally:
        denied.chmod(stat.S_IRWXU)  # tmp_path cleanup


def test_a_dangling_symlink_is_not_treated_as_a_read_failure(
    tmp_path: Path,
    seeded_origin: Path,
    make_bare_repo: Callable[[], Path],
    vault_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The risk the refusal introduces, pinned so a later "tighten the stat" change cannot reopen
    it. A symlink whose target does not exist is a legible state of the vault, not an I/O failure —
    a plugin uninstalled out from under a link, say — and it is already handled correctly by being
    excluded from the capture. If the walker stat'ed each entry *through* the link (`stat()` rather
    than `lstat()`), a dangling one would raise `ENOENT`, land in the unreadable list, and refuse the
    baseline forever on a condition no retry can clear."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)
    (vault_dir / ".obsidian" / "snippets").mkdir()
    (vault_dir / ".obsidian" / "snippets" / "gone.css").symlink_to(tmp_path / "never-existed")

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    with caplog.at_level(logging.INFO):
        took_baseline = ensure_obsidian_baseline(runner, vault_dir)

    assert took_baseline is True
    assert _records_with_event(caplog, "baseline_refused_incomplete_walk") == []
    staged = runner.run(["diff", "--cached", "--name-only"]).stdout.splitlines()
    assert ".obsidian/app.json" in staged
    assert ".obsidian/snippets/gone.css" not in staged
    diagnostic = _one_record_with_event(caplog, "baseline_unselected_paths")
    assert "snippets/gone.css" in getattr(diagnostic, "unselected_paths")  # noqa: B009 -- LogRecord attr


def _walk_log_payload(record: logging.LogRecord) -> dict[str, object]:
    return {
        field: getattr(record, field)
        for field in ("unselected_count", "unselected_paths", "unreadable_count", "unreadable_paths")
    }


def test_an_unreadable_subtree_is_reported_on_the_reapply_branch_too(
    tmp_path: Path,
    seeded_origin: Path,
    make_bare_repo: Callable[[], Path],
    vault_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """BROKEN, reproduced (independent review of this change): the diagnostic reported only
    `unselected`, and dropped the walk's `unreadable` list on the floor. On a vault that already has
    a baseline — every deployed vault — the reapply branch is the *only* branch that ever runs, so
    this record is the only thing the committer says about `.obsidian/` at all. With a subtree
    unreadable, it read `unselected_count: 0, unselected_paths: []`: byte-identical to the healthy
    "everything is captured" answer, with the unreadable subtree and everything under it invisible.

    That is the same failure shape this change exists to remove — a mechanism reporting a complete
    answer while having dropped data — reintroduced in the mechanism that removes it. Nothing else
    covers the gap either: `.obsidian` is pruned by the git-dir exclude rule, so `git add -A` never
    descends into it and `commands/commit.py`'s `VAULT_READ_FAILURE` classification is unreachable
    for these paths.

    So the two answers must be distinguishable, which is what this asserts directly rather than by
    checking fields one at a time. The level differs too: on a baselined vault this warning is the
    only signal that will ever exist for an unreadable `.obsidian/`, and an operator scanning for
    something wrong must not have to read every `info` line to find it."""
    nas = make_bare_repo()
    git_dir = tmp_path / "git-dir"
    _write_obsidian_dir(vault_dir)
    obsidian = vault_dir / ".obsidian"
    plugin_dir = _write_plugin(obsidian, "dataview")
    (plugin_dir / "data.json").write_text('{"never": "selected"}\n')

    runner = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    assert ensure_obsidian_baseline(runner, vault_dir) is True
    stage_all(runner)
    create_commit(runner, cycle_time=datetime.now(UTC))

    caplog.clear()
    runner_healthy = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
    with caplog.at_level(logging.INFO):
        assert ensure_obsidian_baseline(runner_healthy, vault_dir) is False
    healthy = _one_record_with_event(caplog, "baseline_unselected_paths")

    plugins_dir = obsidian / "plugins"
    plugins_dir.chmod(0)
    try:
        caplog.clear()
        runner_degraded = _provision(git_dir, vault_dir, origin_url=str(seeded_origin), nas_url=str(nas))
        with caplog.at_level(logging.INFO):
            assert ensure_obsidian_baseline(runner_degraded, vault_dir) is False
        degraded = _one_record_with_event(caplog, "baseline_unselected_paths")

        assert _walk_log_payload(degraded) != _walk_log_payload(healthy), (
            "the degraded walk reported the same thing as the healthy one; an unreadable subtree is invisible"
        )
        assert getattr(degraded, "unreadable_paths") == [str(plugins_dir)]  # noqa: B009 -- LogRecord attr
        assert getattr(degraded, "unreadable_count") == 1  # noqa: B009 -- LogRecord attr
        assert degraded.levelno == logging.WARNING
        assert healthy.levelno == logging.INFO
        assert getattr(healthy, "unreadable_count") == 0  # noqa: B009 -- LogRecord attr
    finally:
        plugins_dir.chmod(stat.S_IRWXU)  # tmp_path cleanup
