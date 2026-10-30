{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:jobs_pipelines', 'tier:lite', 'databricks_direct']) }}
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_pipelines_inventory_tier.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
WITH latest_pipelines AS (
  SELECT workspace_id, pipeline_id, name AS pipeline_name, pipeline_type, settings, delete_time,
         settings.serverless  AS setting_serverless,
         settings.development AS setting_development,
         settings.continuous  AS setting_continuous,
         settings.photon      AS setting_photon,
         settings.edition     AS setting_edition,
         settings.channel     AS setting_channel
  FROM {{ source('system_lakeflow', 'pipelines') }}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC) = 1
)
SELECT p.workspace_id, w.workspace_name, p.pipeline_id, p.pipeline_name, p.pipeline_type,
       p.setting_serverless, p.setting_development, p.setting_continuous, p.setting_photon,
       p.setting_edition, p.setting_channel
FROM latest_pipelines p
LEFT JOIN {{ source('system_access', 'workspaces_latest') }} w ON w.workspace_id = p.workspace_id
WHERE p.delete_time IS NULL
ORDER BY p.workspace_id, p.pipeline_type, p.pipeline_name
) q
