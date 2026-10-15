-- query_id: cost_chargeback_by_warehouse
-- title: Chargeback by SQL warehouse, with change vs the previous period
-- domain: cost   tier: standard
-- reads: system.billing.usage, system.billing.list_prices, system.query.history
-- requires: SELECT on system.billing and system.query; GA (system.billing.usage/list_prices are
--   generally available; system.query.history is GA but covers SQL warehouses only - the queries
--   and usd_per_1000_queries columns are NULL/0 for a warehouse with no captured query history)
-- empty_if: ingestion_lag, no_activity
-- params: :period_days (default 30) rolling window in days - the current period is the most
--   recent N days, the previous period the equal-length N days before it; :warn_increase_pct
--   (default 25) percent increase in list-priced spend, current period over previous, at/above
--   which WARN; :crit_increase_pct (default 50) percent increase at/above which CRITICAL;
--   :min_spend_usd (default 20) the current period's own list-priced spend must be at least this
--   before a percent change is judged; :top_n (default 20) how many status=OK, and separately how
--   many status=NOT_ASSESSED, warehouses ranked by current-period usd_list descending are kept as
--   their own row before the rest of that same status are pooled into their own is_other=true row
--   - a real finding (WARN/CRITICAL) is always kept as its own row, never pooled, so it can never
--   be hidden behind the cap. Needs 2x :period_days of billing history in the account for the
--   previous-period comparison; see caveats.
-- confidence: needs_confirmation
-- confidence_note: New app-owned query, not yet run live. Reuses the effective-list-price join
--   every priced cost query in this app already uses (DEC-66.1) and cost_period_over_period's own
--   WARN/CRITICAL band and NOT_ASSESSED coverage guard (DEC-62/DEC-64), at the (workspace_id,
--   warehouse_id) grain; billed_hours is COUNT(DISTINCT usage_start_time) over ORIGINAL billing
--   rows only (one hourly billing-bucket boundary counted once, however many SKU rows - a base-DBU
--   row and a serverless/photon surcharge row for the same hour - bill inside it), never
--   SUM(usage_end_time - usage_start_time), which double-counts a hour with more than one billed
--   SKU row. Confirm on your account that a warehouse you know grew or shrank between two recent
--   equal periods matches change_pct, and that a warehouse's own billed_hours matches its known
--   up-time in whole billed hours.
-- read_this: One row = one SQL warehouse's spend in the selected window against the equal-length
--   window right before it, both at the effective list price (DEC-66.1), plus billed_hours (how
--   many distinct hourly billing buckets it billed something in) and queries/usd_per_1000_queries
--   (a cost-per-query efficiency figure, from system.query.history). usd_list and prev_usd_list are
--   the two totals; change_usd_list and change_pct compare them. price_basis discloses pricing
--   coverage across both windows combined; not_assessed_reason explains a NOT_ASSESSED row.
--   is_other=true marks up to two pooled rows: one for every status=OK warehouse ranked below
--   :top_n by current spend (reads status OK), one for every status=NOT_ASSESSED warehouse ranked
--   below :top_n (reads status NOT_ASSESSED, not_assessed_reason NULL - the pooled rows can carry
--   different reasons); either is present only when pooled_count > 0 for that status
--   (workspace_id/warehouse_id NULL on both). share_of_total_pct still adds up to ~100% across
--   every row including these. Warehouse name/type/size are not resolved here - join
--   sql_warehouse_config_current on warehouse_id.
-- healthy: status OK - no increase at/above :warn_increase_pct, or the row's own current-period
--   spend (a real $0 counts) sits below :min_spend_usd.
-- investigate_if: CRITICAL - the increase is at/above :crit_increase_pct on at least
--   :min_spend_usd of current spend, or brand-new spend above the floor with nothing in the
--   previous period to compare against. WARN - at/above :warn_increase_pct. NOT_ASSESSED - read
--   not_assessed_reason: 'previous_window_not_covered' when the account's own earliest recorded
--   usage does not reach back far enough to cover the previous window at this window length;
--   'current_period_unpriced' / 'previous_period_unpriced' when that side's whole spend could not
--   be priced at all. A row with is_other=true is always OK or NOT_ASSESSED, never WARN/CRITICAL
--   (see caveats).
-- actions: 1) open sql_warehouse_config_current for this warehouse_id to see its size, auto-stop
--   and channel (free); 2) open compute_warehouse_idle_gaps for the same warehouse to see whether
--   the spend increase tracks more real query load or more idle running time (free); 3) if the
--   growth is deliberate, right-size or scale down the warehouse's config, or set a budget policy,
--   before it compounds another period (config, or spend if intentional).
-- not_assessed_reasons: previous_window_not_covered: the snapshot does not reach back far enough
--   to cover the previous period; current_period_unpriced: this period's spend could not be
--   priced; previous_period_unpriced: the previous period's spend could not be priced
-- next: sql_warehouse_config_current (to resolve warehouse_id to a name, size and config),
--   compute_warehouse_idle_gaps (for this warehouse's own idle-time detail), cost_period_over_period
--   (for the same window cut by workspace and product line instead)
-- caveats: DEC-66.1 - the effective list price only, never a negotiated rate; the same basis on
--   both windows. DEC-62 - both windows are measured, never projected or scaled. This needs the
--   account's own billing history to reach back 2x :period_days for a real previous-period
--   comparison (DEC-64); short of that, every individual warehouse row reads NOT_ASSESSED
--   (previous_window_not_covered) - the NOT_ASSESSED is_other row then pools all but the top
--   :top_n of them by current spend, same as an OK list would. POOLING - the OK is_other row only
--   ever pools warehouses that were ALREADY status=OK on their own, and always reads status OK
--   itself, however large the combined dollars; the NOT_ASSESSED is_other row only ever pools
--   warehouses that were ALREADY status=NOT_ASSESSED. Neither can hide a WARN/CRITICAL warehouse,
--   which always keeps its own row regardless of :top_n. billed_hours/queries on either is_other
--   row are the SUM of the pooled warehouses' own billed_hours/queries (a plain total, not a
--   deduplicated hour count across warehouses). price_basis is computed over BOTH windows combined - 'unpriced' when any
--   non-free-usage SKU in either window had no matching list_prices row, 'free' when every matched
--   SKU is a FREE_USAGE SKU, 'priced' otherwise. Corrections are netted (SUM across all
--   record_types - never filter to ORIGINAL only, except billed_hours which is ORIGINAL-only by
--   design - see confidence_note). The current day is excluded from both windows, since
--   billing.usage lands with ingestion lag and the trailing day is provisional.
WITH snapshot AS (
  SELECT MIN(usage_date) AS snapshot_start
  FROM system.billing.usage
),
priced AS (
  SELECT u.workspace_id, u.usage_metadata.warehouse_id AS warehouse_id, u.usage_date,
         u.record_type, u.usage_start_time, u.sku_name,
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
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.warehouse_id IS NOT NULL
    AND u.usage_date >= dateadd(day, -(:period_days * 2), current_date())
    AND u.usage_date <  current_date()
),
raw_agg AS (
  SELECT workspace_id, warehouse_id,
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
                  THEN usage_quantity ELSE 0 END)                            AS previous_priced_quantity,
         COUNT(DISTINCT CASE WHEN record_type = 'ORIGINAL'
                               AND usage_date >= dateadd(day, -:period_days, current_date())
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
  FROM system.query.history
  WHERE compute.warehouse_id IS NOT NULL
    AND start_time >= CAST(dateadd(day, -:period_days, current_date()) AS TIMESTAMP)
    AND start_time <  current_date()
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
  -- OK and NOT_ASSESSED warehouses are both poolable - on an account short of 2x :period_days of
  -- billing history, every warehouse reads NOT_ASSESSED, and that list needs the same cap an OK
  -- list gets.
  -- ranked PER STATUS, never combined - a NOT_ASSESSED row's usd_list is always NULL (sorted
  -- last), so ranking both statuses together would push every NOT_ASSESSED row past :top_n as
  -- soon as 20+ OK rows existed anywhere, regardless of how few NOT_ASSESSED rows there are.
  SELECT scored.*,
         ROW_NUMBER() OVER (PARTITION BY status ORDER BY usd_list DESC NULLS LAST, warehouse_id) AS rn
  FROM scored
  WHERE status IN ('OK', 'NOT_ASSESSED')
),
poolable_kept AS (
  -- The top :top_n OK/NOT_ASSESSED warehouses by current spend, kept as their own row.
  SELECT workspace_id, warehouse_id, FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
         billed_hours, queries, usd_per_1000_queries, usd_list, share_of_total_pct, prev_usd_list,
         change_usd_list, change_pct, price_basis, status, not_assessed_reason
  FROM poolable_ranked
  WHERE rn <= :top_n
),
poolable_pooled_raw AS (
  -- Every remaining OK/NOT_ASSESSED warehouse beyond :top_n - re-aggregated below into one
  -- is_other row per status (OK rows and NOT_ASSESSED rows are never combined into one row).
  SELECT * FROM poolable_ranked WHERE rn > :top_n
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
    -- raise :top_n to see them individually.
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
