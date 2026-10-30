"""tools/direct_jobs_health.py

Jobs health: failed, flaky and slow jobs, cold and failed cluster starts, timeouts and ownership,
job size, and what each pipeline costs. Reads system.* directly; tools/build_direct_dashboards.py
writes it with the other direct dashboards.
"""
from __future__ import annotations

from tools.build_direct_dashboards import (
    END, FAILED_JOBS, START, WS, Dataset, _label, _page, bar, counter, dataset_json, global_filters, header, place,
    table,
)
from tools import build_databricks_dashboards as bdd

TITLE = "Jobs health"
P = "jobs"

# The thresholds, in one place (the guide quotes them).
FLAKY_MIN_RUNS = 5      # a job needs this many runs before it can be called flaky
FLAKY_FAILED_PCT = 20   # flaky: at least this share of its runs failed, but not all
SLOW_FACTOR = 1.5       # a slow run: this many times the job's usual (median) run time...
SLOW_EXTRA_MIN = 5      # ...and at least this many minutes longer
USUAL_MIN_RUNS = 5      # successful runs needed to know a job's usual run time

# One row per job run in the window: its last attempt's result, its run time, and whether it was slow.
_RUNS = f"""slices AS (
  SELECT CAST(workspace_id AS STRING) AS workspace_id, CAST(job_id AS STRING) AS job_id, run_id,
         period_start_time, period_end_time, result_state, termination_code, run_duration_seconds
  FROM system.lakeflow.job_run_timeline
  WHERE period_start_time >= {START} AND period_start_time < {END}
),
ends AS (
  -- a repaired run writes one end row per attempt: its result is the last one's
  SELECT workspace_id, job_id, run_id, period_end_time AS run_end, result_state, termination_code,
         NULLIF(run_duration_seconds, 0) AS reported_s
  FROM slices
  WHERE result_state IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id, run_id ORDER BY period_end_time DESC) = 1
),
starts AS (
  SELECT workspace_id, job_id, run_id, MIN(period_start_time) AS run_start
  FROM slices
  GROUP BY workspace_id, job_id, run_id
),
runs AS (
  SELECT e.workspace_id, e.job_id, e.run_id, e.run_end, e.result_state, e.termination_code,
         CAST(s.run_start AS DATE) AS run_date,
         COALESCE(e.reported_s, timestampdiff(SECOND, s.run_start, e.run_end)) / 60.0 AS minutes,
         CASE WHEN e.result_state IN ('FAILED', 'ERROR', 'TIMED_OUT') THEN 1 ELSE 0 END AS failed
  FROM ends e
  JOIN starts s ON s.workspace_id = e.workspace_id AND s.job_id = e.job_id AND s.run_id = e.run_id
),
usual AS (
  SELECT workspace_id, job_id, percentile(minutes, 0.5) AS usual_minutes
  FROM runs
  WHERE result_state = 'SUCCEEDED'
  GROUP BY workspace_id, job_id
  HAVING COUNT(*) >= {USUAL_MIN_RUNS}
),
judged AS (
  SELECT r.*, u.usual_minutes,
         CASE WHEN r.result_state = 'SUCCEEDED' AND r.minutes > {SLOW_FACTOR} * u.usual_minutes
                   AND r.minutes - u.usual_minutes >= {SLOW_EXTRA_MIN} THEN 1 ELSE 0 END AS slow
  FROM runs r
  LEFT JOIN usual u ON u.workspace_id = r.workspace_id AND u.job_id = r.job_id
)"""
_RUNS_STEPS = {
    "slices": "Job run rows that started in the window; a long run has one row per hour.",
    "ends": "Each run's end row; a repaired run keeps its last attempt's result and error.",
    "starts": "Each run's first row: when it started.",
    "runs": "One row per run: day, minutes it took, and 1 when it failed (failed, error or timed out).",
    "usual": f"Each job's usual run time: the median of its successful runs, once it has {USUAL_MIN_RUNS} or more.",
    "judged": f"A slow run: succeeded, but took over {SLOW_FACTOR}x the job's usual time and {SLOW_EXTRA_MIN}+ "
              "minutes longer.",
}
_NAMED = f"""named AS (
  SELECT j.*, {WS.format(t='j')} AS workspace, COALESCE(jb.name, j.job_id) AS job_name
  FROM {{src}} j
  LEFT JOIN ws ON ws.workspace_id = j.workspace_id
  LEFT JOIN jb ON jb.workspace_id = j.workspace_id AND jb.job_id = j.job_id
)"""
_RELIABILITY = (f"CASE WHEN failed_runs = 0 THEN 'No failures' "
                f"WHEN failed_runs = runs AND runs >= 3 THEN 'Always fails' "
                f"WHEN runs >= {FLAKY_MIN_RUNS} AND 100.0 * failed_runs / runs >= {FLAKY_FAILED_PCT} THEN 'Flaky' "
                f"ELSE 'Some failures' END")


