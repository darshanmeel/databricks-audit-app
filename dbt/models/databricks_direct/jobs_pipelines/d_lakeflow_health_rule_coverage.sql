{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:jobs_pipelines', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_health_rule_coverage.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
WITH latest_jobs AS (
  SELECT workspace_id, job_id, name AS job_name, health_rules, delete_time
  FROM {{ source('system_lakeflow', 'jobs') }}
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
) q
