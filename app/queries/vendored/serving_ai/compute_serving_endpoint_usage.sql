-- query_id: compute_serving_endpoint_usage
-- title: Serving endpoint request volume, errors, tokens, and cost by day
-- domain: serving_ai   tier: deep
-- reads: system.serving.served_entities, system.serving.endpoint_usage, system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.serving, system.billing; system.serving must be enabled per-metastore (empty until Model Serving is in use); system.billing is GA.
-- empty_if: usage_tracking_off, schema_not_enabled, preview_unavailable
-- params: :period_days (default 30) rolling window in days
-- confidence: needs_confirmation
-- confidence_note: Column names (request_time, workspace_id, served_entity_id, status_code, input_token_count, output_token_count) and the served_entities-to-endpoint_usage join are verified against a live workspace system-schema dump, but the 2xx-as-success classification is a working assumption, not vendor-documented.
-- read_this: One row = a day + endpoint + served entity's request volume. The columns that matter are total_requests, error_requests, and net_dbus - use this as the detail behind a dormancy or cost finding, not as a standalone health signal (this file mixes several metrics with no single waste driver).
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: compute_serving_endpoint_cost_status (if you want to know which of these endpoints have zero traffic), cost_by_compute_resource (if you want the endpoint's cost trend outside the serving grain)
-- caveats: system.serving.* is empty unless Model Serving is in use (and the serving schema is enabled) - read zero rows as "not measured", never as a true zero. The served_entities dimension is change-history (one row per config change) and is deduped to the latest row per (workspace_id, endpoint_id, served_entity_id) by change_time DESC before the join, so a renamed/reconfigured entity is not counted twice. endpoint_usage has no endpoint_id column, so the join is on (workspace_id, served_entity_id) and endpoint_id is sourced from served_entities. endpoint_usage is one row per request (no request_count column), so request volumes are COUNT(*)/CASE-based. Verified columns: request_time (timestamp), workspace_id, served_entity_id, status_code (int HTTP status), input_token_count, output_token_count. usage_quantity/DBUs live in system.billing.usage, not here - this table is a request/token counter, not a dollar source. net_dbus is exact billed DBUs (usage_unit='DBU'); est_usd_list is an ESTIMATE AT THE EFFECTIVE LIST PRICE (usage_quantity x list_prices.pricing.effective_list.default - DEC-66.1), NOT the negotiated invoice rate (not in any system table), and excludes cloud infra/egress dollars - treat it as directional. Cost is attributed by billing usage_metadata.endpoint_id over the same :period_days window, rolled up at (workspace_id, endpoint_id, usage_date) grain, then LEFT JOINed pre-aggregated so rows are never multiplied. billing.usage cannot see served_entity_id, so when an endpoint hosts multiple served entities the day's net_dbus/est_usd_list is split across those served-entity rows by each one's share of that day's requests - the rows add up to the endpoint's bill, but one served entity's share is an estimate. price_basis is 'unpriced' when any non-free-usage SKU billed to this endpoint/day had no matching list_prices row (net_dbus/est_usd_list then understate cost), 'free' when every matched SKU is a FREE_USAGE SKU (a real $0), and 'priced' otherwise.
-- endpoint_usage only gets rows when AI Gateway usage tracking is enabled per endpoint (off by default) - a busy endpoint with tracking off reads zero; it covers only Model Serving, so vector search, feature serving, and agent endpoints never appear.
WITH entities AS (
  -- served_entities is change-history; keep only the latest config row per entity.
  SELECT
    workspace_id,
    endpoint_id,
    endpoint_name,
    served_entity_id,
    served_entity_name,
    entity_type,
    entity_name,
    entity_version
  FROM (
    SELECT
      se.*,
      ROW_NUMBER() OVER (
        PARTITION BY se.workspace_id, se.endpoint_id, se.served_entity_id
        ORDER BY se.change_time DESC
      ) AS _rn
    FROM system.serving.served_entities se
  )
  WHERE _rn = 1
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM system.billing.list_prices
),
cost_rollup AS (
  -- Pre-aggregated billing cost per serving endpoint per day (billing key = usage_metadata.endpoint_id).
  -- Grouped by (workspace_id, endpoint_id, usage_date) to match this query's endpoint/day join grain;
  -- billing.usage cannot break cost down to served_entity_id. Unique per key so the LEFT JOIN below is safe.
  SELECT u.workspace_id,
         u.usage_metadata.endpoint_id                     AS endpoint_id,
         u.usage_date                                     AS usage_date,
         SUM(u.usage_quantity)                            AS net_dbus,
         SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS est_usd_list,
         CASE
           WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                         THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END                                               AS price_basis
  FROM system.billing.usage u
  LEFT JOIN price p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.endpoint_id IS NOT NULL
    AND u.usage_date >= dateadd(day, -:period_days, current_date())
    AND u.usage_date <  current_date()
  GROUP BY u.workspace_id, u.usage_metadata.endpoint_id, u.usage_date
),
finding AS (
  SELECT
    CAST(eu.request_time AS DATE)        AS usage_date,
    eu.workspace_id                      AS workspace_id,
    ent.endpoint_id                      AS endpoint_id,
    ent.endpoint_name AS endpoint_name,
    eu.served_entity_id                  AS served_entity_id,
    ent.served_entity_name AS served_entity_name,
    ent.entity_type                      AS entity_type,
    ent.entity_name                      AS entity_name,
    -- status_code is the HTTP-status column on endpoint_usage; classify 2xx as success,
    -- everything else as error. endpoint_usage is one row per request, so counts are
    -- COUNT(*)/CASE-based rather than a summed request_count column.
    SUM(CASE WHEN eu.status_code BETWEEN 200 AND 299 THEN 1 ELSE 0 END) AS success_requests,
    SUM(CASE WHEN eu.status_code IS NOT NULL AND NOT (eu.status_code BETWEEN 200 AND 299)
             THEN 1 ELSE 0 END)                                         AS error_requests,
    COUNT(*)                                                            AS total_requests,
    -- Token throughput is a separate magnitude from request counts (never summed with them).
    SUM(COALESCE(eu.input_token_count, 0))                             AS input_tokens,
    SUM(COALESCE(eu.output_token_count, 0))                            AS output_tokens
  FROM system.serving.endpoint_usage eu
  LEFT JOIN entities ent
    ON  eu.workspace_id     = ent.workspace_id
    AND eu.served_entity_id = ent.served_entity_id
  WHERE eu.request_time >= dateadd(day, -:period_days, current_date())
    AND eu.request_time <  current_date()
  GROUP BY
    CAST(eu.request_time AS DATE),
    eu.workspace_id,
    ent.endpoint_id,
    ent.endpoint_name,
    eu.served_entity_id,
    ent.served_entity_name,
    ent.entity_type,
    ent.entity_name
)
SELECT
  finding.usage_date,
  finding.workspace_id,
  finding.endpoint_id,
  finding.endpoint_name,
  finding.served_entity_id,
  finding.served_entity_name,
  finding.entity_type,
  finding.entity_name,
  finding.success_requests,
  finding.error_requests,
  finding.total_requests,
  finding.input_tokens,
  finding.output_tokens,
  -- cost columns: the endpoint/day bill split by each served entity's share of that day's requests,
  -- so summing rows gives the bill once rather than once per served entity.
  COALESCE(cr.net_dbus, 0) * finding.total_requests
    / SUM(finding.total_requests) OVER (PARTITION BY finding.workspace_id, finding.endpoint_id, finding.usage_date) AS net_dbus,
  COALESCE(cr.est_usd_list, 0) * finding.total_requests
    / SUM(finding.total_requests) OVER (PARTITION BY finding.workspace_id, finding.endpoint_id, finding.usage_date) AS est_usd_list,
  COALESCE(cr.price_basis, 'priced') AS price_basis
FROM finding
LEFT JOIN cost_rollup cr
  ON  finding.workspace_id = cr.workspace_id
  AND finding.endpoint_id  = cr.endpoint_id
  AND finding.usage_date   = cr.usage_date
ORDER BY usage_date DESC, total_requests DESC
