"""Tests for `obsidian_tools/vault_git/baseline_selector.py` — the pure `.obsidian/` baseline
allowlist, tested as a pure function over hand-built (or generated) candidate lists rather than
against a real filesystem.

Why this file exists, and why it looks different from `test_vault_git_baseline.py`: the bug class
this module was built to end (`ppat/obsidian-tools#22`) is "which paths does this pattern match?" —
a question best answered by enumerating adversarial candidates cheaply, not by building one real
directory tree per case. `test_vault_git_baseline.py` keeps the real-filesystem, real-git
integration tests (they still matter — the walker and the git plumbing around the selector need
exactly as much proof as the selector itself); this file is where the volume of adversarial cases
lives.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from obsidian_tools.vault_git.baseline_selector import PathInfo, select_baseline_paths

# Basenames shaped like they hold a credential or per-instance secret/state -- never selected no
# matter where they appear. Not an exhaustive list of every dangerous name; a representative sample
# spanning the concrete leak this module exists to prevent (`data.json`) and adjacent shapes.
_CREDENTIAL_SHAPED_BASENAMES = ("data.json", "secrets.json", ".env", "credentials.json", "creds.json")

_GLOB_METACHAR_BASENAMES = ("weird[1].css", "star*.css", "question?.css")


def _select(*infos: PathInfo) -> list[str]:
    return select_baseline_paths(infos)


# --- top-level files -------------------------------------------------------------------------


def test_allowlisted_top_level_file_is_selected() -> None:
    assert _select(PathInfo("app.json", is_file=True, is_symlink=False)) == ["app.json"]


def test_non_allowlisted_top_level_file_is_excluded() -> None:
    for basename in _CREDENTIAL_SHAPED_BASENAMES:
        assert _select(PathInfo(basename, is_file=True, is_symlink=False)) == []


def test_top_level_symlink_is_excluded_even_with_an_allowlisted_name() -> None:
    assert _select(PathInfo("app.json", is_file=True, is_symlink=True)) == []


def test_top_level_directory_is_never_selected_even_with_an_allowlisted_name() -> None:
    assert _select(PathInfo("app.json", is_file=False, is_symlink=False)) == []


# --- themes/ and snippets/ ---------------------------------------------------------------------


def test_css_at_depth_one_is_selected_under_each_baseline_dir() -> None:
    for branch in ("themes", "snippets"):
        path = f"{branch}/custom.css"
        assert _select(PathInfo(path, is_file=True, is_symlink=False)) == [path]


def test_css_at_arbitrary_depth_is_selected_under_each_baseline_dir() -> None:
    for branch in ("themes", "snippets"):
        path = f"{branch}/deep/nested/inner/style.css"
        assert _select(PathInfo(path, is_file=True, is_symlink=False)) == [path]


def test_non_css_data_json_at_any_depth_is_excluded_under_each_baseline_dir() -> None:
    for branch in ("themes", "snippets"):
        for depth_path in (f"{branch}/data.json", f"{branch}/sub/dir/creds.json"):
            assert _select(PathInfo(depth_path, is_file=True, is_symlink=False)) == []


def test_symlinked_css_is_excluded_under_each_baseline_dir() -> None:
    for branch in ("themes", "snippets"):
        path = f"{branch}/escape.css"
        assert _select(PathInfo(path, is_file=True, is_symlink=True)) == []


def test_theme_manifest_at_depth_one_is_selected() -> None:
    path = "themes/Minimal/manifest.json"
    assert _select(PathInfo(path, is_file=True, is_symlink=False)) == [path]


def test_theme_manifest_below_depth_one_is_excluded() -> None:
    """`#3`/`docs/settings-lock.md` both specify *a theme's own* manifest.json -- depth 1 relative
    to the theme's own directory, never a manifest found deeper by a recursive search."""
    path = "themes/Minimal/nested/manifest.json"
    assert _select(PathInfo(path, is_file=True, is_symlink=False)) == []


