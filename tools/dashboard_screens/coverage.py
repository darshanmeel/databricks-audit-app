"""Coverage & Gaps tab (web/src/tabs/tab_coverage.tsx): Data sources, Not assessed, Known
limitations. The app's own coverage numbers (GET /api/coverage: which system tables a direct
export actually touched) are not snapshotted, so Data sources reads access_source_table_coverage
instead -- the one check that reads every system table this audit uses and judges the 5 that must
fill every day (billing usage, query history, node timeline, job run timeline, audit log) for gaps
and lag. Not assessed and Known limitations read the snapshot's own run log and hold no checks of
their own, same as in the app."""
from tools import build_databricks_dashboards as bdd
from tools.dashboard_kit import INT, MONEY, NUM, bar, kpi, table

TOTAL_CHECKS = len(bdd.all_checks())


def build(d):
    sources(d)
    not_assessed(d)
    limits(d)


def sources(d):
    p = d.page("sources")
    p.kpis(
        kpi("System tables checked", """
SELECT count(*) AS value, concat(CAST(count(DISTINCT schema_name) AS STRING), ' schemas') AS target
FROM {S}.access_source_table_coverage
""", how="Every system table this audit reads, from access_source_table_coverage."),
        kpi("Empty tables", """
SELECT count_if(total_row_count = 0) AS value, 'May be a feature not turned on for this account' AS target
FROM {S}.access_source_table_coverage
""", target="target", tone="warn", how="Rows with total_row_count = 0, account-wide, not just this window."),
        kpi("Tables with a freshness problem", """
WITH s AS (SELECT * FROM {S}.access_source_table_coverage WHERE status <> 'OK')
SELECT count(*) AS value,
       COALESCE((SELECT concat('Worst: ', schema_name, '.', table_name) FROM s
                 ORDER BY CASE status WHEN 'CRITICAL' THEN 0 ELSE 1 END, lag_days DESC NULLS LAST LIMIT 1), 'None') AS target
""", target="target", tone="warn",
            how="Tables flagged Warn (a gap or lag) or Critical (no rows at all); the target names the worst by lag."),
        kpi("What this audit cost", """
WITH c AS (SELECT * FROM {named:cost_audit_self_usage} WHERE {ws})
SELECT ROUND(COALESCE((SELECT SUM(est_audit_usd_list) FROM c), 0) * {disc}, 2) AS value,
       concat(CAST(COALESCE((SELECT SUM(matched_statement_count) FROM c), 0) AS STRING), ' queries, ',
              CAST((SELECT count(*) FROM c) AS STRING), ' warehouses, list price') AS target
""", target="target", fmt=MONEY,
            how="This audit's own queries, matched by client app on each warehouse, priced at list less your discount."),
    )
    p.charts(
        bar("System tables by status", """
SELECT CASE status WHEN 'OK' THEN 'OK' WHEN 'WARN' THEN 'Gap or lag' WHEN 'CRITICAL' THEN 'No rows' ELSE status END AS status,
       count(*) AS tables
FROM {S}.access_source_table_coverage
GROUP BY 1
""", "status", "tables", fmt=INT, category_title="Status", value_title="Tables",
            how="Every system table this audit reads, grouped by its freshness status."),
        bar("Tables with the longest lag", """
SELECT concat(schema_name, '.', table_name) AS source, max(lag_days) AS days_since_row
FROM {S}.access_source_table_coverage
WHERE lag_days IS NOT NULL
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "source", "days_since_row", fmt=NUM, category_title="Table", value_title="Days since last row",
            how="Days between the newest row the snapshot saw and the snapshot's own run time; only tables with a measured lag."),
    )
    p.table(table("System tables, worst first", """
SELECT status, concat(schema_name, '.', table_name) AS source, rows_in_window, total_row_count,
       COALESCE(min_time, '-') AS min_time, COALESCE(max_time, '-') AS max_time,
       COALESCE(lag_days, -1) AS lag_days, days_with_data_in_window
FROM {S}.access_source_table_coverage
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, total_row_count ASC
LIMIT 200
""", [("status", "Status"), ("source", "System table"), ("rows_in_window", "Rows in window", INT),
      ("total_row_count", "Rows total", INT), ("min_time", "Earliest"), ("max_time", "Latest"),
      ("lag_days", "Lag, days", INT), ("days_with_data_in_window", "Days with data", INT)], height=10,
            how="Every system table this audit reads, emptiest and most critical first."))
    p.checks()


def _not_assessed_sql(d):
    entries = [(q, d.title_of(q), "Coverage") for q in bdd.all_checks()]
    inner = bdd._status_sql("{S}", entries)
    return ("SELECT check_title, COALESCE(error, 'No reason recorded') AS reason, row_count\n"
            "FROM (\n" + inner + ") t\n"
            "WHERE t.verdict = 'Not assessed'\n"
            "ORDER BY check_title\n")


def not_assessed(d):
    p = d.page("couldnt", title="Not assessed")
    na_sql = _not_assessed_sql(d)
    p.text(["A check reads \"Not assessed\" only when the snapshot could not run it at all -- a missing grant, "
            "a system table not enabled, or a read that failed. An empty panel for that check elsewhere in these "
            "dashboards is never a verified zero.",
            "The snapshot's run log carries no workspace_id, so every count on this page is account-wide; "
            "the Workspace filter above does not reach it."])
    p.kpis(
        kpi("Checks not assessed", "WITH n AS (\n" + na_sql + ")\nSELECT count(*) AS value, "
            f"concat('of ', CAST({TOTAL_CHECKS} AS STRING), ' checks this audit runs') AS target FROM n",
            target="target", tone="warn",
            how="Checks whose snapshot run logged an error or came back not ok, account-wide."),
        kpi("Checks assessed", "WITH n AS (\n" + na_sql + f")\nSELECT ({TOTAL_CHECKS} - count(*)) AS value, "
            "'ran clean or flagged something' AS target FROM n", target="target",
            how="Every other check: it ran and either came back clean or flagged something."),
    )
    p.table(table("Checks not assessed, with the reason", na_sql,
                  [("check_title", "Check"), ("reason", "Reason"), ("row_count", "Rows", INT)], height=10,
                  how="Every check with no verified result this snapshot, account-wide; the reason is from the run log."))


# The same general notes web/src/tabs/tab_coverage.tsx's CV_LIMITATIONS array carries -- plain
# text, no check of its own, kept word for word so this page never drifts from the app's.
_LIMITATIONS = [
    ("Some rankings are by size, not a threshold",
     "Spend by job, spend by cluster or warehouse, and spend by pipeline are ranked by size alone -- "
     "they always read Ranked, never Critical or Warn, however big the number."),
    ("Autoscale churn counts events, not time",
     "Warehouse autoscale churn counts how often a warehouse's cluster count changes, not how long it "
     "spent scaled up."),
    ("Task memory-pressure rule",
     "A job task is flagged for memory pressure from its 90th-percentile worker memory and swap; the "
     "swap threshold defaults to 10% in config/thresholds.yml, so a single brief spike can be enough to flag it."),
    ("Redacted SQL text pools together",
     "When a statement's text is hidden from the export, every redacted statement in the window pools "
     "into one heaviest-statement shape instead of being counted separately."),
]


def limits(d):
    p = d.page("limits", title="Known limitations")
    p.text(["What this shows: what still limits these numbers, beyond the fixed 30-day, list-price "
            "window stated on every page."])
    for title, note in _LIMITATIONS:
        p.text([f"## {title}", note])