def _per_job(where: str) -> str:
    return f"""{_RUNS},
per_job AS (
  SELECT workspace_id, job_id, COUNT(*) AS runs, SUM(failed) AS failed_runs, SUM(slow) AS slow_runs,
         COUNT(DISTINCT CASE WHEN failed = 1 THEN run_date END) AS days_failed,
         COUNT(DISTINCT CASE WHEN slow = 1 THEN run_date END) AS days_slow,
         percentile(minutes, 0.5) AS median_minutes, AVG(minutes) AS avg_minutes, MAX(minutes) AS max_minutes,
         SUM(minutes) / 60.0 AS total_hours,
         -- the error of its latest failed run
         MAX_BY(termination_code, CASE WHEN failed = 1 THEN unix_timestamp(run_end) ELSE 0 END) AS last_code
  FROM judged
  GROUP BY workspace_id, job_id
),
{_NAMED.format(src='per_job')},
ranked AS (
  SELECT *, {_RELIABILITY} AS reliability,
         ROW_NUMBER() OVER (ORDER BY failed_runs DESC, failed_runs / runs DESC, workspace, job_name) AS fail_rank,
         ROW_NUMBER() OVER (ORDER BY total_hours DESC, workspace, job_name) AS time_rank
  FROM named
)
SELECT concat(job_name, ' (', job_id, ')') AS job, workspace, reliability, 1 AS jobs,
       runs, failed_runs, ROUND(100.0 * failed_runs / runs, 1) AS failure_pct, days_failed, days_slow, slow_runs,
       ROUND(median_minutes, 1) AS median_minutes, ROUND(avg_minutes, 1) AS avg_minutes,
       ROUND(max_minutes, 1) AS max_minutes, ROUND(total_hours, 1) AS total_hours,
       CASE WHEN failed_runs > 0 THEN last_code END AS last_failure,
       CASE WHEN failed_runs > 0 THEN 1 ELSE 0 END AS failing,
       CASE WHEN reliability = 'Flaky' THEN 1 ELSE 0 END AS flaky,
       CASE WHEN reliability = 'Always fails' THEN 1 ELSE 0 END AS always_fails,
       CASE WHEN time_rank <= 10 THEN {_label('job_name', 'workspace')} ELSE 'All others' END AS top_by_time
FROM ranked
{where}"""


_PER_JOB_STEPS = {
    **_RUNS_STEPS,
    "per_job": "Per job: runs, failed and slow runs, days with a failure or a slow run, median, average and "
               "longest run, total hours, and the latest failure's error.",
    "named": "Workspace and job names added.",
    "ranked": f"Reliability: Always fails (every run, 3+), Flaky ({FLAKY_MIN_RUNS}+ runs, {FLAKY_FAILED_PCT}%+ "
              "failed), Some failures, No failures; ranked by failed runs and by total hours.",
}

JOB_DAYS = Dataset(
    "job_days", "Job runs per day: runs, failed and slow, per job",
    [], ["ws", "jb"],
    f"""{_RUNS},
{_NAMED.format(src='judged')},
ranked AS (
  SELECT *, DENSE_RANK() OVER (ORDER BY job_failed DESC, workspace_id, job_id) AS rk
  FROM (SELECT *, SUM(failed) OVER (PARTITION BY workspace_id, job_id) AS job_failed FROM named)
),
labelled AS (
  SELECT run_date, workspace, concat(job_name, ' (', job_id, ')') AS job,
         CASE WHEN job_failed > 0 AND rk <= 10 THEN {_label('job_name', 'workspace')} ELSE 'All others' END AS top_failing_job,
         failed, slow
  FROM ranked
)
SELECT run_date, workspace, job, top_failing_job,
       COUNT(*) AS runs, SUM(failed) AS failed_runs, SUM(slow) AS slow_runs
FROM labelled
GROUP BY run_date, workspace, job, top_failing_job
ORDER BY run_date, workspace, job""",
    "Every job's runs per day: how many, how many failed, how many were slow.",
    "The charts over time on Failed jobs and Failed job details.",
    "One row per job and day; the 10 jobs with the most failed runs keep their name in top_failing_job.",
    {**_RUNS_STEPS, "named": "Workspace and job names added.",
     "ranked": "Jobs ranked by their failed runs in the window.",
     "labelled": "The 10 with the most failed runs keep their name; the rest read All others."},
)

