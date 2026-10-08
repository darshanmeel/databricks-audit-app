-- query_id: cost_chargeback_by_cluster
-- title: Chargeback by cluster, job cluster or pipeline, current period versus the one before it
-- domain: cost   tier: standard
-- reads: system.billing.usage, system.billing.list_prices, system.compute.clusters,
--   system.lakeflow.jobs, system.lakeflow.pipelines, system.access.workspaces_latest
-- requires: SELECT on system.billing, system.compute, system.lakeflow and system.access; GA (all
--   six tables are generally available)
-- empty_if: no_activity
-- params: :period_days (default 30) rolling window in days - the current period is the most
--   recent N days, the previous period is the equal-length N days immediately before it;
--   :warn_increase_pct (default 25) percent increase in a row's list-priced spend, current period
--   over previous, at/above which WARN; :crit_increase_pct (default 50) percent increase at/above
--   which CRITICAL; :min_spend_usd (default 20) the current period's own list-priced spend must be
--   at least this before a percent change is judged - below it the row reads OK regardless of the
--   swing, since a percent figure on a tiny base is noise; :top_n (default 20) how many status=OK
--   rows, ranked by current-period spend descending, are kept as their own row before the rest are
--   pooled into one is_other=true row - every WARN/CRITICAL/NOT_ASSESSED row is always kept as its
--   own row, never pooled, so a real finding on a small cluster can never be hidden behind the cap
-- confidence: needs_confirmation
-- confidence_note: New app-owned query, not yet run live. Reuses the effective-list-price join
--   every priced cost query in this app already uses (DEC-66.1) and the exact current-vs-previous-
--   window contract cost_period_over_period already established (same NOT_ASSESSED / price_basis
--   rules, applied per cluster/job-cluster/pipeline instead of per workspace+product); confirm on
--   your account that a cluster, job or pipeline you know grew or shrank between two recent equal
--   periods matches this query's change_pct, and that "all-purpose" here (a billed cluster_id with
--   no dlt_pipeline_id or job_id at all) matches how you expect interactive clusters to be billed.
-- read_this: One row = one all-purpose cluster (workspace_id, cluster_id, cluster_kind =
--   'all_purpose'), by name and owner; one row per job whose runs used job clusters in the window
--   (workspace_id, job_id, cluster_kind = 'job_cluster') - a job's short-lived job clusters are
--   rolled up to the job rather than listed one ephemeral cluster_id at a time; PLUS one row per
--   classic (non-serverless) Lakeflow Declarative Pipeline whose compute billed with a cluster_id
--   in the window (workspace_id, pipeline_id, cluster_kind = 'pipeline'), by pipeline name. The
--   three kinds are mutually exclusive by construction (split on dlt_pipeline_id first, then
--   job_id), so nothing is double-counted across them. est_current_usd_list / est_previous_usd_list
--   are the two totals; est_change_usd_list and change_pct compare them. price_basis discloses
--   pricing coverage across both windows combined; not_assessed_reason explains a NOT_ASSESSED row.
--   is_other=true is the one pooled row for every status=OK row ranked below :top_n by current
--   spend (every other column NULL on that row, pooled_count says how many); it always reads status
--   OK (see caveats).
-- healthy: status OK - no increase at/above :warn_increase_pct, or the row's own current-period
--   spend (a real $0 counts) sits below :min_spend_usd
-- investigate_if: CRITICAL - the increase is at/above :crit_increase_pct on at least
--   :min_spend_usd of current spend, or the row is brand-new spend above the floor with nothing in
--   the previous period to compare against; WARN - at/above :warn_increase_pct. NOT_ASSESSED - read
--   not_assessed_reason: 'previous_window_not_covered' when the account's own earliest recorded
--   usage does not reach back far enough to cover the previous window at this window length;
--   'current_period_unpriced' when this row's current-period spend could not be priced at all;
--   'previous_period_unpriced' is the same gap on the previous period's own side.
-- actions: 1) open cost_chargeback_by_job for a job_cluster row, or cost_chargeback_by_tag_value
--   for the tag behind the growth (free); 2) if the growth is deliberate, right-size the cluster or
--   set a budget policy before it compounds another period (config); 3) for a growing all_purpose
--   cluster, check compute_warehouse_idle_gaps' compute-domain sibling (cluster utilization) before
--   assuming more spend means more real work (free).
-- next: cost_chargeback_by_job (the job behind a job_cluster row),
--   cost_chargeback_reconcile (this view's own total against the account's billing total),
--   cost_chargeback_by_tag_value (the tag cut of the same spend)
-- caveats: DEC-66.1 - the effective list price only, never a negotiated rate; the same basis on
--   both windows. "all-purpose" means a cluster-attributed billed row with no dlt_pipeline_id or
--   job_id at all (interactive use); it is not read from cluster_source, so a cluster created via
--   the UI but driven only by job runs in this window shows up entirely under job_cluster, never
--   under its own cluster_id - the intended "rolled up to their job" behavior. owner for a
--   job_cluster row is the job's run_as, and for a pipeline row is the pipeline's run_as - neither
--   is a person who clicked anything. This is a chargeback reference, not a fix for a listed
--   problem. Corrections are netted (SUM across all record_types - never filter to ORIGINAL only).
--   The current day is excluded from both windows, same as every other windowed cost query, since
--   billing.usage lands with ingestion lag and the trailing day is provisional. POOLING - the
--   is_other row only ever pools rows that were ALREADY status=OK on their own, and it always reads
--   status OK itself, however large the combined dollars - a rollup of small, healthy rows is never
--   itself a finding; it can never hide a WARN/CRITICAL/NOT_ASSESSED row, which always keeps its
--   own row regardless of :top_n.
-- not_assessed_reasons: previous_window_not_covered: the snapshot does not reach back far enough
--   to cover the previous period; current_period_unpriced: this period's spend could not be
--   priced; previous_period_unpriced: the previous period's spend could not be priced
WITH snapshot AS (
  SELECT MIN(usage_date) AS snapshot_start FROM system.billing.usage
),
priced AS (
  SELECT u.workspace_id,
         CASE
           WHEN u.usage_metadata.dlt_pipeline_id IS NOT NULL THEN 'pipeline'
           WHEN u.usage_metadata.job_id IS NULL THEN 'all_purpose'
           ELSE 'job_cluster'
         END AS cluster_kind,
         CASE
           WHEN u.usage_metadata.dlt_pipeline_id IS NOT NULL THEN u.usage_metadata.dlt_pipeline_id
           WHEN u.usage_metadata.job_id IS NULL THEN u.usage_metadata.cluster_id
           ELSE u.usage_metadata.job_id
         END AS entity_id,
         u.usage_date, u.sku_name,
         u.usage_quantity                    AS usage_quantity,
         u.usage_quantity * lp.list_rate     AS list_cost,
         lp.list_rate                        AS list_rate
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
    AND u.usage_metadata.cluster_id IS NOT NULL
    AND u.usage_date >= dateadd(day, -(:period_days * 2), current_date())
    AND u.usage_date <  current_date()
),
raw_agg AS (
  SELECT workspace_id, cluster_kind, entity_id,
         SUM(CASE WHEN usage_date >= dateadd(day, -:period_days, current_date())
                  THEN list_cost END)                                         AS current_cost,
         SUM(CASE WHEN usage_date <  dateadd(day, -:period_days, current_date())
                  THEN list_cost END)                                         AS previous_cost,
         SUM(CASE WHEN usage_date >= dateadd(day, -:period_days, current_date())
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                             AS current_unpriced_quantity,
         SUM(CASE WHEN usage_date >= dateadd(day, -:period_days, current_date())
                   AND list_rate IS NOT NULL
                  THEN usage_quantity ELSE 0 END)                             AS current_priced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -:period_days, current_date())
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                             AS previous_unpriced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -:period_days, current_date())
                   AND list_rate IS NOT NULL
                  THEN usage_quantity ELSE 0 END)                             AS previous_priced_quantity
  FROM priced
  GROUP BY workspace_id, cluster_kind, entity_id
),
agg AS (
  SELECT *,
         CASE WHEN current_cost IS NULL AND current_unpriced_quantity = 0 THEN 0 ELSE current_cost END AS eff_current_cost,
         CASE WHEN previous_cost IS NULL AND previous_unpriced_quantity = 0 THEN 0 ELSE previous_cost END AS eff_previous_cost
  FROM raw_agg
),
clusters AS (
  -- clusters/jobs/pipelines/ws below join with IS NOT DISTINCT FROM on workspace_id (a real
  -- account never has a NULL workspace_id here; this only matters for an account-level billing
  -- row, where a plain "=" join to workspace_id would never match a NULL to a NULL).
  SELECT workspace_id, cluster_id, cluster_name, owned_by
  FROM system.compute.clusters
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC) = 1
),
jobs AS (
  SELECT workspace_id, job_id, name AS job_name, run_as
  FROM system.lakeflow.jobs
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),
pipelines AS (
  SELECT workspace_id, pipeline_id, name AS pipeline_name, run_as
  FROM system.lakeflow.pipelines
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC) = 1
),
ws AS (
  SELECT workspace_id, workspace_name FROM system.access.workspaces_latest
),
total AS (
  SELECT SUM(eff_current_cost) AS total_current_usd_list FROM agg
),
scored AS (
  SELECT a.workspace_id,
         ws.workspace_name,
         a.cluster_kind,
         a.entity_id,
         CASE a.cluster_kind
           WHEN 'all_purpose' THEN cl.cluster_name
           WHEN 'pipeline'    THEN pl.pipeline_name
           ELSE j.job_name
         END                                                                          AS name,
         CASE a.cluster_kind
           WHEN 'all_purpose' THEN cl.owned_by
           WHEN 'pipeline'    THEN pl.run_as
           ELSE j.run_as
         END                                                                          AS owner,
         a.current_unpriced_quantity, a.previous_unpriced_quantity,
         a.current_priced_quantity, a.previous_priced_quantity,
         a.eff_current_cost, a.eff_previous_cost,
         ROUND(a.eff_current_cost, 2)                                                 AS est_current_usd_list,
         ROUND(a.eff_current_cost * 100.0 / NULLIF(t.total_current_usd_list, 0), 1)   AS share_of_total_pct,
         ROUND(a.eff_previous_cost, 2)                                                AS est_previous_usd_list,
         ROUND(a.eff_current_cost - a.eff_previous_cost, 2)                           AS est_change_usd_list,
         ROUND((a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100, 1) AS change_pct,
         CASE
           WHEN (a.current_unpriced_quantity + a.previous_unpriced_quantity) > 0 THEN 'unpriced'
           WHEN (a.current_priced_quantity + a.previous_priced_quantity) = 0     THEN 'free'
           ELSE 'priced'
         END                                                                           AS price_basis,
         CASE
           WHEN s.snapshot_start > dateadd(day, -(:period_days * 2), current_date())
             THEN 'NOT_ASSESSED'
           WHEN a.current_cost IS NULL AND a.current_unpriced_quantity > 0
             THEN 'NOT_ASSESSED'
           WHEN a.previous_cost IS NULL AND a.previous_unpriced_quantity > 0
             THEN 'NOT_ASSESSED'
           WHEN COALESCE(a.eff_current_cost, 0) < :min_spend_usd THEN 'OK'
           WHEN a.eff_previous_cost = 0 THEN 'CRITICAL'
           WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= :crit_increase_pct
             THEN 'CRITICAL'
           WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= :warn_increase_pct
             THEN 'WARN'
           ELSE 'OK'
         END                                                                           AS status,
         CASE
           WHEN s.snapshot_start > dateadd(day, -(:period_days * 2), current_date())
             THEN 'previous_window_not_covered'
           WHEN a.current_cost IS NULL AND a.current_unpriced_quantity > 0
             THEN 'current_period_unpriced'
           WHEN a.previous_cost IS NULL AND a.previous_unpriced_quantity > 0
             THEN 'previous_period_unpriced'
           ELSE NULL
         END                                                                           AS not_assessed_reason
  FROM agg a
  LEFT JOIN clusters cl  ON a.cluster_kind = 'all_purpose' AND cl.workspace_id IS NOT DISTINCT FROM a.workspace_id AND cl.cluster_id   = a.entity_id
  LEFT JOIN jobs j       ON a.cluster_kind = 'job_cluster' AND j.workspace_id  IS NOT DISTINCT FROM a.workspace_id AND j.job_id       = a.entity_id
  LEFT JOIN pipelines pl ON a.cluster_kind = 'pipeline'    AND pl.workspace_id IS NOT DISTINCT FROM a.workspace_id AND pl.pipeline_id = a.entity_id
  LEFT JOIN ws           ON ws.workspace_id IS NOT DISTINCT FROM a.workspace_id
  CROSS JOIN snapshot s
  CROSS JOIN total t
),
flagged AS (
  -- Every non-OK row (WARN/CRITICAL/NOT_ASSESSED) always keeps its own row - never pooled.
  SELECT workspace_id, workspace_name, cluster_kind, entity_id, name, owner,
         FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
         est_current_usd_list, share_of_total_pct, est_previous_usd_list, est_change_usd_list,
         change_pct, price_basis, status, not_assessed_reason
  FROM scored
  WHERE status != 'OK'
),
ok_ranked AS (
  SELECT scored.*,
         ROW_NUMBER() OVER (ORDER BY est_current_usd_list DESC NULLS LAST, cluster_kind, entity_id) AS rn
  FROM scored
  WHERE status = 'OK'
),
ok_kept AS (
  -- The top :top_n OK rows by current spend, kept as their own row.
  SELECT workspace_id, workspace_name, cluster_kind, entity_id, name, owner,
         FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
         est_current_usd_list, share_of_total_pct, est_previous_usd_list, est_change_usd_list,
         change_pct, price_basis, status, not_assessed_reason
  FROM ok_ranked
  WHERE rn <= :top_n
),
ok_pooled_raw AS (
  -- Every remaining OK row beyond :top_n - re-aggregated into one is_other row below.
  SELECT * FROM ok_ranked WHERE rn > :top_n
),
other_row AS (
  SELECT
    CAST(NULL AS STRING) AS workspace_id,
    CAST(NULL AS STRING) AS workspace_name,
    CAST(NULL AS STRING) AS cluster_kind,
    CAST(NULL AS STRING) AS entity_id,
    CAST(NULL AS STRING) AS name,
    CAST(NULL AS STRING) AS owner,
    TRUE                  AS is_other,
    COUNT(*)              AS pooled_count,
    ROUND(SUM(eff_current_cost), 2)                                              AS est_current_usd_list,
    ROUND(SUM(eff_current_cost) * 100.0 / NULLIF(MAX(t.total_current_usd_list), 0), 1) AS share_of_total_pct,
    ROUND(SUM(eff_previous_cost), 2)                                             AS est_previous_usd_list,
    ROUND(SUM(eff_current_cost) - SUM(eff_previous_cost), 2)                     AS est_change_usd_list,
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
  FROM ok_pooled_raw
  CROSS JOIN total t
)
SELECT * FROM (
  SELECT * FROM flagged
  UNION ALL
  SELECT * FROM ok_kept
  UNION ALL
  SELECT * FROM other_row WHERE pooled_count > 0
) u
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         is_other,
         est_change_usd_list DESC NULLS LAST,
         workspace_id, cluster_kind, entity_id
