"""Queries tab (web/src/tabs/tab_query.tsx, query_trend.tsx, query_queue.tsx): Heavy queries,
Efficiency, Reliability, Capacity, Trend. "By team" is left out: it has no check of its own, just
the same tag-rollup widget Cost > Allocation already uses."""
from tools.dashboard_kit import INT, MONEY, NUM, PCT, bar, kpi, line, table

# query_top_by_cost's own group, with a name where one is resolvable (job/notebook/pipeline) and
# a short kind + id otherwise -- the dashboard's twin of the app's heavyGroupCell/topCostGroupCell
# (web/src/tabs/tab_query.tsx). One row per warehouse a group ran on; ROLLUP_CTE below sums a
# group across every warehouse it touched, the way the app's client-side rollup does.
GROUP_CTES = """g AS (
  SELECT t.*, COALESCE(t.source_kind, t.group_kind) AS kind,
         COALESCE(CAST(t.source_id AS STRING), t.group_id) AS gid
  FROM {named:query_top_by_cost} t
  WHERE {ws}
),
glabel AS (
  SELECT g.*,
         CASE
           WHEN kind = 'job' THEN COALESCE(j.n, concat('Job ', left(gid, 8)))
           WHEN kind = 'notebook' THEN COALESCE(nb.n, concat('Notebook ', left(gid, 8)))
           WHEN kind = 'pipeline' THEN COALESCE(p.n, concat('Pipeline ', left(gid, 8)))
           WHEN kind IS NULL THEN concat('Ad-hoc ', left(gid, 8))
           ELSE concat(initcap(kind), ' ', left(gid, 8))
         END AS group_label
  FROM g
  LEFT JOIN (SELECT workspace_id, job_id, max(name) AS n FROM {S}.dim_job GROUP BY 1, 2) j
    ON j.workspace_id = g.workspace_id AND j.job_id = g.gid
  LEFT JOIN (SELECT workspace_id, notebook_id, max(notebook_path) AS n FROM {S}.dim_notebook GROUP BY 1, 2) nb
    ON nb.workspace_id = g.workspace_id AND nb.notebook_id = g.gid
  LEFT JOIN (SELECT workspace_id, pipeline_id, max(pipeline_name) AS n FROM {S}.dim_pipeline GROUP BY 1, 2) p
    ON p.workspace_id = g.workspace_id AND p.pipeline_id = g.gid
)"""

# One row per query group (job/notebook/pipeline/text-hash), summed across every warehouse it ran
# on. A text-hash group is the same query text wherever it ran, so it rolls up account-wide; a
# job/notebook/pipeline id is only unique inside its own workspace, so that key keeps workspace_id.
ROLLUP_CTE = """rollup AS (
  SELECT group_kind, CASE WHEN group_kind = 'text hash' THEN NULL ELSE workspace_id END AS ws_key,
         group_id, statement_type,
         MAX_BY(group_label, runs) AS group_label,
         SUM(est_cost_usd_list) AS usd,
         SUM(runs) AS runs,
         SUM(p50_duration_ms * runs) / NULLIF(SUM(CASE WHEN p50_duration_ms IS NOT NULL THEN runs END), 0) AS p50_ms,
         SUM(p95_duration_ms * runs) / NULLIF(SUM(CASE WHEN p95_duration_ms IS NOT NULL THEN runs END), 0) AS p95_ms,
         SUM(read_files) AS read_files,
         SUM(pruned_files) AS pruned_files,
         MIN(CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END) AS sev,
         count(DISTINCT warehouse_id) AS warehouses,
         MAX_BY(top_user, runs) AS top_user,
         max(last_seen) AS last_seen
  FROM glabel
  GROUP BY 1, 2, 3, 4
)"""


def _with_group(select_tail: str) -> str:
    return "WITH " + GROUP_CTES + "\n" + select_tail


def _with_rollup(select_tail: str) -> str:
    return "WITH " + GROUP_CTES + ",\n" + ROLLUP_CTE + "\n" + select_tail


def build(d):
    heavy(d)
    efficiency(d)
    reliability(d)
    capacity(d)
    trend(d)


