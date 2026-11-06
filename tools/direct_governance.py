"""tools/direct_governance.py

Three governance dashboards on system.access.*, one per story:
- Workspace usage: who logs in, per workspace class and workspace, in a picked month, and which
  workspaces have had no activity for 90+ days;
- Broad grants: every privilege granted in Unity Catalog, ALL PRIVILEGES and account users flagged;
- Admin changes: who changed groups, roles, access, grants and settings, a person or a service principal.
And Governance: the three as tabs of one dashboard, with an Overview, outbound network denials and
the dashboard's own load (its tagged queries in system.query.history).
tools/build_direct_dashboards.py writes them with the other direct dashboards.
"""
from __future__ import annotations

from tools import build_databricks_dashboards as bdd
from tools.build_direct_dashboards import (
    END, START, WS, Dataset, _page, bar, counter, dataset_json, field_filter, header, place, table, time_window,
)

USAGE_TITLE, GRANTS_TITLE, ADMIN_TITLE = "Workspace usage", "Broad grants", "Admin changes"
GOV_TITLE, NETWORK_TITLE, LOAD_TITLE = "Governance", "Network", "Dashboard load"
P = "gov"
INACTIVE_DAYS = 90   # no audit event for this long: inactive
LOOKBACK_DAYS = 365  # how far back the audit log is read for the last activity

# (class, LIKE pattern on the lower-case workspace name); the first match wins, the rest read Not classified.
# Set with build_direct_dashboards.py --workspace-class CLASS=PATTERN.
WORKSPACE_CLASSES: list[tuple[str, str]] = []

MONTH_PARAM = {"displayName": "month", "keyword": "month", "dataType": "DATE",
               "defaultSelection": {"values": {"dataType": "DATE", "values": [{"value": "now/M"}]}}}


def _classes() -> str:
    if not WORKSPACE_CLASSES:
        return """classes AS (
  -- no classes set: every workspace reads Not classified
  SELECT CAST(NULL AS STRING) AS workspace_class, CAST(NULL AS STRING) AS name_pattern, 0 AS priority
  WHERE FALSE
)"""
    rows = ",\n         ".join(f"('{c}', '{p.lower()}', {i})" for i, (c, p) in enumerate(WORKSPACE_CLASSES))
    return f"""classes AS (
  SELECT * FROM VALUES {rows}
    AS c(workspace_class, name_pattern, priority)
)"""


def _ws_class() -> str:
    return f"""{_classes()},
ws_class AS (
  SELECT w.workspace_id, w.workspace_name,
         COALESCE(MIN_BY(c.workspace_class, c.priority), 'Not classified') AS workspace_class
  FROM ws w LEFT JOIN classes c ON lower(w.workspace_name) LIKE c.name_pattern
  GROUP BY w.workspace_id, w.workspace_name
)"""


_CLASS_STEPS = {
    "classes": "The workspace classes: a LIKE pattern on the workspace name per class; none set means Not classified.",
    "ws_class": "Each workspace's class: the first pattern its name matches, else Not classified.",
}


def active_users() -> Dataset:
    return Dataset(
        "monthly_active_users", "Monthly active users: who logged in, per workspace",
        [], ["ws"],
        f"""{_ws_class()},
logins AS (
  SELECT DISTINCT CAST(workspace_id AS STRING) AS workspace_id,
         lower(COALESCE(user_identity.email, user_identity.subject_name)) AS user_name
  FROM system.access.audit
  WHERE service_name = 'accounts' AND action_name RLIKE '(?i)login'
    AND response.status_code = 200
    AND event_date >= CAST(date_trunc('MONTH', :month) AS DATE)
    AND event_date < add_months(CAST(date_trunc('MONTH', :month) AS DATE), 1)
    AND workspace_id IS NOT NULL AND workspace_id <> 0
)
SELECT {WS.format(t='l')} AS workspace, COALESCE(c.workspace_class, 'Not classified') AS workspace_class,
       l.user_name
FROM logins l
LEFT JOIN ws ON ws.workspace_id = l.workspace_id
LEFT JOIN ws_class c ON c.workspace_id = l.workspace_id
-- people only: a service principal logs in under its application id, not an email
WHERE l.user_name LIKE '%@%'""",
        "Each person with at least one successful login to a workspace in the picked month.",
        "The active-user tiles and charts on Workspace usage.",
        "One row per workspace and person; the widgets count distinct people.",
        {**_CLASS_STEPS,
         "logins": "Successful logins (accounts service, login actions, status 200) in the picked month, per "
                   "workspace and person; account-console logins left out."},
        head="Dates from the Month filter.", window=False, params=[MONTH_PARAM],
    )


