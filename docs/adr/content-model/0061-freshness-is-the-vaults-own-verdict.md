# 0061. Freshness is the vault system's own evidenced verdict, stamped in `verified:` only with the evidence recorded

**Status:** Proposed (supersedes the `reviewed:`-age staleness signal in [ADR-0018](./0018-lint-pass-policy.md)) ·
**Pillar:** [The vault system does its own job](../../../DESIGN.md#the-vault-system-does-its-own-job) ·
**Serves:** [S2](../../../USE_CASES.md#s2--sound) ·
**Unit:** [A5](../../../ROADMAP.md#group-a--pipeline-mechanisms)

## Context

Each domain overlay sets how fast a claim goes stale, and stale-claim detection measured that dial by
`reviewed:` age. But `reviewed:` is set by a human, and review never transfers authority
([ADR-0009](./0009-three-field-provenance-split.md)). Under the owner's standing ruling the human is
not a reviewer and nothing waits on him, so a detector keyed on `reviewed:` age marks every note
stale forever. Stamping `reviewed:` from a model's verdict would forge a human-actor field.

A freshness verdict is also a claim of its own, so it needs evidence. The facts the dial exists for
are perishable ones that depend on no other note — a restaurant's opening hours, say. The vault
cannot check them against itself; only the source the claim cites can. And a stamp that nothing
could prove wrong carries no information.

## Decision

**Freshness is judged by the vault system on recorded evidence, and stamped in its own field.**

- **The evidence.** The lint pass gives the agent runtime two sources
  ([ADR-0054](../platform/0054-agent-runtime.md)):
  - the notes a note depends on (`refs:`), read through its own access;
  - the external sources a claim cites in its inline provenance, fetched by the **evidence fetcher**.
    This is a separate component that holds no credential and no mount. The citations are written
    by clients, so the fetch is kept away from every credential the lint pass holds. Its containment
    is specified, not assumed:
    - **Addresses.** It resolves each name once, rejects any address that is not globally
      routable, and connects to the address it validated. That defeats DNS rebinding. Rejected
      addresses include loopback, private, carrier-grade NAT, link-local, cloud metadata, unique
      local, and IPv4-mapped IPv6 forms of any of those.
    - **Redirects.** Every redirect hop is re-validated the same way, up to a small bound.
    - **Scheme and proxy.** Only `https` and `http`. No proxy taken from the environment.
    - **Bounds.** Response size and time are capped.
    - **Callers.** A network policy admits only the lint pass as a caller — with no credential of
      its own, the fetcher has nothing else to lean on — and allows egress to public destinations
      only, as defence in depth behind its own address checks.
    - **Output.** It returns only the fetched content, its hash and the address it came from.
- **`verified:`** — the date a claim's support was last confirmed. It is stamped only when the
  runtime's verdict comes with recorded evidence: each source's location, when it was retrieved, a
  hash of its content, and the passage relied on. The record goes in the audit trail. A verdict
  without that evidence never stamps. Anyone can check a stamp by re-reading the recorded passage, so
  a stamp can be shown wrong.
- **What a stale-claim finding resolves to:**
  - supported, with evidence: stamp `verified:`;
  - contradicted by the evidence: annotate the note and lower `confidence:`;
  - **a failure on the fetcher's side** — every fetch in the pass failing, or *every* one of a small
    set of canary fetches of known public pages failing: nothing is judged that pass. The findings
    are retried, and nothing decays because the vault system's own reach is broken. One lost canary
    page does not stall judgement; the loss is recorded and the canary set is changed in a release.
    A failure on the fetcher's side that lasts longer than an area's dial is emitted as a residue
    ([ADR-0064](../operability/0064-operational-conditions-have-resolvers.md));
  - **a source unreachable for a transient reason** (a timeout, or a 5xx response) on a pass whose
    canaries succeeded: retried on the next pass;
  - **no cited source at all, or a source dead** — the name does not resolve, or the source answers
    404 or 410, on every pass *on which the canaries were healthy* across the area's dial. Passes with
    a failure on the fetcher's side are skipped, not counted. Then: lower `confidence:` one step and
    annotate the reason. An unsourced perishable claim decays, as it should.

  The first two end the finding until the dial runs out again or a dependency changes.
- **When a note is stale.** Either a note it depends on changed after its freshness baseline, or its
  baseline is older than the area's dial. The baseline is the latest of `verified:`, `reviewed:` and
  `updated:` (or `created:` when a note has never been updated). A note is never stale on arrival,
  and never exempt forever.
- **`verified:` sits under the no-raise guard's evidence rule.** Moving it forward is a trust-bearing
  act, and ADR-0055's resolutions may do it only with recorded evidence. `reviewed:` stays an
  optional human-actor field that no component stamps.
- **The field comes with the schema's release** ([ADR-0063](./0063-schema-published-from-this-repository.md)).
  No manual schema edit is needed.

## Alternatives considered

- **Keying staleness on `reviewed:` age** — only a human could close the finding.
- **Stamping `reviewed:` from the runtime's verdict** — forges a human-actor field.
- **A verdict with no evidence** — a model asserting freshness is unfalsifiable, and it would renew
  trust indefinitely.
- **Checking a claim only against other notes** — the perishable external facts the dial exists for
  cannot be checked that way.

## Consequences

- **The vault system reaches outside only through the evidence fetcher**, which holds nothing worth
  stealing and can reach nothing inside. Injected content in a fetched page can at worst produce a
  wrong verdict within the closed set, and the evidence record makes that verdict checkable.
- **Dropping external evidence altogether was weighed and rejected, on the whole set's reading, not
  handed to the owner.** Without it, `verified:` would be possible only for claims grounded in other
  notes, and every perishable external fact — the travel and dining overlays' whole reason to exist
  — would decay on its dial, with nothing in the vault system able to confirm it. Keeping those
  claims honest is S2, the vault system's own job, and nothing may wait on a person to do it. A
  contained fetcher is the smallest mechanism that does that job.
- **Required injection, for the implementation:** a citation that resolves to, or redirects to, a
  private or metadata address is refused; and with the fetcher's egress broken, no note decays.
- **C3's factual-grounding sample uses the same evidence path**, so every sampled verdict is
  checkable ([ROADMAP](../../../ROADMAP.md#group-c--content-work)).
