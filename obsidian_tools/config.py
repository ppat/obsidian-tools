"""Environment-driven configuration shared by every obsidian-tools subcommand.

Nothing here is specific to `commit`. A future `replicate` subcommand (the Mac-side
`local-replicator`, tracked separately — see ppat/obsidian-tools#3) reads its own configuration
through the same `get_env`/`require_env` helpers and gets its own dataclass beside `CommitConfig`,
rather than each subcommand growing its own ad hoc environment parsing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

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
            # insurance (docs/DESIGN.md §2 item 5), not something the committer needs to do its
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
