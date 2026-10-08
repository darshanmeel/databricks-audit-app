"""tests/test_findings/test_perf_daily_by_resource.py.

Proves findings.f_perf_daily_by_resource against every builder's raw query.history and
lakeflow.job_run_timeline rows: statement counts, failures and time per warehouse add up to the
raw statements, each finished job run counts once with its final result, and the grain holds.
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest
import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

QID = "perf_daily_by_resource"


def _raw(table: str, sql: str) -> tuple:
    pattern = (dbutil.PARQUET_DIR / table / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        return con.execute(sql.replace("{src}", f"read_parquet('{pattern}', union_by_name=true)")).fetchone()
    finally:
        con.close()


def _sums(window_days: int, rtype: str) -> tuple:
    out = [r for r in dbutil.rows(QID, window_days) if r["resource_type"] == rtype]
    return sum(r["runs"] for r in out), sum(r["failed"] for r in out), sum(r["total_s"] for r in out)


@pytest.mark.parametrize("window_days", [7, 30, 90])
def test_grain_uniqueness(window_days):
    cols = yaml.safe_load((ROOT / "config" / "grains" / f"{QID}.yml").read_text(encoding="ascii"))[QID]
    out = dbutil.rows(QID, window_days)
    assert out
    keys = [tuple(r[c] for c in cols) for r in out]
    assert len(keys) == len(set(keys)), "duplicate grain rows"


@pytest.mark.parametrize("window_days", [7, 30, 90])
def test_warehouse_statements_add_up_to_raw(window_days):
    raw = _raw("query__history", f"""
        SELECT COUNT(*), SUM(CASE WHEN execution_status = 'FAILED' THEN 1 ELSE 0 END),
               SUM(COALESCE(total_duration_ms, 0)) / 1000.0
        FROM {{src}}
        WHERE compute.warehouse_id IS NOT NULL
          AND start_time >= DATE '{dbutil.TEST_TODAY}' - INTERVAL {window_days} DAY
          AND start_time < DATE '{dbutil.TEST_TODAY}'
    """)
    runs, failed, total_s = _sums(window_days, "warehouse")
    assert raw[0] > 0
    assert (runs, failed) == (raw[0], raw[1])
    assert abs(total_s - raw[2]) < 1


@pytest.mark.parametrize("window_days", [7, 30, 90])
def test_warehouse_waits_shuffle_and_cache_add_up_to_raw(window_days):
    raw = _raw("query__history", f"""
        SELECT SUM(COALESCE(waiting_at_capacity_duration_ms, 0)) / 1000.0,
               SUM(COALESCE(waiting_for_compute_duration_ms, 0)) / 1000.0,
               SUM(COALESCE(shuffle_read_bytes, 0)) / 1e9,
               SUM(COALESCE(read_bytes, 0)) / 1e9,
               SUM(COALESCE(read_bytes, 0) * COALESCE(read_io_cache_percent, 0) / 100.0) / 1e9,
               SUM(CASE WHEN from_result_cache THEN 1 ELSE 0 END)
        FROM {{src}}
        WHERE compute.warehouse_id IS NOT NULL
          AND start_time >= DATE '{dbutil.TEST_TODAY}' - INTERVAL {window_days} DAY
          AND start_time < DATE '{dbutil.TEST_TODAY}'
    """)
    out = dbutil.rows(QID, window_days)
    wh = [r for r in out if r["resource_type"] == "warehouse"]
    cols = ["slot_wait_s", "provision_s", "shuffle_gb", "read_gb", "cache_read_gb", "result_cache_runs"]
    got = [sum(r[c] or 0 for r in wh) for c in cols]
    # Each row is rounded (0.1 s, 1e-6 GB), so the totals may drift by half a unit a row.
    slack = [0.05 * len(wh) + 0.01, 0.05 * len(wh) + 0.01, 1e-6 * len(wh) + 1e-6, 1e-6 * len(wh) + 1e-6, 1e-6 * len(wh) + 1e-6, 0]
    for c, g, r, tol in zip(cols, got, raw, slack):
        assert abs(g - (r or 0)) <= tol, c
    assert got[0] + got[1] > 0 and got[3] > 0
    assert all(r[c] == 0 for r in out if r["resource_type"] != "warehouse" for c in cols)


@pytest.mark.parametrize("window_days", [7, 30, 90])
def test_each_finished_job_run_counts_once_with_its_final_result(window_days):
    raw = _raw("lakeflow__job_run_timeline", f"""
        WITH r AS (
          SELECT workspace_id, job_id, run_id, result_state, period_end_time
          FROM {{src}}
          WHERE period_start_time >= DATE '{dbutil.TEST_TODAY}' - INTERVAL {window_days} DAY
        ), final AS (
          SELECT * FROM r WHERE result_state IS NOT NULL
          QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id, run_id ORDER BY period_end_time DESC) = 1
        )
        SELECT COUNT(*), SUM(CASE WHEN result_state IN ('FAILED', 'ERROR', 'TIMED_OUT') THEN 1 ELSE 0 END)
        FROM final WHERE period_end_time < DATE '{dbutil.TEST_TODAY}'
    """)
    runs, failed, _ = _sums(window_days, "job")
    assert raw[0] > 0
    assert (runs, failed) == (raw[0], raw[1])


@pytest.mark.parametrize("window_days", [7, 30])
def test_task_runs_and_pipeline_updates_count_once_with_their_final_row(window_days):
    tasks = _raw("lakeflow__job_task_run_timeline", f"""
        WITH r AS (
          SELECT * FROM {{src}}
          WHERE period_start_time >= DATE '{dbutil.TEST_TODAY}' - INTERVAL {window_days} DAY
        ), final AS (
          SELECT * FROM r WHERE result_state IS NOT NULL
          QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id, run_id ORDER BY period_end_time DESC) = 1
        )
        SELECT COUNT(*), SUM(COALESCE(setup_duration_seconds, 0)) FROM final
        WHERE period_end_time < DATE '{dbutil.TEST_TODAY}'
    """)
    updates = _raw("lakeflow__pipeline_update_timeline", f"""
        WITH r AS (
          SELECT * FROM {{src}}
          WHERE period_start_time >= DATE '{dbutil.TEST_TODAY}' - INTERVAL {window_days} DAY
        ), final AS (
          SELECT * FROM r WHERE result_state IS NOT NULL
          QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id, update_id ORDER BY period_end_time DESC) = 1
        )
        SELECT COUNT(*), SUM(CASE WHEN result_state = 'FAILED' THEN 1 ELSE 0 END) FROM final
        WHERE period_end_time < DATE '{dbutil.TEST_TODAY}'
    """)
    out = dbutil.rows(QID, window_days)
    jobs = [r for r in out if r["resource_type"] == "job"]
    pipes = [r for r in out if r["resource_type"] == "pipeline"]
    assert tasks[0] > 0 and updates[0] > 0
    assert sum(r["task_runs"] for r in jobs) == tasks[0]
    assert abs(sum(r["setup_s"] for r in jobs) - tasks[1]) < 1
    assert (sum(r["runs"] for r in pipes), sum(r["failed"] for r in pipes)) == (updates[0], updates[1])


def test_tail_past_100_is_pooled_and_keys_name_the_resource():
    out = dbutil.rows(QID, 30)
    jobs = [r for r in out if r["resource_type"] == "job"]
    assert len({r["resource_key"] for r in jobs if not r["is_other"]}) == 100
    assert any(r["is_other"] for r in jobs)
    for r in out:
        suffix = "other" if r["is_other"] else r["resource_id"]
        assert r["resource_key"] == f"{r['workspace_id'] or 'account'}:{suffix}"
        assert r["failed"] <= r["runs"]
        assert str(r["usage_date"]) < dbutil.TEST_TODAY
