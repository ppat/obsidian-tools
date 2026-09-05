from __future__ import annotations

import pytest

from obsidian_tools.config import CommitConfig, ConfigError, DrainConfig, ReplicateConfig, VaultExporterConfig
from obsidian_tools.vault_git.commit import DEFAULT_MAX_DELETION_FRACTION


def test_from_env_applies_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OBSIDIAN_GIT_DIR", raising=False)
    monkeypatch.delenv("OBSIDIAN_VAULT_DIR", raising=False)
    monkeypatch.delenv("GIT_COMMIT_BRANCH", raising=False)
    monkeypatch.delenv("GIT_COMMIT_MAX_DELETION_FRACTION", raising=False)
    monkeypatch.delenv("GIT_SSH_KNOWN_HOSTS_PATH", raising=False)
    monkeypatch.delenv("GIT_SSH_KNOWN_HOSTS_EXTRA", raising=False)
    monkeypatch.setenv("GIT_REMOTE_ORIGIN_URL", "git@github.com:ppat/obsidian-vault.git")
    monkeypatch.setenv("GIT_REMOTE_NAS_URL", "git@nas:vault.git")

    config = CommitConfig.from_env()

    assert config.git_dir == "/git/vault.git"
    assert config.vault_dir == "/vault/brain"
    assert config.branch == "main"
    assert config.author_name == "brain-committer"
    assert config.max_deletion_fraction == DEFAULT_MAX_DELETION_FRACTION
    assert config.nas_url == "git@nas:vault.git"
    assert config.ssh_known_hosts_path.endswith("/.ssh/known_hosts")
    assert config.ssh_known_hosts_extra == ""


def test_from_env_nas_url_is_optional_and_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """The NAS is a second push target for independence insurance, not required for the committer
    to do its primary job (ADR-0029) -- an operator without the NAS's SSH access,
    authorized_keys entry, bare repo and host key set up yet must still be able to run this
    component. Regression test for `GIT_REMOTE_NAS_URL` going from `require_env` to optional."""
    monkeypatch.setenv("GIT_REMOTE_ORIGIN_URL", "git@github.com:ppat/obsidian-vault.git")
    monkeypatch.delenv("GIT_REMOTE_NAS_URL", raising=False)

    config = CommitConfig.from_env()

    assert config.nas_url is None


def test_from_env_treats_an_empty_nas_url_the_same_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_REMOTE_ORIGIN_URL", "git@github.com:ppat/obsidian-vault.git")
    monkeypatch.setenv("GIT_REMOTE_NAS_URL", "")

    config = CommitConfig.from_env()

    assert config.nas_url is None


def test_from_env_ssh_known_hosts_extra_is_overridable_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_REMOTE_ORIGIN_URL", "git@github.com:ppat/obsidian-vault.git")
    monkeypatch.delenv("GIT_REMOTE_NAS_URL", raising=False)
    monkeypatch.setenv("GIT_SSH_KNOWN_HOSTS_EXTRA", "nas.example.invalid ssh-ed25519 AAAA...\n")

    config = CommitConfig.from_env()

    assert config.ssh_known_hosts_extra == "nas.example.invalid ssh-ed25519 AAAA...\n"


