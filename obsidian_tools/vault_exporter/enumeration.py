"""Pure classification: given the bytes Obsidian's Local REST API returned for `GET /vault/`, what
is the file count and is this a healthy result. No network, no sockets — `client.py` is the only
thing that talks to Obsidian; this module never imports it.

The endpoint's shape (established from the plugin's own published OpenAPI spec,
coddingtonbear/obsidian-local-rest-api `docs/openapi.yaml`, `/vault/` operation): a 200 response
body is a JSON object with a `files` array of strings, one entry per top-level vault entry, with
directories suffixed `/` (e.g. `{"files": ["note.md", "10-areas/"]}`). This is a *top-level* listing,
not a recursive walk of the whole vault -- sufficient for ADR-0037's assertion ("the returned list is
non-empty"), which needs evidence the vault opened at all, not a full inventory.

"Non-empty" is asserted literally, as ADR-0037 and ot#121 word it: `file_count >= 1`. Deliberately
resisting a higher threshold -- any specific number is an assumption about vault content that
today's contents would satisfy and tomorrow's bulk import or archive purge might not, and a
threshold that drifts out of date fails in the direction of false alarms.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast


@dataclass(frozen=True, slots=True)
class EnumerationOutcome:
    """The pure verdict over one enumeration response. `file_count` is always the number of `files`
    entries found, even when `healthy` is False (e.g. `0` for an empty vault) -- `reason` is empty
    exactly when `healthy` is True, and otherwise names why not, for the caller's log line."""

    file_count: int
    healthy: bool
    reason: str


def _unhealthy(reason: str) -> EnumerationOutcome:
    return EnumerationOutcome(file_count=0, healthy=False, reason=reason)


def classify_enumeration_payload(payload: object) -> EnumerationOutcome:
    """Classify an already-JSON-decoded response body. Total: every input produces an
    `EnumerationOutcome`, never an exception -- a malformed payload is simply an unhealthy verdict,
    since "the response didn't have a usable files list" is exactly the kind of fact this exporter
    exists to surface, not to raise past.
    """
    if not isinstance(payload, dict):
        return _unhealthy("response body was not a JSON object")

    files: object = cast("dict[str, object]", payload).get("files")
    if not isinstance(files, list):
        return _unhealthy("response had no 'files' array")

    entries = cast("list[object]", files)
    if not all(isinstance(entry, str) for entry in entries):
        return _unhealthy("'files' array contained a non-string entry")

    count = len(entries)
    if count == 0:
        return _unhealthy("'files' array was empty")
    return EnumerationOutcome(file_count=count, healthy=True, reason="")


def decode_enumeration_response(raw_body: bytes) -> EnumerationOutcome:
    """`classify_enumeration_payload`, plus the JSON decode step -- still pure (no I/O, just parsing
    bytes already read into memory by `client.fetch_vault_listing`). A body that isn't valid JSON at
    all is the same kind of unhealthy verdict as a validly-JSON-but-wrong-shaped one, not a special
    case the caller has to catch separately.
    """
    try:
        payload: object = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        return _unhealthy(f"response body was not valid JSON: {exc}")
    return classify_enumeration_payload(payload)
