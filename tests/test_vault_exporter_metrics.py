"""Tests for `obsidian_tools/vault_exporter/metrics.py`.

`render_prometheus_text` is pure (`MetricsSnapshot` in, bytes out) and tested as a table, same style
as `enumeration.py`'s tests. `VaultMetricsState` holds real mutable state but no I/O -- exercised
directly, no threads, no HTTP; the threaded/HTTP path is `tests/test_vault_exporter_server.py`.
"""

from __future__ import annotations

from obsidian_tools.vault_exporter.metrics import MetricsSnapshot, VaultMetricsState, render_prometheus_text

# --------------------------------------------------------------------------------------------
# render_prometheus_text -- pure
# --------------------------------------------------------------------------------------------


def test_before_any_success_both_gauges_are_omitted_entirely() -> None:
    """The falsifiable core of the "distinguishable from a healthy zero" requirement: red if a
    value line for either gauge ever appears before a snapshot has recorded a real success --
    emitting `0` by default would be indistinguishable from a genuine (if impossible, per
    enumeration.py) healthy zero-count result."""
    text = render_prometheus_text(MetricsSnapshot(file_count=None, success_timestamp=None))

    # A value line always immediately follows its TYPE line, i.e. starts right after a newline --
    # searching for the newline-prefixed form (rather than the bare metric name) is what excludes
    # the HELP/TYPE comment lines below, which legitimately contain the metric name too.
    assert b"\nobsidian_vault_files_total " not in text
    assert b"\nobsidian_vault_enumeration_success_timestamp_seconds " not in text
    # The HELP/TYPE lines make the metric discoverable even with no value yet -- red if a scrape
    # before the first poll returns a body with no trace of either metric name at all.
    assert b"# TYPE obsidian_vault_files_total gauge" in text
    assert b"# TYPE obsidian_vault_enumeration_success_timestamp_seconds gauge" in text


def test_after_a_success_both_gauges_carry_their_values() -> None:
    """Red if either value is ever missing, mislabeled, or formatted such that Prometheus's text
    exposition parser would reject the line."""
    text = render_prometheus_text(MetricsSnapshot(file_count=42, success_timestamp=1_700_000_000.0))

    assert b"obsidian_vault_files_total 42" in text
    assert b"obsidian_vault_enumeration_success_timestamp_seconds 1700000000.0" in text


def test_a_healthy_zero_count_is_impossible_to_construct_from_enumeration_but_would_still_render() -> None:
    """Documents the boundary precisely: metrics.py itself does not forbid `file_count=0` in a
    snapshot (that assertion lives in enumeration.py, one layer up) -- if it ever did render, `0`
    is textually distinguishable from the omitted-line case above by definition, so the "no
    default-zero" property holds at this layer regardless of what upstream ever passes in. Red if
    this snapshot's rendering ever collapses into the omitted-line form instead of a real `0`."""
    text = render_prometheus_text(MetricsSnapshot(file_count=0, success_timestamp=5.0))

    assert b"obsidian_vault_files_total 0" in text


# --------------------------------------------------------------------------------------------
# VaultMetricsState -- real mutable state, no I/O
# --------------------------------------------------------------------------------------------


def test_state_starts_with_no_recorded_success() -> None:
    """Red if a freshly constructed state ever reports a snapshot other than all-None."""
    state = VaultMetricsState()

    assert state.snapshot() == MetricsSnapshot(file_count=None, success_timestamp=None)


def test_record_success_is_reflected_in_the_next_snapshot() -> None:
    """Red if a recorded success is ever lost or partially applied."""
    state = VaultMetricsState()

    state.record_success(7, 123.0)

    assert state.snapshot() == MetricsSnapshot(file_count=7, success_timestamp=123.0)


def test_a_later_success_overwrites_an_earlier_one() -> None:
    """Red if state ever accumulates rather than reflecting only the most recent success."""
    state = VaultMetricsState()
    state.record_success(7, 123.0)

    state.record_success(9, 456.0)

    assert state.snapshot() == MetricsSnapshot(file_count=9, success_timestamp=456.0)
