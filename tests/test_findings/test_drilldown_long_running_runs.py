"""tests/test_findings/test_drilldown_long_running_runs.py

Ported from the earlier subaudit suite's tests/
test_long_running_runs.py's main()/check() calls (17 assertions), reading the dbt-built
findings.f_lakeflow_long_running_runs table through tests/dbutil.py instead of a hand-translated
DuckDB query string.

The fixture (tests/fixtures/drilldown.py's build_long_running_runs) shares the drill-down batch's
one workspace (9001) and its source tables with the other two drill-down scenarios (dd_tcu_,
dd_qts_ -- see that module's docstring), so f_lakeflow_long_running_runs reports long runs from
all three at once. This file filters the workspace-scoped rows down to this scenario's own
dd_lrr_ ids (DEC-15: "assertions filter on these dd_-prefixed ... ids, never on workspace alone")
before applying the original assertions, which then hold unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dbutil

WINDOW_DAYS = 30
WORKSPACE = "9001"


def test_lakeflow_long_running_runs():
    raw = dbutil.rows("lakeflow_long_running_runs", WINDOW_DAYS, workspace_ids=[WORKSPACE])
    out = [r for r in raw if r["run_id"].startswith("dd_lrr_")]

    by_run = {r["run_id"]: r for r in out}
    assert len(out) == len(by_run), "one row per run"
    assert set(by_run) == {"dd_lrr_R1", "dd_lrr_R3", "dd_lrr_R4", "dd_lrr_R5"}, f"runs reported: {sorted(by_run)}"

    r1 = by_run["dd_lrr_R1"]
    assert abs(r1["run_hours"] - 5.0) < 0.01, (
        f"multi-task run with zeroed durations must fall back to wall clock: {r1['run_hours']}"
    )
    assert r1["run_s_is_lower_bound"] is True and r1["in_flight"] is False and r1["status"] == "WARN", "R1 flags"
    assert r1["wait_share_pct"] is None and r1["setup_s"] is None and r1["queue_s"] is None, (
        f"wait_share must be NULL, not 0, for a multi-task job: {r1['wait_share_pct']}"
    )
    assert (
        r1["top_task_key"] == "transform"
        and abs(r1["top_task_hours"] - 4.0) < 0.01
        and r1["top_task_cluster_id"] == "dd_lrr_c1"
    ), "top task from task execution seconds"
    assert r1["tasks_seen"] == 4 and r1["tasks_in_flight"] == 0, (
        f"tasks_seen counts the skipped zero-length task too: {r1['tasks_seen']}"
    )
    assert r1["top_3_tasks"].startswith("transform=4.0h, ") and "notify" not in r1["top_3_tasks"], (
        f"skipped task never a top task: {r1['top_3_tasks']}"
    )
    assert r1["job_name"] == "etl_v2" and r1["workspace_name"] == "dd-prod", "names"
    assert r1["run_url"] == "https://dbc-123.cloud.databricks.com/jobs/dd_lrr_J1/runs/dd_lrr_R1", r1["run_url"]
    assert r1["top_task_cluster_url"] == "https://dbc-123.cloud.databricks.com/compute/clusters/dd_lrr_c1", (
        r1["top_task_cluster_url"]
    )
    assert (
        r1["finished_runs_in_window"] == 3
        and abs(r1["job_p50_hours"] - 3.0) < 0.01
        and abs(r1["x_vs_job_p50"] - 1.7) < 0.01
    ), f"job normal from finished runs incl. the short one: {r1['finished_runs_in_window']} / {r1['job_p50_hours']}"

    r4 = by_run["dd_lrr_R4"]
    assert abs(r4["run_hours"] - 4.0) < 0.01 and r4["run_s_is_lower_bound"] is False, (
        "legacy single-task run keeps its reported duration"
    )
    assert abs(r4["wait_share_pct"] - 37.5) < 0.01, f"legacy phases give a real wait share: {r4['wait_share_pct']}"

    r5 = by_run["dd_lrr_R5"]
    assert r5["in_flight"] is True and 2.9 < r5["run_hours"] < 3.1 and r5["status"] == "CRITICAL", (
        f"in-flight run past the bound is CRITICAL: {r5['run_hours']}"
    )
    assert (
        r5["top_task_state"] == "RUNNING"
        and r5["tasks_in_flight"] == 1
        and r5["job_name"] is None
        and r5["run_end"] is None
    ), "in-flight task/run shape"

    order = [r["status"] for r in out]
    rank = {"CRITICAL": 0, "WARN": 1, "NOT_ASSESSED": 2, "OK": 3}
    assert order == sorted(order, key=rank.get), f"worst-first: {order}"
