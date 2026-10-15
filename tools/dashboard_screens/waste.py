"""Waste & savings tab (web/src/tabs/tab_waste.tsx): Priced, Flagged not priced, How it's counted.

Waste has no registry pages of its own (every WASTE_ITEMS id is homed on another area's tab), so
the 3 pages are built by hand rather than from tab_registry ids -- PRICED_IDS/UNPRICED_IDS mirror
WASTE_ITEMS in web/src/components/tab_registry.ts exactly."""
from tools.dashboard_kit import INT, MONEY, NUM, PCT, bar, kpi, table
from tools import build_databricks_dashboards as bdd

PRICED_IDS = ["compute_warehouse_idle_minutes", "compute_idle_node_ratio", "lakeflow_failed_jobs_wasted_dbus",
              "cost_failed_statement_waste", "compute_serving_endpoint_cost_status"]
UNPRICED_IDS = ["instance_pools_idle_capacity", "lakeflow_jobs_on_all_purpose", "lakeflow_stale_zombie_jobs",
                "lakeflow_pipeline_idle_tail_duration", "compute_serving_dormant_endpoints",
                "cost_premium_serverless_photon"]


def build(d):
    priced(d)
    unpriced(d)
    method(d)


def _area_of(query_id: str) -> str:
    """The label of the tab this check is actually placed on (area_registry(), not WASTE_ITEMS'
    own area field, so this always agrees with where "Open in ..." would send someone)."""
    for area in bdd.area_registry().values():
        for _, _, ids, refs in area["pages"]:
            if query_id in ids or query_id in refs:
                return area["label"]
    return "Other"


def _values_sql(entries) -> str:
    return ",\n       ".join(
        f"({bdd._sql_str(q)}, {bdd._sql_str(title)}, {bdd._sql_str(area)})" for q, title, area in entries
    )


def _status_cte(entries) -> str:
    """checks/status: one row per entry, its verdict and flagged-row counts from snapshot_log --
    LEFT JOIN so a check that never flagged still gets a row (SQL: outer, keep all rows)."""
    return f"""checks AS (
  SELECT * FROM (VALUES
       {_values_sql(entries)}
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


def _named_ws(cte: str) -> str:
    """waste_cte's `named` carries workspace_name -- alias it to ws_name so {ws} (which always
    filters the bare ws_name column) can be used, without a WHERE naming its own SELECT's alias."""
    return cte + ",\nnamed_ws AS (\n  SELECT *, workspace_name AS ws_name FROM named\n)\n"


def _saving_ws(cte: str) -> str:
    return cte + ",\nsaving_ws AS (\n  SELECT *, workspace_name AS ws_name FROM saving\n)\n"


def _lbl(name_col: str, ws_col: str) -> str:
    """"name" or "name · workspace" -- the kit's own lbl_<kind> shape, for CTEs that build their
    own resource_name/workspace_name instead of going through {named:...} (so two same-named
    warehouses in different workspaces stay two bars, not one)."""
    return f"CASE WHEN {ws_col} IS NULL THEN {name_col} ELSE concat({name_col}, ' · ', {ws_col}) END"


