-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_jobs_no_timeout.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_jobs_no_timeout.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH latest_jobs AS (
  SELECT workspace_id, job_id, name AS job_name, timeout_seconds, delete_time, change_time,
         -- a RUN_DURATION_SECONDS/GREATER_THAN health rule bounds a run's duration same as a timeout;
         -- health_rules holds at most a couple of entries, so three positions is enough headroom
         COALESCE((try_element_at(health_rules, 1).metric = 'RUN_DURATION_SECONDS' AND try_element_at(health_rules, 1).operator = 'GREATER_THAN')
          OR (try_element_at(health_rules, 2).metric = 'RUN_DURATION_SECONDS' AND try_element_at(health_rules, 2).operator = 'GREATER_THAN')
          OR (try_element_at(health_rules, 3).metric = 'RUN_DURATION_SECONDS' AND try_element_at(health_rules, 3).operator = 'GREATER_THAN'), FALSE) AS has_duration_health_rule
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),
population_floor AS (
  -- earliest change_time, over this workspace's FULL job SCD history, at which any job row already
  -- shows a non-NULL timeout_seconds -- the point after which a NULL is assumed to mean "explicitly
  -- no bound", not "not yet populated".
  SELECT workspace_id, MIN(change_time) AS population_floor_time
  FROM `system`.`lakeflow`.`jobs`
  WHERE timeout_seconds IS NOT NULL
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
  -- Per (workspace_id, job_id) DBUs + effective-list $ over the cost-lookback window. job_id is NOT
  -- globally unique -> keyed on workspace_id + job_id.
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
    AND u.usage_date >= date_sub(__AS_OF_DATE__, __WINDOW_DAYS__)
    AND u.usage_date <  __AS_OF_DATE__
  GROUP BY u.workspace_id, u.usage_metadata.job_id
),
judged AS (
  SELECT lj.workspace_id, lj.job_id, lj.job_name, lj.timeout_seconds, lj.has_duration_health_rule,
         (lj.timeout_seconds IS NULL OR lj.timeout_seconds = 0) AND NOT lj.has_duration_health_rule AS no_timeout,
         CASE
           WHEN lj.timeout_seconds IS NULL AND NOT lj.has_duration_health_rule
                AND (pf.population_floor_time IS NULL OR lj.change_time < pf.population_floor_time)
             THEN 'not_populated'
           WHEN lj.timeout_seconds IS NULL AND NOT lj.has_duration_health_rule
             THEN 'after_population'
         END AS timeout_null_reason
  FROM latest_jobs lj
  LEFT JOIN population_floor pf ON pf.workspace_id = lj.workspace_id
  WHERE lj.delete_time IS NULL
)
SELECT j.workspace_id, w.workspace_name, j.job_id, j.job_name, j.timeout_seconds,
       j.no_timeout, j.has_duration_health_rule AS bounded_by_health_rule, j.timeout_null_reason,
       ROUND(COALESCE(cr.net_dbus, 0), 2)     AS net_dbus,
       ROUND(COALESCE(cr.est_usd_list, 0), 2) AS est_usd_list,
       COALESCE(cr.price_basis, 'priced')     AS price_basis,
       -- status: a flagged job (no_timeout) reads WARN, or CRITICAL when its own spend over the
       -- cost-lookback window is also high (200) -- an unflagged or health-rule-
       -- bounded job is OK. NOT_ASSESSED when the NULL itself predates population, not a confirmed gap.
       CASE
         WHEN j.no_timeout AND j.timeout_null_reason = 'not_populated' THEN 'NOT_ASSESSED'
         WHEN j.no_timeout AND COALESCE(cr.est_usd_list, 0) >= 200 THEN 'CRITICAL'
         WHEN j.no_timeout THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE WHEN j.no_timeout AND j.timeout_null_reason = 'not_populated' THEN 'timeout_not_populated' END AS not_assessed_reason
FROM judged j
LEFT JOIN cost_rollup cr ON cr.workspace_id = j.workspace_id AND cr.job_id = j.job_id
LEFT JOIN `system`.`access`.`workspaces_latest` w ON w.workspace_id = j.workspace_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         est_usd_list DESC, j.workspace_id, j.job_id
) q
