-- generated from dbt/models/databricks_direct/serving_ai/d_compute_serving_endpoint_cost_status.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/serving_ai/compute_serving_endpoint_cost_status.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH entities AS (   -- served_entities is change-history -> collapse to the latest config row per entity
  SELECT workspace_id, endpoint_id, endpoint_name, served_entity_id, served_entity_name,
         entity_type, entity_name, entity_version, latest_change_time
  FROM (
    SELECT se.workspace_id, se.endpoint_id, se.endpoint_name, se.served_entity_id,
           se.served_entity_name, se.entity_type, se.entity_name, se.entity_version,
           MAX(se.change_time) OVER (PARTITION BY se.workspace_id, se.endpoint_id, se.served_entity_id) AS latest_change_time,
           ROW_NUMBER() OVER (PARTITION BY se.workspace_id, se.endpoint_id, se.served_entity_id ORDER BY se.change_time DESC) AS _rn
    FROM `system`.`serving`.`served_entities` se
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
  FROM `system`.`serving`.`endpoint_usage` eu
  WHERE eu.request_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND eu.request_time <  __AS_OF_DATE__
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
  FROM `system`.`serving`.`endpoint_usage` eu
  JOIN ent_map m ON eu.workspace_id = m.workspace_id AND eu.served_entity_id = m.served_entity_id
  WHERE eu.request_time >= dateadd(day, -90, __AS_OF_DATE__)
  GROUP BY m.workspace_id, m.endpoint_id
),
retention_history AS (   -- how far the account's OWN endpoint_usage history actually reaches back
  SELECT workspace_id, MIN(request_time) AS earliest_request_time
  FROM `system`.`serving`.`endpoint_usage`
  GROUP BY workspace_id
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
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
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND (u.usage_metadata.endpoint_id IS NOT NULL OR u.usage_metadata.endpoint_name IS NOT NULL)
    AND u.usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND u.usage_date <  __AS_OF_DATE__
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
           AND datediff(__AS_OF_DATE__, rh.earliest_request_time) < 90
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
           AND datediff(__AS_OF_DATE__, rh.earliest_request_time) < 90
        THEN 'insufficient_retention_history'
    END AS not_assessed_reason,
    rh.earliest_request_time,
    CASE WHEN rh.earliest_request_time IS NULL THEN NULL
         ELSE datediff(__AS_OF_DATE__, rh.earliest_request_time) END AS retention_days_available,
    90                                        AS retention_days_required,
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
           AND datediff(__AS_OF_DATE__, rh.earliest_request_time) < 90     THEN 'NOT_ASSESSED'  -- genuinely-tracked, too new to judge
      WHEN COALESCE(uev.ever_requests, 0) = 0 AND ce.net_dbus > 0     THEN 'WARN'            -- spend but tracking off (no history at all, or not enough): enable tracking, do not call it idle
      WHEN COALESCE(uep.ep_requests, 0) = 0 AND ce.net_dbus > 0       THEN 'CRITICAL'        -- tracked + spend + zero requests = truly idle
      WHEN COALESCE(uep.ep_requests, 0) <= 10         THEN 'WARN'
      ELSE 'OK'
    END AS status
  FROM cost_ep ce
  LEFT JOIN entity_agg     ent ON ent.workspace_id = ce.workspace_id AND ent.endpoint_id = ce.endpoint_id
  LEFT JOIN usage_entity   pue ON pue.workspace_id = ce.workspace_id AND pue.served_entity_id = ent.primary_served_entity_id
  LEFT JOIN usage_endpoint uep ON uep.workspace_id = ce.workspace_id AND uep.endpoint_id = ce.endpoint_id
  LEFT JOIN usage_ever     uev ON uev.workspace_id = ce.workspace_id AND uev.endpoint_id = ce.endpoint_id
  LEFT JOIN retention_history rh ON rh.workspace_id = ce.workspace_id
  LEFT JOIN `system`.`access`.`workspaces_latest` w ON w.workspace_id = ce.workspace_id
)
SELECT *
FROM scored
ORDER BY
  CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
  net_dbus DESC, endpoint_requests_window ASC
LIMIT 100000
) q
