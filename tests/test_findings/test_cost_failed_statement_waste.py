"""tests/test_findings/test_cost_failed_statement_waste.py -- P4-01-W2 (tasks/P4-WASTE-SPEC.md
section 8), proves the new app-owned `findings.f_cost_failed_statement_waste` (W2b: failed SQL
statements and the compute Databricks attributed to them) against tests/fixtures/idle_waste.py's
own `iw_wh_fail*` rows (workspace `iw_ws8`; every assertion filters to those ids, per DEC-15).

Per the spec's own checklist (section 8):
  - grain, columns, the section 6.4 table and its waste_reason texts;
  - no row for any W1 warehouse (iw_wh_pro/multi/sls/... never had a FAILED statement);
  - invariants: est_wasted <= est_attributed, and the reason/status pairing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR / "fixtures"))
import dbutil  # noqa: E402

QUERY_ID = "cost_failed_statement_waste"
GRAINS_PATH = ROOT / "config" / "grains" / f"{QUERY_ID}.yml"
WINDOWS = (7, 30, 90)
STATUS_VALUES = {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}

COLUMNS = [
    "window_days",
    "workspace_id", "warehouse_id", "statements", "failed_statements", "canceled_statements",
    "failed_with_cost", "failed_dbus", "est_wasted_usd_list", "est_canceled_usd_list",
    "est_attributed_usd_list", "failed_cost_share_pct", "price_basis", "cost_basis", "waste_reason",
    "not_assessed_reason", "status",
]

# Section 6.4's table (D(2), inside every window this file checks).
EXPECTED = {
    "iw_wh_fail": dict(
        statements=4, failed_statements=2, canceled_statements=1, failed_with_cost=1,
        failed_dbus=12.0, est_wasted_usd_list=6.00, est_canceled_usd_list=0.50,
        est_attributed_usd_list=8.50, failed_cost_share_pct=70.6, price_basis="priced",
        cost_basis="statement", not_assessed_reason=None, status="WARN",
    ),
    "iw_wh_fail_noattr": dict(
        statements=2, failed_statements=2, canceled_statements=0, failed_with_cost=0,
        failed_dbus=None, est_wasted_usd_list=None, est_canceled_usd_list=None,
        est_attributed_usd_list=None, failed_cost_share_pct=None, price_basis=None,
        cost_basis=None, not_assessed_reason="no_billing_rows", status="NOT_ASSESSED",
    ),
    # Hour 18 ($10): h1 300 of 500 task ms -> $6.00 / 12 DBU, h3 canceled 100 -> $2.00. Hour 19 ($2):
    # h4's in-hour half (100) of 200 -> $1.00 / 2 DBU. h6 failed with no task time.
    "iw_hf_wh": dict(
        statements=6, failed_statements=3, canceled_statements=1, failed_with_cost=2,
        failed_dbus=14.0, est_wasted_usd_list=7.00, est_canceled_usd_list=2.00,
        est_attributed_usd_list=12.00, failed_cost_share_pct=58.3, price_basis="priced",
        cost_basis="run_time_share", not_assessed_reason=None, status="WARN",
    ),
}

WASTE_REASON = {
    "iw_wh_fail": "2 of 4 statements failed; 1 of them used billed compute; "
                  "1 canceled, not counted as waste",
    "iw_wh_fail_noattr": "failed statements found, but the warehouse has no billed usage in the "
                          "window, so there is nothing to price",
    "iw_hf_wh": "3 of 6 statements failed; 2 of them ran in billed hours, priced at their share "
                   "of the task time in those hours (no per-statement cost in "
                   "billing.attributed_usage); 1 canceled, not counted as waste",
}

W1_WAREHOUSES = {
    "iw_wh_pro", "iw_wh_multi", "iw_wh_sls", "iw_wh_noev", "iw_wh_quiet", "iw_wh_open",
    "iw_wh_carry", "iw_wh_nostart",
}


def _grains() -> dict:
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _iw(rows: list[dict]) -> dict[str, dict]:
    return {r["warehouse_id"]: r for r in rows if (r["warehouse_id"] or "").startswith(("iw_wh_", "iw_hf_"))}


def _close(a, b, tol: float = 1e-6) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) < tol


def _assert_row(actual: dict, expected: dict) -> None:
    for key, want in expected.items():
        got = actual[key]
        if isinstance(want, float):
            assert _close(got, want), f"{key}: got {got!r}, want {want!r}"
        else:
            assert got == want, f"{key}: got {got!r}, want {want!r}"


def test_grain_columns_and_window_zero():
    grains = _grains()
    assert grains[QUERY_ID] == ["workspace_id", "warehouse_id"]
    for w in WINDOWS:
        out = dbutil.rows(QUERY_ID, w)
        assert out, f"no rows at window {w}"
        assert list(out[0].keys()) == COLUMNS, list(out[0].keys())
    assert dbutil.rows(QUERY_ID, 0) == []


def test_scenarios_at_every_window():
    for w in WINDOWS:
        by_wh = _iw(dbutil.rows(QUERY_ID, w))
        assert set(by_wh) == set(EXPECTED), (w, sorted(by_wh))
        for wh, expected in EXPECTED.items():
            _assert_row(by_wh[wh], expected)
            assert by_wh[wh]["waste_reason"] == WASTE_REASON[wh], (w, wh)


def test_no_row_for_any_w1_warehouse():
    for w in WINDOWS:
        by_wh = _iw(dbutil.rows(QUERY_ID, w))
        assert not (set(by_wh) & W1_WAREHOUSES), (w, set(by_wh) & W1_WAREHOUSES)


def test_invariants():
    for w in WINDOWS:
        for r in _iw(dbutil.rows(QUERY_ID, w)).values():
            assert r["status"] in STATUS_VALUES, r["status"]
            assert (r["not_assessed_reason"] is not None) == (r["status"] == "NOT_ASSESSED")
            if r["est_attributed_usd_list"] is not None and r["est_wasted_usd_list"] is not None:
                assert r["est_wasted_usd_list"] <= r["est_attributed_usd_list"] + 0.01
