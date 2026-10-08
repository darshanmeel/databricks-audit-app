{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:governance_access', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/governance_access/access_admin_role_change_events.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT workspace_id, service_name, action_name,
       {{ mask_user('actor') }} AS actor,
       granted_to, permission, workspace_name,
       event_count, failed_event_count, distinct_source_ips, workspace_token_iplist_actions,
       first_event_time, last_event_time,
       -- status: a handful of action names change the account/admin role itself and read CRITICAL
       -- at any count; every other allow-listed action keeps the worst-first volume band
       -- (field heuristic; {{ param('access_admin_role_change_events', 'warn_admin_events', 20) }} / {{ param('access_admin_role_change_events', 'crit_admin_events', 100) }}).
       CASE
         WHEN action_name IN ('changeAccountOwner', 'setAccountAdmin', 'removeAccountAdmin', 'setAdmin', 'removeAdmin') THEN 'CRITICAL'
         -- A token permission grant: WARN anywhere, CRITICAL in prod (the prod words of env_patterns).
         WHEN action_name = 'changeDbTokenAcl'
              AND lower(workspace_name) RLIKE '(^|[^a-z0-9])(prod|prd|production|live)([^a-z0-9]|$)' THEN 'CRITICAL'
         WHEN action_name = 'changeDbTokenAcl' THEN 'WARN'
         WHEN event_count >= {{ param('access_admin_role_change_events', 'crit_admin_events', 100) }} THEN 'CRITICAL'
         WHEN event_count >= {{ param('access_admin_role_change_events', 'warn_admin_events', 20) }} THEN 'WARN'
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
    FROM {{ source('system_access', 'audit') }}
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
      AND event_date >= {{ audit_today() }} - INTERVAL {{ w }} DAYS
      AND event_date < {{ audit_today() }}
    QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY event_time DESC) = 1
  )
  GROUP BY 1, 2, 3, 4, 5, 6
) a
LEFT JOIN (SELECT workspace_id AS ws_id, workspace_name FROM {{ source('system_access', 'workspaces_latest') }}) w
  ON w.ws_id = a.workspace_id
ORDER BY event_count DESC
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
