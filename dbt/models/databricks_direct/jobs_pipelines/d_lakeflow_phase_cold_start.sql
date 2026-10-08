{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:jobs_pipelines', 'tier:lite', 'databricks_direct']) }}
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_phase_cold_start.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH run_level AS (
  SELECT workspace_id, job_id,
         COUNT(DISTINCT run_id)                                                      AS runs,
         SUM(CASE WHEN run_duration_seconds > 0 THEN queue_duration_seconds END)     AS queue_s_total,
         SUM(CASE WHEN run_duration_seconds > 0 THEN execution_duration_seconds END) AS execution_s_total,
         SUM(CASE WHEN run_duration_seconds > 0 THEN cleanup_duration_seconds END)   AS cleanup_s_total,
         SUM(run_duration_seconds)                                                   AS run_s_total,
         PERCENTILE(CASE WHEN run_duration_seconds > 0 THEN queue_duration_seconds END, 0.95) AS queue_s_p95
  FROM {{ source('system_lakeflow', 'job_run_timeline') }}
  WHERE period_start_time >= dateadd(day, -{{ w }}, {{ audit_today() }})
    AND period_end_time < date_trunc('DAY', {{ audit_now() }})
    AND result_state IS NOT NULL     -- end row carries the final durations
  GROUP BY workspace_id, job_id
),
task_setup AS (
  SELECT workspace_id, job_id,
         COUNT(*)                                              AS task_rows,
         SUM(setup_duration_seconds)                           AS setup_s_total,
         PERCENTILE(setup_duration_seconds, 0.95)               AS setup_s_p95,
         SUM(CASE WHEN setup_duration_seconds IS NULL THEN 1 ELSE 0 END) AS rows_setup_null
  FROM {{ source('system_lakeflow', 'job_task_run_timeline') }}
  WHERE period_start_time >= dateadd(day, -{{ w }}, {{ audit_today() }})
    AND period_end_time < date_trunc('DAY', {{ audit_now() }})
    -- task end row carries the final durations; a failed start's long setup is not a cold start
    AND result_state = 'SUCCEEDED'
  GROUP BY workspace_id, job_id
)
SELECT r.workspace_id, r.job_id,
       r.runs,
       ts.setup_s_total,
       r.queue_s_total,
       r.execution_s_total,
       r.cleanup_s_total,
       r.run_s_total,
       ts.setup_s_p95,
       r.queue_s_p95,
       COALESCE(ts.rows_setup_null, r.runs)                    AS rows_setup_null,
       -- status: worst-first band on p95 setup (cold-start) seconds (field heuristic;
       -- {{ param('lakeflow_phase_cold_start', 'warn_setup_p95_s', 60) }} / {{ param('lakeflow_phase_cold_start', 'crit_setup_p95_s', 300) }}), from task_setup so a multi-task job is judged
       -- too. A job with no task row in the window has nothing to band on -> NOT_ASSESSED,
       -- distinct from every observed task's setup folding to NULL (also NOT_ASSESSED, via the
       -- p95-is-NULL branch below).
       CASE
         WHEN ts.task_rows IS NULL OR ts.task_rows = 0 THEN 'NOT_ASSESSED'
         WHEN ts.setup_s_p95 IS NULL THEN 'NOT_ASSESSED'
         WHEN ts.setup_s_p95 >= {{ param('lakeflow_phase_cold_start', 'crit_setup_p95_s', 300) }} THEN 'CRITICAL'
         WHEN ts.setup_s_p95 >= {{ param('lakeflow_phase_cold_start', 'warn_setup_p95_s', 60) }} THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM run_level r
LEFT JOIN task_setup ts ON r.workspace_id = ts.workspace_id AND r.job_id = ts.job_id
ORDER BY setup_s_p95 DESC
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
