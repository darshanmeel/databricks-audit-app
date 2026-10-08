-- query_id: task_cluster_utilization
-- title: Inside a long job run, what each task's classic cluster was doing - waiting, skewed, compute-bound or undersized
-- domain: compute   tier: standard
-- reads: system.lakeflow.job_run_timeline, system.lakeflow.job_task_run_timeline, system.compute.node_timeline, system.compute.clusters, system.lakeflow.jobs, system.access.workspaces_latest
-- requires: SELECT on system.lakeflow, system.compute AND system.access (the latter only for workspace_name and the three URL columns - see caveats to drop it); GA. SCOPE LIMIT: node_timeline holds classic all-purpose and job clusters ONLY, so a serverless or SQL-warehouse task has no hardware profile and comes back NOT_ASSESSED (its SQL is in query_task_statement_breakdown instead)
-- empty_if: schema_not_enabled, compute_scope_gap, retention_window, no_activity, ingestion_lag
-- params: :period_days (default 30) rolling window in days, capped at 90 in-SQL by node_timeline retention; :warn_run_hours (default 2) run wall-clock hours at/above which a run is examined (the same bound as lakeflow_long_running_runs, so the two compose); :min_slices (default 60) minimum node-minutes overlapping the task before a cluster is judged (fewer -> NOT_ASSESSED); :waiting_cpu_pct (default 20) average worker CPU percent below which the cluster was mostly waiting, not working; :busy_cpu_pct (default 70) median worker CPU percent at/above which the task is genuinely compute-bound; :crit_mem_pct (default 85) p90 worker memory percent at/above which the cluster looks undersized (CRITICAL); :crit_swap_pct (default 10) p90 worker (or driver, on a single-node cluster) memory swap percent at/above which the cluster is judged memory-pressure (CRITICAL) - a sustained figure, because a small non-zero swap is normal on these nodes; :skew_gap_pct (default 40) gap in average CPU between the hottest and the coldest worker that flags skew; :warn_io_wait_pct (default 20) average worker CPU-wait percent at/above which the task is I/O-bound; :top_n (default 200) row cap
-- confidence: needs_confirmation
-- confidence_note: Every column used (node_timeline's cpu_*/mem_*/network_* and driver, job_task_run_timeline.compute_ids, clusters' node types and autoscale bounds, workspaces_latest.workspace_url) was checked against a live system-catalog schema dump of this account taken 2026-07-07, and the table scopes, retention, the zero-valued run durations and the run / cluster URL shapes against the Databricks doc pages on 2026-09-21 - but the query has NOT been executed on a workspace. Confirm on your account: 1) for a task you know ran on a job cluster, node_minutes is roughly (nodes x task minutes) - far below that means node_timeline's cluster_id does not match compute_ids, or the task fell in its ~10-minute blind spot; 2) cluster_url opens the cluster and spark_ui_url opens its Spark UI - the /sparkUi suffix is the one unverified URL shape here, drop that column if it 404s; 3) a serverless task shows cluster_id NULL and status NOT_ASSESSED rather than an error.
-- read_this: One row = one task run of one job run that lasted at least :warn_run_hours, with the per-minute hardware telemetry of the classic cluster it ran on, restricted to the task's own window. The columns that matter are bottleneck_hint (WAITING_OR_IDLE, DRIVER_BOUND, IO_WAIT, SKEWED, COMPUTE_BOUND, MEMORY_PRESSURE or MIXED) and the four numbers behind it: worker_cpu_p50_pct, worker_cpu_spread_pct (hottest minus coldest worker), worker_mem_p90_pct and worker_cpu_wait_avg_pct. overlapping_task_runs says how many OTHER task runs shared the cluster during the window - above 0 the profile is the cluster's, not this task's. This query cannot see the SQL or the Spark stage; it tells you the SHAPE of the problem so you open the Spark UI (spark_ui_url) already knowing what to look for.
-- healthy: bottleneck_hint = COMPUTE_BOUND or MIXED with overlapping_task_runs = 0 - the cluster was doing the work; the time is the job's design or input volume, not a misfit between task and compute - field heuristic; every threshold is a header param.
-- investigate_if: WAITING_OR_IDLE, DRIVER_BOUND, IO_WAIT or SKEWED (WARN) - the cluster was billed while mostly idle, single-threaded on the driver, stalled on I/O, or hot on a few workers only; MEMORY_PRESSURE (CRITICAL) - p90 worker memory at/above :crit_mem_pct or p90 worker swap at/above :crit_swap_pct, so spill and OOM are the likely cause of the length (field heuristic). NOT_ASSESSED is not a pass: read not_assessed_reason.
-- actions: 1) open spark_ui_url (or run_url and the task's Spark UI tab) with the hint in hand - SKEWED: find the stage whose task durations are lopsided and salt / repartition the key; WAITING_OR_IDLE: look at the driver log for the pause (an external call, a collect(), a serial loop); IO_WAIT: the input is small-file or remote-storage bound, compact or cache it; DRIVER_BOUND: the work is not distributed (pandas, toPandas(), driver-side loops) (free); 2) MEMORY_PRESSURE: raise spark.sql.shuffle.partitions / cut the widest stage, or move to a memory-optimized worker_node_type; SKEWED with autoscale: the cluster is not the problem, the key is (config); 3) if the profile is genuinely COMPUTE_BOUND for hours and x_vs_job_p50 in lakeflow_long_running_runs is near 1, buy time - more workers, a faster family, or Photon (spend).
-- next: query_task_statement_breakdown (for the serverless / warehouse tasks of the same runs - the statements themselves), lakeflow_long_running_runs (for the run-level picture this query zooms into), node_timeline_utilization (for the same cluster's profile over the whole window instead of one task), compute_idle_node_ratio (if WAITING_OR_IDLE recurs - the cluster-level idle ranking with list-price waste), classic_clusters_config_current (if the node type or autoscale range looks wrong), lakeflow_jobs_on_all_purpose (if overlapping_task_runs is high - the cluster is shared), cost_workspace_names (if you dropped the workspaces_latest join and want to resolve workspace_id separately)
-- not_assessed_reasons: task_not_executed: the task was skipped or blocked and never ran; no_cluster_recorded: the task ran on compute this check cannot see (serverless, or a row from before cluster ids were recorded); no_node_timeline_rows: a cluster was recorded but no matching node telemetry exists for it; too_few_slices: too few overlapping minutes of node telemetry were recorded to judge
-- caveats: SCOPE - system.compute.node_timeline records classic all-purpose and job clusters only, so a serverless or SQL-warehouse task has no rows here: it is kept as a NOT_ASSESSED row with not_assessed_reason = no_cluster_recorded (compute_ids empty - serverless, or a row from before compute_ids was populated in early Dec 2025) or no_node_timeline_rows (an id was recorded but nothing overlaps the task window - a SQL warehouse id, which the doc says compute_ids also carries, a node that ran under ~10 minutes, which the doc says may not appear in node_timeline at all, or activity past its 90-DAY default retention, which is why :period_days is capped at LEAST(:period_days, 90)); too_few_slices means fewer than :min_slices overlapping node-minutes, tune it down for short tasks; task_not_executed means the task was SKIPPED or BLOCKED and never ran (the doc represents it as a row whose period_start_time equals period_end_time). node_timeline and clusters cover all-purpose, job AND Lakeflow pipeline clusters (cluster_source UI / API / JOB / PIPELINE / PIPELINE_MAINTENANCE), never serverless or SQL warehouses. The cluster is the FIRST entry of the task's compute_ids (as lakeflow_long_running_runs does); compute_ids_count shows when a task recorded more than one. The profile is CPU / memory of the machines during the task's window, NOT the task's own consumption: on a shared all-purpose cluster every concurrent workload is in the numbers, and overlapping_task_runs counts the other task runs (any job, in the window) on the same cluster whose window overlaps - read the hint with care whenever it is above 0. There is no Spark stage, job or statement in any system table; the hint is a machine-level inference (field heuristics in the :params, in this precedence: MEMORY_PRESSURE, SKEWED, COMPUTE_BOUND, IO_WAIT, DRIVER_BOUND, WAITING_OR_IDLE, else MIXED) meant to tell you what to look for when you do open the Spark UI. DURATIONS - the doc states that run_duration_seconds and the other phase columns on job_run_timeline are populated only for legacy single-task jobs and log 0 for every multi-task job, so a 0 is treated as "not reported" and the run bound falls back to wall clock between the run's first and last observed rows (exactly what lakeflow_long_running_runs must do too). Task seconds are execution_duration_seconds where populated (end row only, since early Dec 2025, 0 treated the same way), else wall clock between the task's first and last observed rows, to current_timestamp() while in flight; the telemetry window is that same wall clock, so cluster start-up minutes before the task's first row are excluded (that is the setup phase, see lakeflow_phase_cold_start). Single-node clusters have no worker rows: single_node = true and the hint is computed from the driver's numbers instead. mem_used_percent includes OS and background processes, so a memory floor of 30-40 percent on an idle node is normal. SWAP IS THRESHOLDED - the MEMORY_PRESSURE rule judges the p90 of worker (or driver, on a single-node cluster) swap against :crit_swap_pct, not any swap above zero, because a small non-zero swap figure is normal on these nodes; lakeflow_job_compute_pressure judges its own job-level swap the same way, with the same default, so the two now agree on a task/job's swap-driven verdict. Cluster config comes from system.compute.clusters, SCD2 - the latest row per cluster, so a job cluster that was resized between runs shows its last shape. Like lakeflow_long_running_runs this query keeps the current day so a run in flight is examined; very recent minutes may still be materializing. Names are resolved by LEFT JOIN so a missing name never drops a row: job_name comes from system.lakeflow.jobs (SCD2) which one-time SUBMIT_RUN / WORKFLOW_RUN executions never write to; workspace_name and the three URLs come from system.access.workspaces_latest, a DIFFERENT system schema - if you cannot read it this query ERRORS rather than degrading, so drop workspace_name, the three URL columns and the ws LEFT JOIN, and resolve ids with cost_workspace_names. run_url and cluster_url follow the shapes lakeflow_long_running_runs uses; spark_ui_url appends /sparkUi to the cluster page and is the one unverified URL shape (the documented alternative needs the run's spark_context_id, which no system table carries). The doc says a terminated cluster is permanently deleted 30 days after termination unless pinned, and only a limited number of recently terminated job clusters keep their Spark UI, so open the link soon - or turn on cluster log delivery to a Unity Catalog volume (GA since Apr 2026) so the Spark event log outlives the cluster and can be replayed. The job run page itself is kept for 60 days. No identities are emitted, so nothing is masked. There are no dollars here; price the cluster with compute_idle_node_ratio or cost_by_job.
WITH run_agg AS (
  SELECT workspace_id, job_id, run_id,
         MIN(period_start_time)    AS run_start,
         MAX(period_end_time)      AS last_seen,
         MAX(result_state)         AS result_state,       -- NULL until the run's end row lands
         -- end row only; NULL before Dec 2025 and 0 for every multi-task job (doc), so 0 = not reported
         NULLIF(MAX(run_duration_seconds), 0) AS run_s_reported
  FROM system.lakeflow.job_run_timeline
  WHERE period_start_time >= dateadd(day, -LEAST(:period_days, 90), current_date())
  GROUP BY workspace_id, job_id, run_id
),
run_obs AS (
  SELECT workspace_id, job_id, run_id,
         (result_state IS NULL) AS in_flight,
         COALESCE(run_s_reported,
                  timestampdiff(SECOND, run_start,
                                CASE WHEN result_state IS NULL THEN current_timestamp()
                                     ELSE last_seen END)) AS run_s
  FROM run_agg
),
long_runs AS (
  -- the same run bound as lakeflow_long_running_runs, so the two queries compose
  SELECT workspace_id, job_id, run_id, in_flight, run_s
  FROM run_obs
  WHERE run_s >= :warn_run_hours * 3600
     OR run_s IS NULL
),
task_agg AS (
  -- one row per TASK RUN of a long run (run_id here is the task run id)
  SELECT t.workspace_id, t.job_id, t.job_run_id,
         t.run_id                              AS task_run_id,
         t.task_key,
         MIN(t.period_start_time)              AS task_start,
         MAX(t.period_end_time)                AS task_last_seen,
         MAX(t.result_state)                   AS task_result_state,
         NULLIF(MAX(t.execution_duration_seconds), 0) AS task_exec_s,
         -- first compute id of the task; NULL on serverless and on pre-Dec-2025 rows
         MAX(try_element_at(t.compute_ids, 1)) AS cluster_id,
         -- guarded: size(NULL) is -1 outside ANSI mode, and a -1 would read as a count
         MAX(CASE WHEN t.compute_ids IS NULL THEN NULL ELSE size(t.compute_ids) END) AS compute_ids_count
  FROM system.lakeflow.job_task_run_timeline t
  JOIN long_runs r
    ON  r.workspace_id = t.workspace_id
    AND r.job_id       = t.job_id
    AND r.run_id       = t.job_run_id
  WHERE t.period_start_time >= dateadd(day, -LEAST(:period_days, 90), current_date())
  GROUP BY t.workspace_id, t.job_id, t.job_run_id, t.run_id, t.task_key
),
task_obs AS (
  SELECT workspace_id, job_id, job_run_id, task_run_id, task_key, task_start, task_result_state,
         cluster_id, compute_ids_count,
         -- a task that never ran: SKIPPED / BLOCKED, or the doc's zero-length row
         (task_result_state IN ('SKIPPED', 'BLOCKED')
          OR (task_result_state IS NOT NULL AND task_start = task_last_seen)) AS task_not_executed,
         -- the telemetry window: the task's observed wall clock, open-ended while in flight
         CASE WHEN task_result_state IS NULL THEN current_timestamp() ELSE task_last_seen END AS task_end,
         COALESCE(task_exec_s,
                  timestampdiff(SECOND, task_start,
                                CASE WHEN task_result_state IS NULL THEN current_timestamp()
                                     ELSE task_last_seen END)) AS task_s
  FROM task_agg
),
slices AS (
  -- every node-minute of the task's cluster that overlaps the task's window
  SELECT t.workspace_id, t.job_run_id, t.task_run_id, t.cluster_id,
         n.instance_id, n.driver,
         n.cpu_user_percent + n.cpu_system_percent AS cpu_pct,
         n.cpu_wait_percent, n.mem_used_percent, n.mem_swap_percent,
         n.network_sent_bytes, n.network_received_bytes
  FROM task_obs t
  JOIN system.compute.node_timeline n
    ON  n.workspace_id = t.workspace_id
    AND n.cluster_id   = t.cluster_id
    AND n.start_time   <  t.task_end
    AND n.end_time     >  t.task_start
  WHERE n.start_time >= dateadd(day, -LEAST(:period_days, 90), current_date())
),
per_node AS (
  -- one row per node of the task's cluster, to measure skew across workers
  SELECT workspace_id, job_run_id, task_run_id, cluster_id, instance_id, driver,
         AVG(cpu_pct) AS node_cpu_avg
  FROM slices
  GROUP BY workspace_id, job_run_id, task_run_id, cluster_id, instance_id, driver
),
skew AS (
  SELECT workspace_id, job_run_id, task_run_id, cluster_id,
         COUNT(*)                                           AS nodes_seen,
         SUM(CASE WHEN driver THEN 0 ELSE 1 END)            AS worker_nodes_seen,
         MAX(CASE WHEN driver THEN NULL ELSE node_cpu_avg END)
           - MIN(CASE WHEN driver THEN NULL ELSE node_cpu_avg END) AS worker_cpu_spread_pct
  FROM per_node
  GROUP BY workspace_id, job_run_id, task_run_id, cluster_id
),
profile AS (
  SELECT workspace_id, job_run_id, task_run_id, cluster_id,
         COUNT(*)                                                  AS node_minutes,
         SUM(CASE WHEN driver THEN 0 ELSE 1 END)                   AS worker_minutes,
         SUM(CASE WHEN driver THEN 1 ELSE 0 END)                   AS driver_minutes,
         AVG(CASE WHEN driver THEN NULL ELSE cpu_pct END)          AS worker_cpu_avg_pct,
         percentile(CASE WHEN driver THEN NULL ELSE cpu_pct END, 0.5) AS worker_cpu_p50_pct,
         percentile(CASE WHEN driver THEN NULL ELSE cpu_pct END, 0.9) AS worker_cpu_p90_pct,
         MAX(CASE WHEN driver THEN NULL ELSE cpu_pct END)          AS worker_cpu_peak_pct,
         AVG(CASE WHEN driver THEN NULL ELSE cpu_wait_percent END) AS worker_cpu_wait_avg_pct,
         AVG(CASE WHEN driver THEN NULL ELSE mem_used_percent END) AS worker_mem_avg_pct,
         percentile(CASE WHEN driver THEN NULL ELSE mem_used_percent END, 0.9) AS worker_mem_p90_pct,
         MAX(CASE WHEN driver THEN NULL ELSE mem_used_percent END) AS worker_mem_peak_pct,
         percentile(CASE WHEN driver THEN NULL ELSE mem_swap_percent END, 0.9) AS worker_swap_p90_pct,
         MAX(CASE WHEN driver THEN NULL ELSE mem_swap_percent END) AS worker_swap_peak_pct,
         AVG(CASE WHEN driver THEN cpu_pct END)                    AS driver_cpu_avg_pct,
         MAX(CASE WHEN driver THEN cpu_pct END)                    AS driver_cpu_peak_pct,
         percentile(CASE WHEN driver THEN mem_used_percent END, 0.9) AS driver_mem_p90_pct,
         MAX(CASE WHEN driver THEN mem_used_percent END)           AS driver_mem_peak_pct,
         percentile(CASE WHEN driver THEN mem_swap_percent END, 0.9) AS driver_swap_p90_pct,
         MAX(CASE WHEN driver THEN mem_swap_percent END)           AS driver_swap_peak_pct,
         SUM(network_sent_bytes)                                   AS network_sent_bytes,
         SUM(network_received_bytes)                               AS network_received_bytes
  FROM slices
  GROUP BY workspace_id, job_run_id, task_run_id, cluster_id
),
all_task_windows AS (
  -- every task run in the window with its first cluster, to count cluster sharing
  SELECT workspace_id, run_id AS task_run_id,
         MAX(try_element_at(compute_ids, 1)) AS cluster_id,
         MIN(period_start_time)              AS t_start,
         CASE WHEN MAX(result_state) IS NULL THEN current_timestamp()
              ELSE MAX(period_end_time) END  AS t_end
  FROM system.lakeflow.job_task_run_timeline
  WHERE period_start_time >= dateadd(day, -LEAST(:period_days, 90), current_date())
  GROUP BY workspace_id, run_id
),
sharing AS (
  SELECT t.workspace_id, t.job_run_id, t.task_run_id,
         COUNT(o.task_run_id) AS overlapping_task_runs
  FROM task_obs t
  LEFT JOIN all_task_windows o
    ON  o.workspace_id = t.workspace_id
    AND o.cluster_id   = t.cluster_id
    AND o.task_run_id <> t.task_run_id
    AND o.t_end        >  o.t_start          -- a never-run task (zero-length row) shares nothing
    AND o.t_start      <  t.task_end
    AND o.t_end        >  t.task_start
  GROUP BY t.workspace_id, t.job_run_id, t.task_run_id
),
latest_clusters AS (
  -- system.compute.clusters is SCD2: the newest row per cluster (deleted clusters kept)
  SELECT workspace_id, cluster_id, cluster_name, cluster_source, driver_node_type, worker_node_type,
         worker_count, min_autoscale_workers, max_autoscale_workers, dbr_version
  FROM system.compute.clusters
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC
  ) = 1
),
latest_jobs AS (
  -- system.lakeflow.jobs is SCD2: one row per change, so take the newest per job
  SELECT workspace_id, job_id, name AS job_name
  FROM system.lakeflow.jobs
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
),
ws AS (
  -- workspace name + the base URL the deep links are built from (the stored URL ends in '/')
  SELECT workspace_id, workspace_name,
         regexp_replace(workspace_url, '/+$', '') AS base_url
  FROM system.access.workspaces_latest
),
judged AS (
  SELECT t.*, r.run_s, r.in_flight,
         p.node_minutes, p.worker_minutes, p.driver_minutes,
         k.nodes_seen, k.worker_nodes_seen, k.worker_cpu_spread_pct,
         p.worker_cpu_avg_pct, p.worker_cpu_p50_pct, p.worker_cpu_p90_pct, p.worker_cpu_peak_pct,
         p.worker_cpu_wait_avg_pct, p.worker_mem_avg_pct, p.worker_mem_p90_pct,
         p.worker_mem_peak_pct, p.worker_swap_p90_pct, p.worker_swap_peak_pct,
         p.driver_cpu_avg_pct, p.driver_cpu_peak_pct, p.driver_mem_p90_pct, p.driver_mem_peak_pct,
         p.driver_swap_p90_pct, p.driver_swap_peak_pct, p.network_sent_bytes, p.network_received_bytes,
         s.overlapping_task_runs,
         (COALESCE(p.worker_minutes, 0) = 0 AND COALESCE(p.driver_minutes, 0) > 0) AS single_node,
         -- the numbers the hint is judged on: the workers', or the driver's on a single-node cluster
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_minutes ELSE p.driver_minutes END AS judged_minutes,
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_cpu_avg_pct ELSE p.driver_cpu_avg_pct END AS eff_cpu_avg,
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_cpu_p50_pct ELSE p.driver_cpu_avg_pct END AS eff_cpu_p50,
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_mem_p90_pct ELSE p.driver_mem_p90_pct END AS eff_mem_p90,
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_swap_p90_pct ELSE p.driver_swap_p90_pct END AS eff_swap_p90,
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_cpu_wait_avg_pct ELSE NULL END AS eff_cpu_wait
  FROM task_obs t
  JOIN long_runs r
    ON  r.workspace_id = t.workspace_id
    AND r.job_id       = t.job_id
    AND r.run_id       = t.job_run_id
  LEFT JOIN profile p
    ON  p.workspace_id = t.workspace_id
    AND p.job_run_id   = t.job_run_id
    AND p.task_run_id  = t.task_run_id
  LEFT JOIN skew k
    ON  k.workspace_id = t.workspace_id
    AND k.job_run_id   = t.job_run_id
    AND k.task_run_id  = t.task_run_id
  LEFT JOIN sharing s
    ON  s.workspace_id = t.workspace_id
    AND s.job_run_id   = t.job_run_id
    AND s.task_run_id  = t.task_run_id
),
hinted AS (
  SELECT j.*,
         -- machine-level inference, in this precedence (field heuristics; see caveats)
         CASE
           WHEN j.task_not_executed                                                THEN NULL
           WHEN j.judged_minutes IS NULL OR j.judged_minutes < :min_slices        THEN NULL
           WHEN j.eff_mem_p90 >= :crit_mem_pct OR COALESCE(j.eff_swap_p90, 0) >= :crit_swap_pct THEN 'MEMORY_PRESSURE'
           WHEN COALESCE(j.worker_cpu_spread_pct, 0) >= :skew_gap_pct             THEN 'SKEWED'
           WHEN j.eff_cpu_p50 >= :busy_cpu_pct                                     THEN 'COMPUTE_BOUND'
           WHEN COALESCE(j.eff_cpu_wait, 0) >= :warn_io_wait_pct                   THEN 'IO_WAIT'
           WHEN j.eff_cpu_avg < :waiting_cpu_pct
            AND NOT j.single_node
            AND COALESCE(j.driver_cpu_avg_pct, 0) >= :busy_cpu_pct                 THEN 'DRIVER_BOUND'
           WHEN j.eff_cpu_avg < :waiting_cpu_pct                                   THEN 'WAITING_OR_IDLE'
           ELSE 'MIXED'
         END AS bottleneck_hint
  FROM judged j
)
SELECT h.workspace_id,
       w.workspace_name,                                          -- needs system.access; see caveats
       h.job_id,
       j.job_name,                                                -- NULL for submit/workflow runs
       h.job_run_id,
       ROUND(h.run_s / 3600.0, 2)                                 AS run_hours,
       h.in_flight,
       h.task_key,
       h.task_run_id,
       COALESCE(h.task_result_state, 'RUNNING')                   AS task_state,
       h.task_start,
       ROUND(h.task_s / 3600.0, 2)                                AS task_hours,
       h.cluster_id,
       h.compute_ids_count,
       c.cluster_name, c.cluster_source,
       c.worker_node_type, c.driver_node_type,
       c.worker_count                                             AS worker_count_configured,
       c.min_autoscale_workers, c.max_autoscale_workers, c.dbr_version,
       h.nodes_seen, h.worker_nodes_seen, h.node_minutes, h.worker_minutes, h.single_node,
       ROUND(h.worker_cpu_avg_pct, 1)                             AS worker_cpu_avg_pct,
       ROUND(h.worker_cpu_p50_pct, 1)                             AS worker_cpu_p50_pct,
       ROUND(h.worker_cpu_p90_pct, 1)                             AS worker_cpu_p90_pct,
       ROUND(h.worker_cpu_peak_pct, 1)                            AS worker_cpu_peak_pct,
       ROUND(h.worker_cpu_wait_avg_pct, 1)                        AS worker_cpu_wait_avg_pct,
       ROUND(h.worker_cpu_spread_pct, 1)                          AS worker_cpu_spread_pct,
       ROUND(h.worker_mem_avg_pct, 1)                             AS worker_mem_avg_pct,
       ROUND(h.worker_mem_p90_pct, 1)                             AS worker_mem_p90_pct,
       ROUND(h.worker_mem_peak_pct, 1)                            AS worker_mem_peak_pct,
       ROUND(h.worker_swap_p90_pct, 1)                            AS worker_swap_p90_pct,
       ROUND(h.worker_swap_peak_pct, 1)                           AS worker_swap_peak_pct,
       ROUND(h.driver_cpu_avg_pct, 1)                             AS driver_cpu_avg_pct,
       ROUND(h.driver_cpu_peak_pct, 1)                            AS driver_cpu_peak_pct,
       ROUND(h.driver_mem_peak_pct, 1)                            AS driver_mem_peak_pct,
       ROUND(h.network_sent_bytes / 1e9, 2)                       AS network_sent_gb,
       ROUND(h.network_received_bytes / 1e9, 2)                   AS network_received_gb,
       h.overlapping_task_runs,
       h.bottleneck_hint,
       -- why a task could not be judged (NULL when it could)
       CASE
         WHEN h.bottleneck_hint IS NOT NULL THEN NULL
         WHEN h.task_not_executed           THEN 'task_not_executed'
         WHEN h.cluster_id IS NULL          THEN 'no_cluster_recorded'
         WHEN h.node_minutes IS NULL        THEN 'no_node_timeline_rows'
         ELSE 'too_few_slices'
       END                                                        AS not_assessed_reason,
       -- deep links; all three are NULL without system.access (see caveats)
       CONCAT(w.base_url, '/jobs/', h.job_id, '/runs/', h.job_run_id) AS run_url,
       CONCAT(w.base_url, '/compute/clusters/', h.cluster_id)     AS cluster_url,
       CONCAT(w.base_url, '/compute/clusters/', h.cluster_id, '/sparkUi') AS spark_ui_url,
       -- status: worst-first band on the hint (field heuristics; every threshold is a header param).
       -- A task with no telemetry is NOT_ASSESSED - "could not look", never "nothing there".
       CASE
         WHEN h.bottleneck_hint IS NULL                                   THEN 'NOT_ASSESSED'
         WHEN h.bottleneck_hint = 'MEMORY_PRESSURE'                       THEN 'CRITICAL'
         WHEN h.bottleneck_hint IN ('SKEWED', 'IO_WAIT', 'DRIVER_BOUND', 'WAITING_OR_IDLE') THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM hinted h
LEFT JOIN latest_clusters c
  ON  h.workspace_id = c.workspace_id
  AND h.cluster_id   = c.cluster_id
LEFT JOIN latest_jobs j
  ON  h.workspace_id = j.workspace_id
  AND h.job_id       = j.job_id
LEFT JOIN ws w
  ON  h.workspace_id = w.workspace_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         task_hours DESC,
         workspace_id, job_id, job_run_id, task_key
LIMIT :top_n