def inactive() -> Dataset:
    return Dataset(
        "workspace_activity", f"Workspace activity: last audit event, inactive after {INACTIVE_DAYS} days",
        [], ["ws"],
        f"""{_ws_class()},
seen AS (
  SELECT CAST(workspace_id AS STRING) AS workspace_id, MAX(event_date) AS last_activity
  FROM system.access.audit
  WHERE event_date >= date_sub(current_date(), {LOOKBACK_DAYS})
  GROUP BY 1
),
w AS (
  SELECT c.workspace_id, c.workspace_name, c.workspace_class, s.last_activity,
         datediff(current_date(), s.last_activity) AS days_since
  FROM ws_class c LEFT JOIN seen s ON s.workspace_id = c.workspace_id
)
SELECT COALESCE(workspace_name, workspace_id) AS workspace, workspace_id, workspace_class, 1 AS workspaces,
       COALESCE(CAST(last_activity AS STRING), 'none in {LOOKBACK_DAYS} days') AS last_activity,
       days_since,
       CASE WHEN last_activity IS NULL OR days_since >= {INACTIVE_DAYS} THEN 1 ELSE 0 END AS inactive,
       CASE WHEN last_activity IS NULL THEN 1 ELSE 0 END AS no_events
FROM w
ORDER BY no_events DESC, days_since DESC""",
        f"Every workspace with the day of its last audit event; inactive with none for {INACTIVE_DAYS}+ days or "
        f"none in the last {LOOKBACK_DAYS}.",
        "The inactive-workspace tiles and table on Workspace usage.",
        "One row per workspace, longest without activity first.",
        {**_CLASS_STEPS,
         "seen": f"Each workspace's last day with any audit event, over the last {LOOKBACK_DAYS} days.",
         "w": "Every workspace, with its last activity and the days since (none when it has no events)."},
        head="Today's view; no dates.", window=False,
    )


# A service principal acts under its application id (a UUID), a person under an email.
_UUID = "'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'"


def _actor_type(col: str) -> str:
    return (f"CASE WHEN {col} RLIKE {_UUID} THEN 'Service principal' WHEN {col} LIKE '%@%' THEN 'User' "
            f"WHEN {col} IS NULL THEN 'Unknown' ELSE 'Other (system)' END")


_WS_AUDIT = ("CASE WHEN {t}.workspace_id = '0' THEN 'Account' "
             "ELSE COALESCE(ws.workspace_name, {t}.workspace_id) END")

# UC grant changes: request_params['changes'] is a JSON list of {principal, add, remove}.
_CHANGES = "ARRAY<STRUCT<principal: STRING, add: ARRAY<STRING>, remove: ARRAY<STRING>>>"


