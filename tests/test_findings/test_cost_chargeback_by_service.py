"""tests/test_findings/test_cost_chargeback_by_service.py.

f_cost_chargeback_by_service reads system.billing.usage + system.billing.list_prices
(tests/fixtures/chargeback_a.py, workspace cba.WS_SVC) and chargebacks list-priced spend per
workspace + billing_origin_product, current window vs the equal-length window right before it. Its
own scenarios use SYNTHETIC, cba_-prefixed product values rather than real Databricks names - a
real name is shared with other builders' own fixtures (each in their own workspace, so it never
collides on the workspace + product grain, but would on product alone; see chargeback_a.py's own
scenario map). Every expected number below is hand-derived directly from it.
"""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from dbutil import rows  # noqa: E402

QID = "cost_chargeback_by_service"


def _by_product(rows_: list[dict]) -> dict:
    return {r["billing_origin_product"]: r for r in rows_}


def test_grain_is_unique_and_window_days_is_set():
    rows_30 = rows(QID, 30)
    assert len(rows_30) >= 1
    for row in rows_30:
        assert row["window_days"] == 30
    keys = [(r["workspace_id"], r["billing_origin_product"]) for r in rows_30]
    assert len(keys) == len(set(keys)), "duplicate (workspace_id, billing_origin_product) grain"


def test_ok_band_and_fallback_service_label():
    """CBA_SVC_OK: cur=40, prev=38 -> change 5.3% -> OK. Not a recognized product name, so
    service_label falls back to the generic lowercase-with-spaces form (no hand-picked mapping
    exists for it -- initcap has no DuckDB translation rule, see app/core/translate.py)."""
    r = _by_product(rows(QID, 30))["CBA_SVC_OK"]
    assert r["usd_list"] == 40.0
    assert r["prev_usd_list"] == 38.0
    assert r["change_pct"] == 5.3
    assert r["status"] == "OK"
    assert r["service_label"] == "cba svc ok"


def test_warn_band_at_exact_threshold():
    """CBA_SVC_WARN: cur=125, prev=100 -> change 25.0% -> WARN."""
    r = _by_product(rows(QID, 30))["CBA_SVC_WARN"]
    assert r["change_pct"] == 25.0
    assert r["status"] == "WARN"


def test_critical_band():
    """CBA_SVC_CRIT: cur=300, prev=100 -> change 200% -> CRITICAL."""
    r = _by_product(rows(QID, 30))["CBA_SVC_CRIT"]
    assert r["change_pct"] == 200.0
    assert r["status"] == "CRITICAL"


def test_current_period_unpriced_reads_not_assessed():
    """CBA_SVC_NA: current window entirely unpriced, previous priced -> NOT_ASSESSED /
    current_period_unpriced."""
    r = _by_product(rows(QID, 30))["CBA_SVC_NA"]
    assert r["usd_list"] is None
    assert r["price_basis"] == "unpriced"
    assert r["status"] == "NOT_ASSESSED"
    assert r["not_assessed_reason"] == "current_period_unpriced"


def test_unattributed_product_kept_as_its_own_line():
    """A NULL billing_origin_product is kept as its own single 'Unattributed' line, never dropped
    and never split - chargeback_a.py alone contributes $15 (below :min_spend_usd) to it, but
    another builder's fixture could add more, so only the shape (exactly one such row, at least $15)
    is asserted."""
    unattr_rows = [r for r in rows(QID, 30) if r["billing_origin_product"] is None]
    assert len(unattr_rows) == 1, "unattributed (NULL billing_origin_product) usage must collapse to one row"
    assert unattr_rows[0]["service_label"] == "Unattributed"
    assert unattr_rows[0]["usd_list"] >= 15.0
