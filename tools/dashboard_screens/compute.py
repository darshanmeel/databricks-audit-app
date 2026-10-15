"""Compute tab (web/src/tabs/tab_compute.tsx): Warehouses, Clusters & pools, Configuration."""
from tools.build_databricks_dashboards import autostop_cte
from tools.dashboard_kit import INT, MONEY, NUM, PCT, bar, kpi, table, ws_clause


def build(d):
    warehouses(d)
    clusters(d)
    config(d)


def warehouses(d):
    p = d.page("warehouses")
    p.kpis(
        kpi("Idle while running", """
WITH r AS (SELECT * FROM {named:compute_warehouse_idle_minutes} WHERE {ws} AND not_assessed_reason IS NULL)
SELECT ROUND(100.0 * SUM(counted_idle_minutes) / NULLIF(SUM(running_minutes), 0), 0) AS value,
       concat(CAST(ROUND(SUM(counted_idle_minutes) / 60.0) AS STRING), ' h idle of ',
              CAST(ROUND(SUM(running_minutes) / 60.0) AS STRING), ' h running') AS target
FROM r
""", target="target", fmt=PCT, tone="warn",
            how="Idle minutes while up (gaps of 60s or more) as a % of running minutes, summed over measured warehouses."),
        kpi("Idle cost, window", """
WITH r AS (SELECT * FROM {named:compute_warehouse_idle_minutes} WHERE {ws} AND status IN ('CRITICAL', 'WARN'))
SELECT ROUND(COALESCE((SELECT SUM(est_wasted_usd_list) FROM r), 0) * {disc}, 0) AS value,
       COALESCE((SELECT concat('Worst: ', nm_warehouse) FROM r ORDER BY est_wasted_usd_list DESC LIMIT 1),
                'None flagged') AS target
""", target="target", fmt=MONEY, tone="crit",
            how="$ of idle time for warehouses flagged Critical or Warn, at list price less your discount."),
        kpi("Waiting for auto-stop", """
WITH r AS (SELECT * FROM {named:compute_warehouse_idle_minutes} WHERE {ws} AND not_assessed_reason IS NULL)
SELECT ROUND(SUM(stop_tail_minutes) / 60.0, 0) AS value,
       concat(CAST(ROUND(100.0 * SUM(stop_tail_minutes) / NULLIF(SUM(counted_idle_minutes), 0)) AS STRING),
              '% of idle time') AS target
FROM r
""", target="target", fmt=NUM,
            how="Hours spent up after the last query, waiting for the auto-stop timer, as a share of idle time."),
        kpi("Warehouses flagged", """
WITH r AS (SELECT * FROM {named:compute_warehouse_idle_minutes} WHERE {ws} AND not_assessed_reason IS NULL)
SELECT (SELECT count(*) FROM r WHERE status IN ('CRITICAL', 'WARN')) AS value,
       concat('of ', CAST((SELECT count(*) FROM r) AS STRING), ' measured') AS target
""", target="target", tone="warn",
            how="Warehouses with Critical or Warn idle status, of all with a known running/idle split."),
    )
    p.charts(
        bar("Where each warehouse's running time went", """
WITH r AS (SELECT * FROM {named:compute_warehouse_idle_minutes} WHERE {ws} AND not_assessed_reason IS NULL),
top AS (SELECT workspace_id, warehouse_id, lbl_warehouse FROM r ORDER BY running_minutes DESC LIMIT 8)
SELECT tw.lbl_warehouse AS warehouse, 'Busy' AS segment, SUM(r.busy_minutes) / 60.0 AS hours
FROM r JOIN top tw ON tw.workspace_id = r.workspace_id AND tw.warehouse_id = r.warehouse_id GROUP BY 1, 2
UNION ALL
SELECT tw.lbl_warehouse, 'Idle over 1 min', SUM(r.counted_idle_minutes) / 60.0
FROM r JOIN top tw ON tw.workspace_id = r.workspace_id AND tw.warehouse_id = r.warehouse_id GROUP BY 1, 2
UNION ALL
SELECT tw.lbl_warehouse, 'Short pauses', SUM(GREATEST(r.idle_minutes - r.counted_idle_minutes, 0)) / 60.0
FROM r JOIN top tw ON tw.workspace_id = r.workspace_id AND tw.warehouse_id = r.warehouse_id GROUP BY 1, 2
""", "warehouse", "hours", series="segment", fmt=NUM, category_title="Warehouse", value_title="Hours",
            how="Top 8 warehouses by running time, split into busy, idle over 1 min, and short pauses under 1 min."),
        bar("Autoscale churn, by warehouse", """
SELECT lbl_warehouse AS warehouse, SUM(churn_events) AS events
FROM {named:compute_warehouse_autoscale_churn}
WHERE {ws} AND churn_events > 0
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "warehouse", "events", fmt=INT, category_title="Warehouse", value_title="Churn events",
            how="Scale-up plus scale-down events per warehouse in the window; only warehouses with at least one."),
    )
    p.charts(
        bar("Auto-stop saving at 1 min (serverless) or 10 min (pro, classic), by warehouse", autostop_cte("{S}") + f"""
SELECT concat(resource_name, ' · ', workspace_name) AS warehouse, ROUND(MAX(saving_usd), 0) AS usd
FROM saving
WHERE saving_usd >= 1 AND {ws_clause('workspace_name')}
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "warehouse", "usd", fmt=MONEY, category_title="Warehouse", value_title="Saving",
            how="Saving if auto-stop were the lowest Databricks allows: 1 min serverless, 10 min pro/classic; priced per cluster-minute."),
        bar("Late auto-stop, extra wait by warehouse", """
SELECT lbl_warehouse AS warehouse, ROUND(max(late_extra_minutes), 0) AS minutes
FROM {named:compute_warehouse_late_autostop}
WHERE {ws} AND status = 'WARN'
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "warehouse", "minutes", fmt=NUM, category_title="Warehouse", value_title="Extra minutes",
            how="Extra minutes a warehouse stayed up past its own auto-stop timer, for warehouses flagged Warn on it."),
    )
    p.charts(
        bar("Cache reuse, by warehouse (lowest first)", """
SELECT lbl_warehouse AS warehouse, ROUND(max(from_result_cache_pct), 0) AS pct
FROM {named:compute_warehouse_cache_reuse}
WHERE {ws} AND status = 'WARN'
GROUP BY 1 ORDER BY 2 ASC LIMIT 8
""", "warehouse", "pct", fmt=PCT, category_title="Warehouse", value_title="Result-cache reuse",
            how="Share of SELECTs served from the result cache; the 8 lowest among warehouses flagged Warn on it."),
        bar("Starts, by warehouse", """
SELECT lbl_warehouse AS warehouse, SUM(starts) AS starts
FROM {named:perf_daily_by_resource}
WHERE {ws} AND resource_type = 'warehouse' AND NOT is_other
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "warehouse", "starts", fmt=INT, category_title="Warehouse", value_title="Starts",
            how="Warehouse starts in the window; folded 'other warehouses' rows excluded so each bar is one warehouse."),
    )
    p.chart(
        bar("Warehouses by auto-stop setting", """
WITH r AS (
  SELECT *,
         CASE WHEN auto_stop_minutes IS NULL THEN '6 Unknown'
              WHEN auto_stop_minutes = 0 THEN '5 Off'
              WHEN auto_stop_minutes <= 5 THEN '1 Up to 5 min'
              WHEN auto_stop_minutes <= 15 THEN '2 6-15 min'
              WHEN auto_stop_minutes <= 30 THEN '3 16-30 min'
              ELSE '4 Over 30 min' END AS band
  FROM {named:compute_warehouse_idle_minutes}
  WHERE {ws} AND not_assessed_reason IS NULL
)
SELECT band, 'Idle' AS segment, ROUND(SUM(est_wasted_usd_list) * {disc}, 0) AS usd FROM r GROUP BY 1, 2
UNION ALL
SELECT band, 'Busy' AS segment, ROUND(SUM(GREATEST(est_usd_list - est_wasted_usd_list, 0)) * {disc}, 0) AS usd FROM r GROUP BY 1, 2
""", "band", "usd", series="segment", fmt=MONEY, category_title="Auto-stop setting", value_title="$", sort="category",
            how="Idle vs busy $ by current auto-stop setting, bands from 5 min up to off; list price less your discount.")
    )
    p.table(table("Idle time by warehouse", """
SELECT n_warehouse AS warehouse, CASE WHEN auto_stop_minutes = 0 THEN 'off'
            WHEN auto_stop_minutes IS NULL THEN '-' ELSE CAST(auto_stop_minutes AS STRING) END AS auto_stop,
       ROUND(idle_share_pct, 0) AS idle_share_pct, ROUND(est_wasted_usd_list * {disc}, 0) AS idle_usd, status,
       ROUND(start_gap_minutes, 0) AS start_gap_min, ROUND(between_queries_minutes, 0) AS between_queries_min,
       ROUND(stop_tail_minutes, 0) AS stop_tail_min, ROUND(no_query_minutes, 0) AS no_query_min
FROM {named:compute_warehouse_idle_minutes}
WHERE {ws} AND not_assessed_reason IS NULL
ORDER BY est_wasted_usd_list DESC NULLS LAST
LIMIT 50
""", [("warehouse", "Warehouse"), ("auto_stop", "Auto-stop"), ("idle_share_pct", "Idle share %", PCT),
      ("idle_usd", "Idle $", MONEY), ("status", "Status"), ("start_gap_min", "Start gap, min", NUM),
      ("between_queries_min", "Between queries, min", NUM), ("stop_tail_min", "Waiting to stop, min", NUM),
      ("no_query_min", "No query at all, min", NUM)], height=10,
        how="Every measured warehouse, worst idle $ first; the last four columns break the idle time down."))
    p.table(table("Starts and stops, by warehouse", """
WITH starts AS (
  SELECT workspace_id, warehouse_id, lbl_warehouse, SUM(starts) AS starts, SUM(start_s) AS start_s,
         SUM(failed_starts) AS failed_starts
  FROM {named:perf_daily_by_resource}
  WHERE {ws} AND resource_type = 'warehouse' AND NOT is_other
  GROUP BY 1, 2, 3
),
stops AS (
  SELECT workspace_id, warehouse_id, SUM(CASE WHEN event_type = 'STOPPED' THEN event_count ELSE 0 END) AS stops,
         SUM(CASE WHEN event_type = 'SCALED_UP' THEN event_count ELSE 0 END) AS scale_ups
  FROM {S}.sql_warehouse_events_activity
  GROUP BY 1, 2
),
idle AS (
  SELECT workspace_id, warehouse_id, running_minutes, counted_idle_minutes, auto_stop_minutes
  FROM {S}.compute_warehouse_idle_minutes
  WHERE not_assessed_reason IS NULL
)
SELECT s.lbl_warehouse AS warehouse, s.starts, ROUND(s.start_s / 60.0, 1) AS start_minutes, s.failed_starts,
       COALESCE(p.stops, 0) AS stops, COALESCE(p.scale_ups, 0) AS scale_ups,
       ROUND(i.running_minutes / 60.0, 0) AS running_hours,
       ROUND(100.0 * i.counted_idle_minutes / NULLIF(i.running_minutes, 0), 0) AS idle_pct
FROM starts s
LEFT JOIN stops p ON p.workspace_id = s.workspace_id AND p.warehouse_id = s.warehouse_id
LEFT JOIN idle i ON i.workspace_id = s.workspace_id AND i.warehouse_id = s.warehouse_id
ORDER BY s.starts DESC
LIMIT 20
""", [("warehouse", "Warehouse"), ("starts", "Starts", INT), ("start_minutes", "Starting, min", NUM),
      ("failed_starts", "Failed to start", INT), ("stops", "Stops", INT), ("scale_ups", "Scale-ups", INT),
      ("running_hours", "Running, h", NUM), ("idle_pct", "Idle %", PCT)],
        how="Starts, stops and scale-ups per warehouse in the window; folded 'other warehouses' rows excluded."))
    p.table(table("Auto-stop saving and extra cold starts, by warehouse", autostop_cte("{S}") + f"""
SELECT concat(resource_name, ' · ', workspace_name) AS warehouse, warehouse_kind,
       CASE WHEN auto_stop_minutes = 0 OR auto_stop_minutes IS NULL THEN 'off' ELSE CAST(auto_stop_minutes AS STRING) END
            AS current_autostop,
       target_minutes, ROUND(COALESCE(saving_usd, 0), 0) AS saving_usd, extra_cold_starts
FROM saving
WHERE {ws_clause('workspace_name')}
ORDER BY saving_usd DESC NULLS LAST
LIMIT 50
""", [("warehouse", "Warehouse"), ("warehouse_kind", "Kind"), ("current_autostop", "Current auto-stop, min"),
      ("target_minutes", "Target, min", INT), ("saving_usd", "Possible saving", MONEY),
      ("extra_cold_starts", "Extra cold starts", INT)],
        how="Saving and extra cold starts if auto-stop were 1 min (serverless) or 10 min (pro, classic); worst saving first."))
    p.checks()


