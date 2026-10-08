-- query_id: access_login_concentration
-- title: Login concentration and failed-authentication rollup
-- domain: governance_access   tier: standard
-- reads: system.access.audit
-- requires: SELECT on system.access; Public Preview
-- empty_if: schema_not_enabled, preview_unavailable, privilege_scoped
-- params: :period_days (default 30) rolling window in days; :warn_failed_logins (default 5) non-success auth events for one principal+source_ip+service+action combo that flags WARN; :crit_failed_logins (default 20) that flags CRITICAL
-- confidence: confirmed
-- confidence_note: service_name='accounts' is confirmed; specific action_name values (mfaLogin, tokenLogin, and others) are representative, not a complete enumeration - matched via a login/authenticate name pattern rather than hardcoding a filter list. user_identity.subject_name (not subjectName) was confirmed live 2026-05-30 and is frequently NULL. Whether response.status_code is int or long is unverified, so the IS NOT NULL AND <>200 comparison is written defensively either way.
-- read_this: One row = a workspace x masked principal x source IP x service x login action combo in the window. The columns that matter are non_success_count (a real, non-2xx status - never a missing one), unknown_status_count (rows whose status_code is simply missing, never counted as a failure) and distinct_source_ips (how many DIFFERENT locations that principal authenticated from within THIS ROW's OWN workspace in the window).
-- healthy: status = OK; non_success_count below :warn_failed_logins for one principal+IP+action - field heuristic; some non-success events are normal (typos, expired tokens); a high unknown_status_count is a coverage gap, not itself a failure.
-- investigate_if: status = WARN at/above :warn_failed_logins, CRITICAL at/above :crit_failed_logins - field heuristic; also worth a look whenever distinct_source_ips is high for a single principal in one workspace, which this query surfaces but does not score.
-- actions: 1) confirm whether the failed attempts are a known automation/CI credential that rotated or expired (free); 2) if unexplained, force a credential/token reset for that principal and require MFA (config); 3) if this recurs across many principals, invest in a dedicated identity-threat-detection tool or SIEM integration ahead of Databricks-native monitoring (spend).
-- next: access_runas_escalation (check if the same principal shows run-as activity), access_admin_role_change_events (check if they also touched admin roles)
-- caveats: service_name='accounts' also carries non-login account-management actions (e.g. token/permission changes); action_name is filtered to a login/authenticate name pattern (case-insensitive) first, since the specific action_name values (mfaLogin, tokenLogin, and others) are representative, not a complete list - a login action this account uses under a name the pattern misses would be invisible here, a coverage gap, not a false negative. user_identity.subject_name (not subjectName) was confirmed live on 2026-05-30 and is frequently NULL. response.status_code missing (NULL) is its own unknown_status_count, never folded into non_success_count - a genuinely missing status is not evidence of a failed attempt. This reads system.access.audit, which is Public Preview and deduped by event_id (QUALIFY, latest event_time wins) before anything else, since a replayed/retried audit event must count once. Account-level events are global (workspace_id=0); workspace events are regional, so a single-region query undercounts. Ingest lag is roughly 15 minutes - treat the most recent hour as provisional and re-run later for a complete count. distinct_source_ips is computed in a separate CTE keyed on (workspace_id, RAW pre-mask principal), over every one of that principal's rows IN THAT WORKSPACE in the window, then joined back onto each of this query's own principal+IP+service+action rows - it is NOT `COUNT(DISTINCT source_ip_address)` scoped to the final GROUP BY (that GROUP BY already fixes source_ip_address to one value per row, so a same-position COUNT(DISTINCT source_ip_address) could only ever read 0 or 1, never the principal's true IP spread - the exact bug this CTE fixes), and it is scoped per workspace so the app's workspace filter actually narrows it - a principal seen in two workspaces gets two independent counts, never one account-wide total merged across both. workspace_id is frequently NULL here because service_name='accounts' events are commonly account-scoped, not because the column itself is missing.
WITH base AS (
  SELECT workspace_id,
         COALESCE(user_identity.email, user_identity.subject_name) AS raw_identity,
         source_ip_address, service_name, action_name, event_time, response
  FROM system.access.audit
  WHERE service_name = 'accounts'
    AND action_name RLIKE '(?i)login|authenticat'
    AND event_date >= current_date() - INTERVAL :period_days DAYS
    AND event_date < current_date()
  QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY event_time DESC) = 1
),
-- Distinct source IPs per RAW principal, scoped to the SAME workspace as the rows below (a
-- workspace filter narrows this the same way it narrows every other row here), across the WHOLE
-- window independent of this row's own IP/service/action - the fix for the always-0-or-1 bug
-- (see caveats).
principal_ips AS (
  SELECT workspace_id, raw_identity, COUNT(DISTINCT source_ip_address) AS distinct_source_ips
  FROM base
  GROUP BY workspace_id, raw_identity
)
SELECT b.workspace_id,
       CASE
         WHEN b.raw_identity IS NULL OR b.raw_identity = '__REDACTED__' THEN b.raw_identity
         WHEN b.raw_identity RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN b.raw_identity
         ELSE concat(substr(sha2(lower(trim(b.raw_identity)), 256), 1, 8), ' ', substr(b.raw_identity, 1, 2), '***')
       END AS principal,
       b.source_ip_address, b.service_name, b.action_name,
       COUNT(*) AS event_count,
       SUM(CASE WHEN b.response.status_code = 200 THEN 1 ELSE 0 END) AS success_count,
       SUM(CASE WHEN b.response.status_code IS NOT NULL AND b.response.status_code <> 200 THEN 1 ELSE 0 END) AS non_success_count,
       SUM(CASE WHEN b.response.status_code IS NULL THEN 1 ELSE 0 END) AS unknown_status_count,
       MAX(pi.distinct_source_ips) AS distinct_source_ips,
       MIN(b.event_time) AS first_event_time, MAX(b.event_time) AS last_event_time,
       -- status: worst-first band on REAL non-success (failed) auth attempts per principal+IP+action; a NULL status never counts (field heuristic; :warn_failed_logins / :crit_failed_logins).
       CASE
         WHEN SUM(CASE WHEN b.response.status_code IS NOT NULL AND b.response.status_code <> 200 THEN 1 ELSE 0 END) >= :crit_failed_logins THEN 'CRITICAL'
         WHEN SUM(CASE WHEN b.response.status_code IS NOT NULL AND b.response.status_code <> 200 THEN 1 ELSE 0 END) >= :warn_failed_logins THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM base b
LEFT JOIN principal_ips pi
  ON  b.workspace_id IS NOT DISTINCT FROM pi.workspace_id
  AND b.raw_identity  IS NOT DISTINCT FROM pi.raw_identity
GROUP BY b.workspace_id, b.raw_identity, b.source_ip_address, b.service_name, b.action_name
ORDER BY non_success_count DESC
