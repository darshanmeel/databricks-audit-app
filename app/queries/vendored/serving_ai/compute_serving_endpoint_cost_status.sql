-- query_id: compute_serving_endpoint_cost_status
-- title: Serving endpoint cost, idle, and usage-tracking status
-- domain: serving_ai   tier: standard
-- reads: system.billing.usage, system.billing.list_prices, system.serving.served_entities, system.serving.endpoint_usage, system.access.workspaces_latest
-- requires: SELECT on system.billing, system.serving, system.access; the serving and access system schemas enabled; billing is GA, serving + workspaces_latest are Public Preview
-- empty_if: usage_tracking_off, schema_not_enabled, preview_unavailable
-- params: :period_days (default 30) analysis window in days for requests and cost; :retention_days (default 90) the full endpoint_usage retention window used to tell "usage tracking off" from "idle in window", and the minimum captured history required before that verdict is trusted (see not_assessed_reason: insufficient_retention_history); :warn_low_requests (default 10) endpoint requests over the window at or below which a billed endpoint flags WARN; :top_n (default 100000) row cap
-- confidence: needs_confirmation
-- confidence_note: every column is verified against the workspace system-schema dump; tracking_status/not_assessed_reason are a heuristic inferred from whether a billed endpoint has any serving telemetry and how far its own endpoint_usage history reaches back, not a Databricks-reported flag.
-- read_this: One row = one endpoint that BILLED in the window (any serving product, including Vector Search), aggregated across every served entity it currently has: served_entity_count/entity_types/entity_names summarize all of them, while served_entity_id/served_entity_name/entity_type/entity_name/entity_version (also given as primary_served_entity_id/primary_served_entity_name/primary_entity_type/primary_entity_name/primary_entity_version) describe only its single most-recently-changed one. The columns that matter are est_usd_list (spend) and tracking_status / status - they separate "usage tracking is OFF" (spend but no telemetry), "not enough history yet to tell" (not_assessed_reason=insufficient_retention_history), and "truly idle" (tracked but zero requests). price_basis (free/priced/unpriced) discloses whether est_usd_list is a real $0 (free-usage SKU) or a pricing-coverage gap. est_wasted_usd_list (P3-WASTEUSD) is the endpoint's WHOLE est_usd_list when status=CRITICAL (a confirmed-idle endpoint's entire measured window spend is unused); 0 for real (even if low) traffic, and NULL (cannot tell, not zero) where idle cannot be confirmed: Vector Search, endpoints missing from served_entities, spend with usage tracking off, and insufficient retention history.
-- healthy: status = OK - the endpoint has request volume proportional to its spend.
-- investigate_if: status = CRITICAL (tracked, still billing, zero requests in the window = idle waste) or WARN (very low requests, or spend with usage tracking off so idle cannot be confirmed) - field heuristic; tune :warn_low_requests for your account. status = NOT_ASSESSED can also mean insufficient_retention_history: the account's own endpoint_usage history does not yet reach back :retention_days days, so "tracking off" cannot be told apart from "too new to judge" - read not_assessed_reason.
-- actions: 1) scale-to-zero or delete endpoints flagged CRITICAL idle (free); 2) for spend-with-tracking-off rows, enable AI Gateway usage tracking on the endpoint so idle can actually be measured (config); 3) right-size provisioned throughput or move a low-traffic endpoint to pay-per-token (spend).
-- next: compute_serving_endpoint_usage (per-endpoint request and token detail), cost_by_serving_endpoint (the raw DBU cost split by usage_type), cost_vector_search_spend (for the Vector Search endpoints this flags NOT_ASSESSED)
-- not_assessed_reasons: not_serving_telemetry: Vector Search - no serving-table telemetry exists; not_a_tracked_endpoint: bills but is absent from served_entities; insufficient_retention_history: the account's own endpoint_usage history does not yet reach back the full retention lookback, so tracking-off cannot yet be told apart from too-new-to-judge
-- caveats: ANCHORED ON system.billing.usage so it sees EVERY endpoint-billed product (MODEL_SERVING and VECTOR_SEARCH), then LEFT JOINs the serving tables - which only cover Model Serving endpoints that have AI Gateway usage tracking enabled. served_entities is change-history, deduped to the latest row per (workspace_id, endpoint_id, served_entity_id), then aggregated to ONE ROW PER ENDPOINT - never fanned out by served-entity count: served_entity_count/entity_types/entity_names summarize every current served entity; the bare served_entity_id/served_entity_name/entity_type/entity_name/entity_version columns (identical to their primary_* twins) describe only the one with the latest change_time. endpoint_usage has NO endpoint_id and NO request_count, so requests are COUNT(*) rolled up per served_entity_id and mapped to endpoint_id through served_entities; entity_requests_window/entity_last_request_date reflect only that same primary served entity, while endpoint_requests_window/endpoint_last_request_date are the true endpoint-wide total across every served entity (ep_last_request_date is the same value, kept for callers still on the old column name). tracking_status/not_assessed_reason: NOT_ASSESSED (not_serving_telemetry) for Vector Search, NOT_ASSESSED (not_a_tracked_endpoint) for endpoints that bill but are absent from served_entities, NOT_ASSESSED (insufficient_retention_history) when the workspace's own captured endpoint_usage history (earliest_request_time) is shorter than :retention_days - a newly-onboarded, genuinely-tracked endpoint is not misread as "tracking off"; when NO history exists at all (earliest_request_time IS NULL) that is itself the "usage tracking likely OFF" WARN signal, not a reason to withhold a verdict. Cost = usage_quantity x list_prices.pricing.effective_list.default, an ESTIMATE AT THE EFFECTIVE LIST PRICE (DEC-66.1; not the negotiated invoice - no negotiated-rate source exists; DBU-only, excludes cloud infra/egress). endpoint_name / served_entity_name / entity_name are resource names and are never masked (DEC-73). workspaces_latest is Public Preview (LEFT JOIN; workspace_name may be null). Set :retention_days to roughly your endpoint_usage retention (~90 days) so "usage tracking off" is not misread as idle. price_basis is 'unpriced' when any non-free-usage SKU billed to this endpoint had no matching list_prices row (est_usd_list then understates cost), 'free' when every matched SKU is a FREE_USAGE SKU (a real $0), and 'priced' otherwise. est_wasted_usd_list (P3-WASTEUSD) mirrors the status CASE's own CRITICAL branch: the endpoint's WHOLE est_usd_list, once per endpoint now that it is never fanned out by served entity (so nothing needs dividing to avoid double-counting) - NULL, never 0, for NOT_ASSESSED and the "tracking likely OFF" WARN (idle cannot be confirmed there), and 0 for every OK/low-traffic-WARN row (real, if low, traffic). Route-optimized custom Model Serving endpoints never support usage tracking, so they surface as "usage tracking off" (WARN) even though it can never be enabled - not an actionable config gap for those. The scored rows are read from a derived table so the final ORDER BY's CASE on status binds to this query's own status column, never to the LEFT JOINed system.access.workspaces_latest.status (a same-named column that otherwise wins name resolution on some engines).
WITH entities AS (   -- served_entities is change-history -> collapse to the latest config row per entity
  SELECT workspace_id, endpoint_id, endpoint_name, served_entity_id, served_entity_name,
         entity_type, entity_name, entity_version, latest_change_time
  FROM (
    SELECT se.workspace_id, se.endpoint_id, se.endpoint_name, se.served_entity_id,
           se.served_entity_name, se.entity_type, se.entity_name, se.entity_version,
           MAX(se.change_time) OVER (PARTITION BY se.workspace_id, se.endpoint_id, se.served_entity_id) AS latest_change_time,
           ROW_NUMBER() OVER (PARTITION BY se.workspace_id, se.endpoint_id, se.served_entity_id ORDER BY se.change_time DESC) AS _rn
    FROM system.serving.served_entities se
  ) WHERE _rn = 1
),
entity_agg AS (   -- one row per endpoint - never fanned out by served-entity count
  SELECT workspace_id, endpoint_id,
         MAX(endpoint_name)                                                     AS endpoint_name,
         COUNT(*)                                                               AS served_entity_count,
         array_join(collect_set(entity_type), ', ')                             AS entity_types,
         array_join(collect_set(COALESCE(entity_name, served_entity_name)), ', ') AS entity_names,
         MAX(latest_change_time)                                                AS latest_change_time,
         MAX(CASE WHEN _erank = 1 THEN served_entity_id   END)                  AS primary_served_entity_id,
         MAX(CASE WHEN _erank = 1 THEN served_entity_name END)                  AS primary_served_entity_name,
         MAX(CASE WHEN _erank = 1 THEN entity_type        END)                  AS primary_entity_type,
         MAX(CASE WHEN _erank = 1 THEN entity_name        END)                  AS primary_entity_name,
         MAX(CASE WHEN _erank = 1 THEN entity_version     END)                  AS primary_entity_version
  FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY workspace_id, endpoint_id ORDER BY latest_change_time DESC) AS _erank
    FROM entities
  )
  GROUP BY workspace_id, endpoint_id
),
ent_map AS (   -- served_entity_id -> endpoint_id, so endpoint_usage (which has no endpoint_id) can roll up to endpoint level
  SELECT DISTINCT workspace_id, endpoint_id, served_entity_id FROM entities
),
usage_entity AS (   -- per served entity, inside the analysis window
  SELECT eu.workspace_id, eu.served_entity_id,
         COUNT(*) AS total_requests,
         MAX(CAST(eu.request_time AS DATE)) AS last_request_date
  FROM system.serving.endpoint_usage eu
  WHERE eu.request_time >= dateadd(day, -:period_days, current_date())
    AND eu.request_time <  current_date()
  GROUP BY eu.workspace_id, eu.served_entity_id
),
usage_endpoint AS (   -- requests rolled up to endpoint, inside the analysis window
  SELECT m.workspace_id, m.endpoint_id,
         SUM(ue.total_requests) AS ep_requests,
         MAX(ue.last_request_date) AS ep_last_request_date
  FROM usage_entity ue
  JOIN ent_map m ON ue.workspace_id = m.workspace_id AND ue.served_entity_id = m.served_entity_id
  GROUP BY m.workspace_id, m.endpoint_id
),
usage_ever AS (   -- ANY tracked row over the full retention window -> distinguishes "tracking off" from "idle in window"
  SELECT m.workspace_id, m.endpoint_id, COUNT(*) AS ever_requests
  FROM system.serving.endpoint_usage eu
  JOIN ent_map m ON eu.workspace_id = m.workspace_id AND eu.served_entity_id = m.served_entity_id
  WHERE eu.request_time >= dateadd(day, -:retention_days, current_date())
  GROUP BY m.workspace_id, m.endpoint_id
),
retention_history AS (   -- how far the account's OWN endpoint_usage history actually reaches back
  SELECT workspace_id, MIN(request_time) AS earliest_request_time
  FROM system.serving.endpoint_usage
  GROUP BY workspace_id
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM system.billing.list_prices
),
cost AS (   -- billing per (workspace, endpoint, product); VS carries endpoint_name, model serving carries endpoint_id -> COALESCE
  SELECT u.workspace_id,
         u.usage_metadata.endpoint_id                     AS endpoint_id,
         u.usage_metadata.endpoint_name                   AS endpoint_name_billing,
         u.billing_origin_product                         AS product,
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
    AND (u.usage_metadata.endpoint_id IS NOT NULL OR u.usage_metadata.endpoint_name IS NOT NULL)
    AND u.usage_date >= dateadd(day, -:period_days, current_date())
    AND u.usage_date <  current_date()
  GROUP BY u.workspace_id, u.usage_metadata.endpoint_id, u.usage_metadata.endpoint_name, u.billing_origin_product
),
cost_ep AS (   -- collapse products -> one row per endpoint (total cost), keep the product list
  SELECT workspace_id,
         COALESCE(endpoint_id, endpoint_name_billing)              AS endpoint_key,
         MAX(endpoint_id)                                          AS endpoint_id,
         MAX(endpoint_name_billing)                                AS endpoint_name_billing,
         SUM(net_dbus)                                             AS net_dbus,
         SUM(est_usd_list)                                         AS est_usd_list,
         array_join(collect_set(product), ', ')                    AS products,
         MAX(CASE WHEN product = 'VECTOR_SEARCH' THEN 1 ELSE 0 END) AS is_vs,
         CASE
           WHEN SUM(CASE WHEN price_basis = 'unpriced' THEN 1 ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN price_basis = 'priced'   THEN 1 ELSE 0 END) = 0
                AND SUM(CASE WHEN price_basis = 'free' THEN 1 ELSE 0 END) > 0 THEN 'free'
           ELSE 'priced'
         END                                                        AS price_basis
  FROM cost GROUP BY workspace_id, COALESCE(endpoint_id, endpoint_name_billing)
),
scored AS (
  SELECT
    ce.workspace_id,
    w.workspace_name,
    ce.endpoint_id,
    COALESCE(ent.endpoint_name, ce.endpoint_name_billing) AS endpoint_name,
    ce.products,
    ent.primary_served_entity_id                           AS served_entity_id,
    ent.primary_served_entity_name                         AS served_entity_name,
    ent.primary_entity_type                                AS entity_type,
    ent.primary_entity_name                                AS entity_name,
    ent.primary_entity_version                             AS entity_version,
    ent.latest_change_time,
    ROUND(ce.net_dbus, 1)                                  AS net_dbus,
    ROUND(ce.est_usd_list, 2)                              AS est_usd_list,
    ce.price_basis                                         AS price_basis,
    -- est_wasted_usd_list (P3-WASTEUSD): mirrors the status CASE's own CRITICAL branch below
    -- exactly - a confirmed-idle endpoint's WHOLE measured spend is waste; NULL (not 0) where idle
    -- cannot be confirmed (Vector Search / untracked / tracking-off / insufficient history); 0 for
    -- real, if low, traffic. Never divided: one row per endpoint now, so nothing to double-count.
    CASE
      WHEN ce.is_vs = 1                                            THEN NULL
      WHEN ent.endpoint_id IS NULL                                THEN NULL
      WHEN COALESCE(uev.ever_requests, 0) = 0 AND ce.net_dbus > 0  THEN NULL
      -- a missing price would read as a smaller waste; unknown stays NULL, never an undercount
      WHEN ce.price_basis = 'unpriced'                             THEN NULL
      WHEN COALESCE(uep.ep_requests, 0) = 0 AND ce.net_dbus > 0    THEN ROUND(ce.est_usd_list, 2)
      ELSE 0
    END                                                     AS est_wasted_usd_list,
    COALESCE(pue.total_requests, 0)                        AS entity_requests_window,
    pue.last_request_date                                  AS entity_last_request_date,
    COALESCE(uep.ep_requests, 0)                           AS endpoint_requests_window,
    uep.ep_last_request_date                               AS endpoint_last_request_date,
    uep.ep_last_request_date                               AS ep_last_request_date,  -- kept: old column name
    -- Is the "0 requests" real, or just untracked? (free-text detail; the enum band is `status`)
    CASE
      WHEN ce.is_vs = 1                                            THEN 'Vector Search - no serving-table telemetry (see cost_vector_search_spend)'
      WHEN ent.endpoint_id IS NULL                                THEN 'bills but not in served_entities (not a tracked model-serving endpoint)'
      WHEN COALESCE(uev.ever_requests, 0) = 0 AND ce.net_dbus > 0
           AND rh.earliest_request_time IS NOT NULL
           AND datediff(current_date(), rh.earliest_request_time) < :retention_days
        THEN 'not enough endpoint_usage history to judge tracking (retention shorter than the lookback)'
      WHEN COALESCE(uev.ever_requests, 0) = 0 AND ce.net_dbus > 0  THEN 'usage tracking likely OFF - spend but zero tracked rows in retention'
      WHEN COALESCE(uep.ep_requests, 0) = 0                        THEN 'tracking ON - idle in window (had traffic within retention)'
      ELSE                                                             'tracking ON - active'
    END AS tracking_status,
    CASE
      WHEN ce.is_vs = 1                                            THEN 'not_serving_telemetry'
      WHEN ent.endpoint_id IS NULL                                THEN 'not_a_tracked_endpoint'
      WHEN COALESCE(uev.ever_requests, 0) = 0 AND ce.net_dbus > 0
           AND rh.earliest_request_time IS NOT NULL
           AND datediff(current_date(), rh.earliest_request_time) < :retention_days
        THEN 'insufficient_retention_history'
    END AS not_assessed_reason,
    rh.earliest_request_time,
    CASE WHEN rh.earliest_request_time IS NULL THEN NULL
         ELSE datediff(current_date(), rh.earliest_request_time) END AS retention_days_available,
    :retention_days                                        AS retention_days_required,
    ent.served_entity_count,
    ent.entity_types,
    ent.entity_names,
    ent.primary_served_entity_id,
    ent.primary_served_entity_name,
    ent.primary_entity_type,
    ent.primary_entity_name,
    ent.primary_entity_version,
    CASE
      WHEN ce.is_vs = 1                                                THEN 'NOT_ASSESSED'   -- Vector Search: no serving telemetry
      WHEN ent.endpoint_id IS NULL                                    THEN 'NOT_ASSESSED'   -- bills but not a tracked model-serving endpoint
      WHEN COALESCE(uev.ever_requests, 0) = 0 AND ce.net_dbus > 0
           AND rh.earliest_request_time IS NOT NULL
           AND datediff(current_date(), rh.earliest_request_time) < :retention_days     THEN 'NOT_ASSESSED'  -- genuinely-tracked, too new to judge
      WHEN COALESCE(uev.ever_requests, 0) = 0 AND ce.net_dbus > 0     THEN 'WARN'            -- spend but tracking off (no history at all, or not enough): enable tracking, do not call it idle
      WHEN COALESCE(uep.ep_requests, 0) = 0 AND ce.net_dbus > 0       THEN 'CRITICAL'        -- tracked + spend + zero requests = truly idle
      WHEN COALESCE(uep.ep_requests, 0) <= :warn_low_requests         THEN 'WARN'
      ELSE 'OK'
    END AS status
  FROM cost_ep ce
  LEFT JOIN entity_agg     ent ON ent.workspace_id = ce.workspace_id AND ent.endpoint_id = ce.endpoint_id
  LEFT JOIN usage_entity   pue ON pue.workspace_id = ce.workspace_id AND pue.served_entity_id = ent.primary_served_entity_id
  LEFT JOIN usage_endpoint uep ON uep.workspace_id = ce.workspace_id AND uep.endpoint_id = ce.endpoint_id
  LEFT JOIN usage_ever     uev ON uev.workspace_id = ce.workspace_id AND uev.endpoint_id = ce.endpoint_id
  LEFT JOIN retention_history rh ON rh.workspace_id = ce.workspace_id
  LEFT JOIN system.access.workspaces_latest w ON w.workspace_id = ce.workspace_id
)
SELECT *
FROM scored
ORDER BY
  CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
  net_dbus DESC, endpoint_requests_window ASC
LIMIT :top_n
