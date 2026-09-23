# 0056. The inbox is promotion's work list: a scheduled sweep announces what no client did

**Status:** Proposed (supersedes the pointer check's formulation — "outside the enqueuer's own scope" — in [ADR-0021](./0021-authority-by-message-shape.md) and [ADR-0047](./0047-subject-scheme-and-account-topology.md): a pointer outside the inbox is refused) ·
**Pillar:** [The vault system does its own job](../../../DESIGN.md#the-vault-system-does-its-own-job) ·
**Serves:** [S3](../../../USE_CASES.md#s3--placed) ·
**Unit:** [A3](../../../ROADMAP.md#group-a--pipeline-mechanisms) ·
**Ticket:** [ot#86](https://github.com/ppat/obsidian-tools/issues/86)

## Context

`promotion-processor` relocates notes out of the inbox into their curated homes, draining the
promotion stream in real time. A message on that stream is a pointer to something already written in
`00-inbox/`, and any client holding the pointer-shape credential may announce what it wrote by
enqueuing one ([ADR-0021](./0021-authority-by-message-shape.md)).

If pointers are promotion's only source of work, placement depends on the writer's cooperation. A
note written by a client that holds no pointer credential, that dies between its write and its
publish, or that simply never announces, stays in the inbox indefinitely — and
[S3](../../../USE_CASES.md#s3--placed)'s first criterion, the inbox emptying unattended, then holds
only for clients that behave. The vault system does not know its clients, so it cannot know which
behave.

## Decision

**The inbox itself is promotion's work list.** A sweep — a scheduled entrypoint of
`promotion-processor` — lists `00-inbox/` through the processor's own read access and enqueues a
pointer on the promotion stream for every note it finds that has no pointer of its own still
pending. A client's pointer only hastens promotion; it is never a precondition for it.

- **Every note leaves the inbox by exactly one of three routes.**
  - It is promoted to its curated home, keeping its file name.
  - It is placed beside the note already there, under a path given a disambiguating suffix, when
    its curated path is already taken. A taken path does not mean the same subject. Whether the two
    are one subject is the roll-up pass's later judgement
    ([ADR-0060](./0060-roll-up-pass-owned-by-promotion-processor.md)). The stem that no longer
    matches its slug is then a lint finding resolved by an alias
    ([ADR-0055](../content-model/0055-lint-findings-resolved-by-the-vault.md)).
  - It is quarantined, because the admission validator refused it. The lint pass re-validates it and
    repairs it where it can. A note it records as re-admissible is relocated out of quarantine by
    `promotion-processor` itself, on the sweep's schedule and directly through its handle, since
    pointers stay inbox-only. Re-admissions are few, and they carry no one's waiting.

  A note whose destination the runtime cannot yet decide — or cannot decide because it is
  unreachable ([ADR-0054](../platform/0054-agent-runtime.md)) — **stays in the inbox**, is counted,
  and is swept again. It is never parked where nothing reads it back. Quarantine is only ever the
  validator's verdict.

- **Swept work travels the same path as announced work**: one consumer, one pointer check, one depth
  metric. Batch backpressure keys on promotion-stream depth
  ([ADR-0022](./0022-batch-stream-mechanics.md)), and swept pointers count toward it even though
  nobody is known to be waiting on them. The sweep bounds that cost to at most one pending pointer
  per inbox note. A pointer that keeps failing is dead-lettered after its delivery budget, so a
  failing note adds at most one pointer per sweep interval, which drains between sweeps. Swept depth
  therefore never holds bulk work back indefinitely.
- **Duplicates are harmless.** A pointer naming a note no longer in the inbox — promoted,
  quarantined, or moved since — settles with nothing to do.
- **The pointer check is a path rule**: any pointer naming a path outside the inbox is refused before
  anything else. It is a rule the processor can evaluate, since a queue credential carries no handle
  scope. The sweep, which lists only the inbox, cannot produce such a pointer.
- **The sweep holds a pointer-shape producer credential** beside the processor's consumer identity
  ([ADR-0051](./0051-consumer-identity-in-stream-account.md)). A credential is defined by the shape of
  the message it may publish, not by who holds it, so this is the existing kind issued to one more
  holder, not a new kind ([ADR-0047](./0047-subject-scheme-and-account-topology.md)).

## Alternatives considered

- **Pointers only** — the gap above: placement rests on client cooperation the vault system cannot
  observe or enforce.
- **Requiring every writing client to announce** — a requirement on parties the vault system does not
  know is not a mechanism, and nothing could detect its violation except the note left behind.
- **The lint pass enqueuing inbox pointers** — it walks the whole vault anyway, but on its own
  cadence and without a producer credential, and it would braid vault maintenance with placement,
  two jobs with different owners.
- **`promotion-processor` relocating what it lists directly, bypassing the stream** — two work paths
  into one processor, and swept work invisible to the backpressure that keeps bulk work from
  starving the paths where someone is waiting. One exception is accepted: quarantine re-admissions,
  which are few, which no one waits on, and which a pointer cannot name, since pointers stay
  inbox-only.

## Consequences

- **An unannounced note waits at most one sweep interval** before promotion takes it; an announced
  one is taken in real time.
- **A note the sweep finds on consecutive passes has not been placed.** Either its promotion keeps
  failing, and its pointer is dead-lettered each time, or its destination is undecided. Both are
  counted per note, apart from the other, and both count against
  [S3](../../../USE_CASES.md#s3--placed): being retried is not being placed.
- **The processor gains two credentials:** the sweep's pointer credential, and the model endpoint's
  for destination judgement.
- **The promotion stream's producers are clients and one component**, each with its own user in the
  pointer shape's account ([ADR-0059](../write-model/0059-one-holder-per-credential-access-record.md)).
  The stream table names the sweep among the producers; the account topology needs no change.
