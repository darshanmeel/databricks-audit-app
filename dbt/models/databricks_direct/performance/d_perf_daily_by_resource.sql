{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:performance', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/app/performance/perf_daily_by_resource.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH stmts AS (
  SELECT 'warehouse' AS resource_type, workspace_id, compute.warehouse_id AS resource_id, date(start_time) AS usage_date,
         1 AS runs,
         CASE WHEN execution_status = 'FAILED' THEN 1 ELSE 0 END AS failed,
         COALESCE(total_duration_ms, 0) / 1000.0 AS total_s,
         (COALESCE(waiting_at_capacity_duration_ms, 0) + COALESCE(waiting_for_compute_duration_ms, 0)) / 1000.0 AS queued_s,
         COALESCE(execution_duration_ms, 0) / 1000.0 AS exec_s,
         COALESCE(spilled_local_bytes, 0) / 1e9 AS spilled_gb,
         0 AS task_runs, CAST(0 AS DOUBLE) AS setup_s,
         COALESCE(waiting_at_capacity_duration_ms, 0) / 1000.0 AS slot_wait_s,
         COALESCE(waiting_for_compute_duration_ms, 0) / 1000.0 AS provision_s,
         COALESCE(shuffle_read_bytes, 0) / 1e9 AS shuffle_gb,
         COALESCE(read_bytes, 0) / 1e9 AS read_gb,
         COALESCE(read_bytes, 0) * COALESCE(read_io_cache_percent, 0) / 100.0 / 1e9 AS cache_read_gb,
         CASE WHEN from_result_cache THEN 1 ELSE 0 END AS result_cache_runs,
         CAST(0 AS DOUBLE) AS provision_clock_s, 0 AS starts, CAST(0 AS DOUBLE) AS start_s, CAST(0 AS DOUBLE) AS start_s_max, 0 AS failed_starts
  FROM {{ source('system_query', 'history') }}
  WHERE start_time >= {{ audit_today() }} - INTERVAL {{ w }} DAYS
    AND start_time < {{ audit_today() }}
    AND compute.warehouse_id IS NOT NULL
),
run_rows AS (
  SELECT workspace_id, job_id, run_id, period_end_time, result_state,
         NULLIF(run_duration_seconds, 0) AS run_s_reported,
         COALESCE(queue_duration_seconds, 0) AS queue_s,
         MIN(period_start_time) OVER (PARTITION BY workspace_id, job_id, run_id) AS run_start
  FROM {{ source('system_lakeflow', 'job_run_timeline') }}
  WHERE period_start_time >= dateadd(day, -{{ w }}, {{ audit_today() }})
),
runs AS (
  SELECT 'job' AS resource_type, workspace_id, job_id AS resource_id, date(run_start) AS usage_date,
         1 AS runs,
         CASE WHEN result_state IN ('FAILED', 'ERROR', 'TIMED_OUT') THEN 1 ELSE 0 END AS failed,
         COALESCE(run_s_reported, timestampdiff(SECOND, run_start, period_end_time)) AS total_s,
         queue_s AS queued_s,
         CAST(0 AS DOUBLE) AS exec_s, CAST(0 AS DOUBLE) AS spilled_gb, 0 AS task_runs, CAST(0 AS DOUBLE) AS setup_s,
         CAST(0 AS DOUBLE) AS slot_wait_s, CAST(0 AS DOUBLE) AS provision_s, CAST(0 AS DOUBLE) AS shuffle_gb, CAST(0 AS DOUBLE) AS read_gb, CAST(0 AS DOUBLE) AS cache_read_gb, 0 AS result_cache_runs,
         CAST(0 AS DOUBLE) AS provision_clock_s, 0 AS starts, CAST(0 AS DOUBLE) AS start_s, CAST(0 AS DOUBLE) AS start_s_max, 0 AS failed_starts
  FROM run_rows
  WHERE result_state IS NOT NULL
    AND period_end_time < date_trunc('DAY', {{ audit_now() }})
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id, run_id ORDER BY period_end_time DESC) = 1
),
task_rows AS (
  SELECT workspace_id, job_id, run_id, period_end_time, result_state,
         COALESCE(setup_duration_seconds, 0) AS setup_s,
         COALESCE(execution_duration_seconds, 0) AS exec_s,
         MIN(period_start_time) OVER (PARTITION BY workspace_id, job_id, run_id) AS task_start
  FROM {{ source('system_lakeflow', 'job_task_run_timeline') }}
  WHERE period_start_time >= dateadd(day, -{{ w }}, {{ audit_today() }})
),
tasks AS (
  SELECT 'job' AS resource_type, workspace_id, job_id AS resource_id, date(task_start) AS usage_date,
         0 AS runs, 0 AS failed, CAST(0 AS DOUBLE) AS total_s, CAST(0 AS DOUBLE) AS queued_s,
         exec_s, CAST(0 AS DOUBLE) AS spilled_gb, 1 AS task_runs, setup_s,
         CAST(0 AS DOUBLE) AS slot_wait_s, CAST(0 AS DOUBLE) AS provision_s, CAST(0 AS DOUBLE) AS shuffle_gb, CAST(0 AS DOUBLE) AS read_gb, CAST(0 AS DOUBLE) AS cache_read_gb, 0 AS result_cache_runs,
         CAST(0 AS DOUBLE) AS provision_clock_s, 0 AS starts, CAST(0 AS DOUBLE) AS start_s, CAST(0 AS DOUBLE) AS start_s_max, 0 AS failed_starts
  FROM task_rows
  WHERE result_state IS NOT NULL
    AND period_end_time < date_trunc('DAY', {{ audit_now() }})
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id, run_id ORDER BY period_end_time DESC) = 1
),
update_rows AS (
  SELECT workspace_id, pipeline_id, update_id, period_end_time, result_state,
         MIN(period_start_time) OVER (PARTITION BY workspace_id, pipeline_id, update_id) AS update_start
  FROM {{ source('system_lakeflow', 'pipeline_update_timeline') }}
  WHERE period_start_time >= dateadd(day, -{{ w }}, {{ audit_today() }})
),
updates AS (
  SELECT 'pipeline' AS resource_type, workspace_id, pipeline_id AS resource_id, date(update_start) AS usage_date,
         1 AS runs,
         CASE WHEN result_state = 'FAILED' THEN 1 ELSE 0 END AS failed,
         timestampdiff(SECOND, update_start, period_end_time) AS total_s,
         CAST(0 AS DOUBLE) AS queued_s, CAST(0 AS DOUBLE) AS exec_s, CAST(0 AS DOUBLE) AS spilled_gb, 0 AS task_runs, CAST(0 AS DOUBLE) AS setup_s,
         CAST(0 AS DOUBLE) AS slot_wait_s, CAST(0 AS DOUBLE) AS provision_s, CAST(0 AS DOUBLE) AS shuffle_gb, CAST(0 AS DOUBLE) AS read_gb, CAST(0 AS DOUBLE) AS cache_read_gb, 0 AS result_cache_runs,
         CAST(0 AS DOUBLE) AS provision_clock_s, 0 AS starts, CAST(0 AS DOUBLE) AS start_s, CAST(0 AS DOUBLE) AS start_s_max, 0 AS failed_starts
  FROM update_rows
  WHERE result_state IS NOT NULL
    AND period_end_time < date_trunc('DAY', {{ audit_now() }})
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id, update_id ORDER BY period_end_time DESC) = 1
),
wait_spans AS (
  SELECT workspace_id, compute.warehouse_id AS warehouse_id, start_time,
         unix_millis(start_time) AS w_start,
         unix_millis(start_time) + waiting_for_compute_duration_ms AS w_end
  FROM {{ source('system_query', 'history') }}
  WHERE start_time >= {{ audit_today() }} - INTERVAL {{ w }} DAYS
    AND start_time < {{ audit_today() }}
    AND compute.warehouse_id IS NOT NULL
    AND waiting_for_compute_duration_ms > 0
),
wait_breaks AS (
  SELECT workspace_id, warehouse_id, start_time, w_start, w_end,
         CASE WHEN w_start <= MAX(w_end) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY w_start, w_end
                                               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
              THEN 0 ELSE 1 END AS new_span
  FROM wait_spans
),
wait_numbered AS (
  SELECT workspace_id, warehouse_id, start_time, w_start, w_end,
         SUM(new_span) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY w_start, w_end
                             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS span_id
  FROM wait_breaks
),
wait_clock AS (
  SELECT 'warehouse' AS resource_type, workspace_id, warehouse_id AS resource_id, date(MIN(start_time)) AS usage_date,
         0 AS runs, 0 AS failed, CAST(0 AS DOUBLE) AS total_s, CAST(0 AS DOUBLE) AS queued_s,
         CAST(0 AS DOUBLE) AS exec_s, CAST(0 AS DOUBLE) AS spilled_gb, 0 AS task_runs, CAST(0 AS DOUBLE) AS setup_s,
         CAST(0 AS DOUBLE) AS slot_wait_s, CAST(0 AS DOUBLE) AS provision_s, CAST(0 AS DOUBLE) AS shuffle_gb, CAST(0 AS DOUBLE) AS read_gb, CAST(0 AS DOUBLE) AS cache_read_gb, 0 AS result_cache_runs,
         (MAX(w_end) - MIN(w_start)) / 1000.0 AS provision_clock_s, 0 AS starts, CAST(0 AS DOUBLE) AS start_s,
         CAST(0 AS DOUBLE) AS start_s_max, 0 AS failed_starts
  FROM wait_numbered
  GROUP BY workspace_id, warehouse_id, span_id
),
wh_state AS (
  SELECT workspace_id, warehouse_id, event_type, event_time,
         LEAD(event_type) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY event_time,
           CASE event_type WHEN 'STARTING' THEN 1 WHEN 'RUNNING' THEN 2 WHEN 'STOPPING' THEN 3 ELSE 4 END) AS next_type,
         LEAD(event_time) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY event_time,
           CASE event_type WHEN 'STARTING' THEN 1 WHEN 'RUNNING' THEN 2 WHEN 'STOPPING' THEN 3 ELSE 4 END) AS next_time
  FROM {{ source('system_compute', 'warehouse_events') }}
  WHERE event_time >= {{ audit_today() }} - INTERVAL {{ w }} DAYS
    AND event_time < {{ audit_today() }}
    AND event_type IN ('STARTING', 'RUNNING', 'STOPPING', 'STOPPED')
),
wh_starts AS (
  SELECT 'warehouse' AS resource_type, workspace_id, warehouse_id AS resource_id, date(event_time) AS usage_date,
         0 AS runs, 0 AS failed, CAST(0 AS DOUBLE) AS total_s, CAST(0 AS DOUBLE) AS queued_s,
         CAST(0 AS DOUBLE) AS exec_s, CAST(0 AS DOUBLE) AS spilled_gb, 0 AS task_runs, CAST(0 AS DOUBLE) AS setup_s,
         CAST(0 AS DOUBLE) AS slot_wait_s, CAST(0 AS DOUBLE) AS provision_s, CAST(0 AS DOUBLE) AS shuffle_gb, CAST(0 AS DOUBLE) AS read_gb, CAST(0 AS DOUBLE) AS cache_read_gb, 0 AS result_cache_runs,
         CAST(0 AS DOUBLE) AS provision_clock_s, 1 AS starts,
         (unix_millis(next_time) - unix_millis(event_time)) / 1000.0 AS start_s,
         (unix_millis(next_time) - unix_millis(event_time)) / 1000.0 AS start_s_max,
         CASE WHEN next_type = 'RUNNING' THEN 0 ELSE 1 END AS failed_starts
  FROM wh_state
  WHERE event_type = 'STARTING' AND next_type IN ('RUNNING', 'STOPPING', 'STOPPED')
),
by_resource AS (
  SELECT * FROM stmts
  UNION ALL SELECT * FROM runs
  UNION ALL SELECT * FROM tasks
  UNION ALL SELECT * FROM updates
  UNION ALL SELECT * FROM wait_clock
  UNION ALL SELECT * FROM wh_starts
),
daily AS (
  SELECT resource_type, workspace_id, resource_id, usage_date,
         SUM(runs) AS runs, SUM(failed) AS failed, SUM(total_s) AS total_s, SUM(queued_s) AS queued_s,
         SUM(exec_s) AS exec_s, SUM(spilled_gb) AS spilled_gb, SUM(task_runs) AS task_runs, SUM(setup_s) AS setup_s,
         SUM(slot_wait_s) AS slot_wait_s, SUM(provision_s) AS provision_s, SUM(shuffle_gb) AS shuffle_gb,
         SUM(read_gb) AS read_gb, SUM(cache_read_gb) AS cache_read_gb, SUM(result_cache_runs) AS result_cache_runs,
         SUM(provision_clock_s) AS provision_clock_s, SUM(starts) AS starts, SUM(start_s) AS start_s,
         MAX(start_s_max) AS start_s_max, SUM(failed_starts) AS failed_starts
  FROM by_resource
  GROUP BY resource_type, workspace_id, resource_id, usage_date
),
ranked AS (
  SELECT d.*,
         DENSE_RANK() OVER (PARTITION BY resource_type
                            ORDER BY resource_total_s DESC, workspace_id ASC NULLS LAST, resource_id ASC NULLS LAST) AS busy_rank
  FROM (
    SELECT daily.*, SUM(total_s + exec_s) OVER (PARTITION BY resource_type, workspace_id, resource_id) AS resource_total_s
    FROM daily
  ) d
),
keyed AS (
  SELECT usage_date, resource_type, workspace_id, runs, failed, total_s, queued_s, exec_s, spilled_gb, task_runs, setup_s,
         slot_wait_s, provision_s, shuffle_gb, read_gb, cache_read_gb, result_cache_runs,
         provision_clock_s, starts, start_s, start_s_max, failed_starts,
         busy_rank > 100 AS is_other,
         CASE WHEN busy_rank > 100 THEN NULL ELSE resource_id END AS resource_id,
         concat(COALESCE(workspace_id, 'account'), ':', CASE WHEN busy_rank > 100 THEN 'other' ELSE resource_id END) AS resource_key
  FROM ranked
)
SELECT usage_date,
       resource_type,
       resource_key,
       workspace_id,
       resource_id,
       CASE WHEN resource_type = 'warehouse' THEN resource_id END AS warehouse_id,
       CASE WHEN resource_type = 'job' THEN resource_id END AS job_id,
       CASE WHEN resource_type = 'pipeline' THEN resource_id END AS pipeline_id,
       is_other,
       CASE WHEN is_other THEN COUNT(*) END AS pooled_count,
       SUM(runs) AS runs,
       SUM(failed) AS failed,
       ROUND(SUM(total_s), 1) AS total_s,
       ROUND(SUM(queued_s), 1) AS queued_s,
       ROUND(SUM(exec_s), 1) AS exec_s,
       ROUND(SUM(spilled_gb), 3) AS spilled_gb,
       SUM(task_runs) AS task_runs,
       ROUND(SUM(setup_s), 1) AS setup_s,
       ROUND(SUM(slot_wait_s), 1) AS slot_wait_s,
       ROUND(SUM(provision_s), 1) AS provision_s,
       ROUND(SUM(shuffle_gb), 6) AS shuffle_gb,
       ROUND(SUM(read_gb), 6) AS read_gb,
       ROUND(SUM(cache_read_gb), 6) AS cache_read_gb,
       SUM(result_cache_runs) AS result_cache_runs,
       ROUND(SUM(provision_clock_s), 1) AS provision_clock_s,
       SUM(starts) AS starts,
       ROUND(SUM(start_s), 1) AS start_s,
       ROUND(MAX(start_s_max), 1) AS start_s_max,
       SUM(failed_starts) AS failed_starts
FROM keyed
GROUP BY usage_date, resource_type, resource_key, workspace_id, resource_id, is_other
ORDER BY usage_date DESC, resource_type, resource_key
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