def grants() -> Dataset:
    return Dataset(
        "grant_events", "Grants: every privilege granted in Unity Catalog, broad ones flagged",
        [], ["ws"],
        f"""ev AS (
  SELECT event_id, event_time, CAST(workspace_id AS STRING) AS workspace_id,
         COALESCE(user_identity.email, user_identity.subject_name) AS actor,
         upper(request_params['securable_type']) AS object_type,
         request_params['securable_full_name'] AS object,
         request_params['changes'] AS changes
  FROM system.access.audit
  WHERE service_name = 'unityCatalog' AND action_name = 'updatePermissions'
    AND response.status_code = 200
    AND event_date >= {START} AND event_date < {END}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY event_time DESC) = 1
),
per_grantee AS (
  SELECT ev.*, explode(from_json(changes, '{_CHANGES}')) AS c
  FROM ev
),
granted AS (
  SELECT event_time, workspace_id, actor, object_type, object, c.principal AS grantee,
         upper(replace(p, ' ', '_')) AS privilege
  FROM per_grantee
  LATERAL VIEW explode(c.add) x AS p
),
judged AS (
  SELECT g.*,
         g.privilege = 'ALL_PRIVILEGES' AND g.object_type IN ('CATALOG', 'SCHEMA', 'TABLE') AS all_privileges,
         lower(g.grantee) = 'account users' AS to_account_users
  FROM granted g
)
SELECT CAST(event_time AS STRING) AS granted_at, CAST(event_time AS DATE) AS day,
       {_WS_AUDIT.format(t='j')} AS workspace, object_type, object, privilege, grantee, actor,
       {_actor_type('actor')} AS actor_type, 1 AS grants,
       CASE WHEN all_privileges AND to_account_users THEN 'All privileges to account users'
            WHEN all_privileges THEN 'All privileges'
            WHEN to_account_users THEN 'To account users' ELSE 'Not broad' END AS why,
       CASE WHEN all_privileges OR to_account_users THEN 1 ELSE 0 END AS critical
FROM judged j
LEFT JOIN ws ON ws.workspace_id = j.workspace_id
ORDER BY critical DESC, event_time DESC""",
        "Every privilege granted in Unity Catalog in the window, one row per object, grantee and privilege; "
        "ALL PRIVILEGES on a catalog, schema or table, or anything to account users, is critical.",
        "The Broad grants page.",
        "One row per privilege granted, critical first, newest first.",
        {"ev": "Successful Unity Catalog permission changes (GRANT and REVOKE) in the window, each event once.",
         "per_grantee": "One row per grantee in the change.",
         "granted": "One row per privilege added (revokes left out); ALL PRIVILEGES and ALL_PRIVILEGES read the same.",
         "judged": "Critical: ALL PRIVILEGES on a catalog, schema or table, or a grant to account users."},
        head="Dates from the Time Window.",
    )


# Admin actions: (service, action, category), from Databricks' audit log reference.
ADMIN_ACTIONS = [
    *[("accounts", a, "Group membership") for a in (
        "addPrincipalToGroup", "addPrincipalsToGroup", "removePrincipalFromGroup", "removePrincipalsFromGroup")],
    *[("accounts", a, "Groups") for a in ("createGroup", "removeGroup", "updateGroup")],
    *[("accounts", a, "Admin role") for a in ("setAdmin", "removeAdmin", "setAccountAdmin", "removeAccountAdmin")],
    ("accountsManager", "changeAccountOwner", "Admin role"),
    *[("workspace", a, "Workspace access") for a in (
        "updateRoleAssignment", "addPermissionAssignment", "updatePermissionAssignment", "deletePermissionAssignment")],
    *[("unityCatalog", a, "Grants (Unity Catalog)") for a in ("updatePermissions", "updateSharePermissions")],
    *[("accounts", a, "Permissions (ACL)") for a in (
        "changeDatabricksSqlAcl", "changeDatabricksWorkspaceAcl", "changeDatabricksWorkspaceDirectoryAcl",
        "changeServicePrincipalAcls")],
    ("workspace", "changeWorkspaceAcl", "Permissions (ACL)"),
    ("accountsAccessControl", "updateRuleSet", "Permissions (ACL)"),
    *[("clusterPolicies", a, "Cluster policies") for a in ("create", "edit", "delete", "changeClusterPolicyAcl")],
    *[("workspace", a, "Settings") for a in ("workspaceConfEdit", "setSetting", "deleteSetting")],
    *[("accounts", a, "Settings") for a in ("setSetting", "deleteSetting")],
]


