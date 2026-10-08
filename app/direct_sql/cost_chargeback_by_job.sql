-- generated from dbt/models/databricks_direct/cost/d_cost_chargeback_by_job.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_chargeback_by_job.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH snapshot AS (
  SELECT MIN(usage_date) AS snapshot_start FROM `system`.`billing`.`usage`
),
priced AS (
  SELECT u.workspace_id, u.usage_metadata.job_id AS job_id, u.usage_date, u.sku_name,
         u.usage_quantity                    AS usage_quantity,
         u.usage_quantity * lp.list_rate     AS list_cost,
         lp.list_rate                        AS list_rate
  FROM `system`.`billing`.`usage` u
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
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
    ON u.sku_name   = lp.sku_name
   AND u.cloud      = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.job_id IS NOT NULL
    AND u.usage_date >= dateadd(day, -(__WINDOW_DAYS__ * 2), __AS_OF_DATE__)
    AND u.usage_date <  __AS_OF_DATE__
),
raw_agg AS (
  -- Deliberately no ELSE 0 on current_cost/previous_cost: a job outside the side being summed
  -- contributes nothing, so a fully-unpriced side keeps NULL rather than a false 0.
  SELECT workspace_id, job_id,
         SUM(CASE WHEN usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
                  THEN list_cost END)                                         AS current_cost,
         SUM(CASE WHEN usage_date <  dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
                  THEN list_cost END)                                         AS previous_cost,
         SUM(CASE WHEN usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                             AS current_unpriced_quantity,
         SUM(CASE WHEN usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
                   AND list_rate IS NOT NULL
                  THEN usage_quantity ELSE 0 END)                             AS current_priced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                             AS previous_unpriced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
                   AND list_rate IS NOT NULL
                  THEN usage_quantity ELSE 0 END)                             AS previous_priced_quantity
  FROM priced
  GROUP BY workspace_id, job_id
),
agg AS (
  -- eff_current_cost/eff_previous_cost: a side whose SUM is NULL only because it had NO matching
  -- rows at all (its own unpriced quantity is 0 too) resolves to a real 0; a side whose SUM is
  -- NULL because every one of its rows was genuinely unpriced stays NULL, caught below instead.
  SELECT *,
         CASE WHEN current_cost IS NULL AND current_unpriced_quantity = 0 THEN 0 ELSE current_cost END AS eff_current_cost,
         CASE WHEN previous_cost IS NULL AND previous_unpriced_quantity = 0 THEN 0 ELSE previous_cost END AS eff_previous_cost
  FROM raw_agg
),
latest_jobs AS (
  -- joined below with IS NOT DISTINCT FROM on workspace_id (a real account never has a NULL
  -- workspace_id here; this only matters for an account-level billing row, where a plain "="
  -- join to workspace_id would never match a NULL to a NULL).
  SELECT workspace_id, job_id, name AS job_name, run_as, creator_user_name
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),
total AS (
  SELECT SUM(eff_current_cost) AS total_current_usd_list FROM agg
),
scored AS (
  SELECT a.workspace_id, a.job_id, j.job_name, j.run_as, j.creator_user_name,
         a.current_unpriced_quantity, a.previous_unpriced_quantity,
         a.current_priced_quantity, a.previous_priced_quantity,
         a.eff_current_cost, a.eff_previous_cost,
         ROUND(a.eff_current_cost, 2)                                                 AS est_current_usd_list,
         ROUND(a.eff_current_cost * 100.0 / NULLIF(t.total_current_usd_list, 0), 1)   AS share_of_total_pct,
         ROUND(a.eff_previous_cost, 2)                                                AS est_previous_usd_list,
         ROUND(a.eff_current_cost - a.eff_previous_cost, 2)                           AS est_change_usd_list,
         ROUND((a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100, 1) AS change_pct,
         CASE
           WHEN (a.current_unpriced_quantity + a.previous_unpriced_quantity) > 0 THEN 'unpriced'
           WHEN (a.current_priced_quantity + a.previous_priced_quantity) = 0     THEN 'free'
           ELSE 'priced'
         END                                                                           AS price_basis,
         CASE
           WHEN s.snapshot_start > dateadd(day, -(__WINDOW_DAYS__ * 2), __AS_OF_DATE__)
             THEN 'NOT_ASSESSED'
           WHEN a.current_cost IS NULL AND a.current_unpriced_quantity > 0
             THEN 'NOT_ASSESSED'
           WHEN a.previous_cost IS NULL AND a.previous_unpriced_quantity > 0
             THEN 'NOT_ASSESSED'
           WHEN COALESCE(a.eff_current_cost, 0) < 20 THEN 'OK'
           WHEN a.eff_previous_cost = 0 THEN 'CRITICAL'
           WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= 50
             THEN 'CRITICAL'
           WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= 25
             THEN 'WARN'
           ELSE 'OK'
         END                                                                           AS status,
         CASE
           WHEN s.snapshot_start > dateadd(day, -(__WINDOW_DAYS__ * 2), __AS_OF_DATE__)
             THEN 'previous_window_not_covered'
           WHEN a.current_cost IS NULL AND a.current_unpriced_quantity > 0
             THEN 'current_period_unpriced'
           WHEN a.previous_cost IS NULL AND a.previous_unpriced_quantity > 0
             THEN 'previous_period_unpriced'
           ELSE NULL
         END                                                                           AS not_assessed_reason
  FROM agg a
  LEFT JOIN latest_jobs j ON j.workspace_id IS NOT DISTINCT FROM a.workspace_id AND j.job_id = a.job_id
  CROSS JOIN snapshot s
  CROSS JOIN total t
),
flagged AS (
  -- Every non-OK job (WARN/CRITICAL/NOT_ASSESSED) always keeps its own row - never pooled.
  SELECT workspace_id, job_id, job_name, run_as, creator_user_name,
         FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
         est_current_usd_list, share_of_total_pct, est_previous_usd_list, est_change_usd_list,
         change_pct, price_basis, status, not_assessed_reason
  FROM scored
  WHERE status != 'OK'
),
ok_ranked AS (
  SELECT scored.*,
         ROW_NUMBER() OVER (ORDER BY est_current_usd_list DESC NULLS LAST, job_id) AS rn
  FROM scored
  WHERE status = 'OK'
),
ok_kept AS (
  -- The top 20 OK jobs by current spend, kept as their own row.
  SELECT workspace_id, job_id, job_name, run_as, creator_user_name,
         FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
         est_current_usd_list, share_of_total_pct, est_previous_usd_list, est_change_usd_list,
         change_pct, price_basis, status, not_assessed_reason
  FROM ok_ranked
  WHERE rn <= 20
),
ok_pooled_raw AS (
  -- Every remaining OK job beyond 20 - re-aggregated into one is_other row below.
  SELECT * FROM ok_ranked WHERE rn > 20
),
other_row AS (
  SELECT
    CAST(NULL AS STRING) AS workspace_id,
    CAST(NULL AS STRING) AS job_id,
    CAST(NULL AS STRING) AS job_name,
    CAST(NULL AS STRING) AS run_as,
    CAST(NULL AS STRING) AS creator_user_name,
    TRUE                  AS is_other,
    COUNT(*)              AS pooled_count,
    ROUND(SUM(eff_current_cost), 2)                                              AS est_current_usd_list,
    ROUND(SUM(eff_current_cost) * 100.0 / NULLIF(MAX(t.total_current_usd_list), 0), 1) AS share_of_total_pct,
    ROUND(SUM(eff_previous_cost), 2)                                             AS est_previous_usd_list,
    ROUND(SUM(eff_current_cost) - SUM(eff_previous_cost), 2)                     AS est_change_usd_list,
    ROUND((SUM(eff_current_cost) - SUM(eff_previous_cost)) / NULLIF(SUM(eff_previous_cost), 0) * 100, 1) AS change_pct,
    CASE
      WHEN (SUM(current_unpriced_quantity) + SUM(previous_unpriced_quantity)) > 0 THEN 'unpriced'
      WHEN (SUM(current_priced_quantity) + SUM(previous_priced_quantity)) = 0     THEN 'free'
      ELSE 'priced'
    END AS price_basis,
    -- Every pooled row was already status=OK on its own - a rollup of small, healthy jobs is never
    -- itself a finding, so this row always reads OK regardless of the combined dollars.
    'OK' AS status,
    CAST(NULL AS STRING) AS not_assessed_reason
  FROM ok_pooled_raw
  CROSS JOIN total t
)
SELECT * FROM (
  SELECT * FROM flagged
  UNION ALL
  SELECT * FROM ok_kept
  UNION ALL
  SELECT * FROM other_row WHERE pooled_count > 0
) u
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         is_other,
         est_change_usd_list DESC NULLS LAST,
         workspace_id, job_id
) q