JOBS = Dataset(
    "jobs", "Jobs: runs, failures, run time and reliability, one row per job",
    [], ["ws", "jb"], _per_job("ORDER BY CASE reliability WHEN 'Always fails' THEN 0 WHEN 'Flaky' THEN 1 "
                               "WHEN 'Some failures' THEN 2 ELSE 3 END, failure_pct DESC, total_hours DESC"),
    "Per job that ran: runs, failures, failure %, days with failures or slow runs, run times, and its "
    "reliability.",
    "The tiles and reliability table on Failed jobs, and the run-time chart and table on Slow jobs.",
    "One row per job, always failing and flaky first.",
    _PER_JOB_STEPS,
)

TOP_FAILED = Dataset(
    "top_failed_jobs", "Top 20 failed jobs",
    [], ["ws", "jb"], _per_job("WHERE failed_runs > 0 AND fail_rank <= 20\nORDER BY fail_rank"),
    "The 20 jobs with the most failed runs, with their run counts, failure %, median run time and the days "
    "they failed or ran slow.",
    "The table on Failed job details.",
    "Up to 20 rows, most failed runs first.",
    _PER_JOB_STEPS,
)

SLOWER = Dataset(
    "slower_than_usual", "Jobs slower than usual: the last days against the rest of the window",
    [("reg", "lakeflow_job_duration_regression")], ["ws"],
    f"""r AS (
  SELECT r.*, {WS.format(t='r')} AS workspace, COALESCE(r.job_name, CAST(r.job_id AS STRING)) AS name
  FROM reg r LEFT JOIN ws ON ws.workspace_id = CAST(r.workspace_id AS STRING)
)
SELECT concat(name, ' (', job_id, ')') AS job, workspace, status, recent_runs, baseline_runs,
       recent_median_minutes, baseline_median_minutes, slowdown_ratio,
       CASE grew_most WHEN 'queue_or_setup' THEN 'Waiting or cluster start' WHEN 'run_time' THEN 'Run time' END
         AS what_grew,
       CASE WHEN status IN ('CRITICAL', 'WARN') THEN 1 ELSE 0 END AS flagged
FROM r
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         slowdown_ratio DESC NULLS LAST""",
    "Per job: the median run time of its last days (up to 7) against the rest of the window; 1.5x is Warn, "
    "2x Critical.",
    "The slower-than-usual tile and table on Slow jobs.",
    "One row per job, the biggest slowdown first.",
    {"r": "Workspace name added."},
)

COLD = Dataset(
    "cold_starts", "Cold starts: time spent starting clusters, per job",
    [("cold", "lakeflow_phase_cold_start")], ["ws", "jb"],
    f"""c AS (
  SELECT c.*, {WS.format(t='c')} AS workspace, COALESCE(jb.name, CAST(c.job_id AS STRING)) AS job_name,
         DENSE_RANK() OVER (ORDER BY c.setup_s_p95 DESC NULLS LAST, c.workspace_id, c.job_id) AS rk
  FROM cold c
  LEFT JOIN ws ON ws.workspace_id = CAST(c.workspace_id AS STRING)
  LEFT JOIN jb ON jb.workspace_id = CAST(c.workspace_id AS STRING) AND jb.job_id = CAST(c.job_id AS STRING)
)
SELECT concat(job_name, ' (', job_id, ')') AS job, workspace, status, runs,
       ROUND(setup_s_p95, 0) AS setup_p95_s, ROUND(setup_s_total / 3600.0, 1) AS setup_hours,
       ROUND(queue_s_p95, 0) AS queue_p95_s,
       CASE WHEN rk <= 10 AND setup_s_p95 > 0 THEN {_label('job_name', 'workspace')} END AS top_job,
       CASE WHEN status IN ('CRITICAL', 'WARN') THEN 1 ELSE 0 END AS flagged
FROM c
ORDER BY setup_s_p95 DESC NULLS LAST""",
    "Per job: how long its clusters took to start (p95 and in total) and how long runs waited in the queue; "
    "p95 of 60 s is Warn, 300 s Critical.",
    "The cold-start tile, chart and table on Slow jobs.",
    "One row per job, slowest start first; the 10 slowest keep their name for the chart.",
    {"c": "Workspace and job names added; ranked by p95 start time for the top 10."},
)

