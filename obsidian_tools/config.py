"""Environment-driven configuration shared by every obsidian-tools subcommand.

Nothing here is specific to `commit`. A future `replicate` subcommand (the Mac-side
`local-replicator`, tracked separately — see ppat/obsidian-tools#3) reads its own configuration
through the same `get_env`/`require_env` helpers and gets its own dataclass beside `CommitConfig`,
rather than each subcommand growing its own ad hoc environment parsing.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass

from obsidian_tools.batch_processor.fairness import subjects_overlap
from obsidian_tools.batch_processor.preflight import DEFAULT_RAW_LAYER_PREFIX
from obsidian_tools.vault_git.commit import DEFAULT_MAX_DELETION_FRACTION


class ConfigError(RuntimeError):
    """A required piece of configuration is missing or invalid."""


def get_env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def get_env_optional(name: str) -> str | None:
    """`None` when `name` is unset or set to an empty string, distinguishing "not configured" from
    an empty override. Used for genuinely optional configuration (`CommitConfig.nas_url`) — unlike
    `get_env`, there is no sensible non-empty default to fall back to."""
    return os.environ.get(name) or None


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"required environment variable {name} is not set")
    return value


def get_env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not a valid float") from exc


def get_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not a valid int") from exc


def get_env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if not raw:
        return default
    normalized = raw.strip().lower()
    if normalized in ("1", "true", "yes"):
        return True
    if normalized in ("0", "false", "no"):
        return False
    raise ConfigError(f"{name}={raw!r} is not a valid bool (expected one of: true, false, yes, no, 1, 0)")


@dataclass(frozen=True, slots=True)
class CommitConfig:
    """Configuration for the `commit` subcommand, read once from the environment."""

    git_dir: str
    vault_dir: str
    branch: str
    author_name: str
    author_email: str
    origin_url: str
    # `None` when the NAS remote isn't configured at all -- see `from_env`'s comment on
    # GIT_REMOTE_NAS_URL for why that's a legitimate, supported run rather than a missing setting.
    nas_url: str | None
    ssh_key_path: str
    # Where the assembled `known_hosts` file is written and then read back from for
    # `GIT_SSH_COMMAND`'s `UserKnownHostsFile` -- not a path to a pre-existing mounted file anymore
    # (obsidian_tools/vault_git/known_hosts.py). Defaults under $HOME rather than
    # /etc/obsidian-tools/git-ssh/ because the latter sits on the read-only root filesystem; $HOME
    # is the writable emptyDir this process actually has.
    ssh_known_hosts_path: str
    # Verbatim extra `known_hosts` lines (e.g. the NAS's host key, published nowhere fetchable) --
    # appended as-is to the fetched GitHub keys. See known_hosts.py's module docstring.
    ssh_known_hosts_extra: str
    max_deletion_fraction: float

    @classmethod
    def from_env(cls) -> CommitConfig:
        return cls(
            git_dir=get_env("OBSIDIAN_GIT_DIR", "/git/vault.git"),
            # Must be a subdirectory of the volume mount, never the mount root: the mount root
            # carries an ext4 lost+found, root-owned mode 0700 and unreadable by this workload's
            # uid, and headless Obsidian (which shares the same volume, and fails outright if it
            # has to watch a directory it can't read) is the reason the vault already lives at a
            # subdirectory rather than the root. Pointing the work tree there too means this
            # component inherits that fix instead of needing its own. Don't "simplify" this back to
            # the mount root.
            vault_dir=get_env("OBSIDIAN_VAULT_DIR", "/vault/brain"),
            branch=get_env("GIT_COMMIT_BRANCH", "main"),
            # Not GIT_AUTHOR_NAME/GIT_AUTHOR_EMAIL: those are git's own reserved environment
            # variables (see git(1) ENVIRONMENT VARIABLES) and git reads them directly, ahead of
            # `user.name`/`user.email` config, for the *author* identity only — an operator setting
            # them for git's own sake would silently reconfigure this tool too, and since nothing
            # here sets GIT_COMMITTER_NAME/EMAIL to match, the commit's author and committer would
            # then disagree. GIT_COMMIT_ prefixed, matching GIT_COMMIT_BRANCH above.
            author_name=get_env("GIT_COMMIT_AUTHOR_NAME", "brain-committer"),
            author_email=get_env("GIT_COMMIT_AUTHOR_EMAIL", "brain-committer@noreply.invalid"),
            origin_url=require_env("GIT_REMOTE_ORIGIN_URL"),
            # Unlike origin, genuinely optional: the NAS is a second push target for independence
            # insurance (ADR-0029), not something the committer needs to do its
            # primary job. Requiring it up front would also require, before the component could run
            # even once, everything the NAS remote depends on -- SSH access to the NAS, an
            # authorized_keys entry, a bare repository created by hand, and a host key published
            # nowhere (see GIT_SSH_KNOWN_HOSTS_EXTRA above). `run()` logs which remotes are
            # configured either way; see obsidian_tools/commands/commit.py.
            nas_url=get_env_optional("GIT_REMOTE_NAS_URL"),
            ssh_key_path=get_env("GIT_SSH_KEY_PATH", "/etc/obsidian-tools/git-ssh/id_ed25519"),
            ssh_known_hosts_path=get_env("GIT_SSH_KNOWN_HOSTS_PATH", os.path.expanduser("~/.ssh/known_hosts")),
            ssh_known_hosts_extra=get_env("GIT_SSH_KNOWN_HOSTS_EXTRA", ""),
            # The mass-deletion guard's threshold (obsidian_tools/vault_git/commit.py) previously
            # had no env var reaching it at all — a genuine archive purge had no way past it short
            # of editing source. >=1.0 disables the guard entirely for a run; see that module's
            # docstring for why that's the right shape for an operator escape hatch here.
            max_deletion_fraction=get_env_float("GIT_COMMIT_MAX_DELETION_FRACTION", DEFAULT_MAX_DELETION_FRACTION),
        )


@dataclass(frozen=True, slots=True)
class ReplicateConfig:
    """Configuration for the `replicate` subcommand (`local-replicator`), read once from the
    environment. Runs on the operator's Mac, not in-cluster — see
    obsidian_tools/commands/replicate.py and ADR-0025.

    No default for `icloud_vault_dir`: unlike every path in `CommitConfig`, this one names a
    per-operator vault name (`iCloud Drive/Obsidian/<Vault Name>`) that this codebase has no
    business guessing at.
    """

    cache_clone_dir: str
    icloud_vault_dir: str
    branch: str
    origin_url: str
    ssh_key_path: str
    ssh_known_hosts_path: str
    spool_dir: str

    @classmethod
    def from_env(cls) -> ReplicateConfig:
        return cls(
            # The single parked clone (ADR-0025): stays checked out at
            # LAST_CHECKOUT between cycles, so it doubles as both the pull-forward target and the
            # drift-comparison baseline. Not `/git/vault.git` (CommitConfig's bare, detached
            # git-dir) — this one is an ordinary, checked-out working tree, because rsync's publish
            # step reads its files directly.
            cache_clone_dir=get_env("OBSIDIAN_CACHE_CLONE_DIR", os.path.expanduser("~/.cache/obsidian-vault")),
            icloud_vault_dir=require_env("ICLOUD_VAULT_DIR"),
            branch=get_env("GIT_COMMIT_BRANCH", "main"),
            # Same variable name as CommitConfig — the same origin repository, read here through a
            # separate read-only deploy key rather than the committer's read-write one, but the two
            # configs never share a process, so reusing the name costs nothing.
            origin_url=require_env("GIT_REMOTE_ORIGIN_URL"),
            # Mac-appropriate defaults, distinct from CommitConfig's in-cluster mount paths — this
            # process reads its own SSH key from wherever the operator installed it, not from a
            # Kubernetes Secret volume.
            ssh_key_path=get_env("GIT_SSH_KEY_PATH", os.path.expanduser("~/.ssh/obsidian_vault_readonly")),
            ssh_known_hosts_path=get_env("GIT_SSH_KNOWN_HOSTS_PATH", os.path.expanduser("~/.ssh/known_hosts")),
            # Local, durable storage for drift patches, spooled before publish overwrites the
            # device copy that produced them (ADR-0025). Application
            # Support, not Caches — unlike the parked clone (disposable, rebuildable from origin),
            # an undrained spool entry may be the only record of a human's edit until the drainer
            # sends it onward, so it must not be treated as something the OS is free to purge.
            # Shared with `DrainConfig`'s own default below by construction, not by importing one
            # from the other — the two configs never share a process (see that dataclass's
            # docstring for why draining is deliberately its own subcommand and its own config).
            spool_dir=get_env(
                "LOCAL_REPLICATOR_SPOOL_DIR",
                os.path.expanduser("~/Library/Application Support/obsidian-tools/local-replicator/spool"),
            ),
        )


@dataclass(frozen=True, slots=True)
class DrainConfig:
    """Configuration for the `drain` subcommand — the spool drainer (ADR-0024, ADR-0025),
    read once from the environment. Runs on the operator's Mac, alongside `replicate` but
    on its own schedule — see obsidian_tools/commands/drain.py.

    Deliberately its own dataclass, not a slice of `ReplicateConfig`: the drainer is decoupled from
    the replication cycle by design (ADR-0024: a separate drainer, decoupled from the cycle's own
    numbering, sends the spool onward), and giving it `ReplicateConfig`'s
    full environment would make it require `ICLOUD_VAULT_DIR`/`GIT_REMOTE_ORIGIN_URL` it has no use
    for at all — it never touches iCloud or git.
    """

    spool_dir: str

    @classmethod
    def from_env(cls) -> DrainConfig:
        return cls(
            spool_dir=get_env(
                "LOCAL_REPLICATOR_SPOOL_DIR",
                os.path.expanduser("~/Library/Application Support/obsidian-tools/local-replicator/spool"),
            )
        )


@dataclass(frozen=True, slots=True)
class BatchProducerConfig:
    """Configuration for the `enqueue-batch` subcommand — the batch stream's producer (unit B1,
    ot#125), read once from the environment. Runs in the Coder workspace against that workspace's
    own git checkout, so its git paths have no defaults worth guessing: unlike `CommitConfig`'s
    in-cluster mount points, there is no fixed location a workspace repository lives at.

    Deliberately its own dataclass rather than a slice of `CommitConfig`: the two share the *idea* of
    a git directory and nothing else — no SSH key, no remotes, no branch, no deletion guard — and
    the shape `DrainConfig`'s docstring argues for applies unchanged here.
    """

    git_dir: str
    work_tree: str
    # What `git diff --cached` diffs against, and therefore the tree every pre-image hash is read
    # from (batch_producer/generation.py). One setting for both on purpose: two would be two
    # things to keep equal, and a patch hashed against a different tree than it was generated
    # against is a staleness check that passes when it should not.
    base_rev: str
    nats_url: str
    nats_user: str
    nats_password: str
    # Must match the subscribe grant on this credential, which ADR-0047 scopes to
    # `_INBOX_BATCH.*.*` and nothing else. The library's default (`_INBOX`) is refused by that
    # grant, and the refusal surfaces as a publish timeout that reads as an unreachable broker --
    # see batch_producer/nats_client.py's discipline 3 for the failure this default prevents.
    nats_inbox_prefix: str
    # The stream's one top-level subject token (ADR-0047). The batch id becomes the trailing token,
    # which that record deliberately left free for routing.
    subject_prefix: str
    # Well under NATS's 1 MiB default `max_payload`, leaving the JSON envelope and any future field
    # room without recalculating this. Not larger: a chunk is the transaction and redelivery unit,
    # so its size is how much work one staleness rejection throws away.
    max_chunk_patch_bytes: int
    connect_timeout_seconds: float
    publish_timeout_seconds: float
    # Bounded, and low. `nats-py` defaults to 60 attempts on a 2-second timer, so a wrong or rotated
    # credential spends four quiet minutes retrying before anything says so; this producer runs to
    # completion and exits, so failing soon and loudly is strictly better than eventually.
    max_reconnect_attempts: int

    @classmethod
    def from_env(cls) -> BatchProducerConfig:
        return cls(
            git_dir=require_env("BATCH_GIT_DIR"),
            work_tree=require_env("BATCH_WORK_TREE"),
            base_rev=get_env("BATCH_BASE_REV", "HEAD"),
            nats_url=require_env("BATCH_NATS_URL"),
            nats_user=get_env("BATCH_NATS_USER", "batch-producer"),
            nats_password=require_env("BATCH_NATS_PASSWORD"),
            nats_inbox_prefix=get_env("BATCH_NATS_INBOX_PREFIX", "_INBOX_BATCH"),
            subject_prefix=get_env("BATCH_SUBJECT_PREFIX", "batch"),
            max_chunk_patch_bytes=get_env_int("BATCH_MAX_CHUNK_PATCH_BYTES", 262144),
            connect_timeout_seconds=get_env_float("BATCH_CONNECT_TIMEOUT_SECONDS", 5.0),
            publish_timeout_seconds=get_env_float("BATCH_PUBLISH_TIMEOUT_SECONDS", 10.0),
            max_reconnect_attempts=get_env_int("BATCH_MAX_RECONNECT_ATTEMPTS", 3),
        )


@dataclass(frozen=True, slots=True)
class VaultExporterConfig:
    """Configuration for the `export-metrics` subcommand — ADR-0037's independent vault-loaded
    exporter (unit D1, ot#121), read once from the environment. Runs in-cluster, alongside headless
    Obsidian and the two MCP servers, but is not a client of `GitRunner`: it never touches git or
    the vault volume at all, and mounts nothing (a fourth mounter would be a change to ADR-0001, not
    a manifest detail — see DESIGN.md's "One writer, one door"). Its only I/O is the same
    authenticated call to Obsidian's Local REST API the MCP servers already make, plus serving its
    own `/metrics`. Deliberately its own dataclass rather than a slice of `CommitConfig`: the two
    run in the same cluster but share no git/SSH configuration at all.

    `obsidian_base_url`/`obsidian_api_key`/`verify_tls` deliberately reuse the exact env var names
    (`OBSIDIAN_BASE_URL`, `OBSIDIAN_API_KEY`, `OBSIDIAN_VERIFY_SSL`) the `mcp-obsidian-agent`
    Deployment already sets (ppat/homelab-ops-kubernetes-apps,
    apps/subsystems/ai/obsidian-vault/mcp-obsidian-agent/deployment.yaml) rather than inventing an
    exporter-prefixed set: both processes authenticate to the identical listener with the identical
    token shape, so the manifest wiring this subcommand's container can copy that env block
    verbatim instead of re-deriving it.
    """

    obsidian_base_url: str
    # Never logged, never echoed in an error message (see vault_exporter/client.py) -- this bearer
    # token grants full read/write to the authoritative vault, with no path-scoped permissions of
    # its own (the same reason blackbox was rejected for this check, ADR-0037's alternatives
    # section).
    obsidian_api_key: str
    # Default False: the Local REST API plugin generates its own self-signed certificate at
    # runtime (obsidian-vault/obsidian/deployment.yaml's own comment), so there is no stable cert
    # to verify against ahead of time. Matches mcp-obsidian-agent's own OBSIDIAN_VERIFY_SSL=false
    # for the identical reason, stated explicitly there too.
    verify_tls: bool
    # 60s, not the module's usual 30s for its other ServiceMonitors: this signal detects a
    # condition that persists until a human intervenes (ADR-0037's incident took hours to notice
    # through the GUI, not seconds), so a slower poll loses no real detection latency and halves
    # the call volume against Obsidian's REST API.
    poll_interval_seconds: float
    request_timeout_seconds: float
    listen_host: str
    listen_port: int

    @classmethod
    def from_env(cls) -> VaultExporterConfig:
        return cls(
            obsidian_base_url=get_env("OBSIDIAN_BASE_URL", "https://obsidian.obsidian-vault.svc.cluster.local:27124"),
            obsidian_api_key=require_env("OBSIDIAN_API_KEY"),
            verify_tls=get_env_bool("OBSIDIAN_VERIFY_SSL", False),
            poll_interval_seconds=get_env_float("VAULT_EXPORTER_POLL_INTERVAL_SECONDS", 60.0),
            request_timeout_seconds=get_env_float("VAULT_EXPORTER_REQUEST_TIMEOUT_SECONDS", 10.0),
            # 0.0.0.0, not loopback: Prometheus scrapes this pod over the pod network, from a
            # different pod entirely (../network-policy.yaml's own Prometheus ingress exception),
            # never from inside this container.
            listen_host=get_env("VAULT_EXPORTER_LISTEN_HOST", "0.0.0.0"),
            # Arbitrary and unclaimed by anything else in this project; the manifest PR
            # (apps#3946) picks the actual container port when it lands and can override this via
            # env if 9877 ever collides with something in that repo's own port list.
            listen_port=get_env_int("VAULT_EXPORTER_LISTEN_PORT", 9877),
        )


# Where a Kubernetes workload's projected service-account credentials are mounted. Overridable
# below only so the test suite can point at files it wrote; nothing in a manifest should need to.
DEFAULT_SERVICE_ACCOUNT_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


@dataclass(frozen=True, slots=True)
class AgentInstanceConfig:
    """Which agent MCP instance a batch run stops, which lease says so, and how to reach the
    cluster's API to do either.

    Its own dataclass rather than the same fields duplicated into two configs, because
    `batch-processor` and its watchdog are separate processes on separate schedules that must agree,
    to the character, on which objects they mean — one stopping an instance the other is not
    watching is exactly the silent, indefinite outage unit D4 exists to close. Composed into both
    rather than inherited by either, matching how nothing else in this file uses subclassing.
    """

    api_url: str
    # All three required with no default, for the reason the MCP tool names are: each must equal the
    # `resourceNames` entry in the RBAC grant character for character (ADR-0052 narrows that grant
    # to the agent instance alone), and a default that disagreed with the grant would present as a
    # 403 at the first call of every run rather than as a missing setting.
    namespace: str
    deployment: str
    lease_name: str
    # Who the lease says holds the door. `concurrencyPolicy: Forbid` prevents a second *scheduled*
    # run, never a hand-run Job, so this is the only thing that makes an overlap visible.
    holder_identity: str
    token_path: str
    # `None` disables the CA override, which is only ever right off-cluster; in-cluster the API
    # server's certificate is signed by a CA no public trust store knows.
    ca_path: str | None
    timeout_seconds: float
    verify_tls: bool

    @classmethod
    def from_env(cls) -> AgentInstanceConfig:
        return cls(
            api_url=get_env("BATCH_KUBERNETES_API_URL", "") or _in_cluster_api_url(),
            namespace=require_env("BATCH_AGENT_INSTANCE_NAMESPACE"),
            deployment=require_env("BATCH_AGENT_INSTANCE_DEPLOYMENT"),
            lease_name=require_env("BATCH_AGENT_INSTANCE_LEASE"),
            # The pod's own name under the downward API, and the container's hostname without one.
            # Either identifies the run well enough to answer "which one is holding this".
            holder_identity=get_env("BATCH_RUN_IDENTITY", "") or socket.gethostname(),
            token_path=get_env("BATCH_KUBERNETES_TOKEN_PATH", f"{DEFAULT_SERVICE_ACCOUNT_DIR}/token"),
            ca_path=get_env("BATCH_KUBERNETES_CA_PATH", f"{DEFAULT_SERVICE_ACCOUNT_DIR}/ca.crt") or None,
            timeout_seconds=get_env_float("BATCH_KUBERNETES_TIMEOUT_SECONDS", 10.0),
            verify_tls=get_env_bool("BATCH_KUBERNETES_VERIFY_TLS", True),
        )


def _in_cluster_api_url() -> str:
    """The API server as the kubelet advertises it to every pod.

    Not the `kubernetes.default.svc` name: these two variables are injected into every container in
    the namespace and are the one address that works before any DNS resolver does — which matters
    for a watchdog whose whole job is to run when other things are broken.
    """
    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    if not host:
        raise ConfigError(
            "KUBERNETES_SERVICE_HOST is not set and BATCH_KUBERNETES_API_URL does not name an API server; "
            "this component only runs in-cluster"
        )
    # A bare IPv6 literal is not a valid URL host without brackets, and the kubelet injects one
    # unbracketed on dual-stack clusters.
    return f"https://[{host}]:{port}" if ":" in host else f"https://{host}:{port}"


@dataclass(frozen=True, slots=True)
class BatchProcessorConfig:
    """Configuration for the `process-batch` subcommand — `batch-processor` (unit A2, ot#5).

    Deliberately its own dataclass and not a slice of `BatchProducerConfig`: the two speak to the
    same broker about the same stream and share not one credential, endpoint or timeout — the
    producer runs in the Coder workspace holding the one credential permitted to publish, this runs
    in-cluster holding the one permitted to consume (ADR-0047).
    """

    nats_url: str
    nats_user: str
    nats_password: str
    # Must match this credential's own subscribe grant. Left at the library default here, unlike
    # the producer's: ADR-0047 scopes the *producer's* reply inbox, while this consumer lives in the
    # account holding the streams, whose grant is the deployment's to state (apps#3875). A
    # non-default value without a matching grant fails as a timeout that reads as a dead broker.
    nats_inbox_prefix: str
    stream: str
    durable: str
    # The batch stream's one top-level subject token (ADR-0047); the consumer filters on
    # `<prefix>.>`, so every batch under it is drained by the one FIFO consumer.
    subject_prefix: str
    # Its own top-level token, checked below to be disjoint from `subject_prefix`: a dead-letter
    # subject inside the batch stream's own subject list is re-consumed by the consumer that just
    # gave up on it, restarting its delivery count — an infinite, silent loop.
    dead_letter_subject_prefix: str
    # After this many deliveries the broker stops on its own, so the processor dead-letters on the
    # last delivery rather than waiting for one that never comes.
    max_deliver: int
    # Must exceed the longest a chunk can legitimately take to apply. Shorter, and the broker
    # redelivers work still in hand.
    ack_wait_seconds: float
    # How long an empty stream is waited on before the run declares itself finished — what makes a
    # scheduled run against an empty stream cost one fetch, which is ADR-0022's triggering policy
    # expressed as a schedule rather than as code.
    idle_timeout_seconds: float
    connect_timeout_seconds: float
    # Optional on purpose, and its absence is a declared state rather than a silent default:
    # `promotion-processor` is a separate unit (A3, ot#86) and is not built, so a run today has no
    # depth to yield to and says so once, loudly. Set, it must be readable — a configured stream
    # that cannot be read stops the run rather than letting bulk proceed blind.
    promotion_stream: str | None
    promotion_consumer: str | None
    promotion_depth_threshold: int
    backoff_base_delay_seconds: float
    backoff_max_delay_seconds: float
    backoff_jitter_fraction: float
    # Bounds one specific wait, never the run: an undrained promotion stream would otherwise hold
    # the agent instance stopped indefinitely. ADR-0022's maximum run duration is a different
    # mechanism in a different ticket (ot#89).
    max_consecutive_yields: int
    mcp_url: str
    # Carries this component's write scope, the widest in the system. Never logged, never echoed in
    # an error message (`batch_processor/mcp_client.py`).
    mcp_api_key: str
    # Required, with no default, all three: a tool's name is deployment identity. The gateway
    # prefixes each with the access group the key reaches the server through, so a regrouping
    # renames every tool while changing nothing about what any of them does, and a guessed name is
    # wrong in a way that presents identically to a gate refusal at every call site. What a tool
    # *accepts* is the opposite kind of fact and is not configuration at all: the argument shapes
    # are fixed by the tools' schemas and live in `batch_processor/mcp_client.py`.
    mcp_tool_read: str
    mcp_tool_write: str
    mcp_tool_delete: str
    mcp_timeout_seconds: float
    mcp_verify_tls: bool
    mcp_retries: int
    # The write-once layer's prefix (ADR-0015), configurable so the vault's layer naming and this
    # component's enforcement of it can move together rather than needing a release to disagree.
    raw_layer_prefix: str
    # How long a lease survives without renewal. Sized against how often the processor renews
    # (before every chunk and every yield), never against how long a chunk takes: it is a liveness
    # signal, not a run budget.
    lease_ttl_seconds: float
    agent_instance: AgentInstanceConfig

    @classmethod
    def from_env(cls) -> BatchProcessorConfig:
        subject_prefix = get_env("BATCH_SUBJECT_PREFIX", "batch")
        dead_letter_subject_prefix = get_env("BATCH_DEAD_LETTER_SUBJECT_PREFIX", "batch-dead-letter")
        if subjects_overlap(subject_prefix, dead_letter_subject_prefix):
            raise ConfigError(
                f"BATCH_DEAD_LETTER_SUBJECT_PREFIX={dead_letter_subject_prefix!r} overlaps "
                f"BATCH_SUBJECT_PREFIX={subject_prefix!r}: a dead-lettered chunk would be redelivered to "
                "the consumer that just gave up on it"
            )
        return cls(
            nats_url=require_env("BATCH_NATS_URL"),
            nats_user=get_env("BATCH_PROCESSOR_NATS_USER", "batch-processor"),
            nats_password=require_env("BATCH_PROCESSOR_NATS_PASSWORD"),
            nats_inbox_prefix=get_env("BATCH_PROCESSOR_NATS_INBOX_PREFIX", "_INBOX"),
            stream=get_env("BATCH_STREAM", "batch"),
            durable=get_env("BATCH_CONSUMER_DURABLE", "batch-processor"),
            subject_prefix=subject_prefix,
            dead_letter_subject_prefix=dead_letter_subject_prefix,
            max_deliver=get_env_int("BATCH_MAX_DELIVER", 5),
            ack_wait_seconds=get_env_float("BATCH_ACK_WAIT_SECONDS", 120.0),
            idle_timeout_seconds=get_env_float("BATCH_IDLE_TIMEOUT_SECONDS", 30.0),
            connect_timeout_seconds=get_env_float("BATCH_CONNECT_TIMEOUT_SECONDS", 5.0),
            promotion_stream=get_env_optional("BATCH_PROMOTION_STREAM"),
            promotion_consumer=get_env_optional("BATCH_PROMOTION_CONSUMER"),
            promotion_depth_threshold=get_env_int("BATCH_PROMOTION_DEPTH_THRESHOLD", 1),
            backoff_base_delay_seconds=get_env_float("BATCH_BACKOFF_BASE_DELAY_SECONDS", 1.0),
            backoff_max_delay_seconds=get_env_float("BATCH_BACKOFF_MAX_DELAY_SECONDS", 60.0),
            backoff_jitter_fraction=get_env_float("BATCH_BACKOFF_JITTER_FRACTION", 0.25),
            max_consecutive_yields=get_env_int("BATCH_MAX_CONSECUTIVE_YIELDS", 30),
            mcp_url=require_env("BATCH_MCP_URL"),
            mcp_api_key=require_env("BATCH_MCP_API_KEY"),
            mcp_tool_read=require_env("BATCH_MCP_TOOL_READ"),
            mcp_tool_write=require_env("BATCH_MCP_TOOL_WRITE"),
            mcp_tool_delete=require_env("BATCH_MCP_TOOL_DELETE"),
            mcp_timeout_seconds=get_env_float("BATCH_MCP_TIMEOUT_SECONDS", 30.0),
            mcp_verify_tls=get_env_bool("BATCH_MCP_VERIFY_TLS", True),
            mcp_retries=get_env_int("BATCH_MCP_RETRIES", 3),
            raw_layer_prefix=get_env("BATCH_RAW_LAYER_PREFIX", DEFAULT_RAW_LAYER_PREFIX),
            lease_ttl_seconds=get_env_float("BATCH_LEASE_TTL_SECONDS", 300.0),
            agent_instance=AgentInstanceConfig.from_env(),
        )


@dataclass(frozen=True, slots=True)
class WatchdogConfig:
    """Configuration for the `watch-agent-instance` subcommand — unit D4's non-deferrable half.

    Nothing but the instance. Giving the watchdog the processor's broker and MCP configuration would
    make it require an environment it has no use for — `DrainConfig`'s argument for not being a
    slice of `ReplicateConfig`, and here it is stronger: what the watchdog watches is precisely the
    thing that may be broken, so it must not depend on any of it to start. What it does depend on
    is the cluster's own API, which is the one dependency this mechanism trades for the previous
    one's (ADR-0052).
    """

    agent_instance: AgentInstanceConfig

    @classmethod
    def from_env(cls) -> WatchdogConfig:
        return cls(agent_instance=AgentInstanceConfig.from_env())


@dataclass(frozen=True, slots=True)
class LintPassConfig:
    """Configuration for the `lint` subcommand — the lint pass (unit A5, ot#83).

    Its own dataclass: it shares the MCP seam with `batch-processor` but not its key, its tools or
    its reach. Its key is granted read, write and append only — never delete (ADR-0004) — so no
    delete tool is configurable here at all.
    """

    vault_dir: str
    """The vault on the read-only mount: `/vault/brain`, the git committer's stanza."""
    mcp_url: str
    # Carries the ingestor handle's write scope. Never logged, never echoed in an error message
    # (`batch_processor/mcp_client.py`).
    mcp_api_key: str
    # Required with no default, as for `batch-processor`: a tool's name is deployment identity, and a
    # guessed one fails as a refusal at every call site.
    mcp_tool_read: str
    mcp_tool_write: str
    mcp_tool_append: str
    mcp_timeout_seconds: float
    mcp_verify_tls: bool
    mcp_retries: int
    # The review digest's destination and its bearer token, both deployment facts with no default.
    digest_hook_url: str
    digest_hook_token: str
    digest_hook_timeout_seconds: float
    # ADR-0018's "hard-capped at roughly seven".
    digest_max_items: int

    @classmethod
    def from_env(cls) -> LintPassConfig:
        max_items = get_env_int("LINT_DIGEST_MAX_ITEMS", 7)
        if max_items < 1:
            raise ConfigError(f"LINT_DIGEST_MAX_ITEMS={max_items} must be at least 1")
        return cls(
            vault_dir=get_env("LINT_VAULT_DIR", "/vault/brain"),
            mcp_url=require_env("LINT_MCP_URL"),
            mcp_api_key=require_env("LINT_MCP_API_KEY"),
            mcp_tool_read=require_env("LINT_MCP_TOOL_READ"),
            mcp_tool_write=require_env("LINT_MCP_TOOL_WRITE"),
            mcp_tool_append=require_env("LINT_MCP_TOOL_APPEND"),
            mcp_timeout_seconds=get_env_float("LINT_MCP_TIMEOUT_SECONDS", 30.0),
            mcp_verify_tls=get_env_bool("LINT_MCP_VERIFY_TLS", True),
            mcp_retries=get_env_int("LINT_MCP_RETRIES", 3),
            digest_hook_url=require_env("LINT_DIGEST_HOOK_URL"),
            digest_hook_token=require_env("LINT_DIGEST_HOOK_TOKEN"),
            digest_hook_timeout_seconds=get_env_float("LINT_DIGEST_HOOK_TIMEOUT_SECONDS", 10.0),
            digest_max_items=max_items,
        )
