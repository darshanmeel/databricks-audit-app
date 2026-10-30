-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_pipeline_idle_tail_duration.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_pipeline_idle_tail_duration.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH price AS (
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
  SELECT u.workspace_id,
         u.usage_metadata.dlt_pipeline_id AS dlt_pipeline_id,
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
    AND u.usage_metadata.dlt_pipeline_id IS NOT NULL
    AND u.usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND u.usage_date <  __AS_OF_DATE__
  GROUP BY u.workspace_id, u.usage_metadata.dlt_pipeline_id
),
billed_hours AS (
  SELECT u.workspace_id, u.usage_metadata.dlt_pipeline_id AS pipeline_id, u.record_id,
         u.usage_start_time, u.usage_end_time, u.usage_quantity,
         COALESCE(u.product_features.is_serverless, FALSE) AS serverless
  FROM `system`.`billing`.`usage` u
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.dlt_pipeline_id IS NOT NULL
    AND u.usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND u.usage_date <  __AS_OF_DATE__
),
billed_active AS (
  -- whether any update slice overlaps the billed row's hour; serverless rows never linger
  SELECT b.workspace_id, b.pipeline_id, b.record_id, b.usage_quantity, b.serverless,
         MAX(CASE WHEN s.pipeline_id IS NOT NULL THEN 1 ELSE 0 END) AS overlapped
  FROM billed_hours b
  LEFT JOIN `system`.`lakeflow`.`pipeline_update_timeline` s
    ON s.workspace_id = b.workspace_id AND s.pipeline_id = b.pipeline_id
   AND s.period_start_time < b.usage_end_time AND s.period_end_time > b.usage_start_time
  GROUP BY b.workspace_id, b.pipeline_id, b.record_id, b.usage_quantity, b.serverless
),
idle AS (
  SELECT workspace_id, pipeline_id,
         SUM(CASE WHEN overlapped = 0 AND NOT serverless THEN usage_quantity ELSE 0 END) AS idle_dbus
  FROM billed_active
  GROUP BY workspace_id, pipeline_id
)
SELECT u.workspace_id, u.pipeline_id, p.pipeline_type, p.setting_continuous, p.setting_development,
       COUNT(DISTINCT CASE WHEN u.result_state IS NOT NULL THEN u.update_id END) AS updates,
       -- every hourly slice counts: an update over an hour writes one row per hour, the result on the last
       SUM(unix_timestamp(u.period_end_time) - unix_timestamp(u.period_start_time)) AS active_seconds_total,
       COALESCE(MAX(cr.net_dbus), 0)     AS net_dbus,
       COALESCE(MAX(cr.est_usd_list), 0) AS est_usd_list,
       COALESCE(MAX(cr.price_basis), 'priced') AS price_basis,
       COALESCE(MAX(i.idle_dbus), 0) AS idle_dbus,
       MAX(i.idle_dbus) / NULLIF(MAX(cr.net_dbus), 0) AS idle_share,
       -- status: worst-first band on DBUs billed outside update time (field heuristic; 100 / 500).
       CASE
         WHEN p.setting_continuous = true THEN 'NOT_ASSESSED'
         WHEN COALESCE(MAX(i.idle_dbus), 0) >= 500 THEN 'CRITICAL'
         WHEN COALESCE(MAX(i.idle_dbus), 0) >= 100 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`lakeflow`.`pipeline_update_timeline` u
LEFT JOIN (
  SELECT workspace_id, pipeline_id, pipeline_type,
         settings.continuous  AS setting_continuous,
         settings.development AS setting_development
  FROM `system`.`lakeflow`.`pipelines`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC) = 1
) p
  ON u.workspace_id = p.workspace_id AND u.pipeline_id = p.pipeline_id
LEFT JOIN cost_rollup cr
  ON u.workspace_id = cr.workspace_id AND u.pipeline_id = cr.dlt_pipeline_id
LEFT JOIN idle i
  ON u.workspace_id = i.workspace_id AND u.pipeline_id = i.pipeline_id
WHERE u.period_start_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  AND u.period_end_time < date_trunc('DAY', __AS_OF_TS__)
GROUP BY u.workspace_id, u.pipeline_id, p.pipeline_type, p.setting_continuous, p.setting_development
ORDER BY idle_dbus DESC, net_dbus DESC
) q
