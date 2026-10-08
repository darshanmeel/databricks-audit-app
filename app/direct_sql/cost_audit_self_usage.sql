-- generated from dbt/models/databricks_direct/cost/d_cost_audit_self_usage.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_audit_self_usage.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH matched AS (
  SELECT workspace_id, compute.warehouse_id AS warehouse_id, statement_id,
         client_application, total_duration_ms, start_time
  FROM `system`.`query`.`history`
  WHERE start_time >= date_sub(__AS_OF_DATE__, __WINDOW_DAYS__)
    AND start_time < __AS_OF_DATE__
    AND compute.warehouse_id IS NOT NULL
    -- the export's query tag (fixed key audit_app), or its client-application name
    AND (client_application ILIKE '%__QUERY_SOURCE__%' OR array_contains(map_keys(query_tags), 'audit_app'))
),
warehouse_all AS (
  SELECT workspace_id, compute.warehouse_id AS warehouse_id,
         SUM(total_duration_ms) AS all_duration_ms
  FROM `system`.`query`.`history`
  WHERE start_time >= date_sub(__AS_OF_DATE__, __WINDOW_DAYS__)
    AND start_time < __AS_OF_DATE__
    AND compute.warehouse_id IS NOT NULL
  GROUP BY workspace_id, compute.warehouse_id
),
matched_agg AS (
  SELECT workspace_id, warehouse_id,
         COUNT(*) AS matched_statement_count,
         SUM(total_duration_ms) AS matched_duration_ms,
         MIN(start_time) AS first_query_time,
         MAX(start_time) AS last_query_time,
         MAX(client_application) AS matched_client_application
  FROM matched
  GROUP BY workspace_id, warehouse_id
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
wh_usd AS (
  SELECT u.workspace_id, u.usage_metadata.warehouse_id AS warehouse_id,
         SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS warehouse_usd_list,
         CASE WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                            THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
              WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
              ELSE 'priced' END AS price_basis
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.warehouse_id IS NOT NULL
    AND u.usage_date >= date_sub(__AS_OF_DATE__, __WINDOW_DAYS__)
    AND u.usage_date < __AS_OF_DATE__
  GROUP BY u.workspace_id, u.usage_metadata.warehouse_id
),
wh_name AS (
  SELECT warehouse_id, warehouse_name
  FROM `system`.`compute`.`warehouses`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
)
SELECT m.workspace_id,
       m.warehouse_id,
       n.warehouse_name,
       m.matched_client_application,
       m.matched_statement_count,
       ROUND(m.matched_duration_ms / 1000.0, 1) AS matched_duration_secs,
       ROUND(a.all_duration_ms / 1000.0, 1) AS warehouse_all_duration_secs,
       ROUND(m.matched_duration_ms * 100.0 / NULLIF(a.all_duration_ms, 0), 1) AS matched_share_pct,
       ROUND(w.warehouse_usd_list, 2) AS est_usd_list,
       CASE WHEN w.price_basis IN ('priced', 'free') AND a.all_duration_ms > 0
            THEN ROUND(w.warehouse_usd_list * m.matched_duration_ms / a.all_duration_ms, 2) END AS est_audit_usd_list,
       w.price_basis,
       m.first_query_time, m.last_query_time
FROM matched_agg m
LEFT JOIN warehouse_all a ON a.workspace_id = m.workspace_id AND a.warehouse_id = m.warehouse_id
LEFT JOIN wh_usd w        ON w.workspace_id = m.workspace_id AND w.warehouse_id = m.warehouse_id
LEFT JOIN wh_name n       ON n.warehouse_id = m.warehouse_id
ORDER BY est_audit_usd_list DESC NULLS LAST, m.workspace_id, m.warehouse_id
) q