def admin_changes() -> Dataset:
    actions = ",\n         ".join(f"('{s}', '{a}', '{c}')" for s, a, c in ADMIN_ACTIONS)
    rp = "e.request_params"
    return Dataset(
        "admin_changes", "Admin changes: who changed groups, roles, grants and settings",
        [], ["ws"],
        f"""actions AS (
  SELECT * FROM VALUES {actions}
    AS a(service_name, action_name, category)
),
ev AS (
  SELECT e.event_time, CAST(e.workspace_id AS STRING) AS workspace_id, e.service_name, e.action_name, a.category,
         COALESCE(e.user_identity.email, e.user_identity.subject_name) AS actor,
         e.response.status_code AS status_code,
         -- what was changed: the object, or user -> group; the keys differ by action
         COALESCE({rp}['securable_full_name'],
                  CASE WHEN {rp}['targetGroupName'] IS NOT NULL
                       THEN concat(COALESCE({rp}['targetUserName'], {rp}['targetUserId'], '?'), ' -> ',
                                   {rp}['targetGroupName']) END,
                  {rp}['targetUserName'], {rp}['targetUserId'], {rp}['groupName'], {rp}['name'],
                  {rp}['principal'], {rp}['resourceId'], {rp}['policy_id'], {rp}['workspaceConfKeys'], '-') AS target,
         {rp}['changes'] AS changes
  FROM system.access.audit e
  JOIN actions a ON a.service_name = e.service_name AND a.action_name = e.action_name
  WHERE e.event_date >= {START} AND e.event_date < {END}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY e.event_id ORDER BY e.event_time DESC) = 1
)
SELECT CAST(event_time AS STRING) AS changed_at, CAST(event_time AS DATE) AS day,
       {_WS_AUDIT.format(t='e')} AS workspace, category, action_name AS action, target,
       left(changes, 300) AS change, actor, {_actor_type('actor')} AS actor_type, 1 AS changes_count,
       CASE WHEN status_code = 200 THEN 'Done' WHEN status_code IS NULL THEN 'Unknown' ELSE 'Failed' END AS outcome,
       CASE WHEN actor LIKE '%@%' THEN 1 ELSE 0 END AS by_user
FROM ev e
LEFT JOIN ws ON ws.workspace_id = e.workspace_id
ORDER BY event_time DESC""",
        "Every admin action in the window (group membership, groups, admin roles, workspace access, grants, "
        "permissions, cluster policies, settings), who did it and whether that was a person or a service principal.",
        "The Admin changes page.",
        "One row per action, newest first.",
        {"actions": "The admin actions watched: service, action and its category.",
         "ev": "Those actions in the window, each event once, with the actor and what was changed."},
        head="Dates from the Time Window.",
    )


def _distinct(w: dict, col: str) -> dict:
    """A widget's measure as a distinct count: one person in two workspaces counts once."""
    f = w["widget"]["queries"][0]["query"]["fields"][-1]
    old = f["name"]
    f.update({"name": f"countdistinct({col})", "expression": f"COUNT(DISTINCT `{col}`)"})
    for enc in w["widget"]["spec"]["encodings"].values():
        if isinstance(enc, dict) and enc.get("fieldName") == old:
            enc["fieldName"] = f["name"]
    return w


def _month_filter(d: Dataset) -> dict:
    q = {"name": f"q_{bdd.hid('month:' + d.key)}",
         "query": {"datasetName": d.name, "parameters": [{"name": "month", "keyword": "month"}],
                   "disaggregated": False}}
    return {"widget": {
        "name": bdd.hid("widget:month:" + d.key), "queries": [q],
        "spec": {"version": 2, "widgetType": "filter-date-picker",
                 "encodings": {"fields": [{"parameterName": "month", "queryName": q["name"]}]},
                 "selection": {"defaultSelection": MONTH_PARAM["defaultSelection"]},
                 "frame": {"showTitle": True, "title": "Month (any day in it)"}}}}


USAGE_DATASETS: list[Dataset] = [active_users(), inactive()]
GRANTS_DATASETS: list[Dataset] = [grants()]
ADMIN_DATASETS: list[Dataset] = [admin_changes()]


def _temporal(key: str, d: Dataset, col: str, val_title: str, title: str, desc: str, color: str) -> dict:
    w = bdd.bar_widget(key, d.name, "day", col, title, category_label="Day", value_label=val_title, color_col=color,
                       temporal=True, width=1, height=1, x=0, y=0)
    w["widget"]["spec"]["frame"].update({"description": desc, "showDescription": True})
    return w


def _one_page(title: str, key: str, layout: list, datasets: list[Dataset]) -> dict:
    return bdd.tag_dashboard_queries({"datasets": [dataset_json(d) for d in datasets],
                                      "pages": [_page(key, title, layout, prefix=P)]}, title)


