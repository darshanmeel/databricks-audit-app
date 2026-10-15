-- hand-written; not generated. Databricks twin of dbt/models/tags/tag_workspace.sql: the current row
-- of the workspace tag history (shared _workspace_history.sql block) per (workspace_id, tag_key).
-- Whole snapshot, complete days only, not windowed.
WITH
__WORKSPACE_HISTORY__
SELECT workspace_id, tag_key, display_key, tag_value, is_allocating, top_value, top_values,
       share, coverage, dbus_tagged, dbus_total, reason, valid_from
FROM ws_history
WHERE is_current
