"""The one seam onto the gateway handle a batch run turns off, and the watchdog's single pass.

"Batch mode" is not a mode: it is entirely which gateway handle is enabled (ADR-0022, ADR-0003).
The agent-facing handle goes down for the duration of a run so interactive writers cannot race the
batch through the editor's single event loop; the ingestor handle stays up, which is what lets
`promotion-processor` keep draining mid-batch. This module is the only thing in the package that
touches that switch.

## Why the lease and the disable travel in one request

`watchdog.py` explains why the liveness signal must be a deadline rather than a flag. The other
half of that argument is here: if disabling the handle and stamping the lease were two requests, a
process killed between them would leave a handle disabled with no lease — which the watchdog is
required to read as "an operator did this" and leave alone, permanently. One `POST` carrying both
makes that state unreachable.

## The metadata is read before it is written, and that is not incidental

The gateway's key update replaces the metadata object wholesale, so writing only the lease field
would silently drop every other annotation on the handle. Each write here is therefore
read-modify-write over the current metadata. It is not atomic, and it does not need to be: the only
two writers are a live processor and a watchdog that writes exclusively when it has already
concluded the processor is gone.

## What is unverified here

The endpoint paths, the `blocked` field and the `metadata` object are the gateway's own key-management
API. Nothing in this repository has yet run against the deployed gateway, so this seam is where a
mismatch would surface, and it is deliberately small for that reason: three requests, one field, one
metadata key.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import cast

from obsidian_tools.batch_processor.transport import TransportUnreachableError, build_opener, send
from obsidian_tools.batch_processor.watchdog import AgentHandleStatus, WatchdogVerdict, decide
from obsidian_tools.config import AgentHandleConfig

logger = logging.getLogger(__name__)

# The one metadata key this component owns on the agent handle. Named for what it is rather than
# for the component, because what a reader of the gateway's own configuration needs to know is that
# a batch run is holding the handle down, not which binary is doing it.
LEASE_FIELD = "batch_run_lease_expires_at"


class AgentHandleError(RuntimeError):
    """The gateway could not be reached, or answered something this seam cannot read."""


class AgentHandleClient:
    """The agent handle, as the gateway's key API exposes it."""

    def __init__(
        self,
        *,
        base_url: str,
        admin_key: str,
        handle_key: str,
        timeout_seconds: float,
        verify_tls: bool,
    ) -> None:
        self._url = base_url.rstrip("/")
        self._admin_key = admin_key
        self._handle_key = handle_key
        self._timeout = timeout_seconds
        self._opener = build_opener(verify_tls)

    def status(self) -> AgentHandleStatus:
        info = self._get(f"/key/info?key={self._handle_key}")
        fields = _object(info.get("info"), "info")
        return AgentHandleStatus(
            enabled=not _truthy(fields.get("blocked")),
            lease_expires_at=_parse_lease(_object(fields.get("metadata"), "metadata").get(LEASE_FIELD)),
        )

    def begin_batch_run(self, lease_expires_at: datetime) -> None:
        """Disable the handle and stamp the lease, in one request. See the module docstring."""
        self._update(blocked=True, lease=lease_expires_at)
        logger.info(
            "the agent handle is down for a batch run",
            extra={"event": "agent_handle_disabled", "lease_expires_at": lease_expires_at.isoformat()},
        )

    def renew_lease(self, lease_expires_at: datetime) -> None:
        """Push the deadline forward, and change nothing else.

        Deliberately *not* re-asserting `blocked`. Doing so looks like cheap self-healing and is
        actually two jobs braided into one call: the renewal would then also be a disable, which
        both hides a missing `begin_batch_run` (a run whose handle was never taken down would still
        end up with it down, and no test could tell) and silently overrides whoever changed the
        handle mid-run. Renewing says one thing — this processor is still alive.
        """
        self._update(blocked=None, lease=lease_expires_at)

    def end_batch_run(self) -> None:
        """Re-enable the handle and drop the lease. The lease must go with it: a stale deadline left
        behind on an enabled handle is a fact about a run that has finished."""
        self._update(blocked=False, lease=None)
        logger.info("the agent handle is back up", extra={"event": "agent_handle_enabled"})

    # --- the transport --------------------------------------------------------------------------

    def _update(self, *, blocked: bool | None, lease: datetime | None) -> None:
        """`blocked=None` means "leave it as found" — the gateway's update replaces the whole
        record, so the current value has to be read and written back rather than omitted."""
        current = _object(self._get(f"/key/info?key={self._handle_key}").get("info"), "info")
        metadata = _object(current.get("metadata"), "metadata")
        if lease is None:
            metadata.pop(LEASE_FIELD, None)
        else:
            metadata[LEASE_FIELD] = lease.isoformat()
        resolved = _truthy(current.get("blocked")) if blocked is None else blocked
        self._post("/key/update", {"key": self._handle_key, "blocked": resolved, "metadata": metadata})

    def _get(self, path: str) -> dict[str, object]:
        return self._json(path, method="GET", body=None)

    def _post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        return self._json(path, method="POST", body=json.dumps(payload).encode("utf-8"))

    def _json(self, path: str, *, method: str, body: bytes | None) -> dict[str, object]:
        url = f"{self._url}{path}"
        headers = {"Authorization": f"Bearer {self._admin_key}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = send(self._opener, url, method=method, headers=headers, body=body, timeout=self._timeout)
        except TransportUnreachableError as exc:
            raise AgentHandleError(str(exc)) from exc
        if response.status != 200:
            raise AgentHandleError(f"{method} {path} answered HTTP {response.status}: {response.body[:200]!r}")
        try:
            decoded: object = json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AgentHandleError(f"{method} {path} did not answer with JSON: {exc}") from exc
        if not isinstance(decoded, dict):
            raise AgentHandleError(f"{method} {path} answered with a {type(decoded).__name__}, expected an object")
        return cast("dict[str, object]", decoded)


def build_handle_client(config: AgentHandleConfig) -> AgentHandleClient:
    """The one place the shared handle configuration becomes a client, so the processor and the
    watchdog cannot drift into talking to the handle differently."""
    return AgentHandleClient(
        base_url=config.gateway_url,
        admin_key=config.gateway_admin_key,
        handle_key=config.agent_handle_key,
        timeout_seconds=config.gateway_timeout_seconds,
        verify_tls=config.gateway_verify_tls,
    )


def run_watchdog_once(client: AgentHandleClient, now: datetime) -> WatchdogVerdict:
    """One watchdog pass: read the handle, decide, and act on the one verdict that acts.

    Everything decided is decided in `watchdog.decide`; this function only reads, dispatches and
    logs. A pass that changes nothing still logs what it saw, because "the watchdog ran and the
    handle was healthy" and "the watchdog did not run" are otherwise the same silence.
    """
    status = client.status()
    verdict = decide(status, now)
    fields = {
        "event": "watchdog_pass",
        "verdict": verdict.name.lower(),
        "handle_enabled": status.enabled,
        "lease_expires_at": None if status.lease_expires_at is None else status.lease_expires_at.isoformat(),
    }
    if verdict is WatchdogVerdict.RE_ENABLE:
        client.end_batch_run()
        logger.warning("re-enabled the agent handle: the batch run holding it down is gone", extra=fields)
    elif verdict is WatchdogVerdict.DISABLED_BY_SOMEONE_ELSE:
        logger.warning("the agent handle is disabled by something other than a batch run", extra=fields)
    else:
        logger.info("agent handle checked", extra=fields)
    return verdict


def _object(value: object, what: str) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AgentHandleError(f"the gateway's {what!r} is a {type(value).__name__}, expected an object")
    return dict(cast("dict[str, object]", value))


def _truthy(value: object) -> bool:
    return value is True


def _parse_lease(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentHandleError(f"the handle's {LEASE_FIELD} is a {type(value).__name__}, expected a timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AgentHandleError(f"the handle's {LEASE_FIELD} {value!r} is not a timestamp: {exc}") from exc
    if parsed.tzinfo is None:
        # A naive deadline cannot be compared with a timezone-aware `now` without inventing an
        # offset, and inventing one here would silently shift the watchdog's trigger by hours.
        raise AgentHandleError(f"the handle's {LEASE_FIELD} {value!r} carries no timezone")
    return parsed
