from __future__ import annotations

from obsidian_tools.vault_git.ssh import build_ssh_command


def test_includes_explicit_identity_and_known_hosts() -> None:
    command = build_ssh_command("/secrets/id_ed25519", "/secrets/known_hosts")

    assert "-i /secrets/id_ed25519" in command
    assert "-o UserKnownHostsFile=/secrets/known_hosts" in command


def test_never_disables_host_key_checking() -> None:
    command = build_ssh_command("/secrets/id_ed25519", "/secrets/known_hosts")

    assert "StrictHostKeyChecking=no" not in command
    assert "StrictHostKeyChecking=yes" in command
