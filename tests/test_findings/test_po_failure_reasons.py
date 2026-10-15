"""tests/test_findings/test_po_failure_reasons.py

Proves findings.f_po_failure_reasons (grain [catalog_name, schema_name, table_id, table_name,
operation_status], windowed on :period_days) against tests/fixtures/storage.py's own `stg_pofr_*`
rows: the right grain, one row per table x failed-status combo, the OK/WARN/CRITICAL bands off
failed_operations, the reason/fix plain-language mapping for the 3 named operation_status values
plus the generic fallback, that a SUCCESSFUL row never surfaces (the query's own status filter), and
that estimated_dbus_spent sums ESTIMATED_DBU usage only (a same-op row in another usage_unit counts
toward failed_operations but not toward the dollar-shaped sum).

See tests/fixtures/storage.py's own `_build_po_failure_reasons` docstring for the exact scenario
table this file's expectations are computed from.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "po_failure_reasons.yml"
QID = "po_failure_reasons"
WINDOW = 30


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _own() -> list[dict]:
    return [r for r in dbutil.rows(QID, WINDOW) if r["table_id"].startswith("stg_pofr_")]


def _by_table_id() -> dict:
    return {r["table_id"]: r for r in _own()}


def test_grain_uniqueness():
    cols = _grains()[QID]
    out = dbutil.rows(QID, WINDOW)
    assert out
    keys = [tuple(r[c] for c in cols) for r in out]
    assert len(keys) == len(set(keys)), "duplicate grain rows"


def test_status_enum():
    valid = {"OK", "WARN", "CRITICAL"}
    for r in _own():
        assert r["status"] in valid


def test_successful_ops_never_surface():
    # stg_pofr_e is SUCCESSFUL only -- the query's own WHERE operation_status LIKE 'FAILED%' must
    # drop it before it ever reaches GROUP BY, so no row for it exists at all.
    assert "stg_pofr_e" not in _by_table_id()


def test_below_warn_reads_ok():
    # stg_pofr_a: 2 FAILED: INTERNAL_ERROR ops (< warn_failed_ops=5) -> OK.
    r = _by_table_id()["stg_pofr_a"]
    assert r["operation_status"] == "FAILED: INTERNAL_ERROR"
    assert r["failed_operations"] == 2
    assert r["status"] == "OK"
    assert r["reason"] == "a Databricks-side error; usually resolves on retry"
    assert r["fix"] == "retry; if it keeps failing, open a support ticket with the operation_type and time"


def test_estimated_dbu_only_sum():
    # stg_pofr_a's 2 ops: one 1.0 ESTIMATED_DBU, one 999.0 in a different usage_unit -- the second
    # must count toward failed_operations but NOT toward estimated_dbus_spent.
    r = _by_table_id()["stg_pofr_a"]
    assert r["failed_operations"] == 2
    assert r["estimated_dbus_spent"] == 1.0


def test_warn_band_and_reason():
    # stg_pofr_b: 7 FAILED: PRIVATE_LINK_SETUP_ERROR ops (>=5, <20) -> WARN.
    r = _by_table_id()["stg_pofr_b"]
    assert r["operation_status"] == "FAILED: PRIVATE_LINK_SETUP_ERROR"
    assert r["failed_operations"] == 7
    assert r["estimated_dbus_spent"] == 7.0
    assert r["status"] == "WARN"
    assert r["reason"] == "network setup blocks predictive optimization from reaching this table"
    assert "private link" in r["fix"]


def test_critical_band_and_reason():
    # stg_pofr_c: 25 FAILED: AUTO_TTL_COLUMN_DOES_NOT_EXIST_ERROR ops (>=20) -> CRITICAL.
    r = _by_table_id()["stg_pofr_c"]
    assert r["operation_status"] == "FAILED: AUTO_TTL_COLUMN_DOES_NOT_EXIST_ERROR"
    assert r["failed_operations"] == 25
    assert r["estimated_dbus_spent"] == 12.5
    assert r["status"] == "CRITICAL"
    assert r["reason"] == "auto-TTL is configured to a column that does not exist on this table"


def test_unmapped_status_falls_back_to_generic_reason_fix():
    # stg_pofr_d: 6 FAILED: SOME_OTHER_ERROR ops (>=5, <20) -> WARN via the generic ELSE branch,
    # not one of the 3 named reasons.
    r = _by_table_id()["stg_pofr_d"]
    assert r["operation_status"] == "FAILED: SOME_OTHER_ERROR"
    assert r["status"] == "WARN"
    assert r["reason"] == "operation failed; see operation_status for detail"
    assert r["fix"] == "check the table and re-run predictive optimization manually"


def test_worst_first_order():
    own = _own()
    ranks = {"CRITICAL": 0, "WARN": 1, "OK": 2}
    order = [ranks[r["status"]] for r in own]
    assert order == sorted(order)
