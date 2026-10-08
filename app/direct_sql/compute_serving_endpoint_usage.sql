-- generated from dbt/models/databricks_direct/serving_ai/d_compute_serving_endpoint_usage.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/serving_ai/compute_serving_endpoint_usage.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
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
    FROM `system`.`serving`.`served_entities` se
  )
  WHERE _rn = 1
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
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.endpoint_id IS NOT NULL
    AND u.usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND u.usage_date <  __AS_OF_DATE__
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
  FROM `system`.`serving`.`endpoint_usage` eu
  LEFT JOIN entities ent
    ON  eu.workspace_id     = ent.workspace_id
    AND eu.served_entity_id = ent.served_entity_id
  WHERE eu.request_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND eu.request_time <  __AS_OF_DATE__
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
) q
