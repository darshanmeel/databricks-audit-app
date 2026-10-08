-- generated from dbt/models/databricks_direct/governance_access/d_access_runas_escalation.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_runas_escalation.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH pair_history AS (
  -- Every (run_by, run_as) pair this table has EVER seen, over its FULL retention (not just
  -- __WINDOW_DAYS__) - used to tell a genuinely new pair from one that simply didn't run earlier in a
  -- short window. Heavier than the rest of this query; see caveats.
  SELECT identity_metadata.run_by AS run_by, identity_metadata.run_as AS run_as,
         MIN(event_time) AS ever_first_event_time
  FROM `system`.`access`.`audit`
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
  FROM `system`.`access`.`audit`
  WHERE identity_metadata.run_by IS NOT NULL
    AND identity_metadata.run_as IS NOT NULL
    AND identity_metadata.run_by <> identity_metadata.run_as
    AND event_date >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
    AND event_date < __AS_OF_DATE__
  GROUP BY workspace_id, service_name, action_name, identity_metadata.run_by, identity_metadata.run_as
),
classified AS (
  -- Shape (service-principal vs human) and novelty, not raw event volume, drive status below.
  SELECT w.*,
         w.run_by RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' AS run_by_is_service_principal,
         w.run_as RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' AS run_as_is_service_principal,
         (h.ever_first_event_time IS NULL
          OR h.ever_first_event_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS) AS newly_seen_pair
  FROM window_pairs w
  LEFT JOIN pair_history h ON h.run_by = w.run_by AND h.run_as = w.run_as
)
SELECT workspace_id, service_name, action_name,
       run_by AS run_by,
       run_as AS run_as,
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
