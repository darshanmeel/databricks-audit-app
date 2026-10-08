-- generated from dbt/models/databricks_direct/governance_access/d_access_network_inbound_denials.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_network_inbound_denials.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT workspace_id, policy_outcome, rule_label, request_path,
       authenticated_as AS authenticated_as,
       source.ip AS source_ip,
       COUNT(*) AS denial_count,
       MIN(event_time) AS first_event_time, MAX(event_time) AS last_event_time,
       -- status: worst-first band on inbound denial volume per rule+path+principal+IP (field heuristic; 10 / 50).
       CASE
         WHEN COUNT(*) >= 50 THEN 'CRITICAL'
         WHEN COUNT(*) >= 10 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`access`.`inbound_network`
WHERE event_time >= __AS_OF_TS__ - INTERVAL __WINDOW_DAYS__ DAYS
GROUP BY 1, 2, 3, 4, authenticated_as, 6
ORDER BY denial_count DESC
) q
