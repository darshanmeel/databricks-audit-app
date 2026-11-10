{{ config(materialized='table', schema='tags', tags=['tags']) }}
-- One row per (window_days, unit_id, level, tag_key, tag_value): the real tag values a tags.cost_unit
-- row carries -- compute (the tag on the bill), work (the job's or pipeline's tags, or a warehouse
-- query share's query tags) and workspace (the workspace's tag valid on each billed day). No
-- untagged rows: app/core/rollup.py takes the remainder from the unit's own usd at request time.
-- Built from the same tags.cost_alloc rows and unit_id formula as tags.cost_unit.
{% if target.type != 'duckdb' %}
{{ exceptions.raise_compiler_error("tags.cost_unit_tag is DuckDB-only for now (tasks/P4-T-SPEC.md, todo later)") }}
{% endif %}

WITH
hashed AS (
    SELECT w.window_days, a.*,
           CASE WHEN a.list_rate IS NOT NULL THEN a.qty * a.list_rate END AS part_usd,
           md5(COALESCE(a.workspace_id, '~') || '|' || a.compute_kind || '|' || COALESCE(a.compute_id, '~')
               || '|' || a.work_kind || '|' || COALESCE(a.work_id, '~') || '|' || a.usage_unit || '|'
               || COALESCE((SELECT string_agg(e.key || '=' || e.value, ',' ORDER BY e.key)
                            FROM unnest(map_entries(a.custom_tags)) AS t(e)), '~')) AS unit_id
    FROM {{ ref('cost_alloc') }} a
    JOIN (SELECT unnest({{ var('windows') }}) AS window_days) w
      ON a.usage_date >= {{ audit_today() }} - INTERVAL (w.window_days) DAY
),

-- ---------------------------------------------------------------------------------------------
-- Compute level: the unit's own custom_tags, one row per normalised key (the alphabetically first
-- raw key wins). usd = the whole unit's usd.
-- ---------------------------------------------------------------------------------------------
unit_totals AS (
    SELECT window_days, unit_id, ANY_VALUE(compute_source) AS compute_source,
           ANY_VALUE(custom_tags) AS custom_tags, COALESCE(SUM(part_usd), 0) AS usd
    FROM hashed
    GROUP BY window_days, unit_id
),
compute_pairs AS (
    SELECT ut.window_days, ut.unit_id, ut.compute_source, ut.usd, x.raw_key,
           {{ norm_tag_key('x.raw_key') }} AS tag_key, trim(x.raw_value) AS tag_value
    FROM unit_totals ut, LATERAL (
        SELECT e.key AS raw_key, e.value AS raw_value FROM unnest(map_entries(ut.custom_tags)) AS y(e)
    ) x
    WHERE x.raw_key IS NOT NULL AND x.raw_value IS NOT NULL AND trim(x.raw_value) <> '' AND {{ norm_tag_key('x.raw_key') }} <> ''
    QUALIFY ROW_NUMBER() OVER (PARTITION BY ut.window_days, ut.unit_id, {{ norm_tag_key('x.raw_key') }} ORDER BY x.raw_key) = 1
),
compute_level AS (
    SELECT window_days, unit_id, 'compute' AS level, tag_key, raw_key, tag_value, compute_source AS source, usd
    FROM compute_pairs
),

-- ---------------------------------------------------------------------------------------------
-- Workspace level: the workspace's own tag on each billed day.
-- ---------------------------------------------------------------------------------------------
workspace_level AS (
    SELECT h.window_days, h.unit_id, 'workspace' AS level, wh.tag_key, wh.display_key AS raw_key,
           wh.tag_value, 'workspace_inferred' AS source, COALESCE(SUM(h.part_usd), 0) AS usd
    FROM hashed h
    JOIN {{ ref('tag_workspace_history') }} wh
      ON wh.workspace_id = h.workspace_id AND wh.is_allocating
     AND h.usage_date >= wh.valid_from AND (wh.valid_to IS NULL OR h.usage_date < wh.valid_to)
    GROUP BY h.window_days, h.unit_id, wh.tag_key, wh.display_key, wh.tag_value
),

-- ---------------------------------------------------------------------------------------------
-- Work level: each part's own work tags -- a job's or pipeline's latest tags, or, for a warehouse's
-- query share, the query tags of that part's slice of attributed DBUs. Dedup per normalised key.
-- ---------------------------------------------------------------------------------------------
work_pairs AS (
    SELECT h.window_days, h.unit_id, h.work_source AS source, h.part_usd, x.raw_key,
           {{ norm_tag_key('x.raw_key') }} AS tag_key, trim(x.raw_value) AS tag_value
    FROM hashed h, LATERAL (
        SELECT e.key AS raw_key, e.value AS raw_value FROM unnest(map_entries(h.work_tags)) AS y(e)
    ) x
    WHERE h.work_source IS NOT NULL AND x.raw_key IS NOT NULL AND x.raw_value IS NOT NULL
      AND trim(x.raw_value) <> '' AND {{ norm_tag_key('x.raw_key') }} <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY h.window_days, h.rid, CAST(h.work_tags AS VARCHAR), {{ norm_tag_key('x.raw_key') }} ORDER BY x.raw_key
    ) = 1
),
work_level AS (
    SELECT window_days, unit_id, 'work' AS level, tag_key, ANY_VALUE(raw_key) AS raw_key, tag_value,
           ANY_VALUE(source) AS source, COALESCE(SUM(part_usd), 0) AS usd
    FROM work_pairs
    GROUP BY window_days, unit_id, tag_key, tag_value
)

SELECT window_days, unit_id, level, tag_key, raw_key, tag_value, source, usd
FROM compute_level
WHERE usd <> 0

UNION ALL

SELECT window_days, unit_id, level, tag_key, raw_key, tag_value, source, usd
FROM work_level
WHERE usd <> 0

UNION ALL

SELECT window_days, unit_id, level, tag_key, raw_key, tag_value, source, usd
FROM workspace_level
WHERE usd <> 0
