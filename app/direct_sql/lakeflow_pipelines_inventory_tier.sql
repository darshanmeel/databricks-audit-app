-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_pipelines_inventory_tier.sql by tools/build_direct_sql.py; edit the query, never this file.
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
  FROM `system`.`lakeflow`.`pipelines`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC) = 1
)
SELECT p.workspace_id, w.workspace_name, p.pipeline_id, p.pipeline_name, p.pipeline_type,
       p.setting_serverless, p.setting_development, p.setting_continuous, p.setting_photon,
       p.setting_edition, p.setting_channel
FROM latest_pipelines p
LEFT JOIN `system`.`access`.`workspaces_latest` w ON w.workspace_id = p.workspace_id
WHERE p.delete_time IS NULL
ORDER BY p.workspace_id, p.pipeline_type, p.pipeline_name
) q
