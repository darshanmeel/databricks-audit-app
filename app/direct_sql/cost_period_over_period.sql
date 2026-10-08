-- generated from dbt/models/databricks_direct/cost/d_cost_period_over_period.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_period_over_period.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH snapshot AS (
  -- the WHOLE account's own earliest recorded usage_date, over ALL history the snapshot carries
  -- (unrestricted by the window below, and never filtered to this row's own workspace/product) -
  -- the DEC-64 coverage signal shared by every row (see caveats).
  SELECT MIN(usage_date) AS snapshot_start
  FROM `system`.`billing`.`usage`
),
priced AS (
  SELECT u.workspace_id, u.billing_origin_product, u.usage_date, u.sku_name,
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
  WHERE u.usage_date >= dateadd(day, -(__WINDOW_DAYS__ * 2), __AS_OF_DATE__)
    AND u.usage_date <  __AS_OF_DATE__
),
raw_agg AS (
  -- Deliberately NO "ELSE 0" on current_cost/previous_cost: a row outside the side being summed
  -- must contribute NOTHING (NULL, skipped by SUM), not a literal 0 -- an "ELSE 0" there means
  -- ANY row on the OTHER side alone is enough to turn a fully-unpriced side's SUM from NULL into
  -- a false concrete 0, silently hiding the "this side could not be priced at all" case the
  -- NOT_ASSESSED branches below exist to catch. The four quantity sums keep their own ELSE 0 --
  -- they are per-side row counts, never a price total, so a side with zero matching rows
  -- legitimately IS a concrete 0 there.
  SELECT workspace_id, billing_origin_product,
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
                  THEN usage_quantity ELSE 0 END)                            AS previous_priced_quantity
  FROM priced
  GROUP BY workspace_id, billing_origin_product
),
agg AS (
  -- eff_current_cost/eff_previous_cost: a side whose SUM is NULL only because it had NO matching
  -- rows at all (never a pricing gap -- the per-side unpriced quantity is 0 too) resolves to a
  -- real 0, exactly the "the missing side sums to a genuine $0, never NOT_ASSESSED" guarantee the
  -- caveats below describe; a side whose SUM is NULL because every one of its rows was genuinely
  -- unpriced stays NULL here and is caught by the NOT_ASSESSED branches below instead.
  SELECT *,
         CASE WHEN current_cost IS NULL AND current_unpriced_quantity = 0 THEN 0 ELSE current_cost END AS eff_current_cost,
         CASE WHEN previous_cost IS NULL AND previous_unpriced_quantity = 0 THEN 0 ELSE previous_cost END AS eff_previous_cost
  FROM raw_agg
)
SELECT a.workspace_id,
       a.billing_origin_product,
       ROUND(a.eff_current_cost, 2)                        AS est_current_usd_list,
       ROUND(a.eff_previous_cost, 2)                        AS est_previous_usd_list,
       ROUND(a.eff_current_cost - a.eff_previous_cost, 2)   AS est_change_usd_list,
       ROUND((a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100, 1) AS change_pct,
       -- price_basis: a real $0 (free-usage SKUs) vs a pricing-coverage gap (understates cost),
       -- computed over BOTH windows combined (caveats).
       CASE
         WHEN (a.current_unpriced_quantity + a.previous_unpriced_quantity) > 0 THEN 'unpriced'
         WHEN (a.current_priced_quantity + a.previous_priced_quantity) = 0     THEN 'free'
         ELSE 'priced'
       END AS price_basis,
       CASE
         WHEN s.snapshot_start > dateadd(day, -(__WINDOW_DAYS__ * 2), __AS_OF_DATE__)
           THEN 'NOT_ASSESSED'
         WHEN a.current_unpriced_quantity > 0
           THEN 'NOT_ASSESSED'
         WHEN a.previous_unpriced_quantity > 0
           THEN 'NOT_ASSESSED'
         WHEN COALESCE(a.eff_current_cost, 0) < 50 THEN 'OK'
         WHEN a.eff_previous_cost = 0         THEN 'CRITICAL'
         WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= 50
           THEN 'CRITICAL'
         WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= 25
           THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN s.snapshot_start > dateadd(day, -(__WINDOW_DAYS__ * 2), __AS_OF_DATE__)
           THEN 'previous_window_not_covered'
         WHEN a.current_unpriced_quantity > 0
           THEN 'current_period_unpriced'
         WHEN a.previous_unpriced_quantity > 0
           THEN 'previous_period_unpriced'
         ELSE NULL
       END AS not_assessed_reason
FROM agg a
CROSS JOIN snapshot s
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         est_change_usd_list DESC,
         workspace_id, billing_origin_product
) q
