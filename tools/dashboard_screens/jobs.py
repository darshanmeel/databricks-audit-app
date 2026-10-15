"""Jobs tab (web/src/tabs/tab_jobs.tsx): Failures, Slow & queued, Hygiene, Compute fit, Pipelines."""
from tools.dashboard_kit import INT, MONEY, NUM, PCT, bar, kpi, table


def build(d):
    failures(d)
    slow(d)
    hygiene(d)
    compute_fit(d)
    pipelines(d)


def failures(d):
    p = d.page("failures")
    p.kpis(
        kpi("Broken now", """
WITH r AS (SELECT * FROM {named:lakeflow_job_reliability} WHERE {ws})
SELECT (SELECT count(*) FROM r WHERE status = 'CRITICAL') AS value,
       COALESCE((SELECT concat('Worst: ', nm_job, ', last ', CAST(consecutive_failures AS STRING), ' runs failed')
                 FROM r WHERE status = 'CRITICAL' ORDER BY consecutive_failures DESC LIMIT 1), 'None right now') AS target
""", target="target", tone="crit",
            how="Last 3 or more runs failed in a row (job reliability check)."),
        kpi("Flaky jobs", """
WITH r AS (SELECT * FROM {named:lakeflow_job_reliability} WHERE {ws})
SELECT (SELECT count(*) FROM r WHERE status = 'WARN') AS value,
       COALESCE((SELECT concat('Worst: ', nm_job, ', ', CAST(failure_rate_pct AS STRING), '% failed')
                 FROM r WHERE status = 'WARN' ORDER BY failure_rate_pct DESC LIMIT 1), 'None') AS target
""", target="target", tone="warn",
            how="20% or more of runs failed, over 5+ runs, and not already Broken (job reliability check)."),
        kpi("Cost of failed runs", """
WITH w AS (SELECT * FROM {named:lakeflow_failed_jobs_wasted_dbus} WHERE status IN ('CRITICAL', 'WARN') AND {ws})
SELECT ROUND(COALESCE((SELECT SUM(est_wasted_usd_list) FROM w), 0) * {disc}, 0) AS value,
       concat(CAST((SELECT count(*) FROM w) AS STRING), ' jobs; worst: ',
              COALESCE((SELECT nm_job FROM w ORDER BY est_wasted_usd_list DESC LIMIT 1), 'none')) AS target
""", target="target", fmt=MONEY, tone="warn",
            how="List-price $ of runs that failed, Critical/Warn jobs only, after discount."),
        kpi("Failure rate (%)", """
WITH r AS (SELECT * FROM {named:lakeflow_job_reliability} WHERE {ws})
SELECT ROUND(100.0 * SUM(failed_runs) / NULLIF(SUM(runs), 0), 1) AS value,
       concat(CAST(SUM(failed_runs) AS STRING), ' of ', CAST(SUM(runs) AS STRING), ' runs failed') AS target
FROM r
""", target="target", fmt=PCT,
            how="Failed runs over all runs, jobs with enough runs to judge (job reliability check)."),
    )
    p.text(["Broken: the last 3 or more runs failed in a row. Flaky: 20% or more of runs failed, over at least 5 runs."],
           height=1)
    p.charts(
        bar("Jobs and pipelines by day", """
SELECT usage_date AS day,
       CASE day_state WHEN 'ok' THEN 'Ran fine' WHEN 'failed' THEN 'Failed' WHEN 'slow' THEN 'Ran slow'
                      WHEN 'skipped' THEN 'Skipped' ELSE day_state END AS state,
       count(*) AS resources
FROM {named:lakeflow_daily_state}
WHERE {ws}
GROUP BY 1, 2
""", "day", "resources", series="state", time=True, fmt=INT, category_title="Day", value_title="Jobs and pipelines",
            how="Per-day worst run state: Failed beats Ran slow (60s+ over its own median) beats Skipped beats Ran fine."),
        bar("Failed runs by reason", """
SELECT CASE WHEN termination_code IN ('TIMED_OUT', 'TIMEDOUT') THEN 'Timed out'
            WHEN termination_code IN ('DRIVER_ERROR', 'CLUSTER_ERROR', 'DRIVER_UNREACHABLE', 'DRIVER_UNRESPONSIVE',
                                      'CLOUD_FAILURE') THEN 'Driver or cluster lost'
            ELSE 'Execution error' END AS bucket,
       COALESCE(termination_code, 'no code recorded') AS reason,
       SUM(run_rows) AS runs
FROM {named:lakeflow_termination_taxonomy}
WHERE {ws} AND COALESCE(termination_code, '') <> 'SUCCESS' AND upper(COALESCE(termination_code, '')) NOT LIKE '%CANCEL%'
GROUP BY 1, 2
""", "reason", "runs", series="bucket", fmt=INT, category_title="Termination code", value_title="Failed attempts",
            how="Failed attempts, cancels excluded, grouped into timed out, driver/cluster lost, or execution error."),
    )
    p.table(table("Jobs with failed runs", """
SELECT r.status, r.n_job AS job, date_format(r.latest_run_start, 'yyyy-MM-dd HH:mm') AS latest_run,
       r.failed_runs, r.runs, r.failure_rate_pct, r.consecutive_failures,
       COALESCE(r.latest_termination_code, '-') AS reason,
       ROUND(w.est_wasted_usd_list * {disc}, 0) AS lost_usd
FROM {named:lakeflow_job_reliability} r
LEFT JOIN {S}.lakeflow_failed_jobs_wasted_dbus w ON w.workspace_id = r.workspace_id AND w.job_id = r.job_id
WHERE r.failed_runs > 0 AND {ws:r}
ORDER BY CASE r.status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, r.failed_runs DESC
LIMIT 500
""", [("status", "Status"), ("job", "Job"), ("latest_run", "Latest run"), ("failed_runs", "Failed", INT),
      ("runs", "Runs", INT), ("failure_rate_pct", "Failed %", PCT), ("consecutive_failures", "In a row", INT),
      ("reason", "Last reason"), ("lost_usd", "$ lost", MONEY)], height=10,
        how="Every job with 1+ failed run, Critical (3+ in a row) first then Warn, with $ lost from failed runs."))
    p.checks()


