"""tests/test_findings/test_cost_monthly_actuals.py -- P3-FINOPS.

Proves, against tests/fixtures/finops.py's own rows (`fo_ma_*`, account-level usage -- workspace_id
IS NULL, see that module's docstring for why), that findings.f_cost_monthly_actuals (grain
[workspace_id, billing_origin_product, month_start], windowless -- always window_days=0, no
:period_days) has: the right grain, calendar-month grouping, is_partial_month TRUE for the current
(September 2026) month AND, separately (DEC-64, review fix must_fix 8), for whichever month holds
the WHOLE account's own earliest recorded usage_date (an export that starts mid-month), with
partial_reason distinguishing the two, days_captured as a distinct-day count, today's own row
(D(0)) EXCLUDED (the same `usage_date < current_date()` cut-off every other cost query in this app
uses, so a month's total always reconciles with a window total for the same complete days),
first_day/last_day as the range's own earliest/latest counted usage_date, price_basis
(priced/unpriced/free), and one product spanning two separate months producing two separate rows.

Every expectation below is computed by hand from tests/fixtures/finops.py's own scenario table and
independently cross-checked by rendering the generated DuckDB SQL against an in-memory DuckDB
seeded only from finops.build() (never read back from the model under test).

Every other finops.py scenario product (fo_ppp_*, fo_ds_*) also produces its own row here (this
query is windowless with no product filter, so every usage row in the fixture becomes a real row
somewhere) -- this file asserts only on the fo_ma_* products it owns.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import duckdb
import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "cost_monthly_actuals.yml"
PARQUET_DIR = ROOT / "tests" / "fixtures" / "parquet"

QID = "cost_monthly_actuals"
CURRENT_MONTH = date(2026, 9, 1)  # matches audit_today() for the `test` dbt target (2026-09-21)


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _ma() -> list[dict]:
    # account-level usage (workspace_id IS NULL, review fix must_fix 9) -- dbutil.rows() has no
    # way to filter on IS NULL, so this reads every row and filters in Python instead.
    return [
        r for r in dbutil.rows(QID, 0)
        if r["workspace_id"] is None and r["billing_origin_product"].startswith("fo_ma_")
    ]


def _by_product_month() -> dict:
    return {(r["billing_origin_product"], str(r["month_start"])): r for r in _ma()}


def _account_snapshot_start():
    """MIN(usage_date) across the WHOLE raw billing__usage fixture parquet -- every fixture
    builder in this tree, not just finops.py -- independently computed from the same raw parquet
    the model's own `snapshot` CTE reads from system.billing.usage (DEC-64's account-wide
    coverage signal, review fix must_fix 8). Returns None if the parquet has not been written."""
    pattern = (PARQUET_DIR / "billing__usage" / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        return con.execute(
            f"SELECT MIN(usage_date) FROM read_parquet('{pattern}', union_by_name=true)"
        ).fetchone()[0]
    finally:
        con.close()


def test_grain_uniqueness():
    grains = _grains()
    cols = grains[QID]
    out = dbutil.rows(QID, 0)
    assert out
    keys = [tuple(r[c] for c in cols) for r in out]
    assert len(keys) == len(set(keys)), "duplicate grain rows"


def test_windowless_only_window_days_zero():
    # windowless (no :period_days): only window_days=0 is ever populated, for ANY product.
    out = _ma()
    assert out
    for r in out:
        assert r["window_days"] == 0
    assert dbutil.rows(QID, 7) == []
    assert dbutil.rows(QID, 30) == []
    assert dbutil.rows(QID, 90) == []


def test_no_status_column():
    # Inventory query (DEC-08): actuals, not a verdict.
    for r in _ma():
        assert "status" not in r


# =====================================================================================================
# fo_ma_cur: Sept 2026 (D0's own month) -- D(1)+D(3)+D(5) count, net=35.00, days_captured=3,
# is_partial_month=TRUE, partial_reason='month_in_progress'. D(0) (today, still in flight) is
# written by the fixture but EXCLUDED here, the same `usage_date < current_date()` cut-off
# cost_period_over_period / cost_daily_spikes already use -- proving this query no longer counts
# the current, still-landing day either. first_day/last_day are the remaining rows' own range.
# =====================================================================================================
def test_current_month_is_partial_and_excludes_today():
    rows = _by_product_month()
    r = rows[("fo_ma_cur", "2026-09-01")]
    assert r["net_list_cost_usd"] == 35.0
    assert r["days_captured"] == 3
    assert r["is_partial_month"] is True
    assert r["partial_reason"] == "month_in_progress"
    assert r["price_basis"] == "priced"
    assert str(r["first_day"]) == "2026-09-16"
    assert str(r["last_day"]) == "2026-09-20"


# =====================================================================================================
# fo_ma_past: August 2026, 3 distinct days, net=50.00. Not the current month, and (per this
# fixture's own D(180)/D(250) anchor rows elsewhere) not the account's own oldest month either, so
# is_partial_month=FALSE, partial_reason=None.
# =====================================================================================================
def test_past_month_is_not_partial():
    rows = _by_product_month()
    r = rows[("fo_ma_past", "2026-08-01")]
    assert r["net_list_cost_usd"] == 50.0
    assert r["days_captured"] == 3
    assert r["is_partial_month"] is False
    assert r["partial_reason"] is None
    assert str(r["first_day"]) == "2026-08-05"
    assert str(r["last_day"]) == "2026-08-20"


# =====================================================================================================
# DEC-64 (review fix must_fix 8): whichever calendar month holds the WHOLE account's own earliest
# recorded usage_date is itself only partly captured (an export that starts mid-month), unless that
# date happens to fall on the 1st. Derived independently from the raw fixture parquet (never
# hard-coded), since the account-wide minimum depends on every fixture builder writing to
# billing.usage in this tree (parallel work), not just this file's own fo_ma_* rows.
# =====================================================================================================
def test_export_starts_mid_month_marks_the_oldest_month_partial():
    snapshot_start = _account_snapshot_start()
    if snapshot_start is None or snapshot_start.day == 1:
        return
    oldest_month = date(snapshot_start.year, snapshot_start.month, 1)
    matching = [r for r in dbutil.rows(QID, 0) if str(r["month_start"]) == str(oldest_month)]
    assert matching, f"expected >= 1 row for the account's own oldest month {oldest_month}"
    for r in matching:
        assert r["is_partial_month"] is True, r
        if oldest_month == CURRENT_MONTH:
            # month_in_progress takes precedence when the account's whole history is this young.
            assert r["partial_reason"] == "month_in_progress", r
        else:
            assert r["partial_reason"] == "export_starts_mid_month", r


# =====================================================================================================
# fo_ma_unpriced: August 2026, one priced day (8.00) + one unpriced day -> net=8.00 (the unpriced
# day's dollars are ignored by SUM, never forced to 0), days_captured=2, price_basis='unpriced'.
# =====================================================================================================
def test_price_basis_unpriced():
    rows = _by_product_month()
    r = rows[("fo_ma_unpriced", "2026-08-01")]
    assert r["net_list_cost_usd"] == 8.0
    assert r["days_captured"] == 2
    assert r["price_basis"] == "unpriced"


# =====================================================================================================
# fo_ma_free: August 2026, one day entirely on a FREE_USAGE-named SKU -> net_list_cost_usd is NULL
# (the group's only row, so SUM never has another row to contribute a concrete 0 -- distinct from
# cost_period_over_period's fo_ppp_free, which always has at least two rows in its group), a real
# $0, never a coverage gap; price_basis='free'.
# =====================================================================================================
def test_price_basis_free_is_null_not_zero():
    rows = _by_product_month()
    r = rows[("fo_ma_free", "2026-08-01")]
    assert r["net_list_cost_usd"] is None
    assert r["days_captured"] == 1
    assert r["price_basis"] == "free"


# =====================================================================================================
# fo_ma_multi: usage in TWO different calendar months for the same product -> two separate output
# rows, one per month, each with its own days_captured/net/is_partial_month.
# =====================================================================================================
def test_one_product_two_months_two_rows():
    rows = _by_product_month()
    july = rows[("fo_ma_multi", "2026-07-01")]
    august = rows[("fo_ma_multi", "2026-08-01")]
    assert july["net_list_cost_usd"] == 5.0
    assert july["days_captured"] == 1
    assert july["is_partial_month"] is False
    assert august["net_list_cost_usd"] == 9.0
    assert august["days_captured"] == 1
    assert august["is_partial_month"] is False
