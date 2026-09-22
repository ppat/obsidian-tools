from __future__ import annotations

import pytest

from obsidian_tools.config import (
    BatchProcessorConfig,
    CommitConfig,
    ConfigError,
    DrainConfig,
    LintPassConfig,
    ReplicateConfig,
    VaultExporterConfig,
    WatchdogConfig,
)
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


def _batch_processor_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "BATCH_NATS_URL": "nats://broker:4222",
        "BATCH_PROCESSOR_NATS_PASSWORD": "pw",
        "BATCH_MCP_URL": "https://gateway/mcp",
        "BATCH_MCP_API_KEY": "sk-key",
        "BATCH_MCP_TOOL_READ": "read",
        "BATCH_MCP_TOOL_WRITE": "write",
        "BATCH_MCP_TOOL_DELETE": "delete",
        "BATCH_KUBERNETES_API_URL": "https://kubernetes.example:6443",
        "BATCH_AGENT_INSTANCE_NAMESPACE": "obsidian-vault",
        "BATCH_AGENT_INSTANCE_DEPLOYMENT": "mcp-obsidian-agent",
        "BATCH_AGENT_INSTANCE_LEASE": "batch-mode",
    }.items():
        monkeypatch.setenv(name, value)


def test_batch_processor_config_has_no_promotion_stream_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """`promotion-processor` is a separate, unbuilt unit, so a run today has nothing to yield to.
    The absence is a declared state the run reports once, not a silent default — red if a value
    were invented here, because the run would then fail against a stream that does not exist."""
    _batch_processor_env(monkeypatch)
    monkeypatch.delenv("BATCH_PROMOTION_STREAM", raising=False)

    assert BatchProcessorConfig.from_env().promotion_stream is None


def test_batch_processor_config_refuses_a_dead_letter_subject_inside_the_batch_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead-lettered chunk republished onto the stream it was taken from is re-consumed with a
    fresh delivery count — an infinite, silent loop. Red if this were left to be noticed at runtime:
    by then the loop is already running and every chunk behind it is stuck."""
    _batch_processor_env(monkeypatch)
    monkeypatch.setenv("BATCH_SUBJECT_PREFIX", "batch")
    monkeypatch.setenv("BATCH_DEAD_LETTER_SUBJECT_PREFIX", "batch.dead")

    with pytest.raises(ConfigError, match="overlaps"):
        BatchProcessorConfig.from_env()


@pytest.mark.parametrize("missing", ["BATCH_MCP_TOOL_READ", "BATCH_MCP_TOOL_WRITE", "BATCH_MCP_TOOL_DELETE"])
def test_batch_processor_config_requires_every_mcp_tool_name(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    """No defaults, deliberately: this repository has never run against the deployed MCP surface,
    and a guessed tool name presents identically to a gate refusal at every call site. Red if a
    default appeared — the deployment's own setting would silently stop mattering."""
    _batch_processor_env(monkeypatch)
    monkeypatch.delenv(missing, raising=False)

    with pytest.raises(ConfigError, match=missing):
        BatchProcessorConfig.from_env()


