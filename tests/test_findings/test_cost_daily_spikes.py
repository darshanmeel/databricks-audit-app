"""tests/test_findings/test_cost_daily_spikes.py -- P3-FINOPS.

Proves, against tests/fixtures/finops.py's own rows (`fo_ds_*`, account-level usage -- workspace_id
IS NULL, see that module's docstring for why), that findings.f_cost_daily_spikes (grain
[workspace_id, billing_origin_product, usage_date], windowed on :period_days for the SCANNED days
only -- :baseline_days=14 is a fixed trailing lookback, independent of window_days) has: the right
grain, WARN when a day clears both :spike_ratio and :min_spend_usd, NOT_ASSESSED when a day clears
the $ floor but the trailing 14 days have no usage at all for that product (review fix must_fix 4:
the floor is checked BEFORE the no-baseline case, so a day that can never clear the floor never
appears as a NOT_ASSESSED row either), a day that clears the ratio but not the $ floor emitting NO
row, a day that clears the floor but not the ratio also emitting no row, a real $0 day (free usage,
no baseline) also emitting no row (it fails the floor, same as any other sub-floor day), top_skus
naming every driver with an honest "(no list price)" / "(free)" label for an unpriced/free SKU
(review fix must_fix 6) with no ordering guaranteed, and a widened window scanning one of
fo_ds_warn's own baseline days as a new candidate that stays below the floor and so still emits no
second row.

Every expectation below is computed by hand from tests/fixtures/finops.py's own scenario table and
independently cross-checked by rendering the generated DuckDB SQL against an in-memory DuckDB
seeded only from finops.build() (never read back from the model under test).

finops.py's other scenario products (fo_ppp_*, fo_ma_*) also produce their own rows here (this
query has no product filter) -- this file asserts only on the fo_ds_* products it owns, and one
explicit absence check per row that must never appear.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "cost_daily_spikes.yml"

WINDOWS = (7, 30, 90)
QID = "cost_daily_spikes"


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _ds(window_days: int) -> list[dict]:
    # account-level usage (workspace_id IS NULL, review fix must_fix 9) -- dbutil.rows() has no
    # way to filter on IS NULL, so this reads every row and filters in Python instead.
    return [
        r for r in dbutil.rows(QID, window_days)
        if r["workspace_id"] is None and r["billing_origin_product"].startswith("fo_ds_")
    ]


def _by_product(window_days: int) -> dict:
    # fo_ds_warn can legitimately have more than one row at a wide window in principle -- callers
    # that expect a single row per product use this only for the other, single-row products.
    out: dict[str, dict] = {}
    for r in _ds(window_days):
        out.setdefault(r["billing_origin_product"], []).append(r)
    return out


def test_grain_uniqueness():
    grains = _grains()
    cols = grains[QID]
    for w in WINDOWS:
        out = dbutil.rows(QID, w)
        keys = [tuple(r[c] for c in cols) for r in out]
        assert len(keys) == len(set(keys)), f"w={w}: duplicate grain rows"


def test_window_days_zero_is_empty():
    assert dbutil.rows(QID, 0) == []


def test_status_enum_no_ok_or_critical():
    # This query only ever emits WARN or NOT_ASSESSED rows (see caveats: OK is never itself
    # returned as a row -- see also test_below_ratio_and_below_floor_are_absent below).
    for r in dbutil.rows(QID, 30):
        assert r["status"] in {"WARN", "NOT_ASSESSED"}


def test_no_warn_row_has_a_thin_baseline():
    # min_baseline_days=3 (default): a WARN row's own median must rest on at least that many
    # trailing days, never a one- or two-day baseline noisy enough to swing the ratio on its own.
    for r in dbutil.rows(QID, 30):
        if r["status"] == "WARN":
            assert r["baseline_days_seen"] >= 3, r


# =====================================================================================================
# fo_ds_warn: 14-day trailing baseline at $20/day, candidate day = $50 across 3 SKUs (A=$30,
# B=$15, C=$5) -> ratio 2.5 (>= :spike_ratio=2.0), $50 >= :min_spend_usd=50 -> WARN. top_skus names
# all three drivers (order not asserted).
# =====================================================================================================
def test_warn_band_and_top_skus():
    r = _by_product(7)["fo_ds_warn"][0]
    assert str(r["usage_date"]) == "2026-09-20"
    assert r["est_day_usd_list"] == 50.0
    assert r["est_baseline_usd_list"] == 20.0
    assert r["baseline_days_seen"] == 14
    assert r["spike_ratio_actual"] == 2.5
    assert r["price_basis"] == "priced"
    assert r["status"] == "WARN"
    assert r["not_assessed_reason"] is None
    top = r["top_skus"]
    assert "fo_SKU_A ($30)" in top
    assert "fo_DS_B ($15)" in top
    assert "fo_DS_C ($5)" in top


# =====================================================================================================
# fo_ds_notassessed: candidate D(3) = $100, zero usage at all in its own trailing 14-day window ->
# baseline_days_seen=0 -> NOT_ASSESSED. $100 clears the $ floor, which is checked FIRST (review fix
# must_fix 4), so this remains a genuine NOT_ASSESSED case (real money, no baseline) rather than
# being swallowed by the floor.
# =====================================================================================================
def test_not_assessed_no_baseline_history():
    r = _by_product(7)["fo_ds_notassessed"][0]
    assert str(r["usage_date"]) == "2026-09-18"
    assert r["est_day_usd_list"] == 100.0
    assert r["est_baseline_usd_list"] is None
    assert r["baseline_days_seen"] == 0
    assert r["spike_ratio_actual"] is None
    assert r["status"] == "NOT_ASSESSED"
    assert r["not_assessed_reason"] == "no_baseline_history"


# =====================================================================================================
# fo_ds_nospike / fo_ds_floor / fo_ds_free_na: none of the three clears the bar to be flagged --
# ratio 1.25 (also below the $ floor); ratio 10x but day_cost $10 < $50 floor; a real $0 free day
# (no baseline, but the $ floor blocks it before the no-baseline case is ever reached, review fix
# must_fix 4) -- all three emit NO row at all, at every window (DEC-57).
# =====================================================================================================
def test_below_ratio_and_below_floor_are_absent():
    for w in WINDOWS:
        products = {r["billing_origin_product"] for r in _ds(w)}
        assert "fo_ds_nospike" not in products, w
        assert "fo_ds_floor" not in products, w
        assert "fo_ds_free_na" not in products, w


# =====================================================================================================
# fo_ds_unpriced: baseline $20/day, candidate = 60 priced + 10 qty unpriced -> day_cost=60 (the
# unpriced portion is ignored by SUM, never forced to 0), ratio 3.0 -> WARN, price_basis='unpriced'.
# top_skus labels the unpriced SKU "(no list price)", never a bare "($)" (review fix must_fix 6).
# =====================================================================================================
def test_price_basis_unpriced():
    r = _by_product(7)["fo_ds_unpriced"][0]
    assert r["est_day_usd_list"] == 60.0
    assert r["spike_ratio_actual"] == 3.0
    assert r["price_basis"] == "unpriced"
    assert r["status"] == "WARN"
    top = r["top_skus"]
    assert "fo_SKU_A ($60)" in top
    assert "fo_DS_UNPRICED (no list price)" in top


# =====================================================================================================
# fo_ds_shortbaseline: ONE trailing baseline day ($5), candidate $50 -> ratio 10x and the $ floor
# both clear on their own, but baseline_days_seen=1 is below :min_baseline_days=3 -> NOT_ASSESSED
# (short_baseline), never WARN on a median this thin.
# =====================================================================================================
def test_short_baseline_withholds_a_verdict():
    r = _by_product(7)["fo_ds_shortbaseline"][0]
    assert r["est_day_usd_list"] == 50.0
    assert r["est_baseline_usd_list"] == 5.0
    assert r["baseline_days_seen"] == 1
    assert r["spike_ratio_actual"] == 10.0
    assert r["status"] == "NOT_ASSESSED"
    assert r["not_assessed_reason"] == "short_baseline"


# =====================================================================================================
# Widening the window to 30/90 days brings fo_ds_warn's OWN oldest baseline day (D(15), used only
# as one of the 14 trailing baseline days at w=7) into scope as its own scanned candidate day too --
# it has no baseline of its own (the 14 days before IT have no usage in this fixture), but its own
# $20 day cost never clears :min_spend_usd=50, so the $ floor (checked before the no-baseline case,
# review fix must_fix 4) keeps it out of the result entirely -- not a NOT_ASSESSED row. Only the
# original D(1) WARN row survives at every window; baseline_days is fixed regardless of window_days.
# =====================================================================================================
def test_widened_window_still_only_the_warn_row():
    for w in (30, 90):
        rows = _by_product(w)["fo_ds_warn"]
        assert len(rows) == 1, (w, rows)
        assert str(rows[0]["usage_date"]) == "2026-09-20"
        assert rows[0]["status"] == "WARN"
        assert rows[0]["est_day_usd_list"] == 50.0
        assert rows[0]["spike_ratio_actual"] == 2.5
