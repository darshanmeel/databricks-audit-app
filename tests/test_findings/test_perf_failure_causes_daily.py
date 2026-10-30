"""tests/test_findings/test_perf_failure_causes_daily.py.

Proves findings.f_perf_failure_causes_daily against every builder's raw query.history and
lakeflow.job_run_timeline rows: failed statements by error class and failed job runs by
termination code add up to the raw failures, and the grain holds.
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

QID = "perf_failure_causes_daily"


def _raw(table: str, sql: str) -> tuple:
    pattern = (dbutil.PARQUET_DIR / table / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        return con.execute(sql.replace("{src}", f"read_parquet('{pattern}', union_by_name=true)")).fetchone()
    finally:
        con.close()


@pytest.mark.parametrize("window_days", [7, 30, 90])
def test_grain_uniqueness(window_days):
    cols = yaml.safe_load((ROOT / "config" / "grains" / f"{QID}.yml").read_text(encoding="ascii"))[QID]
    out = dbutil.rows(QID, window_days)
    assert out
    keys = [tuple(r[c] for c in cols) for r in out]
    assert len(keys) == len(set(keys)), "duplicate grain rows"


@pytest.mark.parametrize("window_days", [7, 30])
def test_failures_add_up_to_raw_and_carry_a_cause(window_days):
    stmts = _raw("query__history", f"""
        SELECT COUNT(*) FROM {{src}}
        WHERE execution_status = 'FAILED' AND compute.warehouse_id IS NOT NULL
          AND start_time >= DATE '{dbutil.TEST_TODAY}' - INTERVAL {window_days} DAY
          AND start_time < DATE '{dbutil.TEST_TODAY}'
    """)[0]
    runs = _raw("lakeflow__job_run_timeline", f"""
        WITH r AS (
          SELECT * FROM {{src}}
          WHERE period_start_time >= DATE '{dbutil.TEST_TODAY}' - INTERVAL {window_days} DAY
        ), final AS (
          SELECT * FROM r WHERE result_state IS NOT NULL
          QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id, run_id ORDER BY period_end_time DESC) = 1
        )
        SELECT COUNT(*) FROM final
        WHERE result_state IN ('FAILED', 'ERROR', 'TIMED_OUT') AND period_end_time < DATE '{dbutil.TEST_TODAY}'
    """)[0]
    out = dbutil.rows(QID, window_days)
    assert stmts > 0 and runs > 0
    assert sum(r["failures"] for r in out if r["resource_type"] == "warehouse") == stmts
    assert sum(r["failures"] for r in out if r["resource_type"] == "job") == runs
    assert all(r["cause"] for r in out)
    # Only the class is kept, never message text.
    assert all(" " not in r["cause"] for r in out if r["resource_type"] == "warehouse")
