"""tests/test_findings/test_cost_daily_by_resource.py.

Proves findings.f_cost_daily_by_resource against every builder's raw billing.usage rows: each
resource type's DBUs add up to the raw usage it claims, the tail past 100 resources is pooled per
workspace and day without losing usage, and the grain holds.
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

QID = "cost_daily_by_resource"

# The raw usage each resource type reads, as a WHERE fragment over billing.usage.
TYPE_FILTERS = {
    "workspace": "TRUE",
    "warehouse": "usage_metadata.warehouse_id IS NOT NULL",
    "job": "usage_metadata.job_id IS NOT NULL",
    "cluster": "usage_metadata.cluster_id IS NOT NULL AND usage_metadata.job_id IS NULL "
               "AND usage_metadata.dlt_pipeline_id IS NULL",
    "pipeline": "usage_metadata.dlt_pipeline_id IS NOT NULL",
}


def _raw_window(window_days: int) -> str:
    return (f"usage_date >= DATE '{dbutil.TEST_TODAY}' - INTERVAL {window_days} DAY "
            f"AND usage_date < DATE '{dbutil.TEST_TODAY}'")


def _raw_one(sql: str):
    pattern = (dbutil.PARQUET_DIR / "billing__usage" / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        return con.execute(sql.replace("{src}", f"read_parquet('{pattern}', union_by_name=true)")).fetchone()[0]
    finally:
        con.close()


def _raw_dbus(window_days: int, type_filter: str) -> float:
    return dbutil.usage_sum(
        f"upper(usage_unit) = 'DBU' AND ({type_filter}) "
        f"AND usage_date >= DATE '{dbutil.TEST_TODAY}' - INTERVAL {window_days} DAY "
        f"AND usage_date < DATE '{dbutil.TEST_TODAY}'"
    )


@pytest.mark.parametrize("window_days", [7, 30, 90])
def test_grain_uniqueness(window_days):
    cols = yaml.safe_load((ROOT / "config" / "grains" / f"{QID}.yml").read_text(encoding="ascii"))[QID]
    out = dbutil.rows(QID, window_days)
    assert out
    keys = [tuple(r[c] for c in cols) for r in out]
    assert len(keys) == len(set(keys)), "duplicate grain rows"


@pytest.mark.parametrize("window_days", [7, 30, 90])
def test_each_type_adds_up_to_raw_dbus(window_days):
    out = dbutil.rows(QID, window_days)
    for rtype, where in TYPE_FILTERS.items():
        got = sum(r["dbus"] for r in out if r["resource_type"] == rtype)
        raw = _raw_dbus(window_days, where)
        assert raw > 0, rtype
        assert abs(got - raw) < 1e-3, (rtype, got, raw)


def test_tail_past_100_is_pooled_per_workspace_and_day():
    out = dbutil.rows(QID, 30)
    jobs = [r for r in out if r["resource_type"] == "job"]
    kept = {r["resource_key"] for r in jobs if not r["is_other"]}
    pooled = [r for r in jobs if r["is_other"]]
    assert len(kept) == 100 and pooled
    for r in pooled:
        assert r["resource_id"] is None and r["pooled_count"] >= 1
        assert r["resource_key"] == f"{r['workspace_id'] or 'account'}:other"
    assert not any(r["is_other"] for r in out if r["resource_type"] == "workspace")


def test_keys_name_the_workspace_and_resource():
    out = dbutil.rows(QID, 7)
    for r in out:
        if r["resource_type"] == "workspace":
            assert r["resource_key"] == (r["workspace_id"] or "account")
        elif not r["is_other"]:
            assert r["resource_key"] == f"{r['workspace_id'] or 'account'}:{r['resource_id']}"
    assert all(str(r["usage_date"]) < dbutil.TEST_TODAY for r in out)


@pytest.mark.parametrize("window_days", [7, 30])
def test_billed_hours_and_runs_count_distinct_per_resource_day(window_days):
    out = dbutil.rows(QID, window_days)
    hours = _raw_one(f"""
        SELECT SUM(n) FROM (
          SELECT COUNT(DISTINCT usage_start_time) AS n FROM {{src}}
          WHERE usage_metadata.warehouse_id IS NOT NULL AND record_type = 'ORIGINAL' AND {_raw_window(window_days)}
          GROUP BY workspace_id, usage_metadata.warehouse_id, usage_date)""")
    runs = _raw_one(f"""
        SELECT SUM(n) FROM (
          SELECT COUNT(DISTINCT usage_metadata.job_run_id) AS n FROM {{src}}
          WHERE usage_metadata.job_id IS NOT NULL AND {_raw_window(window_days)}
          GROUP BY workspace_id, usage_metadata.job_id, usage_date)""")
    assert hours > 0 and runs > 0
    assert sum(r["billed_hours"] for r in out if r["resource_type"] == "warehouse") == hours
    assert sum(r["billed_runs"] for r in out if r["resource_type"] == "job") == runs
    assert all(r["billed_hours"] is None for r in out if r["resource_type"] in ("workspace", "job", "pipeline"))
    assert all(r["billed_runs"] is None for r in out if r["resource_type"] in ("workspace", "warehouse", "cluster"))
