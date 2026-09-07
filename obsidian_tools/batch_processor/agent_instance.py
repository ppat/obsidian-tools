"""The one seam onto the agent MCP instance a batch run stops, and the watchdog's single pass.

"Batch mode" is not a mode: it is entirely which of the two MCP instances is running (ADR-0052,
ADR-0022, ADR-0003). The agent instance's Deployment is scaled to zero for the duration of a run so
interactive writers cannot race the batch through the editor's single event loop; the ingestor
instance is untouched, which is what lets `promotion-processor` keep draining mid-batch and what
keeps the run's own write path open. This module is the only thing in the package that touches the
Kubernetes API.

## Why two writes in a fixed order rather than one atomic write

The stop and the lease cannot travel in one request, because one request would mean writing the
Deployment's own body — and that authority is refused. A caller able to write the body can change
the image and can change `OBSIDIAN_WRITE_PATHS`, which *is* Gate 2. So the grant is the `scale`
subresource and a separate `Lease`, and **ordering replaces atomicity**:

| Order | What a crash between the two writes leaves |
| --- | --- |
| `begin_batch_run`: take the lease, **then** stop the instance | Instance running, lease held and expiring |
| `end_batch_run`: start the instance, **then** release the lease | Instance running, stale lease |

Both interruptions leave the instance **running**, so "stopped with no lease" stays unreachable by
any partial run — which is what lets the watchdog keep reading that state as an operator's own hold
and leave it alone forever (`watchdog.py`). Reverse either pair and that reading becomes false: a
crash would strand the instance at zero with nothing to say a run did it.

## Every write names only the fields it owns

Both patches are `application/merge-patch+json` bodies carrying one `spec` and nothing else. The
predecessor of this seam had to read-modify-write because the API it drove replaced its whole record
on update; a merge patch does not, so the read is gone and with it the window between it and the
write. What survives from that argument is the discipline it protected: a write must never carry a
field it did not set out to change. The `Lease` ships in git with an empty `spec`, so nothing that
reconciles the declared state owns the fields written here.

## What is unverified here

The namespace, the Deployment name and the Lease name are deployment facts, and each is required
configuration with no default for the same reason the MCP tool names are: the values must match the
`resourceNames` in the RBAC grant character for character, and a default that disagreed with the
grant would present as a 403 at the first call of every run. Nothing in this repository has run
against the deployed cluster, so this seam is where a mismatch surfaces, and it is deliberately
small for that reason: two objects, four requests, one integer and one timestamp.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from obsidian_tools.batch_processor.transport import TransportUnreachableError, build_opener, send
from obsidian_tools.batch_processor.watchdog import (
    AgentInstanceStatus,
    WatchdogVerdict,
    decide,
    lease_deadline,
)
from obsidian_tools.config import AgentInstanceConfig

logger = logging.getLogger(__name__)

MERGE_PATCH = "application/merge-patch+json"

RUNNING_REPLICAS = 1
"""What the instance is restored to. A constant, not a remembered value, and that is the whole
reason a *count* was chosen over narrowing the instance's write scope for the run (ADR-0052): a
restore that put back a stored value would, when it went wrong, leave the instance running under a
scope nobody chose — writes permitted where they must not land, which no watchdog can detect. A
failed restore of a count leaves the instance stopped, which is loud and is the watchdog's whole
subject. The instance is single-replica by design; a second pod would be a second caller of the
editor's single event loop."""

STOPPED_REPLICAS = 0


class AgentInstanceError(RuntimeError):
    """The Kubernetes API could not be reached, or answered something this seam cannot read."""