FAILED_STARTS = Dataset(
    "failed_cluster_starts", "Failed cluster starts: runs that failed before any work ran",
    [("starts", "lakeflow_failed_cluster_starts")], ["ws", "jb"],
    f"""s AS (
  SELECT s.*, {WS.format(t='s')} AS workspace, COALESCE(jb.name, CAST(s.job_id AS STRING)) AS job_name
  FROM starts s
  LEFT JOIN ws ON ws.workspace_id = CAST(s.workspace_id AS STRING)
  LEFT JOIN jb ON jb.workspace_id = CAST(s.workspace_id AS STRING) AND jb.job_id = CAST(s.job_id AS STRING)
)
SELECT concat(job_name, ' (', job_id, ')') AS job, workspace, termination_code AS error_code, failed_starts,
       ROUND(setup_s_total / 60.0, 1) AS setup_minutes, CAST(last_failed_at AS STRING) AS last_failed
FROM s
ORDER BY failed_starts DESC""",
    "Task runs that failed during setup with no run time: the cluster never started. Per job and error code.",
    "The failed-starts tile, chart and table on Slow jobs.",
    "One row per job and error code, most failures first.",
    {"s": "Workspace and job names added."},
)

NO_TIMEOUT = Dataset(
    "jobs_no_timeout", "Jobs with no timeout",
    [("nt", "lakeflow_jobs_no_timeout")], [],
    """n AS (
  SELECT * FROM nt WHERE no_timeout
)
SELECT concat(COALESCE(job_name, CAST(job_id AS STRING)), ' (', job_id, ')') AS job,
       COALESCE(workspace_name, CAST(workspace_id AS STRING)) AS workspace, status,
       CASE WHEN has_duration_warning THEN 'yes' ELSE 'no' END AS duration_warning,
       est_usd_list AS spend_usd, CASE WHEN status IN ('CRITICAL', 'WARN') THEN 1 ELSE 0 END AS flagged
FROM n
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, spend_usd DESC""",
    "Jobs with no timeout set: a hung run keeps paying until someone stops it. $200+ spend is Critical.",
    "The timeout tile and table on Hygiene.",
    "One row per job with no timeout, most $ first.",
    {"n": "Only jobs with no timeout."},
)

TASK_NO_TIMEOUT = Dataset(
    "tasks_no_timeout", "Tasks with no timeout (and none on their job)",
    [("nt", "lakeflow_job_tasks_no_timeout")], [],
    """n AS (
  SELECT * FROM nt WHERE no_timeout AND NOT parent_gone
)
SELECT concat(COALESCE(job_name, CAST(job_id AS STRING)), ' (', job_id, ')') AS job, task_key AS task,
       COALESCE(workspace_name, CAST(workspace_id AS STRING)) AS workspace, status,
       est_usd_list AS job_spend_usd, CASE WHEN status IN ('CRITICAL', 'WARN') THEN 1 ELSE 0 END AS flagged
FROM n
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, job_spend_usd DESC""",
    "Tasks with no timeout of their own; the job's $ decides Critical ($200+).",
    "The task-timeout tile and table on Hygiene.",
    "One row per task, most job $ first.",
    {"n": "Only tasks with no timeout whose job still exists."},
)

OWNERSHIP = Dataset(
    "ownership_risk", "Jobs with an ownership risk",
    [("own", "lakeflow_job_ownership_orphans")], [],
    """o AS (
  SELECT * FROM own WHERE status IN ('CRITICAL', 'WARN')
)
SELECT concat(COALESCE(job_name, CAST(job_id AS STRING)), ' (', job_id, ')') AS job,
       COALESCE(workspace_name, CAST(workspace_id AS STRING)) AS workspace, status,
       CASE WHEN principal_missing THEN 'Owner or run-as missing'
            WHEN run_as_kind = 'HUMAN' AND is_scheduled THEN 'Scheduled, runs as a person'
            ELSE 'Runs as a person' END AS risk,
       COALESCE(run_as_user_name, '-') AS runs_as, COALESCE(creator_user_name, '-') AS created_by,
       CASE WHEN run_as_kind = 'HUMAN' THEN 1 ELSE 0 END AS runs_as_person, 1 AS flagged
FROM o
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 ELSE 1 END, workspace, job""",
    "Jobs that run as a person instead of a service principal (Critical when scheduled), or whose owner or "
    "run-as is missing.",
    "The ownership tile and table on Hygiene.",
    "One row per risky job, scheduled ones first.",
    {"o": "Only Critical or Warn jobs."},
    head="Current job settings; no dates.", window=False,
)

