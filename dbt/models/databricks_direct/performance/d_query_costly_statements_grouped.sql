{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:performance', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/performance/query_costly_statements_grouped.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH devalued AS (
  SELECT
    statement_id,
    workspace_id,
    statement_type,
    compute.warehouse_id AS warehouse_id,
    start_time,
    execution_duration_ms,
    (statement_text IS NULL OR statement_text = '<REDACTED>') AS is_redacted,
    regexp_replace(
      regexp_replace(statement_text, '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}', '<email>'),
      concat(chr(39), '[^', chr(39), ']*', chr(39)), '?'
    ) AS statement_text
  FROM {{ source('system_query', 'history') }}
  WHERE start_time >= dateadd(DAY, -{{ w }}, {{ audit_today() }})
    AND start_time <  {{ audit_today() }}
    AND execution_status = 'FINISHED'
    AND from_result_cache = false
    AND execution_duration_ms > 0
),
fp AS (
  SELECT
    -- a redacted statement has no text to shape - key it by its own statement_id so it is never
    -- pooled with another redacted statement (they'd otherwise all sha2 to the same placeholder text).
    CASE WHEN is_redacted THEN concat('statement:', statement_id)
         ELSE sha2(statement_text, 256)
    END AS statement_fingerprint,
    *
  FROM devalued
)
SELECT
  workspace_id,
  statement_fingerprint,
  MAX(is_redacted)                           AS is_redacted,
  MAX(statement_type)                        AS statement_type,
  MIN(statement_text)                        AS sample_statement_text,
  COUNT(*)                                   AS runs,
  SUM(execution_duration_ms)                 AS total_exec_ms,
  CAST(AVG(execution_duration_ms) AS BIGINT) AS avg_exec_ms,
  MAX(execution_duration_ms)                 AS max_exec_ms,
  COUNT(DISTINCT warehouse_id)               AS distinct_warehouses,
  MIN(start_time)                            AS first_seen,
  MAX(start_time)                            AS last_seen,
  -- status: worst-first band on cumulative execution time for the shape (field heuristic).
  CASE
    WHEN SUM(execution_duration_ms) >= {{ param('query_costly_statements_grouped', 'crit_total_hours', 5) }} * 3600 * 1000 THEN 'CRITICAL'
    WHEN SUM(execution_duration_ms) >= {{ param('query_costly_statements_grouped', 'warn_total_hours', 1) }} * 3600 * 1000 THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM fp
GROUP BY workspace_id, statement_fingerprint
ORDER BY total_exec_ms DESC
LIMIT {{ param('query_costly_statements_grouped', 'top_n', 500) }}
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
