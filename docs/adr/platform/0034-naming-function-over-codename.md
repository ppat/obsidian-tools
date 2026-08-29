# 0034. Infrastructure is named by function, never by the project codename

**Status:** Accepted

## Context

The project's codename is BRAIN. The namespace was nearly named for it — three attempts ran
`brain` → `obsidian-brain` → `obsidian-vault`, each wrong for a different reason — and the volume
was nearly `brain-vault`.

## Decision

Identifiers say what a thing does: the namespace is `obsidian-vault` (matching the sibling app
namespaces named by function), the volume is `vault-data` (the fleet's established data-volume
suffix — and deliberately *not* named for the application, because it holds vault content only,
never the application's own state, which is ephemeral by design —
[ADR-0033](./0033-volume-and-deployment-shape.md)). A `-system` suffix was rejected because in this
fleet it marks operators and control planes other namespaces consume, which this workload is not;
bare `obsidian` was rejected as naming the software rather than the purpose. Repository names
(`obsidian-vault`, `obsidian-tools`) follow the same rule; BRAIN survives as prose, not as an
identifier.

## Consequences

One caveat made the rename free only by timing: pruning is disabled fleet-wide, so a namespace
rename after anything is deployed strands the old objects for manual deletion. The rename landed
before first deployment and cost nothing; the next one would not.
