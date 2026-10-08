-- query_id: storage_unused_table_cost
-- title: Unused tables - storage and upkeep cost
-- domain: storage   tier: standard
-- reads: system.information_schema.tables, system.access.table_lineage,
--   system.storage.table_metrics_history, system.storage.predictive_optimization_operations_history,
--   system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.information_schema, system.access, system.storage and system.billing;
--   system.access.table_lineage is GA; information_schema requires Unity Catalog;
--   system.storage.table_metrics_history and system.storage.predictive_optimization_operations_history
--   must be enabled for this metastore - if either is not, this check errors and reads not assessed
--   (system table not enabled) until it is turned on
-- empty_if: schema_not_enabled, po_not_enabled, lineage_inference_only, retention_window, privilege_scoped
-- params: :period_days (default 30) rolling window in days for the lineage-source rule (same rule
--   as access_dead_table_candidates) and for the Predictive Optimization upkeep window;
--   :storage_usd_per_gb_month (default 0.02) declared $/GB-month used to estimate the cloud
--   storage bill for a table's active bytes - roughly Azure hot-tier blob storage, an estimate of
--   the cloud storage bill, never a Databricks billing figure, and changeable in
--   config/thresholds.yml; :crit_usd_month (default 100) est_total_usd_month at/above which
--   CRITICAL; :warn_usd_month (default 20) at/above which WARN; :top_n (default 500) largest
--   tables by est_total_usd_month kept as their own row before the rest are pooled into one
--   is_other=true row
-- confidence: needs_confirmation
-- confidence_note: New app-owned query, not yet run live. The dead-table rule (a MANAGED/EXTERNAL
--   table that never appears as a lineage SOURCE in the window) is the exact same rule
--   access_dead_table_candidates already uses. The Predictive Optimization upkeep $ is new: that
--   history table carries no sku_name of its own to join to list_prices per row, so this query
--   derives one account-wide $/DBU rate from system.billing.usage rows with
--   billing_origin_product = 'PREDICTIVE_OPTIMIZATION' in the same window (still the same
--   effective-list-price join every priced check here uses, DEC-66.1) and applies that one rate to
--   each table's own DBU count. Confirm on your account that a table you know is unused shows up
--   here, that its active_gb matches a manual DESCRIBE DETAIL, and that a table with real
--   Predictive Optimization activity in the window shows a non-zero po_upkeep_usd_window.
-- read_this: One row is one MANAGED or EXTERNAL table that never appeared as a lineage source in
--   the window, priced. It was never read (the same candidate list access_dead_table_candidates
--   produces). active_gb is that table's latest active-bytes snapshot inside the window;
--   est_storage_usd_month prices it at :storage_usd_per_gb_month. po_upkeep_usd_window is its own
--   Predictive Optimization DBUs in the window at the blended account-wide list rate (price_basis
--   discloses whether that rate could be derived at all, or reads no_size for a table with no
--   size on record). est_total_usd_month is
--   est_storage_usd_month plus the upkeep scaled from the window to a 30-day month - the one
--   number to sort or budget on. days_since_altered / days_since_last_write read the table's own
--   last DDL change and the last time anything wrote to it (a lineage TARGET) on record, NULL when
--   neither ever happened. Only the largest :top_n tables by est_total_usd_month keep their own
--   row; is_other=true marks a single pooled row for the rest (workspace-free columns all NULL,
--   pooled_count = how many), and always reads status=OK - see caveats. A table with no size on
--   record is still listed, status OK and price_basis no_size: unused, but not priced.
-- healthy: status OK - est_total_usd_month below :warn_usd_month. This list is already filtered
--   to unused tables, so a low-dollar OK row, not an empty result, is the healthy signal to look for.
-- investigate_if: CRITICAL - est_total_usd_month at/above :crit_usd_month. WARN - at/above
--   :warn_usd_month. NOT_ASSESSED - read not_assessed_reason: 'po_upkeep_unpriced' when the table
--   has real Predictive Optimization DBUs in the window but the account had no priced
--   PREDICTIVE_OPTIMIZATION usage to derive a rate from (upkeep, and so the total, unknown).
-- actions: 1) cross-check the table against system.access.audit, query.history and a manual
--   DESCRIBE DETAIL before touching anything, same as access_dead_table_candidates (free); 2) if
--   genuinely unused, drop it (or move it to a to-be-deleted schema for a cooling-off period) to
--   stop paying both its storage and its upkeep every month (spend); 3) if it must be kept, turn
--   off Predictive Optimization on it specifically to stop the upkeep $ while leaving the storage
--   $ alone (config).
-- not_assessed_reasons: po_upkeep_unpriced: this table
--   has Predictive Optimization activity in the window but the account-wide rate could not be
--   priced - the upkeep and total dollars are unknown, not zero
-- next: access_dead_table_candidates (the same candidate list without the dollars),
--   storage_po_coverage (is Predictive Optimization even on for this table's catalog),
--   po_maintenance_cost_by_table (this table's own Predictive Optimization operations and any
--   FAILED ones)
-- caveats: Inherits every caveat access_dead_table_candidates documents for the dead-table rule
--   itself (lineage undercounts reads with no captured source; a candidate to cross-check, never a
--   DROP recommendation on its own; system/information_schema excluded by exact name; a
--   view-mediated read still counts as a source read). SIZE - active_gb is the latest snapshot
--   inside :period_days, not the true current size if the table has not been snapshotted
--   recently; a table dropped before the window's end is excluded, same as storage_growth.
--   UPKEEP RATE - po_upkeep_usd_window prices every table's own DBUs at ONE blended account-wide
--   $/DBU rate (see confidence_note), not a per-table price - two tables with the same DBU count
--   always get the same upkeep dollars. SCALING - est_total_usd_month scales the window's own
--   upkeep dollars to 30 days (upkeep * 30 / :period_days); it never rescales or forecasts
--   est_storage_usd_month, which is already a monthly rate. POOLING - is_other=true always reads
--   status=OK, however large the combined dollars, and only ever pools rows that were already
--   priced (never a WARN/CRITICAL row, and never a NOT_ASSESSED or no_size row, which always keeps
--   its own row regardless of :top_n since there is no dollar figure to pool it on). table_owner is raw
--   unless privacy.mask_user_identities is on (off by default), same as
--   access_dead_table_candidates. The current day is excluded from every window here, same as
--   every other check in this app.
WITH lineage_window AS (
  SELECT *
  FROM system.access.table_lineage
  WHERE event_date >= dateadd(day, -:period_days, current_date())
    AND event_date < current_date()
),
-- Every table that appeared as a lineage SOURCE in the window - same rule (and same reasoning
-- for keeping every direct_access value) as access_dead_table_candidates.
source_tables AS (
  SELECT DISTINCT
         source_table_catalog AS catalog,
         source_table_schema  AS schema,
         source_table_name    AS name
  FROM lineage_window
  WHERE source_table_name IS NOT NULL
),
-- The last time anything wrote to each table (a lineage TARGET) on record - not window-bound
-- like source_tables above, since "how long has nobody even written to it" only gets more
-- meaningful the further back it reaches.
last_write AS (
  SELECT target_table_catalog AS catalog,
         target_table_schema  AS schema,
         target_table_name    AS name,
         MAX(event_time)      AS last_write_time
  FROM system.access.table_lineage
  WHERE target_table_name IS NOT NULL
  GROUP BY target_table_catalog, target_table_schema, target_table_name
),
inventory AS (
  SELECT table_catalog, table_schema, table_name, table_type, table_owner, last_altered
  FROM system.information_schema.tables
  WHERE table_catalog <> 'system'
    AND table_schema <> 'information_schema'
    AND table_type IN ('MANAGED', 'EXTERNAL')
),
unused AS (
  SELECT inv.*
  FROM inventory inv
  LEFT JOIN source_tables src
    ON  inv.table_catalog = src.catalog
    AND inv.table_schema  = src.schema
    AND inv.table_name    = src.name
  WHERE src.name IS NULL          -- never appeared as a lineage source in the window
),
-- Latest active-bytes snapshot per table inside the window - information_schema.tables carries
-- no table_id, so the join back to it is on the full (catalog, schema, table) name, same as the
-- comment on this query's own header explains.
latest_size AS (
  SELECT catalog_name, schema_name, table_name, active_bytes
  FROM system.storage.table_metrics_history
  WHERE snapshot_date >= current_date() - INTERVAL :period_days DAYS
    AND snapshot_date < current_date()
    AND table_dropped_time IS NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY catalog_name, schema_name, table_name ORDER BY snapshot_date DESC
  ) = 1
),
po_usage AS (
  SELECT catalog_name, schema_name, table_name,
         SUM(CAST(usage_quantity AS DOUBLE)) AS po_dbu_sum
  FROM system.storage.predictive_optimization_operations_history
  WHERE start_time >= current_date() - INTERVAL :period_days DAYS
    AND start_time <  current_date()
  GROUP BY catalog_name, schema_name, table_name
),
-- One blended $/DBU rate for Predictive Optimization in the same window, from the same
-- effective-list-price join every priced cost check in this app uses (DEC-66.1).
-- predictive_optimization_operations_history carries no sku_name to price per row, so this is
-- one account-wide rate applied to every table's own DBU count below, never a per-row lookup.
po_billed AS (
  SELECT u.sku_name,
         u.usage_quantity                AS usage_quantity,
         u.usage_quantity * lp.list_rate AS list_cost,
         lp.list_rate                    AS list_rate
  FROM system.billing.usage u
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
    FROM system.billing.list_prices
  ) lp
    ON u.sku_name   = lp.sku_name
   AND u.cloud      = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.billing_origin_product = 'PREDICTIVE_OPTIMIZATION'
    AND u.usage_date >= dateadd(day, -:period_days, current_date())
    AND u.usage_date <  current_date()
),
po_rate AS (
  SELECT
    SUM(list_cost) / NULLIF(SUM(usage_quantity), 0) AS dbu_rate,
    -- price_basis: 'no_po_activity' when the account has no PREDICTIVE_OPTIMIZATION billing rows
    -- in the window at all (COUNT(*) guards the SUMs below, which are NULL - not 0 - over zero
    -- rows and would otherwise fall through to the wrong branch); otherwise the same 'unpriced' /
    -- 'free' / 'priced' disclosure every other priced check here uses.
    CASE
      WHEN COUNT(*) = 0 THEN 'no_po_activity'
      WHEN SUM(CASE WHEN list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                    THEN usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
      WHEN SUM(CASE WHEN list_rate IS NOT NULL THEN usage_quantity ELSE 0 END) = 0 THEN 'free'
      ELSE 'priced'
    END AS price_basis
  FROM po_billed
),
sized AS (
  SELECT
    u.table_catalog, u.table_schema, u.table_name, u.table_type, u.table_owner, u.last_altered,
    ls.active_bytes,
    COALESCE(po.po_dbu_sum, 0) AS po_dbu_sum,
    lw.last_write_time,
    r.dbu_rate,
    r.price_basis
  FROM unused u
  LEFT JOIN latest_size ls
    ON  u.table_catalog = ls.catalog_name
    AND u.table_schema  = ls.schema_name
    AND u.table_name    = ls.table_name
  LEFT JOIN po_usage po
    ON  u.table_catalog = po.catalog_name
    AND u.table_schema  = po.schema_name
    AND u.table_name    = po.table_name
  LEFT JOIN last_write lw
    ON  u.table_catalog = lw.catalog
    AND u.table_schema  = lw.schema
    AND u.table_name    = lw.name
  CROSS JOIN po_rate r
),
priced AS (
  SELECT
    s.*,
    CASE WHEN s.active_bytes IS NULL THEN NULL
         ELSE ROUND(s.active_bytes / POWER(1024.0, 3), 3) END AS active_gb,
    CASE WHEN s.active_bytes IS NULL THEN NULL
         ELSE ROUND(s.active_bytes / POWER(1024.0, 3) * :storage_usd_per_gb_month, 2) END
      AS est_storage_usd_month,
    -- A table with zero Predictive Optimization DBUs in the window is a real, known $0, whatever
    -- the account-wide rate reads - only a NONZERO DBU count with no derivable rate is unknown.
    CASE WHEN s.po_dbu_sum = 0 THEN 0.0 ELSE ROUND(s.po_dbu_sum * s.dbu_rate, 2) END
      AS po_upkeep_usd_window
  FROM sized s
),
scored AS (
  SELECT
    p.*,
    datediff(current_date(), DATE(p.last_altered))     AS days_since_altered,
    datediff(current_date(), DATE(p.last_write_time))  AS days_since_last_write,
    CASE
      WHEN p.active_bytes IS NULL THEN NULL
      WHEN p.po_dbu_sum > 0 AND p.po_upkeep_usd_window IS NULL THEN NULL
      ELSE ROUND(p.est_storage_usd_month + COALESCE(p.po_upkeep_usd_window, 0) * 30.0 / :period_days, 2)
    END AS est_total_usd_month,
    -- No size on record is not a gap in the check: the table is unused, only its cost is unknown.
    CASE
      WHEN p.active_bytes IS NOT NULL AND p.po_dbu_sum > 0 AND p.po_upkeep_usd_window IS NULL
        THEN 'po_upkeep_unpriced'
    END AS not_assessed_reason
  FROM priced p
),
banded AS (
  SELECT
    sc.*,
    CASE
      WHEN sc.not_assessed_reason IS NOT NULL      THEN 'NOT_ASSESSED'
      WHEN sc.est_total_usd_month IS NULL          THEN 'OK'
      WHEN sc.est_total_usd_month >= :crit_usd_month THEN 'CRITICAL'
      WHEN sc.est_total_usd_month >= :warn_usd_month THEN 'WARN'
      ELSE 'OK'
    END AS status
  FROM scored sc
),
-- Only a priced row has a real est_total_usd_month to rank on; a NOT_ASSESSED or unsized row
-- always keeps its own row below, never pooled - there is no dollar figure to pool it by.
assessed AS (
  -- table_catalog/schema/name break a $ tie deterministically (same tiebreak style
  -- cost_chargeback_by_warehouse's own poolable_ranked uses), so which table lands in the pooled
  -- row is stable across runs, never arbitrary.
  SELECT b.*, ROW_NUMBER() OVER (
    ORDER BY b.est_total_usd_month DESC, b.table_catalog, b.table_schema, b.table_name
  ) AS rn
  FROM banded b
  WHERE b.est_total_usd_month IS NOT NULL
),
unpriced_rows AS (
  SELECT b.* FROM banded b WHERE b.est_total_usd_month IS NULL
),
kept AS (
  SELECT
    table_catalog, table_schema, table_name, table_type,
    CASE
      WHEN table_owner IS NULL OR table_owner = '__REDACTED__' THEN table_owner
      WHEN table_owner RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN table_owner
      ELSE concat(substr(sha2(lower(trim(table_owner)), 256), 1, 8), ' ', substr(table_owner, 1, 2), '***')
    END AS table_owner,
    active_gb, est_storage_usd_month, po_upkeep_usd_window, price_basis, est_total_usd_month,
    days_since_altered, days_since_last_write,
    FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
    not_assessed_reason, status
  FROM assessed
  WHERE rn <= :top_n
  UNION ALL
  SELECT
    table_catalog, table_schema, table_name, table_type,
    CASE
      WHEN table_owner IS NULL OR table_owner = '__REDACTED__' THEN table_owner
      WHEN table_owner RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN table_owner
      ELSE concat(substr(sha2(lower(trim(table_owner)), 256), 1, 8), ' ', substr(table_owner, 1, 2), '***')
    END AS table_owner,
    active_gb, est_storage_usd_month, po_upkeep_usd_window,
    CASE WHEN active_gb IS NULL THEN 'no_size' ELSE price_basis END AS price_basis,
    est_total_usd_month,
    days_since_altered, days_since_last_write,
    FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
    not_assessed_reason, status
  FROM unpriced_rows
),
pooled AS (
  -- Every remaining priced table beyond :top_n, re-aggregated into one is_other row - always
  -- status=OK (see caveats), only ever pooling rows that were already priced and below the floor.
  SELECT
    CAST(NULL AS STRING) AS table_catalog, CAST(NULL AS STRING) AS table_schema,
    CAST(NULL AS STRING) AS table_name, CAST(NULL AS STRING) AS table_type,
    CAST(NULL AS STRING) AS table_owner,
    ROUND(SUM(active_gb), 3)              AS active_gb,
    ROUND(SUM(est_storage_usd_month), 2)  AS est_storage_usd_month,
    ROUND(SUM(po_upkeep_usd_window), 2)   AS po_upkeep_usd_window,
    MAX(price_basis)                      AS price_basis,
    ROUND(SUM(est_total_usd_month), 2)    AS est_total_usd_month,
    CAST(NULL AS BIGINT) AS days_since_altered, CAST(NULL AS BIGINT) AS days_since_last_write,
    TRUE AS is_other, COUNT(*) AS pooled_count,
    CAST(NULL AS STRING) AS not_assessed_reason,
    'OK' AS status
  FROM assessed
  WHERE rn > :top_n
)
SELECT * FROM (
  SELECT * FROM kept
  UNION ALL
  SELECT * FROM pooled WHERE pooled_count > 0
) u
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         is_other,
         est_total_usd_month DESC NULLS LAST,
         table_catalog, table_schema, table_name
