{{ config(materialized='ephemeral', tags=['tags']) }}
-- The one dollar split cost_day, cost_unit and cost_unit_tag read: one row per (billing row, part),
-- priced at the effective list price. A warehouse's DBUs split into its query share -- one part per
-- distinct set of query tags, by attributed DBUs -- and the rest (warehouse_idle, or
-- warehouse_unsplit on a day with no attribution); every other row is one part, labelled with its
-- job's or pipeline's latest tags. Parts of one billing row share its rid.
{% if target.type != 'duckdb' %}
{{ exceptions.raise_compiler_error("tags.cost_alloc is DuckDB-only; app/direct_sql/tags/_cost_alloc.sql is its Databricks twin") }}
{% endif %}

WITH priced AS (
    SELECT u.record_id AS rid,
           u.workspace_id, u.usage_date, u.cloud, u.sku_name, u.billing_origin_product, u.usage_unit,
           COALESCE(u.product_features.is_serverless, FALSE) AS is_serverless,
           COALESCE(u.product_features.is_photon, FALSE) AS is_photon,
           COALESCE(upper(u.product_features.performance_target) = 'PERFORMANCE_OPTIMIZED', FALSE) AS is_perf_optimized,
           COALESCE(cardinality(u.custom_tags) > 0, FALSE) AS has_custom_tags,
           u.usage_quantity, u.custom_tags,
           u.usage_metadata.warehouse_id AS warehouse_id,
           u.usage_metadata.job_id AS job_id,
           u.usage_metadata.dlt_pipeline_id AS pipeline_id,
           CASE
               WHEN u.usage_metadata.warehouse_id IS NOT NULL THEN 'warehouse'
               WHEN u.usage_metadata.cluster_id IS NOT NULL THEN 'cluster'
               WHEN u.usage_metadata.endpoint_id IS NOT NULL OR u.usage_metadata.endpoint_name IS NOT NULL THEN 'endpoint'
               WHEN u.usage_metadata.app_id IS NOT NULL THEN 'app'
               WHEN u.usage_metadata.job_id IS NOT NULL OR u.usage_metadata.dlt_pipeline_id IS NOT NULL
                    OR u.usage_metadata.notebook_id IS NOT NULL OR u.usage_metadata.budget_policy_id IS NOT NULL
                    OR u.usage_metadata.usage_policy_id IS NOT NULL OR u.product_features.is_serverless IS TRUE
                    THEN 'serverless'
               ELSE 'other'
           END AS compute_kind,
           COALESCE(u.usage_metadata.warehouse_id, u.usage_metadata.cluster_id, u.usage_metadata.endpoint_id,
                    u.usage_metadata.endpoint_name, u.usage_metadata.app_id, u.usage_metadata.budget_policy_id,
                    u.usage_metadata.usage_policy_id) AS compute_id,
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
        FROM priced WHERE compute_kind = 'warehouse' AND usage_unit = 'DBU'
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
parts AS (
SELECT p.rid, p.usage_date, p.workspace_id, p.cloud, p.sku_name, p.billing_origin_product, p.usage_unit,
       p.is_serverless, p.is_photon, p.is_perf_optimized, p.has_custom_tags, p.custom_tags,
       p.compute_kind, p.compute_id, 'query' AS work_kind, CAST(NULL AS VARCHAR) AS work_id,
       q.query_tags AS work_tags, 'attributed_query_tags' AS work_source, p.list_rate,
       p.usage_quantity * s.split_f * q.mix_dbus / s.attributed_dbus AS qty
FROM priced p
JOIN wh_split s ON s.workspace_id = p.workspace_id AND s.warehouse_id = p.warehouse_id AND s.usage_date = p.usage_date
JOIN query_mix q ON q.workspace_id = p.workspace_id AND q.warehouse_id = p.warehouse_id AND q.usage_date = p.usage_date
WHERE p.compute_kind = 'warehouse' AND p.usage_unit = 'DBU' AND s.split_f > 0
UNION ALL
SELECT p.rid, p.usage_date, p.workspace_id, p.cloud, p.sku_name, p.billing_origin_product, p.usage_unit,
       p.is_serverless, p.is_photon, p.is_perf_optimized, p.has_custom_tags, p.custom_tags,
       p.compute_kind, p.compute_id,
       CASE WHEN s.attributed_dbus > 0 THEN 'warehouse_idle' ELSE 'warehouse_unsplit' END,
       CAST(NULL AS VARCHAR), CAST(NULL AS MAP(VARCHAR, VARCHAR)), CAST(NULL AS VARCHAR), p.list_rate,
       p.usage_quantity * (1 - s.split_f)
FROM priced p
JOIN wh_split s ON s.workspace_id = p.workspace_id AND s.warehouse_id = p.warehouse_id AND s.usage_date = p.usage_date
WHERE p.compute_kind = 'warehouse' AND p.usage_unit = 'DBU' AND s.split_f < 1
UNION ALL
SELECT p.rid, p.usage_date, p.workspace_id, p.cloud, p.sku_name, p.billing_origin_product, p.usage_unit,
       p.is_serverless, p.is_photon, p.is_perf_optimized, p.has_custom_tags, p.custom_tags,
       p.compute_kind, p.compute_id,
       CASE WHEN p.job_id IS NOT NULL THEN 'job' WHEN p.pipeline_id IS NOT NULL THEN 'pipeline' ELSE 'none' END,
       COALESCE(p.job_id, p.pipeline_id),
       CASE WHEN p.job_id IS NOT NULL THEN j.tags WHEN p.pipeline_id IS NOT NULL THEN pl.tags END,
       CASE WHEN p.job_id IS NOT NULL AND j.tags IS NOT NULL THEN 'job_tags'
            WHEN p.job_id IS NULL AND pl.tags IS NOT NULL THEN 'pipeline_tags' END,
       p.list_rate, p.usage_quantity
FROM priced p
LEFT JOIN job_tags j ON j.workspace_id = p.workspace_id AND j.job_id = p.job_id
LEFT JOIN pipeline_tags pl ON pl.workspace_id = p.workspace_id AND pl.pipeline_id = p.pipeline_id
WHERE NOT (p.compute_kind = 'warehouse' AND p.usage_unit = 'DBU')
)
SELECT *,
       CASE compute_kind
           WHEN 'warehouse' THEN 'billing:warehouse'
           WHEN 'cluster' THEN 'billing:cluster'
           WHEN 'endpoint' THEN 'billing:endpoint'
           WHEN 'app' THEN 'billing:app'
           WHEN 'serverless' THEN CASE WHEN compute_id IS NOT NULL THEN 'billing:budget_policy' ELSE 'billing:serverless' END
           ELSE 'billing:other'
       END AS compute_source
FROM parts
