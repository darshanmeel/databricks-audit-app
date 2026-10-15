-- query_id: access_network_inbound_denials
-- title: Inbound network policy denials
-- domain: governance_access   tier: deep
-- reads: system.access.inbound_network
-- requires: SELECT on system.access; Public Preview
-- empty_if: schema_not_enabled, preview_unavailable
-- params: :period_days (default 30) rolling window in days (inbound retention caps at 30d regardless); :warn_denial_count (default 10) denials for one rule+path+principal+IP that flags WARN; :crit_denial_count (default 50) that flags CRITICAL
-- confidence: needs_confirmation
-- confidence_note: The nested subfield source.ip is unverified - Databricks' docs describe the source struct as having subfields "including ip, private-link" but "including" is non-exhaustive, so the exact identifier (ip vs ip_address; private-link may need backtick-quoting) has not been confirmed column-by-column. All other columns (policy_outcome, rule_label, request_path, authenticated_as, event_time) are confirmed. A wrong subfield name errors the entire statement, so verify source.ip in your workspace (or drop it) before relying on this query.
-- read_this: One row = a workspace x policy outcome x rule x request path x principal x source IP combo in the window. The column that matters is denial_count - how many times that combination was denied at the network edge.
-- healthy: status = OK; denial_count below :warn_denial_count for one rule+path+principal+IP - field heuristic.
-- investigate_if: status = WARN at/above :warn_denial_count, CRITICAL at/above :crit_denial_count - field heuristic; a sudden spike of denials from one source IP against multiple paths is worth prioritizing even below the threshold.
-- actions: 1) confirm whether the denied traffic is a misconfigured internal client (bad DNS, wrong endpoint) rather than an external actor (free); 2) if the source is unexpected, tighten or add an explicit ingress rule and confirm the policy is not left in DENY_DRY_RUN (config); 3) if denials persist from external ranges, engage network/security to add IP-range blocking upstream of Databricks (spend).
-- next: access_network_outbound_denials (check the egress side), access_admin_role_change_events (see if the same principal also touched admin roles)
-- caveats: This surfaces DENIALS ONLY - policy_outcome is DENY or DENY_DRY_RUN, so there is no allow-side baseline here. Inbound retention is capped at 30 days regardless of :period_days - widening the parameter past 30 will not surface older data. This table has no event_date column, only event_time. An empty result means no ingress policy is configured, not that nothing was denied - degrade to "not assessed", never "clean". This reads system.access.inbound_network, which is Public Preview and regional. WORKSPACE_ID: added to the SELECT/GROUP BY (confirmed present on this table) - this table is already per-workspace, so the app's workspace filter now actually narrows this finding.
-- NEEDS CONFIRMATION: the source.ip nested subfield name is unverified in this account.
-- A wrong name errors the whole statement - drop or replace it with the verified subfield
-- before running this in your workspace.
SELECT workspace_id, policy_outcome, rule_label, request_path,
       CASE
         WHEN authenticated_as IS NULL OR authenticated_as = '__REDACTED__' THEN authenticated_as
         WHEN authenticated_as RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN authenticated_as
         ELSE concat(substr(sha2(lower(trim(authenticated_as)), 256), 1, 8), ' ', substr(authenticated_as, 1, 2), '***')
       END AS authenticated_as,
       source.ip AS source_ip,
       COUNT(*) AS denial_count,
       MIN(event_time) AS first_event_time, MAX(event_time) AS last_event_time,
       -- status: worst-first band on inbound denial volume per rule+path+principal+IP (field heuristic; :warn_denial_count / :crit_denial_count).
       CASE
         WHEN COUNT(*) >= :crit_denial_count THEN 'CRITICAL'
         WHEN COUNT(*) >= :warn_denial_count THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM system.access.inbound_network
WHERE event_time >= current_timestamp() - INTERVAL :period_days DAYS
GROUP BY 1, 2, 3, 4, authenticated_as, 6
ORDER BY denial_count DESC