OVERSIZED = Dataset(
    "oversized_jobs", "Oversized jobs: compute that sat mostly idle",
    [("ov", "lakeflow_job_oversized")], ["ws"],
    f"""o AS (
  SELECT o.*, {WS.format(t='o')} AS workspace, COALESCE(o.job_name, CAST(o.job_id AS STRING)) AS name
  FROM ov o LEFT JOIN ws ON ws.workspace_id = CAST(o.workspace_id AS STRING)
  WHERE o.oversized
)
SELECT concat(name, ' (', job_id, ')') AS job, workspace, status, worker_node_type AS node_type,
       workers_configured_max AS max_workers, worker_nodes_seen_max AS workers_used,
       worker_cpu_p90_pct AS cpu_p90_pct, worker_mem_peak_pct AS memory_peak_pct,
       job_list_cost_usd AS spend_usd, est_saving_usd_list AS saving_usd, suggested_action,
       CASE WHEN status IN ('CRITICAL', 'WARN') THEN 1 ELSE 0 END AS flagged
FROM o
ORDER BY saving_usd DESC NULLS LAST""",
    "Jobs whose workers stayed under 30% CPU (p90) and 40% memory (peak): one size down or fewer workers, "
    "and what that would save.",
    "The oversized tiles and table on Job size.",
    "One row per oversized job, biggest saving first.",
    {"o": "Workspace name added; only oversized jobs."},
)

PRESSURE = Dataset(
    "compute_pressure", "Compute pressure: what limits each job",
    [("cp", "lakeflow_job_compute_pressure")], ["ws"],
    f"""p AS (
  SELECT p.*, {WS.format(t='p')} AS workspace, COALESCE(p.job_name, CAST(p.job_id AS STRING)) AS name
  FROM cp p LEFT JOIN ws ON ws.workspace_id = CAST(p.workspace_id AS STRING)
)
SELECT concat(name, ' (', job_id, ')') AS job, workspace, status, 1 AS jobs,
       CASE pressure WHEN 'MEMORY' THEN 'Memory: too little' WHEN 'CPU' THEN 'CPU' WHEN 'SKEW' THEN 'Skew'
                     WHEN 'DRIVER' THEN 'Driver' WHEN 'IO_WAIT' THEN 'I/O wait' WHEN 'IDLE' THEN 'Idle: too big'
                     WHEN 'NONE' THEN 'Balanced' ELSE 'Not judged' END AS pressure,
       CASE scaling_hint WHEN 'SCALE_UP_MEMORY' THEN 'More memory per node' WHEN 'SCALE_OUT' THEN 'More workers'
                         WHEN 'FIX_SKEW' THEN 'Fix data skew' WHEN 'DISTRIBUTE_WORK' THEN 'Move work off the driver'
                         WHEN 'FIX_IO' THEN 'Fix I/O' WHEN 'SCALE_DOWN' THEN 'Smaller cluster' END AS hint,
       pressure_reason AS why, task_hours_total AS task_hours, worker_node_type AS node_type,
       worker_mem_p90_pct AS memory_p90_pct, worker_cpu_p90_pct AS cpu_p90_pct,
       CASE WHEN pressure = 'MEMORY' THEN 1 ELSE 0 END AS memory_bound,
       CASE WHEN status IN ('CRITICAL', 'WARN') THEN 1 ELSE 0 END AS flagged
FROM p
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         task_hours DESC""",
    "Per job: what its compute ran short of (memory, CPU, I/O, the driver), was uneven (skew) or too big for "
    "(idle), with the change to try.",
    "The pressure tiles, chart and table on Job size.",
    "One row per job, memory-bound first, then by task hours.",
    {"p": "Workspace name added."},
)

