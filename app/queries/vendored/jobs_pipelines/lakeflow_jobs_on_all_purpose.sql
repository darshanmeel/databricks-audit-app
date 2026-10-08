-- query_id: lakeflow_jobs_on_all_purpose
-- title: Jobs running on all-purpose (interactive) clusters
-- domain: jobs_pipelines   tier: standard
-- reads: system.lakeflow.job_task_run_timeline, system.compute.clusters, system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.lakeflow, system.compute, system.billing; GA
-- empty_if: schema_not_enabled
-- params: :period_days (default 30) rolling window in days; :crit_share_usd (default 100) this job's own metered $ on an all-purpose cluster that flags CRITICAL; :top_n (default 100000) row cap
-- confidence: needs_confirmation
-- confidence_note: The cross-domain join to system.compute.clusters, whether compute_ids is populated on older rows, and whether compute (the struct array this query now reads to tell a SQL-warehouse id apart from a cluster id, and to attribute cost to a job's own billing rows) populates on the same rows as compute_ids are the things to confirm on your own workspace.
-- read_this: One row = one (job, all-purpose cluster) placement over the window. cluster_source is the column that matters - UI or API means the job ran on an interactive/all-purpose cluster (the anti-pattern) instead of a cheaper jobs cluster; WAREHOUSE means the id was a SQL warehouse, not a cluster, so this check does not apply. est_usd_list_share is this job's OWN metered $ on this cluster, from the billing rows that carry both this cluster and this job - never the shared cluster's whole bill, never an even split of it.
-- healthy: cluster_source = JOB, i.e. the task ran on dedicated jobs compute - est_usd_list_share is then informational only (field heuristic).
-- investigate_if: cluster_source IN ('UI','API') (WARN), especially with est_usd_list_share at/above :crit_share_usd (CRITICAL) - a job pinned to an always-on interactive cluster (field heuristic). cluster_source = WAREHOUSE is not this anti-pattern (a SQL-warehouse task, not a cluster) and always reads OK. The trailing NOT_ASSESSED summary row counts runs dropped for null/empty compute_ids; do not read those as "clean".
-- actions: 1) point the job at a job cluster / new-cluster-per-run instead of an existing all-purpose cluster (free, in the job definition); 2) if the interactive cluster exists only to serve jobs, add auto-termination or delete it (config); 3) if shared interactive compute is genuinely required, move the heavy jobs to a right-sized jobs cluster (spend / reshape).
-- next: cost_by_job (to dollarize the same jobs' DBUs), lakeflow_failed_jobs_wasted_dbus (if these jobs also fail), compute_warehouse_idle_gaps (if the shared cluster sits idle between runs)
-- caveats: Anti-pattern = a job task running on cluster_source IN ('UI','API') (all-purpose). EXPLODE(compute_ids) drops rows where compute_ids is NULL OR empty (e.g. early-Dec-2025 rows) - both shapes are surfaced as a separate NOT_ASSESSED summary row (task_runs = dropped runs), NEVER implicitly counted as zero or as clean (an empty, non-NULL array silently vanished from both branches before this fix - EXPLODE() of an empty array yields zero rows, so it was invisible even in the "dropped" count). compute_ids can ALSO carry SQL-warehouse ids, not just cluster ids (Databricks' own docs, already noted in this folder's lakeflow_long_running_runs.sql for its own first-compute-id link) - a warehouse id never has a row in system.compute.clusters, so it used to fall through the cluster_source LEFT JOIN as a false NOT_ASSESSED. This query now also explodes compute (a parallel struct-array column on the same table, one entry per compute_ids entry, with separate cluster_id/warehouse_id fields) and labels an id WAREHOUSE, status OK, whenever it names a warehouse instead of a cluster - a real NOT_ASSESSED now means an id that is neither a known cluster nor a known warehouse (e.g. a cluster deleted before system tables started recording, or a genuine dimension gap), not "ran on a warehouse". Confirm the exact cluster_source column name and value enum, and that system.compute.clusters is SCD2 on your workspace (the latest row is taken by change_time). net_dbus is exact billed DBUs (usage_unit='DBU'); est_usd_list is an ESTIMATE AT THE EFFECTIVE LIST PRICE (usage_quantity x list_prices.pricing.effective_list.default - DEC-66.1), NOT the negotiated invoice rate, and excludes cloud infra/egress $. Cost is attributed to THIS JOB from the cluster's own billing rows that carry usage_metadata.job_id = this job (pre-aggregated per (workspace_id, cluster_id, job_id) then LEFT JOINed so rows are never multiplied): a shared all-purpose cluster's notebook or other-job usage carries no job_id match and is correctly excluded, and summing net_dbus/est_usd_list across every job on a shared cluster no longer double-counts the cluster's bill. est_usd_list_share is kept as its own column (read by the app's discount/column logic) but is now identical to est_usd_list, not a further even split by jobs_sharing_cluster - jobs_sharing_cluster stays as informational context (how many distinct jobs use this cluster), no longer a divisor. price_basis is 'unpriced' when any non-free-usage SKU billed to this job's own usage on the shared cluster had no matching list_prices row (est_usd_list/est_usd_list_share then understate cost), 'free' when every matched SKU is a FREE_USAGE SKU (a real $0), 'priced' otherwise, and NULL on the trailing NOT_ASSESSED dropped-runs summary row (no cost was attributed there). Ordered worst-status-first (CRITICAL, WARN, NOT_ASSESSED, then OK, each by est_usd_list_share DESC) before :top_n is applied, so a flagged placement is never cut before an OK one; :top_n defaults to 100000 (was 500) because this is a per-placement finding, not a small inventory, and a low build-time cap can silently drop the flagged rows that matter, with no thresholds.yml override needed.
WITH task_compute AS (
  SELECT workspace_id, job_id, run_id, task_key, EXPLODE(compute_ids) AS compute_id
  FROM system.lakeflow.job_task_run_timeline
  WHERE period_start_time >= dateadd(DAY, -:period_days, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())
    AND result_state IS NOT NULL
    AND compute_ids IS NOT NULL
    AND size(compute_ids) > 0
),
-- Runs dropped because compute_ids was NULL OR an empty array -> reported as NOT_ASSESSED, never
-- as zero. EXPLODE() of an empty (non-NULL) array yields zero rows, so a size(compute_ids) = 0
-- run would otherwise vanish from task_compute WITHOUT being counted here either - counted as
-- neither flagged nor clean, just invisible.
dropped AS (
  SELECT workspace_id, COUNT(DISTINCT run_id) AS dropped_runs
  FROM system.lakeflow.job_task_run_timeline
  WHERE period_start_time >= dateadd(DAY, -:period_days, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())
    AND result_state IS NOT NULL
    AND (compute_ids IS NULL OR size(compute_ids) = 0)
  GROUP BY workspace_id
),
-- compute_ids can name a SQL warehouse as well as a cluster (Databricks' own docs) - a warehouse
-- id has no row in system.compute.clusters, so it used to fall through the LEFT JOIN below as a
-- false NOT_ASSESSED. compute (same table, one struct per compute_ids entry, cluster_id and
-- warehouse_id as separate fields) tells the two apart without guessing at an id's shape.
warehouse_compute AS (
  SELECT DISTINCT workspace_id, w.warehouse_id AS compute_id
  FROM (
    SELECT workspace_id, EXPLODE(compute) AS w
    FROM system.lakeflow.job_task_run_timeline
    WHERE period_start_time >= dateadd(DAY, -:period_days, current_date())
      AND period_end_time < date_trunc('DAY', current_timestamp())
      AND result_state IS NOT NULL
      AND compute IS NOT NULL
      AND size(compute) > 0
  ) exploded
  WHERE w.warehouse_id IS NOT NULL
),
-- distinct jobs sharing each cluster in the window (informational only - see caveats).
cluster_jobs AS (
  SELECT workspace_id, compute_id, COUNT(DISTINCT job_id) AS jobs_sharing_cluster
  FROM task_compute
  GROUP BY workspace_id, compute_id
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM system.billing.list_prices
),
cost_rollup AS (
  -- Pre-aggregated per (cluster, job) over the SAME window as the finding, from usage_metadata.
  -- job_id: this job's OWN billed usage on the cluster, not the cluster's whole bill - notebook
  -- or another job's usage on the same shared cluster carries a different (or no) job_id and is
  -- excluded here, never folded into this job's row.
  SELECT u.workspace_id,
         u.usage_metadata.cluster_id                      AS cluster_id,
         u.usage_metadata.job_id                           AS job_id,
         SUM(u.usage_quantity)                            AS net_dbus,
         SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS est_usd_list,
         CASE
           WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                         THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END                                               AS price_basis
  FROM system.billing.usage u
  LEFT JOIN price p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.cluster_id IS NOT NULL
    AND u.usage_metadata.job_id IS NOT NULL
    AND u.usage_date >= dateadd(DAY, -:period_days, current_date())
    AND u.usage_date <  current_date()
  GROUP BY u.workspace_id, u.usage_metadata.cluster_id, u.usage_metadata.job_id
),
finding AS (
  SELECT tc.workspace_id,
         CAST(tc.job_id AS STRING)         AS job_id,
         CAST(tc.compute_id AS STRING)     AS compute_id,
         COALESCE(c.cluster_source, CASE WHEN wc.compute_id IS NOT NULL THEN 'WAREHOUSE' END) AS cluster_source,
         COUNT(DISTINCT tc.run_id)          AS task_runs,
         COALESCE(MAX(cr.net_dbus), 0)      AS net_dbus,
         COALESCE(MAX(cr.est_usd_list), 0)  AS est_usd_list,
         COALESCE(MAX(cr.price_basis), 'priced') AS price_basis,
         MAX(cj.jobs_sharing_cluster)       AS jobs_sharing_cluster
  FROM task_compute tc
  LEFT JOIN (
    SELECT workspace_id, cluster_id, cluster_source
    FROM system.compute.clusters
    QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC) = 1
  ) c
    ON tc.workspace_id = c.workspace_id AND tc.compute_id = c.cluster_id
  LEFT JOIN warehouse_compute wc
    ON tc.workspace_id = wc.workspace_id AND tc.compute_id = wc.compute_id
  LEFT JOIN cost_rollup cr
    ON tc.workspace_id = cr.workspace_id AND tc.compute_id = cr.cluster_id AND tc.job_id = cr.job_id
  LEFT JOIN cluster_jobs cj
    ON tc.workspace_id = cj.workspace_id AND tc.compute_id = cj.compute_id
  GROUP BY tc.workspace_id, tc.job_id, tc.compute_id, c.cluster_source, wc.compute_id
)
SELECT * FROM (
  SELECT
    workspace_id, job_id, compute_id, cluster_source, task_runs,
    net_dbus, est_usd_list, price_basis, jobs_sharing_cluster,
    -- kept under its old name (the app's discount/column logic reads it) - now just this job's
    -- own metered $ (est_usd_list), not a further even split.
    est_usd_list                                                           AS est_usd_list_share,
    CASE
      WHEN cluster_source IS NULL THEN 'NOT_ASSESSED'
      WHEN cluster_source IN ('UI', 'API') AND est_usd_list >= :crit_share_usd THEN 'CRITICAL'
      WHEN cluster_source IN ('UI', 'API') THEN 'WARN'
      ELSE 'OK'
    END AS status
  FROM finding
  UNION ALL
  SELECT
    workspace_id,
    CAST(NULL AS STRING)                       AS job_id,
    CAST(NULL AS STRING)                       AS compute_id,
    'null or empty compute_ids'                AS cluster_source,
    dropped_runs                               AS task_runs,
    CAST(NULL AS DOUBLE)                       AS net_dbus,
    CAST(NULL AS DOUBLE)                       AS est_usd_list,
    CAST(NULL AS STRING)                       AS price_basis,
    CAST(NULL AS BIGINT)                       AS jobs_sharing_cluster,
    CAST(NULL AS DOUBLE)                       AS est_usd_list_share,
    'NOT_ASSESSED'                             AS status
  FROM dropped
) placements
-- Worst-status-first, THEN by share: a low-cap :top_n must never cut a flagged placement before
-- an OK one (the naive DESC-by-share order used to do exactly that when the cap bound). The
-- UNION ALL is wrapped in a derived table so the engine can bind ORDER BY to an expression
-- (some engines cannot ORDER BY an expression directly on a top-level UNION ALL).
ORDER BY
  CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
  est_usd_list_share DESC
LIMIT :top_n
