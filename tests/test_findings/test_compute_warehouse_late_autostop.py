"""findings.f_compute_warehouse_late_autostop against tests/fixtures/compute_coverage.py's
`cc_wh_late*` warehouses (window 30, header defaults: late_pct 20, min_late_seconds 60,
warn_late_stops 3). Waits are counted by hand from that module's scenario."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

QID = "compute_warehouse_late_autostop"


def _rows() -> list[dict]:
    return dbutil.rows(QID, 30, workspace_ids=["cc_ws1"])


def _by_wh(warehouse_id: str) -> dict:
    matches = [r for r in _rows() if r["warehouse_id"] == warehouse_id]
    assert len(matches) == 1, f"expected one row for {warehouse_id}, got {len(matches)}"
    return matches[0]


def test_grain_unique():
    keys = [(r["workspace_id"], r["warehouse_id"]) for r in dbutil.rows(QID, 30)]
    assert keys and len(keys) == len(set(keys))


def test_live_pattern_measures_last_query_to_stop():
    # STOPPING, SCALED_DOWN to 0 and STOPPED in one second count as one stop; an 11-minute pause
    # between two queries with no stop in it is not a stop
    r = _by_wh("cc_wh_late")
    assert r["warehouse_kind"] == "serverless"
    assert r["auto_stops"] == 8
    assert r["late_stops"] == 4
    assert r["late_extra_minutes"] == 30.8  # 919 + 432 + 336 + 162 s
    assert r["typical_wait_minutes"] == 11.5  # median of 619 x4, 762, 936, 1032, 1519 s
    assert r["longest_wait_minutes"] == 25.3
    assert r["last_activity_at"] == datetime(2026, 9, 17, 7, 0, 20)
    assert r["stop_at"] == datetime(2026, 9, 17, 7, 25, 39)
    assert r["late_reason"] == "4 of 8 auto-stops came more than 20% after the setting (31 extra min); longest 25.3 min"
    assert r["status"] == "WARN"


def test_manual_and_busy_stops_are_left_out():
    # a stop 2 min after the query is manual; a stop while a query runs is not an auto-stop; a start
    # with no query waits from its RUNNING event
    r = _by_wh("cc_wh_late_edge")
    assert r["auto_stops"] == 3
    assert r["late_stops"] == 2
    assert r["late_extra_minutes"] == 8.0
    assert r["typical_wait_minutes"] == 13.0
    assert r["longest_wait_minutes"] == 15.0
    assert r["last_activity_at"] == datetime(2026, 9, 16, 3, 0, 30)
    assert r["status"] == "OK"


def test_short_autostop_needs_a_minute_over():
    # the third stop waits 100 s after its client's 90 s result fetch, so it is on time
    r = _by_wh("cc_wh_late_short")
    assert (r["auto_stops"], r["late_stops"], r["late_extra_minutes"]) == (3, 1, 1.5)


def test_autostop_off_has_no_row():
    assert not [r for r in _rows() if r["warehouse_id"] == "cc_wh_late_off"]
