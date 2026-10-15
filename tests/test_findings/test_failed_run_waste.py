"""tests/test_findings/test_failed_run_waste.py -- P4-01-W2 (tasks/P4-WASTE-SPEC.md section 8),
proves the rewritten `findings.f_lakeflow_failed_jobs_wasted_dbus` (W2a: failed job runs and
failed attempts later repaired, measured per run from the billing run id) against
tests/fixtures/idle_waste.py's own `iw_job_*` rows (workspace `iw_ws8`; every assertion filters
to those ids, per DEC-15).

Per the spec's own checklist (section 8):
  - the section 6.3 table at window 30 and 7 (D(3)/D(2) are inside both);
  - no row for J5 (cancelled only) or J6 (a repair still in flight);
  - waste_reason exact for J1, J2, J3, J4, J7;
  - invariants on assessed rows: est_wasted = est_failed + est_repair; failure_rate_pct =
    round(100 * failed / distinct, 1); wasted_share_pct <= 100.05;
  - net_dbus for J1 cross-checked against an independent SUM over the raw billing.usage parquet.
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

QUERY_ID = "lakeflow_failed_jobs_wasted_dbus"
GRAINS_PATH = ROOT / "config" / "grains" / "lakeflow_dims.yml"
WINDOWS = (7, 30)
STATUS_VALUES = {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}

# Section 6.3's table at window_days = 30 (D(3) and D(2) are also inside 7 and 90, so the rows
# are identical there -- this file checks 7 and 30).
EXPECTED = {
    "iw_job_mixed": dict(
        distinct_runs=5, failed_runs=2, repaired_runs=0, cancelled_runs=1,
        failed_runs_unbilled=0, failure_rate_pct=40.0,
        last_failed_termination_code="DRIVER_ERROR", last_failed_run_id="iw_job_mixed_r2",
        wasted_dbus=30.0, est_failed_usd_list=9.00, est_repair_usd_list=0.00,
        est_wasted_usd_list=9.00, est_cancelled_usd_list=1.50, net_dbus=110.0,
        est_usd_list=33.00, est_unattributed_usd_list=1.50, wasted_share_pct=27.3,
        price_basis="priced", not_assessed_reason=None, status="WARN",
    ),
    "iw_job_repair": dict(
        distinct_runs=1, failed_runs=0, repaired_runs=1, cancelled_runs=0,
        failed_runs_unbilled=0, failure_rate_pct=0.0,
        last_failed_termination_code="RUN_EXECUTION_ERROR", last_failed_run_id="iw_job_repair_r1",
        wasted_dbus=75.0, est_failed_usd_list=0.00, est_repair_usd_list=22.50,
        est_wasted_usd_list=22.50, est_cancelled_usd_list=0.00, net_dbus=100.0,
        est_usd_list=30.00, est_unattributed_usd_list=0.00, wasted_share_pct=75.0,
        price_basis="priced", not_assessed_reason=None, status="WARN",
    ),
    "iw_job_broken": dict(
        distinct_runs=4, failed_runs=4, repaired_runs=0, cancelled_runs=0,
        failed_runs_unbilled=1, failure_rate_pct=100.0,
        last_failed_termination_code="WORKSPACE_RUN_LIMIT_EXCEEDED",
        last_failed_run_id="iw_job_broken_r4",
        wasted_dbus=150.0, est_failed_usd_list=45.00, est_repair_usd_list=0.00,
        est_wasted_usd_list=45.00, est_cancelled_usd_list=0.00, net_dbus=150.0,
        est_usd_list=45.00, est_unattributed_usd_list=0.00, wasted_share_pct=100.0,
        price_basis="priced", not_assessed_reason=None, status="CRITICAL",
    ),
    "iw_job_norunid": dict(
        distinct_runs=2, failed_runs=2, repaired_runs=0, cancelled_runs=0,
        failed_runs_unbilled=2, failure_rate_pct=100.0,
        last_failed_termination_code="RUN_EXECUTION_ERROR",
        last_failed_run_id="iw_job_norunid_r2",
        wasted_dbus=None, est_failed_usd_list=None, est_repair_usd_list=None,
        est_wasted_usd_list=None, est_cancelled_usd_list=None, net_dbus=20.0,
        est_usd_list=6.00, est_unattributed_usd_list=6.00, wasted_share_pct=None,
        price_basis="priced", not_assessed_reason="no_run_id_in_billing", status="NOT_ASSESSED",
    ),
    "iw_job_nobill": dict(
        distinct_runs=1, failed_runs=1, repaired_runs=0, cancelled_runs=0,
        failed_runs_unbilled=1, failure_rate_pct=100.0,
        last_failed_termination_code="RUN_EXECUTION_ERROR",
        last_failed_run_id="iw_job_nobill_r1",
        wasted_dbus=None, est_failed_usd_list=None, est_repair_usd_list=None,
        est_wasted_usd_list=None, est_cancelled_usd_list=None, net_dbus=None,
        est_usd_list=None, est_unattributed_usd_list=None, wasted_share_pct=None,
        price_basis=None, not_assessed_reason="no_billing_rows", status="NOT_ASSESSED",
    ),
}

WASTE_REASON = {
    "iw_job_mixed": "2 of 5 runs failed (40%); last failure DRIVER_ERROR",
    "iw_job_repair": "0 of 1 runs failed (0%); 1 run failed first and was repaired; last failure "
                      "RUN_EXECUTION_ERROR",
    "iw_job_broken": "4 of 4 runs failed (100%); 1 failed run had no billed usage; last failure "
                     "WORKSPACE_RUN_LIMIT_EXCEEDED",
    "iw_job_norunid": "failed runs or failed attempts found, but this job's billing rows carry no "
                       "run id, so what they cost cannot be measured (the job's whole spend is in "
                       "est_usd_list, not spread over its runs)",
    "iw_job_nobill": "failed runs or failed attempts found, but no billed usage for this job in "
                      "the window - the runs may have failed before compute started, or billing "
                      "has not landed yet",
}


def _grains() -> dict:
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _iw(rows: list[dict]) -> dict[str, dict]:
    return {r["job_id"]: r for r in rows if (r["job_id"] or "").startswith("iw_job_")}


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


def test_grain_and_window_zero():
    grains = _grains()
    assert grains[QUERY_ID] == ["workspace_id", "job_id"]
    assert dbutil.rows(QUERY_ID, 0) == []


def test_scenarios_at_30_and_7():
    for w in WINDOWS:
        by_job = _iw(dbutil.rows(QUERY_ID, w))
        assert set(by_job) == set(EXPECTED), (w, sorted(by_job))
        for job_id, expected in EXPECTED.items():
            _assert_row(by_job[job_id], expected)
            assert by_job[job_id]["waste_reason"] == WASTE_REASON[job_id], (w, job_id)


def test_no_row_for_cancelled_only_or_in_flight_repair():
    for w in WINDOWS:
        by_job = _iw(dbutil.rows(QUERY_ID, w))
        assert "iw_job_cancelonly" not in by_job, w  # J5: cancelled + succeeded only
        assert "iw_job_inflight" not in by_job, w    # J6: last attempt has no end row yet


def test_status_values_valid():
    for w in WINDOWS:
        for r in dbutil.rows(QUERY_ID, w):
            assert r["status"] in STATUS_VALUES, r["status"]


def test_invariants_on_assessed_rows():
    for w in WINDOWS:
        for r in _iw(dbutil.rows(QUERY_ID, w)).values():
            if r["not_assessed_reason"] is not None:
                assert r["status"] == "NOT_ASSESSED"
                continue
            assert r["status"] != "NOT_ASSESSED"
            assert _close(r["est_wasted_usd_list"], r["est_failed_usd_list"] + r["est_repair_usd_list"])
            want_rate = round(100.0 * r["failed_runs"] / r["distinct_runs"], 1)
            assert _close(r["failure_rate_pct"], want_rate)
            if r["wasted_share_pct"] is not None:
                assert r["wasted_share_pct"] <= 100.05


def test_status_not_gated_by_dollar_floor():
    # review fix: the CASE order used to check `waste_raw < :min_waste_usd` before the failure-rate
    # branch, so a cheap-but-broken job could read OK. iw_job_broken fails 4 of 4 runs (100% >=
    # crit_failure_pct=50) and must read CRITICAL from the rate alone, whatever its dollar size.
    for w in WINDOWS:
        row = _iw(dbutil.rows(QUERY_ID, w))["iw_job_broken"]
        assert row["failure_rate_pct"] == 100.0
        assert row["status"] == "CRITICAL"


def test_net_dbus_cross_check_j1():
    for w in WINDOWS:
        row = _iw(dbutil.rows(QUERY_ID, w))["iw_job_mixed"]
        summed = dbutil.usage_sum("usage_metadata.job_id = 'iw_job_mixed'")
        assert _close(row["net_dbus"], summed), (w, row["net_dbus"], summed)
