-- generated from dbt/models/databricks_direct/performance/d_query_shuffle_write_amplification.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/performance/query_shuffle_write_amplification.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT date(start_time) AS day, workspace_id, compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
       executed_by AS executed_by,
       statement_type,
       COUNT(*) AS query_count,
       SUM(shuffle_read_bytes) AS shuffle_read_bytes_sum,
       SUM(written_bytes)      AS written_bytes_sum,
       SUM(written_rows)       AS written_rows_sum,
       SUM(written_files)      AS written_files_sum,
       SUM(read_bytes)         AS read_bytes_sum,
       -- status: worst-first band on shuffle volume OR small-file write amplification (field heuristic;
       -- 50 / 200 / 32 / 8).
       CASE
         WHEN SUM(shuffle_read_bytes) >= 200 * 1e9
           OR (SUM(written_files) > 0 AND SUM(written_bytes) / SUM(written_files) < 8 * 1e6) THEN 'CRITICAL'
         WHEN SUM(shuffle_read_bytes) >= 50 * 1e9
           OR (SUM(written_files) > 0 AND SUM(written_bytes) / SUM(written_files) < 32 * 1e6) THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`query`.`history`
WHERE start_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
  AND start_time < __AS_OF_DATE__
  AND (shuffle_read_bytes > 0 OR written_bytes > 0)
GROUP BY date(start_time), workspace_id, compute.type, compute.warehouse_id, executed_by, statement_type
ORDER BY shuffle_read_bytes_sum DESC
) q
