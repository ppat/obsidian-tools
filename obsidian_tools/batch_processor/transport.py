"""The stdlib HTTP plumbing both of this component's HTTP seams sit on.

`mcp_client.py` (the gated MCP path) and `agent_instance.py` (the Kubernetes API) each carry a
bearer credential that grants real authority, and each needs the same three things: TLS whose
verification is a deliberate per-connection choice, a refusal to follow redirects, and a response
delivered as status plus body rather than as an exception. Those are properties of the connection,
not of either protocol, so they live once here — the same argument that puts every git invocation
behind `GitRunner` (ADR-0046), applied at a smaller scale.

**Never follow a redirect.** Both credentials are bearer tokens sent on every request; a redirect
would let one response retarget the next request, sending the token somewhere the configuration
never named. Neither endpoint has any legitimate reason to issue one, and one of the two tokens is a
service-account token the API server would honour from anywhere it was replayed.

Stdlib `urllib`, deliberately, matching `vault_exporter/client.py` and `vault_git/known_hosts.py`:
`pyproject.toml`'s runtime dependency list is spent one entry at a time, and a bearer header, a
timeout and a POST do not require an HTTP library.
"""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import Message
from typing import IO, cast


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict[str, str])


class TransportUnreachableError(RuntimeError):
    """Nothing was decided: a timeout, a refused connection, a TLS failure, a refused redirect.

    Distinct from any status the peer actually returned, because the caller's reaction differs —
    this is the retryable one, and every status is a verdict of some kind.
    """


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Raising `HTTPError` is the base class's own way of refusing once its redirect budget runs
    out, so the caller needs no separate exception path."""

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


def build_opener(verify_tls: bool, *, ca_path: str | None = None) -> urllib.request.OpenerDirector:
    """An opener that refuses redirects and verifies TLS unless told otherwise.

    `verify_tls=False`'s effect is scoped to this opener's own `SSLContext` and never touches
    global `ssl` state.

    `ca_path` replaces the system trust store rather than adding to it, which is the point for the
    Kubernetes API server: its certificate is signed by the cluster's own CA, which no public store
    knows, and trusting that CA *alongside* the public roots would leave the connection satisfied by
    any publicly-issued certificate for the same name. An unreadable or malformed file fails here,
    at construction, rather than on the first request.
    """
    context = ssl.create_default_context(cafile=ca_path)
    if not verify_tls:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    # `build_opener` replaces the default instance of any handler class a passed-in handler
    # subclasses (its own documented behaviour), so passing `_RefuseRedirects` — rather than adding
    # it alongside the default handler — is what makes refusal the only redirect behaviour in play.
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), _RefuseRedirects())


def send(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> HttpResponse:
    """One request. Raises `TransportUnreachableError` only when nobody answered.

    A non-2xx response is returned rather than raised, body included: for a JSON-RPC peer the body
    of an error status is often the whole explanation, and discarding it leaves an operator holding
    a bare status code. Error text is built from the URL and the underlying exception, never from
    the request headers — both callers put a bearer token there.
    """
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            return HttpResponse(
                status=cast("int", response.status),
                body=cast("bytes", response.read()),
                headers=dict(cast("Message[str, str]", response.headers).items()),
            )
    except urllib.error.HTTPError as exc:
        return HttpResponse(status=exc.code, body=exc.read(), headers=dict(exc.headers.items()))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransportUnreachableError(f"{method} {url} did not complete: {exc}") from exc
