{{ config(materialized='table', schema='tags', tags=['tags']) }}
-- Every billed dollar per day, labelled with its work and compute tags, so a tag filter can narrow
-- any spend panel. Per tag key the work value (the query's tags for its share of a warehouse, else
-- the job's or pipeline's own tags) wins over the compute value (the tags on the bill); the
-- workspace level (tags.tag_workspace) is added at request time by app/core/tag_spend.py -- the
-- same order app/core/rollup.py uses.
--
-- The split itself is tags.cost_alloc's, shared with cost_unit and cost_unit_tag.
--
-- One row per (usage_date, workspace, cloud, SKU, product, usage unit, the product flags, sig_id). `sig`
-- is the winning tags as JSON [{"k","v","l","s"}] (normalised key, value, level, source); sig_id
-- is its md5, '[]' when untagged. Rows sum exactly to system.billing.usage.
{% if target.type != 'duckdb' %}
{{ exceptions.raise_compiler_error("tags.cost_day is DuckDB-only; app/direct_sql/tags/cost_day.sql is its Databricks twin") }}
{% endif %}

WITH keyed AS (
    SELECT *, md5(COALESCE(work_source, '~') || '|'
                  || COALESCE(CAST(work_tags AS VARCHAR), '~') || '|' || compute_source || '|'
                  || COALESCE(CAST(custom_tags AS VARCHAR), '~')) AS combo_id
    FROM {{ ref('cost_alloc') }}
),
combos AS (
    SELECT combo_id, ANY_VALUE(work_tags) AS work_tags,
           ANY_VALUE(work_source) AS work_source, ANY_VALUE(custom_tags) AS custom_tags,
           ANY_VALUE(compute_source) AS compute_source
    FROM keyed
    GROUP BY combo_id
),
entries AS (
    SELECT c.combo_id, 1 AS rnk, 'work' AS level, c.work_source AS source, e.key AS raw_key, e.value AS raw_value
    FROM combos c, unnest(map_entries(c.work_tags)) AS x(e)
    UNION ALL
    SELECT c.combo_id, 2, 'compute', c.compute_source, e.key, e.value
    FROM combos c, unnest(map_entries(c.custom_tags)) AS x(e)
),
effective AS (
    SELECT combo_id, {{ norm_tag_key('raw_key') }} AS tag_key, trim(raw_value) AS tag_value, level, source
    FROM entries
    WHERE raw_key IS NOT NULL AND raw_value IS NOT NULL AND trim(raw_value) <> ''
      AND {{ norm_tag_key('raw_key') }} <> ''
    QUALIFY ROW_NUMBER() OVER (PARTITION BY combo_id, {{ norm_tag_key('raw_key') }} ORDER BY rnk, raw_key) = 1
),
sigs AS (
    SELECT c.combo_id,
           COALESCE(to_json(list({'k': e.tag_key, 'v': e.tag_value, 'l': e.level, 's': e.source} ORDER BY e.tag_key)
                            FILTER (WHERE e.tag_key IS NOT NULL))::VARCHAR, '[]') AS sig
    FROM combos c
    LEFT JOIN effective e ON e.combo_id = c.combo_id
    GROUP BY c.combo_id
)
SELECT k.usage_date, k.workspace_id, k.cloud, k.sku_name, k.billing_origin_product, k.usage_unit,
       k.is_serverless, k.is_photon, k.is_perf_optimized, k.has_custom_tags, md5(s.sig) AS sig_id, s.sig,
       SUM(CASE WHEN k.list_rate IS NOT NULL THEN k.qty * k.list_rate END) AS usd,
       SUM(CASE WHEN k.list_rate IS NULL THEN k.qty ELSE 0 END) AS unpriced_quantity,
       SUM(k.qty) AS quantity,
       {{ audit_today() }} AS as_of_date
FROM keyed k
JOIN sigs s ON s.combo_id = k.combo_id
GROUP BY ALL