def clusters(d):
    p = d.page("clusters")
    p.kpis(
        kpi("Idle clusters (possible waste)", """
WITH r AS (SELECT * FROM {named:compute_idle_node_ratio} WHERE {ws})
SELECT ROUND(COALESCE((SELECT SUM(est_wasted_usd_list) FROM r WHERE status IN ('CRITICAL', 'WARN')), 0) * {disc}, 0) AS value,
       concat((SELECT CAST(count(*) AS STRING) FROM r WHERE status IN ('CRITICAL', 'WARN')), ' of ',
              (SELECT CAST(count(*) AS STRING) FROM r), ' flagged') AS target
""", target="target", fmt=MONEY, tone="crit",
            how="$ of est_wasted_usd_list for clusters flagged Critical or Warn on idle-node ratio, less discount."),
        kpi("Worst idle-node ratio", """
WITH i AS (SELECT * FROM {named:compute_idle_node_ratio} WHERE {ws}),
cfg AS (SELECT cluster_id, cluster_source FROM {S}.classic_clusters_config_current)
SELECT ROUND(COALESCE(MAX(i.idle_ratio), 0) * 100, 0) AS value,
       COALESCE(MAX_BY(i.nm_cluster, i.idle_ratio), 'None measured') AS target
FROM i LEFT JOIN cfg ON cfg.cluster_id = i.cluster_id
WHERE COALESCE(cfg.cluster_source, '') <> 'JOB'
""", target="target", fmt=PCT,
            how="Highest idle worker-node share among clusters that are not job-run clusters, matching the bar below."),
        kpi("Pool idle capacity", """
SELECT COALESCE((SELECT SUM(min_idle_instances) FROM {named:instance_pools_idle_capacity} WHERE {ws}), 0) AS value,
       COALESCE((SELECT concat('Worst: ', COALESCE(instance_pool_name, instance_pool_id)) FROM {named:instance_pools_idle_capacity}
                 WHERE {ws} ORDER BY min_idle_instances DESC LIMIT 1), 'No pools in this snapshot') AS target
""", target="target",
            how="Instances kept warm and idle across pools (min_idle_instances), even with nothing running."),
        kpi("No auto-terminate", """
WITH c AS (SELECT * FROM {named:classic_clusters_config_current} WHERE {ws} AND cluster_source IN ('UI', 'API'))
SELECT (SELECT count(*) FROM c WHERE auto_termination_minutes IS NULL OR auto_termination_minutes = 0) AS value,
       concat('of ', CAST((SELECT count(*) FROM c) AS STRING), ' interactive clusters') AS target
""", target="target", tone="warn",
            how="Interactive (UI, API) clusters with auto-termination off or unset, of all interactive clusters."),
    )
    p.charts(
        bar("Worst idle-node ratio, by cluster", """
WITH idle AS (SELECT * FROM {named:compute_idle_node_ratio}),
cfg AS (SELECT cluster_id, cluster_source FROM {S}.classic_clusters_config_current)
SELECT idle.lbl_cluster AS cluster, ROUND(MAX(idle.idle_ratio) * 100, 0) AS idle_pct
FROM idle LEFT JOIN cfg ON cfg.cluster_id = idle.cluster_id
WHERE {ws} AND COALESCE(cfg.cluster_source, '') <> 'JOB'
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "cluster", "idle_pct", fmt=PCT, category_title="Cluster", value_title="Idle nodes %",
            how="Top 8 clusters by idle worker-node share over the window; job-run clusters are excluded."),
        bar("Cluster count by worker node type", """
SELECT COALESCE(worker_node_type, '(single-node / unknown)') AS node_type, count(*) AS clusters
FROM {named:classic_clusters_config_current}
WHERE {ws}
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "node_type", "clusters", fmt=INT, category_title="Worker node type", value_title="Clusters",
            how="Clusters grouped by worker node type; single-node or unset types grouped into one bar."),
    )
    p.charts(
        bar("All-purpose clusters by auto-termination", """
WITH c AS (SELECT * FROM {named:classic_clusters_config_current} WHERE {ws} AND cluster_source IN ('UI', 'API'))
SELECT CASE WHEN auto_termination_minutes IS NULL OR auto_termination_minutes = 0 THEN '6 Off'
            WHEN auto_termination_minutes <= 15 THEN '1 Up to 15 min' WHEN auto_termination_minutes <= 30 THEN '2 16-30 min'
            WHEN auto_termination_minutes <= 60 THEN '3 31-60 min' WHEN auto_termination_minutes <= 120 THEN '4 61-120 min'
            ELSE '5 Over 120 min' END AS band,
       count(*) AS clusters
FROM c
GROUP BY 1
""", "band", "clusters", fmt=INT, category_title="Auto-termination", value_title="Clusters", sort="category",
            how="Interactive (UI, API) clusters by auto-termination setting; 'Off' means unset or 0."),
        bar("Pools by idle capacity", """
SELECT COALESCE(instance_pool_name, instance_pool_id) AS pool, SUM(min_idle_instances) AS idle_instances
FROM {named:instance_pools_idle_capacity}
WHERE {ws}
GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""", "pool", "idle_instances", fmt=INT, category_title="Pool", value_title="Idle instances",
            how="Instance pools by minimum idle instances kept warm, worst 8."),
    )
    p.table(table("All-purpose clusters that ran, auto-termination over 30 min", """
WITH cfg AS (SELECT cluster_id, auto_termination_minutes FROM {S}.classic_clusters_config_current WHERE cluster_source IN ('UI', 'API')),
idle AS (SELECT * FROM {named:compute_idle_node_ratio} WHERE {ws})
SELECT idle.nm_cluster AS cluster, idle.ws_name AS workspace,
       CASE WHEN cfg.auto_termination_minutes = 0 THEN 'off' ELSE CAST(cfg.auto_termination_minutes AS STRING) END AS auto_term,
       ROUND(idle.idle_ratio * 100, 0) AS idle_pct, ROUND(idle.est_wasted_usd_list * {disc}, 0) AS idle_usd,
       ROUND(idle.est_usd_list * {disc}, 0) AS total_usd
FROM idle JOIN cfg ON cfg.cluster_id = idle.cluster_id
WHERE cfg.auto_termination_minutes IS NULL OR cfg.auto_termination_minutes = 0 OR cfg.auto_termination_minutes > 30
ORDER BY idle.est_wasted_usd_list DESC NULLS LAST
LIMIT 50
""", [("cluster", "Cluster"), ("workspace", "Workspace"), ("auto_term", "Auto-termination, min"),
      ("idle_pct", "Idle nodes %", PCT), ("idle_usd", "Idle $", MONEY), ("total_usd", "Total $", MONEY)],
        how="Interactive clusters with auto-termination off, unset or over 30 min, worst idle $ first."))
    p.checks()


