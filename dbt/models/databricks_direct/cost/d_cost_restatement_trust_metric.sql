{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:cost', 'tier:lite', 'databricks_direct']) }}
-- generated from app/queries/vendored/cost/cost_restatement_trust_metric.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT workspace_id, cloud,
       SUM(usage_quantity) AS net_usage_quantity,
       SUM(CASE WHEN record_type = 'ORIGINAL'    THEN usage_quantity      ELSE 0 END) AS original_usage_quantity,
       SUM(CASE WHEN record_type = 'RETRACTION'  THEN ABS(usage_quantity) ELSE 0 END) AS retracted_abs_quantity,
       SUM(CASE WHEN record_type = 'RESTATEMENT' THEN usage_quantity      ELSE 0 END) AS restatement_usage_quantity,
       MAX(ingestion_date) AS max_ingestion_date
FROM {{ source('system_billing', 'usage') }}
WHERE usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
  AND usage_date < {{ audit_today() }}
GROUP BY workspace_id, cloud
ORDER BY cloud
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
