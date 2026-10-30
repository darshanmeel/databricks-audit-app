-- query_id: lakeflow_job_ownership_orphans
-- title: Jobs running as a human identity, and jobs whose owner or run-as principal is missing
-- domain: jobs_pipelines   tier: standard
-- reads: system.lakeflow.jobs, system.access.workspaces_latest
-- requires: SELECT on system.lakeflow AND on system.access (the latter only for the workspace_name column - see caveats to drop it); GA (system.lakeflow.jobs is generally available; creator_user_name/run_as_user_name/trigger_type/paused are not populated for rows emitted before early December 2025)
-- empty_if: schema_not_enabled, submit_run_skipped
-- params: none - the per-job pattern (human run-as, missing principal) either applies to this job or it does not; there is no per-job count to threshold.
-- confidence: needs_confirmation
-- confidence_note: creator_user_name, run_as_user_name, trigger_type and paused are not populated for rows emitted before early December 2025, and jobs is SCD2 (one row per change) - an untouched job can still carry a fully-unpopulated latest row on an otherwise-current account. A job with NEITHER identity recorded reads NOT_ASSESSED (identity_not_recorded), never a scored missing principal; only a job that recorded one identity but is now missing the other reads principal_missing. FedRAMP/redacted workspaces emit '__REDACTED__' for masked identities; a redacted identity is normalized to NULL before judging and is excluded from principal_missing, so a masking policy never misreads as a missing/deleted principal. Whether a run-as identity is a service principal is inferred from its shape (an application-id UUID, the same heuristic access_runas_escalation.sql uses for run_by/run_as) - there is no principal-type column on this table to confirm it against.
-- read_this: One row = a job. creator_user_name/run_as_user_name are this job's owner and run-as identity (masked - see caveats); run_as_kind reads them as SERVICE_PRINCIPAL or HUMAN. is_scheduled is true when the job also runs on a schedule or other automatic trigger and is not paused - not only on demand. principal_missing is true when the job recorded one identity but the other is gone - not simply never-recorded (that is identity_not_recorded, shown for context and never scored). identity_redacted is true when a FedRAMP/redacted workspace masked an identity - not a missing principal.
-- healthy: run_as_kind = SERVICE_PRINCIPAL and principal_missing is false - field heuristic.
-- investigate_if: status = CRITICAL - the job runs as a human identity (run_as_kind = HUMAN) AND is_scheduled - the worse pattern, since it breaks the moment that person leaves or loses access and runs every scheduled action under one person's privileges; status = WARN - the job runs as a human on demand only, or principal_missing is true - field heuristic; this is a governance posture signal, not a cost figure. NOT_ASSESSED is not a pass: identity_not_recorded is true - neither identity is populated for this job's row yet.
-- actions: 1) move a flagged job - especially a scheduled one - onto a service principal run-as (free/config); 2) for principal_missing, confirm whether the missing owner or run-as was removed from the account - a deleted principal is visible here only when the table drops its name, not when its UUID/name simply stays on the job row - and re-set run_as explicitly (config); 3) require run_as to be set explicitly to a service principal in your job-creation template (config); 4) n/a - this finding does not itself justify new spend.
-- next: lakeflow_health_rule_coverage (for the related governance-coverage picture), lakeflow_stale_zombie_jobs (orphaned jobs are often also stale)
-- not_assessed_reasons: identity_not_recorded: neither creator nor run-as is populated for this job's row yet (short-history account, or an untouched job under SCD2)
-- caveats: creator_user_name, run_as_user_name, trigger_type and paused are not populated for rows emitted before early December 2025, so on a short-history account - or simply an untouched job, since jobs is SCD2 and writes a new row only when a job changes - they are NULL on the latest row; such a job reads identity_not_recorded = true and status = NOT_ASSESSED rather than a false-clean OK. A job that recorded only one identity (the other NULL) reads principal_missing = true instead, since that is a recorded gap, not an unpopulated column. A deleted principal is visible only when the table drops its name (principal_missing); a deleted user or service principal whose name or UUID is still on the job row is not detected - there is no principals source until the SCIM collector (T-44, DEC-56). FedRAMP/redacted workspaces emit '__REDACTED__', which is likewise normalized to NULL before anything is judged, and identity_redacted is set instead of principal_missing, so a masking policy never misreads as a missing/deleted principal. A run-as identity is judged SERVICE_PRINCIPAL when it matches an application-id UUID shape and HUMAN otherwise - Databricks convention, not a column this table carries to confirm it against, so an unusually-named human account or a non-UUID-shaped service principal on your account would be misread; confirm a sample. jobs is SCD2 (one row per change); this takes the latest row per (workspace_id, job_id) by change_time, then excludes delete_time IS NOT NULL rows (must dedupe change-history before counting, or jobs inflate). job_id is unique only within a workspace, so the partition includes workspace_id. trigger_type IS NOT NULL AND NOT paused marks a job that runs on a schedule or another automatic trigger (PERIODIC, FILE_ARRIVAL, TABLE, CONTINUOUS, ...) and is not currently paused, rather than only on demand or paused; a job whose creator and run-as are the same human is no longer read as healthy by itself - a human running their own job, especially a scheduled production one, is exactly the pattern this check flags, and a human creator handing run-as to a service principal is exactly the pattern it treats as healthy. IDENTITY MASKING (DEC-66.3): creator_user_name/run_as_user_name are masked to "<id> <first 2 chars>***" the same way every other vendored finding masks a human identity - a GUID or NULL/'__REDACTED__' passes through unchanged; masking itself is optional (config/settings.yml's privacy.mask_user_identities, off by default). This is a governance posture signal, not a cost figure. system.lakeflow.jobs is a definition table that one-time SUBMIT_RUN/WORKFLOW_RUN executions skip entirely, so jobs run only via submit/workflow runs never appear here and are invisible to this ownership assessment. workspace_name comes from system.access.workspaces_latest, a DIFFERENT system schema: if system.access is not enabled or you cannot read it, this query ERRORS rather than degrading, so drop the workspace_name column and its LEFT JOIN (two lines) and resolve ids with cost_workspace_names separately.
WITH latest_jobs AS (
  SELECT workspace_id, job_id, name AS job_name, creator_user_name, run_as_user_name, trigger_type, paused, delete_time
  FROM system.lakeflow.jobs
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
),
-- normalise unavailable / redacted identity placeholders to NULL so they are never judged a
-- real owner, a real run-as, or a real schedule. identity_redacted is captured BEFORE the
-- normalisation below so a masked identity is never misread as a missing principal.
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
       CASE
         WHEN j.creator_user_name IS NULL OR j.creator_user_name = '__REDACTED__' THEN j.creator_user_name
         WHEN j.creator_user_name RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN j.creator_user_name
         ELSE concat(substr(sha2(lower(trim(j.creator_user_name)), 256), 1, 8), ' ', substr(j.creator_user_name, 1, 2), '***')
       END AS creator_user_name,
       CASE
         WHEN j.run_as_user_name IS NULL OR j.run_as_user_name = '__REDACTED__' THEN j.run_as_user_name
         WHEN j.run_as_user_name RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN j.run_as_user_name
         ELSE concat(substr(sha2(lower(trim(j.run_as_user_name)), 256), 1, 8), ' ', substr(j.run_as_user_name, 1, 2), '***')
       END AS run_as_user_name,
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
LEFT JOIN system.access.workspaces_latest w ON w.workspace_id = j.workspace_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         j.workspace_id, j.job_id