def config(d):
    p = d.page("config")
    p.kpis(
        kpi("Clusters flagged", """
SELECT count_if(status IN ('CRITICAL', 'WARN')) AS value, concat('of ', CAST(count(*) AS STRING), ' clusters') AS target
FROM {named:compute_cluster_config_posture} WHERE {ws}
""", target="target", tone="warn",
            how="Clusters with a Critical or Warn setup flag: no isolation, EOL runtime, no policy, or auto-term risk."),
        kpi("Warehouses flagged", """
SELECT count_if(status = 'WARN') AS value, concat('of ', CAST(count(*) AS STRING), ' warehouses') AS target
FROM {named:compute_warehouse_config_posture} WHERE {ws}
""", target="target", tone="warn",
            how="Warehouses with a Warn setup flag: auto-stop risk, classic type, or preview channel."),
    )
    p.charts(
        bar("Setup risks, by flag", """
WITH c AS (
  SELECT cc.*, COALESCE(dw1.name, cc.workspace_id, 'Account-level') AS ws_name
  FROM {S}.compute_cluster_config_posture cc
  LEFT JOIN {S}.dim_workspace dw1 ON dw1.workspace_id = cc.workspace_id
),
cf AS (SELECT * FROM c WHERE status IN ('CRITICAL', 'WARN') AND """ + ws_clause("ws_name") + """),
w AS (
  SELECT wc.*, COALESCE(dw2.name, wc.workspace_id, 'Account-level') AS ws_name
  FROM {S}.compute_warehouse_config_posture wc
  LEFT JOIN {S}.dim_workspace dw2 ON dw2.workspace_id = wc.workspace_id
),
wf AS (SELECT * FROM w WHERE status = 'WARN' AND """ + ws_clause("ws_name") + """)
SELECT 'No user isolation' AS flag, 'Cluster' AS kind, count(*) AS n FROM cf WHERE flag_no_isolation
UNION ALL SELECT 'End-of-support runtime', 'Cluster', count(*) FROM cf WHERE flag_eol_runtime
UNION ALL SELECT 'No policy', 'Cluster', count(*) FROM cf WHERE flag_no_policy
UNION ALL SELECT 'Auto-term off/too high', 'Cluster', count(*) FROM cf WHERE flag_auto_term_risk
UNION ALL SELECT 'Auto-stop off/too high', 'Warehouse', count(*) FROM wf WHERE flag_autostop_risk
UNION ALL SELECT 'Classic type', 'Warehouse', count(*) FROM wf WHERE flag_classic_type
UNION ALL SELECT 'Preview channel', 'Warehouse', count(*) FROM wf WHERE flag_preview_channel
""", "flag", "n", series="kind", fmt=INT, category_title="Flag", value_title="Flagged",
            how="Clusters and warehouses flagged per setup risk; clusters need Critical or Warn, warehouses need Warn."),
    )
    p.table(table("Cluster setup risks", """
SELECT status, n_cluster AS cluster, reasons
FROM {named:compute_cluster_config_posture}
WHERE {ws} AND status IN ('CRITICAL', 'WARN')
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 ELSE 1 END, n_cluster
LIMIT 50
""", [("status", "Status"), ("cluster", "Cluster"), ("reasons", "Reasons")],
        how="Flagged clusters (Critical or Warn) with every reason listed, worst status first."))
    p.table(table("Warehouse setup risks", """
SELECT status, n_warehouse AS warehouse, reasons
FROM {named:compute_warehouse_config_posture}
WHERE {ws} AND status = 'WARN'
ORDER BY n_warehouse
LIMIT 50
""", [("status", "Status"), ("warehouse", "Warehouse"), ("reasons", "Reasons")],
        how="Warehouses flagged Warn, with every reason listed."))
    p.checks()
