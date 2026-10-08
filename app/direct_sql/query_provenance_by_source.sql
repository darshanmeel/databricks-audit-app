-- generated from dbt/models/databricks_direct/performance/d_query_provenance_by_source.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/performance/query_provenance_by_source.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT workspace_id,
       compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
       CASE WHEN executed_by IS NULL              THEN 'unknown'
            WHEN executed_by LIKE '%@%'           THEN 'user'
            ELSE 'service_principal' END AS identity_type,
       executed_by AS executed_by,
       -- same precedence as query_top_by_cost, so a statement with several origins (an alert run
       -- by a job) gets one label on every page
       CASE WHEN query_source.sql_query_id        IS NOT NULL THEN 'sql_editor'
            WHEN query_source.dashboard_id        IS NOT NULL THEN 'dashboard'
            WHEN query_source.legacy_dashboard_id IS NOT NULL THEN 'legacy_dashboard'
            WHEN query_source.genie_space_id      IS NOT NULL THEN 'genie'
            WHEN query_source.alert_id            IS NOT NULL THEN 'alert'
            WHEN query_source.job_info.job_id     IS NOT NULL THEN 'job'
            WHEN query_source.notebook_id         IS NOT NULL THEN 'notebook'
            ELSE 'other' END AS source_kind,
       query_source.job_info.job_id AS job_id,
       query_source.dashboard_id    AS dashboard_id,
       query_source.notebook_id     AS notebook_id,
       COUNT(*) AS query_count,
       SUM(execution_duration_ms) AS execution_duration_ms_sum,
       SUM(total_duration_ms)     AS total_duration_ms_sum,
       SUM(read_bytes)            AS read_bytes_sum
FROM `system`.`query`.`history`
WHERE start_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
  AND start_time < __AS_OF_DATE__
GROUP BY workspace_id, compute.type, compute.warehouse_id, source_kind,
         CASE WHEN executed_by IS NULL              THEN 'unknown'
              WHEN executed_by LIKE '%@%'           THEN 'user'
              ELSE 'service_principal' END,
         executed_by,
         query_source.job_info.job_id, query_source.dashboard_id, query_source.notebook_id
ORDER BY workspace_id, compute_type, source_kind
) q
