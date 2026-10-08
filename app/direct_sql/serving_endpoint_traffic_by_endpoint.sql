-- generated from dbt/models/databricks_direct/serving_ai/d_serving_endpoint_traffic_by_endpoint.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/serving_ai/serving_endpoint_traffic_by_endpoint.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH entities AS (
  SELECT workspace_id, endpoint_id, endpoint_name, served_entity_id
  FROM (
    SELECT
      se.workspace_id, se.endpoint_id, se.endpoint_name, se.served_entity_id,
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
  -- Pre-aggregated to EXACTLY this query's join grain (endpoint_id) so the LEFT JOIN is strictly 1:1
  -- and never multiplies result rows. endpoint_id is a globally-unique GUID -> keyed on id alone.
  SELECT
    u.usage_metadata.endpoint_id                     AS endpoint_id,
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
  GROUP BY u.usage_metadata.endpoint_id
)
SELECT
  ent.workspace_id                                  AS workspace_id,
  ent.endpoint_id                                   AS endpoint_id,
  ent.endpoint_name AS endpoint_name,
  COUNT(*)                                          AS request_count,
  SUM(CASE WHEN eu.status_code BETWEEN 200 AND 299 THEN 1 ELSE 0 END) AS success_requests,
  SUM(CASE WHEN eu.status_code IS NOT NULL AND NOT (eu.status_code BETWEEN 200 AND 299) THEN 1 ELSE 0 END) AS error_requests,
  SUM(COALESCE(eu.input_token_count, 0))            AS input_tokens,
  SUM(COALESCE(eu.output_token_count, 0))           AS output_tokens,
  MAX(CAST(eu.request_time AS DATE))                AS last_request_date,
  -- cost columns: rollup is constant within each endpoint group (1:1), surfaced via MAX() so the
  -- existing per-endpoint grain and COUNT(*) semantics are unchanged.
  MAX(COALESCE(cr.net_dbus, 0))                     AS net_dbus,
  MAX(COALESCE(cr.est_usd_list, 0))                 AS est_usd_list,
  COALESCE(MAX(cr.price_basis), 'priced')           AS price_basis
FROM `system`.`serving`.`endpoint_usage` eu
LEFT JOIN entities ent
  ON  eu.workspace_id     = ent.workspace_id
  AND eu.served_entity_id = ent.served_entity_id
LEFT JOIN cost_rollup cr
  ON  ent.endpoint_id = cr.endpoint_id
WHERE eu.request_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  AND eu.request_time <  __AS_OF_DATE__
GROUP BY ent.workspace_id, ent.endpoint_id, ent.endpoint_name
ORDER BY request_count DESC, endpoint_id
) q
