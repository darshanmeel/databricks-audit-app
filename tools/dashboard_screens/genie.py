"""Genie tab (web/src/tabs/tab_genie.tsx): one page, Genie usage by kind, use, workspace, agent."""
from tools.dashboard_kit import MONEY, NUM, PCT, bar, kpi, table

# "Built in" = reached outside the browser UI, or a service principal; "browser" = a person in the
# UI (the app's own genieUse rule). Repeated per widget since each dataset is self-contained.
_USE_CASE_SQL = """CASE WHEN identity_type = 'service_principal' OR (channel IS NOT NULL AND channel <> '' AND upper(channel) <> 'UI')
            THEN 'Built into apps or agents'
            WHEN upper(COALESCE(channel, '')) = 'UI' AND identity_type = 'user' THEN 'People in the browser'
            ELSE 'Not recorded' END"""
_SURFACE_CASE_SQL = """CASE surface WHEN 'GENIE_CODE' THEN 'Genie Code' WHEN 'GENIE_AGENTS' THEN 'Genie agents'
            WHEN 'GENIE_ONE' THEN 'Genie One' ELSE 'Not recorded' END"""


def build(d):
    p = d.page("usage")
    p.kpis(
        kpi("Genie spend", """
WITH g AS (SELECT period, SUM(usd_list) AS usd FROM {named:genie_usage} WHERE {ws} GROUP BY 1)
SELECT ROUND(COALESCE((SELECT usd FROM g WHERE period = 'current'), 0) * {disc}, 0) AS value,
       concat('Before $', CAST(ROUND(COALESCE((SELECT usd FROM g WHERE period = 'previous'), 0) * {disc}, 0) AS STRING)) AS target
""", target="target", fmt=MONEY,
            how="List $ for Genie usage this period, after your discount; target is the same figure for the period before."),
        kpi("DBUs", """
WITH g AS (
  SELECT period, SUM(dbus) AS dbus, SUM(CASE WHEN is_free THEN dbus ELSE 0 END) AS free_dbus
  FROM {named:genie_usage} WHERE {ws} GROUP BY 1
)
SELECT ROUND(COALESCE((SELECT dbus FROM g WHERE period = 'current'), 0), 0) AS value,
       concat('Free tier: ', CAST(ROUND(COALESCE((SELECT free_dbus FROM g WHERE period = 'current'), 0), 0) AS STRING), ' DBU') AS target
""", target="target", fmt=NUM,
            how="Genie DBUs this period; target is the part of that covered by the free tier."),
        kpi("Built into apps or agents", """
WITH u AS (
  SELECT period, """ + _USE_CASE_SQL + """ AS use_kind, usd_list
  FROM {named:genie_usage} WHERE {ws}
),
s AS (SELECT period, use_kind, SUM(usd_list) AS usd FROM u GROUP BY 1, 2)
SELECT ROUND(COALESCE((SELECT usd FROM s WHERE period = 'current' AND use_kind = 'Built into apps or agents'), 0) * {disc}, 0) AS value,
       concat('Browser $', CAST(ROUND(COALESCE((SELECT usd FROM s WHERE period = 'current' AND use_kind = 'People in the browser'), 0) * {disc}, 0) AS STRING)) AS target
""", target="target", fmt=MONEY,
            how="Genie $ run by a service principal or a non-UI channel this period; target is the browser-UI $."),
        kpi("People using Genie", """
WITH g AS (SELECT * FROM {named:genie_usage} WHERE {ws} AND period = 'current')
SELECT (SELECT COUNT(DISTINCT CASE WHEN identity_type = 'user' AND dbus <> 0 THEN run_as END) FROM g) AS value,
       concat('Workspaces: ', CAST((SELECT COUNT(DISTINCT workspace_id) FROM g) AS STRING),
              '; agents: ', CAST((SELECT COUNT(DISTINCT agent_id) FROM g) AS STRING)) AS target
""", target="target",
            how="Distinct users with nonzero Genie DBUs this period; target counts the workspaces and agents involved."),
    )
    p.table(table("Spend by kind of use", """
WITH g AS (
  SELECT """ + _SURFACE_CASE_SQL + """ AS kind, period, SUM(dbus) AS dbus, SUM(usd_list) AS usd
  FROM {named:genie_usage} WHERE {ws} GROUP BY 1, 2
)
SELECT c.kind, ROUND(c.dbus, 0) AS dbus, ROUND(c.usd * {disc}, 0) AS usd,
       ROUND(100.0 * (c.usd - COALESCE(p.usd, 0)) / NULLIF(p.usd, 0), 1) AS chg_pct
FROM (SELECT * FROM g WHERE period = 'current') c
LEFT JOIN (SELECT * FROM g WHERE period = 'previous') p ON p.kind = c.kind
ORDER BY c.usd DESC
""", [("kind", "Kind"), ("dbus", "DBUs", NUM), ("usd", "$", MONEY), ("chg_pct", "Change %", PCT)], height=6,
        how="Genie $ and DBUs by surface (Genie Code, agents, One) this period, vs the period before."))
    p.table(table("Built in or in the browser", """
WITH g AS (
  SELECT """ + _USE_CASE_SQL + """ AS kind, period, SUM(dbus) AS dbus, SUM(usd_list) AS usd
  FROM {named:genie_usage} WHERE {ws} GROUP BY 1, 2
)
SELECT c.kind, ROUND(c.dbus, 0) AS dbus, ROUND(c.usd * {disc}, 0) AS usd,
       ROUND(100.0 * (c.usd - COALESCE(p.usd, 0)) / NULLIF(p.usd, 0), 1) AS chg_pct
FROM (SELECT * FROM g WHERE period = 'current') c
LEFT JOIN (SELECT * FROM g WHERE period = 'previous') p ON p.kind = c.kind
ORDER BY c.usd DESC
""", [("kind", "Kind"), ("dbus", "DBUs", NUM), ("usd", "$", MONEY), ("chg_pct", "Change %", PCT)], height=5,
        how="Genie $ and DBUs split built-in (service principal or non-UI channel) vs browser-UI, vs the period before."))
    p.table(table("Who uses Genie, where and how", """
SELECT CASE WHEN identity_type = 'service_principal' THEN concat('service principal (', run_as, ')') ELSE COALESCE(run_as, 'Not recorded') END AS who,
       nm_workspace AS workspace,
       """ + _SURFACE_CASE_SQL + """ AS kind,
       CASE WHEN channel IS NULL OR channel = '' THEN 'Not recorded' WHEN upper(channel) = 'UI' THEN 'UI' ELSE concat('Non-UI . ', channel) END AS channel,
       ROUND(SUM(dbus), 0) AS dbus,
       ROUND(SUM(usd_list) * {disc}, 0) AS usd
FROM {named:genie_usage}
WHERE {ws} AND period = 'current'
GROUP BY 1, 2, 3, 4
HAVING SUM(dbus) <> 0 OR SUM(usd_list) <> 0
ORDER BY usd DESC
LIMIT 200
""", [("who", "User or service principal"), ("workspace", "Workspace"), ("kind", "Kind"), ("channel", "Channel"),
      ("dbus", "DBUs", NUM), ("usd", "$", MONEY)], height=10,
        how="Every user or service principal with nonzero Genie $ or DBUs this period, by workspace, surface and channel."))
    p.charts(
        bar("Genie spend by workspace", """
SELECT nm_workspace AS workspace, ROUND(SUM(usd_list) * {disc}, 0) AS usd
FROM {named:genie_usage}
WHERE {ws} AND period = 'current'
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "workspace", "usd", fmt=MONEY, category_title="Workspace", value_title="Spend",
            how="Genie list $ this period by workspace, after your discount."),
        bar("Top Genie agents by spend", """
SELECT concat(nm_workspace, ' . ', agent_id) AS agent, ROUND(SUM(usd_list) * {disc}, 0) AS usd
FROM {named:genie_usage}
WHERE {ws} AND period = 'current' AND agent_id IS NOT NULL
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "agent", "usd", fmt=MONEY, category_title="Agent", value_title="Spend",
            how="Genie list $ this period by agent, labeled with its workspace so the same agent id in two workspaces stays two bars."),
    )
    p.charts(
        bar("Genie spend per day", """
SELECT usage_date AS day, ROUND(SUM(usd_list) * {disc}, 0) AS usd
FROM {named:genie_usage}
WHERE {ws} AND period = 'current'
GROUP BY 1 ORDER BY 1
""", "day", "usd", time=True, fmt=MONEY, category_title="Day", value_title="Spend",
            how="Genie list $ by day this period, after your discount."),
    )
    p.checks()