PIPE_COST = Dataset(
    "pipeline_cost", "Pipelines: spend per pipeline",
    [("pc", "lakeflow_pipeline_cost")], ["ws"],
    f"""p AS (
  SELECT p.*, {WS.format(t='p')} AS workspace, COALESCE(p.pipeline_name, CAST(p.pipeline_id AS STRING)) AS name,
         ROW_NUMBER() OVER (ORDER BY p.net_list_cost DESC NULLS LAST, p.pipeline_id) AS rn
  FROM pc p LEFT JOIN ws ON ws.workspace_id = CAST(p.workspace_id AS STRING)
)
SELECT concat(name, ' (', pipeline_id, ')') AS pipeline, workspace, status, 1 AS pipelines,
       CASE WHEN is_continuous THEN 'Continuous' ELSE 'Triggered' END AS mode,
       CASE WHEN rn <= 10 THEN {_label('name', 'workspace')} ELSE 'All others' END AS top_pipeline,
       ROUND(net_list_cost, 2) AS spend_usd, ROUND(net_pipeline_dbus, 1) AS dbus, updates,
       updates_failed_or_canceled AS failed_updates,
       CASE WHEN nothing_delivered THEN 'yes' ELSE 'no' END AS nothing_delivered
FROM p
ORDER BY spend_usd DESC NULLS LAST""",
    "Per pipeline: list-price spend, DBUs, updates and failed updates in the window.",
    "The Pipelines page.",
    "One row per pipeline, most $ first; the 10 costliest keep their name for the chart.",
    {"p": "Workspace name added; ranked by $ for the top 10."},
)

DATASETS = [JOB_DAYS, JOBS, TOP_FAILED, FAILED_JOBS, SLOWER, COLD, FAILED_STARTS, NO_TIMEOUT, TASK_NO_TIMEOUT,
            OWNERSHIP, OVERSIZED, PRESSURE, PIPE_COST]


def _temporal(key: str, d: Dataset, cat: str, col: str, val_title: str, title: str, desc: str, color: str) -> dict:
    w = bdd.bar_widget(key, d.name, cat, col, title, category_label="Day", value_label=val_title, color_col=color,
                       temporal=True, width=1, height=1, x=0, y=0)
    w["widget"]["spec"]["frame"].update({"description": desc, "showDescription": True})
    return w


