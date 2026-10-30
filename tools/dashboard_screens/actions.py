"""Actions tab (web/src/tabs/tab_actions.tsx): the flagged checks turned into fixes.

One row per ACTION_TEMPLATES entry (web/src/components/action_templates.ts), parsed straight off
that file the way check_labels() parses labels.ts -- the app's own fix catalog, not reimplemented
by hand. A template is flagged when any of the checks it names is Critical or Warn in the last
snapshot; its possible $ comes from waste_cte, by whichever of the 5 priced checks the template
names as `waste`. Team names are spelled out as plain teams (_TEAM_NAMES) before they reach the
screen.

The KPI row above the fix table still counts every check the app places on a tab (area_registry()),
the wider "flagged checks" picture; its possible $ uses the kit's two reuse functions, waste_cte()
and the snapshot_log join, the same as the fix table."""
import json
import re

from tools.dashboard_kit import INT, MONEY, bar, kpi, table
from tools import build_databricks_dashboards as bdd

PRICED_SOURCE_TO_CHECK = """CASE source_label
           WHEN 'Warehouse idle time' THEN 'compute_warehouse_idle_minutes'
           WHEN 'Idle cluster nodes' THEN 'compute_idle_node_ratio'
           WHEN 'Compute burned on failed runs' THEN 'lakeflow_failed_jobs_wasted_dbus'
           WHEN 'Compute burned on failed statements' THEN 'cost_failed_statement_waste'
           WHEN 'Endpoints billed with no requests' THEN 'compute_serving_endpoint_cost_status'
         END"""

# Role names off the screen: these show up only as values in the "Team" column.
_TEAM_NAMES = {"FinOps": "Finance", "Data eng": "Data engineering"}


def build(d):
    fixes(d)


def _check_entries(d):
    """[(query_id, title, area_label)] for every check the app places on a tab -- the universe the
    KPI row above the fix table counts."""
    out = []
    for area in bdd.area_registry().values():
        for _, _, ids, refs in area["pages"]:
            for q in ids + refs:
                out.append((q, d.title_of(q), area["label"]))
    return out


def _strip_with(sql: str) -> str:
    s = sql.lstrip()
    return s[5:] if s[:5].upper() == "WITH " else s


def _status_cte(d) -> str:
    """checks/status: one row per check the app places anywhere, its verdict from snapshot_log --
    LEFT JOIN so a check that never flagged still gets a row (SQL: outer, keep all rows)."""
    values = ",\n       ".join(
        f"({bdd._sql_str(q)}, {bdd._sql_str(title)}, {bdd._sql_str(area)})"
        for q, title, area in _check_entries(d)
    )
    return f"""checks AS (
  SELECT * FROM (VALUES
       {values}
  ) AS c(query_id, check_title, area)
),
status AS (
  SELECT c.query_id, c.check_title, c.area,
         CASE WHEN l.query_id IS NULL THEN 'Not run'
              WHEN NOT l.ok THEN 'Not assessed'
              WHEN l.critical > 0 THEN 'Critical'
              WHEN l.warn > 0 THEN 'Warn'
              WHEN l.has_status THEN 'OK'
              WHEN l.row_count = 0 THEN 'No rows'
              ELSE 'Listed' END AS verdict,
         COALESCE(l.critical, 0) AS critical_rows, COALESCE(l.warn, 0) AS warn_rows
  FROM checks c
  LEFT JOIN {{S}}.snapshot_log l ON l.query_id = c.query_id
)
"""


def _priced_cte() -> str:
    """The 5 priced waste sources, by the check that owns each one, workspace-filterable."""
    return _strip_with(bdd.waste_cte("{S}")) + f""",
priced AS (
  SELECT {PRICED_SOURCE_TO_CHECK} AS query_id, source_label, wasted, workspace_name AS ws_name
  FROM named
),
priced_f AS (
  SELECT * FROM priced WHERE {{ws}}
)
"""


