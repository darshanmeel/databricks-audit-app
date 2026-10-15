-- query_id: lakeflow_retries_repairs
-- title: Job run retries and repair attempts, with DBU cost
-- domain: jobs_pipelines   tier: standard
-- reads: system.lakeflow.job_run_timeline, system.lakeflow.job_task_run_timeline, system.billing.usage,
--   system.billing.list_prices
-- requires: SELECT on system.lakeflow, system.billing; GA
-- empty_if: schema_not_enabled
-- params: :period_days (default 30) rolling window in days; :warn_total_retries (default 5) total retries for a job in the window that flags WARN; :crit_total_retries (default 20) that flags CRITICAL
-- confidence: needs_confirmation
-- confidence_note: Repairs (end rows per run_id - 1) follow the documented job_run_timeline visibility caveat. Task retries count a task's extra end rows within one attempt of its run, and retry runs are runs whose trigger_type names a retry; confirm on a job with max_retries set that its retries show here.
-- read_this: One row = a job in the window. total_retries = repairs (a run re-run by hand or API, same run_id) + task_retries (a task's automatic retries within one attempt of its run) + retry_runs (runs started as a retry of an earlier run). runs_with_retry is how many runs needed any of these; net_dbus/est_usd_list is the job's overall DBU cost for context.
-- healthy: total_retries low relative to distinct_runs - field heuristic; tune :warn_total_retries for your account.
-- investigate_if: total_retries at/above :warn_total_retries (WARN) or :crit_total_retries (CRITICAL) - field heuristic; a job where runs_with_retry is close to distinct_runs is retrying on (almost) every run, which usually points at a systemic cause.
-- actions: 1) read the failure reason on a recently-retried run and fix the root cause (free); 2) tune the job's retry policy (max retries, backoff) so it fails fast instead of burning attempts on a non-transient error (config); 3) if retries trace back to under-provisioned compute, resize the job cluster (spend).
-- next: lakeflow_failed_jobs_wasted_dbus (for the DBU-waste view of the same failing jobs), lakeflow_termination_taxonomy (for the account-wide termination_code picture behind the retries)
-- caveats: Repairs are (non-NULL-result_state rows per run_id) minus 1, per the documented visibility caveat - end rows only, so a long run's hourly slices are not miscounted. A task end row belongs to the attempt of its run that had not ended yet when the task ended; a task re-run by a repair is therefore a repair, not a task retry. net_dbus is the exact billed DBUs (usage_unit='DBU'); est_usd_list is an ESTIMATE AT THE EFFECTIVE LIST PRICE (usage_quantity x list_prices.pricing.effective_list.default - DEC-66.1), not your negotiated invoice rate, and it excludes cloud infra/egress cost - treat it as directional. Cost is attributed by (workspace_id, job_id) over the window, not per run/retry; the rollup is pre-aggregated then LEFT JOINed, so result rows are never multiplied. price_basis is 'unpriced' when any non-free-usage SKU billed to this job had no matching list_prices row (net_dbus/est_usd_list then understate cost), 'free' when every matched SKU is a FREE_USAGE SKU (a real $0), and 'priced' otherwise.
WITH run_rows AS (
  SELECT workspace_id, job_id, run_id, period_end_time, result_state, trigger_type
  FROM system.lakeflow.job_run_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())
),
per_run AS (
  SELECT workspace_id, job_id, run_id,
         SUM(CASE WHEN result_state IS NOT NULL THEN 1 ELSE 0 END) AS attempt_rows,   -- one end row per attempt
         MAX(CASE WHEN upper(trigger_type) LIKE '%RETRY%' THEN 1 ELSE 0 END) AS is_retry_run
  FROM run_rows
  GROUP BY workspace_id, job_id, run_id
  HAVING SUM(CASE WHEN result_state IS NOT NULL THEN 1 ELSE 0 END) > 0
),
task_ends AS (
  SELECT workspace_id, job_run_id, task_key, period_end_time
  FROM system.lakeflow.job_task_run_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())
    AND result_state IS NOT NULL
),
task_attempts AS (
  -- the attempt of its run a task end row belongs to: how many of the run's end rows came before it
  SELECT t.workspace_id, t.job_run_id, t.task_key, t.period_end_time, COUNT(r.run_id) AS run_attempt
  FROM task_ends t
  LEFT JOIN run_rows r
    ON r.workspace_id = t.workspace_id AND r.run_id = t.job_run_id
   AND r.result_state IS NOT NULL AND r.period_end_time < t.period_end_time
  GROUP BY t.workspace_id, t.job_run_id, t.task_key, t.period_end_time
),
task_retries AS (
  -- within one attempt of a run, a task's end rows beyond its first are automatic retries
  SELECT workspace_id, job_run_id, SUM(task_rows - 1) AS task_retries
  FROM (
    SELECT workspace_id, job_run_id, task_key, run_attempt, COUNT(*) AS task_rows
    FROM task_attempts
    GROUP BY workspace_id, job_run_id, task_key, run_attempt
  ) a
  GROUP BY workspace_id, job_run_id
),
runs AS (
  SELECT pr.*, pr.attempt_rows - 1 AS repairs, COALESCE(tr.task_retries, 0) AS task_retries
  FROM per_run pr
  LEFT JOIN task_retries tr ON tr.workspace_id = pr.workspace_id AND tr.job_run_id = pr.run_id
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
       SUM(pr.repairs)         AS repairs,
       SUM(pr.task_retries)    AS task_retries,
       SUM(pr.is_retry_run)    AS retry_runs,
       SUM(pr.repairs + pr.task_retries + pr.is_retry_run) AS total_retries,
       SUM(CASE WHEN pr.repairs + pr.task_retries + pr.is_retry_run > 0 THEN 1 ELSE 0 END) AS runs_with_retry,
       COALESCE(MAX(cr.net_dbus), 0)     AS net_dbus,
       COALESCE(MAX(cr.est_usd_list), 0) AS est_usd_list,
       COALESCE(MAX(cr.price_basis), 'priced') AS price_basis,
       -- status: worst-first band on total retries in the window (field heuristic; :warn_total_retries / :crit_total_retries).
       CASE
         WHEN SUM(pr.repairs + pr.task_retries + pr.is_retry_run) >= :crit_total_retries THEN 'CRITICAL'
         WHEN SUM(pr.repairs + pr.task_retries + pr.is_retry_run) >= :warn_total_retries THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM runs pr
LEFT JOIN cost_rollup cr
  ON pr.workspace_id = cr.workspace_id AND pr.job_id = cr.job_id
GROUP BY pr.workspace_id, pr.job_id
ORDER BY total_retries DESC
