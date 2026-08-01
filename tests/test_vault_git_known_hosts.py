"""Tests for `obsidian_tools/vault_git/known_hosts.py`.

`remote_host`/`any_remote_is_github`/`format_known_hosts` are pure and tested as exhaustive
boundary tables, adversarial inputs included -- no git, no ssh, no network. `fetch_github_host_keys`
and `assemble_known_hosts` are the one place this codebase talks to a genuinely external service
(`obsidian_tools/vault_git/known_hosts.py`'s module docstring); per this repo's testing convention,
that HTTP call is the only thing mocked here -- `urllib.request.urlopen` for the fetch itself, and
`fetch_github_host_keys` for `assemble_known_hosts`'s own tests, which otherwise write a real file
to a real temp path. Never git, never ssh: those still run for real everywhere else in this suite.
"""

from __future__ import annotations

import json
import logging
import urllib.error
from collections.abc import Sequence
from pathlib import Path

import pytest

from obsidian_tools.vault_git.known_hosts import (
    GITHUB_META_URL,
    KnownHostsError,
    any_remote_is_github,
    assemble_known_hosts,
    fetch_github_host_keys,
    format_known_hosts,
    remote_host,
)

# --------------------------------------------------------------------------------------------
# remote_host / any_remote_is_github -- pure
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected_host"),
    [
        pytest.param("git@github.com:ppat/obsidian-vault.git", "github.com", id="scp-like"),
        pytest.param("ssh://git@github.com/ppat/obsidian-vault.git", "github.com", id="ssh-scheme"),
        pytest.param("ssh://git@github.com:2222/ppat/obsidian-vault.git", "github.com", id="ssh-scheme-with-port"),
        pytest.param("git@GitHub.COM:ppat/obsidian-vault.git", "GitHub.COM", id="scp-like-mixed-case-unlowered"),
        pytest.param("git@nas.lan:vault.git", "nas.lan", id="scp-like-non-github-host"),
        pytest.param("/tmp/some/bare-repo.git", None, id="local-filesystem-path"),
        pytest.param("", None, id="empty-string"),
        pytest.param("not a url at all", None, id="garbage-no-at-no-scheme"),
        pytest.param("user@no-colon-after-at", None, id="at-sign-but-no-colon"),
        pytest.param("https://github.com/ppat/obsidian-vault.git", "github.com", id="https-scheme"),
    ],
)
def test_remote_host(url: str, expected_host: str | None) -> None:
    assert remote_host(url) == expected_host


@pytest.mark.parametrize(
    ("urls", "expected"),
    [
        pytest.param(["git@github.com:ppat/obsidian-vault.git"], True, id="single-github-remote"),
        pytest.param(["git@github.com:ppat/obsidian-vault.git", "git@nas.lan:vault.git"], True, id="mixed-remotes"),
        pytest.param(["git@nas.lan:vault.git"], False, id="nas-only"),
        pytest.param([], False, id="no-remotes-at-all"),
        pytest.param(["/tmp/bare-repo.git"], False, id="local-path-only"),
        pytest.param(["git@GITHUB.COM:ppat/obsidian-vault.git"], True, id="case-insensitive-match"),
    ],
)
def test_any_remote_is_github(urls: Sequence[str], expected: bool) -> None:
    assert any_remote_is_github(urls) is expected


