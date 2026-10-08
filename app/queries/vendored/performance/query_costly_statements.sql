-- query_id: query_costly_statements
-- title: Heaviest individual SQL statements (with per-warehouse framing)
-- domain: performance   tier: standard
-- reads: system.query.history
-- requires: SELECT on system.query; GA
-- empty_if: schema_not_enabled, compute_scope_gap, preview_unavailable
-- params: :period_days (default 30) rolling window in days; :warn_wh_share (default 0.1) fraction of a warehouse's total execution time one statement must eat to flag WARN; :crit_wh_share (default 0.25) same for CRITICAL; :top_n (default 1000) row cap
-- confidence: confirmed
-- confidence_note: system.query.history columns verified in a live workspace.
-- read_this: One row = one heavy FINISHED statement. execution_duration_ms is the column that matters (Databricks has no per-query dollar column; warehouse DBUs are allocated in proportion to it, so ranking by it ~= ranking by cost within one warehouse). pct_of_warehouse_exec_ms shows how much of that warehouse's total execution time this single statement ate; statement_fingerprint groups repeat shapes.
-- healthy: no single statement dominates its warehouse - pct_of_warehouse_exec_ms below :warn_wh_share (field heuristic - tune for your account).
-- investigate_if: pct_of_warehouse_exec_ms at/above :warn_wh_share (WARN) or :crit_wh_share (CRITICAL) - one statement shape eating a large share of a warehouse's execution time (field heuristic).
-- actions: 1) tune the statement - add filters / partition pruning, avoid full scans, cache reused CTEs (free); 2) route heavy recurring shapes to a right-sized dedicated warehouse or schedule them off-peak (config); 3) scale the warehouse up/out only after tuning, if the statement is genuinely large (spend).
-- next: query_costly_statements_grouped (to roll repeat shapes up by fingerprint), query_pruning_effectiveness (if read_files far exceeds pruned_files), query_local_spillage (if spilled_local_bytes is high)
-- caveats: Databricks has NO per-query dollar column; warehouse DBUs are allocated in proportion to execution_duration_ms, so ranking by execution_duration_ms approximates cost ONLY WITHIN one warehouse - a longer statement on a bigger warehouse is not necessarily costlier than a shorter one elsewhere, which is exactly why pct_of_warehouse_exec_ms frames each statement against its own warehouse. Serverless rows carry warehouse_id NULL and are pooled into one NULL-warehouse partition. PRIVACY: executed_by is masked in-SQL to DEC-66.3's identity format (executed_by_user_id, or when that is NULL the first 8 hex chars of sha2(lower(trim(executed_by)), 256), + a space + the first 2 chars of the raw identity + '***'; NULL/'__REDACTED__' and a service-principal GUID are kept as-is), and statement_text has emails and single-quoted string literals stripped so only the query SHAPE (identifiers / structure) - never data values - is returned. statement_fingerprint = sha2 of that de-valued text, so identical shapes share a fingerprint; it is not reversible to the original query.
-- system.query.history captures only SQL-warehouse and serverless statements - queries run on classic all-purpose/job clusters are NOT recorded at all, so a heavy classic-compute workload is entirely invisible to this ranking (absence here is not proof of no heavy statements).
WITH devalued AS (
  SELECT
    workspace_id,
    statement_id,
    statement_type,
    -- executed_by: DEC-66.3 identity format (executed_by_user_id, or when NULL the first 8 hex chars of
    -- sha2(lower(trim(executed_by)), 256), + ' ' + first-2 + '***'; NULL/'__REDACTED__'/GUID kept as-is).
    CASE
      WHEN executed_by IS NULL OR executed_by = '__REDACTED__' THEN executed_by
      WHEN executed_by RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN executed_by
      ELSE concat(COALESCE(executed_by_user_id, substr(sha2(lower(trim(executed_by)), 256), 1, 8)), ' ', substr(executed_by, 1, 2), '***')
    END                                    AS executed_by,
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
  FROM system.query.history
  WHERE start_time >= dateadd(DAY, -:period_days, current_date())
    AND start_time <  current_date()
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
    WHEN b.execution_duration_ms / NULLIF(SUM(b.execution_duration_ms) OVER (PARTITION BY b.warehouse_id), 0) >= :crit_wh_share THEN 'CRITICAL'
    WHEN b.execution_duration_ms / NULLIF(SUM(b.execution_duration_ms) OVER (PARTITION BY b.warehouse_id), 0) >= :warn_wh_share THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM base b
ORDER BY b.execution_duration_ms DESC
LIMIT :top_n
