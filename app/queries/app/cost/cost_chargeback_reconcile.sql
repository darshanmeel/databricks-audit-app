-- query_id: cost_chargeback_reconcile
-- title: Chargeback self-check - the shared price join and the by-user split against the billing total
-- domain: cost   tier: standard
-- reads: system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.billing; GA (system.billing.usage and system.billing.list_prices
--   are generally available)
-- params: :period_days (default 30) rolling window in days, this single-window check only (no
--   previous-period comparison); :gap_warn_pct (default 1) percent of the billing total a
--   full-coverage check's own gap must reach before WARN; :gap_crit_pct (default 5) percent
--   at/above which CRITICAL
-- confidence: needs_confirmation
-- confidence_note: New app-owned query, not yet run live. Reuses the effective-list-price join
--   every priced cost query in this app already uses (DEC-66.1); confirm each 'full' check reads
--   0.0 gap_usd_list on a live account (algebraically guaranteed for a bug-free implementation -
--   each is the same priced usage rows, grouped a different way, with no extra row filter).
-- read_this: One row = one named chargeback cut's own recomputed total against ONE workspace's own
--   billing total for the window (both priced at the effective list price, DEC-66.1), or the
--   account-level bucket for usage that carries no workspace_id at all. Only two
--   checks have coverage_type 'full' and a real gap verdict: price_join_fanout (not a named
--   chargeback view but the shared price join itself - it compares the plain join every chargeback
--   query in this set uses against a de-duplicated, at-most-one-price-match-per-usage-row version
--   of the same total; every priced total in this app already dollarizes each usage row once, at
--   its one latest-matching price, so a positive gap here measures overlapping rows in Databricks'
--   own system.billing.list_prices - a source data-quality signal, not an error in this app's own
--   totals) and by_user (the coarse
--   warehouse-DBU / job-DBU / neither three-bucket split - a positive gap means a usage row matched
--   more than one bucket, most often a job task billed on a SQL warehouse carrying both a
--   warehouse_id and a job_id). by_service/by_sku/by_workspace are not checked here: each simply
--   re-sums the exact same priced rows this file already sums by a different key, so their own gap
--   is always algebraically 0 - a passing check with no power to catch anything, dropped rather
--   than kept for a false sense of coverage. 'overlapping' (by_tag_value) means a usage row can
--   legitimately land in more than one group (a resource with several tags), so view_total is
--   EXPECTED to exceed the billing total - coverage_pct over 100% there is normal, not a bug;
--   'partial' (by_warehouse, by_job, by_cluster) means the cut is scoped to only some compute by
--   design - coverage_pct shows how much of the account's total it reaches, with no gap verdict.
-- healthy: a 'full' check with gap_usd_list at/below :gap_warn_pct of that row's own billing total;
--   any 'overlapping' or 'partial' check (informational only, always OK)
-- investigate_if: CRITICAL - a 'full' check's |gap_pct| at/above :gap_crit_pct; WARN - at/above
--   :gap_warn_pct. Judged per workspace (see caveats), so a small per-workspace billing total can
--   swing gap_pct further than the same dollar gap would account-wide. NOT_ASSESSED - that row's
--   own billing total is NULL or 0 (nothing to reconcile against, at any window length).
-- actions: 1) a nonzero gap on by_user means the three-bucket split (warehouse DBU + job DBU +
--   neither) no longer partitions every priced row - check for a usage row carrying both a
--   warehouse_id and a job_id, and compare against cost_chargeback_by_allocation_tag's
--   usage_unit grouping for a sanity check (free); 2) a positive price_join_fanout gap means
--   system.billing.list_prices has an overlapping validity window for some sku_name/cloud/
--   usage_unit - report it as a system-table data-quality issue; every chargeback total in this
--   app already prices each usage row once, at the latest matching price, so this gap is never an
--   error in those totals to go fix (free).
-- next: cost_chargeback_by_job, cost_chargeback_by_cluster, cost_chargeback_by_tag_value,
--   cost_chargeback_by_allocation_tag (the individual cuts this file cross-checks)
-- caveats: Every check here recomputes its OWN total from THIS file's own raw_priced CTE, grouped
--   the SAME way the named cut groups it - it proves that cut's own grouping logic still partitions
--   (or, for by_tag_value, still legitimately overlaps) the same rows this file sees, not that
--   raw_priced's own dollar-per-row math is correct (a bug baked into how many dollars a row is
--   worth in the first place would pass every check here identically, since they all share this
--   same CTE). by_user here is the coarse three-bucket split only
--   (warehouse DBU + job DBU + neither), not a reproduction of any per-identity duration-weighted
--   split a richer by_user chargeback view might compute - a bug inside such a split, if one
--   exists elsewhere in this app, would not surface as a gap here. WORKSPACE - every total here is
--   now computed per workspace_id (a NULL workspace_id is the same genuine account-level bucket
--   the chargeback cuts themselves keep), so this file reconciles cost_chargeback_by_service's
--   or cost_chargeback_by_sku's own workspace-scoped rows against THAT workspace's own billing
--   total, not the account's whole total; summing gap_usd_list back across every workspace for a
--   'full' check reproduces the old account-wide gap. DEC-66.1 - the effective list
--   price only, never a negotiated rate. Corrections are netted (SUM across all record_types -
--   never filter to ORIGINAL only). The current day is excluded, same as every other windowed cost
--   query.
WITH window_usage AS (
  SELECT u.record_id, u.workspace_id, u.usage_date, u.usage_end_time, u.sku_name, u.cloud,
         u.usage_unit, u.usage_quantity, u.billing_origin_product, u.custom_tags,
         u.usage_metadata.warehouse_id AS warehouse_id,
         u.usage_metadata.job_id       AS job_id,
         u.usage_metadata.cluster_id   AS cluster_id
  FROM system.billing.usage u
  WHERE u.usage_date >= dateadd(day, -:period_days, current_date())
    AND u.usage_date <  current_date()
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
  FROM system.billing.list_prices
),
raw_priced AS (
  -- the plain, non-deduped join every chargeback query in this set uses.
  SELECT w.*, p.list_rate, w.usage_quantity * p.list_rate AS usd_list
  FROM window_usage w
  LEFT JOIN price p
    ON  w.sku_name = p.sku_name AND w.cloud = p.cloud AND w.usage_unit = p.usage_unit
    AND w.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR w.usage_end_time < p.price_end_time)
),
raw_total AS (
  SELECT workspace_id, SUM(usd_list) AS total_usd_list FROM raw_priced GROUP BY workspace_id
),
deduped_priced AS (
  -- exactly one matching price row per usage row (most recently started wins), to check whether
  -- the plain join above ever fans out on an overlapping list_prices validity window.
  SELECT w.record_id, w.workspace_id, w.usage_quantity * p.list_rate AS usd_list
  FROM window_usage w
  LEFT JOIN price p
    ON  w.sku_name = p.sku_name AND w.cloud = p.cloud AND w.usage_unit = p.usage_unit
    AND w.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR w.usage_end_time < p.price_end_time)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY w.record_id ORDER BY p.price_start_time DESC) = 1
),
deduped_total AS (
  SELECT workspace_id, SUM(usd_list) AS total_usd_list FROM deduped_priced GROUP BY workspace_id
),
by_tag_value_total AS (
  -- deliberately NOT expected to equal the billing total: cost_chargeback_by_tag_value explodes
  -- EVERY key a usage row carries, so a resource tagged with N keys contributes its dollars N
  -- times here - the 'overlapping' coverage_type below labels this as expected, not a bug.
  SELECT rp.workspace_id, SUM(rp.usd_list) AS total_usd_list
  FROM raw_priced rp
  LATERAL VIEW OUTER explode(rp.custom_tags) t AS tag_key, tag_value
  GROUP BY rp.workspace_id
),
by_user_buckets_total AS (
  -- the same three mutually exclusive conditions a per-identity chargeback view splits usage
  -- across: warehouse DBU, job DBU, or neither (serverless/model-serving/storage/etc.).
  SELECT workspace_id,
    SUM(CASE WHEN upper(usage_unit) = 'DBU' AND warehouse_id IS NOT NULL THEN usd_list ELSE 0 END)
    + SUM(CASE WHEN upper(usage_unit) = 'DBU' AND job_id IS NOT NULL THEN usd_list ELSE 0 END)
    + SUM(CASE WHEN warehouse_id IS NULL AND job_id IS NULL THEN usd_list ELSE 0 END) AS total_usd_list
  FROM raw_priced
  GROUP BY workspace_id
),
by_warehouse_total AS (
  SELECT workspace_id, SUM(usd_list) AS total_usd_list FROM raw_priced
  WHERE upper(usage_unit) = 'DBU' AND warehouse_id IS NOT NULL
  GROUP BY workspace_id
),
by_job_total AS (
  SELECT workspace_id, SUM(usd_list) AS total_usd_list FROM raw_priced
  WHERE upper(usage_unit) = 'DBU' AND job_id IS NOT NULL
  GROUP BY workspace_id
),
by_cluster_total AS (
  SELECT workspace_id, SUM(usd_list) AS total_usd_list FROM raw_priced
  WHERE upper(usage_unit) = 'DBU' AND cluster_id IS NOT NULL   -- same scope cost_chargeback_by_cluster's own WHERE uses, both all_purpose and job_cluster kinds
  GROUP BY workspace_id
),
checks AS (
  -- LEFT JOIN off raw_total (every workspace_id present in the window) so a workspace with $0 on
  -- a partial check's own scope (by_warehouse/by_job/by_cluster) still gets a row here, not a
  -- silently dropped one; IS NOT DISTINCT FROM so the NULL (account-level) workspace_id matches
  -- itself instead of failing a plain `=` join.
  SELECT 'price_join_fanout' AS check_name, 'full' AS coverage_type, r.workspace_id,
         d.total_usd_list AS billing_total_usd_list, r.total_usd_list AS view_total_usd_list
  FROM raw_total r LEFT JOIN deduped_total d ON d.workspace_id IS NOT DISTINCT FROM r.workspace_id
  UNION ALL
  SELECT 'by_tag_value', 'overlapping', r.workspace_id, r.total_usd_list, s.total_usd_list
  FROM raw_total r LEFT JOIN by_tag_value_total s ON s.workspace_id IS NOT DISTINCT FROM r.workspace_id
  UNION ALL
  SELECT 'by_user', 'full', r.workspace_id, r.total_usd_list, s.total_usd_list
  FROM raw_total r LEFT JOIN by_user_buckets_total s ON s.workspace_id IS NOT DISTINCT FROM r.workspace_id
  UNION ALL
  SELECT 'by_warehouse', 'partial', r.workspace_id, r.total_usd_list, s.total_usd_list
  FROM raw_total r LEFT JOIN by_warehouse_total s ON s.workspace_id IS NOT DISTINCT FROM r.workspace_id
  UNION ALL
  SELECT 'by_job', 'partial', r.workspace_id, r.total_usd_list, s.total_usd_list
  FROM raw_total r LEFT JOIN by_job_total s ON s.workspace_id IS NOT DISTINCT FROM r.workspace_id
  UNION ALL
  SELECT 'by_cluster', 'partial', r.workspace_id, r.total_usd_list, s.total_usd_list
  FROM raw_total r LEFT JOIN by_cluster_total s ON s.workspace_id IS NOT DISTINCT FROM r.workspace_id
)
SELECT c.check_name,
       c.workspace_id,
       c.coverage_type,
       ROUND(c.billing_total_usd_list, 2)                                        AS billing_total_usd_list,
       ROUND(c.view_total_usd_list, 2)                                           AS view_total_usd_list,
       ROUND(c.view_total_usd_list - c.billing_total_usd_list, 2)                AS gap_usd_list,
       ROUND((c.view_total_usd_list - c.billing_total_usd_list) * 100.0
             / NULLIF(c.billing_total_usd_list, 0), 2)                           AS gap_pct,
       CASE WHEN c.coverage_type IN ('partial', 'overlapping')
            THEN ROUND(c.view_total_usd_list * 100.0 / NULLIF(c.billing_total_usd_list, 0), 1)
       END                                                                        AS coverage_pct,
       CASE
         WHEN c.coverage_type IN ('partial', 'overlapping') THEN 'OK'
         WHEN c.billing_total_usd_list IS NULL OR c.billing_total_usd_list = 0 THEN 'NOT_ASSESSED'
         WHEN ABS(c.view_total_usd_list - c.billing_total_usd_list) * 100.0 / c.billing_total_usd_list
              >= :gap_crit_pct THEN 'CRITICAL'
         WHEN ABS(c.view_total_usd_list - c.billing_total_usd_list) * 100.0 / c.billing_total_usd_list
              >= :gap_warn_pct THEN 'WARN'
         ELSE 'OK'
       END                                                                        AS status
FROM checks c
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         c.coverage_type, c.check_name, c.workspace_id
