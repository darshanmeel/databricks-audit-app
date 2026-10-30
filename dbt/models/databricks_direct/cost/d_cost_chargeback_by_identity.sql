{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:cost', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/cost/cost_chargeback_by_identity.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT usage_date, cloud, workspace_id, billing_origin_product,
       CASE
         WHEN identity_metadata.run_as IS NULL OR identity_metadata.run_as = '__REDACTED__' THEN 'unknown'
         WHEN identity_metadata.run_as LIKE '%@%' THEN 'user'
         ELSE 'service_principal'
       END                          AS identity_type,
       {{ mask_user('identity_metadata.run_as') }}                          AS identity_run_as,
       {{ mask_user('identity_metadata.owned_by') }}                          AS identity_owned_by,
       {{ mask_user('identity_metadata.created_by') }}                          AS identity_created_by,
       SUM(usage_quantity) AS net_usage_quantity
FROM {{ source('system_billing', 'usage') }}
WHERE usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
  AND usage_date < {{ audit_today() }}
  AND usage_unit = 'DBU'
GROUP BY usage_date, cloud, workspace_id, billing_origin_product,
         CASE
           WHEN identity_metadata.run_as IS NULL OR identity_metadata.run_as = '__REDACTED__' THEN 'unknown'
           WHEN identity_metadata.run_as LIKE '%@%' THEN 'user'
           ELSE 'service_principal'
         END,
         identity_metadata.run_as, identity_metadata.owned_by, identity_metadata.created_by
ORDER BY usage_date DESC, workspace_id, identity_type
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
