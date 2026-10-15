"""tests/test_findings/test_cost_period_over_period.py -- P3-FINOPS.

Proves, against tests/fixtures/finops.py's own rows (`fo_ppp_*`, account-level usage --
workspace_id IS NULL, see that module's docstring for why), that
findings.f_cost_period_over_period (grain [workspace_id, billing_origin_product], windowed on
:period_days) has: the right grain, every status band (OK via the $ floor, WARN, CRITICAL via
percent AND via brand-new spend with a zero previous period), NOT_ASSESSED per DEC-64 -- now an
account-wide coverage signal, not a per-row one (review fix must_fix 2) -- when the WHOLE
account's own earliest usage does not reach back far enough to cover the previous window,
NOT_ASSESSED when a row's own current- or previous-period spend has ANY unpriced usage, not only
when the whole side is unpriced,
price_basis (priced/unpriced/free) with the "both windows combined" rule and its window-dependent
NULL-vs-0 mechanics, corrections (a RETRACTION row) netting out of the sum, and window-days
scaling (current/previous both grow with window_days).

Every expectation below is computed by hand from tests/fixtures/finops.py's own scenario table and
independently cross-checked by rendering the generated DuckDB SQL against an in-memory DuckDB
seeded only from finops.build() (never read back from the model under test) -- see that module's
docstring for the full scenario -> expected-value map this file exercises.

finops.py's own scenario products also appear, correctly, as rows in OTHER queries' output (e.g.
fo_ds_warn's candidate-day usage is also a real cost_period_over_period row) -- this file asserts
only on the fo_ppp_* products it owns and ignores everything else in the result set.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import duckdb
import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR / "fixtures"))
import dbutil  # noqa: E402
import finops as fo  # noqa: E402 -- fo.D0 (matches audit_today() for the `test` dbt target)

GRAINS_PATH = ROOT / "config" / "grains" / "cost_period_over_period.yml"
PARQUET_DIR = ROOT / "tests" / "fixtures" / "parquet"

WINDOWS = (7, 30, 90)
QID = "cost_period_over_period"
D0 = fo.D0


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _ppp(window_days: int) -> dict:
    # account-level usage (workspace_id IS NULL, review fix must_fix 9) -- dbutil.rows() has no
    # way to filter on IS NULL (passing workspace_ids=[None] would match nothing, since
    # `x IN (NULL)` is never true in SQL), so this reads every row and filters in Python instead.
    return {
        r["billing_origin_product"]: r
        for r in dbutil.rows(QID, window_days)
        if r["workspace_id"] is None and r["billing_origin_product"].startswith("fo_ppp_")
    }


def _account_snapshot_start():
    """MIN(usage_date) across the WHOLE raw billing__usage fixture parquet -- every fixture
    builder in this tree, not just finops.py -- independently computed from the same raw parquet
    the model's own `snapshot` CTE reads from system.billing.usage (DEC-64's account-wide coverage
    signal, review fix must_fix 2). Returns None if the parquet has not been written yet."""
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
    for w in WINDOWS:
        out = dbutil.rows(QID, w)
        assert out, w
        keys = [tuple(r[c] for c in cols) for r in out]
        assert len(keys) == len(set(keys)), f"w={w}: duplicate grain rows"


def test_window_days_zero_is_empty():
    # windowed (period_days-driven): the model only ever emits window_days in {7, 30, 90}.
    assert dbutil.rows(QID, 0) == []


def test_status_enum():
    valid = {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}
    for r in dbutil.rows(QID, 30):
        assert r["status"] in valid


# =====================================================================================================
# fo_ppp_warn: current=130, previous=100 -> +30.0% -> WARN (>=:warn_increase_pct=25, <:crit=50).
# =====================================================================================================
def test_warn_band():
    r = _ppp(7)["fo_ppp_warn"]
    assert r["est_current_usd_list"] == 130.0
    assert r["est_previous_usd_list"] == 100.0
    assert r["est_change_usd_list"] == 30.0
    assert r["change_pct"] == 30.0
    assert r["price_basis"] == "priced"
    assert r["status"] == "WARN"
    assert r["not_assessed_reason"] is None


# =====================================================================================================
# fo_ppp_crit: current=200, previous=100 -> +100.0% -> CRITICAL (>=:crit_increase_pct=50).
# =====================================================================================================
def test_critical_band_by_percent():
    r = _ppp(7)["fo_ppp_crit"]
    assert r["est_current_usd_list"] == 200.0
    assert r["est_previous_usd_list"] == 100.0
    assert r["est_change_usd_list"] == 100.0
    assert r["change_pct"] == 100.0
    assert r["status"] == "CRITICAL"


# =====================================================================================================
# fo_ppp_ok_floor: current=40 (< :min_spend_usd=50) -> OK, no matter that the swing is +700%. The
# $ floor is checked BEFORE the percent bands (see the model's own CASE order).
# =====================================================================================================
def test_ok_via_min_spend_floor():
    r = _ppp(7)["fo_ppp_ok_floor"]
    assert r["est_current_usd_list"] == 40.0
    assert r["est_previous_usd_list"] == 5.0
    assert r["change_pct"] == 700.0
    assert r["status"] == "OK"


# =====================================================================================================
# fo_ppp_crit_new: current=80, previous window has NO usage at all for this product (previous=0),
# but its own history is old enough to be covered (DEC-64) -> CRITICAL via the brand-new-spend
# branch, never a percent (there is no honest percentage for a jump from zero) -- change_pct is
# NULL, not a huge number.
# =====================================================================================================
def test_critical_band_brand_new_spend():
    r = _ppp(7)["fo_ppp_crit_new"]
    assert r["est_current_usd_list"] == 80.0
    assert r["est_previous_usd_list"] == 0.0
    assert r["est_change_usd_list"] == 80.0
    assert r["change_pct"] is None
    assert r["status"] == "CRITICAL"
    assert r["not_assessed_reason"] is None


# =====================================================================================================
# fo_ppp_crit_new_short_history: usage only at D(2), no anchor of its own. Under the OLD per-row
# DEC-64 check this product's own short history would have read NOT_ASSESSED; review fix must_fix
# 2 makes coverage account-wide, and every OTHER scenario's own anchor already covers the account
# as a whole, so this still reads a real verdict -- CRITICAL via the brand-new-spend branch, same
# as fo_ppp_crit_new above, proving coverage genuinely no longer depends on THIS row's own history.
# =====================================================================================================
def test_critical_band_brand_new_spend_with_short_own_history():
    for w in WINDOWS:
        r = _ppp(w)["fo_ppp_crit_new_short_history"]
        assert r["est_current_usd_list"] == 100.0, w
        assert r["est_previous_usd_list"] == 0.0, w
        assert r["change_pct"] is None, w
        assert r["status"] == "CRITICAL", w
        assert r["not_assessed_reason"] is None, w


# =====================================================================================================
# DEC-64 coverage is now account-wide (review fix must_fix 2): every row reads NOT_ASSESSED
# (previous_window_not_covered) together whenever the WHOLE account's own earliest usage_date does
# not reach back far enough to cover a given window's previous period -- never a per-row check.
# Derived independently from the raw fixture parquet (never hard-coded), since the account-wide
# minimum depends on every fixture builder writing to billing.usage in this tree (parallel work),
# not just this file's own fo_ppp_* rows.
# =====================================================================================================
def test_previous_window_coverage_is_account_wide():
    snapshot_start = _account_snapshot_start()
    for w in WINDOWS:
        threshold = D0 - timedelta(days=2 * w)
        covered = snapshot_start is None or snapshot_start <= threshold
        for r in dbutil.rows(QID, w):
            if covered:
                assert r["not_assessed_reason"] != "previous_window_not_covered", (w, r)
            else:
                assert r["status"] == "NOT_ASSESSED", (w, r)
                assert r["not_assessed_reason"] == "previous_window_not_covered", (w, r)


# =====================================================================================================
# fo_ppp_unpriced: current = 60 priced + 15 qty on a never-registered SKU (ignored by SUM, not
# forced to 0); previous = 60 priced. est_current_usd_list still reads the real 60.0 the priced
# rows sum to (the unpriced row is skipped, not zeroed), but any unpriced usage on a side withholds
# a verdict on that side, so this reads NOT_ASSESSED (current_period_unpriced), not a change_pct
# built on an understated total -- even though most of the side did price.
# =====================================================================================================
def test_price_basis_partially_unpriced_reads_not_assessed():
    r = _ppp(7)["fo_ppp_unpriced"]
    assert r["est_current_usd_list"] == 60.0
    assert r["est_previous_usd_list"] == 60.0
    assert r["price_basis"] == "unpriced"
    assert r["status"] == "NOT_ASSESSED"
    assert r["not_assessed_reason"] == "current_period_unpriced"


# =====================================================================================================
# P4-47: fo_ppp_unpriced_current -- the CURRENT side is entirely on a never-registered SKU (no
# priced row and not FREE_USAGE), so current_cost's own SUM is genuinely NULL -- not a real $0,
# priced or free -- while the previous side is a real, fully-priced 100. NOT_ASSESSED
# (current_period_unpriced), never a false OK/CRITICAL built on a missing number.
# =====================================================================================================
def test_current_period_unpriced_reads_not_assessed():
    r = _ppp(7)["fo_ppp_unpriced_current"]
    assert r["est_current_usd_list"] is None
    assert r["est_previous_usd_list"] == 100.0
    assert r["price_basis"] == "unpriced"
    assert r["status"] == "NOT_ASSESSED"
    assert r["not_assessed_reason"] == "current_period_unpriced"


# =====================================================================================================
# P4-47: fo_ppp_unpriced_previous -- the mirror image: the PREVIOUS side is entirely unpriced, the
# current side is a real, fully-priced 100. NOT_ASSESSED (previous_period_unpriced) -- proving the
# same gap is caught on the previous side too, not only the current one (the bug "remove ELSE 0"
# fixes: before, a real row on the OTHER side alone silently turned a fully-unpriced side's NULL
# sum into a false concrete 0).
# =====================================================================================================
def test_previous_period_unpriced_reads_not_assessed():
    r = _ppp(7)["fo_ppp_unpriced_previous"]
    assert r["est_current_usd_list"] == 100.0
    assert r["est_previous_usd_list"] is None
    assert r["price_basis"] == "unpriced"
    assert r["status"] == "NOT_ASSESSED"
    assert r["not_assessed_reason"] == "previous_period_unpriced"


# =====================================================================================================
# fo_ppp_free: current and previous both entirely on a FREE_USAGE-named SKU -> a real $0, never a
# coverage gap, price_basis='free', OK at every window (review fix must_fix 3: the floor check is
# COALESCE(current_cost, 0) < :min_spend_usd, so a NULL current-period sum reads OK too, not
# CRITICAL). At w=7 D(10) still falls in the previous bucket; change_pct is NULL (0 / NULLIF(0,0)).
# =====================================================================================================
def test_price_basis_free_is_real_zero_not_null():
    r = _ppp(7)["fo_ppp_free"]
    assert r["est_current_usd_list"] == 0.0
    assert r["est_previous_usd_list"] == 0.0
    assert r["est_change_usd_list"] == 0.0
    assert r["change_pct"] is None
    assert r["price_basis"] == "free"
    assert r["status"] == "OK"


# =====================================================================================================
# fo_ppp_free at w=30/90: D(10) shifts into the CURRENT bucket too at these wider windows (10 days
# back is still "current" once the window is 30 or 90 days), so the PREVIOUS side has no matching
# rows at all. P4-47: a side with zero matching rows (its own unpriced_quantity is 0, same as a
# FREE_USAGE-only side) resolves to a real 0.0, not the NULL a bare SQL SUM would produce -- so
# est_current_usd_list is a real 0.0 here too (never NULL: this is a genuine free product with
# real, currently-covered activity, not an uncomparable one -- contrast fo_ppp_unpriced_current/
# _previous above, which stay NULL and read NOT_ASSESSED because they are genuinely unpriced, not
# free). Still OK, never CRITICAL via the "previous_cost = 0" brand-new-spend branch.
# =====================================================================================================
def test_price_basis_free_ok_at_wider_windows():
    for w in (30, 90):
        r = _ppp(w)["fo_ppp_free"]
        assert r["est_current_usd_list"] == 0.0, w
        assert r["est_previous_usd_list"] == 0.0, w
        assert r["price_basis"] == "free", w
        assert r["status"] == "OK", w
        assert r["not_assessed_reason"] is None, w


# =====================================================================================================
# fo_ppp_correction: current = ORIGINAL 100 + RETRACTION -20 (net qty 80, $80); previous = 80 ->
# 0% change -> OK. Proves record_type is never filtered: a RETRACTION nets against its ORIGINAL
# through the same SUM every other row goes through.
# =====================================================================================================
def test_correction_nets_out():
    r = _ppp(7)["fo_ppp_correction"]
    assert r["est_current_usd_list"] == 80.0
    assert r["est_previous_usd_list"] == 80.0
    assert r["change_pct"] == 0.0
    assert r["status"] == "OK"


# =====================================================================================================
# fo_ppp_daily: uniform $10/day, D(1)..D(180) -- est_current_usd_list and est_previous_usd_list
# both scale with window_days (70/70 at w=7, 300/300 at w=30, 900/900 at w=90), 0% change -> OK at
# every window. Proves the windowed loop actually varies the date range per window, not just the
# label.
# =====================================================================================================
def test_window_scaling():
    expected = {7: 70.0, 30: 300.0, 90: 900.0}
    for w, amount in expected.items():
        r = _ppp(w)["fo_ppp_daily"]
        assert r["est_current_usd_list"] == amount, w
        assert r["est_previous_usd_list"] == amount, w
        assert r["change_pct"] == 0.0, w
        assert r["status"] == "OK", w
        assert r["price_basis"] == "priced", w
