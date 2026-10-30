-- query_id: po_failure_reasons
-- title: Predictive optimization failures by table and reason, with estimated DBUs spent
-- domain: storage   tier: standard
-- reads: system.storage.predictive_optimization_operations_history
-- requires: SELECT on system.storage; Public Preview (system.storage.predictive_optimization_
--   operations_history), regional, 180-day retention - if the table is not enabled for this
--   metastore, this check errors and reads not assessed (system table not enabled) until it is.
-- empty_if: schema_not_enabled, po_not_enabled
-- params: :period_days (default 30) rolling window in days; :warn_failed_ops (default 5) failed
--   operations for a table+workspace+reason in the window that flags WARN; :crit_failed_ops (default 20)
--   that flags CRITICAL; :top_n (default 50) how many status=OK table+workspace+reason rows, ranked by
--   failed_operations descending, are kept as their own row before the rest are pooled into one
--   is_other=true row - every WARN/CRITICAL row is always kept as its own row, never pooled
-- confidence: needs_confirmation
-- confidence_note: the operation_status enum values (including the embedded-colon 'FAILED: ...'
--   strings) and usage_unit='ESTIMATED_DBU' are the same ones po_maintenance_cost_by_table already
--   confirms against a live account, but this query itself has not yet run live. The reason/fix
--   plain-language copy below is new and not yet read back to a user.
-- read_this: One row = one table x workspace_id (the workspace whose compute ran the operation) x
--   failure reason (`operation_status`, e.g. 'FAILED: INTERNAL_ERROR') in the window.
--   estimated_dbus_spent sums usage_quantity where
--   usage_unit = ESTIMATED_DBU on the failed operations only - DBUs spent on work that did not
--   complete (shared clusters are apportioned, so treat it as an estimate). reason/fix translate
--   operation_status into plain language for the three values Databricks documents; anything else
--   falls back to a generic reason/fix. is_other=true is the one pooled row for every status=OK
--   row ranked below :top_n by failed_operations (every other column NULL on that row,
--   pooled_count says how many); it always reads status OK.
-- healthy: OK - failed_operations under :warn_failed_ops for that table+workspace+reason (still
--   worth a look, not yet urgent).
-- investigate_if: WARN - failed_operations at/above :warn_failed_ops. CRITICAL - at/above
--   :crit_failed_ops.
-- actions: 1) for PRIVATE_LINK_SETUP_ERROR, fix the private link / network path between the
--   metastore and this table's storage (network config); 2) for
--   AUTO_TTL_COLUMN_DOES_NOT_EXIST_ERROR, point auto-TTL at a column that exists or drop the
--   auto-TTL setting (config); 3) for INTERNAL_ERROR, retry - if it keeps failing, open a support
--   ticket with operation_type and time (support).
-- next: po_maintenance_cost_by_table (this table's full maintenance cost, successes included)
-- caveats: operation_status is an enum of SUCCESSFUL or a literal 'FAILED: <REASON>' string
--   (embedded colon - do not split on the first colon). usage_quantity is ESTIMATED_DBU, not
--   dollars, and can lag the operation row by up to 24 hours while billing populates - the
--   start_time < current_date() guard drops today's rows but not yesterday's still-populating
--   values. system.storage.predictive_optimization_operations_history is Public Preview, regional,
--   and retains 180 days - do not set :period_days beyond that and expect rows. reason/fix cover
--   the three operation_status values Databricks' own docs list; an account-specific value not
--   listed here still gets a generic reason/fix, never an empty string. The :warn_failed_ops/
--   :crit_failed_ops bands are counted per (table, workspace, reason) - a table failing from two
--   workspaces in the window gets two rows, each banded on its own workspace's failure count, not
--   the table's account-wide total. POOLING - the is_other row (workspace_id NULL, since it can
--   pool rows from more than one workspace) only ever pools rows that were ALREADY status=OK on
--   their own, and it always reads status OK itself, however many failed operations the pooled
--   total holds - it never hides a WARN/CRITICAL row, which always keeps its own row regardless of
--   :top_n.
WITH ops AS (
  SELECT workspace_id, catalog_name, schema_name, table_name, table_id, operation_type, operation_status,
         usage_unit, usage_quantity, start_time
  FROM system.storage.predictive_optimization_operations_history
  WHERE start_time >= current_date() - INTERVAL :period_days DAYS
    AND start_time < current_date()
    AND operation_status LIKE 'FAILED%'
),
agg AS (
  SELECT workspace_id, catalog_name, schema_name, table_id, table_name,
         CONCAT(catalog_name, '.', schema_name, '.', table_name) AS full_name,
         operation_status,
         COUNT(*) AS failed_operations,
         COUNT(DISTINCT operation_type) AS operation_types_failed,
         SUM(CASE WHEN usage_unit = 'ESTIMATED_DBU' THEN usage_quantity ELSE 0 END) AS estimated_dbus_spent,
         MAX(start_time) AS last_failed_at
  FROM ops
  GROUP BY workspace_id, catalog_name, schema_name, table_id, table_name, operation_status
),
scored AS (
  SELECT a.*,
    CASE a.operation_status
      WHEN 'FAILED: PRIVATE_LINK_SETUP_ERROR' THEN 'network setup blocks predictive optimization from reaching this table'
      WHEN 'FAILED: AUTO_TTL_COLUMN_DOES_NOT_EXIST_ERROR' THEN 'auto-TTL is configured to a column that does not exist on this table'
      WHEN 'FAILED: INTERNAL_ERROR' THEN 'a Databricks-side error; usually resolves on retry'
      ELSE 'operation failed; see operation_status for detail'
    END AS reason,
    CASE a.operation_status
      WHEN 'FAILED: PRIVATE_LINK_SETUP_ERROR' THEN 'fix the private link / network path between the metastore and this table''s storage'
      WHEN 'FAILED: AUTO_TTL_COLUMN_DOES_NOT_EXIST_ERROR' THEN 'point auto-TTL at a column that exists, or drop the auto-TTL setting'
      WHEN 'FAILED: INTERNAL_ERROR' THEN 'retry; if it keeps failing, open a support ticket with the operation_type and time'
      ELSE 'check the table and re-run predictive optimization manually'
    END AS fix,
    CASE
      WHEN a.failed_operations >= :crit_failed_ops THEN 'CRITICAL'
      WHEN a.failed_operations >= :warn_failed_ops THEN 'WARN'
      ELSE 'OK'
    END AS status
  FROM agg a
),
flagged AS (
  -- Every non-OK row (WARN/CRITICAL) always keeps its own row - never pooled.
  SELECT workspace_id, catalog_name, schema_name, table_id, table_name, full_name, operation_status,
         FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
         operation_types_failed, failed_operations, estimated_dbus_spent, last_failed_at,
         reason, fix, status
  FROM scored
  WHERE status != 'OK'
),
ok_ranked AS (
  SELECT scored.*,
         ROW_NUMBER() OVER (ORDER BY failed_operations DESC, table_id, operation_status) AS rn
  FROM scored
  WHERE status = 'OK'
),
ok_kept AS (
  -- The top :top_n OK rows by failed_operations, kept as their own row.
  SELECT workspace_id, catalog_name, schema_name, table_id, table_name, full_name, operation_status,
         FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
         operation_types_failed, failed_operations, estimated_dbus_spent, last_failed_at,
         reason, fix, status
  FROM ok_ranked
  WHERE rn <= :top_n
),
ok_pooled_raw AS (
  -- Every remaining OK row beyond :top_n - re-aggregated into one is_other row below.
  SELECT * FROM ok_ranked WHERE rn > :top_n
),
other_row AS (
  SELECT
    -- Pooled across whichever workspaces its rows came from, so it can't claim a single one.
    CAST(NULL AS STRING) AS workspace_id,
    CAST(NULL AS STRING) AS catalog_name,
    CAST(NULL AS STRING) AS schema_name,
    CAST(NULL AS STRING) AS table_id,
    CAST(NULL AS STRING) AS table_name,
    CAST(NULL AS STRING) AS full_name,
    CAST(NULL AS STRING) AS operation_status,
    TRUE                  AS is_other,
    COUNT(*)              AS pooled_count,
    SUM(operation_types_failed) AS operation_types_failed,
    SUM(failed_operations)      AS failed_operations,
    ROUND(SUM(estimated_dbus_spent), 2) AS estimated_dbus_spent,
    MAX(last_failed_at)         AS last_failed_at,
    CAST(NULL AS STRING) AS reason,
    CAST(NULL AS STRING) AS fix,
    -- Every pooled row was already status=OK on its own - a rollup of small, healthy rows is never
    -- itself a finding, so this row always reads OK regardless of the combined failure count.
    'OK' AS status
  FROM ok_pooled_raw
)
SELECT * FROM (
  SELECT * FROM flagged
  UNION ALL
  SELECT * FROM ok_kept
  UNION ALL
  SELECT * FROM other_row WHERE pooled_count > 0
) u
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
         is_other,
         failed_operations DESC
