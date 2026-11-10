-- hand-written; not generated. Databricks twin of dbt/models/tags/cost_day.sql: every billed dollar
-- per day, labelled with its work and compute tags (work wins per key). Whole snapshot, no window.
-- The split is the shared _cost_alloc.sql block.
WITH
__COST_ALLOC__,
keyed AS (
    SELECT *, md5(COALESCE(work_source, '~') || '|' || COALESCE(CAST(work_tags AS STRING), '~') || '|'
                  || compute_source || '|' || COALESCE(CAST(custom_tags AS STRING), '~')) AS combo_id
    FROM cost_alloc
),
combos AS (
    SELECT combo_id, any_value(work_tags) AS work_tags, any_value(work_source) AS work_source,
           any_value(custom_tags) AS custom_tags, any_value(compute_source) AS compute_source
    FROM keyed
    GROUP BY combo_id
),
entries AS (
    SELECT c.combo_id, 1 AS rnk, 'work' AS level, c.work_source AS source, x.raw_key, x.raw_value
    FROM combos c LATERAL VIEW explode(c.work_tags) x AS raw_key, raw_value
    UNION ALL
    SELECT c.combo_id, 2, 'compute', c.compute_source, x.raw_key, x.raw_value
    FROM combos c LATERAL VIEW explode(c.custom_tags) x AS raw_key, raw_value
),
effective AS (
    SELECT combo_id, tag_key, tag_value, level, source FROM (
        SELECT combo_id, regexp_replace(lower(raw_key), '[ _-]+', '') AS tag_key, trim(raw_value) AS tag_value,
               level, source, rnk, raw_key
        FROM entries
        WHERE raw_key IS NOT NULL AND raw_value IS NOT NULL AND trim(raw_value) <> ''
    )
    WHERE tag_key <> '' AND __TAG_KEY_FILTER__
    QUALIFY ROW_NUMBER() OVER (PARTITION BY combo_id, tag_key ORDER BY rnk, raw_key) = 1
),
sigs AS (
    SELECT c.combo_id,
           to_json(array_sort(collect_list(
               CASE WHEN e.tag_key IS NOT NULL
                    THEN named_struct('k', e.tag_key, 'v', e.tag_value, 'l', e.level, 's', e.source) END
           ))) AS sig
    FROM combos c
    LEFT JOIN effective e ON e.combo_id = c.combo_id
    GROUP BY c.combo_id
)
SELECT k.usage_date, k.workspace_id, k.cloud, k.sku_name, k.billing_origin_product, k.usage_unit,
       k.is_serverless, k.is_photon, k.is_perf_optimized, k.has_custom_tags, md5(s.sig) AS sig_id, s.sig,
       SUM(CASE WHEN k.list_rate IS NOT NULL THEN k.qty * k.list_rate END) AS usd,
       SUM(CASE WHEN k.list_rate IS NULL THEN k.qty ELSE 0 END) AS unpriced_quantity,
       SUM(k.qty) AS quantity,
       __AS_OF_DATE__ AS as_of_date
FROM keyed k
JOIN sigs s ON s.combo_id = k.combo_id
GROUP BY k.usage_date, k.workspace_id, k.cloud, k.sku_name, k.billing_origin_product, k.usage_unit,
         k.is_serverless, k.is_photon, k.is_perf_optimized, k.has_custom_tags, s.sig