# --------------------------------------------------------------------------------------------
# format_known_hosts -- pure, adversarial table
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("github_keys", "extra_lines", "expected"),
    [
        pytest.param((), "", "", id="nothing-at-all"),
        pytest.param(("ssh-ed25519 AAAA1",), "", "github.com ssh-ed25519 AAAA1\n", id="single-github-key"),
        pytest.param(
            ("ssh-ed25519 AAAA1", "ssh-rsa AAAA2"),
            "",
            "github.com ssh-ed25519 AAAA1\ngithub.com ssh-rsa AAAA2\n",
            id="multiple-github-keys-order-preserved",
        ),
        pytest.param(
            (),
            "nas.lan ssh-ed25519 BBBB1\n",
            "nas.lan ssh-ed25519 BBBB1\n",
            id="extra-only-no-github",
        ),
        pytest.param(
            ("ssh-ed25519 AAAA1",),
            "nas.lan ssh-ed25519 BBBB1",
            "github.com ssh-ed25519 AAAA1\nnas.lan ssh-ed25519 BBBB1\n",
            id="github-then-extra-no-trailing-newline-in-input",
        ),
        pytest.param(
            (),
            "\n\n   \nnas.lan ssh-ed25519 BBBB1\n\n",
            "nas.lan ssh-ed25519 BBBB1\n",
            id="blank-and-whitespace-only-lines-dropped",
        ),
        pytest.param(
            (),
            "  nas.lan ssh-ed25519 BBBB1  ",
            "nas.lan ssh-ed25519 BBBB1\n",
            id="leading-and-trailing-whitespace-stripped",
        ),
        pytest.param(
            (),
            "nas.lan ssh-ed25519 BBBB1\r\nnas.lan ssh-ed25519 BBBB1\r\n",
            "nas.lan ssh-ed25519 BBBB1\nnas.lan ssh-ed25519 BBBB1\n",
            id="crlf-input-and-duplicate-extra-lines-both-preserved-not-deduped",
        ),
        pytest.param(
            ("  ssh-ed25519 AAAA1  ",),
            "",
            "github.com ssh-ed25519 AAAA1\n",
            id="whitespace-padded-github-key-stripped",
        ),
    ],
)
def test_format_known_hosts(github_keys: Sequence[str], extra_lines: str, expected: str) -> None:
    assert format_known_hosts(github_keys=github_keys, extra_lines=extra_lines) == expected


# --------------------------------------------------------------------------------------------
# fetch_github_host_keys -- the one network seam; urllib.request.urlopen is what's mocked
# --------------------------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_fetch_github_host_keys_parses_ssh_keys_field(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps({"ssh_keys": ["ssh-ed25519 AAAA1", "ssh-rsa AAAA2"], "hooks": ["1.2.3.0/24"]}).encode()

    def _fake_urlopen(*_args: object, **_kwargs: object) -> _FakeResponse:
        return _FakeResponse(body)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    keys = fetch_github_host_keys()

    assert keys == ["ssh-ed25519 AAAA1", "ssh-rsa AAAA2"]


def test_fetch_github_host_keys_raises_known_hosts_error_on_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure this whole feature exists to guard against: GitHub unreachable must not silently
    fall back to an unverified connection -- it must raise, not return an empty/partial result."""

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise urllib.error.URLError("network is unreachable")

    monkeypatch.setattr("urllib.request.urlopen", _raise)

    with pytest.raises(KnownHostsError, match=GITHUB_META_URL):
        fetch_github_host_keys()


def test_fetch_github_host_keys_raises_known_hosts_error_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_args: object, **_kwargs: object) -> None:
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", _raise)

    with pytest.raises(KnownHostsError):
        fetch_github_host_keys()


def test_fetch_github_host_keys_raises_known_hosts_error_on_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_urlopen(*_args: object, **_kwargs: object) -> _FakeResponse:
        return _FakeResponse(b"not json at all")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    with pytest.raises(KnownHostsError):
        fetch_github_host_keys()


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"ssh_keys": []}, id="empty-list"),
        pytest.param({"ssh_keys": "not-a-list"}, id="wrong-type"),
        pytest.param({"hooks": ["1.2.3.0/24"]}, id="field-missing-entirely"),
        pytest.param([], id="top-level-not-an-object"),
    ],
)
def test_fetch_github_host_keys_raises_known_hosts_error_on_unusable_response(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    body = json.dumps(payload).encode()

    def _fake_urlopen(*_args: object, **_kwargs: object) -> _FakeResponse:
        return _FakeResponse(body)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    with pytest.raises(KnownHostsError):
        fetch_github_host_keys()


# --------------------------------------------------------------------------------------------
# assemble_known_hosts -- orchestration: fetch-if-needed, format, write. Real filesystem writes;
# only `fetch_github_host_keys` is mocked here (assemble_known_hosts's own seam onto the network).
# --------------------------------------------------------------------------------------------


def test_assemble_known_hosts_skips_the_fetch_when_no_remote_is_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail_if_called(*_args: object, **_kwargs: object) -> list[str]:
        raise AssertionError("fetch_github_host_keys must not be called when no remote is github.com")

    monkeypatch.setattr("obsidian_tools.vault_git.known_hosts.fetch_github_host_keys", _fail_if_called)
    destination = tmp_path / "ssh" / "known_hosts"

    result = assemble_known_hosts(
        remote_urls=["git@nas.lan:vault.git"],
        extra_lines="nas.lan ssh-ed25519 BBBB1\n",
        destination=destination,
    )

    assert result == destination
    assert destination.read_text() == "nas.lan ssh-ed25519 BBBB1\n"


def _fake_fetch_one_github_key(**_kwargs: object) -> list[str]:
    return ["ssh-ed25519 AAAA1"]


def test_assemble_known_hosts_fetches_and_combines_with_extras_when_a_remote_is_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("obsidian_tools.vault_git.known_hosts.fetch_github_host_keys", _fake_fetch_one_github_key)
    destination = tmp_path / "known_hosts"

    assemble_known_hosts(
        remote_urls=["git@github.com:ppat/obsidian-vault.git", "git@nas.lan:vault.git"],
        extra_lines="nas.lan ssh-ed25519 BBBB1\n",
        destination=destination,
    )

    assert destination.read_text() == "github.com ssh-ed25519 AAAA1\nnas.lan ssh-ed25519 BBBB1\n"


def test_assemble_known_hosts_creates_the_parent_directory(tmp_path: Path) -> None:
    destination = tmp_path / "does" / "not" / "exist" / "known_hosts"

    assemble_known_hosts(remote_urls=["git@nas.lan:vault.git"], extra_lines="nas.lan key\n", destination=destination)

    assert destination.exists()


def test_assemble_known_hosts_propagates_fetch_failure_and_does_not_write_the_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The behaviour this whole feature exists for: a fetch failure must fail the run rather than
    proceed with an unverified/partial known_hosts file -- proven here by asserting both that
    `KnownHostsError` propagates out of `assemble_known_hosts` *and* that nothing was written."""

    def _raise(**_kwargs: object) -> list[str]:
        raise KnownHostsError("simulated GitHub outage")

    monkeypatch.setattr("obsidian_tools.vault_git.known_hosts.fetch_github_host_keys", _raise)
    destination = tmp_path / "known_hosts"

    with pytest.raises(KnownHostsError):
        assemble_known_hosts(
            remote_urls=["git@github.com:ppat/obsidian-vault.git"],
            extra_lines="",
            destination=destination,
        )

    assert not destination.exists()


