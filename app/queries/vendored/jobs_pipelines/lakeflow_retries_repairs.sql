-- query_id: lakeflow_retries_repairs
-- title: Job run retries and repair attempts, with DBU cost
-- domain: jobs_pipelines   tier: standard
-- reads: system.lakeflow.job_run_timeline, system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.lakeflow, system.billing; GA
-- empty_if: schema_not_enabled
-- params: :period_days (default 30) rolling window in days; :warn_total_retries (default 5) total retries for a job in the window that flags WARN; :crit_total_retries (default 20) that flags CRITICAL
-- confidence: confirmed
-- confidence_note: The retry count formula (non-NULL-result_state rows per run_id, minus 1) was verified against the documented visibility caveat for system.lakeflow.job_run_timeline.
-- read_this: One row = a job in the window. The column that matters is total_retries - extra attempt rows beyond the first, summed across all the job's runs; runs_with_retry tells you how many distinct runs needed at least one retry, and net_dbus/est_usd_list is the job's overall DBU cost for context.
-- healthy: total_retries low relative to distinct_runs - field heuristic; tune :warn_total_retries for your account.
-- investigate_if: total_retries at/above :warn_total_retries (WARN) or :crit_total_retries (CRITICAL) - field heuristic; a job where runs_with_retry is close to distinct_runs is retrying on (almost) every run, which usually points at a systemic cause.
-- actions: 1) read the failure reason on a recently-retried run and fix the root cause (free); 2) tune the job's retry policy (max retries, backoff) so it fails fast instead of burning attempts on a non-transient error (config); 3) if retries trace back to under-provisioned compute, resize the job cluster (spend).
-- next: lakeflow_failed_jobs_wasted_dbus (for the DBU-waste view of the same failing jobs), lakeflow_termination_taxonomy (for the account-wide termination_code picture behind the retries)
-- caveats: Retries are counted as (non-NULL-result_state rows per run_id) minus 1, per the documented visibility caveat - this filters to end rows first so a single long run's intermediate hourly slices are not miscounted as retries. net_dbus is the exact billed DBUs (usage_unit='DBU'); est_usd_list is an ESTIMATE AT THE EFFECTIVE LIST PRICE (usage_quantity x list_prices.pricing.effective_list.default - DEC-66.1), not your negotiated invoice rate, and it excludes cloud infra/egress cost - treat it as directional. Cost is attributed by (workspace_id, job_id) over the window, not per run/retry; the rollup is pre-aggregated then LEFT JOINed, so result rows are never multiplied. price_basis is 'unpriced' when any non-free-usage SKU billed to this job had no matching list_prices row (net_dbus/est_usd_list then understate cost), 'free' when every matched SKU is a FREE_USAGE SKU (a real $0), and 'priced' otherwise.
WITH end_rows AS (
  SELECT workspace_id, job_id, run_id
  FROM system.lakeflow.job_run_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())
    AND result_state IS NOT NULL          -- one non-NULL result_state row per attempt
),
per_run AS (
  SELECT workspace_id, job_id, run_id, COUNT(*) AS attempt_rows
  FROM end_rows GROUP BY workspace_id, job_id, run_id
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM system.billing.list_prices
),
cost_rollup AS (
  -- Pre-aggregated cost per (workspace_id, job_id). job_id is not globally unique, so we key on
  -- workspace_id + job_id. Window mirrors this query's trailing window via usage_date.
  SELECT u.workspace_id,
         u.usage_metadata.job_id                          AS job_id,
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
    AND u.usage_metadata.job_id IS NOT NULL
    AND u.usage_date >= dateadd(day, -:period_days, current_date())
    AND u.usage_date <  current_date()
  GROUP BY u.workspace_id, u.usage_metadata.job_id
)
SELECT pr.workspace_id, pr.job_id,
       COUNT(*)             AS distinct_runs,
       SUM(pr.attempt_rows)    AS total_attempt_rows,
       SUM(pr.attempt_rows - 1) AS total_retries,
       SUM(CASE WHEN pr.attempt_rows > 1 THEN 1 ELSE 0 END) AS runs_with_retry,
       COALESCE(MAX(cr.net_dbus), 0)     AS net_dbus,
       COALESCE(MAX(cr.est_usd_list), 0) AS est_usd_list,
       COALESCE(MAX(cr.price_basis), 'priced') AS price_basis,
       -- status: worst-first band on total retries in the window (field heuristic; :warn_total_retries / :crit_total_retries).
       CASE
         WHEN SUM(pr.attempt_rows - 1) >= :crit_total_retries THEN 'CRITICAL'
         WHEN SUM(pr.attempt_rows - 1) >= :warn_total_retries THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM per_run pr
LEFT JOIN cost_rollup cr
  ON pr.workspace_id = cr.workspace_id AND pr.job_id = cr.job_id
GROUP BY pr.workspace_id, pr.job_id
ORDER BY total_retries DESC
