"""Builds the `GIT_SSH_COMMAND` used for both remotes, from one mounted key and one mounted known_hosts file.

One SSH key, mounted as a file, used for both `origin` and `nas` (registered as a write deploy key
on GitHub and in the NAS's `authorized_keys`) — one credential, one secret, one mount.

Deliberately does not disable host key checking, and does not bake host keys into the image (the
NAS's key cannot be known at build time). `BatchMode=yes` so a run fails fast and non-interactively
on any auth or host-key problem, rather than hanging forever waiting for a prompt that can never
come in a CronJob pod.
"""

from __future__ import annotations


def build_ssh_command(key_path: str, known_hosts_path: str) -> str:
    return (
        "ssh "
        f"-i {key_path} "
        "-o IdentitiesOnly=yes "
        "-o BatchMode=yes "
        "-o StrictHostKeyChecking=yes "
        f"-o UserKnownHostsFile={known_hosts_path}"
    )