def usage_dashboard() -> dict:
    U, A = active_users(), inactive()  # again: --workspace-class may have set the classes
    USAGE_DATASETS[:] = [U, A]
    usage = place([
        (2, [header("gov:use:head", "Workspace usage",
                    f"Active users per class and workspace in the picked month, and workspaces with no activity for "
                    f"{INACTIVE_DAYS}+ days. The Month filter sets only the active users.")]),
        (1, [_month_filter(U), field_filter("gov:use:class", "workspace_class", "Class", [U, A]),
             field_filter("gov:use:ws", "workspace", "Workspace", [U, A])]),
        (3, [_distinct(counter("gov:use:users", U, "user_name", "Active users", "Distinct people, picked month",
                               agg="COUNT"), "user_name"),
             _distinct(counter("gov:use:ws_active", U, "workspace", "Workspaces with logins", "Picked month",
                               agg="COUNT"), "workspace"),
             counter("gov:use:inactive", A, "inactive", "Inactive workspaces", f"No activity for {INACTIVE_DAYS}+ days"),
             counter("gov:use:none", A, "no_events", "No audit events", f"In the last {LOOKBACK_DAYS} days")]),
        (7, [_distinct(bar("gov:use:by_class", U, "workspace_class", "Class", "user_name", "Active users",
                           "Active users by class", "A person in two classes counts in both", money=False,
                           agg="COUNT"), "user_name"),
             _distinct(bar("gov:use:by_ws", U, "workspace", "Workspace", "user_name", "Active users",
                           "Active users by workspace", "Picked month", money=False, agg="COUNT"), "user_name")]),
        (10, [table("gov:use:inactive_table", A, [("workspace", "Workspace", "text"),
                                                  ("workspace_id", "Workspace id", "text"),
                                                  ("workspace_class", "Class", "text"),
                                                  ("last_activity", "Last activity", "text"),
                                                  ("days_since", "Days since", "int")],
                    f"Workspaces by days since last activity ({INACTIVE_DAYS}+ is inactive)")]),
    ])
    return _one_page(USAGE_TITLE, "usage", usage, USAGE_DATASETS)


def grants_dashboard() -> dict:
    G = GRANTS_DATASETS[0]
    broad = place([
        (2, [header("gov:gr:head", "Broad grants",
                    "Every privilege granted in Unity Catalog, from the audit log. Critical: ALL PRIVILEGES on a "
                    "catalog, schema or table, or anything granted to account users.")]),
        (1, [time_window("gov:gr", [G]), field_filter("gov:gr:ws", "workspace", "Workspace", [G]),
             field_filter("gov:gr:priv", "privilege", "Privilege", [G]),
             field_filter("gov:gr:actor_type", "actor_type", "Actor type", [G])]),
        (3, [counter("gov:gr:critical", G, "critical", "Critical grants", "In the Time Window"),
             counter("gov:gr:all", G, "grants", "Grants", "Privileges granted"),
             _distinct(counter("gov:gr:actors", G, "actor", "Who granted", "Distinct actors", agg="COUNT"), "actor"),
             _distinct(counter("gov:gr:objects", G, "object", "Objects", "With a grant", agg="COUNT"), "object")]),
        (7, [_temporal("gov:gr:by_day", G, "critical", "Critical grants", "Critical grants per day",
                       "By reason", "why"),
             bar("gov:gr:by_priv", G, "privilege", "Privilege", "grants", "Grants", "Grants by privilege",
                 "By who granted them", money=False, color="actor_type")]),
        (10, [table("gov:gr:table", G, [("granted_at", "When", "text"), ("workspace", "Workspace", "text"),
                                        ("object_type", "Type", "text"), ("object", "Object", "text"),
                                        ("privilege", "Privilege", "text"), ("grantee", "Grantee", "text"),
                                        ("actor", "Granted by", "text"), ("actor_type", "Actor type", "text"),
                                        ("why", "Why critical", "text")],
                    "Grants, critical first, newest first")]),
    ])
    return _one_page(GRANTS_TITLE, "grants", broad, GRANTS_DATASETS)


