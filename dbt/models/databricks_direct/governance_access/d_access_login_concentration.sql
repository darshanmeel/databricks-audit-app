{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:governance_access', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/governance_access/access_login_concentration.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH base AS (
  SELECT workspace_id,
         COALESCE(user_identity.email, user_identity.subject_name) AS raw_identity,
         source_ip_address, service_name, action_name, event_time, response
  FROM {{ source('system_access', 'audit') }}
  WHERE service_name = 'accounts'
    AND action_name RLIKE '(?i)login|authenticat'
    AND event_date >= {{ audit_today() }} - INTERVAL {{ w }} DAYS
    AND event_date < {{ audit_today() }}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY event_time DESC) = 1
),
principal_ips AS (
  SELECT workspace_id, raw_identity, COUNT(DISTINCT source_ip_address) AS distinct_source_ips
  FROM base
  GROUP BY workspace_id, raw_identity
)
SELECT b.workspace_id,
       {{ mask_user('b.raw_identity') }} AS principal,
       b.source_ip_address, b.service_name, b.action_name,
       COUNT(*) AS event_count,
       SUM(CASE WHEN b.response.status_code = 200 THEN 1 ELSE 0 END) AS success_count,
       SUM(CASE WHEN b.response.status_code IS NOT NULL AND b.response.status_code <> 200 THEN 1 ELSE 0 END) AS non_success_count,
       SUM(CASE WHEN b.response.status_code IS NULL THEN 1 ELSE 0 END) AS unknown_status_count,
       MAX(pi.distinct_source_ips) AS distinct_source_ips,
       MIN(b.event_time) AS first_event_time, MAX(b.event_time) AS last_event_time,
       -- status: worst-first band on REAL non-success (failed) auth attempts per principal+IP+action; a NULL status never counts (field heuristic; {{ param('access_login_concentration', 'warn_failed_logins', 5) }} / {{ param('access_login_concentration', 'crit_failed_logins', 20) }}).
       CASE
         WHEN SUM(CASE WHEN b.response.status_code IS NOT NULL AND b.response.status_code <> 200 THEN 1 ELSE 0 END) >= {{ param('access_login_concentration', 'crit_failed_logins', 20) }} THEN 'CRITICAL'
         WHEN SUM(CASE WHEN b.response.status_code IS NOT NULL AND b.response.status_code <> 200 THEN 1 ELSE 0 END) >= {{ param('access_login_concentration', 'warn_failed_logins', 5) }} THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM base b
LEFT JOIN principal_ips pi
  ON  b.workspace_id IS NOT DISTINCT FROM pi.workspace_id
  AND b.raw_identity  IS NOT DISTINCT FROM pi.raw_identity
GROUP BY b.workspace_id, b.raw_identity, b.source_ip_address, b.service_name, b.action_name
ORDER BY non_success_count DESC
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
