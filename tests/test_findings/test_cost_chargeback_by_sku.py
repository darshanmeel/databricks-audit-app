"""tests/test_findings/test_cost_chargeback_by_sku.py.

f_cost_chargeback_by_sku reads system.billing.usage + system.billing.list_prices
(tests/fixtures/chargeback_a.py, workspace cba.WS_SKU) and chargebacks list-priced spend per
workspace + (sku_name, cloud, usage_unit), current window vs the equal-length window right before
it. Every expected number below is hand-derived directly from chargeback_a.py's own scenario map.
"""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from dbutil import rows  # noqa: E402

QID = "cost_chargeback_by_sku"


def _by_sku(rows_: list[dict]) -> dict:
    return {r["sku_name"]: r for r in rows_}


def test_grain_is_unique_and_window_days_is_set():
    rows_30 = rows(QID, 30)
    assert len(rows_30) >= 1
    for row in rows_30:
        assert row["window_days"] == 30
    keys = [(r["workspace_id"], r["sku_name"], r["cloud"], r["usage_unit"]) for r in rows_30]
    assert len(keys) == len(set(keys)), "duplicate (workspace_id, sku_name, cloud, usage_unit) grain"


def test_ok_band():
    """cba_SKU_A: cur=40, prev=38 -> change 5.3% -> OK; dbus populated (usage_unit=DBU)."""
    r = _by_sku(rows(QID, 30))["cba_SKU_A"]
    assert r["usd_list"] == 40.0
    assert r["prev_usd_list"] == 38.0
    assert r["change_pct"] == 5.3
    assert r["status"] == "OK"
    assert r["dbus"] == 40.0


def test_warn_band_at_exact_threshold():
    """cba_SKU_B: cur=125, prev=100 -> change 25.0% -> WARN."""
    r = _by_sku(rows(QID, 30))["cba_SKU_B"]
    assert r["change_pct"] == 25.0
    assert r["status"] == "WARN"


def test_critical_band():
    """cba_SKU_C: cur=200, prev=100 -> change 100% -> CRITICAL."""
    r = _by_sku(rows(QID, 30))["cba_SKU_C"]
    assert r["change_pct"] == 100.0
    assert r["status"] == "CRITICAL"


def test_previous_period_unpriced_reads_not_assessed():
    """cba_SKU_NA1: its list_price only starts 20 days ago, so the D(40) (previous) row predates
    it (unpriced) while the D(5) (current) row is priced -> NOT_ASSESSED / previous_period_unpriced,
    never a false CRITICAL from a NULL previous."""
    r = _by_sku(rows(QID, 30))["cba_SKU_NA1"]
    assert r["usd_list"] == 40.0
    assert r["prev_usd_list"] is None
    assert r["status"] == "NOT_ASSESSED"
    assert r["not_assessed_reason"] == "previous_period_unpriced"


def test_non_dbu_unit_has_no_dbus():
    """cba_SKU_STORAGE (GB): cur=100, prev=95 -> change 5.3% -> OK; dbus is NULL (non-DBU unit)."""
    r = _by_sku(rows(QID, 30))["cba_SKU_STORAGE"]
    assert r["usage_unit"] == "GB"
    assert r["usd_list"] == 100.0
    assert r["change_pct"] == 5.3
    assert r["status"] == "OK"
    assert r["dbus"] is None
