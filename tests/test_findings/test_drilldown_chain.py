"""tests/test_findings/test_drilldown_chain.py

The chain test (DEC-14, which supersedes PLAN.md 7.3's one-directional wording): proves the three
drill-down findings compose into one run -> task -> statement drill-down at window_days = 30.

Run-id half (DEC-14): asserts BOTH directions -- step-1 run ids (f_lakeflow_long_running_runs)
== step-2 run ids (f_task_cluster_utilization). PLAN.md 7.3 gives only step-1 subset-of step-2;
the orphan direction step-2 subset-of step-1 is what the page's selection logic (T-32) needs too.
Checked directly against the built fixture (all three drill-down scenarios share the batch's one
workspace and source tables, per tests/fixtures/drilldown.py's module docstring, so both models
see the exact same qualifying long runs): both directions hold on this fixture (12 run ids --
11 dd_ + pressure_jobs.py's pr_R_swap_noise, DEC-65 rule 8 -- exact set equality), so both are
asserted as equalities rather than one-directional subsets.

Task-id half stays as PLAN.md 7.3 states it: every task_run_id appearing in step 2
(f_task_cluster_utilization) is present in the union of step 3's
(f_query_task_statement_breakdown) task_run_id column and its UNATTRIBUTED rows' referenced task
run ids (stmt_task_run_id) -- also checked and holding on this fixture.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dbutil

WINDOW_DAYS = 30
WORKSPACE = "9001"


def test_drilldown_chain():
    step1 = dbutil.rows("lakeflow_long_running_runs", WINDOW_DAYS, workspace_ids=[WORKSPACE])
    step2 = dbutil.rows("task_cluster_utilization", WINDOW_DAYS, workspace_ids=[WORKSPACE])
    step3 = dbutil.rows("query_task_statement_breakdown", WINDOW_DAYS, workspace_ids=[WORKSPACE])

    step1_run_ids = {r["run_id"] for r in step1}
    step2_run_ids = {r["job_run_id"] for r in step2}
    assert step1_run_ids, "fixture produced no long runs at window_days = 30 -- nothing to chain"
    assert step1_run_ids == step2_run_ids, (
        f"DEC-14: step-1 run ids must equal step-2 run ids -- "
        f"only in step 1: {step1_run_ids - step2_run_ids}, only in step 2: {step2_run_ids - step1_run_ids}"
    )

    step2_task_ids = {r["task_run_id"] for r in step2 if r["task_run_id"] is not None}
    step3_task_ids = {r["task_run_id"] for r in step3 if r["task_run_id"] is not None}
    step3_stmt_task_ids = {r["stmt_task_run_id"] for r in step3 if r["stmt_task_run_id"] is not None}
    step3_union = step3_task_ids | step3_stmt_task_ids
    assert step2_task_ids, "fixture produced no task runs at window_days = 30 -- nothing to chain"
    assert step2_task_ids <= step3_union, (
        f"PLAN.md 7.3: every step-2 task_run_id must appear in step-3's task_run_id column or its "
        f"UNATTRIBUTED rows' stmt_task_run_id -- missing: {step2_task_ids - step3_union}"
    )