def heavy(d):
    p = d.page("heavy")
    p.kpis(
        kpi("Query groups over threshold", _with_rollup("""
SELECT (SELECT count(*) FROM rollup WHERE sev <= 1) AS value,
       concat('of ', CAST((SELECT count(*) FROM rollup) AS STRING), ' groups; over $50 or 20% of warehouse spend') AS target
"""), target="target", tone="warn",
            how="Groups summed across warehouses. Flagged at $50 or 20% of a warehouse's spend (warn), "
                "$200 or 40% (critical)."),
        kpi("Worst query group", _with_rollup("""
SELECT ROUND(COALESCE((SELECT usd FROM rollup ORDER BY usd DESC LIMIT 1), 0) * {disc}, 0) AS value,
       COALESCE((SELECT concat('Worst: ', statement_type, ': ', group_label, ', ', CAST(runs AS STRING), ' runs')
                 FROM rollup ORDER BY usd DESC LIMIT 1), 'None this window') AS target
"""), target="target", fmt=MONEY, tone="warn",
            how="Highest list-price $ group, after summing the same query across every warehouse it ran on."),
        kpi("This audit's own query cost", """
SELECT ROUND(COALESCE(SUM(est_audit_usd_list), 0) * {disc}, 0) AS value,
       concat(CAST(COALESCE(SUM(matched_statement_count), 0) AS STRING), ' statements, ',
              CAST(ROUND(COALESCE(SUM(matched_duration_secs), 0) / 60.0, 0) AS STRING), ' min run time') AS target
FROM {named:cost_audit_self_usage}
WHERE {ws}
""", target="target", fmt=MONEY,
            how="Statements matched to this audit tool's own client application; not query cost from your other use of Databricks."),
        kpi("Statements seen", """
WITH p AS (SELECT * FROM {named:query_provenance_by_source} WHERE {ws}),
by_src AS (SELECT source_kind, SUM(query_count) AS n FROM p GROUP BY 1)
SELECT COALESCE((SELECT SUM(n) FROM by_src), 0) AS value,
       COALESCE((SELECT concat('Top: ', COALESCE(initcap(source_kind), 'unknown'), ' (', CAST(n AS STRING), ')')
                 FROM by_src ORDER BY n DESC LIMIT 1), 'No statements this window') AS target
""", target="target",
            how="Statement count from every source (job, notebook, ad-hoc, …) in the window; the line names the biggest source."),
    )
    p.text(["Flagged: $200 or 40% of a warehouse's spend is critical; $50 or 20% is warn."], height=1)
    p.charts(
        bar("Top query groups by $", _with_rollup("""
SELECT concat(statement_type, ': ', group_label) AS group_label, SUM(usd) * {disc} AS usd
FROM rollup
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
"""), "group_label", "usd", fmt=MONEY, category_title="Group", value_title="Spend",
            how="The same query (by job, notebook, pipeline or text) summed across every warehouse it ran on, worst first."),
        bar("Statements by source", """
SELECT CASE source_kind WHEN 'job' THEN 'Job' WHEN 'notebook' THEN 'Notebook' WHEN 'pipeline' THEN 'Pipeline'
            WHEN 'dashboard' THEN 'Dashboard' WHEN 'alert' THEN 'Alert' WHEN 'Genie space' THEN 'Genie space'
            ELSE COALESCE(initcap(source_kind), 'Unknown') END AS source,
       SUM(query_count) AS statements
FROM {named:query_provenance_by_source}
WHERE {ws}
GROUP BY 1 ORDER BY 2 DESC LIMIT 10
""", "source", "statements", fmt=INT, category_title="Source", value_title="Statements",
            how="Statement count grouped by what submitted it."),
    )
    p.charts(
        bar("Warehouse spend by statement type", """
SELECT statement_type, ROUND(SUM(est_cost_usd_list) * {disc}, 0) AS usd
FROM {named:query_top_by_cost}
WHERE {ws}
GROUP BY 1 ORDER BY 2 DESC LIMIT 10
""", "statement_type", "usd", fmt=MONEY, category_title="Statement type", value_title="Spend",
            how="List-price spend by SQL statement type (SELECT, INSERT, …), summed across warehouses."),
        bar("Query groups by status", """
SELECT CASE status WHEN 'CRITICAL' THEN 'Critical' WHEN 'WARN' THEN 'Warn' ELSE 'OK' END AS status,
       count(*) AS groups
FROM {named:query_top_by_cost}
WHERE {ws}
GROUP BY 1
""", "status", "groups", fmt=INT, category_title="Status", value_title="Groups",
            how="Count of (warehouse, group) rows by status, before the cross-warehouse rollup above."),
    )
    p.table(table("Top queries by $", _with_rollup("""
SELECT concat(statement_type, ': ', group_label) AS group_name, warehouses, ROUND(usd * {disc}, 0) AS usd,
       runs, ROUND(p50_ms / 1000.0, 1) AS p50_s, ROUND(p95_ms / 1000.0, 1) AS p95_s,
       ROUND(100.0 * pruned_files / NULLIF(read_files + pruned_files, 0), 0) AS pruned_pct,
       COALESCE(top_user, '-') AS top_user,
       CASE sev WHEN 0 THEN 'CRITICAL' WHEN 1 THEN 'WARN' WHEN 2 THEN 'NOT_ASSESSED' ELSE 'OK' END AS status,
       date_format(last_seen, 'yyyy-MM-dd HH:mm') AS latest_run
FROM rollup
ORDER BY usd DESC
LIMIT 50
"""), [("group_name", "Group"), ("warehouses", "Warehouses", INT), ("usd", "$", MONEY), ("runs", "Runs", INT),
      ("p50_s", "p50 (s)", NUM), ("p95_s", "p95 (s)", NUM), ("pruned_pct", "Files skipped %", PCT),
      ("top_user", "Top user"), ("status", "Status"), ("latest_run", "Latest run")], height=10,
        how="Each group rolled up across every warehouse it ran on; p50/p95 and skip % are run-weighted averages."))
    p.checks()


