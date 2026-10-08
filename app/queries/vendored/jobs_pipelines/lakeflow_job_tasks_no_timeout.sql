-- query_id: lakeflow_job_tasks_no_timeout
-- title: Job tasks with no configured timeout, at-risk DBU exposure
-- domain: jobs_pipelines   tier: standard
-- reads: system.lakeflow.job_tasks, system.lakeflow.jobs, system.billing.usage, system.billing.list_prices, system.access.workspaces_latest
-- requires: SELECT on system.lakeflow, system.billing AND on system.access (the latter only for the workspace_name column - see caveats to drop it); GA (job_tasks.timeout_seconds was added late Nov 2025)
-- empty_if: schema_not_enabled, submit_run_skipped
-- params: :period_days (default 30) billing look-back window for this task's parent job's own cost rollup only (does not change this query's grain/filters); :crit_no_timeout_usd (default 200) est_usd_list over the cost-lookback window above which a flagged (no-timeout) task reads CRITICAL instead of WARN
-- confidence: needs_confirmation
-- confidence_note: timeout_seconds is not populated before late Nov 2025; timeout_null_reason exposes that so a task whose gap predates population degrades instead of reading NULL as "no timeout". net_dbus/est_usd_list here are an upper-bound exposure figure, not the exact cost of this one task - see caveats. The population_floor split and the parent-job exclusion (below) both assume a task with NO timeout, edited or created after timeout_seconds started being populated, can still write timeout_seconds = NULL rather than 0; not yet confirmed against a live workspace.
-- read_this: One row = a task, of a job that still exists. no_timeout is true for an active task with no configured (or zero-second) timeout AND no qualifying health rule either; net_dbus/est_usd_list next to it are the at-risk DBUs of the PARENT JOB (any of its tasks that do have a timeout are included too - see caveats). bounded_by_health_rule is true when the task has no timeout_seconds but IS bounded by a RUN_DURATION_SECONDS/GREATER_THAN health rule instead - such a task is never no_timeout. timeout_null_reason splits a NULL timeout: 'not_populated' means this workspace had not yet started populating timeout_seconds when the task's row was last changed (not a real gap, drives NOT_ASSESSED); 'after_population' means a genuine no-timeout task, counted fully in no_timeout. parent_gone is true when this task's parent job is deleted or no longer exists - nobody can act on those, so it always reads NOT_ASSESSED regardless of no_timeout.
-- healthy: no_timeout is false, for every task that matters - field heuristic.
-- investigate_if: status = WARN - no_timeout is true; status = CRITICAL - no_timeout is true AND the parent job's own est_usd_list is at/above :crit_no_timeout_usd over the cost-lookback window - field heuristic. NOT_ASSESSED is not a pass: read not_assessed_reason (either the parent job is gone, or this task's own NULL timeout predates timeout_seconds being populated).
-- actions: 1) set an explicit timeout on the flagged task in the job UI/API (free); 2) add a default task timeout to your job-creation template or CI job-spec linter (config); 3) n/a - fixing this is free; it prevents future spend rather than requiring new spend.
-- next: lakeflow_jobs_no_timeout (the job-level version of this same gap), lakeflow_tasks_near_timeout (for tasks that DO have a timeout but are running close to it)
-- not_assessed_reasons: timeout_not_populated: this task's own NULL timeout predates timeout_seconds being populated in this workspace, not a confirmed no-timeout gap; orphaned_parent: this task's parent job is deleted or no longer exists, so nobody can act on it
-- caveats: HEALTH RULES - a task with no timeout_seconds but a health_rules entry of metric='RUN_DURATION_SECONDS' and operator='GREATER_THAN' also has a real time limit (Databricks stops the run once it crosses the rule's threshold, the same effect as a timeout) and reads bounded_by_health_rule = true, never no_timeout = true. Any other metric/operator (or an empty/NULL health_rules) does not count, since it does not bound the run's own duration. job_tasks is SCD2, so this takes the latest row per (workspace_id, job_id, task_key) by change_time; task_key is unique only within a job. A task whose parent job is deleted or has no current system.lakeflow.jobs row at all reads parent_gone = true and status = NOT_ASSESSED - nobody can set a timeout on a task of a job that no longer exists. timeout_null_reason splits the not-populated-before-late-Nov-2025 ambiguity by population_floor_time: the earliest change_time, over this workspace's FULL system.lakeflow.job_tasks history, at which any task row already shows a non-NULL timeout_seconds. A NULL-timeout task whose latest row's change_time is BEFORE that floor reads 'not_populated'; at or after the floor reads 'after_population' and counts fully toward no_timeout. If a workspace has never shown a non-NULL timeout_seconds at all, population_floor_time is NULL and every NULL there reads 'not_populated'. net_dbus is the exact billed DBUs (usage_unit='DBU'); est_usd_list is an ESTIMATE AT THE EFFECTIVE LIST PRICE (usage_quantity x list_prices.pricing.effective_list.default - DEC-66.1), not your negotiated invoice rate, and it excludes cloud infra/egress cost - treat it as directional. ATTRIBUTION CAVEAT: this query is grained on TASKS, but system.billing.usage exposes only job_id (no task_key), so DBUs cannot be split per task. net_dbus/est_usd_list here are the parent job's AT-RISK total: the job's whole DBUs over the window, repeated on every no-timeout task row of that job. A qualifying job's cost includes any of its tasks that DO have a timeout, so this over-attributes - treat it as an upper bound on the exposure, not the exact cost of this one task. price_basis is 'unpriced' when the parent job's DBUs included a non-free-usage SKU with no matching list_prices row, 'free' when all its matched usage is FREE_USAGE SKUs only, and 'priced' otherwise. workspace_name/job_name come from system.access.workspaces_latest / system.lakeflow.jobs, and workspace_name's source is a DIFFERENT system schema: if system.access is not enabled or you cannot read it, this query ERRORS rather than degrading, so drop the workspace_name column and its LEFT JOIN (two lines) and resolve ids with cost_workspace_names separately. Tasks run via one-time SUBMIT_RUN/WORKFLOW_RUN are excluded because those runs skip the system.lakeflow.job_tasks dimension table, so any no-timeout exposure in submit/workflow-run jobs is invisible here.
WITH latest_jobs AS (
  SELECT workspace_id, job_id, name AS job_name, delete_time
  FROM system.lakeflow.jobs
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),
latest_tasks AS (
  SELECT workspace_id, job_id, task_key, timeout_seconds, delete_time, change_time,
         -- a RUN_DURATION_SECONDS/GREATER_THAN health rule bounds a run's duration same as a timeout;
         -- health_rules holds at most a couple of entries, so three positions is enough headroom
         COALESCE((try_element_at(health_rules, 1).metric = 'RUN_DURATION_SECONDS' AND try_element_at(health_rules, 1).operator = 'GREATER_THAN')
          OR (try_element_at(health_rules, 2).metric = 'RUN_DURATION_SECONDS' AND try_element_at(health_rules, 2).operator = 'GREATER_THAN')
          OR (try_element_at(health_rules, 3).metric = 'RUN_DURATION_SECONDS' AND try_element_at(health_rules, 3).operator = 'GREATER_THAN'), FALSE) AS has_duration_health_rule
  FROM system.lakeflow.job_tasks
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id, task_key ORDER BY change_time DESC) = 1
),
population_floor AS (
  SELECT workspace_id, MIN(change_time) AS population_floor_time
  FROM system.lakeflow.job_tasks
  WHERE timeout_seconds IS NOT NULL
  GROUP BY workspace_id
),
scoped_tasks AS (
  -- mark tasks whose parent job is deleted or has no current jobs row at all
  SELECT lt.workspace_id, lt.job_id, lj.job_name, lt.task_key, lt.timeout_seconds, lt.change_time,
         lt.has_duration_health_rule,
         (lj.job_id IS NULL OR lj.delete_time IS NOT NULL) AS parent_gone
  FROM latest_tasks lt
  LEFT JOIN latest_jobs lj ON lj.workspace_id = lt.workspace_id AND lj.job_id = lt.job_id
  WHERE lt.delete_time IS NULL
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM system.billing.list_prices
),
-- Pre-aggregated cost per (workspace_id, job_id) over the billing look-back window -- the PARENT
-- job's own whole spend, shared across every no-timeout task of that job (see caveats).
cost_rollup AS (
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
    AND u.usage_date >= date_add(current_date(), -:period_days)
    AND u.usage_date <  current_date()
  GROUP BY u.workspace_id, u.usage_metadata.job_id
),
judged AS (
  SELECT st.workspace_id, st.job_id, st.job_name, st.task_key, st.timeout_seconds,
         st.has_duration_health_rule, st.parent_gone,
         NOT st.parent_gone AND (st.timeout_seconds IS NULL OR st.timeout_seconds = 0)
           AND NOT st.has_duration_health_rule AS no_timeout,
         CASE
           WHEN NOT st.parent_gone AND st.timeout_seconds IS NULL AND NOT st.has_duration_health_rule
                AND (pf.population_floor_time IS NULL OR st.change_time < pf.population_floor_time)
             THEN 'not_populated'
           WHEN NOT st.parent_gone AND st.timeout_seconds IS NULL AND NOT st.has_duration_health_rule
             THEN 'after_population'
         END AS timeout_null_reason
  FROM scoped_tasks st
  LEFT JOIN population_floor pf ON pf.workspace_id = st.workspace_id
)
SELECT j.workspace_id, w.workspace_name, j.job_id, j.job_name, j.task_key, j.timeout_seconds,
       j.no_timeout, j.has_duration_health_rule AS bounded_by_health_rule, j.timeout_null_reason,
       j.parent_gone,
       ROUND(COALESCE(cr.net_dbus, 0), 2)     AS net_dbus,
       ROUND(COALESCE(cr.est_usd_list, 0), 2) AS est_usd_list,
       COALESCE(cr.price_basis, 'priced')     AS price_basis,
       -- status: a flagged task (no_timeout) reads WARN, or CRITICAL when its parent job's own spend
       -- over the cost-lookback window is also high (:crit_no_timeout_usd). An orphaned-parent task,
       -- or one whose NULL predates population, is NOT_ASSESSED - nobody can act on either.
       CASE
         WHEN j.parent_gone THEN 'NOT_ASSESSED'
         WHEN j.no_timeout AND j.timeout_null_reason = 'not_populated' THEN 'NOT_ASSESSED'
         WHEN j.no_timeout AND COALESCE(cr.est_usd_list, 0) >= :crit_no_timeout_usd THEN 'CRITICAL'
         WHEN j.no_timeout THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN j.parent_gone THEN 'orphaned_parent'
         WHEN j.no_timeout AND j.timeout_null_reason = 'not_populated' THEN 'timeout_not_populated'
       END AS not_assessed_reason
FROM judged j
LEFT JOIN cost_rollup cr ON cr.workspace_id = j.workspace_id AND cr.job_id = j.job_id
LEFT JOIN system.access.workspaces_latest w ON w.workspace_id = j.workspace_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         est_usd_list DESC, j.workspace_id, j.job_id, j.task_key
