{{ config(materialized='table', tags=['dims']) }}
-- T-40. Latest row per warehouse_id from system.compute.warehouses (SCD2, latest row by
-- change_time) -- the same partition the vendored library itself uses for this exact table:
-- app/queries/vendored/compute/sql_warehouse_config_current.sql's own ROW_NUMBER() OVER
-- (PARTITION BY warehouse_id ORDER BY change_time DESC), and
-- app/queries/vendored/compute/compute_warehouse_idle_gaps.sql /
-- compute_warehouse_autoscale_churn.sql's own caveats say "warehouse_id is a globally-unique
-- GUID" -- so this dim is partitioned (and joined onto a finding frame, app/ui/data.add_names)
-- on warehouse_id ALONE, never workspace_id + warehouse_id. workspace_id is still carried as a
-- plain column (every source row has one) for a caller that wants to filter/scope by it.
--
-- created_by is deliberately left off this dim: every vendored query that selects it
-- (sql_warehouse_config_current.sql does not, but the source column is the same raw, unmasked
-- identity field task_cluster_utilization/classic_clusters_config_current's owned_by is) is a
-- raw identity column on the source table, not an object name -- this task resolves OBJECT names
-- only, never a person, so it is left out entirely rather than carried unmasked.
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
        warehouse_id,
        warehouse_name,
        created_by,
        ROW_NUMBER() OVER (
            PARTITION BY warehouse_id
            ORDER BY change_time DESC
        ) AS rn
    FROM {{ source('system_compute', 'warehouses') }}
    WHERE warehouse_id IS NOT NULL
)
SELECT
    workspace_id,
    warehouse_id,
    warehouse_name,
    created_by
FROM ranked
WHERE rn = 1
