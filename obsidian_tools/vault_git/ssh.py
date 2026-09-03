"""Builds the `GIT_SSH_COMMAND`, from one mounted key and the `known_hosts` file assembled at runtime.

One SSH key, mounted as a file and registered as a write deploy key on `origin` — one credential,
one secret, one mount.

Deliberately does not disable host key checking, and does not bake host keys into the image; they
are established per run instead (`known_hosts.py`). `BatchMode=yes` so a run fails fast and
non-interactively on any auth or host-key problem, rather than hanging forever waiting for a prompt
that can never come in a CronJob pod.
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
