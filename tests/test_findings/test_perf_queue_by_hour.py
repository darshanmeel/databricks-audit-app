"""tests/test_findings/test_perf_queue_by_hour.py.

Proves findings.f_perf_queue_by_hour against raw query.history: statements, queued statements and
waits add up, hours are 0-23, and the grain holds.
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest
import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

QID = "perf_queue_by_hour"


def _raw(window_days: int) -> tuple:
    pattern = (dbutil.PARQUET_DIR / "query__history" / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        return con.execute(f"""
            SELECT COUNT(*),
                   SUM(CASE WHEN COALESCE(waiting_at_capacity_duration_ms, 0) > 0 THEN 1 ELSE 0 END),
                   SUM(COALESCE(waiting_at_capacity_duration_ms, 0)) / 1000.0,
                   SUM(CASE WHEN COALESCE(waiting_for_compute_duration_ms, 0) > 0 THEN 1 ELSE 0 END),
                   SUM(COALESCE(waiting_for_compute_duration_ms, 0)) / 1000.0
            FROM read_parquet('{pattern}', union_by_name=true)
            WHERE compute.warehouse_id IS NOT NULL
              AND start_time >= DATE '{dbutil.TEST_TODAY}' - INTERVAL {window_days} DAY
              AND start_time < DATE '{dbutil.TEST_TODAY}'
        """).fetchone()
    finally:
        con.close()


@pytest.mark.parametrize("window_days", [7, 30, 90])
def test_grain_uniqueness(window_days):
    cols = yaml.safe_load((ROOT / "config" / "grains" / f"{QID}.yml").read_text(encoding="ascii"))[QID]
    out = dbutil.rows(QID, window_days)
    assert out
    keys = [tuple(r[c] for c in cols) for r in out]
    assert len(keys) == len(set(keys)), "duplicate grain rows"


@pytest.mark.parametrize("window_days", [7, 30, 90])
def test_counts_and_waits_add_up_to_raw(window_days):
    raw = _raw(window_days)
    out = dbutil.rows(QID, window_days)
    assert raw[0] > 0
    assert sum(r["runs"] for r in out) == raw[0]
    assert sum(r["queued_runs"] for r in out) == raw[1]
    assert sum(r["provision_runs"] for r in out) == raw[3]
    # Each row rounds its seconds to 0.1.
    assert abs(sum(r["slot_wait_s"] for r in out) - raw[2]) <= 0.05 * len(out) + 1e-6
    assert abs(sum(r["provision_s"] for r in out) - raw[4]) <= 0.05 * len(out) + 1e-6


@pytest.mark.parametrize("window_days", [7, 30, 90])
def test_hours_and_shares_in_range(window_days):
    for r in dbutil.rows(QID, window_days):
        assert 0 <= r["usage_hour"] <= 23
        assert 0 <= r["queued_runs"] <= r["runs"]
        assert 0 <= r["provision_runs"] <= r["runs"]
