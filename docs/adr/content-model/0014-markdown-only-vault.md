# 0014. The vault receives markdown only; conversion happens outside the boundary

**Status:** Accepted ·
**Serves:** [S1](../../../USE_CASES.md#s1--admitted), [S4](../../../USE_CASES.md#s4--retrievable)

## Context

Content arrives as PDFs, video links, audio, and documents. Storing binaries in the vault would
grow git history permanently, bloat the replicated payload to every device, and defeat the
durability constraint (a binary is not greppable in fifty years). A binary also has no frontmatter
— nothing for the validator to admit or refuse, so it would sit outside the ownership contract
entirely.

## Decision

Markdown only. Video, audio, PDFs and documents are converted to markdown **before** entering, and
the conversion lives outside the vault boundary — in whichever clients own a capture channel (a
conversational assistant, a workflow engine, for example), out of scope for this design. Everything enters through the inbox after
conversion; the bootstrap pile is converted first and then lands via the batch stream. Images are
wanted but deferred past the first pass; `_attachments/` keeps a committed placeholder and the
attachment-location setting is locked day one (both retroactively painful), so nothing has to move
later — the folder holds nothing yet.

## Alternatives considered

- Binaries in an attachments folder — permanent git history growth, large device payloads, and
  contract-less content.
- Originals stored by reference — dangling references to systems the durability constraint refuses
  to depend on.

## Consequences

- "Bulk import" is *not a component*: conversion (out of scope) + patches authored in a git
  working tree by whatever the operator uses, enqueued by the batch producer in an owner-authorised
  run ([B1](../../../ROADMAP.md#group-b--connection-work)) + application by `batch-processor`
  ([A2](../../../ROADMAP.md#group-a--pipeline-mechanisms)). Anyone looking for an import pipeline to
  build will not find one, correctly.
- The replicated payload stays small — which is what makes whole-vault (rather than curated-subset)
  device replication affordable ([ADR-0027](../replication/0027-icloud-transport.md)).
- A pasted binary on a device is not representable upstream; the capture gate withholds the cycle
  rather than destroy it ([ADR-0025](../replication/0025-replication-cycle.md)).
