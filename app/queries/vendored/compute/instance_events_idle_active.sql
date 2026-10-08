-- query_id: instance_events_idle_active
-- title: Classic-instance idle-vs-active event mix
-- domain: compute   tier: lite
-- reads: system.compute.instance_events
-- requires: SELECT on system.compute; Public Preview (system.compute.instance_events may be empty or disabled per workspace)
-- empty_if: preview_unavailable, compute_scope_gap
-- params: :period_days (default 30) rolling window in days
-- confidence: needs_confirmation
-- confidence_note: system.compute.instance_events is Public Preview and may be empty or disabled in your workspace; if so, treat "no rows" as "not populated", not "no idle instances". Columns are confirmed, but a true idle-vs-active MINUTES calculation needs a per-instance windowed lead/lag over event_time (INSTANCE_READY vs INSTANCE_PLACED) that is not implemented here - this query only summarizes event COUNTS, a coarser signal.
-- read_this: One row = a workspace + node_type + availability_type + instance state + event_type combination and how many events/instances hit it over the window. The columns that matter are state (INSTANCE_READY = idle, INSTANCE_PLACED = in use) and instance_count - a node_type with a large INSTANCE_READY instance_count relative to its INSTANCE_PLACED instance_count is spending a lot of launches sitting idle before being used, but this query counts events, not idle minutes, so treat it as a screening signal, not a precise duration.
-- healthy: n/a - status not computed (see caveats); as a heuristic, INSTANCE_READY instance_count well below INSTANCE_PLACED instance_count for the same node_type/availability_type is the healthy shape.
-- investigate_if: n/a - status not computed (see caveats); as a heuristic, INSTANCE_READY instance_count at or above INSTANCE_PLACED instance_count for the same node_type/availability_type over the window is worth a look - field heuristic, not an authoritative threshold.
-- actions: 1) cross-check the worst node_type/availability_type combos here against auto_termination_minutes in classic_clusters_config_current and lower it (free); 2) shift the workload onto instance pools sized closer to actual concurrent demand, or onto Spot/preemptible where availability_type allows (config); 3) if the same node_type is idle-heavy account-wide, right-size or retire that node type (spend).
-- next: classic_clusters_config_current (to see auto_termination_minutes for clusters using this node_type), instance_pools_idle_capacity (if these instances are pool-managed)
-- caveats: PUBLIC PREVIEW - this table may be empty or disabled in your workspace; read "no rows" as "not populated", never as "no idle instances". state enum: INSTANCE_LAUNCHING / INSTANCE_READY (idle) / INSTANCE_PLACED (in use) / INSTANCE_TERMINATED. event_type enum: INSTANCE_LAUNCHING / STATE_TRANSITION. availability_type enum: ON_DEMAND/SPOT (AWS/Azure), ON_DEMAND/PREEMPTIBLE (GCP). cluster_id is populated ONLY when state=INSTANCE_PLACED (not selected by this query, but relevant if you extend it). Regional - run per metastore region. A true idle-vs-active MINUTES measure requires a per-instance windowed lead/lag over event_time (READY vs PLACED) whose exact form is not implemented here and is needs_confirmation; this query only aggregates event and instance COUNTS, which will not tell you how long any single instance sat idle, only how often idle-state events occurred. No status column is computed for this reason - use the healthy/investigate_if heuristics above as a manual read.
-- Scope is CLASSIC compute only: SQL-warehouse placement events and all serverless instances are excluded, so idle serverless or SQL-warehouse capacity is invisible here and never appears as an idle instance.
SELECT workspace_id, node_type, availability_type, state, event_type,
       COUNT(*) AS event_count,
       COUNT(DISTINCT instance_id) AS instance_count,
       MIN(event_time) AS first_event_time, MAX(event_time) AS last_event_time
FROM system.compute.instance_events
WHERE event_time >= current_timestamp() - INTERVAL :period_days DAYS
GROUP BY workspace_id, node_type, availability_type, state, event_type
ORDER BY event_count DESC, instance_count DESC
