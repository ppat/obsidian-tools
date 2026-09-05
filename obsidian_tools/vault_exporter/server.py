"""The impure shell: a background poll of Obsidian's Local REST API, serving what it last observed
as Prometheus text exposition on `/metrics`.

Deliberately two independent loops rather than a scrape-triggered call: fetching from Obsidian on
every Prometheus scrape would tie this exporter's own availability to Obsidian's (a slow or wedged
Obsidian would hold the scrape connection open for as long as it takes to time out), and would make
the scrape interval double as the enumeration-call interval with no way to decouple them. Polling in
the background instead means `/metrics` always answers instantly from memory, and a slow or
unreachable Obsidian shows up as a stale `..._success_timestamp_seconds`, not a failed or slow
scrape.

**Clean shutdown, and why it differs from this codebase's other subcommands.** `commit`/`replicate`/
`drain` are one-shot: their SIGTERM concern is unwinding a partial git/filesystem mutation before it
corrupts state (`cli.py`'s own module docstring). This subcommand never mutates anything -- no git,
no filesystem, no vault write -- so there is no partial-mutation window to protect. What a clean stop
means here instead: **release the listening socket and stop the poll thread**, so a
`terminationGracePeriod` stop doesn't leave a orphaned listener behind and the next pod can bind the
same port immediately. `serve()`'s `try`/`finally` below does exactly that, unconditionally, whether
`GracefulShutdown` (`cli.py`) or anything else is what ends `serve_forever()`.

No use for `obsidian_tools.retry.retry_with_backoff` here, considered and rejected rather than
omitted by oversight: that module's stated rationale is the vault volume's NFS soft-mount semantics,
which don't apply to this cluster-internal HTTP call, and the poll loop's own cadence already *is* a
bounded retry with a much longer backoff (`poll_interval_seconds`, default 60s) than
`retry_with_backoff`'s own maximum (20s) -- adding a second, faster retry layer inside one poll
would only mask sub-minute blips at the cost of blocking that poll for longer, with no evidence this
call has the kind of transient-failure profile that module exists for. If that evidence shows up
later, `fetch_vault_listing` is the one place a retry would wrap.
"""

from __future__ import annotations

import logging
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from obsidian_tools.config import VaultExporterConfig
from obsidian_tools.vault_exporter.client import VaultEnumerationError, fetch_vault_listing
from obsidian_tools.vault_exporter.enumeration import decode_enumeration_response
from obsidian_tools.vault_exporter.metrics import VaultMetricsState, render_prometheus_text

logger = logging.getLogger(__name__)


def _poll_once(config: VaultExporterConfig, state: VaultMetricsState) -> None:
    """One enumeration attempt. Never raises -- a failure is logged and the poll loop moves on to
    wait for the next cycle; see the module docstring for why that (not a faster in-call retry) is
    this exporter's whole retry story."""
    try:
        raw_body = fetch_vault_listing(
            config.obsidian_base_url,
            config.obsidian_api_key,
            verify_tls=config.verify_tls,
            timeout=config.request_timeout_seconds,
        )
    except VaultEnumerationError as exc:
        # Deliberately no state mutation here: `obsidian_vault_files_total` and
        # `..._success_timestamp_seconds` both stay exactly where the last success left them (or
        # absent, if there hasn't been one yet) -- see metrics.py's docstring for why that's the
        # signal, not a gap.
        logger.warning(
            "vault enumeration request failed", extra={"event": "vault_enumeration_failed", "error": str(exc)}
        )
        return

    outcome = decode_enumeration_response(raw_body)
    if outcome.healthy:
        state.record_success(outcome.file_count, time.time())
        logger.info(
            "vault enumeration succeeded",
            extra={"event": "vault_enumeration_succeeded", "file_count": outcome.file_count},
        )
    else:
        # A 200 with an empty or malformed body is *not* success (ADR-0037's "non-empty" assertion,
        # enumeration.py) -- logged distinctly from a request failure since operators diagnose the
        # two differently (a code/response-shape problem vs. a connectivity/auth one), but both
        # leave the metrics state exactly where it was, for the same reason.
        logger.warning(
            "vault enumeration returned an unhealthy result",
            extra={"event": "vault_enumeration_unhealthy", "reason": outcome.reason, "file_count": outcome.file_count},
        )


def _poll_loop(config: VaultExporterConfig, state: VaultMetricsState, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        _poll_once(config, state)
        stop_event.wait(config.poll_interval_seconds)


def _build_handler(state: VaultMetricsState) -> type[BaseHTTPRequestHandler]:
    """A handler class bound to `state` by closure rather than a module-level global -- keeps
    `VaultMetricsState` an explicit collaborator `serve()` constructs and owns, the same way
    `cycle.py` constructs and owns its own collaborators (survey's §5)."""

    class MetricsHandler(BaseHTTPRequestHandler):
        server_version = "obsidian-tools-vault-exporter/1"

        def do_GET(self) -> None:
            if self.path != "/metrics":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = render_prometheus_text(state.snapshot())
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # matches the base class's own parameter name
            # Structured JSON, not BaseHTTPRequestHandler's default stderr line (logging_config.py's
            # convention: fields in `extra`, never composed into the message string). `format` and
            # `args` (the default combined-log-format pieces) are dropped in favor of the fields
            # this codebase's log pipeline actually indexes on.
            logger.info(
                "metrics request served",
                extra={"event": "metrics_request_served", "client": self.address_string(), "path": self.path},
            )

    return MetricsHandler


def serve(config: VaultExporterConfig) -> int:
    """Block serving `/metrics` until interrupted (in practice, `cli.py`'s SIGTERM-raised
    `GracefulShutdown`, which this function does not catch -- it propagates to `main()`, same as
    every other subcommand). Starts the background poll thread, then blocks the calling thread
    inside `serve_forever()`; the `finally` is what makes the shutdown clean regardless of how this
    function is left (see module docstring).
    """
    state = VaultMetricsState()
    stop_event = threading.Event()
    poll_thread = threading.Thread(
        target=_poll_loop, args=(config, state, stop_event), name="vault-enumeration-poll", daemon=True
    )
    poll_thread.start()

    httpd = ThreadingHTTPServer((config.listen_host, config.listen_port), _build_handler(state))
    # Logs the *bound* port, not `config.listen_port` -- identical when a fixed port is configured,
    # but correct when `listen_port=0` (used by this package's own SIGTERM-shutdown test) asks the
    # OS to pick one.
    bound_host, bound_port = httpd.server_address[0], httpd.server_address[1]
    logger.info(
        "metrics server listening",
        extra={"event": "metrics_server_started", "host": bound_host, "port": bound_port},
    )
    try:
        httpd.serve_forever()
        return 0
    finally:
        stop_event.set()
        httpd.server_close()
        poll_thread.join(timeout=config.poll_interval_seconds)
