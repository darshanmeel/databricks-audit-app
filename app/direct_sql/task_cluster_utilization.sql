-- generated from dbt/models/databricks_direct/compute/d_task_cluster_utilization.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/compute/task_cluster_utilization.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH run_agg AS (
  SELECT workspace_id, job_id, run_id,
         MIN(period_start_time)    AS run_start,
         MAX(period_end_time)      AS last_seen,
         MAX(result_state)         AS result_state,       -- NULL until the run's end row lands
         -- end row only; NULL before Dec 2025 and 0 for every multi-task job (doc), so 0 = not reported
         NULLIF(MAX(run_duration_seconds), 0) AS run_s_reported
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -LEAST(__WINDOW_DAYS__, 90), __AS_OF_DATE__)
  GROUP BY workspace_id, job_id, run_id
),
run_obs AS (
  SELECT workspace_id, job_id, run_id,
         (result_state IS NULL) AS in_flight,
         COALESCE(run_s_reported,
                  timestampdiff(SECOND, run_start,
                                CASE WHEN result_state IS NULL THEN __AS_OF_TS__
                                     ELSE last_seen END)) AS run_s
  FROM run_agg
),
long_runs AS (
  -- the same run bound as lakeflow_long_running_runs, so the two queries compose
  SELECT workspace_id, job_id, run_id, in_flight, run_s
  FROM run_obs
  WHERE run_s >= 2 * 3600
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
  FROM `system`.`lakeflow`.`job_task_run_timeline` t
  JOIN long_runs r
    ON  r.workspace_id = t.workspace_id
    AND r.job_id       = t.job_id
    AND r.run_id       = t.job_run_id
  WHERE t.period_start_time >= dateadd(day, -LEAST(__WINDOW_DAYS__, 90), __AS_OF_DATE__)
  GROUP BY t.workspace_id, t.job_id, t.job_run_id, t.run_id, t.task_key
),
task_obs AS (
  SELECT workspace_id, job_id, job_run_id, task_run_id, task_key, task_start, task_result_state,
         cluster_id, compute_ids_count,
         -- a task that never ran: SKIPPED / BLOCKED, or the doc's zero-length row
         (task_result_state IN ('SKIPPED', 'BLOCKED')
          OR (task_result_state IS NOT NULL AND task_start = task_last_seen)) AS task_not_executed,
         -- the telemetry window: the task's observed wall clock, open-ended while in flight
         CASE WHEN task_result_state IS NULL THEN __AS_OF_TS__ ELSE task_last_seen END AS task_end,
         COALESCE(task_exec_s,
                  timestampdiff(SECOND, task_start,
                                CASE WHEN task_result_state IS NULL THEN __AS_OF_TS__
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
  JOIN `system`.`compute`.`node_timeline` n
    ON  n.workspace_id = t.workspace_id
    AND n.cluster_id   = t.cluster_id
    AND n.start_time   <  t.task_end
    AND n.end_time     >  t.task_start
  WHERE n.start_time >= dateadd(day, -LEAST(__WINDOW_DAYS__, 90), __AS_OF_DATE__)
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
         CASE WHEN MAX(result_state) IS NULL THEN __AS_OF_TS__
              ELSE MAX(period_end_time) END  AS t_end
  FROM `system`.`lakeflow`.`job_task_run_timeline`
  WHERE period_start_time >= dateadd(day, -LEAST(__WINDOW_DAYS__, 90), __AS_OF_DATE__)
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
  FROM `system`.`compute`.`clusters`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC
  ) = 1
),
latest_jobs AS (
  -- system.lakeflow.jobs is SCD2: one row per change, so take the newest per job
  SELECT workspace_id, job_id, name AS job_name
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
),
ws AS (
  -- workspace name + the base URL the deep links are built from (the stored URL ends in '/')
  SELECT workspace_id, workspace_name,
         regexp_replace(workspace_url, '/+$', '') AS base_url
  FROM `system`.`access`.`workspaces_latest`
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
           WHEN j.judged_minutes IS NULL OR j.judged_minutes < 60        THEN NULL
           WHEN j.eff_mem_p90 >= 85 OR COALESCE(j.eff_swap_p90, 0) >= 10 THEN 'MEMORY_PRESSURE'
           WHEN COALESCE(j.worker_cpu_spread_pct, 0) >= 40             THEN 'SKEWED'
           WHEN j.eff_cpu_p50 >= 70                                     THEN 'COMPUTE_BOUND'
           WHEN COALESCE(j.eff_cpu_wait, 0) >= 20                   THEN 'IO_WAIT'
           WHEN j.eff_cpu_avg < 20
            AND NOT j.single_node
            AND COALESCE(j.driver_cpu_avg_pct, 0) >= 70                 THEN 'DRIVER_BOUND'
           WHEN j.eff_cpu_avg < 20                                   THEN 'WAITING_OR_IDLE'
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
LIMIT 100000
) q
