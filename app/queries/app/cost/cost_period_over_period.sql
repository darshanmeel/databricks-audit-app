-- query_id: cost_period_over_period
-- title: Spend this period versus the equal-length period before it, by workspace and product line
-- domain: cost   tier: standard
-- reads: system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.billing; GA (system.billing.usage and system.billing.list_prices
--   are generally available)
-- empty_if: ingestion_lag, no_activity
-- params: :period_days (default 30) rolling window in days - the current period is the most
--   recent N days, the previous period is the equal-length N days immediately before it;
--   :warn_increase_pct (default 25) percent increase in list-priced spend, current period over
--   previous, at/above which WARN; :crit_increase_pct (default 50) percent increase at/above
--   which CRITICAL; :min_spend_usd (default 50) the current period's own list-priced spend must
--   be at least this before a percent change is judged - below it the row reads OK regardless of
--   the swing, since a percent figure on a tiny base is noise
-- confidence: needs_confirmation
-- confidence_note: New app-owned query, not yet run live. Builds on the effective-list-price join
--   every priced cost query in this app already uses (DEC-66.1) and on the "compare two measured
--   periods, never scale one" rule DEC-62 and lakeflow_job_duration_regression already
--   established for a within-window split; confirm on your account that a workspace/product pair
--   you know grew or shrank between two recent equal periods matches this query's change_pct.
-- read_this: One row = a workspace + billing_origin_product's spend in the selected window
--   against the equal-length window immediately before it, both priced at the effective list
--   price (DEC-66.1). est_current_usd_list and est_previous_usd_list are the two totals;
--   est_change_usd_list and change_pct compare them. price_basis (free/priced/unpriced) discloses
--   pricing coverage across both windows combined; not_assessed_reason explains a NOT_ASSESSED row.
-- healthy: status OK - no increase at/above :warn_increase_pct, or the row's own current-period
--   spend (a real $0 counts) sits below :min_spend_usd
-- investigate_if: CRITICAL - the increase is at/above :crit_increase_pct on at least
--   :min_spend_usd of current spend, or the row is brand-new spend above the floor with nothing
--   in the previous period to compare against (there is no honest percentage for a jump from
--   zero, so it reads CRITICAL outright); WARN - at/above :warn_increase_pct. NOT_ASSESSED - read
--   not_assessed_reason: 'previous_window_not_covered' when the whole account's own earliest
--   recorded usage (across every workspace and product, not just this row) does not reach back
--   far enough to cover the previous window (DEC-64), so no comparison anywhere can be trusted at
--   this window length; 'current_period_unpriced' when ANY of this row's current-period usage
--   could not be priced (at least one current-period SKU was unmatched in list_prices and was not
--   FREE_USAGE), so est_current_usd_list understates the true figure and no verdict is safe to
--   judge on it, even when the rest of the period priced fine; 'previous_period_unpriced' is the
--   same gap on the previous period's own side.
-- actions: 1) open cost_dollarized_by_sku_day for this workspace and window, sorted by day and
--   SKU, to see which SKU actually moved (free); 2) check cost_monthly_actuals for the same
--   workspace/product to see whether this is a genuine trend or one busy stretch inside an
--   otherwise flat month (free); 3) if the increase is real usage rather than a one-off backfill
--   or restatement, the matching domain tab (Jobs, Serving, Compute) names the resource driving
--   it, and right-sizing or scaling it down is the fix (config, or spend if the extra usage is
--   deliberate growth).
-- not_assessed_reasons: previous_window_not_covered: the snapshot does not reach back far enough
--   to cover the previous period, so there is nothing honest to compare against;
--   current_period_unpriced: any of this period's usage could not be priced, not just all of it;
--   previous_period_unpriced: any of the previous period's usage could not be priced
-- next: cost_dollarized_by_sku_day (for the SKU behind the move), cost_monthly_actuals (for the
--   calendar-month trend), overview_spend_estimate (for the account's current headline number)
-- caveats: DEC-66.1 - the effective list price only, never a negotiated rate; the same basis on
--   both windows, so the comparison is apples-to-apples even though neither total is a real bill.
--   DEC-62 - both windows are measured, never projected or scaled; a 7-day window is compared
--   against the 7 days before it, never annualized. DEC-64 - NOT_ASSESSED
--   (previous_window_not_covered) fires from the WHOLE ACCOUNT's own earliest recorded
--   usage_date across system.billing.usage (never a single workspace/product's own history, so a
--   genuinely brand-new product or workspace is judged the same as everything else, never
--   withheld just because it personally lacks older rows): when that account-wide date is more
--   recent than the previous window's start, the export itself cannot cover the comparison for
--   ANY row at this window length, and every row reads NOT_ASSESSED rather than a percent jump
--   built on a partial history. A workspace/product with usage in only one of the two windows
--   still gets a real row once the account-wide history clears the coverage check: the missing
--   side sums to a genuine $0, never NOT_ASSESSED - that is an actual appearance or disappearance
--   of spend, not a coverage gap: a side's own SUM is resolved to a real 0 whenever it has no
--   matching rows at all (that side's own unpriced-quantity is 0 too), never left as the NULL an
--   empty SQL SUM naturally produces. NOT_ASSESSED (current_period_unpriced /
--   previous_period_unpriced) fires separately, per row and per side, whenever that side has ANY
--   unpriced usage at all (see investigate_if) - even a side that is mostly priced, since a
--   partially-priced total is a coverage gap, not a trustworthy figure to judge or compare, so it
--   is withheld rather than shown as a misleading verdict on an understated number. price_basis is computed over BOTH windows combined - 'unpriced' when any
--   non-free-usage SKU in either window had no matching list_prices row (the totals then
--   understate cost), 'free' when every matched SKU is a FREE_USAGE SKU (a real $0), 'priced'
--   otherwise. Corrections are netted (SUM across all record_types - never filter to ORIGINAL
--   only). The current day is excluded from both windows, same as every other windowed cost
--   query, since billing.usage lands with ingestion lag and the trailing day is provisional.
WITH snapshot AS (
  -- the WHOLE account's own earliest recorded usage_date, over ALL history the snapshot carries
  -- (unrestricted by the window below, and never filtered to this row's own workspace/product) -
  -- the DEC-64 coverage signal shared by every row (see caveats).
  SELECT MIN(usage_date) AS snapshot_start
  FROM system.billing.usage
),
priced AS (
  SELECT u.workspace_id, u.billing_origin_product, u.usage_date, u.sku_name,
         u.usage_quantity                AS usage_quantity,
         u.usage_quantity * lp.list_rate AS list_cost,
         lp.list_rate                    AS list_rate
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
  WHERE u.usage_date >= dateadd(day, -(:period_days * 2), current_date())
    AND u.usage_date <  current_date()
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
         SUM(CASE WHEN usage_date >= dateadd(day, -:period_days, current_date())
                  THEN list_cost END)                                        AS current_cost,
         SUM(CASE WHEN usage_date <  dateadd(day, -:period_days, current_date())
                  THEN list_cost END)                                        AS previous_cost,
         SUM(CASE WHEN usage_date >= dateadd(day, -:period_days, current_date())
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                            AS current_unpriced_quantity,
         SUM(CASE WHEN usage_date >= dateadd(day, -:period_days, current_date())
                   AND list_rate IS NOT NULL
                  THEN usage_quantity ELSE 0 END)                            AS current_priced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -:period_days, current_date())
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                            AS previous_unpriced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -:period_days, current_date())
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
         WHEN s.snapshot_start > dateadd(day, -(:period_days * 2), current_date())
           THEN 'NOT_ASSESSED'
         WHEN a.current_unpriced_quantity > 0
           THEN 'NOT_ASSESSED'
         WHEN a.previous_unpriced_quantity > 0
           THEN 'NOT_ASSESSED'
         WHEN COALESCE(a.eff_current_cost, 0) < :min_spend_usd THEN 'OK'
         WHEN a.eff_previous_cost = 0         THEN 'CRITICAL'
         WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= :crit_increase_pct
           THEN 'CRITICAL'
         WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= :warn_increase_pct
           THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN s.snapshot_start > dateadd(day, -(:period_days * 2), current_date())
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
