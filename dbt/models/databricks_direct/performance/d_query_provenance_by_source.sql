{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:performance', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/performance/query_provenance_by_source.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT workspace_id,
       compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
       CASE WHEN executed_by IS NULL              THEN 'unknown'
            WHEN executed_by LIKE '%@%'           THEN 'user'
            ELSE 'service_principal' END AS identity_type,
       {{ mask_user('executed_by', 'executed_by_user_id') }} AS executed_by,
       -- same precedence as query_top_by_cost, so a statement with several origins (an alert run
       -- by a job) gets one label on every page
       CASE WHEN query_source.sql_query_id        IS NOT NULL THEN 'sql_editor'
            WHEN query_source.dashboard_id        IS NOT NULL THEN 'dashboard'
            WHEN query_source.legacy_dashboard_id IS NOT NULL THEN 'legacy_dashboard'
            WHEN query_source.genie_space_id      IS NOT NULL THEN 'genie'
            WHEN query_source.alert_id            IS NOT NULL THEN 'alert'
            WHEN query_source.job_info.job_id     IS NOT NULL THEN 'job'
            WHEN query_source.notebook_id         IS NOT NULL THEN 'notebook'
            ELSE 'other' END AS source_kind,
       query_source.job_info.job_id AS job_id,
       query_source.dashboard_id    AS dashboard_id,
       query_source.notebook_id     AS notebook_id,
       COUNT(*) AS query_count,
       SUM(execution_duration_ms) AS execution_duration_ms_sum,
       SUM(total_duration_ms)     AS total_duration_ms_sum,
       SUM(read_bytes)            AS read_bytes_sum
FROM {{ source('system_query', 'history') }}
WHERE start_time >= {{ audit_today() }} - INTERVAL {{ w }} DAYS
  AND start_time < {{ audit_today() }}
GROUP BY workspace_id, compute.type, compute.warehouse_id, source_kind,
         CASE WHEN executed_by IS NULL              THEN 'unknown'
              WHEN executed_by LIKE '%@%'           THEN 'user'
              ELSE 'service_principal' END,
         {{ mask_user('executed_by', 'executed_by_user_id') }},
         query_source.job_info.job_id, query_source.dashboard_id, query_source.notebook_id
ORDER BY workspace_id, compute_type, source_kind
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
