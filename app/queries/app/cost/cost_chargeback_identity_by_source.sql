-- query_id: cost_chargeback_identity_by_source
-- title: Chargeback by identity (user or service principal), split by source, with change vs the
--   previous period
-- domain: cost   tier: standard
-- reads: system.billing.usage, system.billing.list_prices, system.query.history,
--   system.compute.clusters
-- requires: SELECT on system.billing, system.query and system.compute; GA (system.billing.usage/
--   list_prices and system.compute.clusters are generally available; system.query.history is GA
--   but SCOPE: SQL warehouses only - classic, pro and serverless. Serverless compute for notebooks
--   and jobs has no warehouse_id and is not covered by the sql_warehouse source's duration split)
-- empty_if: ingestion_lag, no_activity
-- params: :period_days (default 30) rolling window in days; :warn_increase_pct (default 25)
--   percent increase in list-priced spend, current period over previous, at/above which WARN;
--   :crit_increase_pct (default 50) percent increase at/above which CRITICAL; :min_spend_usd
--   (default 20) the current period's own list-priced spend must be at least this before a percent
--   change is judged; :top_n (default 20) how many status=OK, and separately how many
--   status=NOT_ASSESSED, (identity, source[, warehouse]) rows ranked by current-period usd_list
--   descending are kept as their own row before the rest of that same status are pooled into their
--   own is_other=true row - a real finding (WARN/CRITICAL) is always kept as its own row, never
--   pooled. Needs 2x :period_days of billing history in the account for the previous-period
--   comparison; see caveats.
-- confidence: needs_confirmation
-- confidence_note: New app-owned query, not yet run live. Splits spend across three sources that
--   never overlap so nothing is double-counted: sql_warehouse (a SQL warehouse's DBU cost split
--   across identities by their share of that warehouse-day's SUMMED system.query.history
--   total_duration_ms, keeping workspace_id/warehouse_id as its own dimension), jobs (billed
--   jobs-compute DBU usage, identity via COALESCE(owned_by, run_as, 'unknown')), and other
--   (everything else - serverless/notebook/all-purpose-interactive - identity via
--   COALESCE(owned_by, run_as, the cluster's own latest owned_by for an all-purpose cluster_id,
--   'unknown')). Reuses the effective-list-price join every priced cost query in this app already
--   uses (DEC-66.1) and cost_period_over_period's own WARN/CRITICAL band (DEC-62); the
--   NOT_ASSESSED pricing-coverage gate is computed once per SOURCE (not per identity - see
--   caveats), unlike the other cost_chargeback_by_* checks in this app, which compute it per row.
--   Confirm on your account: an identity you know grew spend between two recent equal periods
--   matches change_pct, and that all-purpose-cluster usage (identity_metadata.run_as blank there
--   per Databricks' own documentation) resolves through owned_by or the cluster fallback instead of
--   collapsing to 'unknown'.
-- read_this: One row = one (identity_run_as, source) - PLUS workspace_id/warehouse_id on
--   sql_warehouse rows - with attributed spend in the current window against the equal-length
--   window right before it. There is no separate 'total' row per identity: sum this query's own
--   rows for a given identity_run_as yourself if you want one. identity_run_as is the RAW,
--   UNMASKED principal (unlike cost_chargeback_by_identity's hashed run_as) - this is a raw
--   chargeback/billing extract, meant to run under the same SELECT on system.billing a masked
--   report would also need. usd_list/prev_usd_list are the two totals; change_usd_list and
--   change_pct compare them. is_other=true marks up to two pooled rows: one for every status=OK
--   row ranked below :top_n by current spend (reads status OK), one for every status=NOT_ASSESSED
--   row ranked below :top_n (reads status NOT_ASSESSED, not_assessed_reason NULL); either is
--   present only when pooled_count > 0 for that status (identity_run_as/source/workspace_id/
--   warehouse_id all NULL on both). share_of_total_pct still adds up to ~100% including these.
--   Warehouse name is not resolved here - join sql_warehouse_config_current on warehouse_id.
-- healthy: status OK - no increase at/above :warn_increase_pct, or the row's own current-period
--   spend (a real $0 counts) sits below :min_spend_usd, and its source's pricing coverage is not a
--   full gap (see investigate_if).
-- investigate_if: CRITICAL - the increase is at/above :crit_increase_pct on at least
--   :min_spend_usd of current spend, or brand-new spend above the floor with nothing in the
--   previous period to compare against - a growing 'unknown' identity is judged the same way, so a
--   widening attribution gap shows up as a real WARN/CRITICAL. WARN - at/above
--   :warn_increase_pct. NOT_ASSESSED - read not_assessed_reason: 'previous_window_not_covered'
--   when the account's own earliest recorded usage does not reach back far enough to cover the
--   previous window at this window length (every row, every source); 'current_period_unpriced' /
--   'previous_period_unpriced' when that ROW's OWN SOURCE had no priced spend at all, account-wide,
--   on that side of the window - every row of that source reads NOT_ASSESSED together (see
--   caveats). A row with is_other=true is always OK or NOT_ASSESSED, never WARN/CRITICAL.
-- actions: 1) open cost_chargeback_by_allocation_tag or the Cost > Allocation tab to see which
--   cost_center/team this identity's spend rolls up under (free); 2) for a sql_warehouse row, open
--   sql_warehouse_config_current for warehouse_id to see the warehouse's own size/config (free);
--   3) if the growth is deliberate, confirm the identity still needs that level of access/spend, or
--   set a budget policy, before it compounds another period (config, or spend if intentional).
-- not_assessed_reasons: previous_window_not_covered: the snapshot does not reach back far enough
--   to cover the previous period; current_period_unpriced: this row's own source had no priced
--   spend at all, account-wide, in the current window; previous_period_unpriced: same, for the
--   previous window
-- next: cost_chargeback_by_identity (the masked, day-level cut of the same identities),
--   sql_warehouse_config_current (to resolve a sql_warehouse row's warehouse_id),
--   cost_chargeback_by_allocation_tag (the cost_center/team cut of the same spend)
-- caveats: MASKING - no masking is applied here (unlike cost_chargeback_by_identity's hash): a
--   chargeback-by-person report is meaningless once the person is hashed, so this is a raw extract
--   for FinOps/billing use, gated on the same SELECT on system.billing a masked report would also
--   need; the app's own optional display-time user-masking setting (off by default) still applies
--   when it renders this query's identity_run_as/identity_type columns, exactly as it would any
--   other user-identity column. SHARE OF BUSY TIME (sql_warehouse source) - each identity's share
--   of a warehouse-day is its share of that day's SUMMED query.history total_duration_ms
--   (concurrent queries are not merged into wall-clock time) - a proportional-use estimate, not a
--   wall-clock idle/busy split (see compute_warehouse_idle_minutes for that); a warehouse-day with
--   billed dollars but no recorded query at all attributes that whole day to 'unknown' rather than
--   dropping it. PRICING GATE - price_basis and the current_period_unpriced/previous_period_unpriced
--   NOT_ASSESSED reasons are computed ONCE PER SOURCE (sql_warehouse/jobs/other), from that
--   source's own raw usage rows, BEFORE any identity split - never per identity - because
--   splitting an already-summed unpriced/priced quantity by duration share would misrepresent it;
--   every row of one source therefore shares that source's own price_basis and NOT_ASSESSED gate,
--   the same design cost_chargeback_by_identity's three-source split already used, now extended to
--   also gate the PREVIOUS window (the earlier version of this check only checked the current
--   window, which could misread a genuinely unpriced previous period as brand-new CRITICAL spend).
--   POOLING - the OK is_other row only ever pools rows that were ALREADY status=OK on their own,
--   and always reads status OK itself, however large the combined dollars; the NOT_ASSESSED
--   is_other row only ever pools rows that were ALREADY status=NOT_ASSESSED (its own
--   not_assessed_reason is NULL - the pooled rows can carry different reasons; raise :top_n to see
--   them individually). Neither can hide a WARN/CRITICAL row. Each one's own price_basis is
--   'unpriced' if any pooled row's own source was partially unpriced, 'free' only if every pooled
--   row was 'free', else 'priced'. Corrections are netted (SUM across all record_types - never filter to ORIGINAL
--   only). The current day is excluded from every window, since billing.usage lands with ingestion
--   lag and the trailing day is provisional. This is account-wide (no separate per-workspace
--   identity total) except that sql_warehouse rows keep their own workspace_id/warehouse_id.
WITH snapshot AS (
  SELECT MIN(usage_date) AS snapshot_start
  FROM system.billing.usage
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
  FROM system.billing.list_prices
),
latest_clusters AS (
  SELECT workspace_id, cluster_id, owned_by
  FROM system.compute.clusters
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC) = 1
),
-- ===== source: sql_warehouse =====
wh_day AS (
  SELECT u.workspace_id, u.usage_metadata.warehouse_id AS warehouse_id, u.usage_date,
         SUM(u.usage_quantity * p.list_rate) AS usd_list,
         SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN u.usage_quantity ELSE 0 END) AS unpriced_q,
         SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) AS priced_q
  FROM system.billing.usage u
  LEFT JOIN price p
    ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.warehouse_id IS NOT NULL
    AND u.usage_date >= dateadd(day, -(:period_days * 2), current_date())
    AND u.usage_date <  current_date()
  GROUP BY u.workspace_id, u.usage_metadata.warehouse_id, u.usage_date
),
wh_identity_duration AS (
  SELECT q.workspace_id, q.compute.warehouse_id AS warehouse_id, DATE(q.start_time) AS usage_date,
         COALESCE(q.executed_by, 'unknown') AS identity_run_as,
         SUM(COALESCE(q.total_duration_ms, 0)) AS duration_ms
  FROM system.query.history q
  WHERE q.compute.warehouse_id IS NOT NULL
    AND q.start_time >= CAST(dateadd(day, -(:period_days * 2), current_date()) AS TIMESTAMP)
    AND q.start_time <  current_date()
  GROUP BY q.workspace_id, q.compute.warehouse_id, DATE(q.start_time), COALESCE(q.executed_by, 'unknown')
),
wh_day_total_duration AS (
  SELECT workspace_id, warehouse_id, usage_date, SUM(duration_ms) AS total_duration_ms
  FROM wh_identity_duration
  GROUP BY workspace_id, warehouse_id, usage_date
),
wh_split AS (
  -- one row per warehouse-day-identity: the day's dollars times that identity's duration share, or
  -- (when the day has no recorded query duration at all) the whole day's dollars under 'unknown'.
  SELECT d.workspace_id, d.warehouse_id, d.usage_date,
         COALESCE(i.identity_run_as, 'unknown') AS identity_run_as,
         CASE WHEN t.total_duration_ms > 0 THEN d.usd_list * i.duration_ms / t.total_duration_ms
              ELSE d.usd_list END AS usd_list
  FROM wh_day d
  LEFT JOIN wh_day_total_duration t
    ON t.workspace_id = d.workspace_id AND t.warehouse_id = d.warehouse_id AND t.usage_date = d.usage_date
  LEFT JOIN wh_identity_duration i
    ON  i.workspace_id = d.workspace_id AND i.warehouse_id = d.warehouse_id AND i.usage_date = d.usage_date
    AND t.total_duration_ms > 0
),
wh_by_identity AS (
  SELECT workspace_id, warehouse_id, identity_run_as,
         SUM(CASE WHEN usage_date >= dateadd(day, -:period_days, current_date()) THEN usd_list END) AS cur_usd_list,
         SUM(CASE WHEN usage_date <  dateadd(day, -:period_days, current_date()) THEN usd_list END) AS prev_usd_list
  FROM wh_split
  GROUP BY workspace_id, warehouse_id, identity_run_as
),
wh_gate AS (
  SELECT
    (SUM(CASE WHEN usage_date >= dateadd(day, -:period_days, current_date()) THEN usd_list END) IS NULL
     AND SUM(CASE WHEN usage_date >= dateadd(day, -:period_days, current_date()) THEN unpriced_q ELSE 0 END) > 0) AS current_period_unpriced,
    (SUM(CASE WHEN usage_date <  dateadd(day, -:period_days, current_date()) THEN usd_list END) IS NULL
     AND SUM(CASE WHEN usage_date <  dateadd(day, -:period_days, current_date()) THEN unpriced_q ELSE 0 END) > 0) AS previous_period_unpriced,
    CASE
      WHEN SUM(unpriced_q) > 0 THEN 'unpriced'
      WHEN SUM(priced_q) = 0   THEN 'free'
      ELSE 'priced'
    END AS price_basis
  FROM wh_day
),
-- ===== source: jobs =====
jobs_usage AS (
  SELECT COALESCE(u.identity_metadata.owned_by, u.identity_metadata.run_as, 'unknown') AS identity_run_as,
         u.usage_date,
         u.usage_quantity * p.list_rate AS usd_list,
         CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
              THEN u.usage_quantity ELSE 0 END AS unpriced_q,
         CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END AS priced_q
  FROM system.billing.usage u
  LEFT JOIN price p
    ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.job_id IS NOT NULL
    AND u.usage_date >= dateadd(day, -(:period_days * 2), current_date())
    AND u.usage_date <  current_date()
),
jobs_by_identity AS (
  SELECT identity_run_as,
         SUM(CASE WHEN usage_date >= dateadd(day, -:period_days, current_date()) THEN usd_list END) AS cur_usd_list,
         SUM(CASE WHEN usage_date <  dateadd(day, -:period_days, current_date()) THEN usd_list END) AS prev_usd_list
  FROM jobs_usage
  GROUP BY identity_run_as
),
jobs_gate AS (
  SELECT
    (SUM(CASE WHEN usage_date >= dateadd(day, -:period_days, current_date()) THEN usd_list END) IS NULL
     AND SUM(CASE WHEN usage_date >= dateadd(day, -:period_days, current_date()) THEN unpriced_q ELSE 0 END) > 0) AS current_period_unpriced,
    (SUM(CASE WHEN usage_date <  dateadd(day, -:period_days, current_date()) THEN usd_list END) IS NULL
     AND SUM(CASE WHEN usage_date <  dateadd(day, -:period_days, current_date()) THEN unpriced_q ELSE 0 END) > 0) AS previous_period_unpriced,
    CASE
      WHEN SUM(unpriced_q) > 0 THEN 'unpriced'
      WHEN SUM(priced_q) = 0   THEN 'free'
      ELSE 'priced'
    END AS price_basis
  FROM jobs_usage
),
-- ===== source: other (no warehouse_id and no job_id -- serverless, notebooks, all-purpose, etc.) =====
other_usage AS (
  SELECT COALESCE(u.identity_metadata.owned_by, u.identity_metadata.run_as, lc.owned_by, 'unknown') AS identity_run_as,
         u.usage_date,
         u.usage_quantity * p.list_rate AS usd_list,
         CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
              THEN u.usage_quantity ELSE 0 END AS unpriced_q,
         CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END AS priced_q
  FROM system.billing.usage u
  LEFT JOIN price p
    ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  LEFT JOIN latest_clusters lc
    ON  lc.workspace_id = u.workspace_id AND lc.cluster_id = u.usage_metadata.cluster_id
  WHERE u.usage_metadata.warehouse_id IS NULL
    AND u.usage_metadata.job_id IS NULL
    AND u.usage_date >= dateadd(day, -(:period_days * 2), current_date())
    AND u.usage_date <  current_date()
),
other_by_identity AS (
  SELECT identity_run_as,
         SUM(CASE WHEN usage_date >= dateadd(day, -:period_days, current_date()) THEN usd_list END) AS cur_usd_list,
         SUM(CASE WHEN usage_date <  dateadd(day, -:period_days, current_date()) THEN usd_list END) AS prev_usd_list
  FROM other_usage
  GROUP BY identity_run_as
),
other_gate AS (
  SELECT
    (SUM(CASE WHEN usage_date >= dateadd(day, -:period_days, current_date()) THEN usd_list END) IS NULL
     AND SUM(CASE WHEN usage_date >= dateadd(day, -:period_days, current_date()) THEN unpriced_q ELSE 0 END) > 0) AS current_period_unpriced,
    (SUM(CASE WHEN usage_date <  dateadd(day, -:period_days, current_date()) THEN usd_list END) IS NULL
     AND SUM(CASE WHEN usage_date <  dateadd(day, -:period_days, current_date()) THEN unpriced_q ELSE 0 END) > 0) AS previous_period_unpriced,
    CASE
      WHEN SUM(unpriced_q) > 0 THEN 'unpriced'
      WHEN SUM(priced_q) = 0   THEN 'free'
      ELSE 'priced'
    END AS price_basis
  FROM other_usage
),
combined AS (
  SELECT identity_run_as, 'sql_warehouse' AS source, workspace_id, warehouse_id,
         COALESCE(cur_usd_list, 0) AS cur_raw, COALESCE(prev_usd_list, 0) AS prev_raw
  FROM wh_by_identity
  UNION ALL
  SELECT identity_run_as, 'jobs' AS source, CAST(NULL AS STRING) AS workspace_id, CAST(NULL AS STRING) AS warehouse_id,
         COALESCE(cur_usd_list, 0), COALESCE(prev_usd_list, 0)
  FROM jobs_by_identity
  UNION ALL
  SELECT identity_run_as, 'other' AS source, CAST(NULL AS STRING) AS workspace_id, CAST(NULL AS STRING) AS warehouse_id,
         COALESCE(cur_usd_list, 0), COALESCE(prev_usd_list, 0)
  FROM other_by_identity
),
total AS (
  SELECT SUM(cur_raw) AS total_usd_list FROM combined
),
scored AS (
  SELECT c.identity_run_as,
         CASE
           WHEN c.identity_run_as = 'unknown' THEN 'unknown'
           WHEN c.identity_run_as LIKE '%@%'   THEN 'user'
           ELSE 'service_principal'
         END AS identity_type,
         c.source, c.workspace_id, c.warehouse_id,
         c.cur_raw, c.prev_raw,
         ROUND(c.cur_raw, 2)                                          AS usd_list,
         ROUND(c.cur_raw * 100.0 / NULLIF(t.total_usd_list, 0), 1)    AS share_of_total_pct,
         ROUND(c.prev_raw, 2)                                         AS prev_usd_list,
         ROUND(c.cur_raw - c.prev_raw, 2)                             AS change_usd_list,
         ROUND((c.cur_raw - c.prev_raw) / NULLIF(c.prev_raw, 0) * 100, 1) AS change_pct,
         CASE c.source
           WHEN 'sql_warehouse' THEN wg.price_basis
           WHEN 'jobs'          THEN jg.price_basis
           WHEN 'other'         THEN og.price_basis
         END AS price_basis,
         CASE
           WHEN s.snapshot_start > dateadd(day, -(:period_days * 2), current_date()) THEN 'NOT_ASSESSED'
           WHEN c.source = 'sql_warehouse' AND (wg.current_period_unpriced OR wg.previous_period_unpriced) THEN 'NOT_ASSESSED'
           WHEN c.source = 'jobs'          AND (jg.current_period_unpriced OR jg.previous_period_unpriced) THEN 'NOT_ASSESSED'
           WHEN c.source = 'other'         AND (og.current_period_unpriced OR og.previous_period_unpriced) THEN 'NOT_ASSESSED'
           WHEN COALESCE(c.cur_raw, 0) < :min_spend_usd THEN 'OK'
           WHEN c.prev_raw = 0                          THEN 'CRITICAL'
           WHEN (c.cur_raw - c.prev_raw) / NULLIF(c.prev_raw, 0) * 100 >= :crit_increase_pct THEN 'CRITICAL'
           WHEN (c.cur_raw - c.prev_raw) / NULLIF(c.prev_raw, 0) * 100 >= :warn_increase_pct  THEN 'WARN'
           ELSE 'OK'
         END AS status,
         CASE
           WHEN s.snapshot_start > dateadd(day, -(:period_days * 2), current_date()) THEN 'previous_window_not_covered'
           WHEN c.source = 'sql_warehouse' AND wg.current_period_unpriced  THEN 'current_period_unpriced'
           WHEN c.source = 'jobs'          AND jg.current_period_unpriced  THEN 'current_period_unpriced'
           WHEN c.source = 'other'         AND og.current_period_unpriced THEN 'current_period_unpriced'
           WHEN c.source = 'sql_warehouse' AND wg.previous_period_unpriced  THEN 'previous_period_unpriced'
           WHEN c.source = 'jobs'          AND jg.previous_period_unpriced  THEN 'previous_period_unpriced'
           WHEN c.source = 'other'         AND og.previous_period_unpriced THEN 'previous_period_unpriced'
           ELSE NULL
         END AS not_assessed_reason
  FROM combined c
  CROSS JOIN snapshot s
  CROSS JOIN total t
  CROSS JOIN wh_gate wg
  CROSS JOIN jobs_gate jg
  CROSS JOIN other_gate og
),
flagged AS (
  -- A real finding (WARN/CRITICAL) always keeps its own row - never pooled.
  SELECT identity_run_as, identity_type, source, workspace_id, warehouse_id,
         FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
         usd_list, share_of_total_pct, prev_usd_list, change_usd_list, change_pct,
         price_basis, status, not_assessed_reason
  FROM scored
  WHERE status IN ('WARN', 'CRITICAL')
),
poolable_ranked AS (
  -- OK and NOT_ASSESSED rows are both poolable - on an account short of 2x :period_days of billing
  -- history, every row reads NOT_ASSESSED, and that list needs the same cap an OK list gets.
  -- ranked PER STATUS, never combined - a NOT_ASSESSED row's usd_list is always NULL (sorted
  -- last), so ranking both statuses together would push every NOT_ASSESSED row past :top_n as
  -- soon as 20+ OK rows existed anywhere, regardless of how few NOT_ASSESSED rows there are.
  SELECT scored.*,
         ROW_NUMBER() OVER (PARTITION BY status ORDER BY usd_list DESC NULLS LAST, identity_run_as, source) AS rn
  FROM scored
  WHERE status IN ('OK', 'NOT_ASSESSED')
),
poolable_kept AS (
  -- The top :top_n OK/NOT_ASSESSED rows by current spend, kept as their own row.
  SELECT identity_run_as, identity_type, source, workspace_id, warehouse_id,
         FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
         usd_list, share_of_total_pct, prev_usd_list, change_usd_list, change_pct,
         price_basis, status, not_assessed_reason
  FROM poolable_ranked
  WHERE rn <= :top_n
),
poolable_pooled_raw AS (
  -- Every remaining OK/NOT_ASSESSED row beyond :top_n - re-aggregated below into one is_other row
  -- per status (OK rows and NOT_ASSESSED rows are never combined into one row).
  SELECT * FROM poolable_ranked WHERE rn > :top_n
),
other_ok_row AS (
  SELECT
    CAST(NULL AS STRING) AS identity_run_as,
    'other'               AS identity_type,
    CAST(NULL AS STRING) AS source,
    CAST(NULL AS STRING) AS workspace_id,
    CAST(NULL AS STRING) AS warehouse_id,
    TRUE                  AS is_other,
    COUNT(*)              AS pooled_count,
    ROUND(SUM(cur_raw), 2)                                              AS usd_list,
    ROUND(SUM(cur_raw) * 100.0 / NULLIF(MAX(t.total_usd_list), 0), 1)   AS share_of_total_pct,
    ROUND(SUM(prev_raw), 2)                                             AS prev_usd_list,
    ROUND(SUM(cur_raw) - SUM(prev_raw), 2)                              AS change_usd_list,
    ROUND((SUM(cur_raw) - SUM(prev_raw)) / NULLIF(SUM(prev_raw), 0) * 100, 1) AS change_pct,
    CASE
      WHEN SUM(CASE WHEN price_basis = 'unpriced' THEN 1 ELSE 0 END) > 0 THEN 'unpriced'
      WHEN SUM(CASE WHEN price_basis = 'priced'   THEN 1 ELSE 0 END) = 0 THEN 'free'
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
    CAST(NULL AS STRING) AS identity_run_as,
    'other'               AS identity_type,
    CAST(NULL AS STRING) AS source,
    CAST(NULL AS STRING) AS workspace_id,
    CAST(NULL AS STRING) AS warehouse_id,
    TRUE                  AS is_other,
    COUNT(*)              AS pooled_count,
    ROUND(SUM(cur_raw), 2)                                              AS usd_list,
    ROUND(SUM(cur_raw) * 100.0 / NULLIF(MAX(t.total_usd_list), 0), 1)   AS share_of_total_pct,
    ROUND(SUM(prev_raw), 2)                                             AS prev_usd_list,
    ROUND(SUM(cur_raw) - SUM(prev_raw), 2)                              AS change_usd_list,
    ROUND((SUM(cur_raw) - SUM(prev_raw)) / NULLIF(SUM(prev_raw), 0) * 100, 1) AS change_pct,
    CASE
      WHEN SUM(CASE WHEN price_basis = 'unpriced' THEN 1 ELSE 0 END) > 0 THEN 'unpriced'
      WHEN SUM(CASE WHEN price_basis = 'priced'   THEN 1 ELSE 0 END) = 0 THEN 'free'
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
         identity_run_as, source