def priced(d):
    p = d.page("priced", "Priced")
    cte = _named_ws(bdd.waste_cte("{S}"))
    p.kpis(
        kpi("Possible waste", f"""{cte}SELECT ROUND(COALESCE(SUM(wasted), 0), 0) AS value,
       concat(CAST(count(*) AS STRING), ' resources flagged') AS target
FROM named_ws
WHERE {{ws}}
""", target="target", fmt=MONEY, tone="crit",
            how="Sum of the 5 priced waste checks' $, each floored at $50 * window/30 days and discounted; below the floor doesn't count."),
        kpi("Top 3 resources", f"""{cte}, ranked AS (
  SELECT wasted, ROW_NUMBER() OVER (ORDER BY wasted DESC) AS rn FROM named_ws WHERE {{ws}}
)
SELECT ROUND(COALESCE(SUM(wasted), 0), 0) AS value, 'Top 3 by possible waste' AS target
FROM ranked WHERE rn <= 3
""", target="target", fmt=MONEY, how="The 3 flagged resources with the most possible-waste $, summed."),
        kpi("Biggest single fix", f"""{cte}SELECT ROUND(COALESCE((SELECT MAX(wasted) FROM named_ws WHERE {{ws}}), 0), 0) AS value,
       COALESCE((SELECT concat('Resource: ', resource_name) FROM named_ws WHERE {{ws}} ORDER BY wasted DESC LIMIT 1),
                'None flagged') AS target
""", target="target", fmt=MONEY, how="The single flagged resource with the most possible-waste $."),
        kpi("Resources flagged", f"""{cte}SELECT (SELECT count(*) FROM named_ws WHERE {{ws}}) AS value,
       concat(CAST((SELECT count(DISTINCT source_label) FROM named_ws WHERE {{ws}}) AS STRING),
              ' of 5 priced sources') AS target
""", target="target", fmt=INT, how="Resources over the floor on any of the 5 priced checks, in the Workspace filter above."),
    )
    p.text(["Possible waste is priced only where a source supports it -- see How it's counted for the rule."],
           height=1)
    p.charts(
        bar("Possible waste by source", f"""{cte}SELECT source_label AS source, ROUND(SUM(wasted), 2) AS usd
FROM named_ws
WHERE {{ws}}
GROUP BY 1
""", "source", "usd", fmt=MONEY, category_title="Source", value_title="Possible $",
            how="Possible-waste $ summed by which of the 5 priced checks flagged it."),
        bar("Possible waste by resource", f"""{cte}SELECT resource_name AS resource, ROUND(SUM(wasted), 2) AS usd
FROM named_ws
WHERE {{ws}}
GROUP BY 1 ORDER BY 2 DESC LIMIT 10
""", "resource", "usd", fmt=MONEY, category_title="Resource", value_title="Possible $",
            how="The 10 flagged resources with the most possible-waste $."),
    )
    p.charts(
        bar("Warehouses with the most idle time", f"""{cte}SELECT {_lbl('resource_name', 'workspace_name')} AS warehouse,
       ROUND(SUM(wasted), 0) AS usd
FROM named_ws
WHERE {{ws}} AND source_label = 'Warehouse idle time'
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "warehouse", "usd", fmt=MONEY, category_title="Warehouse", value_title="Possible $",
            how="Idle-time $ from the same floored, discounted rows as Possible waste above, so this never lists a warehouse that KPI excludes."),
        bar("Auto-stop saving available", f"""{_saving_ws(bdd.autostop_cte("{S}"))}SELECT {_lbl('resource_name', 'workspace_name')} AS warehouse,
       ROUND(SUM(saving_usd), 0) AS usd
FROM saving_ws
WHERE {{ws}} AND saving_usd > 0
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "warehouse", "usd", fmt=MONEY, category_title="Warehouse", value_title="Saving if tightened",
            how="Saving from auto-stop at 1 min (serverless) or 10 min (pro, classic) -- the lowest Databricks allows -- vs each warehouse's current setting."),
    )
    p.charts(
        bar("Cache reuse, lowest first", """
SELECT lbl_warehouse AS warehouse, ROUND(MAX(avg_read_io_cache_percent), 0) AS pct
FROM {named:compute_warehouse_cache_reuse}
WHERE {ws} AND avg_read_io_cache_percent IS NOT NULL
GROUP BY 1 ORDER BY 2 ASC LIMIT 8
""", "warehouse", "pct", fmt=PCT, category_title="Warehouse", value_title="Disk cache %",
            how="Average disk-cache hit rate per warehouse, lowest 8; well under 100% means repeat reads keep hitting cloud storage."),
        bar("Compute burned on failed job runs", """
SELECT nm_job AS job, ROUND(SUM(est_wasted_usd_list) * {disc}, 0) AS usd
FROM {named:lakeflow_failed_jobs_wasted_dbus}
WHERE {ws} AND status IN ('CRITICAL', 'WARN')
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "job", "usd", fmt=MONEY, category_title="Job", value_title="Possible $",
            how="DBU cost of runs that failed, by job, priced at list and discounted."),
    )
    p.table(table("Top resources by possible waste", f"""{cte}SELECT source_label AS source, resource_name AS resource, workspace_name AS workspace,
       CASE entity_kind WHEN 'warehouse' THEN 'SQL warehouse' WHEN 'cluster' THEN 'Cluster'
                         WHEN 'job' THEN 'Job' WHEN 'endpoint' THEN 'Serving endpoint' ELSE entity_kind END AS kind,
       ROUND(wasted, 2) AS possible_usd
FROM named_ws
WHERE {{ws}}
ORDER BY wasted DESC
LIMIT 100
""", [("source", "Source"), ("resource", "Resource"), ("workspace", "Workspace"), ("kind", "Kind"),
      ("possible_usd", "Possible $", MONEY)], height=11,
                   how="Every flagged resource across the 5 priced checks, worst first by possible $."))
    p.checks()


def unpriced(d):
    p = d.page("unpriced", "Flagged, not priced")
    entries = [(q, d.title_of(q), _area_of(q)) for q in UNPRICED_IDS]
    cte = _status_cte(entries)
    why = d.why_of("cost_premium_serverless_photon") or d.title_of("cost_premium_serverless_photon")
    p.kpis(
        kpi("Flagged, not priced", f"""WITH {cte}
SELECT (SELECT count(*) FROM status WHERE verdict IN ('Critical', 'Warn')) AS value,
       COALESCE(
         (SELECT concat('Worst: ', check_title) FROM status WHERE verdict = 'Critical' ORDER BY critical_rows DESC LIMIT 1),
         (SELECT concat('Worst: ', check_title) FROM status WHERE verdict = 'Warn' ORDER BY warn_rows DESC LIMIT 1),
         'Nothing flagged') AS target
""", target="target", tone="warn",
            how="Checks that flag rows but carry no $ column, at Critical or Warn in the last snapshot; account-wide."),
        kpi("Rows flagged", f"""WITH {cte}
SELECT COALESCE(SUM(critical_rows + warn_rows), 0) AS value,
       concat('across ', CAST((SELECT count(*) FROM status WHERE verdict IN ('Critical', 'Warn')) AS STRING),
              ' checks') AS target
FROM status
WHERE verdict IN ('Critical', 'Warn')
""", target="target", how="Rows at Critical or Warn, summed across the unpriced checks above; account-wide."),
        kpi("Not flagged", f"""WITH {cte}
SELECT count(*) AS value, 'Nothing to fix on these checks' AS target
FROM status
WHERE verdict NOT IN ('Critical', 'Warn') AND query_id <> 'cost_premium_serverless_photon'
""", target="target", how="Of the 5 flaggable unpriced checks, how many have nothing flagged right now; account-wide."),
        kpi("Serverless & Photon DBUs", f"""
SELECT ROUND(COALESCE(SUM(net_usage_quantity), 0), 0) AS value,
       {bdd._sql_str(why)} AS target
FROM {{named:cost_premium_serverless_photon}}
WHERE {{ws}} AND (COALESCE(is_serverless, FALSE) OR COALESCE(is_photon, FALSE))
""", target="target", fmt=NUM,
            how="DBUs billed on serverless or Photon SKUs in the window -- not a flagged check, just the premium-priced share to sanity-check."),
    )
    p.text(["Ranked by rows flagged, not by $ -- none of these checks carries a waste dollar column.",
            "Flagged, Rows flagged and Not flagged are account-wide; only Serverless & Photon DBUs follows the Workspace filter above."],
           height=2)
    p.charts(
        bar("Flagged rows by check", f"""WITH {cte}
SELECT check_title AS check_name, COALESCE(critical_rows, 0) + COALESCE(warn_rows, 0) AS rows_flagged
FROM status
""", "check_name", "rows_flagged", fmt=INT, category_title="Check", value_title="Flagged rows",
            how="Critical plus Warn rows per unpriced check, account-wide."),
    )
    p.table(table("Every check here, worst first", f"""WITH {cte}
SELECT verdict AS status, check_title AS check_name, area,
       COALESCE(critical_rows, 0) + COALESCE(warn_rows, 0) AS rows_flagged
FROM status
ORDER BY CASE verdict WHEN 'Critical' THEN 0 WHEN 'Warn' THEN 1 ELSE 2 END, rows_flagged DESC
""", [("status", "Status"), ("check_name", "Check"), ("area", "Area"), ("rows_flagged", "Flagged rows", INT)],
                   how="The 5 unpriced checks, worst first by rows flagged; account-wide."))
    p.checks()


def method(d):
    p = d.page("method", "How it's counted")
    p.text([
        "Each source below is a check that already runs on its own home tab -- nothing here is a separate "
        "measurement.",
        "A check counts toward the priced total only when its own rows carry a possible-waste dollar column, "
        "the one column this app treats as recoverable waste. A check with no dollar column ranks by how many "
        "rows it flags instead (Flagged, not priced).",
    ], height=3)
    priced_rows = ",\n       ".join(
        f"({bdd._sql_str(d.title_of(q))}, {bdd._sql_str(_area_of(q))}, {bdd._sql_str(d.why_of(q) or '')})"
        for q in PRICED_IDS
    )
    p.table(table("Priced sources", f"""SELECT check_name, area, why FROM (VALUES
       {priced_rows}
) AS t(check_name, area, why)
""", [("check_name", "Check"), ("area", "Area"), ("why", "Why")], height=7,
                   how="The 5 checks whose own rows carry a possible-waste $ column; their totals are on Priced."))
    unpriced_rows = ",\n       ".join(
        f"({bdd._sql_str(d.title_of(q))}, {bdd._sql_str(_area_of(q))}, {bdd._sql_str(d.why_of(q) or '')})"
        for q in UNPRICED_IDS if q != "cost_premium_serverless_photon"
    )
    p.table(table("Flagged, no dollar figure", f"""SELECT check_name, area, why FROM (VALUES
       {unpriced_rows}
) AS t(check_name, area, why)
""", [("check_name", "Check"), ("area", "Area"), ("why", "Why")], height=7,
                   how="Checks that flag rows but have no $ column; ranked by rows flagged instead, on Flagged, not priced."))
    p.checks()
