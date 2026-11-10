-- hand-written; not generated. Databricks twin of dbt/models/tags/cost_unit.sql: one row per
-- (window_days, unit_id), every dollar of system.billing.usage split by the shared _cost_alloc.sql
-- block. __WINDOW_DAYS__ is filled in at export time, one window per run.
WITH
__COST_ALLOC_WINDOW__,
hashed AS (
    SELECT __WINDOW_DAYS__ AS window_days, a.*,
           md5(COALESCE(a.workspace_id, '~') || '|' || a.compute_kind || '|' || COALESCE(a.compute_id, '~')
               || '|' || a.work_kind || '|' || COALESCE(a.work_id, '~') || '|' || a.usage_unit || '|'
               || COALESCE(CASE WHEN a.custom_tags IS NULL OR size(a.custom_tags) = 0 THEN NULL
                                ELSE array_join(array_sort(transform(map_entries(a.custom_tags), e -> concat(e.key, '=', e.value))), ',')
                           END, '~')) AS unit_id
    FROM cost_alloc a
)
SELECT
    window_days,
    unit_id,
    ANY_VALUE(workspace_id) AS workspace_id,
    ANY_VALUE(compute_kind) AS compute_kind,
    ANY_VALUE(compute_id) AS compute_id,
    ANY_VALUE(work_kind) AS work_kind,
    ANY_VALUE(work_id) AS work_id,
    ANY_VALUE(usage_unit) AS usage_unit,
    COALESCE(SUM(CASE WHEN list_rate IS NOT NULL THEN qty * list_rate END), 0) AS usd,
    COALESCE(SUM(CASE WHEN list_rate IS NULL THEN qty ELSE 0 END), 0) AS unpriced_quantity,
    COALESCE(SUM(qty), 0) AS quantity,
    COUNT(DISTINCT rid) AS record_count
FROM hashed
GROUP BY window_days, unit_id
HAVING NOT (COALESCE(SUM(CASE WHEN list_rate IS NOT NULL THEN qty * list_rate END), 0) = 0 AND COALESCE(SUM(qty), 0) = 0)