def test_symlinked_theme_manifest_is_excluded() -> None:
    path = "themes/Minimal/manifest.json"
    assert _select(PathInfo(path, is_file=True, is_symlink=True)) == []


# --- plugins/ --------------------------------------------------------------------------------


def test_plugin_code_files_at_depth_two_are_selected() -> None:
    for filename in ("manifest.json", "main.js", "styles.css"):
        path = f"plugins/obsidian-local-rest-api/{filename}"
        assert _select(PathInfo(path, is_file=True, is_symlink=False)) == [path]


def test_plugin_data_json_is_never_selected() -> None:
    """Regression case for the vulnerability this whole allowlist design exists to prevent
    (ppat/obsidian-tools#3): a plugin's `data.json` -- the Local REST API plugin's bearer token
    lives at exactly this kind of path -- must never be captured, for any plugin."""
    path = "plugins/obsidian-local-rest-api/data.json"
    assert _select(PathInfo(path, is_file=True, is_symlink=False)) == []


def test_plugin_code_file_below_depth_two_is_excluded() -> None:
    path = "plugins/obsidian-local-rest-api/nested/manifest.json"
    assert _select(PathInfo(path, is_file=True, is_symlink=False)) == []


def test_symlinked_plugin_code_file_is_excluded() -> None:
    """The security-shaped hole (`#22`): `manifest.json -> /etc/passwd` inside a real, non-symlink
    plugin directory used to be captured -- `is_file()` follows a symlink to a real target, and the
    plugins loop never checked `is_symlink()` at all, unlike the themes/snippets loops."""
    path = "plugins/obsidian-local-rest-api/manifest.json"
    assert _select(PathInfo(path, is_file=True, is_symlink=True)) == []


def test_symlinked_plugin_directory_contents_are_excluded_even_if_a_candidate_is_produced() -> None:
    """The wedging hole (`#22`): a symlinked plugin *directory* (the standard local
    plugin-development layout, `plugins/my-plugin -> ~/dev/my-plugin`). The real walker
    (`baseline._iter_obsidian_candidates`) never produces a candidate for anything beneath a
    symlinked directory at all, so this case can't arise from a real filesystem walk today -- but
    this selector is the second, independent line of defence, not a function that gets to assume
    its caller is bug-free forever. If a future rewrite of the walker (back to `rglob`, say)
    reintroduced following symlinked directories and propagated `is_symlink=True` down to what it
    finds underneath, this proves the selector alone would still refuse it."""
    path = "plugins/my-plugin/manifest.json"
    assert _select(PathInfo(path, is_file=True, is_symlink=True)) == []


def test_plugin_directory_itself_is_never_selected() -> None:
    assert _select(PathInfo("plugins/my-plugin", is_file=False, is_symlink=True)) == []
    assert _select(PathInfo("plugins/my-plugin", is_file=False, is_symlink=False)) == []


# --- cross-cutting: names with spaces, non-ASCII, and glob metacharacters --------------------


def test_spaces_and_non_ascii_basenames_are_selected_when_otherwise_allowlisted() -> None:
    for basename in ("my theme.css", "日本語.css", "café.css"):
        path = f"themes/Minimal/{basename}"
        assert _select(PathInfo(path, is_file=True, is_symlink=False)) == [path]


def test_glob_metacharacter_basenames_are_selected_as_literal_names_when_otherwise_allowlisted() -> None:
    """The selector matches by string comparison only -- `*`/`[`/`?` in an actual filename are not
    special to it. Whether a returned path is later safe to hand to `git add` as a *pathspec*
    despite containing these characters is `baseline.py`'s concern (`:(literal)` pathspec magic),
    not this pure function's."""
    for basename in _GLOB_METACHAR_BASENAMES:
        path = f"snippets/{basename}"
        assert _select(PathInfo(path, is_file=True, is_symlink=False)) == [path]


def test_paths_differing_only_by_depth_are_judged_independently() -> None:
    shallow = PathInfo("themes/Minimal/manifest.json", is_file=True, is_symlink=False)
    deep = PathInfo("themes/Minimal/nested/manifest.json", is_file=True, is_symlink=False)
    assert select_baseline_paths([shallow, deep]) == ["themes/Minimal/manifest.json"]


