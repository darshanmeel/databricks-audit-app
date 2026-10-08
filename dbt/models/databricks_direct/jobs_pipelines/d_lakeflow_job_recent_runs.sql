{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:jobs_pipelines', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/app/jobs_pipelines/lakeflow_job_recent_runs.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH all_rows AS (
  SELECT workspace_id, job_id, run_id, period_start_time, period_end_time,
         result_state, termination_code,
         NULLIF(run_duration_seconds, 0) AS run_s_reported   -- 0 = not reported on a multi-task job
  FROM {{ source('system_lakeflow', 'job_run_timeline') }}
  WHERE period_start_time >= dateadd(day, -{{ w }}, {{ audit_today() }})
),
run_span AS (
  -- the run's TRUE first observed slice, across every attempt's own rows (see caveats)
  SELECT workspace_id, job_id, run_id, MIN(period_start_time) AS run_start
  FROM all_rows
  GROUP BY workspace_id, job_id, run_id
),
end_rows AS (
  -- no day cutoff here (unlike lakeflow_job_reliability's own daily aggregate): this is a
  -- per-run listing, so a run whose only end row landed today is still a real, finished run --
  -- dropping it would read as "still running" (see caveats).
  SELECT workspace_id, job_id, run_id, period_end_time, result_state, termination_code, run_s_reported
  FROM all_rows
  WHERE result_state IS NOT NULL
),
run_last_end AS (
  -- the run's LAST attempt's own end row only - never MAX(result_state)/MAX(termination_code),
  -- which compares attempts' text alphabetically (see caveats)
  SELECT workspace_id, job_id, run_id, result_state, termination_code, run_s_reported,
         period_end_time                                                    AS run_end,
         COUNT(*) OVER (PARTITION BY workspace_id, job_id, run_id)          AS attempts
  FROM end_rows
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id, run_id ORDER BY period_end_time DESC
  ) = 1
),
run_obs AS (
  SELECT s.workspace_id, s.job_id, s.run_id, s.run_start,
         e.run_end, e.result_state, e.termination_code, e.run_s_reported, e.attempts,
         (e.run_id IS NULL) AS in_flight,
         COALESCE(e.run_s_reported,
                  timestampdiff(SECOND, s.run_start,
                                CASE WHEN e.run_id IS NULL THEN {{ audit_now() }} ELSE e.run_end END)) AS run_s
  FROM run_span s
  LEFT JOIN run_last_end e
    ON  e.workspace_id = s.workspace_id AND e.job_id = s.job_id AND e.run_id = s.run_id
),
failing_tasks AS (
  SELECT workspace_id, job_id, job_run_id,
         concat_ws(', ', collect_set(task_key)) AS failing_task_keys,
         MAX(termination_code)                  AS failing_task_termination_code
  FROM {{ source('system_lakeflow', 'job_task_run_timeline') }}
  WHERE period_start_time >= dateadd(day, -{{ w }}, {{ audit_today() }})
    AND result_state IN ('FAILED', 'ERROR', 'TIMED_OUT')
  GROUP BY workspace_id, job_id, job_run_id
),
run_cost AS (
  -- the exact per-run attribution and effective-list price join lakeflow_job_run_cost uses
  -- (usage_metadata.job_id/job_run_id, DEC-66.1, usage_unit matched)
  SELECT u.workspace_id,
         u.usage_metadata.job_id     AS job_id,
         u.usage_metadata.job_run_id AS job_run_id,
         SUM(u.usage_quantity)                AS net_dbus,
         SUM(u.usage_quantity * lp.list_rate) AS net_list_cost
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
sql_errors AS (
  -- de-valued (emails / string literals stripped) error text of this run's own failed SQL
  -- statements, same de-value pattern query_failed_queries_daily uses
  SELECT workspace_id,
         query_source.job_info.job_run_id AS job_run_id,
         MAX(
           regexp_replace(
             regexp_replace(error_message, '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}', '<email>'),
             concat(chr(39), '[^', chr(39), ']*', chr(39)), '?'
           )
         ) AS sql_error_sample
  FROM {{ source('system_query', 'history') }}
  WHERE start_time >= dateadd(day, -{{ w }}, {{ audit_today() }})
    AND execution_status = 'FAILED'
    AND query_source.job_info.job_run_id IS NOT NULL
  GROUP BY workspace_id, query_source.job_info.job_run_id
),
latest_jobs AS (
  SELECT workspace_id, job_id, name AS job_name
  FROM {{ source('system_lakeflow', 'jobs') }}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),
ranked AS (
  SELECT r.*,
         ROW_NUMBER() OVER (PARTITION BY r.workspace_id, r.job_id ORDER BY r.run_start DESC) AS run_rank
  FROM run_obs r
)
SELECT r.workspace_id,
       w.workspace_name,
       r.job_id,
       j.job_name,                                                  -- NULL for submit/workflow runs
       r.run_id,
       r.run_start,
       CASE WHEN r.in_flight THEN NULL ELSE r.run_end END           AS run_end,
       r.in_flight,
       ROUND(r.run_s / 60.0, 1)                                     AS run_minutes,
       (r.run_s_reported IS NULL)                                   AS run_duration_is_lower_bound,
       r.result_state,
       r.termination_code,
       r.attempts,
       ft.failing_task_keys,
       ft.failing_task_termination_code,
       ROUND(rc.net_dbus, 2)                                        AS net_dbus,
       ROUND(rc.net_list_cost, 2)                                   AS net_list_cost,
       se.sql_error_sample
FROM ranked r
LEFT JOIN failing_tasks ft
  ON r.workspace_id = ft.workspace_id AND r.job_id = ft.job_id AND r.run_id = ft.job_run_id
LEFT JOIN run_cost rc
  ON r.workspace_id = rc.workspace_id AND r.job_id = rc.job_id AND r.run_id = rc.job_run_id
LEFT JOIN sql_errors se
  ON r.workspace_id = se.workspace_id AND r.run_id = se.job_run_id
LEFT JOIN latest_jobs j ON r.workspace_id = j.workspace_id AND r.job_id = j.job_id
LEFT JOIN {{ source('system_access', 'workspaces_latest') }} w ON r.workspace_id = w.workspace_id
WHERE r.run_rank <= {{ param('lakeflow_job_recent_runs', 'runs_per_job', 10) }}
ORDER BY r.workspace_id, r.job_id, r.run_start DESC
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
