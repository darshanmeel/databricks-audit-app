-- query_id: cost_sku_trend_12m
-- title: Fastest-growing SKUs, last 12 months
-- domain: cost   tier: standard
-- reads: system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.billing; GA (system.billing.usage and system.billing.list_prices
--   are generally available)
-- params: :warn_growth_pct (default 50) growth_3m_pct (last 3 full months' average vs the 3
--   before) at/above which WARN; :crit_growth_pct (default 100) growth_3m_pct at/above which
--   CRITICAL; :warn_min_usd (default 500) usd_last_3m_avg floor a WARN growth also needs;
--   :crit_min_usd (default 1000) usd_last_3m_avg floor a CRITICAL growth also needs;
--   :new_sku_min_usd (default 500) usd_last_3m_avg floor above which a SKU with no spend in its
--   first 3 months reads WARN. Not windowed by :period_days - this check always covers the last
--   12 full calendar months plus the current partial month, regardless of the app's 7/30/90-day
--   selector.
-- confidence: needs_confirmation
-- confidence_note: New app-owned query, not yet run live. Reuses the same
--   pricing.effective_list.default join (DEC-66.1) and price_basis CASE cost_monthly_actuals
--   already uses, and the "measured, never forecast" rule DEC-62 established. product_label's
--   CASE only names the billing_origin_product values already seen elsewhere in this app
--   (ALL_PURPOSE, JOBS, SQL, DLT, MODEL_SERVING, VECTOR_SEARCH, DEFAULT_STORAGE/STORAGE) - confirm
--   it against your own account's values. Confirm a SKU you know grew this year (Genie, Model
--   Serving, a serverless SKU) shows a matching growth_3m_pct/growth_12m_pct here.
-- read_this: One row = one workspace_id + sku_name in one calendar month, over the last 12 full
--   months plus the current partial month (a NULL workspace_id is a genuine account-level usage
--   row, its own group like any workspace's, never dropped or blended into one). is_partial_month
--   is TRUE only for the current, still-in-progress month - never compare it to a full month.
--   net_list_cost_usd/price_basis are that one workspace/month's own dollars, priced the same way
--   cost_monthly_actuals prices a month. The remaining columns are the SKU's OWN trend WITHIN THAT
--   WORKSPACE, repeated identically on every one of that workspace+SKU's rows (this grain keeps
--   one row shape for the whole table - no separate month=NULL summary row): usd_last_3m_avg /
--   usd_prior_3m_avg are its average monthly dollars over the last 3 full months and the 3 before
--   that; growth_3m_pct compares them. usd_first_3m_avg is its average over the OLDEST 3 of the 12
--   full months; growth_12m_pct compares the last 3 to that. share_of_total_last_3m is this SKU's
--   share of every SKU's usd_last_3m_avg combined WITHIN THE SAME WORKSPACE (read it off any one
--   of the workspace+SKU's own rows, not summed across rows - it repeats). new_this_year is TRUE
--   when usd_first_3m_avg is a real 0 (no spend at all in the oldest 3 months, in that workspace)
--   - a plain fact, independent of size; see status for when that also reads WARN.
-- healthy: status OK - growth_3m_pct below :warn_growth_pct, or usd_last_3m_avg below
--   :warn_min_usd (a percentage on a small base is noise), and not a material new_this_year SKU.
-- investigate_if: CRITICAL - growth_3m_pct at/above :crit_growth_pct with usd_last_3m_avg at/above
--   :crit_min_usd. WARN - growth_3m_pct at/above :warn_growth_pct with usd_last_3m_avg at/above
--   :warn_min_usd, OR new_this_year with usd_last_3m_avg at/above :new_sku_min_usd (a SKU with
--   nothing 10-12 months ago that is already spending real money now).
-- actions: 1) open cost_chargeback_by_allocation_tag or the Cost > Allocation tab for this
--   workspace and SKU's billing_origin_product to see which tag started using it (free); 2) if the
--   growth is deliberate, set or tighten a budget policy for that workspace/tag before it compounds
--   another quarter (config, not code).
-- next: cost_chargeback_by_allocation_tag (for the workspace/tag behind the growth),
--   overview_dbu_by_sku (for the same SKU's workspace-level DBUs), cost_monthly_actuals (for the
--   account's full month-by-month actuals, every product, not just the growing ones)
-- caveats: DEC-66.1 - the effective list price only, never a negotiated rate. DEC-62 - every
--   figure here is a measured average of real months, never a forecast or an annualised number;
--   the current partial month is excluded from every 3-month average (last/prior/first) and is
--   never itself compared to a full month, only shown for reference with is_partial_month=TRUE.
--   GRAIN - the SKU-level columns (usd_last_3m_avg onward, including status) are computed once per
--   workspace_id + sku_name and repeated on that workspace+SKU's own month rows, chosen over a
--   separate month=NULL summary row so every row in this table has the same shape. A SKU whose
--   real history is shorter than 12 months (a young account, or an export that only recently
--   started capturing this SKU) shows the same zeros in its oldest months as a genuine
--   new_this_year SKU would - this query cannot tell the two apart; check cost_monthly_actuals for
--   how far back your own export's history actually reaches. workspace_id narrows this to one
--   workspace at a time, the same as every other per-workspace finding; a NULL workspace_id is a
--   genuine account-level usage row (some SKUs bill with no workspace_id at all), kept as its own
--   group, never merged into a real workspace's numbers or dropped - the actions above are how to
--   get the tag-level cut within a workspace. Corrections are netted (SUM across all record_types
--   - never filter to ORIGINAL only), the same as every other cost_* query. price_basis is
--   computed per workspace/SKU/month: 'unpriced' when any non-free-usage row that month had no
--   matching list_prices row (net_list_cost_usd then understates that month), 'free' when every
--   matched SKU is a FREE_USAGE SKU (a real $0), 'priced' otherwise. The in-flight day (today) is
--   excluded, same as every other
--   cost query here.
WITH bounds AS (
  SELECT
    CAST(date_trunc('MONTH', current_date()) AS DATE)                          AS cur_month_start,
    CAST(dateadd(month, -3,  date_trunc('MONTH', current_date())) AS DATE)     AS last3_start,
    CAST(dateadd(month, -6,  date_trunc('MONTH', current_date())) AS DATE)     AS prior3_start,
    CAST(dateadd(month, -9,  date_trunc('MONTH', current_date())) AS DATE)     AS mid3_end,
    CAST(dateadd(month, -12, date_trunc('MONTH', current_date())) AS DATE)     AS window_start
),
priced AS (
  SELECT u.workspace_id, u.sku_name, u.billing_origin_product, u.usage_date,
         u.usage_quantity                    AS usage_quantity,
         u.usage_quantity * lp.list_rate     AS list_cost,
         lp.list_rate                        AS list_rate
  FROM system.billing.usage u
  CROSS JOIN bounds b
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
  WHERE u.usage_date >= b.window_start
    AND u.usage_date <  current_date()
),
monthly AS (
  SELECT
    p.workspace_id,
    p.sku_name,
    MAX(p.billing_origin_product)                   AS billing_origin_product,
    CAST(date_trunc('MONTH', p.usage_date) AS DATE)  AS month_start,
    ROUND(SUM(p.list_cost), 2)                       AS net_list_cost_usd,
    CASE
      WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(p.sku_name) NOT LIKE '%FREE_USAGE%'
                    THEN p.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
      WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN p.usage_quantity ELSE 0 END) = 0 THEN 'free'
      ELSE 'priced'
    END AS price_basis
  FROM priced p
  GROUP BY p.workspace_id, p.sku_name, CAST(date_trunc('MONTH', p.usage_date) AS DATE)
),
sku_bucket AS (
  -- per-workspace+SKU dollars in each 3-month bucket, from the raw priced rows (never from
  -- `monthly`) so a bucket with zero usage in every one of its 3 months still resolves to a real
  -- 0.0, never a NULL average over fewer months. FROM is the full `priced` set (the current
  -- partial month's own rows included, not filtered out here) so a brand-new workspace+SKU whose
  -- only usage is this month still gets a group and is not silently dropped by the later JOIN to
  -- `monthly` - each bucket's own CASE bounds it to full months only (the current month never
  -- satisfies any of the three).
  SELECT
    p.workspace_id,
    p.sku_name,
    ROUND(SUM(CASE WHEN p.usage_date >= b.last3_start AND p.usage_date < b.cur_month_start
                   THEN p.list_cost ELSE 0 END) / 3.0, 2)                        AS usd_last_3m_avg,
    ROUND(SUM(CASE WHEN p.usage_date >= b.prior3_start AND p.usage_date < b.last3_start
                   THEN p.list_cost ELSE 0 END) / 3.0, 2)                        AS usd_prior_3m_avg,
    ROUND(SUM(CASE WHEN p.usage_date >= b.window_start AND p.usage_date < b.mid3_end
                   THEN p.list_cost ELSE 0 END) / 3.0, 2)                        AS usd_first_3m_avg
  FROM priced p
  CROSS JOIN bounds b
  GROUP BY p.workspace_id, p.sku_name
),
sku_metrics AS (
  SELECT
    sb.workspace_id,
    sb.sku_name,
    sb.usd_last_3m_avg,
    sb.usd_prior_3m_avg,
    sb.usd_first_3m_avg,
    ROUND((sb.usd_last_3m_avg - sb.usd_prior_3m_avg) / NULLIF(sb.usd_prior_3m_avg, 0) * 100, 1) AS growth_3m_pct,
    ROUND((sb.usd_last_3m_avg - sb.usd_first_3m_avg) / NULLIF(sb.usd_first_3m_avg, 0) * 100, 1)  AS growth_12m_pct,
    -- share is within the SKU's own workspace (or within the NULL account-level group), never
    -- blended with another workspace's dollars.
    ROUND(100.0 * sb.usd_last_3m_avg
          / NULLIF(SUM(sb.usd_last_3m_avg) OVER (PARTITION BY sb.workspace_id), 0), 1)           AS share_of_total_last_3m,
    (sb.usd_first_3m_avg = 0)                                                                     AS new_this_year
  FROM sku_bucket sb
)
SELECT
    m.workspace_id,
    m.sku_name,
    m.billing_origin_product,
    CASE m.billing_origin_product
      WHEN 'ALL_PURPOSE'     THEN 'All-purpose compute'
      WHEN 'JOBS'            THEN 'Jobs'
      WHEN 'SQL'             THEN 'SQL warehouses'
      WHEN 'DLT'             THEN 'Lakeflow Declarative Pipelines'
      WHEN 'MODEL_SERVING'   THEN 'Model serving'
      WHEN 'VECTOR_SEARCH'   THEN 'Vector search'
      WHEN 'DEFAULT_STORAGE' THEN 'Storage'
      WHEN 'STORAGE'         THEN 'Storage'
      ELSE m.billing_origin_product
    END                                                       AS product_label,
    m.month_start,
    (m.month_start = b.cur_month_start)                       AS is_partial_month,
    m.net_list_cost_usd,
    m.price_basis,
    sm.usd_last_3m_avg,
    sm.usd_prior_3m_avg,
    sm.growth_3m_pct,
    sm.usd_first_3m_avg,
    sm.growth_12m_pct,
    sm.share_of_total_last_3m,
    sm.new_this_year,
    CASE
      WHEN sm.growth_3m_pct >= :crit_growth_pct AND sm.usd_last_3m_avg >= :crit_min_usd THEN 'CRITICAL'
      WHEN sm.growth_3m_pct >= :warn_growth_pct AND sm.usd_last_3m_avg >= :warn_min_usd  THEN 'WARN'
      WHEN sm.new_this_year AND sm.usd_last_3m_avg >= :new_sku_min_usd                   THEN 'WARN'
      ELSE 'OK'
    END AS status
FROM monthly m
CROSS JOIN bounds b
JOIN sku_metrics sm
  ON sm.sku_name = m.sku_name
 AND sm.workspace_id IS NOT DISTINCT FROM m.workspace_id
ORDER BY CASE
           WHEN sm.growth_3m_pct >= :crit_growth_pct AND sm.usd_last_3m_avg >= :crit_min_usd THEN 0
           WHEN sm.growth_3m_pct >= :warn_growth_pct AND sm.usd_last_3m_avg >= :warn_min_usd  THEN 1
           WHEN sm.new_this_year AND sm.usd_last_3m_avg >= :new_sku_min_usd                   THEN 1
           ELSE 2
         END,
         sm.usd_last_3m_avg DESC,
         m.workspace_id,
         m.sku_name,
         m.month_start DESC
