-- generated from dbt/models/databricks_direct/performance/d_query_pruning_effectiveness.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/performance/query_pruning_effectiveness.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH latest_wh AS (
  SELECT warehouse_id, warehouse_name
  FROM `system`.`compute`.`warehouses`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
),
daily AS (
  SELECT date(start_time) AS day, workspace_id, compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
         executed_by AS executed_by,
         statement_type,
         COUNT(*) AS query_count,
         SUM(pruned_files)    AS pruned_files_sum,
         SUM(read_files)      AS read_files_sum,
         SUM(read_partitions) AS read_partitions_sum,
         SUM(read_bytes)      AS read_bytes_sum,
         SUM(read_rows)       AS read_rows_sum
  FROM `system`.`query`.`history`
  WHERE start_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
    AND start_time < __AS_OF_DATE__
    AND (pruned_files > 0 OR read_files > 0)
  GROUP BY date(start_time), workspace_id, compute.type, compute.warehouse_id, executed_by, statement_type
),
scored AS (
  SELECT d.*,
         d.pruned_files_sum * 1.0 / NULLIF(d.pruned_files_sum + d.read_files_sum, 0) AS pruning_ratio,
         -- day_status: the raw per-day band before the recurrence cap (see enriched/status below).
         CASE
           WHEN d.pruned_files_sum + d.read_files_sum = 0 THEN 'NOT_ASSESSED'
           WHEN d.read_bytes_sum < 1073741824 THEN 'NOT_ASSESSED'
           WHEN d.pruned_files_sum * 1.0 / NULLIF(d.pruned_files_sum + d.read_files_sum, 0) < 0.2 THEN 'CRITICAL'
           WHEN d.pruned_files_sum * 1.0 / NULLIF(d.pruned_files_sum + d.read_files_sum, 0) < 0.5 THEN 'WARN'
           ELSE 'OK'
         END AS day_status
  FROM daily d
),
enriched AS (
  SELECT s.*,
         COUNT(*) OVER (PARTITION BY s.workspace_id, s.compute_type, s.warehouse_id, s.executed_by, s.statement_type) AS days_seen,
         SUM(CASE WHEN s.day_status IN ('WARN', 'CRITICAL') THEN 1 ELSE 0 END)
           OVER (PARTITION BY s.workspace_id, s.compute_type, s.warehouse_id, s.executed_by, s.statement_type) AS days_flagged
  FROM scored s
)
SELECT e.day, e.workspace_id, e.compute_type, e.warehouse_id, lw.warehouse_name,
       e.executed_by, e.statement_type,
       e.query_count, e.pruned_files_sum, e.read_files_sum, e.read_partitions_sum,
       e.read_bytes_sum, e.read_rows_sum,
       ROUND(e.pruning_ratio, 3) AS pruning_ratio,
       e.days_seen, e.days_flagged,
       -- status: a day below 1073741824 is already NOT_ASSESSED (day_status), and a CRITICAL day
       -- that is a one-off (days_flagged below 2) is capped to WARN.
       CASE
         WHEN e.day_status = 'CRITICAL' AND e.days_flagged < 2 THEN 'WARN'
         ELSE e.day_status
       END AS status
FROM enriched e
LEFT JOIN latest_wh lw ON lw.warehouse_id = e.warehouse_id
ORDER BY e.pruning_ratio ASC NULLS LAST
) q
