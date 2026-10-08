{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:governance_access', 'tier:deep', 'databricks_direct']) }}
-- generated from app/queries/vendored/governance_access/access_network_outbound_denials.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT workspace_id, network_source_type, destination_type, access_type, destination,
       dns_event.rcode                AS dns_rcode,
       storage_event.rejection_reason AS storage_rejection_reason,
       COUNT(*) AS denial_count,
       MIN(event_time) AS first_event_time, MAX(event_time) AS last_event_time,
       -- status: worst-first band on outbound denial volume per workspace+source+destination combo (field heuristic; {{ param('access_network_outbound_denials', 'warn_denial_count', 10) }} / {{ param('access_network_outbound_denials', 'crit_denial_count', 50) }}).
       CASE
         WHEN COUNT(*) >= {{ param('access_network_outbound_denials', 'crit_denial_count', 50) }} THEN 'CRITICAL'
         WHEN COUNT(*) >= {{ param('access_network_outbound_denials', 'warn_denial_count', 10) }} THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM {{ source('system_access', 'outbound_network') }}
WHERE event_time >= {{ audit_now() }} - INTERVAL {{ w }} DAYS
GROUP BY 1, 2, 3, 4, 5, 6, 7
ORDER BY denial_count DESC
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
