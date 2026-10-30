-- query_id: lakeflow_job_run_cost
-- title: DBUs and dollar cost per job run, with start time, result and serverless vs classic
-- domain: jobs_pipelines   tier: standard
-- reads: system.billing.usage, system.billing.list_prices, system.lakeflow.job_run_timeline,
--   system.lakeflow.jobs
-- requires: SELECT on system.billing, system.lakeflow; GA (billing.usage/list_prices and
--   lakeflow.job_run_timeline/jobs are generally available)
-- empty_if: schema_not_enabled, no_activity, ingestion_lag
-- params: :period_days (default 30) rolling window in days; :top_n (default 100000) row cap - far
--   above any account's run count in a normal window, because a cut row here would read as a run
--   that cost nothing rather than one simply not shown
-- confidence: needs_confirmation
-- confidence_note: usage_metadata.job_id, usage_metadata.job_run_id and
--   product_features.is_serverless are documented system.billing.usage columns (checked against a
--   live system-catalog schema dump); the price join is the exact effective-list join T-69A
--   verified elsewhere in this library (DEC-66.1), and the run_start / result_state /
--   termination_code lookup follows lakeflow_long_running_runs's own verified job_run_timeline
--   shape. This exact query has not itself been run against a live workspace. Confirm on your
--   account: 1) a job run you know the classic-cluster cost of shows a plausible
--   net_run_dbus/net_list_cost here; 2) a run you know executed on serverless job compute
--   reads compute_type = 'serverless', and one you know ran on a dedicated job cluster reads
--   'classic'; 3) job_run_timeline.run_id for that same run lines up with the run_start you see in
--   the Jobs UI.
-- read_this: One row = one job run with at least one billed DBU row attributed to it
--   (usage_metadata.job_id + job_run_id) in the window. The columns that matter are net_run_dbus
--   and net_list_cost (the run's DBUs and its effective-list-price dollar estimate),
--   compute_type (serverless / classic / mixed) and run_start / result_state / termination_code
--   from the run's own timeline row. price_basis tells you whether a low or $0 net_list_cost
--   is a real free-usage SKU or a pricing-coverage gap. This is the per-run detail behind
--   lakeflow_job_cost_summary's per-job roll-up.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: lakeflow_job_cost_summary (for the per-job roll-up this feeds: runs, total, median and max
--   cost per run), lakeflow_pipeline_cost (the pipeline-side equivalent), lakeflow_failed_jobs_wasted_dbus
--   (if result_state is a failure - the job-level wasted-DBU view), lakeflow_jobs_on_all_purpose
--   (if compute_type is classic and the job shares an all-purpose cluster)
-- caveats: ATTRIBUTION - usage_metadata.job_id/job_run_id attribute a billed system.billing.usage
--   row to the run that was executing when it was recorded; both ids are unique only WITHIN a
--   workspace, so every join/group here is on (workspace_id, job_id, job_run_id), never job_run_id
--   alone. Only usage rows carrying BOTH job_id AND job_run_id feed this query - a job_id-attributed
--   row with no job_run_id (an early-data gap; job_run_id is a newer column than job_id) is
--   excluded here, so net_run_dbus summed across every run of a job (see lakeflow_job_cost_summary)
--   can be LESS than that same job's whole-window total in cost_by_job or
--   lakeflow_failed_jobs_wasted_dbus, which key on job_id alone; use one of those two for the job's
--   true whole-window total, this pair for the per-run breakdown. usage_metadata.job_id/job_run_id
--   populate for jobs-compute (classic and serverless); they are NULL for interactive / SQL-editor
--   usage. SHARED JOB CLUSTERS - a classic job cluster bills DBUs for as long as it runs, and
--   system.billing.usage slices that cluster-time into rows Databricks itself tags with
--   job_id/job_run_id; when several tasks of the SAME run share one job cluster (the common case),
--   every one of them bills under that SAME job_run_id, so their shared DBUs land correctly,
--   undivided, on this one row - nothing here is a manual per-task split. NOT ATTRIBUTABLE: (a) a
--   job whose code runs on a shared ALL-PURPOSE / interactive cluster instead of a dedicated job
--   cluster - its own share is still tagged with job_id/job_run_id and priced here, but the
--   cluster's other concurrent work (interactive users, other jobs) and its idle time between jobs
--   never carries any job_run_id and is invisible to this query - see lakeflow_jobs_on_all_purpose
--   for that placement-premium risk at the cluster level; (b) a job cluster's own startup/init span
--   before the first task begins running, which Databricks bills to the cluster but is not
--   documented as job_run_id-tagged during that init window - a run whose net_run_dbus looks light
--   against its wall-clock duration may be missing that startup slice, not genuinely cheap. PRICE -
--   net_list_cost is SUM(usage_quantity * list_prices.pricing.effective_list.default) per run
--   (DEC-66.1, the one dollar basis this app uses) - an estimate at that basis, never a negotiated
--   or billed dollar (no negotiated-rate source exists anywhere in system.billing), and it is left
--   NULL rather than forced to 0 when nothing priced it; price_basis is 'unpriced' when any
--   non-free-usage SKU billed to this run had no matching list_prices row (net_list_cost then
--   understates cost), 'free' when every matched SKU is a FREE_USAGE SKU (a real $0), and 'priced'
--   otherwise. The price window is open-interval (price_end_time NULL = currently effective).
--   COMPUTE_TYPE - 'serverless' when every DBU on the run has product_features.is_serverless = TRUE,
--   'classic' when none does, 'mixed' when both classic and serverless DBUs billed to the same
--   job_run_id (a job whose tasks use different compute); product_features is sparse, so a row
--   where is_serverless is NULL counts as classic, same as overview_serverless_classic_split.
--   compute_type is NULL in the rare case both the classic and serverless DBU totals net to zero or
--   below (e.g. a full retraction), since neither branch of the CASE above is then true.
--   TIMELINE - run_start/result_state/termination_code come from system.lakeflow.job_run_timeline,
--   read for every run that has usage in run_usage above, with NO window lower bound on the
--   timeline side (only runs already inside :period_days on the usage side reach this join, so this
--   never pulls in an unrelated run) - a run started well before :period_days still shows its TRUE
--   run_start, even when job_run_timeline slices a run over about an hour into multiple hourly rows
--   (the same slicing lakeflow_long_running_runs and lakeflow_job_duration_regression document):
--   run_start is MIN(period_start_time) across every observed slice of the run, never just the
--   first in-window one. result_state/termination_code are read from the run's LAST end row only
--   (the highest period_end_time among rows where result_state IS NOT NULL - the same convention
--   lakeflow_job_reliability uses), never MAX(result_state)/MAX(termination_code): a repaired or
--   retried run writes one end row per attempt sharing run_id, and comparing text alphabetically
--   (MAX) can read an earlier attempt's state (TIMED_OUT beats a later SUCCEEDED) or pair one
--   attempt's code with a different attempt's state; this query instead shows the run's FINAL
--   attempt's own outcome, matching what the Jobs UI shows for that run. in_flight is NULL when the
--   run has NO job_run_timeline row at all (its state is genuinely unknown - ingestion lag or a
--   gap, never read as "running"), TRUE when the run has at least one observed slice but its latest
--   slice has not yet produced an end row (still executing, including a repair attempt still
--   running after an earlier attempt already failed), and FALSE once a final end row has landed;
--   result_state and termination_code are NULL whenever in_flight is TRUE or NULL. Its cost is still
--   governed by the usage side's own usage_date < current_date() rule, so an in-flight run's
--   net_run_dbus/net_list_cost reflect only its billed usage through yesterday. job_name comes from
--   system.lakeflow.jobs (SCD2,
--   latest row by change_time, deleted jobs kept) and is NULL for one-time SUBMIT_RUN / WORKFLOW_RUN
--   executions, which never write to that table. No identities are emitted, so nothing here needs
--   masking. This is an inventory, not a finding - no WARN/CRITICAL band is invented for a dollar
--   figure alone; :top_n orders worst (most expensive) first so a cut row is always the cheapest,
--   never the run you most needed to see.
WITH run_usage AS (
  -- Net DBUs and effective-list dollar estimate per job run (DEC-66.1: pricing.effective_list.default,
  -- the exact price join T-69A settled on). Only usage rows carrying BOTH job_id AND job_run_id.
  SELECT u.workspace_id,
         u.usage_metadata.job_id     AS job_id,
         u.usage_metadata.job_run_id AS job_run_id,
         SUM(u.usage_quantity)                                          AS net_run_dbus,
         SUM(CASE WHEN u.product_features.is_serverless = TRUE
                  THEN u.usage_quantity ELSE 0 END)                     AS serverless_dbus,
         SUM(CASE WHEN u.product_features.is_serverless = TRUE
                  THEN 0 ELSE u.usage_quantity END)                     AS classic_dbus,
         MAX(lp.currency_code)                                          AS currency_code,
         SUM(u.usage_quantity * lp.list_rate)                           AS net_list_cost,
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
run_rows AS (
  -- Every observed timeline row (including in-progress, NULL-result_state slices) for a run that
  -- already has billed usage in run_usage above - no window lower bound on the timeline side (see
  -- caveats): a run started well before :period_days still keeps its TRUE run_start, and a run
  -- sliced hourly past about an hour keeps every one of its observed slices.
  SELECT t.workspace_id, t.job_id, t.run_id, t.period_start_time, t.period_end_time,
         t.result_state, t.termination_code
  FROM system.lakeflow.job_run_timeline t
  JOIN (SELECT DISTINCT workspace_id, job_id, job_run_id FROM run_usage) k
    ON  t.workspace_id = k.workspace_id
    AND t.job_id       = k.job_id
    AND t.run_id       = k.job_run_id
),
run_span AS (
  -- The run's TRUE first observed slice (never just its first in-window one); last_slice_start is
  -- used only to detect an unclosed final attempt below.
  SELECT workspace_id, job_id, run_id,
         MIN(period_start_time) AS run_start,
         MAX(period_start_time) AS last_slice_start
  FROM run_rows
  GROUP BY workspace_id, job_id, run_id
),
run_last_end AS (
  -- The run's LAST attempt's own end row only (highest period_end_time among rows that actually
  -- ended) - never MAX(result_state)/MAX(termination_code), which compares attempts' text
  -- alphabetically and can pair one attempt's code with a different attempt's state.
  SELECT workspace_id, job_id, run_id, result_state, termination_code,
         period_end_time AS last_end
  FROM run_rows
  WHERE result_state IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id, run_id ORDER BY period_end_time DESC
  ) = 1
),
run_state AS (
  -- One row per run that has at least one job_run_timeline row (a run with none simply has no row
  -- here, so it LEFT JOINs to NULL below - genuinely unknown, never "in flight"). in_flight is TRUE
  -- when the run has no landed end row at all, OR its latest observed slice started at/after its
  -- last end row's own end time (a new attempt already running after an earlier attempt's outcome
  -- landed - a repair in progress); result_state/termination_code are then NULL too, since that
  -- earlier attempt's outcome is not this run's current state.
  SELECT s.workspace_id, s.job_id, s.run_id, s.run_start,
         (e.run_id IS NULL OR s.last_slice_start >= e.last_end) AS in_flight,
         CASE WHEN e.run_id IS NULL OR s.last_slice_start >= e.last_end THEN NULL
              ELSE e.result_state END AS result_state,
         CASE WHEN e.run_id IS NULL OR s.last_slice_start >= e.last_end THEN NULL
              ELSE e.termination_code END AS termination_code
  FROM run_span s
  LEFT JOIN run_last_end e
    ON  e.workspace_id = s.workspace_id
    AND e.job_id       = s.job_id
    AND e.run_id       = s.run_id
),
latest_jobs AS (
  -- system.lakeflow.jobs is SCD2: one row per change, so take the newest per job.
  SELECT workspace_id, job_id, name AS job_name
  FROM system.lakeflow.jobs
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
)
SELECT d.workspace_id,
       d.job_id,
       j.job_name,                                                -- NULL for submit/workflow runs
       d.job_run_id,
       m.run_start,
       m.in_flight,
       m.result_state,
       m.termination_code,
       d.net_run_dbus,
       d.currency_code,
       d.net_list_cost,
       COALESCE(d.price_basis, 'priced') AS price_basis,
       CASE
         WHEN d.classic_dbus = 0 AND d.serverless_dbus > 0 THEN 'serverless'
         WHEN d.serverless_dbus = 0 AND d.classic_dbus > 0 THEN 'classic'
         WHEN d.serverless_dbus > 0 AND d.classic_dbus > 0 THEN 'mixed'
         ELSE NULL
       END AS compute_type
FROM run_usage d
LEFT JOIN run_state m
  ON  m.workspace_id = d.workspace_id
  AND m.job_id       = d.job_id
  AND m.run_id       = d.job_run_id
LEFT JOIN latest_jobs j
  ON  j.workspace_id = d.workspace_id
  AND j.job_id       = d.job_id
ORDER BY d.net_list_cost DESC NULLS LAST, d.net_run_dbus DESC
LIMIT :top_n
