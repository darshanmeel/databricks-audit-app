"""tests/test_findings/test_cost_sku_trend_12m.py

Proves, against tests/fixtures/finops.py's own rows (`fo_trend_*`, account-level usage --
workspace_id IS NULL, same convention as its fo_ppp_*/fo_ma_*/fo_ds_* siblings; see that module's
docstring for the full scenario table), that findings.f_cost_sku_trend_12m (grain
[workspace_id, sku_name, month_start], windowless -- always window_days=0, the last 12 full
calendar months plus the current partial one, no :period_days) has: the right grain, one row per
workspace_id + sku_name per calendar month it actually has usage in (a month with no usage for a
workspace+SKU has no row -- no phantom zero months), the workspace+SKU-level trend columns
(usd_last_3m_avg / usd_prior_3m_avg / growth_3m_pct / usd_first_3m_avg / growth_12m_pct /
new_this_year / status) repeated identically across every one of that workspace+SKU's own rows,
is_partial_month TRUE only for the current month and never folded into any 3-month average,
product_label's CASE mapping, and every status band (CRITICAL via doubling, WARN via a 60% jump,
WARN via a new-this-year SKU, OK via a flat SKU).

Every expectation below is computed by hand from tests/fixtures/finops.py's own `_build_sku_trend_
12m` scenario table (fo_trend_double/warn60/new_vs/flat prices at $1.00/DBU, so usage_quantity IS
the dollar amount). Every fo_trend_* row carries workspace_id=NULL (finops.py's own account-level
convention -- see that module's docstring); the query's null-safe join (IS NOT DISTINCT FROM) keeps
that as its own group rather than dropping it, so every row asserted below still resolves.

finops.py's own scenario products also appear, correctly, as rows in OTHER queries' output, and --
in reverse -- this query has no product/sku filter of its own either, so every OTHER scenario in
finops.py (and every other fixture builder's own billing.usage rows that fall inside the 12-month-
plus-partial window) becomes its own extra, correctly computed row here too. This file filters to
sku_name.startswith("fo_trend_") and asserts nothing about the account-wide total those extra rows
contribute to: share_of_total_last_3m is now computed PARTITION BY workspace_id, so it is checked
only by relative order among the four fo_trend_* SKUs (all sharing the same workspace_id=NULL
partition, hence the same denominator), never by its absolute value.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "cost_sku_trend_12m.yml"

QID = "cost_sku_trend_12m"
CURRENT_MONTH = date(2026, 9, 1)  # matches audit_today() for the `test` dbt target (2026-09-21)


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _trend() -> list[dict]:
    return [r for r in dbutil.rows(QID, 0) if r["sku_name"].startswith("fo_trend_")]


def _by_sku_month() -> dict:
    return {(r["sku_name"], str(r["month_start"])): r for r in _trend()}


def _rows_for(sku_name: str) -> list[dict]:
    return [r for r in _trend() if r["sku_name"] == sku_name]


def test_grain_uniqueness():
    grains = _grains()
    cols = grains[QID]
    out = dbutil.rows(QID, 0)
    assert out
    keys = [tuple(r[c] for c in cols) for r in out]
    assert len(keys) == len(set(keys)), "duplicate grain rows"


def test_windowless_only_window_days_zero():
    out = _trend()
    assert out
    for r in out:
        assert r["window_days"] == 0
    assert dbutil.rows(QID, 7) == []
    assert dbutil.rows(QID, 30) == []
    assert dbutil.rows(QID, 90) == []


def test_status_enum():
    valid = {"OK", "WARN", "CRITICAL"}
    for r in _trend():
        assert r["status"] in valid


# =====================================================================================================
# fo_trend_double: 1000/mo Sep-Nov'25 (first 3) and Mar-May'26 (prior 3), 2000/mo Jun-Aug'26
# (last 3) -> growth_3m_pct = growth_12m_pct = +100.0% -> CRITICAL. One extra Sept 2026 (current,
# partial) row of 9999 that must NOT move any of the 3-month averages.
# =====================================================================================================
def test_doubling_is_critical():
    rows = _rows_for("fo_trend_double")
    assert len(rows) == 10  # 3 (first) + 3 (prior) + 3 (last) + 1 (current partial month)
    for r in rows:
        assert r["workspace_id"] is None  # finops.py's own account-level convention
        assert r["billing_origin_product"] == "MODEL_SERVING"
        assert r["product_label"] == "Model serving"
        assert r["usd_first_3m_avg"] == 1000.0
        assert r["usd_prior_3m_avg"] == 1000.0
        assert r["usd_last_3m_avg"] == 2000.0
        assert r["growth_3m_pct"] == 100.0
        assert r["growth_12m_pct"] == 100.0
        assert r["new_this_year"] is False
        assert r["status"] == "CRITICAL"


def test_doubling_current_month_is_partial_and_excluded_from_averages():
    r = _by_sku_month()[("fo_trend_double", "2026-09-01")]
    assert r["is_partial_month"] is True
    assert r["net_list_cost_usd"] == 9999.0
    # the partial month's own huge dollar figure must not have leaked into usd_last_3m_avg
    assert r["usd_last_3m_avg"] == 2000.0


def test_doubling_full_months_are_not_partial():
    for month in ("2025-09-01", "2026-06-01", "2026-08-01"):
        r = _by_sku_month()[("fo_trend_double", month)]
        assert r["is_partial_month"] is False
        assert r["net_list_cost_usd"] in (1000.0, 2000.0)
        assert r["price_basis"] == "priced"


# =====================================================================================================
# fo_trend_warn60: 500/mo first 3 and prior 3, 800/mo last 3 -> growth_3m_pct = growth_12m_pct =
# +60.0% -> WARN (>=50%, <100%).
# =====================================================================================================
def test_warn60_is_warn():
    rows = _rows_for("fo_trend_warn60")
    assert len(rows) == 9  # 3 + 3 + 3, no current-month row for this scenario
    for r in rows:
        assert r["workspace_id"] is None
        assert r["billing_origin_product"] == "SQL"
        assert r["product_label"] == "SQL warehouses"
        assert r["usd_first_3m_avg"] == 500.0
        assert r["usd_prior_3m_avg"] == 500.0
        assert r["usd_last_3m_avg"] == 800.0
        assert r["growth_3m_pct"] == 60.0
        assert r["growth_12m_pct"] == 60.0
        assert r["new_this_year"] is False
        assert r["status"] == "WARN"


# =====================================================================================================
# fo_trend_new_vs: no usage at all before Jun'26 -> usd_prior_3m_avg = usd_first_3m_avg = 0 (a real
# 0.0, never NULL) -> growth_3m_pct/growth_12m_pct NULL (NULLIF avoids the divide-by-zero) ->
# new_this_year=TRUE, usd_last_3m_avg=700 >= :new_sku_min_usd(500) -> WARN via that branch alone.
# =====================================================================================================
def test_new_this_year_is_warn():
    rows = _rows_for("fo_trend_new_vs")
    assert len(rows) == 3  # only the last-3-months rows -- no usage at all before Jun'26
    for r in rows:
        assert r["workspace_id"] is None
        assert r["billing_origin_product"] == "VECTOR_SEARCH"
        assert r["product_label"] == "Vector search"
        assert r["usd_first_3m_avg"] == 0.0
        assert r["usd_prior_3m_avg"] == 0.0
        assert r["usd_last_3m_avg"] == 700.0
        assert r["growth_3m_pct"] is None
        assert r["growth_12m_pct"] is None
        assert r["new_this_year"] is True
        assert r["status"] == "WARN"
    months = {str(r["month_start"]) for r in rows}
    assert months == {"2026-06-01", "2026-07-01", "2026-08-01"}


# =====================================================================================================
# fo_trend_flat: 600/mo in all three buckets -> growth_3m_pct = growth_12m_pct = 0.0% -> OK, proving
# a $ amount above every WARN/CRITICAL floor still reads OK when growth is flat.
# =====================================================================================================
def test_flat_is_ok():
    rows = _rows_for("fo_trend_flat")
    assert len(rows) == 9
    for r in rows:
        assert r["workspace_id"] is None
        assert r["billing_origin_product"] == "JOBS"
        assert r["product_label"] == "Jobs"
        assert r["usd_first_3m_avg"] == 600.0
        assert r["usd_prior_3m_avg"] == 600.0
        assert r["usd_last_3m_avg"] == 600.0
        assert r["growth_3m_pct"] == 0.0
        assert r["growth_12m_pct"] == 0.0
        assert r["new_this_year"] is False
        assert r["status"] == "OK"


# =====================================================================================================
# share_of_total_last_3m: PARTITION BY workspace_id -- the four fo_trend_* SKUs all carry
# workspace_id=NULL, so they share one denominator (every NULL-workspace SKU's own usd_last_3m_avg,
# from every fixture builder, not just this file's own four) -- never asserted by absolute value,
# only by relative order among the four (usd_last_3m_avg 2000 > 800 > 700 > 600), which must hold
# regardless of what any other builder contributes to that shared partition.
# =====================================================================================================
def test_share_of_total_ordering():
    share = {
        sku: _rows_for(sku)[0]["share_of_total_last_3m"]
        for sku in ("fo_trend_double", "fo_trend_warn60", "fo_trend_new_vs", "fo_trend_flat")
    }
    assert share["fo_trend_double"] > share["fo_trend_warn60"] > share["fo_trend_new_vs"] > share["fo_trend_flat"] > 0
    for v in share.values():
        assert 0 < v <= 100
    # repeated identically across a SKU's own rows (the grain's repeated-columns design)
    for sku in share:
        assert all(r["share_of_total_last_3m"] == share[sku] for r in _rows_for(sku))


# =====================================================================================================
# The grain now keys on workspace_id too (one row per workspace+SKU+month, not just SKU+month).
# Summing a SKU's rows across whatever workspaces it has for a given month must still land on the
# same monthly dollars the pre-workspace_id, account-wide query would have shown -- every fo_trend_*
# row is workspace_id=NULL here (finops.py's own convention, see the module docstring), so that sum
# has exactly one term per month, but it is the same SUM-across-workspace-groups the UI's own
# SKU-trend card now does to rebuild a per-SKU view out of these per-workspace rows.
# =====================================================================================================
def test_summing_workspaces_matches_old_account_wide_monthly_dollars():
    by_sku_month: dict[tuple[str, str], float] = {}
    for r in _trend():
        if r["is_partial_month"]:
            continue
        key = (r["sku_name"], str(r["month_start"]))
        by_sku_month[key] = by_sku_month.get(key, 0.0) + r["net_list_cost_usd"]

    for month in ("2025-09-01", "2025-10-01", "2025-11-01", "2026-03-01", "2026-04-01", "2026-05-01"):
        assert by_sku_month[("fo_trend_double", month)] == 1000.0
        assert by_sku_month[("fo_trend_warn60", month)] == 500.0
        assert by_sku_month[("fo_trend_flat", month)] == 600.0
    for month in ("2026-06-01", "2026-07-01", "2026-08-01"):
        assert by_sku_month[("fo_trend_double", month)] == 2000.0
        assert by_sku_month[("fo_trend_warn60", month)] == 800.0
        assert by_sku_month[("fo_trend_new_vs", month)] == 700.0
        assert by_sku_month[("fo_trend_flat", month)] == 600.0
