{{ config(materialized='table', tags=['dims']) }}
-- T-40. Latest row per (workspace_id, pipeline_id) from system.lakeflow.pipelines (SCD2, latest
-- row by change_time) -- the same partition the vendored library itself uses for this exact
-- table: app/queries/vendored/jobs_pipelines/lakeflow_pipelines_inventory_tier.sql /
-- lakeflow_pipeline_cost.sql / lakeflow_pipeline_idle_tail_duration.sql's own QUALIFY
-- ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC), and
-- lakeflow_pipeline_cost.sql's own caveat says "dlt_pipeline_id is unique only WITHIN a
-- workspace, so every join/group here is on (workspace_id, pipeline_id), never pipeline_id
-- alone" -- so this dim is partitioned (and joined onto a finding frame, app/ui/data.add_names)
-- on (workspace_id, pipeline_id), never pipeline_id alone.
--
-- created_by / run_as are deliberately left off this dim: system.lakeflow.pipelines carries them
-- raw (lakeflow_pipelines_inventory_tier.sql selects created_by/run_as straight off the source
-- with no xx****@**** masking at all, unlike identity_metadata.run_as elsewhere, DEC-48) -- this
-- task resolves OBJECT names only, never a person, so they are left out entirely.
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
        pipeline_id,
        name AS pipeline_name,
        created_by,
        run_as,
        ROW_NUMBER() OVER (
            PARTITION BY workspace_id, pipeline_id
            ORDER BY change_time DESC
        ) AS rn
    FROM {{ source('system_lakeflow', 'pipelines') }}
    WHERE workspace_id IS NOT NULL AND pipeline_id IS NOT NULL
)
SELECT
    workspace_id,
    pipeline_id,
    pipeline_name,
    created_by,
    run_as
FROM ranked
WHERE rn = 1