def test_assemble_known_hosts_logs_what_was_assembled_and_from_where(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Alerting is off on this platform -- this log line is the only diagnostic a failed host-key
    verification has to point back to (docs/DESIGN.md), so the event must actually be emitted, at a
    level a failed run's logs would show."""
    monkeypatch.setattr("obsidian_tools.vault_git.known_hosts.fetch_github_host_keys", _fake_fetch_one_github_key)
    destination = tmp_path / "known_hosts"

    with caplog.at_level(logging.INFO):
        assemble_known_hosts(
            remote_urls=["git@github.com:ppat/obsidian-vault.git"],
            extra_lines="nas.lan key\n",
            destination=destination,
        )

    events = {getattr(r, "event", None) for r in caplog.records}
    assert "known_hosts_github_fetched" in events
    assert "known_hosts_assembled" in events

    [assembled] = [r for r in caplog.records if getattr(r, "event", None) == "known_hosts_assembled"]
    assert assembled.github_key_count == 1  # type: ignore[attr-defined]
    assert assembled.extra_line_count == 1  # type: ignore[attr-defined]
    assert assembled.path == str(destination)  # type: ignore[attr-defined]


def test_assemble_known_hosts_logs_the_skip_when_no_github_remote_is_configured(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        assemble_known_hosts(
            remote_urls=["git@nas.lan:vault.git"], extra_lines="nas.lan key\n", destination=tmp_path / "known_hosts"
        )

    events = {getattr(r, "event", None) for r in caplog.records}
    assert "known_hosts_github_fetch_skipped" in events
    assert "known_hosts_github_fetched" not in events


def test_assemble_known_hosts_writes_world_readable_not_secret_permissions(tmp_path: Path) -> None:
    """A host public key is not a secret (module docstring) -- the written file should be ordinary
    world-readable config, not locked down like the private key it sits next to."""
    destination = tmp_path / "known_hosts"

    assemble_known_hosts(remote_urls=["git@nas.lan:vault.git"], extra_lines="nas.lan key\n", destination=destination)

    assert destination.stat().st_mode & 0o777 == 0o644
