"""local-replicator: the Mac-side `replicate` subcommand.

The only component in BRAIN's design that runs outside the cluster (docs/DESIGN.md §2 item 10) —
a launchd job on the operator's Mac, not a Flux-managed workload. Its job is the `PVC → human`
direction: get vault content from the authoritative cluster copy onto the Mac and iOS via iCloud,
non-destructively, without ever becoming a second writer of vault content.

See `cycle.py` for the seven-step cycle (park, overlay, diff, spool, reset/pull, publish, advance)
and its module docstring for the ordering rationale and the one-parked-clone simplification.
"""

from __future__ import annotations
