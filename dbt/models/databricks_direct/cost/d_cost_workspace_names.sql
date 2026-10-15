{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:cost', 'tier:lite', 'databricks_direct']) }}
-- generated from app/queries/vendored/cost/cost_workspace_names.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT workspace_id, workspace_name, status
FROM {{ source('system_access', 'workspaces_latest') }}
ORDER BY workspace_name, workspace_id
) q
