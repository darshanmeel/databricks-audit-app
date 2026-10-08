-- query_id: cost_workspace_names
-- title: Workspace ID to name lookup
-- domain: cost   tier: lite
-- reads: system.access.workspaces_latest
-- requires: SELECT on system.access; GA (system.access.workspaces_latest is generally available)
-- empty_if: schema_not_enabled, preview_unavailable
-- params: none - this is a full snapshot lookup with no date filter
-- confidence: confirmed
-- confidence_note: workspace_id, workspace_name, and status are documented system.access.workspaces_latest columns.
-- read_this: One row = a workspace. The columns that matter are workspace_id (the join key every other cost/identity query keys on) and workspace_name (the human label to show instead of the numeric id) - status distinguishes an active workspace from a deleted one.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: cost_totals_by_sku_day (to join workspace_id to workspace_name onto the cost totals), cost_chargeback_by_identity (to join the same onto the identity chargeback cut)
-- caveats: This is deliberately a separate, tiny lookup - it is not joined into billing.usage here - so that if system.access.workspaces_latest is not enabled (or errors) in your account, only the name resolution is lost and the per-workspace cost data (keyed on workspace_id) is unaffected; do the join yourself and fall back to the numeric workspace_id when a name is missing. status distinguishes active vs deleted workspaces. This is one row per workspace (latest snapshot), not a history.
SELECT workspace_id, workspace_name, status
FROM system.access.workspaces_latest
ORDER BY workspace_name, workspace_id
