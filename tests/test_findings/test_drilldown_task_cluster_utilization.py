"""tests/test_findings/test_drilldown_task_cluster_utilization.py

Ported from the earlier subaudit suite's tests/
test_task_cluster_utilization.py's main()/check() calls (41 assertions), reading the dbt-built
findings.f_task_cluster_utilization table through tests/dbutil.py.

The last two ported checks (the subaudit's own :period_days 120 vs 30 "LEAST(:period_days, 90)
cap" comparison) have no direct analogue here: the generated model only ever materializes the
three fixed windows 7/30/90 (dbt_project.yml `vars.windows`), never an ungenerated window like
120. This file adapts them to compare window_days = 90 against window_days = 30 instead --
between those two windows this fixture adds no new qualifying run (the next run after the ones
within 30 days is dd_tcu_R_old, 100 days before AS_OF, still excluded at 90), so the two checks'
actual assertions (dd_tcu_T_old stays excluded; the row set does not change) still hold and still
exercise the same LEAST(window_days, 90) cap in the model.

See tests/fixtures/drilldown.py's module docstring for why this file filters the workspace-scoped
rows down to this scenario's own dd_tcu_ ids before applying the original assertions (DEC-15).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dbutil

WORKSPACE = "9001"


def _tcu_rows(window_days: int) -> list[dict]:
    raw = dbutil.rows("task_cluster_utilization", window_days, workspace_ids=[WORKSPACE])
    return [r for r in raw if r["task_run_id"] and r["task_run_id"].startswith("dd_tcu_")]


def test_task_cluster_utilization():
    out = _tcu_rows(30)

    by_task = {r["task_run_id"]: r for r in out}
    assert len(out) == len(by_task), "fan-out: more than one row per task run"
    assert (
        "dd_tcu_T_r2" not in by_task and "dd_tcu_T_other" not in by_task and "dd_tcu_T_old" not in by_task
    ), "short runs / out-of-window runs excluded"
    assert len([r for r in out if r["job_run_id"] == "dd_tcu_R1"]) == 11, (
        f"R1 must have 11 task rows, got {[r['task_key'] for r in out]}"
    )

    hints = {k: (by_task[k]["bottleneck_hint"], by_task[k]["status"]) for k in by_task}
    assert hints["dd_tcu_T_wait"] == ("WAITING_OR_IDLE", "WARN"), hints["dd_tcu_T_wait"]
    assert hints["dd_tcu_T_busy"] == ("COMPUTE_BOUND", "OK"), hints["dd_tcu_T_busy"]
    assert hints["dd_tcu_T_skew"] == ("SKEWED", "WARN"), hints["dd_tcu_T_skew"]
    assert hints["dd_tcu_T_mem"] == ("MEMORY_PRESSURE", "CRITICAL"), hints["dd_tcu_T_mem"]
    assert hints["dd_tcu_T_io"] == ("IO_WAIT", "WARN"), hints["dd_tcu_T_io"]
    assert hints["dd_tcu_T_drv"] == ("DRIVER_BOUND", "WARN"), hints["dd_tcu_T_drv"]
    assert hints["dd_tcu_T_single"] == ("COMPUTE_BOUND", "OK"), hints["dd_tcu_T_single"]
    assert hints["dd_tcu_T_live"] == ("MIXED", "OK"), hints["dd_tcu_T_live"]
    assert (
        hints["dd_tcu_T_short"] == (None, "NOT_ASSESSED")
        and by_task["dd_tcu_T_short"]["not_assessed_reason"] == "too_few_slices"
    ), by_task["dd_tcu_T_short"]
    assert (
        hints["dd_tcu_T_sls"] == (None, "NOT_ASSESSED")
        and by_task["dd_tcu_T_sls"]["not_assessed_reason"] == "no_cluster_recorded"
    ), by_task["dd_tcu_T_sls"]
    assert (
        hints["dd_tcu_T_wh"] == (None, "NOT_ASSESSED")
        and by_task["dd_tcu_T_wh"]["not_assessed_reason"] == "no_node_timeline_rows"
    ), by_task["dd_tcu_T_wh"]
    assert (
        hints["dd_tcu_T_skip"] == (None, "NOT_ASSESSED")
        and by_task["dd_tcu_T_skip"]["not_assessed_reason"] == "task_not_executed"
    ), by_task["dd_tcu_T_skip"]
    assert by_task["dd_tcu_T_skip"]["task_state"] == "SKIPPED" and by_task["dd_tcu_T_skip"]["task_hours"] == 0, (
        "skipped task shape"
    )
    assert all(r["not_assessed_reason"] is None for r in out if r["status"] != "NOT_ASSESSED"), (
        "assessed rows carry no reason"
    )

    b = by_task["dd_tcu_T_busy"]
    assert b["node_minutes"] == 360 and b["worker_minutes"] == 240, (
        f"telemetry outside the task window leaked: {b['node_minutes']}/{b['worker_minutes']}"
    )
    assert b["nodes_seen"] == 3 and b["worker_nodes_seen"] == 2, "node counts"
    assert abs(b["worker_cpu_avg_pct"] - 90) < 0.1 and abs(b["worker_cpu_p50_pct"] - 90) < 0.1, (
        f"busy cpu {b['worker_cpu_avg_pct']}"
    )
    assert b["overlapping_task_runs"] == 1, f"shared cluster must show 1 overlapping task run, got {b['overlapping_task_runs']}"
    assert b["worker_count_configured"] == 4 and b["cluster_name"] == "busy-new", "SCD2 clusters: latest row"
    assert b["cluster_url"] == "https://dbc-123.cloud.databricks.com/compute/clusters/dd_tcu_c-busy", b["cluster_url"]
    assert b["spark_ui_url"] == "https://dbc-123.cloud.databricks.com/compute/clusters/dd_tcu_c-busy/sparkUi", (
        b["spark_ui_url"]
    )
    assert b["run_url"] == "https://dbc-123.cloud.databricks.com/jobs/dd_tcu_J1/runs/dd_tcu_R1", b["run_url"]
    assert b["workspace_name"] == "dd-prod" and b["job_name"] == "etl_daily_v2", "names resolved"
    assert abs(b["run_hours"] - 5.0) < 0.01 and abs(b["task_hours"] - 2.0) < 0.01 and b["in_flight"] is False, "hours"
    assert b["compute_ids_count"] == 1 and b["single_node"] is False, "compute_ids_count / single_node"

    assert by_task["dd_tcu_T_wait"]["overlapping_task_runs"] == 0, "no sharing on c-wait in the window"
    assert abs(by_task["dd_tcu_T_skew"]["worker_cpu_spread_pct"] - 85) < 0.1, (
        f"spread {by_task['dd_tcu_T_skew']['worker_cpu_spread_pct']}"
    )
    sg = by_task["dd_tcu_T_single"]
    assert sg["single_node"] is True and sg["worker_minutes"] == 0 and abs(sg["driver_cpu_avg_pct"] - 90) < 0.1, (
        "single-node judged on driver"
    )
    assert sg["worker_cpu_avg_pct"] is None, "no worker numbers on a single-node cluster"
    assert abs(by_task["dd_tcu_T_mem"]["worker_mem_p90_pct"] - 92) < 0.1, "mem p90"
    assert abs(by_task["dd_tcu_T_io"]["worker_cpu_wait_avg_pct"] - 35) < 0.1, "cpu wait"
    assert abs(by_task["dd_tcu_T_drv"]["driver_cpu_avg_pct"] - 95) < 0.1, "driver cpu"
    lv = by_task["dd_tcu_T_live"]
    assert (
        lv["in_flight"] is True
        and lv["task_state"] == "RUNNING"
        and lv["job_name"] is None
        and 2.9 < lv["run_hours"] < 3.1
    ), f"in-flight row {lv['run_hours']}"
    assert lv["node_minutes"] >= 3 * 170, f"in-flight telemetry to now: {lv['node_minutes']}"

    order = [r["status"] for r in out]
    rank = {"CRITICAL": 0, "WARN": 1, "NOT_ASSESSED": 2, "OK": 3}
    assert order == sorted(order, key=rank.get) and order[0] == "CRITICAL", f"order {order}"
    assert set(order) <= set(rank), "status enum"

    # LEAST(window_days, 90) cap, adapted to the generated windows (7/30/90 -- see module
    # docstring): dd_tcu_R_old (100 days before AS_OF) stays excluded, and no run between 30 and
    # 90 days old exists in this fixture, so the row set is identical at window_days = 90.
    out90 = _tcu_rows(90)
    assert "dd_tcu_T_old" not in {r["task_run_id"] for r in out90}, "LEAST(window_days, 90) cap"
    assert len(out90) == len(out), "same rows at window_days = 90 as at 30"
