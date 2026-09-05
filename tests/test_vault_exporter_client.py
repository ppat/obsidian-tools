"""Tests for `obsidian_tools/vault_exporter/client.py` -- the one HTTP seam to Obsidian.

Per this repo's own testing convention (see `tests/test_vault_git_known_hosts.py`'s docstring) and
this task's brief: don't mock the HTTP dependency to test a decision -- the decision
(`enumeration.py`) is pure and needs no mock at all. What's tested here is the client's *own*
behavior -- the request it builds and how it reacts to real responses -- against a real local HTTP
server on an ephemeral port, exactly the same "test the real thing" discipline the crash-injection
harness uses for git. No `urllib.request.urlopen` mocking anywhere in this file.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from obsidian_tools.vault_exporter.client import VaultEnumerationError, fetch_vault_listing

_TOKEN = "s3cr3t-vault-bearer-token"  # a test fixture value, not a real credential


class _TestServer:
    def __init__(self, respond: Callable[[BaseHTTPRequestHandler], None]) -> None:
        self.received_auth_headers: list[str | None] = []
        respond_fn = respond
        received = self.received_auth_headers

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                received.append(self.headers.get("Authorization"))
                respond_fn(self)

            def log_message(self, format: str, *args: object) -> None:  # silence stderr noise
                pass

        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def server_factory() -> Iterator[Callable[[Callable[[BaseHTTPRequestHandler], None]], _TestServer]]:
    servers: list[_TestServer] = []

    def _make(respond: Callable[[BaseHTTPRequestHandler], None]) -> _TestServer:
        server = _TestServer(respond)
        servers.append(server)
        return server

    yield _make
    for server in servers:
        server.close()


def _respond_ok(files: list[str]) -> Callable[[BaseHTTPRequestHandler], None]:
    body = json.dumps({"files": files}).encode()

    def _respond(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    return _respond


def test_fetch_returns_the_raw_response_body_on_success(
    server_factory: Callable[[Callable[[BaseHTTPRequestHandler], None]], _TestServer],
) -> None:
    """Red if a real 200 response's body is ever altered, truncated, or not returned verbatim for
    `enumeration.decode_enumeration_response` to parse."""
    server = server_factory(_respond_ok(["note.md", "10-areas/"]))

    body = fetch_vault_listing(server.base_url, _TOKEN, verify_tls=False, timeout=2.0)

    assert body == b'{"files": ["note.md", "10-areas/"]}'


def test_fetch_sends_the_bearer_token_as_the_authorization_header(
    server_factory: Callable[[Callable[[BaseHTTPRequestHandler], None]], _TestServer],
) -> None:
    """Red if the token is ever sent in a different header, a different scheme, or not at all --
    proven against a real server that actually inspects the header it received, not an assertion
    about what `Request(...)` was constructed with."""
    server = server_factory(_respond_ok([]))

    fetch_vault_listing(server.base_url, _TOKEN, verify_tls=False, timeout=2.0)

    assert server.received_auth_headers == [f"Bearer {_TOKEN}"]


def test_fetch_raises_on_a_non_2xx_response(
    server_factory: Callable[[Callable[[BaseHTTPRequestHandler], None]], _TestServer],
) -> None:
    """Red if an authentication failure (a real, live consequence of a revoked/rotated token) is
    ever swallowed into an empty or default success instead of a raised error."""

    def _respond_401(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(401)
        handler.end_headers()

    server = server_factory(_respond_401)

    with pytest.raises(VaultEnumerationError):
        fetch_vault_listing(server.base_url, _TOKEN, verify_tls=False, timeout=2.0)


def test_fetch_refuses_to_follow_a_redirect(
    server_factory: Callable[[Callable[[BaseHTTPRequestHandler], None]], _TestServer],
) -> None:
    """The security property named in this unit's brief, proven against a real redirect response
    rather than asserted about opener configuration: red if a 302 (to anywhere -- same host or a
    different one) is ever followed rather than refused. Refusing every redirect is a strict
    superset of "never follow one to another host" (client.py's module docstring)."""

    def _respond_redirect(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(302)
        handler.send_header("Location", "http://attacker.example.invalid/steal")
        handler.end_headers()

    server = server_factory(_respond_redirect)

    with pytest.raises(VaultEnumerationError):
        fetch_vault_listing(server.base_url, _TOKEN, verify_tls=False, timeout=2.0)


def test_a_fetch_failure_never_echoes_the_token_in_its_error_message(
    server_factory: Callable[[Callable[[BaseHTTPRequestHandler], None]], _TestServer],
) -> None:
    """The other security property named in this unit's brief. Red if the token this test passes in
    ever appears in the raised exception's message -- which is exactly what `server.py`'s poll loop
    logs verbatim via `str(exc)`."""

    def _respond_500(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(500)
        handler.end_headers()

    server = server_factory(_respond_500)

    with pytest.raises(VaultEnumerationError) as exc_info:
        fetch_vault_listing(server.base_url, _TOKEN, verify_tls=False, timeout=2.0)

    assert _TOKEN not in str(exc_info.value)


def test_fetch_raises_on_connection_refused() -> None:
    """No server listening at all (unlike every other test here) -- red if a connection failure is
    ever anything other than `VaultEnumerationError`, e.g. an uncaught `ConnectionRefusedError`
    reaching `server.py`'s poll loop, which only catches the former."""
    with pytest.raises(VaultEnumerationError):
        fetch_vault_listing("http://127.0.0.1:1", _TOKEN, verify_tls=False, timeout=2.0)
