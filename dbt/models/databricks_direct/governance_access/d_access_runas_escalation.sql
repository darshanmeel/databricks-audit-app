{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:governance_access', 'tier:deep', 'stars', 'databricks_direct']) }}
-- generated from app/queries/vendored/governance_access/access_runas_escalation.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH pair_history AS (
  -- Every (run_by, run_as) pair this table has EVER seen, over its FULL retention (not just
  -- {{ w }}) - used to tell a genuinely new pair from one that simply didn't run earlier in a
  -- short window. Heavier than the rest of this query; see caveats.
  SELECT identity_metadata.run_by AS run_by, identity_metadata.run_as AS run_as,
         MIN(event_time) AS ever_first_event_time
  FROM {{ source('system_access', 'audit') }}
  WHERE identity_metadata.run_by IS NOT NULL
    AND identity_metadata.run_as IS NOT NULL
    AND identity_metadata.run_by <> identity_metadata.run_as
  GROUP BY identity_metadata.run_by, identity_metadata.run_as
),
window_pairs AS (
  SELECT workspace_id, service_name, action_name,
         identity_metadata.run_by AS run_by,
         identity_metadata.run_as AS run_as,
         COUNT(*) AS event_count,
         MIN(event_time) AS first_event_time,
         MAX(event_time) AS last_event_time
  FROM {{ source('system_access', 'audit') }}
  WHERE identity_metadata.run_by IS NOT NULL
    AND identity_metadata.run_as IS NOT NULL
    AND identity_metadata.run_by <> identity_metadata.run_as
    AND event_date >= {{ audit_today() }} - INTERVAL {{ w }} DAYS
    AND event_date < {{ audit_today() }}
  GROUP BY workspace_id, service_name, action_name, identity_metadata.run_by, identity_metadata.run_as
),
classified AS (
  -- Shape (service-principal vs human) and novelty, not raw event volume, drive status below.
  SELECT w.*,
         w.run_by RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' AS run_by_is_service_principal,
         w.run_as RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' AS run_as_is_service_principal,
         (h.ever_first_event_time IS NULL
          OR h.ever_first_event_time >= {{ audit_today() }} - INTERVAL {{ w }} DAYS) AS newly_seen_pair
  FROM window_pairs w
  LEFT JOIN pair_history h ON h.run_by = w.run_by AND h.run_as = w.run_as
)
SELECT workspace_id, service_name, action_name,
       {{ mask_user('run_by') }} AS run_by,
       {{ mask_user('run_as') }} AS run_as,
       CASE WHEN run_by_is_service_principal AND NOT run_as_is_service_principal THEN 'service_principal_to_user'
            WHEN NOT run_by_is_service_principal AND run_as_is_service_principal THEN 'user_to_service_principal'
            WHEN NOT run_by_is_service_principal AND NOT run_as_is_service_principal THEN 'user_to_user'
            ELSE 'service_principal_to_service_principal' END AS delegation_kind,
       newly_seen_pair,
       event_count, first_event_time, last_event_time,
       -- status: band on NOVELTY and SHAPE, not raw volume or delegation kind alone. A known pair
       -- (any kind) is OK; a newly seen pair is WARN, or CRITICAL when it is a human assuming
       -- another identity either direction. Zero rows does not mean OK - see caveats.
       CASE
         WHEN newly_seen_pair
              AND ((run_by_is_service_principal AND NOT run_as_is_service_principal)
                   OR (NOT run_by_is_service_principal AND NOT run_as_is_service_principal))
              THEN 'CRITICAL'
         WHEN newly_seen_pair THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM classified
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, event_count DESC
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
