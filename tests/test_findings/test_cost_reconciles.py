"""tests/test_findings/test_cost_reconciles.py -- 2026-09-25 cost-cutoff fix.

The bug this proves fixed: the Cost page showed two different dollar figures for what a user reads
as "the same money" -- an "Est spend" card (a rolling window, cost_dollarized_by_sku_day) and a
"Dollars per calendar month" bar (cost_monthly_actuals) -- because cost_monthly_actuals alone
included the snapshot's own in-flight day while every windowed cost query already stopped at
`usage_date < current_date()`. The fix gives cost_monthly_actuals that exact same cut-off.

This file proves the resulting invariant GENERICALLY, across the WHOLE fixture database (every
fixture module's rows, not just tests/fixtures/finops.py's own fo_ma_* scenario), never by
reading back from the model under test and never by hard-coding an expected dollar figure: for the
current, in-progress calendar month, summing cost_monthly_actuals.net_list_cost_usd over that
month must equal summing cost_dollarized_by_sku_day.net_list_cost (at window_days=30, the app's
own default window) over usage_date >= that month's first day. Both queries price every row at the
same effective list rate (DEC-66.1: pricing.effective_list.default) and apply the same
`usage_date < current_date()` cut-off, so the two sums are drawn from exactly the same set of
underlying usage rows once the 30-day window and the month-start filter line up -- window_days=30
is used because tests/fixtures/base.py's AS_OF (2026-09-21) is only 20 days into September, so a
30-day trailing window (back to 2026-08-22) comfortably covers the whole current month "so far";
picking a wider account-wide comparison than any single scenario's own product also exercises
whatever OTHER fixture modules (billing.py, chargeback.py, lakeflow.py, ...) happen to have written
to system.billing.usage in the last 20 days, not just finops.py's own fo_ma_cur rows.
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

# Matches audit_today() for the `test` dbt target (2026-09-21, dbt/macros/audit_time.sql) --
# the current calendar month's first day, in the export's own dates, no timezone shift.
CURRENT_MONTH_START = "2026-09-01"
WINDOW_DAYS = 30


def _sum(con: duckdb.DuckDBPyConnection, sql: str, params: list) -> float | None:
    return con.execute(sql, params).fetchone()[0]


def test_monthly_actuals_reconciles_with_dollarized_window_for_current_month():
    con = duckdb.connect(str(dbutil.DB_PATH), read_only=True)
    try:
        monthly_total = _sum(
            con,
            """
            SELECT ROUND(SUM(net_list_cost_usd), 2)
            FROM findings.f_cost_monthly_actuals
            WHERE window_days = 0 AND month_start = CAST(? AS DATE)
            """,
            [CURRENT_MONTH_START],
        )
        window_total = _sum(
            con,
            """
            SELECT ROUND(SUM(net_list_cost), 2)
            FROM findings.f_cost_dollarized_by_sku_day
            WHERE window_days = ? AND usage_date >= CAST(? AS DATE)
            """,
            [WINDOW_DAYS, CURRENT_MONTH_START],
        )
    finally:
        con.close()
    # Both None would make this test vacuously true (no fixture data landed this month at all) --
    # fail loudly instead, since that would mean the fixture DB is not what this test expects.
    assert monthly_total is not None, "no cost_monthly_actuals rows for the current month at all"
    assert window_total is not None, "no cost_dollarized_by_sku_day rows for the current month at all"
    assert monthly_total == window_total, (
        f"cost_monthly_actuals (month total {monthly_total}) and cost_dollarized_by_sku_day "
        f"(window total {window_total}) disagree on the current month's dollars -- exactly the "
        "'two different costs on the same page' bug this fix is meant to close"
    )
