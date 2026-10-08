-- query_id: access_admin_role_change_events
-- title: Admin and permission changes
-- domain: governance_access   tier: standard
-- reads: system.access.audit, system.access.workspaces_latest
-- requires: SELECT on system.access; Public Preview
-- empty_if: schema_not_enabled, preview_unavailable, privilege_scoped
-- params: :period_days (default 30) rolling window in days; :warn_admin_events (default 20) admin/role-change events by one workspace+actor+action in the window that flags WARN; :crit_admin_events (default 100) that flags CRITICAL
-- confidence: needs_confirmation
-- confidence_note: workspace_id, user_identity.subject_name (not subjectName) and the overall audit-log schema are confirmed against a live workspace (2026-05-30) and against Databricks' own audit-log/diagnostic-log-reference documentation. The action_name allow-list below (which service_name + action_name pairs count as a state-changing admin/permission/security-configuration change) is sourced from that same published reference, not yet cross-checked action-by-action against a live account's actual event stream -- treat the five category groupings as reasonable, not guaranteed complete or exact; see the caveats.
-- read_this: One row = a workspace x actor (masked identity) x service x action combo in the window, restricted to actions that ATTEMPT to change an admin role, a permission/grant, a security setting, a cluster policy, a token or an IP access list -- reads, credential-vending calls and token logins never appear here (they used to, before this allow-list). The columns that matter are event_count (how often they attempted one of those actions, whether it succeeded or not), failed_event_count (how many of those attempts were denied or a non-200 response, so did not actually change anything), and distinct_source_ips (how many different locations it came from).
-- healthy: status = OK; event_count below :warn_admin_events per workspace/actor/action, and the action is not one of the always-CRITICAL ones below - field heuristic (tune :warn_admin_events for your account); event_count includes failed/denied attempts, so a high failed_event_count with a low event_count is still worth a look even at OK.
-- investigate_if: status = WARN at any count for changeDbTokenAcl (a token permission grant; admins can always create tokens, so a grant only matters for a non-admin), CRITICAL when that grant is in a prod workspace; status = CRITICAL at any count for changeAccountOwner, setAccountAdmin, removeAccountAdmin, setAdmin or removeAdmin (these change who controls the account or a workspace's admin role, so one attempt is already worth reviewing); every other allow-listed action reads WARN at/above :warn_admin_events, CRITICAL at/above :crit_admin_events - field heuristic; also worth a look whenever distinct_source_ips is unusually high for one actor.
-- actions: 1) confirm the actor and action are expected admin/automation activity and cross-check against your own change log (free); 2) if unexpected, review the actor's current grants via access_grants_inventory and rotate credentials if compromise is suspected (config); 3) if this is a legitimate but noisy automation script, move it to a dedicated service principal with scoped permissions so it stops tripping this alert (spend/eng time).
-- next: access_runas_escalation (if the same actor also shows run-as differences), access_grants_inventory (see what that actor's role change actually granted)
-- caveats: This is a discovery-style rollup of STATE-CHANGING admin/permission/security-configuration events only; pair it with the current-state access_grants_inventory. The action_name allow-list covers five categories, each paired with the service_name that documents it: (1) account/workspace admin-role and group-membership changes (service='accounts': setAdmin, removeAdmin, setAccountAdmin, removeAccountAdmin, addPrincipalToGroup, addPrincipalsToGroup, removePrincipalFromGroup, removePrincipalsFromGroup, createGroup, removeGroup, updateGroup; service='accountsManager': changeAccountOwner; service='workspace': updateRoleAssignment, addPermissionAssignment, updatePermissionAssignment, deletePermissionAssignment); (2) permission/grant changes (service='accounts': changeDatabricksSqlAcl, changeDatabricksWorkspaceAcl, changeDatabricksWorkspaceDirectoryAcl, changeServicePrincipalAcls; service='accountsAccessControl': updateRuleSet; service='workspace': changeWorkspaceAcl; service='unityCatalog': updatePermissions, updateSharePermissions); (3) workspace security-configuration edits (service='accounts': enableClusterAcls/disableClusterAcls, enableTableAcls/disableTableAcls, enableWorkspaceAcls/disableWorkspaceAcls, setSetting, deleteSetting; service='workspace': workspaceConfEdit, setSetting, deleteSetting); (4) cluster-policy edits (service='clusterPolicies': create, edit, delete, changeClusterPolicyAcl); (5) token and IP-access-list changes, account-scoped (service='accounts': generateDbToken, revokeDbToken, changeDbTokenAcl, createIpAccessList, updateIpAccessList, deleteIpAccessList). Deliberately excluded, and the actual bug this rewrite fixes: reads and getX/listX calls (e.g. getTable), credential-vending calls (generateTemporaryTableCredential and similar), and token-login/OAuth-mint events (tokenLogin, mintOAuthToken, mintOAuthAuthorizationCode) -- those are usage, not a change. action_name coverage beyond this list is representative-not-complete: an account may run admin/security actions this list does not yet name, so a clean result here is not proof nothing changed; do not hardcode a narrower list than the one above. Deduped by event_id (QUALIFY, latest event_time wins) before anything else, since a replayed/retried audit event must count once. event_count counts every attempt at an allow-listed action regardless of outcome; failed_event_count is how many of those attempts got a real, non-200 response (a missing/NULL status_code is not itself evidence of a denial, so it is never counted) -- the WARN/CRITICAL band is still on event_count (total attempts), not failed_event_count alone, except for the five action names that read CRITICAL at any count regardless of either threshold: they change who controls the account (changeAccountOwner, setAccountAdmin, removeAccountAdmin) or a workspace's own admin role (setAdmin, removeAdmin), so a single attempt already warrants review. Account-level events are global (workspace_id=0, audit_level=ACCOUNT_LEVEL); workspace-level events carry their real workspace_id and are regional, so a single-region query will miss activity in other regions. This reads system.access.audit, which is Public Preview. The user_identity struct field is subject_name (confirmed live 2026-05-30), not subjectName. This query historically used a 90-day window; set :period_days=90 to reproduce it. PRIVACY: actor is masked to DEC-66.3's identity format -- system.access.audit carries no per-row real user id, so every non-NULL/non-GUID identity uses the hash-derived id (the first 8 hex chars of sha2(lower(trim(identity)), 256), + ' ' + first-2-chars + '***'); NULL/'__REDACTED__' and a service-principal GUID pass through unchanged. CATEGORY (5b), UNCONFIRMED: category (5) above only ever matched service_name='accounts' -- unlike categories (1)-(3), which each also match the equivalent service_name='workspace' action, so a workspace-scoped token/IP-access-list change would have been invisible here if Databricks logs one that way. A second branch now also matches service_name='workspace' AND action_name RLIKE '(?i)token|ipaccesslist' -- deliberately broader than an exact action_name list, since the real workspace-scoped action names are not yet confirmed against a live account. workspace_token_iplist_actions lists every distinct action_name this branch actually matched for the row's group, so a reader can see exactly what it caught before trusting it; do not treat a non-NULL value here as a confirmed finding until you have checked, on a live account, that each listed action_name is genuinely a token/IP-access-list change (not, say, a read like getTokenPermissionLevels) -- narrow the RLIKE to an exact action_name list once confirmed, the same way categories (1)-(4) are exact. workspace_token_iplist_actions is NULL when this branch matched nothing for the row. TOKEN GRANTS: granted_to and permission read request_params['targetUserId'] and request_params['aclPermissionSet'] on changeDbTokenAcl events (key names from Databricks' audit-log reference, not yet confirmed live; NULL when the event uses other keys); granted_to is the id the event records, not a name. Prod is matched on the workspace name only (the prod words of env_patterns: prod, prd, production, live), so a workspace that is prod only by its tags reads WARN, not CRITICAL.
SELECT workspace_id, service_name, action_name,
       CASE
         WHEN actor IS NULL OR actor = '__REDACTED__' THEN actor
         WHEN actor RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN actor
         ELSE concat(substr(sha2(lower(trim(actor)), 256), 1, 8), ' ', substr(actor, 1, 2), '***')
       END AS actor,
       granted_to, permission, workspace_name,
       event_count, failed_event_count, distinct_source_ips, workspace_token_iplist_actions,
       first_event_time, last_event_time,
       -- status: a handful of action names change the account/admin role itself and read CRITICAL
       -- at any count; every other allow-listed action keeps the worst-first volume band
       -- (field heuristic; :warn_admin_events / :crit_admin_events).
       CASE
         WHEN action_name IN ('changeAccountOwner', 'setAccountAdmin', 'removeAccountAdmin', 'setAdmin', 'removeAdmin') THEN 'CRITICAL'
         -- A token permission grant: WARN anywhere, CRITICAL in prod (the prod words of env_patterns).
         WHEN action_name = 'changeDbTokenAcl'
              AND lower(workspace_name) RLIKE '(^|[^a-z0-9])(prod|prd|production|live)([^a-z0-9]|$)' THEN 'CRITICAL'
         WHEN action_name = 'changeDbTokenAcl' THEN 'WARN'
         WHEN event_count >= :crit_admin_events THEN 'CRITICAL'
         WHEN event_count >= :warn_admin_events THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM (
  SELECT workspace_id, service_name, action_name,
         COALESCE(user_identity.email, user_identity.subject_name) AS actor,
         -- Who a token permission went to and at what level, as the event records them.
         CASE WHEN action_name = 'changeDbTokenAcl' THEN request_params['targetUserId'] END AS granted_to,
         CASE WHEN action_name = 'changeDbTokenAcl' THEN request_params['aclPermissionSet'] END AS permission,
         COUNT(*) AS event_count,
         SUM(CASE WHEN response.status_code IS NOT NULL AND response.status_code <> 200 THEN 1 ELSE 0 END) AS failed_event_count,
         COUNT(DISTINCT source_ip_address) AS distinct_source_ips,
         -- CATEGORY (5b), UNCONFIRMED (see caveats): every action_name this row's (5b) branch
         -- matched, so a reader can check each one before trusting it.
         array_join(collect_set(CASE WHEN service_name = 'workspace' AND action_name RLIKE '(?i)token|ipaccesslist'
                                      THEN action_name END), ', ')            AS workspace_token_iplist_actions,
         MIN(event_time) AS first_event_time, MAX(event_time) AS last_event_time
  FROM (
    -- Deduped by event_id (latest event_time wins) before any aggregation, so a replayed/retried
    -- audit event counts once.
    SELECT *
    FROM system.access.audit
    WHERE (
            -- (1) account/workspace admin-role and group-membership changes
            (service_name = 'accounts' AND action_name IN (
                'setAdmin', 'removeAdmin', 'setAccountAdmin', 'removeAccountAdmin',
                'addPrincipalToGroup', 'addPrincipalsToGroup',
                'removePrincipalFromGroup', 'removePrincipalsFromGroup',
                'createGroup', 'removeGroup', 'updateGroup'))
         OR (service_name = 'accountsManager' AND action_name = 'changeAccountOwner')
         OR (service_name = 'workspace' AND action_name IN (
                'updateRoleAssignment', 'addPermissionAssignment',
                'updatePermissionAssignment', 'deletePermissionAssignment'))
            -- (2) permission and grant changes
         OR (service_name = 'accounts' AND action_name IN (
                'changeDatabricksSqlAcl', 'changeDatabricksWorkspaceAcl',
                'changeDatabricksWorkspaceDirectoryAcl', 'changeServicePrincipalAcls'))
         OR (service_name = 'accountsAccessControl' AND action_name = 'updateRuleSet')
         OR (service_name = 'workspace' AND action_name = 'changeWorkspaceAcl')
         OR (service_name = 'unityCatalog' AND action_name IN ('updatePermissions', 'updateSharePermissions'))
            -- (3) workspace security-configuration edits
         OR (service_name = 'accounts' AND action_name IN (
                'enableClusterAcls', 'disableClusterAcls',
                'enableTableAcls', 'disableTableAcls',
                'enableWorkspaceAcls', 'disableWorkspaceAcls',
                'setSetting', 'deleteSetting'))
         OR (service_name = 'workspace' AND action_name IN ('workspaceConfEdit', 'setSetting', 'deleteSetting'))
            -- (4) cluster-policy edits
         OR (service_name = 'clusterPolicies' AND action_name IN ('create', 'edit', 'delete', 'changeClusterPolicyAcl'))
            -- (5) token and IP-access-list changes -- account-scoped (exact action_name list)
         OR (service_name = 'accounts' AND action_name IN (
                'generateDbToken', 'revokeDbToken', 'changeDbTokenAcl',
                'createIpAccessList', 'updateIpAccessList', 'deleteIpAccessList'))
            -- (5b) token and IP-access-list changes -- workspace-scoped, UNCONFIRMED (see caveats)
         OR (service_name = 'workspace' AND action_name RLIKE '(?i)token|ipaccesslist')
          )
      AND event_date >= current_date() - INTERVAL :period_days DAYS
      AND event_date < current_date()
    QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY event_time DESC) = 1
  )
  GROUP BY 1, 2, 3, 4, 5, 6
) a
LEFT JOIN (SELECT workspace_id AS ws_id, workspace_name FROM system.access.workspaces_latest) w
  ON w.ws_id = a.workspace_id
ORDER BY event_count DESC
