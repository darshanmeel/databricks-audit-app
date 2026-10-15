-- query_id: cost_chargeback_by_workspace
-- title: Chargeback by workspace, with change vs the previous period
-- domain: cost   tier: standard
-- reads: system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.billing; GA (system.billing.usage and system.billing.list_prices
--   are generally available)
-- empty_if: ingestion_lag, no_activity
-- params: :period_days (default 30) rolling window in days - the current period is the most
--   recent N days, the previous period the equal-length N days before it; :warn_increase_pct
--   (default 25) percent increase in list-priced spend, current period over previous, at/above
--   which WARN; :crit_increase_pct (default 50) percent increase at/above which CRITICAL;
--   :min_spend_usd (default 20) the current period's own list-priced spend must be at least this
--   before a percent change is judged - below it the row reads OK regardless of the swing.
--   Needs 2x :period_days of billing history in the account for the previous-period comparison;
--   see caveats.
-- confidence: needs_confirmation
-- confidence_note: New app-owned query, not yet run live. Reuses the effective-list-price join
--   every priced cost query in this app already uses (DEC-66.1) and cost_period_over_period's own
--   WARN/CRITICAL band and NOT_ASSESSED coverage guard (DEC-62/DEC-64), collapsed to workspace_id
--   alone instead of workspace + product line; confirm a workspace you know grew or shrank between
--   two recent equal periods matches this query's change_pct.
-- read_this: One row = one workspace's spend in the selected window against the equal-length
--   window right before it, both at the effective list price (DEC-66.1). A NULL workspace_id is a
--   genuine account-level usage row (some networking/storage SKUs bill with no workspace_id at
--   all), kept as its own line, never dropped. usd_list and prev_usd_list are the two totals;
--   change_usd_list and change_pct compare them. price_basis discloses pricing coverage across
--   both windows combined; not_assessed_reason explains a NOT_ASSESSED row. workspace_name is not
--   resolved here - join cost_workspace_names on workspace_id for the human label.
-- healthy: status OK - no increase at/above :warn_increase_pct, or the row's own current-period
--   spend (a real $0 counts) sits below :min_spend_usd.
-- investigate_if: CRITICAL - the increase is at/above :crit_increase_pct on at least
--   :min_spend_usd of current spend, or brand-new spend above the floor with nothing in the
--   previous period to compare against. WARN - at/above :warn_increase_pct. NOT_ASSESSED - read
--   not_assessed_reason: 'previous_window_not_covered' when the account's own earliest recorded
--   usage does not reach back far enough to cover the previous window at this window length;
--   'current_period_unpriced' / 'previous_period_unpriced' when that side's whole spend could not
--   be priced at all.
-- actions: 1) open cost_period_over_period for the same workspace to see which product line is
--   driving the move (free); 2) open cost_chargeback_by_allocation_tag for the same workspace to
--   see which cost_center/team is behind it (free); 3) if the increase is deliberate growth, set
--   or tighten a budget policy for the workspace before it compounds another period (config, or
--   spend if intentional).
-- not_assessed_reasons: previous_window_not_covered: the snapshot does not reach back far enough
--   to cover the previous period; current_period_unpriced: this period's spend could not be
--   priced; previous_period_unpriced: the previous period's spend could not be priced
-- next: cost_workspace_names (to resolve workspace_id to a human name), cost_period_over_period
--   (for the product-line cut inside this workspace), cost_chargeback_by_allocation_tag (for the
--   cost_center/team cut inside this workspace)
-- caveats: DEC-66.1 - the effective list price only, never a negotiated rate; the same basis on
--   both windows. DEC-62 - both windows are measured, never projected or scaled. This needs the
--   account's own billing history to reach back 2x :period_days for a real previous-period
--   comparison (DEC-64); short of that, every row reads NOT_ASSESSED (previous_window_not_covered)
--   rather than a false CRITICAL "infinite increase" from an empty previous window. Deliberately
--   NOT joined to system.access.workspaces_latest for a name here (the same separation
--   cost_workspace_names documents): if that table is not enabled or errors in your account, this
--   workspace-keyed cost data is unaffected - resolve the name yourself via cost_workspace_names.
--   price_basis is computed over BOTH windows combined - 'unpriced' when any non-free-usage SKU in
--   either window had no matching list_prices row (the totals then understate cost), 'free' when
--   every matched SKU is a FREE_USAGE SKU (a real $0), 'priced' otherwise. Corrections are netted
--   (SUM across all record_types - never filter to ORIGINAL only). The current day is excluded
--   from both windows, since billing.usage lands with ingestion lag and the trailing day is
--   provisional. Workspace count is normally modest even in a large account, so this query is
--   never top-N'd or pooled into an "other" row, unlike cost_chargeback_by_warehouse.
WITH snapshot AS (
  SELECT MIN(usage_date) AS snapshot_start
  FROM system.billing.usage
),
priced AS (
  -- ws_key normalizes NULL (account-level) to a real string so it is a safe group key like any
  -- other value; workspace_id itself is carried through unchanged for display.
  SELECT COALESCE(u.workspace_id, '__account_level__') AS ws_key,
         u.workspace_id, u.usage_date, u.sku_name,
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
  SELECT ws_key, MAX(workspace_id) AS workspace_id,
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
  GROUP BY ws_key
),
agg AS (
  SELECT *,
         CASE WHEN current_cost IS NULL AND current_unpriced_quantity = 0 THEN 0 ELSE current_cost END AS eff_current_cost,
         CASE WHEN previous_cost IS NULL AND previous_unpriced_quantity = 0 THEN 0 ELSE previous_cost END AS eff_previous_cost
  FROM raw_agg
),
total AS (
  SELECT SUM(eff_current_cost) AS total_usd_list FROM agg
)
SELECT a.workspace_id,
       ROUND(a.eff_current_cost, 2)                                      AS usd_list,
       ROUND(a.eff_current_cost * 100.0 / NULLIF(t.total_usd_list, 0), 1) AS share_of_total_pct,
       ROUND(a.eff_previous_cost, 2)                                     AS prev_usd_list,
       ROUND(a.eff_current_cost - a.eff_previous_cost, 2)                AS change_usd_list,
       ROUND((a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100, 1) AS change_pct,
       CASE
         WHEN (a.current_unpriced_quantity + a.previous_unpriced_quantity) > 0 THEN 'unpriced'
         WHEN (a.current_priced_quantity + a.previous_priced_quantity) = 0     THEN 'free'
         ELSE 'priced'
       END AS price_basis,
       CASE
         WHEN s.snapshot_start > dateadd(day, -(:period_days * 2), current_date()) THEN 'NOT_ASSESSED'
         WHEN a.current_cost IS NULL AND a.current_unpriced_quantity > 0           THEN 'NOT_ASSESSED'
         WHEN a.previous_cost IS NULL AND a.previous_unpriced_quantity > 0         THEN 'NOT_ASSESSED'
         WHEN COALESCE(a.eff_current_cost, 0) < :min_spend_usd                    THEN 'OK'
         WHEN a.eff_previous_cost = 0                                             THEN 'CRITICAL'
         WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= :crit_increase_pct
           THEN 'CRITICAL'
         WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= :warn_increase_pct
           THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN s.snapshot_start > dateadd(day, -(:period_days * 2), current_date()) THEN 'previous_window_not_covered'
         WHEN a.current_cost IS NULL AND a.current_unpriced_quantity > 0           THEN 'current_period_unpriced'
         WHEN a.previous_cost IS NULL AND a.previous_unpriced_quantity > 0         THEN 'previous_period_unpriced'
         ELSE NULL
       END AS not_assessed_reason
FROM agg a
CROSS JOIN snapshot s
CROSS JOIN total t
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         change_usd_list DESC NULLS LAST,
         a.workspace_id
