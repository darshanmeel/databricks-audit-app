-- query_id: access_network_outbound_denials
-- title: Outbound network policy denials
-- domain: governance_access   tier: deep
-- reads: system.access.outbound_network
-- requires: SELECT on system.access; Public Preview
-- empty_if: schema_not_enabled, preview_unavailable, compute_scope_gap
-- params: :period_days (default 30) rolling window in days (outbound retention allows up to 365d); :warn_denial_count (default 10) denials to one destination that flags WARN; :crit_denial_count (default 50) that flags CRITICAL
-- confidence: confirmed
-- confidence_note: All columns (network_source_type, destination_type, access_type, destination, dns_event.rcode, storage_event.rejection_reason, event_time) are confirmed.
-- read_this: One row = a workspace x source type x destination type x access type x destination combo in the window. The column that matters is denial_count - how many times egress to that destination was blocked from that workspace.
-- healthy: status = OK; denial_count below :warn_denial_count for one destination - field heuristic.
-- investigate_if: status = WARN at/above :warn_denial_count, CRITICAL at/above :crit_denial_count - field heuristic; a new destination_type=STORAGE denial with a non-null storage_rejection_reason is worth a look regardless of volume.
-- actions: 1) confirm the destination is genuinely unexpected rather than a known SaaS/API dependency that needs an egress rule (free); 2) add an explicit egress allow rule for legitimate destinations, or investigate the workload if it is not (config); 3) if repeated denials point at exfiltration attempts, engage security to review the workload's code and rotate any credentials it held (spend).
-- next: access_network_inbound_denials (check the ingress side), access_vector_search_traffic (if the blocked destination is a Vector Search endpoint)
-- caveats: This surfaces DENIALS ONLY, so there is no allow-side traffic baseline and you cannot compute an allow/deny ratio from this alone. dns_event is NULL unless destination_type=DNS; storage_event is NULL unless destination_type=STORAGE. An empty table means no egress policy is configured or the Preview feature has not populated data yet - read that as "not assessed", never as zero exfiltration attempts. This table has no event_date column, only event_time. Outbound retention is 365 days, much longer than the inbound table, so widening :period_days here is meaningful. Regional. WORKSPACE_ID: added to the SELECT/GROUP BY (confirmed present on this table) - this table is already per-workspace, so the app's workspace filter now actually narrows this finding; a destination denied from several workspaces now surfaces as one row per workspace instead of one pooled row, and denial_count/status band on that workspace's own volume, not the account-wide total.
-- This table only records serverless egress denials; egress from classic (non-serverless) compute is not covered here at all, so a clean result does not clear classic-compute workloads.
SELECT workspace_id, network_source_type, destination_type, access_type, destination,
       dns_event.rcode                AS dns_rcode,
       storage_event.rejection_reason AS storage_rejection_reason,
       COUNT(*) AS denial_count,
       MIN(event_time) AS first_event_time, MAX(event_time) AS last_event_time,
       -- status: worst-first band on outbound denial volume per workspace+source+destination combo (field heuristic; :warn_denial_count / :crit_denial_count).
       CASE
         WHEN COUNT(*) >= :crit_denial_count THEN 'CRITICAL'
         WHEN COUNT(*) >= :warn_denial_count THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM system.access.outbound_network
WHERE event_time >= current_timestamp() - INTERVAL :period_days DAYS
GROUP BY 1, 2, 3, 4, 5, 6, 7
ORDER BY denial_count DESC
