-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_retries_repairs.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_retries_repairs.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH run_rows AS (
  SELECT workspace_id, job_id, run_id, period_end_time, result_state, trigger_type
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND period_end_time < date_trunc('DAY', __AS_OF_TS__)
),
per_run AS (
  SELECT workspace_id, job_id, run_id,
         SUM(CASE WHEN result_state IS NOT NULL THEN 1 ELSE 0 END) AS attempt_rows,   -- one end row per attempt
         MAX(CASE WHEN upper(trigger_type) LIKE '%RETRY%' THEN 1 ELSE 0 END) AS is_retry_run
  FROM run_rows
  GROUP BY workspace_id, job_id, run_id
  HAVING SUM(CASE WHEN result_state IS NOT NULL THEN 1 ELSE 0 END) > 0
),
task_ends AS (
  SELECT workspace_id, job_run_id, task_key, period_end_time
  FROM `system`.`lakeflow`.`job_task_run_timeline`
  WHERE period_start_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND period_end_time < date_trunc('DAY', __AS_OF_TS__)
    AND result_state IS NOT NULL
),
task_attempts AS (
  -- the attempt of its run a task end row belongs to: how many of the run's end rows came before it
  SELECT t.workspace_id, t.job_run_id, t.task_key, t.period_end_time, COUNT(r.run_id) AS run_attempt
  FROM task_ends t
  LEFT JOIN run_rows r
    ON r.workspace_id = t.workspace_id AND r.run_id = t.job_run_id
   AND r.result_state IS NOT NULL AND r.period_end_time < t.period_end_time
  GROUP BY t.workspace_id, t.job_run_id, t.task_key, t.period_end_time
),
task_retries AS (
  -- within one attempt of a run, a task's end rows beyond its first are automatic retries
  SELECT workspace_id, job_run_id, SUM(task_rows - 1) AS task_retries
  FROM (
    SELECT workspace_id, job_run_id, task_key, run_attempt, COUNT(*) AS task_rows
    FROM task_attempts
    GROUP BY workspace_id, job_run_id, task_key, run_attempt
  ) a
  GROUP BY workspace_id, job_run_id
),
runs AS (
  SELECT pr.*, pr.attempt_rows - 1 AS repairs, COALESCE(tr.task_retries, 0) AS task_retries
  FROM per_run pr
  LEFT JOIN task_retries tr ON tr.workspace_id = pr.workspace_id AND tr.job_run_id = pr.run_id
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
  -- Pre-aggregated cost per (workspace_id, job_id). job_id is not globally unique, so we key on
  -- workspace_id + job_id. Window mirrors this query's trailing window via usage_date.
  SELECT u.workspace_id,
         u.usage_metadata.job_id                          AS job_id,
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
SELECT pr.workspace_id, pr.job_id,
       COUNT(*)             AS distinct_runs,
       SUM(pr.attempt_rows)    AS total_attempt_rows,
       SUM(pr.repairs)         AS repairs,
       SUM(pr.task_retries)    AS task_retries,
       SUM(pr.is_retry_run)    AS retry_runs,
       SUM(pr.repairs + pr.task_retries + pr.is_retry_run) AS total_retries,
       SUM(CASE WHEN pr.repairs + pr.task_retries + pr.is_retry_run > 0 THEN 1 ELSE 0 END) AS runs_with_retry,
       COALESCE(MAX(cr.net_dbus), 0)     AS net_dbus,
       COALESCE(MAX(cr.est_usd_list), 0) AS est_usd_list,
       COALESCE(MAX(cr.price_basis), 'priced') AS price_basis,
       -- status: worst-first band on total retries in the window (field heuristic; 5 / 20).
       CASE
         WHEN SUM(pr.repairs + pr.task_retries + pr.is_retry_run) >= 20 THEN 'CRITICAL'
         WHEN SUM(pr.repairs + pr.task_retries + pr.is_retry_run) >= 5 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM runs pr
LEFT JOIN cost_rollup cr
  ON pr.workspace_id = cr.workspace_id AND pr.job_id = cr.job_id
GROUP BY pr.workspace_id, pr.job_id
ORDER BY total_retries DESC
) q