def _action_templates() -> list[dict]:
    """ACTION_TEMPLATES (web/src/components/action_templates.ts): key, kind, team, the checks it
    covers, its noun (singular, plural), effort and how-to -- read off the TS source the way
    check_labels() reads labels.ts, nothing duplicated by hand."""
    ts = bdd._ts_source("action_templates.ts")
    block = re.search(r"ACTION_TEMPLATES: ActionTemplate\[\] = \[(.*?)\n\];", ts, re.S).group(1)
    out = []
    for part in re.split(r"\{\s*key:\s*", block)[1:]:
        key = re.match(r'"(\w+)"', part).group(1)
        kind = re.search(r'kind:\s*"([^"]+)"', part).group(1)
        team = re.search(r'team:\s*"([^"]+)"', part).group(1)
        waste = re.search(r'waste:\s*"([^"]+)"', part)
        checks = bdd._quoted_ids(re.search(r"checks:\s*\[(.*?)\]", part).group(1))
        noun = re.search(r'noun:\s*\[\s*("(?:[^"\\]|\\.)*"),\s*("(?:[^"\\]|\\.)*")\s*\]', part)
        effort = re.search(r'effort:\s*("(?:[^"\\]|\\.)*")', part)
        how = re.search(r'how:\s*("(?:[^"\\]|\\.)*")', part)
        out.append({
            "key": key, "kind": kind, "team": _TEAM_NAMES.get(team, team),
            "waste": waste.group(1) if waste else None, "checks": checks,
            "noun": (json.loads(noun.group(1)), json.loads(noun.group(2))),
            "effort": json.loads(effort.group(1)), "how": json.loads(how.group(1)),
        })
    return out


def _templates_cte() -> str:
    """templates / template_checks / template_status: one row per ACTION_TEMPLATES entry, its
    worst verdict and flagged-row count straight off snapshot_log -- self-contained, so it needs
    no other CTE in scope."""
    templates = _action_templates()
    t_rows = ",\n       ".join(
        f"({bdd._sql_str(t['key'])}, {bdd._sql_str(t['kind'])}, {bdd._sql_str(t['team'])}, "
        f"{bdd._sql_str(t['noun'][0])}, {bdd._sql_str(t['noun'][1])}, {bdd._sql_str(t['effort'])}, "
        f"{bdd._sql_str(t['how'])}, "
        + (bdd._sql_str(t["waste"]) if t["waste"] else "CAST(NULL AS STRING)") + ")"
        for t in templates
    )
    c_rows = ",\n       ".join(
        f"({bdd._sql_str(t['key'])}, {bdd._sql_str(q)})" for t in templates for q in t["checks"]
    )
    return f"""templates AS (
  SELECT * FROM (VALUES
       {t_rows}
  ) AS v(tkey, kind, team, noun_s, noun_p, effort, how, waste)
),
template_checks AS (
  SELECT * FROM (VALUES
       {c_rows}
  ) AS v(tkey, query_id)
),
check_status AS (
  SELECT tc.tkey,
         CASE WHEN l.query_id IS NULL THEN 'Not run'
              WHEN NOT l.ok THEN 'Not assessed'
              WHEN l.critical > 0 THEN 'Critical'
              WHEN l.warn > 0 THEN 'Warn'
              ELSE 'OK' END AS verdict,
         COALESCE(l.critical, 0) AS critical_rows, COALESCE(l.warn, 0) AS warn_rows
  FROM template_checks tc
  LEFT JOIN {{S}}.snapshot_log l ON l.query_id = tc.query_id
),
template_status AS (
  SELECT tkey,
         MAX(CASE verdict WHEN 'Critical' THEN 3 WHEN 'Warn' THEN 2 ELSE 0 END) AS sev,
         SUM(CASE WHEN verdict IN ('Critical', 'Warn') THEN critical_rows + warn_rows ELSE 0 END) AS rows_flagged
  FROM check_status
  GROUP BY 1
)
"""


