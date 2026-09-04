"""The one seam that talks to Obsidian's Local REST API -- stdlib `urllib.request`, the same
approach `obsidian_tools/vault_git/known_hosts.py` already established in this codebase for "a
stdlib-only authenticated-ish HTTP call with a short timeout and an explicit failure type". No new
runtime dependency: two gauges and one GET request do not genuinely need a Prometheus client library
or an HTTP client library (CLAUDE.md's dependency-list rule), and `urllib.request.Request(headers=)`
already covers everything this call needs -- a bearer header, a timeout, and (below) redirect
refusal.

`GET {base_url}/vault/` returns the vault's top-level entries (see `enumeration.py`'s docstring for
where that endpoint shape comes from). Authentication is `Authorization: Bearer <token>` -- the
plugin's OpenAPI spec's `apiKeyAuth` security scheme (`scheme: bearer`, `type: http`).

Two things this module must never do, because the token in play grants full read/write to the
authoritative vault with no path-scoped permissions of its own (the same fact that ruled out
blackbox for this check, ADR-0037's alternatives section):

- **Never log or echo the token.** `VaultEnumerationError`'s messages below are built from the URL
  and the underlying exception's own text -- neither ever contains the `Authorization` header value.
- **Never follow a redirect.** A purpose-built exporter calling one fixed, configured host doesn't
  have blackbox's "credentials attached per-module, target per-probe" aiming problem, but it would
  reintroduce a narrower version of it if a redirect could retarget a single request -- so
  `_RefuseRedirects` below refuses every redirect outright (a strict superset of "never follow one to
  *another host*": simpler to implement and simpler to verify than comparing hostnames per redirect,
  and this endpoint has no legitimate reason to ever issue one).
"""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request
from email.message import Message
from typing import IO


class VaultEnumerationError(RuntimeError):
    """The enumeration call failed -- a network error, a timeout, a non-2xx response, or a refused
    redirect. Never constructed with the bearer token in its message; see the module docstring."""


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Raising `HTTPError` from `redirect_request` is the sanctioned way to refuse a redirect (it's
    what the base class itself does once `max_redirections` is exceeded) -- the caller below already
    treats any `HTTPError`/`URLError` as a fetch failure, so no separate exception path is needed."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message[str, str],
        newurl: str,
    ) -> urllib.request.Request | None:
        raise urllib.error.HTTPError(
            req.full_url, code, f"refused to follow a redirect (HTTP {code}) to {newurl!r}", headers, fp
        )


def fetch_vault_listing(base_url: str, api_key: str, *, verify_tls: bool, timeout: float) -> bytes:
    """Fetch the raw response body of `GET {base_url}/vault/`. Raises `VaultEnumerationError` on any
    failure; callers pass the returned bytes to `enumeration.decode_enumeration_response`, which is
    pure and never touches the network.

    `verify_tls=False`'s effect is scoped narrowly to this one connection's `SSLContext` -- it never
    changes global `ssl` module state, unlike `ssl._create_default_https_context` monkeypatching.
    """
    url = f"{base_url.rstrip('/')}/vault/"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})

    ssl_context = ssl.create_default_context()
    if not verify_tls:
        # The plugin generates its own self-signed certificate at runtime -- see
        # `VaultExporterConfig.verify_tls`'s docstring for why there is no stable cert to pin.
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    # build_opener replaces the default instance of any handler class a passed-in handler
    # subclasses (per its own docs), so passing `_RefuseRedirects` here -- not adding it alongside
    # the default `HTTPRedirectHandler` -- is what makes refusal the only redirect behavior in play.
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl_context), _RefuseRedirects())
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.read()  # type: ignore[no-any-return]
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise VaultEnumerationError(f"vault enumeration request to {url} failed: {exc}") from exc
