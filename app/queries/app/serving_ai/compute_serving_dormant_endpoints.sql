-- query_id: compute_serving_dormant_endpoints
-- title: Serving endpoints with no request traffic in the window - idle waste and misconfiguration
-- domain: serving_ai   tier: standard
-- reads: system.serving.served_entities, system.serving.endpoint_usage, system.billing.usage
-- requires: SELECT on system.serving; system.serving must be enabled per-metastore (empty until
--   Model Serving is in use); system.serving is GA; AI Gateway usage tracking must be enabled per
--   endpoint (off by default)
-- empty_if: schema_not_enabled, preview_unavailable
-- params: :period_days (default 30) rolling window in days; :warn_low_requests (default 10)
--   endpoint requests over the window below which a billed endpoint flags WARN
-- confidence: needs_confirmation
-- confidence_note: Column names (request_time, workspace_id, served_entity_id) and the
--   served_entities-to-endpoint_usage join are verified against a live workspace system-schema
--   dump, but status bands are working heuristics, not vendor-documented.
-- read_this: One row = one endpoint's traffic status over the window, its served entities (model
--   versions) summed. Columns that matter: endpoint_name, served_entity_count, latest_change_date
--   (recency floor: the newest change to any of its entities), total_requests (traffic volume,
--   all entities), last_request_date, usage_tracked (any tracked request in endpoint_usage's
--   retention), billed_dbus (the endpoint's DBUs in the window), and status (classification). Rows are ordered worst-first (CRITICAL then
--   WARN then NOT_ASSESSED then OK) so the findings tab shows the highest-risk endpoints first.
-- healthy: status = OK - at least :warn_low_requests requests in the window, or idle but billing
--   nothing (scale-to-zero, external model, pay-per-token).
-- investigate_if: status = CRITICAL (tracked, billed, and zero requests = idle waste) or WARN
--   (billed with very few requests) or NOT_ASSESSED (usage tracking off, or reconfigured inside
--   the window, with little traffic).
-- actions: 1) scale-to-zero or delete endpoints flagged CRITICAL idle (free); 2) for NOT_ASSESSED,
--   wait out of the window to re-assess if the endpoint becomes active; 3) for WARN, evaluate if
--   provisioned throughput or endpoint count matches the measured traffic (spend optimization).
-- next: compute_serving_endpoint_usage (per-endpoint request and token detail),
--   compute_serving_endpoint_cost_status (for cost and tracking_status by endpoint), cost_by_serving_endpoint
--   (for the raw DBU cost by endpoint)
-- not_assessed_reasons: usage_tracking_off: no tracked request in endpoint_usage's retention, so
--   traffic cannot be measured; changed_in_window: reconfigured inside the window with little traffic
-- caveats: system.serving.* is empty unless Model Serving is in use and the serving schema is
--   enabled - read zero rows as not measured, never as a true zero. served_entities is
--   change-history and is deduped to the latest row per (workspace_id, endpoint_id, served_entity_id)
--   by change_time DESC before the join, so a renamed/reconfigured entity is not counted twice.
--   endpoint_usage has no endpoint_id column, so the join is on (workspace_id, served_entity_id)
--   and endpoint_id is sourced from served_entities. endpoint_usage is one row per request (no
--   request_count column), so request volumes are COUNT(*)-based. latest_change_time is the
--   recency floor. AI Gateway usage tracking is off by default: an endpoint with no tracked
--   request anywhere in endpoint_usage's retention reads NOT_ASSESSED (usage_tracking_off), never
--   idle. served_entity_count counts the entities in the endpoint's current config version only;
--   traffic to earlier versions still counts. served_entities keeps 365 days and writes rows only
--   on config changes, so an endpoint untouched for over a year may be missing. Endpoint name and served entity name are
--   resource names and are never masked. Deleted endpoints (endpoint_delete_time set on any of
--   their rows) are left out: they no longer exist to be idle. Databricks' own pay-per-token
--   foundation model endpoints (name databricks-*, every entity FOUNDATION_MODEL) are left out
--   too: every workspace has them, they bill per token and cost nothing while idle.
--   Ported from the MIT-licensed reference (c) 2026 darshanmeel.
WITH history AS (
  SELECT se.*,
         MAX(se.endpoint_config_version) OVER (PARTITION BY se.workspace_id, se.endpoint_id) AS current_version,
         MAX(se.endpoint_delete_time) OVER (PARTITION BY se.workspace_id, se.endpoint_id) AS deleted_time,
         MAX(se.change_time) OVER (PARTITION BY se.workspace_id, se.endpoint_id) AS latest_change_time,
         ROW_NUMBER() OVER (
           PARTITION BY se.workspace_id, se.endpoint_id, se.served_entity_id ORDER BY se.change_time DESC
         ) AS _rn
  FROM system.serving.served_entities se
),
entity_map AS (
  -- every entity the endpoint ever served, so traffic to a retired version still counts
  SELECT DISTINCT workspace_id, endpoint_id, served_entity_id FROM history
),
traffic AS (
  SELECT m.workspace_id, m.endpoint_id,
         SUM(CASE WHEN eu.request_time >= dateadd(day, -:period_days, current_date())
                   AND eu.request_time < current_date() THEN 1 ELSE 0 END) AS total_requests,
         MAX(CASE WHEN eu.request_time < current_date() THEN CAST(eu.request_time AS DATE) END) AS last_request_date,
         -- any tracked request in the table's retention shows usage tracking is on
         COUNT(*) AS tracked_requests_ever
  FROM entity_map m
  JOIN system.serving.endpoint_usage eu
    ON eu.workspace_id = m.workspace_id AND eu.served_entity_id = m.served_entity_id
  GROUP BY m.workspace_id, m.endpoint_id
),
billed AS (
  SELECT workspace_id, usage_metadata.endpoint_id AS endpoint_id, SUM(usage_quantity) AS billed_dbus
  FROM system.billing.usage
  WHERE usage_metadata.endpoint_id IS NOT NULL
    AND upper(usage_unit) = 'DBU'
    AND usage_date >= dateadd(day, -:period_days, current_date())
    AND usage_date < current_date()
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
    WHEN total_requests >= :warn_low_requests THEN 'OK'
    WHEN NOT usage_tracked THEN 'NOT_ASSESSED'
    WHEN latest_change_time >= dateadd(day, -:period_days, current_date()) THEN 'NOT_ASSESSED'
    WHEN billed_dbus = 0 THEN 'OK'   -- idle, but it costs nothing while idle
    WHEN total_requests = 0 THEN 'CRITICAL'
    ELSE 'WARN'
  END AS status,
  CASE
    WHEN total_requests >= :warn_low_requests THEN NULL
    WHEN NOT usage_tracked THEN 'usage_tracking_off'
    WHEN latest_change_time >= dateadd(day, -:period_days, current_date()) THEN 'changed_in_window'
  END AS not_assessed_reason
FROM judged
WHERE endpoint_delete_time IS NULL
  AND NOT (endpoint_name LIKE 'databricks-%' AND all_foundation = 1)
ORDER BY
  CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
  billed_dbus DESC, total_requests ASC