def test_the_watchdog_config_needs_nothing_but_the_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the watchdog watches is precisely what may be broken, so it must not need the broker or
    the MCP surface to start. Red if it grew a dependency on either."""
    for name in ("BATCH_NATS_URL", "BATCH_PROCESSOR_NATS_PASSWORD", "BATCH_MCP_URL", "BATCH_MCP_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BATCH_KUBERNETES_API_URL", "https://kubernetes.example:6443")
    monkeypatch.setenv("BATCH_AGENT_INSTANCE_NAMESPACE", "obsidian-vault")
    monkeypatch.setenv("BATCH_AGENT_INSTANCE_DEPLOYMENT", "mcp-obsidian-agent")
    monkeypatch.setenv("BATCH_AGENT_INSTANCE_LEASE", "batch-mode")

    assert WatchdogConfig.from_env().agent_instance.deployment == "mcp-obsidian-agent"


@pytest.mark.parametrize(
    "missing",
    ["BATCH_AGENT_INSTANCE_NAMESPACE", "BATCH_AGENT_INSTANCE_DEPLOYMENT", "BATCH_AGENT_INSTANCE_LEASE"],
)
def test_the_watchdog_config_requires_every_object_name(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    """No defaults, for the reason the MCP tool names have none: each of these must equal the
    `resourceNames` entry in the RBAC grant character for character (ADR-0052), and a default that
    disagreed with the grant would present as a 403 at the first call of every run rather than as a
    missing setting. Red if a default appeared — the deployment's own value would silently stop
    mattering, and the failure would look like a permissions bug."""
    monkeypatch.setenv("BATCH_KUBERNETES_API_URL", "https://kubernetes.example:6443")
    monkeypatch.setenv("BATCH_AGENT_INSTANCE_NAMESPACE", "obsidian-vault")
    monkeypatch.setenv("BATCH_AGENT_INSTANCE_DEPLOYMENT", "mcp-obsidian-agent")
    monkeypatch.setenv("BATCH_AGENT_INSTANCE_LEASE", "batch-mode")
    monkeypatch.delenv(missing, raising=False)

    with pytest.raises(ConfigError, match=missing):
        WatchdogConfig.from_env()


def test_the_api_server_is_addressed_by_the_variables_the_kubelet_injects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not `kubernetes.default.svc`: these two are injected into every container and are the one
    address that works before any DNS resolver does, which matters most for a watchdog whose whole
    job is to run when other things are broken. Red if a DNS name were the default."""
    monkeypatch.delenv("BATCH_KUBERNETES_API_URL", raising=False)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.43.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    monkeypatch.setenv("BATCH_AGENT_INSTANCE_NAMESPACE", "obsidian-vault")
    monkeypatch.setenv("BATCH_AGENT_INSTANCE_DEPLOYMENT", "mcp-obsidian-agent")
    monkeypatch.setenv("BATCH_AGENT_INSTANCE_LEASE", "batch-mode")

    assert WatchdogConfig.from_env().agent_instance.api_url == "https://10.43.0.1:443"


def test_an_ipv6_api_server_address_is_bracketed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The kubelet injects the address unbracketed, and a bare IPv6 literal is not a valid URL host.
    Red without the brackets: every request on a dual-stack cluster would fail to parse its own
    URL, and the failure would read as an unreachable API server."""
    monkeypatch.delenv("BATCH_KUBERNETES_API_URL", raising=False)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "fd00::1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    monkeypatch.setenv("BATCH_AGENT_INSTANCE_NAMESPACE", "obsidian-vault")
    monkeypatch.setenv("BATCH_AGENT_INSTANCE_DEPLOYMENT", "mcp-obsidian-agent")
    monkeypatch.setenv("BATCH_AGENT_INSTANCE_LEASE", "batch-mode")

    assert WatchdogConfig.from_env().agent_instance.api_url == "https://[fd00::1]:443"


_LINT_ENV = {
    "LINT_MCP_URL": "http://gateway/obsidian_ingestor_mcp/mcp",
    "LINT_MCP_API_KEY": "sk-key",
    "LINT_MCP_TOOL_READ": "read",
    "LINT_MCP_TOOL_WRITE": "write",
    "LINT_MCP_TOOL_APPEND": "append",
    "LINT_DIGEST_HOOK_URL": "http://openclaw/hooks/agent",
    "LINT_DIGEST_HOOK_TOKEN": "hooks-token",
}


def _lint_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _LINT_ENV.items():
        monkeypatch.setenv(name, value)


def test_lint_pass_config_reads_the_mount_path_and_the_digest_cap_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _lint_env(monkeypatch)
    monkeypatch.delenv("LINT_VAULT_DIR", raising=False)
    monkeypatch.delenv("LINT_DIGEST_MAX_ITEMS", raising=False)

    config = LintPassConfig.from_env()

    assert config.vault_dir == "/vault/brain"
    assert config.digest_max_items == 7
    assert not hasattr(config, "mcp_tool_delete")  # the lint key holds no delete tool (ADR-0004)


@pytest.mark.parametrize("missing", sorted(_LINT_ENV))
def test_lint_pass_config_requires_every_deployment_fact(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    """Tool names, the door, the key, the hook and its token have no defaults: each is deployment
    identity, and a guessed one fails as a refusal wherever it is used."""
    _lint_env(monkeypatch)
    monkeypatch.delenv(missing, raising=False)

    with pytest.raises(ConfigError, match=missing):
        LintPassConfig.from_env()


def test_lint_pass_config_refuses_a_digest_cap_below_one(monkeypatch: pytest.MonkeyPatch) -> None:
    _lint_env(monkeypatch)
    monkeypatch.setenv("LINT_DIGEST_MAX_ITEMS", "0")

    with pytest.raises(ConfigError, match="LINT_DIGEST_MAX_ITEMS"):
        LintPassConfig.from_env()
