-- hand-written; not generated. Databricks twin of dbt/models/tags/tag_workspace_history.sql: the
-- workspace's tag per period (SCD type 2), built by the shared _workspace_history.sql block. Whole
-- snapshot, complete days only, not windowed.
WITH
__WORKSPACE_HISTORY__
SELECT workspace_id, tag_key, display_key, tag_value, is_allocating, valid_from, valid_to, is_current,
       top_value, top_values, share, coverage, dbus_tagged, dbus_total, reason
FROM ws_history
