"""tests/test_findings/test_cost_chargeback_by_workspace.py.

f_cost_chargeback_by_workspace reads system.billing.usage + system.billing.list_prices
(tests/fixtures/chargeback_a.py, one dedicated workspace per scenario) and chargebacks list-priced
spend per workspace_id, current window vs the equal-length window right before it. Every expected
number below is hand-derived directly from chargeback_a.py's own scenario map.
"""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from dbutil import rows  # noqa: E402
import chargeback_a as cba  # noqa: E402

QID = "cost_chargeback_by_workspace"

ALL_WS = [cba.WS_OK, cba.WS_WARN, cba.WS_CRIT, cba.WS_NA_CUR, cba.WS_WIN]


def _by_ws(rows_: list[dict]) -> dict:
    return {r["workspace_id"]: r for r in rows_}


def test_grain_is_unique_and_window_days_is_set():
    rows_30 = rows(QID, 30, workspace_ids=ALL_WS)
    assert len(rows_30) >= 1
    for row in rows_30:
        assert row["window_days"] == 30
    keys = [r["workspace_id"] for r in rows_30]
    assert len(keys) == len(set(keys)), "duplicate workspace_id grain"


def test_ok_band():
    """cba_ws_ok: cur=40, prev=38 -> change 5.3% -> OK."""
    r = _by_ws(rows(QID, 30, workspace_ids=ALL_WS))[cba.WS_OK]
    assert r["usd_list"] == 40.0
    assert r["prev_usd_list"] == 38.0
    assert r["change_pct"] == 5.3
    assert r["status"] == "OK"


def test_warn_band_at_exact_threshold():
    """cba_ws_warn: cur=125, prev=100 -> change 25.0% -> WARN."""
    r = _by_ws(rows(QID, 30, workspace_ids=ALL_WS))[cba.WS_WARN]
    assert r["change_pct"] == 25.0
    assert r["status"] == "WARN"


def test_critical_band():
    """cba_ws_crit: cur=200, prev=100 -> change 100% -> CRITICAL."""
    r = _by_ws(rows(QID, 30, workspace_ids=ALL_WS))[cba.WS_CRIT]
    assert r["change_pct"] == 100.0
    assert r["status"] == "CRITICAL"


def test_current_period_unpriced_reads_not_assessed():
    """cba_ws_na_cur: current window entirely unpriced, previous priced -> NOT_ASSESSED /
    current_period_unpriced."""
    r = _by_ws(rows(QID, 30, workspace_ids=ALL_WS))[cba.WS_NA_CUR]
    assert r["usd_list"] is None
    assert r["price_basis"] == "unpriced"
    assert r["status"] == "NOT_ASSESSED"
    assert r["not_assessed_reason"] == "current_period_unpriced"


def test_account_level_row_kept_as_its_own_line():
    """A NULL workspace_id is kept as its own single line, never dropped and never split -
    chargeback_a.py alone contributes $15 (below :min_spend_usd) to it, but another builder's
    fixture could add more, so only the shape (exactly one such row, at least $15) is asserted."""
    acct_rows = [x for x in rows(QID, 30) if x["workspace_id"] is None]
    assert len(acct_rows) == 1, "account-level (NULL workspace_id) usage must collapse to one row"
    assert acct_rows[0]["usd_list"] >= 15.0


def test_window_boundaries_and_as_of_day_excluded():
    """cba_ws_win: D(5)=5, D(20)=7, D(45)=11, D0(today)=999 (always excluded). Expected sums:
    5 @ w=7, 12 @ w=30, 23 @ w=90."""
    row_7 = _by_ws(rows(QID, 7, workspace_ids=[cba.WS_WIN]))[cba.WS_WIN]
    row_30 = _by_ws(rows(QID, 30, workspace_ids=[cba.WS_WIN]))[cba.WS_WIN]
    row_90 = _by_ws(rows(QID, 90, workspace_ids=[cba.WS_WIN]))[cba.WS_WIN]

    assert row_7["usd_list"] == 5.0
    assert row_30["usd_list"] == 12.0
    assert row_90["usd_list"] == 23.0
