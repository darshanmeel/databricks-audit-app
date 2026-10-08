-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_job_ownership_orphans.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_job_ownership_orphans.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
WITH latest_jobs AS (
  SELECT workspace_id, job_id, name AS job_name, creator_user_name, run_as_user_name, trigger_type, paused, delete_time
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
),
norm AS (
  SELECT workspace_id, job_id, job_name,
         (upper(COALESCE(creator_user_name, '')) = '__REDACTED__'
            OR upper(COALESCE(run_as_user_name, '')) = '__REDACTED__') AS identity_redacted,
         CASE WHEN creator_user_name IS NULL
                OR trim(creator_user_name) = ''
                OR upper(creator_user_name) = '__REDACTED__'
              THEN NULL ELSE creator_user_name END AS creator_user_name,
         CASE WHEN run_as_user_name IS NULL
                OR trim(run_as_user_name) = ''
                OR upper(run_as_user_name) = '__REDACTED__'
              THEN NULL ELSE run_as_user_name END  AS run_as_user_name,
         (trigger_type IS NOT NULL AND trim(trigger_type) <> ''
            AND NOT COALESCE(paused, FALSE)) AS is_scheduled
  FROM latest_jobs
  WHERE delete_time IS NULL
),
judged AS (
  SELECT workspace_id, job_id, job_name, creator_user_name, run_as_user_name, is_scheduled, identity_redacted,
         -- run-as kind: a service-principal application-id UUID, a human login, or unknown
         -- (NULL run_as_user_name -- caught separately by principal_missing/identity_not_recorded below)
         CASE
           WHEN run_as_user_name RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
             THEN 'SERVICE_PRINCIPAL'
           WHEN run_as_user_name IS NOT NULL THEN 'HUMAN'
           ELSE NULL
         END AS run_as_kind,
         -- one identity recorded but the other is gone: nobody clearly owns or runs the job.
         -- Excludes a redacted identity (masked, not missing) and a job with NEITHER identity
         -- recorded (identity_not_recorded below, an unpopulated column, not a gap).
         (NOT identity_redacted AND ((creator_user_name IS NULL) <> (run_as_user_name IS NULL))) AS principal_missing,
         (creator_user_name IS NULL AND run_as_user_name IS NULL) AS identity_not_recorded
  FROM norm
)
SELECT j.workspace_id, w.workspace_name, j.job_id, j.job_name,
       j.creator_user_name AS creator_user_name,
       j.run_as_user_name AS run_as_user_name,
       j.run_as_kind, j.is_scheduled, j.identity_redacted, j.principal_missing, j.identity_not_recorded,
       -- status: a scheduled human run-as is the worse pattern (CRITICAL, matching the double weight
       -- this check has always given it); a manual human run-as or a missing principal is WARN;
       -- neither identity recorded at all is NOT_ASSESSED, not a false-clean OK.
       CASE
         WHEN j.identity_not_recorded THEN 'NOT_ASSESSED'
         WHEN j.run_as_kind = 'HUMAN' AND j.is_scheduled THEN 'CRITICAL'
         WHEN j.run_as_kind = 'HUMAN' THEN 'WARN'
         WHEN j.principal_missing THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE WHEN j.identity_not_recorded THEN 'identity_not_recorded' END AS not_assessed_reason
FROM judged j
LEFT JOIN `system`.`access`.`workspaces_latest` w ON w.workspace_id = j.workspace_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         j.workspace_id, j.job_id
) q
