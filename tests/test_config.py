from __future__ import annotations

import pytest

from obsidian_tools.config import CommitConfig, ConfigError


def test_from_env_applies_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OBSIDIAN_GIT_DIR", raising=False)
    monkeypatch.delenv("OBSIDIAN_VAULT_DIR", raising=False)
    monkeypatch.delenv("GIT_COMMIT_BRANCH", raising=False)
    monkeypatch.setenv("GIT_REMOTE_ORIGIN_URL", "git@github.com:ppat/obsidian-vault.git")
    monkeypatch.setenv("GIT_REMOTE_NAS_URL", "git@nas:vault.git")

    config = CommitConfig.from_env()

    assert config.git_dir == "/git/vault.git"
    assert config.vault_dir == "/vault/brain"
    assert config.branch == "main"
    assert config.author_name == "brain-committer"


def test_author_name_env_var_does_not_collide_with_gits_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """GIT_AUTHOR_NAME/GIT_AUTHOR_EMAIL are git's own reserved environment variables — an operator
    setting them for git's sake must not silently reconfigure this tool's author identity too."""
    monkeypatch.setenv("GIT_REMOTE_ORIGIN_URL", "git@github.com:ppat/obsidian-vault.git")
    monkeypatch.setenv("GIT_REMOTE_NAS_URL", "git@nas:vault.git")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "some unrelated git invocation")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "unrelated@example.invalid")
    monkeypatch.delenv("GIT_COMMIT_AUTHOR_NAME", raising=False)
    monkeypatch.delenv("GIT_COMMIT_AUTHOR_EMAIL", raising=False)

    config = CommitConfig.from_env()

    assert config.author_name == "brain-committer"
    assert config.author_email == "brain-committer@noreply.invalid"


def test_from_env_requires_origin_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIT_REMOTE_ORIGIN_URL", raising=False)
    monkeypatch.setenv("GIT_REMOTE_NAS_URL", "git@nas:vault.git")

    with pytest.raises(ConfigError, match="GIT_REMOTE_ORIGIN_URL"):
        CommitConfig.from_env()


def test_from_env_requires_nas_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_REMOTE_ORIGIN_URL", "git@github.com:ppat/obsidian-vault.git")
    monkeypatch.delenv("GIT_REMOTE_NAS_URL", raising=False)

    with pytest.raises(ConfigError, match="GIT_REMOTE_NAS_URL"):
        CommitConfig.from_env()
