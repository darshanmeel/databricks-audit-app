-- query_id: query_pruning_effectiveness
-- title: File-pruning effectiveness per query group
-- domain: performance   tier: standard
-- reads: system.query.history, system.compute.warehouses
-- requires: SELECT on system.query AND system.compute; GA (system.query.history is generally available)
-- empty_if: schema_not_enabled, preview_unavailable, compute_scope_gap, privilege_scoped
-- params: :period_days (default 30) rolling window in days; :warn_prune_ratio (default 0.5) pruning ratio (pruned_files / (pruned_files + read_files)) below which a day flags WARN; :crit_prune_ratio (default 0.2) ... below which it flags CRITICAL; :min_read_bytes (default 1073741824) minimum read_bytes_sum a day must have before it is judged on its own - below it the day reads NOT_ASSESSED; :min_recurring_flagged_days (default 2) number of flagged days the same (workspace, compute_type, warehouse, identity, statement_type) pattern must show in the window before a CRITICAL day is trusted alone - a one-off flagged day is capped at WARN
-- confidence: confirmed
-- confidence_note: Columns verified against system.query.history in a live workspace.
-- read_this: One row = a day + warehouse + identity + statement_type whose queries pruned or read files during a scan. The columns that matter are pruned_files_sum and read_files_sum - together they give pruning_ratio (pruned / (pruned + read)); a low ratio on a repeating group means partition or file layout, or the query's own predicates, are not letting Databricks skip files it should be skipping. days_seen and days_flagged say, for that same (workspace, compute_type, warehouse, identity, statement_type) pattern across the whole window, how many days were observed and how many flagged WARN or CRITICAL - a CRITICAL day that is a one-off (days_flagged below :min_recurring_flagged_days) reads WARN instead, since one bad day is not the recurring-pattern signal this query exists to surface.
-- healthy: pruning_ratio at/above :warn_prune_ratio (field heuristic - tune :warn_prune_ratio for your account), or the day's read_bytes_sum is below :min_read_bytes (too little volume to judge, reads NOT_ASSESSED).
-- investigate_if: pruning_ratio below :warn_prune_ratio (WARN) or below :crit_prune_ratio (CRITICAL) - field heuristic; a CRITICAL day only stays CRITICAL when days_flagged reaches :min_recurring_flagged_days for that pattern, otherwise it is capped to WARN. A stable low ratio on the same table/warehouse combination over several days is the real signal, not one bad day.
-- actions: 1) check whether your WHERE/JOIN predicates actually align with the table's partition, Z-order, or liquid-clustering columns (free); 2) run OPTIMIZE / re-cluster the table on the columns these queries filter by (config); 3) if the table is still poorly organized after re-clustering, repartition or rewrite it with a layout that matches the query pattern (spend - a rewrite cost).
-- next: query_local_spillage (if the same warehouse also spills, pointing to under-provisioned memory on top of poor pruning), query_shuffle_write_amplification (if the same reads are also shuffle-heavy)
-- caveats: Pruning effectiveness here is pruned_files / (pruned_files + read_files) - there is no total-partition denominator and no per-table rollup in system tables, so this is per-statement-group only, never "percent of your table pruned." read_partitions is a POST-pruning count (partitions actually read after pruning), not partitions pruned - do not read it as the inverse of pruned_files. The WHERE clause only includes rows where pruning or reading actually happened, guarding out non-scan statements; whether Databricks reports NULL or 0 for these counters on a non-scan statement is undocumented, so treat a missing row as "not a scan," not "perfect pruning." A day below :min_read_bytes reads NOT_ASSESSED rather than being banded on too little volume to mean anything - most CRITICAL rows on a small account came from tiny single-day groups, not a real recurring pruning problem, before this floor existed. days_seen/days_flagged are computed per (workspace_id, compute_type, warehouse_id, executed_by, statement_type) across the WHOLE window, not just the current day, so they are stable across every row of the same pattern. warehouse_name is the latest name for warehouse_id from system.compute.warehouses (SCD2, latest row by change_time) and is NULL for a deleted warehouse or a NULL warehouse_id (serverless). This table is regional.
-- system.query.history only captures queries run on SQL warehouses or serverless compute; queries on classic all-purpose or job clusters never appear here, so poor pruning on those clusters is entirely invisible to this analysis.
WITH latest_wh AS (
  SELECT warehouse_id, warehouse_name
  FROM system.compute.warehouses
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
),
daily AS (
  SELECT date(start_time) AS day, workspace_id, compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
         CASE
           WHEN executed_by IS NULL OR executed_by = '__REDACTED__' THEN executed_by
           WHEN executed_by RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN executed_by
           ELSE concat(COALESCE(MAX(executed_by_user_id), substr(sha2(lower(trim(executed_by)), 256), 1, 8)), ' ', substr(executed_by, 1, 2), '***')
         END AS executed_by,
         statement_type,
         COUNT(*) AS query_count,
         SUM(pruned_files)    AS pruned_files_sum,
         SUM(read_files)      AS read_files_sum,
         SUM(read_partitions) AS read_partitions_sum,
         SUM(read_bytes)      AS read_bytes_sum,
         SUM(read_rows)       AS read_rows_sum
  FROM system.query.history
  WHERE start_time >= current_date() - INTERVAL :period_days DAYS
    AND start_time < current_date()
    AND (pruned_files > 0 OR read_files > 0)
  GROUP BY date(start_time), workspace_id, compute.type, compute.warehouse_id, executed_by, statement_type
),
scored AS (
  SELECT d.*,
         d.pruned_files_sum * 1.0 / NULLIF(d.pruned_files_sum + d.read_files_sum, 0) AS pruning_ratio,
         -- day_status: the raw per-day band before the recurrence cap (see enriched/status below).
         CASE
           WHEN d.pruned_files_sum + d.read_files_sum = 0 THEN 'NOT_ASSESSED'
           WHEN d.read_bytes_sum < :min_read_bytes THEN 'NOT_ASSESSED'
           WHEN d.pruned_files_sum * 1.0 / NULLIF(d.pruned_files_sum + d.read_files_sum, 0) < :crit_prune_ratio THEN 'CRITICAL'
           WHEN d.pruned_files_sum * 1.0 / NULLIF(d.pruned_files_sum + d.read_files_sum, 0) < :warn_prune_ratio THEN 'WARN'
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
       -- status: a day below :min_read_bytes is already NOT_ASSESSED (day_status), and a CRITICAL day
       -- that is a one-off (days_flagged below :min_recurring_flagged_days) is capped to WARN.
       CASE
         WHEN e.day_status = 'CRITICAL' AND e.days_flagged < :min_recurring_flagged_days THEN 'WARN'
         ELSE e.day_status
       END AS status
FROM enriched e
LEFT JOIN latest_wh lw ON lw.warehouse_id = e.warehouse_id
ORDER BY e.pruning_ratio ASC NULLS LAST
