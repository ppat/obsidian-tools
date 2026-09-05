"""Tests for `obsidian_tools/vault_exporter/server.py`.

Two real-HTTP integration tests, no mocking: `test_metrics_handler_...` drives the handler through a
real `ThreadingHTTPServer` on an ephemeral port to prove the `/metrics` wiring (the handler reads
`VaultMetricsState` correctly, unknown paths 404). `test_serve_shuts_down_cleanly_on_sigterm` proves
the long-running subcommand's whole reason for being different from `commit`/`replicate`/`drain`:
that a SIGTERM actually releases the listening socket. It runs in a subprocess, for the same reason
`tests/test_cli.py::test_installed_sigterm_handler_raises_graceful_shutdown` does: sending a real
SIGTERM to the pytest process itself would kill the *runner*, not just the subject under test, if
anything about signal delivery here were ever wrong.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from obsidian_tools.vault_exporter.metrics import VaultMetricsState
from obsidian_tools.vault_exporter.server import _build_handler  # pyright: ignore[reportPrivateUsage]

# --------------------------------------------------------------------------------------------
# The /metrics handler -- real HTTP, no poll thread (state is set directly)
# --------------------------------------------------------------------------------------------


def test_metrics_handler_serves_current_state_and_404s_on_other_paths() -> None:
    """Red if the handler ever serves a stale/wrong body, the wrong content type, or answers a
    non-/metrics path with anything but 404 -- each assertion below is independently falsifiable."""
    state = VaultMetricsState()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _build_handler(state))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2.0) as response:
            before_body = response.read()
            content_type = response.headers.get("Content-Type")
        # See test_vault_exporter_metrics.py for why the newline-prefixed form is what's checked.
        assert b"\nobsidian_vault_files_total " not in before_body
        assert content_type is not None
        assert content_type.startswith("text/plain")

        state.record_success(3, 111.0)

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2.0) as response:
            after_body = response.read()
        assert b"obsidian_vault_files_total 3" in after_body
        assert b"obsidian_vault_enumeration_success_timestamp_seconds 111.0" in after_body

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=2.0)
        assert exc_info.value.code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# --------------------------------------------------------------------------------------------
# serve() -- real SIGTERM, in a subprocess (see module docstring for why)
# --------------------------------------------------------------------------------------------


def _free_port() -> int:
    """A port the kernel says is free right now.

    Not a constant: two runs of this test overlapping -- a re-run started while a previous child is
    still shutting down, or a suite run beside another -- would collide on a fixed port, and the
    child would fail to bind rather than exercise the shutdown path. The test then reports a
    shutdown defect that is not there.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_serve_shuts_down_cleanly_on_sigterm() -> None:
    """This subcommand is long-running, unlike `commit`/`replicate`/`drain` -- `server.py`'s module
    docstring concludes a clean shutdown here means releasing the listening socket and stopping the
    poll thread, not protecting a partial git mutation (there is none to protect). Proven two ways:
    the child process's traceback shows `GracefulShutdown` (SIGTERM was converted, not fatal), and
    the parent can bind the identical port immediately afterward (red if `serve()`'s `finally` ever
    stops calling `httpd.server_close()`, e.g. during a future refactor of the shutdown path)."""
    port = _free_port()
    script = f"""
import os, signal, socket, sys, threading, time
from obsidian_tools import cli
from obsidian_tools.config import VaultExporterConfig
from obsidian_tools.vault_exporter.server import serve

cli.install_signal_handlers()

config = VaultExporterConfig(
    obsidian_base_url="http://127.0.0.1:1",
    obsidian_api_key="test-token",
    verify_tls=False,
    poll_interval_seconds=0.05,
    request_timeout_seconds=1.0,
    listen_host="127.0.0.1",
    listen_port={port},
)

def _terminate_once_listening():
    # Loudly, not silently: if the server never listens, exiting this thread quietly leaves
    # serve() running until the parent's subprocess timeout, and the failure then looks like a
    # hung shutdown rather than a server that never started.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.connect(("127.0.0.1", {port}))
            break
        except OSError:
            time.sleep(0.02)
        finally:
            probe.close()
    else:
        print("NEVER_LISTENED", file=sys.stderr, flush=True)
        os._exit(3)
    os.kill(os.getpid(), signal.SIGTERM)

threading.Thread(target=_terminate_once_listening, daemon=True).start()
serve(config)
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15)

    assert "GracefulShutdown" in result.stderr, (
        f"expected a GracefulShutdown traceback from the child process; "
        f"got returncode={result.returncode!r} stderr={result.stderr!r}"
    )

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", port))
    finally:
        probe.close()