def fixes(d):
    p = d.page("fixes", "Actions")
    status_cte = _status_cte(d)
    priced_cte = _priced_cte()
    templates_cte = _templates_cte()

    p.kpis(
        kpi("Flagged checks", f"""WITH {status_cte}
SELECT (SELECT count(*) FROM status WHERE verdict IN ('Critical', 'Warn')) AS value,
       COALESCE(
         (SELECT concat('Worst: ', check_title, ', ', CAST(critical_rows AS STRING), ' critical rows')
          FROM status WHERE verdict = 'Critical' ORDER BY critical_rows DESC LIMIT 1),
         (SELECT concat('Worst: ', check_title, ', ', CAST(warn_rows AS STRING), ' warn rows')
          FROM status WHERE verdict = 'Warn' ORDER BY warn_rows DESC LIMIT 1),
         'Nothing flagged') AS target
""", target="target", tone="warn",
            how="Checks across every tab at Critical or Warn in the last snapshot; account-wide, not filtered by workspace."),
        kpi("Critical checks", f"""WITH {status_cte}
SELECT (SELECT count(*) FROM status WHERE verdict = 'Critical') AS value,
       concat('of ', CAST((SELECT count(*) FROM status WHERE verdict IN ('Critical', 'Warn')) AS STRING),
              ' flagged checks') AS target
""", target="target", tone="crit",
            how="Of the flagged checks above, the ones where every row is Critical, not just some."),
        kpi("Possible waste", f"""WITH {priced_cte}
SELECT ROUND(COALESCE((SELECT SUM(wasted) FROM priced_f), 0), 0) AS value,
       concat(CAST((SELECT count(DISTINCT query_id) FROM priced_f) AS STRING),
              ' of 5 priced checks flagged') AS target
""", target="target", fmt=MONEY, tone="warn",
            how="$ from the 5 priced waste checks (see Waste & savings), floored and discounted; follows the Workspace filter above."),
        kpi("Not priced", f"""WITH {status_cte},
{priced_cte}, priced_sum AS (
  SELECT query_id, ROUND(SUM(wasted), 2) AS possible_usd FROM priced_f GROUP BY 1
)
SELECT (SELECT count(*) FROM status s LEFT JOIN priced_sum ps ON ps.query_id = s.query_id
        WHERE s.verdict IN ('Critical', 'Warn') AND ps.possible_usd IS NULL) AS value,
       'No dollar figure from this window' AS target
""", target="target",
            how="Flagged checks that match none of the 5 priced checks, so no $ figure; account-wide."),
    )
    p.kpis(
        kpi("Security risks", f"""WITH {templates_cte}
SELECT (SELECT count(*) FROM template_status ts JOIN templates t ON t.tkey = ts.tkey
        WHERE t.kind = 'risk' AND ts.sev >= 2) AS value,
       COALESCE(
         (SELECT concat('Worst: ', t.how) FROM template_status ts JOIN templates t ON t.tkey = ts.tkey
          WHERE t.kind = 'risk' AND ts.sev >= 2 ORDER BY ts.sev DESC, ts.rows_flagged DESC LIMIT 1),
         'Nothing flagged') AS target
""", target="target", tone="crit",
            how="Unmasked sensitive columns, admin role changes or broad grants flagged Critical or Warn; account-wide."),
    )
    p.text(["Rebuilt from the data on every refresh: a fix disappears once its check stops flagging.",
            "Flagged checks, Critical checks, Security risks and every fix below are account-wide; only their $ follows the Workspace filter above."],
           height=2)
    p.charts(
        bar("Flagged checks by area", f"""WITH {status_cte}
SELECT area, count(*) AS flagged
FROM status
WHERE verdict IN ('Critical', 'Warn')
GROUP BY 1
""", "area", "flagged", fmt=INT, category_title="Area", value_title="Flagged checks",
            how="Flagged checks (Critical or Warn) grouped by the tab they live on; account-wide."),
        bar("Possible $ by source", f"""WITH {priced_cte}
SELECT source_label, ROUND(SUM(wasted), 2) AS usd
FROM priced_f
GROUP BY 1
""", "source_label", "usd", fmt=MONEY, category_title="Source", value_title="Possible $",
            how="Possible-waste $ per priced check, from Waste & savings' own floor and discount."),
    )
    p.table(table("Fixes, worst first", f"""WITH {templates_cte},
{priced_cte}, priced_sum AS (
  SELECT query_id, ROUND(SUM(wasted), 2) AS possible_usd FROM priced_f GROUP BY 1
)
SELECT CASE ts.sev WHEN 3 THEN 'Critical' WHEN 2 THEN 'Warn' ELSE 'OK' END AS status,
       t.kind AS kind,
       concat(CAST(COALESCE(ts.rows_flagged, 0) AS STRING), ' ',
              CASE WHEN COALESCE(ts.rows_flagged, 0) = 1 THEN t.noun_s ELSE t.noun_p END) AS fix,
       t.team AS team, t.effort AS effort, t.how AS how,
       ROUND(ps.possible_usd, 0) AS possible_usd
FROM templates t
JOIN template_status ts ON ts.tkey = t.tkey
LEFT JOIN priced_sum ps ON ps.query_id = t.waste
WHERE ts.sev >= 2
ORDER BY ts.sev DESC, ps.possible_usd DESC NULLS LAST, t.tkey
LIMIT 100
""", [("status", "Status"), ("kind", "Kind"), ("fix", "Fix"), ("team", "Team"), ("effort", "Effort"),
      ("how", "How"), ("possible_usd", "Possible $", MONEY)], height=12,
                   how="One row per fix template (action_templates.ts) with a Critical or Warn check; $ only where it names one of the 5 priced waste checks."))
    p.checks()
