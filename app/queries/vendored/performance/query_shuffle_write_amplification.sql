-- query_id: query_shuffle_write_amplification
-- title: Shuffle-heavy queries and small-file write amplification
-- domain: performance   tier: standard
-- reads: system.query.history
-- requires: SELECT on system.query; GA (system.query.history is generally available)
-- empty_if: schema_not_enabled, preview_unavailable, compute_scope_gap
-- params: :period_days (default 30) rolling window in days; :warn_shuffle_gb (default 50) daily shuffle_read_bytes per group that flags WARN; :crit_shuffle_gb (default 200) ... that flags CRITICAL; :warn_avg_file_mb (default 32) average written-file size in MB below which a group flags WARN (small-file amplification); :crit_avg_file_mb (default 8) ... below which it flags CRITICAL
-- confidence: confirmed
-- confidence_note: Columns verified against system.query.history in a live workspace.
-- read_this: One row = a day + warehouse + identity + statement_type whose queries shuffled data or wrote files. Two independent signals live in this row: shuffle_read_bytes_sum (a shuffle-heavy or skewed-join signal) and written_files_sum versus written_bytes_sum (write amplification - lots of small files instead of a few well-sized ones). Check both; a query can be shuffle-heavy without writing anything, or write badly without shuffling.
-- healthy: shuffle_read_bytes_sum below :warn_shuffle_gb GB/day AND average written-file size at/above :warn_avg_file_mb MB (field heuristics - tune :warn_shuffle_gb / :warn_avg_file_mb for your account).
-- investigate_if: shuffle_read_bytes_sum at/above :warn_shuffle_gb GB (WARN) or :crit_shuffle_gb GB (CRITICAL), OR average written-file size below :warn_avg_file_mb MB (WARN) or :crit_avg_file_mb MB (CRITICAL) - field heuristics; either signal alone is enough to flag the row.
-- actions: 1) for shuffle, check the join/aggregation for a skewed key or a missing broadcast hint; for small files, check whether the write has a narrow partition-by or a streaming micro-batch producing many tiny files (free); 2) tune shuffle partitions and the broadcast-join threshold, or enable auto-compaction / optimized writes on the target table (config); 3) run OPTIMIZE to compact existing small files, or move the write to a warehouse with more memory to reduce spill-driven shuffle (spend).
-- next: query_local_spillage (if the same warehouse also spills - shuffle and spill often co-occur), query_pruning_effectiveness (small files hurt pruning too, on the read side)
-- caveats: shuffle_read_bytes is a shuffle-heavy / skewed-join signal, not a cost figure. written_files versus written_rows/written_bytes is what tells you about write amplification (many small files instead of a few well-sized ones) - neither column alone is meaningful, only their ratio. The WHERE clause only includes rows that actually shuffled or wrote something, so a quiet day for a warehouse means no matching rows here, not zero activity. This table is regional. Classic-cluster statements are absent from system.query.history entirely, so this leans toward SQL-warehouse and serverless workloads.
SELECT date(start_time) AS day, workspace_id, compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
       CASE
         WHEN executed_by IS NULL OR executed_by = '__REDACTED__' THEN executed_by
         WHEN executed_by RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN executed_by
         ELSE concat(COALESCE(MAX(executed_by_user_id), substr(sha2(lower(trim(executed_by)), 256), 1, 8)), ' ', substr(executed_by, 1, 2), '***')
       END AS executed_by,
       statement_type,
       COUNT(*) AS query_count,
       SUM(shuffle_read_bytes) AS shuffle_read_bytes_sum,
       SUM(written_bytes)      AS written_bytes_sum,
       SUM(written_rows)       AS written_rows_sum,
       SUM(written_files)      AS written_files_sum,
       SUM(read_bytes)         AS read_bytes_sum,
       -- status: worst-first band on shuffle volume OR small-file write amplification (field heuristic;
       -- :warn_shuffle_gb / :crit_shuffle_gb / :warn_avg_file_mb / :crit_avg_file_mb).
       CASE
         WHEN SUM(shuffle_read_bytes) >= :crit_shuffle_gb * 1e9
           OR (SUM(written_files) > 0 AND SUM(written_bytes) / SUM(written_files) < :crit_avg_file_mb * 1e6) THEN 'CRITICAL'
         WHEN SUM(shuffle_read_bytes) >= :warn_shuffle_gb * 1e9
           OR (SUM(written_files) > 0 AND SUM(written_bytes) / SUM(written_files) < :warn_avg_file_mb * 1e6) THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM system.query.history
WHERE start_time >= current_date() - INTERVAL :period_days DAYS
  AND start_time < current_date()
  AND (shuffle_read_bytes > 0 OR written_bytes > 0)
GROUP BY date(start_time), workspace_id, compute.type, compute.warehouse_id, executed_by, statement_type
ORDER BY shuffle_read_bytes_sum DESC