def test_max_deletion_fraction_is_overridable_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mass-deletion guard's threshold used to have no env var reaching it at all -- a genuine
    archive purge had no way past it short of editing source. Regression test for the wiring."""
    monkeypatch.setenv("GIT_REMOTE_ORIGIN_URL", "git@github.com:ppat/obsidian-vault.git")
    monkeypatch.setenv("GIT_REMOTE_NAS_URL", "git@nas:vault.git")
    monkeypatch.setenv("GIT_COMMIT_MAX_DELETION_FRACTION", "1.0")

    config = CommitConfig.from_env()

    assert config.max_deletion_fraction == 1.0


def test_max_deletion_fraction_rejects_a_non_numeric_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_REMOTE_ORIGIN_URL", "git@github.com:ppat/obsidian-vault.git")
    monkeypatch.setenv("GIT_REMOTE_NAS_URL", "git@nas:vault.git")
    monkeypatch.setenv("GIT_COMMIT_MAX_DELETION_FRACTION", "not-a-number")

    with pytest.raises(ConfigError, match="GIT_COMMIT_MAX_DELETION_FRACTION"):
        CommitConfig.from_env()


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


def test_replicate_config_applies_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OBSIDIAN_CACHE_CLONE_DIR", raising=False)
    monkeypatch.delenv("GIT_COMMIT_BRANCH", raising=False)
    monkeypatch.delenv("LOCAL_REPLICATOR_SPOOL_DIR", raising=False)
    icloud_path = "/Users/operator/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/BRAIN"
    monkeypatch.setenv("ICLOUD_VAULT_DIR", icloud_path)
    monkeypatch.setenv("GIT_REMOTE_ORIGIN_URL", "git@github.com:ppat/obsidian-vault.git")

    config = ReplicateConfig.from_env()

    assert config.cache_clone_dir.endswith("/.cache/obsidian-vault")
    assert config.branch == "main"
    assert config.icloud_vault_dir.endswith("/Obsidian/BRAIN")
    assert config.spool_dir.endswith("/obsidian-tools/local-replicator/spool")


def test_replicate_config_requires_icloud_vault_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ICLOUD_VAULT_DIR", raising=False)
    monkeypatch.setenv("GIT_REMOTE_ORIGIN_URL", "git@github.com:ppat/obsidian-vault.git")

    with pytest.raises(ConfigError, match="ICLOUD_VAULT_DIR"):
        ReplicateConfig.from_env()


def test_replicate_config_requires_origin_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ICLOUD_VAULT_DIR", "/Users/operator/iCloud/Obsidian/BRAIN")
    monkeypatch.delenv("GIT_REMOTE_ORIGIN_URL", raising=False)

    with pytest.raises(ConfigError, match="GIT_REMOTE_ORIGIN_URL"):
        ReplicateConfig.from_env()


def test_replicate_config_spool_dir_is_overridable_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ICLOUD_VAULT_DIR", "/Users/operator/iCloud/Obsidian/BRAIN")
    monkeypatch.setenv("GIT_REMOTE_ORIGIN_URL", "git@github.com:ppat/obsidian-vault.git")
    monkeypatch.setenv("LOCAL_REPLICATOR_SPOOL_DIR", "/custom/spool/path")

    config = ReplicateConfig.from_env()

    assert config.spool_dir == "/custom/spool/path"


def test_drain_config_applies_default_spool_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCAL_REPLICATOR_SPOOL_DIR", raising=False)

    config = DrainConfig.from_env()

    assert config.spool_dir.endswith("/obsidian-tools/local-replicator/spool")


def test_drain_config_shares_the_same_env_var_and_default_as_replicate_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two configs are deliberately separate dataclasses (config.py's `DrainConfig` docstring)
    but must still agree on where the spool lives without importing one from the other -- proven
    here by asserting they produce the identical path from the identical environment."""
    monkeypatch.delenv("LOCAL_REPLICATOR_SPOOL_DIR", raising=False)
    monkeypatch.setenv("ICLOUD_VAULT_DIR", "/Users/operator/iCloud/Obsidian/BRAIN")
    monkeypatch.setenv("GIT_REMOTE_ORIGIN_URL", "git@github.com:ppat/obsidian-vault.git")

    assert ReplicateConfig.from_env().spool_dir == DrainConfig.from_env().spool_dir

    monkeypatch.setenv("LOCAL_REPLICATOR_SPOOL_DIR", "/custom/shared/spool")

    assert ReplicateConfig.from_env().spool_dir == DrainConfig.from_env().spool_dir == "/custom/shared/spool"