def test_mixed_candidate_list_selects_only_the_allowlisted_non_symlink_entries() -> None:
    """One representative candidate per branch, good and bad, in a single call -- proving the
    branches don't interfere with each other's decisions."""
    candidates = [
        PathInfo("app.json", is_file=True, is_symlink=False),  # top-level: keep
        PathInfo("workspace.json", is_file=True, is_symlink=False),  # top-level: not allowlisted
        PathInfo("themes/Minimal/theme.css", is_file=True, is_symlink=False),  # themes css: keep
        PathInfo("themes/Minimal/data.json", is_file=True, is_symlink=False),  # themes: not allowlisted
        PathInfo("themes/Minimal/manifest.json", is_file=True, is_symlink=True),  # themes manifest: symlink
        PathInfo("snippets/custom.css", is_file=True, is_symlink=False),  # snippets css: keep
        PathInfo("plugins/obsidian-local-rest-api/main.js", is_file=True, is_symlink=False),  # plugin code: keep
        PathInfo("plugins/obsidian-local-rest-api/data.json", is_file=True, is_symlink=False),  # plugin state
    ]
    assert select_baseline_paths(candidates) == [
        "app.json",
        "plugins/obsidian-local-rest-api/main.js",
        "snippets/custom.css",
        "themes/Minimal/theme.css",
    ]


# --- property: a denylist asserted against an allowlist implementation -----------------------

# Deliberately narrow strategies -- the point of this property is the *invariant* it checks, not
# broad fuzzing of every possible path shape (already covered by the table-driven cases above).
_branch_names = st.sampled_from(("app.json", "appearance.json", "core-plugins.json", "themes", "snippets", "plugins"))
_basenames = st.sampled_from(
    (
        "app.json",
        "theme.css",
        "manifest.json",
        "main.js",
        "styles.css",
        *_CREDENTIAL_SHAPED_BASENAMES,
        *_GLOB_METACHAR_BASENAMES,
    )
)
_middle_components = st.sampled_from(("Minimal", "obsidian-local-rest-api", "sub", "deep/nested"))
_depths = st.integers(min_value=0, max_value=3)


@st.composite
def _path_infos(draw: st.DrawFn) -> PathInfo:
    top = draw(_branch_names)
    if top in ("themes", "snippets", "plugins"):
        middle = draw(_middle_components)
        extra_depth = draw(_depths)
        basename = draw(_basenames)
        parts = [top, middle] + ["nested"] * extra_depth + [basename]
        relative_path = "/".join(parts)
    else:
        relative_path = top
    is_symlink = draw(st.booleans())
    is_file = draw(st.booleans())
    return PathInfo(relative_path=relative_path, is_file=is_file, is_symlink=is_symlink)


@given(st.lists(_path_infos(), max_size=25, unique_by=lambda info: info.relative_path))
def test_selected_paths_are_never_symlinks_and_never_credential_shaped(candidates: list[PathInfo]) -> None:
    """The safety invariant, phrased independently of the allowlist implementation: no matter what
    tree is generated, nothing selected is a symlink, nothing selected is a non-file (a directory),
    and nothing selected has a basename shaped like a credential or per-instance secret. Asserting a
    denylist-shaped property against an allowlist-shaped implementation is two independent
    expressions of the same intent -- which is what makes this worth having rather than a
    tautological restatement of `_is_allowlisted`."""
    by_path = {candidate.relative_path: candidate for candidate in candidates}

    for selected_path in select_baseline_paths(candidates):
        candidate = by_path[selected_path]
        assert candidate.is_file, f"{selected_path} is not a regular file and must never be selected"
        assert not candidate.is_symlink, f"{selected_path} is a symlink and must never be selected"
        basename = selected_path.rsplit("/", 1)[-1]
        assert basename not in _CREDENTIAL_SHAPED_BASENAMES, (
            f"{selected_path} has a credential-shaped basename and must never be selected"
        )
