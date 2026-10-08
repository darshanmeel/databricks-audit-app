-- query_id: lakeflow_jobs_no_timeout
-- title: Jobs with no configured timeout, at-risk DBU exposure
-- domain: jobs_pipelines   tier: standard
-- reads: system.lakeflow.jobs, system.billing.usage, system.billing.list_prices, system.access.workspaces_latest
-- requires: SELECT on system.lakeflow, system.billing AND on system.access (the latter only for the workspace_name column - see caveats to drop it); GA (timeout_seconds was added early Dec 2025)
-- empty_if: schema_not_enabled, submit_run_skipped
-- params: :period_days (default 30) cost-lookback window for this job's own DBU/dollar rollup; :crit_no_timeout_usd (default 200) est_usd_list over the cost-lookback window above which a flagged (no-timeout) job reads CRITICAL instead of WARN
-- confidence: needs_confirmation
-- confidence_note: timeout_seconds is not populated before early Dec 2025; timeout_null_reason exposes that so this query degrades to "not assessed" for a job whose gap is really unpopulated history, instead of over-claiming "no timeout" on old records - confirm the NULL semantics on your account. The population_floor split (below) additionally assumes a job with NO timeout, edited or created AFTER timeout_seconds started being populated, can still write timeout_seconds = NULL rather than 0; if Databricks always writes 0 for that case instead, timeout_null_reason = 'after_population' should never occur in practice and the split adds no signal beyond the simpler no_timeout = (NULL OR 0) flag. Not yet confirmed against a live workspace.
-- read_this: One row = a job. no_timeout is true for an active job with no configured (or zero-second) timeout AND no qualifying health rule either; net_dbus/est_usd_list next to it are this job's own DBUs/list-price dollars over the cost-lookback window, for scale regardless of the flag. bounded_by_health_rule is true when the job has no timeout_seconds but IS bounded by a RUN_DURATION_SECONDS/GREATER_THAN health rule instead - such a job is never no_timeout, so the two flags together say which mechanism (if either) applies. timeout_null_reason splits a NULL timeout: 'not_populated' means this workspace had not yet started populating timeout_seconds when the job's row was last changed (not a real gap, drives NOT_ASSESSED); 'after_population' means the row was changed after this workspace started populating the column, so the NULL is read as a genuine no-timeout job and no_timeout counts it fully.
-- healthy: no_timeout is false, for every job that matters - field heuristic.
-- investigate_if: status = WARN - no_timeout is true; status = CRITICAL - no_timeout is true AND est_usd_list is at/above :crit_no_timeout_usd over the cost-lookback window - field heuristic. NOT_ASSESSED is not a pass: it means this job's own NULL timeout predates timeout_seconds being populated in this workspace - read not_assessed_reason.
-- actions: 1) set an explicit timeout on the flagged job (free); 2) add a default job timeout to your job-creation template or CI job-spec linter (config); 3) n/a - fixing this is free; it prevents future spend from a runaway job rather than requiring new spend.
-- next: lakeflow_job_tasks_no_timeout (the task-level version of this same gap), lakeflow_stale_zombie_jobs (a no-timeout job that also never runs is lower priority)
-- not_assessed_reasons: timeout_not_populated: this job's own NULL timeout predates timeout_seconds being populated in this workspace, not a confirmed no-timeout gap
-- caveats: HEALTH RULES - a job with no timeout_seconds but a health_rules entry of metric='RUN_DURATION_SECONDS' and operator='GREATER_THAN' also has a real time limit (Databricks stops the run once it crosses the rule's threshold, the same effect as a timeout) and reads bounded_by_health_rule = true, never no_timeout = true. Any other metric/operator (or an empty/NULL health_rules) does not count, since it does not bound the run's own duration. timeout_seconds is not populated before early Dec 2025, so a NULL is ambiguous (no timeout vs not-yet-populated) for a job with no qualifying health rule. timeout_null_reason splits that ambiguity by population_floor_time: the earliest change_time, over this workspace's FULL system.lakeflow.jobs history, at which any job row already shows a non-NULL timeout_seconds. A NULL-timeout job whose latest row's change_time is BEFORE that floor reads 'not_populated'; at or after the floor reads 'after_population' and counts fully toward no_timeout. If a workspace has never shown a non-NULL timeout_seconds at all, population_floor_time is NULL and every NULL there reads 'not_populated' - the conservative reading, since there is no evidence the column is even populated in that workspace. net_dbus is the exact billed DBUs (usage_unit='DBU'); est_usd_list is an ESTIMATE AT THE EFFECTIVE LIST PRICE (usage_quantity x list_prices.pricing.effective_list.default - DEC-66.1), not your negotiated invoice rate, and it excludes cloud infra/egress cost - treat it as directional. Cost is attributed by (workspace_id, usage_metadata.job_id) over the window (per-job), not per run/event; the rollup is pre-aggregated then LEFT JOINed 1:1 to the job, so result rows are never multiplied. This query is a per-job state check with no time window of its own (a current-state snapshot) for no_timeout/bounded_by_health_rule/timeout_null_reason; net_dbus/est_usd_list are this job's own DBUs/dollars over the configurable cost-lookback window :period_days - that window affects only the added cost columns, never the flag or status. price_basis is 'unpriced' when this job's DBUs included a non-free-usage SKU with no matching list_prices row (net_dbus/est_usd_list then understate its cost), 'free' when all its matched usage is FREE_USAGE SKUs only, and 'priced' otherwise. workspace_name/job_name come from system.access.workspaces_latest / system.lakeflow.jobs, and workspace_name's source is a DIFFERENT system schema: if system.access is not enabled or you cannot read it, this query ERRORS rather than degrading, so drop the workspace_name column and its LEFT JOIN (two lines) and resolve ids with cost_workspace_names separately. One-time SUBMIT_RUN/WORKFLOW_RUN executions never write to system.lakeflow.jobs, so runaway ephemeral or submit-run workloads (a prime no-timeout risk) are entirely invisible to this inventory and undercount the true no-timeout exposure.
WITH latest_jobs AS (
  SELECT workspace_id, job_id, name AS job_name, timeout_seconds, delete_time, change_time,
         -- a RUN_DURATION_SECONDS/GREATER_THAN health rule bounds a run's duration same as a timeout;
         -- health_rules holds at most a couple of entries, so three positions is enough headroom
         COALESCE((try_element_at(health_rules, 1).metric = 'RUN_DURATION_SECONDS' AND try_element_at(health_rules, 1).operator = 'GREATER_THAN')
          OR (try_element_at(health_rules, 2).metric = 'RUN_DURATION_SECONDS' AND try_element_at(health_rules, 2).operator = 'GREATER_THAN')
          OR (try_element_at(health_rules, 3).metric = 'RUN_DURATION_SECONDS' AND try_element_at(health_rules, 3).operator = 'GREATER_THAN'), FALSE) AS has_duration_health_rule
  FROM system.lakeflow.jobs
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),
population_floor AS (
  -- earliest change_time, over this workspace's FULL job SCD history, at which any job row already
  -- shows a non-NULL timeout_seconds -- the point after which a NULL is assumed to mean "explicitly
  -- no bound", not "not yet populated".
  SELECT workspace_id, MIN(change_time) AS population_floor_time
  FROM system.lakeflow.jobs
  WHERE timeout_seconds IS NOT NULL
  GROUP BY workspace_id
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM system.billing.list_prices
),
cost_rollup AS (
  -- Per (workspace_id, job_id) DBUs + effective-list $ over the cost-lookback window. job_id is NOT
  -- globally unique -> keyed on workspace_id + job_id.
  SELECT u.workspace_id,
         u.usage_metadata.job_id AS job_id,
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
    AND u.usage_date >= date_sub(current_date(), :period_days)
    AND u.usage_date <  current_date()
  GROUP BY u.workspace_id, u.usage_metadata.job_id
),
judged AS (
  SELECT lj.workspace_id, lj.job_id, lj.job_name, lj.timeout_seconds, lj.has_duration_health_rule,
         (lj.timeout_seconds IS NULL OR lj.timeout_seconds = 0) AND NOT lj.has_duration_health_rule AS no_timeout,
         CASE
           WHEN lj.timeout_seconds IS NULL AND NOT lj.has_duration_health_rule
                AND (pf.population_floor_time IS NULL OR lj.change_time < pf.population_floor_time)
             THEN 'not_populated'
           WHEN lj.timeout_seconds IS NULL AND NOT lj.has_duration_health_rule
             THEN 'after_population'
         END AS timeout_null_reason
  FROM latest_jobs lj
  LEFT JOIN population_floor pf ON pf.workspace_id = lj.workspace_id
  WHERE lj.delete_time IS NULL
)
SELECT j.workspace_id, w.workspace_name, j.job_id, j.job_name, j.timeout_seconds,
       j.no_timeout, j.has_duration_health_rule AS bounded_by_health_rule, j.timeout_null_reason,
       ROUND(COALESCE(cr.net_dbus, 0), 2)     AS net_dbus,
       ROUND(COALESCE(cr.est_usd_list, 0), 2) AS est_usd_list,
       COALESCE(cr.price_basis, 'priced')     AS price_basis,
       -- status: a flagged job (no_timeout) reads WARN, or CRITICAL when its own spend over the
       -- cost-lookback window is also high (:crit_no_timeout_usd) -- an unflagged or health-rule-
       -- bounded job is OK. NOT_ASSESSED when the NULL itself predates population, not a confirmed gap.
       CASE
         WHEN j.no_timeout AND j.timeout_null_reason = 'not_populated' THEN 'NOT_ASSESSED'
         WHEN j.no_timeout AND COALESCE(cr.est_usd_list, 0) >= :crit_no_timeout_usd THEN 'CRITICAL'
         WHEN j.no_timeout THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE WHEN j.no_timeout AND j.timeout_null_reason = 'not_populated' THEN 'timeout_not_populated' END AS not_assessed_reason
FROM judged j
LEFT JOIN cost_rollup cr ON cr.workspace_id = j.workspace_id AND cr.job_id = j.job_id
LEFT JOIN system.access.workspaces_latest w ON w.workspace_id = j.workspace_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         est_usd_list DESC, j.workspace_id, j.job_id
