"""tests/test_findings/test_lakeflow_job_reliability.py -- P3-JOBHEALTH.

Proves, against tests/fixtures/lakeflow.py's SEC P rows (`lf_job_rel_*` / `lf_run_rel_*`, DEC-15),
that findings.f_lakeflow_job_reliability (grain [workspace_id, job_id]) has: the right grain,
worst-first ordering, the status enum, a reachable OK/WARN/CRITICAL/NOT_ASSESSED row each with
exact computed values, the CRITICAL-before-NOT_ASSESSED status precedence (a live failure streak
overrides "too few runs"), a repaired/retried run counted ONCE on its final attempt (never as two
runs, never double-counting a failure), window behaviour at 7 vs 30 days, and the AS_OF-day
exclusion (period_end_time < date_trunc('DAY', now), the same bound
tests/test_findings/test_lakeflow_run_timeline.py already proves for the sibling C2 ids). Per
tests/test_findings/README.md's checklist.

Every expectation below is computed by hand from tests/fixtures/lakeflow.py's own SEC P comment
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
import lakeflow as lf  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "lakeflow_job_reliability.yml"
PARQUET_DIR = ROOT / "tests" / "fixtures" / "parquet"
JRT_GLOB = (PARQUET_DIR / "lakeflow__job_run_timeline" / "*.parquet").as_posix()

WINDOWS = (7, 30, 90)
STATUS_VALUES = {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}
QID = "lakeflow_job_reliability"


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
    return {r["job_id"]: r for r in dbutil.rows(QID, window_days) if r["job_id"].startswith("lf_job_rel_")}


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
# lf_job_rel_crit_streak: SUCCEEDED, SUCCEEDED, FAILED, FAILED, FAILED (oldest -> newest).
# runs=5, failed_runs=3 (60%), consecutive_failures=3 (>= crit 3) -> CRITICAL (also above the warn
# rate, proving the CRITICAL branch wins).
# =====================================================================================================
def test_crit_streak():
    for w in (30, 90):
        r = _by_job(w)["lf_job_rel_crit_streak"]
        assert r["runs"] == 5
        assert r["failed_runs"] == 3
        assert r["failure_rate_pct"] == 60.0
        assert r["consecutive_failures"] == 3
        assert r["status"] == "CRITICAL"
        assert r["not_assessed_reason"] is None
        assert r["latest_result_state"] == "FAILED"
        assert r["last_n_runs_considered"] == 5
        assert r["last_n_failed"] == 3
        assert r["last_n_summary"] == "3 of last 5 runs failed"


# =====================================================================================================
# lf_job_rel_warn_rate: FAILED, SUCCEEDED, FAILED, SUCCEEDED, SUCCEEDED. failed_runs=2 (40% >= warn
# 20%), consecutive_failures=0 (latest run SUCCEEDED) -> WARN, not CRITICAL.
# =====================================================================================================
def test_warn_rate():
    for w in (30, 90):
        r = _by_job(w)["lf_job_rel_warn_rate"]
        assert r["runs"] == 5
        assert r["failed_runs"] == 2
        assert r["failure_rate_pct"] == 40.0
        assert r["consecutive_failures"] == 0
        assert r["status"] == "WARN"
        assert r["latest_result_state"] == "SUCCEEDED"


def test_ok():
    for w in (30, 90):
        r = _by_job(w)["lf_job_rel_ok"]
        assert r["runs"] == 5
        assert r["failed_runs"] == 0
        assert r["failure_rate_pct"] == 0.0
        assert r["consecutive_failures"] == 0
        assert r["status"] == "OK"


# =====================================================================================================
# lf_job_rel_scarce: SUCCEEDED, FAILED (2 runs, below min_runs=5); consecutive_failures=1 (< crit 3)
# -> NOT_ASSESSED, not_assessed_reason='too_few_runs'.
# =====================================================================================================
def test_scarce_not_assessed():
    for w in (30, 90):
        r = _by_job(w)["lf_job_rel_scarce"]
        assert r["runs"] == 2
        assert r["failed_runs"] == 1
        assert r["consecutive_failures"] == 1
        assert r["status"] == "NOT_ASSESSED"
        assert r["not_assessed_reason"] == "too_few_runs"


# =====================================================================================================
# lf_job_rel_scarce_crit: 3 FAILED runs, below min_runs=5, but 3 consecutive failures (>= crit 3) ->
# CRITICAL. Proves the precedence rule: a live failure streak is read BEFORE the too-few-runs check.
# =====================================================================================================
def test_scarce_crit_precedence():
    for w in (30, 90):
        r = _by_job(w)["lf_job_rel_scarce_crit"]
        assert r["runs"] == 3
        assert r["failed_runs"] == 3
        assert r["consecutive_failures"] == 3
        assert r["status"] == "CRITICAL"
        assert r["not_assessed_reason"] is None


# =====================================================================================================
# lf_job_rel_repair_success: 4 plain SUCCEEDED runs + 1 run_id with two end-row attempts (FAILED
# then, later, SUCCEEDED). runs=5 (the repaired run counts ONCE, on its final attempt), failed_runs=0,
# runs_with_retry=1 -> OK. A buggy implementation that counted every attempt row separately would
# read runs=6, failed_runs=1.
# =====================================================================================================
def test_repair_success_counted_once():
    for w in (30, 90):
        r = _by_job(w)["lf_job_rel_repair_success"]
        assert r["runs"] == 5
        assert r["failed_runs"] == 0
        assert r["runs_with_retry"] == 1
        assert r["consecutive_failures"] == 0
        assert r["status"] == "OK"
        assert r["latest_result_state"] == "SUCCEEDED"


# =====================================================================================================
# lf_job_rel_repair_fail: 4 plain SUCCEEDED runs + 1 run_id with two attempts (FAILED, FAILED, final
# result FAILED). runs=5, failed_runs=1 (NOT 2 -- the repeated attempt never double-counts), rate=20%
# (>= warn 20) -> WARN.
# =====================================================================================================
def test_repair_fail_counted_once():
    for w in (30, 90):
        r = _by_job(w)["lf_job_rel_repair_fail"]
        assert r["runs"] == 5
        assert r["failed_runs"] == 1
        assert r["failure_rate_pct"] == 20.0
        assert r["runs_with_retry"] == 1
        assert r["consecutive_failures"] == 1
        assert r["status"] == "WARN"
        assert r["latest_result_state"] == "FAILED"


# =====================================================================================================
# lf_job_rel_window: 3 FAILED runs at D(25) (inside 30/90-day windows, outside the 7-day one) + 5
# SUCCEEDED runs at D(3) (inside all three). window=7 sees only the 5 SUCCEEDED runs -> OK.
# window=30/90 sees all 8 -> WARN (failure rate 37.5%, latest run SUCCEEDED so no CRITICAL streak).
# =====================================================================================================
def test_window_boundary_changes_status():
    r7 = _by_job(7)["lf_job_rel_window"]
    assert r7["runs"] == 5
    assert r7["failed_runs"] == 0
    assert r7["status"] == "OK"

    for w in (30, 90):
        r = _by_job(w)["lf_job_rel_window"]
        assert r["runs"] == 8
        assert r["failed_runs"] == 3
        assert r["failure_rate_pct"] == 37.5
        assert r["consecutive_failures"] == 0
        assert r["status"] == "WARN"


# =====================================================================================================
# lf_job_rel_manyruns: 2 FAILED runs at D(6) (oldest) + 10 SUCCEEDED runs at D(2) (most recent).
# runs=12, failed_runs=2 (16.7% < warn 20) -> OK. last_n_runs_considered=min(10,12)=10 and
# last_n_failed=0, because the 2 failures sit OUTSIDE the most recent 10 runs.
# =====================================================================================================
def test_last_n_differs_from_whole_window():
    for w in (30, 90):
        r = _by_job(w)["lf_job_rel_manyruns"]
        assert r["runs"] == 12
        assert r["failed_runs"] == 2
        assert r["failure_rate_pct"] == 16.7
        assert r["status"] == "OK"
        assert r["last_n_runs_considered"] == 10
        assert r["last_n_failed"] == 0
        assert r["last_n_summary"] == "0 of last 10 runs failed"


# =====================================================================================================
# lf_job_rel_streak_skip: SUCCEEDED, FAILED, FAILED, CANCELLED, FAILED (oldest -> newest). Review
# fix: the CANCELLED run must be stepped over, not treated as breaking the streak -- the streak
# only stops at the job's most recent SUCCEEDED run. consecutive_failures=3 (the two older FAILED
# runs plus the latest FAILED run; the CANCELLED run in between counts toward neither failed_runs
# nor the streak) -> CRITICAL. A buggy "stop at the first non-failed run" implementation would
# read consecutive_failures=1 (only the single latest FAILED run) and never reach CRITICAL.
# =====================================================================================================
def test_streak_steps_over_cancelled_run():
    for w in (30, 90):
        r = _by_job(w)["lf_job_rel_streak_skip"]
        assert r["runs"] == 5
        assert r["failed_runs"] == 3
        assert r["consecutive_failures"] == 3
        assert r["status"] == "CRITICAL"
        assert r["not_assessed_reason"] is None
        assert r["latest_result_state"] == "FAILED"


# =====================================================================================================
# AS_OF-day exclusion: lf_job_rel_asof's one run has period_end_time on the AS_OF day -> the job
# never appears in the model at any window, even though the raw row genuinely exists.
# =====================================================================================================
def test_asof_day_excluded():
    assert _raw_row_exists("job_id = 'lf_job_rel_asof'")
    for w in WINDOWS:
        assert "lf_job_rel_asof" not in _by_job(w)


# =====================================================================================================
# T1: lf_job_rel_ok has 5 SUCCEEDED runs, each exactly 300s (run_dur=0 folds to NULL, so every run
# is measured as its own 5-minute wall clock, start to start+5m) -> successful_runs=5,
# p95_success_minutes=5.0, max_success_minutes=5.0, suggested_timeout_minutes=CEIL(2*5)=10.
# lf_job_rel_crit_streak has only 2 SUCCEEDED runs (< 5) -> suggested_timeout_minutes is NULL, never
# invented from too few runs.
# =====================================================================================================
def test_suggested_timeout_minutes():
    for w in (30, 90):
        by_job = _by_job(w)
        ok = by_job["lf_job_rel_ok"]
        assert ok["successful_runs"] == 5
        assert ok["p95_success_minutes"] == 5.0
        assert ok["max_success_minutes"] == 5.0
        assert ok["suggested_timeout_minutes"] == 10

        streak = by_job["lf_job_rel_crit_streak"]
        assert streak["successful_runs"] == 2
        assert streak["suggested_timeout_minutes"] is None


# =====================================================================================================
# Review fix (cross-domain, run over the WHOLE job_run_timeline table): run_start (and therefore
# latest_run_start) is now the run's TRUE first observed slice across every row of the run_id, not
# the end row's own period_start_time alone. Proven on lakeflow_job_duration_regression's own
# lf_job_dur_slice fixture (SEC Q), whose most recent run is sliced hourly over ~5h: the end row's
# own slice starts at 05:00, but the run's true start is 01:00.
# =====================================================================================================
def test_run_start_is_true_first_slice_not_last_slice():
    for w in (30, 90):
        r = _by_job_id(w, "lf_job_dur_slice")
        assert r["runs"] == 10
        assert r["failed_runs"] == 0
        assert r["latest_run_start"] == lf.DT(2, 1)


def _by_job_id(window_days: int, job_id: str) -> dict:
    return next(r for r in dbutil.rows(QID, window_days) if r["job_id"] == job_id)
