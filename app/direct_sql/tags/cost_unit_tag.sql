-- hand-written; not generated. Databricks twin of dbt/models/tags/cost_unit_tag.sql: one row per
-- (window_days, unit_id, level, tag_key, tag_value) -- the real tag values a tags.cost_unit row
-- carries, the workspace level by the tag valid on each billed day (shared _workspace_history.sql
-- block). Built from the same _cost_alloc.sql rows and unit_id formula as tags/cost_unit.sql.
-- __WINDOW_DAYS__ is filled in at export time.
WITH
__WORKSPACE_HISTORY__,
__COST_ALLOC_WINDOW__,
hashed AS (
    SELECT __WINDOW_DAYS__ AS window_days, a.*,
           CASE WHEN a.list_rate IS NOT NULL THEN a.qty * a.list_rate END AS part_usd,
           md5(COALESCE(a.workspace_id, '~') || '|' || a.compute_kind || '|' || COALESCE(a.compute_id, '~')
               || '|' || a.work_kind || '|' || COALESCE(a.work_id, '~') || '|' || a.usage_unit || '|'
               || COALESCE(CASE WHEN a.custom_tags IS NULL OR size(a.custom_tags) = 0 THEN NULL
                                ELSE array_join(array_sort(transform(map_entries(a.custom_tags), e -> concat(e.key, '=', e.value))), ',')
                           END, '~')) AS unit_id
    FROM cost_alloc a
),

-- ---------------------------------------------------------------------------------------------
-- Compute level: the unit's OWN custom_tags map, exploded (dedup per normalised key). usd = the
-- whole unit's usd.
-- ---------------------------------------------------------------------------------------------
unit_totals AS (
    SELECT window_days, unit_id, ANY_VALUE(compute_source) AS compute_source,
           ANY_VALUE(custom_tags) AS custom_tags, COALESCE(SUM(part_usd), 0) AS usd
    FROM hashed
    GROUP BY window_days, unit_id
),
compute_pairs AS (
    SELECT ut.window_days, ut.unit_id, ut.compute_source, ut.usd, raw_key,
           regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value
    FROM unit_totals ut
    LATERAL VIEW explode(ut.custom_tags) x AS raw_key, raw_value
    WHERE raw_key IS NOT NULL AND raw_value IS NOT NULL AND trim(raw_value) <> ''
      AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY ut.window_days, ut.unit_id, regexp_replace(lower(raw_key), '[ _-]+', '') ORDER BY raw_key
    ) = 1
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
    JOIN ws_history wh
      ON wh.workspace_id = h.workspace_id AND wh.is_allocating
     AND h.usage_date >= wh.valid_from AND (wh.valid_to IS NULL OR h.usage_date < wh.valid_to)
    GROUP BY h.window_days, h.unit_id, wh.tag_key, wh.display_key, wh.tag_value
),

-- ---------------------------------------------------------------------------------------------
-- Work level: each part's own work tags -- a job's or pipeline's latest tags, or, for a warehouse's
-- query share, the query tags of that part's slice of attributed DBUs. Dedup per normalised key.
-- ---------------------------------------------------------------------------------------------
work_pairs AS (
    SELECT h.window_days, h.unit_id, h.work_source AS source, h.part_usd, raw_key,
           regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value
    FROM hashed h
    LATERAL VIEW explode(h.work_tags) x AS raw_key, raw_value
    WHERE h.work_source IS NOT NULL AND raw_key IS NOT NULL AND raw_value IS NOT NULL AND trim(raw_value) <> ''
      AND regexp_replace(lower(raw_key), '[ _-]+', '') <> ''
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY h.window_days, h.rid, CAST(h.work_tags AS STRING), regexp_replace(lower(raw_key), '[ _-]+', '')
        ORDER BY raw_key
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
