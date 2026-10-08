-- query_id: compute_serving_dormant_endpoints
-- title: Serving endpoints with no request traffic in the window - idle waste and misconfiguration
-- domain: serving_ai   tier: standard
-- reads: system.serving.served_entities, system.serving.endpoint_usage
-- requires: SELECT on system.serving; system.serving must be enabled per-metastore (empty until
--   Model Serving is in use); system.serving is GA; AI Gateway usage tracking must be enabled per
--   endpoint (off by default)
-- empty_if: usage_tracking_off, schema_not_enabled, preview_unavailable
-- params: :period_days (default 30) rolling window in days; :warn_low_requests (default 10)
--   endpoint requests over the window below which a billed endpoint flags WARN
-- confidence: needs_confirmation
-- confidence_note: Column names (request_time, workspace_id, served_entity_id) and the
--   served_entities-to-endpoint_usage join are verified against a live workspace system-schema
--   dump, but status bands are working heuristics, not vendor-documented.
-- read_this: One row = one endpoint's traffic status over the window, its served entities (model
--   versions) summed. Columns that matter: endpoint_name, served_entity_count, latest_change_date
--   (recency floor: the newest change to any of its entities), total_requests (traffic volume,
--   all entities), last_request_date, and status (classification). Rows are ordered worst-first (CRITICAL then
--   WARN then NOT_ASSESSED then OK) so the findings tab shows the highest-risk endpoints first.
-- healthy: status = OK - the endpoint has recorded requests over the window.
-- investigate_if: status = CRITICAL (tracked but zero requests = idle waste) or WARN (very low
--   requests = low utilization) or NOT_ASSESSED (created or reconfigured inside the window, so
--   dormancy cannot be confirmed).
-- actions: 1) scale-to-zero or delete endpoints flagged CRITICAL idle (free); 2) for NOT_ASSESSED,
--   wait out of the window to re-assess if the endpoint becomes active; 3) for WARN, evaluate if
--   provisioned throughput or endpoint count matches the measured traffic (spend optimization).
-- next: compute_serving_endpoint_usage (per-endpoint request and token detail),
--   compute_serving_endpoint_cost_status (for cost and tracking_status by endpoint), cost_by_serving_endpoint
--   (for the raw DBU cost by endpoint)
-- caveats: system.serving.* is empty unless Model Serving is in use and the serving schema is
--   enabled - read zero rows as not measured, never as a true zero. served_entities is
--   change-history and is deduped to the latest row per (workspace_id, endpoint_id, served_entity_id)
--   by change_time DESC before the join, so a renamed/reconfigured entity is not counted twice.
--   endpoint_usage has no endpoint_id column, so the join is on (workspace_id, served_entity_id)
--   and endpoint_id is sourced from served_entities. endpoint_usage is one row per request (no
--   request_count column), so request volumes are COUNT(*)-based. latest_change_time is the
--   recency floor - a NOT_ASSESSED endpoint is one that was created or reconfigured inside the
--   window, so dormancy cannot be confirmed until it ages outside the window. AI Gateway usage
--   tracking must be enabled on the endpoint (off by default) - a busy endpoint with tracking off
--   surfaces as idle, not as a true dormancy issue. Endpoint name and served entity name are
--   resource names and are never masked. Deleted endpoints (endpoint_delete_time set on any of
--   their rows) are left out: they no longer exist to be idle. Databricks' own pay-per-token
--   foundation model endpoints (name databricks-*, every entity FOUNDATION_MODEL) are left out
--   too: every workspace has them, they bill per token and cost nothing while idle.
--   Ported from the MIT-licensed reference (c) 2026 darshanmeel.
WITH entities AS (
  -- served_entities is change-history; collapse to the latest config row per entity.
  SELECT
    workspace_id,
    endpoint_id,
    endpoint_name,
    served_entity_id,
    served_entity_name,
    entity_type,
    entity_name,
    entity_version,
    latest_change_time,
    endpoint_delete_time
  FROM (
    SELECT
      se.workspace_id,
      se.endpoint_id,
      se.endpoint_name,
      se.served_entity_id,
      se.served_entity_name,
      se.entity_type,
      se.entity_name,
      se.entity_version,
      MAX(se.change_time) OVER (
        PARTITION BY se.workspace_id, se.endpoint_id, se.served_entity_id
      ) AS latest_change_time,
      MAX(se.endpoint_delete_time) OVER (
        PARTITION BY se.workspace_id, se.endpoint_id
      ) AS endpoint_delete_time,
      ROW_NUMBER() OVER (
        PARTITION BY se.workspace_id, se.endpoint_id, se.served_entity_id
        ORDER BY se.change_time DESC
      ) AS _rn
    FROM system.serving.served_entities se
  )
  WHERE _rn = 1
),
usage_rollup AS (
  -- Per-entity traffic inside the window. Entities with no traffic never appear here,
  -- so the LEFT JOIN below leaves their total_requests NULL (the dormant signal).
  SELECT
    eu.workspace_id,
    eu.served_entity_id,
    COUNT(*) AS total_requests,
    MAX(CAST(eu.request_time AS DATE)) AS last_request_date
  FROM system.serving.endpoint_usage eu
  WHERE eu.request_time >= dateadd(day, -:period_days, current_date())
    AND eu.request_time < current_date()
  GROUP BY eu.workspace_id, eu.served_entity_id
),
per_endpoint AS (
  -- one row per endpoint: its served entities (model versions) are summed, not listed
  SELECT
    ent.workspace_id,
    ent.endpoint_id,
    MAX(ent.endpoint_name) AS endpoint_name,
    COUNT(*) AS served_entity_count,
    MAX(ent.latest_change_time) AS latest_change_time,
    SUM(COALESCE(ur.total_requests, 0)) AS total_requests,
    MAX(ur.last_request_date) AS last_request_date,
    MAX(ent.endpoint_delete_time) AS endpoint_delete_time,
    MIN(CASE WHEN ent.entity_type = 'FOUNDATION_MODEL' THEN 1 ELSE 0 END) AS all_foundation
  FROM entities ent
  LEFT JOIN usage_rollup ur
    ON ent.workspace_id = ur.workspace_id
    AND ent.served_entity_id = ur.served_entity_id
  GROUP BY ent.workspace_id, ent.endpoint_id
)
SELECT
  pe.workspace_id AS workspace_id,
  pe.endpoint_id AS endpoint_id,
  pe.endpoint_name AS endpoint_name,
  pe.served_entity_count AS served_entity_count,
  pe.latest_change_time AS latest_change_time,
  CAST(pe.latest_change_time AS DATE) AS latest_change_date,
  pe.total_requests AS total_requests,
  pe.last_request_date AS last_request_date,
  CASE
    WHEN pe.latest_change_time >= dateadd(day, -:period_days, current_date()) THEN 'NOT_ASSESSED'
    WHEN pe.total_requests = 0 THEN 'CRITICAL'
    WHEN pe.total_requests < :warn_low_requests THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM per_endpoint pe
WHERE pe.endpoint_delete_time IS NULL
  AND NOT (pe.endpoint_name LIKE 'databricks-%' AND pe.all_foundation = 1)
ORDER BY
  CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
  total_requests ASC