def slow(d):
    p = d.page("slow")
    p.kpis(
        kpi("Slower than usual", """
WITH r AS (SELECT * FROM {named:lakeflow_job_duration_regression} WHERE {ws})
SELECT (SELECT count(*) FROM r WHERE status IN ('CRITICAL', 'WARN')) AS value,
       concat('Judged ', CAST((SELECT count(*) FROM r WHERE not_assessed_reason IS NULL) AS STRING), ' of ',
              CAST((SELECT count(*) FROM r) AS STRING), ' jobs') AS target
""", target="target", tone="warn",
            how="Recent runs vs before: 1.5x+ slower is Warn, 2x+ is Critical (duration regression check)."),
        kpi("Worst queue wait p95 (s)", """
WITH r AS (SELECT * FROM {named:lakeflow_job_queue_time} WHERE {ws} AND queue_s_p95 > 0)
SELECT COALESCE((SELECT ROUND(max(queue_s_p95), 0) FROM r), 0) AS value,
       COALESCE((SELECT concat('Worst: ', nm_job) FROM r ORDER BY queue_s_p95 DESC LIMIT 1), 'No queue wait') AS target
""", target="target", fmt=NUM,
            how="p95 seconds waiting for compute before a run starts; Warn at 60s, Critical at 300s."),
        kpi("Worst cold start p95 (s)", """
WITH r AS (SELECT * FROM {named:lakeflow_phase_cold_start} WHERE {ws} AND setup_s_p95 > 0)
SELECT COALESCE((SELECT ROUND(max(setup_s_p95), 0) FROM r), 0) AS value,
       COALESCE((SELECT concat('Worst: ', nm_job) FROM r ORDER BY setup_s_p95 DESC LIMIT 1), 'None measured') AS target
""", target="target", fmt=NUM,
            how="p95 seconds spent starting the cluster before a run's first task."),
        kpi("Jobs measured", """
SELECT count(*) AS value, 'Successful runs, recent vs before' AS target
FROM {named:lakeflow_job_duration_regression} WHERE {ws}
""", target="target",
            how="Jobs with enough successful runs to compare recent duration against earlier runs."),
    )
    p.charts(
        bar("Jobs waiting longest on compute (p95 queue, s)", """
SELECT lbl_job AS job, ROUND(max(queue_s_p95), 0) AS queue_s
FROM {named:lakeflow_job_queue_time}
WHERE {ws} AND queue_s_p95 > 0
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "job", "queue_s", fmt=NUM, category_title="Job", value_title="Seconds",
            how="Worst p95 queue-wait seconds per job; labeled job · workspace so same-named jobs don't merge."),
        bar("Slowest cluster cold starts (p95, s)", """
SELECT lbl_job AS job, ROUND(max(setup_s_p95), 0) AS setup_s
FROM {named:lakeflow_phase_cold_start}
WHERE {ws} AND setup_s_p95 > 0
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "job", "setup_s", fmt=NUM, category_title="Job", value_title="Seconds",
            how="Worst p95 cluster cold-start seconds per job; labeled job · workspace."),
    )
    p.charts(
        bar("Jobs by cluster start time (p95), hours of task start", """
SELECT CASE WHEN setup_s_p95 < 120 THEN '1. Under 2 min' WHEN setup_s_p95 < 300 THEN '2. 2-5 min'
            WHEN setup_s_p95 < 600 THEN '3. 5-10 min' ELSE '4. Over 10 min' END AS band,
       ROUND(SUM(setup_s_total) / 3600.0, 1) AS hours
FROM {named:lakeflow_phase_cold_start}
WHERE {ws} AND setup_s_p95 IS NOT NULL
GROUP BY 1
""", "band", "hours", fmt=NUM, category_title="Cluster start (p95)", value_title="Hours", sort="category",
            how="Total hours spent starting clusters, by each job's own p95 start-time band, numbered so bands sort right."),
        bar("Failed cluster starts, by reason", """
SELECT COALESCE(termination_code, 'no code recorded') AS reason, SUM(failed_starts) AS starts
FROM {named:lakeflow_failed_cluster_starts}
WHERE {ws}
GROUP BY 1
""", "reason", "starts", fmt=INT, category_title="Termination code", value_title="Failed starts",
            how="Cluster starts that failed before a task could run, by termination code."),
    )
    p.charts(
        bar("Slower than usual, by job (recent / before)", """
SELECT lbl_job AS job, ROUND(max(slowdown_ratio), 2) AS ratio
FROM {named:lakeflow_job_duration_regression}
WHERE {ws} AND status IN ('CRITICAL', 'WARN')
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "job", "ratio", fmt=NUM, category_title="Job", value_title="Times slower",
            how="Recent median run time over earlier median, Critical/Warn jobs only; labeled job · workspace."),
    )
    p.table(table("Longest runs", """
SELECT n_job AS job, date_format(run_start, 'yyyy-MM-dd HH:mm') AS started, run_hours,
       CASE WHEN in_flight THEN 'still running' ELSE COALESCE(result_state, '-') END AS result,
       x_vs_job_p50, top_task_key, top_task_share_pct
FROM {named:lakeflow_long_running_runs}
WHERE {ws}
ORDER BY run_hours DESC
LIMIT 20
""", [("job", "Job"), ("started", "Started"), ("run_hours", "Hours", NUM), ("result", "Result"),
      ("x_vs_job_p50", "x its usual", NUM), ("top_task_key", "Longest task"), ("top_task_share_pct", "Task share %", PCT)],
        how="Top 20 longest runs; Critical at 6+ hours or still running, measured against the job's own median."))
    p.checks()


def hygiene(d):
    p = d.page("hygiene")
    p.kpis(
        kpi("Jobs with no timeout", """
SELECT count(*) AS value, 'A stuck run bills until someone stops it' AS target
FROM {named:lakeflow_jobs_no_timeout} WHERE {ws} AND no_timeout
""", target="target", tone="warn",
            how="No timeout_seconds set; Critical once the job has spent $200 or more in the window."),
        kpi("Tasks with no timeout", """
SELECT count(*) AS value, 'Task level, inside jobs' AS target
FROM {named:lakeflow_job_tasks_no_timeout} WHERE {ws} AND no_timeout
""", target="target", tone="warn",
            how="Same no-timeout check, one row per task inside a job."),
        kpi("Health-rule coverage (%)", """
SELECT ROUND(100.0 * count_if(health_rule_count > 0) / NULLIF(count_if(health_rule_count IS NOT NULL), 0), 0) AS value,
       'Active jobs with a health rule' AS target
FROM {named:lakeflow_health_rule_coverage} WHERE {ws}
""", target="target", fmt=PCT,
            how="Share of active jobs with at least 1 health rule configured."),
        kpi("Ownership-risk jobs", """
SELECT count_if(status IN ('CRITICAL', 'WARN')) AS value, 'Owner gone or run as a person' AS target
FROM {named:lakeflow_job_ownership_orphans} WHERE {ws}
""", target="target", tone="warn",
            how="Critical: a scheduled job running as a person. Warn: a manual run-as-person, or no run-as recorded."),
    )
    p.charts(
        bar("Jobs relying on retries or repairs", """
SELECT lbl_job AS job, SUM(total_retries) AS retries
FROM {named:lakeflow_retries_repairs}
WHERE {ws} AND total_retries > 0
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "job", "retries", fmt=INT, category_title="Job", value_title="Retries and repairs",
            how="Repairs plus task retries plus retry runs; Warn at 5+, Critical at 20+; labeled job · workspace."),
        bar("Jobs on all-purpose compute, $", """
SELECT lbl_job AS job, ROUND(SUM(est_usd_list_share) * {disc}, 0) AS usd
FROM {named:lakeflow_jobs_on_all_purpose}
WHERE {ws}
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "job", "usd", fmt=MONEY, category_title="Job", value_title="Spend",
            how="List-price $ run on UI/API clusters instead of job clusters, after discount; labeled job · workspace."),
    )
    p.table(table("Stale jobs with no recent run", """
SELECT n_job AS job, date_format(last_run_start, 'yyyy-MM-dd') AS last_run, date_format(create_time, 'yyyy-MM-dd') AS created,
       ROUND(est_usd_list * {disc}, 0) AS usd
FROM {named:lakeflow_stale_zombie_jobs}
WHERE {ws} AND is_stale = 1
ORDER BY last_run_start NULLS FIRST
LIMIT 50
""", [("job", "Job"), ("last_run", "Last run"), ("created", "Created"), ("usd", "Spend in window", MONEY)],
        how="Jobs with no run in 90+ days (30+ once the workspace has enough history)."))
    p.checks()


def compute_fit(d):
    p = d.page("compute_fit")
    p.kpis(
        kpi("Jobs under pressure", """
SELECT count_if(status IN ('CRITICAL', 'WARN')) AS value, concat('of ', CAST(count(*) AS STRING), ' jobs checked') AS target
FROM {named:lakeflow_job_compute_pressure} WHERE {ws}
""", target="target", tone="crit",
            how="Jobs whose worker CPU, memory, I/O or skew crossed a Critical or Warn band at peak."),
        kpi("Tasks flagged", """
SELECT count_if(status IN ('CRITICAL', 'WARN')) AS value, 'Task runs over 2 hours' AS target
FROM {named:task_cluster_utilization} WHERE {ws}
""", target="target", tone="crit",
            how="Task runs over 2 hours with a Critical or Warn bottleneck (memory pressure, skew, I/O, driver-bound)."),
        kpi("Jobs on oversized compute", """
SELECT count_if(oversized) AS value, 'Low CPU and memory at peak' AS target
FROM {named:lakeflow_job_oversized} WHERE {ws}
""", target="target", tone="warn",
            how="Worker CPU p90 under 30% and peak memory under 40%, so a smaller node would do."),
        kpi("Possible saving", """
SELECT ROUND(COALESCE(SUM(est_saving_usd_list), 0) * {disc}, 0) AS value, 'From smaller compute' AS target
FROM {named:lakeflow_job_oversized} WHERE {ws} AND oversized
""", target="target", fmt=MONEY,
            how="$ saved if oversized jobs moved to the next-smaller node, after discount."),
    )
    p.charts(
        bar("Jobs by compute pressure", """
SELECT CASE COALESCE(pressure, 'NOT_MEASURED') WHEN 'NONE' THEN 'No pressure' WHEN 'MEMORY' THEN 'Memory'
            WHEN 'CPU' THEN 'CPU' WHEN 'IDLE' THEN 'Mostly idle' WHEN 'SKEW' THEN 'Skewed'
            WHEN 'IO_WAIT' THEN 'Waiting on I/O' WHEN 'DRIVER' THEN 'Driver' ELSE 'Not measured' END AS pressure,
       count(*) AS jobs
FROM {named:lakeflow_job_compute_pressure}
WHERE {ws}
GROUP BY 1
""", "pressure", "jobs", fmt=INT, category_title="Pressure", value_title="Jobs",
            how="Jobs by their worst compute-pressure signal at peak: memory, CPU, skew, idle, I/O, or driver."),
        bar("Tasks by bottleneck", """
SELECT CASE COALESCE(bottleneck_hint, 'NONE') WHEN 'WAITING_OR_IDLE' THEN 'Waiting or idle'
            WHEN 'MEMORY_PRESSURE' THEN 'Memory' WHEN 'DRIVER_BOUND' THEN 'Driver' WHEN 'IO_WAIT' THEN 'Waiting on I/O'
            WHEN 'COMPUTE_BOUND' THEN 'CPU' WHEN 'SKEWED' THEN 'Skewed' WHEN 'MIXED' THEN 'Mixed'
            ELSE 'Not measured' END AS hint,
       count(*) AS tasks
FROM {named:task_cluster_utilization}
WHERE {ws}
GROUP BY 1
""", "hint", "tasks", fmt=INT, category_title="Bottleneck", value_title="Task runs",
            how="Task runs over 2 hours by bottleneck: memory, driver, I/O, CPU, skew, waiting/idle, or mixed."),
    )
    p.table(table("Flagged jobs, worst first", """
SELECT status, n_job AS job, pressure, worker_mem_p90_pct, worker_mem_peak_pct, worker_swap_p90_pct,
       worker_cpu_p90_pct, worker_cpu_p50_pct, scaling_hint
FROM {named:lakeflow_job_compute_pressure}
WHERE {ws} AND status IN ('CRITICAL', 'WARN')
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 ELSE 1 END, worker_mem_p90_pct DESC NULLS LAST
LIMIT 50
""", [("status", "Status"), ("job", "Job"), ("pressure", "Pressure"), ("worker_mem_p90_pct", "Memory p90 %", PCT),
      ("worker_mem_peak_pct", "Memory peak %", PCT), ("worker_swap_p90_pct", "Swap p90 %", PCT),
      ("worker_cpu_p90_pct", "CPU p90 %", PCT), ("worker_cpu_p50_pct", "CPU median %", PCT),
      ("scaling_hint", "Suggested lever")],
        how="Jobs with a Critical or Warn pressure signal, Critical first, worst memory p90 first."))
    p.table(table("Jobs on oversized compute", """
SELECT n_job AS job, worker_node_type, workers_configured_max, worker_cpu_p90_pct, worker_mem_p90_pct,
       ROUND(job_list_cost_usd * {disc}, 0) AS job_usd, ROUND(est_saving_usd_list * {disc}, 0) AS saving_usd,
       suggested_action
FROM {named:lakeflow_job_oversized}
WHERE {ws} AND oversized
ORDER BY est_saving_usd_list DESC NULLS LAST
LIMIT 50
""", [("job", "Job"), ("worker_node_type", "Worker type"), ("workers_configured_max", "Max workers", INT),
      ("worker_cpu_p90_pct", "CPU p90 %", PCT), ("worker_mem_p90_pct", "Memory p90 %", PCT),
      ("job_usd", "Job $", MONEY), ("saving_usd", "Est. saving", MONEY), ("suggested_action", "Suggested fix")],
        how="Oversized jobs (low CPU and memory at peak), worst possible saving first, after discount."))
    p.checks()


def pipelines(d):
    p = d.page("pipelines")
    p.kpis(
        kpi("Pipeline spend", """
SELECT ROUND(COALESCE(SUM(net_list_cost), 0) * {disc}, 0) AS value,
       concat(CAST(count(*) AS STRING), ' pipelines') AS target
FROM {named:lakeflow_pipeline_cost} WHERE {ws}
""", target="target", fmt=MONEY,
            how="List-price $ across all pipelines in the window, after discount."),
        kpi("Failed or canceled updates", """
SELECT COALESCE(SUM(updates_failed_or_canceled), 0) AS value,
       concat('of ', CAST(COALESCE(SUM(updates), 0) AS STRING), ' updates') AS target
FROM {named:lakeflow_pipeline_cost} WHERE {ws}
""", target="target", tone="warn",
            how="Pipeline update runs that ended failed or canceled."),
        kpi("Pipelines with an idle tail", """
SELECT count_if(status IN ('CRITICAL', 'WARN')) AS value, 'Compute kept after the last update' AS target
FROM {named:lakeflow_pipeline_idle_tail_duration} WHERE {ws}
""", target="target", tone="warn",
            how="Idle DBUs after the last update; Warn at 100+, Critical at 500+."),
        kpi("Spend with nothing delivered", """
SELECT ROUND(COALESCE(SUM(CASE WHEN nothing_delivered THEN net_list_cost END), 0) * {disc}, 0) AS value,
       concat(CAST(count_if(nothing_delivered) AS STRING), ' pipelines') AS target
FROM {named:lakeflow_pipeline_cost} WHERE {ws}
""", target="target", fmt=MONEY, tone="warn",
            how="$ on pipelines with no update activity, or every update ended with nothing written; Critical at 2000+ DBUs."),
    )
    p.charts(
        bar("Spend by pipeline", """
SELECT lbl_pipeline AS pipeline, ROUND(SUM(net_list_cost) * {disc}, 0) AS usd
FROM {named:lakeflow_pipeline_cost}
WHERE {ws}
GROUP BY 1 ORDER BY 2 DESC LIMIT 10
""", "pipeline", "usd", fmt=MONEY, category_title="Pipeline", value_title="Spend",
            how="List-price $ per pipeline, after discount; labeled pipeline · workspace."),
        bar("Failed updates by pipeline", """
SELECT lbl_pipeline AS pipeline, SUM(failed_update_rows) AS failed
FROM {named:lakeflow_pipeline_update_failures_retries}
WHERE {ws} AND failed_update_rows > 0
GROUP BY 1 ORDER BY 2 DESC LIMIT 10
""", "pipeline", "failed", fmt=INT, category_title="Pipeline", value_title="Failed updates",
            how="Failed update runs per pipeline; labeled pipeline · workspace."),
    )
    p.checks()
