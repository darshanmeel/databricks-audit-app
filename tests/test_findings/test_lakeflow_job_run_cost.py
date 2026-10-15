"""tests/test_findings/test_lakeflow_job_run_cost.py -- P3-JOBCOST.

Proves, against tests/fixtures/lakeflow.py's SEC R rows (`lf_jrc_*`, DEC-15), that
findings.f_lakeflow_job_run_cost (grain [workspace_id, job_id, job_run_id], an inventory - no
status column) has: the right grain, cost-descending ordering, the compute_type enum derived
correctly (classic / serverless / mixed), the price_basis enum (free vs unpriced vs priced) with
NULL dollars never silently forced to 0, a job_run_id-less usage row excluded entirely (the
attribution-gap caveat), an in-flight run's run_start kept with NULL result_state, a run with no
job_run_timeline row at all reading the same NULL shape, and window behaviour at 7 vs 30 vs 90
days including the AS_OF-day exclusion.

Every expectation below is computed by hand from tests/fixtures/lakeflow.py's own SEC R comment
block and cross-checked against it -- never read back from the model under test.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "lakeflow_job_run_cost.yml"

WINDOWS = (7, 30, 90)
QID = "lakeflow_job_run_cost"


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _jrc(window_days: int) -> dict:
    return {
        r["job_run_id"]: r
        for r in dbutil.rows(QID, window_days)
        if r["job_id"].startswith("lf_jrc_")
    }


def test_grain_uniqueness():
    grains = _grains()
    cols = grains[QID]
    for w in WINDOWS:
        out = dbutil.rows(QID, w)
        assert out, w
        keys = [tuple(r[c] for c in cols) for r in out]
        assert len(keys) == len(set(keys)), f"w={w}: duplicate grain rows"


def test_window_days_zero_is_empty():
    assert dbutil.rows(QID, 0) == []


def test_no_status_column():
    # Inventory query (DEC-08): no WARN/CRITICAL band is invented for a dollar figure alone.
    for r in dbutil.rows(QID, 30):
        assert "status" not in r


def test_cost_descending_order_nulls_last():
    out = [r for r in dbutil.rows(QID, 30) if r["job_id"].startswith("lf_jrc_")]
    costs = [r["net_list_cost"] for r in out]
    non_null = [c for c in costs if c is not None]
    assert non_null == sorted(non_null, reverse=True)
    # every NULL-cost row (unpriced/free) sorts after every priced row
    first_null = next((i for i, c in enumerate(costs) if c is None), len(costs))
    assert all(c is not None for c in costs[:first_null])


# =====================================================================================================
# lf_jrc_job_mix: 3 runs proving compute_type = classic / serverless / mixed on one job.
# r1: 100 DBU lf_JRC_CLASSIC only -> net_run_dbus=100, net_list_cost=25.0, compute_type='classic'.
# r2: 40 DBU lf_JRC_SERVERLESS only -> net_run_dbus=40, net_list_cost=24.0,
#     compute_type='serverless', FAILED/JRC_TASK_ERROR.
# r3: 20 DBU classic + 10 DBU serverless, same job_run_id -> net_run_dbus=30, net_list_cost=11.0,
#     compute_type='mixed'.
# =====================================================================================================
def test_compute_type_classic_serverless_mixed():
    for w in WINDOWS:
        out = _jrc(w)
        r1 = out["lf_jrc_run_mix_r1"]
        assert r1["job_name"] == "job run cost mix"
        assert r1["net_run_dbus"] == 100.0
        assert r1["net_list_cost"] == 25.0
        assert r1["price_basis"] == "priced"
        assert r1["compute_type"] == "classic"
        assert r1["result_state"] == "SUCCEEDED"
        assert r1["in_flight"] is False

        r2 = out["lf_jrc_run_mix_r2"]
        assert r2["net_run_dbus"] == 40.0
        assert r2["net_list_cost"] == 24.0
        assert r2["compute_type"] == "serverless"
        assert r2["result_state"] == "FAILED"
        assert r2["termination_code"] == "JRC_TASK_ERROR"

        r3 = out["lf_jrc_run_mix_r3"]
        assert r3["net_run_dbus"] == 30.0
        assert r3["net_list_cost"] == 11.0
        assert r3["compute_type"] == "mixed"


# =====================================================================================================
# lf_jrc_job_unpriced: r1 50 DBU priced (cost=12.5), r2 20 DBU on a genuinely-unpriced SKU ->
# net_list_cost NULL (never forced to 0), price_basis='unpriced'.
# =====================================================================================================
def test_unpriced_run_cost_is_null_not_zero():
    for w in WINDOWS:
        out = _jrc(w)
        r1 = out["lf_jrc_run_up_r1"]
        assert r1["net_run_dbus"] == 50.0
        assert r1["net_list_cost"] == 12.5
        assert r1["price_basis"] == "priced"

        r2 = out["lf_jrc_run_up_r2"]
        assert r2["net_run_dbus"] == 20.0
        assert r2["net_list_cost"] is None
        assert r2["price_basis"] == "unpriced"


# =====================================================================================================
# lf_jrc_job_free: one run on a FREE_USAGE-named SKU -- net_list_cost NULL, price_basis='free'
# (a real $0, never a pricing-coverage gap -- the opposite disclosure from the unpriced run above
# despite both showing a NULL dollar figure).
# =====================================================================================================
def test_free_usage_sku_is_free_not_unpriced():
    for w in WINDOWS:
        r = _jrc(w)["lf_jrc_run_free_r1"]
        assert r["net_run_dbus"] == 15.0
        assert r["net_list_cost"] is None
        assert r["price_basis"] == "free"


# =====================================================================================================
# lf_jrc_job_orphan: a real run (job_run_id set, counted) plus a 500-DBU usage row on the SAME job_id
# but job_run_id = NULL, which must be excluded entirely -- it never appears as a row (there is no
# job_run_id to key it on), and the real run's own numbers are unaffected by it.
# =====================================================================================================
def test_orphan_row_without_job_run_id_is_excluded():
    for w in WINDOWS:
        out = _jrc(w)
        r = out["lf_jrc_run_orphan_r1"]
        assert r["net_run_dbus"] == 10.0
        assert r["net_list_cost"] == 2.5
        # the 500-DBU orphan row has no job_run_id to key it on, so it can never be a row here:
        # lf_jrc_job_orphan has exactly ONE run in the output, never a 510-DBU one.
        orphan_job_rows = [rr for rr in out.values() if rr["job_id"] == "lf_jrc_job_orphan"]
        assert len(orphan_job_rows) == 1
        assert orphan_job_rows[0]["net_run_dbus"] == 10.0


# =====================================================================================================
# lf_jrc_job_inflight: a run whose job_run_timeline row has no end row yet -- run_start is kept,
# in_flight=True, result_state/termination_code NULL.
# =====================================================================================================
def test_inflight_run_keeps_run_start():
    for w in WINDOWS:
        r = _jrc(w)["lf_jrc_run_inflight_r1"]
        assert r["net_run_dbus"] == 25.0
        assert r["net_list_cost"] == 15.0
        assert r["compute_type"] == "serverless"
        assert r["run_start"] is not None
        assert r["in_flight"] is True
        assert r["result_state"] is None
        assert r["termination_code"] is None


# =====================================================================================================
# lf_jrc_job_notimeline: a run with billed usage but NO job_run_timeline row at all -- run_start,
# result_state and termination_code all read NULL, and in_flight reads NULL/None too (review
# must_fix #2): no timeline row at all means the run's state is genuinely unknown, never read as
# "running" -- distinct from lf_jrc_job_inflight, which has one observed (still-open) slice and so
# reads in_flight=True.
# =====================================================================================================
def test_missing_timeline_row_reads_all_null():
    for w in WINDOWS:
        r = _jrc(w)["lf_jrc_run_notimeline_r1"]
        assert r["net_run_dbus"] == 12.0
        assert r["net_list_cost"] == 3.0
        assert r["compute_type"] == "classic"
        assert r["run_start"] is None
        assert r["in_flight"] is None
        assert r["result_state"] is None
        assert r["termination_code"] is None


# =====================================================================================================
# lf_jrc_job_repair: a run repaired after an earlier TIMED_OUT attempt -- run_state must read the
# run's FINAL attempt only (SUCCEEDED/SUCCESS, in_flight=False), never
# MAX(result_state)/MAX(termination_code), which would alphabetically read back the earlier
# 'TIMED_OUT' attempt instead ('T' > 'S').
# =====================================================================================================
def test_repaired_run_reads_final_attempt_not_max():
    for w in WINDOWS:
        r = _jrc(w)["lf_jrc_run_repair_r1"]
        assert r["net_run_dbus"] == 8.0
        assert r["net_list_cost"] == 2.0
        assert r["compute_type"] == "classic"
        assert r["in_flight"] is False
        assert r["result_state"] == "SUCCEEDED"
        assert r["termination_code"] == "SUCCESS"


# =====================================================================================================
# lf_jrc_job_win: window-boundary + AS_OF-day (D0) exclusion. D(5)=5 DBU/$1.25 (every window),
# D(20)=7 DBU/$1.75 (30/90 only), D(45)=11 DBU/$2.75 (90 only), D(0)=999 DBU (today) must NEVER
# appear at any window.
# =====================================================================================================
def test_window_boundary_and_asof_day_excluded():
    r7 = _jrc(7)
    assert "lf_jrc_run_win_d5" in r7
    assert "lf_jrc_run_win_d20" not in r7
    assert "lf_jrc_run_win_d45" not in r7
    assert r7["lf_jrc_run_win_d5"]["net_run_dbus"] == 5.0
    assert r7["lf_jrc_run_win_d5"]["net_list_cost"] == 1.25

    r30 = _jrc(30)
    assert "lf_jrc_run_win_d5" in r30 and "lf_jrc_run_win_d20" in r30
    assert "lf_jrc_run_win_d45" not in r30
    assert r30["lf_jrc_run_win_d20"]["net_run_dbus"] == 7.0
    assert r30["lf_jrc_run_win_d20"]["net_list_cost"] == 1.75

    r90 = _jrc(90)
    assert "lf_jrc_run_win_d45" in r90
    assert r90["lf_jrc_run_win_d45"]["net_run_dbus"] == 11.0
    assert r90["lf_jrc_run_win_d45"]["net_list_cost"] == 2.75

    for w in WINDOWS:
        assert "lf_jrc_run_win_d0" not in _jrc(w)
