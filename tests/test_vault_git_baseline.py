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

import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import make_runner, run_git

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
    are third-party code installed through the ungated GUI path (docs/DESIGN.md Sec 1.3 P8), so
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
    """LOW (`#22`): `_baseline_paths`'s docstring used to claim every returned path is safe to hand
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
