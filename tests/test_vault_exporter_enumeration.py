"""Tests for `obsidian_tools/vault_exporter/enumeration.py` -- the pure classification of an
Obsidian Local REST API `/vault/` response. Table tests over hand-built payloads, no network, no
HTTP client -- `classify_enumeration_payload` is total (never raises), so every case is expressed as
an input/expected-output pair, in the same style as `tests/test_local_replicator_drift.py`.
"""

from __future__ import annotations

import pytest

from obsidian_tools.vault_exporter.enumeration import (
    EnumerationOutcome,
    classify_enumeration_payload,
    decode_enumeration_response,
)

# --------------------------------------------------------------------------------------------
# classify_enumeration_payload -- already-decoded JSON in, verdict out
# --------------------------------------------------------------------------------------------


def test_a_single_file_is_healthy_with_count_one() -> None:
    """Red if the real, documented single-entry shape is ever misclassified."""
    outcome = classify_enumeration_payload({"files": ["note.md"]})
    assert outcome == EnumerationOutcome(file_count=1, healthy=True, reason="")


def test_files_and_directories_mixed_counts_every_entry() -> None:
    """Red if a directory entry (trailing '/', per the plugin's own example) is ever excluded from
    the count -- ADR-0037 asserts the *list* is non-empty, not "at least one plain file"."""
    outcome = classify_enumeration_payload({"files": ["note.md", "10-areas/", "log.md"]})
    assert outcome == EnumerationOutcome(file_count=3, healthy=True, reason="")


def test_an_empty_files_array_is_unhealthy_with_count_zero() -> None:
    """The exact failure ADR-0037 exists to catch when it manifests as a well-formed empty
    response: a 200 with `{"files": []}`. Red if this is ever classified healthy -- that would
    silently defeat the whole "non-empty" assertion the ticket specifies."""
    outcome = classify_enumeration_payload({"files": []})
    assert outcome.healthy is False
    assert outcome.file_count == 0
    assert "empty" in outcome.reason


def test_a_top_level_object_that_is_not_a_dict_is_unhealthy() -> None:
    """Red if a bare JSON array or scalar at the top level is ever treated as usable."""
    outcome = classify_enumeration_payload(["note.md"])
    assert outcome.healthy is False
    assert outcome.file_count == 0


def test_a_missing_files_field_is_unhealthy() -> None:
    """Red if a response shape that dropped the 'files' key entirely is ever treated as success."""
    outcome = classify_enumeration_payload({"unrelated": "field"})
    assert outcome.healthy is False


@pytest.mark.parametrize(
    "wrong_type",
    [
        pytest.param("not-a-list", id="string-instead-of-list"),
        pytest.param(None, id="null-instead-of-list"),
        pytest.param(42, id="number-instead-of-list"),
        pytest.param({"nested": "object"}, id="object-instead-of-list"),
    ],
)
def test_a_files_field_of_the_wrong_type_is_unhealthy(wrong_type: object) -> None:
    """Red if 'files' being anything other than a JSON array (a plausible plugin regression, e.g.
    an object keyed by path) is ever accepted rather than flagged."""
    outcome = classify_enumeration_payload({"files": wrong_type})
    assert outcome.healthy is False
    assert outcome.file_count == 0


def test_a_files_array_with_a_non_string_entry_is_unhealthy() -> None:
    """Red if an array element that isn't a string (the documented entry type) is ever silently
    counted rather than flagged as a shape this module doesn't recognize."""
    outcome = classify_enumeration_payload({"files": ["note.md", 123]})
    assert outcome.healthy is False


def test_a_large_vault_listing_is_healthy_and_counted_exactly() -> None:
    """Red if the count is ever off-by-one or otherwise wrong for a non-trivial list length."""
    files = [f"note-{i}.md" for i in range(500)]
    outcome = classify_enumeration_payload({"files": files})
    assert outcome == EnumerationOutcome(file_count=500, healthy=True, reason="")


# --------------------------------------------------------------------------------------------
# decode_enumeration_response -- raw bytes in (still pure: just JSON parsing already-read bytes)
# --------------------------------------------------------------------------------------------


def test_decode_parses_a_well_formed_response_body() -> None:
    """Red if valid JSON bytes for the documented shape ever fail to decode into a healthy
    outcome."""
    outcome = decode_enumeration_response(b'{"files": ["note.md", "10-areas/"]}')
    assert outcome == EnumerationOutcome(file_count=2, healthy=True, reason="")


def test_decode_treats_invalid_json_as_unhealthy_not_an_exception() -> None:
    """Red if malformed bytes ever raise out of this function instead of producing a verdict --
    `server.py`'s poll loop relies on this being total so it never needs a second except clause for
    decode failures distinct from classification failures."""
    outcome = decode_enumeration_response(b"not json at all")
    assert outcome.healthy is False
    assert "JSON" in outcome.reason


def test_decode_treats_empty_bytes_as_unhealthy() -> None:
    """Red if an empty response body (e.g. a misbehaving proxy) is ever anything but unhealthy."""
    outcome = decode_enumeration_response(b"")
    assert outcome.healthy is False