def test_vault_exporter_config_applies_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OBSIDIAN_BASE_URL", raising=False)
    monkeypatch.delenv("OBSIDIAN_VERIFY_SSL", raising=False)
    monkeypatch.delenv("VAULT_EXPORTER_POLL_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("VAULT_EXPORTER_REQUEST_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("VAULT_EXPORTER_LISTEN_HOST", raising=False)
    monkeypatch.delenv("VAULT_EXPORTER_LISTEN_PORT", raising=False)
    monkeypatch.setenv("OBSIDIAN_API_KEY", "test-token")

    config = VaultExporterConfig.from_env()

    assert config.obsidian_base_url == "https://obsidian.obsidian-vault.svc.cluster.local:27124"
    assert config.obsidian_api_key == "test-token"
    assert config.verify_tls is False
    assert config.poll_interval_seconds == 60.0
    assert config.request_timeout_seconds == 10.0
    assert config.listen_host == "0.0.0.0"  # Prometheus scrapes this pod over the pod network
    assert config.listen_port == 9877


def test_vault_exporter_config_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OBSIDIAN_API_KEY", raising=False)

    with pytest.raises(ConfigError, match="OBSIDIAN_API_KEY"):
        VaultExporterConfig.from_env()


def test_vault_exporter_config_env_var_names_match_mcp_obsidian_agents_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for the deliberate naming choice (VaultExporterConfig's own docstring): this
    subcommand reuses OBSIDIAN_BASE_URL/OBSIDIAN_API_KEY/OBSIDIAN_VERIFY_SSL rather than an
    exporter-prefixed set of its own, so the manifest wiring this container can copy
    mcp-obsidian-agent's Deployment env block verbatim."""
    monkeypatch.setenv("OBSIDIAN_BASE_URL", "https://obsidian.obsidian-vault.svc.cluster.local:27124")
    monkeypatch.setenv("OBSIDIAN_API_KEY", "shared-token")
    monkeypatch.setenv("OBSIDIAN_VERIFY_SSL", "false")

    config = VaultExporterConfig.from_env()

    assert config.obsidian_base_url == "https://obsidian.obsidian-vault.svc.cluster.local:27124"
    assert config.obsidian_api_key == "shared-token"
    assert config.verify_tls is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("true", True, id="true"),
        pytest.param("TRUE", True, id="true-uppercase"),
        pytest.param("1", True, id="one"),
        pytest.param("yes", True, id="yes"),
        pytest.param("false", False, id="false"),
        pytest.param("0", False, id="zero"),
        pytest.param("no", False, id="no"),
    ],
)
def test_vault_exporter_config_verify_tls_is_overridable_via_env(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
) -> None:
    monkeypatch.setenv("OBSIDIAN_API_KEY", "test-token")
    monkeypatch.setenv("OBSIDIAN_VERIFY_SSL", raw)

    assert VaultExporterConfig.from_env().verify_tls is expected


def test_vault_exporter_config_rejects_an_unparseable_verify_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBSIDIAN_API_KEY", "test-token")
    monkeypatch.setenv("OBSIDIAN_VERIFY_SSL", "not-a-bool")

    with pytest.raises(ConfigError, match="OBSIDIAN_VERIFY_SSL"):
        VaultExporterConfig.from_env()


def test_vault_exporter_config_rejects_an_unparseable_poll_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBSIDIAN_API_KEY", "test-token")
    monkeypatch.setenv("VAULT_EXPORTER_POLL_INTERVAL_SECONDS", "soon")

    with pytest.raises(ConfigError, match="VAULT_EXPORTER_POLL_INTERVAL_SECONDS"):
        VaultExporterConfig.from_env()


def test_vault_exporter_config_rejects_an_unparseable_listen_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBSIDIAN_API_KEY", "test-token")
    monkeypatch.setenv("VAULT_EXPORTER_LISTEN_PORT", "not-a-port")

    with pytest.raises(ConfigError, match="VAULT_EXPORTER_LISTEN_PORT"):
        VaultExporterConfig.from_env()


def test_vault_exporter_config_listen_port_is_overridable_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBSIDIAN_API_KEY", "test-token")
    monkeypatch.setenv("VAULT_EXPORTER_LISTEN_PORT", "9100")

    assert VaultExporterConfig.from_env().listen_port == 9100
