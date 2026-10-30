"""Overview tab (web/src/tabs/tab_overview.tsx): the account-wide roll-up everyone sees first --
spend and its change vs the previous period, possible waste, jobs & pipelines, query performance,
the biggest movers, a worklist of what to fix first, then jobs/queue state and spend by day and by
product. One page: the app's Overview has no sub-tabs of its own (AREA_REGISTRY: subtabs: []).

Not reproduced: the "list price -> tagged" path card (needs the app's tag-rollup settings, not a
snapshot table) and the coverage/account-shape aside (a whole-export coverage meter belongs on the
Coverage & Gaps dashboard, not repeated here). Query performance and the Jobs & pipelines fallback
both count flagged checks across their area's pages (area_registry), the same checks app/labels.ts
names -- not one single check standing in for the whole area.
"""
from tools import build_databricks_dashboards as bdd
from tools.dashboard_kit import INT, LINK, MONEY, bar, kpi, table, ws_clause


def build(d):
    overview(d)


def _area_status_entries(d, area_key):
    """(query_id, title, page) for every id placed on an area's own pages (not refs)."""
    area = bdd.area_registry()[area_key]
    return [(q, d.title_of(q), label) for _, label, ids, _ in area["pages"] for q in ids]


def overview(d):
    p = d.page("overview")
    waste_cte = bdd.waste_cte("{S}")
    waste_ws = ws_clause("workspace_name")
    jobs_status_sql = bdd._status_sql("{S}", _area_status_entries(d, "jobs"))
    queries_status_sql = bdd._status_sql("{S}", _area_status_entries(d, "queries"))

    p.kpis(
        kpi("Spend", """
WITH cur AS (SELECT * FROM {named:cost_dollarized_by_sku_day} WHERE {ws}),
     prev AS (SELECT * FROM {named:cost_period_over_period} WHERE status <> 'NOT_ASSESSED' AND {ws}),
     tot AS (
       SELECT COALESCE((SELECT SUM(net_list_cost) FROM cur), 0) AS cur_usd,
              (SELECT SUM(est_current_usd_list) FROM prev) AS cur_judged,
              (SELECT SUM(est_previous_usd_list) FROM prev) AS prev_usd
     )
SELECT ROUND(cur_usd * {disc}, 0) AS value,
       CASE WHEN prev_usd IS NULL OR prev_usd = 0 THEN 'No prior period to compare'
            WHEN ABS(cur_judged - prev_usd) < 0.5 THEN 'About flat vs the previous period'
            WHEN ABS(100.0 * (cur_judged - prev_usd) / prev_usd) > 999 THEN 'No prior period to compare'
            WHEN cur_judged > prev_usd THEN
              concat('Up ', CAST(CAST(ROUND(100.0 * (cur_judged - prev_usd) / prev_usd, 0) AS BIGINT) AS STRING), '% vs the previous period')
            ELSE
              concat('Down ', CAST(CAST(ROUND(100.0 * (prev_usd - cur_judged) / prev_usd, 0) AS BIGINT) AS STRING), '% vs the previous period')
       END AS target
FROM tot
""", target="target", fmt=MONEY,
            how="Total list-price $ this window, after discount. % change compares only rows judged on both sides."),
        kpi("Possible waste", waste_cte + f"""SELECT ROUND(COALESCE(SUM(wasted), 0), 0) AS value,
       CASE WHEN COUNT(*) = 0 THEN 'Nothing flagged'
            ELSE concat(CAST(COUNT(*) AS STRING), ' resources; worst: ', COALESCE(MAX_BY(resource_name, wasted), 'none'))
       END AS target
FROM named
WHERE {waste_ws}
""", target="target", fmt=MONEY, tone="warn",
            how="$ from the 5 priced waste checks (idle warehouse/cluster time, failed runs/statements, idle endpoints), over the cost floor."),
        kpi("Jobs & pipelines", """
WITH r AS (SELECT * FROM {named:lakeflow_job_reliability} WHERE {ws}),
     checks AS (""" + jobs_status_sql + """),
     tot AS (
       SELECT (SELECT count(*) FROM r WHERE status = 'CRITICAL') AS broken,
              (SELECT count_if(critical_rows > 0 OR warn_rows > 0) FROM checks) AS flagged,
              (SELECT concat('Worst: ', nm_job, ', ', CAST(consecutive_failures AS STRING), ' runs failed in a row')
               FROM r WHERE status = 'CRITICAL' ORDER BY consecutive_failures DESC LIMIT 1) AS worst_job,
              (SELECT concat('Worst: ', check_title) FROM checks WHERE critical_rows > 0 OR warn_rows > 0
               ORDER BY critical_rows DESC, warn_rows DESC LIMIT 1) AS worst_check
     )
SELECT CASE WHEN broken > 0 THEN broken ELSE flagged END AS value,
       CASE WHEN broken > 0 THEN COALESCE(worst_job, 'Nothing flagged')
            ELSE COALESCE(worst_check, 'Nothing flagged') END AS target
FROM tot
""", target="target", tone="crit",
            how="Jobs whose last 3+ runs all failed; with none, counts flagged jobs-area checks instead."),
        kpi("Query performance", """
WITH checks AS (""" + queries_status_sql + """)
SELECT count_if(critical_rows > 0 OR warn_rows > 0) AS value,
       COALESCE((SELECT concat('Worst: ', check_title) FROM checks WHERE critical_rows > 0 OR warn_rows > 0
                 ORDER BY critical_rows DESC, warn_rows DESC LIMIT 1), 'Nothing flagged') AS target
FROM checks
""", target="target", tone="warn",
            how="Count of query-area checks with a Critical or Warn row; names the worst one."),
    )

    p.table(table("Every dashboard of the set", f"""
SELECT dashboard, url FROM {{S}}.{bdd.LINKS_TABLE}
WHERE dashboard <> '{d.name}'
ORDER BY dashboard
""", [("dashboard", "Dashboard"), ("url", "Link", LINK)], height=6,
        how="Found by name when the notebook last ran. Empty: import the dashboards, then run the notebook's last cell."))
    p.charts(
        bar("Biggest movers vs the previous period", """
WITH base AS (SELECT * FROM {named:cost_period_over_period} WHERE status <> 'NOT_ASSESSED' AND {ws})
SELECT nm_workspace AS workspace, ROUND(SUM(est_current_usd_list - est_previous_usd_list) * {disc}, 0) AS delta_usd
FROM base
GROUP BY 1
ORDER BY ABS(SUM(est_current_usd_list - est_previous_usd_list)) DESC
LIMIT 8
""", "workspace", "delta_usd", fmt=MONEY, category_title="Workspace", value_title="Change vs previous period",
            how="$ change per workspace vs the prior period, rows judged on both sides only, after discount."),
    )

    p.table(table("Fix these first", waste_cte + f"""
SELECT source_label AS check_label, resource_name AS resource, workspace_name AS workspace,
       ROUND(wasted, 0) AS wasted_usd
FROM named
WHERE {waste_ws}
ORDER BY wasted DESC
LIMIT 10
""", [("check_label", "Check"), ("resource", "Resource"), ("workspace", "Workspace"), ("wasted_usd", "$ wasted", MONEY)],
        how="Top 10 rows from the 5 priced waste checks, worst $ first, after the cost floor and discount."))

    p.charts(
        bar("Jobs and pipelines by day", """
SELECT usage_date AS day,
       CASE day_state WHEN 'ok' THEN 'Ran fine' WHEN 'failed' THEN 'Failed' WHEN 'slow' THEN 'Ran slow'
                      WHEN 'skipped' THEN 'Skipped' ELSE day_state END AS state,
       count(*) AS resources
FROM {named:lakeflow_daily_state}
WHERE {ws}
GROUP BY 1, 2
""", "day", "resources", series="state", time=True, fmt=INT, category_title="Day", value_title="Jobs and pipelines",
            how="Per-day worst run state: Failed beats Ran slow (60s+ over its own median) beats Skipped beats Ran fine."),
        bar("Queued queries by day", """
SELECT day, SUM(queued_at_capacity_count) AS queued
FROM {named:query_queuing_waits}
WHERE {ws}
GROUP BY 1
""", "day", "queued", time=True, fmt=INT, category_title="Day", value_title="Queued queries",
            how="Count of queries that waited for warehouse capacity before they could run."),
    )

    product_case = bdd.PRODUCT_LABEL_CASE_SQL.format(col="billing_origin_product")
    p.charts(
        bar("Daily spend", """
SELECT usage_date AS day, ROUND(SUM(net_list_cost) * {disc}, 0) AS usd
FROM {named:cost_dollarized_by_sku_day}
WHERE {ws}
GROUP BY 1
""", "day", "usd", time=True, fmt=MONEY, category_title="Day", value_title="Spend",
            how="List-price $ per day across all SKUs, after discount."),
        bar("Spend by product", """
SELECT """ + product_case + """ AS product, ROUND(SUM(net_list_cost) * {disc}, 0) AS usd
FROM {named:cost_dollarized_by_sku_day}
WHERE {ws}
GROUP BY 1
ORDER BY 2 DESC
LIMIT 10
""", "product", "usd", fmt=MONEY, category_title="Product", value_title="Spend",
            how="Top 10 billing products by list-price $ this window, after discount."),
    )

    p.checks(ids=[], refs=[
        "overview_spend_estimate", "overview_dbu_by_sku", "overview_serverless_classic_split",
        "overview_daily_dbu_trend", "overview_list_prices_raw",
    ])
