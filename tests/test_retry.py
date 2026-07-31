from __future__ import annotations

import pytest

from obsidian_tools.retry import RetryExhaustedError, retry_with_backoff


def test_retries_transient_failures_then_succeeds() -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return "ok"

    sleeps: list[float] = []
    result = retry_with_backoff(flaky, description="flaky op", retries=5, base_delay=0.01, sleep=sleeps.append)

    assert result == "ok"
    assert calls["n"] == 3
    assert len(sleeps) == 2  # slept between attempts 1->2 and 2->3, not after the final success


def test_gives_up_after_exhausting_retries() -> None:
    def always_fails() -> None:
        raise RuntimeError("permanent")

    with pytest.raises(RetryExhaustedError) as excinfo:
        retry_with_backoff(always_fails, description="doomed op", retries=3, base_delay=0.01, sleep=lambda _: None)

    assert "doomed op" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, RuntimeError)


def test_only_retries_exceptions_in_retry_on() -> None:
    def raises_value_error() -> None:
        raise ValueError("not the kind we retry")

    with pytest.raises(ValueError, match="not the kind we retry"):
        retry_with_backoff(
            raises_value_error,
            description="wrong exception type",
            retries=3,
            base_delay=0.01,
            retry_on=(RuntimeError,),
            sleep=lambda _: None,
        )
