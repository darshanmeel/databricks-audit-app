-- query_id: access_source_table_coverage
-- title: System table coverage - does every system table this app reads have fresh, gap-free data
-- domain: governance_access   tier: lite
-- reads: system.access.audit, system.access.column_lineage, system.access.inbound_network,
--   system.access.outbound_network, system.access.table_lineage, system.access.workspaces_latest,
--   system.ai_gateway.usage, system.billing.attributed_usage, system.billing.list_prices,
--   system.billing.usage, system.compute.clusters, system.compute.instance_events,
--   system.compute.instance_pools, system.compute.node_timeline, system.compute.node_types,
--   system.compute.warehouse_events, system.compute.warehouses,
--   system.information_schema.catalog_privileges, system.information_schema.column_masks,
--   system.information_schema.column_tags, system.information_schema.connection_privileges,
--   system.information_schema.credential_privileges,
--   system.information_schema.external_location_privileges, system.information_schema.row_filters,
--   system.information_schema.schema_privileges, system.information_schema.schema_share_usage,
--   system.information_schema.schema_tags, system.information_schema.share_recipient_privileges,
--   system.information_schema.shares, system.information_schema.table_privileges,
--   system.information_schema.table_share_usage, system.information_schema.table_tags,
--   system.information_schema.tables, system.information_schema.views,
--   system.information_schema.volume_tags, system.information_schema.volumes,
--   system.lakeflow.job_run_timeline, system.lakeflow.job_task_run_timeline,
--   system.lakeflow.job_tasks, system.lakeflow.jobs, system.lakeflow.pipeline_update_timeline,
--   system.lakeflow.pipelines, system.query.history, system.serving.endpoint_usage,
--   system.serving.served_entities
-- requires: SELECT on system.access, system.ai_gateway, system.billing, system.compute, system.information_schema, system.lakeflow, system.query and system.serving; most are GA but several (system.ai_gateway.usage, system.access.inbound_network/outbound_network, system.serving.*) are Public Preview - a missing preview table fails this whole statement (see caveats). Two system tables are deliberately left out of this inventory rather than risk failing it entirely - see caveats: system.data_classification.results (Public Preview, a separate opt-in schema most accounts do not have) and system.storage.predictive_optimization_operations_history (Public Preview, needs Predictive Optimization enabled).
-- empty_if: schema_not_enabled
-- params: :period_days (default 30) rolling window in days used for the window-scoped columns
--   only (rows_in_window, days_with_data_in_window, first/last_date_in_window); the full-history
--   columns (min_time, max_time, lag_days, total_row_count) always cover the table's whole
--   history regardless of this value
-- confidence: needs_confirmation
-- confidence_note: Every column read here (COUNT, MIN, MAX on each table's own documented time
--   column) is a plain aggregate, not new logic, but this exact 45-table shape has not run on a live
--   account yet. Confirm on your own account: 1) this statement completes at all (see caveats on what
--   a missing table does to it); 2) a table you know stopped updating some days ago shows a matching
--   lag_days and a WARN/CRITICAL status, not OK.
-- read_this: One row = one system table this app reads (45 tables; see requires for the two left
--   out). For a table with a time column: rows_in_window/days_with_data_in_window/first_date_in_
--   window/last_date_in_window are clipped to the last :period_days days; min_time/max_time/lag_days
--   are the table's FULL history, never clipped, so a table can show real rows in the window while
--   lag_days still proves it has gone stale since. For a table with no time column (a privilege, tag
--   or config inventory, list_prices, node_types) only total_row_count is measured - there is no
--   window to be stale or gappy in.
-- healthy: status = OK - either the table fills every day (see investigate_if) and has no gap or
--   lag, or it is one of the other 40 tables, which always read OK (see caveats) - counts are shown
--   for reference, a table that only changes sometimes or logs sparse activity has no "should have
--   a row today" expectation.
-- investigate_if: gap/lag status is judged only for the 5 tables that fill every day - billing.
--   usage, access.audit, query.history, compute.node_timeline, lakeflow.job_run_timeline. For those:
--   status = CRITICAL - the table has zero rows at all: a full gap, not just a stale window.
--   status = WARN - zero rows in the window, OR missing one or more days inside the window
--   (days_with_data_in_window < :period_days - 1), OR whose newest row anywhere is more than 2 days
--   old (lag_days > 2). The other 40 tables (change-time tables that only update sometimes, and
--   tables that are sparse or legitimately empty on many accounts) always read OK - read their
--   counts and lag_days for reference, never as a finding.
-- actions: 1) for a fills-every-day table read CRITICAL, enable the missing system schema for this
--   table - Unity Catalog system schemas, per catalog (free); 2) for WARN with a real gap
--   or a stale newest row, check whether the underlying feature that populates the table (verbose
--   audit logging, a job/pipeline that ran, a warehouse that queried) is actually active in the
--   window - the table itself is fine, the source activity is not (free); 3) for WARN on a lag
--   alone, re-run this check tomorrow before assuming the table stopped for good (free).
-- next: n/a
-- caveats: This intentionally excludes system.data_classification.results and system.storage.
--   predictive_optimization_operations_history - both Public Preview, opt-in features many accounts
--   do not have enabled. A missing table errors the WHOLE statement on Databricks SQL (there is no
--   per-branch try/catch across a UNION ALL - the engine resolves every referenced table before
--   running any of it), so a single-statement inventory cannot degrade row-by-row for a table that
--   might not exist; excluding a preview table here is the only way this check can still run on an
--   account that lacks it. If this statement itself fails, the account is missing one of the 45 core
--   tables below entirely (not just short on rows) - each SELECT block is preceded by a `-- schema.
--   table` comment naming it; delete that block plus the UNION ALL joining it to the next block (or
--   the previous one, if it is the last block) to isolate and skip the missing table, then re-run to
--   find the next one if more than one is absent. Every date/timestamp output is CAST to STRING so
--   DATE and TIMESTAMP columns can share one UNION ALL result shape. rows_in_window/total_row_count
--   and days_with_data_in_window/lag_days are NULL for a table with no time column - there is no
--   window to measure. STATUS SCOPE - only the 5 tables that fill every day (billing.usage, access.
--   audit, query.history, compute.node_timeline, lakeflow.job_run_timeline) run the gap/lag state
--   machine; the other 40 - change-time tables (clusters, warehouses, jobs, job_tasks, pipelines,
--   instance_pools, served_entities) that only update sometimes, and tables that are sparse
--   (inbound/outbound_network, ai_gateway.usage, endpoint_usage) or legitimately empty on many
--   accounts (column_masks, row_filters, shares*, connection_privileges) - always read OK regardless
--   of row count, gap or lag; a real WARN/CRITICAL on one of them would be a false alarm, not a
--   finding. This does not attempt to detect a table outside this list entirely (a future
--   system table this app has not started reading yet).
WITH unioned AS (
-- access.audit
SELECT 'system' AS catalog_name, 'access' AS schema_name, 'audit' AS table_name,
       TRUE AS has_time_column, 'event_date' AS time_column_name,
       SUM(CASE WHEN event_date >= date_sub(current_date(), :period_days) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(event_date) AS STRING) AS min_time,
       CAST(MAX(event_date) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN event_date >= date_sub(current_date(), :period_days) THEN CAST(event_date AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN event_date >= date_sub(current_date(), :period_days) THEN CAST(event_date AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN event_date >= date_sub(current_date(), :period_days) THEN CAST(event_date AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(event_date) AS DATE)) AS lag_days,
       CASE WHEN COUNT(*) = 0 THEN 'CRITICAL'
            WHEN SUM(CASE WHEN event_date >= date_sub(current_date(), :period_days) THEN 1 ELSE 0 END) = 0 THEN 'WARN'
            WHEN COUNT(DISTINCT CASE WHEN event_date >= date_sub(current_date(), :period_days) THEN CAST(event_date AS DATE) END) < (:period_days - 1) THEN 'WARN'
            WHEN datediff(current_date(), CAST(MAX(event_date) AS DATE)) > 2 THEN 'WARN'
            ELSE 'OK' END AS status
FROM system.access.audit
UNION ALL
-- access.column_lineage
SELECT 'system' AS catalog_name, 'access' AS schema_name, 'column_lineage' AS table_name,
       TRUE AS has_time_column, 'event_date' AS time_column_name,
       SUM(CASE WHEN event_date >= date_sub(current_date(), :period_days) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(event_date) AS STRING) AS min_time,
       CAST(MAX(event_date) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN event_date >= date_sub(current_date(), :period_days) THEN CAST(event_date AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN event_date >= date_sub(current_date(), :period_days) THEN CAST(event_date AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN event_date >= date_sub(current_date(), :period_days) THEN CAST(event_date AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(event_date) AS DATE)) AS lag_days,
       'OK' AS status
FROM system.access.column_lineage
UNION ALL
-- access.inbound_network
SELECT 'system' AS catalog_name, 'access' AS schema_name, 'inbound_network' AS table_name,
       TRUE AS has_time_column, 'event_time' AS time_column_name,
       SUM(CASE WHEN event_time >= date_sub(current_timestamp(), :period_days) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(event_time) AS STRING) AS min_time,
       CAST(MAX(event_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN event_time >= date_sub(current_timestamp(), :period_days) THEN CAST(event_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN event_time >= date_sub(current_timestamp(), :period_days) THEN CAST(event_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN event_time >= date_sub(current_timestamp(), :period_days) THEN CAST(event_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(event_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM system.access.inbound_network
UNION ALL
-- access.outbound_network
SELECT 'system' AS catalog_name, 'access' AS schema_name, 'outbound_network' AS table_name,
       TRUE AS has_time_column, 'event_time' AS time_column_name,
       SUM(CASE WHEN event_time >= date_sub(current_timestamp(), :period_days) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(event_time) AS STRING) AS min_time,
       CAST(MAX(event_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN event_time >= date_sub(current_timestamp(), :period_days) THEN CAST(event_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN event_time >= date_sub(current_timestamp(), :period_days) THEN CAST(event_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN event_time >= date_sub(current_timestamp(), :period_days) THEN CAST(event_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(event_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM system.access.outbound_network
UNION ALL
-- access.table_lineage
SELECT 'system' AS catalog_name, 'access' AS schema_name, 'table_lineage' AS table_name,
       TRUE AS has_time_column, 'event_date' AS time_column_name,
       SUM(CASE WHEN event_date >= date_sub(current_date(), :period_days) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(event_date) AS STRING) AS min_time,
       CAST(MAX(event_date) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN event_date >= date_sub(current_date(), :period_days) THEN CAST(event_date AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN event_date >= date_sub(current_date(), :period_days) THEN CAST(event_date AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN event_date >= date_sub(current_date(), :period_days) THEN CAST(event_date AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(event_date) AS DATE)) AS lag_days,
       'OK' AS status
FROM system.access.table_lineage
UNION ALL
-- access.workspaces_latest
SELECT 'system' AS catalog_name, 'access' AS schema_name, 'workspaces_latest' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM system.access.workspaces_latest
UNION ALL
-- ai_gateway.usage
SELECT 'system' AS catalog_name, 'ai_gateway' AS schema_name, 'usage' AS table_name,
       TRUE AS has_time_column, 'event_time' AS time_column_name,
       SUM(CASE WHEN event_time >= date_sub(current_timestamp(), :period_days) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(event_time) AS STRING) AS min_time,
       CAST(MAX(event_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN event_time >= date_sub(current_timestamp(), :period_days) THEN CAST(event_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN event_time >= date_sub(current_timestamp(), :period_days) THEN CAST(event_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN event_time >= date_sub(current_timestamp(), :period_days) THEN CAST(event_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(event_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM system.ai_gateway.usage
UNION ALL
-- billing.attributed_usage
SELECT 'system' AS catalog_name, 'billing' AS schema_name, 'attributed_usage' AS table_name,
       TRUE AS has_time_column, 'usage_date' AS time_column_name,
       SUM(CASE WHEN usage_date >= date_sub(current_date(), :period_days) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(usage_date) AS STRING) AS min_time,
       CAST(MAX(usage_date) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN usage_date >= date_sub(current_date(), :period_days) THEN CAST(usage_date AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN usage_date >= date_sub(current_date(), :period_days) THEN CAST(usage_date AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN usage_date >= date_sub(current_date(), :period_days) THEN CAST(usage_date AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(usage_date) AS DATE)) AS lag_days,
       'OK' AS status
FROM system.billing.attributed_usage
UNION ALL
-- billing.list_prices
SELECT 'system' AS catalog_name, 'billing' AS schema_name, 'list_prices' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM system.billing.list_prices
UNION ALL
-- billing.usage
SELECT 'system' AS catalog_name, 'billing' AS schema_name, 'usage' AS table_name,
       TRUE AS has_time_column, 'usage_date' AS time_column_name,
       SUM(CASE WHEN usage_date >= date_sub(current_date(), :period_days) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(usage_date) AS STRING) AS min_time,
       CAST(MAX(usage_date) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN usage_date >= date_sub(current_date(), :period_days) THEN CAST(usage_date AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN usage_date >= date_sub(current_date(), :period_days) THEN CAST(usage_date AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN usage_date >= date_sub(current_date(), :period_days) THEN CAST(usage_date AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(usage_date) AS DATE)) AS lag_days,
       CASE WHEN COUNT(*) = 0 THEN 'CRITICAL'
            WHEN SUM(CASE WHEN usage_date >= date_sub(current_date(), :period_days) THEN 1 ELSE 0 END) = 0 THEN 'WARN'
            WHEN COUNT(DISTINCT CASE WHEN usage_date >= date_sub(current_date(), :period_days) THEN CAST(usage_date AS DATE) END) < (:period_days - 1) THEN 'WARN'
            WHEN datediff(current_date(), CAST(MAX(usage_date) AS DATE)) > 2 THEN 'WARN'
            ELSE 'OK' END AS status
FROM system.billing.usage
UNION ALL
-- compute.clusters
SELECT 'system' AS catalog_name, 'compute' AS schema_name, 'clusters' AS table_name,
       TRUE AS has_time_column, 'change_time' AS time_column_name,
       SUM(CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(change_time) AS STRING) AS min_time,
       CAST(MAX(change_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN CAST(change_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN CAST(change_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN CAST(change_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(change_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM system.compute.clusters
UNION ALL
-- compute.instance_events
SELECT 'system' AS catalog_name, 'compute' AS schema_name, 'instance_events' AS table_name,
       TRUE AS has_time_column, 'event_time' AS time_column_name,
       SUM(CASE WHEN event_time >= date_sub(current_timestamp(), :period_days) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(event_time) AS STRING) AS min_time,
       CAST(MAX(event_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN event_time >= date_sub(current_timestamp(), :period_days) THEN CAST(event_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN event_time >= date_sub(current_timestamp(), :period_days) THEN CAST(event_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN event_time >= date_sub(current_timestamp(), :period_days) THEN CAST(event_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(event_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM system.compute.instance_events
UNION ALL
-- compute.instance_pools
SELECT 'system' AS catalog_name, 'compute' AS schema_name, 'instance_pools' AS table_name,
       TRUE AS has_time_column, 'change_time' AS time_column_name,
       SUM(CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(change_time) AS STRING) AS min_time,
       CAST(MAX(change_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN CAST(change_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN CAST(change_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN CAST(change_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(change_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM system.compute.instance_pools
UNION ALL
-- compute.node_timeline
SELECT 'system' AS catalog_name, 'compute' AS schema_name, 'node_timeline' AS table_name,
       TRUE AS has_time_column, 'start_time' AS time_column_name,
       SUM(CASE WHEN start_time >= date_sub(current_timestamp(), :period_days) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(start_time) AS STRING) AS min_time,
       CAST(MAX(start_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN start_time >= date_sub(current_timestamp(), :period_days) THEN CAST(start_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN start_time >= date_sub(current_timestamp(), :period_days) THEN CAST(start_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN start_time >= date_sub(current_timestamp(), :period_days) THEN CAST(start_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(start_time) AS DATE)) AS lag_days,
       CASE WHEN COUNT(*) = 0 THEN 'CRITICAL'
            WHEN SUM(CASE WHEN start_time >= date_sub(current_timestamp(), :period_days) THEN 1 ELSE 0 END) = 0 THEN 'WARN'
            WHEN COUNT(DISTINCT CASE WHEN start_time >= date_sub(current_timestamp(), :period_days) THEN CAST(start_time AS DATE) END) < (:period_days - 1) THEN 'WARN'
            WHEN datediff(current_date(), CAST(MAX(start_time) AS DATE)) > 2 THEN 'WARN'
            ELSE 'OK' END AS status
FROM system.compute.node_timeline
UNION ALL
-- compute.node_types
SELECT 'system' AS catalog_name, 'compute' AS schema_name, 'node_types' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM system.compute.node_types
UNION ALL
-- compute.warehouse_events
SELECT 'system' AS catalog_name, 'compute' AS schema_name, 'warehouse_events' AS table_name,
       TRUE AS has_time_column, 'event_time' AS time_column_name,
       SUM(CASE WHEN event_time >= date_sub(current_timestamp(), :period_days) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(event_time) AS STRING) AS min_time,
       CAST(MAX(event_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN event_time >= date_sub(current_timestamp(), :period_days) THEN CAST(event_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN event_time >= date_sub(current_timestamp(), :period_days) THEN CAST(event_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN event_time >= date_sub(current_timestamp(), :period_days) THEN CAST(event_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(event_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM system.compute.warehouse_events
UNION ALL
-- compute.warehouses
SELECT 'system' AS catalog_name, 'compute' AS schema_name, 'warehouses' AS table_name,
       TRUE AS has_time_column, 'change_time' AS time_column_name,
       SUM(CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(change_time) AS STRING) AS min_time,
       CAST(MAX(change_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN CAST(change_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN CAST(change_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN CAST(change_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(change_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM system.compute.warehouses
UNION ALL
-- information_schema.catalog_privileges
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'catalog_privileges' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM system.information_schema.catalog_privileges
UNION ALL
-- information_schema.column_masks
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'column_masks' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM system.information_schema.column_masks
UNION ALL
-- information_schema.column_tags
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'column_tags' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM system.information_schema.column_tags
UNION ALL
-- information_schema.connection_privileges
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'connection_privileges' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM system.information_schema.connection_privileges
UNION ALL
-- information_schema.credential_privileges
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'credential_privileges' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM system.information_schema.credential_privileges
UNION ALL
-- information_schema.external_location_privileges
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'external_location_privileges' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM system.information_schema.external_location_privileges
UNION ALL
-- information_schema.row_filters
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'row_filters' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM system.information_schema.row_filters
UNION ALL
-- information_schema.schema_privileges
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'schema_privileges' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM system.information_schema.schema_privileges
UNION ALL
-- information_schema.schema_share_usage
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'schema_share_usage' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM system.information_schema.schema_share_usage
UNION ALL
-- information_schema.schema_tags
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'schema_tags' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM system.information_schema.schema_tags
UNION ALL
-- information_schema.share_recipient_privileges
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'share_recipient_privileges' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM system.information_schema.share_recipient_privileges
UNION ALL
-- information_schema.shares
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'shares' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM system.information_schema.shares
UNION ALL
-- information_schema.table_privileges
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'table_privileges' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM system.information_schema.table_privileges
UNION ALL
-- information_schema.table_share_usage
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'table_share_usage' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM system.information_schema.table_share_usage
UNION ALL
-- information_schema.table_tags
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'table_tags' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM system.information_schema.table_tags
UNION ALL
-- information_schema.tables
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'tables' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM system.information_schema.tables
UNION ALL
-- information_schema.views
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'views' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM system.information_schema.views
UNION ALL
-- information_schema.volume_tags
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'volume_tags' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM system.information_schema.volume_tags
UNION ALL
-- information_schema.volumes
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'volumes' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM system.information_schema.volumes
UNION ALL
-- lakeflow.job_run_timeline
SELECT 'system' AS catalog_name, 'lakeflow' AS schema_name, 'job_run_timeline' AS table_name,
       TRUE AS has_time_column, 'period_start_time' AS time_column_name,
       SUM(CASE WHEN period_start_time >= date_sub(current_timestamp(), :period_days) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(period_start_time) AS STRING) AS min_time,
       CAST(MAX(period_start_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN period_start_time >= date_sub(current_timestamp(), :period_days) THEN CAST(period_start_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN period_start_time >= date_sub(current_timestamp(), :period_days) THEN CAST(period_start_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN period_start_time >= date_sub(current_timestamp(), :period_days) THEN CAST(period_start_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(period_start_time) AS DATE)) AS lag_days,
       CASE WHEN COUNT(*) = 0 THEN 'CRITICAL'
            WHEN SUM(CASE WHEN period_start_time >= date_sub(current_timestamp(), :period_days) THEN 1 ELSE 0 END) = 0 THEN 'WARN'
            WHEN COUNT(DISTINCT CASE WHEN period_start_time >= date_sub(current_timestamp(), :period_days) THEN CAST(period_start_time AS DATE) END) < (:period_days - 1) THEN 'WARN'
            WHEN datediff(current_date(), CAST(MAX(period_start_time) AS DATE)) > 2 THEN 'WARN'
            ELSE 'OK' END AS status
FROM system.lakeflow.job_run_timeline
UNION ALL
-- lakeflow.job_task_run_timeline
SELECT 'system' AS catalog_name, 'lakeflow' AS schema_name, 'job_task_run_timeline' AS table_name,
       TRUE AS has_time_column, 'period_start_time' AS time_column_name,
       SUM(CASE WHEN period_start_time >= date_sub(current_timestamp(), :period_days) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(period_start_time) AS STRING) AS min_time,
       CAST(MAX(period_start_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN period_start_time >= date_sub(current_timestamp(), :period_days) THEN CAST(period_start_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN period_start_time >= date_sub(current_timestamp(), :period_days) THEN CAST(period_start_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN period_start_time >= date_sub(current_timestamp(), :period_days) THEN CAST(period_start_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(period_start_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM system.lakeflow.job_task_run_timeline
UNION ALL
-- lakeflow.job_tasks
SELECT 'system' AS catalog_name, 'lakeflow' AS schema_name, 'job_tasks' AS table_name,
       TRUE AS has_time_column, 'change_time' AS time_column_name,
       SUM(CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(change_time) AS STRING) AS min_time,
       CAST(MAX(change_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN CAST(change_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN CAST(change_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN CAST(change_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(change_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM system.lakeflow.job_tasks
UNION ALL
-- lakeflow.jobs
SELECT 'system' AS catalog_name, 'lakeflow' AS schema_name, 'jobs' AS table_name,
       TRUE AS has_time_column, 'change_time' AS time_column_name,
       SUM(CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(change_time) AS STRING) AS min_time,
       CAST(MAX(change_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN CAST(change_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN CAST(change_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN CAST(change_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(change_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM system.lakeflow.jobs
UNION ALL
-- lakeflow.pipeline_update_timeline
SELECT 'system' AS catalog_name, 'lakeflow' AS schema_name, 'pipeline_update_timeline' AS table_name,
       TRUE AS has_time_column, 'period_start_time' AS time_column_name,
       SUM(CASE WHEN period_start_time >= date_sub(current_timestamp(), :period_days) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(period_start_time) AS STRING) AS min_time,
       CAST(MAX(period_start_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN period_start_time >= date_sub(current_timestamp(), :period_days) THEN CAST(period_start_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN period_start_time >= date_sub(current_timestamp(), :period_days) THEN CAST(period_start_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN period_start_time >= date_sub(current_timestamp(), :period_days) THEN CAST(period_start_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(period_start_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM system.lakeflow.pipeline_update_timeline
UNION ALL
-- lakeflow.pipelines
SELECT 'system' AS catalog_name, 'lakeflow' AS schema_name, 'pipelines' AS table_name,
       TRUE AS has_time_column, 'change_time' AS time_column_name,
       SUM(CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(change_time) AS STRING) AS min_time,
       CAST(MAX(change_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN CAST(change_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN CAST(change_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN CAST(change_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(change_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM system.lakeflow.pipelines
UNION ALL
-- query.history
SELECT 'system' AS catalog_name, 'query' AS schema_name, 'history' AS table_name,
       TRUE AS has_time_column, 'start_time' AS time_column_name,
       SUM(CASE WHEN start_time >= date_sub(current_timestamp(), :period_days) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(start_time) AS STRING) AS min_time,
       CAST(MAX(start_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN start_time >= date_sub(current_timestamp(), :period_days) THEN CAST(start_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN start_time >= date_sub(current_timestamp(), :period_days) THEN CAST(start_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN start_time >= date_sub(current_timestamp(), :period_days) THEN CAST(start_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(start_time) AS DATE)) AS lag_days,
       CASE WHEN COUNT(*) = 0 THEN 'CRITICAL'
            WHEN SUM(CASE WHEN start_time >= date_sub(current_timestamp(), :period_days) THEN 1 ELSE 0 END) = 0 THEN 'WARN'
            WHEN COUNT(DISTINCT CASE WHEN start_time >= date_sub(current_timestamp(), :period_days) THEN CAST(start_time AS DATE) END) < (:period_days - 1) THEN 'WARN'
            WHEN datediff(current_date(), CAST(MAX(start_time) AS DATE)) > 2 THEN 'WARN'
            ELSE 'OK' END AS status
FROM system.query.history
UNION ALL
-- serving.endpoint_usage
SELECT 'system' AS catalog_name, 'serving' AS schema_name, 'endpoint_usage' AS table_name,
       TRUE AS has_time_column, 'request_time' AS time_column_name,
       SUM(CASE WHEN request_time >= date_sub(current_timestamp(), :period_days) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(request_time) AS STRING) AS min_time,
       CAST(MAX(request_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN request_time >= date_sub(current_timestamp(), :period_days) THEN CAST(request_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN request_time >= date_sub(current_timestamp(), :period_days) THEN CAST(request_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN request_time >= date_sub(current_timestamp(), :period_days) THEN CAST(request_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(request_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM system.serving.endpoint_usage
UNION ALL
-- serving.served_entities
SELECT 'system' AS catalog_name, 'serving' AS schema_name, 'served_entities' AS table_name,
       TRUE AS has_time_column, 'change_time' AS time_column_name,
       SUM(CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(change_time) AS STRING) AS min_time,
       CAST(MAX(change_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN CAST(change_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN CAST(change_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN change_time >= date_sub(current_timestamp(), :period_days) THEN CAST(change_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(change_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM system.serving.served_entities
)
SELECT * FROM unioned
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, schema_name, table_name