def admin_dashboard() -> dict:
    C = ADMIN_DATASETS[0]
    admin = place([
        (2, [header("gov:adm:head", "Admin changes",
                    "Who changed groups, admin roles, workspace access, grants, permissions, cluster policies and "
                    "settings. In an IaC platform these come from service principals; a person is a manual change.")]),
        (1, [time_window("gov:adm", [C]), field_filter("gov:adm:ws", "workspace", "Workspace", [C]),
             field_filter("gov:adm:actor_type", "actor_type", "Actor type", [C]),
             field_filter("gov:adm:actor", "actor", "Actor", [C])]),
        (3, [counter("gov:adm:all", C, "changes_count", "Admin changes", "In the Time Window"),
             counter("gov:adm:users", C, "by_user", "By people", "Not a service principal"),
             _distinct(counter("gov:adm:actors", C, "actor", "Actors", "Distinct", agg="COUNT"), "actor"),
             _distinct(counter("gov:adm:kinds", C, "category", "Kinds of change", "Distinct", agg="COUNT"),
                       "category")]),
        (7, [_temporal("gov:adm:by_day", C, "changes_count", "Changes", "Admin changes per day",
                       "By actor type", "actor_type"),
             bar("gov:adm:by_cat", C, "category", "Kind", "changes_count", "Changes", "Admin changes by kind",
                 "By actor type", money=False, color="actor_type")]),
        (10, [table("gov:adm:table", C, [("changed_at", "When", "text"), ("workspace", "Workspace", "text"),
                                         ("category", "Kind", "text"), ("action", "Action", "text"),
                                         ("target", "Object or principal", "text"), ("change", "Change", "text"),
                                         ("actor", "Done by", "text"), ("actor_type", "Actor type", "text"),
                                         ("outcome", "Outcome", "text")],
                    "Admin changes, newest first")]),
    ])
    return _one_page(ADMIN_TITLE, "admin", admin, ADMIN_DATASETS)


# --------------------------------------------------------------------------------------------
# Governance: the three above as tabs of one dashboard, plus Overview, Network and Dashboard load
# --------------------------------------------------------------------------------------------

def network() -> Dataset:
    return Dataset(
        "network_events", "Outbound network: requests a network policy denied, per day and destination",
        [], ["ws"],
        f"""ev AS (
  SELECT CAST(workspace_id AS STRING) AS workspace_id, event_time, network_source_type, destination_type,
         destination, upper(access_type) AS access_type,
         COALESCE(storage_event.rejection_reason, CAST(dns_event.rcode AS STRING)) AS reason
  FROM system.access.outbound_network
  WHERE event_time >= {START} AND event_time < {END}
),
judged AS (
  SELECT ev.*,
         -- a dry-run denial is logged, not blocked: a policy being tried out
         CASE WHEN access_type = 'DRY_RUN_DENIAL' THEN 'Dry run'
              WHEN access_type LIKE 'ALLOW%' THEN 'Allowed' ELSE 'Blocked' END AS outcome
  FROM ev
)
SELECT CAST(event_time AS DATE) AS day, {WS.format(t='j')} AS workspace, network_source_type AS source,
       destination_type, destination, access_type, outcome, COALESCE(reason, '-') AS reason,
       COUNT(*) AS events,
       SUM(CASE WHEN outcome = 'Blocked' THEN 1 ELSE 0 END) AS blocked,
       SUM(CASE WHEN outcome = 'Dry run' THEN 1 ELSE 0 END) AS dry_run,
       CAST(MIN(event_time) AS STRING) AS first_seen, CAST(MAX(event_time) AS STRING) AS last_seen
FROM judged j
LEFT JOIN ws ON ws.workspace_id = j.workspace_id
GROUP BY ALL
ORDER BY blocked DESC, events DESC""",
        "Outbound requests from serverless compute that a network policy denied (or, in dry-run mode, would "
        "have denied), per day, workspace, source and destination.",
        "The Network page.",
        "One row per day, workspace, source, destination, access type and reason; most blocked first.",
        {"ev": "Outbound network events in the window: where to, from which product, and why refused.",
         "judged": "Blocked, Dry run (logged only) or Allowed (with extended network logs on)."},
        head="Dates from the Time Window.",
    )


