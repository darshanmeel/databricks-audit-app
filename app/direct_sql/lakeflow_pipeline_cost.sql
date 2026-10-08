-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_pipeline_cost.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_pipeline_cost.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH pipe_dbus AS (
  -- DBUs billed to each pipeline over the window. Net across ALL record_types; DBU only.
  -- Split maintenance DBUs out via dlt_maintenance_id so they are reported separately.
  SELECT workspace_id,
         usage_metadata.dlt_pipeline_id AS pipeline_id,
         SUM(usage_quantity) AS net_pipeline_dbus,
         SUM(CASE WHEN usage_metadata.dlt_maintenance_id IS NOT NULL
                  THEN usage_quantity ELSE 0 END) AS net_maintenance_dbus
  FROM `system`.`billing`.`usage`
  WHERE usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND usage_date < __AS_OF_DATE__
    AND upper(usage_unit) = 'DBU'
    AND usage_metadata.dlt_pipeline_id IS NOT NULL
  GROUP BY workspace_id, usage_metadata.dlt_pipeline_id
),
pipe_list AS (
  -- Effective-list-price estimate (DEC-66.1): SUM(usage_quantity * list_rate) per pipeline.
  -- Never a negotiated or billed dollar - no negotiated-rate source exists in system.billing.
  SELECT u.workspace_id,
         u.usage_metadata.dlt_pipeline_id AS pipeline_id,
         MAX(lp.currency_code) AS currency_code,
         SUM(u.usage_quantity * lp.list_rate) AS net_list_cost,
         SUM(CASE WHEN u.usage_metadata.dlt_maintenance_id IS NOT NULL
                  THEN u.usage_quantity * lp.list_rate ELSE 0 END) AS net_maintenance_list_cost,
         CASE
           WHEN SUM(CASE WHEN lp.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                         THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN lp.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END AS price_basis
  FROM `system`.`billing`.`usage` u
  LEFT JOIN (
    SELECT sku_name, cloud, currency_code, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective (post-promotion) list price - DEC-66.1
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
  ) lp
    ON u.sku_name = lp.sku_name
   AND u.cloud    = lp.cloud
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND u.usage_date < __AS_OF_DATE__
    AND upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.dlt_pipeline_id IS NOT NULL
  GROUP BY u.workspace_id, u.usage_metadata.dlt_pipeline_id
),
pipe_meta AS (
  -- SCD2 latest row per (workspace_id, pipeline_id): names-not-ids + type + continuous flag.
  SELECT workspace_id, pipeline_id, name AS pipeline_name, pipeline_type,
         settings.continuous AS is_continuous
  FROM `system`.`lakeflow`.`pipelines`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC
  ) = 1
),
pipe_updates_any AS (
  -- every update row seen in the window, ended or not -- tells "no update activity at all" apart
  -- from "an update is running but has not ended yet" before pipe_updates (end rows only) is read.
  SELECT workspace_id, pipeline_id, COUNT(*) AS update_rows_any
  FROM `system`.`lakeflow`.`pipeline_update_timeline`
  WHERE period_start_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  GROUP BY workspace_id, pipeline_id
),
pipe_updates AS (
  -- Over-refresh signal (distinct updates + total active seconds) and the delivered-vs-not split,
  -- both end-row-only (result_state IS NOT NULL).
  SELECT workspace_id, pipeline_id,
         COUNT(*)                                                             AS update_end_rows,
         COUNT(DISTINCT update_id)                                            AS updates,
         SUM(CASE WHEN result_state = 'COMPLETED' THEN 1 ELSE 0 END)          AS updates_completed,
         SUM(CASE WHEN result_state IN ('FAILED', 'CANCELED') THEN 1 ELSE 0 END) AS updates_failed_or_canceled,
         SUM(unix_timestamp(period_end_time) - unix_timestamp(period_start_time)) AS active_seconds_total
  FROM `system`.`lakeflow`.`pipeline_update_timeline`
  WHERE period_start_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND period_end_time < date_trunc('DAY', __AS_OF_TS__)
    AND result_state IS NOT NULL   -- update end row only
  GROUP BY workspace_id, pipeline_id
),
combined AS (
  SELECT d.workspace_id, d.pipeline_id,
         d.net_pipeline_dbus, d.net_maintenance_dbus,
         l.currency_code, l.net_list_cost, l.net_maintenance_list_cost,
         COALESCE(l.price_basis, 'priced')          AS price_basis,
         m.pipeline_name, m.pipeline_type, COALESCE(m.is_continuous, FALSE) AS is_continuous,
         COALESCE(up.updates, 0)                    AS updates,
         COALESCE(up.update_end_rows, 0)            AS update_end_rows,
         COALESCE(ua.update_rows_any, 0)            AS update_rows_any,
         COALESCE(up.updates_completed, 0)          AS updates_completed,
         COALESCE(up.updates_failed_or_canceled, 0) AS updates_failed_or_canceled,
         COALESCE(up.active_seconds_total, 0)       AS active_seconds_total,
         -- in_flight_only: the window saw update activity but no update has ended yet -- cannot yet
         -- tell "delivering" from "nothing delivered".
         (COALESCE(ua.update_rows_any, 0) > 0 AND COALESCE(up.update_end_rows, 0) = 0) AS in_flight_only,
         -- nothing_delivered: either no update activity in the window at all, or every ended update
         -- failed or was canceled (COMPLETED count of 0 alone is not enough -- an update still in
         -- flight also has 0 completed, which is why in_flight_only is checked first).
         (
           (COALESCE(up.update_end_rows, 0) = 0 AND COALESCE(ua.update_rows_any, 0) = 0)
           OR (COALESCE(up.update_end_rows, 0) > 0
               AND COALESCE(up.updates_failed_or_canceled, 0) = up.update_end_rows)
         ) AS nothing_delivered
  FROM pipe_dbus d
  LEFT JOIN pipe_list l         ON d.workspace_id = l.workspace_id AND d.pipeline_id = l.pipeline_id
  LEFT JOIN pipe_meta m         ON d.workspace_id = m.workspace_id AND d.pipeline_id = m.pipeline_id
  LEFT JOIN pipe_updates up     ON d.workspace_id = up.workspace_id AND d.pipeline_id = up.pipeline_id
  LEFT JOIN pipe_updates_any ua ON d.workspace_id = ua.workspace_id AND d.pipeline_id = ua.pipeline_id
)
SELECT c.workspace_id,
       c.pipeline_id,
       c.pipeline_name AS pipeline_name,
       c.pipeline_type, c.is_continuous,
       c.net_pipeline_dbus, c.net_maintenance_dbus,
       c.currency_code, c.net_list_cost, c.net_maintenance_list_cost,
       c.price_basis,
       c.updates, c.update_end_rows, c.update_rows_any,
       c.updates_completed, c.updates_failed_or_canceled,
       c.active_seconds_total,
       c.in_flight_only, c.nothing_delivered,
       (c.net_pipeline_dbus >= 500) AS is_high_spend,
       CASE
         WHEN c.is_continuous THEN NULL
         WHEN c.in_flight_only THEN NULL
         WHEN c.nothing_delivered THEN ROUND(c.net_list_cost - COALESCE(c.net_maintenance_list_cost, 0), 2)
         ELSE 0
       END AS est_wasted_usd_list,
       -- status: CRITICAL/WARN now require the nothing-delivered signal at 2000, not
       -- size alone (field heuristic; 500 / 2000).
       CASE
         WHEN c.is_continuous THEN 'NOT_ASSESSED'    -- continuous pipeline: no discrete completed-update signal
         WHEN c.in_flight_only THEN 'NOT_ASSESSED'   -- update still running at window end: too early to call
         WHEN c.net_pipeline_dbus >= 2000 AND c.nothing_delivered THEN 'CRITICAL'
         WHEN c.net_pipeline_dbus >= 2000 THEN 'WARN'
         WHEN c.net_pipeline_dbus >= 500 AND c.nothing_delivered THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN c.is_continuous THEN 'continuous_pipeline'
         WHEN c.in_flight_only THEN 'update_in_flight'
       END AS not_assessed_reason
FROM combined c
WHERE c.net_pipeline_dbus > 0
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         c.net_pipeline_dbus DESC
) q
