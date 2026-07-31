"""Environment-driven configuration shared by every obsidian-tools subcommand.

Nothing here is specific to `commit`. A future `replicate` subcommand (the Mac-side
`local-replicator`, tracked separately — see ppat/obsidian-tools#3) reads its own configuration
through the same `get_env`/`require_env` helpers and gets its own dataclass beside `CommitConfig`,
rather than each subcommand growing its own ad hoc environment parsing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """A required piece of configuration is missing or invalid."""


def get_env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"required environment variable {name} is not set")
    return value


@dataclass(frozen=True, slots=True)
class CommitConfig:
    """Configuration for the `commit` subcommand, read once from the environment."""

    git_dir: str
    vault_dir: str
    branch: str
    author_name: str
    author_email: str
    origin_url: str
    nas_url: str
    ssh_key_path: str
    ssh_known_hosts_path: str

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
            nas_url=require_env("GIT_REMOTE_NAS_URL"),
            ssh_key_path=get_env("GIT_SSH_KEY_PATH", "/etc/obsidian-tools/git-ssh/id_ed25519"),
            ssh_known_hosts_path=get_env("GIT_SSH_KNOWN_HOSTS_PATH", "/etc/obsidian-tools/git-ssh/known_hosts"),
        )