def dashboard_load() -> Dataset:
    slug = bdd.tag_slug(GOV_TITLE)
    return Dataset(
        "dashboard_load", "Dashboard load: this dashboard's own queries, per tab, from their query tags",
        [], ["wh"],
        f"""q AS (
  SELECT query_tags['tab'] AS tab, CAST(start_time AS DATE) AS day, executed_by,
         CAST(compute.warehouse_id AS STRING) AS warehouse_id, execution_status,
         COALESCE(total_duration_ms, 0) AS duration_ms, COALESCE(read_bytes, 0) AS read_bytes
  FROM system.query.history
  -- the SET QUERY_TAGS statements are not the dashboard's work; on a reused session they carry a tab
  WHERE query_tags['dashboard'] = '{slug}' AND query_tags['tab'] IS NOT NULL
    AND upper(COALESCE(statement_type, '')) <> 'SET'
    AND start_time >= {START} AND start_time < {END}
)
SELECT q.tab, q.day, q.executed_by AS run_as, COALESCE(wh.warehouse_name, q.warehouse_id) AS warehouse,
       COUNT(*) AS queries,
       SUM(CASE WHEN q.execution_status = 'FAILED' THEN 1 ELSE 0 END) AS failed,
       ROUND(SUM(q.duration_ms) / 3600000, 3) AS hours,
       ROUND(SUM(q.read_bytes) / 1e9, 2) AS gb_read
FROM q
LEFT JOIN wh ON wh.warehouse_id = q.warehouse_id
GROUP BY ALL
ORDER BY q.day DESC, hours DESC""",
        f"Every query of this dashboard in the window, found by its query tags (dashboard = {slug}), per tab, "
        "day, identity and warehouse: count, failures, run time and data read.",
        "The Dashboard load page and the Overview tile.",
        "One row per tab, day, identity and warehouse; newest first.",
        {"q": f"This dashboard's queries in the window: query_tags dashboard = {slug}, with a tab."},
        head="Dates from the Time Window.",
    )


def _inner(d: Dataset) -> str:
    return "(\n" + "\n".join("    " + l if l.strip() else l for l in d.sql().rstrip("\n").splitlines()) + "\n  )"


def overview(parts: dict[str, Dataset]) -> Dataset:
    g, a, n, w, l = (parts[k] for k in ("grants", "admin", "network", "inactive", "load"))
    return Dataset(
        "governance_overview", "Governance overview: the headline number of each tab",
        [], [],
        f"""g AS (
  SELECT COALESCE(SUM(critical), 0) AS critical_grants, COUNT(*) AS grants FROM {_inner(g)} x
),
a AS (
  SELECT COALESCE(SUM(by_user), 0) AS manual_changes, COUNT(*) AS admin_changes FROM {_inner(a)} x
),
n AS (
  SELECT COALESCE(SUM(blocked), 0) AS blocked_requests, COALESCE(SUM(dry_run), 0) AS dry_run_requests
  FROM {_inner(n)} x
),
w AS (
  SELECT COALESCE(SUM(inactive), 0) AS inactive_workspaces, COUNT(*) AS workspaces FROM {_inner(w)} x
),
l AS (
  SELECT COALESCE(SUM(queries), 0) AS dashboard_queries, COALESCE(SUM(hours), 0) AS dashboard_hours
  FROM {_inner(l)} x
)
SELECT * FROM g CROSS JOIN a CROSS JOIN n CROSS JOIN w CROSS JOIN l""",
        "One row: critical grants, admin changes by people, blocked outbound requests, inactive workspaces and "
        "this dashboard's own queries, each from the same query as its tab.",
        "The Overview page.",
        "One row, one column per number.",
        {"g": "Broad grants: the Broad grants query, counted.",
         "a": "Admin changes: the Admin changes query, counted; by people = not a service principal.",
         "n": "Network: the Network query, blocked and dry-run requests.",
         "w": f"Workspace usage: the inactive-workspace query ({INACTIVE_DAYS}+ days without activity).",
         "l": "Dashboard load: the Dashboard load query, queries and hours."},
        head="Dates from the Time Window; inactive workspaces are today's view.",
    )


GOV_DATASETS: list[Dataset] = []


def _tab_pages() -> list[dict]:
    """The Workspace usage, Broad grants and Admin changes pages, as their own dashboards build them."""
    return [usage_dashboard()["pages"][0], grants_dashboard()["pages"][0], admin_dashboard()["pages"][0]]


