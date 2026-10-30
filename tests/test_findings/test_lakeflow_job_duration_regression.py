"""tests/test_findings/test_lakeflow_job_duration_regression.py -- P3-JOBHEALTH.

Proves, against tests/fixtures/lakeflow.py's SEC Q rows (`lf_job_dur_*` / `lf_run_dur_*`, DEC-15),
that findings.f_lakeflow_job_duration_regression (grain [workspace_id, job_id]) has: the right
grain, worst-first ordering, the status enum, a reachable OK/WARN/CRITICAL/NOT_ASSESSED row each
with exact computed values, a job that got FASTER never flagged, a repaired/retried run counted
ONCE on its final attempt's own wall clock (never double-counted, never measured across the gap
before a repair), the honest NOT_ASSESSED degrade when :recent_days >= the chosen window (DEC-62 --
comparing two measured periods, never one projected from the other), and the AS_OF-day exclusion.
Per tests/test_findings/README.md's checklist.

Every expectation below is computed by hand from tests/fixtures/lakeflow.py's own SEC Q comment
block and cross-checked against it -- never read back from the model under test.
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR / "fixtures"))
import dbutil  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "lakeflow_job_duration_regression.yml"
PARQUET_DIR = ROOT / "tests" / "fixtures" / "parquet"
JRT_GLOB = (PARQUET_DIR / "lakeflow__job_run_timeline" / "*.parquet").as_posix()

WINDOWS = (7, 30, 90)
STATUS_VALUES = {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}
QID = "lakeflow_job_duration_regression"

SCENARIO_JOBS = [
    "lf_job_dur_crit", "lf_job_dur_warn", "lf_job_dur_ok", "lf_job_dur_scarce_baseline",
    "lf_job_dur_scarce_recent", "lf_job_dur_scarce_both", "lf_job_dur_repair",
]


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _raw_row_exists(where: str) -> bool:
    con = duckdb.connect()
    try:
        sql = f"SELECT COUNT(*) FROM read_parquet('{JRT_GLOB}', union_by_name=true) WHERE {where}"
        return con.execute(sql).fetchone()[0] > 0
    finally:
        con.close()


def _by_job(window_days: int) -> dict:
    return {r["job_id"]: r for r in dbutil.rows(QID, window_days) if r["job_id"].startswith("lf_job_dur_")}


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


def test_status_enum_and_not_assessed_reason():
    for w in WINDOWS:
        for r in dbutil.rows(QID, w):
            assert r["status"] in STATUS_VALUES, (w, r["job_id"], r["status"])
            if r["status"] == "NOT_ASSESSED":
                assert r["not_assessed_reason"], (w, r["job_id"], "NOT_ASSESSED must carry a reason")
            else:
                assert r["not_assessed_reason"] is None, (w, r["job_id"], r["not_assessed_reason"])


def test_worst_first_ordering():
    rank = {"CRITICAL": 0, "WARN": 1, "NOT_ASSESSED": 2, "OK": 3}
    for w in WINDOWS:
        out = dbutil.rows(QID, w)
        order = [rank[r["status"]] for r in out]
        assert order == sorted(order), (w, "status must be worst-first")


# =====================================================================================================
# lf_job_dur_crit: baseline 5x600s (D(20)), recent 5x1500s (D(2)) -> ratio 2.5 (>= crit 2.0) ->
# CRITICAL.
# =====================================================================================================
def test_crit():
    for w in (30, 90):
        r = _by_job(w)["lf_job_dur_crit"]
        assert r["baseline_runs"] == 5
        assert r["baseline_runs_repaired"] == 0
        assert r["recent_runs"] == 5
        assert r["recent_runs_repaired"] == 0
        assert r["baseline_median_minutes"] == 10.0
        assert r["recent_median_minutes"] == 25.0
        assert r["slowdown_ratio"] == 2.5
        assert r["status"] == "CRITICAL"
        assert r["not_assessed_reason"] is None


# =====================================================================================================
# lf_job_dur_warn: baseline 5x600s, recent 5x960s -> ratio 1.6 (>= warn 1.5, < crit 2.0) -> WARN.
# =====================================================================================================
def test_warn():
    for w in (30, 90):
        r = _by_job(w)["lf_job_dur_warn"]
        assert r["baseline_median_minutes"] == 10.0
        assert r["recent_median_minutes"] == 16.0
        assert r["slowdown_ratio"] == 1.6
        assert r["status"] == "WARN"


# =====================================================================================================
# lf_job_dur_ok: baseline 5x600s, recent 5x480s (FASTER) -> ratio 0.8 -> OK. A job that got faster
# is never flagged.
# =====================================================================================================
def test_ok_faster_never_flagged():
    for w in (30, 90):
        r = _by_job(w)["lf_job_dur_ok"]
        assert r["baseline_median_minutes"] == 10.0
        assert r["recent_median_minutes"] == 8.0
        assert r["slowdown_ratio"] == 0.8
        assert r["status"] == "OK"


# =====================================================================================================
# NOT_ASSESSED reasons: scarce_baseline (baseline 2 < 5), scarce_recent (recent 2 < 5), scarce_both
# (both 1 < 5) -- three distinct, named reasons, never a blank.
# =====================================================================================================
def test_not_assessed_reasons():
    for w in (30, 90):
        by_job = _by_job(w)

        sb = by_job["lf_job_dur_scarce_baseline"]
        assert sb["baseline_runs"] == 2
        assert sb["recent_runs"] == 5
        assert sb["status"] == "NOT_ASSESSED"
        assert sb["not_assessed_reason"] == "too_few_baseline_runs"

        sr = by_job["lf_job_dur_scarce_recent"]
        assert sr["baseline_runs"] == 5
        assert sr["recent_runs"] == 2
        assert sr["status"] == "NOT_ASSESSED"
        assert sr["not_assessed_reason"] == "too_few_recent_runs"

        sboth = by_job["lf_job_dur_scarce_both"]
        assert sboth["baseline_runs"] == 1
        assert sboth["recent_runs"] == 1
        assert sboth["status"] == "NOT_ASSESSED"
        assert sboth["not_assessed_reason"] == "too_few_runs_both_sides"


# =====================================================================================================
# lf_job_dur_repair: 5 plain SUCCEEDED baseline runs @600s + one run_id with two SUCCEEDED end-row
# attempts (an early, abandoned one at 9999s, then a later, final one at exactly 600s).
# baseline_runs=6 (NOT 7 -- the repeated attempt counts once, on its final row only),
# baseline_runs_repaired=1, baseline_median_minutes=10.0 -- proves the median used the final
# attempt's own 600s, never the abandoned first attempt's 9999s, and never double-counted the run.
# recent 5x1500s -> ratio 2.5 -> CRITICAL.
# =====================================================================================================
def test_repair_counted_once_on_final_attempt():
    for w in (30, 90):
        r = _by_job(w)["lf_job_dur_repair"]
        assert r["baseline_runs"] == 6
        assert r["baseline_runs_repaired"] == 1
        assert r["baseline_median_minutes"] == 10.0
        assert r["recent_runs"] == 5
        assert r["recent_runs_repaired"] == 0
        assert r["slowdown_ratio"] == 2.5
        assert r["status"] == "CRITICAL"


# =====================================================================================================
# lf_job_dur_slice: baseline 5 runs of 2h each, recent 5 runs of 5h each, EVERY run sliced hourly
# (run_dur=0 throughout, so the wall-clock fallback is what is measured). Review fix: a measured
# attempt is pooled across its own hourly slices, not read off its last slice alone.
# baseline_median_minutes=120.0 (the full 2h, not the last slice's 60.0), recent_median_minutes=
# 300.0 (the full 5h, not 60.0) -> ratio 2.5 -> CRITICAL. The pre-fix query would have measured
# both as their own last <=1h slice (60.0 and 60.0) -> ratio 1.0 -> OK, hiding a real 2h -> 5h
# slowdown entirely.
# =====================================================================================================
def test_multihour_attempt_pooled_across_its_own_slices():
    for w in (30, 90):
        r = _by_job(w)["lf_job_dur_slice"]
        assert r["baseline_runs"] == 5
        assert r["baseline_runs_repaired"] == 0
        assert r["recent_runs"] == 5
        assert r["recent_runs_repaired"] == 0
        assert r["baseline_median_minutes"] == 120.0
        assert r["recent_median_minutes"] == 300.0
        assert r["slowdown_ratio"] == 2.5
        assert r["status"] == "CRITICAL"
        assert r["not_assessed_reason"] is None


# =====================================================================================================
# Review fix (DEC-62): :recent_days is now capped at HALF of :period_days (LEAST(recent_days,
# FLOOR(period_days/2))), so at window=7 the split becomes a real 3-recent-day / 4-baseline-day
# sub-slice, never empty by construction. The SCENARIO_JOBS above still read NOT_ASSESSED at
# window=7 -- but now because their baseline runs genuinely sit at D(20), outside a 7-day window
# entirely, never because the split itself was structurally empty. lf_job_dur_win7's runs sit
# INSIDE the 7-day window on both sides (recent D(1..3), baseline D(4..6)) and read a real
# CRITICAL verdict, proving the fix: a job's own 7-day history is no longer swallowed.
# =====================================================================================================
def test_recent_days_at_window_reads_a_real_baseline():
    by_job = _by_job(7)
    for job_id in SCENARIO_JOBS:
        r = by_job[job_id]
        assert r["baseline_runs"] == 0, (job_id, r["baseline_runs"])
        assert r["status"] == "NOT_ASSESSED", (job_id, r["status"])
        assert r["not_assessed_reason"] in ("too_few_baseline_runs", "too_few_runs_both_sides"), (
            job_id, r["not_assessed_reason"]
        )

    # lf_job_dur_crit has 5 recent runs (inside the 7-day recent slice) -> only baseline is short.
    assert by_job["lf_job_dur_crit"]["not_assessed_reason"] == "too_few_baseline_runs"
    # lf_job_dur_scarce_both has only 1 recent run too -> both sides are short.
    assert by_job["lf_job_dur_scarce_both"]["not_assessed_reason"] == "too_few_runs_both_sides"

    win7 = by_job["lf_job_dur_win7"]
    assert win7["baseline_runs"] == 5
    assert win7["baseline_runs_repaired"] == 0
    assert win7["recent_runs"] == 5
    assert win7["recent_runs_repaired"] == 0
    assert win7["baseline_median_minutes"] == 10.0
    assert win7["recent_median_minutes"] == 25.0
    assert win7["slowdown_ratio"] == 2.5
    assert win7["status"] == "CRITICAL"
    assert win7["not_assessed_reason"] is None


# =====================================================================================================
# AS_OF-day exclusion: lf_job_dur_asof's one run has period_end_time on the AS_OF day -> the job
# never appears in the model at any window, even though the raw row genuinely exists.
# =====================================================================================================
def test_asof_day_excluded():
    assert _raw_row_exists("job_id = 'lf_job_dur_asof'")
    for w in WINDOWS:
        assert "lf_job_dur_asof" not in _by_job(w)


# =====================================================================================================
# T12: b5_job_slower (tests/fixtures/cases_jobs.py, workspace 7705) has 5 baseline runs @ queue=30s
# setup=60s execution=1000s and 5 recent runs @ queue=300s setup=600s execution=1000s -- queue+setup
# grew 10x (90s -> 900s) while execution stayed flat (1000s -> 1000s), so grew_most reads
# 'queue_or_setup', never 'run_time'.
# =====================================================================================================
def test_grew_most_queue_or_setup():
    for w in (30, 90):
        out = {r["job_id"]: r for r in dbutil.rows(QID, w) if r["job_id"] == "b5_job_slower"}
        r = out["b5_job_slower"]
        assert r["baseline_queue_s"] == 30.0
        assert r["recent_queue_s"] == 300.0
        assert r["baseline_setup_s"] == 60.0
        assert r["recent_setup_s"] == 600.0
        assert r["baseline_execution_s"] == 1000.0
        assert r["recent_execution_s"] == 1000.0
        assert r["grew_most"] == "queue_or_setup"
        assert r["status"] == "CRITICAL"  # duration_s itself (run_duration_seconds) is 1200 -> 6000, ratio 5.0
