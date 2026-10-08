{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:compute', 'tier:lite', 'databricks_direct']) }}
-- generated from app/queries/vendored/compute/instance_events_idle_active.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT workspace_id, node_type, availability_type, state, event_type,
       COUNT(*) AS event_count,
       COUNT(DISTINCT instance_id) AS instance_count,
       MIN(event_time) AS first_event_time, MAX(event_time) AS last_event_time
FROM {{ source('system_compute', 'instance_events') }}
WHERE event_time >= {{ audit_now() }} - INTERVAL {{ w }} DAYS
GROUP BY workspace_id, node_type, availability_type, state, event_type
ORDER BY event_count DESC, instance_count DESC
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
