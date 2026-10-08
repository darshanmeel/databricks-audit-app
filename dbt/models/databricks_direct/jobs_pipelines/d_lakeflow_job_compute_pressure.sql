{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:jobs_pipelines', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/app/jobs_pipelines/lakeflow_job_compute_pressure.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH task_runs AS (
  -- one row per TASK RUN starting in the window, any duration (run_id here is the task run id)
  SELECT workspace_id, job_id, job_run_id,
         run_id                              AS task_run_id,
         task_key,
         MIN(period_start_time)              AS task_start,
         MAX(period_end_time)                AS task_last_seen,
         MAX(result_state)                   AS task_result_state,
         NULLIF(MAX(execution_duration_seconds), 0) AS task_exec_s,
         -- first compute id of the task; NULL on serverless and on pre-Dec-2025 rows
         MAX(try_element_at(compute_ids, 1)) AS cluster_id
  FROM {{ source('system_lakeflow', 'job_task_run_timeline') }}
  WHERE period_start_time >= dateadd(day, -LEAST({{ w }}, 90), {{ audit_today() }})
  GROUP BY workspace_id, job_id, job_run_id, run_id, task_key
),
task_obs AS (
  SELECT workspace_id, job_id, job_run_id, task_run_id, task_key, task_start, cluster_id,
         -- a task that never ran: SKIPPED / BLOCKED, or the doc's zero-length row
         (task_result_state IN ('SKIPPED', 'BLOCKED')
          OR (task_result_state IS NOT NULL AND task_start = task_last_seen)) AS task_not_executed,
         -- the telemetry window: the task's observed wall clock, open-ended while in flight
         CASE WHEN task_result_state IS NULL THEN {{ audit_now() }} ELSE task_last_seen END AS task_end,
         COALESCE(task_exec_s,
                  timestampdiff(SECOND, task_start,
                                CASE WHEN task_result_state IS NULL THEN {{ audit_now() }}
                                     ELSE task_last_seen END)) AS task_s
  FROM task_runs
),
task_slices AS (
  -- every node-minute of each executed task run's cluster that overlaps that task run's window
  SELECT t.workspace_id, t.job_id, t.job_run_id, t.task_key, t.cluster_id,
         n.instance_id, n.start_time, n.driver,
         n.cpu_user_percent + n.cpu_system_percent AS cpu_pct,
         n.cpu_wait_percent, n.mem_used_percent, n.mem_swap_percent,
         n.network_received_bytes
  FROM task_obs t
  JOIN {{ source('system_compute', 'node_timeline') }} n
    ON  n.workspace_id = t.workspace_id
    AND n.cluster_id   = t.cluster_id
    AND n.start_time   <  t.task_end
    AND n.end_time     >  t.task_start
  WHERE n.start_time >= dateadd(day, -LEAST({{ w }}, 90), {{ audit_today() }})
    AND CASE WHEN t.task_not_executed THEN 0 ELSE 1 END = 1
),
job_slices AS (
  -- the dedupe: parallel tasks of one run on one shared cluster count each node-minute once
  SELECT DISTINCT workspace_id, job_id, job_run_id, cluster_id, instance_id, start_time, driver,
         cpu_pct, cpu_wait_percent, mem_used_percent, mem_swap_percent, network_received_bytes
  FROM task_slices
),
latest_clusters AS (
  -- system.compute.clusters is SCD2: the newest row per cluster (deleted clusters kept)
  SELECT workspace_id, cluster_id, cluster_source, driver_node_type, worker_node_type,
         worker_count, max_autoscale_workers
  FROM {{ source('system_compute', 'clusters') }}
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC
  ) = 1
),
latest_jobs AS (
  -- system.lakeflow.jobs is SCD2: one row per change, so take the newest per job
  SELECT workspace_id, job_id, name AS job_name
  FROM {{ source('system_lakeflow', 'jobs') }}
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
),
node_mem AS (
  -- memory and cores per node type (account-level reference table)
  SELECT node_type, MAX(memory_mb) AS memory_mb, MAX(core_count) AS core_count
  FROM {{ source('system_compute', 'node_types') }}
  GROUP BY node_type
),
run_starts AS (
  -- per run: its first task start and how many distinct clusters its task runs recorded
  SELECT workspace_id, job_id, job_run_id,
         MIN(task_start)            AS run_start,
         COUNT(DISTINCT cluster_id) AS run_clusters
  FROM task_obs
  GROUP BY workspace_id, job_id, job_run_id
),
run_nodes AS (
  -- one row per node of each run's cluster, to count workers and measure skew
  SELECT workspace_id, job_id, job_run_id, cluster_id, instance_id, driver,
         AVG(cpu_pct) AS node_cpu_avg
  FROM job_slices
  GROUP BY workspace_id, job_id, job_run_id, cluster_id, instance_id, driver
),
run_minutes AS (
  -- each deduplicated node-minute on its minute boundary, to count the workers running at once
  SELECT workspace_id, job_id, job_run_id, cluster_id, instance_id, driver,
         date_trunc('MINUTE', start_time) AS minute_start
  FROM job_slices
),
run_minute_workers AS (
  SELECT workspace_id, job_id, job_run_id, cluster_id, minute_start,
         COUNT(DISTINCT CASE WHEN driver THEN NULL ELSE instance_id END) AS workers_now
  FROM run_minutes
  GROUP BY workspace_id, job_id, job_run_id, cluster_id, minute_start
),
run_peak AS (
  -- the most workers running at once: churned or replaced instances are never counted twice
  SELECT workspace_id, job_id, job_run_id, cluster_id, MAX(workers_now) AS worker_nodes_peak
  FROM run_minute_workers
  GROUP BY workspace_id, job_id, job_run_id, cluster_id
),
run_cpu AS (
  -- the (run, cluster)'s own worker CPU p50: only a CPU-bound cluster can make the job at_ceiling
  SELECT workspace_id, job_id, job_run_id, cluster_id,
         percentile(CASE WHEN driver THEN NULL ELSE cpu_pct END, 0.5) AS pair_cpu_p50
  FROM job_slices
  GROUP BY workspace_id, job_id, job_run_id, cluster_id
),
run_cluster_agg AS (
  SELECT n.workspace_id, n.job_id, n.job_run_id, n.cluster_id,
         MAX(s.run_start)                                       AS run_start,
         MAX(k.worker_nodes_peak)                               AS worker_nodes_seen,
         MAX(u.pair_cpu_p50)                                    AS pair_cpu_p50,
         MAX(CASE WHEN n.driver THEN NULL ELSE n.node_cpu_avg END)
           - MIN(CASE WHEN n.driver THEN NULL ELSE n.node_cpu_avg END) AS worker_cpu_spread_pct,
         -- the ceiling autoscale can reach, else the fixed size (latest SCD2 shape)
         MAX(COALESCE(c.max_autoscale_workers, c.worker_count)) AS workers_configured
  FROM run_nodes n
  LEFT JOIN latest_clusters c
    ON  c.workspace_id = n.workspace_id
    AND c.cluster_id   = n.cluster_id
  LEFT JOIN run_starts s
    ON  s.workspace_id = n.workspace_id
    AND s.job_id       = n.job_id
    AND s.job_run_id   = n.job_run_id
  LEFT JOIN run_peak k
    ON  k.workspace_id = n.workspace_id
    AND k.job_id       = n.job_id
    AND k.job_run_id   = n.job_run_id
    AND k.cluster_id   = n.cluster_id
  LEFT JOIN run_cpu u
    ON  u.workspace_id = n.workspace_id
    AND u.job_id       = n.job_id
    AND u.job_run_id   = n.job_run_id
    AND u.cluster_id   = n.cluster_id
  GROUP BY n.workspace_id, n.job_id, n.job_run_id, n.cluster_id
),
run_cluster AS (
  -- a single-node cluster (0 workers configured) has no worker ceiling to reach: NULL, not TRUE
  SELECT workspace_id, job_id, job_run_id, cluster_id, run_start, worker_nodes_seen,
         worker_cpu_spread_pct, workers_configured,
         CASE WHEN workers_configured IS NULL OR workers_configured = 0 THEN NULL
              ELSE worker_nodes_seen >= workers_configured END AS hit_ceiling,
         -- 1 = this run's cluster was itself CPU-bound (its own worker CPU p50 at/above {{ param('lakeflow_job_compute_pressure', 'busy_cpu_pct', 70) }})
         CASE WHEN pair_cpu_p50 >= {{ param('lakeflow_job_compute_pressure', 'busy_cpu_pct', 70) }} THEN 1 ELSE 0 END AS cpu_bound,
         -- 1 = this run's cluster was CPU-bound AND at its ceiling: the case with a scale-out lever
         CASE WHEN workers_configured IS NULL OR workers_configured = 0 THEN 0
              WHEN worker_nodes_seen >= workers_configured
               AND pair_cpu_p50 >= {{ param('lakeflow_job_compute_pressure', 'busy_cpu_pct', 70) }} THEN 1
              ELSE 0 END                                AS sets_ceiling
  FROM run_cluster_agg
),
ceiling AS (
  -- per job: did ANY CPU-bound run's cluster reach its configured maximum (NULL = no ceiling known)
  SELECT workspace_id, job_id,
         CASE WHEN MAX(CASE WHEN hit_ceiling IS NULL THEN 0 ELSE 1 END) = 0 THEN NULL
              ELSE MAX(sets_ceiling) = 1
         END                        AS at_ceiling,
         MAX(worker_cpu_spread_pct) AS worker_cpu_spread_pct
  FROM run_cluster
  GROUP BY workspace_id, job_id
),
ceiling_ref AS (
  -- the reference (run, cluster) for the reason text: the one that set at_ceiling, else a
  -- CPU-bound one, else any - the most workers running at once first. A light cluster at its fixed
  -- size must never stand in for the CPU-bound cluster the verdict is about.
  SELECT workspace_id, job_id,
         worker_nodes_seen  AS worker_nodes_seen_max,
         workers_configured AS workers_configured_max
  FROM run_cluster
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id
    ORDER BY sets_ceiling DESC, cpu_bound DESC, worker_nodes_seen DESC, run_start DESC, cluster_id
  ) = 1
),
profile AS (
  -- the job's pooled hardware profile over its deduplicated node-minutes
  SELECT workspace_id, job_id,
         COUNT(DISTINCT job_run_id)                                AS runs_with_telemetry,
         COUNT(*)                                                  AS node_minutes,
         SUM(CASE WHEN driver THEN 0 ELSE 1 END)                   AS worker_minutes,
         SUM(CASE WHEN driver THEN 1 ELSE 0 END)                   AS driver_minutes,
         AVG(CASE WHEN driver THEN NULL ELSE cpu_pct END)          AS worker_cpu_avg_pct,
         percentile(CASE WHEN driver THEN NULL ELSE cpu_pct END, 0.5) AS worker_cpu_p50_pct,
         percentile(CASE WHEN driver THEN NULL ELSE cpu_pct END, 0.9) AS worker_cpu_p90_pct,
         AVG(CASE WHEN driver THEN NULL ELSE cpu_wait_percent END) AS worker_cpu_wait_avg_pct,
         AVG(CASE WHEN driver THEN NULL ELSE mem_used_percent END) AS worker_mem_avg_pct,
         percentile(CASE WHEN driver THEN NULL ELSE mem_used_percent END, 0.9) AS worker_mem_p90_pct,
         MAX(CASE WHEN driver THEN NULL ELSE mem_used_percent END) AS worker_mem_peak_pct,
         percentile(CASE WHEN driver THEN NULL ELSE mem_swap_percent END, 0.9) AS worker_swap_p90_pct,
         MAX(CASE WHEN driver THEN NULL ELSE mem_swap_percent END) AS worker_swap_peak_pct,
         AVG(CASE WHEN driver THEN cpu_pct END)                    AS driver_cpu_avg_pct,
         percentile(CASE WHEN driver THEN mem_used_percent END, 0.9) AS driver_mem_p90_pct,
         percentile(CASE WHEN driver THEN mem_swap_percent END, 0.9) AS driver_swap_p90_pct,
         SUM(network_received_bytes)                               AS network_received_bytes
  FROM job_slices
  GROUP BY workspace_id, job_id
),
driver_load AS (
  -- the drivers the DRIVER rule may judge: those of run clusters that had workers. A single-node
  -- cluster's only node is doing its own work (task_cluster_utilization's NOT single_node guard,
  -- which it applies per task run); pooled per job it would read as a busy driver over idle workers.
  SELECT s.workspace_id, s.job_id,
         AVG(CASE WHEN s.driver AND k.worker_nodes_peak > 0 THEN s.cpu_pct END) AS driver_cpu_avg_with_workers_pct
  FROM job_slices s
  LEFT JOIN run_peak k
    ON  k.workspace_id = s.workspace_id
    AND k.job_id       = s.job_id
    AND k.job_run_id   = s.job_run_id
    AND k.cluster_id   = s.cluster_id
  GROUP BY s.workspace_id, s.job_id
),
task_hours AS (
  SELECT workspace_id, job_id, task_key, SUM(task_s) AS task_key_s
  FROM task_obs
  GROUP BY workspace_id, job_id, task_key
),
task_heat AS (
  -- per task_key and NOT deduplicated: where the worker memory was high
  SELECT s.workspace_id, s.job_id, s.task_key,
         percentile(CASE WHEN s.driver THEN NULL ELSE s.mem_used_percent END, 0.9) AS task_mem_p90,
         MAX(h.task_key_s) AS task_key_s
  FROM task_slices s
  LEFT JOIN task_hours h
    ON  h.workspace_id = s.workspace_id
    AND h.job_id       = s.job_id
    AND h.task_key     = s.task_key
  GROUP BY s.workspace_id, s.job_id, s.task_key
),
hottest AS (
  -- NULL when no task's windows had worker memory (a single-node job): nothing to rank tasks by
  SELECT workspace_id, job_id,
         CASE WHEN task_mem_p90 IS NULL THEN NULL ELSE task_key END AS hottest_task_key,
         task_mem_p90 AS hottest_task_mem_p90_pct
  FROM task_heat
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY task_mem_p90 DESC, task_key_s DESC, task_key
  ) = 1
),
run_roll AS (
  SELECT t.workspace_id, t.job_id,
         COUNT(DISTINCT t.job_run_id)                                   AS runs_seen,
         COUNT(DISTINCT CASE WHEN t.task_not_executed THEN NULL
                             ELSE t.job_run_id END)                     AS runs_executed,
         COUNT(DISTINCT CASE WHEN t.task_not_executed THEN NULL
                             WHEN t.cluster_id IS NULL THEN NULL
                             ELSE t.job_run_id END)                     AS runs_with_cluster,
         COUNT(DISTINCT t.task_key)                                     AS task_keys_seen,
         SUM(t.task_s)                                                  AS task_s_total,
         COUNT(DISTINCT t.cluster_id)                                   AS clusters_seen,
         -- over the clusters used; NULL when none of them has a configuration row
         CASE WHEN MAX(CASE WHEN c.cluster_id IS NULL THEN 0 ELSE 1 END) = 0 THEN NULL
              ELSE MAX(CASE WHEN c.cluster_source IN ('UI', 'API') THEN 1 ELSE 0 END) = 1
         END                                                            AS on_all_purpose
  FROM task_obs t
  LEFT JOIN latest_clusters c
    ON  c.workspace_id = t.workspace_id
    AND c.cluster_id   = t.cluster_id
  GROUP BY t.workspace_id, t.job_id
),
run_last AS (
  SELECT workspace_id, job_id,
         MAX(run_start)    AS last_run_start,
         MAX(run_clusters) AS max_clusters_per_run
  FROM run_starts
  GROUP BY workspace_id, job_id
),
job_telemetry_clusters AS (
  SELECT DISTINCT workspace_id, job_id, cluster_id
  FROM job_slices
),
job_classic AS (
  -- the compute ids of the job that are classic clusters: a configuration row or node telemetry
  -- (a SQL task records its warehouse id in compute_ids, which has neither)
  SELECT DISTINCT t.workspace_id, t.job_id, t.cluster_id
  FROM task_obs t
  LEFT JOIN latest_clusters c
    ON  c.workspace_id = t.workspace_id
    AND c.cluster_id   = t.cluster_id
  LEFT JOIN job_telemetry_clusters s
    ON  s.workspace_id = t.workspace_id
    AND s.job_id       = t.job_id
    AND s.cluster_id   = t.cluster_id
  WHERE t.cluster_id IS NOT NULL
    AND (c.cluster_id IS NOT NULL OR s.cluster_id IS NOT NULL)
),
latest_cluster AS (
  -- the classic cluster of the latest-starting task run that recorded one, else any recorded id
  -- (ties: cluster_id)
  SELECT t.workspace_id, t.job_id, t.cluster_id AS latest_cluster_id
  FROM task_obs t
  LEFT JOIN job_classic k
    ON  k.workspace_id = t.workspace_id
    AND k.job_id       = t.job_id
    AND k.cluster_id   = t.cluster_id
  WHERE t.cluster_id IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY t.workspace_id, t.job_id
    ORDER BY CASE WHEN k.cluster_id IS NULL THEN 1 ELSE 0 END, t.task_start DESC, t.cluster_id
  ) = 1
),
judged AS (
  SELECT r.*,
         l.last_run_start, l.max_clusters_per_run,
         lc.latest_cluster_id,
         p.runs_with_telemetry, p.node_minutes, p.worker_minutes, p.driver_minutes,
         p.worker_cpu_avg_pct, p.worker_cpu_p50_pct, p.worker_cpu_p90_pct,
         p.worker_cpu_wait_avg_pct, p.worker_mem_avg_pct, p.worker_mem_p90_pct,
         p.worker_mem_peak_pct, p.worker_swap_p90_pct, p.worker_swap_peak_pct,
         p.driver_cpu_avg_pct, p.driver_mem_p90_pct, p.driver_swap_p90_pct,
         p.network_received_bytes,
         g.at_ceiling, g.worker_cpu_spread_pct,
         f.worker_nodes_seen_max, f.workers_configured_max,
         h.hottest_task_key, h.hottest_task_mem_p90_pct,
         d.driver_cpu_avg_with_workers_pct,
         (COALESCE(p.worker_minutes, 0) = 0 AND COALESCE(p.driver_minutes, 0) > 0) AS single_node,
         -- the numbers the verdict is judged on: the workers', or the driver's on a single-node cluster
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_minutes ELSE p.driver_minutes END AS judged_minutes,
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_cpu_avg_pct ELSE p.driver_cpu_avg_pct END AS eff_cpu_avg,
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_cpu_p50_pct ELSE p.driver_cpu_avg_pct END AS eff_cpu_p50,
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_mem_p90_pct ELSE p.driver_mem_p90_pct END AS eff_mem_p90,
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_swap_p90_pct ELSE p.driver_swap_p90_pct END AS eff_swap_p90,
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_cpu_wait_avg_pct ELSE NULL END AS eff_cpu_wait
  FROM run_roll r
  LEFT JOIN run_last l
    ON  l.workspace_id = r.workspace_id
    AND l.job_id       = r.job_id
  LEFT JOIN latest_cluster lc
    ON  lc.workspace_id = r.workspace_id
    AND lc.job_id       = r.job_id
  LEFT JOIN profile p
    ON  p.workspace_id = r.workspace_id
    AND p.job_id       = r.job_id
  LEFT JOIN ceiling g
    ON  g.workspace_id = r.workspace_id
    AND g.job_id       = r.job_id
  LEFT JOIN ceiling_ref f
    ON  f.workspace_id = r.workspace_id
    AND f.job_id       = r.job_id
  LEFT JOIN hottest h
    ON  h.workspace_id = r.workspace_id
    AND h.job_id       = r.job_id
  LEFT JOIN driver_load d
    ON  d.workspace_id = r.workspace_id
    AND d.job_id       = r.job_id
),
hinted AS (
  SELECT j.*,
         -- machine-level inference, in this precedence (field heuristics; see caveats)
         CASE
           WHEN j.runs_executed = 0                                                   THEN NULL
           WHEN j.judged_minutes IS NULL OR j.judged_minutes < {{ param('lakeflow_job_compute_pressure', 'min_slices', 60) }}            THEN NULL
           WHEN j.eff_mem_p90 >= {{ param('lakeflow_job_compute_pressure', 'crit_mem_pct', 85) }}
             OR COALESCE(j.eff_swap_p90, 0) >= {{ param('lakeflow_job_compute_pressure', 'crit_swap_pct', 10) }}                        THEN 'MEMORY'
           WHEN COALESCE(j.worker_cpu_spread_pct, 0) >= {{ param('lakeflow_job_compute_pressure', 'skew_gap_pct', 40) }}                 THEN 'SKEW'
           WHEN j.eff_cpu_p50 >= {{ param('lakeflow_job_compute_pressure', 'busy_cpu_pct', 70) }}                                         THEN 'CPU'
           WHEN COALESCE(j.eff_cpu_wait, 0) >= {{ param('lakeflow_job_compute_pressure', 'warn_io_wait_pct', 20) }}                       THEN 'IO_WAIT'
           WHEN j.eff_cpu_avg < {{ param('lakeflow_job_compute_pressure', 'waiting_cpu_pct', 20) }}
            AND NOT j.single_node
            AND COALESCE(j.driver_cpu_avg_with_workers_pct, 0) >= {{ param('lakeflow_job_compute_pressure', 'busy_cpu_pct', 70) }}        THEN 'DRIVER'
           WHEN j.eff_cpu_avg < {{ param('lakeflow_job_compute_pressure', 'waiting_cpu_pct', 20) }}                                       THEN 'IDLE'
           ELSE 'NONE'
         END AS pressure
  FROM judged j
)
SELECT h.workspace_id,
       h.job_id,
       j.job_name,                                                -- NULL for submit/workflow runs
       h.runs_seen,
       COALESCE(h.runs_with_telemetry, 0)                         AS runs_with_telemetry,
       h.task_keys_seen,
       ROUND(h.task_s_total / 3600.0, 2)                          AS task_hours_total,
       h.last_run_start,
       h.clusters_seen,
       h.max_clusters_per_run,
       h.on_all_purpose,
       h.latest_cluster_id,
       c.cluster_source,
       c.worker_node_type,
       c.driver_node_type,
       ROUND(m.memory_mb / 1024.0, 1)                             AS worker_memory_gb,
       m.core_count                                               AS worker_cores,
       h.workers_configured_max,
       h.worker_nodes_seen_max,
       h.at_ceiling,
       h.single_node,
       h.node_minutes,
       h.worker_minutes,
       ROUND(h.worker_cpu_avg_pct, 1)                             AS worker_cpu_avg_pct,
       ROUND(h.worker_cpu_p50_pct, 1)                             AS worker_cpu_p50_pct,
       ROUND(h.worker_cpu_p90_pct, 1)                             AS worker_cpu_p90_pct,
       ROUND(h.worker_cpu_spread_pct, 1)                          AS worker_cpu_spread_pct,
       ROUND(h.worker_cpu_wait_avg_pct, 1)                        AS worker_cpu_wait_avg_pct,
       ROUND(h.worker_mem_avg_pct, 1)                             AS worker_mem_avg_pct,
       ROUND(h.worker_mem_p90_pct, 1)                             AS worker_mem_p90_pct,
       ROUND(h.worker_mem_peak_pct, 1)                            AS worker_mem_peak_pct,
       ROUND(h.worker_swap_p90_pct, 1)                            AS worker_swap_p90_pct,
       ROUND(h.worker_swap_peak_pct, 1)                           AS worker_swap_peak_pct,
       ROUND(h.driver_cpu_avg_pct, 1)                             AS driver_cpu_avg_pct,
       ROUND(h.driver_mem_p90_pct, 1)                             AS driver_mem_p90_pct,
       ROUND(h.network_received_bytes / 1e9, 2)                   AS network_received_gb,
       h.hottest_task_key,
       ROUND(h.hottest_task_mem_p90_pct, 1)                       AS hottest_task_mem_p90_pct,
       h.pressure,
       -- the lever: one enum, its words written once in the header's actions ladder
       CASE
         WHEN h.pressure IS NULL                  THEN NULL
         WHEN h.pressure = 'MEMORY'               THEN 'SCALE_UP_MEMORY'
         WHEN h.pressure = 'CPU' AND h.at_ceiling THEN 'SCALE_OUT'
         WHEN h.pressure = 'SKEW'                 THEN 'FIX_SKEW'
         WHEN h.pressure = 'DRIVER'               THEN 'DISTRIBUTE_WORK'
         WHEN h.pressure = 'IO_WAIT'              THEN 'FIX_IO'
         WHEN h.pressure = 'IDLE'                 THEN 'SCALE_DOWN'
         ELSE 'NONE'
       END AS scaling_hint,
       -- the WHY, built from the row's own numbers (whole-number percentages). Every figure is
       -- COALESCEd to 'n/a' as a string: Databricks' CONCAT returns NULL when any argument is NULL
       -- (DuckDB's skips it), and a judged row must always carry its reason. A single-node job is
       -- judged on the driver's numbers, so its reason names them as the driver's.
       CASE
         WHEN h.pressure IS NULL THEN NULL
         ELSE CONCAT(
           CASE h.pressure
             WHEN 'MEMORY' THEN CONCAT(CASE WHEN h.single_node THEN 'driver memory p90 '
                                            ELSE 'worker memory p90 ' END,
                                       COALESCE(CAST(CAST(ROUND(h.eff_mem_p90, 0) AS BIGINT) AS STRING), 'n/a'),
                                       '% (threshold ', {{ param('lakeflow_job_compute_pressure', 'crit_mem_pct', 85) }}, '%), swap p90 ',
                                       CAST(CAST(ROUND(COALESCE(h.eff_swap_p90, 0), 0) AS BIGINT) AS STRING),
                                       '% (threshold ', {{ param('lakeflow_job_compute_pressure', 'crit_swap_pct', 10) }}, '%)',
                                       CASE WHEN h.hottest_task_mem_p90_pct IS NULL THEN ''
                                            ELSE CONCAT('; hottest task ', COALESCE(h.hottest_task_key, 'n/a'))
                                       END)
             WHEN 'CPU' THEN CASE
               WHEN h.single_node THEN CONCAT('driver CPU avg ',
                                              COALESCE(CAST(CAST(ROUND(h.eff_cpu_p50, 0) AS BIGINT) AS STRING), 'n/a'),
                                              '% (threshold ', {{ param('lakeflow_job_compute_pressure', 'busy_cpu_pct', 70) }}, '%) on a single-node cluster')
               ELSE CONCAT('worker CPU p50 ',
                           COALESCE(CAST(CAST(ROUND(h.eff_cpu_p50, 0) AS BIGINT) AS STRING), 'n/a'),
                           '% (threshold ', {{ param('lakeflow_job_compute_pressure', 'busy_cpu_pct', 70) }}, '%), up to ',
                           COALESCE(CAST(h.worker_nodes_seen_max AS STRING), 'n/a'),
                           CASE WHEN h.workers_configured_max IS NULL OR h.workers_configured_max = 0 THEN ''
                                ELSE CONCAT(' of ', CAST(h.workers_configured_max AS STRING)) END,
                           ' workers seen',
                           -- keyed on the quoted cluster's own configuration first, so the ending
                           -- never calls a cluster with an unknown or zero ceiling "headroom"
                           CASE WHEN h.workers_configured_max IS NULL THEN ' -- cluster config not found'
                                WHEN h.workers_configured_max = 0 THEN ' -- no worker ceiling configured'
                                WHEN h.at_ceiling THEN ' -- at ceiling'
                                ELSE ' -- headroom' END)
             END
             WHEN 'SKEW' THEN CONCAT('hottest-coldest worker CPU gap ',
                                     COALESCE(CAST(CAST(ROUND(h.worker_cpu_spread_pct, 0) AS BIGINT) AS STRING), 'n/a'),
                                     '% in the worst run (threshold ', {{ param('lakeflow_job_compute_pressure', 'skew_gap_pct', 40) }}, '%)')
             WHEN 'DRIVER' THEN CONCAT('driver CPU ',
                                       COALESCE(CAST(CAST(ROUND(h.driver_cpu_avg_with_workers_pct, 0) AS BIGINT) AS STRING), 'n/a'),
                                       '% while workers averaged ',
                                       COALESCE(CAST(CAST(ROUND(h.eff_cpu_avg, 0) AS BIGINT) AS STRING), 'n/a'), '%')
             WHEN 'IO_WAIT' THEN CONCAT('worker CPU wait ',
                                        COALESCE(CAST(CAST(ROUND(h.eff_cpu_wait, 0) AS BIGINT) AS STRING), 'n/a'),
                                        '% (threshold ', {{ param('lakeflow_job_compute_pressure', 'warn_io_wait_pct', 20) }}, '%)')
             WHEN 'IDLE' THEN CONCAT(CASE WHEN h.single_node THEN 'the driver averaged '
                                          ELSE 'workers averaged ' END,
                                     COALESCE(CAST(CAST(ROUND(h.eff_cpu_avg, 0) AS BIGINT) AS STRING), 'n/a'),
                                     '% CPU (threshold ', {{ param('lakeflow_job_compute_pressure', 'waiting_cpu_pct', 20) }}, '%)')
             ELSE CONCAT('no threshold crossed: ',
                         CASE WHEN h.single_node THEN 'driver CPU avg ' ELSE 'CPU p50 ' END,
                         COALESCE(CAST(CAST(ROUND(h.eff_cpu_p50, 0) AS BIGINT) AS STRING), 'n/a'),
                         CASE WHEN h.single_node THEN '%, driver memory p90 ' ELSE '%, memory p90 ' END,
                         COALESCE(CAST(CAST(ROUND(h.eff_mem_p90, 0) AS BIGINT) AS STRING), 'n/a'),
                         '%, swap p90 ',
                         CAST(CAST(ROUND(COALESCE(h.eff_swap_p90, 0), 0) AS BIGINT) AS STRING), '%')
           END,
           CASE WHEN h.on_all_purpose THEN ' -- on an all-purpose cluster shared with other work'
                ELSE '' END)
       END AS pressure_reason,
       -- why a job could not be judged (NULL when it could)
       CASE
         WHEN h.pressure IS NOT NULL                        THEN NULL
         WHEN h.runs_executed = 0                           THEN 'task_not_executed'
         WHEN h.runs_with_cluster = 0                       THEN 'no_cluster_recorded'
         WHEN h.node_minutes IS NULL OR h.node_minutes = 0  THEN 'no_node_timeline_rows'
         ELSE 'too_few_slices'
       END AS not_assessed_reason,
       -- status: a WARN must have a lever, so CPU-bound with headroom is OK.
       -- A job with no telemetry is NOT_ASSESSED - "could not look", never "nothing there".
       CASE
         WHEN h.pressure IS NULL                                  THEN 'NOT_ASSESSED'
         WHEN h.pressure = 'MEMORY'                               THEN 'CRITICAL'
         WHEN h.pressure IN ('SKEW', 'DRIVER', 'IO_WAIT', 'IDLE') THEN 'WARN'
         WHEN h.pressure = 'CPU' AND h.at_ceiling                 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM hinted h
LEFT JOIN latest_clusters c
  ON  h.workspace_id      = c.workspace_id
  AND h.latest_cluster_id = c.cluster_id
LEFT JOIN node_mem m
  ON  c.worker_node_type = m.node_type
LEFT JOIN latest_jobs j
  ON  h.workspace_id = j.workspace_id
  AND h.job_id       = j.job_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         task_hours_total DESC,
         workspace_id, job_id
LIMIT {{ param('lakeflow_job_compute_pressure', 'top_n', 100000) }}
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
