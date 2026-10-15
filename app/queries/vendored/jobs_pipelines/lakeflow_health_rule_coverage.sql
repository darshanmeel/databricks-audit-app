-- query_id: lakeflow_health_rule_coverage
-- title: Job health-rule coverage across active jobs
-- domain: jobs_pipelines   tier: standard
-- reads: system.lakeflow.jobs
-- requires: SELECT on system.lakeflow; GA (system.lakeflow.jobs is generally available; health_rules was added late Nov 2025)
-- empty_if: schema_not_enabled, submit_run_skipped
-- params: none - a job either has a health rule or it does not; there is no per-job count to threshold.
-- confidence: needs_confirmation
-- confidence_note: health_rules is documented as an array/struct column; whether CARDINALITY(health_rules) > 0 is the right "has a rule" test on this account is unverified, and the column is not populated before late Nov 2025.
-- read_this: One row = a job. health_rule_count is the number of health rules configured on it, or NULL when health_rules itself is not populated for this job's row yet - read that as "not assessed", never as "no rule".
-- healthy: health_rule_count at least 1 - field heuristic.
-- investigate_if: status = WARN - health_rule_count is 0 (a populated row with no rule configured). NOT_ASSESSED is not a pass: health_rules is not populated for this job's row yet - read not_assessed_reason.
-- actions: 1) add a duration/failure health rule to the flagged job, especially a high-DBU one (free); 2) make health-rule configuration part of your job-creation checklist or template (config); 3) n/a - this finding does not itself justify new spend.
-- next: lakeflow_job_ownership_orphans (for the related governance-coverage picture), lakeflow_jobs_no_timeout (another active-job config gap worth checking alongside)
-- not_assessed_reasons: health_rules_not_populated: health_rules is not populated for this job's row yet (short-history account, or an untouched job under SCD2)
-- caveats: health_rules was not populated before late Nov 2025, so on a short-history account the column is NULL for every job; a job whose row is still NULL there reads NOT_ASSESSED rather than a false "no rule configured". Only a job that exists in a populated window with an empty/absent rule set reads a confident WARN. jobs is SCD2 (one row per change); this takes the latest row per (workspace_id, job_id) by change_time and excludes delete_time IS NOT NULL rows, since counting every change-history row would inflate the job count. job_id is unique only within a workspace, so grouping is by workspace_id + job_id. The "has a rule configured" test (CARDINALITY(health_rules) > 0) assumes health_rules is an array-typed column; if it is a scalar/string on your account, the query still runs and a NULL column still reads NOT_ASSESSED, but confirm the split against your workspace's actual column type before trusting it.
-- system.lakeflow.jobs holds only defined Lakeflow Jobs; workloads launched as one-time SUBMIT_RUN or WORKFLOW_RUN never write to this dimension table, so those runs are absent from this inventory entirely and their health-rule coverage is invisible here.
WITH latest_jobs AS (
  SELECT workspace_id, job_id, name AS job_name, health_rules, delete_time
  FROM system.lakeflow.jobs
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
)
SELECT lj.workspace_id, lj.job_id, lj.job_name,
       CASE WHEN lj.health_rules IS NOT NULL THEN CARDINALITY(lj.health_rules) END AS health_rule_count,
       -- status: a populated row with no rule reads WARN; a NULL column (not yet populated) is
       -- NOT_ASSESSED, never a false-clean OK or a false "no rule configured".
       CASE
         WHEN lj.health_rules IS NULL THEN 'NOT_ASSESSED'
         WHEN CARDINALITY(lj.health_rules) = 0 THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE WHEN lj.health_rules IS NULL THEN 'health_rules_not_populated' END AS not_assessed_reason
FROM latest_jobs lj
WHERE lj.delete_time IS NULL
ORDER BY CASE status WHEN 'WARN' THEN 0 WHEN 'NOT_ASSESSED' THEN 1 ELSE 2 END, lj.workspace_id, lj.job_id
