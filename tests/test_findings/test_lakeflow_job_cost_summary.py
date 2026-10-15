"""tests/test_findings/test_lakeflow_job_cost_summary.py -- P3-JOBCOST.

Proves, against tests/fixtures/lakeflow.py's SEC R rows (`lf_jrc_*`, DEC-15, shared with
test_lakeflow_job_run_cost.py), that findings.f_lakeflow_job_cost_summary (grain
[workspace_id, job_id], an inventory - no status column) correctly rolls lakeflow_job_run_cost's
own per-run rows up to (runs, net_job_dbus, net_list_cost, median/max per run): the right
grain, the job-level price_basis propagation rule (any unpriced run makes the whole job read
unpriced, even when most of it priced fine; a job with only free-usage runs reads free), the
job_run_id-less orphan row excluded from the job's own total, and median/max spread across a job's
several runs, at every window including the AS_OF-day exclusion.

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

GRAINS_PATH = ROOT / "config" / "grains" / "lakeflow_job_cost_summary.yml"

WINDOWS = (7, 30, 90)
QID = "lakeflow_job_cost_summary"


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _by_job(window_days: int) -> dict:
    return {
        r["job_id"]: r
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
    for r in dbutil.rows(QID, 30):
        assert "status" not in r


def test_cost_descending_order_nulls_last():
    out = [r for r in dbutil.rows(QID, 30) if r["job_id"].startswith("lf_jrc_")]
    costs = [r["net_list_cost"] for r in out]
    non_null = [c for c in costs if c is not None]
    assert non_null == sorted(non_null, reverse=True)


# =====================================================================================================
# lf_jrc_job_mix: runs=3, net_job_dbus=170, net_list_cost=60.0 (25.0+24.0+11.0), all priced.
# median_run_dbus=40.0 (sorted 30/40/100), max_run_dbus=100.0; est_median_usd_list=24.0
# (sorted 11.0/24.0/25.0), est_max_usd_list=25.0.
# =====================================================================================================
def test_job_mix_totals_and_spread():
    for w in WINDOWS:
        r = _by_job(w)["lf_jrc_job_mix"]
        assert r["job_name"] == "job run cost mix"
        assert r["runs"] == 3
        assert r["net_job_dbus"] == 170.0
        assert r["net_list_cost"] == 60.0
        assert r["price_basis"] == "priced"
        assert r["median_run_dbus"] == 40.0
        assert r["max_run_dbus"] == 100.0
        assert r["est_median_usd_list"] == 24.0
        assert r["est_max_usd_list"] == 25.0


# =====================================================================================================
# lf_jrc_job_unpriced: one priced run (50 DBU/$12.5) + one genuinely-unpriced run (20 DBU/NULL) ->
# job total DBUs sums both (70), job dollar total sums only the priced run (12.5, never forced to
# 0), and price_basis reads 'unpriced' for the WHOLE job even though most of it priced fine.
# =====================================================================================================
def test_job_unpriced_propagates_from_one_run():
    for w in WINDOWS:
        r = _by_job(w)["lf_jrc_job_unpriced"]
        assert r["runs"] == 2
        assert r["net_job_dbus"] == 70.0
        assert r["net_list_cost"] == 12.5
        assert r["price_basis"] == "unpriced"
        assert r["median_run_dbus"] == 35.0
        assert r["max_run_dbus"] == 50.0
        assert r["est_median_usd_list"] == 12.5
        assert r["est_max_usd_list"] == 12.5


# =====================================================================================================
# lf_jrc_job_free: one run, 15 DBU on a FREE_USAGE-named SKU -> net_list_cost NULL,
# price_basis='free' (a real $0 for the whole job, never a gap).
# =====================================================================================================
def test_job_free_usage_only():
    for w in WINDOWS:
        r = _by_job(w)["lf_jrc_job_free"]
        assert r["runs"] == 1
        assert r["net_job_dbus"] == 15.0
        assert r["net_list_cost"] is None
        assert r["price_basis"] == "free"
        assert r["median_run_dbus"] == 15.0
        assert r["max_run_dbus"] == 15.0
        assert r["est_median_usd_list"] is None
        assert r["est_max_usd_list"] is None


# =====================================================================================================
# lf_jrc_job_orphan: the 500-DBU job_run_id-less usage row must never reach this roll-up either --
# runs=1 (only the real run), net_job_dbus=10 (never 510), net_list_cost=2.5.
# =====================================================================================================
def test_job_orphan_excludes_job_run_id_less_row():
    for w in WINDOWS:
        r = _by_job(w)["lf_jrc_job_orphan"]
        assert r["runs"] == 1
        assert r["net_job_dbus"] == 10.0
        assert r["net_list_cost"] == 2.5
        assert r["price_basis"] == "priced"


# =====================================================================================================
# lf_jrc_job_inflight: one still-running run still rolls up normally (the job total does not need a
# landed end row to be priced -- only the timeline-derived columns on lakeflow_job_run_cost do).
# =====================================================================================================
def test_job_inflight_totals():
    for w in WINDOWS:
        r = _by_job(w)["lf_jrc_job_inflight"]
        assert r["runs"] == 1
        assert r["net_job_dbus"] == 25.0
        assert r["net_list_cost"] == 15.0
        assert r["price_basis"] == "priced"


# =====================================================================================================
# lf_jrc_job_win: window-boundary + AS_OF-day (D0) exclusion, matching
# test_lakeflow_job_run_cost.py's own per-run numbers rolled up.
# window=7:  runs=1, net_job_dbus=5,  net_list_cost=1.25.
# window=30: runs=2, net_job_dbus=12, net_list_cost=3.0,  median_run_dbus=6.0  (5,7 interpolated),
#            max_run_dbus=7.0, est_median_usd_list=1.5, est_max_usd_list=1.75.
# window=90: runs=3, net_job_dbus=23, net_list_cost=5.75, median_run_dbus=7.0  (5,7,11 sorted),
#            max_run_dbus=11.0, est_median_usd_list=1.75, est_max_usd_list=2.75.
# The D(0) run (999 DBU) must never move any of these totals at any window.
# =====================================================================================================
def test_job_win_window_boundary_and_asof_day_excluded():
    r7 = _by_job(7)["lf_jrc_job_win"]
    assert r7["runs"] == 1
    assert r7["net_job_dbus"] == 5.0
    assert r7["net_list_cost"] == 1.25

    r30 = _by_job(30)["lf_jrc_job_win"]
    assert r30["runs"] == 2
    assert r30["net_job_dbus"] == 12.0
    assert r30["net_list_cost"] == 3.0
    assert r30["median_run_dbus"] == 6.0
    assert r30["max_run_dbus"] == 7.0
    assert r30["est_median_usd_list"] == 1.5
    assert r30["est_max_usd_list"] == 1.75

    r90 = _by_job(90)["lf_jrc_job_win"]
    assert r90["runs"] == 3
    assert r90["net_job_dbus"] == 23.0
    assert r90["net_list_cost"] == 5.75
    assert r90["median_run_dbus"] == 7.0
    assert r90["max_run_dbus"] == 11.0
    assert r90["est_median_usd_list"] == 1.75
    assert r90["est_max_usd_list"] == 2.75
