-- hand-written; not generated. Databricks twin of dbt/models/tags/cost_day.sql: every billed dollar
-- per day, labelled with its work and compute tags (work wins per key). Whole snapshot, no window.
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
    FROM `system`.`billing`.`usage` u
    LEFT JOIN (
        SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
               CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
        -- de-duplicated list_prices (dbt/macros/list_prices.sql), written out by hand: overlapping
        -- rows fan out this join.
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, effective_end AS price_end_time
            FROM (
                SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                       price_start_time, price_end_time, next_start_time,
                       CASE
                           WHEN price_end_time IS NULL THEN next_start_time
                           WHEN next_start_time IS NULL THEN price_end_time
                           WHEN price_end_time <= next_start_time THEN price_end_time
                           ELSE next_start_time
                       END AS effective_end
                FROM (
                    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                           price_start_time, price_end_time,
                           LEAD(price_start_time) OVER (
                               PARTITION BY sku_name, cloud, usage_unit
                               ORDER BY price_start_time, price_end_time NULLS LAST
                           ) AS next_start_time
                    FROM `system`.`billing`.`list_prices`
                    WHERE currency_code = 'USD'
                ) ranked
            ) capped
            WHERE effective_end IS NULL OR effective_end > price_start_time
        ) list_prices
    ) lp
      ON u.sku_name = lp.sku_name AND u.cloud = lp.cloud AND u.usage_unit = lp.usage_unit
     AND u.usage_end_time >= lp.price_start_time
     AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
    WHERE u.usage_date < __AS_OF_DATE__
      AND u.ingestion_date < __AS_OF_DATE__
),
attributed AS (
    SELECT workspace_id, usage_metadata.warehouse_id AS warehouse_id, usage_date,
           granular_tags.query_tags AS query_tags, active_usage_quantity
    FROM __SRC_BILLING_ATTRIBUTED_USAGE__
    WHERE usage_metadata.warehouse_id IS NOT NULL AND usage_date < __AS_OF_DATE__
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
    SELECT workspace_id, warehouse_id, usage_date, any_value(query_tags) AS query_tags,
           SUM(active_usage_quantity) AS mix_dbus
    FROM attributed
    GROUP BY workspace_id, warehouse_id, usage_date, CAST(query_tags AS STRING)
),
job_tags AS (
    SELECT workspace_id, job_id, tags FROM __SRC_LAKEFLOW_JOBS__
    QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),
pipeline_tags AS (
    SELECT workspace_id, pipeline_id, tags FROM __SRC_LAKEFLOW_PIPELINES__
    QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC) = 1
),
alloc AS (
    -- ON, not USING: USING moves the join keys to the front of p.*, and the UNION matches by position.
    -- a warehouse's query share, split by each query-tag mix
    SELECT p.*, q.query_tags AS work_tags, 'attributed_query_tags' AS work_source,
           p.usage_quantity * s.split_f * q.mix_dbus / s.attributed_dbus AS qty
    FROM priced p
    JOIN wh_split s ON s.workspace_id = p.workspace_id AND s.warehouse_id = p.warehouse_id AND s.usage_date = p.usage_date
    JOIN query_mix q ON q.workspace_id = p.workspace_id AND q.warehouse_id = p.warehouse_id AND q.usage_date = p.usage_date
    WHERE p.usage_unit = 'DBU' AND s.split_f > 0
    UNION ALL
    -- the rest of the warehouse: idle, or no query attribution that day
    SELECT p.*, CAST(NULL AS MAP<STRING, STRING>) AS work_tags, CAST(NULL AS STRING) AS work_source,
           p.usage_quantity * (1 - s.split_f) AS qty
    FROM priced p
    JOIN wh_split s ON s.workspace_id = p.workspace_id AND s.warehouse_id = p.warehouse_id AND s.usage_date = p.usage_date
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
    SELECT *, md5(COALESCE(work_source, '~') || '|' || COALESCE(CAST(work_tags AS STRING), '~') || '|'
                  || compute_source || '|' || COALESCE(CAST(custom_tags AS STRING), '~')) AS combo_id
    FROM alloc
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
