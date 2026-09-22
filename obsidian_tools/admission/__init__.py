"""The admission validator (unit A4, ot#6): Gate 5, the decisive gate at the curated boundary.

One shared check with three callers — `batch-processor`, `promotion-processor`, the lint pass's own
fixes — fired on every write that crosses into curated space, whoever carries it (ADR-0007). Its
callers depend on it; it depends on nothing but `vault_schema/`, and never on a caller.

`validator.py` holds the whole of it: `admit(path, content) -> Verdict`, pure, returning a decision
rather than enacting one. What a refusal *does* — dead-letter a chunk, withhold a fix, quarantine a
promotion — is each caller's, because only the caller holds the transaction the refusal belongs to.
"""
