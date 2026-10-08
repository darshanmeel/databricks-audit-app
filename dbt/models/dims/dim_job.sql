{{ config(materialized='table', tags=['dims']) }}
-- T-40. Latest row per (workspace_id, job_id) from system.lakeflow.jobs (SCD2, latest row by
-- change_time) -- exactly the join app/queries/vendored/cost/cost_by_job.sql's own caveat
-- names: "Names/owner are not here - join job_id -> system.lakeflow.jobs (SCD2, take the latest
-- row by change_time) for the job name and run_as." Only the job NAME is carried onto this dim.
--
-- job_id is unique only WITHIN a workspace (every vendored query that joins on it says so --
-- e.g. lakeflow_job_ownership_orphans.sql, lakeflow_failed_jobs_wasted_dbus.sql), so the ROW_
-- NUMBER partition and every downstream join key on (workspace_id, job_id), never job_id alone.
--
-- OWNER / RUN-AS COLUMNS ARE CARRIED UNMASKED (DEC-55, your decision 2026-09-22). These are raw
-- identity fields at the source -- system.lakeflow.jobs / compute.clusters / compute.warehouses /
-- lakeflow.pipelines do not mask them, unlike identity_metadata.run_as, which every vendored query
-- partial-masks in-SQL (DEC-48). They are carried here because this runs single-user against your
-- own account and "who owns this job" is the first question a cost finding raises. Consequence,
-- stated once: a masked finding joined to this dim re-identifies the person, and any CSV or
-- findings.json export carries real email addresses. Access control moves to Databricks RLS when
-- the databricks target is live (PLAN D3).
WITH ranked AS (
    SELECT
        workspace_id,
        job_id,
        name,
        run_as,
        run_as_user_name,
        creator_user_name,
        ROW_NUMBER() OVER (
            PARTITION BY workspace_id, job_id
            ORDER BY change_time DESC
        ) AS rn
    FROM {{ source('system_lakeflow', 'jobs') }}
    WHERE workspace_id IS NOT NULL AND job_id IS NOT NULL
)
SELECT
    workspace_id,
    job_id,
    name,
    run_as,
    run_as_user_name,
    creator_user_name
FROM ranked
WHERE rn = 1
