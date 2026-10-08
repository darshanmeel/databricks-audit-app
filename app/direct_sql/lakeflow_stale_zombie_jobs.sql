-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_stale_zombie_jobs.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_stale_zombie_jobs.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH latest_jobs AS (
  SELECT workspace_id, job_id, name, run_as, creator_id, create_time, delete_time
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),
last_run AS (
  SELECT workspace_id, job_id, MAX(period_start_time) AS last_run_start
  FROM `system`.`lakeflow`.`job_run_timeline`
  GROUP BY workspace_id, job_id
),
capture_floor AS (
  -- how far back this WORKSPACE's own job_run_timeline history reaches, unfiltered by job
  SELECT workspace_id,
         datediff(__AS_OF_DATE__, MIN(period_start_time)) AS capture_floor_days
  FROM `system`.`lakeflow`.`job_run_timeline`
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
cost_rollup AS (
  SELECT u.workspace_id,
         u.usage_metadata.job_id AS job_id,
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
    AND u.usage_metadata.job_id IS NOT NULL
    AND u.usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND u.usage_date <  __AS_OF_DATE__
  GROUP BY u.workspace_id, u.usage_metadata.job_id
)
SELECT j.workspace_id, j.job_id,
       j.name AS name,
       r.last_run_start,
       j.create_time,
       COALESCE(cf.capture_floor_days, 0) AS capture_floor_days,
       CASE WHEN r.last_run_start IS NULL
              OR r.last_run_start < dateadd(day, -30, __AS_OF_DATE__)
            THEN 1 ELSE 0 END AS is_stale,
       COALESCE(cr.net_dbus, 0)     AS net_dbus,
       COALESCE(cr.est_usd_list, 0) AS est_usd_list,
       COALESCE(cr.price_basis, 'priced') AS price_basis,
       -- status: worst-first band on days since last run (field heuristic; 30 / 90).
       -- a NULL last_run_start is NOT_ASSESSED, never WARN/CRITICAL, while the job or this workspace's
       -- own capture history is too young to call it stale (too_young_to_call_stale).
       CASE
         WHEN r.last_run_start IS NULL
              AND (COALESCE(cf.capture_floor_days, 0) < 30
                   OR j.create_time > dateadd(day, -30, __AS_OF_DATE__))
              THEN 'NOT_ASSESSED'
         WHEN r.last_run_start IS NULL THEN 'WARN'   -- confirmed no run in a long-enough capture window
         WHEN r.last_run_start < dateadd(day, -90, __AS_OF_DATE__) THEN 'CRITICAL'
         WHEN r.last_run_start < dateadd(day, -30, __AS_OF_DATE__) THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN r.last_run_start IS NULL
              AND (COALESCE(cf.capture_floor_days, 0) < 30
                   OR j.create_time > dateadd(day, -30, __AS_OF_DATE__))
              THEN 'too_young_to_call_stale'
       END AS not_assessed_reason
FROM latest_jobs j
LEFT JOIN last_run r       ON j.workspace_id = r.workspace_id AND j.job_id = r.job_id
LEFT JOIN capture_floor cf ON j.workspace_id = cf.workspace_id
LEFT JOIN cost_rollup cr   ON j.workspace_id = cr.workspace_id AND j.job_id = cr.job_id
WHERE j.delete_time IS NULL
ORDER BY last_run_start ASC
) q
