{{ config(materialized='table', schema='tags', tags=['tags']) }}
-- One row per (window_days, unit_id): every dollar of system.billing.usage, split by tags.cost_alloc
-- (warehouse query share / idle / not split; job, pipeline or none) and summed per unit. Carries no
-- tag columns: tags.cost_unit_tag explodes each unit's tags from the same cost_alloc rows.
--
-- unit_id is stable across windows: md5 of workspace_id|compute_kind|compute_id|work_kind|work_id|
-- usage_unit|<the row's own custom_tags as sorted "k=v" pairs>, '~' for NULL. A tag changed
-- mid-window puts a resource's rows in two units, so a unit's own tags are always unambiguous.
{% if target.type != 'duckdb' %}
{{ exceptions.raise_compiler_error("tags.cost_unit is DuckDB-only for now (tasks/P4-T-SPEC.md, todo later)") }}
{% endif %}

WITH hashed AS (
    SELECT w.window_days, a.*,
           md5(COALESCE(a.workspace_id, '~') || '|' || a.compute_kind || '|' || COALESCE(a.compute_id, '~')
               || '|' || a.work_kind || '|' || COALESCE(a.work_id, '~') || '|' || a.usage_unit || '|'
               || COALESCE((SELECT string_agg(e.key || '=' || e.value, ',' ORDER BY e.key)
                            FROM unnest(map_entries(a.custom_tags)) AS t(e)), '~')) AS unit_id
    FROM {{ ref('cost_alloc') }} a
    JOIN (SELECT unnest({{ var('windows') }}) AS window_days) w
      ON a.usage_date >= {{ audit_today() }} - INTERVAL (w.window_days) DAY
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
