-- generated from dbt/models/databricks_direct/compute/d_instance_events_idle_active.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/compute/instance_events_idle_active.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT workspace_id, node_type, availability_type, state, event_type,
       COUNT(*) AS event_count,
       COUNT(DISTINCT instance_id) AS instance_count,
       MIN(event_time) AS first_event_time, MAX(event_time) AS last_event_time
FROM `system`.`compute`.`instance_events`
WHERE event_time >= __AS_OF_TS__ - INTERVAL __WINDOW_DAYS__ DAYS
GROUP BY workspace_id, node_type, availability_type, state, event_type
ORDER BY event_count DESC, instance_count DESC
) q
