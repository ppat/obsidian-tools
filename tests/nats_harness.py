"""Running a real `nats-server` in a container for a test module, and refusing to pretend it ran.

Not a test module: shared by `test_batch_producer_nats.py` (the producer's authority injections)
and `test_batch_processor_stream.py` (the consumer's FIFO, redelivery and dead-letter behaviour).
Both need the same three things — a broker, a way to wait for it, and a rule about what happens
when there is no Docker daemon — and a second copy of any of them would drift.

**Skip locally, fail in CI.** These modules carry violation injections whose whole value is that
they fire; a skip and a pass are indistinguishable in an exit code, so on a runner where Docker is
expected the absence of a broker has to be a failure. Otherwise a green suite is evidence the
injections were never attempted, which is exactly the shape this repository's testing discipline
refuses.

Set `OBSIDIAN_TOOLS_TEST_NATS_HOST` when the Docker daemon is remote: it publishes the container's
port on its own host, not on this one.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager

import pytest

# renovate: datasource=docker depName=nats versioning=docker
NATS_TAG = "2.14-alpine"
IMAGE = f"nats:{NATS_TAG}"
HOST = os.environ.get("OBSIDIAN_TOOLS_TEST_NATS_HOST", "127.0.0.1")


def docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


def broker_unavailable(reason: str) -> None:
    if os.environ.get("CI"):
        pytest.fail(f"{reason} -- broker-backed injections cannot be skipped in CI")
    pytest.skip(reason)


def wait_for_port(port: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((HOST, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


@contextmanager
def running_broker(server_config: str, port: int) -> Generator[str]:
    """A `nats-server` carrying `server_config`, torn down afterwards. Yields its URL.

    The configuration is written by the container's own shell rather than bind-mounted: a bind
    mount resolves on the *daemon's* filesystem, so it silently mounts nothing when the daemon is
    remote — and then the broker starts with its defaults and every permission test passes for the
    wrong reason.
    """
    if shutil.which("docker") is None or docker("info", "--format", "{{.ServerVersion}}").returncode != 0:
        broker_unavailable("no reachable Docker daemon; this test needs a real nats-server")

    name = f"obsidian-tools-nats-{uuid.uuid4().hex[:8]}"
    started = docker(
        "run",
        "-d",
        "--name",
        name,
        "-p",
        f"{port}:4222",
        "--entrypoint",
        "sh",
        IMAGE,
        "-c",
        f"cat > /tmp/n.conf <<'NATSCONF'\n{server_config}\nNATSCONF\nexec nats-server -c /tmp/n.conf",
    )
    if started.returncode != 0:
        broker_unavailable(f"could not start {IMAGE}: {started.stderr.strip()}")
    try:
        if not wait_for_port(port, timeout_seconds=30):
            logs = docker("logs", name).stderr
            broker_unavailable(f"nats-server did not accept connections on {HOST}:{port}: {logs}")
        yield f"nats://{HOST}:{port}"
    finally:
        docker("rm", "-f", name)
