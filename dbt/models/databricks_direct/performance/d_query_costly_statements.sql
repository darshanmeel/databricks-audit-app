{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:performance', 'tier:standard', 'stars', 'databricks_direct']) }}
-- generated from app/queries/vendored/performance/query_costly_statements.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH devalued AS (
  SELECT
    workspace_id,
    statement_id,
    statement_type,
    -- executed_by: DEC-66.3 identity format (executed_by_user_id, or when NULL the first 8 hex chars of
    -- sha2(lower(trim(executed_by)), 256), + ' ' + first-2 + '***'; NULL/'__REDACTED__'/GUID kept as-is).
    {{ mask_user('executed_by', 'executed_by_user_id') }}                                    AS executed_by,
    compute.warehouse_id                   AS warehouse_id,
    compute.type                           AS compute_type,
    start_time,
    execution_duration_ms,
    waiting_for_compute_duration_ms,
    total_task_duration_ms,
    read_bytes,
    read_files,
    pruned_files,
    read_partitions,
    read_rows,
    produced_rows,
    spilled_local_bytes,
    shuffle_read_bytes,
    from_result_cache,
    -- statement_text de-valued: strip emails, then replace every single-quoted literal with '?'
    -- (chr(39) is the single quote). Keeps the query shape, removes literal data values.
    regexp_replace(
      regexp_replace(statement_text, '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}', '<email>'),
      concat(chr(39), '[^', chr(39), ']*', chr(39)), '?'
    )                                      AS statement_text
  FROM {{ source('system_query', 'history') }}
  WHERE start_time >= dateadd(DAY, -{{ w }}, {{ audit_today() }})
    AND start_time <  {{ audit_today() }}
    AND execution_status = 'FINISHED'
    AND from_result_cache = false
    AND execution_duration_ms > 0
),
base AS (
  SELECT d.*, sha2(d.statement_text, 256) AS statement_fingerprint
  FROM devalued d
)
SELECT
  b.*,
  -- per-warehouse framing: this statement's share of its warehouse's total execution time.
  b.execution_duration_ms / NULLIF(SUM(b.execution_duration_ms) OVER (PARTITION BY b.warehouse_id), 0) AS pct_of_warehouse_exec_ms,
  ROW_NUMBER() OVER (PARTITION BY b.warehouse_id ORDER BY b.execution_duration_ms DESC)                 AS exec_rank_in_warehouse,
  -- status: worst-first band on within-warehouse execution share (field heuristic).
  CASE
    WHEN b.execution_duration_ms / NULLIF(SUM(b.execution_duration_ms) OVER (PARTITION BY b.warehouse_id), 0) >= {{ param('query_costly_statements', 'crit_wh_share', 0.25) }} THEN 'CRITICAL'
    WHEN b.execution_duration_ms / NULLIF(SUM(b.execution_duration_ms) OVER (PARTITION BY b.warehouse_id), 0) >= {{ param('query_costly_statements', 'warn_wh_share', 0.1) }} THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM base b
ORDER BY b.execution_duration_ms DESC
LIMIT {{ param('query_costly_statements', 'top_n', 1000) }}
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
