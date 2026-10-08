-- generated from dbt/models/databricks_direct/cost/d_cost_chargeback_by_warehouse.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_chargeback_by_warehouse.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH snapshot AS (
  SELECT MIN(usage_date) AS snapshot_start
  FROM `system`.`billing`.`usage`
),
priced AS (
  SELECT u.workspace_id, u.usage_metadata.warehouse_id AS warehouse_id, u.usage_date,
         u.record_type, u.usage_start_time, u.sku_name,
         u.usage_quantity                AS usage_quantity,
         u.usage_quantity * lp.list_rate AS list_cost,
         lp.list_rate                    AS list_rate
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
    AND u.usage_metadata.warehouse_id IS NOT NULL
    AND u.usage_date >= dateadd(day, -(__WINDOW_DAYS__ * 2), __AS_OF_DATE__)
    AND u.usage_date <  __AS_OF_DATE__
),
raw_agg AS (
  SELECT workspace_id, warehouse_id,
         SUM(CASE WHEN usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
                  THEN list_cost END)                                        AS current_cost,
         SUM(CASE WHEN usage_date <  dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
                  THEN list_cost END)                                        AS previous_cost,
         SUM(CASE WHEN usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                            AS current_unpriced_quantity,
         SUM(CASE WHEN usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
                   AND list_rate IS NOT NULL
                  THEN usage_quantity ELSE 0 END)                            AS current_priced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                            AS previous_unpriced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
                   AND list_rate IS NOT NULL
                  THEN usage_quantity ELSE 0 END)                            AS previous_priced_quantity,
         COUNT(DISTINCT CASE WHEN record_type = 'ORIGINAL'
                               AND usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
                             THEN usage_start_time END)                      AS billed_hours
  FROM priced
  GROUP BY workspace_id, warehouse_id
),
agg AS (
  SELECT *,
         CASE WHEN current_cost IS NULL AND current_unpriced_quantity = 0 THEN 0 ELSE current_cost END AS eff_current_cost,
         CASE WHEN previous_cost IS NULL AND previous_unpriced_quantity = 0 THEN 0 ELSE previous_cost END AS eff_previous_cost
  FROM raw_agg
),
queries AS (
  SELECT workspace_id, compute.warehouse_id AS warehouse_id, COUNT(*) AS queries
  FROM `system`.`query`.`history`
  WHERE compute.warehouse_id IS NOT NULL
    AND start_time >= CAST(dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__) AS TIMESTAMP)
    AND start_time <  __AS_OF_DATE__
  GROUP BY workspace_id, compute.warehouse_id
),
total AS (
  SELECT SUM(eff_current_cost) AS total_usd_list FROM agg
),
scored AS (
  SELECT a.workspace_id, a.warehouse_id,
         a.billed_hours,
         COALESCE(q.queries, 0)                                            AS queries,
         a.current_unpriced_quantity, a.previous_unpriced_quantity,
         a.current_priced_quantity, a.previous_priced_quantity,
         a.eff_current_cost, a.eff_previous_cost,
         ROUND(a.eff_current_cost, 2)                                      AS usd_list,
         ROUND(a.eff_current_cost * 100.0 / NULLIF(t.total_usd_list, 0), 1) AS share_of_total_pct,
         ROUND(a.eff_previous_cost, 2)                                     AS prev_usd_list,
         ROUND(a.eff_current_cost - a.eff_previous_cost, 2)                AS change_usd_list,
         ROUND((a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100, 1) AS change_pct,
         ROUND(a.eff_current_cost / NULLIF(q.queries, 0) * 1000, 2)        AS usd_per_1000_queries,
         CASE
           WHEN (a.current_unpriced_quantity + a.previous_unpriced_quantity) > 0 THEN 'unpriced'
           WHEN (a.current_priced_quantity + a.previous_priced_quantity) = 0     THEN 'free'
           ELSE 'priced'
         END AS price_basis,
         CASE
           WHEN s.snapshot_start > dateadd(day, -(__WINDOW_DAYS__ * 2), __AS_OF_DATE__) THEN 'NOT_ASSESSED'
           WHEN a.current_cost IS NULL AND a.current_unpriced_quantity > 0           THEN 'NOT_ASSESSED'
           WHEN a.previous_cost IS NULL AND a.previous_unpriced_quantity > 0         THEN 'NOT_ASSESSED'
           WHEN COALESCE(a.eff_current_cost, 0) < 20                    THEN 'OK'
           WHEN a.eff_previous_cost = 0                                             THEN 'CRITICAL'
           WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= 50
             THEN 'CRITICAL'
           WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= 25
             THEN 'WARN'
           ELSE 'OK'
         END AS status,
         CASE
           WHEN s.snapshot_start > dateadd(day, -(__WINDOW_DAYS__ * 2), __AS_OF_DATE__) THEN 'previous_window_not_covered'
           WHEN a.current_cost IS NULL AND a.current_unpriced_quantity > 0           THEN 'current_period_unpriced'
           WHEN a.previous_cost IS NULL AND a.previous_unpriced_quantity > 0         THEN 'previous_period_unpriced'
           ELSE NULL
         END AS not_assessed_reason
  FROM agg a
  CROSS JOIN snapshot s
  CROSS JOIN total t
  LEFT JOIN queries q ON q.workspace_id = a.workspace_id AND q.warehouse_id = a.warehouse_id
),
flagged AS (
  -- A real finding (WARN/CRITICAL) always keeps its own row - never pooled.
  SELECT workspace_id, warehouse_id, FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
         billed_hours, queries, usd_per_1000_queries, usd_list, share_of_total_pct, prev_usd_list,
         change_usd_list, change_pct, price_basis, status, not_assessed_reason
  FROM scored
  WHERE status IN ('WARN', 'CRITICAL')
),
poolable_ranked AS (
  -- OK and NOT_ASSESSED warehouses are both poolable - on an account short of 2x __WINDOW_DAYS__ of
  -- billing history, every warehouse reads NOT_ASSESSED, and that list needs the same cap an OK
  -- list gets.
  -- ranked PER STATUS, never combined - a NOT_ASSESSED row's usd_list is always NULL (sorted
  -- last), so ranking both statuses together would push every NOT_ASSESSED row past 20 as
  -- soon as 20+ OK rows existed anywhere, regardless of how few NOT_ASSESSED rows there are.
  SELECT scored.*,
         ROW_NUMBER() OVER (PARTITION BY status ORDER BY usd_list DESC NULLS LAST, warehouse_id) AS rn
  FROM scored
  WHERE status IN ('OK', 'NOT_ASSESSED')
),
poolable_kept AS (
  -- The top 20 OK/NOT_ASSESSED warehouses by current spend, kept as their own row.
  SELECT workspace_id, warehouse_id, FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
         billed_hours, queries, usd_per_1000_queries, usd_list, share_of_total_pct, prev_usd_list,
         change_usd_list, change_pct, price_basis, status, not_assessed_reason
  FROM poolable_ranked
  WHERE rn <= 20
),
poolable_pooled_raw AS (
  -- Every remaining OK/NOT_ASSESSED warehouse beyond 20 - re-aggregated below into one
  -- is_other row per status (OK rows and NOT_ASSESSED rows are never combined into one row).
  SELECT * FROM poolable_ranked WHERE rn > 20
),
other_ok_row AS (
  SELECT
    CAST(NULL AS STRING)                                                  AS workspace_id,
    CAST(NULL AS STRING)                                                  AS warehouse_id,
    TRUE                                                                  AS is_other,
    COUNT(*)                                                              AS pooled_count,
    SUM(billed_hours)                                                     AS billed_hours,
    SUM(queries)                                                          AS queries,
    ROUND(SUM(eff_current_cost) / NULLIF(SUM(queries), 0) * 1000, 2)      AS usd_per_1000_queries,
    ROUND(SUM(eff_current_cost), 2)                                       AS usd_list,
    ROUND(SUM(eff_current_cost) * 100.0 / NULLIF(MAX(t.total_usd_list), 0), 1) AS share_of_total_pct,
    ROUND(SUM(eff_previous_cost), 2)                                      AS prev_usd_list,
    ROUND(SUM(eff_current_cost) - SUM(eff_previous_cost), 2)              AS change_usd_list,
    ROUND((SUM(eff_current_cost) - SUM(eff_previous_cost)) / NULLIF(SUM(eff_previous_cost), 0) * 100, 1) AS change_pct,
    CASE
      WHEN (SUM(current_unpriced_quantity) + SUM(previous_unpriced_quantity)) > 0 THEN 'unpriced'
      WHEN (SUM(current_priced_quantity) + SUM(previous_priced_quantity)) = 0     THEN 'free'
      ELSE 'priced'
    END AS price_basis,
    -- Every pooled row was already status=OK on its own - a rollup of small, healthy rows is never
    -- itself a finding, so this row always reads OK regardless of the combined dollars.
    'OK' AS status,
    CAST(NULL AS STRING) AS not_assessed_reason
  FROM poolable_pooled_raw
  CROSS JOIN total t
  WHERE status = 'OK'
),
other_not_assessed_row AS (
  SELECT
    CAST(NULL AS STRING)                                                  AS workspace_id,
    CAST(NULL AS STRING)                                                  AS warehouse_id,
    TRUE                                                                  AS is_other,
    COUNT(*)                                                              AS pooled_count,
    SUM(billed_hours)                                                     AS billed_hours,
    SUM(queries)                                                          AS queries,
    ROUND(SUM(eff_current_cost) / NULLIF(SUM(queries), 0) * 1000, 2)      AS usd_per_1000_queries,
    ROUND(SUM(eff_current_cost), 2)                                       AS usd_list,
    ROUND(SUM(eff_current_cost) * 100.0 / NULLIF(MAX(t.total_usd_list), 0), 1) AS share_of_total_pct,
    ROUND(SUM(eff_previous_cost), 2)                                      AS prev_usd_list,
    ROUND(SUM(eff_current_cost) - SUM(eff_previous_cost), 2)              AS change_usd_list,
    ROUND((SUM(eff_current_cost) - SUM(eff_previous_cost)) / NULLIF(SUM(eff_previous_cost), 0) * 100, 1) AS change_pct,
    CASE
      WHEN (SUM(current_unpriced_quantity) + SUM(previous_unpriced_quantity)) > 0 THEN 'unpriced'
      WHEN (SUM(current_priced_quantity) + SUM(previous_priced_quantity)) = 0     THEN 'free'
      ELSE 'priced'
    END AS price_basis,
    'NOT_ASSESSED' AS status,
    -- The pooled rows can carry different reasons; NULL here rather than pick one arbitrarily -
    -- raise 20 to see them individually.
    CAST(NULL AS STRING) AS not_assessed_reason
  FROM poolable_pooled_raw
  CROSS JOIN total t
  WHERE status = 'NOT_ASSESSED'
)
SELECT * FROM (
  SELECT * FROM flagged
  UNION ALL
  SELECT * FROM poolable_kept
  UNION ALL
  SELECT * FROM other_ok_row WHERE pooled_count > 0
  UNION ALL
  SELECT * FROM other_not_assessed_row WHERE pooled_count > 0
) u
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         is_other,
         change_usd_list DESC NULLS LAST,
         workspace_id, warehouse_id
) q
