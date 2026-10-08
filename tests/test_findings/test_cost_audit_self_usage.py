"""tests/test_findings/test_cost_audit_self_usage.py

Proves, against tests/fixtures/compute_coverage.py's own `cc_wh_audit_*` rows (workspace
cc_ws1), that findings.f_cost_audit_self_usage (grain [workspace_id, warehouse_id], window_days
= 30, coverage-only -- no status column) has the right grain, matches this export's own
statements by client_application (never by the shipped library query's dead statement_text
marker), prices the matched share of a warehouse's spend, and drops a warehouse entirely when
none of its statements match. Every expected number is computed by hand from that fixture
module's own scenario table (also cross-checked directly against DuckDB in-session before this
file was written).
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "cost_audit_self_usage.yml"
QID = "cost_audit_self_usage"
CC_WS = "cc_ws1"
_AUDIT_IDS = {"cc_wh_audit_match", "cc_wh_audit_nobill", "cc_wh_audit_none"}


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _rows():
    return [r for r in dbutil.rows(QID, 30, workspace_ids=[CC_WS]) if r["warehouse_id"] in _AUDIT_IDS]


def _by_wh(warehouse_id: str) -> dict | None:
    matches = [r for r in _rows() if r["warehouse_id"] == warehouse_id]
    assert len(matches) <= 1
    return matches[0] if matches else None


def test_grain_uniqueness():
    grains = _grains()
    cols = grains[QID]
    out = dbutil.rows(QID, 30)
    assert out
    keys = [tuple(r[c] for c in cols) for r in out]
    assert len(keys) == len(set(keys)), "duplicate grain rows"


def test_no_status_column():
    for r in _rows():
        assert "status" not in r


def test_matched_and_priced():
    r = _by_wh("cc_wh_audit_match")
    assert r is not None
    assert r["matched_statement_count"] == 2
    assert r["matched_duration_secs"] == 3.0
    assert r["warehouse_all_duration_secs"] == 6.0
    assert r["matched_share_pct"] == 50.0
    assert r["est_usd_list"] == 10.0
    assert r["est_audit_usd_list"] == 5.0
    assert r["price_basis"] == "priced"


def test_matched_but_unpriced_without_billing():
    r = _by_wh("cc_wh_audit_nobill")
    assert r is not None
    assert r["matched_statement_count"] == 1
    assert r["matched_share_pct"] == 50.0
    assert r["est_usd_list"] is None
    assert r["est_audit_usd_list"] is None
    assert r["price_basis"] is None


def test_no_match_produces_no_row():
    assert _by_wh("cc_wh_audit_none") is None
