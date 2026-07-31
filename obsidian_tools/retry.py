"""Small retry-with-backoff helper for operations against the read-only NFS vault mount.

The vault volume is a soft-mounted NFS export (``softerr``, ``timeo=600,retrans=5``): soft-mount
semantics mean I/O returns an error rather than hanging when the server is briefly unreachable, so
a read can fail outright under ordinary NFS trouble. That is expected, not exceptional — a run that
bails on the first failure would fail far more often than the underlying storage actually warrants.
This module exists so callers retry bounded and back off, rather than each reinventing that loop.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

# Module-level, not baked into call-site default arguments, specifically so tests can turn these
# down (monkeypatch the attributes here) without threading retry-tuning parameters through every
# function that ends up calling git with retry=True.
DEFAULT_RETRIES = 5
DEFAULT_BASE_DELAY_SECONDS = 1.0
DEFAULT_MAX_DELAY_SECONDS = 20.0


class RetryExhaustedError(RuntimeError):
    """Raised when every retry attempt has failed. Chains the last underlying failure."""


def retry_with_backoff[T](
    func: Callable[[], T],
    *,
    description: str,
    retries: int | None = None,
    base_delay: float | None = None,
    max_delay: float = DEFAULT_MAX_DELAY_SECONDS,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    logger: logging.Logger | None = None,
    sleep: Callable[[float], None] | None = None,
) -> T:
    """Call ``func``, retrying with exponential backoff on any exception in ``retry_on``.

    Raises ``RetryExhaustedError`` once every attempt has failed, so a persistently-failing read
    surfaces as one clear exception rather than the caller inspecting attempt counts itself.
    """
    resolved_retries = DEFAULT_RETRIES if retries is None else retries
    resolved_base_delay = DEFAULT_BASE_DELAY_SECONDS if base_delay is None else base_delay
    # Resolved here rather than bound as this parameter's default value, so a test can monkeypatch
    # `time.sleep` itself (module-attribute lookup, done fresh on every call) instead of needing to
    # thread a fake sleep function through every layer that ends up calling this with retry=True.
    sleep_fn = sleep if sleep is not None else time.sleep
    log = logger or logging.getLogger(__name__)
    last_error: BaseException | None = None

    for attempt in range(1, resolved_retries + 1):
        try:
            return func()
        except retry_on as exc:
            last_error = exc
            if attempt == resolved_retries:
                break
            delay = min(resolved_base_delay * (2 ** (attempt - 1)), max_delay)
            log.warning(
                "retrying after failure",
                extra={
                    "event": "retry",
                    "operation": description,
                    "attempt": attempt,
                    "retries": resolved_retries,
                    "delay_seconds": delay,
                    "error": str(exc),
                },
            )
            sleep_fn(delay)

    raise RetryExhaustedError(f"{description} failed after {resolved_retries} attempts") from last_error
