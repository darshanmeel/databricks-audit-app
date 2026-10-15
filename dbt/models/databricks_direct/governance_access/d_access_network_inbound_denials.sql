{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:governance_access', 'tier:deep', 'databricks_direct']) }}
-- generated from app/queries/vendored/governance_access/access_network_inbound_denials.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT workspace_id, policy_outcome, rule_label, request_path,
       {{ mask_user('authenticated_as') }} AS authenticated_as,
       source.ip AS source_ip,
       COUNT(*) AS denial_count,
       MIN(event_time) AS first_event_time, MAX(event_time) AS last_event_time,
       -- status: worst-first band on inbound denial volume per rule+path+principal+IP (field heuristic; {{ param('access_network_inbound_denials', 'warn_denial_count', 10) }} / {{ param('access_network_inbound_denials', 'crit_denial_count', 50) }}).
       CASE
         WHEN COUNT(*) >= {{ param('access_network_inbound_denials', 'crit_denial_count', 50) }} THEN 'CRITICAL'
         WHEN COUNT(*) >= {{ param('access_network_inbound_denials', 'warn_denial_count', 10) }} THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM {{ source('system_access', 'inbound_network') }}
WHERE event_time >= {{ audit_now() }} - INTERVAL {{ w }} DAYS
GROUP BY 1, 2, 3, 4, authenticated_as, 6
ORDER BY denial_count DESC
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
