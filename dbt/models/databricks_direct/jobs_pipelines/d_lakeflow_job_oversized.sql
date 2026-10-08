{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:jobs_pipelines', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/app/jobs_pipelines/lakeflow_job_oversized.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH task_runs AS (
  -- one row per TASK RUN starting in the window (run_id here is the task run id)
  SELECT workspace_id, job_id, job_run_id,
         run_id                              AS task_run_id,
         task_key,
         MIN(period_start_time)              AS task_start,
         MAX(period_end_time)                AS task_last_seen,
         MAX(result_state)                   AS task_result_state,
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
         CASE WHEN task_result_state IS NULL THEN {{ audit_now() }} ELSE task_last_seen END AS task_end
  FROM task_runs
),
job_task_agg AS (
  SELECT workspace_id, job_id,
         COUNT(DISTINCT CASE WHEN task_not_executed THEN NULL ELSE job_run_id END) AS runs_executed,
         COUNT(DISTINCT CASE WHEN task_not_executed THEN NULL
                             WHEN cluster_id IS NULL THEN NULL
                             ELSE job_run_id END)                                  AS runs_with_cluster
  FROM task_obs
  GROUP BY workspace_id, job_id
),
task_slices AS (
  -- every node-minute of each executed task run's cluster that overlaps that task run's window
  SELECT t.workspace_id, t.job_id, t.job_run_id, t.cluster_id,
         n.instance_id, n.start_time, n.driver,
         n.cpu_user_percent + n.cpu_system_percent AS cpu_pct,
         n.mem_used_percent, n.mem_swap_percent
  FROM task_obs t
  JOIN {{ source('system_compute', 'node_timeline') }} n
    ON  n.workspace_id = t.workspace_id
    AND n.cluster_id   = t.cluster_id
    AND n.start_time   <  t.task_end
    AND n.end_time     >  t.task_start
  WHERE n.start_time >= dateadd(day, -LEAST({{ w }}, 90), {{ audit_today() }})
    AND NOT t.task_not_executed
),
job_slices AS (
  -- the dedupe: parallel tasks of one run on one shared cluster count each node-minute once
  SELECT DISTINCT workspace_id, job_id, job_run_id, cluster_id, instance_id, start_time, driver,
         cpu_pct, mem_used_percent, mem_swap_percent
  FROM task_slices
),
profile AS (
  -- the job's pooled hardware profile over its deduplicated node-minutes
  SELECT workspace_id, job_id,
         COUNT(DISTINCT job_run_id)                                AS runs_with_telemetry,
         SUM(CASE WHEN driver THEN 0 ELSE 1 END)                   AS worker_minutes,
         SUM(CASE WHEN driver THEN 1 ELSE 0 END)                   AS driver_minutes,
         percentile(CASE WHEN driver THEN NULL ELSE cpu_pct END, 0.9)          AS worker_cpu_p90_pct,
         percentile(CASE WHEN driver THEN NULL ELSE mem_used_percent END, 0.9) AS worker_mem_p90_pct,
         MAX(CASE WHEN driver THEN NULL ELSE cpu_pct END)                      AS worker_cpu_peak_pct,
         MAX(CASE WHEN driver THEN NULL ELSE mem_used_percent END)             AS worker_mem_peak_pct,
         MAX(CASE WHEN driver THEN NULL ELSE mem_swap_percent END)             AS worker_swap_peak_pct
  FROM job_slices
  GROUP BY workspace_id, job_id
),
latest_clusters AS (
  -- system.compute.clusters is SCD2: the newest row per cluster (deleted clusters kept)
  SELECT workspace_id, cluster_id, cluster_source, worker_node_type, worker_count, max_autoscale_workers
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
latest_cluster AS (
  -- the classic cluster (one this query actually has node telemetry for) of the
  -- latest-starting task run (ties: cluster_id)
  SELECT t.workspace_id, t.job_id, t.cluster_id AS latest_cluster_id
  FROM task_obs t
  JOIN (SELECT DISTINCT workspace_id, job_id, cluster_id FROM job_slices) s
    ON  s.workspace_id = t.workspace_id
    AND s.job_id       = t.job_id
    AND s.cluster_id   = t.cluster_id
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY t.workspace_id, t.job_id ORDER BY t.task_start DESC, t.cluster_id
  ) = 1
),
run_starts AS (
  SELECT workspace_id, job_id, job_run_id, MIN(task_start) AS run_start
  FROM task_obs
  GROUP BY workspace_id, job_id, job_run_id
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
  -- the most workers running at once per run+cluster: churned or replaced instances never double-count
  SELECT workspace_id, job_id, job_run_id, cluster_id, MAX(workers_now) AS worker_nodes_peak
  FROM run_minute_workers
  GROUP BY workspace_id, job_id, job_run_id, cluster_id
),
worker_ref AS (
  -- the job's BUSIEST run+cluster (most workers running at once; ties: latest run, then
  -- cluster_id): workers configured vs seen for the suggested_action lever
  SELECT k.workspace_id, k.job_id,
         k.worker_nodes_peak                                AS worker_nodes_seen_max,
         COALESCE(c.max_autoscale_workers, c.worker_count)  AS workers_configured_max
  FROM run_peak k
  LEFT JOIN run_starts s
    ON  s.workspace_id = k.workspace_id AND s.job_id = k.job_id AND s.job_run_id = k.job_run_id
  LEFT JOIN latest_clusters c
    ON  c.workspace_id = k.workspace_id AND c.cluster_id = k.cluster_id
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY k.workspace_id, k.job_id
    ORDER BY k.worker_nodes_peak DESC, s.run_start DESC, k.cluster_id
  ) = 1
),
judged AS (
  SELECT a.workspace_id, a.job_id, a.runs_executed, a.runs_with_cluster,
         p.runs_with_telemetry, p.worker_cpu_p90_pct, p.worker_mem_p90_pct,
         p.worker_cpu_peak_pct, p.worker_mem_peak_pct, p.worker_swap_peak_pct,
         (COALESCE(p.worker_minutes, 0) = 0 AND COALESCE(p.driver_minutes, 0) > 0) AS single_node,
         lc.latest_cluster_id,
         w.worker_nodes_seen_max, w.workers_configured_max
  FROM job_task_agg a
  LEFT JOIN profile p
    ON  p.workspace_id = a.workspace_id AND p.job_id = a.job_id
  LEFT JOIN latest_cluster lc
    ON  lc.workspace_id = a.workspace_id AND lc.job_id = a.job_id
  LEFT JOIN worker_ref w
    ON  w.workspace_id = a.workspace_id AND w.job_id = a.job_id
),
sized AS (
  SELECT j.*,
         CASE
           WHEN j.runs_executed = 0                                                THEN NULL
           WHEN j.runs_with_cluster = 0                                            THEN NULL
           WHEN j.runs_with_telemetry IS NULL OR j.runs_with_telemetry < {{ param('lakeflow_job_oversized', 'min_runs', 3) }} THEN NULL
           WHEN j.single_node                                                      THEN FALSE
           -- memory on its peak and no swap: a smaller node must still hold the busiest minute
           ELSE (j.worker_cpu_p90_pct < {{ param('lakeflow_job_oversized', 'oversized_cpu_pct', 30) }} AND j.worker_mem_peak_pct < {{ param('lakeflow_job_oversized', 'oversized_mem_pct', 40) }}
                 AND COALESCE(j.worker_swap_peak_pct, 0) < {{ param('lakeflow_job_oversized', 'oversized_swap_pct', 1) }})
         END AS oversized
  FROM judged j
),
cost_usage AS (
  -- the exact per-run attribution and effective-list price join lakeflow_job_run_cost /
  -- lakeflow_job_cost_summary use (usage_metadata.job_id/job_run_id, DEC-66.1)
  SELECT u.workspace_id,
         u.usage_metadata.job_id     AS job_id,
         u.usage_metadata.job_run_id AS job_run_id,
         SUM(u.usage_quantity * lp.list_rate)                          AS net_run_list_cost,
         CASE
           WHEN SUM(CASE WHEN lp.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                         THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN lp.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END AS price_basis
  FROM {{ source('system_billing', 'usage') }} u
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective (post-promotion) list price - DEC-66.1
    FROM {{ list_prices() }} list_prices
  ) lp
    ON u.sku_name = lp.sku_name
   AND u.cloud    = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
    AND u.usage_date < {{ audit_today() }}
    AND upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.job_id IS NOT NULL
    AND u.usage_metadata.job_run_id IS NOT NULL
  GROUP BY u.workspace_id, u.usage_metadata.job_id, u.usage_metadata.job_run_id
),
job_cost AS (
  SELECT workspace_id, job_id,
         SUM(net_run_list_cost) AS job_list_cost_usd,
         CASE
           WHEN SUM(CASE WHEN price_basis = 'unpriced' THEN 1 ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN price_basis = 'priced' THEN 1 ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END AS price_basis
  FROM cost_usage
  GROUP BY workspace_id, job_id
),
priced AS (
  SELECT s.*, c.job_list_cost_usd, c.price_basis
  FROM sized s
  LEFT JOIN job_cost c
    ON  c.workspace_id = s.workspace_id AND c.job_id = s.job_id
),
savings AS (
  SELECT p.*,
         CASE
           WHEN p.oversized IS NULL THEN NULL
           WHEN NOT p.oversized THEN 0
           WHEN p.job_list_cost_usd IS NULL THEN NULL
           WHEN p.worker_cpu_p90_pct < {{ param('lakeflow_job_oversized', 'oversized_cpu_pct', 30) }} / 2.0
            AND p.worker_mem_peak_pct < {{ param('lakeflow_job_oversized', 'oversized_mem_pct', 40) }} / 2.0 THEN p.job_list_cost_usd * 0.5
           ELSE p.job_list_cost_usd * 0.25
         END AS est_saving_usd
  FROM priced p
)
SELECT sv.workspace_id,
       sv.job_id,
       jb.job_name,                                              -- NULL for submit/workflow runs
       sv.runs_with_telemetry,
       sv.single_node,
       sv.latest_cluster_id,
       lc2.cluster_source,
       lc2.worker_node_type,
       ROUND(nm.memory_mb / 1024.0, 1)                           AS worker_memory_gb,
       nm.core_count                                              AS worker_cores,
       sv.workers_configured_max,
       sv.worker_nodes_seen_max,
       ROUND(sv.worker_cpu_p90_pct, 1)                           AS worker_cpu_p90_pct,
       ROUND(sv.worker_mem_p90_pct, 1)                           AS worker_mem_p90_pct,
       ROUND(sv.worker_cpu_peak_pct, 1)                          AS worker_cpu_peak_pct,
       ROUND(sv.worker_mem_peak_pct, 1)                          AS worker_mem_peak_pct,
       ROUND(sv.worker_swap_peak_pct, 1)                         AS worker_swap_peak_pct,
       sv.oversized,
       ROUND(sv.job_list_cost_usd, 2)                            AS job_list_cost_usd,
       sv.price_basis,
       ROUND(sv.est_saving_usd, 2)                               AS est_saving_usd_list,
       CASE
         WHEN sv.oversized IS NOT TRUE THEN NULL
         WHEN sv.worker_nodes_seen_max IS NOT NULL AND sv.workers_configured_max IS NOT NULL
          AND sv.worker_nodes_seen_max < sv.workers_configured_max THEN 'fewer workers / lower autoscale max'
         ELSE 'one node size down'
       END                                                        AS suggested_action,
       CASE
         WHEN sv.oversized IS NOT NULL             THEN NULL
         WHEN sv.runs_executed = 0                 THEN 'task_not_executed'
         WHEN sv.runs_with_cluster = 0              THEN 'no_cluster_recorded'
         WHEN sv.runs_with_telemetry IS NULL
           OR sv.runs_with_telemetry = 0            THEN 'no_node_timeline_rows'
         ELSE 'too_few_runs'
       END                                                        AS not_assessed_reason,
       CASE
         WHEN sv.oversized IS NULL                       THEN 'NOT_ASSESSED'
         WHEN COALESCE(sv.est_saving_usd, 0) >= {{ param('lakeflow_job_oversized', 'crit_saving_usd', 200) }} THEN 'CRITICAL'
         WHEN COALESCE(sv.est_saving_usd, 0) >= {{ param('lakeflow_job_oversized', 'warn_saving_usd', 25) }} THEN 'WARN'
         ELSE 'OK'
       END                                                        AS status
FROM savings sv
LEFT JOIN latest_jobs jb
  ON  jb.workspace_id = sv.workspace_id AND jb.job_id = sv.job_id
LEFT JOIN latest_clusters lc2
  ON  lc2.workspace_id = sv.workspace_id AND lc2.cluster_id = sv.latest_cluster_id
LEFT JOIN node_mem nm
  ON  nm.node_type = lc2.worker_node_type
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         est_saving_usd_list DESC NULLS LAST,
         workspace_id, job_id
LIMIT {{ param('lakeflow_job_oversized', 'top_n', 100000) }}
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
