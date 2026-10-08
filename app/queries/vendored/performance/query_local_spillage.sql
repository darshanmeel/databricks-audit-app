-- query_id: query_local_spillage
-- title: Queries spilling to local disk
-- domain: performance   tier: standard
-- reads: system.query.history
-- requires: SELECT on system.query; GA (system.query.history is generally available)
-- empty_if: schema_not_enabled, preview_unavailable, compute_scope_gap
-- params: :period_days (default 30) rolling window in days; :warn_spill_gb (default 1) daily local-spill GB per warehouse that flags WARN; :crit_spill_gb (default 10) daily local-spill GB that flags CRITICAL
-- confidence: confirmed
-- confidence_note: Columns verified against system.query.history in a live workspace.
-- read_this: One row = a day + warehouse + user whose queries spilled to local disk. The column that matters is spilled_local_bytes_sum - bytes a shuffle/sort/join had to write to local SSD because it did not fit in memory. Steady spill on the same warehouse means under-provisioned memory or a skewed query, not a one-off.
-- healthy: spilled_local_bytes_sum below :warn_spill_gb GB/day per warehouse (field heuristic - tune :warn_spill_gb for your account).
-- investigate_if: spilled_local_bytes_sum at/above :warn_spill_gb GB/day (WARN) or :crit_spill_gb GB/day (CRITICAL) - field heuristic; sustained daily spill on the same warehouse is the real signal.
-- actions: 1) rewrite the query to cut shuffle - broadcast the small side, filter earlier, avoid exploding joins (free); 2) raise shuffle partitions or enable disk caching, or move the workload to a warehouse with more memory per node (config); 3) size up to a memory-optimized node type or a larger warehouse (spend).
-- next: query_shuffle_write_amplification (if shuffle_read_bytes_sum is also high), query_costly_statements (if you want the exact statements behind a spilling warehouse)
-- caveats: LOCAL spill ONLY. Databricks system tables expose no spilled_remote_bytes column, so remote spill (disk-to-object-store) is NOT assessed - read "no rows" as "not measured", never as "no spill". Spill is a within-warehouse memory-pressure signal and carries no dollar figure. executed_by is masked in-SQL to DEC-66.3's identity format (executed_by_user_id, or when that is NULL the first 8 hex chars of sha2(lower(trim(executed_by)), 256), + a space + the first 2 chars of the raw identity + '***'; NULL/'__REDACTED__' and a service-principal GUID are kept as-is). Classic-cluster spills can be absent from system.query.history, so this leans toward serverless / DBSQL workloads. history is per-region.
SELECT date(start_time) AS day, workspace_id, compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
       CASE
         WHEN executed_by IS NULL OR executed_by = '__REDACTED__' THEN executed_by
         WHEN executed_by RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN executed_by
         ELSE concat(COALESCE(MAX(executed_by_user_id), substr(sha2(lower(trim(executed_by)), 256), 1, 8)), ' ', substr(executed_by, 1, 2), '***')
       END AS executed_by,
       COUNT(*) AS spilling_query_count,
       SUM(spilled_local_bytes) AS spilled_local_bytes_sum,
       MAX(spilled_local_bytes) AS spilled_local_bytes_max,
       SUM(total_duration_ms)     AS total_duration_ms_sum,
       SUM(execution_duration_ms) AS execution_duration_ms_sum,
       SUM(shuffle_read_bytes)    AS shuffle_read_bytes_sum,
       -- status: worst-first band on daily local spill (field heuristic; :warn_spill_gb / :crit_spill_gb).
       CASE
         WHEN SUM(spilled_local_bytes) >= :crit_spill_gb * 1e9 THEN 'CRITICAL'
         WHEN SUM(spilled_local_bytes) >= :warn_spill_gb * 1e9 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM system.query.history
WHERE start_time >= current_date() - INTERVAL :period_days DAYS
  AND start_time < current_date()
  AND spilled_local_bytes > 0
GROUP BY date(start_time), workspace_id, compute.type, compute.warehouse_id, executed_by
ORDER BY spilled_local_bytes_sum DESC