class AgentInstanceClient:
    """The agent MCP instance's Deployment scale and the batch lease beside it."""

    def __init__(
        self,
        *,
        api_url: str,
        namespace: str,
        deployment: str,
        lease_name: str,
        holder_identity: str,
        token_path: str,
        ca_path: str | None,
        timeout_seconds: float,
        verify_tls: bool,
    ) -> None:
        self._url = api_url.rstrip("/")
        self._scale_path = f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}/scale"
        self._lease_path = f"/apis/coordination.k8s.io/v1/namespaces/{namespace}/leases/{lease_name}"
        self._lease_name = f"{namespace}/{lease_name}"
        self._holder = holder_identity
        self._token_path = token_path
        self._timeout = timeout_seconds
        self._opener = build_opener(verify_tls, ca_path=ca_path if verify_tls else None)

    # --- what the watchdog reads ------------------------------------------------------------------

    def status(self) -> AgentInstanceStatus:
        scale = self._get(self._scale_path, "the agent instance's scale")
        lease = _object(self._get(self._lease_path, "the batch lease").get("spec"), "the lease's spec")
        return AgentInstanceStatus(
            running=_replicas(scale) > 0,
            lease_expires_at=lease_deadline(
                _timestamp(lease.get("renewTime"), "renewTime"),
                _number(lease.get("leaseDurationSeconds"), "leaseDurationSeconds"),
            ),
        )

    # --- the run's two ordered pairs --------------------------------------------------------------

    def begin_batch_run(self, renewed_at: datetime, ttl_seconds: float) -> None:
        """Take the lease, then stop the instance. Never the other way round — see the docstring."""
        self.acquire_lease(renewed_at, ttl_seconds)
        self.stop_instance()

    def end_batch_run(self) -> None:
        """Start the instance, then release the lease. Never the other way round.

        The lease must go with it: a held lease on a running instance is a fact about a run that has
        already finished, and the next hand-stop of the instance would meet it and be undone.
        """
        self.start_instance()
        self.release_lease()

    def renew_lease(self, renewed_at: datetime) -> None:
        """Push `renewTime` forward, and change nothing else.

        Deliberately *not* re-asserting the stop, and deliberately not re-asserting the holder or
        the duration. Doing any of them looks like cheap self-healing and is actually two jobs
        braided into one call: the renewal would then also be a stop, which both hides a missing
        `begin_batch_run` (a run whose instance was never stopped would still end up stopped, and no
        test could tell) and silently overrides whoever changed the instance mid-run. Renewing says
        one thing — this processor is still alive.
        """
        self._patch(self._lease_path, {"spec": {"renewTime": _micro_time(renewed_at)}}, "renew the batch lease")

    # --- the four primitives, separately named because each is a crash seam ------------------------

    def acquire_lease(self, renewed_at: datetime, ttl_seconds: float) -> None:
        stamp = _micro_time(renewed_at)
        self._patch(
            self._lease_path,
            {
                "spec": {
                    # Names which run holds the door. `concurrencyPolicy: Forbid` stops a second
                    # *scheduled* run, never a hand-run Job, so this is the only thing that makes an
                    # overlap visible after the fact.
                    "holderIdentity": self._holder,
                    "acquireTime": stamp,
                    "renewTime": stamp,
                    "leaseDurationSeconds": int(ttl_seconds),
                }
            },
            "acquire the batch lease",
        )

    def release_lease(self) -> None:
        """Clear every field this seam writes. A merge patch deletes a key given `null`, which is
        what lets the release be a patch rather than a replace — and why the grant needs no
        `delete` on the object."""
        self._patch(
            self._lease_path,
            {"spec": {"holderIdentity": None, "acquireTime": None, "renewTime": None, "leaseDurationSeconds": None}},
            "release the batch lease",
        )

    def stop_instance(self) -> None:
        self._scale_to(STOPPED_REPLICAS)
        logger.info(
            "the agent MCP instance is stopped for a batch run",
            extra={"event": "agent_instance_stopped", "holder_identity": self._holder},
        )

    def start_instance(self) -> None:
        self._scale_to(RUNNING_REPLICAS)
        # Desired replicas, not a running pod. The door reopens when the pod is Ready, which is
        # later than this line by however long the image takes to start and pass its probe.
        logger.info("the agent MCP instance is asked to run again", extra={"event": "agent_instance_started"})

    # --- the transport ----------------------------------------------------------------------------

    def _scale_to(self, replicas: int) -> None:
        """Patch the `scale` subresource and check what came back.

        The response is the updated `Scale`, so the count it reports is the API server's own account
        of what it stored. Trusting the 200 alone would let a restore that changed nothing report
        success — the failure mode this whole component exists to make impossible, arrived at from
        the inside.
        """
        answer = self._patch(self._scale_path, {"spec": {"replicas": replicas}}, f"scale to {replicas}")
        stored = _replicas(answer)
        if stored != replicas:
            raise AgentInstanceError(
                f"asked for {replicas} replica(s) on the agent instance and the API server stored {stored}"
            )

    def _get(self, path: str, what: str) -> dict[str, object]:
        return self._json(path, method="GET", body=None, what=what)

    def _patch(self, path: str, body: dict[str, object], what: str) -> dict[str, object]:
        return self._json(path, method="PATCH", body=json.dumps(body).encode("utf-8"), what=what)

    def _json(self, path: str, *, method: str, body: bytes | None, what: str) -> dict[str, object]:
        url = f"{self._url}{path}"
        headers = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = MERGE_PATCH
        try:
            response = send(self._opener, url, method=method, headers=headers, body=body, timeout=self._timeout)
        except TransportUnreachableError as exc:
            raise AgentInstanceError(f"could not {what}: {exc}") from exc
        if response.status == 404 and path == self._lease_path:
            # The grant carries no `create` verb, because RBAC cannot restrict `create` by
            # `resourceNames` and granting it would widen the grant to every Lease in the namespace.
            # So nothing here can recreate the object, and failing loud is the correct behaviour —
            # but only if the message says what is missing, or this reads as a permissions bug.
            raise AgentInstanceError(
                f"the batch lease {self._lease_name!r} does not exist; it ships in the deployment's manifests "
                "and this workload holds no authority to create it"
            )
        if response.status != 200:
            raise AgentInstanceError(f"could not {what}: HTTP {response.status}: {response.body[:200]!r}")
        try:
            decoded: object = json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AgentInstanceError(f"could not {what}: the answer was not JSON: {exc}") from exc
        if not isinstance(decoded, dict):
            raise AgentInstanceError(f"could not {what}: the answer was a {type(decoded).__name__}, expected an object")
        return cast("dict[str, object]", decoded)

    def _token(self) -> str:
        """Read the projected token on every request, never once at startup.

        Bound service-account tokens are rotated in place by the kubelet, and a run measured in
        hours that cached its token at startup starts answering 401 partway through — with the
        instance already stopped, which is precisely the state that must not become permanent.
        """
        try:
            return Path(self._token_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AgentInstanceError(f"could not read the service account token at {self._token_path}: {exc}") from exc


def build_instance_client(config: AgentInstanceConfig) -> AgentInstanceClient:
    """The one place the shared instance configuration becomes a client, so the processor and the
    watchdog cannot drift into naming different objects."""
    return AgentInstanceClient(
        api_url=config.api_url,
        namespace=config.namespace,
        deployment=config.deployment,
        lease_name=config.lease_name,
        holder_identity=config.holder_identity,
        token_path=config.token_path,
        ca_path=config.ca_path,
        timeout_seconds=config.timeout_seconds,
        verify_tls=config.verify_tls,
    )


def run_watchdog_once(client: AgentInstanceClient, now: datetime) -> WatchdogVerdict:
    """One watchdog pass: read the instance and the lease, decide, and act on the verdicts that act.

    Everything decided is decided in `watchdog.decide`; this function only reads, dispatches and
    logs. A pass that changes nothing still logs what it saw, because "the watchdog ran and the
    instance was up" and "the watchdog did not run" are otherwise the same silence.
    """
    status = client.status()
    verdict = decide(status, now)
    fields = {
        "event": "watchdog_pass",
        "verdict": verdict.name.lower(),
        "instance_running": status.running,
        "lease_expires_at": None if status.lease_expires_at is None else status.lease_expires_at.isoformat(),
    }
    if verdict is WatchdogVerdict.RESTART_INSTANCE:
        client.end_batch_run()
        logger.warning("started the agent MCP instance: the batch run holding it stopped is gone", extra=fields)
    elif verdict is WatchdogVerdict.RELEASE_STALE_LEASE:
        client.release_lease()
        logger.info("dropped a lease left behind by a run that has finished", extra=fields)
    elif verdict is WatchdogVerdict.STOPPED_BY_SOMEONE_ELSE:
        logger.warning("the agent MCP instance is stopped by something other than a batch run", extra=fields)
    else:
        logger.info("agent MCP instance checked", extra=fields)
    return verdict


def _micro_time(moment: datetime) -> str:
    """Kubernetes `MicroTime`, which is stricter than RFC 3339 and stricter than `isoformat()`.

    The API server parses these fields with Go's `2006-01-02T15:04:05.000000Z07:00`, and a
    zero-padded fractional layout in Go requires *exactly* six digits when parsing. Python's
    `datetime.isoformat()` omits the fraction entirely whenever `microsecond` is zero, so a lease
    stamped on a whole second would be rejected with a 400 — roughly one write in a million, which
    is the worst possible frequency for a bug in the seam that stops interactive writes.
    """
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _object(value: object, what: str) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AgentInstanceError(f"{what} is a {type(value).__name__}, expected an object")
    return dict(cast("dict[str, object]", value))


def _replicas(scale: dict[str, object]) -> int:
    """The *desired* count, from `spec`, never the observed one from `status`. See `watchdog.py`."""
    value = _object(scale.get("spec"), "the scale's spec").get("replicas")
    if value is None:
        # An absent `spec.replicas` is the API server reporting a Deployment whose field nothing
        # owns, which is exactly how this Deployment ships (the manifest omits it so no reconciler
        # restores the pod mid-run). Absent means the default, which is one.
        return RUNNING_REPLICAS
    if not isinstance(value, int) or isinstance(value, bool):
        raise AgentInstanceError(f"the scale's replicas is a {type(value).__name__}, expected an integer")
    return value


def _number(value: object, what: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise AgentInstanceError(f"the lease's {what} is a {type(value).__name__}, expected a number")
    return float(value)


def _timestamp(value: object, what: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentInstanceError(f"the lease's {what} is a {type(value).__name__}, expected a timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AgentInstanceError(f"the lease's {what} {value!r} is not a timestamp: {exc}") from exc
    if parsed.tzinfo is None:
        # A naive deadline cannot be compared with a timezone-aware `now` without inventing an
        # offset, and inventing one here would silently shift the watchdog's trigger by hours.
        raise AgentInstanceError(f"the lease's {what} {value!r} carries no timezone")
    return parsed