def governance_dashboard() -> dict:
    usage_page, grants_page, admin_page = _tab_pages()
    U, A = USAGE_DATASETS
    G, C = GRANTS_DATASETS[0], ADMIN_DATASETS[0]
    N, L = network(), dashboard_load()
    O = overview({"grants": G, "admin": C, "network": N, "inactive": A, "load": L})
    GOV_DATASETS[:] = [O, G, C, N, U, A, L]

    over = place([
        (2, [header("gov:ov:head", GOV_TITLE,
                    "Grants, admin changes, network denials and workspace use across the account, from "
                    "system.access. Each tile is the headline of its tab.")]),
        (1, [(time_window("gov:ov", [O]), 4)]),
        (3, [counter("gov:ov:crit", O, "critical_grants", "Critical grants", "Broad grants tab"),
             counter("gov:ov:manual", O, "manual_changes", "Admin changes by people", "Admin changes tab"),
             counter("gov:ov:blocked", O, "blocked_requests", "Blocked outbound requests", "Network tab"),
             counter("gov:ov:inactive", O, "inactive_workspaces", "Inactive workspaces",
                     f"No activity for {INACTIVE_DAYS}+ days; Workspace usage tab"),
             counter("gov:ov:load", O, "dashboard_queries", "Queries of this dashboard", "Dashboard load tab"),
             counter("gov:ov:hours", O, "dashboard_hours", "Their query hours", "Dashboard load tab")]),
    ])
    net = place([
        (2, [header("gov:net:head", NETWORK_TITLE,
                    "Outbound requests from serverless compute that a network policy blocked, from "
                    "system.access.outbound_network. Dry run: logged, not blocked.")]),
        (1, [time_window("gov:net", [N]), field_filter("gov:net:ws", "workspace", "Workspace", [N]),
             field_filter("gov:net:source", "source", "Source", [N]),
             field_filter("gov:net:outcome", "outcome", "Outcome", [N])]),
        (3, [counter("gov:net:blocked", N, "blocked", "Blocked requests", "In the Time Window"),
             counter("gov:net:dry", N, "dry_run", "Dry-run denials", "Logged, not blocked"),
             _distinct(counter("gov:net:dest", N, "destination", "Destinations", "Distinct", agg="COUNT"),
                       "destination"),
             _distinct(counter("gov:net:wss", N, "workspace", "Workspaces", "With a denial", agg="COUNT"),
                       "workspace")]),
        (7, [_temporal("gov:net:by_day", N, "events", "Requests", "Denied requests per day", "By outcome",
                       "outcome"),
             bar("gov:net:by_dest", N, "destination", "Destination", "events", "Requests",
                 "Denied requests by destination", "By source", money=False, color="source")]),
        (10, [table("gov:net:table", N, [("day", "Day", "text"), ("workspace", "Workspace", "text"),
                                         ("source", "Source", "text"), ("destination_type", "Type", "text"),
                                         ("destination", "Destination", "text"), ("outcome", "Outcome", "text"),
                                         ("reason", "Reason", "text"), ("events", "Requests", "int"),
                                         ("last_seen", "Last seen", "text")],
                    "Denied requests, most blocked first")]),
    ])
    load = place([
        (2, [header("gov:load:head", LOAD_TITLE,
                    f"This dashboard's own queries, found by their query tags (dashboard = "
                    f"{bdd.tag_slug(GOV_TITLE)}, tab = the page): how many, how long and on which warehouse.")]),
        (1, [time_window("gov:load", [L]), field_filter("gov:load:tab", "tab", "Tab", [L]),
             field_filter("gov:load:wh", "warehouse", "Warehouse", [L])]),
        (3, [counter("gov:load:queries", L, "queries", "Queries", "In the Time Window"),
             counter("gov:load:hours", L, "hours", "Query hours", "Run time, all tabs"),
             counter("gov:load:gb", L, "gb_read", "GB read", "From storage"),
             counter("gov:load:failed", L, "failed", "Failed queries", "A widget showed an error")]),
        (7, [_temporal("gov:load:by_day", L, "queries", "Queries", "Queries per day", "By tab", "tab"),
             bar("gov:load:by_tab", L, "tab", "Tab", "hours", "Hours", "Query hours by tab", "By warehouse",
                 money=False, color="warehouse")]),
        (10, [table("gov:load:table", L, [("day", "Day", "text"), ("tab", "Tab", "text"),
                                          ("run_as", "Run as", "text"), ("warehouse", "Warehouse", "text"),
                                          ("queries", "Queries", "int"), ("failed", "Failed", "int"),
                                          ("hours", "Hours", "num"), ("gb_read", "GB read", "num")],
                    "Queries per tab and day, newest first")]),
    ])
    pages = [_page("overview", "Overview", over, prefix=P), grants_page, admin_page,
             _page("network", NETWORK_TITLE, net, prefix=P), usage_page, _page("load", LOAD_TITLE, load, prefix=P)]
    return bdd.tag_dashboard_queries({"datasets": [dataset_json(d) for d in GOV_DATASETS], "pages": pages},
                                     GOV_TITLE)
