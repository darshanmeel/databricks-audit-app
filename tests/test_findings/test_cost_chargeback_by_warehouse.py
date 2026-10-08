"""tests/test_findings/test_cost_chargeback_by_warehouse.py.

f_cost_chargeback_by_warehouse reads system.billing.usage + system.billing.list_prices +
system.query.history (tests/fixtures/chargeback_a.py, workspace cba.WS_WH) and chargebacks list-
priced DBU spend per (workspace_id, warehouse_id), current window vs the equal-length window right
before it (DEC-62/DEC-66.1, the same band and NOT_ASSESSED coverage guard cost_period_over_period
uses). Every expected number below is hand-derived directly from chargeback_a.py's own scenario
map (reproduced in that file's module docstring).
"""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from dbutil import rows  # noqa: E402
import chargeback_a as cba  # noqa: E402

QID = "cost_chargeback_by_warehouse"


def _by_warehouse(rows_: list[dict]) -> dict:
    return {r["warehouse_id"]: r for r in rows_ if not r["is_other"]}


def test_grain_is_unique_and_window_days_is_set():
    rows_30 = rows(QID, 30, workspace_ids=[cba.WS_WH])
    assert len(rows_30) >= 1
    for row in rows_30:
        assert row["window_days"] == 30
    keys = [(r["workspace_id"], r["warehouse_id"]) for r in rows_30]
    assert len(keys) == len(set(keys)), "duplicate (workspace_id, warehouse_id) grain"


def test_ok_band_and_query_cost_efficiency():
    """cba_wh_ok: cur=30, prev=28 -> change 7.1% -> OK. 3 query.history rows -> queries=3,
    usd_per_1000_queries = 30/3*1000 = 10000.0."""
    by = _by_warehouse(rows(QID, 30, workspace_ids=[cba.WS_WH]))
    r = by["cba_wh_ok"]
    assert r["usd_list"] == 30.0
    assert r["prev_usd_list"] == 28.0
    assert r["change_pct"] == 7.1
    assert r["status"] == "OK"
    assert r["is_other"] is False
    assert r["queries"] == 3
    assert r["usd_per_1000_queries"] == 10000.0


def test_warn_band_at_exact_threshold():
    """cba_wh_warn: cur=125, prev=100 -> change 25.0% -> WARN (inclusive boundary)."""
    r = _by_warehouse(rows(QID, 30, workspace_ids=[cba.WS_WH]))["cba_wh_warn"]
    assert r["change_pct"] == 25.0
    assert r["status"] == "WARN"


def test_critical_band():
    """cba_wh_crit: cur=200, prev=100 -> change 100% -> CRITICAL."""
    r = _by_warehouse(rows(QID, 30, workspace_ids=[cba.WS_WH]))["cba_wh_crit"]
    assert r["change_pct"] == 100.0
    assert r["status"] == "CRITICAL"


def test_brand_new_spend_is_critical():
    """cba_wh_new: cur=50, no previous row at all -> eff_previous_cost resolves to a real 0 ->
    CRITICAL (brand-new spend above the floor with nothing to compare against)."""
    r = _by_warehouse(rows(QID, 30, workspace_ids=[cba.WS_WH]))["cba_wh_new"]
    assert r["usd_list"] == 50.0
    assert r["prev_usd_list"] == 0.0
    assert r["status"] == "CRITICAL"


def test_current_period_unpriced_reads_not_assessed():
    """cba_wh_na_cur: current window is entirely unpriced (no list_price row) while the previous
    window is priced -> NOT_ASSESSED / current_period_unpriced."""
    r = _by_warehouse(rows(QID, 30, workspace_ids=[cba.WS_WH]))["cba_wh_na_cur"]
    assert r["usd_list"] is None
    assert r["price_basis"] == "unpriced"
    assert r["status"] == "NOT_ASSESSED"
    assert r["not_assessed_reason"] == "current_period_unpriced"


def test_previous_period_unpriced_reads_not_assessed():
    """cba_wh_na_prev: previous window is entirely unpriced while current is priced ->
    NOT_ASSESSED / previous_period_unpriced (never a false CRITICAL from a NULL previous)."""
    r = _by_warehouse(rows(QID, 30, workspace_ids=[cba.WS_WH]))["cba_wh_na_prev"]
    assert r["usd_list"] == 50.0
    assert r["prev_usd_list"] is None
    assert r["status"] == "NOT_ASSESSED"
    assert r["not_assessed_reason"] == "previous_period_unpriced"


def test_window_boundaries_and_as_of_day_excluded():
    """cba_wh_win: D(5)=5, D(20)=7, D(45)=11, D0(today)=999 (always excluded). Expected sums:
    5 @ w=7, 12 @ w=30, 23 @ w=90."""
    row_7 = _by_warehouse(rows(QID, 7, workspace_ids=[cba.WS_WH]))["cba_wh_win"]
    row_30 = _by_warehouse(rows(QID, 30, workspace_ids=[cba.WS_WH]))["cba_wh_win"]
    row_90 = _by_warehouse(rows(QID, 90, workspace_ids=[cba.WS_WH]))["cba_wh_win"]

    assert row_7["usd_list"] == 5.0
    assert row_30["usd_list"] == 12.0
    assert row_90["usd_list"] == 23.0


def test_top_n_pooling_cap_holds_and_pooling_actually_happens():
    """25 warehouses (cba_wh_pool_00..24) each carry $1 current-period spend with no previous data
    -> all status=OK. :top_n defaults to 20, so at most 20 of them are ever individually listed
    (the cap can never be exceeded), and since 25 > 20 at least one must be pooled (real pooling
    occurred) -- see chargeback_a.py's module docstring for why an exact split cannot be pinned
    here. Every non-OK warehouse in this file's own scenarios (WARN/CRITICAL/NOT_ASSESSED above)
    is confirmed elsewhere to always keep is_other=False regardless of this cap."""
    all_rows = rows(QID, 30, workspace_ids=[cba.WS_WH])
    pool_rows = [r for r in all_rows if not r["is_other"] and str(r["warehouse_id"]).startswith("cba_wh_pool_")]
    assert len(pool_rows) <= 20
    assert len(pool_rows) < cba.TOP_N_POOL
    for r in pool_rows:
        assert r["status"] == "OK"
