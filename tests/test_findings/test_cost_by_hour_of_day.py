"""tests/test_findings/test_cost_by_hour_of_day.py

Proves, against tests/fixtures/hour_of_day.py's own workspace 9301 rows, that
findings.f_cost_by_hour_of_day (grain [workspace_id, weekday_num, hour_of_day,
billing_origin_product]) correctly prices list-priced spend per weekday x hour-of-day x product,
broadcasts job_runs_active / queries_started once per (workspace, weekday, hour) onto every
product row of that slot, and computes share_of_window_spend / status on the hour-of-day alone
(collapsed across every weekday and product): CRITICAL when one hour-of-day holds >= 25% of the
workspace's window spend, WARN at >= 15%, OK otherwise.

Every expectation below is computed by hand from tests/fixtures/hour_of_day.py's own scenario
table (a single fixed weekday, $1000 total window spend) and cross-checked against it -- never
read back from the model under test.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "cost_by_hour_of_day.yml"
WINDOWS = (7, 30, 90)
QID = "cost_by_hour_of_day"
WS = "9301"


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _rows(window_days: int) -> list[dict]:
    return [r for r in dbutil.rows(QID, window_days) if r["workspace_id"] == WS]


def _by_hour_product(window_days: int) -> dict:
    return {(r["hour_of_day"], r["billing_origin_product"]): r for r in _rows(window_days)}


def test_grain_uniqueness():
    grains = _grains()
    cols = grains[QID]
    for w in WINDOWS:
        out = dbutil.rows(QID, w)
        assert out, w
        keys = [tuple(r[c] for c in cols) for r in out]
        assert len(keys) == len(set(keys)), f"w={w}: duplicate grain rows"


def test_window_days_zero_is_empty():
    assert dbutil.rows(QID, 0) == []


def test_status_enum():
    for r in dbutil.rows(QID, 30):
        assert r["status"] in {"OK", "WARN", "CRITICAL"}


# =====================================================================================================
# Hour 2: $300 across two products (JOBS $200 + ALL_PURPOSE $100) of a $1000 window -> share 30% ->
# CRITICAL on BOTH product rows; job_runs_active (3 distinct runs) is broadcast identically onto
# both, and queries_started is 0 (no query activity in this slot).
# =====================================================================================================
def test_critical_hour_both_products_and_broadcast_job_runs():
    for w in WINDOWS:
        by_hp = _by_hour_product(w)
        jobs_row = by_hp[(2, "JOBS")]
        ap_row = by_hp[(2, "ALL_PURPOSE")]
        assert jobs_row["usd_list"] == 200.0
        assert ap_row["usd_list"] == 100.0
        for r in (jobs_row, ap_row):
            assert r["hour_usd_list"] == 300.0
            assert r["window_usd_list"] == 1000.0
            assert r["share_of_window_spend"] == 30.0
            assert r["status"] == "CRITICAL"
            assert r["job_runs_active"] == 3
            assert r["queries_started"] == 0
            assert r["price_basis"] == "priced"


# =====================================================================================================
# Hour 9: $180 (SQL) of $1000 -> share 18% -> WARN ([15, 25)); 4 queries started, 0 job runs.
# =====================================================================================================
def test_warn_hour_and_queries_started():
    for w in WINDOWS:
        r = _by_hour_product(w)[(9, "SQL")]
        assert r["usd_list"] == 180.0
        assert r["share_of_window_spend"] == 18.0
        assert r["status"] == "WARN"
        assert r["queries_started"] == 4
        assert r["job_runs_active"] == 0


# =====================================================================================================
# Hours 5, 11, 16, 20 ($100 each, share 10%) and hour 23 ($120, share 12%) all stay under
# :warn_hour_share_pct (15) -> OK, with no job/query activity (both counts default to 0).
# =====================================================================================================
def test_ok_hours_below_warn_share():
    for w in WINDOWS:
        by_hp = _by_hour_product(w)
        for hour, usd, share in ((5, 100.0, 10.0), (11, 100.0, 10.0), (16, 100.0, 10.0),
                                  (20, 100.0, 10.0), (23, 120.0, 12.0)):
            r = by_hp[(hour, "JOBS")]
            assert r["usd_list"] == usd
            assert r["share_of_window_spend"] == share
            assert r["status"] == "OK"
            assert r["job_runs_active"] == 0
            assert r["queries_started"] == 0


# =====================================================================================================
# The whole workspace's window spend is exactly $1000, split across exactly 7 hour-of-day slots (8
# rows: hour 2 has two product rows) -- proves no stray row and no double-counting.
# =====================================================================================================
def test_window_total_and_row_count():
    for w in WINDOWS:
        rows = _rows(w)
        assert len(rows) == 8
        assert sum(r["usd_list"] for r in rows) == 1000.0
