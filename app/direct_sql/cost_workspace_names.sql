-- generated from dbt/models/databricks_direct/cost/d_cost_workspace_names.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_workspace_names.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT workspace_id, workspace_name, status
FROM `system`.`access`.`workspaces_latest`
ORDER BY workspace_name, workspace_id
) q
