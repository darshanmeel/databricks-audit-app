-- query_id: cost_daily_spikes
-- title: Days a workspace/product's spend jumped well above its own recent trailing baseline
-- domain: cost   tier: standard
-- reads: system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.billing; GA (system.billing.usage and system.billing.list_prices
--   are generally available)
-- empty_if: ingestion_lag, no_activity
-- params: :period_days (default 30) rolling window in days - the days scanned for a spike;
--   :baseline_days (default 14) trailing days, immediately before each scanned day, whose median
--   spend is that day's baseline; :spike_ratio (default 2.0) a day flags once its own spend is at
--   least this many times its trailing baseline median; :min_spend_usd (default 50) a day's own
--   spend must be at least this before it can flag, so a jump between two tiny dollar figures
--   never reads as a spike; :min_baseline_days (default 3) fewer trailing days than this makes the
--   median too thin to trust, so the day reads NOT_ASSESSED instead of a ratio built on one or two
--   days
-- confidence: needs_confirmation
-- confidence_note: New app-owned query, not yet run live. Uses the same effective-list-price join
--   (DEC-66.1) every priced cost query here already uses; confirm on your account that a day you
--   know had an unusual spend jump (a backfill, a burst of job runs, a new SKU turned on) reads
--   WARN here with a spike_ratio_actual and top_skus that match what you already know happened.
-- read_this: One row = one day this workspace/product's own spend cleared :spike_ratio times its
--   own trailing :baseline_days median, at or above the :min_spend_usd floor. est_day_usd_list is
--   that day's total; est_baseline_usd_list is the trailing median it is judged against;
--   spike_ratio_actual is est_day_usd_list over that median; top_skus names the SKUs that made up
--   most of the day (the drivers to look at first).
-- healthy: status = OK is never itself returned as a row (see caveats: this query emits ONLY
--   WARN or NOT_ASSESSED days) - a day whose own spend never clears :min_spend_usd, or that
--   clears the floor but not :spike_ratio, or that clears both but has no baseline, simply has no
--   row when the floor alone is what stops it, so "no row for a workspace/product in the window"
--   is (mostly) the healthy case - see NOT_ASSESSED below for the one exception.
-- investigate_if: WARN - every row returned above the floor with a real trailing baseline of at
--   least :min_baseline_days days: a real jump on a real amount of money. NOT_ASSESSED - read
--   not_assessed_reason: 'no_baseline_history' when this workspace/product cleared :min_spend_usd
--   on this day but has no earlier spend at all in the trailing :baseline_days before it (a
--   brand-new workspace or a SKU that only just started billing); 'short_baseline' when it has
--   some earlier spend but fewer than :min_baseline_days days of it, too thin a median to trust -
--   the $ floor is checked first, so a NOT_ASSESSED row always represents a day with real money
--   behind it, never a $1 non-event.
-- actions: 1) open cost_dollarized_by_sku_day for this workspace and day and read top_skus
--   against it - a backfill or restatement usually shows as one SKU/day, a genuine spike as many
--   (free); 2) check cost_period_over_period for the same workspace/product - one spike day inside
--   an otherwise flat period reads differently from a period that is trending up throughout
--   (free); 3) if the spike is real new usage (a job that started running more often, a new
--   endpoint, a bigger backfill) rather than a one-off correction, the matching domain tab names
--   the resource and whether it is worth scaling down (config) or is deliberate growth (spend).
-- not_assessed_reasons: no_baseline_history: not enough earlier days for this workspace and
--   product to set a normal baseline yet; short_baseline: some earlier days exist but fewer than
--   :min_baseline_days, too thin a median to trust
-- next: cost_dollarized_by_sku_day (for the SKU-level detail behind one spike day),
--   cost_period_over_period (for whether the whole period is trending, not just one day),
--   cost_monthly_actuals (for the calendar-month total the spike day landed in)
-- caveats: DEC-66.1 - the effective list price only, never a negotiated rate. DEC-62 - every
--   number here is a measured day against a measured trailing median, never a projection.
--   BASELINE - the median is taken over whatever days in the trailing :baseline_days window
--   actually have a usage row for this workspace/product; a day with genuinely zero usage has no
--   row there and is silently skipped rather than counted as a $0 baseline day, so an
--   intermittent workload's baseline sits a little higher than a true zero-inclusive median
--   would. The $ floor (:min_spend_usd) is checked BEFORE the baseline-size cases, so a thin day
--   that could never clear the floor never appears as a NOT_ASSESSED row either - NOT_ASSESSED
--   (no_baseline_history) fires when the day clears the floor but has NO usage rows at all for
--   this workspace/product anywhere in the trailing window; NOT_ASSESSED (short_baseline) fires
--   when it has 1 or more but fewer than :min_baseline_days days of earlier spend - a median over
--   that few days is too easily swung by one unusual day to band a verdict on. Account-level usage (workspace_id NULL) is judged the same way as any
--   workspace's own usage - every join here matches workspace_id/billing_origin_product
--   null-safely, so an account-level day finds its own account-level baseline and drivers instead
--   of always reading no_baseline_history. DRIVERS - top_skus is the top 3 SKUs that day by
--   list-priced dollars, each shown with its own rounded dollar figure, or "(no list price)" /
--   "(free)" when that SKU could not be priced at all; the list is deduplicated and its internal
--   order is not guaranteed to be sorted by dollars - read the per-SKU figures printed in it, not
--   its left-to-right order. price_basis is computed over the flagged day only - 'unpriced' when
--   any non-free-usage SKU that day had no matching list_prices row (est_day_usd_list then
--   understates that day), 'free' when every matched SKU is a FREE_USAGE SKU (a real $0),
--   'priced' otherwise. Corrections are netted (SUM across all record_types - never filter to
--   ORIGINAL only). The current, incomplete day is excluded from both the scanned days and the
--   baseline, since billing.usage lands with ingestion lag and a trailing partial day would
--   otherwise never be able to clear the floor honestly. This query returns ONLY the days that
--   flag or that cannot be assessed - a workspace/product with no spike in the window (including
--   a day whose own spend never clears :min_spend_usd) has no row at all, by design (DEC-57):
--   pair it with cost_period_over_period or cost_monthly_actuals for the full, unfiltered
--   picture.
WITH daily_sku AS (
  SELECT u.workspace_id, u.billing_origin_product, u.usage_date, u.sku_name,
         SUM(u.usage_quantity * lp.list_rate) AS sku_cost,
         SUM(u.usage_quantity)                AS sku_quantity,
         MAX(lp.list_rate)                    AS list_rate
  FROM system.billing.usage u
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
    FROM system.billing.list_prices
  ) lp
    ON u.sku_name   = lp.sku_name
   AND u.cloud      = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_date >= dateadd(day, -(:period_days + :baseline_days), current_date())
    AND u.usage_date <  current_date()
  GROUP BY u.workspace_id, u.billing_origin_product, u.usage_date, u.sku_name
),
daily AS (
  SELECT workspace_id, billing_origin_product, usage_date,
         SUM(sku_cost) AS day_cost,
         SUM(CASE WHEN list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN sku_quantity ELSE 0 END)                          AS unpriced_quantity,
         SUM(CASE WHEN list_rate IS NOT NULL THEN sku_quantity ELSE 0 END) AS priced_quantity
  FROM daily_sku
  GROUP BY workspace_id, billing_origin_product, usage_date
),
candidates AS (
  SELECT * FROM daily WHERE usage_date >= dateadd(day, -:period_days, current_date())
),
baseline_pairs AS (
  -- every earlier day, for the same workspace/product, that falls in this candidate day's own
  -- trailing :baseline_days window (never including the candidate day itself). Null-safe keys
  -- (IS NOT DISTINCT FROM) so account-level usage (workspace_id NULL, PLAN.md 5.7) finds its own
  -- account-level baseline instead of never matching (plain `=` never matches NULL = NULL).
  SELECT c.workspace_id, c.billing_origin_product, c.usage_date AS candidate_date,
         b.day_cost AS baseline_cost
  FROM candidates c
  JOIN daily b
    ON  b.workspace_id            IS NOT DISTINCT FROM c.workspace_id
    AND b.billing_origin_product  IS NOT DISTINCT FROM c.billing_origin_product
    AND b.usage_date >= dateadd(day, -:baseline_days, c.usage_date)
    AND b.usage_date <  c.usage_date
),
baseline AS (
  SELECT workspace_id, billing_origin_product, candidate_date,
         percentile(baseline_cost, 0.5) AS baseline_median_cost,
         COUNT(*)                       AS baseline_days_seen
  FROM baseline_pairs
  GROUP BY workspace_id, billing_origin_product, candidate_date
),
ranked_sku AS (
  SELECT workspace_id, billing_origin_product, usage_date, sku_name, sku_cost
  FROM daily_sku
  WHERE usage_date >= dateadd(day, -:period_days, current_date())
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, billing_origin_product, usage_date ORDER BY sku_cost DESC, sku_name
  ) <= 3
),
drivers AS (
  SELECT workspace_id, billing_origin_product, usage_date,
         array_join(collect_set(
           CASE
             WHEN sku_cost IS NULL THEN concat(sku_name,
               CASE WHEN upper(sku_name) LIKE '%FREE_USAGE%' THEN ' (free)' ELSE ' (no list price)' END)
             ELSE concat(sku_name, ' ($', CAST(CAST(ROUND(sku_cost, 0) AS BIGINT) AS STRING), ')')
           END
         ), ', ') AS top_skus
  FROM ranked_sku
  GROUP BY workspace_id, billing_origin_product, usage_date
)
SELECT c.workspace_id,
       c.billing_origin_product,
       c.usage_date,
       ROUND(c.day_cost, 2)              AS est_day_usd_list,
       ROUND(bl.baseline_median_cost, 2) AS est_baseline_usd_list,
       COALESCE(bl.baseline_days_seen, 0) AS baseline_days_seen,
       ROUND(c.day_cost / NULLIF(bl.baseline_median_cost, 0), 2) AS spike_ratio_actual,
       d.top_skus AS top_skus,
       -- price_basis: a real $0 (free-usage SKUs) vs a pricing-coverage gap (understates the day).
       CASE
         WHEN c.unpriced_quantity > 0 THEN 'unpriced'
         WHEN c.priced_quantity = 0   THEN 'free'
         ELSE 'priced'
       END AS price_basis,
       -- the $ floor is checked FIRST (a day that can never clear it is not worth flagging at
       -- all, priced or not), then the baseline-size cases, then the ratio itself - see caveats.
       CASE
         WHEN COALESCE(c.day_cost, 0) < :min_spend_usd                                  THEN 'OK'
         WHEN bl.baseline_days_seen IS NULL OR bl.baseline_days_seen < :min_baseline_days THEN 'NOT_ASSESSED'
         WHEN c.day_cost >= COALESCE(bl.baseline_median_cost, 0) * :spike_ratio          THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN COALESCE(c.day_cost, 0) < :min_spend_usd                    THEN NULL
         WHEN bl.baseline_days_seen IS NULL OR bl.baseline_days_seen = 0  THEN 'no_baseline_history'
         WHEN bl.baseline_days_seen < :min_baseline_days                 THEN 'short_baseline'
         ELSE NULL
       END AS not_assessed_reason
FROM candidates c
LEFT JOIN baseline bl
  ON  bl.workspace_id           IS NOT DISTINCT FROM c.workspace_id
  AND bl.billing_origin_product IS NOT DISTINCT FROM c.billing_origin_product
  AND bl.candidate_date         = c.usage_date
LEFT JOIN drivers d
  ON  d.workspace_id           IS NOT DISTINCT FROM c.workspace_id
  AND d.billing_origin_product IS NOT DISTINCT FROM c.billing_origin_product
  AND d.usage_date             = c.usage_date
WHERE c.day_cost >= :min_spend_usd
  AND (
        (bl.baseline_days_seen IS NULL OR bl.baseline_days_seen < :min_baseline_days)
     OR c.day_cost >= COALESCE(bl.baseline_median_cost, 0) * :spike_ratio
      )
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         est_day_usd_list DESC,
         workspace_id, billing_origin_product, usage_date
