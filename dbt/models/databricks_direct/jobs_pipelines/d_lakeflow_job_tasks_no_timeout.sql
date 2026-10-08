{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:jobs_pipelines', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_job_tasks_no_timeout.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH latest_jobs AS (
  SELECT workspace_id, job_id, name AS job_name, delete_time
  FROM {{ source('system_lakeflow', 'jobs') }}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),
latest_tasks AS (
  SELECT workspace_id, job_id, task_key, timeout_seconds, delete_time, change_time,
         -- a RUN_DURATION_SECONDS/GREATER_THAN health rule bounds a run's duration same as a timeout;
         -- health_rules holds at most a couple of entries, so three positions is enough headroom
         COALESCE((try_element_at(health_rules, 1).metric = 'RUN_DURATION_SECONDS' AND try_element_at(health_rules, 1).operator = 'GREATER_THAN')
          OR (try_element_at(health_rules, 2).metric = 'RUN_DURATION_SECONDS' AND try_element_at(health_rules, 2).operator = 'GREATER_THAN')
          OR (try_element_at(health_rules, 3).metric = 'RUN_DURATION_SECONDS' AND try_element_at(health_rules, 3).operator = 'GREATER_THAN'), FALSE) AS has_duration_health_rule
  FROM {{ source('system_lakeflow', 'job_tasks') }}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id, task_key ORDER BY change_time DESC) = 1
),
population_floor AS (
  SELECT workspace_id, MIN(change_time) AS population_floor_time
  FROM {{ source('system_lakeflow', 'job_tasks') }}
  WHERE timeout_seconds IS NOT NULL
  GROUP BY workspace_id
),
scoped_tasks AS (
  -- mark tasks whose parent job is deleted or has no current jobs row at all
  SELECT lt.workspace_id, lt.job_id, lj.job_name, lt.task_key, lt.timeout_seconds, lt.change_time,
         lt.has_duration_health_rule,
         (lj.job_id IS NULL OR lj.delete_time IS NOT NULL) AS parent_gone
  FROM latest_tasks lt
  LEFT JOIN latest_jobs lj ON lj.workspace_id = lt.workspace_id AND lj.job_id = lt.job_id
  WHERE lt.delete_time IS NULL
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM {{ list_prices() }} list_prices
),
cost_rollup AS (
  SELECT u.workspace_id,
         u.usage_metadata.job_id                          AS job_id,
         SUM(u.usage_quantity)                            AS net_dbus,
         SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS est_usd_list,
         CASE
           WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                         THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END                                               AS price_basis
  FROM {{ source('system_billing', 'usage') }} u
  LEFT JOIN price p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.job_id IS NOT NULL
    AND u.usage_date >= date_add({{ audit_today() }}, -{{ w }})
    AND u.usage_date <  {{ audit_today() }}
  GROUP BY u.workspace_id, u.usage_metadata.job_id
),
judged AS (
  SELECT st.workspace_id, st.job_id, st.job_name, st.task_key, st.timeout_seconds,
         st.has_duration_health_rule, st.parent_gone,
         NOT st.parent_gone AND (st.timeout_seconds IS NULL OR st.timeout_seconds = 0)
           AND NOT st.has_duration_health_rule AS no_timeout,
         CASE
           WHEN NOT st.parent_gone AND st.timeout_seconds IS NULL AND NOT st.has_duration_health_rule
                AND (pf.population_floor_time IS NULL OR st.change_time < pf.population_floor_time)
             THEN 'not_populated'
           WHEN NOT st.parent_gone AND st.timeout_seconds IS NULL AND NOT st.has_duration_health_rule
             THEN 'after_population'
         END AS timeout_null_reason
  FROM scoped_tasks st
  LEFT JOIN population_floor pf ON pf.workspace_id = st.workspace_id
)
SELECT j.workspace_id, w.workspace_name, j.job_id, j.job_name, j.task_key, j.timeout_seconds,
       j.no_timeout, j.has_duration_health_rule AS bounded_by_health_rule, j.timeout_null_reason,
       j.parent_gone,
       ROUND(COALESCE(cr.net_dbus, 0), 2)     AS net_dbus,
       ROUND(COALESCE(cr.est_usd_list, 0), 2) AS est_usd_list,
       COALESCE(cr.price_basis, 'priced')     AS price_basis,
       -- status: a flagged task (no_timeout) reads WARN, or CRITICAL when its parent job's own spend
       -- over the cost-lookback window is also high ({{ param('lakeflow_job_tasks_no_timeout', 'crit_no_timeout_usd', 200) }}). An orphaned-parent task,
       -- or one whose NULL predates population, is NOT_ASSESSED - nobody can act on either.
       CASE
         WHEN j.parent_gone THEN 'NOT_ASSESSED'
         WHEN j.no_timeout AND j.timeout_null_reason = 'not_populated' THEN 'NOT_ASSESSED'
         WHEN j.no_timeout AND COALESCE(cr.est_usd_list, 0) >= {{ param('lakeflow_job_tasks_no_timeout', 'crit_no_timeout_usd', 200) }} THEN 'CRITICAL'
         WHEN j.no_timeout THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN j.parent_gone THEN 'orphaned_parent'
         WHEN j.no_timeout AND j.timeout_null_reason = 'not_populated' THEN 'timeout_not_populated'
       END AS not_assessed_reason
FROM judged j
LEFT JOIN cost_rollup cr ON cr.workspace_id = j.workspace_id AND cr.job_id = j.job_id
LEFT JOIN {{ source('system_access', 'workspaces_latest') }} w ON w.workspace_id = j.workspace_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         est_usd_list DESC, j.workspace_id, j.job_id, j.task_key
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
