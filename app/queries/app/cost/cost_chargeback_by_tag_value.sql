-- query_id: cost_chargeback_by_tag_value
-- title: Chargeback by every custom tag value, current period versus the one before it
-- domain: cost   tier: standard
-- reads: system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.billing; GA (system.billing.usage and system.billing.list_prices
--   are generally available)
-- empty_if: no_activity
-- params: :period_days (default 30) rolling window in days - the current period is the most
--   recent N days, the previous period is the equal-length N days immediately before it;
--   :top_values_per_key (default 10) the most expensive tag values kept per tag_key before the
--   rest are rolled into that key's own "(other)" row; :warn_increase_pct (default
--   25) percent increase in a row's list-priced spend, current period over previous, at/above
--   which WARN; :crit_increase_pct (default 50) percent increase at/above which CRITICAL;
--   :min_spend_usd (default 20) the current period's own list-priced spend must be at least this
--   before a percent change is judged
-- confidence: needs_confirmation
-- confidence_note: New app-owned query, not yet run live. Generalises
--   cost_chargeback_by_allocation_tag's cost_center/team mechanism to every tag key an account
--   actually uses (no per-account key configuration needed), reusing the same custom_tags explode
--   cost_chargeback_by_tag already proves works against a live workspace and the exact
--   current-vs-previous-window contract cost_period_over_period established; confirm on your
--   account that a tag key/value you know carries real spend shows up with the right dollar figure
--   and the right (other)/(untagged) split.
-- read_this: One row = one workspace's value of one custom_tags key (tag_key, tag_value), with
--   usage in the window, OR that workspace + key's own "(other)" rollup (every value beyond the
--   top :top_values_per_key by current-period spend), OR its own "(untagged)" row (usage whose
--   custom_tags map has no entry for this key at all, even when the resource carries other,
--   unrelated tags) - every real tag key found anywhere in a workspace during the window gets its
--   own top values + (other) + (untagged) set there, so a multi-tagged resource (env AND
--   cost_center AND team, say) contributes to every one of those keys' own totals, not just one.
--   A NULL workspace_id is a genuine account-level usage row (some networking/storage SKUs bill
--   with no workspace_id at all), kept as its own line. pooled_count is how many distinct values
--   an "(other)" row folds in (NULL on every other row, including "(untagged)", which is one
--   value, not a pool). usd_list is this row's current-period dollars at the effective list price
--   (DEC-66.1), summed across every usage_unit (already-priced dollars, unlike a raw quantity, add
--   up fine across units); share_of_total_pct is its share of the account's WHOLE current-period
--   spend (across every workspace, unchanged by this row now being workspace-scoped - see
--   caveats). prev_usd_list / change_usd_list / change_pct compare it against the equal-length
--   window right before. price_basis discloses pricing coverage across both windows combined;
--   not_assessed_reason explains a NOT_ASSESSED row.
-- healthy: status OK - no increase at/above :warn_increase_pct, or the row's own current-period
--   spend (a real $0 counts) sits below :min_spend_usd. The comparison is judged per workspace,
--   not account-wide (see caveats), so a tag value's account-wide swing may not itself flag.
-- investigate_if: CRITICAL - the increase is at/above :crit_increase_pct on at least
--   :min_spend_usd of current spend, or the row is brand-new spend above the floor with nothing in
--   the previous period to compare against; WARN - at/above :warn_increase_pct - this applies to a
--   growing "(untagged)" row exactly like any real value, so a widening tagging gap shows up as a
--   real WARN/CRITICAL, not just a percentage aside. NOT_ASSESSED - read not_assessed_reason:
--   'previous_window_not_covered' when the account's own earliest recorded usage does not reach
--   back far enough to cover the previous window; 'current_period_unpriced' / 'previous_period_
--   unpriced' when that side's whole spend for this row could not be priced at all.
-- actions: 1) open cost_chargeback_by_allocation_tag for the canonical cost_center/team cut of the
--   same spend, or cost_chargeback_by_job / cost_chargeback_by_cluster to find which resource is
--   driving a growing (untagged) row (free); 2) tag the untagged resource, or fix a value that is
--   really the same team spelled two ways (config); 3) if the growth on a real value is deliberate,
--   set or tighten a budget policy for it before it compounds another period (config).
-- next: cost_chargeback_by_allocation_tag (the canonical cost_center/team roll-up of the same
--   data), cost_chargeback_reconcile (this view's own total against the account's billing total),
--   cost_chargeback_by_job (the job behind a growing value)
-- caveats: DEC-66.1 - the effective list price only, never a negotiated rate; the same basis on
--   both windows. WORKSPACE - the grain is now workspace + tag_key + tag_value, so the same tag
--   value appears once per workspace that used it; share_of_total_pct's own denominator stays the
--   account's whole current-period spend across every usage_unit combined (unchanged from before
--   this split - summing a value's rows back across workspaces reproduces its old account-wide
--   share), but the (untagged) row's own subtraction is now done within ONE workspace (that
--   workspace's own total minus that workspace's own tagged-with-this-key total), so an
--   "(untagged)" row reads "untagged spend in this workspace", not "this workspace's share of the
--   account's untagged pool". :top_values_per_key is likewise applied per (workspace, tag_key), so
--   a value ranked in the top N for one workspace can still be folded into another workspace's own
--   (other) row. Both totals are safe to sum across usage_unit because dollars, unlike a raw
--   usage_quantity, add up across units; a tag search over the raw quantity would need to stay
--   scoped per usage_unit instead. An account with NO custom_tags at all anywhere in the window
--   returns no rows (there is no key to report on) - see cost_totals_by_sku_day or
--   overview_spend_estimate for the account's raw total in that case. The literal tag values
--   "(other)" and "(untagged)" are this query's own rollup markers, not real tag values - a
--   genuine tag value that happens to be spelled exactly "(other)" or "(untagged)" would collide
--   with them (use is_other / is_untagged, never a string match on tag_value, to tell them apart).
--   is_other always reads status OK - a rollup of many small values, some past
--   :top_values_per_key, should never itself become a finding; only individual values and
--   (untagged) are flagged. This is a chargeback reference, not a fix for a listed problem.
--   Corrections are netted (SUM across all record_types - never filter to ORIGINAL only). The
--   current day is excluded from both windows, same as every other windowed cost query.
-- not_assessed_reasons: previous_window_not_covered: the snapshot does not reach back far enough
--   to cover the previous period; current_period_unpriced: this period's spend could not be
--   priced; previous_period_unpriced: the previous period's spend could not be priced
WITH snapshot AS (
  SELECT MIN(usage_date) AS snapshot_start FROM system.billing.usage
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
  FROM system.billing.list_prices
),
priced AS (
  SELECT u.workspace_id, u.custom_tags, u.sku_name,
         (u.usage_date >= dateadd(day, -:period_days, current_date())) AS is_current,
         u.usage_quantity * p.list_rate AS usd_list,
         CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
              THEN u.usage_quantity ELSE 0 END AS unpriced_q,
         CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END AS priced_q
  FROM system.billing.usage u
  LEFT JOIN price p
    ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE u.usage_date >= dateadd(day, -(:period_days * 2), current_date())
    AND u.usage_date <  current_date()
),
totals AS (
  -- account-wide current/previous totals across every usage_unit combined, over EVERY usage row
  -- regardless of tags or workspace - the denominator for share_of_total_pct (ws_totals below is
  -- the per-workspace twin the (untagged) row itself subtracts from).
  SELECT
    SUM(CASE WHEN is_current     THEN usd_list END)      AS cur_total_usd,
    SUM(CASE WHEN NOT is_current THEN usd_list END)      AS prev_total_usd,
    SUM(CASE WHEN is_current     THEN unpriced_q ELSE 0 END) AS cur_total_unpriced_q,
    SUM(CASE WHEN is_current     THEN priced_q   ELSE 0 END) AS cur_total_priced_q,
    SUM(CASE WHEN NOT is_current THEN unpriced_q ELSE 0 END) AS prev_total_unpriced_q,
    SUM(CASE WHEN NOT is_current THEN priced_q   ELSE 0 END) AS prev_total_priced_q
  FROM priced
),
ws_totals AS (
  -- same shape as `totals` above, but per workspace -- the (untagged) row's own subtraction has
  -- to happen within one workspace at a time now that the grain is workspace + tag_key (see
  -- caveats); `totals` itself stays account-wide, still the ONLY source for share_of_total_pct.
  SELECT workspace_id,
    SUM(CASE WHEN is_current     THEN usd_list END)      AS cur_total_usd,
    SUM(CASE WHEN NOT is_current THEN usd_list END)      AS prev_total_usd,
    SUM(CASE WHEN is_current     THEN unpriced_q ELSE 0 END) AS cur_total_unpriced_q,
    SUM(CASE WHEN is_current     THEN priced_q   ELSE 0 END) AS cur_total_priced_q,
    SUM(CASE WHEN NOT is_current THEN unpriced_q ELSE 0 END) AS prev_total_unpriced_q,
    SUM(CASE WHEN NOT is_current THEN priced_q   ELSE 0 END) AS prev_total_priced_q
  FROM priced
  GROUP BY workspace_id
),
tagged AS (
  -- one row per (usage row, tag_key/tag_value pair it carries). A plain (non-OUTER) explode
  -- already drops a usage row with no tags at all - its dollars are recovered per key by the
  -- untagged_values subtraction further down instead.
  SELECT p.workspace_id, p.is_current, p.usd_list, p.unpriced_q, p.priced_q, t.tag_key, t.tag_value
  FROM priced p
  LATERAL VIEW explode(p.custom_tags) t AS tag_key, tag_value
),
key_present AS (
  -- per workspace + real key found anywhere in the window: current/previous dollars and
  -- quantities from usage rows that DO carry that key - what untagged_values below subtracts from
  -- `ws_totals` to get "this workspace's rows missing this key", without a second explode or a
  -- key x usage cross join.
  SELECT workspace_id, tag_key,
         SUM(CASE WHEN is_current     THEN usd_list END)      AS cur_present_usd,
         SUM(CASE WHEN NOT is_current THEN usd_list END)      AS prev_present_usd,
         SUM(CASE WHEN is_current     THEN unpriced_q ELSE 0 END) AS cur_present_unpriced_q,
         SUM(CASE WHEN is_current     THEN priced_q   ELSE 0 END) AS cur_present_priced_q,
         SUM(CASE WHEN NOT is_current THEN unpriced_q ELSE 0 END) AS prev_present_unpriced_q,
         SUM(CASE WHEN NOT is_current THEN priced_q   ELSE 0 END) AS prev_present_priced_q
  FROM tagged
  GROUP BY workspace_id, tag_key
),
value_agg AS (
  SELECT workspace_id, tag_key, tag_value,
         SUM(CASE WHEN is_current     THEN usd_list END)      AS cur_usd,
         SUM(CASE WHEN NOT is_current THEN usd_list END)      AS prev_usd,
         SUM(CASE WHEN is_current     THEN unpriced_q ELSE 0 END) AS cur_unpriced_q,
         SUM(CASE WHEN is_current     THEN priced_q   ELSE 0 END) AS cur_priced_q,
         SUM(CASE WHEN NOT is_current THEN unpriced_q ELSE 0 END) AS prev_unpriced_q,
         SUM(CASE WHEN NOT is_current THEN priced_q   ELSE 0 END) AS prev_priced_q
  FROM tagged
  GROUP BY workspace_id, tag_key, tag_value
),
ranked AS (
  SELECT *,
         ROW_NUMBER() OVER (
           PARTITION BY workspace_id, tag_key ORDER BY COALESCE(cur_usd, 0) DESC, tag_value
         ) AS value_rank
  FROM value_agg
),
top_values AS (
  SELECT workspace_id, tag_key, tag_value, cur_usd, prev_usd,
         cur_unpriced_q, cur_priced_q, prev_unpriced_q, prev_priced_q,
         FALSE AS is_other, FALSE AS is_untagged, CAST(NULL AS BIGINT) AS pooled_count
  FROM ranked
  WHERE value_rank <= :top_values_per_key
),
other_values AS (
  -- every value ranked beyond :top_values_per_key for its workspace + key, rolled into one row so
  -- a long-tail tag key (many distinct values) never blows up the row count. pooled_count is how
  -- many distinct values that row folds in -- the app's own "Other (n)" label needs it (cbLabel,
  -- tab_cost.jsx), same as every other chargeback cut's own is_other row.
  SELECT workspace_id, tag_key, CAST(NULL AS STRING) AS tag_value,
         SUM(cur_usd) AS cur_usd, SUM(prev_usd) AS prev_usd,
         SUM(cur_unpriced_q) AS cur_unpriced_q, SUM(cur_priced_q) AS cur_priced_q,
         SUM(prev_unpriced_q) AS prev_unpriced_q, SUM(prev_priced_q) AS prev_priced_q,
         TRUE AS is_other, FALSE AS is_untagged, COUNT(*) AS pooled_count
  FROM ranked
  WHERE value_rank > :top_values_per_key
  GROUP BY workspace_id, tag_key
),
untagged_values AS (
  SELECT kp.workspace_id, kp.tag_key, CAST(NULL AS STRING) AS tag_value,
         t.cur_total_usd  - COALESCE(kp.cur_present_usd, 0)  AS cur_usd,
         t.prev_total_usd - COALESCE(kp.prev_present_usd, 0) AS prev_usd,
         t.cur_total_unpriced_q  - COALESCE(kp.cur_present_unpriced_q, 0)  AS cur_unpriced_q,
         t.cur_total_priced_q   - COALESCE(kp.cur_present_priced_q, 0)    AS cur_priced_q,
         t.prev_total_unpriced_q - COALESCE(kp.prev_present_unpriced_q, 0) AS prev_unpriced_q,
         t.prev_total_priced_q  - COALESCE(kp.prev_present_priced_q, 0)   AS prev_priced_q,
         FALSE AS is_other, TRUE AS is_untagged, CAST(NULL AS BIGINT) AS pooled_count
  FROM key_present kp
  JOIN ws_totals t ON t.workspace_id IS NOT DISTINCT FROM kp.workspace_id
),
combined AS (
  SELECT * FROM top_values
  UNION ALL SELECT * FROM other_values
  UNION ALL SELECT * FROM untagged_values
),
resolved AS (
  -- same NULL-vs-real-zero resolution cost_period_over_period's own `agg` CTE uses: a side with
  -- NO matching rows at all (its own unpriced quantity is 0 too) is a genuine 0, never NOT_ASSESSED.
  SELECT *,
         CASE WHEN cur_usd  IS NULL AND cur_unpriced_q  = 0 THEN 0 ELSE cur_usd  END AS eff_cur_usd,
         CASE WHEN prev_usd IS NULL AND prev_unpriced_q = 0 THEN 0 ELSE prev_usd END AS eff_prev_usd
  FROM combined
)
SELECT
  r.workspace_id,
  r.tag_key,
  CASE WHEN r.is_other THEN '(other)' WHEN r.is_untagged THEN '(untagged)' ELSE r.tag_value END AS tag_value,
  r.is_other,
  r.is_untagged,
  r.pooled_count,
  ROUND(r.eff_cur_usd, 2)                                                    AS usd_list,
  ROUND(r.eff_cur_usd * 100.0 / NULLIF(t.cur_total_usd, 0), 1)               AS share_of_total_pct,
  ROUND(r.eff_prev_usd, 2)                                                   AS prev_usd_list,
  ROUND(r.eff_cur_usd - r.eff_prev_usd, 2)                                   AS change_usd_list,
  ROUND((r.eff_cur_usd - r.eff_prev_usd) / NULLIF(r.eff_prev_usd, 0) * 100, 1) AS change_pct,
  CASE
    WHEN (r.cur_unpriced_q + r.prev_unpriced_q) > 0 THEN 'unpriced'
    WHEN (r.cur_priced_q + r.prev_priced_q) = 0     THEN 'free'
    ELSE 'priced'
  END                                                                         AS price_basis,
  CASE
    WHEN s.snapshot_start > dateadd(day, -(:period_days * 2), current_date())
      THEN 'NOT_ASSESSED'
    WHEN r.cur_usd IS NULL AND r.cur_unpriced_q > 0 THEN 'NOT_ASSESSED'
    WHEN r.prev_usd IS NULL AND r.prev_unpriced_q > 0 THEN 'NOT_ASSESSED'
    WHEN r.is_other THEN 'OK'
    WHEN COALESCE(r.eff_cur_usd, 0) < :min_spend_usd THEN 'OK'
    WHEN r.eff_prev_usd = 0 THEN 'CRITICAL'
    WHEN (r.eff_cur_usd - r.eff_prev_usd) / NULLIF(r.eff_prev_usd, 0) * 100 >= :crit_increase_pct
      THEN 'CRITICAL'
    WHEN (r.eff_cur_usd - r.eff_prev_usd) / NULLIF(r.eff_prev_usd, 0) * 100 >= :warn_increase_pct
      THEN 'WARN'
    ELSE 'OK'
  END                                                                         AS status,
  CASE
    WHEN s.snapshot_start > dateadd(day, -(:period_days * 2), current_date())
      THEN 'previous_window_not_covered'
    WHEN r.cur_usd IS NULL AND r.cur_unpriced_q > 0 THEN 'current_period_unpriced'
    WHEN r.prev_usd IS NULL AND r.prev_unpriced_q > 0 THEN 'previous_period_unpriced'
    ELSE NULL
  END                                                                         AS not_assessed_reason
FROM resolved r
CROSS JOIN totals t
CROSS JOIN snapshot s
-- output names only: Databricks cannot mix an output alias (status) with a column it did not select
ORDER BY workspace_id, tag_key,
         is_untagged, is_other,
         CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         usd_list DESC NULLS LAST
