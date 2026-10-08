-- query_id: cost_by_job
-- title: DBU cost by job
-- domain: cost   tier: standard
-- reads: system.billing.usage, system.billing.list_prices, system.lakeflow.jobs, system.access.workspaces_latest
-- requires: SELECT on system.billing, system.lakeflow, system.access; billing usage/list_prices and system.lakeflow.jobs are GA, workspaces_latest is Public Preview
-- empty_if: ingestion_lag
-- params: :period_days (default 30) rolling window in days; :warn_job_dbus_per_day (default 50) DBUs/day on a single job that flags WARN; :crit_job_dbus_per_day (default 200) DBUs/day that flags CRITICAL
-- confidence: confirmed
-- confidence_note: usage_metadata.job_id and product_features.is_serverless are documented system.billing.usage columns.
-- read_this: One row = a day + workspace + job's DBU cost. The columns that matter are job_id/job_name (which job) and net_usage_quantity (its DBU burn that day, priced at usd_list) - distinct_runs lets you tell a job that is expensive because it runs constantly apart from one that is expensive because a single run is heavy. price_basis (free/priced/unpriced) discloses whether usd_list is a real $0 (free-usage SKU) or understated by a pricing-coverage gap. status is a SPEND-MAGNITUDE rank, not a waste signal: cross-check lakeflow_job_compute_pressure or compute_idle_node_ratio for this job's own clusters before treating a flagged row as wasteful rather than simply large.
-- healthy: net_usage_quantity below :warn_job_dbus_per_day DBUs/day per job (field heuristic - tune :warn_job_dbus_per_day for your account).
-- investigate_if: net_usage_quantity at/above :warn_job_dbus_per_day (WARN) or :crit_job_dbus_per_day (CRITICAL) DBUs/day - field heuristic; a job that is consistently in-band and just large may be fine, a job whose per-run cost keeps climbing is the one to open first.
-- actions: 1) resolve job_id to name/owner/run_as via system.lakeflow.jobs and confirm the job is still needed at this frequency (free); 2) move the job off an always-on all-purpose cluster onto job-scoped compute, or tune its cluster size/autoscaling (config); 3) if the job is genuinely compute-heavy and correctly sized, consider a committed-use discount for that capacity (spend).
-- next: lakeflow_jobs_on_all_purpose (if this job is paying the all-purpose placement premium), lakeflow_failed_runs (if distinct_runs is high relative to net_usage_quantity - retries may be inflating the cost)
-- caveats: usage_metadata.job_id populates for jobs-compute (classic and serverless); it is NULL for interactive / SQL-editor lines and those are excluded here by construction. job_name resolves from the latest (by change_time) system.lakeflow.jobs row (SCD2) and may be null if the job has since been deleted; join job_id -> system.lakeflow.jobs yourself for run_as and other job detail. is_serverless separates jobs-serverless from classic jobs compute, which is the placement-premium signal. usage_quantity is DBU; usd_list is an ESTIMATE AT THE EFFECTIVE LIST PRICE (usage_quantity x list_prices.pricing.effective_list.default - DEC-66.1), not your negotiated invoice rate, priced the same way every other cost_* query in this set prices. price_basis is 'unpriced' when any non-free-usage SKU billed to this job/day had no matching list_prices row (usd_list then understates cost), 'free' when every matched SKU is a FREE_USAGE SKU (a real $0), and 'priced' otherwise. workspace_name comes from the Public Preview workspaces_latest table and may be null.
SELECT u.usage_date, u.cloud, u.workspace_id, w.workspace_name, u.billing_origin_product,
       u.usage_metadata.job_id AS job_id,
       j.job_name,
       u.product_features.is_serverless AS is_serverless,
       SUM(u.usage_quantity) AS net_usage_quantity,
       ROUND(SUM(u.usage_quantity * COALESCE(p.list_rate, 0)), 2) AS usd_list,
       -- price_basis: a real $0 (free-usage SKU) vs a pricing-coverage gap (usd_list understates cost).
       CASE
         WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                       THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
         WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
         ELSE 'priced'
       END AS price_basis,
       COUNT(DISTINCT u.usage_metadata.job_run_id) AS distinct_runs,
       -- status: magnitude band on daily DBU cost per job (field heuristic; :warn_job_dbus_per_day / :crit_job_dbus_per_day) - size, not waste.
       CASE
         WHEN SUM(u.usage_quantity) IS NULL THEN 'NOT_ASSESSED'
         WHEN SUM(u.usage_quantity) >= :crit_job_dbus_per_day THEN 'CRITICAL'
         WHEN SUM(u.usage_quantity) >= :warn_job_dbus_per_day THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM system.billing.usage u
LEFT JOIN (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM system.billing.list_prices
) p
  ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
  AND u.usage_end_time >= p.price_start_time
  AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
LEFT JOIN (
  SELECT workspace_id, job_id, name AS job_name
  FROM system.lakeflow.jobs
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
) j ON j.workspace_id = u.workspace_id AND j.job_id = u.usage_metadata.job_id
LEFT JOIN system.access.workspaces_latest w ON w.workspace_id = u.workspace_id
WHERE u.usage_date >= dateadd(day, -:period_days, current_date())
  AND u.usage_date < current_date()
  AND u.usage_unit = 'DBU'
  AND u.usage_metadata.job_id IS NOT NULL
GROUP BY u.usage_date, u.cloud, u.workspace_id, w.workspace_name, u.billing_origin_product,
         u.usage_metadata.job_id, j.job_name, u.product_features.is_serverless
ORDER BY net_usage_quantity DESC
