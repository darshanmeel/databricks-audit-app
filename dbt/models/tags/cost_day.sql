{{ config(materialized='table', schema='tags', tags=['tags']) }}
-- Every billed dollar per day, labelled with its work and compute tags, so a tag filter can narrow
-- any spend panel. Per tag key the work value (the query's tags for its share of a warehouse, else
-- the job's or pipeline's own tags) wins over the compute value (the tags on the bill); the
-- workspace level (tags.tag_workspace) is added at request time by app/core/tag_spend.py -- the
-- same order app/core/rollup.py uses.
--
-- One row per (usage_date, workspace, cloud, SKU, product, usage unit, the product flags, sig_id). `sig`
-- is the winning tags as JSON [{"k","v","l","s"}] (normalised key, value, level, source); sig_id
-- is its md5, '[]' when untagged. Rows sum exactly to system.billing.usage.
{% if target.type != 'duckdb' %}
{{ exceptions.raise_compiler_error("tags.cost_day is DuckDB-only; app/direct_sql/tags/cost_day.sql is its Databricks twin") }}
{% endif %}

WITH priced AS (
    SELECT u.workspace_id, u.usage_date, u.cloud, u.sku_name, u.billing_origin_product, u.usage_unit,
           COALESCE(u.product_features.is_serverless, FALSE) AS is_serverless,
           COALESCE(u.product_features.is_photon, FALSE) AS is_photon,
           u.product_features.performance_target IS NOT NULL AS is_perf_optimized,
           COALESCE(cardinality(u.custom_tags) > 0, FALSE) AS has_custom_tags,
           u.usage_quantity, u.custom_tags,
           u.usage_metadata.warehouse_id AS warehouse_id,
           u.usage_metadata.job_id AS job_id,
           u.usage_metadata.dlt_pipeline_id AS pipeline_id,
           CASE
               WHEN u.usage_metadata.warehouse_id IS NOT NULL THEN 'billing:warehouse'
               WHEN u.usage_metadata.cluster_id IS NOT NULL THEN 'billing:cluster'
               WHEN u.usage_metadata.endpoint_id IS NOT NULL OR u.usage_metadata.endpoint_name IS NOT NULL THEN 'billing:endpoint'
               WHEN u.usage_metadata.app_id IS NOT NULL THEN 'billing:app'
               WHEN u.usage_metadata.budget_policy_id IS NOT NULL OR u.usage_metadata.usage_policy_id IS NOT NULL THEN 'billing:budget_policy'
               WHEN u.usage_metadata.job_id IS NOT NULL OR u.usage_metadata.dlt_pipeline_id IS NOT NULL
                    OR u.usage_metadata.notebook_id IS NOT NULL OR u.product_features.is_serverless IS TRUE THEN 'billing:serverless'
               ELSE 'billing:other'
           END AS compute_source,
           lp.list_rate
    FROM {{ source('system_billing', 'usage') }} u
    LEFT JOIN (
        SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
               CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
        FROM {{ list_prices() }} list_prices
    ) lp
      ON u.sku_name = lp.sku_name AND u.cloud = lp.cloud AND u.usage_unit = lp.usage_unit
     AND u.usage_end_time >= lp.price_start_time
     AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
    WHERE u.usage_date < {{ audit_today() }}
),
attributed AS (
    SELECT workspace_id, usage_metadata.warehouse_id AS warehouse_id, usage_date,
           granular_tags.query_tags AS query_tags, active_usage_quantity
    FROM {{ source('system_billing', 'attributed_usage') }}
    WHERE usage_metadata.warehouse_id IS NOT NULL AND usage_date < {{ audit_today() }}
),
-- The share of each warehouse-day's DBUs that Databricks attributes to queries.
wh_split AS (
    SELECT b.workspace_id, b.warehouse_id, b.usage_date, COALESCE(a.attributed_dbus, 0) AS attributed_dbus,
           CASE WHEN b.billed_dbus > 0 AND COALESCE(a.attributed_dbus, 0) > 0
                THEN LEAST(a.attributed_dbus, b.billed_dbus) / b.billed_dbus ELSE 0 END AS split_f
    FROM (
        SELECT workspace_id, warehouse_id, usage_date, SUM(usage_quantity) AS billed_dbus
        FROM priced WHERE warehouse_id IS NOT NULL AND usage_unit = 'DBU'
        GROUP BY workspace_id, warehouse_id, usage_date
    ) b
    LEFT JOIN (
        SELECT workspace_id, warehouse_id, usage_date, SUM(active_usage_quantity) AS attributed_dbus
        FROM attributed GROUP BY workspace_id, warehouse_id, usage_date
    ) a USING (workspace_id, warehouse_id, usage_date)
),
-- Each distinct set of query tags' share of a warehouse-day's attributed DBUs.
query_mix AS (
    SELECT workspace_id, warehouse_id, usage_date, ANY_VALUE(query_tags) AS query_tags,
           SUM(active_usage_quantity) AS mix_dbus
    FROM attributed
    GROUP BY workspace_id, warehouse_id, usage_date, CAST(query_tags AS VARCHAR)
),
job_tags AS (
    SELECT workspace_id, job_id, tags FROM {{ source('system_lakeflow', 'jobs') }}
    QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),
pipeline_tags AS (
    SELECT workspace_id, pipeline_id, tags FROM {{ source('system_lakeflow', 'pipelines') }}
    QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC) = 1
),
alloc AS (
    -- a warehouse's query share, split by each query-tag mix
    SELECT p.*, q.query_tags AS work_tags, 'attributed_query_tags' AS work_source,
           p.usage_quantity * s.split_f * q.mix_dbus / s.attributed_dbus AS qty
    FROM priced p
    JOIN wh_split s USING (workspace_id, warehouse_id, usage_date)
    JOIN query_mix q USING (workspace_id, warehouse_id, usage_date)
    WHERE p.usage_unit = 'DBU' AND s.split_f > 0
    UNION ALL
    -- the rest of the warehouse: idle, or no query attribution that day
    SELECT p.*, CAST(NULL AS MAP(VARCHAR, VARCHAR)) AS work_tags, CAST(NULL AS VARCHAR) AS work_source,
           p.usage_quantity * (1 - s.split_f) AS qty
    FROM priced p
    JOIN wh_split s USING (workspace_id, warehouse_id, usage_date)
    WHERE p.usage_unit = 'DBU' AND s.split_f < 1
    UNION ALL
    -- everything else: the job's or pipeline's own latest tags
    SELECT p.*, COALESCE(j.tags, pl.tags) AS work_tags,
           CASE WHEN j.tags IS NOT NULL THEN 'job_tags' WHEN pl.tags IS NOT NULL THEN 'pipeline_tags' END AS work_source,
           p.usage_quantity AS qty
    FROM priced p
    LEFT JOIN job_tags j ON j.workspace_id = p.workspace_id AND j.job_id = p.job_id
    LEFT JOIN pipeline_tags pl ON pl.workspace_id = p.workspace_id AND pl.pipeline_id = p.pipeline_id
    WHERE p.warehouse_id IS NULL OR p.usage_unit <> 'DBU'
),
keyed AS (
    SELECT *, md5(COALESCE(work_source, '~') || '|'
                  || COALESCE(CAST(work_tags AS VARCHAR), '~') || '|' || compute_source || '|'
                  || COALESCE(CAST(custom_tags AS VARCHAR), '~')) AS combo_id
    FROM alloc
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
