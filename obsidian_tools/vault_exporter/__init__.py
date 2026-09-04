"""ADR-0037's independent vault-loaded exporter (unit D1, ot#121).

The whole job: make an authenticated call to Obsidian's Local REST API that enumerates vault
content, assert the result is non-empty, and expose that as a Prometheus gauge plus a last-success
timestamp. Nothing here mounts the vault volume or touches git — the only I/O is HTTP, to one fixed
host.

Deliberately not a probe (ADR-0037): a probe converts observation into restart, and the failure this
exists to catch — a permissions condition at the volume mount root — is one a restart does not fix.
This package's output is read, never acted on automatically.

Split the same way `local_replicator/drift.py` is split from `local_replicator/cycle.py`:
`enumeration.py` and `metrics.py` are pure (no network, no sockets, no threads) — given an
enumeration response or a metrics snapshot, what does it mean and how is it rendered. `client.py`
and `server.py` are the impure shell that makes the HTTP call, holds the one piece of mutable state
across polls, and serves it.
"""

from __future__ import annotations