def efficiency(d):
    p = d.page("efficiency")
    p.kpis(
        kpi("Spilled to local disk (GB)", """
WITH s AS (SELECT * FROM {named:query_local_spillage} WHERE {ws})
SELECT ROUND(COALESCE(SUM(spilled_local_bytes_sum), 0) / 1e9, 1) AS value,
       concat(CAST((SELECT count(DISTINCT warehouse_id) FROM s WHERE spilled_local_bytes_sum > 0) AS STRING),
              ' of ', CAST((SELECT count(DISTINCT warehouse_id) FROM s WHERE warehouse_id IS NOT NULL) AS STRING),
              ' warehouses spilling') AS target
FROM s
""", target="target", fmt=NUM, tone="warn",
            how="Bytes spilled to local disk during execution, summed across warehouses; spilling slows a query."),
        kpi("Shuffled between nodes (GB)", """
WITH s AS (SELECT * FROM {named:query_shuffle_write_amplification} WHERE {ws})
SELECT ROUND(COALESCE(SUM(shuffle_read_bytes_sum), 0) / 1e9, 1) AS value,
       concat('across ', CAST((SELECT count(DISTINCT warehouse_id) FROM s WHERE warehouse_id IS NOT NULL) AS STRING),
              ' warehouses') AS target
FROM s
""", target="target", fmt=NUM, tone="warn",
            how="Bytes shuffled between nodes while running, summed across warehouses; heavy shuffle often means a skewed join."),
        kpi("Read from disk cache (%)", """
WITH c AS (SELECT * FROM {named:query_cache_coldstart} WHERE {ws} AND read_io_cache_percent_avg IS NOT NULL)
SELECT ROUND(COALESCE(SUM(read_io_cache_percent_avg * scanned_query_count) / NULLIF(SUM(scanned_query_count), 0), 0), 0) AS value,
       concat('across ', CAST((SELECT count(DISTINCT warehouse_id) FROM c WHERE warehouse_id IS NOT NULL) AS STRING),
              ' warehouses') AS target
FROM c
""", target="target", fmt=PCT,
            how="Share of scanned bytes served from the warehouse's disk cache, weighted by scans, across warehouses."),
        kpi("Files skipped by pruning (%)", """
WITH p AS (SELECT * FROM {named:query_pruning_effectiveness} WHERE {ws})
SELECT ROUND(100.0 * COALESCE(SUM(pruned_files_sum), 0) / NULLIF(SUM(pruned_files_sum) + SUM(read_files_sum), 0), 0) AS value,
       concat('across ', CAST((SELECT count(DISTINCT warehouse_id) FROM p WHERE warehouse_id IS NOT NULL) AS STRING),
              ' warehouses') AS target
FROM p
""", target="target", fmt=PCT,
            how="Files skipped by file/partition pruning as a share of files read plus skipped, across warehouses."),
    )
    p.charts(
        bar("Warehouses spilling the most (top)", """
SELECT lbl_warehouse, ROUND(SUM(spilled_local_bytes_sum) / 1e9, 1) AS gb
FROM {named:query_local_spillage}
WHERE {ws} AND warehouse_id IS NOT NULL AND spilled_local_bytes_sum > 0
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "lbl_warehouse", "gb", fmt=NUM, category_title="Warehouse", value_title="GB spilled",
            how="Local-disk spill GB per warehouse, top 8."),
        bar("Warehouses shuffling the most (top)", """
SELECT lbl_warehouse, ROUND(SUM(shuffle_read_bytes_sum) / 1e9, 1) AS gb
FROM {named:query_shuffle_write_amplification}
WHERE {ws} AND warehouse_id IS NOT NULL AND shuffle_read_bytes_sum > 0
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "lbl_warehouse", "gb", fmt=NUM, category_title="Warehouse", value_title="GB shuffled",
            how="Shuffle-read GB per warehouse, top 8."),
    )
    p.charts(
        bar("Warehouses missing the cache most (top)", """
WITH c AS (
  SELECT lbl_warehouse, SUM(scanned_query_count) AS scans,
         SUM(read_io_cache_percent_avg * scanned_query_count) / NULLIF(SUM(scanned_query_count), 0) AS cache_pct
  FROM {named:query_cache_coldstart}
  WHERE {ws} AND warehouse_id IS NOT NULL AND read_io_cache_percent_avg IS NOT NULL
  GROUP BY 1
)
SELECT lbl_warehouse, ROUND(100 - cache_pct, 0) AS miss_pct
FROM c
WHERE scans > 0
ORDER BY miss_pct DESC LIMIT 8
""", "lbl_warehouse", "miss_pct", fmt=PCT, category_title="Warehouse", value_title="Cache miss %",
            how="100% minus the scan-weighted disk-cache-hit rate, top 8 warehouses."),
        bar("Warehouses reading the most despite pruning (top)", """
SELECT lbl_warehouse, SUM(read_files_sum) AS files_read
FROM {named:query_pruning_effectiveness}
WHERE {ws} AND warehouse_id IS NOT NULL
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "lbl_warehouse", "files_read", fmt=INT, category_title="Warehouse", value_title="Files read",
            how="Files read after pruning, summed per warehouse, top 8."),
    )
    p.table(table("Efficiency by warehouse", """
WITH spill AS (
  SELECT workspace_id, warehouse_id, MAX(n_warehouse) AS n_warehouse, SUM(spilled_local_bytes_sum) AS spill_bytes
  FROM {named:query_local_spillage}
  WHERE {ws} AND warehouse_id IS NOT NULL
  GROUP BY 1, 2
),
shuffle AS (
  SELECT workspace_id, warehouse_id, MAX(n_warehouse) AS n_warehouse, SUM(shuffle_read_bytes_sum) AS shuffle_bytes
  FROM {named:query_shuffle_write_amplification}
  WHERE {ws} AND warehouse_id IS NOT NULL
  GROUP BY 1, 2
),
prune AS (
  SELECT workspace_id, warehouse_id, MAX(n_warehouse) AS n_warehouse, SUM(pruned_files_sum) AS pruned, SUM(read_files_sum) AS read_f
  FROM {named:query_pruning_effectiveness}
  WHERE {ws} AND warehouse_id IS NOT NULL
  GROUP BY 1, 2
),
cache AS (
  SELECT workspace_id, warehouse_id, MAX(n_warehouse) AS n_warehouse,
         SUM(read_io_cache_percent_avg * scanned_query_count) / NULLIF(SUM(scanned_query_count), 0) AS cache_pct
  FROM {named:query_cache_coldstart}
  WHERE {ws} AND warehouse_id IS NOT NULL AND read_io_cache_percent_avg IS NOT NULL
  GROUP BY 1, 2
),
whs AS (
  SELECT workspace_id, warehouse_id, n_warehouse FROM spill
  UNION SELECT workspace_id, warehouse_id, n_warehouse FROM shuffle
  UNION SELECT workspace_id, warehouse_id, n_warehouse FROM prune
  UNION SELECT workspace_id, warehouse_id, n_warehouse FROM cache
)
SELECT w.n_warehouse AS warehouse,
       ROUND(COALESCE(s.spill_bytes, 0) / 1e9, 1) AS spill_gb,
       ROUND(COALESCE(sh.shuffle_bytes, 0) / 1e9, 1) AS shuffle_gb,
       ROUND(c.cache_pct, 0) AS cache_pct,
       ROUND(100.0 * p.pruned / NULLIF(p.pruned + p.read_f, 0), 0) AS prune_pct
FROM whs w
LEFT JOIN spill s ON s.workspace_id = w.workspace_id AND s.warehouse_id = w.warehouse_id
LEFT JOIN shuffle sh ON sh.workspace_id = w.workspace_id AND sh.warehouse_id = w.warehouse_id
LEFT JOIN prune p ON p.workspace_id = w.workspace_id AND p.warehouse_id = w.warehouse_id
LEFT JOIN cache c ON c.workspace_id = w.workspace_id AND c.warehouse_id = w.warehouse_id
ORDER BY spill_gb DESC
LIMIT 50
""", [("warehouse", "Warehouse"), ("spill_gb", "Spilled (GB)", NUM), ("shuffle_gb", "Shuffled (GB)", NUM),
      ("cache_pct", "From disk cache %", PCT), ("prune_pct", "Files skipped %", PCT)],
        how="Spill, shuffle, cache and pruning joined per warehouse id, not by name, so same-named warehouses in "
            "different workspaces stay separate rows."))
    p.checks()


def reliability(d):
    p = d.page("reliability")
    p.kpis(
        kpi("Failed statements", """
WITH g AS (SELECT * FROM {named:query_failed_statements_grouped} WHERE {ws})
SELECT COALESCE((SELECT SUM(statements) FROM g WHERE execution_status = 'FAILED'), 0) AS value,
       concat(CAST(COALESCE((SELECT SUM(statements) FROM g WHERE execution_status = 'CANCELED'), 0) AS STRING),
              ' canceled, not counted; ',
              CAST(COALESCE((SELECT count(DISTINCT error_class) FROM g WHERE execution_status = 'FAILED'), 0) AS STRING),
              ' error classes') AS target
""", target="target", tone="crit",
            how="Statements with execution_status = FAILED; canceled statements are tracked separately, not counted here."),
        kpi("Compute burned on failed statements", """
WITH w AS (SELECT * FROM {named:cost_failed_statement_waste} WHERE {ws})
SELECT ROUND(COALESCE(SUM(est_wasted_usd_list), 0) * {disc}, 0) AS value,
       concat(CAST((SELECT count(*) FROM w WHERE status IN ('CRITICAL', 'WARN')) AS STRING), ' of ',
              CAST((SELECT count(*) FROM w) AS STRING), ' warehouses flagged') AS target
FROM w
""", target="target", fmt=MONEY, tone="warn",
            how="List-price $ of warehouse time spent on statements that failed, from the per-warehouse waste check."),
        kpi("Days with a failure", """
WITH f AS (SELECT * FROM {named:query_failed_queries_daily} WHERE {ws} AND execution_status IS NOT NULL AND execution_status <> 'FINISHED')
SELECT (SELECT count(DISTINCT day) FROM f WHERE query_count > 0) AS value,
       'days with a failed or canceled statement' AS target
""", target="target",
            how="Distinct days with at least one failed or canceled statement."),
        kpi("Worst warehouse burned", """
WITH w AS (SELECT * FROM {named:cost_failed_statement_waste} WHERE {ws})
SELECT ROUND(COALESCE((SELECT est_wasted_usd_list FROM w ORDER BY est_wasted_usd_list DESC LIMIT 1), 0) * {disc}, 0) AS value,
       COALESCE((SELECT concat('Worst: ', nm_warehouse, ', ', CAST(failed_statements AS STRING), ' failed')
                 FROM w ORDER BY est_wasted_usd_list DESC LIMIT 1), 'None this window') AS target
""", target="target", fmt=MONEY, tone="warn",
            how="Highest list-price $ burned on failed statements, for one warehouse."),
    )
    p.charts(
        line("Failed or canceled statements per day", """
SELECT day, SUM(query_count) AS statements
FROM {named:query_failed_queries_daily}
WHERE {ws} AND execution_status IS NOT NULL AND execution_status <> 'FINISHED'
GROUP BY 1
""", "day", "statements", fmt=INT, value_title="Statements",
            how="Daily count of statements whose execution_status is not FINISHED (failed or canceled)."),
        bar("Failed statements by error class", """
SELECT error_class, SUM(statements) AS statements
FROM {named:query_failed_statements_grouped}
WHERE {ws} AND execution_status = 'FAILED'
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "error_class", "statements", fmt=INT, category_title="Error class", value_title="Statements",
            how="Failed statements grouped by error class, top 8."),
    )
    p.charts(
        bar("Failed statements by source", """
SELECT CASE COALESCE(source_kind, 'none') WHEN 'job' THEN 'Job' WHEN 'notebook' THEN 'Notebook' WHEN 'pipeline' THEN 'Pipeline'
            WHEN 'dashboard' THEN 'Dashboard' WHEN 'alert' THEN 'Alert' WHEN 'saved query' THEN 'Saved query'
            WHEN 'Genie space' THEN 'Genie space' ELSE 'Ad-hoc' END AS source,
       SUM(statements) AS statements
FROM {named:query_failed_statements_grouped}
WHERE {ws} AND execution_status = 'FAILED'
GROUP BY 1 ORDER BY 2 DESC
""", "source", "statements", fmt=INT, category_title="Source", value_title="Statements",
            how="Failed statements grouped by what submitted them."),
        bar("Compute burned by warehouse", """
SELECT lbl_warehouse, ROUND(SUM(est_wasted_usd_list) * {disc}, 0) AS usd
FROM {named:cost_failed_statement_waste}
WHERE {ws}
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "lbl_warehouse", "usd", fmt=MONEY, category_title="Warehouse", value_title="Spend",
            how="List-price $ of warehouse time spent on failed statements, summed per warehouse, top 8."),
    )
    p.table(table("Top errors", """
SELECT error_class, n_warehouse AS warehouse,
       CASE COALESCE(source_kind, 'none') WHEN 'job' THEN 'Job' WHEN 'notebook' THEN 'Notebook' WHEN 'pipeline' THEN 'Pipeline'
            WHEN 'dashboard' THEN 'Dashboard' WHEN 'alert' THEN 'Alert' WHEN 'saved query' THEN 'Saved query'
            WHEN 'Genie space' THEN 'Genie space' ELSE 'Ad-hoc' END AS source,
       statements, date_format(last_seen, 'yyyy-MM-dd HH:mm') AS last_seen, status
FROM {named:query_failed_statements_grouped}
WHERE {ws} AND execution_status = 'FAILED'
ORDER BY statements DESC
LIMIT 50
""", [("error_class", "Error class"), ("warehouse", "Warehouse"), ("source", "Source"),
      ("statements", "Count", INT), ("last_seen", "Last seen (UTC)"), ("status", "Status")],
        how="Failed statements by error class and warehouse, worst count first."))
    p.checks()


def capacity(d):
    p = d.page("capacity")
    p.kpis(
        kpi("Warehouses under pressure", """
WITH pr AS (SELECT * FROM {named:query_warehouse_pressure} WHERE {ws})
SELECT (SELECT count(*) FROM pr WHERE status IN ('CRITICAL', 'WARN')) AS value,
       COALESCE((SELECT concat('Worst: ', nm_warehouse) FROM pr WHERE status IN ('CRITICAL', 'WARN')
                 ORDER BY CASE status WHEN 'CRITICAL' THEN 0 ELSE 1 END, capacity_wait_pct DESC NULLS LAST LIMIT 1),
                'None flagged') AS target
""", target="target", tone="crit",
            how="Warehouses flagged on memory and/or capacity pressure from recent query load."),
        kpi("Queued for a slot (s)", """
WITH q AS (SELECT * FROM {named:query_queuing_waits} WHERE {ws})
SELECT ROUND(COALESCE(SUM(waiting_at_capacity_ms_sum), 0) / 1000.0, 0) AS value,
       COALESCE((SELECT concat('Worst: ', nm_warehouse) FROM (
            SELECT nm_warehouse, SUM(waiting_at_capacity_ms_sum) AS s FROM q GROUP BY 1 ORDER BY 2 DESC LIMIT 1
       )), 'None queued') AS target
FROM q
""", target="target", fmt=NUM, tone="warn",
            how="Seconds queries waited for a warehouse slot once it was at its cluster limit, summed across warehouses."),
        kpi("Waiting for start-up (s)", """
WITH q AS (SELECT * FROM {named:query_queuing_waits} WHERE {ws})
SELECT ROUND(COALESCE(SUM(waiting_for_compute_ms_sum), 0) / 1000.0, 0) AS value,
       COALESCE((SELECT concat('Worst: ', nm_warehouse) FROM (
            SELECT nm_warehouse, SUM(waiting_for_compute_ms_sum) AS s FROM q GROUP BY 1 ORDER BY 2 DESC LIMIT 1
       )), 'None waiting') AS target
FROM q
""", target="target", fmt=NUM, tone="warn",
            how="Seconds queries waited for a new cluster to start, summed across warehouses."),
        kpi("Workload peak hour (UTC)", """
WITH m AS (
  SELECT hour_of_day, SUM(query_count) AS n
  FROM {named:query_workload_mix_hours}
  WHERE {ws}
  GROUP BY 1
), tot AS (SELECT SUM(n) AS total FROM m)
SELECT COALESCE((SELECT hour_of_day FROM m ORDER BY n DESC LIMIT 1), 0) AS value,
       COALESCE((SELECT concat(lpad(CAST(hour_of_day AS STRING), 2, '0'), ':00 UTC, ',
                 CAST(ROUND(100.0 * n / NULLIF((SELECT total FROM tot), 0), 0) AS STRING), '% of statements')
                 FROM m ORDER BY n DESC LIMIT 1), 'No statements this window') AS target
""", target="target", fmt=INT,
            how="The UTC hour with the most statements run; the line gives its share of all statements in the window."),
        kpi("Result fetching by clients (s)", """
WITH w AS (SELECT * FROM {named:perf_daily_by_resource} WHERE {ws} AND resource_type = 'warehouse')
SELECT ROUND(COALESCE(SUM(fetch_s), 0), 0) AS value,
       COALESCE((SELECT concat('Worst: ', nm_warehouse) FROM (
           SELECT nm_warehouse, SUM(fetch_s) AS s FROM w GROUP BY 1 ORDER BY 2 DESC LIMIT 1
       )), 'None this window') AS target
FROM w
""", target="target", fmt=NUM,
            how="Seconds results sat waiting for the client to fetch them, summed across warehouses; a slow client, not the query."),
    )
    p.charts(
        bar("Warehouses by pressure", """
SELECT CASE COALESCE(pressure, 'NOT_MEASURED') WHEN 'NONE' THEN 'No pressure' WHEN 'MEMORY' THEN 'Memory'
            WHEN 'CAPACITY' THEN 'Capacity' WHEN 'MEMORY_AND_CAPACITY' THEN 'Memory and capacity'
            ELSE 'Not measured' END AS pressure,
       count(*) AS warehouses
FROM {named:query_warehouse_pressure}
WHERE {ws}
GROUP BY 1
""", "pressure", "warehouses", fmt=INT, category_title="Pressure", value_title="Warehouses",
            how="Count of warehouses by their pressure flag."),
        bar("Workload by hour of day (UTC)", """
SELECT concat(lpad(CAST(hour_of_day AS STRING), 2, '0'), ':00') AS hour, SUM(query_count) AS statements
FROM {named:query_workload_mix_hours}
WHERE {ws}
GROUP BY 1
""", "hour", "statements", fmt=INT, category_title="Hour (UTC)", value_title="Statements", sort="category",
            how="Statement count by the UTC hour it started, bars left to right in hour order."),
    )
    p.charts(
        bar("Warehouses queued longest for a slot (s)", """
SELECT lbl_warehouse, ROUND(SUM(waiting_at_capacity_ms_sum) / 1000.0, 0) AS wait_s
FROM {named:query_queuing_waits}
WHERE {ws} AND waiting_at_capacity_ms_sum > 0
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "lbl_warehouse", "wait_s", fmt=NUM, category_title="Warehouse", value_title="Seconds",
            how="Seconds waiting for a warehouse slot, summed per warehouse, top 8."),
        bar("Warehouses waiting longest for start-up (s)", """
SELECT lbl_warehouse, ROUND(SUM(waiting_for_compute_ms_sum) / 1000.0, 0) AS wait_s
FROM {named:query_queuing_waits}
WHERE {ws} AND waiting_for_compute_ms_sum > 0
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "lbl_warehouse", "wait_s", fmt=NUM, category_title="Warehouse", value_title="Seconds",
            how="Seconds waiting for a new cluster to start, summed per warehouse, top 8."),
    )
    p.table(table("Queued time by warehouse", """
WITH q AS (
  SELECT nm_warehouse, workspace_id, warehouse_id,
         SUM(waiting_at_capacity_ms_sum) AS slot_ms, SUM(waiting_for_compute_ms_sum) AS start_ms,
         SUM(query_count) AS queries
  FROM {named:query_queuing_waits}
  WHERE {ws}
  GROUP BY 1, 2, 3
),
pr AS (
  SELECT workspace_id, warehouse_id, scaling_hint FROM {S}.query_warehouse_pressure
)
SELECT q.nm_warehouse AS warehouse, ROUND(q.slot_ms / 1000.0, 0) AS slot_wait_s,
       ROUND(q.start_ms / 1000.0, 0) AS startup_wait_s, q.queries,
       COALESCE(pr.scaling_hint, '-') AS fix
FROM q
LEFT JOIN pr ON pr.workspace_id = q.workspace_id AND pr.warehouse_id = q.warehouse_id
ORDER BY slot_wait_s DESC
LIMIT 50
""", [("warehouse", "Warehouse"), ("slot_wait_s", "Waiting for a slot (s)", NUM),
      ("startup_wait_s", "Waiting for start-up (s)", NUM), ("queries", "Queries", INT), ("fix", "The fix")],
        how="Slot wait and start-up wait seconds per warehouse (own workspace + warehouse id), worst slot wait first."))
    p.checks()


def trend(d):
    p = d.page("trend", "Trend")
    p.kpis(
        kpi("Spill (GB)", """
WITH w AS (SELECT * FROM {named:perf_daily_by_resource} WHERE {ws} AND resource_type = 'warehouse')
SELECT ROUND(COALESCE(SUM(spilled_gb), 0), 0) AS value,
       concat('across ', CAST((SELECT count(DISTINCT warehouse_id) FROM w WHERE spilled_gb > 0) AS STRING), ' warehouses') AS target
FROM w
""", target="target", fmt=NUM, tone="warn",
            how="Daily local-disk spill GB for SQL warehouses, summed over the window."),
        kpi("Shuffle (GB)", """
WITH w AS (SELECT * FROM {named:perf_daily_by_resource} WHERE {ws} AND resource_type = 'warehouse')
SELECT ROUND(COALESCE(SUM(shuffle_gb), 0), 0) AS value,
       concat('across ', CAST((SELECT count(DISTINCT warehouse_id) FROM w WHERE shuffle_gb > 0) AS STRING), ' warehouses') AS target
FROM w
""", target="target", fmt=NUM, tone="warn",
            how="Daily shuffle-read GB for SQL warehouses, summed over the window."),
        kpi("Slot wait (s)", """
WITH w AS (SELECT * FROM {named:perf_daily_by_resource} WHERE {ws} AND resource_type = 'warehouse')
SELECT ROUND(COALESCE(SUM(slot_wait_s), 0), 0) AS value,
       COALESCE((SELECT concat('Worst: ', nm_warehouse) FROM (
           SELECT nm_warehouse, SUM(slot_wait_s) AS s FROM w GROUP BY 1 ORDER BY 2 DESC LIMIT 1
       )), 'None this window') AS target
FROM w
""", target="target", fmt=NUM, tone="warn",
            how="Daily seconds queries waited for a free warehouse slot, summed over the window."),
        kpi("Provisioning wait, by the clock (s)", """
WITH w AS (SELECT * FROM {named:perf_daily_by_resource} WHERE {ws} AND resource_type = 'warehouse')
SELECT ROUND(COALESCE(SUM(provision_clock_s), 0), 0) AS value,
       concat(CAST(COALESCE(SUM(starts), 0) AS STRING), ' starts, ', CAST(COALESCE(SUM(failed_starts), 0) AS STRING),
              ' failed') AS target
FROM w
""", target="target", fmt=NUM,
            how="Wall-clock seconds spent starting warehouse compute, summed over the window, with start and failed-start counts."),
    )
    p.charts(
        line("Slot wait by day (s)", """
SELECT usage_date AS day, ROUND(SUM(slot_wait_s), 0) AS wait_s
FROM {named:perf_daily_by_resource}
WHERE {ws} AND resource_type = 'warehouse'
GROUP BY 1
""", "day", "wait_s", fmt=NUM, value_title="Seconds",
            how="Daily total seconds queries waited for a free warehouse slot."),
        line("Provisioning wait by day (s)", """
SELECT usage_date AS day, ROUND(SUM(provision_clock_s), 0) AS wait_s
FROM {named:perf_daily_by_resource}
WHERE {ws} AND resource_type = 'warehouse'
GROUP BY 1
""", "day", "wait_s", fmt=NUM, value_title="Seconds",
            how="Daily wall-clock seconds spent starting warehouse compute."),
    )
    p.charts(
        bar("Spill by warehouse (top)", """
SELECT COALESCE(lbl_warehouse, 'Other warehouses') AS warehouse, ROUND(SUM(spilled_gb), 1) AS gb
FROM {named:perf_daily_by_resource}
WHERE {ws} AND resource_type = 'warehouse' AND spilled_gb > 0
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "warehouse", "gb", fmt=NUM, category_title="Warehouse", value_title="GB",
            how="Local-disk spill GB per warehouse, top 8; rows with no warehouse id roll into 'Other warehouses'."),
        bar("Shuffle by warehouse (top)", """
SELECT COALESCE(lbl_warehouse, 'Other warehouses') AS warehouse, ROUND(SUM(shuffle_gb), 1) AS gb
FROM {named:perf_daily_by_resource}
WHERE {ws} AND resource_type = 'warehouse' AND shuffle_gb > 0
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "warehouse", "gb", fmt=NUM, category_title="Warehouse", value_title="GB",
            how="Shuffle-read GB per warehouse, top 8; rows with no warehouse id roll into 'Other warehouses'."),
    )
    p.table(table("SQL warehouse trend by day", """
SELECT date_format(usage_date, 'yyyy-MM-dd') AS day, ROUND(SUM(spilled_gb), 1) AS spill_gb,
       ROUND(SUM(shuffle_gb), 1) AS shuffle_gb, ROUND(SUM(slot_wait_s), 0) AS slot_wait_s,
       ROUND(SUM(provision_clock_s), 0) AS provision_wait_s, SUM(runs) AS queries, SUM(failed) AS failed
FROM {named:perf_daily_by_resource}
WHERE {ws} AND resource_type = 'warehouse'
GROUP BY 1
ORDER BY 1 DESC
LIMIT 60
""", [("day", "Day"), ("spill_gb", "Spilled (GB)", NUM), ("shuffle_gb", "Shuffled (GB)", NUM),
      ("slot_wait_s", "Slot wait (s)", NUM), ("provision_wait_s", "Provisioning wait (s)", NUM),
      ("queries", "Queries", INT), ("failed", "Failed", INT)],
        how="Daily totals for SQL warehouses: spill, shuffle, slot wait, provisioning wait, queries and failures."))
    p.checks(ids=[], refs=["perf_daily_by_resource"])
