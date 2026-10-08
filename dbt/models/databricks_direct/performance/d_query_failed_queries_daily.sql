{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:performance', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/performance/query_failed_queries_daily.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT date(start_time) AS day, workspace_id, compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
       execution_status, statement_type,
       {{ mask_user('executed_by', 'MAX(executed_by_user_id)') }} AS executed_by,
       COUNT(*) AS query_count,
       SUM(total_duration_ms)     AS total_duration_ms_sum,
       SUM(execution_duration_ms) AS execution_duration_ms_sum,
       -- error text de-valued at source: strip emails, then single-quoted string literals (chr(39) is
       -- the single quote) - keeps the error SHAPE, drops literal data values.
       regexp_replace(
         regexp_replace(MAX(error_message), '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}', '<email>'),
         concat(chr(39), '[^', chr(39), ']*', chr(39)), '?'
       )                          AS error_message_sample,
       -- status: worst-first band on daily failed/canceled query count (field heuristic; {{ param('query_failed_queries_daily', 'warn_failed_count', 5) }} / {{ param('query_failed_queries_daily', 'crit_failed_count', 20) }}).
       CASE
         WHEN COUNT(*) >= {{ param('query_failed_queries_daily', 'crit_failed_count', 20) }} THEN 'CRITICAL'
         WHEN COUNT(*) >= {{ param('query_failed_queries_daily', 'warn_failed_count', 5) }} THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM {{ source('system_query', 'history') }}
WHERE start_time >= {{ audit_today() }} - INTERVAL {{ w }} DAYS
  AND start_time < {{ audit_today() }}
  AND execution_status IN ('FAILED','CANCELED')
GROUP BY date(start_time), workspace_id, compute.type, compute.warehouse_id, execution_status, statement_type, executed_by
ORDER BY query_count DESC
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
