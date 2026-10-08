{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:cost', 'tier:lite', 'databricks_direct']) }}
-- generated from app/queries/app/cost/overview_serverless_classic_split.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH usage_rows AS (
    SELECT workspace_id, usage_quantity, product_features, custom_tags,
           CASE WHEN (product_features.is_serverless = TRUE OR upper(sku_name) LIKE '%SERVERLESS%'
               OR billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH', 'GENIE', 'AI_FUNCTIONS',
                                             'AI_GATEWAY', 'AGENT_BRICKS', 'LAKEBASE', 'APPS'))
                THEN TRUE ELSE FALSE END AS on_serverless
    FROM {{ source('system_billing', 'usage') }}
    WHERE usage_unit = 'DBU'
      AND usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
      AND usage_date < {{ audit_today() }}
)
SELECT
    workspace_id,
    SUM(CASE WHEN on_serverless THEN usage_quantity ELSE 0 END) AS serverless_net_dbus,
    SUM(CASE WHEN on_serverless THEN 0 ELSE usage_quantity END) AS classic_net_dbus,
    ROUND(100.0 * SUM(CASE WHEN on_serverless THEN usage_quantity ELSE 0 END)
          / NULLIF(SUM(usage_quantity), 0), 1) AS serverless_pct,
    SUM(CASE WHEN product_features.is_photon = TRUE THEN usage_quantity ELSE 0 END) AS photon_net_dbus,
    ROUND(100.0 * SUM(CASE WHEN product_features.is_photon = TRUE THEN usage_quantity ELSE 0 END)
          / NULLIF(SUM(usage_quantity), 0), 1) AS photon_pct,
    SUM(CASE WHEN product_features.performance_target IS NOT NULL THEN usage_quantity ELSE 0 END) AS perf_optimized_net_dbus,
    SUM(CASE WHEN cardinality(map_keys(custom_tags)) = 0 THEN usage_quantity ELSE 0 END) AS untagged_net_dbus,
    SUM(usage_quantity) AS total_net_dbus
FROM usage_rows
GROUP BY workspace_id
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
