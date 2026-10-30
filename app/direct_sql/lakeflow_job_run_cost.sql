-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_job_run_cost.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/jobs_pipelines/lakeflow_job_run_cost.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH run_usage AS (
  -- Net DBUs and effective-list dollar estimate per job run (DEC-66.1: pricing.effective_list.default,
  -- the exact price join T-69A settled on). Only usage rows carrying BOTH job_id AND job_run_id.
  SELECT u.workspace_id,
         u.usage_metadata.job_id     AS job_id,
         u.usage_metadata.job_run_id AS job_run_id,
         SUM(u.usage_quantity)                                          AS net_run_dbus,
         SUM(CASE WHEN u.product_features.is_serverless = TRUE
                  THEN u.usage_quantity ELSE 0 END)                     AS serverless_dbus,
         SUM(CASE WHEN u.product_features.is_serverless = TRUE
                  THEN 0 ELSE u.usage_quantity END)                     AS classic_dbus,
         MAX(lp.currency_code)                                          AS currency_code,
         SUM(u.usage_quantity * lp.list_rate)                           AS net_list_cost,
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
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND u.usage_date < __AS_OF_DATE__
    AND upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.job_id IS NOT NULL
    AND u.usage_metadata.job_run_id IS NOT NULL
  GROUP BY u.workspace_id, u.usage_metadata.job_id, u.usage_metadata.job_run_id
),
run_rows AS (
  -- Every observed timeline row (including in-progress, NULL-result_state slices) for a run that
  -- already has billed usage in run_usage above - no window lower bound on the timeline side (see
  -- caveats): a run started well before __WINDOW_DAYS__ still keeps its TRUE run_start, and a run
  -- sliced hourly past about an hour keeps every one of its observed slices.
  SELECT t.workspace_id, t.job_id, t.run_id, t.period_start_time, t.period_end_time,
         t.result_state, t.termination_code
  FROM `system`.`lakeflow`.`job_run_timeline` t
  JOIN (SELECT DISTINCT workspace_id, job_id, job_run_id FROM run_usage) k
    ON  t.workspace_id = k.workspace_id
    AND t.job_id       = k.job_id
    AND t.run_id       = k.job_run_id
),
run_span AS (
  -- The run's TRUE first observed slice (never just its first in-window one); last_slice_start is
  -- used only to detect an unclosed final attempt below.
  SELECT workspace_id, job_id, run_id,
         MIN(period_start_time) AS run_start,
         MAX(period_start_time) AS last_slice_start
  FROM run_rows
  GROUP BY workspace_id, job_id, run_id
),
run_last_end AS (
  -- The run's LAST attempt's own end row only (highest period_end_time among rows that actually
  -- ended) - never MAX(result_state)/MAX(termination_code), which compares attempts' text
  -- alphabetically and can pair one attempt's code with a different attempt's state.
  SELECT workspace_id, job_id, run_id, result_state, termination_code,
         period_end_time AS last_end
  FROM run_rows
  WHERE result_state IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id, run_id ORDER BY period_end_time DESC
  ) = 1
),
run_state AS (
  -- One row per run that has at least one job_run_timeline row (a run with none simply has no row
  -- here, so it LEFT JOINs to NULL below - genuinely unknown, never "in flight"). in_flight is TRUE
  -- when the run has no landed end row at all, OR its latest observed slice started at/after its
  -- last end row's own end time (a new attempt already running after an earlier attempt's outcome
  -- landed - a repair in progress); result_state/termination_code are then NULL too, since that
  -- earlier attempt's outcome is not this run's current state.
  SELECT s.workspace_id, s.job_id, s.run_id, s.run_start,
         (e.run_id IS NULL OR s.last_slice_start >= e.last_end) AS in_flight,
         CASE WHEN e.run_id IS NULL OR s.last_slice_start >= e.last_end THEN NULL
              ELSE e.result_state END AS result_state,
         CASE WHEN e.run_id IS NULL OR s.last_slice_start >= e.last_end THEN NULL
              ELSE e.termination_code END AS termination_code
  FROM run_span s
  LEFT JOIN run_last_end e
    ON  e.workspace_id = s.workspace_id
    AND e.job_id       = s.job_id
    AND e.run_id       = s.run_id
),
latest_jobs AS (
  -- system.lakeflow.jobs is SCD2: one row per change, so take the newest per job.
  SELECT workspace_id, job_id, name AS job_name
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
)
SELECT d.workspace_id,
       d.job_id,
       j.job_name,                                                -- NULL for submit/workflow runs
       d.job_run_id,
       m.run_start,
       m.in_flight,
       m.result_state,
       m.termination_code,
       d.net_run_dbus,
       d.currency_code,
       d.net_list_cost,
       COALESCE(d.price_basis, 'priced') AS price_basis,
       CASE
         WHEN d.classic_dbus = 0 AND d.serverless_dbus > 0 THEN 'serverless'
         WHEN d.serverless_dbus = 0 AND d.classic_dbus > 0 THEN 'classic'
         WHEN d.serverless_dbus > 0 AND d.classic_dbus > 0 THEN 'mixed'
         ELSE NULL
       END AS compute_type
FROM run_usage d
LEFT JOIN run_state m
  ON  m.workspace_id = d.workspace_id
  AND m.job_id       = d.job_id
  AND m.run_id       = d.job_run_id
LEFT JOIN latest_jobs j
  ON  j.workspace_id = d.workspace_id
  AND j.job_id       = d.job_id
ORDER BY d.net_list_cost DESC NULLS LAST, d.net_run_dbus DESC
LIMIT 100000
) q
