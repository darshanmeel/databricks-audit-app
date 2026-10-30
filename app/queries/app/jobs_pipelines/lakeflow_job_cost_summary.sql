-- query_id: lakeflow_job_cost_summary
-- title: Per-job cost roll-up - runs, total, median and max cost per run
-- domain: jobs_pipelines   tier: standard
-- reads: system.billing.usage, system.billing.list_prices, system.lakeflow.jobs
-- requires: SELECT on system.billing, system.lakeflow; GA (billing.usage/list_prices and
--   lakeflow.jobs are generally available)
-- empty_if: schema_not_enabled, no_activity, ingestion_lag
-- params: :period_days (default 30) rolling window in days
-- confidence: needs_confirmation
-- confidence_note: Aggregates the exact per-run attribution and effective-list price join
--   lakeflow_job_run_cost uses (usage_metadata.job_id/job_run_id, DEC-66.1) one level up, to
--   (workspace_id, job_id); neither query has itself been run against a live workspace. Confirm a
--   job you know the run count of for the window shows the matching `runs` here.
-- read_this: One row = one job's cost roll-up over every run lakeflow_job_run_cost attributed to it
--   in this same window: runs (how many distinct job runs had billed DBUs), net_job_dbus /
--   net_list_cost (the total), and est_median_usd_list / est_max_usd_list (is the job
--   expensive because every run costs about the same, or because a few runs spike?).
--   price_basis discloses a pricing-coverage gap the same way lakeflow_job_run_cost does.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: lakeflow_job_run_cost (for the per-run detail behind this roll-up), cost_by_job (for the
--   job's whole-window DBU total across every job_id-attributed usage row, including any without a
--   job_run_id - see caveats), lakeflow_failed_jobs_wasted_dbus (if failures are eating into the total)
-- caveats: This is lakeflow_job_run_cost's own per-run rows (same usage_metadata.job_id/job_run_id
--   attribution, same effective-list price join, DEC-66.1) aggregated one level up to
--   (workspace_id, job_id) - read that query's header for the attribution and shared-job-cluster
--   rules this total inherits unchanged, INCLUDING that a job_id-attributed usage row with no
--   job_run_id is excluded from both queries; net_job_dbus/net_list_cost here can therefore be
--   LESS than the SAME job's whole-window total in cost_by_job or lakeflow_failed_jobs_wasted_dbus,
--   which key on job_id alone and do not require job_run_id - never read this query as the job's
--   authoritative whole-window spend, use cost_by_job for that; this pair is for the per-run
--   breakdown and the run-to-run spread. runs counts only runs with at least one billed DBU row in
--   the window (job_run_timeline is not read here, so a run with zero billed usage - e.g. it failed
--   before any compute started - is not counted and not a gap in this total). net_list_cost is
--   left NULL rather than forced to 0 when nothing priced it; price_basis is 'unpriced' when any run
--   of the job is 'unpriced' (the job total then understates cost), 'free' when no run of the job
--   ever priced (every matched SKU was FREE_USAGE), and 'priced' otherwise - the same three-state
--   rule lakeflow_job_run_cost uses, applied across the job's own runs. median_run_dbus /
--   est_median_usd_list / max_run_dbus / est_max_usd_list are computed over the job's own runs in
--   the window (the exact percentile, interpolated between the two middle runs when the run count
--   is even, and MAX; both skip any run whose own value is NULL because nothing priced it) - a job
--   with one enormous run and many cheap ones shows est_max_usd_list far above est_median_usd_list;
--   a job whose runs cost about the same shows the two close together.
--   job_id is unique only within a workspace, so every grouping is on (workspace_id, job_id).
--   job_name comes from system.lakeflow.jobs (SCD2, latest row by change_time, deleted jobs kept)
--   and is NULL for one-time SUBMIT_RUN / WORKFLOW_RUN executions, which never write to that table.
--   No identities are emitted, so nothing here needs masking. This is an inventory, not a finding -
--   no WARN/CRITICAL band is invented for a dollar total alone.
WITH run_usage AS (
  -- Same per-run attribution and price join as lakeflow_job_run_cost's own run_usage CTE
  -- (usage_metadata.job_id/job_run_id, DEC-66.1 effective-list price) - see that query's header.
  SELECT u.workspace_id,
         u.usage_metadata.job_id     AS job_id,
         u.usage_metadata.job_run_id AS job_run_id,
         SUM(u.usage_quantity)                                          AS net_run_dbus,
         MAX(lp.currency_code)                                          AS currency_code,
         SUM(u.usage_quantity * lp.list_rate)                           AS net_run_list_cost,
         CASE
           WHEN SUM(CASE WHEN lp.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                         THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN lp.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END AS price_basis
  FROM system.billing.usage u
  LEFT JOIN (
    SELECT sku_name, cloud, currency_code, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective (post-promotion) list price - DEC-66.1
    FROM system.billing.list_prices
  ) lp
    ON u.sku_name = lp.sku_name
   AND u.cloud    = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_date >= dateadd(day, -:period_days, current_date())
    AND u.usage_date < current_date()
    AND upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.job_id IS NOT NULL
    AND u.usage_metadata.job_run_id IS NOT NULL
  GROUP BY u.workspace_id, u.usage_metadata.job_id, u.usage_metadata.job_run_id
),
job_totals AS (
  SELECT workspace_id, job_id,
         COUNT(DISTINCT job_run_id)               AS runs,
         SUM(net_run_dbus)                        AS net_job_dbus,
         SUM(net_run_list_cost)                   AS net_list_cost,
         MAX(currency_code)                       AS currency_code,
         percentile(net_run_dbus, 0.5)      AS median_run_dbus,
         MAX(net_run_dbus)                        AS max_run_dbus,
         percentile(net_run_list_cost, 0.5) AS est_median_usd_list,
         MAX(net_run_list_cost)                   AS est_max_usd_list,
         CASE
           WHEN SUM(CASE WHEN price_basis = 'unpriced' THEN 1 ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN price_basis = 'priced' THEN 1 ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END AS price_basis
  FROM run_usage
  GROUP BY workspace_id, job_id
),
latest_jobs AS (
  -- system.lakeflow.jobs is SCD2: one row per change, so take the newest per job.
  SELECT workspace_id, job_id, name AS job_name
  FROM system.lakeflow.jobs
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
)
SELECT t.workspace_id,
       t.job_id,
       j.job_name,                                                -- NULL for submit/workflow runs
       t.runs,
       t.net_job_dbus,
       t.currency_code,
       t.net_list_cost,
       COALESCE(t.price_basis, 'priced') AS price_basis,
       t.median_run_dbus,
       t.max_run_dbus,
       t.est_median_usd_list,
       t.est_max_usd_list
FROM job_totals t
LEFT JOIN latest_jobs j
  ON  j.workspace_id = t.workspace_id
  AND j.job_id       = t.job_id
ORDER BY t.net_list_cost DESC NULLS LAST, t.net_job_dbus DESC
