-- query_id: lakeflow_pipelines_inventory_tier
-- title: Pipeline inventory by type, edition, and key settings
-- domain: jobs_pipelines   tier: lite
-- reads: system.lakeflow.pipelines, system.access.workspaces_latest
-- requires: SELECT on system.lakeflow AND on system.access (the latter only for the workspace_name column - see caveats to drop it); Public Preview (system.lakeflow.pipelines)
-- empty_if: schema_not_enabled, preview_unavailable
-- params: none - this is a point-in-time inventory with no tunable thresholds.
-- confidence: needs_confirmation
-- confidence_note: whether settings is a STRUCT (dot-access, used here) or a MAP (settings['key']) on your account is unverified; confirm before trusting the setting_* columns.
-- read_this: One row = a pipeline. Use this to see your pipeline mix (serverless vs classic, continuous vs triggered, dev vs prod) before drilling into a cost or idle-tail finding.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: lakeflow_pipeline_cost (drill into DBU cost per pipeline), lakeflow_pipeline_idle_tail_duration (drill into idle-tail exposure per pipeline)
-- caveats: pipelines is Public Preview and SCD2 - this table may be empty/disabled on your account, and the query takes the latest row per (workspace_id, pipeline_id) by change_time. setting_edition here is the pipeline product edition (CORE/PRO/ADVANCED); the billing DLT tier is a separate concept that surfaces in billing.product_features.dlt_tier, not here. The settings.serverless / settings.development / settings.continuous / settings.photon / settings.edition / settings.channel dot-access assumes settings is a STRUCT - the key names are confirmed, but whether it is a STRUCT (dot-access) or a MAP (settings['key']) is unverified on your account. workspace_name comes from system.access.workspaces_latest, a DIFFERENT system schema: if system.access is not enabled or you cannot read it, this query ERRORS rather than degrading, so drop the workspace_name column and its LEFT JOIN (two lines) and resolve ids with cost_workspace_names separately.
-- NEEDS CONFIRMATION: settings.<key> dot-access vs settings['<key>'] map-access is UNVERIFIED.
WITH latest_pipelines AS (
  SELECT workspace_id, pipeline_id, name AS pipeline_name, pipeline_type, settings, delete_time,
         settings.serverless  AS setting_serverless,
         settings.development AS setting_development,
         settings.continuous  AS setting_continuous,
         settings.photon      AS setting_photon,
         settings.edition     AS setting_edition,
         settings.channel     AS setting_channel
  FROM system.lakeflow.pipelines
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC) = 1
)
SELECT p.workspace_id, w.workspace_name, p.pipeline_id, p.pipeline_name, p.pipeline_type,
       p.setting_serverless, p.setting_development, p.setting_continuous, p.setting_photon,
       p.setting_edition, p.setting_channel
FROM latest_pipelines p
LEFT JOIN system.access.workspaces_latest w ON w.workspace_id = p.workspace_id
WHERE p.delete_time IS NULL
ORDER BY p.workspace_id, p.pipeline_type, p.pipeline_name
