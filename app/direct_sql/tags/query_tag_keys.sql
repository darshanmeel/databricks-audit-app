-- hand-written; not generated. Databricks twin of dbt/models/tags/query_tag_keys.sql: statements per
-- (window_days, unit_id, own_keys, origin, job_id, pipeline_id, notebook_id), where own_keys is the
-- sorted, comma-joined set of the query's OWN query_tags keys (normalised) -- so the Tags page can
-- tell which mandatory tags a query carried together, where it came from, and which job or pipeline
-- ran it. notebook_id is kept for serverless queries only, to find their usage policy's bill.
-- unit_id hashes exactly like perf_unit.sql. __WINDOW_DAYS__ is filled in at export time.
WITH history AS (
    SELECT __WINDOW_DAYS__ AS window_days, q.workspace_id,
           CASE WHEN q.compute.warehouse_id IS NOT NULL THEN 'warehouse'
                WHEN q.compute.cluster_id IS NOT NULL THEN 'cluster'
                ELSE 'serverless' END AS compute_kind,
           COALESCE(q.compute.warehouse_id, q.compute.cluster_id) AS compute_id,
           CASE WHEN q.query_tags IS NULL THEN ''
                ELSE array_join(array_sort(array_distinct(filter(
                         transform(map_keys(map_filter(q.query_tags, (k, v) -> v IS NOT NULL)),
                                   k -> regexp_replace(lower(k), '[ _-]+', '')),
                         k -> k <> ''))), ',')
           END AS own_keys,
           CASE WHEN q.query_source.job_info.job_id IS NOT NULL THEN 'job'
                WHEN q.query_source.pipeline_info.pipeline_id IS NOT NULL THEN 'pipeline'
                WHEN q.query_source.dashboard_id IS NOT NULL OR q.query_source.legacy_dashboard_id IS NOT NULL THEN 'dashboard'
                WHEN q.query_source.genie_space_id IS NOT NULL THEN 'genie'
                WHEN q.query_source.alert_id IS NOT NULL THEN 'alert'
                WHEN q.query_source.notebook_id IS NOT NULL THEN 'notebook'
                WHEN q.query_source.sql_query_id IS NOT NULL OR q.client_application ILIKE '%sql editor%' THEN 'sql_editor'
                WHEN q.client_application IS NOT NULL THEN 'tool'
                ELSE 'other' END AS origin,
           q.query_source.job_info.job_id AS job_id,
           q.query_source.pipeline_info.pipeline_id AS pipeline_id,
           CASE WHEN q.compute.warehouse_id IS NULL AND q.compute.cluster_id IS NULL
                THEN q.query_source.notebook_id END AS notebook_id
    FROM `system`.`query`.`history` q
    WHERE q.start_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
      AND q.start_time < __AS_OF_DATE__
)
SELECT
    window_days,
    md5(COALESCE(workspace_id, '~') || '|' || compute_kind || '|' || COALESCE(compute_id, '~')) AS unit_id,
    own_keys,
    origin,
    job_id,
    pipeline_id,
    notebook_id,
    COUNT(*) AS statements
FROM history
GROUP BY 1, 2, 3, 4, 5, 6, 7
