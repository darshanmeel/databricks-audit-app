-- generated from dbt/models/databricks_direct/performance/d_query_failed_queries_daily.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/performance/query_failed_queries_daily.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT date(start_time) AS day, workspace_id, compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
       execution_status, statement_type,
       executed_by AS executed_by,
       COUNT(*) AS query_count,
       SUM(total_duration_ms)     AS total_duration_ms_sum,
       SUM(execution_duration_ms) AS execution_duration_ms_sum,
       -- error text de-valued at source: strip emails, then single-quoted string literals (chr(39) is
       -- the single quote) - keeps the error SHAPE, drops literal data values.
       regexp_replace(
         regexp_replace(MAX(error_message), '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}', '<email>'),
         concat(chr(39), '[^', chr(39), ']*', chr(39)), '?'
       )                          AS error_message_sample,
       -- status: worst-first band on daily failed/canceled query count (field heuristic; 5 / 20).
       CASE
         WHEN COUNT(*) >= 20 THEN 'CRITICAL'
         WHEN COUNT(*) >= 5 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`query`.`history`
WHERE start_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
  AND start_time < __AS_OF_DATE__
  AND execution_status IN ('FAILED','CANCELED')
GROUP BY date(start_time), workspace_id, compute.type, compute.warehouse_id, execution_status, statement_type, executed_by
ORDER BY query_count DESC
) q
