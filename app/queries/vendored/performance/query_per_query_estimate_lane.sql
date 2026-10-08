-- query_id: query_per_query_estimate_lane
-- title: Per-query duration and bytes for cost-estimate allocation
-- domain: performance   tier: deep
-- reads: system.query.history
-- requires: SELECT on system.query; GA (system.query.history is generally available)
-- empty_if: schema_not_enabled, preview_unavailable, compute_scope_gap
-- params: :period_days (default 30) rolling window in days
-- confidence: confirmed
-- confidence_note: Columns verified against system.query.history in a live workspace; this query returns raw per-statement rows for per-statement cost allocation, not an aggregate.
-- read_this: One row = one finished, non-cached query on a SQL warehouse or serverless compute. The columns that matter are execution_duration_ms (used to allocate warehouse DBU cost across queries) and waiting_for_compute_duration_ms (should be netted out before allocating, since it is provisioning wait, not query work). There is no per-query dollar or DBU column here - use this as the weighting input for spreading system.billing.usage's hourly warehouse DBU total across the queries that ran in that hour, not as a cost figure on its own.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: query_costly_statements (for the same per-query granularity with pruning/shuffle/spill signals attached), cost_dollarized_by_sku_day (for the aggregate dollar figure this table feeds into)
-- caveats: This is an ESTIMATE-LANE INPUT ONLY - there is no per-query DBU or dollar column anywhere in system tables. To turn this into a dollar estimate, allocate system.billing.usage's hourly warehouse DBU total across the queries that ran in that hour, weighted by execution_duration_ms (net of waiting_for_compute_duration_ms), optionally further weighted by total_task_duration_ms or read_bytes, and reconcile so the per-query estimates sum back to the metered DBU for that hour. Serverless rows have warehouse_id NULL and are excluded here - serverless cost has to be attributed by job_id instead, from a different table. There is no query_hash column, so you cannot group same-shape queries together from this table alone. The result set can be large - narrow :period_days or add a duration threshold if you are running this ad hoc. This table is regional while billing data is global, so any dollar attribution you build on top of this is only valid within one region at a time. Classic clusters are absent from this table entirely.
SELECT date_trunc('HOUR', start_time) AS usage_hour, workspace_id,
       compute.warehouse_id AS warehouse_id, compute.type AS compute_type,
       CASE
         WHEN executed_by IS NULL OR executed_by = '__REDACTED__' THEN executed_by
         WHEN executed_by RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN executed_by
         ELSE concat(COALESCE(executed_by_user_id, substr(sha2(lower(trim(executed_by)), 256), 1, 8)), ' ', substr(executed_by, 1, 2), '***')
       END AS executed_by,
       statement_id, statement_type,
       execution_duration_ms, waiting_for_compute_duration_ms, total_task_duration_ms,
       read_bytes, total_duration_ms
FROM system.query.history
WHERE start_time >= current_date() - INTERVAL :period_days DAYS
  AND start_time < current_date()
  AND execution_status = 'FINISHED'
  AND from_result_cache = false
  AND execution_duration_ms > 0
  AND compute.warehouse_id IS NOT NULL
ORDER BY usage_hour, warehouse_id, statement_id
