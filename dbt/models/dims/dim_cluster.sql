{{ config(materialized='table', tags=['dims']) }}
-- T-40. Latest row per cluster_id from system.compute.clusters (SCD2, latest row by change_time)
-- -- the same partition the vendored library itself uses for this exact table:
-- app/queries/vendored/compute/classic_clusters_config_current.sql's own ROW_NUMBER() OVER
-- (PARTITION BY cluster_id ORDER BY change_time DESC), and both
-- app/queries/vendored/compute/compute_idle_node_ratio.sql and
-- app/queries/vendored/compute/task_cluster_utilization.sql's own caveats/comments say
-- "cluster_id is a globally-unique GUID" -- so this dim is partitioned (and joined onto a
-- finding frame, app/ui/data.add_names) on cluster_id ALONE, never workspace_id + cluster_id.
-- workspace_id is still carried as a plain column (every source row has one), so a caller that
-- does have workspace_id in hand can still filter/scope by it if it wants to.
--
-- cluster_source is carried because app.ui.catalog.SCOPE_JOINS's own client-side workspace scope
-- (compute_idle_node_ratio, node_timeline_utilization) reads bare cluster_id with no name or
-- placement context at all; cluster_source (UI/JOB/API/...) is a cheap, useful companion.
--
-- owned_by is deliberately left off this dim: every vendored query that selects it
-- (classic_clusters_config_current.sql) partial-masks it in-SQL (xx****@****, DEC-48) because it
-- is a raw identity column on the source table, not an object name -- this task resolves OBJECT
-- names only, never a person, so it is left out entirely rather than carried unmasked.
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
        cluster_id,
        cluster_name,
        cluster_source,
        owned_by,
        ROW_NUMBER() OVER (
            PARTITION BY cluster_id
            ORDER BY change_time DESC
        ) AS rn
    FROM {{ source('system_compute', 'clusters') }}
    WHERE cluster_id IS NOT NULL
)
SELECT
    workspace_id,
    cluster_id,
    cluster_name,
    cluster_source,
    owned_by
FROM ranked
WHERE rn = 1
