{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:serving_ai', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/app/serving_ai/compute_serving_dormant_endpoints.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH history AS (
  SELECT se.*,
         MAX(se.endpoint_config_version) OVER (PARTITION BY se.workspace_id, se.endpoint_id) AS current_version,
         MAX(se.endpoint_delete_time) OVER (PARTITION BY se.workspace_id, se.endpoint_id) AS deleted_time,
         MAX(se.change_time) OVER (PARTITION BY se.workspace_id, se.endpoint_id) AS latest_change_time,
         ROW_NUMBER() OVER (
           PARTITION BY se.workspace_id, se.endpoint_id, se.served_entity_id ORDER BY se.change_time DESC
         ) AS _rn
  FROM {{ source('system_serving', 'served_entities') }} se
),
entity_map AS (
  -- every entity the endpoint ever served, so traffic to a retired version still counts
  SELECT DISTINCT workspace_id, endpoint_id, served_entity_id FROM history
),
traffic AS (
  SELECT m.workspace_id, m.endpoint_id,
         SUM(CASE WHEN eu.request_time >= dateadd(day, -{{ w }}, {{ audit_today() }})
                   AND eu.request_time < {{ audit_today() }} THEN 1 ELSE 0 END) AS total_requests,
         MAX(CASE WHEN eu.request_time < {{ audit_today() }} THEN CAST(eu.request_time AS DATE) END) AS last_request_date,
         -- any tracked request in the table's retention shows usage tracking is on
         COUNT(*) AS tracked_requests_ever
  FROM entity_map m
  JOIN {{ source('system_serving', 'endpoint_usage') }} eu
    ON eu.workspace_id = m.workspace_id AND eu.served_entity_id = m.served_entity_id
  GROUP BY m.workspace_id, m.endpoint_id
),
billed AS (
  SELECT workspace_id, usage_metadata.endpoint_id AS endpoint_id, SUM(usage_quantity) AS billed_dbus
  FROM {{ source('system_billing', 'usage') }}
  WHERE usage_metadata.endpoint_id IS NOT NULL
    AND upper(usage_unit) = 'DBU'
    AND usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
    AND usage_date < {{ audit_today() }}
  GROUP BY workspace_id, usage_metadata.endpoint_id
),
per_endpoint AS (
  -- one row per endpoint; only the entities in its current config are counted
  SELECT workspace_id, endpoint_id,
         MAX(endpoint_name) AS endpoint_name,
         COUNT(DISTINCT CASE WHEN endpoint_config_version = current_version OR current_version IS NULL
                             THEN served_entity_id END) AS served_entity_count,
         MAX(latest_change_time) AS latest_change_time,
         MAX(deleted_time) AS endpoint_delete_time,
         MIN(CASE WHEN entity_type = 'FOUNDATION_MODEL' THEN 1 ELSE 0 END) AS all_foundation
  FROM history
  WHERE _rn = 1
  GROUP BY workspace_id, endpoint_id
),
judged AS (
  SELECT pe.*,
         COALESCE(t.total_requests, 0) AS total_requests,
         t.last_request_date,
         COALESCE(t.tracked_requests_ever, 0) > 0 AS usage_tracked,
         COALESCE(b.billed_dbus, 0) AS billed_dbus
  FROM per_endpoint pe
  LEFT JOIN traffic t ON t.workspace_id = pe.workspace_id AND t.endpoint_id = pe.endpoint_id
  LEFT JOIN billed b ON b.workspace_id = pe.workspace_id AND b.endpoint_id = pe.endpoint_id
)
SELECT
  workspace_id,
  endpoint_id,
  endpoint_name,
  served_entity_count,
  latest_change_time,
  CAST(latest_change_time AS DATE) AS latest_change_date,
  total_requests,
  last_request_date,
  usage_tracked,
  ROUND(billed_dbus, 2) AS billed_dbus,
  CASE
    WHEN total_requests >= {{ param('compute_serving_dormant_endpoints', 'warn_low_requests', 10) }} THEN 'OK'
    WHEN NOT usage_tracked THEN 'NOT_ASSESSED'
    WHEN latest_change_time >= dateadd(day, -{{ w }}, {{ audit_today() }}) THEN 'NOT_ASSESSED'
    WHEN billed_dbus = 0 THEN 'OK'   -- idle, but it costs nothing while idle
    WHEN total_requests = 0 THEN 'CRITICAL'
    ELSE 'WARN'
  END AS status,
  CASE
    WHEN total_requests >= {{ param('compute_serving_dormant_endpoints', 'warn_low_requests', 10) }} THEN NULL
    WHEN NOT usage_tracked THEN 'usage_tracking_off'
    WHEN latest_change_time >= dateadd(day, -{{ w }}, {{ audit_today() }}) THEN 'changed_in_window'
  END AS not_assessed_reason
FROM judged
WHERE endpoint_delete_time IS NULL
  AND NOT (endpoint_name LIKE 'databricks-%' AND all_foundation = 1)
ORDER BY
  CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
  billed_dbus DESC, total_requests ASC
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
