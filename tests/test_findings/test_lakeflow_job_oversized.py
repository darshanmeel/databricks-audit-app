"""tests/test_findings/test_lakeflow_job_oversized.py

Proves, against tests/fixtures/oversized_jobs.py's own `ovj_*` rows, that
findings.f_lakeflow_job_oversized (grain [workspace_id, job_id]) correctly judges a job's pooled
worker CPU p90 and memory peak (and any swap) against the header's oversized thresholds, prices the job at list (the same
DEC-66.1 join lakeflow_job_run_cost / lakeflow_job_cost_summary use), picks the right
est_saving_usd_list multiplier (0.5 when CPU p90 and memory peak sit under HALF their
thresholds, 0.25 otherwise), the right suggested_action (workers seen < configured -> fewer workers, else one node
size down), the CRITICAL/WARN/OK dollar bands, and NOT_ASSESSED (too_few_runs) when fewer than
:min_runs job-cluster runs carried node telemetry.

Every expectation below is computed by hand from tests/fixtures/oversized_jobs.py's own scenario
table and cross-checked against it -- never read back from the model under test.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "lakeflow_job_oversized.yml"
WINDOWS = (7, 30, 90)
QID = "lakeflow_job_oversized"


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _by_job(window_days: int) -> dict:
    return {
        r["job_id"]: r
        for r in dbutil.rows(QID, window_days)
        if r["job_id"].startswith("ovj_")
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


def test_status_enum():
    for r in dbutil.rows(QID, 30):
        assert r["status"] in {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}


# =====================================================================================================
# ovj_crit: 3 runs, autoscale cluster (max 4, 2 workers seen), worker CPU p90 10% / mem p90 15% --
# both under HALF the thresholds (15 / 20) -- and $1000 of job compute (2000 DBU @ $0.5) ->
# oversized TRUE, est_saving_usd_list = 1000 x 0.5 = 500 -> CRITICAL (>= 200); workers seen (2) <
# configured (4) -> 'fewer workers / lower autoscale max'.
# =====================================================================================================
def test_critical_both_under_half_threshold():
    for w in WINDOWS:
        r = _by_job(w)["ovj_crit"]
        assert r["runs_with_telemetry"] == 3
        assert r["single_node"] is False
        assert r["worker_cpu_p90_pct"] == 10.0
        assert r["worker_mem_p90_pct"] == 15.0
        assert r["worker_cpu_peak_pct"] == 10.0
        assert r["worker_mem_peak_pct"] == 15.0
        assert r["worker_swap_peak_pct"] == 0.0
        assert r["workers_configured_max"] == 4
        assert r["worker_nodes_seen_max"] == 2
        assert r["oversized"] is True
        assert r["job_list_cost_usd"] == 1000.0
        assert r["price_basis"] == "priced"
        assert r["est_saving_usd_list"] == 500.0
        assert r["suggested_action"] == "fewer workers / lower autoscale max"
        assert r["not_assessed_reason"] is None
        assert r["status"] == "CRITICAL"


# =====================================================================================================
# ovj_warn: 3 runs, fixed cluster (4 of 4 workers), worker CPU p90 25% / mem p90 35% -- under the
# full thresholds (30 / 40) but NOT under half -- and $400 of job compute -> est_saving_usd_list =
# 400 x 0.25 = 100 -> WARN ([25, 200)); workers seen (4) = configured (4) -> 'one node size down'.
# =====================================================================================================
def test_warn_not_under_half_threshold():
    for w in WINDOWS:
        r = _by_job(w)["ovj_warn"]
        assert r["runs_with_telemetry"] == 3
        assert r["worker_cpu_p90_pct"] == 25.0
        assert r["worker_mem_p90_pct"] == 35.0
        assert r["worker_mem_peak_pct"] == 35.0
        assert r["workers_configured_max"] == 4
        assert r["worker_nodes_seen_max"] == 4
        assert r["oversized"] is True
        assert r["job_list_cost_usd"] == 400.0
        assert r["est_saving_usd_list"] == 100.0
        assert r["suggested_action"] == "one node size down"
        assert r["status"] == "WARN"


# =====================================================================================================
# ovj_ok: worker CPU p90 60% / mem p90 60% -- above both thresholds -> oversized FALSE ->
# est_saving_usd_list 0 -> OK, regardless of its own $300 of job compute.
# =====================================================================================================
def test_ok_not_oversized():
    for w in WINDOWS:
        r = _by_job(w)["ovj_ok"]
        assert r["oversized"] is False
        assert r["job_list_cost_usd"] == 300.0
        assert r["est_saving_usd_list"] == 0.0
        assert r["suggested_action"] is None
        assert r["status"] == "OK"


# =====================================================================================================
# ovj_na: 1 run only (< :min_runs default 3), with real telemetry and a real cluster ->
# NOT_ASSESSED / too_few_runs; est_saving_usd_list is NULL (unknown), never a fabricated 0.
# =====================================================================================================
def test_not_assessed_too_few_runs():
    for w in WINDOWS:
        r = _by_job(w)["ovj_na"]
        assert r["runs_with_telemetry"] == 1
        assert r["oversized"] is None
        assert r["est_saving_usd_list"] is None
        assert r["suggested_action"] is None
        assert r["not_assessed_reason"] == "too_few_runs"
        assert r["status"] == "NOT_ASSESSED"


# =====================================================================================================
# ovj_spike / ovj_swap: CPU p90 10% and memory p90 15% -- oversized on p90 alone -- but one worker
# minute per run peaks at 70% memory (>= 40) or swaps 2%: a smaller node would not hold that
# minute -> oversized FALSE -> est_saving_usd_list 0 -> OK.
# =====================================================================================================
def test_memory_peak_blocks_oversized():
    for w in WINDOWS:
        r = _by_job(w)["ovj_spike"]
        assert r["worker_mem_p90_pct"] == 15.0
        assert r["worker_mem_peak_pct"] == 70.0
        assert r["oversized"] is False
        assert r["est_saving_usd_list"] == 0.0
        assert r["suggested_action"] is None
        assert r["status"] == "OK"


def test_swap_blocks_oversized():
    for w in WINDOWS:
        r = _by_job(w)["ovj_swap"]
        assert r["worker_mem_peak_pct"] == 15.0
        assert r["worker_swap_peak_pct"] == 2.0
        assert r["oversized"] is False
        assert r["est_saving_usd_list"] == 0.0
        assert r["status"] == "OK"
