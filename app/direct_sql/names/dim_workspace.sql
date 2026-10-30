-- hand-written; not generated. One row per workspace id seen in workspaces_latest, 365 days of
-- billing.usage or the regional tables below; env and tags are added by the loader. One query:
-- every table it reads is a prerequisite, so a missing grant fails it and names fall back to ids.
WITH ids_workspaces_latest AS (
    SELECT DISTINCT workspace_id
    FROM system.access.workspaces_latest
    WHERE workspace_id IS NOT NULL
),
ids_usage AS (
    SELECT DISTINCT workspace_id
    FROM system.billing.usage
    WHERE workspace_id IS NOT NULL
      AND usage_date >= __AS_OF_DATE__ - INTERVAL 365 DAYS
),
-- regional presence: full SCD/reference tables, plus the two busiest event tables capped to the
-- last 30 days so this never scans a full audit or query history.
regional_ids AS (
    SELECT workspace_id FROM system.compute.clusters WHERE workspace_id IS NOT NULL
    UNION
    SELECT workspace_id FROM system.compute.warehouses WHERE workspace_id IS NOT NULL
    UNION
    SELECT workspace_id FROM system.compute.instance_pools WHERE workspace_id IS NOT NULL
    UNION
    SELECT workspace_id FROM system.lakeflow.jobs WHERE workspace_id IS NOT NULL
    UNION
    SELECT workspace_id FROM system.lakeflow.pipelines WHERE workspace_id IS NOT NULL
    UNION
    SELECT workspace_id FROM system.serving.served_entities WHERE workspace_id IS NOT NULL
    UNION
    SELECT workspace_id FROM system.lakeflow.job_run_timeline
    WHERE workspace_id IS NOT NULL AND period_start_time >= __AS_OF_DATE__ - INTERVAL 30 DAYS
    UNION
    SELECT workspace_id FROM system.query.history
    WHERE workspace_id IS NOT NULL AND start_time >= __AS_OF_DATE__ - INTERVAL 30 DAYS
),
all_ids AS (
    SELECT workspace_id FROM ids_workspaces_latest
    UNION
    SELECT workspace_id FROM ids_usage
    UNION
    SELECT workspace_id FROM regional_ids
),
latest_ws AS (
    SELECT workspace_id, workspace_name, workspace_url
    FROM (
        SELECT
            workspace_id,
            workspace_name,
            workspace_url,
            ROW_NUMBER() OVER (PARTITION BY workspace_id ORDER BY create_time DESC) AS rn
        FROM system.access.workspaces_latest
    ) w
    WHERE rn = 1
)
SELECT
    all_ids.workspace_id,
    latest_ws.workspace_name AS name,
    latest_ws.workspace_url AS url,
    CASE
        WHEN (SELECT COUNT(*) FROM regional_ids) = 0 THEN CAST(NULL AS BOOLEAN)
        WHEN r.workspace_id IS NOT NULL THEN TRUE
        ELSE FALSE
    END AS in_snapshot_region,
    (u.workspace_id IS NOT NULL) AS billed_in_snapshot
FROM all_ids
LEFT JOIN latest_ws ON latest_ws.workspace_id = all_ids.workspace_id
LEFT JOIN regional_ids r ON r.workspace_id = all_ids.workspace_id
LEFT JOIN ids_usage u ON u.workspace_id = all_ids.workspace_id
ORDER BY all_ids.workspace_id
