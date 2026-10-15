-- query_id: cost_monthly_actuals
-- title: Dollars per calendar month, by workspace and product line
-- domain: cost   tier: standard
-- reads: system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.billing; GA (system.billing.usage and system.billing.list_prices
--   are generally available)
-- empty_if: ingestion_lag
-- params: none (windowless - one row per calendar month the export actually carries, bounded
--   only by however much system.billing.usage history the snapshot holds)
-- confidence: needs_confirmation
-- confidence_note: New app-owned query, not yet run live. Uses the same
--   pricing.effective_list.default join (DEC-66.1) and price_basis CASE every priced cost query
--   here already uses; confirm month_start groups usage_date the way your calendar expects
--   (month_start is the first day of the month, in the export's own dates - no timezone shift).
-- read_this: One row = a workspace + billing_origin_product's total list-priced spend for one
--   calendar month. month_start is the first day of that month; is_partial_month is TRUE for the
--   current, still-in-progress calendar month AND for the export's own oldest month if the
--   snapshot's history starts partway through it - read either kind's total as partial, never as
--   the month's final number (DEC-62: no forecast, no scaling a partial month up to a full one).
--   partial_reason says which kind ('month_in_progress' or 'export_starts_mid_month'). days_captured
--   is how many distinct days that month actually have a usage row for this workspace/product, a
--   quick honesty check on how complete the month's total is; first_day/last_day are that range's
--   own earliest/latest usage_date, so a partial month can be labelled honestly (e.g. "16-22 Sep")
--   instead of implied as the whole month.
-- healthy: n/a - inventory (actuals, not a verdict)
-- investigate_if: n/a - inventory (actuals, not a verdict); pair with cost_period_over_period for
--   a judged comparison
-- actions: n/a - inventory (reference/trend input)
-- next: cost_period_over_period (for a judged current-vs-previous comparison with WARN/CRITICAL
--   bands), cost_dollarized_by_sku_day (for the same dollars broken out by day and SKU),
--   overview_spend_estimate (for the account's current headline number)
-- caveats: DEC-66.1 - the effective list price only, never a negotiated rate; DEC-62 - this is
--   the actual dollars the export captured for each month, never a forecast or a partial month
--   scaled up to a full one - a partial month is labelled is_partial_month and left as-is.
--   is_partial_month is TRUE for the CURRENT calendar month (month_start equals the first of this
--   month, partial_reason 'month_in_progress') AND, separately (DEC-64), for whichever month holds
--   the WHOLE account's own earliest recorded usage_date across system.billing.usage, if that date
--   is not itself the 1st - the export's own history starts partway through that month, so its
--   total is genuinely incomplete no matter how many days it shows (partial_reason
--   'export_starts_mid_month'); a 365-day export starting in mid-September, for example, makes its
--   oldest month (September of the year before) partial even though most of its dollars are real.
--   Any OTHER older month is still only as complete as days_captured shows - a full month rarely
--   shows fewer than ~28-31 distinct days, and a workload can be legitimately intermittent.
--   Windowless: every calendar month system.billing.usage
--   carries is returned (no :period_days), the same "snapshot-type" shape as
--   overview_list_prices_raw and cost_workspace_names, so this query is unaffected by the app's
--   7/30/90-day window selector - it always reads the export's own full history. Unlike
--   cost_dollarized_by_sku_day this rolls SKU/usage_type/usage_unit all the way up to one dollar
--   figure per workspace/product/month - use cost_dollarized_by_sku_day for the SKU-level detail
--   behind any one month. price_basis is computed over the WHOLE month - 'unpriced' when any
--   non-free-usage SKU that month had no matching list_prices row (net_list_cost_usd then
--   understates that month), 'free' when every matched SKU is a FREE_USAGE SKU (a real $0),
--   'priced' otherwise. Corrections are netted (SUM across all record_types - never filter to
--   ORIGINAL only). The in-flight day (today) is EXCLUDED - the same `usage_date < current_date()`
--   cut-off every other windowed cost query in this app uses (cost_dollarized_by_sku_day,
--   cost_period_over_period, overview_spend_estimate, ...), so this month's total, for the days it
--   actually has, always reconciles to the cent with a window total (e.g.
--   cost_dollarized_by_sku_day) summed over the same complete days - never two different-looking
--   dollar figures on the same screen for the same days. is_partial_month is still TRUE for the
--   current month (it is still short whatever days have not finished yet); first_day, last_day and
--   days_captured say exactly which days are counted so far. billing.usage also lands with
--   ingestion lag, so the last day or two of an otherwise-"complete" month can still be
--   under-counted.
WITH snapshot AS (
  -- the WHOLE account's own earliest recorded usage_date across system.billing.usage - the
  -- DEC-64 signal for whether the export's own oldest month is itself only partly captured.
  SELECT MIN(usage_date) AS snapshot_start
  FROM system.billing.usage
)
SELECT
    u.workspace_id,
    u.billing_origin_product,
    CAST(date_trunc('MONTH', u.usage_date) AS DATE)               AS month_start,
    MAX(lp.currency_code)                                         AS currency_code,
    ROUND(SUM(u.usage_quantity * lp.list_rate), 2)                AS net_list_cost_usd,
    COUNT(DISTINCT u.usage_date)                                  AS days_captured,
    CASE
      WHEN CAST(date_trunc('MONTH', u.usage_date) AS DATE) = CAST(date_trunc('MONTH', current_date()) AS DATE)
        THEN TRUE
      WHEN s.snapshot_start > CAST(date_trunc('MONTH', u.usage_date) AS DATE)
        THEN TRUE
      ELSE FALSE
    END AS is_partial_month,
    CASE
      WHEN CAST(date_trunc('MONTH', u.usage_date) AS DATE) = CAST(date_trunc('MONTH', current_date()) AS DATE)
        THEN 'month_in_progress'
      WHEN s.snapshot_start > CAST(date_trunc('MONTH', u.usage_date) AS DATE)
        THEN 'export_starts_mid_month'
      ELSE NULL
    END AS partial_reason,
    -- price_basis: a real $0 (free-usage SKUs) vs a pricing-coverage gap (understates the month).
    CASE
      WHEN SUM(CASE WHEN lp.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                    THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
      WHEN SUM(CASE WHEN lp.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
      ELSE 'priced'
    END AS price_basis,
    MIN(u.usage_date)                                             AS first_day,
    MAX(u.usage_date)                                             AS last_day
FROM system.billing.usage u
CROSS JOIN snapshot s
LEFT JOIN (
  SELECT sku_name, cloud, currency_code, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
  FROM system.billing.list_prices
) lp
  ON u.sku_name   = lp.sku_name
 AND u.cloud      = lp.cloud
 AND u.usage_unit = lp.usage_unit
 AND u.usage_end_time >= lp.price_start_time
 AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
WHERE u.usage_date < current_date()
GROUP BY u.workspace_id, u.billing_origin_product, CAST(date_trunc('MONTH', u.usage_date) AS DATE),
         s.snapshot_start
ORDER BY month_start DESC, workspace_id, billing_origin_product
