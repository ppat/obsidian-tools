"""Tests for `obsidian_tools/batch_producer/producer.py` and the `enqueue-batch` entrypoint — the
paths that resolve before any broker is contacted.

The end-to-end run against a real broker lives in `tests/test_batch_producer_nats.py`, beside the
fixture that provides one.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from obsidian_tools.batch_producer.producer import EXIT_FAILED, EXIT_OK, new_batch_id
from obsidian_tools.batch_producer.producer import run as run_batch
from obsidian_tools.commands import enqueue_batch
from obsidian_tools.config import BatchProducerConfig

_SCRUBBED_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True, env=_SCRUBBED_GIT_ENV)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    work_tree = tmp_path / "work"
    work_tree.mkdir()
    _git("init", "-q", "--initial-branch=main", cwd=work_tree)
    _git("config", "user.email", "t@example.invalid", cwd=work_tree)
    _git("config", "user.name", "t", cwd=work_tree)
    (work_tree / "seed.md").write_bytes(b"seed\n")
    _git("add", "-A", cwd=work_tree)
    _git("commit", "-qm", "seed", cwd=work_tree)
    return work_tree


def config_for(repo: Path, **overrides: object) -> BatchProducerConfig:
    base: dict[str, object] = {
        "git_dir": str(repo / ".git"),
        "work_tree": str(repo),
        "base_rev": "HEAD",
        # Deliberately unreachable: every test in this file must return before any connection is
        # attempted, so a test that regresses into contacting a broker fails here rather than
        # silently reaching out to whatever happens to be listening.
        "nats_url": "nats://127.0.0.1:1",
        "nats_user": "batch-producer",
        "nats_password": "pw",
        "nats_inbox_prefix": "_INBOX_BATCH",
        "subject_prefix": "batch",
        "max_chunk_patch_bytes": 262144,
        "connect_timeout_seconds": 1.0,
        "publish_timeout_seconds": 1.0,
        "max_reconnect_attempts": 1,
    }
    return BatchProducerConfig(**{**base, **overrides})  # type: ignore[arg-type]


def test_an_empty_index_succeeds_without_contacting_the_broker(repo: Path) -> None:
    """Red if an empty staging area produces a run: the producer would connect, publish nothing, and
    report success indistinguishably from a run that had work — and it would do so on every
    invocation. The unreachable broker URL is what makes this falsifiable rather than assumed."""
    assert run_batch(config_for(repo)) == EXIT_OK


def test_a_file_diff_larger_than_the_budget_is_refused_before_connecting(repo: Path) -> None:
    """A file's diff is the indivisible atom, so an oversized one cannot be placed. Red if this is
    discovered at the broker instead: the failure would name a subject rather than the file, and it
    would be discovered after part of the batch had already been enqueued."""
    (repo / "big.md").write_bytes(b"x\n" * 5000)
    _git("add", "-A", cwd=repo)

    assert enqueue_batch.run(config_for(repo, max_chunk_patch_bytes=100)) == EXIT_FAILED


def test_a_non_utf8_file_is_refused_before_connecting(repo: Path) -> None:
    """Red if the refusal happens after the connection is open, or as an unhandled exception rather
    than an exit code — the entrypoint's whole job is turning every named refusal into one."""
    (repo / "latin.md").write_bytes(b"caf\xe9\n")
    _git("add", "-A", cwd=repo)

    assert enqueue_batch.run(config_for(repo)) == EXIT_FAILED


def test_a_git_failure_is_an_exit_code_not_a_traceback(repo: Path) -> None:
    """Red if a `GitCommandError` escapes the entrypoint. A batch generated against a base revision
    that does not exist is an operator error, and it must report as one."""
    (repo / "new.md").write_bytes(b"new\n")
    _git("add", "-A", cwd=repo)

    assert enqueue_batch.run(config_for(repo, base_rev="no-such-rev")) == EXIT_FAILED


def test_an_unreachable_broker_is_an_exit_code_not_a_traceback(repo: Path) -> None:
    """The one test here that does attempt a connection, to the address nothing listens on. Red if a
    connection failure escapes as an exception rather than becoming an exit code."""
    (repo / "new.md").write_bytes(b"new\n")
    _git("add", "-A", cwd=repo)

    assert enqueue_batch.run(config_for(repo)) == EXIT_FAILED


def test_a_generated_batch_id_is_a_legal_subject_token() -> None:
    """The batch id becomes a subject token, so anything outside that alphabet would be rejected by
    `validate_chunk` at encode time — after the whole batch had been generated. Red if the generator
    ever produces uppercase, hyphens or dots, which `uuid4()`'s dashed string form does."""
    from obsidian_tools.batch_producer.chunk import is_valid_batch_id

    assert all(is_valid_batch_id(new_batch_id()) for _ in range(50))