def dashboard() -> dict:
    D, J, T, F, S, C, X = JOB_DAYS, JOBS, TOP_FAILED, FAILED_JOBS, SLOWER, COLD, FAILED_STARTS
    failed = place([
        (2, [header("jh:fail:head", "Failed jobs",
                    f"Failed runs over time and by workspace, and which jobs fail often. Flaky: {FLAKY_MIN_RUNS}+ "
                    f"runs and {FLAKY_FAILED_PCT}%+ of them failed. Time Window and Workspace in Global Filters.")]),
        (3, [counter("jh:fail:runs", D, "failed_runs", "Failed runs", "Failed, error or timed out"),
             counter("jh:fail:jobs", J, "failing", "Jobs with failures", "At least one failed run"),
             counter("jh:fail:flaky", J, "flaky", "Flaky jobs", f"{FLAKY_FAILED_PCT}%+ of runs failed, not all"),
             counter("jh:fail:usd", F, "wasted_usd", "Cost of failed runs", "List price", True)]),
        (7, [_temporal("jh:fail:by_day", D, "run_date", "failed_runs", "Failed runs", "Failed runs per day",
                       "By workspace", "workspace")]),
        (7, [bar("jh:fail:by_ws", D, "workspace", "Workspace", "failed_runs", "Failed runs",
                 "Failed runs by workspace", "In the Time Window", money=False),
             bar("jh:fail:by_rel", J, "reliability", "Reliability", "jobs", "Jobs", "Jobs by reliability",
                 "Always fails, flaky, some failures, none", money=False)]),
        (10, [table("jh:fail:table", J, [("job", "Job", "text"), ("workspace", "Workspace", "text"),
                                         ("reliability", "Reliability", "text"), ("runs", "Runs", "int"),
                                         ("failed_runs", "Failed", "int"), ("failure_pct", "Failed %", "pct"),
                                         ("last_failure", "Last error", "text")],
                    "Jobs by reliability: always failing and flaky first")]),
    ])
    details = place([
        (2, [header("jh:det:head", "Failed job details",
                    "The 20 jobs with the most failed runs: how often they run, fail and run slow, and on which days.")]),
        (8, [_temporal("jh:det:by_day", D, "run_date", "failed_runs", "Failed runs",
                       "Failed runs per day, top 10 failing jobs", "The rest in All others", "top_failing_job")]),
        (10, [table("jh:det:table", T, [("workspace", "Workspace", "text"), ("job", "Job", "text"),
                                        ("runs", "Runs", "int"), ("failed_runs", "Failed", "int"),
                                        ("failure_pct", "Failed %", "pct"), ("median_minutes", "Median, min", "num"),
                                        ("days_failed", "Days with failures", "int"),
                                        ("days_slow", "Days with slow runs", "int"),
                                        ("last_failure", "Last error", "text")],
                    f"Top 20 failed jobs (a slow run: {SLOW_FACTOR}x its usual time and {SLOW_EXTRA_MIN}+ min longer)")]),
    ])
    slow = place([
        (2, [header("jh:slow:head", "Slow jobs",
                    "Jobs running slower than usual, cluster start times, and runs whose cluster never started.")]),
        (3, [counter("jh:slow:slower", S, "flagged", "Slower than usual", "Last days' median 1.5x+ the window's"),
             counter("jh:slow:runs", J, "slow_runs", "Slow runs", f"{SLOW_FACTOR}x usual, {SLOW_EXTRA_MIN}+ min longer"),
             counter("jh:slow:cold", C, "flagged", "Slow cold starts", "Jobs with p95 start 60 s+"),
             counter("jh:slow:failed", X, "failed_starts", "Failed cluster starts", "Task runs")]),
        (8, [bar("jh:slow:longest", J, "top_by_time", "Job", "max_minutes", "Longest run, min",
                 "Longest run, top 10 jobs by total run time", "Minutes", money=False, agg="MAX"),
             bar("jh:slow:avg", J, "top_by_time", "Job", "avg_minutes", "Average run, min",
                 "Average run, top 10 jobs by total run time", "Minutes", money=False, agg="MAX")]),
        (10, [table("jh:slow:runtime", J, [("job", "Job", "text"), ("workspace", "Workspace", "text"),
                                           ("runs", "Runs", "int"), ("avg_minutes", "Average, min", "num"),
                                           ("max_minutes", "Longest, min", "num"), ("total_hours", "Total, h", "num"),
                                           ("failed_runs", "Failed", "int")],
                    "Jobs by run time")]),
        (9, [table("jh:slow:slower_table", S, [("job", "Job", "text"), ("workspace", "Workspace", "text"),
                                               ("recent_median_minutes", "Recent median, min", "num"),
                                               ("baseline_median_minutes", "Before, min", "num"),
                                               ("slowdown_ratio", "Slower by", "num"),
                                               ("what_grew", "What grew", "text"), ("status", "Status", "text")],
                   "Jobs slower than usual, biggest slowdown first")]),
        (7, [bar("jh:slow:p95", C, "top_job", "Job", "setup_p95_s", "Start p95, s", "Cluster start p95, top 10 jobs",
                 "Seconds before the first task could run", money=False, agg="MAX"),
             bar("jh:slow:codes", X, "error_code", "Error code", "failed_starts", "Failed starts",
                 "Failed cluster starts by error code", "Task runs", money=False)]),
        (8, [table("jh:slow:starts_table", X, [("job", "Job", "text"), ("workspace", "Workspace", "text"),
                                               ("error_code", "Error code", "text"),
                                               ("failed_starts", "Failed starts", "int"),
                                               ("last_failed", "Last", "text")],
                   "Failed cluster starts, most first")]),
    ])
    N, K, O = NO_TIMEOUT, TASK_NO_TIMEOUT, OWNERSHIP
    hygiene = place([
        (2, [header("jh:hyg:head", "Hygiene",
                    "Jobs and tasks with no timeout, and jobs that run as a person or have lost their owner.")]),
        (3, [counter("jh:hyg:jobs", N, "flagged", "Jobs with no timeout", "Warn; Critical at $200+"),
             counter("jh:hyg:tasks", K, "flagged", "Tasks with no timeout", "Nor on their job"),
             counter("jh:hyg:own", O, "flagged", "Ownership risk", "Person or missing owner"),
             counter("jh:hyg:person", O, "runs_as_person", "Run as a person", "Not a service principal")]),
        (9, [table("jh:hyg:jobs_table", N, [("job", "Job", "text"), ("workspace", "Workspace", "text"),
                                           ("duration_warning", "Duration warning", "text"),
                                           ("spend_usd", "$", "money"), ("status", "Status", "text")],
                   "Jobs with no timeout, most $ first")]),
        (9, [table("jh:hyg:tasks_table", K, [("job", "Job", "text"), ("task", "Task", "text"),
                                             ("workspace", "Workspace", "text"), ("job_spend_usd", "Job $", "money"),
                                             ("status", "Status", "text")],
                   "Tasks with no timeout, most job $ first")]),
        (9, [table("jh:hyg:own_table", O, [("job", "Job", "text"), ("workspace", "Workspace", "text"),
                                           ("risk", "Risk", "text"), ("runs_as", "Runs as", "text"),
                                           ("created_by", "Created by", "text"), ("status", "Status", "text")],
                   "Jobs with an ownership risk, scheduled ones first")]),
    ])
    V, R = OVERSIZED, PRESSURE
    size = place([
        (2, [header("jh:size:head", "Job size",
                    "Jobs on more compute than they use, and jobs short of memory or held back by CPU, I/O, skew "
                    "or the driver.")]),
        (3, [counter("jh:size:over", V, "flagged", "Oversized jobs", "Saving $25+"),
             counter("jh:size:save", V, "saving_usd", "Possible saving", "One size down or fewer workers", True),
             counter("jh:size:mem", R, "memory_bound", "Short of memory", "Memory p90 85%+ or swapping"),
             counter("jh:size:press", R, "flagged", "Jobs under pressure", "Critical or Warn")]),
        (7, [bar("jh:size:by_pressure", R, "pressure", "Pressure", "jobs", "Jobs", "Jobs by compute pressure",
                 "What limits each job", money=False, color="status"),
             bar("jh:size:hours", R, "pressure", "Pressure", "task_hours", "Task hours",
                 "Task hours by compute pressure", "Where the run time goes", money=False)]),
        (9, [table("jh:size:over_table", V, [("job", "Job", "text"), ("workspace", "Workspace", "text"),
                                             ("node_type", "Node type", "text"), ("max_workers", "Max workers", "int"),
                                             ("workers_used", "Workers used", "int"),
                                             ("cpu_p90_pct", "CPU p90 %", "pct"),
                                             ("memory_peak_pct", "Memory peak %", "pct"),
                                             ("spend_usd", "$", "money"), ("saving_usd", "Saving $", "money"),
                                             ("suggested_action", "Try", "text")],
                   "Oversized jobs, biggest saving first")]),
        (10, [table("jh:size:press_table", R, [("job", "Job", "text"), ("workspace", "Workspace", "text"),
                                               ("pressure", "Pressure", "text"), ("hint", "Try", "text"),
                                               ("why", "Why", "text"), ("task_hours", "Task h", "num"),
                                               ("status", "Status", "text")],
                    "Compute pressure by job, short of memory first")]),
    ])
    PC = PIPE_COST
    pipelines = place([
        (2, [header("jh:pl:head", "Pipelines", "What each pipeline cost in the Time Window, at list price.")]),
        (3, [counter("jh:pl:usd", PC, "spend_usd", "Pipeline spend", "List price", True),
             counter("jh:pl:count", PC, "pipelines", "Pipelines", "With spend"),
             counter("jh:pl:max", PC, "spend_usd", "Costliest pipeline", "One pipeline", True, "MAX"),
             counter("jh:pl:failed", PC, "failed_updates", "Failed updates", "Failed or canceled")]),
        (8, [bar("jh:pl:top", PC, "top_pipeline", "Pipeline", "spend_usd", "$", "Spend by pipeline",
                 "Top 10; the rest in All others", color="mode")]),
        (10, [table("jh:pl:table", PC, [("pipeline", "Pipeline", "text"), ("workspace", "Workspace", "text"),
                                        ("mode", "Mode", "text"), ("spend_usd", "$", "money"), ("dbus", "DBUs", "num"),
                                        ("updates", "Updates", "int"), ("failed_updates", "Failed updates", "int"),
                                        ("nothing_delivered", "Nothing delivered", "text"),
                                        ("status", "Status", "text")],
                    "Pipelines, most $ first")]),
    ])
    return bdd.tag_dashboard_queries({
        "datasets": [dataset_json(d) for d in DATASETS],
        "pages": [_page("failed", "Failed jobs", failed, prefix=P),
                  _page("details", "Failed job details", details, prefix=P),
                  _page("slow", "Slow jobs", slow, prefix=P), _page("hygiene", "Hygiene", hygiene, prefix=P),
                  _page("size", "Job size", size, prefix=P), _page("pipelines", "Pipelines", pipelines, prefix=P),
                  global_filters(DATASETS, P)],
    }, TITLE)

