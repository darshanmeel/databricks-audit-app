-- query_id: cost_daily_by_resource
-- title: Daily DBUs and list-price dollars per workspace, warehouse, job, all-purpose cluster and pipeline
-- domain: cost   tier: standard
-- reads: system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.billing; GA
-- empty_if: no_activity
-- params: :period_days (default 30) rolling window in days
-- confidence: needs_confirmation
-- confidence_note: New app-owned query, not yet run live. Confirm that one warehouse's days add up
--   to its total in cost_chargeback_by_warehouse for the same window.
-- read_this: One row = one day's usage for one resource. resource_type is workspace, warehouse,
--   job, cluster (an all-purpose cluster: usage with a cluster_id but no job or pipeline) or
--   pipeline. resource_key is the workspace_id for a workspace row, else workspace_id:resource_id.
--   Per type, the 100 resources with the most list-price dollars in the window keep their own
--   rows; the rest are pooled per workspace and day into one row with is_other TRUE, resource_key
--   workspace_id:other and pooled_count resources. dbus are net DBUs; usd_list prices every usage
--   unit at the effective list rate (free-tier usage is a real $0, unpriced usage adds nothing and
--   is counted in unpriced_quantity). billed_hours (warehouse and cluster) counts the hours it
--   billed; billed_runs counts the job runs (job) or pipeline updates (pipeline) that billed that
--   day. Use it to compare a resource's average day before and after a change, and to split a
--   change into how long or how often it ran against what each hour or run cost.
--   warehouse_id, job_id, pipeline_id and cluster_id repeat resource_id for that type, so a tag
--   filter reaches the resource's own tags.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (see Cost > Before & after)
-- next: cost_chargeback_by_warehouse, cost_chargeback_by_job, cost_chargeback_by_cluster (the
--   same resources as one total for this period and the one before)
-- caveats: GRAIN - usage_date, resource_type, resource_key. The types overlap: a job's usage is
--   also in its workspace's rows, so never add rows of different types together. A job includes
--   its serverless and job-cluster usage. A run or hour that spans midnight counts on both days.
--   A workspace_id NULL row is account-level usage, resource_key 'account'. Corrections are
--   netted (SUM across every record_type); billed_hours counts ORIGINAL records only. The current
--   day is excluded, since billing.usage lands with ingestion lag. DEC-66.1 - the effective list
--   price only, never a negotiated rate.
WITH priced AS (
  SELECT u.workspace_id, u.usage_date,
         u.usage_metadata.warehouse_id    AS warehouse_id,
         u.usage_metadata.job_id          AS job_id,
         u.usage_metadata.job_run_id      AS job_run_id,
         u.usage_metadata.dlt_pipeline_id AS pipeline_id,
         u.usage_metadata.dlt_update_id   AS update_id,
         CASE WHEN u.usage_metadata.job_id IS NULL AND u.usage_metadata.dlt_pipeline_id IS NULL
              THEN u.usage_metadata.cluster_id END AS cluster_id,
         CASE WHEN u.record_type = 'ORIGINAL' THEN u.usage_start_time END AS hour_slot,
         CASE WHEN upper(u.usage_unit) = 'DBU' THEN u.usage_quantity ELSE 0 END AS dbus,
         u.usage_quantity * lp.list_rate AS list_cost,
         CASE WHEN lp.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
              THEN u.usage_quantity ELSE 0 END AS unpriced_quantity
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
  WHERE u.usage_date >= dateadd(day, -:period_days, current_date())
    AND u.usage_date <  current_date()
),
by_resource AS (
  SELECT 'workspace' AS resource_type, workspace_id, workspace_id AS resource_id, usage_date, dbus, list_cost, unpriced_quantity,
         CAST(NULL AS TIMESTAMP) AS hour_slot, CAST(NULL AS STRING) AS run_ref
  FROM priced
  UNION ALL
  SELECT 'warehouse', workspace_id, warehouse_id, usage_date, dbus, list_cost, unpriced_quantity, hour_slot, CAST(NULL AS STRING)
  FROM priced WHERE warehouse_id IS NOT NULL
  UNION ALL
  SELECT 'job', workspace_id, job_id, usage_date, dbus, list_cost, unpriced_quantity, CAST(NULL AS TIMESTAMP), job_run_id
  FROM priced WHERE job_id IS NOT NULL
  UNION ALL
  SELECT 'cluster', workspace_id, cluster_id, usage_date, dbus, list_cost, unpriced_quantity, hour_slot, CAST(NULL AS STRING)
  FROM priced WHERE cluster_id IS NOT NULL
  UNION ALL
  SELECT 'pipeline', workspace_id, pipeline_id, usage_date, dbus, list_cost, unpriced_quantity, CAST(NULL AS TIMESTAMP), update_id
  FROM priced WHERE pipeline_id IS NOT NULL
),
daily AS (
  SELECT resource_type, workspace_id, resource_id, usage_date,
         SUM(dbus) AS dbus, SUM(list_cost) AS list_cost, SUM(unpriced_quantity) AS unpriced_quantity,
         COUNT(DISTINCT hour_slot) AS billed_hours, COUNT(DISTINCT run_ref) AS billed_runs
  FROM by_resource
  GROUP BY resource_type, workspace_id, resource_id, usage_date
),
totals AS (
  SELECT d.*,
         SUM(COALESCE(list_cost, 0)) OVER (PARTITION BY resource_type, workspace_id, resource_id) AS total_cost,
         SUM(dbus) OVER (PARTITION BY resource_type, workspace_id, resource_id)                AS total_dbus
  FROM daily d
),
ranked AS (
  SELECT t.*,
         DENSE_RANK() OVER (PARTITION BY resource_type
                            ORDER BY total_cost DESC, total_dbus DESC,
                                     workspace_id ASC NULLS LAST, resource_id ASC NULLS LAST) AS spend_rank
  FROM totals t
),
keyed AS (
  SELECT usage_date, resource_type, workspace_id, dbus, list_cost, unpriced_quantity, billed_hours, billed_runs,
         -- Workspaces are never pooled: an account has few enough of them.
         (resource_type <> 'workspace' AND spend_rank > 100) AS is_other,
         CASE WHEN resource_type <> 'workspace' AND spend_rank > 100 THEN NULL ELSE resource_id END AS resource_id,
         CASE
           WHEN resource_type = 'workspace' THEN COALESCE(workspace_id, 'account')
           WHEN spend_rank > 100 THEN concat(COALESCE(workspace_id, 'account'), ':other')
           ELSE concat(COALESCE(workspace_id, 'account'), ':', resource_id)
         END AS resource_key
  FROM ranked
)
SELECT usage_date,
       resource_type,
       resource_key,
       workspace_id,
       resource_id,
       CASE WHEN resource_type = 'warehouse' THEN resource_id END AS warehouse_id,
       CASE WHEN resource_type = 'job' THEN resource_id END AS job_id,
       CASE WHEN resource_type = 'pipeline' THEN resource_id END AS pipeline_id,
       CASE WHEN resource_type = 'cluster' THEN resource_id END AS cluster_id,
       is_other,
       CASE WHEN is_other THEN COUNT(*) END AS pooled_count,
       ROUND(SUM(dbus), 4) AS dbus,
       -- A day of free-tier usage only is a real $0; a day with unpriced usage stays partial.
       ROUND(CASE WHEN SUM(list_cost) IS NULL AND SUM(unpriced_quantity) = 0 THEN 0 ELSE SUM(list_cost) END, 2) AS usd_list,
       ROUND(SUM(unpriced_quantity), 4) AS unpriced_quantity,
       CASE WHEN resource_type IN ('warehouse', 'cluster') THEN SUM(billed_hours) END AS billed_hours,
       CASE WHEN resource_type IN ('job', 'pipeline') THEN SUM(billed_runs) END AS billed_runs
FROM keyed
GROUP BY usage_date, resource_type, resource_key, workspace_id, resource_id, is_other
ORDER BY usage_date DESC, resource_type, resource_key
