"""tests/fixtures/cases_jobs.py -- B5 Jobs batch's own new fixture rows, for
lakeflow_job_run_changes (T11) and lakeflow_job_duration_regression's grew_most (T12).

Own workspace (7705, never shared with any other builder, DEC-15's own per-builder isolation) and
own id namespace (`b5_`) throughout: jobs, runs, clusters and task-run ids. Self-contained -- it
writes its own lakeflow__job_run_timeline / job_task_run_timeline / jobs / compute__clusters rows
directly (the same DDL shapes tests/fixtures/lakeflow.py's own helpers write) rather than importing
that module's per-builder helpers, so this module has no load-order dependency on another builder.

b5_job_changed: one SUCCEEDED baseline run at D(5) on cluster b5_cluster_old (dbr 13.3.x,
m5.xlarge, 2 fixed workers), one FAILED latest run at D(1) on cluster b5_cluster_new (dbr 14.3.x,
m5.2xlarge, 4 fixed workers) -- the failed latest run makes the job a lakeflow_job_run_changes
candidate on its own (no p90 needed), and the one SUCCEEDED run before it is the baseline.
runtime_version and worker_node_type both read baseline != latest (changed = true); no
system.access.table_lineage or system.storage.table_metrics_history rows exist for this job, so
upstream_tables/input_bytes both read NULL, never a fabricated 0 or empty list. queue_s/setup_s
grow 10x between the two runs (30s -> 300s queue, 60s -> 600s setup) while execution_s stays flat
(1000s -> 1000s), so lakeflow_job_duration_regression's grew_most reads 'queue_or_setup' for this
job once it has enough runs on both sides to be judged at all -- see the module docstring note
below on why that needs a second scenario.

b5_job_slower: the same queue/setup-vs-execution shape, but with 5 baseline SUCCEEDED runs (D(20))
and 5 recent SUCCEEDED runs (D(2)), comfortably above lakeflow_job_duration_regression's own
:min_runs_each_side=3, so grew_most is judged rather than NOT_ASSESSED -- baseline queue+setup medians 30+60=90s,
recent 300+600=900s (10x); baseline/recent execution medians both 1000s (flat) -> grew_most =
'queue_or_setup'.

b5_job_never_succeeded: two FAILED runs (D(3), D(1)), no SUCCEEDED run anywhere in the window --
the latest run (D(1)) makes the job a candidate on its own, but baseline_pick finds nothing, so
lakeflow_job_run_changes must emit the single no_success_in_window marker row (baseline_run_id/
attribute/values all NULL, latest_run_id = b5_run_never_succeeded_2) instead of a comparison.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import duckdb

from base import AS_OF, write_parquet  # noqa: F401 (write_parquet re-exported for build_fixtures.py)

WS = "7705"
ACCOUNT_ID = "b5_acct"
D0 = AS_OF.date()
H = timedelta(hours=1)
M = timedelta(minutes=1)


def D(n: int) -> datetime:
    return datetime.combine(D0 - timedelta(days=n), datetime.min.time()) + timedelta(hours=9)


_JRT_SQL = (
    "INSERT INTO lakeflow__job_run_timeline (account_id, workspace_id, job_id, run_id, "
    "period_start_time, period_end_time, trigger_type, result_state, run_type, run_name, "
    "compute_ids, termination_code, job_parameters, source_task_run_id, root_task_run_id, "
    "compute, termination_type, setup_duration_seconds, queue_duration_seconds, "
    "run_duration_seconds, cleanup_duration_seconds, execution_duration_seconds) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _jrt(con, job_id, run_id, start, end, *, result_state, termination_code=None,
         queue=None, setup=None, run_dur=None, execution=None):
    con.execute(_JRT_SQL, [
        ACCOUNT_ID, WS, job_id, run_id, start, end, "SCHEDULE", result_state, "JOB_RUN", None,
        None, termination_code, {}, None, None, None, None, setup, queue, run_dur, 0, execution,
    ])


_JTRT_SQL = (
    "INSERT INTO lakeflow__job_task_run_timeline (account_id, workspace_id, job_id, run_id, "
    "period_start_time, period_end_time, task_key, compute_ids, result_state, job_run_id, "
    "parent_run_id, termination_code, compute, termination_type, task_parameters, "
    "setup_duration_seconds, cleanup_duration_seconds, execution_duration_seconds) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _jtrt(con, job_id, run_id, start, end, *, cluster_id, result_state):
    con.execute(_JTRT_SQL, [
        ACCOUNT_ID, WS, job_id, run_id, start, end, "main", [cluster_id], result_state, run_id,
        run_id, None, None, None, {}, 0, 0, 0,
    ])


_JOBS_SQL = (
    "INSERT INTO lakeflow__jobs (account_id, workspace_id, job_id, name, creator_id, tags, "
    "run_as, change_time, delete_time, description, trigger, trigger_type, run_as_user_name, "
    "creator_user_name, paused, timeout_seconds, health_rules, deployment, create_time) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _job_row(con, job_id, name, change_time):
    con.execute(_JOBS_SQL, [
        ACCOUNT_ID, WS, job_id, name, None, {}, None, change_time, None, None, None, None, None,
        None, False, None, None, None, change_time - timedelta(days=200),
    ])


_CLUSTER_SQL = (
    "INSERT INTO compute__clusters (account_id, workspace_id, cluster_id, cluster_name, "
    "owned_by, create_time, delete_time, driver_node_type, worker_node_type, worker_count, "
    "min_autoscale_workers, max_autoscale_workers, auto_termination_minutes, "
    "enable_elastic_disk, tags, cluster_source, init_scripts, aws_attributes, azure_attributes, "
    "gcp_attributes, driver_instance_pool_id, worker_instance_pool_id, dbr_version, change_time, "
    "change_date, data_security_mode, policy_id) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _cluster_row(con, cluster_id, worker_node_type, worker_count, dbr_version, change_time):
    con.execute(_CLUSTER_SQL, [
        ACCOUNT_ID, WS, cluster_id, cluster_id, None, change_time - timedelta(days=200), None,
        "m5.xlarge", worker_node_type, worker_count, None, None, 30, True, {}, "JOB", None, None,
        None, None, None, None, dbr_version, change_time, change_time.date(), None, None,
    ])


def _run(con, job_id, run_id, cluster_id, day, *, result_state, termination_code=None,
         queue, setup, run_dur, execution):
    start = D(day)
    end = start + 10 * M
    _jrt(con, job_id, run_id, start, end, result_state=result_state,
         termination_code=termination_code, queue=queue, setup=setup, run_dur=run_dur,
         execution=execution)
    _jtrt(con, job_id, run_id, start, end, cluster_id=cluster_id, result_state=result_state)


def build(con: duckdb.DuckDBPyConnection) -> None:
    _cluster_row(con, "b5_cluster_old", "m5.xlarge", 2, "13.3.x-scala2.12", AS_OF - timedelta(days=200))
    _cluster_row(con, "b5_cluster_new", "m5.2xlarge", 4, "14.3.x-scala2.12", AS_OF - timedelta(days=200))

    # ---- b5_job_changed: one baseline + one failed latest run (a run_changes candidate on its
    # own, no p90 needed) -----------------------------------------------------------------------
    _job_row(con, "b5_job_changed", "b5 job changed", AS_OF - timedelta(days=200))
    _run(con, "b5_job_changed", "b5_run_changed_base", "b5_cluster_old", 5,
         result_state="SUCCEEDED", queue=30, setup=60, run_dur=1200, execution=1000)
    _run(con, "b5_job_changed", "b5_run_changed_latest", "b5_cluster_new", 1,
         result_state="FAILED", termination_code="B5_TASK_ERROR",
         queue=300, setup=600, run_dur=6000, execution=1000)

    # ---- b5_job_slower: 5 baseline + 5 recent SUCCEEDED runs, same queue/setup-vs-execution
    # shape, comfortably above lakeflow_job_duration_regression's :min_runs_each_side=3, so grew_most is
    # judged rather than NOT_ASSESSED -----------------------------------------------------------
    _job_row(con, "b5_job_slower", "b5 job slower", AS_OF - timedelta(days=200))
    for i in range(5):
        _run(con, "b5_job_slower", f"b5_run_slower_base_{i:02d}", "b5_cluster_old", 20 + i,
             result_state="SUCCEEDED", queue=30, setup=60, run_dur=1200, execution=1000)
    for i in range(5):
        _run(con, "b5_job_slower", f"b5_run_slower_recent_{i:02d}", "b5_cluster_new", 2 + i,
             result_state="SUCCEEDED", queue=300, setup=600, run_dur=6000, execution=1000)

    # ---- b5_job_never_succeeded: every run FAILED -- no SUCCEEDED baseline anywhere, so the
    # candidate must read as the no_success_in_window marker row, never as "nothing changed" ------
    _job_row(con, "b5_job_never_succeeded", "b5 job never succeeded", AS_OF - timedelta(days=200))
    _run(con, "b5_job_never_succeeded", "b5_run_never_succeeded_1", "b5_cluster_old", 3,
         result_state="FAILED", termination_code="B5_TASK_ERROR",
         queue=30, setup=60, run_dur=1200, execution=1000)
    _run(con, "b5_job_never_succeeded", "b5_run_never_succeeded_2", "b5_cluster_new", 1,
         result_state="FAILED", termination_code="B5_TASK_ERROR",
         queue=300, setup=600, run_dur=6000, execution=1000)
