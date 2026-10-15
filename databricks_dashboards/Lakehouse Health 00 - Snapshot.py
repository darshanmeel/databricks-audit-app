# Databricks notebook source
# MAGIC %md
# MAGIC # Lakehouse Health: snapshot
# MAGIC
# MAGIC Runs every check once and saves the results in `<catalog>.databricks_audit_dashboard`; the dashboards in this
# MAGIC folder read only those tables.
# MAGIC
# MAGIC - Set **catalog** in the widgets above; change the tag widgets only if your keys differ.
# MAGIC - **Run all**. A check that fails (usually a missing grant) leaves an empty table and its reason in
# MAGIC   `snapshot_log`; the rest still run.
# MAGIC - Rerun for new numbers.

# COMMAND ----------

dbutils.widgets.text("catalog", "", "Catalog")
dbutils.widgets.text("tag_keys", "cost_center,domain,environment", "Tags every resource should carry")
dbutils.widgets.text("chargeback_keys", "cost_center,domain", "Tags that say who pays, first match wins")
dbutils.widgets.text("query_tags", "cost_center=lakehouse_health,domain=lakehouse_health", "Tags on this notebook's own queries")

# COMMAND ----------

# names for the ids the checks return (app/direct_sql/names)
DIMS = {}
DIMS["dim_workspace"] = r"""
-- hand-written; not generated. One row per workspace id seen in workspaces_latest, 365 days of
-- billing.usage or the regional tables below; env and tags are added by the loader. One query:
-- every table it reads is a prerequisite, so a missing grant fails it and names fall back to ids.
WITH ids_workspaces_latest AS (
    SELECT DISTINCT workspace_id
    FROM system.access.workspaces_latest
    WHERE workspace_id IS NOT NULL
),
ids_usage AS (
    SELECT DISTINCT workspace_id
    FROM system.billing.usage
    WHERE workspace_id IS NOT NULL
      AND usage_date >= current_date() - INTERVAL 365 DAYS
),
-- regional presence: full SCD/reference tables, plus the two busiest event tables capped to the
-- last 30 days so this never scans a full audit or query history.
regional_ids AS (
    SELECT workspace_id FROM system.compute.clusters WHERE workspace_id IS NOT NULL
    UNION
    SELECT workspace_id FROM system.compute.warehouses WHERE workspace_id IS NOT NULL
    UNION
    SELECT workspace_id FROM system.compute.instance_pools WHERE workspace_id IS NOT NULL
    UNION
    SELECT workspace_id FROM system.lakeflow.jobs WHERE workspace_id IS NOT NULL
    UNION
    SELECT workspace_id FROM system.lakeflow.pipelines WHERE workspace_id IS NOT NULL
    UNION
    SELECT workspace_id FROM system.serving.served_entities WHERE workspace_id IS NOT NULL
    UNION
    SELECT workspace_id FROM system.lakeflow.job_run_timeline
    WHERE workspace_id IS NOT NULL AND period_start_time >= current_date() - INTERVAL 30 DAYS
    UNION
    SELECT workspace_id FROM system.query.history
    WHERE workspace_id IS NOT NULL AND start_time >= current_date() - INTERVAL 30 DAYS
),
all_ids AS (
    SELECT workspace_id FROM ids_workspaces_latest
    UNION
    SELECT workspace_id FROM ids_usage
    UNION
    SELECT workspace_id FROM regional_ids
),
latest_ws AS (
    SELECT workspace_id, workspace_name, workspace_url
    FROM (
        SELECT
            workspace_id,
            workspace_name,
            workspace_url,
            ROW_NUMBER() OVER (PARTITION BY workspace_id ORDER BY create_time DESC) AS rn
        FROM system.access.workspaces_latest
    ) w
    WHERE rn = 1
)
SELECT
    all_ids.workspace_id,
    latest_ws.workspace_name AS name,
    latest_ws.workspace_url AS url,
    CASE
        WHEN (SELECT COUNT(*) FROM regional_ids) = 0 THEN CAST(NULL AS BOOLEAN)
        WHEN r.workspace_id IS NOT NULL THEN TRUE
        ELSE FALSE
    END AS in_snapshot_region,
    (u.workspace_id IS NOT NULL) AS billed_in_snapshot
FROM all_ids
LEFT JOIN latest_ws ON latest_ws.workspace_id = all_ids.workspace_id
LEFT JOIN regional_ids r ON r.workspace_id = all_ids.workspace_id
LEFT JOIN ids_usage u ON u.workspace_id = all_ids.workspace_id
ORDER BY all_ids.workspace_id
"""
DIMS["dim_warehouse"] = r"""
-- hand-written; not generated. Latest row per warehouse_id (a globally-unique GUID), same rule
-- as dbt/models/dims/dim_warehouse.sql. Reads system.* directly, so it does not run against the
-- DuckDB fixtures (system is a reserved catalog there), unlike the dbt model it mirrors.
WITH ranked AS (
    SELECT
        workspace_id,
        warehouse_id,
        warehouse_name,
        created_by,
        delete_time,
        ROW_NUMBER() OVER (
            PARTITION BY warehouse_id
            ORDER BY change_time DESC
        ) AS rn
    FROM system.compute.warehouses
    WHERE warehouse_id IS NOT NULL
)
SELECT workspace_id, warehouse_id, warehouse_name, created_by, delete_time
FROM ranked
WHERE rn = 1
"""
DIMS["dim_job"] = r"""
-- hand-written; not generated. Latest row per (workspace_id, job_id), same rule as
-- dbt/models/dims/dim_job.sql. Reads system.* directly, so it does not run against the DuckDB
-- fixtures (system is a reserved catalog there), unlike the dbt model it mirrors.
WITH ranked AS (
    SELECT
        workspace_id,
        job_id,
        name,
        run_as,
        run_as_user_name,
        creator_user_name,
        delete_time,
        ROW_NUMBER() OVER (
            PARTITION BY workspace_id, job_id
            ORDER BY change_time DESC
        ) AS rn
    FROM system.lakeflow.jobs
    WHERE workspace_id IS NOT NULL AND job_id IS NOT NULL
)
SELECT workspace_id, job_id, name, run_as, run_as_user_name, creator_user_name, delete_time
FROM ranked
WHERE rn = 1
"""
DIMS["dim_cluster"] = r"""
-- hand-written; not generated. Latest row per cluster billed or changed in the export window.
-- Job and pipeline clusters are skipped: one per run, auto-named; the app names them by their job
-- or pipeline instead.
WITH billed AS (
    SELECT DISTINCT usage_metadata.cluster_id AS cluster_id
    FROM system.billing.usage
    WHERE usage_metadata.cluster_id IS NOT NULL
      AND usage_date >= current_date() - INTERVAL 30 DAYS
),
ranked AS (
    SELECT
        workspace_id,
        cluster_id,
        cluster_name,
        cluster_source,
        owned_by,
        change_time,
        delete_time,
        ROW_NUMBER() OVER (
            PARTITION BY cluster_id
            ORDER BY change_time DESC
        ) AS rn
    FROM system.compute.clusters
    WHERE cluster_id IS NOT NULL
      AND COALESCE(cluster_source, '') NOT IN ('JOB', 'PIPELINE', 'PIPELINE_MAINTENANCE')
)
SELECT workspace_id, cluster_id, cluster_name, cluster_source, owned_by, delete_time
FROM ranked
WHERE rn = 1
  AND (
      cluster_id IN (SELECT cluster_id FROM billed)
      OR change_time >= current_date() - INTERVAL 30 DAYS
      OR delete_time IS NULL  -- every live cluster, so the Tags page sees unused ones too
  )
"""
DIMS["dim_pipeline"] = r"""
-- hand-written; not generated. Latest row per (workspace_id, pipeline_id), same rule as
-- dbt/models/dims/dim_pipeline.sql. Reads system.* directly, so it does not run against the
-- DuckDB fixtures (system is a reserved catalog there), unlike the dbt model it mirrors.
WITH ranked AS (
    SELECT
        workspace_id,
        pipeline_id,
        name AS pipeline_name,
        created_by,
        run_as,
        ROW_NUMBER() OVER (
            PARTITION BY workspace_id, pipeline_id
            ORDER BY change_time DESC
        ) AS rn
    FROM system.lakeflow.pipelines
    WHERE workspace_id IS NOT NULL AND pipeline_id IS NOT NULL
)
SELECT workspace_id, pipeline_id, pipeline_name, created_by, run_as
FROM ranked
WHERE rn = 1
"""
DIMS["dim_notebook"] = r"""
-- hand-written; not generated. Notebooks have no system table of their own, so the latest path
-- per (workspace_id, notebook_id) comes from the billing rows that carry it, over the export window.
WITH ranked AS (
    SELECT
        workspace_id,
        usage_metadata.notebook_id   AS notebook_id,
        usage_metadata.notebook_path AS notebook_path,
        ROW_NUMBER() OVER (
            PARTITION BY workspace_id, usage_metadata.notebook_id
            ORDER BY usage_end_time DESC
        ) AS rn
    FROM system.billing.usage
    WHERE usage_metadata.notebook_id IS NOT NULL
      AND usage_metadata.notebook_path IS NOT NULL
      AND usage_date >= current_date() - INTERVAL 30 DAYS
)
SELECT workspace_id, notebook_id, notebook_path
FROM ranked
WHERE rn = 1
"""


# COMMAND ----------

# every check, unchanged from the app (app/direct_sql)
CHECKS = {}
CHECKS["compute_warehouse_idle_minutes"] = r"""
-- generated from dbt/models/databricks_direct/compute/d_compute_warehouse_idle_minutes.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/compute/compute_warehouse_idle_minutes.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH bounds AS (
  -- the window as epoch seconds: [first day 00:00, today 00:00)
  SELECT unix_timestamp(CAST(current_date() - INTERVAL 30 DAYS AS TIMESTAMP)) AS win_start_s,
         unix_timestamp(CAST(current_date() AS TIMESTAMP))                            AS win_end_s
),
ev_all AS (
  -- every documented event before today; state_rank orders events that share a timestamp
  SELECT e.workspace_id, e.warehouse_id, e.event_type, e.cluster_count,
         unix_timestamp(e.event_time) AS t,
         CASE e.event_type WHEN 'STARTING' THEN 1 WHEN 'RUNNING' THEN 2
                           WHEN 'SCALED_UP' THEN 3 WHEN 'SCALED_DOWN' THEN 3
                           WHEN 'STOPPING' THEN 4 ELSE 5 END AS state_rank
  FROM `system`.`compute`.`warehouse_events` e
  WHERE e.event_time < current_date()
    AND e.event_type IN ('STARTING', 'RUNNING', 'SCALED_UP', 'SCALED_DOWN', 'STOPPING', 'STOPPED')
),
prior AS (
  -- the last event before the window: the state the warehouse was in when the window opened
  SELECT a.workspace_id, a.warehouse_id, a.event_type, a.cluster_count
  FROM ev_all a CROSS JOIN bounds b
  WHERE a.t < b.win_start_s
  QUALIFY ROW_NUMBER() OVER (PARTITION BY a.workspace_id, a.warehouse_id
                             ORDER BY a.t DESC, a.state_rank DESC) = 1
),
ev AS (
  SELECT x.*,
         CASE WHEN x.event_type IN ('STARTING', 'RUNNING', 'SCALED_UP')
                OR (x.event_type = 'SCALED_DOWN' AND COALESCE(x.cluster_count, 1) > 0)
              THEN 1 ELSE 0 END AS is_up,
         CASE WHEN x.event_type IN ('RUNNING', 'SCALED_UP')
                OR (x.event_type = 'SCALED_DOWN' AND COALESCE(x.cluster_count, 1) > 0)
              THEN 1 ELSE 0 END AS is_running,
         GREATEST(COALESCE(x.cluster_count, 1), 1) AS clusters
  FROM (
    SELECT a.workspace_id, a.warehouse_id, a.event_type, a.cluster_count, a.t, a.state_rank,
           0 AS carried
    FROM ev_all a CROSS JOIN bounds b
    WHERE a.t >= b.win_start_s
    UNION ALL
    SELECT p.workspace_id, p.warehouse_id, p.event_type, p.cluster_count, b.win_start_s AS t,
           0 AS state_rank, 1 AS carried
    FROM prior p CROSS JOIN bounds b
  ) x
),
seg AS (
  -- one row per event: the state it holds until the next event (or the window's end)
  SELECT v.*,
         LEAD(v.t) OVER (PARTITION BY v.workspace_id, v.warehouse_id ORDER BY v.t, v.state_rank) AS next_t,
         COALESCE(LEAD(v.t) OVER (PARTITION BY v.workspace_id, v.warehouse_id ORDER BY v.t, v.state_rank),
                  b.win_end_s) AS seg_end_s,
         COALESCE(LAG(v.is_running) OVER (PARTITION BY v.workspace_id, v.warehouse_id
                                          ORDER BY v.t, v.state_rank), 0) AS prev_running,
         ROW_NUMBER() OVER (PARTITION BY v.workspace_id, v.warehouse_id ORDER BY v.t, v.state_rank) AS seq
  FROM ev v CROSS JOIN bounds b
),
seg_runs AS (
  SELECT s.*,
         SUM(CASE WHEN s.is_running = 1 AND s.prev_running = 0 THEN 1 ELSE 0 END)
           OVER (PARTITION BY s.workspace_id, s.warehouse_id ORDER BY s.t, s.state_rank
                 ROWS UNBOUNDED PRECEDING) AS run_no
  FROM seg s
),
running AS (
  -- contiguous running periods
  SELECT workspace_id, warehouse_id, run_no,
         MIN(t) AS run_start_s, MAX(seg_end_s) AS run_end_s
  FROM seg_runs
  WHERE is_running = 1
  GROUP BY workspace_id, warehouse_id, run_no
),
wh_time AS (
  SELECT workspace_id, warehouse_id,
         MIN(t)                                                        AS measured_from_s,
         MAX(CASE WHEN seq = 1 AND (carried = 1 OR event_type = 'STARTING') THEN 1 ELSE 0 END) AS start_state_known,
         SUM(is_up * (seg_end_s - t))                                  AS up_s,
         SUM(is_running * (seg_end_s - t))                             AS running_s,
         SUM(is_up * (seg_end_s - t) * clusters)                       AS up_cluster_s,
         MAX(CASE WHEN is_up = 1 THEN clusters END)                    AS max_clusters_seen,
         MAX(CASE WHEN is_running = 1 AND next_t IS NULL THEN 1 ELSE 0 END) AS open_at_window_end
  FROM seg
  GROUP BY workspace_id, warehouse_id
),
running_periods AS (
  SELECT workspace_id, warehouse_id, COUNT(*) AS running_periods FROM running
  GROUP BY workspace_id, warehouse_id
),
qh AS (
  -- any statement history at all in the window? (none = the source is missing, not "all idle")
  SELECT COUNT(*) AS qh_rows
  FROM `system`.`query`.`history`
  WHERE start_time >= current_date() - INTERVAL 30 DAYS
    AND start_time < current_date()
),
stmt AS (
  SELECT q.workspace_id, q.compute.warehouse_id AS warehouse_id,
         GREATEST(unix_timestamp(q.start_time), b.win_start_s) AS s,
         -- busy until the client has fetched the result, which comes after end_time
         LEAST(COALESCE(unix_timestamp(q.end_time) + CAST(FLOOR(COALESCE(q.result_fetch_duration_ms, 0) / 1000) AS BIGINT),
                        unix_timestamp(q.update_time), unix_timestamp(q.start_time)), b.win_end_s) AS e
  FROM `system`.`query`.`history` q CROSS JOIN bounds b
  WHERE q.compute.warehouse_id IS NOT NULL
    AND q.start_time >= current_date() - INTERVAL 30 DAYS - INTERVAL 48 HOURS
    AND q.start_time < current_date()
),
stmt_ord AS (
  SELECT workspace_id, warehouse_id, s, e,
         MAX(e) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY s, e
                      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_max_e
  FROM stmt
  WHERE e >= s
),
stmt_isl AS (
  SELECT workspace_id, warehouse_id, s, e,
         SUM(CASE WHEN prev_max_e IS NULL OR s > prev_max_e THEN 1 ELSE 0 END)
           OVER (PARTITION BY workspace_id, warehouse_id ORDER BY s, e ROWS UNBOUNDED PRECEDING) AS island_no
  FROM stmt_ord
),
busy AS (
  SELECT workspace_id, warehouse_id, island_no, MIN(s) AS bs, MAX(e) AS be
  FROM stmt_isl
  GROUP BY workspace_id, warehouse_id, island_no
),
pb AS (
  -- busy islands clipped to each running period they touch
  SELECT r.workspace_id, r.warehouse_id, r.run_no,
         GREATEST(x.bs, r.run_start_s) AS bs, LEAST(x.be, r.run_end_s) AS be
  FROM running r
  JOIN busy x
    ON  x.workspace_id = r.workspace_id AND x.warehouse_id = r.warehouse_id
    AND x.bs <= r.run_end_s AND x.be >= r.run_start_s
),
qgap AS (
  -- time from one busy stretch's end to the next one's start: what an auto-stop setting acts on;
  -- the last stretch's gap runs to the end of its running period, so the final tail is replayed too
  SELECT p.workspace_id, p.warehouse_id,
         COALESCE(LEAD(p.bs) OVER (PARTITION BY p.workspace_id, p.warehouse_id ORDER BY p.bs), r.run_end_s) - p.be AS g,
         -- the final tail has no next query, so stopping after it costs no cold start
         LEAD(p.bs) OVER (PARTITION BY p.workspace_id, p.warehouse_id ORDER BY p.bs) IS NULL AS is_tail
  FROM pb p
  JOIN running r
    ON r.workspace_id = p.workspace_id AND r.warehouse_id = p.warehouse_id AND r.run_no = p.run_no
),
busy_agg AS (
  SELECT workspace_id, warehouse_id, SUM(be - bs) AS busy_s FROM pb GROUP BY workspace_id, warehouse_id
),
marks AS (
  SELECT workspace_id, warehouse_id, run_no, run_start_s AS bs, run_start_s AS be, 0 AS kind FROM running
  UNION ALL
  SELECT workspace_id, warehouse_id, run_no, bs, be, 1 AS kind FROM pb
  UNION ALL
  SELECT workspace_id, warehouse_id, run_no, run_end_s AS bs, run_end_s AS be, 2 AS kind FROM running
),
gaps AS (
  SELECT workspace_id, warehouse_id, run_no, kind AS opened_by, be AS gap_start_s,
         LEAD(bs)   OVER (PARTITION BY workspace_id, warehouse_id, run_no ORDER BY bs, kind, be) AS gap_end_s,
         LEAD(kind) OVER (PARTITION BY workspace_id, warehouse_id, run_no ORDER BY bs, kind, be) AS closed_by
  FROM marks
),
idle AS (
  SELECT g.workspace_id, g.warehouse_id, g.gap_start_s, g.gap_end_s,
         g.gap_end_s - g.gap_start_s AS gap_s,
         CASE WHEN g.opened_by = 0 AND g.closed_by = 2 THEN 'no_query'
              WHEN g.opened_by = 0 THEN 'start'
              WHEN g.closed_by = 2 THEN 'tail'
              ELSE 'between' END AS gap_kind
  FROM gaps g
  WHERE g.gap_end_s IS NOT NULL AND g.gap_end_s > g.gap_start_s
),
idle_agg AS (
  SELECT workspace_id, warehouse_id,
         SUM(gap_s) AS idle_s,
         SUM(CASE WHEN gap_s > 60 THEN gap_s ELSE 0 END) AS counted_s,
         SUM(CASE WHEN gap_s > 60 THEN 1 ELSE 0 END)     AS counted_gaps,
         MAX(CASE WHEN gap_s > 60 THEN gap_s END)        AS longest_s,
         SUM(CASE WHEN gap_s > 60 AND gap_kind = 'start'    THEN gap_s ELSE 0 END) AS start_s,
         SUM(CASE WHEN gap_s > 60 AND gap_kind = 'between'  THEN gap_s ELSE 0 END) AS between_s,
         SUM(CASE WHEN gap_s > 60 AND gap_kind = 'tail'     THEN gap_s ELSE 0 END) AS tail_s,
         SUM(CASE WHEN gap_s > 60 AND gap_kind = 'no_query' THEN gap_s ELSE 0 END) AS no_query_s,
         SUM(CASE WHEN gap_s > 60 AND gap_kind = 'no_query' THEN 1 ELSE 0 END)     AS no_query_periods
  FROM idle
  GROUP BY workspace_id, warehouse_id
),
counted_cw AS (
  -- cluster-seconds inside each counted gap, from the running segments it spans
  SELECT i.workspace_id, i.warehouse_id,
         SUM((LEAST(i.gap_end_s, s.seg_end_s) - GREATEST(i.gap_start_s, s.t)) * s.clusters) AS idle_cluster_s
  FROM idle i
  JOIN seg s
    ON  s.workspace_id = i.workspace_id AND s.warehouse_id = i.warehouse_id AND s.is_running = 1
    AND s.t < i.gap_end_s AND s.seg_end_s > i.gap_start_s
  WHERE i.gap_s > 60
  GROUP BY i.workspace_id, i.warehouse_id
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
bill_rows AS (
  SELECT u.workspace_id, u.usage_metadata.warehouse_id AS warehouse_id,
         unix_timestamp(u.usage_start_time) AS us,
         u.usage_quantity AS dbus,
         u.usage_quantity * COALESCE(p.list_rate, 0) AS usd,
         CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
              THEN u.usage_quantity ELSE 0 END AS unpriced_q,
         CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END AS priced_q,
         CASE WHEN u.product_features.is_serverless = TRUE THEN 1 ELSE 0 END AS serverless
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.warehouse_id IS NOT NULL
    AND u.usage_date >= current_date() - INTERVAL 30 DAYS
    AND u.usage_date < current_date()
),
bill AS (
  SELECT r.workspace_id, r.warehouse_id,
         SUM(r.dbus) AS net_dbus,
         SUM(r.usd)  AS usd_all,
         SUM(CASE WHEN t.measured_from_s IS NOT NULL
                   AND r.us >= FLOOR(t.measured_from_s / 3600) * 3600 THEN r.usd END) AS measured_usd,
         CASE WHEN SUM(r.unpriced_q) > 0 THEN 'unpriced'
              WHEN SUM(r.priced_q) = 0   THEN 'free'
              ELSE 'priced' END AS price_basis,
         MAX(r.serverless) AS any_serverless
  FROM bill_rows r
  LEFT JOIN wh_time t ON t.workspace_id = r.workspace_id AND t.warehouse_id = r.warehouse_id
  GROUP BY r.workspace_id, r.warehouse_id
),
cfg AS (
  SELECT warehouse_id, warehouse_type, auto_stop_minutes
  FROM `system`.`compute`.`warehouses`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
),
replay AS (
  -- the gaps an auto-stop acts on: between and after queries, a running period with no query at all
  -- (it idles N minutes and stops; no query waits), and the wait before a period's first query (it
  -- stops, and that query waits for a cold start)
  SELECT workspace_id, warehouse_id, g, is_tail FROM qgap
  UNION ALL
  SELECT workspace_id, warehouse_id, gap_s AS g, TRUE AS is_tail FROM idle WHERE gap_kind = 'no_query'
  UNION ALL
  SELECT workspace_id, warehouse_id, gap_s AS g, FALSE AS is_tail FROM idle WHERE gap_kind = 'start'
),
whatif AS (
  -- replay the gaps under an auto-stop of N minutes: a gap up to N stays idle, a longer one idles
  -- N minutes, stops, and the next query waits for a cold start; a pause of 60 or
  -- less is never counted, as in counted_idle_minutes
  SELECT q.workspace_id, q.warehouse_id,
         SUM(CASE WHEN q.g > 60 THEN LEAST(q.g, 60) ELSE 0 END)   AS idle_1_s,  SUM(CASE WHEN q.g > 60   AND NOT q.is_tail THEN 1 ELSE 0 END) AS starts_1,
         SUM(CASE WHEN q.g > 60 THEN LEAST(q.g, 120) ELSE 0 END)  AS idle_2_s,  SUM(CASE WHEN q.g > 120  AND NOT q.is_tail THEN 1 ELSE 0 END) AS starts_2,
         SUM(CASE WHEN q.g > 60 THEN LEAST(q.g, 300) ELSE 0 END)  AS idle_5_s,  SUM(CASE WHEN q.g > 300  AND NOT q.is_tail THEN 1 ELSE 0 END) AS starts_5,
         SUM(CASE WHEN q.g > 60 THEN LEAST(q.g, 600) ELSE 0 END)  AS idle_10_s, SUM(CASE WHEN q.g > 600  AND NOT q.is_tail THEN 1 ELSE 0 END) AS starts_10,
         SUM(CASE WHEN q.g > 60 THEN LEAST(q.g, 1800) ELSE 0 END) AS idle_30_s, SUM(CASE WHEN q.g > 1800 AND NOT q.is_tail THEN 1 ELSE 0 END) AS starts_30,
         -- auto-stop 0 means it never stops: every gap stays idle
         SUM(CASE WHEN q.g <= 60 THEN 0
                  WHEN c.auto_stop_minutes > 0 THEN LEAST(q.g, c.auto_stop_minutes * 60) ELSE q.g END) AS idle_now_s,
         SUM(CASE WHEN c.auto_stop_minutes > 0 AND q.g > c.auto_stop_minutes * 60 AND NOT q.is_tail THEN 1 ELSE 0 END) AS starts_now
  FROM replay q
  LEFT JOIN cfg c ON c.warehouse_id = q.warehouse_id
  WHERE q.g > 0
  GROUP BY q.workspace_id, q.warehouse_id
),
keys AS (
  SELECT workspace_id, warehouse_id FROM wh_time
  UNION
  SELECT workspace_id, warehouse_id FROM bill
),
joined AS (
  SELECT k.workspace_id, k.warehouse_id,
         c.warehouse_type, c.auto_stop_minutes,
         t.warehouse_id AS has_events, t.start_state_known, t.open_at_window_end,
         t.up_s, t.running_s, t.up_cluster_s, t.max_clusters_seen,
         rp.running_periods,
         ba.busy_s,
         ia.idle_s, ia.counted_s, ia.counted_gaps, ia.longest_s, ia.start_s, ia.between_s,
         ia.tail_s, ia.no_query_s, ia.no_query_periods,
         cw.idle_cluster_s,
         bl.net_dbus, bl.usd_all, bl.measured_usd, bl.price_basis, bl.any_serverless,
         wi.idle_1_s, wi.starts_1, wi.idle_2_s, wi.starts_2, wi.idle_5_s, wi.starts_5, wi.idle_10_s, wi.starts_10,
         wi.idle_30_s, wi.starts_30, wi.idle_now_s, wi.starts_now,
         q.qh_rows,
         CASE WHEN t.warehouse_id IS NULL THEN 'no_warehouse_events'
              WHEN q.qh_rows = 0          THEN 'no_query_history'
              WHEN t.running_s = 0        THEN 'no_running_time_measured'
         END AS na_reason
  FROM keys k
  CROSS JOIN qh q
  LEFT JOIN wh_time t          ON t.workspace_id = k.workspace_id  AND t.warehouse_id = k.warehouse_id
  LEFT JOIN running_periods rp ON rp.workspace_id = k.workspace_id AND rp.warehouse_id = k.warehouse_id
  LEFT JOIN busy_agg ba        ON ba.workspace_id = k.workspace_id AND ba.warehouse_id = k.warehouse_id
  LEFT JOIN idle_agg ia        ON ia.workspace_id = k.workspace_id AND ia.warehouse_id = k.warehouse_id
  LEFT JOIN counted_cw cw      ON cw.workspace_id = k.workspace_id AND cw.warehouse_id = k.warehouse_id
  LEFT JOIN bill bl            ON bl.workspace_id = k.workspace_id AND bl.warehouse_id = k.warehouse_id
  LEFT JOIN cfg c              ON c.warehouse_id  = k.warehouse_id
  LEFT JOIN whatif wi          ON wi.workspace_id = k.workspace_id AND wi.warehouse_id = k.warehouse_id
  WHERE COALESCE(t.running_s, 0) > 0 OR bl.net_dbus IS NOT NULL
),
priced AS (
  SELECT j.*,
         CASE WHEN j.na_reason IS NULL AND j.price_basis IN ('priced', 'free')
               AND j.measured_usd IS NOT NULL AND j.up_cluster_s > 0
              THEN j.measured_usd * COALESCE(j.idle_cluster_s, 0) / j.up_cluster_s END AS wasted_raw,
         CASE WHEN j.na_reason IS NULL AND j.price_basis IN ('priced', 'free')
               AND j.measured_usd IS NOT NULL AND j.up_cluster_s > 0
              THEN j.measured_usd * 60 / j.up_cluster_s END AS usd_per_cluster_minute
  FROM joined j
)
SELECT p.workspace_id,
       p.warehouse_id,
       CASE WHEN upper(p.warehouse_type) = 'SERVERLESS' OR p.any_serverless = 1 THEN 'serverless'
            WHEN upper(p.warehouse_type) = 'PRO'     THEN 'pro'
            WHEN upper(p.warehouse_type) = 'CLASSIC' THEN 'classic'
       END                                                               AS warehouse_kind,
       p.auto_stop_minutes,
       CASE WHEN p.has_events IS NULL THEN NULL ELSE p.start_state_known = 1 END   AS start_state_known,
       CASE WHEN p.has_events IS NULL THEN NULL ELSE p.open_at_window_end = 1 END  AS open_at_window_end,
       COALESCE(p.running_periods, 0)                                    AS running_periods,
       ROUND(p.up_s / 60.0, 1)                                           AS up_minutes,
       ROUND(p.running_s / 60.0, 1)                                      AS running_minutes,
       CASE WHEN p.na_reason IN ('no_warehouse_events', 'no_query_history') THEN NULL
            ELSE ROUND(COALESCE(p.busy_s, 0) / 60.0, 1) END              AS busy_minutes,
       CASE WHEN p.na_reason IN ('no_warehouse_events', 'no_query_history') THEN NULL
            ELSE ROUND(COALESCE(p.idle_s, 0) / 60.0, 1) END              AS idle_minutes,
       CASE WHEN p.na_reason IN ('no_warehouse_events', 'no_query_history') THEN NULL
            ELSE ROUND(COALESCE(p.counted_s, 0) / 60.0, 1) END           AS counted_idle_minutes,
       CASE WHEN p.na_reason IN ('no_warehouse_events', 'no_query_history') THEN NULL
            ELSE COALESCE(p.counted_gaps, 0) END                         AS counted_idle_gaps,
       -- the same NULL rule for these five
       CASE WHEN p.na_reason IN ('no_warehouse_events', 'no_query_history') THEN NULL
            ELSE ROUND(COALESCE(p.longest_s, 0) / 60.0, 1) END           AS longest_idle_gap_minutes,
       CASE WHEN p.na_reason IN ('no_warehouse_events', 'no_query_history') THEN NULL
            ELSE ROUND(COALESCE(p.start_s, 0) / 60.0, 1) END             AS start_gap_minutes,
       CASE WHEN p.na_reason IN ('no_warehouse_events', 'no_query_history') THEN NULL
            ELSE ROUND(COALESCE(p.between_s, 0) / 60.0, 1) END           AS between_queries_minutes,
       -- with auto-stop off nothing waits to stop it: the stretch after the last query is running
       -- with no query, not an auto-stop wait
       CASE WHEN p.na_reason IN ('no_warehouse_events', 'no_query_history') THEN NULL
            WHEN p.auto_stop_minutes = 0 THEN 0
            ELSE ROUND(COALESCE(p.tail_s, 0) / 60.0, 1) END              AS stop_tail_minutes,
       CASE WHEN p.na_reason IN ('no_warehouse_events', 'no_query_history') THEN NULL
            ELSE ROUND((COALESCE(p.no_query_s, 0)
                        + CASE WHEN p.auto_stop_minutes = 0 THEN COALESCE(p.tail_s, 0) ELSE 0 END) / 60.0, 1)
       END                                                               AS no_query_minutes,
       CASE WHEN p.na_reason IS NULL
            THEN ROUND(COALESCE(p.counted_s, 0) * 100.0 / p.running_s, 1) END AS idle_share_pct,
       ROUND(p.up_cluster_s / 60.0, 1)                                   AS up_cluster_minutes,
       CASE WHEN p.na_reason IS NULL
            THEN ROUND(COALESCE(p.idle_cluster_s, 0) / 60.0, 1) END      AS idle_cluster_minutes,
       p.max_clusters_seen,
       ROUND(p.net_dbus, 2)                                              AS net_dbus,
       CASE WHEN p.price_basis IN ('priced', 'free') THEN ROUND(p.usd_all, 2) END AS est_usd_list,
       ROUND(p.wasted_raw, 2)                                            AS est_wasted_usd_list,
       p.price_basis,
       60                                                 AS idle_gap_seconds,
       -- no statement history: no gaps to replay; one busy stretch: no gaps, so 0
       CASE WHEN p.qh_rows > 0 THEN ROUND(COALESCE(p.idle_1_s, 0) / 60.0, 1) END  AS autostop_1_idle_minutes,
       CASE WHEN p.qh_rows > 0 THEN COALESCE(p.starts_1, 0) END                   AS autostop_1_cold_starts,
       CASE WHEN p.qh_rows > 0 THEN ROUND(COALESCE(p.idle_2_s, 0) / 60.0, 1) END  AS autostop_2_idle_minutes,
       CASE WHEN p.qh_rows > 0 THEN COALESCE(p.starts_2, 0) END                   AS autostop_2_cold_starts,
       CASE WHEN p.qh_rows > 0 THEN ROUND(COALESCE(p.idle_5_s, 0) / 60.0, 1) END  AS autostop_5_idle_minutes,
       CASE WHEN p.qh_rows > 0 THEN COALESCE(p.starts_5, 0) END                   AS autostop_5_cold_starts,
       CASE WHEN p.qh_rows > 0 THEN ROUND(COALESCE(p.idle_10_s, 0) / 60.0, 1) END AS autostop_10_idle_minutes,
       CASE WHEN p.qh_rows > 0 THEN COALESCE(p.starts_10, 0) END                  AS autostop_10_cold_starts,
       CASE WHEN p.qh_rows > 0 THEN ROUND(COALESCE(p.idle_30_s, 0) / 60.0, 1) END AS autostop_30_idle_minutes,
       CASE WHEN p.qh_rows > 0 THEN COALESCE(p.starts_30, 0) END                  AS autostop_30_cold_starts,
       CASE WHEN p.qh_rows > 0 AND p.auto_stop_minutes IS NOT NULL
            THEN ROUND(COALESCE(p.idle_now_s, 0) / 60.0, 1) END                    AS autostop_now_idle_minutes,
       CASE WHEN p.qh_rows > 0 AND p.auto_stop_minutes IS NOT NULL
            THEN COALESCE(p.starts_now, 0) END                                     AS autostop_now_cold_starts,
       ROUND(p.usd_per_cluster_minute, 4)                                        AS usd_per_cluster_minute,
       CASE
         WHEN p.na_reason = 'no_warehouse_events' THEN
           'billed in the window but no start or stop events were recorded for it, so its running time cannot be measured; if auto-stop is off it may have run the whole window'
         WHEN p.na_reason = 'no_query_history' THEN
           'no SQL statement history was exported for the window, so time running a query cannot be told apart from idle time'
         WHEN p.na_reason = 'no_running_time_measured' THEN
           'billed in the window, but its events show no running time that can be measured (it was already running when its first recorded event in the window arrived)'
         ELSE
           CONCAT_WS('; ',
             CONCAT('idle ', CAST(ROUND(COALESCE(p.counted_s, 0) / 60.0, 0) AS BIGINT), ' of ',
                    CAST(ROUND(p.running_s / 60.0, 0) AS BIGINT), ' running min (',
                    CAST(ROUND(COALESCE(p.counted_s, 0) * 100.0 / p.running_s, 0) AS BIGINT), '%) in ',
                    COALESCE(p.counted_gaps, 0),
                    CASE WHEN COALESCE(p.counted_gaps, 0) = 1 THEN ' gap' ELSE ' gaps' END,
                    ' longer than ', 60, ' s'),
             CASE WHEN COALESCE(p.counted_gaps, 0) > 0
                  THEN CONCAT('longest ', CAST(ROUND(COALESCE(p.longest_s, 0) / 60.0, 0) AS BIGINT), ' min') END,
             CASE WHEN COALESCE(p.tail_s, 0) > 0
                  THEN CONCAT(CAST(ROUND(COALESCE(p.tail_s, 0) / 60.0, 0) AS BIGINT),
                              CASE WHEN p.auto_stop_minutes = 0 THEN ' min after the last query, auto-stop off'
                                   ELSE ' min after the last query, waiting for auto-stop' END) END,
             CASE WHEN COALESCE(p.no_query_s, 0) > 0
                  THEN CONCAT(CAST(ROUND(COALESCE(p.no_query_s, 0) / 60.0, 0) AS BIGINT), ' min in ',
                              COALESCE(p.no_query_periods, 0),
                              CASE WHEN COALESCE(p.no_query_periods, 0) = 1 THEN ' running period' ELSE ' running periods' END,
                              ' with no query at all') END,
             CASE WHEN p.wasted_raw IS NULL
                  THEN CONCAT('no dollar figure: ',
                              CASE WHEN p.price_basis = 'unpriced' THEN 'its usage has no list price'
                                   ELSE 'no billing rows for it in the window' END) END)
       END                                                                AS waste_reason,
       p.na_reason                                                       AS not_assessed_reason,
       CASE
         WHEN p.na_reason IS NOT NULL THEN 'NOT_ASSESSED'
         -- a NULL (unpriced) wasted_raw never meets a dollar band
         WHEN (COALESCE(p.counted_s, 0) >= 30 * 60
               AND COALESCE(p.counted_s, 0) * 100.0 / p.running_s >= 60)
              OR p.wasted_raw >= 200 THEN 'CRITICAL'
         WHEN (COALESCE(p.counted_s, 0) >= 30 * 60
               AND COALESCE(p.counted_s, 0) * 100.0 / p.running_s >= 30)
              OR p.wasted_raw >= 20 THEN 'WARN'
         ELSE 'OK'
       END                                                                AS status
FROM priced p
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         est_wasted_usd_list DESC NULLS LAST, counted_idle_minutes DESC NULLS LAST,
         workspace_id, warehouse_id
) q
"""
CHECKS["compute_idle_node_ratio"] = r"""
-- generated from dbt/models/databricks_direct/compute/d_compute_idle_node_ratio.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/compute/compute_idle_node_ratio.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH price AS (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
    FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
cost_rollup AS (
    -- Pre-aggregated per cluster_id (globally-unique GUID -> workspace_id not needed for the join).
    SELECT u.usage_metadata.cluster_id                         AS cluster_id,
           SUM(u.usage_quantity)                               AS net_dbus,
           SUM(u.usage_quantity * COALESCE(p.list_rate, 0))    AS est_usd_list,
           -- price_basis: a real $0 (free-usage SKU) vs a pricing-coverage gap (understates cost).
           CASE
             WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                           THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
             WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
             ELSE 'priced'
           END                                                  AS price_basis
    FROM `system`.`billing`.`usage` u
    LEFT JOIN price p
      ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
     AND u.usage_end_time >= p.price_start_time
     AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
    WHERE upper(u.usage_unit) = 'DBU'
      AND u.usage_metadata.cluster_id IS NOT NULL
      AND u.usage_date >= dateadd(DAY, -LEAST(30, 90), current_date())
      AND u.usage_date <  current_date()
    GROUP BY u.usage_metadata.cluster_id
),
finding AS (
    SELECT
        cluster_id,
        -- lexicographically-largest node_type label (representative only, not load-weighted)
        MAX(node_type)                                                       AS node_type,
        COUNT(*)                                                             AS total_slices,
        SUM(CASE WHEN NOT driver THEN 1 ELSE 0 END)                          AS worker_slices,
        -- idle = CPU busy (user+system) below 5 on that minute-slice, worker-only.
        SUM(CASE WHEN NOT driver AND (cpu_user_percent + cpu_system_percent) < 5
                 THEN 1 ELSE 0 END)                                          AS worker_idle_slices,
        SUM(CASE WHEN driver THEN 1 ELSE 0 END)                              AS driver_slices,
        -- driver fallback for a genuine single-node cluster (no worker slices at all)
        SUM(CASE WHEN driver AND (cpu_user_percent + cpu_system_percent) < 5
                 THEN 1 ELSE 0 END)                                          AS driver_idle_slices,
        COUNT(DISTINCT CASE WHEN driver THEN NULL ELSE node_type END)        AS worker_node_type_variants,
        -- utilization averaged over ALL slices (NOT only idle rows) so the mean is honest.
        AVG(cpu_user_percent + cpu_system_percent)                          AS avg_cpu_pct_all,
        MAX(cpu_user_percent + cpu_system_percent)                          AS peak_cpu_pct_all,
        AVG(mem_used_percent)                                               AS avg_mem_pct_all,
        MAX(mem_used_percent)                                               AS peak_mem_pct_all,
        MIN(start_time)                                                     AS first_slice,
        MAX(end_time)                                                       AS last_slice
    FROM `system`.`compute`.`node_timeline`
    WHERE start_time >= dateadd(DAY, -LEAST(30, 90), current_date())
      AND start_time < current_date()   -- billing stops before today too
    GROUP BY cluster_id
),
judged AS (
    -- worker slices judge idle when there are any; a single-node cluster (no workers at all)
    -- falls back to its driver instead of being silently dropped.
    SELECT f.*,
           CASE WHEN f.worker_slices > 0 THEN f.worker_slices ELSE f.driver_slices END AS judged_slices,
           CASE WHEN f.worker_slices > 0 THEN f.worker_idle_slices ELSE f.driver_idle_slices END AS judged_idle_slices
    FROM finding f
)
SELECT
    f.cluster_id,
    f.node_type,
    f.total_slices,
    f.judged_idle_slices                                                  AS idle_slices,
    -- idle_ratio = idle minute-slices / judged minute-slices (worker-only, or driver on a
    -- single-node cluster) - a fraction 0-1, never "hours".
    f.judged_idle_slices / NULLIF(f.judged_slices, 0)                     AS idle_ratio,
    f.worker_node_type_variants,
    f.avg_cpu_pct_all,
    f.peak_cpu_pct_all,
    f.avg_mem_pct_all,
    f.peak_mem_pct_all,
    f.first_slice,
    f.last_slice,
    COALESCE(cr.net_dbus, 0)     AS net_dbus,
    COALESCE(cr.est_usd_list, 0) AS est_usd_list,
    COALESCE(cr.price_basis, 'priced') AS price_basis,
    -- directional wasted-$ overlay: idle worker minutes over ALL node minutes, so the driver's
    -- share of the bill is never counted as idle worker waste.
    COALESCE(cr.est_usd_list, 0) * (f.judged_idle_slices / NULLIF(
      CASE WHEN f.worker_slices > 0 THEN f.worker_slices + f.driver_slices ELSE f.driver_slices END, 0)) AS est_wasted_usd_list,
    -- status: worst-first band on idle_ratio (field heuristic). Too few judged slices -> NOT_ASSESSED.
    CASE
      WHEN f.judged_slices < 60                                                THEN 'NOT_ASSESSED'
      WHEN f.judged_idle_slices / NULLIF(f.judged_slices, 0) >= 0.8       THEN 'CRITICAL'
      WHEN f.judged_idle_slices / NULLIF(f.judged_slices, 0) >= 0.5       THEN 'WARN'
      ELSE 'OK'
    END AS status
FROM judged f
LEFT JOIN cost_rollup cr
  ON f.cluster_id = cr.cluster_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END,
         est_wasted_usd_list DESC
LIMIT 100000
) q
"""
CHECKS["lakeflow_failed_jobs_wasted_dbus"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_failed_jobs_wasted_dbus.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_failed_jobs_wasted_dbus.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
usage_rows AS (
  SELECT u.workspace_id,
         u.usage_metadata.job_id     AS job_id,
         u.usage_metadata.job_run_id AS job_run_id,
         u.usage_quantity            AS dbus,
         u.usage_quantity * p.list_rate AS usd,
         CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
              THEN u.usage_quantity ELSE 0 END AS unpriced_q,
         CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END AS priced_q
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE u.usage_date >= dateadd(day, -30, current_date())
    AND u.usage_date < current_date()
    AND upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.job_id IS NOT NULL
),
job_usage AS (
  SELECT workspace_id, job_id,
         SUM(dbus) AS net_dbus, SUM(usd) AS usd_priced_part,
         SUM(unpriced_q) AS unpriced_q, SUM(priced_q) AS priced_q,
         SUM(CASE WHEN job_run_id IS NULL THEN dbus ELSE 0 END)       AS unattributed_dbus,
         SUM(CASE WHEN job_run_id IS NULL THEN usd END)               AS unattributed_usd,
         SUM(CASE WHEN job_run_id IS NULL THEN unpriced_q ELSE 0 END) AS unattributed_unpriced_q,
         SUM(CASE WHEN job_run_id IS NOT NULL THEN 1 ELSE 0 END)      AS run_rows
  FROM usage_rows
  GROUP BY workspace_id, job_id
),
run_usage AS (
  SELECT workspace_id, job_id, job_run_id,
         SUM(dbus) AS run_dbus, SUM(usd) AS run_usd, SUM(unpriced_q) AS run_unpriced_q
  FROM usage_rows
  WHERE job_run_id IS NOT NULL
  GROUP BY workspace_id, job_id, job_run_id
),
cand AS (
  SELECT DISTINCT workspace_id, job_id, run_id
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())
    AND result_state IS NOT NULL
),
run_rows AS (
  SELECT t.workspace_id, t.job_id, t.run_id, t.period_start_time, t.period_end_time,
         t.result_state, t.termination_code,
         unix_timestamp(t.period_end_time) - unix_timestamp(t.period_start_time) AS slice_s
  FROM `system`.`lakeflow`.`job_run_timeline` t
  JOIN cand c ON c.workspace_id = t.workspace_id AND c.job_id = t.job_id AND c.run_id = t.run_id
),
slices AS (
  SELECT r.*,
         1 + COALESCE(SUM(CASE WHEN r.result_state IS NOT NULL THEN 1 ELSE 0 END) OVER (
               PARTITION BY r.workspace_id, r.job_id, r.run_id
               ORDER BY r.period_end_time, r.period_start_time
               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS attempt_no
  FROM run_rows r
),
attempts AS (
  SELECT workspace_id, job_id, run_id, attempt_no,
         SUM(slice_s)          AS attempt_s,
         MAX(result_state)     AS attempt_state,   -- one end row per attempt, or none (in flight)
         MAX(termination_code) AS attempt_code,
         MAX(period_end_time)  AS attempt_end
  FROM slices
  GROUP BY workspace_id, job_id, run_id, attempt_no
),
att2 AS (
  SELECT a.*, MAX(a.attempt_no) OVER (PARTITION BY a.workspace_id, a.job_id, a.run_id) AS last_attempt
  FROM attempts a
),
runs AS (
  SELECT workspace_id, job_id, run_id,
         MAX(CASE WHEN attempt_no = last_attempt THEN attempt_state END) AS final_state,
         MAX(CASE WHEN attempt_no = last_attempt THEN attempt_end END)   AS final_end,
         MAX(CASE WHEN attempt_no = last_attempt AND attempt_state IS NULL THEN 1 ELSE 0 END) AS in_flight,
         SUM(attempt_s) AS run_s,
         SUM(CASE WHEN attempt_no < last_attempt AND attempt_state IN ('FAILED', 'ERROR', 'TIMED_OUT')
                  THEN attempt_s ELSE 0 END) AS failed_attempt_s
  FROM att2
  GROUP BY workspace_id, job_id, run_id
),
classed AS (
  SELECT r.*, ru.run_dbus, ru.run_usd, ru.run_unpriced_q,
         CASE WHEN r.final_state IN ('FAILED', 'ERROR', 'TIMED_OUT') THEN 'failed'
              WHEN r.final_state = 'CANCELLED' THEN 'cancelled'
              WHEN r.failed_attempt_s > 0 THEN 'repaired'
              ELSE 'ok' END AS run_class
  FROM runs r
  LEFT JOIN run_usage ru
    ON ru.workspace_id = r.workspace_id AND ru.job_id = r.job_id AND ru.job_run_id = r.run_id
  WHERE r.in_flight = 0
    AND r.final_end < date_trunc('DAY', current_timestamp())
),
last_fail AS (
  -- the most recent failed attempt (a final failure or a repaired one) of these runs
  SELECT a.workspace_id, a.job_id, a.run_id AS last_failed_run_id,
         a.attempt_code AS last_failed_termination_code
  FROM att2 a
  JOIN classed c ON c.workspace_id = a.workspace_id AND c.job_id = a.job_id AND c.run_id = a.run_id
  WHERE a.attempt_state IN ('FAILED', 'ERROR', 'TIMED_OUT')
  QUALIFY ROW_NUMBER() OVER (PARTITION BY a.workspace_id, a.job_id
                             ORDER BY a.attempt_end DESC, a.run_id DESC) = 1
),
per_job AS (
  SELECT workspace_id, job_id,
         COUNT(*) AS distinct_runs,
         SUM(CASE WHEN run_class = 'failed'    THEN 1 ELSE 0 END) AS failed_runs,
         SUM(CASE WHEN run_class = 'repaired'  THEN 1 ELSE 0 END) AS repaired_runs,
         SUM(CASE WHEN run_class = 'cancelled' THEN 1 ELSE 0 END) AS cancelled_runs,
         SUM(CASE WHEN run_class = 'failed' AND run_dbus IS NULL THEN 1 ELSE 0 END) AS failed_runs_unbilled,
         SUM(CASE WHEN run_class = 'failed' THEN run_dbus
                  WHEN run_class = 'repaired' AND run_s > 0 THEN run_dbus * failed_attempt_s / run_s END) AS wasted_dbus_raw,
         SUM(CASE WHEN run_class = 'failed' THEN run_usd END) AS failed_usd,
         SUM(CASE WHEN run_class = 'repaired' AND run_s > 0 THEN run_usd * failed_attempt_s / run_s END) AS repair_usd,
         SUM(CASE WHEN run_class = 'cancelled' THEN run_usd END) AS cancelled_usd,
         SUM(CASE WHEN run_class IN ('failed', 'repaired') THEN COALESCE(run_unpriced_q, 0) ELSE 0 END) AS waste_unpriced_q
  FROM classed
  GROUP BY workspace_id, job_id
),
judged AS (
  SELECT p.*, j.net_dbus, j.usd_priced_part, j.unpriced_q, j.priced_q, j.unattributed_dbus,
         j.unattributed_usd, j.unattributed_unpriced_q,
         lf.last_failed_run_id, lf.last_failed_termination_code,
         (j.workspace_id IS NOT NULL) AS has_usage,
         p.failed_runs * 100.0 / p.distinct_runs AS rate_raw,
         COALESCE(p.failed_usd, 0) + COALESCE(p.repair_usd, 0) AS waste_raw,
         CASE WHEN j.workspace_id IS NULL THEN 'no_billing_rows'
              WHEN j.run_rows = 0          THEN 'no_run_id_in_billing'
              WHEN p.waste_unpriced_q > 0  THEN 'unpriced'
         END AS na_reason
  FROM per_job p
  LEFT JOIN job_usage j  ON j.workspace_id = p.workspace_id  AND j.job_id = p.job_id
  LEFT JOIN last_fail lf ON lf.workspace_id = p.workspace_id AND lf.job_id = p.job_id
  WHERE p.failed_runs > 0 OR p.repaired_runs > 0
)
SELECT workspace_id, job_id,
       distinct_runs, failed_runs, repaired_runs, cancelled_runs, failed_runs_unbilled,
       ROUND(rate_raw, 1)                                                     AS failure_rate_pct,
       last_failed_termination_code, last_failed_run_id,
       CASE WHEN na_reason IN ('no_billing_rows', 'no_run_id_in_billing') THEN NULL
            ELSE ROUND(COALESCE(wasted_dbus_raw, 0), 2) END                   AS wasted_dbus,
       CASE WHEN na_reason IS NULL THEN ROUND(COALESCE(failed_usd, 0), 2) END    AS est_failed_usd_list,
       CASE WHEN na_reason IS NULL THEN ROUND(COALESCE(repair_usd, 0), 2) END    AS est_repair_usd_list,
       CASE WHEN na_reason IS NULL THEN ROUND(waste_raw, 2) END                   AS est_wasted_usd_list,
       CASE WHEN na_reason IS NULL THEN ROUND(COALESCE(cancelled_usd, 0), 2) END AS est_cancelled_usd_list,
       ROUND(net_dbus, 2)                                                     AS net_dbus,
       CASE WHEN NOT has_usage OR unpriced_q > 0 THEN NULL
            ELSE ROUND(COALESCE(usd_priced_part, 0), 2) END                   AS est_usd_list,
       CASE WHEN NOT has_usage THEN NULL
            WHEN unattributed_dbus = 0 THEN 0
            WHEN unattributed_unpriced_q > 0 THEN NULL
            ELSE ROUND(unattributed_usd, 2) END                               AS est_unattributed_usd_list,
       CASE WHEN na_reason IS NULL AND NOT (unpriced_q > 0)
            THEN ROUND(waste_raw * 100.0 / NULLIF(usd_priced_part, 0), 1) END AS wasted_share_pct,
       CASE WHEN NOT has_usage THEN NULL
            WHEN unpriced_q > 0 THEN 'unpriced' WHEN priced_q = 0 THEN 'free' ELSE 'priced' END AS price_basis,
       CASE
         WHEN na_reason = 'no_billing_rows' THEN
           'failed runs or failed attempts found, but no billed usage for this job in the window - the runs may have failed before compute started, or billing has not landed yet'
         WHEN na_reason = 'no_run_id_in_billing' THEN
           'failed runs or failed attempts found, but this job''s billing rows carry no run id, so what they cost cannot be measured (the job''s whole spend is in est_usd_list, not spread over its runs)'
         WHEN na_reason = 'unpriced' THEN
           'the failed runs'' usage has no list price, so there is no dollar figure (wasted_dbus is shown)'
         ELSE
           CONCAT_WS('; ',
             CONCAT(failed_runs, ' of ', distinct_runs, ' runs failed (', CAST(ROUND(rate_raw, 0) AS BIGINT), '%)'),
             CASE WHEN repaired_runs = 1 THEN '1 run failed first and was repaired'
                  WHEN repaired_runs > 1 THEN CONCAT(repaired_runs, ' runs failed first and were repaired') END,
             CASE WHEN failed_runs_unbilled = 1 THEN '1 failed run had no billed usage'
                  WHEN failed_runs_unbilled > 1 THEN CONCAT(failed_runs_unbilled, ' failed runs had no billed usage') END,
             CASE WHEN last_failed_termination_code IS NOT NULL
                  THEN CONCAT('last failure ', last_failed_termination_code) END)
       END                                                                    AS waste_reason,
       na_reason                                                              AS not_assessed_reason,
       -- the $ floor (1) gates only the $ branches, never the failure-rate ones -- a
       -- job failing outright must not read OK just because it is cheap. The rate branches
       -- themselves need 3 runs first -- 1 of 1 failed is not a rate, just one data point.
       CASE
         WHEN na_reason IS NOT NULL                                                        THEN 'NOT_ASSESSED'
         WHEN (distinct_runs >= 3 AND rate_raw >= 50)
           OR waste_raw >= GREATEST(200, 1)                       THEN 'CRITICAL'
         WHEN (distinct_runs >= 3 AND rate_raw >= 10)
           OR waste_raw >= GREATEST(20, 1)                       THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM judged
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         est_wasted_usd_list DESC NULLS LAST, failed_runs DESC, workspace_id, job_id
) q
"""
CHECKS["instance_pools_idle_capacity"] = r"""
-- generated from dbt/models/databricks_direct/compute/d_instance_pools_idle_capacity.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/compute/instance_pools_idle_capacity.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
cost_rollup AS (
  SELECT u.workspace_id,
         u.usage_metadata.instance_pool_id AS instance_pool_id,
         SUM(u.usage_quantity)                            AS net_dbus,
         SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS est_usd_list,
         CASE
           WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                         THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END                                               AS price_basis
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.instance_pool_id IS NOT NULL
    AND u.usage_date >= date_add(current_date(), -CAST(30 AS INT))
    AND u.usage_date <  current_date()
  GROUP BY u.workspace_id, u.usage_metadata.instance_pool_id
)
SELECT p.instance_pool_id,
       p.instance_pool_name AS instance_pool_name,
       p.node_type, p.min_idle_instances, p.max_capacity,
       p.idle_instance_autotermination_minutes, p.enable_elastic_disk, p.preloaded_spark_version,
       p.preloaded_docker_images, p.tags, p.aws_attributes, p.azure_attributes, p.gcp_attributes, p.disk_spec,
       p.create_time, p.delete_time, p.change_time, p.workspace_id, p.account_id,
       COALESCE(cr.net_dbus, 0)     AS net_dbus,
       COALESCE(cr.est_usd_list, 0) AS est_usd_list,
       COALESCE(cr.price_basis, 'priced') AS price_basis,
       -- status: worst-first band on the standing idle-instance floor (field heuristic; 5 / 20).
       CASE
         WHEN p.min_idle_instances IS NULL THEN 'NOT_ASSESSED'
         WHEN p.min_idle_instances >= 20 THEN 'CRITICAL'
         WHEN p.min_idle_instances >= 5 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY instance_pool_id ORDER BY change_time DESC) AS rn
  FROM `system`.`compute`.`instance_pools`
) p
LEFT JOIN cost_rollup cr
  ON cr.workspace_id = p.workspace_id
 AND cr.instance_pool_id = p.instance_pool_id
WHERE p.rn = 1 AND p.delete_time IS NULL
ORDER BY p.min_idle_instances DESC
) q
"""
CHECKS["lakeflow_jobs_on_all_purpose"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_jobs_on_all_purpose.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_jobs_on_all_purpose.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH task_compute AS (
  SELECT workspace_id, job_id, run_id, task_key, EXPLODE(compute_ids) AS compute_id
  FROM `system`.`lakeflow`.`job_task_run_timeline`
  WHERE period_start_time >= dateadd(DAY, -30, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())
    AND result_state IS NOT NULL
    AND compute_ids IS NOT NULL
    AND size(compute_ids) > 0
),
serverless_runs AS (
  SELECT DISTINCT workspace_id, run_id
  FROM (
    SELECT workspace_id, run_id, EXPLODE(compute) AS c
    FROM `system`.`lakeflow`.`job_task_run_timeline`
    WHERE period_start_time >= dateadd(DAY, -30, current_date())
      AND period_end_time < date_trunc('DAY', current_timestamp())
      AND result_state IS NOT NULL
      AND compute IS NOT NULL
      AND size(compute) > 0
  ) exploded
  WHERE upper(c.type) = 'SERVERLESS'
),
dropped AS (
  SELECT t.workspace_id, COUNT(DISTINCT t.run_id) AS dropped_runs
  FROM `system`.`lakeflow`.`job_task_run_timeline` t
  LEFT JOIN serverless_runs s ON s.workspace_id = t.workspace_id AND s.run_id = t.run_id
  WHERE t.period_start_time >= dateadd(DAY, -30, current_date())
    AND t.period_end_time < date_trunc('DAY', current_timestamp())
    AND t.result_state IS NOT NULL
    AND (t.compute_ids IS NULL OR size(t.compute_ids) = 0)
    AND s.run_id IS NULL
  GROUP BY t.workspace_id
),
warehouse_compute AS (
  SELECT DISTINCT workspace_id, w.warehouse_id AS compute_id
  FROM (
    SELECT workspace_id, EXPLODE(compute) AS w
    FROM `system`.`lakeflow`.`job_task_run_timeline`
    WHERE period_start_time >= dateadd(DAY, -30, current_date())
      AND period_end_time < date_trunc('DAY', current_timestamp())
      AND result_state IS NOT NULL
      AND compute IS NOT NULL
      AND size(compute) > 0
  ) exploded
  WHERE w.warehouse_id IS NOT NULL
),
cluster_jobs AS (
  SELECT workspace_id, compute_id, COUNT(DISTINCT job_id) AS jobs_sharing_cluster
  FROM task_compute
  GROUP BY workspace_id, compute_id
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
cost_rollup AS (
  -- Pre-aggregated per (cluster, job) over the SAME window as the finding, from usage_metadata.
  -- job_id: this job's OWN billed usage on the cluster, not the cluster's whole bill - notebook
  -- or another job's usage on the same shared cluster carries a different (or no) job_id and is
  -- excluded here, never folded into this job's row.
  SELECT u.workspace_id,
         u.usage_metadata.cluster_id                      AS cluster_id,
         u.usage_metadata.job_id                           AS job_id,
         SUM(u.usage_quantity)                            AS net_dbus,
         SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS est_usd_list,
         CASE
           WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                         THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END                                               AS price_basis
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.cluster_id IS NOT NULL
    AND u.usage_metadata.job_id IS NOT NULL
    AND u.usage_date >= dateadd(DAY, -30, current_date())
    AND u.usage_date <  current_date()
  GROUP BY u.workspace_id, u.usage_metadata.cluster_id, u.usage_metadata.job_id
),
task_hours AS (
  SELECT DISTINCT workspace_id, job_id, compute_id, date_trunc('HOUR', period_start_time) AS usage_hour
  FROM (
    SELECT workspace_id, job_id, period_start_time, EXPLODE(compute_ids) AS compute_id
    FROM `system`.`lakeflow`.`job_task_run_timeline`
    WHERE period_start_time >= dateadd(DAY, -30, current_date())
      AND period_end_time < date_trunc('DAY', current_timestamp())
      AND compute_ids IS NOT NULL
      AND size(compute_ids) > 0
  ) t
),
cluster_hours AS (
  SELECT u.workspace_id, u.usage_metadata.cluster_id AS cluster_id,
         date_trunc('HOUR', u.usage_start_time)            AS usage_hour,
         SUM(u.usage_quantity)                            AS net_dbus,
         SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS est_usd_list
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.cluster_id IS NOT NULL
    AND u.usage_date >= dateadd(DAY, -30, current_date())
    AND u.usage_date <  current_date()
  GROUP BY u.workspace_id, u.usage_metadata.cluster_id, date_trunc('HOUR', u.usage_start_time)
),
hour_cost AS (
  SELECT th.workspace_id, th.job_id, th.compute_id,
         SUM(ch.net_dbus) AS net_dbus, SUM(ch.est_usd_list) AS est_usd_list
  FROM task_hours th
  JOIN cluster_hours ch
    ON ch.workspace_id = th.workspace_id AND ch.cluster_id = th.compute_id AND ch.usage_hour = th.usage_hour
  GROUP BY th.workspace_id, th.job_id, th.compute_id
),
finding AS (
  SELECT tc.workspace_id,
         CAST(tc.job_id AS STRING)         AS job_id,
         CAST(tc.compute_id AS STRING)     AS compute_id,
         COALESCE(c.cluster_source, CASE WHEN wc.compute_id IS NOT NULL THEN 'WAREHOUSE' END) AS cluster_source,
         COUNT(DISTINCT tc.run_id)          AS task_runs,
         COALESCE(MAX(cr.net_dbus),
                  CASE WHEN c.cluster_source IN ('UI', 'API') THEN MAX(hc.net_dbus) END, 0)     AS net_dbus,
         COALESCE(MAX(cr.est_usd_list),
                  CASE WHEN c.cluster_source IN ('UI', 'API') THEN MAX(hc.est_usd_list) END, 0) AS est_usd_list,
         COALESCE(MAX(cr.price_basis), 'priced') AS price_basis,
         CASE WHEN MAX(cr.est_usd_list) IS NOT NULL THEN 'job_metered'
              WHEN c.cluster_source IN ('UI', 'API') AND MAX(hc.est_usd_list) IS NOT NULL THEN 'cluster_hours'
         END                                AS cost_basis,
         MAX(cj.jobs_sharing_cluster)       AS jobs_sharing_cluster
  FROM task_compute tc
  LEFT JOIN (
    SELECT workspace_id, cluster_id, cluster_source
    FROM `system`.`compute`.`clusters`
    QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC) = 1
  ) c
    ON tc.workspace_id = c.workspace_id AND tc.compute_id = c.cluster_id
  LEFT JOIN warehouse_compute wc
    ON tc.workspace_id = wc.workspace_id AND tc.compute_id = wc.compute_id
  LEFT JOIN cost_rollup cr
    ON tc.workspace_id = cr.workspace_id AND tc.compute_id = cr.cluster_id AND tc.job_id = cr.job_id
  LEFT JOIN hour_cost hc
    ON tc.workspace_id = hc.workspace_id AND tc.compute_id = hc.compute_id AND tc.job_id = hc.job_id
  LEFT JOIN cluster_jobs cj
    ON tc.workspace_id = cj.workspace_id AND tc.compute_id = cj.compute_id
  GROUP BY tc.workspace_id, tc.job_id, tc.compute_id, c.cluster_source, wc.compute_id
)
SELECT * FROM (
  SELECT
    workspace_id, job_id, compute_id, cluster_source, task_runs,
    net_dbus, est_usd_list, price_basis, cost_basis, jobs_sharing_cluster,
    -- kept under its old name (the app's discount/column logic reads it) - now just this job's
    -- own metered $ (est_usd_list), not a further even split.
    est_usd_list                                                           AS est_usd_list_share,
    CASE
      WHEN cluster_source IS NULL THEN 'NOT_ASSESSED'
      WHEN cluster_source IN ('UI', 'API') AND est_usd_list >= 100 THEN 'CRITICAL'
      WHEN cluster_source IN ('UI', 'API') THEN 'WARN'
      ELSE 'OK'
    END AS status
  FROM finding
  UNION ALL
  SELECT
    workspace_id,
    CAST(NULL AS STRING)                       AS job_id,
    CAST(NULL AS STRING)                       AS compute_id,
    'null or empty compute_ids'                AS cluster_source,
    dropped_runs                               AS task_runs,
    CAST(NULL AS DOUBLE)                       AS net_dbus,
    CAST(NULL AS DOUBLE)                       AS est_usd_list,
    CAST(NULL AS STRING)                       AS price_basis,
    CAST(NULL AS STRING)                       AS cost_basis,
    CAST(NULL AS BIGINT)                       AS jobs_sharing_cluster,
    CAST(NULL AS DOUBLE)                       AS est_usd_list_share,
    'NOT_ASSESSED'                             AS status
  FROM dropped
) placements
ORDER BY
  CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
  est_usd_list_share DESC
LIMIT 100000
) q
"""
CHECKS["lakeflow_stale_zombie_jobs"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_stale_zombie_jobs.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_stale_zombie_jobs.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH latest_jobs AS (
  SELECT workspace_id, job_id, name, run_as, creator_id, create_time, delete_time
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),
last_run AS (
  SELECT workspace_id, job_id, MAX(period_start_time) AS last_run_start
  FROM `system`.`lakeflow`.`job_run_timeline`
  GROUP BY workspace_id, job_id
),
capture_floor AS (
  -- how far back this WORKSPACE's own job_run_timeline history reaches, unfiltered by job
  SELECT workspace_id,
         datediff(current_date(), MIN(period_start_time)) AS capture_floor_days
  FROM `system`.`lakeflow`.`job_run_timeline`
  GROUP BY workspace_id
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
cost_rollup AS (
  SELECT u.workspace_id,
         u.usage_metadata.job_id AS job_id,
         SUM(u.usage_quantity)                            AS net_dbus,
         SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS est_usd_list,
         CASE
           WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                         THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END                                               AS price_basis
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.job_id IS NOT NULL
    AND u.usage_date >= dateadd(day, -30, current_date())
    AND u.usage_date <  current_date()
  GROUP BY u.workspace_id, u.usage_metadata.job_id
)
SELECT j.workspace_id, j.job_id,
       j.name AS name,
       r.last_run_start,
       j.create_time,
       COALESCE(cf.capture_floor_days, 0) AS capture_floor_days,
       CASE WHEN r.last_run_start IS NULL
              OR r.last_run_start < dateadd(day, -30, current_date())
            THEN 1 ELSE 0 END AS is_stale,
       COALESCE(cr.net_dbus, 0)     AS net_dbus,
       COALESCE(cr.est_usd_list, 0) AS est_usd_list,
       COALESCE(cr.price_basis, 'priced') AS price_basis,
       -- status: worst-first band on days since last run (field heuristic; 30 / 90).
       -- a NULL last_run_start is NOT_ASSESSED, never WARN/CRITICAL, while the job or this workspace's
       -- own capture history is too young to call it stale (too_young_to_call_stale).
       CASE
         WHEN r.last_run_start IS NULL
              AND (COALESCE(cf.capture_floor_days, 0) < 30
                   OR j.create_time > dateadd(day, -30, current_date()))
              THEN 'NOT_ASSESSED'
         WHEN r.last_run_start IS NULL THEN 'WARN'   -- confirmed no run in a long-enough capture window
         WHEN r.last_run_start < dateadd(day, -90, current_date()) THEN 'CRITICAL'
         WHEN r.last_run_start < dateadd(day, -30, current_date()) THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN r.last_run_start IS NULL
              AND (COALESCE(cf.capture_floor_days, 0) < 30
                   OR j.create_time > dateadd(day, -30, current_date()))
              THEN 'too_young_to_call_stale'
       END AS not_assessed_reason
FROM latest_jobs j
LEFT JOIN last_run r       ON j.workspace_id = r.workspace_id AND j.job_id = r.job_id
LEFT JOIN capture_floor cf ON j.workspace_id = cf.workspace_id
LEFT JOIN cost_rollup cr   ON j.workspace_id = cr.workspace_id AND j.job_id = cr.job_id
WHERE j.delete_time IS NULL
ORDER BY last_run_start ASC
) q
"""
CHECKS["lakeflow_pipeline_idle_tail_duration"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_pipeline_idle_tail_duration.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_pipeline_idle_tail_duration.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
cost_rollup AS (
  SELECT u.workspace_id,
         u.usage_metadata.dlt_pipeline_id AS dlt_pipeline_id,
         SUM(u.usage_quantity)                            AS net_dbus,
         SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS est_usd_list,
         CASE
           WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                         THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END                                               AS price_basis
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.dlt_pipeline_id IS NOT NULL
    AND u.usage_date >= dateadd(day, -30, current_date())
    AND u.usage_date <  current_date()
  GROUP BY u.workspace_id, u.usage_metadata.dlt_pipeline_id
),
billed_hours AS (
  SELECT u.workspace_id, u.usage_metadata.dlt_pipeline_id AS pipeline_id, u.record_id,
         u.usage_start_time, u.usage_end_time, u.usage_quantity,
         COALESCE(u.product_features.is_serverless, FALSE) AS serverless
  FROM `system`.`billing`.`usage` u
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.dlt_pipeline_id IS NOT NULL
    AND u.usage_date >= dateadd(day, -30, current_date())
    AND u.usage_date <  current_date()
),
billed_active AS (
  -- whether any update slice overlaps the billed row's hour; serverless rows never linger
  SELECT b.workspace_id, b.pipeline_id, b.record_id, b.usage_quantity, b.serverless,
         MAX(CASE WHEN s.pipeline_id IS NOT NULL THEN 1 ELSE 0 END) AS overlapped
  FROM billed_hours b
  LEFT JOIN `system`.`lakeflow`.`pipeline_update_timeline` s
    ON s.workspace_id = b.workspace_id AND s.pipeline_id = b.pipeline_id
   AND s.period_start_time < b.usage_end_time AND s.period_end_time > b.usage_start_time
  GROUP BY b.workspace_id, b.pipeline_id, b.record_id, b.usage_quantity, b.serverless
),
idle AS (
  SELECT workspace_id, pipeline_id,
         SUM(CASE WHEN overlapped = 0 AND NOT serverless THEN usage_quantity ELSE 0 END) AS idle_dbus
  FROM billed_active
  GROUP BY workspace_id, pipeline_id
)
SELECT u.workspace_id, u.pipeline_id, p.pipeline_type, p.setting_continuous, p.setting_development,
       COUNT(DISTINCT CASE WHEN u.result_state IS NOT NULL THEN u.update_id END) AS updates,
       -- every hourly slice counts: an update over an hour writes one row per hour, the result on the last
       SUM(unix_timestamp(u.period_end_time) - unix_timestamp(u.period_start_time)) AS active_seconds_total,
       COALESCE(MAX(cr.net_dbus), 0)     AS net_dbus,
       COALESCE(MAX(cr.est_usd_list), 0) AS est_usd_list,
       COALESCE(MAX(cr.price_basis), 'priced') AS price_basis,
       COALESCE(MAX(i.idle_dbus), 0) AS idle_dbus,
       MAX(i.idle_dbus) / NULLIF(MAX(cr.net_dbus), 0) AS idle_share,
       -- status: worst-first band on DBUs billed outside update time (field heuristic; 100 / 500).
       CASE
         WHEN p.setting_continuous = true THEN 'NOT_ASSESSED'
         WHEN COALESCE(MAX(i.idle_dbus), 0) >= 500 THEN 'CRITICAL'
         WHEN COALESCE(MAX(i.idle_dbus), 0) >= 100 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`lakeflow`.`pipeline_update_timeline` u
LEFT JOIN (
  SELECT workspace_id, pipeline_id, pipeline_type,
         settings.continuous  AS setting_continuous,
         settings.development AS setting_development
  FROM `system`.`lakeflow`.`pipelines`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC) = 1
) p
  ON u.workspace_id = p.workspace_id AND u.pipeline_id = p.pipeline_id
LEFT JOIN cost_rollup cr
  ON u.workspace_id = cr.workspace_id AND u.pipeline_id = cr.dlt_pipeline_id
LEFT JOIN idle i
  ON u.workspace_id = i.workspace_id AND u.pipeline_id = i.pipeline_id
WHERE u.period_start_time >= dateadd(day, -30, current_date())
  AND u.period_end_time < date_trunc('DAY', current_timestamp())
GROUP BY u.workspace_id, u.pipeline_id, p.pipeline_type, p.setting_continuous, p.setting_development
ORDER BY idle_dbus DESC, net_dbus DESC
) q
"""
CHECKS["cost_premium_serverless_photon"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_premium_serverless_photon.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_premium_serverless_photon.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT usage_date, cloud, workspace_id, sku_name, billing_origin_product,
       product_features.is_serverless      AS is_serverless,
       product_features.is_photon          AS is_photon,
       product_features.jobs_tier          AS jobs_tier,
       product_features.sql_tier           AS sql_tier,
       product_features.dlt_tier           AS dlt_tier,
       product_features.performance_target AS performance_target,
       SUM(usage_quantity) AS net_usage_quantity
FROM `system`.`billing`.`usage`
WHERE usage_date >= dateadd(day, -30, current_date())
  AND usage_date < current_date()
  AND usage_unit = 'DBU'
GROUP BY usage_date, cloud, workspace_id, sku_name, billing_origin_product,
         product_features.is_serverless, product_features.is_photon, product_features.jobs_tier,
         product_features.sql_tier, product_features.dlt_tier, product_features.performance_target
ORDER BY usage_date DESC, sku_name, cloud
) q
"""
CHECKS["compute_warehouse_cache_reuse"] = r"""
-- generated from dbt/models/databricks_direct/compute/d_compute_warehouse_cache_reuse.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/compute/compute_warehouse_cache_reuse.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH wh AS (
  SELECT warehouse_id, warehouse_name, warehouse_type, auto_stop_minutes
  FROM `system`.`compute`.`warehouses`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
),
q AS (
  SELECT h.workspace_id, h.compute.warehouse_id AS warehouse_id, h.start_time,
         h.from_result_cache, h.read_io_cache_percent,
         upper(COALESCE(h.statement_type, '')) = 'SELECT' AS is_select,
         -- no text, or text redacted for this reader, is no evidence of a repeat
         CASE WHEN trim(COALESCE(h.statement_text, '')) NOT IN ('', '<REDACTED>')
              THEN sha2(h.statement_text, 256) END AS statement_hash,
         -- idle time before this query: from the latest end of any earlier query, never below 0
         GREATEST(unix_timestamp(h.start_time)
           - unix_timestamp(MAX(COALESCE(h.end_time, h.update_time, h.start_time)) OVER (
               PARTITION BY h.compute.warehouse_id ORDER BY h.start_time
               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)), 0) AS gap_seconds
  FROM `system`.`query`.`history` h
  WHERE h.compute.warehouse_id IS NOT NULL
    AND h.start_time >= date_sub(current_date(), 30)
    AND h.start_time < current_date()
),
stmt_counts AS (
  SELECT warehouse_id, statement_hash, COUNT(*) AS n
  FROM q
  WHERE is_select AND statement_hash IS NOT NULL
  GROUP BY warehouse_id, statement_hash
),
repeated_agg AS (
  SELECT warehouse_id, SUM(CASE WHEN n > 1 THEN n ELSE 0 END) AS repeated_query_rows,
         SUM(n) AS texted_selects
  FROM stmt_counts
  GROUP BY warehouse_id
),
agg AS (
  SELECT warehouse_id,
         COUNT(*) AS queries,
         SUM(CASE WHEN is_select THEN 1 ELSE 0 END) AS select_queries,
         SUM(CASE WHEN is_select AND from_result_cache THEN 1 ELSE 0 END) AS from_cache_queries,
         AVG(read_io_cache_percent) AS avg_read_io_cache_percent,
         percentile(gap_seconds, 0.5) AS median_gap_seconds
  FROM q
  GROUP BY warehouse_id
)
SELECT
  a.warehouse_id,
  w.warehouse_name,
  CASE WHEN upper(w.warehouse_type) = 'SERVERLESS' THEN 'serverless'
       WHEN upper(w.warehouse_type) = 'PRO'        THEN 'pro'
       WHEN upper(w.warehouse_type) = 'CLASSIC'     THEN 'classic'
  END AS warehouse_kind,
  w.auto_stop_minutes,
  a.queries,
  a.select_queries,
  a.queries - a.select_queries AS other_statements,
  CASE WHEN a.select_queries > 0 THEN ROUND(a.from_cache_queries * 100.0 / a.select_queries, 1) END AS from_result_cache_pct,
  ROUND(a.avg_read_io_cache_percent, 1) AS avg_read_io_cache_percent,
  CASE WHEN r.texted_selects > 0 THEN ROUND(r.repeated_query_rows * 100.0 / r.texted_selects, 1) END AS repeated_statement_pct,
  ROUND(a.median_gap_seconds / 60.0, 1) AS median_gap_minutes,
  CASE
    WHEN a.select_queries < 10 THEN 'OK'
    -- Warn only when auto-stop sits ABOVE this warehouse's own type minimum -- 17 of 19 WARN
    -- warehouses on the fixture were already at the pro/classic floor (10 min) with nothing left to
    -- lower, so a fixed :warn_long_autostop_minutes threshold flagged them for a fix that does not
    -- exist. A type this query does not recognise (warehouse_kind NULL) never warns on auto-stop
    -- alone, rather than guess a floor for it.
    WHEN (a.from_cache_queries * 100.0 / a.select_queries) < 5
         -- auto-stop off (0) leaves the most room to lower
         AND CASE WHEN w.auto_stop_minutes = 0 THEN TRUE
                  WHEN upper(w.warehouse_type) = 'SERVERLESS' THEN w.auto_stop_minutes > 5
                  WHEN upper(w.warehouse_type) IN ('PRO', 'CLASSIC') THEN w.auto_stop_minutes > 10
                  ELSE FALSE END
      THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM agg a
LEFT JOIN wh w ON w.warehouse_id = a.warehouse_id
LEFT JOIN repeated_agg r ON r.warehouse_id = a.warehouse_id
WHERE a.queries > 0
ORDER BY CASE status WHEN 'WARN' THEN 0 ELSE 1 END, from_result_cache_pct ASC NULLS LAST
) q
"""
CHECKS["cost_dollarized_by_sku_day"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_dollarized_by_sku_day.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_dollarized_by_sku_day.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT u.workspace_id, u.usage_date, u.cloud, u.sku_name, u.billing_origin_product, u.usage_type, u.usage_unit,
       lp.currency_code,
       SUM(u.usage_quantity)               AS net_usage_quantity,
       SUM(u.usage_quantity * lp.list_rate) AS net_list_cost,
       -- price_basis: a real $0 (free-usage SKU) vs a pricing-coverage gap (NULL, understates cost).
       CASE
         WHEN lp.list_rate IS NOT NULL THEN 'priced'
         WHEN upper(u.sku_name) LIKE '%FREE_USAGE%' THEN 'free'
         ELSE 'unpriced'
       END                                  AS price_basis
FROM `system`.`billing`.`usage` u
LEFT JOIN (
  SELECT sku_name, cloud, currency_code, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective (post-promotion) list price - DEC-66.1
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
) lp
  ON u.sku_name   = lp.sku_name
 AND u.cloud      = lp.cloud
 AND u.usage_unit = lp.usage_unit
 AND u.usage_end_time >= lp.price_start_time
 AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
WHERE u.usage_date >= dateadd(day, -30, current_date())
  AND u.usage_date < current_date()
GROUP BY u.workspace_id, u.usage_date, u.cloud, u.sku_name, u.billing_origin_product, u.usage_type, u.usage_unit, lp.currency_code,
         CASE
           WHEN lp.list_rate IS NOT NULL THEN 'priced'
           WHEN upper(u.sku_name) LIKE '%FREE_USAGE%' THEN 'free'
           ELSE 'unpriced'
         END
ORDER BY u.usage_date DESC, u.cloud, u.sku_name
) q
"""
CHECKS["cost_period_over_period"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_period_over_period.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_period_over_period.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH snapshot AS (
  -- the WHOLE account's own earliest recorded usage_date, over ALL history the snapshot carries
  -- (unrestricted by the window below, and never filtered to this row's own workspace/product) -
  -- the DEC-64 coverage signal shared by every row (see caveats).
  SELECT MIN(usage_date) AS snapshot_start
  FROM `system`.`billing`.`usage`
),
priced AS (
  SELECT u.workspace_id, u.billing_origin_product, u.usage_date, u.sku_name,
         u.usage_quantity                AS usage_quantity,
         u.usage_quantity * lp.list_rate AS list_cost,
         lp.list_rate                    AS list_rate
  FROM `system`.`billing`.`usage` u
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
    FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
  ) lp
    ON u.sku_name   = lp.sku_name
   AND u.cloud      = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_date >= dateadd(day, -(30 * 2), current_date())
    AND u.usage_date <  current_date()
),
raw_agg AS (
  -- Deliberately NO "ELSE 0" on current_cost/previous_cost: a row outside the side being summed
  -- must contribute NOTHING (NULL, skipped by SUM), not a literal 0 -- an "ELSE 0" there means
  -- ANY row on the OTHER side alone is enough to turn a fully-unpriced side's SUM from NULL into
  -- a false concrete 0, silently hiding the "this side could not be priced at all" case the
  -- NOT_ASSESSED branches below exist to catch. The four quantity sums keep their own ELSE 0 --
  -- they are per-side row counts, never a price total, so a side with zero matching rows
  -- legitimately IS a concrete 0 there.
  SELECT workspace_id, billing_origin_product,
         SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date())
                  THEN list_cost END)                                        AS current_cost,
         SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date())
                  THEN list_cost END)                                        AS previous_cost,
         SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date())
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                            AS current_unpriced_quantity,
         SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date())
                   AND list_rate IS NOT NULL
                  THEN usage_quantity ELSE 0 END)                            AS current_priced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date())
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                            AS previous_unpriced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date())
                   AND list_rate IS NOT NULL
                  THEN usage_quantity ELSE 0 END)                            AS previous_priced_quantity
  FROM priced
  GROUP BY workspace_id, billing_origin_product
),
agg AS (
  -- eff_current_cost/eff_previous_cost: a side whose SUM is NULL only because it had NO matching
  -- rows at all (never a pricing gap -- the per-side unpriced quantity is 0 too) resolves to a
  -- real 0, exactly the "the missing side sums to a genuine $0, never NOT_ASSESSED" guarantee the
  -- caveats below describe; a side whose SUM is NULL because every one of its rows was genuinely
  -- unpriced stays NULL here and is caught by the NOT_ASSESSED branches below instead.
  SELECT *,
         CASE WHEN current_cost IS NULL AND current_unpriced_quantity = 0 THEN 0 ELSE current_cost END AS eff_current_cost,
         CASE WHEN previous_cost IS NULL AND previous_unpriced_quantity = 0 THEN 0 ELSE previous_cost END AS eff_previous_cost
  FROM raw_agg
)
SELECT a.workspace_id,
       a.billing_origin_product,
       ROUND(a.eff_current_cost, 2)                        AS est_current_usd_list,
       ROUND(a.eff_previous_cost, 2)                        AS est_previous_usd_list,
       ROUND(a.eff_current_cost - a.eff_previous_cost, 2)   AS est_change_usd_list,
       ROUND((a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100, 1) AS change_pct,
       -- price_basis: a real $0 (free-usage SKUs) vs a pricing-coverage gap (understates cost),
       -- computed over BOTH windows combined (caveats).
       CASE
         WHEN (a.current_unpriced_quantity + a.previous_unpriced_quantity) > 0 THEN 'unpriced'
         WHEN (a.current_priced_quantity + a.previous_priced_quantity) = 0     THEN 'free'
         ELSE 'priced'
       END AS price_basis,
       CASE
         WHEN s.snapshot_start > dateadd(day, -(30 * 2), current_date())
           THEN 'NOT_ASSESSED'
         WHEN a.current_unpriced_quantity > 0
           THEN 'NOT_ASSESSED'
         WHEN a.previous_unpriced_quantity > 0
           THEN 'NOT_ASSESSED'
         WHEN COALESCE(a.eff_current_cost, 0) < 50 THEN 'OK'
         WHEN a.eff_previous_cost = 0         THEN 'CRITICAL'
         WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= 50
           THEN 'CRITICAL'
         WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= 25
           THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN s.snapshot_start > dateadd(day, -(30 * 2), current_date())
           THEN 'previous_window_not_covered'
         WHEN a.current_unpriced_quantity > 0
           THEN 'current_period_unpriced'
         WHEN a.previous_unpriced_quantity > 0
           THEN 'previous_period_unpriced'
         ELSE NULL
       END AS not_assessed_reason
FROM agg a
CROSS JOIN snapshot s
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         est_change_usd_list DESC,
         workspace_id, billing_origin_product
) q
"""
CHECKS["cost_monthly_actuals"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_monthly_actuals.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_monthly_actuals.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
WITH snapshot AS (
  -- the WHOLE account's own earliest recorded usage_date across system.billing.usage - the
  -- DEC-64 signal for whether the export's own oldest month is itself only partly captured.
  SELECT MIN(usage_date) AS snapshot_start
  FROM `system`.`billing`.`usage`
)
SELECT
    u.workspace_id,
    u.billing_origin_product,
    CAST(date_trunc('MONTH', u.usage_date) AS DATE)               AS month_start,
    MAX(lp.currency_code)                                         AS currency_code,
    ROUND(SUM(u.usage_quantity * lp.list_rate), 2)                AS net_list_cost_usd,
    COUNT(DISTINCT u.usage_date)                                  AS days_captured,
    CASE
      WHEN CAST(date_trunc('MONTH', u.usage_date) AS DATE) = CAST(date_trunc('MONTH', current_date()) AS DATE)
        THEN TRUE
      WHEN s.snapshot_start > CAST(date_trunc('MONTH', u.usage_date) AS DATE)
        THEN TRUE
      ELSE FALSE
    END AS is_partial_month,
    CASE
      WHEN CAST(date_trunc('MONTH', u.usage_date) AS DATE) = CAST(date_trunc('MONTH', current_date()) AS DATE)
        THEN 'month_in_progress'
      WHEN s.snapshot_start > CAST(date_trunc('MONTH', u.usage_date) AS DATE)
        THEN 'export_starts_mid_month'
      ELSE NULL
    END AS partial_reason,
    -- price_basis: a real $0 (free-usage SKUs) vs a pricing-coverage gap (understates the month).
    CASE
      WHEN SUM(CASE WHEN lp.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                    THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
      WHEN SUM(CASE WHEN lp.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
      ELSE 'priced'
    END AS price_basis,
    MIN(u.usage_date)                                             AS first_day,
    MAX(u.usage_date)                                             AS last_day
FROM `system`.`billing`.`usage` u
CROSS JOIN snapshot s
LEFT JOIN (
  SELECT sku_name, cloud, currency_code, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
) lp
  ON u.sku_name   = lp.sku_name
 AND u.cloud      = lp.cloud
 AND u.usage_unit = lp.usage_unit
 AND u.usage_end_time >= lp.price_start_time
 AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
WHERE u.usage_date < current_date()
GROUP BY u.workspace_id, u.billing_origin_product, CAST(date_trunc('MONTH', u.usage_date) AS DATE),
         s.snapshot_start
ORDER BY month_start DESC, workspace_id, billing_origin_product
) q
"""
CHECKS["cost_unnamed_workspaces"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_unnamed_workspaces.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_unnamed_workspaces.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH priced AS (
  SELECT u.workspace_id, u.usage_date, u.usage_unit, u.usage_quantity,
         u.usage_quantity * p.list_rate AS list_cost
  FROM `system`.`billing`.`usage` u
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
    FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
  ) p
    ON  u.sku_name   = p.sku_name
    AND u.cloud      = p.cloud
    AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE u.workspace_id IS NOT NULL
    AND datediff(current_date(), u.usage_date) <= 365
    AND u.usage_date < current_date()
),
by_ws AS (
  SELECT workspace_id,
         MIN(usage_date)                                                          AS first_used,
         MAX(usage_date)                                                          AS last_used,
         COUNT(DISTINCT usage_date)                                               AS active_days,
         SUM(CASE WHEN upper(usage_unit) = 'DBU' THEN usage_quantity ELSE 0 END)  AS dbus_365d,
         SUM(list_cost)                                                           AS net_list_cost_usd_365d,
         SUM(CASE WHEN usage_date >= current_date() - INTERVAL 30 DAYS
                  THEN list_cost END)                                             AS window_cost,
         SUM(CASE WHEN usage_date >= current_date() - INTERVAL 30 DAYS
                  THEN usage_quantity END)                                        AS window_quantity
  FROM priced
  GROUP BY workspace_id
)
SELECT
  b.workspace_id,
  b.first_used,
  b.last_used,
  b.active_days,
  ROUND(b.dbus_365d, 2)                          AS dbus_365d,
  ROUND(b.net_list_cost_usd_365d, 2)             AS net_list_cost_usd_365d,
  ROUND(COALESCE(b.window_cost, 0), 2)           AS net_list_cost_usd,
  datediff(b.last_used, b.first_used) + 1        AS lifetime_days,
  (datediff(b.last_used, b.first_used) + 1) < 30 AS short_lived,
  CASE
    -- usage on a SKU with no list price is still money moving
    WHEN COALESCE(b.window_cost, 0) > 0 OR COALESCE(b.window_quantity, 0) > 0 THEN 'CRITICAL'
    WHEN datediff(current_date(), b.last_used) <= 90 THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM by_ws b
LEFT JOIN `system`.`access`.`workspaces_latest` wl ON wl.workspace_id = b.workspace_id
WHERE wl.workspace_id IS NULL
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
         net_list_cost_usd DESC NULLS LAST, b.last_used DESC, b.workspace_id
) q
"""
CHECKS["query_failed_queries_daily"] = r"""
-- generated from dbt/models/databricks_direct/performance/d_query_failed_queries_daily.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/performance/query_failed_queries_daily.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT date(start_time) AS day, workspace_id, compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
       execution_status, statement_type,
       executed_by AS executed_by,
       COUNT(*) AS query_count,
       SUM(total_duration_ms)     AS total_duration_ms_sum,
       SUM(execution_duration_ms) AS execution_duration_ms_sum,
       -- error text de-valued at source: strip emails, then single-quoted string literals (chr(39) is
       -- the single quote) - keeps the error SHAPE, drops literal data values.
       regexp_replace(
         regexp_replace(max_by(error_message, start_time), '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}', '<email>'),
         concat(chr(39), '[^', chr(39), ']*', chr(39)), '?'
       )                          AS error_message_sample,
       -- the day's failed and canceled count on this warehouse and statement type, every user and status
       SUM(COUNT(*)) OVER (
         PARTITION BY date(start_time), workspace_id, compute.warehouse_id, statement_type) AS warehouse_day_type_count,
       -- status: worst-first band on that daily count (field heuristic; 5 / 20).
       CASE
         WHEN SUM(COUNT(*)) OVER (
                PARTITION BY date(start_time), workspace_id, compute.warehouse_id, statement_type) >= 20 THEN 'CRITICAL'
         WHEN SUM(COUNT(*)) OVER (
                PARTITION BY date(start_time), workspace_id, compute.warehouse_id, statement_type) >= 5 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`query`.`history`
WHERE start_time >= current_date() - INTERVAL 30 DAYS
  AND start_time < current_date()
  AND execution_status IN ('FAILED','CANCELED')
GROUP BY date(start_time), workspace_id, compute.type, compute.warehouse_id, execution_status, statement_type, executed_by
ORDER BY query_count DESC
) q
"""
CHECKS["query_queuing_waits"] = r"""
-- generated from dbt/models/databricks_direct/performance/d_query_queuing_waits.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/performance/query_queuing_waits.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH per_query AS (
  SELECT date(start_time) AS day, workspace_id, compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
         waiting_at_capacity_duration_ms, waiting_for_compute_duration_ms, total_duration_ms,
         COALESCE(waiting_at_capacity_duration_ms, 0) + COALESCE(waiting_for_compute_duration_ms, 0) AS wait_ms
  FROM `system`.`query`.`history`
  WHERE start_time >= current_date() - INTERVAL 30 DAYS
    AND start_time < current_date()
)
SELECT day, workspace_id, compute_type, warehouse_id,
       COUNT(*) AS query_count,
       SUM(CASE WHEN waiting_at_capacity_duration_ms > 0 THEN 1 ELSE 0 END) AS queued_at_capacity_count,
       SUM(CASE WHEN waiting_for_compute_duration_ms > 0 THEN 1 ELSE 0 END) AS waited_for_compute_count,
       SUM(waiting_at_capacity_duration_ms)  AS waiting_at_capacity_ms_sum,
       SUM(waiting_for_compute_duration_ms)  AS waiting_for_compute_ms_sum,
       SUM(total_duration_ms)                AS total_duration_ms_sum,
       -- avg_wait_ms: the average combined wait of statements that waited at all (a real, all-zero
       -- day has no such statement -> NULL, never 0 and never NOT_ASSESSED on its own).
       SUM(CASE WHEN wait_ms > 0 THEN wait_ms ELSE 0 END)
         / NULLIF(SUM(CASE WHEN wait_ms > 0 THEN 1 ELSE 0 END), 0) AS avg_wait_ms,
       -- avg_slot_wait_ms: the average wait for a free slot of statements that queued at capacity;
       -- a cold start (waiting for compute) is the auto-stop's doing, not a too-small warehouse
       SUM(CASE WHEN waiting_at_capacity_duration_ms > 0 THEN waiting_at_capacity_duration_ms ELSE 0 END)
         / NULLIF(SUM(CASE WHEN waiting_at_capacity_duration_ms > 0 THEN 1 ELSE 0 END), 0) AS avg_slot_wait_ms,
       -- status: worst-first band on the PER-STATEMENT average SLOT wait (field heuristic;
       -- 60 / 600), not the day's raw total - so many tiny waits on a
       -- busy warehouse no longer outrank one genuinely long wait. NOT_ASSESSED is checked only
       -- when avg_wait_ms does not already clear a band, and triggers when EITHER bucket's raw SUM
       -- is NULL across the whole group (e.g. a SERVERLESS_COMPUTE day that never reports
       -- waiting_at_capacity at all) - the same rule as before, unaffected by the average.
       CASE
         WHEN SUM(CASE WHEN waiting_at_capacity_duration_ms > 0 THEN waiting_at_capacity_duration_ms ELSE 0 END)
                / NULLIF(SUM(CASE WHEN waiting_at_capacity_duration_ms > 0 THEN 1 ELSE 0 END), 0) >= 600 * 1000 THEN 'CRITICAL'
         WHEN SUM(CASE WHEN waiting_at_capacity_duration_ms > 0 THEN waiting_at_capacity_duration_ms ELSE 0 END)
                / NULLIF(SUM(CASE WHEN waiting_at_capacity_duration_ms > 0 THEN 1 ELSE 0 END), 0) >= 60 * 1000 THEN 'WARN'
         WHEN SUM(waiting_at_capacity_duration_ms) IS NULL OR SUM(waiting_for_compute_duration_ms) IS NULL THEN 'NOT_ASSESSED'
         ELSE 'OK'
       END AS status
FROM per_query
GROUP BY day, workspace_id, compute_type, warehouse_id
ORDER BY (waiting_at_capacity_ms_sum + waiting_for_compute_ms_sum) DESC
) q
"""
CHECKS["perf_daily_by_resource"] = r"""
-- generated from dbt/models/databricks_direct/performance/d_perf_daily_by_resource.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/performance/perf_daily_by_resource.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH stmts AS (
  SELECT 'warehouse' AS resource_type, workspace_id, compute.warehouse_id AS resource_id, date(start_time) AS usage_date,
         1 AS runs,
         CASE WHEN execution_status = 'FAILED' THEN 1 ELSE 0 END AS failed,
         COALESCE(total_duration_ms, 0) / 1000.0 AS total_s,
         (COALESCE(waiting_at_capacity_duration_ms, 0) + COALESCE(waiting_for_compute_duration_ms, 0)) / 1000.0 AS queued_s,
         COALESCE(execution_duration_ms, 0) / 1000.0 AS exec_s,
         COALESCE(spilled_local_bytes, 0) / 1e9 AS spilled_gb,
         0 AS task_runs, CAST(0 AS DOUBLE) AS setup_s,
         COALESCE(waiting_at_capacity_duration_ms, 0) / 1000.0 AS slot_wait_s,
         COALESCE(waiting_for_compute_duration_ms, 0) / 1000.0 AS provision_s,
         COALESCE(shuffle_read_bytes, 0) / 1e9 AS shuffle_gb,
         COALESCE(read_bytes, 0) / 1e9 AS read_gb,
         COALESCE(read_bytes, 0) * COALESCE(read_io_cache_percent, 0) / 100.0 / 1e9 AS cache_read_gb,
         CASE WHEN from_result_cache THEN 1 ELSE 0 END AS result_cache_runs,
         COALESCE(result_fetch_duration_ms, 0) / 1000.0 AS fetch_s,
         CAST(0 AS DOUBLE) AS provision_clock_s, 0 AS starts, CAST(0 AS DOUBLE) AS start_s, CAST(0 AS DOUBLE) AS start_s_max, 0 AS failed_starts
  FROM `system`.`query`.`history`
  WHERE start_time >= current_date() - INTERVAL 30 DAYS
    AND start_time < current_date()
    AND compute.warehouse_id IS NOT NULL
),
run_rows AS (
  -- A repair keeps the run_id: rows after an earlier end row belong to the next attempt.
  SELECT workspace_id, job_id, run_id, period_start_time, period_end_time, result_state,
         NULLIF(run_duration_seconds, 0) AS run_s_reported,
         COALESCE(queue_duration_seconds, 0) AS queue_s,
         MAX(CASE WHEN result_state IS NOT NULL THEN period_end_time END) OVER (
           PARTITION BY workspace_id, job_id, run_id ORDER BY period_start_time
           ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_attempt_end
  FROM `system`.`lakeflow`.`job_run_timeline`
  -- a week before the window, so a run that began before it shows its real start
  WHERE period_start_time >= dateadd(day, -(30 + 7), current_date())
),
run_attempts AS (
  SELECT workspace_id, job_id, run_id,
         MIN(period_start_time)                                            AS attempt_start,
         MAX(CASE WHEN result_state IS NOT NULL THEN period_end_time END)  AS period_end_time,
         MAX(CASE WHEN result_state IS NOT NULL THEN result_state END)     AS result_state,
         MAX(CASE WHEN result_state IS NOT NULL THEN run_s_reported END)   AS run_s_reported,
         MAX(CASE WHEN result_state IS NOT NULL THEN queue_s END)          AS queue_s
  FROM run_rows
  GROUP BY workspace_id, job_id, run_id, prev_attempt_end
),
run_final AS (
  -- each run once, on its final attempt (timed on its own); the day is the run's first start
  SELECT *, MIN(attempt_start) OVER (PARTITION BY workspace_id, job_id, run_id) AS run_start
  FROM run_attempts
  WHERE period_end_time IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id, run_id ORDER BY period_end_time DESC) = 1
),
runs AS (
  SELECT 'job' AS resource_type, workspace_id, job_id AS resource_id, date(run_start) AS usage_date,
         1 AS runs,
         CASE WHEN result_state IN ('FAILED', 'ERROR', 'TIMED_OUT') THEN 1 ELSE 0 END AS failed,
         COALESCE(run_s_reported, timestampdiff(SECOND, attempt_start, period_end_time)) AS total_s,
         queue_s AS queued_s,
         CAST(0 AS DOUBLE) AS exec_s, CAST(0 AS DOUBLE) AS spilled_gb, 0 AS task_runs, CAST(0 AS DOUBLE) AS setup_s,
         CAST(0 AS DOUBLE) AS slot_wait_s, CAST(0 AS DOUBLE) AS provision_s, CAST(0 AS DOUBLE) AS shuffle_gb, CAST(0 AS DOUBLE) AS read_gb, CAST(0 AS DOUBLE) AS cache_read_gb, 0 AS result_cache_runs, CAST(0 AS DOUBLE) AS fetch_s,
         CAST(0 AS DOUBLE) AS provision_clock_s, 0 AS starts, CAST(0 AS DOUBLE) AS start_s, CAST(0 AS DOUBLE) AS start_s_max, 0 AS failed_starts
  FROM run_final
  WHERE period_end_time < date_trunc('DAY', current_timestamp())
    AND run_start >= dateadd(day, -30, current_date())
),
task_rows AS (
  SELECT workspace_id, job_id, run_id, period_end_time, result_state,
         COALESCE(setup_duration_seconds, 0) AS setup_s,
         COALESCE(execution_duration_seconds, 0) AS exec_s,
         MIN(period_start_time) OVER (PARTITION BY workspace_id, job_id, run_id) AS task_start
  FROM `system`.`lakeflow`.`job_task_run_timeline`
  WHERE period_start_time >= dateadd(day, -(30 + 7), current_date())
),
tasks AS (
  SELECT 'job' AS resource_type, workspace_id, job_id AS resource_id, date(task_start) AS usage_date,
         0 AS runs, 0 AS failed, CAST(0 AS DOUBLE) AS total_s, CAST(0 AS DOUBLE) AS queued_s,
         -- a skipped or blocked task never ran, so it does not dilute the average setup
         exec_s, CAST(0 AS DOUBLE) AS spilled_gb,
         CASE WHEN result_state IN ('SKIPPED', 'BLOCKED') THEN 0 ELSE 1 END AS task_runs, setup_s,
         CAST(0 AS DOUBLE) AS slot_wait_s, CAST(0 AS DOUBLE) AS provision_s, CAST(0 AS DOUBLE) AS shuffle_gb, CAST(0 AS DOUBLE) AS read_gb, CAST(0 AS DOUBLE) AS cache_read_gb, 0 AS result_cache_runs, CAST(0 AS DOUBLE) AS fetch_s,
         CAST(0 AS DOUBLE) AS provision_clock_s, 0 AS starts, CAST(0 AS DOUBLE) AS start_s, CAST(0 AS DOUBLE) AS start_s_max, 0 AS failed_starts
  FROM task_rows
  WHERE result_state IS NOT NULL
    AND period_end_time < date_trunc('DAY', current_timestamp())
    AND task_start >= dateadd(day, -30, current_date())
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id, run_id ORDER BY period_end_time DESC) = 1
),
update_rows AS (
  SELECT workspace_id, pipeline_id, update_id, period_end_time, result_state,
         MIN(period_start_time) OVER (PARTITION BY workspace_id, pipeline_id, update_id) AS update_start
  FROM `system`.`lakeflow`.`pipeline_update_timeline`
  WHERE period_start_time >= dateadd(day, -(30 + 7), current_date())
),
updates AS (
  SELECT 'pipeline' AS resource_type, workspace_id, pipeline_id AS resource_id, date(update_start) AS usage_date,
         1 AS runs,
         CASE WHEN result_state = 'FAILED' THEN 1 ELSE 0 END AS failed,
         timestampdiff(SECOND, update_start, period_end_time) AS total_s,
         CAST(0 AS DOUBLE) AS queued_s, CAST(0 AS DOUBLE) AS exec_s, CAST(0 AS DOUBLE) AS spilled_gb, 0 AS task_runs, CAST(0 AS DOUBLE) AS setup_s,
         CAST(0 AS DOUBLE) AS slot_wait_s, CAST(0 AS DOUBLE) AS provision_s, CAST(0 AS DOUBLE) AS shuffle_gb, CAST(0 AS DOUBLE) AS read_gb, CAST(0 AS DOUBLE) AS cache_read_gb, 0 AS result_cache_runs, CAST(0 AS DOUBLE) AS fetch_s,
         CAST(0 AS DOUBLE) AS provision_clock_s, 0 AS starts, CAST(0 AS DOUBLE) AS start_s, CAST(0 AS DOUBLE) AS start_s_max, 0 AS failed_starts
  FROM update_rows
  WHERE result_state IS NOT NULL
    AND period_end_time < date_trunc('DAY', current_timestamp())
    AND update_start >= dateadd(day, -30, current_date())
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id, update_id ORDER BY period_end_time DESC) = 1
),
wait_spans AS (
  SELECT workspace_id, compute.warehouse_id AS warehouse_id, start_time,
         unix_millis(start_time) AS w_start,
         unix_millis(start_time) + waiting_for_compute_duration_ms AS w_end
  FROM `system`.`query`.`history`
  WHERE start_time >= current_date() - INTERVAL 30 DAYS
    AND start_time < current_date()
    AND compute.warehouse_id IS NOT NULL
    AND waiting_for_compute_duration_ms > 0
),
wait_breaks AS (
  SELECT workspace_id, warehouse_id, start_time, w_start, w_end,
         CASE WHEN w_start <= MAX(w_end) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY w_start, w_end
                                               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
              THEN 0 ELSE 1 END AS new_span
  FROM wait_spans
),
wait_numbered AS (
  SELECT workspace_id, warehouse_id, start_time, w_start, w_end,
         SUM(new_span) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY w_start, w_end
                             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS span_id
  FROM wait_breaks
),
wait_clock AS (
  SELECT 'warehouse' AS resource_type, workspace_id, warehouse_id AS resource_id, date(MIN(start_time)) AS usage_date,
         0 AS runs, 0 AS failed, CAST(0 AS DOUBLE) AS total_s, CAST(0 AS DOUBLE) AS queued_s,
         CAST(0 AS DOUBLE) AS exec_s, CAST(0 AS DOUBLE) AS spilled_gb, 0 AS task_runs, CAST(0 AS DOUBLE) AS setup_s,
         CAST(0 AS DOUBLE) AS slot_wait_s, CAST(0 AS DOUBLE) AS provision_s, CAST(0 AS DOUBLE) AS shuffle_gb, CAST(0 AS DOUBLE) AS read_gb, CAST(0 AS DOUBLE) AS cache_read_gb, 0 AS result_cache_runs, CAST(0 AS DOUBLE) AS fetch_s,
         (MAX(w_end) - MIN(w_start)) / 1000.0 AS provision_clock_s, 0 AS starts, CAST(0 AS DOUBLE) AS start_s,
         CAST(0 AS DOUBLE) AS start_s_max, 0 AS failed_starts
  FROM wait_numbered
  GROUP BY workspace_id, warehouse_id, span_id
),
wh_state AS (
  SELECT workspace_id, warehouse_id, event_type, event_time,
         LEAD(event_type) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY event_time,
           CASE event_type WHEN 'STARTING' THEN 1 WHEN 'RUNNING' THEN 2 WHEN 'STOPPING' THEN 3 ELSE 4 END) AS next_type,
         LEAD(event_time) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY event_time,
           CASE event_type WHEN 'STARTING' THEN 1 WHEN 'RUNNING' THEN 2 WHEN 'STOPPING' THEN 3 ELSE 4 END) AS next_time
  FROM `system`.`compute`.`warehouse_events`
  WHERE event_time >= current_date() - INTERVAL 30 DAYS
    AND event_time < current_date()
    AND event_type IN ('STARTING', 'RUNNING', 'STOPPING', 'STOPPED')
),
wh_starts AS (
  SELECT 'warehouse' AS resource_type, workspace_id, warehouse_id AS resource_id, date(event_time) AS usage_date,
         0 AS runs, 0 AS failed, CAST(0 AS DOUBLE) AS total_s, CAST(0 AS DOUBLE) AS queued_s,
         CAST(0 AS DOUBLE) AS exec_s, CAST(0 AS DOUBLE) AS spilled_gb, 0 AS task_runs, CAST(0 AS DOUBLE) AS setup_s,
         CAST(0 AS DOUBLE) AS slot_wait_s, CAST(0 AS DOUBLE) AS provision_s, CAST(0 AS DOUBLE) AS shuffle_gb, CAST(0 AS DOUBLE) AS read_gb, CAST(0 AS DOUBLE) AS cache_read_gb, 0 AS result_cache_runs, CAST(0 AS DOUBLE) AS fetch_s,
         CAST(0 AS DOUBLE) AS provision_clock_s, 1 AS starts,
         (unix_millis(next_time) - unix_millis(event_time)) / 1000.0 AS start_s,
         (unix_millis(next_time) - unix_millis(event_time)) / 1000.0 AS start_s_max,
         CASE WHEN next_type = 'RUNNING' THEN 0 ELSE 1 END AS failed_starts
  FROM wh_state
  WHERE event_type = 'STARTING' AND next_type IN ('RUNNING', 'STOPPING', 'STOPPED')
),
by_resource AS (
  SELECT * FROM stmts
  UNION ALL SELECT * FROM runs
  UNION ALL SELECT * FROM tasks
  UNION ALL SELECT * FROM updates
  UNION ALL SELECT * FROM wait_clock
  UNION ALL SELECT * FROM wh_starts
),
daily AS (
  SELECT resource_type, workspace_id, resource_id, usage_date,
         SUM(runs) AS runs, SUM(failed) AS failed, SUM(total_s) AS total_s, SUM(queued_s) AS queued_s,
         SUM(exec_s) AS exec_s, SUM(spilled_gb) AS spilled_gb, SUM(task_runs) AS task_runs, SUM(setup_s) AS setup_s,
         SUM(slot_wait_s) AS slot_wait_s, SUM(provision_s) AS provision_s, SUM(shuffle_gb) AS shuffle_gb,
         SUM(read_gb) AS read_gb, SUM(cache_read_gb) AS cache_read_gb, SUM(result_cache_runs) AS result_cache_runs, SUM(fetch_s) AS fetch_s,
         SUM(provision_clock_s) AS provision_clock_s, SUM(starts) AS starts, SUM(start_s) AS start_s,
         MAX(start_s_max) AS start_s_max, SUM(failed_starts) AS failed_starts
  FROM by_resource
  GROUP BY resource_type, workspace_id, resource_id, usage_date
),
ranked AS (
  SELECT d.*,
         DENSE_RANK() OVER (PARTITION BY resource_type
                            ORDER BY resource_total_s DESC, workspace_id ASC NULLS LAST, resource_id ASC NULLS LAST) AS busy_rank
  FROM (
    SELECT daily.*, SUM(total_s) OVER (PARTITION BY resource_type, workspace_id, resource_id) AS resource_total_s
    FROM daily
  ) d
),
keyed AS (
  SELECT usage_date, resource_type, workspace_id, runs, failed, total_s, queued_s, exec_s, spilled_gb, task_runs, setup_s,
         slot_wait_s, provision_s, shuffle_gb, read_gb, cache_read_gb, result_cache_runs, fetch_s,
         provision_clock_s, starts, start_s, start_s_max, failed_starts,
         busy_rank > 100 AS is_other,
         CASE WHEN busy_rank > 100 THEN NULL ELSE resource_id END AS resource_id,
         concat(COALESCE(workspace_id, 'account'), ':', CASE WHEN busy_rank > 100 THEN 'other' ELSE resource_id END) AS resource_key
  FROM ranked
)
SELECT usage_date,
       resource_type,
       resource_key,
       workspace_id,
       resource_id,
       CASE WHEN resource_type = 'warehouse' THEN resource_id END AS warehouse_id,
       CASE WHEN resource_type = 'job' THEN resource_id END AS job_id,
       CASE WHEN resource_type = 'pipeline' THEN resource_id END AS pipeline_id,
       is_other,
       CASE WHEN is_other THEN COUNT(*) END AS pooled_count,
       SUM(runs) AS runs,
       SUM(failed) AS failed,
       ROUND(SUM(total_s), 1) AS total_s,
       ROUND(SUM(queued_s), 1) AS queued_s,
       ROUND(SUM(exec_s), 1) AS exec_s,
       ROUND(SUM(spilled_gb), 3) AS spilled_gb,
       SUM(task_runs) AS task_runs,
       ROUND(SUM(setup_s), 1) AS setup_s,
       ROUND(SUM(slot_wait_s), 1) AS slot_wait_s,
       ROUND(SUM(provision_s), 1) AS provision_s,
       ROUND(SUM(shuffle_gb), 6) AS shuffle_gb,
       ROUND(SUM(read_gb), 6) AS read_gb,
       ROUND(SUM(cache_read_gb), 6) AS cache_read_gb,
       SUM(result_cache_runs) AS result_cache_runs,
       ROUND(SUM(fetch_s), 1) AS fetch_s,
       ROUND(SUM(provision_clock_s), 1) AS provision_clock_s,
       SUM(starts) AS starts,
       ROUND(SUM(start_s), 1) AS start_s,
       ROUND(MAX(start_s_max), 1) AS start_s_max,
       SUM(failed_starts) AS failed_starts
FROM keyed
GROUP BY usage_date, resource_type, resource_key, workspace_id, resource_id, is_other
ORDER BY usage_date DESC, resource_type, resource_key
) q
"""
CHECKS["lakeflow_job_reliability"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_job_reliability.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/jobs_pipelines/lakeflow_job_reliability.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH end_rows AS (
  SELECT workspace_id, job_id, run_id, period_start_time, period_end_time,
         result_state, termination_code,
         NULLIF(run_duration_seconds, 0) AS run_s_reported   -- 0 = not reported on a multi-task job
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
    AND result_state IS NOT NULL                                   -- end row only
),
run_first AS (
  -- the run's TRUE first observed slice, not just its end row's own slice start -- a run over ~1h
  -- is sliced hourly and only the end row carries result_state.
  SELECT workspace_id, job_id, run_id, MIN(period_start_time) AS run_start
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
  GROUP BY workspace_id, job_id, run_id
),
run_final AS (
  -- one row per RUN, deduplicated to its FINAL attempt: a repaired/retried run writes one
  -- non-NULL-result_state end row per attempt, sharing run_id (see caveats) - the run's own
  -- outcome is the outcome of its LAST attempt, and attempt_rows counts every attempt it took.
  -- The current day is dropped only after that pick, so a run repaired today is not read as
  -- yesterday's failure.
  SELECT * FROM (
  SELECT e.workspace_id, e.job_id, e.run_id,
         f.run_start,
         e.period_end_time                                           AS run_end,
         e.run_s_reported,
         e.result_state                                              AS final_result_state,
         e.termination_code                                          AS final_termination_code,
         COUNT(*) OVER (PARTITION BY e.workspace_id, e.job_id, e.run_id) AS attempt_rows
  FROM end_rows e
  JOIN run_first f
    ON  f.workspace_id = e.workspace_id AND f.job_id = e.job_id AND f.run_id = e.run_id
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY e.workspace_id, e.job_id, e.run_id ORDER BY e.period_end_time DESC
  ) = 1
  ) f
  WHERE run_end < date_trunc('DAY', current_timestamp())
),
run_judged AS (
  SELECT workspace_id, job_id, run_id, run_start, final_result_state, final_termination_code,
         attempt_rows,
         CASE WHEN final_result_state IN ('FAILED', 'ERROR', 'TIMED_OUT') THEN 1 ELSE 0 END AS is_failed,
         CASE WHEN attempt_rows > 1 THEN 1 ELSE 0 END                                       AS repaired,
         -- reported duration when the column is there, else wall clock from the run's true start
         -- (T1: feeds suggested_timeout_minutes below)
         COALESCE(run_s_reported, timestampdiff(SECOND, run_start, run_end)) AS duration_s,
         ROW_NUMBER() OVER (
           PARTITION BY workspace_id, job_id ORDER BY run_start DESC, run_id DESC
         )                                                                                  AS recency_rank
  FROM run_final
),
run_first_success AS (
  -- the recency_rank of the job's most recent SUCCEEDED run (NULL if it has none in the window)
  -- -- the only rank that stops the streak; a SKIPPED / CANCELLED / BLOCKED run in between is
  -- stepped over, never treated as if it broke the streak (see caveats).
  SELECT workspace_id, job_id, run_id, recency_rank, is_failed,
         MIN(CASE WHEN final_result_state = 'SUCCEEDED' THEN recency_rank END) OVER (
           PARTITION BY workspace_id, job_id
         ) AS first_success_rank
  FROM run_judged
),
job_streak AS (
  -- consecutive failures counting back from the latest run: every FAILED/ERROR/TIMED_OUT run
  -- more recent than the job's most recent SUCCEEDED run (or, if it has none in the window,
  -- every failed run there is) -- a SKIPPED/CANCELLED/BLOCKED run in between does not count
  -- toward the streak, but does not stop it either.
  SELECT workspace_id, job_id,
         SUM(CASE WHEN is_failed = 1 AND recency_rank < COALESCE(first_success_rank, 2147483647)
                  THEN 1 ELSE 0 END) AS consecutive_failures
  FROM run_first_success
  GROUP BY workspace_id, job_id
),
job_totals AS (
  SELECT workspace_id, job_id,
         COUNT(*)         AS runs,
         SUM(is_failed)   AS failed_runs,
         SUM(repaired)    AS runs_with_retry,
         MAX(run_start)   AS latest_run_start
  FROM run_judged
  GROUP BY workspace_id, job_id
),
last_n AS (
  SELECT workspace_id, job_id,
         COUNT(*)       AS last_n_runs_considered,
         SUM(is_failed) AS last_n_failed
  FROM run_judged
  WHERE recency_rank <= 10
  GROUP BY workspace_id, job_id
),
success_stats AS (
  -- T1: a concrete timeout suggestion needs 5+ successful runs to mean anything (see caveats).
  SELECT workspace_id, job_id,
         SUM(CASE WHEN final_result_state = 'SUCCEEDED' THEN 1 ELSE 0 END) AS successful_runs,
         percentile(CASE WHEN final_result_state = 'SUCCEEDED' AND attempt_rows = 1 THEN duration_s / 60.0 END, 0.95) AS p95_success_minutes,
         MAX(CASE WHEN final_result_state = 'SUCCEEDED' AND attempt_rows = 1 THEN duration_s / 60.0 END)              AS max_success_minutes
  FROM run_judged
  GROUP BY workspace_id, job_id
),
latest AS (
  SELECT workspace_id, job_id,
         final_result_state     AS latest_result_state,
         final_termination_code AS latest_termination_code
  FROM run_judged
  WHERE recency_rank = 1
),
latest_jobs AS (
  -- system.lakeflow.jobs is SCD2: one row per change, so take the newest per job.
  SELECT workspace_id, job_id, name AS job_name
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
)
SELECT t.workspace_id,
       t.job_id,
       j.job_name,                                                -- NULL for submit/workflow runs
       t.runs,
       t.failed_runs,
       ROUND(100.0 * t.failed_runs / NULLIF(t.runs, 0), 1)         AS failure_rate_pct,
       t.runs_with_retry,
       n.last_n_runs_considered,
       n.last_n_failed,
       CONCAT(CAST(n.last_n_failed AS STRING), ' of last ',
              CAST(n.last_n_runs_considered AS STRING), ' runs failed') AS last_n_summary,
       s.consecutive_failures,
       t.latest_run_start,
       l.latest_result_state,
       l.latest_termination_code,
       ss.successful_runs,
       ROUND(ss.p95_success_minutes, 1) AS p95_success_minutes,
       ROUND(ss.max_success_minutes, 1) AS max_success_minutes,
       -- T1: a concrete change, never invented from too few runs (caveats) - 2x p95, never below
       -- the longest successful run, only once there are 5+ successful runs to measure it from.
       CASE WHEN ss.successful_runs >= 5
            THEN CEIL(GREATEST(2 * ss.p95_success_minutes, ss.max_success_minutes))
            ELSE NULL END AS suggested_timeout_minutes,
       -- status: a live failure streak wins first, regardless of 5 (field heuristic).
       CASE
         WHEN s.consecutive_failures >= 3            THEN 'CRITICAL'
         WHEN t.runs < 5                                             THEN 'NOT_ASSESSED'
         WHEN (100.0 * t.failed_runs / NULLIF(t.runs, 0)) >= 20 THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN s.consecutive_failures >= 3 THEN NULL
         WHEN t.runs < 5                                   THEN 'too_few_runs'
         ELSE NULL
       END AS not_assessed_reason
FROM job_totals t
JOIN job_streak s ON s.workspace_id = t.workspace_id AND s.job_id = t.job_id
JOIN last_n n     ON n.workspace_id = t.workspace_id AND n.job_id = t.job_id
JOIN latest l     ON l.workspace_id = t.workspace_id AND l.job_id = t.job_id
LEFT JOIN success_stats ss ON ss.workspace_id = t.workspace_id AND ss.job_id = t.job_id
LEFT JOIN latest_jobs j ON j.workspace_id = t.workspace_id AND j.job_id = t.job_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         s.consecutive_failures DESC,
         failure_rate_pct DESC,
         workspace_id, job_id
) q
"""
CHECKS["lakeflow_job_duration_regression"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_job_duration_regression.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/jobs_pipelines/lakeflow_job_duration_regression.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH all_rows AS (
  -- every row, not just end rows: a run over ~1h is sliced hourly and only its LAST row carries
  -- result_state (see caveats), so the attempt's true start has to come from its own earlier
  -- NULL-state slices too, not from the last slice alone.
  SELECT workspace_id, job_id, run_id, period_start_time, period_end_time, result_state,
         NULLIF(run_duration_seconds, 0)   AS run_s_reported,
         NULLIF(queue_duration_seconds, 0) AS queue_s_reported,
         NULLIF(setup_duration_seconds, 0) AS setup_s_reported,
         NULLIF(execution_duration_seconds, 0) AS execution_s_reported
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())   -- drop incomplete current day
),
row_marked AS (
  -- prev_attempt_end: the END time of this run_id's most recent PRIOR attempt (NULL for a run's
  -- first, or only, attempt). Constant across every row of one attempt, including its own
  -- earlier NULL-state hourly slices, because it only advances once an attempt's own end row has
  -- been passed - this is what lets attempt_span below collapse a whole attempt, sliced hourly or
  -- not, down to its own true start.
  SELECT workspace_id, job_id, run_id, period_start_time, period_end_time, result_state,
         run_s_reported, queue_s_reported, setup_s_reported, execution_s_reported,
         MAX(CASE WHEN result_state IS NOT NULL THEN period_end_time END) OVER (
           PARTITION BY workspace_id, job_id, run_id
           ORDER BY period_start_time
           ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
         ) AS prev_attempt_end
  FROM all_rows
),
attempt_span AS (
  -- one row per ATTEMPT (not per run, not per hourly slice): every row sharing a run_id AND a
  -- prev_attempt_end is part of the same attempt, so this is the fix for the hourly-slicing bug -
  -- attempt_start is the attempt's own true first minute, not its last slice's start.
  SELECT workspace_id, job_id, run_id, prev_attempt_end,
         MIN(period_start_time)                                          AS attempt_start,
         MAX(CASE WHEN result_state IS NOT NULL THEN period_end_time END) AS attempt_end,
         MAX(CASE WHEN result_state IS NOT NULL THEN result_state END)    AS attempt_result_state,
         MAX(CASE WHEN result_state IS NOT NULL THEN run_s_reported END)  AS run_s_reported,
         MAX(CASE WHEN result_state IS NOT NULL THEN queue_s_reported END)     AS queue_s_reported,
         MAX(CASE WHEN result_state IS NOT NULL THEN setup_s_reported END)     AS setup_s_reported,
         MAX(CASE WHEN result_state IS NOT NULL THEN execution_s_reported END) AS execution_s_reported
  FROM row_marked
  GROUP BY workspace_id, job_id, run_id, prev_attempt_end
),
run_final AS (
  -- one row per RUN, deduplicated to its FINAL attempt (see lakeflow_job_reliability and
  -- caveats above): a repaired run's duration is measured across every slice of its LAST attempt,
  -- from that attempt's own true start to its own end. An attempt still in flight right now
  -- (attempt_end IS NULL, no end row observed yet) is dropped - this query only measures a run
  -- once it has finished.
  SELECT workspace_id, job_id, run_id,
         attempt_start                                              AS run_start,
         attempt_end                                                AS run_end,
         attempt_result_state                                       AS final_result_state,
         run_s_reported, queue_s_reported, setup_s_reported, execution_s_reported,
         COUNT(*) OVER (PARTITION BY workspace_id, job_id, run_id)  AS attempt_rows
  FROM attempt_span
  WHERE attempt_end IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id, run_id ORDER BY attempt_end DESC
  ) = 1
),
run_duration AS (
  SELECT workspace_id, job_id, run_id, run_start, final_result_state, attempt_rows,
         -- reported duration when the column is there (legacy single-task job), else wall clock
         -- of this same final attempt, now spanning every slice it took (a lower bound only when
         -- the attempt's own first slice fell before the window start; see caveats)
         COALESCE(run_s_reported, timestampdiff(SECOND, run_start, run_end)) AS duration_s,
         -- queue/setup/execution stay NULL when not reported (legacy-single-task-only columns,
         -- T12) - never a wall-clock fallback, since there is no wall-clock split of the run into
         -- these three phases without them
         queue_s_reported, setup_s_reported, execution_s_reported
  FROM run_final
),
bucketed AS (
  -- the "recent" slice is capped at half the chosen window (FLOOR, matching Spark's truncating
  -- integer division so both dialects agree) so it is always a real sub-slice of the window with
  -- a non-empty remainder to compare against, even when 7 >= 30 (the
  -- account's shortest offered window); see caveats.
  SELECT workspace_id, job_id, run_id, final_result_state, attempt_rows, duration_s,
         queue_s_reported, setup_s_reported, execution_s_reported,
         CASE WHEN run_start >= dateadd(day, -LEAST(7, CAST(FLOOR(30 / 2) AS INT)), current_date())
              THEN 'RECENT' ELSE 'BASELINE' END AS bucket
  FROM run_duration
),
agg AS (
  SELECT workspace_id, job_id,
         SUM(CASE WHEN bucket = 'RECENT'   AND final_result_state = 'SUCCEEDED' THEN 1 ELSE 0 END) AS recent_runs,
         SUM(CASE WHEN bucket = 'RECENT'   AND final_result_state = 'SUCCEEDED'
                   AND attempt_rows > 1                                        THEN 1 ELSE 0 END) AS recent_runs_repaired,
         SUM(CASE WHEN bucket = 'BASELINE' AND final_result_state = 'SUCCEEDED' THEN 1 ELSE 0 END) AS baseline_runs,
         SUM(CASE WHEN bucket = 'BASELINE' AND final_result_state = 'SUCCEEDED'
                   AND attempt_rows > 1                                        THEN 1 ELSE 0 END) AS baseline_runs_repaired,
         percentile(
           CASE WHEN bucket = 'RECENT'   AND final_result_state = 'SUCCEEDED' THEN duration_s END, 0.5
         ) AS recent_median_s,
         percentile(
           CASE WHEN bucket = 'BASELINE' AND final_result_state = 'SUCCEEDED' THEN duration_s END, 0.5
         ) AS baseline_median_s,
         -- T12: which part grew - capacity (queue/setup) or the code/data itself (execution)
         percentile(CASE WHEN bucket = 'RECENT'   AND final_result_state = 'SUCCEEDED' THEN queue_s_reported END, 0.5) AS recent_queue_s,
         percentile(CASE WHEN bucket = 'BASELINE' AND final_result_state = 'SUCCEEDED' THEN queue_s_reported END, 0.5) AS baseline_queue_s,
         percentile(CASE WHEN bucket = 'RECENT'   AND final_result_state = 'SUCCEEDED' THEN setup_s_reported END, 0.5) AS recent_setup_s,
         percentile(CASE WHEN bucket = 'BASELINE' AND final_result_state = 'SUCCEEDED' THEN setup_s_reported END, 0.5) AS baseline_setup_s,
         percentile(CASE WHEN bucket = 'RECENT'   AND final_result_state = 'SUCCEEDED' THEN execution_s_reported END, 0.5) AS recent_execution_s,
         percentile(CASE WHEN bucket = 'BASELINE' AND final_result_state = 'SUCCEEDED' THEN execution_s_reported END, 0.5) AS baseline_execution_s
  FROM bucketed
  GROUP BY workspace_id, job_id
),
grown AS (
  -- T12: which part grew more, proportionally - queue+setup (capacity) or execution (code/data).
  -- A side's ratio is NULL, not a false 0/1, whenever either its own baseline or recent median
  -- could not be measured (see caveats) - never invented from a missing reading.
  SELECT workspace_id, job_id,
         (COALESCE(recent_queue_s, 0) + COALESCE(recent_setup_s, 0))
           / NULLIF(COALESCE(baseline_queue_s, 0) + COALESCE(baseline_setup_s, 0), 0) AS qs_ratio,
         recent_execution_s / NULLIF(baseline_execution_s, 0)                       AS exec_ratio
  FROM agg
),
latest_jobs AS (
  -- system.lakeflow.jobs is SCD2: one row per change, so take the newest per job.
  SELECT workspace_id, job_id, name AS job_name
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
)
SELECT a.workspace_id,
       a.job_id,
       j.job_name,                                                -- NULL for submit/workflow runs
       a.recent_runs,
       a.recent_runs_repaired,
       a.baseline_runs,
       a.baseline_runs_repaired,
       ROUND(a.recent_median_s / 60.0, 1)                          AS recent_median_minutes,
       ROUND(a.baseline_median_s / 60.0, 1)                        AS baseline_median_minutes,
       ROUND(a.recent_median_s / NULLIF(a.baseline_median_s, 0), 2) AS slowdown_ratio,
       a.recent_queue_s, a.baseline_queue_s,
       a.recent_setup_s, a.baseline_setup_s,
       a.recent_execution_s, a.baseline_execution_s,
       -- T12: names a side only when it actually grew (ratio > 1) and grew more than the other
       -- measurable side - NULL when neither grew, or neither is measurable (see caveats)
       CASE
         WHEN g.qs_ratio > 1 AND (g.exec_ratio IS NULL OR g.qs_ratio >= g.exec_ratio)   THEN 'queue_or_setup'
         WHEN g.exec_ratio > 1 AND (g.qs_ratio IS NULL OR g.exec_ratio > g.qs_ratio)     THEN 'run_time'
         ELSE NULL
       END AS grew_most,
       CASE
         WHEN a.recent_runs < 3
           OR a.baseline_runs < 3                                            THEN 'NOT_ASSESSED'
         WHEN (a.recent_median_s / NULLIF(a.baseline_median_s, 0)) >= 2.0       THEN 'CRITICAL'
         WHEN (a.recent_median_s / NULLIF(a.baseline_median_s, 0)) >= 1.5        THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN a.recent_runs < 3 AND a.baseline_runs < 3 THEN 'too_few_runs_both_sides'
         WHEN a.recent_runs < 3                                           THEN 'too_few_recent_runs'
         WHEN a.baseline_runs < 3                                         THEN 'too_few_baseline_runs'
         ELSE NULL
       END AS not_assessed_reason
FROM agg a
LEFT JOIN grown g       ON g.workspace_id = a.workspace_id AND g.job_id = a.job_id
LEFT JOIN latest_jobs j ON j.workspace_id = a.workspace_id AND j.job_id = a.job_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         slowdown_ratio DESC,
         workspace_id, job_id
) q
"""
CHECKS["cost_chargeback_by_warehouse"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_chargeback_by_warehouse.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_chargeback_by_warehouse.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH snapshot AS (
  SELECT MIN(usage_date) AS snapshot_start
  FROM `system`.`billing`.`usage`
),
priced AS (
  SELECT u.workspace_id, CASE WHEN upper(u.usage_unit) = 'DBU' THEN u.usage_metadata.warehouse_id END AS warehouse_id, u.usage_date,
         u.record_type, u.usage_start_time, u.sku_name,
         u.usage_quantity                AS usage_quantity,
         u.usage_quantity * lp.list_rate AS list_cost,
         lp.list_rate                    AS list_rate
  FROM `system`.`billing`.`usage` u
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
    FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
  ) lp
    ON u.sku_name   = lp.sku_name
   AND u.cloud      = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  -- no unit/id filter: usage with no warehouse id (and any non-DBU unit) becomes the workspace's
  -- own NULL-warehouse row, so the rows add up to total spend
  WHERE u.usage_date >= dateadd(day, -(30 * 2), current_date())
    AND u.usage_date <  current_date()
),
raw_agg AS (
  SELECT workspace_id, warehouse_id,
         SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date())
                  THEN list_cost END)                                        AS current_cost,
         SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date())
                  THEN list_cost END)                                        AS previous_cost,
         SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date())
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                            AS current_unpriced_quantity,
         SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date())
                   AND list_rate IS NOT NULL
                  THEN usage_quantity ELSE 0 END)                            AS current_priced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date())
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                            AS previous_unpriced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date())
                   AND list_rate IS NOT NULL
                  THEN usage_quantity ELSE 0 END)                            AS previous_priced_quantity,
         COUNT(DISTINCT CASE WHEN record_type = 'ORIGINAL'
                               AND usage_date >= dateadd(day, -30, current_date())
                             THEN usage_start_time END)                      AS billed_hours
  FROM priced
  GROUP BY workspace_id, warehouse_id
),
agg AS (
  SELECT *,
         CASE WHEN current_cost IS NULL AND current_unpriced_quantity = 0 THEN 0 ELSE current_cost END AS eff_current_cost,
         CASE WHEN previous_cost IS NULL AND previous_unpriced_quantity = 0 THEN 0 ELSE previous_cost END AS eff_previous_cost
  FROM raw_agg
),
queries AS (
  SELECT workspace_id, compute.warehouse_id AS warehouse_id, COUNT(*) AS queries
  FROM `system`.`query`.`history`
  WHERE compute.warehouse_id IS NOT NULL
    AND start_time >= CAST(dateadd(day, -30, current_date()) AS TIMESTAMP)
    AND start_time <  current_date()
  GROUP BY workspace_id, compute.warehouse_id
),
total AS (
  SELECT SUM(eff_current_cost) AS total_usd_list FROM agg
),
scored AS (
  SELECT a.workspace_id, a.warehouse_id,
         a.billed_hours,
         COALESCE(q.queries, 0)                                            AS queries,
         a.current_unpriced_quantity, a.previous_unpriced_quantity,
         a.current_priced_quantity, a.previous_priced_quantity,
         a.eff_current_cost, a.eff_previous_cost,
         ROUND(a.eff_current_cost, 2)                                      AS usd_list,
         ROUND(a.eff_current_cost * 100.0 / NULLIF(t.total_usd_list, 0), 1) AS share_of_total_pct,
         ROUND(a.eff_previous_cost, 2)                                     AS prev_usd_list,
         ROUND(a.eff_current_cost - a.eff_previous_cost, 2)                AS change_usd_list,
         ROUND((a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100, 1) AS change_pct,
         ROUND(a.eff_current_cost / NULLIF(q.queries, 0) * 1000, 2)        AS usd_per_1000_queries,
         CASE
           WHEN (a.current_unpriced_quantity + a.previous_unpriced_quantity) > 0 THEN 'unpriced'
           WHEN (a.current_priced_quantity + a.previous_priced_quantity) = 0     THEN 'free'
           ELSE 'priced'
         END AS price_basis,
         CASE
           WHEN s.snapshot_start > dateadd(day, -(30 * 2), current_date()) THEN 'NOT_ASSESSED'
           WHEN a.current_cost IS NULL AND a.current_unpriced_quantity > 0           THEN 'NOT_ASSESSED'
           WHEN a.previous_cost IS NULL AND a.previous_unpriced_quantity > 0         THEN 'NOT_ASSESSED'
           -- Spend outside any warehouse is context for the total, not a warehouse to fix.
           WHEN a.warehouse_id IS NULL THEN 'OK'
           WHEN COALESCE(a.eff_current_cost, 0) < 20                    THEN 'OK'
           WHEN a.eff_previous_cost = 0                                             THEN 'CRITICAL'
           WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= 50
             THEN 'CRITICAL'
           WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= 25
             THEN 'WARN'
           ELSE 'OK'
         END AS status,
         CASE
           WHEN s.snapshot_start > dateadd(day, -(30 * 2), current_date()) THEN 'previous_window_not_covered'
           WHEN a.current_cost IS NULL AND a.current_unpriced_quantity > 0           THEN 'current_period_unpriced'
           WHEN a.previous_cost IS NULL AND a.previous_unpriced_quantity > 0         THEN 'previous_period_unpriced'
           ELSE NULL
         END AS not_assessed_reason
  FROM agg a
  CROSS JOIN snapshot s
  CROSS JOIN total t
  LEFT JOIN queries q ON q.workspace_id = a.workspace_id AND q.warehouse_id = a.warehouse_id
),
flagged AS (
  -- A real finding (WARN/CRITICAL) always keeps its own row - never pooled.
  SELECT workspace_id, warehouse_id, FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
         billed_hours, queries, usd_per_1000_queries, usd_list, share_of_total_pct, prev_usd_list,
         change_usd_list, change_pct, price_basis, status, not_assessed_reason
  FROM scored
  WHERE status IN ('WARN', 'CRITICAL') OR warehouse_id IS NULL   -- the not-on-a-warehouse row is never pooled
),
poolable_ranked AS (
  -- OK and NOT_ASSESSED warehouses are both poolable - on an account short of 2x 30 of
  -- billing history, every warehouse reads NOT_ASSESSED, and that list needs the same cap an OK
  -- list gets.
  -- ranked PER STATUS, never combined - a NOT_ASSESSED row's usd_list is always NULL (sorted
  -- last), so ranking both statuses together would push every NOT_ASSESSED row past 20 as
  -- soon as 20+ OK rows existed anywhere, regardless of how few NOT_ASSESSED rows there are.
  SELECT scored.*,
         ROW_NUMBER() OVER (PARTITION BY status ORDER BY usd_list DESC NULLS LAST, warehouse_id) AS rn
  FROM scored
  WHERE status IN ('OK', 'NOT_ASSESSED') AND warehouse_id IS NOT NULL
),
poolable_kept AS (
  -- The top 20 OK/NOT_ASSESSED warehouses by current spend, kept as their own row.
  SELECT workspace_id, warehouse_id, FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
         billed_hours, queries, usd_per_1000_queries, usd_list, share_of_total_pct, prev_usd_list,
         change_usd_list, change_pct, price_basis, status, not_assessed_reason
  FROM poolable_ranked
  WHERE rn <= 20
),
poolable_pooled_raw AS (
  -- Every remaining OK/NOT_ASSESSED warehouse beyond 20 - re-aggregated below into one
  -- is_other row per status (OK rows and NOT_ASSESSED rows are never combined into one row).
  SELECT * FROM poolable_ranked WHERE rn > 20
),
other_ok_row AS (
  SELECT
    CAST(NULL AS STRING)                                                  AS workspace_id,
    CAST(NULL AS STRING)                                                  AS warehouse_id,
    TRUE                                                                  AS is_other,
    COUNT(*)                                                              AS pooled_count,
    SUM(billed_hours)                                                     AS billed_hours,
    SUM(queries)                                                          AS queries,
    ROUND(SUM(eff_current_cost) / NULLIF(SUM(queries), 0) * 1000, 2)      AS usd_per_1000_queries,
    ROUND(SUM(eff_current_cost), 2)                                       AS usd_list,
    ROUND(SUM(eff_current_cost) * 100.0 / NULLIF(MAX(t.total_usd_list), 0), 1) AS share_of_total_pct,
    ROUND(SUM(eff_previous_cost), 2)                                      AS prev_usd_list,
    ROUND(SUM(eff_current_cost) - SUM(eff_previous_cost), 2)              AS change_usd_list,
    ROUND((SUM(eff_current_cost) - SUM(eff_previous_cost)) / NULLIF(SUM(eff_previous_cost), 0) * 100, 1) AS change_pct,
    CASE
      WHEN (SUM(current_unpriced_quantity) + SUM(previous_unpriced_quantity)) > 0 THEN 'unpriced'
      WHEN (SUM(current_priced_quantity) + SUM(previous_priced_quantity)) = 0     THEN 'free'
      ELSE 'priced'
    END AS price_basis,
    -- Every pooled row was already status=OK on its own - a rollup of small, healthy rows is never
    -- itself a finding, so this row always reads OK regardless of the combined dollars.
    'OK' AS status,
    CAST(NULL AS STRING) AS not_assessed_reason
  FROM poolable_pooled_raw
  CROSS JOIN total t
  WHERE status = 'OK'
),
other_not_assessed_row AS (
  SELECT
    CAST(NULL AS STRING)                                                  AS workspace_id,
    CAST(NULL AS STRING)                                                  AS warehouse_id,
    TRUE                                                                  AS is_other,
    COUNT(*)                                                              AS pooled_count,
    SUM(billed_hours)                                                     AS billed_hours,
    SUM(queries)                                                          AS queries,
    ROUND(SUM(eff_current_cost) / NULLIF(SUM(queries), 0) * 1000, 2)      AS usd_per_1000_queries,
    ROUND(SUM(eff_current_cost), 2)                                       AS usd_list,
    ROUND(SUM(eff_current_cost) * 100.0 / NULLIF(MAX(t.total_usd_list), 0), 1) AS share_of_total_pct,
    ROUND(SUM(eff_previous_cost), 2)                                      AS prev_usd_list,
    ROUND(SUM(eff_current_cost) - SUM(eff_previous_cost), 2)              AS change_usd_list,
    ROUND((SUM(eff_current_cost) - SUM(eff_previous_cost)) / NULLIF(SUM(eff_previous_cost), 0) * 100, 1) AS change_pct,
    CASE
      WHEN (SUM(current_unpriced_quantity) + SUM(previous_unpriced_quantity)) > 0 THEN 'unpriced'
      WHEN (SUM(current_priced_quantity) + SUM(previous_priced_quantity)) = 0     THEN 'free'
      ELSE 'priced'
    END AS price_basis,
    'NOT_ASSESSED' AS status,
    -- The pooled rows can carry different reasons; NULL here rather than pick one arbitrarily -
    -- raise 20 to see them individually.
    CAST(NULL AS STRING) AS not_assessed_reason
  FROM poolable_pooled_raw
  CROSS JOIN total t
  WHERE status = 'NOT_ASSESSED'
)
SELECT * FROM (
  SELECT * FROM flagged
  UNION ALL
  SELECT * FROM poolable_kept
  UNION ALL
  SELECT * FROM other_ok_row WHERE pooled_count > 0
  UNION ALL
  SELECT * FROM other_not_assessed_row WHERE pooled_count > 0
) u
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         is_other,
         change_usd_list DESC NULLS LAST,
         workspace_id, warehouse_id
) q
"""
CHECKS["cost_chargeback_by_cluster"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_chargeback_by_cluster.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_chargeback_by_cluster.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH snapshot AS (
  SELECT MIN(usage_date) AS snapshot_start FROM `system`.`billing`.`usage`
),
priced AS (
  SELECT u.workspace_id,
         CASE
           WHEN NOT (upper(u.usage_unit) = 'DBU' AND u.usage_metadata.cluster_id IS NOT NULL) THEN 'no_cluster'
           WHEN u.usage_metadata.dlt_pipeline_id IS NOT NULL THEN 'pipeline'
           WHEN u.usage_metadata.job_id IS NULL THEN 'all_purpose'
           ELSE 'job_cluster'
         END AS cluster_kind,
         CASE
           WHEN NOT (upper(u.usage_unit) = 'DBU' AND u.usage_metadata.cluster_id IS NOT NULL) THEN CAST(NULL AS STRING)
           WHEN u.usage_metadata.dlt_pipeline_id IS NOT NULL THEN u.usage_metadata.dlt_pipeline_id
           WHEN u.usage_metadata.job_id IS NULL THEN u.usage_metadata.cluster_id
           ELSE u.usage_metadata.job_id
         END AS entity_id,
         u.usage_date, u.sku_name,
         u.usage_quantity                    AS usage_quantity,
         u.usage_quantity * lp.list_rate     AS list_cost,
         lp.list_rate                        AS list_rate
  FROM `system`.`billing`.`usage` u
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
    FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
  ) lp
    ON u.sku_name   = lp.sku_name
   AND u.cloud      = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  -- no unit/id filter: usage with no cluster id (and any non-DBU unit) becomes the workspace's own
  -- 'no_cluster' row, so the rows add up to total spend
  WHERE u.usage_date >= dateadd(day, -(30 * 2), current_date())
    AND u.usage_date <  current_date()
),
raw_agg AS (
  SELECT workspace_id, cluster_kind, entity_id,
         SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date())
                  THEN list_cost END)                                         AS current_cost,
         SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date())
                  THEN list_cost END)                                         AS previous_cost,
         SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date())
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                             AS current_unpriced_quantity,
         SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date())
                   AND list_rate IS NOT NULL
                  THEN usage_quantity ELSE 0 END)                             AS current_priced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date())
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                             AS previous_unpriced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date())
                   AND list_rate IS NOT NULL
                  THEN usage_quantity ELSE 0 END)                             AS previous_priced_quantity
  FROM priced
  GROUP BY workspace_id, cluster_kind, entity_id
),
agg AS (
  SELECT *,
         CASE WHEN current_cost IS NULL AND current_unpriced_quantity = 0 THEN 0 ELSE current_cost END AS eff_current_cost,
         CASE WHEN previous_cost IS NULL AND previous_unpriced_quantity = 0 THEN 0 ELSE previous_cost END AS eff_previous_cost
  FROM raw_agg
),
clusters AS (
  -- clusters/jobs/pipelines/ws below join with IS NOT DISTINCT FROM on workspace_id (a real
  -- account never has a NULL workspace_id here; this only matters for an account-level billing
  -- row, where a plain "=" join to workspace_id would never match a NULL to a NULL).
  SELECT workspace_id, cluster_id, cluster_name, owned_by
  FROM `system`.`compute`.`clusters`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC) = 1
),
jobs AS (
  -- run_as is the principal id; run_as_user_name is the email or service principal name
  SELECT workspace_id, job_id, name AS job_name, COALESCE(run_as_user_name, run_as) AS run_as
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),
pipelines AS (
  SELECT workspace_id, pipeline_id, name AS pipeline_name, run_as
  FROM `system`.`lakeflow`.`pipelines`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC) = 1
),
ws AS (
  SELECT workspace_id, workspace_name FROM `system`.`access`.`workspaces_latest`
),
total AS (
  SELECT SUM(eff_current_cost) AS total_current_usd_list FROM agg
),
scored AS (
  SELECT a.workspace_id,
         ws.workspace_name,
         a.cluster_kind,
         a.entity_id,
         CASE a.cluster_kind
           WHEN 'all_purpose' THEN cl.cluster_name
           WHEN 'pipeline'    THEN pl.pipeline_name
           ELSE j.job_name
         END                                                                          AS name,
         CASE a.cluster_kind
           WHEN 'all_purpose' THEN cl.owned_by
           WHEN 'pipeline'    THEN pl.run_as
           ELSE j.run_as
         END                                                                          AS owner,
         a.current_unpriced_quantity, a.previous_unpriced_quantity,
         a.current_priced_quantity, a.previous_priced_quantity,
         a.eff_current_cost, a.eff_previous_cost,
         ROUND(a.eff_current_cost, 2)                                                 AS est_current_usd_list,
         ROUND(a.eff_current_cost * 100.0 / NULLIF(t.total_current_usd_list, 0), 1)   AS share_of_total_pct,
         ROUND(a.eff_previous_cost, 2)                                                AS est_previous_usd_list,
         ROUND(a.eff_current_cost - a.eff_previous_cost, 2)                           AS est_change_usd_list,
         ROUND((a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100, 1) AS change_pct,
         CASE
           WHEN (a.current_unpriced_quantity + a.previous_unpriced_quantity) > 0 THEN 'unpriced'
           WHEN (a.current_priced_quantity + a.previous_priced_quantity) = 0     THEN 'free'
           ELSE 'priced'
         END                                                                           AS price_basis,
         CASE
           WHEN s.snapshot_start > dateadd(day, -(30 * 2), current_date())
             THEN 'NOT_ASSESSED'
           WHEN a.current_cost IS NULL AND a.current_unpriced_quantity > 0
             THEN 'NOT_ASSESSED'
           WHEN a.previous_cost IS NULL AND a.previous_unpriced_quantity > 0
             THEN 'NOT_ASSESSED'
           -- Spend outside any cluster is context for the total, not a cluster to fix.
           WHEN a.cluster_kind = 'no_cluster' THEN 'OK'
           WHEN COALESCE(a.eff_current_cost, 0) < 20 THEN 'OK'
           WHEN a.eff_previous_cost = 0 THEN 'CRITICAL'
           WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= 50
             THEN 'CRITICAL'
           WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= 25
             THEN 'WARN'
           ELSE 'OK'
         END                                                                           AS status,
         CASE
           WHEN s.snapshot_start > dateadd(day, -(30 * 2), current_date())
             THEN 'previous_window_not_covered'
           WHEN a.current_cost IS NULL AND a.current_unpriced_quantity > 0
             THEN 'current_period_unpriced'
           WHEN a.previous_cost IS NULL AND a.previous_unpriced_quantity > 0
             THEN 'previous_period_unpriced'
           ELSE NULL
         END                                                                           AS not_assessed_reason
  FROM agg a
  LEFT JOIN clusters cl  ON a.cluster_kind = 'all_purpose' AND cl.workspace_id IS NOT DISTINCT FROM a.workspace_id AND cl.cluster_id   = a.entity_id
  LEFT JOIN jobs j       ON a.cluster_kind = 'job_cluster' AND j.workspace_id  IS NOT DISTINCT FROM a.workspace_id AND j.job_id       = a.entity_id
  LEFT JOIN pipelines pl ON a.cluster_kind = 'pipeline'    AND pl.workspace_id IS NOT DISTINCT FROM a.workspace_id AND pl.pipeline_id = a.entity_id
  LEFT JOIN ws           ON ws.workspace_id IS NOT DISTINCT FROM a.workspace_id
  CROSS JOIN snapshot s
  CROSS JOIN total t
),
flagged AS (
  -- Every non-OK row (WARN/CRITICAL/NOT_ASSESSED) always keeps its own row - never pooled.
  SELECT workspace_id, workspace_name, cluster_kind, entity_id, name, owner,
         FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
         est_current_usd_list, share_of_total_pct, est_previous_usd_list, est_change_usd_list,
         change_pct, price_basis, status, not_assessed_reason
  FROM scored
  WHERE status != 'OK' OR cluster_kind = 'no_cluster'   -- the no-cluster row is never pooled
),
ok_ranked AS (
  SELECT scored.*,
         ROW_NUMBER() OVER (ORDER BY est_current_usd_list DESC NULLS LAST, cluster_kind, entity_id) AS rn
  FROM scored
  WHERE status = 'OK' AND cluster_kind <> 'no_cluster'
),
ok_kept AS (
  -- The top 20 OK rows by current spend, kept as their own row.
  SELECT workspace_id, workspace_name, cluster_kind, entity_id, name, owner,
         FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
         est_current_usd_list, share_of_total_pct, est_previous_usd_list, est_change_usd_list,
         change_pct, price_basis, status, not_assessed_reason
  FROM ok_ranked
  WHERE rn <= 20
),
ok_pooled_raw AS (
  -- Every remaining OK row beyond 20 - re-aggregated into one is_other row below.
  SELECT * FROM ok_ranked WHERE rn > 20
),
other_row AS (
  SELECT
    CAST(NULL AS STRING) AS workspace_id,
    CAST(NULL AS STRING) AS workspace_name,
    CAST(NULL AS STRING) AS cluster_kind,
    CAST(NULL AS STRING) AS entity_id,
    CAST(NULL AS STRING) AS name,
    CAST(NULL AS STRING) AS owner,
    TRUE                  AS is_other,
    COUNT(*)              AS pooled_count,
    ROUND(SUM(eff_current_cost), 2)                                              AS est_current_usd_list,
    ROUND(SUM(eff_current_cost) * 100.0 / NULLIF(MAX(t.total_current_usd_list), 0), 1) AS share_of_total_pct,
    ROUND(SUM(eff_previous_cost), 2)                                             AS est_previous_usd_list,
    ROUND(SUM(eff_current_cost) - SUM(eff_previous_cost), 2)                     AS est_change_usd_list,
    ROUND((SUM(eff_current_cost) - SUM(eff_previous_cost)) / NULLIF(SUM(eff_previous_cost), 0) * 100, 1) AS change_pct,
    CASE
      WHEN (SUM(current_unpriced_quantity) + SUM(previous_unpriced_quantity)) > 0 THEN 'unpriced'
      WHEN (SUM(current_priced_quantity) + SUM(previous_priced_quantity)) = 0     THEN 'free'
      ELSE 'priced'
    END AS price_basis,
    -- Every pooled row was already status=OK on its own - a rollup of small, healthy rows is never
    -- itself a finding, so this row always reads OK regardless of the combined dollars.
    'OK' AS status,
    CAST(NULL AS STRING) AS not_assessed_reason
  FROM ok_pooled_raw
  CROSS JOIN total t
)
SELECT * FROM (
  SELECT * FROM flagged
  UNION ALL
  SELECT * FROM ok_kept
  UNION ALL
  SELECT * FROM other_row WHERE pooled_count > 0
) u
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         is_other,
         est_change_usd_list DESC NULLS LAST,
         workspace_id, cluster_kind, entity_id
) q
"""
CHECKS["cost_daily_spikes"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_daily_spikes.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_daily_spikes.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH daily_sku AS (
  SELECT u.workspace_id, u.billing_origin_product, u.usage_date, u.sku_name,
         SUM(u.usage_quantity * lp.list_rate) AS sku_cost,
         SUM(u.usage_quantity)                AS sku_quantity,
         MAX(lp.list_rate)                    AS list_rate
  FROM `system`.`billing`.`usage` u
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
    FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
  ) lp
    ON u.sku_name   = lp.sku_name
   AND u.cloud      = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_date >= dateadd(day, -(30 + 14), current_date())
    AND u.usage_date <  current_date()
  GROUP BY u.workspace_id, u.billing_origin_product, u.usage_date, u.sku_name
),
daily AS (
  SELECT workspace_id, billing_origin_product, usage_date,
         SUM(sku_cost) AS day_cost,
         SUM(CASE WHEN list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN sku_quantity ELSE 0 END)                          AS unpriced_quantity,
         SUM(CASE WHEN list_rate IS NOT NULL THEN sku_quantity ELSE 0 END) AS priced_quantity
  FROM daily_sku
  GROUP BY workspace_id, billing_origin_product, usage_date
),
candidates AS (
  SELECT * FROM daily WHERE usage_date >= dateadd(day, -30, current_date())
),
baseline_pairs AS (
  -- every earlier day, for the same workspace/product, that falls in this candidate day's own
  -- trailing 14 window (never including the candidate day itself). Null-safe keys
  -- (IS NOT DISTINCT FROM) so account-level usage (workspace_id NULL, PLAN.md 5.7) finds its own
  -- account-level baseline instead of never matching (plain `=` never matches NULL = NULL).
  SELECT c.workspace_id, c.billing_origin_product, c.usage_date AS candidate_date,
         b.day_cost AS baseline_cost
  FROM candidates c
  JOIN daily b
    ON  b.workspace_id            IS NOT DISTINCT FROM c.workspace_id
    AND b.billing_origin_product  IS NOT DISTINCT FROM c.billing_origin_product
    AND b.usage_date >= dateadd(day, -14, c.usage_date)
    AND b.usage_date <  c.usage_date
),
baseline AS (
  SELECT workspace_id, billing_origin_product, candidate_date,
         percentile(baseline_cost, 0.5) AS baseline_median_cost,
         COUNT(*)                       AS baseline_days_seen
  FROM baseline_pairs
  GROUP BY workspace_id, billing_origin_product, candidate_date
),
ranked_sku AS (
  SELECT workspace_id, billing_origin_product, usage_date, sku_name, sku_cost
  FROM daily_sku
  WHERE usage_date >= dateadd(day, -30, current_date())
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, billing_origin_product, usage_date ORDER BY sku_cost DESC, sku_name
  ) <= 3
),
drivers AS (
  SELECT workspace_id, billing_origin_product, usage_date,
         array_join(collect_set(
           CASE
             WHEN sku_cost IS NULL THEN concat(sku_name,
               CASE WHEN upper(sku_name) LIKE '%FREE_USAGE%' THEN ' (free)' ELSE ' (no list price)' END)
             ELSE concat(sku_name, ' ($', CAST(CAST(ROUND(sku_cost, 0) AS BIGINT) AS STRING), ')')
           END
         ), ', ') AS top_skus
  FROM ranked_sku
  GROUP BY workspace_id, billing_origin_product, usage_date
)
SELECT c.workspace_id,
       c.billing_origin_product,
       c.usage_date,
       ROUND(c.day_cost, 2)              AS est_day_usd_list,
       ROUND(bl.baseline_median_cost, 2) AS est_baseline_usd_list,
       COALESCE(bl.baseline_days_seen, 0) AS baseline_days_seen,
       ROUND(c.day_cost / NULLIF(bl.baseline_median_cost, 0), 2) AS spike_ratio_actual,
       d.top_skus AS top_skus,
       -- price_basis: a real $0 (free-usage SKUs) vs a pricing-coverage gap (understates the day).
       CASE
         WHEN c.unpriced_quantity > 0 THEN 'unpriced'
         WHEN c.priced_quantity = 0   THEN 'free'
         ELSE 'priced'
       END AS price_basis,
       -- the $ floor is checked FIRST (a day that can never clear it is not worth flagging at
       -- all, priced or not), then the baseline-size cases, then the ratio itself - see caveats.
       CASE
         WHEN COALESCE(c.day_cost, 0) < 50                                  THEN 'OK'
         WHEN bl.baseline_days_seen IS NULL OR bl.baseline_days_seen < 3 THEN 'NOT_ASSESSED'
         WHEN c.day_cost >= COALESCE(bl.baseline_median_cost, 0) * 2.0          THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN COALESCE(c.day_cost, 0) < 50                    THEN NULL
         WHEN bl.baseline_days_seen IS NULL OR bl.baseline_days_seen = 0  THEN 'no_baseline_history'
         WHEN bl.baseline_days_seen < 3                 THEN 'short_baseline'
         ELSE NULL
       END AS not_assessed_reason
FROM candidates c
LEFT JOIN baseline bl
  ON  bl.workspace_id           IS NOT DISTINCT FROM c.workspace_id
  AND bl.billing_origin_product IS NOT DISTINCT FROM c.billing_origin_product
  AND bl.candidate_date         = c.usage_date
LEFT JOIN drivers d
  ON  d.workspace_id           IS NOT DISTINCT FROM c.workspace_id
  AND d.billing_origin_product IS NOT DISTINCT FROM c.billing_origin_product
  AND d.usage_date             = c.usage_date
WHERE c.day_cost >= 50
  AND (
        (bl.baseline_days_seen IS NULL OR bl.baseline_days_seen < 3)
     OR c.day_cost >= COALESCE(bl.baseline_median_cost, 0) * 2.0
      )
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         est_day_usd_list DESC,
         workspace_id, billing_origin_product, usage_date
) q
"""
CHECKS["cost_by_hour_of_day"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_by_hour_of_day.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_by_hour_of_day.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective (post-promotion) list price - DEC-66.1
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
usage_priced AS (
  SELECT u.workspace_id, u.billing_origin_product,
         (datediff(DATE(u.usage_start_time), DATE '2023-01-02') % 7) AS weekday_num,
         hour(u.usage_start_time)             AS hour_of_day,
         u.usage_quantity * lp.list_rate      AS usd,
         CASE WHEN lp.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
              THEN u.usage_quantity ELSE 0 END AS unpriced_quantity,
         CASE WHEN lp.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END AS priced_quantity
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price lp
    ON  u.sku_name   = lp.sku_name
    AND u.cloud      = lp.cloud
    AND u.usage_unit = lp.usage_unit
    AND u.usage_end_time >= lp.price_start_time
    AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_start_time >= dateadd(day, -30, current_date())
    AND u.usage_start_time < current_date()
),
spend_grid AS (
  SELECT workspace_id, weekday_num, hour_of_day, billing_origin_product,
         SUM(usd)                AS usd_list,
         SUM(unpriced_quantity)  AS unpriced_quantity,
         SUM(priced_quantity)    AS priced_quantity
  FROM usage_priced
  GROUP BY workspace_id, weekday_num, hour_of_day, billing_origin_product
),
hour_totals AS (
  -- this workspace's whole spend in this hour-of-day, across every weekday and product
  SELECT workspace_id, hour_of_day, SUM(usd_list) AS hour_usd_list
  FROM spend_grid
  GROUP BY workspace_id, hour_of_day
),
workspace_totals AS (
  -- this workspace's whole window spend, every weekday, hour and product
  SELECT workspace_id, SUM(usd_list) AS window_usd_list
  FROM spend_grid
  GROUP BY workspace_id
),
job_runs AS (
  SELECT workspace_id,
         (datediff(DATE(period_start_time), DATE '2023-01-02') % 7) AS weekday_num,
         hour(period_start_time)                                     AS hour_of_day,
         COUNT(DISTINCT CONCAT(job_id, '::', run_id))                AS job_runs_active
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
    AND period_start_time < current_date()
  GROUP BY workspace_id, weekday_num, hour_of_day
),
query_counts AS (
  SELECT workspace_id,
         (datediff(DATE(start_time), DATE '2023-01-02') % 7) AS weekday_num,
         hour(start_time)                                     AS hour_of_day,
         COUNT(*)                                              AS queries_started
  FROM `system`.`query`.`history`
  WHERE start_time >= dateadd(day, -30, current_date())
    AND start_time < current_date()
  GROUP BY workspace_id, weekday_num, hour_of_day
)
SELECT g.workspace_id,
       CASE g.weekday_num
         WHEN 0 THEN 'Monday'    WHEN 1 THEN 'Tuesday' WHEN 2 THEN 'Wednesday'
         WHEN 3 THEN 'Thursday'  WHEN 4 THEN 'Friday'  WHEN 5 THEN 'Saturday'
         ELSE 'Sunday'
       END                                                  AS weekday,
       g.weekday_num,
       g.hour_of_day,
       g.billing_origin_product,
       ROUND(g.usd_list, 2)                                 AS usd_list,
       CASE
         WHEN g.unpriced_quantity > 0 THEN 'unpriced'
         WHEN g.priced_quantity = 0   THEN 'free'
         ELSE 'priced'
       END                                                  AS price_basis,
       COALESCE(jr.job_runs_active, 0)                      AS job_runs_active,
       COALESCE(qc.queries_started, 0)                      AS queries_started,
       ROUND(ht.hour_usd_list, 2)                           AS hour_usd_list,
       ROUND(wt.window_usd_list, 2)                         AS window_usd_list,
       ROUND(100.0 * ht.hour_usd_list / NULLIF(wt.window_usd_list, 0), 1) AS share_of_window_spend,
       CASE
         WHEN 100.0 * ht.hour_usd_list / NULLIF(wt.window_usd_list, 0) >= 25 THEN 'CRITICAL'
         WHEN 100.0 * ht.hour_usd_list / NULLIF(wt.window_usd_list, 0) >= 15 THEN 'WARN'
         ELSE 'OK'
       END                                                  AS status
FROM spend_grid g
LEFT JOIN hour_totals ht
  ON  ht.workspace_id IS NOT DISTINCT FROM g.workspace_id AND ht.hour_of_day = g.hour_of_day
LEFT JOIN workspace_totals wt
  ON  wt.workspace_id IS NOT DISTINCT FROM g.workspace_id
LEFT JOIN job_runs jr
  ON  jr.workspace_id IS NOT DISTINCT FROM g.workspace_id
  AND jr.weekday_num = g.weekday_num AND jr.hour_of_day = g.hour_of_day
LEFT JOIN query_counts qc
  ON  qc.workspace_id IS NOT DISTINCT FROM g.workspace_id
  AND qc.weekday_num = g.weekday_num AND qc.hour_of_day = g.hour_of_day
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
         g.workspace_id, g.weekday_num, g.hour_of_day, g.billing_origin_product
) q
"""
CHECKS["cost_totals_by_sku_day"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_totals_by_sku_day.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_totals_by_sku_day.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT usage_date, cloud, workspace_id, sku_name, billing_origin_product, usage_type, usage_unit,
       product_features.is_serverless AS is_serverless,
       SUM(usage_quantity) AS net_usage_quantity,
       SUM(CASE WHEN record_type = 'ORIGINAL'    THEN usage_quantity ELSE 0 END) AS original_usage_quantity,
       SUM(CASE WHEN record_type = 'RETRACTION'  THEN usage_quantity ELSE 0 END) AS retraction_usage_quantity,
       SUM(CASE WHEN record_type = 'RESTATEMENT' THEN usage_quantity ELSE 0 END) AS restatement_usage_quantity,
       COUNT(*) AS record_count
FROM `system`.`billing`.`usage`
WHERE usage_date >= dateadd(day, -30, current_date())
  AND usage_date < current_date()
GROUP BY usage_date, cloud, workspace_id, sku_name, billing_origin_product, usage_type, usage_unit,
         product_features.is_serverless
ORDER BY usage_date DESC, workspace_id, sku_name
) q
"""
CHECKS["cost_workspace_names"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_workspace_names.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_workspace_names.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT workspace_id, workspace_name, status
FROM `system`.`access`.`workspaces_latest`
ORDER BY workspace_name, workspace_id
) q
"""
CHECKS["cost_by_billing_origin_product"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_by_billing_origin_product.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_by_billing_origin_product.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT billing_origin_product, usage_unit, cloud, workspace_id,
       SUM(usage_quantity) AS net_usage_quantity,
       COUNT(*)            AS record_count
FROM `system`.`billing`.`usage`
WHERE usage_date >= dateadd(day, -30, current_date())
  AND usage_date < current_date()
GROUP BY billing_origin_product, usage_unit, cloud, workspace_id
ORDER BY billing_origin_product, usage_unit, cloud, workspace_id
) q
"""
CHECKS["cost_sku_trend_12m"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_sku_trend_12m.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_sku_trend_12m.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
WITH bounds AS (
  SELECT
    CAST(date_trunc('MONTH', current_date()) AS DATE)                          AS cur_month_start,
    CAST(dateadd(month, -3,  date_trunc('MONTH', current_date())) AS DATE)     AS last3_start,
    CAST(dateadd(month, -6,  date_trunc('MONTH', current_date())) AS DATE)     AS prior3_start,
    -- billing keeps 365 days, so the month 12 back is already cut short: 11 full months
    CAST(dateadd(month, -8,  date_trunc('MONTH', current_date())) AS DATE)     AS mid3_end,
    CAST(dateadd(month, -11, date_trunc('MONTH', current_date())) AS DATE)     AS window_start
),
priced AS (
  SELECT u.workspace_id, u.sku_name, u.billing_origin_product, u.usage_date,
         u.usage_quantity                    AS usage_quantity,
         u.usage_quantity * lp.list_rate     AS list_cost,
         lp.list_rate                        AS list_rate
  FROM `system`.`billing`.`usage` u
  CROSS JOIN bounds b
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
    FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
  ) lp
    ON u.sku_name   = lp.sku_name
   AND u.cloud      = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_date >= b.window_start
    AND u.usage_date <  current_date()
),
monthly AS (
  SELECT
    p.workspace_id,
    p.sku_name,
    MAX(p.billing_origin_product)                   AS billing_origin_product,
    CAST(date_trunc('MONTH', p.usage_date) AS DATE)  AS month_start,
    ROUND(SUM(p.list_cost), 2)                       AS net_list_cost_usd,
    CASE
      WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(p.sku_name) NOT LIKE '%FREE_USAGE%'
                    THEN p.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
      WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN p.usage_quantity ELSE 0 END) = 0 THEN 'free'
      ELSE 'priced'
    END AS price_basis
  FROM priced p
  GROUP BY p.workspace_id, p.sku_name, CAST(date_trunc('MONTH', p.usage_date) AS DATE)
),
sku_bucket AS (
  -- per-workspace+SKU dollars in each 3-month bucket, from the raw priced rows (never from
  -- `monthly`) so a bucket with zero usage in every one of its 3 months still resolves to a real
  -- 0.0, never a NULL average over fewer months. FROM is the full `priced` set (the current
  -- partial month's own rows included, not filtered out here) so a brand-new workspace+SKU whose
  -- only usage is this month still gets a group and is not silently dropped by the later JOIN to
  -- `monthly` - each bucket's own CASE bounds it to full months only (the current month never
  -- satisfies any of the three).
  SELECT
    p.workspace_id,
    p.sku_name,
    ROUND(SUM(CASE WHEN p.usage_date >= b.last3_start AND p.usage_date < b.cur_month_start
                   THEN p.list_cost ELSE 0 END) / 3.0, 2)                        AS usd_last_3m_avg,
    ROUND(SUM(CASE WHEN p.usage_date >= b.prior3_start AND p.usage_date < b.last3_start
                   THEN p.list_cost ELSE 0 END) / 3.0, 2)                        AS usd_prior_3m_avg,
    ROUND(SUM(CASE WHEN p.usage_date >= b.window_start AND p.usage_date < b.mid3_end
                   THEN p.list_cost ELSE 0 END) / 3.0, 2)                        AS usd_first_3m_avg
  FROM priced p
  CROSS JOIN bounds b
  GROUP BY p.workspace_id, p.sku_name
),
sku_metrics AS (
  SELECT
    sb.workspace_id,
    sb.sku_name,
    sb.usd_last_3m_avg,
    sb.usd_prior_3m_avg,
    sb.usd_first_3m_avg,
    ROUND((sb.usd_last_3m_avg - sb.usd_prior_3m_avg) / NULLIF(sb.usd_prior_3m_avg, 0) * 100, 1) AS growth_3m_pct,
    ROUND((sb.usd_last_3m_avg - sb.usd_first_3m_avg) / NULLIF(sb.usd_first_3m_avg, 0) * 100, 1)  AS growth_12m_pct,
    -- share is within the SKU's own workspace (or within the NULL account-level group), never
    -- blended with another workspace's dollars.
    ROUND(100.0 * sb.usd_last_3m_avg
          / NULLIF(SUM(sb.usd_last_3m_avg) OVER (PARTITION BY sb.workspace_id), 0), 1)           AS share_of_total_last_3m,
    (sb.usd_first_3m_avg = 0)                                                                     AS new_this_year
  FROM sku_bucket sb
)
SELECT
    m.workspace_id,
    m.sku_name,
    m.billing_origin_product,
    CASE m.billing_origin_product
      WHEN 'ALL_PURPOSE'     THEN 'All-purpose compute'
      WHEN 'JOBS'            THEN 'Jobs'
      WHEN 'SQL'             THEN 'SQL warehouses'
      WHEN 'DLT'             THEN 'Lakeflow Declarative Pipelines'
      WHEN 'MODEL_SERVING'   THEN 'Model serving'
      WHEN 'VECTOR_SEARCH'   THEN 'Vector search'
      WHEN 'DEFAULT_STORAGE' THEN 'Storage'
      WHEN 'STORAGE'         THEN 'Storage'
      ELSE m.billing_origin_product
    END                                                       AS product_label,
    m.month_start,
    (m.month_start = b.cur_month_start)                       AS is_partial_month,
    m.net_list_cost_usd,
    m.price_basis,
    sm.usd_last_3m_avg,
    sm.usd_prior_3m_avg,
    sm.growth_3m_pct,
    sm.usd_first_3m_avg,
    sm.growth_12m_pct,
    sm.share_of_total_last_3m,
    sm.new_this_year,
    CASE
      -- spend from nothing (prior 3 months $0) is growth past any band
      WHEN (sm.growth_3m_pct >= 100 OR COALESCE(sm.usd_prior_3m_avg, 0) = 0)
           AND sm.usd_last_3m_avg >= 1000 THEN 'CRITICAL'
      WHEN (sm.growth_3m_pct >= 50 OR COALESCE(sm.usd_prior_3m_avg, 0) = 0)
           AND sm.usd_last_3m_avg >= 500  THEN 'WARN'
      WHEN sm.new_this_year AND sm.usd_last_3m_avg >= 500                   THEN 'WARN'
      ELSE 'OK'
    END AS status
FROM monthly m
CROSS JOIN bounds b
JOIN sku_metrics sm
  ON sm.sku_name = m.sku_name
 AND sm.workspace_id IS NOT DISTINCT FROM m.workspace_id
ORDER BY CASE
           WHEN (sm.growth_3m_pct >= 100 OR COALESCE(sm.usd_prior_3m_avg, 0) = 0)
                AND sm.usd_last_3m_avg >= 1000 THEN 0
           WHEN (sm.growth_3m_pct >= 50 OR COALESCE(sm.usd_prior_3m_avg, 0) = 0)
                AND sm.usd_last_3m_avg >= 500  THEN 1
           WHEN sm.new_this_year AND sm.usd_last_3m_avg >= 500                   THEN 1
           ELSE 2
         END,
         sm.usd_last_3m_avg DESC,
         m.workspace_id,
         m.sku_name,
         m.month_start DESC
) q
"""
CHECKS["cost_chargeback_by_allocation_tag"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_chargeback_by_allocation_tag.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_chargeback_by_allocation_tag.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH priced_usage AS (
  -- Every usage row in the window, priced at the effective list price (DEC-66.1, the exact join
  -- T-69A settled on), carrying its own custom_tags map and sku_name for the price_basis check
  -- below. One row per system.billing.usage row (record_id is unique per row).
  SELECT u.record_id, u.workspace_id, u.usage_unit, u.usage_quantity, u.sku_name, u.custom_tags,
         lp.list_rate,
         u.usage_quantity * lp.list_rate AS list_cost
  FROM `system`.`billing`.`usage` u
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
    FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
  ) lp
    ON u.sku_name   = lp.sku_name
   AND u.cloud      = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_date >= dateadd(day, -30, current_date())
    AND u.usage_date < current_date()
),
tag_pairs AS (
  -- one row per (usage row, custom_tags key/value pair); OUTER so a usage row with no tags at
  -- all still appears once with tag_key/tag_value NULL, same explode cost_chargeback_by_tag uses.
  SELECT p.record_id, p.workspace_id, p.usage_unit, p.usage_quantity, p.sku_name, p.list_rate,
         p.list_cost, t.tag_key, t.tag_value
  FROM priced_usage p
       LATERAL VIEW OUTER explode(p.custom_tags) t AS tag_key, tag_value
),
ranked AS (
  -- DEC-60 rule 4 normalization. A key earlier in the allocation_keys list outranks a later one when a
  -- row carries both; a row whose tag_key matches none (or has no tags at all) ranks NULL.
  SELECT record_id, workspace_id, usage_unit, usage_quantity, sku_name, list_rate, list_cost,
         tag_key, tag_value, norm_key,
         -- the key list gets the same normalization, so "cost_center, team" matches too
         NULLIF(instr(concat(',', lower(replace(replace(replace('costcenter,team', ' ', ''), '-', ''), '_', '')), ','),
                      concat(',', norm_key, ',')), 0) AS key_rank
  FROM (
    SELECT tag_pairs.*, lower(replace(replace(replace(tag_key, ' ', ''), '-', ''), '_', '')) AS norm_key
    FROM tag_pairs
  ) n
),
picked AS (
  -- ONE allocation identity per usage row: the highest-priority matching key's own value, or
  -- NULL (allocation_key NULL too) when the row matches neither key at all -- even when it
  -- carries OTHER tags (env, owner, ...), which is deliberately different from
  -- cost_chargeback_by_tag's "no tags at all" reading of untagged.
  SELECT record_id, workspace_id, usage_unit, usage_quantity, sku_name, list_rate, list_cost,
         CASE WHEN key_rank IS NULL THEN NULL WHEN norm_key = 'costcenter' THEN 'cost_center' ELSE norm_key END AS allocation_key,
         CASE WHEN key_rank IS NOT NULL THEN tag_value END              AS allocation_value
  FROM ranked
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY record_id
    ORDER BY CASE WHEN key_rank IS NULL THEN 1 ELSE 0 END, key_rank
  ) = 1
),
agg AS (
  SELECT workspace_id, usage_unit, allocation_key, allocation_value,
         SUM(usage_quantity)                                                 AS net_usage_quantity,
         SUM(list_cost)                                                      AS net_list_cost_usd,
         SUM(CASE WHEN list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                            AS unpriced_quantity,
         SUM(CASE WHEN list_rate IS NOT NULL THEN usage_quantity ELSE 0 END) AS priced_quantity
  FROM picked
  GROUP BY workspace_id, usage_unit, allocation_key, allocation_value
),
unit_totals AS (
  -- this workspace + usage_unit's own total priced dollars across every allocation group
  -- (matched and missing) -- never summed across usage_unit (DEC-24 / this query's own contract).
  SELECT workspace_id, usage_unit, SUM(net_list_cost_usd) AS unit_total_cost_usd
  FROM agg
  GROUP BY workspace_id, usage_unit
)
SELECT a.workspace_id,
       a.usage_unit,
       a.allocation_key,
       a.allocation_value,
       (a.allocation_value IS NULL)                                             AS is_missing_allocation_key,
       ROUND(a.net_usage_quantity, 4)                                           AS net_usage_quantity,
       ROUND(a.net_list_cost_usd, 2)                                            AS net_list_cost_usd,
       ROUND(
         CASE WHEN a.unpriced_quantity = 0 AND a.priced_quantity = 0 THEN 0 ELSE a.net_list_cost_usd END
         * 100.0 / NULLIF(t.unit_total_cost_usd, 0), 1
       )                                                                         AS share_of_unit_pct,
       CASE
         WHEN a.unpriced_quantity > 0 THEN 'unpriced'
         WHEN a.priced_quantity = 0   THEN 'free'
         ELSE 'priced'
       END AS price_basis,
       CASE
         WHEN a.allocation_value IS NOT NULL THEN 'OK'
         WHEN a.unpriced_quantity = 0 AND a.priced_quantity = 0           THEN 'OK'
         WHEN a.net_list_cost_usd IS NULL
              OR t.unit_total_cost_usd IS NULL
              OR t.unit_total_cost_usd = 0                                THEN 'NOT_ASSESSED'
         WHEN (a.net_list_cost_usd * 100.0 / t.unit_total_cost_usd) >= 50
                                                                           THEN 'CRITICAL'
         WHEN (a.net_list_cost_usd * 100.0 / t.unit_total_cost_usd) >= 20
                                                                           THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN a.allocation_value IS NOT NULL THEN NULL
         WHEN a.unpriced_quantity = 0 AND a.priced_quantity = 0           THEN NULL
         WHEN a.net_list_cost_usd IS NULL THEN 'missing_allocation_spend_unpriced'
         WHEN t.unit_total_cost_usd IS NULL OR t.unit_total_cost_usd = 0
                                              THEN 'no_priced_spend_in_window'
         ELSE NULL
       END AS not_assessed_reason
FROM agg a
JOIN unit_totals t
  ON t.workspace_id IS NOT DISTINCT FROM a.workspace_id AND t.usage_unit = a.usage_unit
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         a.workspace_id, a.usage_unit, is_missing_allocation_key DESC, a.net_list_cost_usd DESC
) q
"""
CHECKS["cost_chargeback_by_tag"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_chargeback_by_tag.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_chargeback_by_tag.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT usage_date, cloud, workspace_id, billing_origin_product, tag_key, tag_value,
       SUM(usage_quantity) AS net_usage_quantity,
       COUNT(*) AS record_count
FROM `system`.`billing`.`usage`
     LATERAL VIEW OUTER explode(custom_tags) t AS tag_key, tag_value
WHERE usage_date >= dateadd(day, -30, current_date())
  AND usage_date < current_date()
  AND upper(usage_unit) = 'DBU'   -- DSU, GB and HOUR rows would be added in as if they were DBUs
GROUP BY usage_date, cloud, workspace_id, billing_origin_product, tag_key, tag_value
ORDER BY usage_date DESC, workspace_id, tag_key, tag_value
) q
"""
CHECKS["cost_chargeback_by_identity"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_chargeback_by_identity.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_chargeback_by_identity.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT usage_date, cloud, workspace_id, billing_origin_product,
       CASE
         WHEN identity_metadata.run_as IS NULL OR identity_metadata.run_as = '__REDACTED__' THEN 'unknown'
         WHEN identity_metadata.run_as LIKE '%@%' THEN 'user'
         ELSE 'service_principal'
       END                          AS identity_type,
       identity_metadata.run_as                          AS identity_run_as,
       identity_metadata.owned_by                          AS identity_owned_by,
       identity_metadata.created_by                          AS identity_created_by,
       SUM(usage_quantity) AS net_usage_quantity
FROM `system`.`billing`.`usage`
WHERE usage_date >= dateadd(day, -30, current_date())
  AND usage_date < current_date()
  AND usage_unit = 'DBU'
GROUP BY usage_date, cloud, workspace_id, billing_origin_product,
         CASE
           WHEN identity_metadata.run_as IS NULL OR identity_metadata.run_as = '__REDACTED__' THEN 'unknown'
           WHEN identity_metadata.run_as LIKE '%@%' THEN 'user'
           ELSE 'service_principal'
         END,
         identity_metadata.run_as, identity_metadata.owned_by, identity_metadata.created_by
ORDER BY usage_date DESC, workspace_id, identity_type
) q
"""
CHECKS["cost_chargeback_by_workspace"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_chargeback_by_workspace.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_chargeback_by_workspace.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH snapshot AS (
  SELECT MIN(usage_date) AS snapshot_start
  FROM `system`.`billing`.`usage`
),
priced AS (
  -- ws_key normalizes NULL (account-level) to a real string so it is a safe group key like any
  -- other value; workspace_id itself is carried through unchanged for display.
  SELECT COALESCE(u.workspace_id, '__account_level__') AS ws_key,
         u.workspace_id, u.usage_date, u.sku_name,
         u.usage_quantity                AS usage_quantity,
         u.usage_quantity * lp.list_rate AS list_cost,
         lp.list_rate                    AS list_rate
  FROM `system`.`billing`.`usage` u
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
    FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
  ) lp
    ON u.sku_name   = lp.sku_name
   AND u.cloud      = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_date >= dateadd(day, -(30 * 2), current_date())
    AND u.usage_date <  current_date()
),
raw_agg AS (
  SELECT ws_key, MAX(workspace_id) AS workspace_id,
         SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date())
                  THEN list_cost END)                                        AS current_cost,
         SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date())
                  THEN list_cost END)                                        AS previous_cost,
         SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date())
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                            AS current_unpriced_quantity,
         SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date())
                   AND list_rate IS NOT NULL
                  THEN usage_quantity ELSE 0 END)                            AS current_priced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date())
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                            AS previous_unpriced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date())
                   AND list_rate IS NOT NULL
                  THEN usage_quantity ELSE 0 END)                            AS previous_priced_quantity
  FROM priced
  GROUP BY ws_key
),
agg AS (
  SELECT *,
         CASE WHEN current_cost IS NULL AND current_unpriced_quantity = 0 THEN 0 ELSE current_cost END AS eff_current_cost,
         CASE WHEN previous_cost IS NULL AND previous_unpriced_quantity = 0 THEN 0 ELSE previous_cost END AS eff_previous_cost
  FROM raw_agg
),
total AS (
  SELECT SUM(eff_current_cost) AS total_usd_list FROM agg
)
SELECT a.workspace_id,
       ROUND(a.eff_current_cost, 2)                                      AS usd_list,
       ROUND(a.eff_current_cost * 100.0 / NULLIF(t.total_usd_list, 0), 1) AS share_of_total_pct,
       ROUND(a.eff_previous_cost, 2)                                     AS prev_usd_list,
       ROUND(a.eff_current_cost - a.eff_previous_cost, 2)                AS change_usd_list,
       ROUND((a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100, 1) AS change_pct,
       CASE
         WHEN (a.current_unpriced_quantity + a.previous_unpriced_quantity) > 0 THEN 'unpriced'
         WHEN (a.current_priced_quantity + a.previous_priced_quantity) = 0     THEN 'free'
         ELSE 'priced'
       END AS price_basis,
       CASE
         WHEN s.snapshot_start > dateadd(day, -(30 * 2), current_date()) THEN 'NOT_ASSESSED'
         WHEN a.current_cost IS NULL AND a.current_unpriced_quantity > 0           THEN 'NOT_ASSESSED'
         WHEN a.previous_cost IS NULL AND a.previous_unpriced_quantity > 0         THEN 'NOT_ASSESSED'
         WHEN COALESCE(a.eff_current_cost, 0) < 20                    THEN 'OK'
         WHEN a.eff_previous_cost = 0                                             THEN 'CRITICAL'
         WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= 50
           THEN 'CRITICAL'
         WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= 25
           THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN s.snapshot_start > dateadd(day, -(30 * 2), current_date()) THEN 'previous_window_not_covered'
         WHEN a.current_cost IS NULL AND a.current_unpriced_quantity > 0           THEN 'current_period_unpriced'
         WHEN a.previous_cost IS NULL AND a.previous_unpriced_quantity > 0         THEN 'previous_period_unpriced'
         ELSE NULL
       END AS not_assessed_reason
FROM agg a
CROSS JOIN snapshot s
CROSS JOIN total t
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         change_usd_list DESC NULLS LAST,
         a.workspace_id
) q
"""
CHECKS["cost_chargeback_by_service"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_chargeback_by_service.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_chargeback_by_service.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH snapshot AS (
  SELECT MIN(usage_date) AS snapshot_start
  FROM `system`.`billing`.`usage`
),
priced AS (
  -- bop_key normalizes NULL (unattributed) to a real string so it is a safe group key like any
  -- other value; billing_origin_product itself is carried through unchanged for display.
  SELECT COALESCE(u.billing_origin_product, '__unattributed__') AS bop_key,
         u.billing_origin_product, u.workspace_id, u.usage_date, u.sku_name,
         u.usage_quantity                AS usage_quantity,
         u.usage_quantity * lp.list_rate AS list_cost,
         lp.list_rate                    AS list_rate
  FROM `system`.`billing`.`usage` u
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
    FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
  ) lp
    ON u.sku_name   = lp.sku_name
   AND u.cloud      = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_date >= dateadd(day, -(30 * 2), current_date())
    AND u.usage_date <  current_date()
),
raw_agg AS (
  SELECT workspace_id, bop_key, MAX(billing_origin_product) AS billing_origin_product,
         SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date())
                  THEN list_cost END)                                        AS current_cost,
         SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date())
                  THEN list_cost END)                                        AS previous_cost,
         SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date())
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                            AS current_unpriced_quantity,
         SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date())
                   AND list_rate IS NOT NULL
                  THEN usage_quantity ELSE 0 END)                            AS current_priced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date())
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                            AS previous_unpriced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date())
                   AND list_rate IS NOT NULL
                  THEN usage_quantity ELSE 0 END)                            AS previous_priced_quantity
  FROM priced
  GROUP BY workspace_id, bop_key
),
agg AS (
  SELECT *,
         CASE WHEN current_cost IS NULL AND current_unpriced_quantity = 0 THEN 0 ELSE current_cost END AS eff_current_cost,
         CASE WHEN previous_cost IS NULL AND previous_unpriced_quantity = 0 THEN 0 ELSE previous_cost END AS eff_previous_cost
  FROM raw_agg
),
total AS (
  SELECT SUM(eff_current_cost) AS total_usd_list FROM agg
)
SELECT a.workspace_id,
       a.billing_origin_product,
       CASE
         WHEN a.bop_key = '__unattributed__' THEN 'Unattributed'
         ELSE CASE upper(a.billing_origin_product)
           WHEN 'ALL_PURPOSE'          THEN 'All-purpose compute'
           WHEN 'JOBS'                 THEN 'Jobs'
           WHEN 'SQL'                  THEN 'SQL warehouses'
           WHEN 'DLT'                  THEN 'Lakeflow Declarative Pipelines'
           WHEN 'MODEL_SERVING'        THEN 'Model serving'
           WHEN 'VECTOR_SEARCH'        THEN 'Vector search'
           WHEN 'LAKEHOUSE_MONITORING' THEN 'Lakehouse monitoring'
           WHEN 'FEATURE_SERVING'      THEN 'Feature serving'
           WHEN 'MLFLOW'               THEN 'MLflow'
           WHEN 'APPS'                 THEN 'Apps'
           WHEN 'GENIE'                THEN 'Genie'
           WHEN 'CLEAN_ROOMS'          THEN 'Clean rooms'
           WHEN 'DEFAULT_STORAGE'      THEN 'Storage'
           WHEN 'STORAGE'              THEN 'Storage'
           WHEN 'NETWORKING'           THEN 'Networking'
           ELSE replace(lower(a.billing_origin_product), '_', ' ')
         END
       END                                                                AS service_label,
       ROUND(a.eff_current_cost, 2)                                      AS usd_list,
       ROUND(a.eff_current_cost * 100.0 / NULLIF(t.total_usd_list, 0), 1) AS share_of_total_pct,
       ROUND(a.eff_previous_cost, 2)                                     AS prev_usd_list,
       ROUND(a.eff_current_cost - a.eff_previous_cost, 2)                AS change_usd_list,
       ROUND((a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100, 1) AS change_pct,
       CASE
         WHEN (a.current_unpriced_quantity + a.previous_unpriced_quantity) > 0 THEN 'unpriced'
         WHEN (a.current_priced_quantity + a.previous_priced_quantity) = 0     THEN 'free'
         ELSE 'priced'
       END AS price_basis,
       CASE
         WHEN s.snapshot_start > dateadd(day, -(30 * 2), current_date()) THEN 'NOT_ASSESSED'
         WHEN a.current_cost IS NULL AND a.current_unpriced_quantity > 0           THEN 'NOT_ASSESSED'
         WHEN a.previous_cost IS NULL AND a.previous_unpriced_quantity > 0         THEN 'NOT_ASSESSED'
         WHEN COALESCE(a.eff_current_cost, 0) < 20                    THEN 'OK'
         WHEN a.eff_previous_cost = 0                                             THEN 'CRITICAL'
         WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= 50
           THEN 'CRITICAL'
         WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= 25
           THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN s.snapshot_start > dateadd(day, -(30 * 2), current_date()) THEN 'previous_window_not_covered'
         WHEN a.current_cost IS NULL AND a.current_unpriced_quantity > 0           THEN 'current_period_unpriced'
         WHEN a.previous_cost IS NULL AND a.previous_unpriced_quantity > 0         THEN 'previous_period_unpriced'
         ELSE NULL
       END AS not_assessed_reason
FROM agg a
CROSS JOIN snapshot s
CROSS JOIN total t
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         a.workspace_id,
         change_usd_list DESC NULLS LAST,
         a.billing_origin_product
) q
"""
CHECKS["cost_chargeback_by_sku"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_chargeback_by_sku.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_chargeback_by_sku.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH snapshot AS (
  SELECT MIN(usage_date) AS snapshot_start
  FROM `system`.`billing`.`usage`
),
priced AS (
  SELECT u.sku_name, u.cloud, u.usage_unit, u.workspace_id, u.usage_date,
         u.usage_quantity                AS usage_quantity,
         u.usage_quantity * lp.list_rate AS list_cost,
         lp.list_rate                    AS list_rate
  FROM `system`.`billing`.`usage` u
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
    FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
  ) lp
    ON u.sku_name   = lp.sku_name
   AND u.cloud      = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_date >= dateadd(day, -(30 * 2), current_date())
    AND u.usage_date <  current_date()
),
raw_agg AS (
  SELECT sku_name, cloud, usage_unit, workspace_id,
         SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date())
                  THEN list_cost END)                                        AS current_cost,
         SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date())
                  THEN list_cost END)                                        AS previous_cost,
         SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date())
                   AND upper(usage_unit) = 'DBU'
                  THEN usage_quantity END)                                   AS current_dbus,
         SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date())
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                            AS current_unpriced_quantity,
         SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date())
                   AND list_rate IS NOT NULL
                  THEN usage_quantity ELSE 0 END)                            AS current_priced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date())
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                            AS previous_unpriced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date())
                   AND list_rate IS NOT NULL
                  THEN usage_quantity ELSE 0 END)                            AS previous_priced_quantity
  FROM priced
  GROUP BY sku_name, cloud, usage_unit, workspace_id
),
agg AS (
  SELECT *,
         CASE WHEN current_cost IS NULL AND current_unpriced_quantity = 0 THEN 0 ELSE current_cost END AS eff_current_cost,
         CASE WHEN previous_cost IS NULL AND previous_unpriced_quantity = 0 THEN 0 ELSE previous_cost END AS eff_previous_cost
  FROM raw_agg
),
total AS (
  SELECT SUM(eff_current_cost) AS total_usd_list FROM agg
)
SELECT a.workspace_id,
       a.sku_name,
       replace(lower(a.sku_name), '_', ' ')                               AS sku_name_readable,
       a.cloud,
       a.usage_unit,
       ROUND(a.eff_current_cost, 2)                                      AS usd_list,
       ROUND(a.current_dbus, 2)                                          AS dbus,
       ROUND(a.eff_current_cost * 100.0 / NULLIF(t.total_usd_list, 0), 1) AS share_of_total_pct,
       ROUND(a.eff_previous_cost, 2)                                     AS prev_usd_list,
       ROUND(a.eff_current_cost - a.eff_previous_cost, 2)                AS change_usd_list,
       ROUND((a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100, 1) AS change_pct,
       CASE
         WHEN (a.current_unpriced_quantity + a.previous_unpriced_quantity) > 0 THEN 'unpriced'
         WHEN (a.current_priced_quantity + a.previous_priced_quantity) = 0     THEN 'free'
         ELSE 'priced'
       END AS price_basis,
       CASE
         WHEN s.snapshot_start > dateadd(day, -(30 * 2), current_date()) THEN 'NOT_ASSESSED'
         WHEN a.current_cost IS NULL AND a.current_unpriced_quantity > 0           THEN 'NOT_ASSESSED'
         WHEN a.previous_cost IS NULL AND a.previous_unpriced_quantity > 0         THEN 'NOT_ASSESSED'
         WHEN COALESCE(a.eff_current_cost, 0) < 20                    THEN 'OK'
         WHEN a.eff_previous_cost = 0                                             THEN 'CRITICAL'
         WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= 50
           THEN 'CRITICAL'
         WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= 25
           THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN s.snapshot_start > dateadd(day, -(30 * 2), current_date()) THEN 'previous_window_not_covered'
         WHEN a.current_cost IS NULL AND a.current_unpriced_quantity > 0           THEN 'current_period_unpriced'
         WHEN a.previous_cost IS NULL AND a.previous_unpriced_quantity > 0         THEN 'previous_period_unpriced'
         ELSE NULL
       END AS not_assessed_reason
FROM agg a
CROSS JOIN snapshot s
CROSS JOIN total t
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         a.workspace_id,
         change_usd_list DESC NULLS LAST,
         a.sku_name, a.cloud, a.usage_unit
) q
"""
CHECKS["cost_chargeback_by_job"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_chargeback_by_job.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_chargeback_by_job.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH snapshot AS (
  SELECT MIN(usage_date) AS snapshot_start FROM `system`.`billing`.`usage`
),
priced AS (
  SELECT u.workspace_id, CASE WHEN upper(u.usage_unit) = 'DBU' THEN u.usage_metadata.job_id END AS job_id, u.usage_date, u.sku_name,
         u.usage_quantity                    AS usage_quantity,
         u.usage_quantity * lp.list_rate     AS list_cost,
         lp.list_rate                        AS list_rate
  FROM `system`.`billing`.`usage` u
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
    FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
  ) lp
    ON u.sku_name   = lp.sku_name
   AND u.cloud      = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  -- no unit/id filter: usage with no job id (and any non-DBU unit) becomes the workspace's own
  -- NULL-job row, so the rows add up to total spend
  WHERE u.usage_date >= dateadd(day, -(30 * 2), current_date())
    AND u.usage_date <  current_date()
),
raw_agg AS (
  -- Deliberately no ELSE 0 on current_cost/previous_cost: a job outside the side being summed
  -- contributes nothing, so a fully-unpriced side keeps NULL rather than a false 0.
  SELECT workspace_id, job_id,
         SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date())
                  THEN list_cost END)                                         AS current_cost,
         SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date())
                  THEN list_cost END)                                         AS previous_cost,
         SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date())
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                             AS current_unpriced_quantity,
         SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date())
                   AND list_rate IS NOT NULL
                  THEN usage_quantity ELSE 0 END)                             AS current_priced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date())
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                             AS previous_unpriced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date())
                   AND list_rate IS NOT NULL
                  THEN usage_quantity ELSE 0 END)                             AS previous_priced_quantity
  FROM priced
  GROUP BY workspace_id, job_id
),
agg AS (
  -- eff_current_cost/eff_previous_cost: a side whose SUM is NULL only because it had NO matching
  -- rows at all (its own unpriced quantity is 0 too) resolves to a real 0; a side whose SUM is
  -- NULL because every one of its rows was genuinely unpriced stays NULL, caught below instead.
  SELECT *,
         CASE WHEN current_cost IS NULL AND current_unpriced_quantity = 0 THEN 0 ELSE current_cost END AS eff_current_cost,
         CASE WHEN previous_cost IS NULL AND previous_unpriced_quantity = 0 THEN 0 ELSE previous_cost END AS eff_previous_cost
  FROM raw_agg
),
latest_jobs AS (
  -- joined below with IS NOT DISTINCT FROM on workspace_id (a real account never has a NULL
  -- workspace_id here; this only matters for an account-level billing row, where a plain "="
  -- join to workspace_id would never match a NULL to a NULL).
  -- run_as is the principal id; run_as_user_name is the email or service principal name
  SELECT workspace_id, job_id, name AS job_name, COALESCE(run_as_user_name, run_as) AS run_as, creator_user_name
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),
total AS (
  SELECT SUM(eff_current_cost) AS total_current_usd_list FROM agg
),
scored AS (
  SELECT a.workspace_id, a.job_id, j.job_name, j.run_as, j.creator_user_name,
         a.current_unpriced_quantity, a.previous_unpriced_quantity,
         a.current_priced_quantity, a.previous_priced_quantity,
         a.eff_current_cost, a.eff_previous_cost,
         ROUND(a.eff_current_cost, 2)                                                 AS est_current_usd_list,
         ROUND(a.eff_current_cost * 100.0 / NULLIF(t.total_current_usd_list, 0), 1)   AS share_of_total_pct,
         ROUND(a.eff_previous_cost, 2)                                                AS est_previous_usd_list,
         ROUND(a.eff_current_cost - a.eff_previous_cost, 2)                           AS est_change_usd_list,
         ROUND((a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100, 1) AS change_pct,
         CASE
           WHEN (a.current_unpriced_quantity + a.previous_unpriced_quantity) > 0 THEN 'unpriced'
           WHEN (a.current_priced_quantity + a.previous_priced_quantity) = 0     THEN 'free'
           ELSE 'priced'
         END                                                                           AS price_basis,
         CASE
           WHEN s.snapshot_start > dateadd(day, -(30 * 2), current_date())
             THEN 'NOT_ASSESSED'
           WHEN a.current_cost IS NULL AND a.current_unpriced_quantity > 0
             THEN 'NOT_ASSESSED'
           WHEN a.previous_cost IS NULL AND a.previous_unpriced_quantity > 0
             THEN 'NOT_ASSESSED'
           -- Spend outside any job is context for the total, not a job to fix.
           WHEN a.job_id IS NULL THEN 'OK'
           WHEN COALESCE(a.eff_current_cost, 0) < 20 THEN 'OK'
           WHEN a.eff_previous_cost = 0 THEN 'CRITICAL'
           WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= 50
             THEN 'CRITICAL'
           WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= 25
             THEN 'WARN'
           ELSE 'OK'
         END                                                                           AS status,
         CASE
           WHEN s.snapshot_start > dateadd(day, -(30 * 2), current_date())
             THEN 'previous_window_not_covered'
           WHEN a.current_cost IS NULL AND a.current_unpriced_quantity > 0
             THEN 'current_period_unpriced'
           WHEN a.previous_cost IS NULL AND a.previous_unpriced_quantity > 0
             THEN 'previous_period_unpriced'
           ELSE NULL
         END                                                                           AS not_assessed_reason
  FROM agg a
  LEFT JOIN latest_jobs j ON j.workspace_id IS NOT DISTINCT FROM a.workspace_id AND j.job_id = a.job_id
  CROSS JOIN snapshot s
  CROSS JOIN total t
),
flagged AS (
  -- Every non-OK job (WARN/CRITICAL/NOT_ASSESSED) always keeps its own row - never pooled.
  SELECT workspace_id, job_id, job_name, run_as, creator_user_name,
         FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
         est_current_usd_list, share_of_total_pct, est_previous_usd_list, est_change_usd_list,
         change_pct, price_basis, status, not_assessed_reason
  FROM scored
  WHERE status != 'OK' OR job_id IS NULL   -- the no-job row is never pooled
),
ok_ranked AS (
  SELECT scored.*,
         ROW_NUMBER() OVER (ORDER BY est_current_usd_list DESC NULLS LAST, job_id) AS rn
  FROM scored
  WHERE status = 'OK' AND job_id IS NOT NULL
),
ok_kept AS (
  -- The top 20 OK jobs by current spend, kept as their own row.
  SELECT workspace_id, job_id, job_name, run_as, creator_user_name,
         FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
         est_current_usd_list, share_of_total_pct, est_previous_usd_list, est_change_usd_list,
         change_pct, price_basis, status, not_assessed_reason
  FROM ok_ranked
  WHERE rn <= 20
),
ok_pooled_raw AS (
  -- Every remaining OK job beyond 20 - re-aggregated into one is_other row below.
  SELECT * FROM ok_ranked WHERE rn > 20
),
other_row AS (
  SELECT
    CAST(NULL AS STRING) AS workspace_id,
    CAST(NULL AS STRING) AS job_id,
    CAST(NULL AS STRING) AS job_name,
    CAST(NULL AS STRING) AS run_as,
    CAST(NULL AS STRING) AS creator_user_name,
    TRUE                  AS is_other,
    COUNT(*)              AS pooled_count,
    ROUND(SUM(eff_current_cost), 2)                                              AS est_current_usd_list,
    ROUND(SUM(eff_current_cost) * 100.0 / NULLIF(MAX(t.total_current_usd_list), 0), 1) AS share_of_total_pct,
    ROUND(SUM(eff_previous_cost), 2)                                             AS est_previous_usd_list,
    ROUND(SUM(eff_current_cost) - SUM(eff_previous_cost), 2)                     AS est_change_usd_list,
    ROUND((SUM(eff_current_cost) - SUM(eff_previous_cost)) / NULLIF(SUM(eff_previous_cost), 0) * 100, 1) AS change_pct,
    CASE
      WHEN (SUM(current_unpriced_quantity) + SUM(previous_unpriced_quantity)) > 0 THEN 'unpriced'
      WHEN (SUM(current_priced_quantity) + SUM(previous_priced_quantity)) = 0     THEN 'free'
      ELSE 'priced'
    END AS price_basis,
    -- Every pooled row was already status=OK on its own - a rollup of small, healthy jobs is never
    -- itself a finding, so this row always reads OK regardless of the combined dollars.
    'OK' AS status,
    CAST(NULL AS STRING) AS not_assessed_reason
  FROM ok_pooled_raw
  CROSS JOIN total t
)
SELECT * FROM (
  SELECT * FROM flagged
  UNION ALL
  SELECT * FROM ok_kept
  UNION ALL
  SELECT * FROM other_row WHERE pooled_count > 0
) u
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         is_other,
         est_change_usd_list DESC NULLS LAST,
         workspace_id, job_id
) q
"""
CHECKS["cost_chargeback_identity_by_source"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_chargeback_identity_by_source.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_chargeback_identity_by_source.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH snapshot AS (
  SELECT MIN(usage_date) AS snapshot_start
  FROM `system`.`billing`.`usage`
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
latest_clusters AS (
  SELECT workspace_id, cluster_id, owned_by
  FROM `system`.`compute`.`clusters`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC) = 1
),
wh_day AS (
  SELECT u.workspace_id, u.usage_metadata.warehouse_id AS warehouse_id, u.usage_date,
         SUM(u.usage_quantity * p.list_rate) AS usd_list,
         SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN u.usage_quantity ELSE 0 END) AS unpriced_q,
         SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) AS priced_q
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.warehouse_id IS NOT NULL
    AND u.usage_date >= dateadd(day, -(30 * 2), current_date())
    AND u.usage_date <  current_date()
  GROUP BY u.workspace_id, u.usage_metadata.warehouse_id, u.usage_date
),
wh_identity_duration AS (
  SELECT q.workspace_id, q.compute.warehouse_id AS warehouse_id, DATE(q.start_time) AS usage_date,
         COALESCE(q.executed_by, 'unknown') AS identity_run_as,
         SUM(COALESCE(q.total_duration_ms, 0)) AS duration_ms
  FROM `system`.`query`.`history` q
  WHERE q.compute.warehouse_id IS NOT NULL
    AND q.start_time >= CAST(dateadd(day, -(30 * 2), current_date()) AS TIMESTAMP)
    AND q.start_time <  current_date()
  GROUP BY q.workspace_id, q.compute.warehouse_id, DATE(q.start_time), COALESCE(q.executed_by, 'unknown')
),
wh_day_total_duration AS (
  SELECT workspace_id, warehouse_id, usage_date, SUM(duration_ms) AS total_duration_ms
  FROM wh_identity_duration
  GROUP BY workspace_id, warehouse_id, usage_date
),
wh_split AS (
  -- one row per warehouse-day-identity: the day's dollars times that identity's duration share, or
  -- (when the day has no recorded query duration at all) the whole day's dollars under 'unknown'.
  SELECT d.workspace_id, d.warehouse_id, d.usage_date,
         COALESCE(i.identity_run_as, 'unknown') AS identity_run_as,
         CASE WHEN t.total_duration_ms > 0 THEN d.usd_list * i.duration_ms / t.total_duration_ms
              ELSE d.usd_list END AS usd_list
  FROM wh_day d
  LEFT JOIN wh_day_total_duration t
    ON t.workspace_id = d.workspace_id AND t.warehouse_id = d.warehouse_id AND t.usage_date = d.usage_date
  LEFT JOIN wh_identity_duration i
    ON  i.workspace_id = d.workspace_id AND i.warehouse_id = d.warehouse_id AND i.usage_date = d.usage_date
    AND t.total_duration_ms > 0
),
wh_by_identity AS (
  SELECT workspace_id, warehouse_id, identity_run_as,
         SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date()) THEN usd_list END) AS cur_usd_list,
         SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date()) THEN usd_list END) AS prev_usd_list
  FROM wh_split
  GROUP BY workspace_id, warehouse_id, identity_run_as
),
wh_gate AS (
  SELECT
    (SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date()) THEN usd_list END) IS NULL
     AND SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date()) THEN unpriced_q ELSE 0 END) > 0) AS current_period_unpriced,
    (SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date()) THEN usd_list END) IS NULL
     AND SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date()) THEN unpriced_q ELSE 0 END) > 0) AS previous_period_unpriced,
    CASE
      WHEN SUM(unpriced_q) > 0 THEN 'unpriced'
      WHEN SUM(priced_q) = 0   THEN 'free'
      ELSE 'priced'
    END AS price_basis
  FROM wh_day
),
jobs_usage AS (
  SELECT COALESCE(u.identity_metadata.owned_by, u.identity_metadata.run_as, 'unknown') AS identity_run_as,
         u.usage_date,
         u.usage_quantity * p.list_rate AS usd_list,
         CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
              THEN u.usage_quantity ELSE 0 END AS unpriced_q,
         CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END AS priced_q
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.job_id IS NOT NULL
    AND u.usage_date >= dateadd(day, -(30 * 2), current_date())
    AND u.usage_date <  current_date()
),
jobs_by_identity AS (
  SELECT identity_run_as,
         SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date()) THEN usd_list END) AS cur_usd_list,
         SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date()) THEN usd_list END) AS prev_usd_list
  FROM jobs_usage
  GROUP BY identity_run_as
),
jobs_gate AS (
  SELECT
    (SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date()) THEN usd_list END) IS NULL
     AND SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date()) THEN unpriced_q ELSE 0 END) > 0) AS current_period_unpriced,
    (SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date()) THEN usd_list END) IS NULL
     AND SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date()) THEN unpriced_q ELSE 0 END) > 0) AS previous_period_unpriced,
    CASE
      WHEN SUM(unpriced_q) > 0 THEN 'unpriced'
      WHEN SUM(priced_q) = 0   THEN 'free'
      ELSE 'priced'
    END AS price_basis
  FROM jobs_usage
),
other_usage AS (
  SELECT COALESCE(u.identity_metadata.owned_by, u.identity_metadata.run_as, lc.owned_by, 'unknown') AS identity_run_as,
         u.usage_date,
         u.usage_quantity * p.list_rate AS usd_list,
         CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
              THEN u.usage_quantity ELSE 0 END AS unpriced_q,
         CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END AS priced_q
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  LEFT JOIN latest_clusters lc
    ON  lc.workspace_id = u.workspace_id AND lc.cluster_id = u.usage_metadata.cluster_id
  WHERE u.usage_metadata.warehouse_id IS NULL
    AND u.usage_metadata.job_id IS NULL
    AND u.usage_date >= dateadd(day, -(30 * 2), current_date())
    AND u.usage_date <  current_date()
),
other_by_identity AS (
  SELECT identity_run_as,
         SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date()) THEN usd_list END) AS cur_usd_list,
         SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date()) THEN usd_list END) AS prev_usd_list
  FROM other_usage
  GROUP BY identity_run_as
),
other_gate AS (
  SELECT
    (SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date()) THEN usd_list END) IS NULL
     AND SUM(CASE WHEN usage_date >= dateadd(day, -30, current_date()) THEN unpriced_q ELSE 0 END) > 0) AS current_period_unpriced,
    (SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date()) THEN usd_list END) IS NULL
     AND SUM(CASE WHEN usage_date <  dateadd(day, -30, current_date()) THEN unpriced_q ELSE 0 END) > 0) AS previous_period_unpriced,
    CASE
      WHEN SUM(unpriced_q) > 0 THEN 'unpriced'
      WHEN SUM(priced_q) = 0   THEN 'free'
      ELSE 'priced'
    END AS price_basis
  FROM other_usage
),
combined AS (
  SELECT identity_run_as, 'sql_warehouse' AS source, workspace_id, warehouse_id,
         COALESCE(cur_usd_list, 0) AS cur_raw, COALESCE(prev_usd_list, 0) AS prev_raw
  FROM wh_by_identity
  UNION ALL
  SELECT identity_run_as, 'jobs' AS source, CAST(NULL AS STRING) AS workspace_id, CAST(NULL AS STRING) AS warehouse_id,
         COALESCE(cur_usd_list, 0), COALESCE(prev_usd_list, 0)
  FROM jobs_by_identity
  UNION ALL
  SELECT identity_run_as, 'other' AS source, CAST(NULL AS STRING) AS workspace_id, CAST(NULL AS STRING) AS warehouse_id,
         COALESCE(cur_usd_list, 0), COALESCE(prev_usd_list, 0)
  FROM other_by_identity
),
total AS (
  SELECT SUM(cur_raw) AS total_usd_list FROM combined
),
scored AS (
  SELECT c.identity_run_as,
         CASE
           WHEN c.identity_run_as = 'unknown' THEN 'unknown'
           WHEN c.identity_run_as LIKE '%@%'   THEN 'user'
           ELSE 'service_principal'
         END AS identity_type,
         c.source, c.workspace_id, c.warehouse_id,
         c.cur_raw, c.prev_raw,
         ROUND(c.cur_raw, 2)                                          AS usd_list,
         ROUND(c.cur_raw * 100.0 / NULLIF(t.total_usd_list, 0), 1)    AS share_of_total_pct,
         ROUND(c.prev_raw, 2)                                         AS prev_usd_list,
         ROUND(c.cur_raw - c.prev_raw, 2)                             AS change_usd_list,
         ROUND((c.cur_raw - c.prev_raw) / NULLIF(c.prev_raw, 0) * 100, 1) AS change_pct,
         CASE c.source
           WHEN 'sql_warehouse' THEN wg.price_basis
           WHEN 'jobs'          THEN jg.price_basis
           WHEN 'other'         THEN og.price_basis
         END AS price_basis,
         CASE
           WHEN s.snapshot_start > dateadd(day, -(30 * 2), current_date()) THEN 'NOT_ASSESSED'
           WHEN c.source = 'sql_warehouse' AND (wg.current_period_unpriced OR wg.previous_period_unpriced) THEN 'NOT_ASSESSED'
           WHEN c.source = 'jobs'          AND (jg.current_period_unpriced OR jg.previous_period_unpriced) THEN 'NOT_ASSESSED'
           WHEN c.source = 'other'         AND (og.current_period_unpriced OR og.previous_period_unpriced) THEN 'NOT_ASSESSED'
           WHEN COALESCE(c.cur_raw, 0) < 20 THEN 'OK'
           WHEN c.prev_raw = 0                          THEN 'CRITICAL'
           WHEN (c.cur_raw - c.prev_raw) / NULLIF(c.prev_raw, 0) * 100 >= 50 THEN 'CRITICAL'
           WHEN (c.cur_raw - c.prev_raw) / NULLIF(c.prev_raw, 0) * 100 >= 25  THEN 'WARN'
           ELSE 'OK'
         END AS status,
         CASE
           WHEN s.snapshot_start > dateadd(day, -(30 * 2), current_date()) THEN 'previous_window_not_covered'
           WHEN c.source = 'sql_warehouse' AND wg.current_period_unpriced  THEN 'current_period_unpriced'
           WHEN c.source = 'jobs'          AND jg.current_period_unpriced  THEN 'current_period_unpriced'
           WHEN c.source = 'other'         AND og.current_period_unpriced THEN 'current_period_unpriced'
           WHEN c.source = 'sql_warehouse' AND wg.previous_period_unpriced  THEN 'previous_period_unpriced'
           WHEN c.source = 'jobs'          AND jg.previous_period_unpriced  THEN 'previous_period_unpriced'
           WHEN c.source = 'other'         AND og.previous_period_unpriced THEN 'previous_period_unpriced'
           ELSE NULL
         END AS not_assessed_reason
  FROM combined c
  CROSS JOIN snapshot s
  CROSS JOIN total t
  CROSS JOIN wh_gate wg
  CROSS JOIN jobs_gate jg
  CROSS JOIN other_gate og
),
flagged AS (
  -- A real finding (WARN/CRITICAL) always keeps its own row - never pooled.
  SELECT identity_run_as, identity_type, source, workspace_id, warehouse_id,
         FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
         usd_list, share_of_total_pct, prev_usd_list, change_usd_list, change_pct,
         price_basis, status, not_assessed_reason
  FROM scored
  WHERE status IN ('WARN', 'CRITICAL')
),
poolable_ranked AS (
  -- OK and NOT_ASSESSED rows are both poolable - on an account short of 2x 30 of billing
  -- history, every row reads NOT_ASSESSED, and that list needs the same cap an OK list gets.
  -- ranked PER STATUS, never combined - a NOT_ASSESSED row's usd_list is always NULL (sorted
  -- last), so ranking both statuses together would push every NOT_ASSESSED row past 20 as
  -- soon as 20+ OK rows existed anywhere, regardless of how few NOT_ASSESSED rows there are.
  SELECT scored.*,
         ROW_NUMBER() OVER (PARTITION BY status ORDER BY usd_list DESC NULLS LAST, identity_run_as, source) AS rn
  FROM scored
  WHERE status IN ('OK', 'NOT_ASSESSED')
),
poolable_kept AS (
  -- The top 20 OK/NOT_ASSESSED rows by current spend, kept as their own row.
  SELECT identity_run_as, identity_type, source, workspace_id, warehouse_id,
         FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
         usd_list, share_of_total_pct, prev_usd_list, change_usd_list, change_pct,
         price_basis, status, not_assessed_reason
  FROM poolable_ranked
  WHERE rn <= 20
),
poolable_pooled_raw AS (
  -- Every remaining OK/NOT_ASSESSED row beyond 20 - re-aggregated below into one is_other row
  -- per status (OK rows and NOT_ASSESSED rows are never combined into one row).
  SELECT * FROM poolable_ranked WHERE rn > 20
),
other_ok_row AS (
  SELECT
    CAST(NULL AS STRING) AS identity_run_as,
    'other'               AS identity_type,
    CAST(NULL AS STRING) AS source,
    CAST(NULL AS STRING) AS workspace_id,
    CAST(NULL AS STRING) AS warehouse_id,
    TRUE                  AS is_other,
    COUNT(*)              AS pooled_count,
    ROUND(SUM(cur_raw), 2)                                              AS usd_list,
    ROUND(SUM(cur_raw) * 100.0 / NULLIF(MAX(t.total_usd_list), 0), 1)   AS share_of_total_pct,
    ROUND(SUM(prev_raw), 2)                                             AS prev_usd_list,
    ROUND(SUM(cur_raw) - SUM(prev_raw), 2)                              AS change_usd_list,
    ROUND((SUM(cur_raw) - SUM(prev_raw)) / NULLIF(SUM(prev_raw), 0) * 100, 1) AS change_pct,
    CASE
      WHEN SUM(CASE WHEN price_basis = 'unpriced' THEN 1 ELSE 0 END) > 0 THEN 'unpriced'
      WHEN SUM(CASE WHEN price_basis = 'priced'   THEN 1 ELSE 0 END) = 0 THEN 'free'
      ELSE 'priced'
    END AS price_basis,
    -- Every pooled row was already status=OK on its own - a rollup of small, healthy rows is never
    -- itself a finding, so this row always reads OK regardless of the combined dollars.
    'OK' AS status,
    CAST(NULL AS STRING) AS not_assessed_reason
  FROM poolable_pooled_raw
  CROSS JOIN total t
  WHERE status = 'OK'
),
other_not_assessed_row AS (
  SELECT
    CAST(NULL AS STRING) AS identity_run_as,
    'other'               AS identity_type,
    CAST(NULL AS STRING) AS source,
    CAST(NULL AS STRING) AS workspace_id,
    CAST(NULL AS STRING) AS warehouse_id,
    TRUE                  AS is_other,
    COUNT(*)              AS pooled_count,
    ROUND(SUM(cur_raw), 2)                                              AS usd_list,
    ROUND(SUM(cur_raw) * 100.0 / NULLIF(MAX(t.total_usd_list), 0), 1)   AS share_of_total_pct,
    ROUND(SUM(prev_raw), 2)                                             AS prev_usd_list,
    ROUND(SUM(cur_raw) - SUM(prev_raw), 2)                              AS change_usd_list,
    ROUND((SUM(cur_raw) - SUM(prev_raw)) / NULLIF(SUM(prev_raw), 0) * 100, 1) AS change_pct,
    CASE
      WHEN SUM(CASE WHEN price_basis = 'unpriced' THEN 1 ELSE 0 END) > 0 THEN 'unpriced'
      WHEN SUM(CASE WHEN price_basis = 'priced'   THEN 1 ELSE 0 END) = 0 THEN 'free'
      ELSE 'priced'
    END AS price_basis,
    'NOT_ASSESSED' AS status,
    -- The pooled rows can carry different reasons; NULL here rather than pick one arbitrarily -
    -- raise 20 to see them individually.
    CAST(NULL AS STRING) AS not_assessed_reason
  FROM poolable_pooled_raw
  CROSS JOIN total t
  WHERE status = 'NOT_ASSESSED'
)
SELECT * FROM (
  SELECT * FROM flagged
  UNION ALL
  SELECT * FROM poolable_kept
  UNION ALL
  SELECT * FROM other_ok_row WHERE pooled_count > 0
  UNION ALL
  SELECT * FROM other_not_assessed_row WHERE pooled_count > 0
) u
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         is_other,
         change_usd_list DESC NULLS LAST,
         identity_run_as, source
) q
"""
CHECKS["cost_chargeback_by_tag_value"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_chargeback_by_tag_value.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_chargeback_by_tag_value.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH snapshot AS (
  SELECT MIN(usage_date) AS snapshot_start FROM `system`.`billing`.`usage`
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
priced AS (
  SELECT u.workspace_id, u.custom_tags, u.sku_name,
         (u.usage_date >= dateadd(day, -30, current_date())) AS is_current,
         u.usage_quantity * p.list_rate AS usd_list,
         CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
              THEN u.usage_quantity ELSE 0 END AS unpriced_q,
         CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END AS priced_q
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE u.usage_date >= dateadd(day, -(30 * 2), current_date())
    AND u.usage_date <  current_date()
),
totals AS (
  -- account-wide current/previous totals across every usage_unit combined, over EVERY usage row
  -- regardless of tags or workspace - the denominator for share_of_total_pct (ws_totals below is
  -- the per-workspace twin the (untagged) row itself subtracts from).
  SELECT
    SUM(CASE WHEN is_current     THEN usd_list END)      AS cur_total_usd,
    SUM(CASE WHEN NOT is_current THEN usd_list END)      AS prev_total_usd,
    SUM(CASE WHEN is_current     THEN unpriced_q ELSE 0 END) AS cur_total_unpriced_q,
    SUM(CASE WHEN is_current     THEN priced_q   ELSE 0 END) AS cur_total_priced_q,
    SUM(CASE WHEN NOT is_current THEN unpriced_q ELSE 0 END) AS prev_total_unpriced_q,
    SUM(CASE WHEN NOT is_current THEN priced_q   ELSE 0 END) AS prev_total_priced_q
  FROM priced
),
ws_totals AS (
  -- same shape as `totals` above, but per workspace -- the (untagged) row's own subtraction has
  -- to happen within one workspace at a time now that the grain is workspace + tag_key (see
  -- caveats); `totals` itself stays account-wide, still the ONLY source for share_of_total_pct.
  SELECT workspace_id,
    SUM(CASE WHEN is_current     THEN usd_list END)      AS cur_total_usd,
    SUM(CASE WHEN NOT is_current THEN usd_list END)      AS prev_total_usd,
    SUM(CASE WHEN is_current     THEN unpriced_q ELSE 0 END) AS cur_total_unpriced_q,
    SUM(CASE WHEN is_current     THEN priced_q   ELSE 0 END) AS cur_total_priced_q,
    SUM(CASE WHEN NOT is_current THEN unpriced_q ELSE 0 END) AS prev_total_unpriced_q,
    SUM(CASE WHEN NOT is_current THEN priced_q   ELSE 0 END) AS prev_total_priced_q
  FROM priced
  GROUP BY workspace_id
),
tagged AS (
  -- one row per (usage row, tag_key/tag_value pair it carries). A plain (non-OUTER) explode
  -- already drops a usage row with no tags at all - its dollars are recovered per key by the
  -- untagged_values subtraction further down instead.
  SELECT p.workspace_id, p.is_current, p.usd_list, p.unpriced_q, p.priced_q, t.tag_key, t.tag_value
  FROM priced p
  LATERAL VIEW explode(p.custom_tags) t AS tag_key, tag_value
),
key_present AS (
  -- per workspace + real key found anywhere in the window: current/previous dollars and
  -- quantities from usage rows that DO carry that key - what untagged_values below subtracts from
  -- `ws_totals` to get "this workspace's rows missing this key", without a second explode or a
  -- key x usage cross join.
  SELECT workspace_id, tag_key,
         SUM(CASE WHEN is_current     THEN usd_list END)      AS cur_present_usd,
         SUM(CASE WHEN NOT is_current THEN usd_list END)      AS prev_present_usd,
         SUM(CASE WHEN is_current     THEN unpriced_q ELSE 0 END) AS cur_present_unpriced_q,
         SUM(CASE WHEN is_current     THEN priced_q   ELSE 0 END) AS cur_present_priced_q,
         SUM(CASE WHEN NOT is_current THEN unpriced_q ELSE 0 END) AS prev_present_unpriced_q,
         SUM(CASE WHEN NOT is_current THEN priced_q   ELSE 0 END) AS prev_present_priced_q
  FROM tagged
  GROUP BY workspace_id, tag_key
),
value_agg AS (
  SELECT workspace_id, tag_key, tag_value,
         SUM(CASE WHEN is_current     THEN usd_list END)      AS cur_usd,
         SUM(CASE WHEN NOT is_current THEN usd_list END)      AS prev_usd,
         SUM(CASE WHEN is_current     THEN unpriced_q ELSE 0 END) AS cur_unpriced_q,
         SUM(CASE WHEN is_current     THEN priced_q   ELSE 0 END) AS cur_priced_q,
         SUM(CASE WHEN NOT is_current THEN unpriced_q ELSE 0 END) AS prev_unpriced_q,
         SUM(CASE WHEN NOT is_current THEN priced_q   ELSE 0 END) AS prev_priced_q
  FROM tagged
  GROUP BY workspace_id, tag_key, tag_value
),
ranked AS (
  SELECT *,
         ROW_NUMBER() OVER (
           PARTITION BY workspace_id, tag_key ORDER BY COALESCE(cur_usd, 0) DESC, tag_value
         ) AS value_rank
  FROM value_agg
),
top_values AS (
  SELECT workspace_id, tag_key, tag_value, cur_usd, prev_usd,
         cur_unpriced_q, cur_priced_q, prev_unpriced_q, prev_priced_q,
         FALSE AS is_other, FALSE AS is_untagged, CAST(NULL AS BIGINT) AS pooled_count
  FROM ranked
  WHERE value_rank <= 10
),
other_values AS (
  -- every value ranked beyond 10 for its workspace + key, rolled into one row so
  -- a long-tail tag key (many distinct values) never blows up the row count. pooled_count is how
  -- many distinct values that row folds in -- the app's own "Other (n)" label needs it (cbLabel,
  -- tab_cost.jsx), same as every other chargeback cut's own is_other row.
  SELECT workspace_id, tag_key, CAST(NULL AS STRING) AS tag_value,
         SUM(cur_usd) AS cur_usd, SUM(prev_usd) AS prev_usd,
         SUM(cur_unpriced_q) AS cur_unpriced_q, SUM(cur_priced_q) AS cur_priced_q,
         SUM(prev_unpriced_q) AS prev_unpriced_q, SUM(prev_priced_q) AS prev_priced_q,
         TRUE AS is_other, FALSE AS is_untagged, COUNT(*) AS pooled_count
  FROM ranked
  WHERE value_rank > 10
  GROUP BY workspace_id, tag_key
),
untagged_values AS (
  -- every workspace with spend x every key seen anywhere in the account, so a workspace that never
  -- carries a key still gets its whole spend as (untagged) and each key's rows sum to the total.
  SELECT t.workspace_id, k.tag_key, CAST(NULL AS STRING) AS tag_value,
         t.cur_total_usd  - COALESCE(kp.cur_present_usd, 0)  AS cur_usd,
         t.prev_total_usd - COALESCE(kp.prev_present_usd, 0) AS prev_usd,
         t.cur_total_unpriced_q  - COALESCE(kp.cur_present_unpriced_q, 0)  AS cur_unpriced_q,
         t.cur_total_priced_q   - COALESCE(kp.cur_present_priced_q, 0)    AS cur_priced_q,
         t.prev_total_unpriced_q - COALESCE(kp.prev_present_unpriced_q, 0) AS prev_unpriced_q,
         t.prev_total_priced_q  - COALESCE(kp.prev_present_priced_q, 0)   AS prev_priced_q,
         FALSE AS is_other, TRUE AS is_untagged, CAST(NULL AS BIGINT) AS pooled_count
  FROM ws_totals t
  CROSS JOIN (SELECT DISTINCT tag_key FROM key_present) k
  LEFT JOIN key_present kp
    ON kp.workspace_id IS NOT DISTINCT FROM t.workspace_id AND kp.tag_key = k.tag_key
),
untagged_nonzero AS (
  SELECT * FROM untagged_values
  WHERE ABS(COALESCE(cur_usd, 0)) > 0.000001 OR ABS(COALESCE(prev_usd, 0)) > 0.000001
     OR cur_unpriced_q > 0.000001 OR prev_unpriced_q > 0.000001
     OR cur_priced_q > 0.000001 OR prev_priced_q > 0.000001
),
combined AS (
  SELECT * FROM top_values
  UNION ALL SELECT * FROM other_values
  UNION ALL SELECT * FROM untagged_nonzero
),
resolved AS (
  -- same NULL-vs-real-zero resolution cost_period_over_period's own `agg` CTE uses: a side with
  -- NO matching rows at all (its own unpriced quantity is 0 too) is a genuine 0, never NOT_ASSESSED.
  SELECT *,
         CASE WHEN cur_usd  IS NULL AND cur_unpriced_q  = 0 THEN 0 ELSE cur_usd  END AS eff_cur_usd,
         CASE WHEN prev_usd IS NULL AND prev_unpriced_q = 0 THEN 0 ELSE prev_usd END AS eff_prev_usd
  FROM combined
)
SELECT
  r.workspace_id,
  r.tag_key,
  CASE WHEN r.is_other THEN '(other)' WHEN r.is_untagged THEN '(untagged)' ELSE r.tag_value END AS tag_value,
  r.is_other,
  r.is_untagged,
  r.pooled_count,
  ROUND(r.eff_cur_usd, 2)                                                    AS usd_list,
  ROUND(r.eff_cur_usd * 100.0 / NULLIF(t.cur_total_usd, 0), 1)               AS share_of_total_pct,
  ROUND(r.eff_prev_usd, 2)                                                   AS prev_usd_list,
  ROUND(r.eff_cur_usd - r.eff_prev_usd, 2)                                   AS change_usd_list,
  ROUND((r.eff_cur_usd - r.eff_prev_usd) / NULLIF(r.eff_prev_usd, 0) * 100, 1) AS change_pct,
  CASE
    WHEN (r.cur_unpriced_q + r.prev_unpriced_q) > 0 THEN 'unpriced'
    WHEN (r.cur_priced_q + r.prev_priced_q) = 0     THEN 'free'
    ELSE 'priced'
  END                                                                         AS price_basis,
  CASE
    WHEN s.snapshot_start > dateadd(day, -(30 * 2), current_date())
      THEN 'NOT_ASSESSED'
    WHEN r.cur_usd IS NULL AND r.cur_unpriced_q > 0 THEN 'NOT_ASSESSED'
    WHEN r.prev_usd IS NULL AND r.prev_unpriced_q > 0 THEN 'NOT_ASSESSED'
    WHEN r.is_other THEN 'OK'
    WHEN COALESCE(r.eff_cur_usd, 0) < 20 THEN 'OK'
    WHEN r.eff_prev_usd = 0 THEN 'CRITICAL'
    WHEN (r.eff_cur_usd - r.eff_prev_usd) / NULLIF(r.eff_prev_usd, 0) * 100 >= 50
      THEN 'CRITICAL'
    WHEN (r.eff_cur_usd - r.eff_prev_usd) / NULLIF(r.eff_prev_usd, 0) * 100 >= 25
      THEN 'WARN'
    ELSE 'OK'
  END                                                                         AS status,
  CASE
    WHEN s.snapshot_start > dateadd(day, -(30 * 2), current_date())
      THEN 'previous_window_not_covered'
    WHEN r.cur_usd IS NULL AND r.cur_unpriced_q > 0 THEN 'current_period_unpriced'
    WHEN r.prev_usd IS NULL AND r.prev_unpriced_q > 0 THEN 'previous_period_unpriced'
    ELSE NULL
  END                                                                         AS not_assessed_reason
FROM resolved r
CROSS JOIN totals t
CROSS JOIN snapshot s
ORDER BY workspace_id, tag_key,
         is_untagged, is_other,
         CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         usd_list DESC NULLS LAST
) q
"""
CHECKS["cost_chargeback_reconcile"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_chargeback_reconcile.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_chargeback_reconcile.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH window_usage AS (
  SELECT u.record_id, u.workspace_id, u.usage_date, u.usage_end_time, u.sku_name, u.cloud,
         u.usage_unit, u.usage_quantity, u.billing_origin_product, u.custom_tags,
         u.usage_metadata.warehouse_id AS warehouse_id,
         u.usage_metadata.job_id       AS job_id,
         u.usage_metadata.cluster_id   AS cluster_id
  FROM `system`.`billing`.`usage` u
  WHERE u.usage_date >= dateadd(day, -30, current_date())
    AND u.usage_date <  current_date()
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
  FROM `system`.`billing`.`list_prices`
),
raw_priced AS (
  -- the plain, non-deduped join every chargeback query in this set uses.
  SELECT w.*, p.list_rate, w.usage_quantity * p.list_rate AS usd_list
  FROM window_usage w
  LEFT JOIN price p
    ON  w.sku_name = p.sku_name AND w.cloud = p.cloud AND w.usage_unit = p.usage_unit
    AND w.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR w.usage_end_time < p.price_end_time)
),
raw_total AS (
  SELECT workspace_id, SUM(usd_list) AS total_usd_list FROM raw_priced GROUP BY workspace_id
),
deduped_priced AS (
  -- exactly one matching price row per usage row (most recently started wins), to check whether
  -- the plain join above ever fans out on an overlapping list_prices validity window.
  SELECT w.record_id, w.workspace_id, w.usage_quantity * p.list_rate AS usd_list
  FROM window_usage w
  LEFT JOIN price p
    ON  w.sku_name = p.sku_name AND w.cloud = p.cloud AND w.usage_unit = p.usage_unit
    AND w.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR w.usage_end_time < p.price_end_time)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY w.record_id ORDER BY p.price_start_time DESC) = 1
),
deduped_total AS (
  SELECT workspace_id, SUM(usd_list) AS total_usd_list FROM deduped_priced GROUP BY workspace_id
),
by_tag_value_total AS (
  -- deliberately NOT expected to equal the billing total: cost_chargeback_by_tag_value explodes
  -- EVERY key a usage row carries, so a resource tagged with N keys contributes its dollars N
  -- times here - the 'overlapping' coverage_type below labels this as expected, not a bug.
  SELECT rp.workspace_id, SUM(rp.usd_list) AS total_usd_list
  FROM raw_priced rp
  LATERAL VIEW OUTER explode(rp.custom_tags) t AS tag_key, tag_value
  GROUP BY rp.workspace_id
),
by_user_buckets_total AS (
  -- the same three mutually exclusive conditions a per-identity chargeback view splits usage
  -- across: warehouse DBU, job DBU, or neither (serverless/model-serving/storage/etc.).
  SELECT workspace_id,
    SUM(CASE WHEN upper(usage_unit) = 'DBU' AND warehouse_id IS NOT NULL THEN usd_list ELSE 0 END)
    + SUM(CASE WHEN upper(usage_unit) = 'DBU' AND job_id IS NOT NULL THEN usd_list ELSE 0 END)
    + SUM(CASE WHEN warehouse_id IS NULL AND job_id IS NULL THEN usd_list ELSE 0 END) AS total_usd_list
  FROM raw_priced
  GROUP BY workspace_id
),
by_warehouse_total AS (
  SELECT workspace_id, SUM(usd_list) AS total_usd_list FROM raw_priced
  WHERE upper(usage_unit) = 'DBU' AND warehouse_id IS NOT NULL
  GROUP BY workspace_id
),
by_job_total AS (
  SELECT workspace_id, SUM(usd_list) AS total_usd_list FROM raw_priced
  WHERE upper(usage_unit) = 'DBU' AND job_id IS NOT NULL
  GROUP BY workspace_id
),
by_cluster_total AS (
  SELECT workspace_id, SUM(usd_list) AS total_usd_list FROM raw_priced
  WHERE upper(usage_unit) = 'DBU' AND cluster_id IS NOT NULL   -- same scope cost_chargeback_by_cluster's own WHERE uses, both all_purpose and job_cluster kinds
  GROUP BY workspace_id
),
checks AS (
  -- LEFT JOIN off raw_total (every workspace_id present in the window) so a workspace with $0 on
  -- a partial check's own scope (by_warehouse/by_job/by_cluster) still gets a row here, not a
  -- silently dropped one; IS NOT DISTINCT FROM so the NULL (account-level) workspace_id matches
  -- itself instead of failing a plain `=` join.
  SELECT 'price_join_fanout' AS check_name, 'full' AS coverage_type, r.workspace_id,
         d.total_usd_list AS billing_total_usd_list, r.total_usd_list AS view_total_usd_list
  FROM raw_total r LEFT JOIN deduped_total d ON d.workspace_id IS NOT DISTINCT FROM r.workspace_id
  UNION ALL
  SELECT 'by_tag_value', 'overlapping', r.workspace_id, r.total_usd_list, s.total_usd_list
  FROM raw_total r LEFT JOIN by_tag_value_total s ON s.workspace_id IS NOT DISTINCT FROM r.workspace_id
  UNION ALL
  SELECT 'by_user', 'full', r.workspace_id, r.total_usd_list, s.total_usd_list
  FROM raw_total r LEFT JOIN by_user_buckets_total s ON s.workspace_id IS NOT DISTINCT FROM r.workspace_id
  UNION ALL
  SELECT 'by_warehouse', 'partial', r.workspace_id, r.total_usd_list, s.total_usd_list
  FROM raw_total r LEFT JOIN by_warehouse_total s ON s.workspace_id IS NOT DISTINCT FROM r.workspace_id
  UNION ALL
  SELECT 'by_job', 'partial', r.workspace_id, r.total_usd_list, s.total_usd_list
  FROM raw_total r LEFT JOIN by_job_total s ON s.workspace_id IS NOT DISTINCT FROM r.workspace_id
  UNION ALL
  SELECT 'by_cluster', 'partial', r.workspace_id, r.total_usd_list, s.total_usd_list
  FROM raw_total r LEFT JOIN by_cluster_total s ON s.workspace_id IS NOT DISTINCT FROM r.workspace_id
)
SELECT c.check_name,
       c.workspace_id,
       c.coverage_type,
       ROUND(c.billing_total_usd_list, 2)                                        AS billing_total_usd_list,
       ROUND(c.view_total_usd_list, 2)                                           AS view_total_usd_list,
       ROUND(c.view_total_usd_list - c.billing_total_usd_list, 2)                AS gap_usd_list,
       ROUND((c.view_total_usd_list - c.billing_total_usd_list) * 100.0
             / NULLIF(c.billing_total_usd_list, 0), 2)                           AS gap_pct,
       CASE WHEN c.coverage_type IN ('partial', 'overlapping')
            THEN ROUND(c.view_total_usd_list * 100.0 / NULLIF(c.billing_total_usd_list, 0), 1)
       END                                                                        AS coverage_pct,
       CASE
         WHEN c.coverage_type IN ('partial', 'overlapping') THEN 'OK'
         WHEN c.billing_total_usd_list IS NULL OR c.billing_total_usd_list = 0 THEN 'NOT_ASSESSED'
         WHEN ABS(c.view_total_usd_list - c.billing_total_usd_list) * 100.0 / c.billing_total_usd_list
              >= 5 THEN 'CRITICAL'
         WHEN ABS(c.view_total_usd_list - c.billing_total_usd_list) * 100.0 / c.billing_total_usd_list
              >= 1 THEN 'WARN'
         ELSE 'OK'
       END                                                                        AS status
FROM checks c
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         c.coverage_type, c.check_name, c.workspace_id
) q
"""
CHECKS["cost_by_job"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_by_job.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_by_job.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT u.usage_date, u.cloud, u.workspace_id, w.workspace_name, u.billing_origin_product,
       u.usage_metadata.job_id AS job_id,
       j.job_name,
       u.product_features.is_serverless AS is_serverless,
       SUM(u.usage_quantity) AS net_usage_quantity,
       ROUND(SUM(u.usage_quantity * COALESCE(p.list_rate, 0)), 2) AS usd_list,
       -- price_basis: a real $0 (free-usage SKU) vs a pricing-coverage gap (usd_list understates cost).
       CASE
         WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                       THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
         WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
         ELSE 'priced'
       END AS price_basis,
       COUNT(DISTINCT u.usage_metadata.job_run_id) AS distinct_runs,
       -- status: magnitude band on daily DBU cost per job (field heuristic; 50 / 200) - size, not waste.
       CASE
         WHEN SUM(u.usage_quantity) IS NULL THEN 'NOT_ASSESSED'
         -- the job's whole day, every product and compute kind together
         WHEN SUM(SUM(u.usage_quantity)) OVER (PARTITION BY u.usage_date, u.workspace_id, u.usage_metadata.job_id) >= 200 THEN 'CRITICAL'
         WHEN SUM(SUM(u.usage_quantity)) OVER (PARTITION BY u.usage_date, u.workspace_id, u.usage_metadata.job_id) >= 50 THEN 'WARN'
         ELSE 'OK'
       END AS status,
       SUM(SUM(u.usage_quantity)) OVER (PARTITION BY u.usage_date, u.workspace_id, u.usage_metadata.job_id) AS job_day_dbus
FROM `system`.`billing`.`usage` u
LEFT JOIN (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
) p
  ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
  AND u.usage_end_time >= p.price_start_time
  AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
LEFT JOIN (
  SELECT workspace_id, job_id, name AS job_name
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
) j ON j.workspace_id = u.workspace_id AND j.job_id = u.usage_metadata.job_id
LEFT JOIN `system`.`access`.`workspaces_latest` w ON w.workspace_id = u.workspace_id
WHERE u.usage_date >= dateadd(day, -30, current_date())
  AND u.usage_date < current_date()
  AND u.usage_unit = 'DBU'
  AND u.usage_metadata.job_id IS NOT NULL
GROUP BY u.usage_date, u.cloud, u.workspace_id, w.workspace_name, u.billing_origin_product,
         u.usage_metadata.job_id, j.job_name, u.product_features.is_serverless
ORDER BY net_usage_quantity DESC
) q
"""
CHECKS["cost_by_compute_resource"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_by_compute_resource.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_by_compute_resource.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT u.usage_date, u.cloud, u.workspace_id, w.workspace_name, u.billing_origin_product,
       u.usage_metadata.cluster_id       AS cluster_id,
       lc.cluster_name,
       u.usage_metadata.warehouse_id     AS warehouse_id,
       lw.warehouse_name,
       u.usage_metadata.instance_pool_id AS instance_pool_id,
       lp.instance_pool_name,
       u.product_features.is_serverless  AS is_serverless,
       SUM(u.usage_quantity) AS net_usage_quantity,
       ROUND(SUM(u.usage_quantity * COALESCE(pr.list_rate, 0)), 2) AS usd_list,
       -- price_basis: a real $0 (free-usage SKU) vs a pricing-coverage gap (usd_list understates cost).
       CASE
         WHEN SUM(CASE WHEN pr.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                       THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
         WHEN SUM(CASE WHEN pr.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
         ELSE 'priced'
       END AS price_basis,
       -- status: magnitude band on daily DBU cost per resource (field heuristic; 50 / 200) - size, not waste.
       CASE
         WHEN SUM(u.usage_quantity) IS NULL THEN 'NOT_ASSESSED'
         WHEN SUM(u.usage_quantity) >= 200 THEN 'CRITICAL'
         WHEN SUM(u.usage_quantity) >= 50 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`billing`.`usage` u
LEFT JOIN (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
) pr
  ON  u.sku_name = pr.sku_name AND u.cloud = pr.cloud AND u.usage_unit = pr.usage_unit
  AND u.usage_end_time >= pr.price_start_time
  AND (pr.price_end_time IS NULL OR u.usage_end_time < pr.price_end_time)
LEFT JOIN (
  SELECT workspace_id, cluster_id, cluster_name
  FROM `system`.`compute`.`clusters`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC) = 1
) lc
  ON lc.workspace_id = u.workspace_id AND lc.cluster_id = u.usage_metadata.cluster_id
LEFT JOIN (
  SELECT workspace_id, warehouse_id, warehouse_name
  FROM `system`.`compute`.`warehouses`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id ORDER BY change_time DESC) = 1
) lw
  ON lw.workspace_id = u.workspace_id AND lw.warehouse_id = u.usage_metadata.warehouse_id
LEFT JOIN (
  SELECT workspace_id, instance_pool_id, instance_pool_name
  FROM `system`.`compute`.`instance_pools`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, instance_pool_id ORDER BY change_time DESC) = 1
) lp
  ON lp.workspace_id = u.workspace_id AND lp.instance_pool_id = u.usage_metadata.instance_pool_id
LEFT JOIN `system`.`access`.`workspaces_latest` w ON w.workspace_id = u.workspace_id
WHERE u.usage_date >= dateadd(day, -30, current_date())
  AND u.usage_date < current_date()
  AND u.usage_unit = 'DBU'
  AND (u.usage_metadata.cluster_id IS NOT NULL
       OR u.usage_metadata.warehouse_id IS NOT NULL
       OR u.usage_metadata.instance_pool_id IS NOT NULL)
GROUP BY u.usage_date, u.cloud, u.workspace_id, w.workspace_name, u.billing_origin_product,
         u.usage_metadata.cluster_id, lc.cluster_name,
         u.usage_metadata.warehouse_id, lw.warehouse_name,
         u.usage_metadata.instance_pool_id, lp.instance_pool_name,
         u.product_features.is_serverless
ORDER BY net_usage_quantity DESC
) q
"""
CHECKS["cost_by_notebook"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_by_notebook.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_by_notebook.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT usage_date, cloud, workspace_id, billing_origin_product,
       usage_metadata.notebook_id     AS notebook_id,
       product_features.is_serverless AS is_serverless,
       SUM(usage_quantity) AS net_usage_quantity,
       -- status: magnitude band on daily DBU cost per notebook (field heuristic; 10 / 50).
       CASE
         WHEN SUM(usage_quantity) IS NULL THEN 'NOT_ASSESSED'
         WHEN SUM(usage_quantity) >= 50 THEN 'CRITICAL'
         WHEN SUM(usage_quantity) >= 10 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`billing`.`usage`
WHERE usage_date >= dateadd(day, -30, current_date())
  AND usage_date < current_date()
  AND usage_unit = 'DBU'
  AND usage_metadata.notebook_id IS NOT NULL
GROUP BY usage_date, cloud, workspace_id, billing_origin_product,
         usage_metadata.notebook_id, product_features.is_serverless
ORDER BY net_usage_quantity DESC
) q
"""
CHECKS["cost_default_storage_dsu"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_default_storage_dsu.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_default_storage_dsu.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT usage_date, cloud, workspace_id, sku_name, usage_type, usage_unit,
       usage_metadata.storage_api_type AS storage_api_type,
       usage_metadata.catalog_id       AS catalog_id,
       SUM(usage_quantity) AS net_usage_quantity
FROM `system`.`billing`.`usage`
WHERE usage_date >= dateadd(day, -30, current_date())
  AND usage_date < current_date()
  -- the product covers stored data (STORAGE_SPACE); storage_api_type is set on API-operation rows only
  AND (billing_origin_product = 'DEFAULT_STORAGE' OR usage_metadata.storage_api_type IS NOT NULL)
GROUP BY usage_date, cloud, workspace_id, sku_name, usage_type, usage_unit,
         usage_metadata.storage_api_type, usage_metadata.catalog_id
ORDER BY usage_date DESC, cloud, sku_name
) q
"""
CHECKS["cost_genai_token_gpu"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_genai_token_gpu.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_genai_token_gpu.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT u.usage_date, u.cloud, u.workspace_id, u.sku_name, u.billing_origin_product, u.usage_type, u.usage_unit,
       u.product_features.serving_type AS serving_type,
       u.usage_metadata.endpoint_name  AS endpoint_name,
       u.usage_metadata.endpoint_id    AS endpoint_id,
       SUM(u.usage_quantity) AS net_usage_quantity,
       ROUND(SUM(u.usage_quantity * COALESCE(p.list_rate, 0)), 2) AS usd_list,
       CASE
         WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                       THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
         WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
         ELSE 'priced'
       END AS price_basis
FROM `system`.`billing`.`usage` u
LEFT JOIN (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
) p
  ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
 AND u.usage_end_time >= p.price_start_time
 AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
WHERE u.usage_date >= dateadd(day, -30, current_date())
  AND u.usage_date < current_date()
  AND u.usage_type IN ('TOKEN', 'GPU_TIME', 'ANSWER')
GROUP BY u.usage_date, u.cloud, u.workspace_id, u.sku_name, u.billing_origin_product, u.usage_type, u.usage_unit,
         u.product_features.serving_type, u.usage_metadata.endpoint_name, u.usage_metadata.endpoint_id
ORDER BY usage_date DESC, cloud, usage_type
) q
"""
CHECKS["cost_daily_by_resource"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_daily_by_resource.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_daily_by_resource.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH priced AS (
  SELECT u.workspace_id, u.usage_date,
         u.usage_metadata.warehouse_id    AS warehouse_id,
         u.usage_metadata.job_id          AS job_id,
         u.usage_metadata.job_run_id      AS job_run_id,
         u.usage_metadata.dlt_pipeline_id AS pipeline_id,
         u.usage_metadata.dlt_update_id   AS update_id,
         CASE WHEN u.usage_metadata.job_id IS NULL AND u.usage_metadata.dlt_pipeline_id IS NULL
              THEN u.usage_metadata.cluster_id END AS cluster_id,
         CASE WHEN u.record_type = 'ORIGINAL' THEN u.usage_start_time END AS hour_slot,
         CASE WHEN upper(u.usage_unit) = 'DBU' THEN u.usage_quantity ELSE 0 END AS dbus,
         u.usage_quantity * lp.list_rate AS list_cost,
         CASE WHEN lp.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
              THEN u.usage_quantity ELSE 0 END AS unpriced_quantity
  FROM `system`.`billing`.`usage` u
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
    FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
  ) lp
    ON u.sku_name   = lp.sku_name
   AND u.cloud      = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_date >= dateadd(day, -30, current_date())
    AND u.usage_date <  current_date()
),
by_resource AS (
  SELECT 'workspace' AS resource_type, workspace_id, workspace_id AS resource_id, usage_date, dbus, list_cost, unpriced_quantity,
         CAST(NULL AS TIMESTAMP) AS hour_slot, CAST(NULL AS STRING) AS run_ref
  FROM priced
  UNION ALL
  SELECT 'warehouse', workspace_id, warehouse_id, usage_date, dbus, list_cost, unpriced_quantity, hour_slot, CAST(NULL AS STRING)
  FROM priced WHERE warehouse_id IS NOT NULL
  UNION ALL
  SELECT 'job', workspace_id, job_id, usage_date, dbus, list_cost, unpriced_quantity, CAST(NULL AS TIMESTAMP), job_run_id
  FROM priced WHERE job_id IS NOT NULL
  UNION ALL
  SELECT 'cluster', workspace_id, cluster_id, usage_date, dbus, list_cost, unpriced_quantity, hour_slot, CAST(NULL AS STRING)
  FROM priced WHERE cluster_id IS NOT NULL
  UNION ALL
  SELECT 'pipeline', workspace_id, pipeline_id, usage_date, dbus, list_cost, unpriced_quantity, CAST(NULL AS TIMESTAMP), update_id
  FROM priced WHERE pipeline_id IS NOT NULL
),
daily AS (
  SELECT resource_type, workspace_id, resource_id, usage_date,
         SUM(dbus) AS dbus, SUM(list_cost) AS list_cost, SUM(unpriced_quantity) AS unpriced_quantity,
         COUNT(DISTINCT hour_slot) AS billed_hours, COUNT(DISTINCT run_ref) AS billed_runs
  FROM by_resource
  GROUP BY resource_type, workspace_id, resource_id, usage_date
),
totals AS (
  SELECT d.*,
         SUM(COALESCE(list_cost, 0)) OVER (PARTITION BY resource_type, workspace_id, resource_id) AS total_cost,
         SUM(dbus) OVER (PARTITION BY resource_type, workspace_id, resource_id)                AS total_dbus
  FROM daily d
),
ranked AS (
  SELECT t.*,
         DENSE_RANK() OVER (PARTITION BY resource_type
                            ORDER BY total_cost DESC, total_dbus DESC,
                                     workspace_id ASC NULLS LAST, resource_id ASC NULLS LAST) AS spend_rank
  FROM totals t
),
keyed AS (
  SELECT usage_date, resource_type, workspace_id, dbus, list_cost, unpriced_quantity, billed_hours, billed_runs,
         -- Workspaces are never pooled: an account has few enough of them.
         (resource_type <> 'workspace' AND spend_rank > 100) AS is_other,
         CASE WHEN resource_type <> 'workspace' AND spend_rank > 100 THEN NULL ELSE resource_id END AS resource_id,
         CASE
           WHEN resource_type = 'workspace' THEN COALESCE(workspace_id, 'account')
           WHEN spend_rank > 100 THEN concat(COALESCE(workspace_id, 'account'), ':other')
           ELSE concat(COALESCE(workspace_id, 'account'), ':', resource_id)
         END AS resource_key
  FROM ranked
)
SELECT usage_date,
       resource_type,
       resource_key,
       workspace_id,
       resource_id,
       CASE WHEN resource_type = 'warehouse' THEN resource_id END AS warehouse_id,
       CASE WHEN resource_type = 'job' THEN resource_id END AS job_id,
       CASE WHEN resource_type = 'pipeline' THEN resource_id END AS pipeline_id,
       CASE WHEN resource_type = 'cluster' THEN resource_id END AS cluster_id,
       is_other,
       CASE WHEN is_other THEN COUNT(*) END AS pooled_count,
       ROUND(SUM(dbus), 4) AS dbus,
       -- A day of free-tier usage only is a real $0; a day with unpriced usage stays partial.
       ROUND(CASE WHEN SUM(list_cost) IS NULL AND SUM(unpriced_quantity) = 0 THEN 0 ELSE SUM(list_cost) END, 2) AS usd_list,
       ROUND(SUM(unpriced_quantity), 4) AS unpriced_quantity,
       CASE WHEN resource_type IN ('warehouse', 'cluster') THEN SUM(billed_hours) END AS billed_hours,
       CASE WHEN resource_type IN ('job', 'pipeline') THEN SUM(billed_runs) END AS billed_runs
FROM keyed
GROUP BY usage_date, resource_type, resource_key, workspace_id, resource_id, is_other
ORDER BY usage_date DESC, resource_type, resource_key
) q
"""
CHECKS["perf_failure_causes_daily"] = r"""
-- generated from dbt/models/databricks_direct/performance/d_perf_failure_causes_daily.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/performance/perf_failure_causes_daily.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH stmt_fail AS (
  SELECT 'warehouse' AS resource_type, workspace_id, compute.warehouse_id AS resource_id, date(start_time) AS usage_date,
         COALESCE(NULLIF(regexp_extract(error_message, concat('^', chr(92), '[([A-Za-z0-9_.]+)', chr(92), ']'), 1), ''), 'unclassified') AS cause
  FROM `system`.`query`.`history`
  WHERE execution_status = 'FAILED'
    AND compute.warehouse_id IS NOT NULL
    AND start_time >= current_date() - INTERVAL 30 DAYS
    AND start_time < current_date()
),
run_rows AS (
  SELECT workspace_id, job_id, run_id, period_end_time, result_state, termination_code,
         MIN(period_start_time) OVER (PARTITION BY workspace_id, job_id, run_id) AS run_start
  FROM `system`.`lakeflow`.`job_run_timeline`
  -- a week before the window, so a run that began before it shows its real start
  WHERE period_start_time >= dateadd(day, -(30 + 7), current_date())
),
final_runs AS (
  SELECT workspace_id, job_id, run_start, result_state, termination_code
  FROM run_rows
  WHERE result_state IS NOT NULL
    AND period_end_time < date_trunc('DAY', current_timestamp())
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id, run_id ORDER BY period_end_time DESC) = 1
),
run_fail AS (
  SELECT 'job' AS resource_type, workspace_id, job_id AS resource_id, date(run_start) AS usage_date,
         COALESCE(NULLIF(termination_code, ''), 'unknown') AS cause
  FROM final_runs
  WHERE result_state IN ('FAILED', 'ERROR', 'TIMED_OUT')
    AND run_start >= dateadd(day, -30, current_date())
),
fails AS (
  SELECT * FROM stmt_fail
  UNION ALL
  SELECT * FROM run_fail
)
SELECT usage_date,
       resource_type,
       concat(COALESCE(workspace_id, 'account'), ':', resource_id) AS resource_key,
       workspace_id,
       resource_id,
       cause,
       COUNT(*) AS failures
FROM fails
GROUP BY usage_date, resource_type, workspace_id, resource_id, cause
ORDER BY usage_date DESC, resource_type, resource_key, cause
) q
"""
CHECKS["cost_usage_policy_coverage"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_usage_policy_coverage.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_usage_policy_coverage.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT usage_date, cloud, workspace_id, billing_origin_product,
       -- serverless-only products carry no is_serverless (no classic choice), yet take usage policies
       COALESCE(product_features.is_serverless,
                CASE WHEN billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH', 'GENIE', 'AI_FUNCTIONS',
                                                     'AI_GATEWAY', 'AGENT_BRICKS', 'LAKEBASE', 'APPS')
                     THEN true END) AS is_serverless,
       CASE WHEN usage_metadata.usage_policy_id  IS NOT NULL THEN 'usage_policy'
            WHEN usage_metadata.budget_policy_id IS NOT NULL THEN 'budget_policy_legacy'
            ELSE 'none' END AS policy_coverage,
       CASE WHEN cardinality(map_keys(custom_tags)) > 0 THEN 'tagged' ELSE 'untagged' END AS tag_coverage,
       SUM(usage_quantity) AS net_usage_quantity,
       -- status: yes/no coverage-gap flag on serverless spend (field heuristic).
       CASE
         WHEN COALESCE(product_features.is_serverless,
                CASE WHEN billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH', 'GENIE', 'AI_FUNCTIONS',
                                                     'AI_GATEWAY', 'AGENT_BRICKS', 'LAKEBASE', 'APPS')
                     THEN true END) IS NULL THEN 'NOT_ASSESSED'
         WHEN COALESCE(product_features.is_serverless,
                CASE WHEN billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH', 'GENIE', 'AI_FUNCTIONS',
                                                     'AI_GATEWAY', 'AGENT_BRICKS', 'LAKEBASE', 'APPS')
                     THEN true END) = true
              AND (CASE WHEN usage_metadata.usage_policy_id  IS NOT NULL THEN 'usage_policy'
                        WHEN usage_metadata.budget_policy_id IS NOT NULL THEN 'budget_policy_legacy'
                        ELSE 'none' END) = 'none'
              THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`billing`.`usage`
WHERE usage_date >= dateadd(day, -30, current_date())
  AND usage_date < current_date()
  AND usage_unit = 'DBU'
GROUP BY usage_date, cloud, workspace_id, billing_origin_product,
         COALESCE(product_features.is_serverless,
                CASE WHEN billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH', 'GENIE', 'AI_FUNCTIONS',
                                                     'AI_GATEWAY', 'AGENT_BRICKS', 'LAKEBASE', 'APPS')
                     THEN true END),
         CASE WHEN usage_metadata.usage_policy_id  IS NOT NULL THEN 'usage_policy'
              WHEN usage_metadata.budget_policy_id IS NOT NULL THEN 'budget_policy_legacy'
              ELSE 'none' END,
         CASE WHEN cardinality(map_keys(custom_tags)) > 0 THEN 'tagged' ELSE 'untagged' END
ORDER BY status DESC, net_usage_quantity DESC
) q
"""
CHECKS["cost_actual_vs_list_by_sku"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_actual_vs_list_by_sku.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_actual_vs_list_by_sku.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT u.workspace_id, u.cloud, u.sku_name, u.usage_unit, u.billing_origin_product,
       SUM(u.usage_quantity)                       AS net_usage_quantity,
       SUM(u.usage_quantity * lp.list_rate)        AS net_list_cost,
       -- status: this check can never be assessed - no negotiated-rate source exists anywhere in
       -- system.billing, so every row reads NOT_ASSESSED instead of a fabricated realization band.
       'NOT_ASSESSED'                                                      AS status,
       'no_negotiated_rate_source'                                        AS not_assessed_reason
FROM `system`.`billing`.`usage` u
LEFT JOIN (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective (post-promotion) list price - DEC-66.1's basis
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
) lp
  ON  u.sku_name      = lp.sku_name
  AND u.cloud         = lp.cloud
  AND u.usage_unit    = lp.usage_unit
  AND u.usage_date    >= DATE(lp.price_start_time)
  AND (lp.price_end_time IS NULL OR u.usage_date < DATE(lp.price_end_time))   -- active rows carry NULL end_time
WHERE u.usage_date >= dateadd(day, -30, current_date())
  AND u.usage_date < current_date()
  AND upper(u.usage_unit) = 'DBU'   -- price only DBU rows against a per-DBU rate; never blend bytes/hours/tokens
GROUP BY u.workspace_id, u.cloud, u.sku_name, u.usage_unit, u.billing_origin_product
ORDER BY net_list_cost DESC NULLS LAST
) q
"""
CHECKS["cost_networking_egress"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_networking_egress.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_networking_egress.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT u.usage_date, u.cloud, u.workspace_id, u.sku_name, u.usage_type, u.usage_unit,
       u.usage_metadata.source_region      AS source_region,
       u.usage_metadata.destination_region AS destination_region,
       u.usage_metadata.networking_client  AS networking_client,
       u.usage_metadata.recipient_id       AS recipient_id,
       SUM(u.usage_quantity) AS net_usage_quantity,
       ROUND(SUM(u.usage_quantity * COALESCE(p.list_rate, 0)), 2) AS usd_list,
       CASE
         WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                       THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
         WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
         ELSE 'priced'
       END AS price_basis
FROM `system`.`billing`.`usage` u
LEFT JOIN (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
) p
  ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
 AND u.usage_end_time >= p.price_start_time
 AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
WHERE u.usage_date >= dateadd(day, -30, current_date())
  AND u.usage_date < current_date()
  AND u.usage_type IN ('NETWORK_BYTE', 'NETWORK_HOUR')
GROUP BY u.usage_date, u.cloud, u.workspace_id, u.sku_name, u.usage_type, u.usage_unit,
         u.usage_metadata.source_region, u.usage_metadata.destination_region,
         u.usage_metadata.networking_client, u.usage_metadata.recipient_id
ORDER BY usage_date DESC, cloud, usage_type
) q
"""
CHECKS["cost_cloud_infra"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_cloud_infra.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_cloud_infra.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT u.workspace_id, u.usage_date, u.cloud, p.currency_code,
       -- bare SUM (no COALESCE): a fully-unpriced day/cloud/currency group must stay NULL, never a
       -- silent $0 -- see caveats ("An empty result means not assessed, never $0").
       SUM(u.usage_quantity * p.list_rate)               AS net_list_cost,
       COUNT(*)                                         AS record_count,
       -- price_basis: distinguishes a real $0 (free-usage SKUs, no list_prices row by design) from
       -- a coverage gap (a priced SKU with no matching list_prices row, which understates the total).
       CASE
         WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                       THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
         WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
         ELSE 'priced'
       END                                               AS price_basis
FROM `system`.`billing`.`usage` u
LEFT JOIN (
  SELECT sku_name, cloud, usage_unit, currency_code, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
) p
  ON u.sku_name = p.sku_name
 AND u.cloud = p.cloud
 AND u.usage_unit = p.usage_unit
 AND u.usage_end_time >= p.price_start_time
 AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
WHERE u.usage_date >= dateadd(day, -30, current_date())
  AND u.usage_date < current_date()
GROUP BY u.workspace_id, u.usage_date, u.cloud, p.currency_code
ORDER BY u.usage_date DESC, u.cloud, p.currency_code
) q
"""
CHECKS["cost_restatement_trust_metric"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_restatement_trust_metric.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_restatement_trust_metric.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT workspace_id, cloud,
       SUM(usage_quantity) AS net_usage_quantity,
       SUM(CASE WHEN record_type = 'ORIGINAL'    THEN usage_quantity      ELSE 0 END) AS original_usage_quantity,
       SUM(CASE WHEN record_type = 'RETRACTION'  THEN ABS(usage_quantity) ELSE 0 END) AS retracted_abs_quantity,
       SUM(CASE WHEN record_type = 'RESTATEMENT' THEN usage_quantity      ELSE 0 END) AS restatement_usage_quantity,
       MAX(ingestion_date) AS max_ingestion_date
FROM `system`.`billing`.`usage`
WHERE usage_date >= dateadd(day, -30, current_date())
  AND usage_date < current_date()
  AND upper(usage_unit) = 'DBU'   -- one unit, so the restated share is not DBU, GB and hours added up
GROUP BY workspace_id, cloud
ORDER BY cloud
) q
"""
CHECKS["cost_by_serving_endpoint"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_by_serving_endpoint.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_by_serving_endpoint.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT usage_date, cloud, workspace_id, billing_origin_product,
       usage_metadata.endpoint_id AS endpoint_id,
       usage_metadata.endpoint_name AS endpoint_name,
       usage_type,
       SUM(usage_quantity) AS net_usage_quantity
FROM `system`.`billing`.`usage`
WHERE usage_date >= dateadd(day, -30, current_date())
  AND usage_date < current_date()
  AND usage_unit = 'DBU'
  AND billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH')
GROUP BY usage_date, cloud, workspace_id, billing_origin_product,
         usage_metadata.endpoint_id, usage_metadata.endpoint_name, usage_type
ORDER BY usage_date DESC, workspace_id, endpoint_id
) q
"""
CHECKS["cost_serving_mode_by_endpoint"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_serving_mode_by_endpoint.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_serving_mode_by_endpoint.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT
  u.usage_date                           AS usage_date,
  u.workspace_id                         AS workspace_id,
  u.usage_metadata.endpoint_id           AS endpoint_id,
  u.usage_metadata.endpoint_name AS endpoint_name,
  u.cloud                                AS cloud,
  u.sku_name                             AS sku_name,
  u.usage_type                           AS usage_type,
  u.usage_unit                           AS usage_unit,
  u.product_features.serving_type        AS serving_type,
  CASE WHEN upper(u.sku_name) LIKE '%SERVERLESS_REAL_TIME_INFERENCE_LAUNCH%'
       THEN TRUE ELSE FALSE END          AS is_launch_sku,
  SUM(u.usage_quantity)                  AS net_usage_quantity,
  SUM(u.usage_quantity * lp.list_rate)   AS net_list_cost,
  -- price_basis: a real $0 (free-usage SKU) vs a pricing-coverage gap (NULL, understates cost).
  CASE
    WHEN lp.list_rate IS NOT NULL THEN 'priced'
    WHEN upper(u.sku_name) LIKE '%FREE_USAGE%' THEN 'free'
    ELSE 'unpriced'
  END                                     AS price_basis,
  -- status: est_usd_list/day band per endpoint + day + usage_type (field heuristic; 50 / 250).
  -- a FREE_USAGE SKU with no matching price row is a real $0 (OK), never NOT_ASSESSED -- that
  -- reading is reserved for a genuine pricing-coverage gap (price_basis = 'unpriced').
  CASE
    WHEN upper(u.sku_name) LIKE '%FREE_USAGE%' AND SUM(u.usage_quantity * lp.list_rate) IS NULL THEN 'OK'
    WHEN SUM(u.usage_quantity * lp.list_rate) IS NULL THEN 'NOT_ASSESSED'
    -- the endpoint's whole day for this usage type, every SKU slice together
    WHEN SUM(SUM(u.usage_quantity * lp.list_rate)) OVER (PARTITION BY u.usage_date, u.workspace_id, u.usage_metadata.endpoint_id, u.usage_type) >= 250 THEN 'CRITICAL'
    WHEN SUM(SUM(u.usage_quantity * lp.list_rate)) OVER (PARTITION BY u.usage_date, u.workspace_id, u.usage_metadata.endpoint_id, u.usage_type) >= 50 THEN 'WARN'
    ELSE 'OK'
  END AS status,
  SUM(SUM(u.usage_quantity * lp.list_rate)) OVER (PARTITION BY u.usage_date, u.workspace_id, u.usage_metadata.endpoint_id, u.usage_type) AS endpoint_day_usd
FROM `system`.`billing`.`usage` u
LEFT JOIN (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective (post-promotion) list price - DEC-66.1
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
) lp
  ON  u.sku_name = lp.sku_name
  AND u.cloud    = lp.cloud
  AND u.usage_unit = lp.usage_unit
  AND u.usage_end_time >= lp.price_start_time
  AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
WHERE u.usage_date >= dateadd(day, -30, current_date())
  AND u.usage_date <  current_date()
  AND u.billing_origin_product = 'MODEL_SERVING'
GROUP BY
  u.usage_date,
  u.workspace_id,
  u.usage_metadata.endpoint_id,
  u.usage_metadata.endpoint_name,
  u.cloud,
  u.sku_name,
  u.usage_type,
  u.usage_unit,
  u.product_features.serving_type,
  CASE WHEN upper(u.sku_name) LIKE '%SERVERLESS_REAL_TIME_INFERENCE_LAUNCH%' THEN TRUE ELSE FALSE END,
  CASE
    WHEN lp.list_rate IS NOT NULL THEN 'priced'
    WHEN upper(u.sku_name) LIKE '%FREE_USAGE%' THEN 'free'
    ELSE 'unpriced'
  END
ORDER BY net_list_cost DESC NULLS LAST
) q
"""
CHECKS["cost_vector_search_spend"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_vector_search_spend.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_vector_search_spend.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT u.usage_date, u.cloud, u.workspace_id, u.sku_name, u.usage_type, u.usage_unit,
       u.usage_metadata.endpoint_name AS endpoint_name,
       u.usage_metadata.endpoint_id   AS endpoint_id,
       lp.currency_code,
       SUM(u.usage_quantity)                AS net_usage_quantity,
       SUM(u.usage_quantity * lp.list_rate) AS net_list_cost,
       -- price_basis: a real $0 (free-usage SKU) vs a pricing-coverage gap (NULL, understates cost).
       CASE
         WHEN lp.list_rate IS NOT NULL THEN 'priced'
         WHEN upper(u.sku_name) LIKE '%FREE_USAGE%' THEN 'free'
         ELSE 'unpriced'
       END                                   AS price_basis,
       -- status: est_usd_list/day band per endpoint (field heuristic; 25 / 100).
       -- a FREE_USAGE SKU with no matching price row is a real $0 (OK), never NOT_ASSESSED -- that
       -- reading is reserved for a genuine pricing-coverage gap (price_basis = 'unpriced').
       CASE
         WHEN upper(u.sku_name) LIKE '%FREE_USAGE%' AND SUM(u.usage_quantity * lp.list_rate) IS NULL THEN 'OK'
         WHEN SUM(u.usage_quantity * lp.list_rate) IS NULL THEN 'NOT_ASSESSED'
         -- the endpoint's whole day, compute and storage SKUs together
         WHEN SUM(SUM(u.usage_quantity * lp.list_rate)) OVER (PARTITION BY u.usage_date, u.workspace_id, u.usage_metadata.endpoint_id) >= 100 THEN 'CRITICAL'
         WHEN SUM(SUM(u.usage_quantity * lp.list_rate)) OVER (PARTITION BY u.usage_date, u.workspace_id, u.usage_metadata.endpoint_id) >= 25 THEN 'WARN'
         ELSE 'OK'
       END AS status,
       SUM(SUM(u.usage_quantity * lp.list_rate)) OVER (PARTITION BY u.usage_date, u.workspace_id, u.usage_metadata.endpoint_id) AS endpoint_day_usd
FROM `system`.`billing`.`usage` u
LEFT JOIN (
  SELECT sku_name, cloud, usage_unit, currency_code, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective (post-promotion) list price - DEC-66.1
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
) lp
  ON u.sku_name = lp.sku_name
 AND u.cloud    = lp.cloud
 AND u.usage_unit = lp.usage_unit
 AND u.usage_end_time >= lp.price_start_time
 AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
WHERE u.billing_origin_product = 'VECTOR_SEARCH'
  AND u.usage_date >= dateadd(day, -30, current_date())
  AND u.usage_date < current_date()
GROUP BY u.usage_date, u.cloud, u.workspace_id, u.sku_name, u.usage_type, u.usage_unit,
         u.usage_metadata.endpoint_name, u.usage_metadata.endpoint_id, lp.currency_code,
         CASE
           WHEN lp.list_rate IS NOT NULL THEN 'priced'
           WHEN upper(u.sku_name) LIKE '%FREE_USAGE%' THEN 'free'
           ELSE 'unpriced'
         END
ORDER BY net_list_cost DESC NULLS LAST
) q
"""
CHECKS["genie_usage"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_genie_usage.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/genie_usage.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH snapshot AS (
  SELECT MIN(usage_date) AS snapshot_start FROM `system`.`billing`.`usage`
),
priced AS (
  SELECT u.usage_date, u.workspace_id,
         u.usage_metadata.genie.surface  AS surface,
         u.usage_metadata.genie.channel  AS channel,
         u.usage_metadata.genie.agent_id AS agent_id,
         u.identity_metadata.run_as      AS run_as_raw,
         CASE WHEN upper(u.sku_name) LIKE '%FREE%' THEN TRUE ELSE FALSE END AS is_free,
         u.usage_quantity,
         lp.list_rate
  FROM `system`.`billing`.`usage` u
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
    FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
  ) lp
    ON u.sku_name   = lp.sku_name
   AND u.cloud      = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.billing_origin_product = 'GENIE'
    AND upper(u.usage_unit) = 'DBU'
    AND u.usage_date >= dateadd(day, -(30 * 2), current_date())
    AND u.usage_date <  current_date()
),
agg AS (
  SELECT usage_date, workspace_id, surface, channel, agent_id, run_as_raw, is_free,
         SUM(usage_quantity)                                                        AS dbus,
         -- A free-tier row is a real $0; an unpriced row adds nothing to the dollars.
         SUM(CASE WHEN is_free THEN 0 ELSE usage_quantity * list_rate END)          AS usd_list,
         SUM(CASE WHEN NOT is_free AND list_rate IS NULL THEN usage_quantity ELSE 0 END) AS unpriced_dbus
  FROM priced
  GROUP BY usage_date, workspace_id, surface, channel, agent_id, run_as_raw, is_free
)
SELECT a.usage_date,
       CASE WHEN a.usage_date >= dateadd(day, -30, current_date()) THEN 'current' ELSE 'previous' END AS period,
       a.workspace_id,
       a.surface,
       a.channel,
       a.agent_id,
       CASE
         WHEN a.run_as_raw IS NULL OR a.run_as_raw = '__REDACTED__' THEN 'unknown'
         WHEN a.run_as_raw LIKE '%@%' THEN 'user'
         ELSE 'service_principal'
       END AS identity_type,
       run_as_raw AS run_as,
       a.is_free,
       ROUND(a.dbus, 4) AS dbus,
       ROUND(a.usd_list, 2) AS usd_list,
       ROUND(a.unpriced_dbus, 4) AS unpriced_dbus,
       CASE
         WHEN a.is_free THEN 'free'
         WHEN a.unpriced_dbus = 0 THEN 'priced'
         WHEN a.unpriced_dbus >= a.dbus THEN 'unpriced'
         ELSE 'partly_priced'
       END AS price_basis,
       CASE WHEN s.snapshot_start <= dateadd(day, -(30 * 2), current_date()) THEN TRUE ELSE FALSE END AS previous_period_covered
FROM agg a
CROSS JOIN snapshot s
ORDER BY a.usage_date DESC, a.workspace_id, usd_list DESC
) q
"""
CHECKS["compute_warehouse_idle_gaps"] = r"""
-- generated from dbt/models/databricks_direct/compute/d_compute_warehouse_idle_gaps.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/compute/compute_warehouse_idle_gaps.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH bounds AS (
    SELECT unix_timestamp(CAST(current_date() - INTERVAL 30 DAYS AS TIMESTAMP)) AS win_start_s,
           unix_timestamp(CAST(current_date() AS TIMESTAMP))                            AS win_end_s
),
ev_all AS (
    SELECT e.warehouse_id, e.event_type, e.cluster_count,
           unix_timestamp(e.event_time) AS t,
           CASE e.event_type WHEN 'STARTING' THEN 1 WHEN 'RUNNING' THEN 2
                             WHEN 'SCALED_UP' THEN 3 WHEN 'SCALED_DOWN' THEN 3
                             WHEN 'STOPPING' THEN 4 ELSE 5 END AS state_rank
    FROM `system`.`compute`.`warehouse_events` e
    WHERE e.event_time < current_date()
      AND e.event_type IN ('STARTING', 'RUNNING', 'SCALED_UP', 'SCALED_DOWN', 'STOPPING', 'STOPPED')
),
prior AS (
    -- the last event before the window: the state the warehouse was in when the window opened
    SELECT a.warehouse_id, a.event_type, a.cluster_count
    FROM ev_all a CROSS JOIN bounds b
    WHERE a.t < b.win_start_s
    QUALIFY ROW_NUMBER() OVER (PARTITION BY a.warehouse_id ORDER BY a.t DESC, a.state_rank DESC) = 1
),
ev AS (
    SELECT x.*,
           CASE WHEN x.event_type IN ('RUNNING', 'SCALED_UP')
                  OR (x.event_type = 'SCALED_DOWN' AND COALESCE(x.cluster_count, 1) > 0)
                THEN 1 ELSE 0 END AS is_running
    FROM (
        SELECT a.warehouse_id, a.event_type, a.cluster_count, a.t, a.state_rank, 1 AS is_real_event
        FROM ev_all a CROSS JOIN bounds b
        WHERE a.t >= b.win_start_s
        UNION ALL
        SELECT p.warehouse_id, p.event_type, p.cluster_count, b.win_start_s AS t, 0 AS state_rank, 0 AS is_real_event
        FROM prior p CROSS JOIN bounds b
    ) x
),
seg AS (
    -- one row per event: the state it holds until the next event (or the window's end)
    SELECT v.*,
           COALESCE(LEAD(v.t) OVER (PARTITION BY v.warehouse_id ORDER BY v.t, v.state_rank),
                    b.win_end_s) AS seg_end_s,
           COALESCE(LAG(v.is_running) OVER (PARTITION BY v.warehouse_id ORDER BY v.t, v.state_rank), 0) AS prev_running
    FROM ev v CROSS JOIN bounds b
),
seg_runs AS (
    SELECT s.*,
           SUM(CASE WHEN s.is_running = 1 AND s.prev_running = 0 THEN 1 ELSE 0 END)
             OVER (PARTITION BY s.warehouse_id ORDER BY s.t, s.state_rank ROWS UNBOUNDED PRECEDING) AS run_no
    FROM seg s
),
running AS (
    -- contiguous running periods: consecutive running-type events merge into one
    SELECT warehouse_id, run_no, MIN(t) AS run_start_s, MAX(seg_end_s) AS run_end_s
    FROM seg_runs
    WHERE is_running = 1
    GROUP BY warehouse_id, run_no
),
max_run AS (
    SELECT warehouse_id, MAX(run_end_s - run_start_s) AS max_running_gap_seconds
    FROM running
    GROUP BY warehouse_id
),
wh_time AS (
    SELECT warehouse_id,
           COUNT(*)                                                              AS event_count,
           SUM(is_real_event)                                                    AS real_event_count,
           SUM(is_running * (seg_end_s - t))                                     AS running_seconds,
           SUM(CASE WHEN event_type = 'STARTING' THEN seg_end_s - t ELSE 0 END)  AS starting_seconds,
           SUM(CASE WHEN event_type = 'STOPPED'  THEN seg_end_s - t ELSE 0 END)  AS stopped_seconds,
           SUM(CASE WHEN event_type = 'RUNNING'  THEN 1 ELSE 0 END)              AS running_events,
           SUM(CASE WHEN event_type = 'STARTING' THEN 1 ELSE 0 END)              AS starting_events,
           SUM(CASE WHEN event_type = 'STOPPED'  THEN 1 ELSE 0 END)              AS stopped_events
    FROM seg
    GROUP BY warehouse_id
),
price AS (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
    FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
cost_rollup AS (
    -- Pre-aggregated per-warehouse DBU/$ over the SAME [today - 30, today) window the
    -- event side above uses. warehouse_id is a globally-unique GUID, so we key on it alone.
    SELECT
        u.usage_metadata.warehouse_id                    AS warehouse_id,
        SUM(u.usage_quantity)                            AS net_dbus,
        SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS est_usd_list,
        CASE
          WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                        THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
          WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
          ELSE 'priced'
        END                                               AS price_basis
    FROM `system`.`billing`.`usage` u
    LEFT JOIN price p
      ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
     AND u.usage_end_time >= p.price_start_time
     AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
    WHERE upper(u.usage_unit) = 'DBU'
      AND u.usage_metadata.warehouse_id IS NOT NULL
      AND u.usage_date >= current_date() - INTERVAL 30 DAYS
      AND u.usage_date <  current_date()
    GROUP BY u.usage_metadata.warehouse_id
)
SELECT
    t.warehouse_id,
    t.event_count,
    COALESCE(t.running_seconds, 0)  AS running_seconds,
    COALESCE(t.starting_seconds, 0) AS starting_seconds,
    COALESCE(t.stopped_seconds, 0)  AS stopped_seconds,
    m.max_running_gap_seconds,
    COALESCE(t.running_events, 0)  AS running_events,
    COALESCE(t.starting_events, 0) AS starting_events,
    COALESCE(t.stopped_events, 0)  AS stopped_events,
    -- ADDED cost visibility (see header caveats): exact billed DBUs and list-price $ ESTIMATE for this warehouse
    COALESCE(cr.net_dbus, 0)     AS net_dbus,
    COALESCE(cr.est_usd_list, 0) AS est_usd_list,
    COALESCE(cr.price_basis, 'priced') AS price_basis,
    -- status: worst-first band on the single longest continuous RUNNING period (field heuristic; 4 / 24).
    CASE
        WHEN m.max_running_gap_seconds IS NULL THEN 'NOT_ASSESSED'
        WHEN m.max_running_gap_seconds >= 24 * 3600 THEN 'CRITICAL'
        WHEN m.max_running_gap_seconds >= 4 * 3600 THEN 'WARN'
        ELSE 'OK'
    END AS status
FROM wh_time t
LEFT JOIN max_run m ON m.warehouse_id = t.warehouse_id
LEFT JOIN cost_rollup cr ON cr.warehouse_id = t.warehouse_id
WHERE COALESCE(t.running_seconds, 0) > 0 OR t.real_event_count > 0
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END, max_running_gap_seconds DESC
) q
"""
CHECKS["compute_warehouse_autoscale_churn"] = r"""
-- generated from dbt/models/databricks_direct/compute/d_compute_warehouse_autoscale_churn.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/compute/compute_warehouse_autoscale_churn.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH price AS (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
    FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
cost_rollup AS (
    SELECT u.usage_metadata.warehouse_id                     AS warehouse_id,
           SUM(u.usage_quantity)                             AS net_dbus,
           SUM(u.usage_quantity * COALESCE(p.list_rate, 0))  AS est_usd_list,
           CASE
             WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                           THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
             WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
             ELSE 'priced'
           END                                                AS price_basis
    FROM `system`.`billing`.`usage` u
    LEFT JOIN price p
      ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
     AND u.usage_end_time >= p.price_start_time
     AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
    WHERE upper(u.usage_unit) = 'DBU'
      AND u.usage_metadata.warehouse_id IS NOT NULL
      AND u.usage_date >= current_date() - INTERVAL 30 DAYS
      AND u.usage_date <  current_date()
    GROUP BY u.usage_metadata.warehouse_id
),
latest_wh AS (
  SELECT warehouse_id, warehouse_name,
         ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) AS rn
  FROM `system`.`compute`.`warehouses`
),
wh_name AS (SELECT warehouse_id, warehouse_name FROM latest_wh WHERE rn = 1),
ev AS (
  -- prev_cluster_count from ALL event types (not just SCALED_UP/DOWN), so a 0->N transition
  -- logged as SCALED_UP is correctly told apart from a churn transition between nonzero counts.
  -- next_event_time (LEAD) gives each event's own held duration, for the time-weighted average.
  SELECT warehouse_id, event_type, cluster_count, event_time,
         LAG(cluster_count) OVER (PARTITION BY warehouse_id ORDER BY event_time) AS prev_cluster_count,
         LEAD(event_time) OVER (PARTITION BY warehouse_id ORDER BY event_time)   AS next_event_time
  FROM `system`.`compute`.`warehouse_events`
  WHERE event_time >= current_timestamp() - INTERVAL 30 DAYS
)
SELECT
    e.warehouse_id,
    wn.warehouse_name,
    -- original columns, meaning unchanged (front-end compatibility -- see read_this above)
    SUM(CASE WHEN e.event_type = 'SCALED_UP'   THEN 1 ELSE 0 END)                  AS scaled_up_events,
    SUM(CASE WHEN e.event_type = 'SCALED_DOWN' THEN 1 ELSE 0 END)                  AS scaled_down_events,
    SUM(CASE WHEN e.event_type IN ('SCALED_UP', 'SCALED_DOWN') THEN 1 ELSE 0 END)  AS scaling_events,
    SUM(CASE WHEN e.event_type IN ('SCALED_UP', 'SCALED_DOWN') THEN 1 ELSE 0 END)  AS scaling_events_total,
    -- new columns: true churn (nonzero-to-nonzero transitions) apart from ordinary starts/stops
    SUM(CASE WHEN e.event_type IN ('SCALED_UP', 'SCALED_DOWN')
              AND COALESCE(e.prev_cluster_count, 0) > 0 AND COALESCE(e.cluster_count, 0) > 0
             THEN 1 ELSE 0 END)                                          AS churn_events,
    SUM(CASE WHEN e.event_type = 'SCALED_UP' AND COALESCE(e.prev_cluster_count, 0) = 0
              AND COALESCE(e.cluster_count, 0) > 0 THEN 1 ELSE 0 END)    AS start_events,
    SUM(CASE WHEN e.event_type = 'SCALED_DOWN' AND COALESCE(e.cluster_count, 0) = 0
              AND COALESCE(e.prev_cluster_count, 0) > 0 THEN 1 ELSE 0 END) AS stop_events,
    LEAST(
      SUM(CASE WHEN e.event_type = 'SCALED_UP' AND COALESCE(e.prev_cluster_count, 0) = 0
                AND COALESCE(e.cluster_count, 0) > 0 THEN 1 ELSE 0 END),
      SUM(CASE WHEN e.event_type = 'SCALED_DOWN' AND COALESCE(e.cluster_count, 0) = 0
                AND COALESCE(e.prev_cluster_count, 0) > 0 THEN 1 ELSE 0 END)
    )                                                                     AS start_stop_cycles,
    COUNT(*)                                                              AS total_events,
    MIN(e.event_time)                                                     AS first_event_time,
    MAX(e.event_time)                                                     AS last_event_time,
    (unix_timestamp(MAX(e.event_time)) - unix_timestamp(MIN(e.event_time))) / 3600.0 AS observed_hours,
    -- hours with clusters running: churn per hour of the stopped time in between means nothing
    SUM(CASE WHEN COALESCE(e.cluster_count, 0) > 0 THEN unix_timestamp(e.next_event_time) - unix_timestamp(e.event_time) ELSE 0 END) / 3600.0 AS running_hours,
    (SUM(CASE WHEN e.event_type IN ('SCALED_UP', 'SCALED_DOWN')
                AND COALESCE(e.prev_cluster_count, 0) > 0 AND COALESCE(e.cluster_count, 0) > 0
               THEN 1 ELSE 0 END)
     / NULLIF(SUM(CASE WHEN COALESCE(e.cluster_count, 0) > 0 THEN unix_timestamp(e.next_event_time) - unix_timestamp(e.event_time) ELSE 0 END) / 3600.0, 0)) AS churn_events_per_hour,
    (LEAST(
       SUM(CASE WHEN e.event_type = 'SCALED_UP' AND COALESCE(e.prev_cluster_count, 0) = 0
                 AND COALESCE(e.cluster_count, 0) > 0 THEN 1 ELSE 0 END),
       SUM(CASE WHEN e.event_type = 'SCALED_DOWN' AND COALESCE(e.cluster_count, 0) = 0
                 AND COALESCE(e.prev_cluster_count, 0) > 0 THEN 1 ELSE 0 END)
     )
     / NULLIF((unix_timestamp(MAX(e.event_time)) - unix_timestamp(MIN(e.event_time))) / 3600.0, 0)) AS start_stop_cycles_per_hour,
    MAX(e.cluster_count)                                                  AS max_cluster_count,
    AVG(e.cluster_count)                                                  AS avg_cluster_count,
    SUM(e.cluster_count * (unix_timestamp(e.next_event_time) - unix_timestamp(e.event_time)))
      / NULLIF(SUM(unix_timestamp(e.next_event_time) - unix_timestamp(e.event_time)), 0) AS avg_cluster_count_time_weighted,
    COALESCE(cr.net_dbus, 0)     AS net_dbus,
    COALESCE(cr.est_usd_list, 0) AS est_usd_list,
    COALESCE(cr.price_basis, 'priced') AS price_basis,
    -- status: worst-first band on TRUE churn events (nonzero-to-nonzero) per RUNNING hour - the
    -- fix - not the old scaling_events_total rate, which also counted ordinary starts and stops.
    CASE
        WHEN (unix_timestamp(MAX(e.event_time)) - unix_timestamp(MIN(e.event_time))) / 3600.0 < 1 THEN 'NOT_ASSESSED'
        WHEN SUM(CASE WHEN e.event_type IN ('SCALED_UP', 'SCALED_DOWN')
                       AND COALESCE(e.prev_cluster_count, 0) > 0 AND COALESCE(e.cluster_count, 0) > 0
                      THEN 1 ELSE 0 END)
             / NULLIF(SUM(CASE WHEN COALESCE(e.cluster_count, 0) > 0 THEN unix_timestamp(e.next_event_time) - unix_timestamp(e.event_time) ELSE 0 END) / 3600.0, 0) >= 10 THEN 'CRITICAL'
        WHEN SUM(CASE WHEN e.event_type IN ('SCALED_UP', 'SCALED_DOWN')
                       AND COALESCE(e.prev_cluster_count, 0) > 0 AND COALESCE(e.cluster_count, 0) > 0
                      THEN 1 ELSE 0 END)
             / NULLIF(SUM(CASE WHEN COALESCE(e.cluster_count, 0) > 0 THEN unix_timestamp(e.next_event_time) - unix_timestamp(e.event_time) ELSE 0 END) / 3600.0, 0) >= 4 THEN 'WARN'
        ELSE 'OK'
    END AS status
FROM ev e
LEFT JOIN cost_rollup cr ON e.warehouse_id = cr.warehouse_id
LEFT JOIN wh_name wn ON wn.warehouse_id = e.warehouse_id
GROUP BY e.warehouse_id, wn.warehouse_name, cr.net_dbus, cr.est_usd_list, cr.price_basis
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END, churn_events DESC
) q
"""
CHECKS["compute_warehouse_autostop_churn"] = r"""
-- generated from dbt/models/databricks_direct/compute/d_compute_warehouse_autostop_churn.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/compute/compute_warehouse_autostop_churn.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH wh AS (
  SELECT warehouse_id, warehouse_name, warehouse_type, auto_stop_minutes
  FROM `system`.`compute`.`warehouses`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
),
events AS (
  SELECT workspace_id, warehouse_id, event_type, cluster_count, event_time
  FROM `system`.`compute`.`warehouse_events`
  WHERE event_time >= date_sub(current_date(), 30)
    AND event_time < current_date()
),
last_query AS (
  SELECT workspace_id, q.compute.warehouse_id AS warehouse_id,
         COALESCE(dateadd(MILLISECOND, COALESCE(result_fetch_duration_ms, 0), end_time), update_time, start_time) AS finished_at
  FROM `system`.`query`.`history` q
  WHERE q.compute.warehouse_id IS NOT NULL
    AND start_time >= date_sub(current_date(), 30) - INTERVAL 48 HOURS
    AND start_time < current_date()
),
prior_start AS (
  -- the last STARTING/RUNNING event before the window, carried in at the window start - without
  -- this, the FIRST stop in the window whose real start event fell before it has no last_start_at
  -- at all and idle_wait_minutes falls back to a query that can be hours older than the actual
  -- (unseen) restart.
  SELECT workspace_id, warehouse_id, MAX(event_time) AS event_time
  FROM `system`.`compute`.`warehouse_events`
  WHERE event_time < date_sub(current_date(), 30)
    AND event_type IN ('STARTING', 'RUNNING')
  GROUP BY workspace_id, warehouse_id
),
timeline AS (
  -- one timeline per (workspace, warehouse) of query-finish times, STARTING/RUNNING events and
  -- STOPPED events, so each stop's idle-from time is a running MAX(...) OVER, not a per-stop
  -- correlated subquery.
  SELECT workspace_id, warehouse_id, finished_at AS t, finished_at AS q_finished_at,
         CAST(NULL AS TIMESTAMP) AS start_at, 0 AS event_rank
  FROM last_query
  UNION ALL
  SELECT workspace_id, warehouse_id, event_time AS t, CAST(NULL AS TIMESTAMP) AS q_finished_at,
         event_time AS start_at, 1 AS event_rank
  FROM events
  WHERE event_type IN ('STARTING', 'RUNNING')
  UNION ALL
  SELECT workspace_id, warehouse_id,
         CAST(date_sub(current_date(), 30) AS TIMESTAMP) AS t,
         CAST(NULL AS TIMESTAMP) AS q_finished_at,
         event_time AS start_at, 1 AS event_rank
  FROM prior_start
  UNION ALL
  SELECT workspace_id, warehouse_id, event_time AS t, CAST(NULL AS TIMESTAMP) AS q_finished_at,
         CAST(NULL AS TIMESTAMP) AS start_at, 2 AS event_rank
  FROM events
  WHERE event_type = 'STOPPED'
),
timeline_ranked AS (
  SELECT workspace_id, warehouse_id, t, event_rank,
         MAX(q_finished_at) OVER (
           PARTITION BY workspace_id, warehouse_id
           ORDER BY t, event_rank
           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
         ) AS last_query_finished_at,
         MAX(start_at) OVER (
           PARTITION BY workspace_id, warehouse_id
           ORDER BY t, event_rank
           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
         ) AS last_start_at
  FROM timeline
),
stops AS (
  SELECT workspace_id, warehouse_id, t AS stop_time, last_query_finished_at, last_start_at
  FROM timeline_ranked
  WHERE event_rank = 2
),
autostop_stops AS (
  SELECT s.workspace_id, s.warehouse_id, s.stop_time,
         (unix_timestamp(s.stop_time) - unix_timestamp(
            CASE WHEN s.last_query_finished_at IS NULL THEN s.last_start_at
                 WHEN s.last_start_at IS NULL THEN s.last_query_finished_at
                 ELSE GREATEST(s.last_query_finished_at, s.last_start_at) END
          )) / 60.0 AS idle_wait_minutes
  FROM stops s
  WHERE s.last_query_finished_at IS NOT NULL OR s.last_start_at IS NOT NULL
),
daily_events AS (
  SELECT workspace_id, warehouse_id, date_trunc('day', event_time) AS day,
         SUM(CASE WHEN event_type = 'STOPPED' THEN 1 ELSE 0 END)  AS stops_that_day,
         SUM(CASE WHEN event_type = 'STARTING' THEN 1 ELSE 0 END) AS starts_that_day
  FROM events
  GROUP BY workspace_id, warehouse_id, date_trunc('day', event_time)
),
stop_setting AS (
  -- the auto-stop setting in force at each stop (warehouses is SCD2), not today's
  SELECT a.workspace_id, a.warehouse_id, a.stop_time, a.idle_wait_minutes, h.auto_stop_minutes
  FROM autostop_stops a
  LEFT JOIN `system`.`compute`.`warehouses` h
    ON h.warehouse_id = a.warehouse_id AND h.change_time <= a.stop_time
  QUALIFY ROW_NUMBER() OVER (PARTITION BY a.workspace_id, a.warehouse_id, a.stop_time ORDER BY h.change_time DESC) = 1
),
daily_autostops AS (
  -- auto-stop off (0) never stops a warehouse, so none of its stops is an auto-stop
  SELECT s.workspace_id, s.warehouse_id, date_trunc('day', s.stop_time) AS day,
         COUNT(*) AS autostop_count,
         SUM(s.idle_wait_minutes) AS autostop_idle_minutes
  FROM stop_setting s
  WHERE s.auto_stop_minutes > 0
    AND s.idle_wait_minutes >= s.auto_stop_minutes - 1
  GROUP BY s.workspace_id, s.warehouse_id, date_trunc('day', s.stop_time)
),
bounds AS (
  SELECT unix_timestamp(CAST(current_date() - INTERVAL 30 DAYS AS TIMESTAMP)) AS win_start_s,
         unix_timestamp(CAST(current_date() AS TIMESTAMP))                            AS win_end_s
),
ev_all AS (
  SELECT e.workspace_id, e.warehouse_id, e.event_type, e.cluster_count,
         unix_timestamp(e.event_time) AS t,
         CASE e.event_type WHEN 'STARTING' THEN 1 WHEN 'RUNNING' THEN 2
                           WHEN 'SCALED_UP' THEN 3 WHEN 'SCALED_DOWN' THEN 3
                           WHEN 'STOPPING' THEN 4 ELSE 5 END AS state_rank
  FROM `system`.`compute`.`warehouse_events` e
  WHERE e.event_time < current_date()
    AND e.event_type IN ('STARTING', 'RUNNING', 'SCALED_UP', 'SCALED_DOWN', 'STOPPING', 'STOPPED')
),
prior AS (
  -- the last event before the window: the state the warehouse was in when the window opened
  SELECT a.workspace_id, a.warehouse_id, a.event_type, a.cluster_count
  FROM ev_all a CROSS JOIN bounds b
  WHERE a.t < b.win_start_s
  QUALIFY ROW_NUMBER() OVER (PARTITION BY a.workspace_id, a.warehouse_id
                             ORDER BY a.t DESC, a.state_rank DESC) = 1
),
wh_ev AS (
  SELECT x.*,
         CASE WHEN x.event_type IN ('STARTING', 'RUNNING', 'SCALED_UP')
                OR (x.event_type = 'SCALED_DOWN' AND COALESCE(x.cluster_count, 1) > 0)
              THEN 1 ELSE 0 END AS is_up
  FROM (
    SELECT a.workspace_id, a.warehouse_id, a.event_type, a.cluster_count, a.t, a.state_rank
    FROM ev_all a CROSS JOIN bounds b
    WHERE a.t >= b.win_start_s
    UNION ALL
    SELECT p.workspace_id, p.warehouse_id, p.event_type, p.cluster_count, b.win_start_s AS t,
           0 AS state_rank
    FROM prior p CROSS JOIN bounds b
  ) x
),
wh_seg AS (
  -- one row per event: up-state holds until the next event, or the window's end for the last one
  SELECT v.*,
         COALESCE(LEAD(v.t) OVER (PARTITION BY v.workspace_id, v.warehouse_id ORDER BY v.t, v.state_rank),
                  b.win_end_s) AS seg_end_s
  FROM wh_ev v CROSS JOIN bounds b
),
wh_up AS (
  -- cluster-seconds, so a scaled-up hour does not cheapen the one-cluster auto-stop wait
  SELECT workspace_id, warehouse_id, MIN(t) AS measured_from_s,
         SUM(is_up * (seg_end_s - t) * GREATEST(COALESCE(cluster_count, 1), 1)) AS up_cluster_seconds
  FROM wh_seg
  GROUP BY workspace_id, warehouse_id
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
wh_cost AS (
  -- same list-price approach as compute_warehouse_idle_minutes: DBUs * effective list price, from
  -- the hour of the first event measured on (billing before it has no up-time to divide by)
  SELECT u.usage_metadata.warehouse_id AS warehouse_id,
         -- any unpriced, non-free usage leaves the rate unknown (NULL), never a smaller $
         CASE WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                            THEN 1 ELSE 0 END) > 0 THEN NULL
              ELSE SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) END AS usd_total
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  JOIN wh_up r ON r.warehouse_id = u.usage_metadata.warehouse_id
  WHERE upper(u.usage_unit) = 'DBU'
    AND unix_timestamp(u.usage_start_time) >= FLOOR(r.measured_from_s / 3600) * 3600
    AND u.usage_metadata.warehouse_id IS NOT NULL
    AND u.usage_date >= date_sub(current_date(), 30)
    AND u.usage_date < current_date()
  GROUP BY u.usage_metadata.warehouse_id
),
wh_rate AS (
  -- dollars per running cluster-minute over the window, applied to each day's auto-stop idle minutes
  SELECT r.warehouse_id,
         CASE WHEN r.up_cluster_seconds > 0 THEN c.usd_total / (r.up_cluster_seconds / 60.0) END AS usd_per_minute
  FROM wh_up r
  LEFT JOIN wh_cost c ON c.warehouse_id = r.warehouse_id
),
daily AS (
  -- one row per warehouse per day - the same per-day shape this query used to return, kept here as
  -- an intermediate so it can be rolled up to one row per warehouse below.
  SELECT
    d.workspace_id,
    d.warehouse_id,
    d.day,
    COALESCE(a.autostop_count, 0) AS autostop_count,
    ROUND(COALESCE(a.autostop_idle_minutes, 0), 1) AS autostop_idle_minutes,
    ROUND(COALESCE(a.autostop_idle_minutes, 0) * rt.usd_per_minute, 2) AS est_wasted_usd_list,
    (upper(w.warehouse_type) IN ('CLASSIC', 'PRO') AND d.starts_that_day >= 5) AS cold_start_risk,
    CASE
      WHEN COALESCE(a.autostop_count, 0) >= 10
           AND w.auto_stop_minutes >= 10 THEN 'CRITICAL'
      WHEN COALESCE(a.autostop_count, 0) >= 5
           AND w.auto_stop_minutes >= 10 THEN 'WARN'
      ELSE 'OK'
    END AS status
  FROM daily_events d
  -- OUTER: a warehouse with events but no compute.warehouses row (deleted before system tables
  -- started recording) still gets a day row here, same as compute_warehouse_idle_minutes' cfg.
  LEFT JOIN wh w ON w.warehouse_id = d.warehouse_id
  LEFT JOIN daily_autostops a
    ON a.workspace_id = d.workspace_id AND a.warehouse_id = d.warehouse_id AND a.day = d.day
  LEFT JOIN wh_rate rt ON rt.warehouse_id = d.warehouse_id
),
ranked AS (
  SELECT *,
         ROW_NUMBER() OVER (
           PARTITION BY workspace_id, warehouse_id
           ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
                    autostop_count DESC, est_wasted_usd_list DESC NULLS LAST, day DESC
         ) AS worst_rank
  FROM daily
)
SELECT
  r.workspace_id,
  r.warehouse_id,
  w.warehouse_name,
  CASE WHEN upper(w.warehouse_type) = 'SERVERLESS' THEN 'serverless'
       WHEN upper(w.warehouse_type) = 'PRO'        THEN 'pro'
       WHEN upper(w.warehouse_type) = 'CLASSIC'     THEN 'classic'
  END AS warehouse_kind,
  w.auto_stop_minutes,
  COUNT(*)                                                       AS days_observed,
  SUM(CASE WHEN d.status != 'OK' THEN 1 ELSE 0 END)               AS days_flagged,
  SUM(CASE WHEN d.cold_start_risk THEN 1 ELSE 0 END)              AS cold_start_risk_days,
  SUM(d.autostop_count)                                           AS total_autostop_count,
  ROUND(SUM(d.autostop_idle_minutes), 1)                          AS total_autostop_idle_minutes,
  ROUND(SUM(d.est_wasted_usd_list), 2)                            AS total_est_wasted_usd_list,
  r.day                                                           AS worst_day,
  r.autostop_count                                                AS worst_day_autostop_count,
  r.est_wasted_usd_list                                           AS worst_day_est_wasted_usd_list,
  r.status
FROM ranked r
LEFT JOIN wh w ON w.warehouse_id = r.warehouse_id
JOIN daily d ON d.workspace_id = r.workspace_id AND d.warehouse_id = r.warehouse_id
WHERE r.worst_rank = 1
GROUP BY r.workspace_id, r.warehouse_id, w.warehouse_name, w.warehouse_type, w.auto_stop_minutes,
         r.day, r.autostop_count, r.est_wasted_usd_list, r.status
ORDER BY CASE r.status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
         total_est_wasted_usd_list DESC NULLS LAST, r.warehouse_id
) q
"""
CHECKS["compute_warehouse_late_autostop"] = r"""
-- generated from dbt/models/databricks_direct/compute/d_compute_warehouse_late_autostop.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/compute/compute_warehouse_late_autostop.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH wh AS (
  SELECT warehouse_id, warehouse_name, warehouse_type, auto_stop_minutes
  FROM `system`.`compute`.`warehouses`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
),
ev AS (
  -- documented events from 2 days before the window, so the first stop has its start and state
  SELECT workspace_id, warehouse_id, event_type, event_time AS ts,
         CASE event_type WHEN 'STARTING' THEN 1 WHEN 'RUNNING' THEN 2
                         WHEN 'SCALED_UP' THEN 3 WHEN 'SCALED_DOWN' THEN 3
                         WHEN 'STOPPING' THEN 4 ELSE 5 END AS state_rank,
         CASE WHEN event_type IN ('STOPPING', 'STOPPED')
                OR (event_type = 'SCALED_DOWN' AND COALESCE(cluster_count, 1) = 0)
              THEN 0 ELSE 1 END AS is_up
  FROM `system`.`compute`.`warehouse_events`
  WHERE event_time >= date_sub(current_date(), 30) - INTERVAL 48 HOURS
    AND event_time < current_date()
    AND event_type IN ('STARTING', 'RUNNING', 'SCALED_UP', 'SCALED_DOWN', 'STOPPING', 'STOPPED')
),
ev_seq AS (
  -- a stop is the first down event after an up one
  SELECT e.*,
         e.is_up = 0 AND LAG(e.is_up) OVER (PARTITION BY e.workspace_id, e.warehouse_id
                                            ORDER BY e.ts, e.state_rank) = 1 AS is_stop
  FROM ev e
),
timeline AS (
  -- statements at their start carrying their end, and every state event; a running MAX over it
  -- gives each stop its last activity without a per-stop scan
  SELECT q.workspace_id, q.compute.warehouse_id AS warehouse_id, q.start_time AS ts, 0 AS state_rank,
         COALESCE(dateadd(MILLISECOND, COALESCE(q.result_fetch_duration_ms, 0), q.end_time), q.update_time, q.start_time) AS stmt_end,
         CAST(NULL AS TIMESTAMP) AS start_ts, FALSE AS is_stop
  FROM `system`.`query`.`history` q
  WHERE q.compute.warehouse_id IS NOT NULL
    AND q.start_time >= date_sub(current_date(), 30) - INTERVAL 48 HOURS
    AND q.start_time < current_date()
  UNION ALL
  SELECT workspace_id, warehouse_id, ts, state_rank, CAST(NULL AS TIMESTAMP) AS stmt_end,
         CASE WHEN event_type IN ('STARTING', 'RUNNING') THEN ts END AS start_ts,
         COALESCE(is_stop, FALSE) AS is_stop
  FROM ev_seq
),
ordered AS (
  SELECT workspace_id, warehouse_id, ts, is_stop,
         MAX(stmt_end) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY ts, state_rank
                             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS last_end,
         MAX(start_ts) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY ts, state_rank
                             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS last_start
  FROM timeline
),
stops AS (
  -- a statement still running at the stop means it was not an auto-stop
  SELECT workspace_id, warehouse_id, ts AS stop_at,
         GREATEST(last_end, last_start) AS idle_from
  FROM ordered
  WHERE is_stop
    AND ts >= CAST(date_sub(current_date(), 30) AS TIMESTAMP)
    AND GREATEST(last_end, last_start) IS NOT NULL
    AND (last_end IS NULL OR last_end < ts)
),
judged AS (
  -- the auto-stop setting in force at each stop, else the earliest recorded one
  SELECT s.workspace_id, s.warehouse_id, s.stop_at, s.idle_from,
         unix_timestamp(s.stop_at) - unix_timestamp(s.idle_from) AS wait_s,
         h.auto_stop_minutes
  FROM stops s
  JOIN `system`.`compute`.`warehouses` h ON h.warehouse_id = s.warehouse_id
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY s.workspace_id, s.warehouse_id, s.stop_at
    ORDER BY CASE WHEN h.change_time <= s.stop_at THEN 0 ELSE 1 END,
             CASE WHEN h.change_time <= s.stop_at THEN h.change_time END DESC,
             h.change_time) = 1
),
autostops AS (
  SELECT j.*,
         j.wait_s > j.auto_stop_minutes * 60 * (1 + 20 / 100.0)
           AND j.wait_s - j.auto_stop_minutes * 60 > 60 AS is_late
  FROM judged j
  WHERE j.auto_stop_minutes > 0
    AND j.wait_s >= j.auto_stop_minutes * 60 - 60
),
longest AS (
  SELECT workspace_id, warehouse_id, wait_s, idle_from, stop_at
  FROM autostops
  WHERE is_late
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id ORDER BY wait_s DESC, stop_at DESC) = 1
),
agg AS (
  SELECT workspace_id, warehouse_id,
         COUNT(*) AS auto_stops,
         percentile(wait_s, 0.5) AS typical_wait_s,
         SUM(CASE WHEN is_late THEN 1 ELSE 0 END) AS late_stops,
         SUM(CASE WHEN is_late THEN wait_s - auto_stop_minutes * 60 ELSE 0 END) AS late_extra_s
  FROM autostops
  GROUP BY workspace_id, warehouse_id
)
SELECT a.workspace_id,
       a.warehouse_id,
       w.warehouse_name,
       CASE WHEN upper(w.warehouse_type) = 'SERVERLESS' THEN 'serverless'
            WHEN upper(w.warehouse_type) = 'PRO'        THEN 'pro'
            WHEN upper(w.warehouse_type) = 'CLASSIC'    THEN 'classic'
       END                                                   AS warehouse_kind,
       w.auto_stop_minutes,
       a.auto_stops,
       ROUND(a.typical_wait_s / 60.0, 1)                     AS typical_wait_minutes,
       a.late_stops,
       ROUND(a.late_extra_s / 60.0, 1)                       AS late_extra_minutes,
       ROUND(l.wait_s / 60.0, 1)                             AS longest_wait_minutes,
       l.idle_from                                           AS last_activity_at,
       l.stop_at,
       CASE WHEN a.late_stops = 0
            THEN CONCAT('all ', a.auto_stops, ' auto-stops within ', 20, '% of the setting')
            ELSE CONCAT(a.late_stops, ' of ', a.auto_stops, ' auto-stops came more than ', 20,
                        '% after the setting (', CAST(ROUND(a.late_extra_s / 60.0, 0) AS BIGINT),
                        ' extra min); longest ', CAST(ROUND(l.wait_s / 60.0, 1) AS STRING), ' min')
       END                                                   AS late_reason,
       CASE WHEN a.late_stops >= 3 THEN 'WARN' ELSE 'OK' END AS status
FROM agg a
LEFT JOIN wh w      ON w.warehouse_id = a.warehouse_id
LEFT JOIN longest l ON l.workspace_id = a.workspace_id AND l.warehouse_id = a.warehouse_id
ORDER BY CASE WHEN a.late_stops >= 3 THEN 0 ELSE 1 END,
         late_extra_minutes DESC, a.warehouse_id
) q
"""
CHECKS["sql_warehouse_events_activity"] = r"""
-- generated from dbt/models/databricks_direct/compute/d_sql_warehouse_events_activity.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/compute/sql_warehouse_events_activity.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH current_warehouses AS (
  SELECT workspace_id, warehouse_id, warehouse_name, first_seen_time
  FROM (
    SELECT workspace_id, warehouse_id, warehouse_name, delete_time,
           MIN(change_time) OVER (PARTITION BY workspace_id, warehouse_id) AS first_seen_time,
           ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id ORDER BY change_time DESC) AS rn
    FROM `system`.`compute`.`warehouses`
  )
  WHERE rn = 1 AND delete_time IS NULL
),
events AS (
  SELECT workspace_id, warehouse_id, event_type,
         COUNT(*)           AS event_count,
         MAX(event_time)    AS last_event_time,
         MIN(event_time)    AS first_event_time,
         MAX(cluster_count) AS max_cluster_count,
         AVG(cluster_count) AS avg_cluster_count
  FROM `system`.`compute`.`warehouse_events`
  WHERE event_time >= current_timestamp() - INTERVAL 30 DAYS
  GROUP BY workspace_id, warehouse_id, event_type
),
warehouse_keys AS (
  -- current warehouses plus any warehouse with events, so a deleted/never-captured one still shows
  SELECT workspace_id, warehouse_id FROM current_warehouses
  UNION
  SELECT workspace_id, warehouse_id FROM events
),
running_events AS (
  SELECT workspace_id, warehouse_id, MAX(event_time) AS last_running_event_time
  FROM `system`.`compute`.`warehouse_events`
  WHERE event_type IN ('RUNNING', 'STARTING')
    AND event_time >= current_timestamp() - INTERVAL 30 DAYS
  GROUP BY workspace_id, warehouse_id
),
last_state AS (
  -- the warehouse's latest event in the window: one that leaves it running means it is up, not stale
  SELECT workspace_id, warehouse_id, event_type AS last_event_type
  FROM `system`.`compute`.`warehouse_events`
  WHERE event_time >= current_timestamp() - INTERVAL 30 DAYS
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id ORDER BY event_time DESC) = 1
),
billed AS (
  SELECT workspace_id, usage_metadata.warehouse_id AS warehouse_id,
         SUM(usage_quantity) AS net_dbus
  FROM `system`.`billing`.`usage`
  WHERE upper(usage_unit) = 'DBU'
    AND usage_metadata.warehouse_id IS NOT NULL
    AND usage_date >= dateadd(day, -30, current_date())
    AND usage_date < current_date()
  GROUP BY workspace_id, usage_metadata.warehouse_id
)
SELECT wk.workspace_id, wk.warehouse_id, cw.warehouse_name,
       e.event_type,
       COALESCE(e.event_count, 0) AS event_count,
       e.last_event_time, e.first_event_time, e.max_cluster_count, e.avg_cluster_count,
       COALESCE(b.net_dbus, 0) AS net_dbus,
       CASE
         WHEN e.event_type IS NULL AND cw.first_seen_time >= dateadd(day, -30, current_date())
              THEN 'NOT_ASSESSED'                      -- first seen within the window: no activity expected yet
         WHEN e.event_type IS NULL AND COALESCE(b.net_dbus, 0) > 0
              THEN 'NOT_ASSESSED'                       -- billed with zero events: likely always-on, not confirmably dormant
         WHEN e.event_type IS NULL THEN 'CRITICAL'      -- zero events AND zero billed DBUs: genuinely dormant
         WHEN e.event_type NOT IN ('RUNNING', 'STARTING') THEN 'NOT_ASSESSED'
         WHEN ls.last_event_type IN ('RUNNING', 'STARTING', 'SCALED_UP', 'SCALED_DOWN') THEN 'OK'
         WHEN (unix_timestamp(current_timestamp()) - unix_timestamp(re.last_running_event_time)) / 86400.0 >= 14 THEN 'CRITICAL'
         WHEN (unix_timestamp(current_timestamp()) - unix_timestamp(re.last_running_event_time)) / 86400.0 >= 7 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM warehouse_keys wk
LEFT JOIN current_warehouses cw ON wk.workspace_id = cw.workspace_id AND wk.warehouse_id = cw.warehouse_id
LEFT JOIN events e          ON wk.workspace_id = e.workspace_id AND wk.warehouse_id = e.warehouse_id
LEFT JOIN running_events re ON wk.workspace_id = re.workspace_id AND wk.warehouse_id = re.warehouse_id
LEFT JOIN billed b          ON wk.workspace_id = b.workspace_id AND wk.warehouse_id = b.warehouse_id
LEFT JOIN last_state ls     ON wk.workspace_id = ls.workspace_id AND wk.warehouse_id = ls.warehouse_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END,
         last_event_time ASC NULLS FIRST
) q
"""
CHECKS["sql_warehouse_config_current"] = r"""
-- generated from dbt/models/databricks_direct/compute/d_sql_warehouse_config_current.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/compute/sql_warehouse_config_current.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT warehouse_id, warehouse_name AS warehouse_name, workspace_id, account_id, warehouse_type, warehouse_channel,
       warehouse_size, min_clusters, max_clusters, auto_stop_minutes, tags, change_time, delete_time
FROM (
  SELECT warehouse_id, warehouse_name, workspace_id, account_id, warehouse_type, warehouse_channel,
         warehouse_size, min_clusters, max_clusters, auto_stop_minutes, tags, change_time, delete_time,
         ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) AS rn
  FROM `system`.`compute`.`warehouses`
)
WHERE rn = 1 AND delete_time IS NULL
ORDER BY warehouse_id
) q
"""
CHECKS["node_timeline_utilization"] = r"""
-- generated from dbt/models/databricks_direct/compute/d_node_timeline_utilization.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/compute/node_timeline_utilization.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT cluster_id, node_type, driver,
       COUNT(*) AS minute_rows,
       MIN(start_time) AS first_minute, MAX(end_time) AS last_minute,
       AVG(cpu_user_percent + cpu_system_percent) AS avg_cpu_pct,
       MAX(cpu_user_percent + cpu_system_percent) AS peak_cpu_pct,
       AVG(mem_used_percent) AS avg_mem_pct, MAX(mem_used_percent) AS peak_mem_pct,
       AVG(cpu_wait_percent) AS avg_cpu_wait_pct,
       SUM(network_sent_bytes)     AS total_network_sent_bytes,
       SUM(network_received_bytes) AS total_network_received_bytes,
       -- status: oversized-candidate band (field heuristic). Too few slices -> NOT_ASSESSED.
       CASE
         WHEN COUNT(*) < 15 THEN 'NOT_ASSESSED'
         WHEN AVG(cpu_user_percent + cpu_system_percent) < 20
          AND AVG(mem_used_percent) < 30 THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE WHEN COUNT(*) < 15
            THEN concat(CAST(COUNT(*) AS STRING), ' of ', CAST(15 AS STRING), ' minutes needed')
       END AS not_assessed_reason
FROM `system`.`compute`.`node_timeline`
WHERE start_time >= dateadd(DAY, -LEAST(30, 90), current_date())
GROUP BY cluster_id, node_type, driver
ORDER BY
  CASE status WHEN 'WARN' THEN 0 WHEN 'NOT_ASSESSED' THEN 1 ELSE 2 END,
  avg_cpu_pct ASC
LIMIT 100000
) q
"""
CHECKS["instance_events_idle_active"] = r"""
-- generated from dbt/models/databricks_direct/compute/d_instance_events_idle_active.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/compute/instance_events_idle_active.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT workspace_id, node_type, availability_type, state, event_type,
       COUNT(*) AS event_count,
       COUNT(DISTINCT instance_id) AS instance_count,
       MIN(event_time) AS first_event_time, MAX(event_time) AS last_event_time
FROM `system`.`compute`.`instance_events`
WHERE event_time >= current_timestamp() - INTERVAL 30 DAYS
GROUP BY workspace_id, node_type, availability_type, state, event_type
ORDER BY event_count DESC, instance_count DESC
) q
"""
CHECKS["classic_clusters_config_current"] = r"""
-- generated from dbt/models/databricks_direct/compute/d_classic_clusters_config_current.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/compute/classic_clusters_config_current.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT cluster_id,
       cluster_name AS cluster_name,
       owned_by AS owned_by,
       driver_node_type, worker_node_type, worker_count,
       min_autoscale_workers, max_autoscale_workers, auto_termination_minutes, enable_elastic_disk,
       cluster_source, dbr_version, data_security_mode, policy_id, driver_instance_pool_id,
       worker_instance_pool_id, tags, init_scripts, aws_attributes, azure_attributes, gcp_attributes,
       create_time, delete_time, change_time, workspace_id, account_id
FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY cluster_id ORDER BY change_time DESC) AS rn
  FROM `system`.`compute`.`clusters`
)
WHERE rn = 1 AND delete_time IS NULL
ORDER BY cluster_id
) q
"""
CHECKS["node_types_reference"] = r"""
-- generated from dbt/models/databricks_direct/compute/d_node_types_reference.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/compute/node_types_reference.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT node_type, core_count, memory_mb, gpu_count, account_id
FROM `system`.`compute`.`node_types`
ORDER BY node_type
) q
"""
CHECKS["compute_cluster_config_posture"] = r"""
-- generated from dbt/models/databricks_direct/compute/d_compute_cluster_config_posture.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/compute/compute_cluster_config_posture.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
WITH latest AS (
  SELECT *,
         ROW_NUMBER() OVER (PARTITION BY cluster_id ORDER BY change_time DESC) AS rn
  FROM `system`.`compute`.`clusters`
),
current_clusters AS (
  SELECT * FROM latest WHERE rn = 1 AND delete_time IS NULL
),
flagged AS (
  SELECT
    cluster_id, workspace_id, account_id, cluster_name, owned_by, cluster_source, dbr_version,
    data_security_mode, policy_id, worker_count, min_autoscale_workers, max_autoscale_workers,
    auto_termination_minutes, create_time, change_time,
    (cluster_source IN ('UI', 'API')) AS is_all_purpose,
    (data_security_mode = 'NONE' OR data_security_mode LIKE 'LEGACY_%')  AS flag_no_isolation,
    (dbr_version RLIKE '^([0-9]|1[0-3])[.]' OR dbr_version RLIKE '^14[.][0-2][.]'
     OR dbr_version RLIKE '^1[56][.][0-3][.]' OR dbr_version RLIKE '^17[.][0-2][.]'
     OR dbr_version RLIKE '^18[.][01][.]')                                     AS flag_eol_runtime,
    (policy_id IS NULL)                                                       AS flag_no_policy,
    ((cluster_source IN ('UI', 'API'))
     AND (auto_termination_minutes IS NULL OR auto_termination_minutes = 0
          OR auto_termination_minutes > 120))               AS flag_auto_term_risk
  FROM current_clusters
)
SELECT
  cluster_id,
  workspace_id,
  cluster_name,
  owned_by                                                                         AS owned_by,
  cluster_source,
  is_all_purpose,
  dbr_version,
  data_security_mode,
  policy_id,
  worker_count, min_autoscale_workers, max_autoscale_workers, auto_termination_minutes,
  flag_eol_runtime,
  flag_no_isolation,
  flag_no_policy,
  flag_auto_term_risk,
  CONCAT_WS('; ',
    CASE WHEN flag_no_isolation THEN
      CONCAT('access mode ', data_security_mode, ' bypasses Unity Catalog (no user isolation)')
    END,
    CASE WHEN data_security_mode IS NULL THEN
      'access mode not recorded, so Unity Catalog isolation could not be checked'
    END,
    CASE WHEN flag_eol_runtime THEN
      CONCAT('runtime ', dbr_version, ' is past Databricks end-of-support')
    END,
    CASE WHEN flag_no_policy THEN 'no cluster policy attached' END,
    CASE WHEN flag_auto_term_risk THEN
      CASE
        WHEN auto_termination_minutes IS NULL OR auto_termination_minutes = 0
          THEN 'auto-termination is off'
        ELSE CONCAT('auto-termination ', CAST(auto_termination_minutes AS STRING),
                     ' min, above the ', CAST(120 AS STRING), ' min limit')
      END
    END
  )                                                                           AS reasons,
  CASE
    WHEN flag_no_isolation THEN 'CRITICAL'
    WHEN flag_eol_runtime OR flag_no_policy OR flag_auto_term_risk THEN 'WARN'
    WHEN data_security_mode IS NULL THEN 'NOT_ASSESSED'
    ELSE 'OK'
  END                                                                         AS status,
  create_time, change_time
FROM flagged
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         cluster_id
) q
"""
CHECKS["compute_warehouse_config_posture"] = r"""
-- generated from dbt/models/databricks_direct/compute/d_compute_warehouse_config_posture.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/compute/compute_warehouse_config_posture.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
WITH latest AS (
  SELECT *,
         ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) AS rn
  FROM `system`.`compute`.`warehouses`
),
current_warehouses AS (
  SELECT * FROM latest WHERE rn = 1 AND delete_time IS NULL
),
flagged AS (
  SELECT
    warehouse_id, workspace_id, account_id, warehouse_name, created_by, warehouse_type,
    warehouse_channel, warehouse_size, min_clusters, max_clusters, auto_stop_minutes, change_time,
    (auto_stop_minutes IS NULL OR auto_stop_minutes = 0
     OR auto_stop_minutes > 60)          AS flag_autostop_risk,
    (warehouse_type = 'CLASSIC')                             AS flag_classic_type,
    (warehouse_channel IN ('CHANNEL_NAME_PREVIEW', 'PREVIEW')) AS flag_preview_channel
  FROM current_warehouses
)
SELECT
  warehouse_id,
  workspace_id,
  warehouse_name,
  created_by                                                         AS created_by,
  warehouse_type,
  warehouse_channel,
  warehouse_size,
  min_clusters, max_clusters, auto_stop_minutes,
  flag_autostop_risk,
  flag_classic_type,
  flag_preview_channel,
  CONCAT_WS('; ',
    CASE WHEN flag_autostop_risk THEN
      CASE
        WHEN auto_stop_minutes IS NULL OR auto_stop_minutes = 0 THEN 'auto-stop is off'
        ELSE CONCAT('auto-stop ', CAST(auto_stop_minutes AS STRING), ' min, above the ',
                     CAST(60 AS STRING), ' min limit')
      END
    END,
    CASE WHEN flag_classic_type THEN
      'CLASSIC type; Pro/Serverless offers faster start-up and Unity Catalog-native governance'
    END,
    CASE WHEN flag_preview_channel THEN
      'on the Preview channel (pre-release features, confirm this is intentional)'
    END
  )                                                           AS reasons,
  CASE
    WHEN flag_autostop_risk OR flag_classic_type OR flag_preview_channel THEN 'WARN'
    ELSE 'OK'
  END                                                         AS status,
  change_time
FROM flagged
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END,
         warehouse_id
) q
"""
CHECKS["lakeflow_failed_runs"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_failed_runs.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_failed_runs.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH end_rows AS (
  SELECT workspace_id, job_id, run_id, run_type, trigger_type,
         result_state, termination_code, period_start_time, period_end_time
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())   -- drop incomplete current day
    AND result_state IS NOT NULL                                   -- end row only
),
latest_jobs AS (
  SELECT workspace_id, job_id, name AS job_name
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),
grouped AS (
  SELECT workspace_id, job_id, run_type, trigger_type, result_state, termination_code,
         COUNT(*)                AS run_rows,
         COUNT(DISTINCT run_id)  AS distinct_runs,
         SUM(CASE WHEN result_state IN ('FAILED','ERROR','TIMED_OUT') THEN 1 ELSE 0 END) AS failed_run_rows
  FROM end_rows
  GROUP BY workspace_id, job_id, run_type, trigger_type, result_state, termination_code
)
SELECT g.workspace_id, g.job_id, j.job_name, w.workspace_name,
       g.run_type, g.trigger_type, g.result_state, g.termination_code,
       g.run_rows, g.distinct_runs, g.failed_run_rows,
       -- status: worst-first band on failed run rows per group (field heuristic; 5 / 20).
       CASE
         WHEN g.failed_run_rows >= 20 THEN 'CRITICAL'
         WHEN g.failed_run_rows >= 5 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM grouped g
LEFT JOIN latest_jobs j ON j.workspace_id = g.workspace_id AND j.job_id = g.job_id
LEFT JOIN `system`.`access`.`workspaces_latest` w ON w.workspace_id = g.workspace_id
ORDER BY failed_run_rows DESC
) q
"""
CHECKS["lakeflow_never_started_runs"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_never_started_runs.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_never_started_runs.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH runs AS (
  SELECT workspace_id, job_id, run_id, termination_code, period_start_time, period_end_time
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())
    AND result_state IS NOT NULL
),
task_rows AS (
  SELECT DISTINCT workspace_id, job_id, job_run_id AS run_id
  FROM `system`.`lakeflow`.`job_task_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
),
flagged AS (
  SELECT r.workspace_id, r.job_id, r.run_id, r.termination_code,
         (r.period_start_time = r.period_end_time)                               AS is_zero_length,
         (r.termination_code = 'INVALID_RUN_CONFIGURATION' AND t.run_id IS NULL) AS is_invalid_config_no_tasks
  FROM runs r
  LEFT JOIN task_rows t
    ON  t.workspace_id = r.workspace_id
    AND t.job_id       = r.job_id
    AND t.run_id       = r.run_id
),
grouped AS (
  SELECT workspace_id, job_id, termination_code,
         COUNT(DISTINCT CASE WHEN is_zero_length THEN run_id END)                                AS never_started_runs_zero_length,
         COUNT(DISTINCT CASE WHEN is_invalid_config_no_tasks THEN run_id END)                    AS never_started_runs_invalid_config_no_tasks,
         COUNT(DISTINCT CASE WHEN is_zero_length OR is_invalid_config_no_tasks THEN run_id END) AS never_started_runs
  FROM flagged
  GROUP BY workspace_id, job_id, termination_code
),
latest_jobs AS (
  SELECT workspace_id, job_id, name AS job_name
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
)
SELECT g.workspace_id, g.job_id, j.job_name, w.workspace_name, g.termination_code,
       g.never_started_runs_zero_length, g.never_started_runs_invalid_config_no_tasks,
       g.never_started_runs,
       -- status: worst-first band on never-started run count (field heuristic; 3 / 10).
       CASE
         WHEN g.never_started_runs >= 10 THEN 'CRITICAL'
         WHEN g.never_started_runs >= 3 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM grouped g
LEFT JOIN latest_jobs j ON j.workspace_id = g.workspace_id AND j.job_id = g.job_id
LEFT JOIN `system`.`access`.`workspaces_latest` w ON w.workspace_id = g.workspace_id
ORDER BY never_started_runs DESC
) q
"""
CHECKS["lakeflow_succeeded_with_failed_tasks"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_succeeded_with_failed_tasks.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_succeeded_with_failed_tasks.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH job_end AS (
  SELECT workspace_id, job_id, run_id AS job_run_id, result_state AS job_result_state
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())
    AND result_state IS NOT NULL
),
task_end AS (
  -- each task's last result in the run: a task that failed and then succeeded on a retry or repair
  -- did not leave a failure behind
  SELECT workspace_id, job_id, job_run_id, task_key, result_state AS task_result_state
  FROM `system`.`lakeflow`.`job_task_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
    AND result_state IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id, job_run_id, task_key ORDER BY period_end_time DESC) = 1
)
SELECT j.workspace_id, j.job_id,
       COUNT(DISTINCT j.job_run_id) AS succeeded_runs,
       COUNT(DISTINCT CASE WHEN t.task_result_state IN ('FAILED','ERROR','TIMED_OUT')
                           THEN j.job_run_id END) AS succeeded_runs_with_failed_task,
       -- status: worst-first band on succeeded-but-had-a-failed-task run count (field heuristic;
       -- 3 / 10).
       CASE
         WHEN COUNT(DISTINCT CASE WHEN t.task_result_state IN ('FAILED','ERROR','TIMED_OUT') THEN j.job_run_id END) >= 10 THEN 'CRITICAL'
         WHEN COUNT(DISTINCT CASE WHEN t.task_result_state IN ('FAILED','ERROR','TIMED_OUT') THEN j.job_run_id END) >= 3 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM job_end j
LEFT JOIN task_end t
  ON j.workspace_id = t.workspace_id AND j.job_id = t.job_id AND j.job_run_id = t.job_run_id
WHERE j.job_result_state = 'SUCCEEDED'
GROUP BY j.workspace_id, j.job_id
ORDER BY succeeded_runs_with_failed_task DESC
) q
"""
CHECKS["lakeflow_termination_taxonomy"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_termination_taxonomy.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_termination_taxonomy.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT workspace_id, job_id, termination_code,
       COUNT(*)              AS run_rows,
       COUNT(DISTINCT run_id) AS distinct_runs
FROM `system`.`lakeflow`.`job_run_timeline`
WHERE period_start_time >= dateadd(day, -30, current_date())
  AND period_end_time < date_trunc('DAY', current_timestamp())
  AND result_state IS NOT NULL          -- end row only
  AND termination_code IS NOT NULL
GROUP BY workspace_id, job_id, termination_code
ORDER BY workspace_id, run_rows DESC
) q
"""
CHECKS["lakeflow_daily_state"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_daily_state.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/jobs_pipelines/lakeflow_daily_state.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH run_rows AS (
  -- A repair keeps the run_id: rows after an earlier end row belong to the next attempt.
  SELECT workspace_id, job_id, run_id, period_start_time, period_end_time, result_state,
         NULLIF(run_duration_seconds, 0) AS run_s_reported,
         MAX(CASE WHEN result_state IS NOT NULL THEN period_end_time END) OVER (
           PARTITION BY workspace_id, job_id, run_id ORDER BY period_start_time
           ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_attempt_end
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
),
attempts AS (
  SELECT workspace_id, job_id, run_id,
         MIN(period_start_time)                                            AS attempt_start,
         MAX(CASE WHEN result_state IS NOT NULL THEN period_end_time END)  AS attempt_end,
         MAX(CASE WHEN result_state IS NOT NULL THEN result_state END)     AS result_state,
         MAX(CASE WHEN result_state IS NOT NULL THEN run_s_reported END)   AS run_s_reported
  FROM run_rows
  GROUP BY workspace_id, job_id, run_id, prev_attempt_end
),
job_final AS (
  -- each run once, on its final attempt; the day is the run's first start
  SELECT workspace_id, job_id, run_id, attempt_start, attempt_end, result_state, run_s_reported,
         MIN(attempt_start) OVER (PARTITION BY workspace_id, job_id, run_id) AS run_start
  FROM attempts
  WHERE attempt_end IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id, run_id ORDER BY attempt_end DESC) = 1
),
job_runs AS (
  SELECT 'job' AS resource_type, workspace_id, job_id AS resource_id, date(run_start) AS usage_date,
         result_state,
         COALESCE(run_s_reported, timestampdiff(SECOND, attempt_start, attempt_end)) AS run_s
  FROM job_final
  WHERE attempt_end < date_trunc('DAY', current_timestamp())
),
update_rows AS (
  SELECT workspace_id, pipeline_id, update_id, period_end_time, result_state,
         MIN(period_start_time) OVER (PARTITION BY workspace_id, pipeline_id, update_id) AS update_start
  FROM `system`.`lakeflow`.`pipeline_update_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
),
updates AS (
  SELECT 'pipeline' AS resource_type, workspace_id, pipeline_id AS resource_id, date(update_start) AS usage_date,
         result_state,
         timestampdiff(SECOND, update_start, period_end_time) AS run_s
  FROM update_rows
  WHERE result_state IS NOT NULL
    AND period_end_time < date_trunc('DAY', current_timestamp())
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id, update_id ORDER BY period_end_time DESC) = 1
),
all_runs AS (
  SELECT * FROM job_runs
  UNION ALL SELECT * FROM updates
),
typical AS (
  SELECT resource_type, workspace_id, resource_id, percentile(run_s, 0.5) AS median_run_s
  FROM all_runs
  WHERE result_state NOT IN ('SKIPPED', 'CANCELED', 'CANCELLED')
  GROUP BY resource_type, workspace_id, resource_id
),
daily AS (
  SELECT resource_type, workspace_id, resource_id, usage_date,
         COUNT(*) AS runs,
         SUM(CASE WHEN result_state IN ('FAILED', 'ERROR', 'TIMED_OUT') THEN 1 ELSE 0 END) AS failed,
         SUM(CASE WHEN result_state IN ('SKIPPED', 'CANCELED', 'CANCELLED') THEN 1 ELSE 0 END) AS skipped,
         SUM(CASE WHEN result_state NOT IN ('SKIPPED', 'CANCELED', 'CANCELLED') THEN 1 ELSE 0 END) AS timed_runs,
         SUM(CASE WHEN result_state NOT IN ('SKIPPED', 'CANCELED', 'CANCELLED') THEN run_s END) AS total_run_s,
         MAX(CASE WHEN result_state NOT IN ('SKIPPED', 'CANCELED', 'CANCELLED') THEN run_s END) AS max_run_s,
         AVG(CASE WHEN result_state NOT IN ('SKIPPED', 'CANCELED', 'CANCELLED') THEN run_s END) AS avg_run_s
  FROM all_runs
  GROUP BY resource_type, workspace_id, resource_id, usage_date
),
stated AS (
SELECT d.usage_date,
       d.resource_type,
       concat(COALESCE(d.workspace_id, 'account'), ':', d.resource_id) AS resource_key,
       d.workspace_id,
       d.resource_id,
       d.runs,
       d.failed,
       d.skipped,
       d.timed_runs,
       ROUND(COALESCE(d.total_run_s, 0), 1) AS total_run_s,
       ROUND(d.max_run_s, 1) AS max_run_s,
       ROUND(d.avg_run_s, 1) AS avg_run_s,
       ROUND(t.median_run_s, 1) AS median_run_s,
       CASE
         WHEN d.failed > 0 THEN 'failed'
         WHEN d.avg_run_s IS NOT NULL AND t.median_run_s > 0
              AND d.avg_run_s >= 1.5 * t.median_run_s
              AND d.avg_run_s - t.median_run_s >= 60 THEN 'slow'
         WHEN d.skipped = d.runs THEN 'skipped'
         ELSE 'ok'
       END AS day_state
FROM daily d
LEFT JOIN typical t
  ON  t.resource_type = d.resource_type AND t.workspace_id IS NOT DISTINCT FROM d.workspace_id
  AND t.resource_id = d.resource_id
),
per_resource AS (
  SELECT stated.*,
         SUM(CASE WHEN day_state = 'failed' THEN 1 ELSE 0 END) OVER (PARTITION BY resource_type, resource_key) AS failed_days,
         SUM(CASE WHEN day_state = 'slow' THEN 1 ELSE 0 END) OVER (PARTITION BY resource_type, resource_key) AS slow_days,
         SUM(failed) OVER (PARTITION BY resource_type, resource_key) AS resource_failed
  FROM stated
)
SELECT usage_date, resource_type, resource_key, workspace_id, resource_id,
       CASE WHEN resource_type = 'job' THEN resource_id END AS job_id,
       CASE WHEN resource_type = 'pipeline' THEN resource_id END AS pipeline_id,
       runs, failed, skipped,
       timed_runs, total_run_s, max_run_s, avg_run_s, median_run_s, day_state, failed_days, slow_days,
       DENSE_RANK() OVER (ORDER BY failed_days DESC, slow_days DESC, resource_failed DESC,
                                   resource_type, resource_key) AS trouble_rank
FROM per_resource
ORDER BY trouble_rank, usage_date
) q
"""
CHECKS["lakeflow_job_queue_time"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_job_queue_time.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_job_queue_time.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH end_rows AS (
  SELECT workspace_id, job_id, run_id, queue_duration_seconds, run_duration_seconds
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())   -- drop incomplete current day
    AND result_state IS NOT NULL                                   -- end row only
)
SELECT workspace_id, job_id,
       COUNT(DISTINCT run_id)                                   AS distinct_runs,
       SUM(CASE WHEN run_duration_seconds > 0 THEN queue_duration_seconds END)                   AS queue_s_total,
       percentile(CASE WHEN run_duration_seconds > 0 THEN queue_duration_seconds END, 0.95) AS queue_s_p95,
       SUM(CASE WHEN run_duration_seconds > 0 AND queue_duration_seconds IS NOT NULL THEN 0 ELSE 1 END) AS runs_queue_null,
       -- status: worst-first band on p95 queue seconds (field heuristic; 60 / 300).
       -- A job with no run reporting real wall-clock time has nothing to band on -> NOT_ASSESSED,
       -- distinct from every observed run's queue phase folding to NULL (also NOT_ASSESSED, via
       -- the p95-is-NULL branch below). Reading queue_duration_seconds only where
       -- run_duration_seconds > 0 keeps a genuine 0-second queue on a legacy single-task job as a
       -- real 0 (OK), instead of NULLIF(queue_duration_seconds, 0) folding EVERY 0 to NULL and
       -- inflating p95 off queued runs alone.
       CASE
         WHEN SUM(CASE WHEN run_duration_seconds > 0 THEN 1 ELSE 0 END) = 0 THEN 'NOT_ASSESSED'
         WHEN percentile(CASE WHEN run_duration_seconds > 0 THEN queue_duration_seconds END, 0.95) IS NULL THEN 'NOT_ASSESSED'
         WHEN percentile(CASE WHEN run_duration_seconds > 0 THEN queue_duration_seconds END, 0.95) >= 300 THEN 'CRITICAL'
         WHEN percentile(CASE WHEN run_duration_seconds > 0 THEN queue_duration_seconds END, 0.95) >= 60 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM end_rows
GROUP BY workspace_id, job_id
ORDER BY queue_s_p95 DESC
) q
"""
CHECKS["lakeflow_phase_cold_start"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_phase_cold_start.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_phase_cold_start.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH run_level AS (
  SELECT workspace_id, job_id,
         COUNT(DISTINCT run_id)                                                      AS runs,
         SUM(CASE WHEN run_duration_seconds > 0 THEN queue_duration_seconds END)     AS queue_s_total,
         SUM(CASE WHEN run_duration_seconds > 0 THEN execution_duration_seconds END) AS execution_s_total,
         SUM(CASE WHEN run_duration_seconds > 0 THEN cleanup_duration_seconds END)   AS cleanup_s_total,
         SUM(run_duration_seconds)                                                   AS run_s_total,
         PERCENTILE(CASE WHEN run_duration_seconds > 0 THEN queue_duration_seconds END, 0.95) AS queue_s_p95
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())
    AND result_state IS NOT NULL     -- end row carries the final durations
  GROUP BY workspace_id, job_id
),
task_rows AS (
  SELECT workspace_id, job_id, job_run_id, setup_duration_seconds
  FROM `system`.`lakeflow`.`job_task_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())
    -- task end row carries the final durations; a failed start's long setup is not a cold start
    AND result_state = 'SUCCEEDED'
),
run_setup AS (
  -- a run's cold start is its longest task setup: later tasks reuse the cluster already running
  SELECT workspace_id, job_id, job_run_id, MAX(setup_duration_seconds) AS run_setup_s
  FROM task_rows
  GROUP BY workspace_id, job_id, job_run_id
),
task_setup AS (
  SELECT t.workspace_id, t.job_id,
         COUNT(*)                                                          AS task_rows,
         SUM(t.setup_duration_seconds)                                     AS setup_s_total,
         SUM(CASE WHEN t.setup_duration_seconds IS NULL THEN 1 ELSE 0 END) AS rows_setup_null,
         MAX(p.setup_s_p95)                                                AS setup_s_p95
  FROM task_rows t
  LEFT JOIN (
    SELECT workspace_id, job_id, PERCENTILE(run_setup_s, 0.95) AS setup_s_p95
    FROM run_setup
    GROUP BY workspace_id, job_id
  ) p ON p.workspace_id = t.workspace_id AND p.job_id = t.job_id
  GROUP BY t.workspace_id, t.job_id
)
SELECT r.workspace_id, r.job_id,
       r.runs,
       ts.setup_s_total,
       r.queue_s_total,
       r.execution_s_total,
       r.cleanup_s_total,
       r.run_s_total,
       ts.setup_s_p95,
       r.queue_s_p95,
       COALESCE(ts.rows_setup_null, r.runs)                    AS rows_setup_null,
       -- status: worst-first band on p95 setup (cold-start) seconds (field heuristic;
       -- 60 / 300), from task_setup so a multi-task job is judged
       -- too. A job with no task row in the window has nothing to band on -> NOT_ASSESSED,
       -- distinct from every observed task's setup folding to NULL (also NOT_ASSESSED, via the
       -- p95-is-NULL branch below).
       CASE
         WHEN ts.task_rows IS NULL OR ts.task_rows = 0 THEN 'NOT_ASSESSED'
         WHEN ts.setup_s_p95 IS NULL THEN 'NOT_ASSESSED'
         WHEN ts.setup_s_p95 >= 300 THEN 'CRITICAL'
         WHEN ts.setup_s_p95 >= 60 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM run_level r
LEFT JOIN task_setup ts ON r.workspace_id = ts.workspace_id AND r.job_id = ts.job_id
ORDER BY setup_s_p95 DESC
) q
"""
CHECKS["lakeflow_failed_cluster_starts"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_failed_cluster_starts.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/jobs_pipelines/lakeflow_failed_cluster_starts.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH final_tasks AS (
  SELECT workspace_id, job_id, period_start_time, period_end_time, result_state,
         COALESCE(NULLIF(termination_code, ''), 'unknown') AS termination_code,
         setup_duration_seconds, execution_duration_seconds
  FROM `system`.`lakeflow`.`job_task_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())
    AND result_state IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id, run_id ORDER BY period_end_time DESC) = 1
)
SELECT workspace_id,
       job_id,
       termination_code,
       COUNT(*)                    AS failed_starts,
       SUM(setup_duration_seconds) AS setup_s_total,
       MIN(period_start_time)      AS first_failed_at,
       MAX(period_end_time)        AS last_failed_at
FROM final_tasks
WHERE result_state IN ('FAILED', 'ERROR', 'TIMED_OUT')
  AND setup_duration_seconds IS NOT NULL
  AND COALESCE(execution_duration_seconds, 0) = 0
GROUP BY workspace_id, job_id, termination_code
ORDER BY failed_starts DESC, setup_s_total DESC, workspace_id, job_id, termination_code
) q
"""
CHECKS["lakeflow_workload_mix_hours"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_workload_mix_hours.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_workload_mix_hours.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT workspace_id, job_id, run_type, trigger_type, run_day,
       COUNT(DISTINCT run_id) AS distinct_runs,
       SUM(CASE WHEN result_state IS NOT NULL THEN 1 ELSE 0 END) AS completed_run_rows,
       SUM(execution_duration_seconds) AS execution_s_total
FROM (
  -- each run counts once, on the day it started, even when its hourly rows cross midnight
  SELECT workspace_id, job_id, run_id, run_type, trigger_type, result_state, execution_duration_seconds,
         date_trunc('DAY', MIN(period_start_time) OVER (PARTITION BY workspace_id, job_id, run_id)) AS run_day
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
    AND period_start_time < date_trunc('DAY', current_timestamp())
) r
GROUP BY workspace_id, job_id, run_type, trigger_type, run_day
ORDER BY workspace_id, run_day DESC
) q
"""
CHECKS["lakeflow_long_running_runs"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_long_running_runs.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_long_running_runs.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH run_rows AS (
  -- A repair keeps the run_id: rows after an earlier end row belong to the next attempt.
  SELECT workspace_id, job_id, run_id, period_start_time, period_end_time, result_state, termination_code,
         run_duration_seconds, setup_duration_seconds, queue_duration_seconds,
         execution_duration_seconds, cleanup_duration_seconds,
         MAX(CASE WHEN result_state IS NOT NULL THEN period_end_time END) OVER (
           PARTITION BY workspace_id, job_id, run_id ORDER BY period_start_time
           ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_attempt_end
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
),
run_agg AS (
  -- one row per attempt; the state and durations come from its own end row
  SELECT workspace_id, job_id, run_id,
         MIN(period_start_time)          AS attempt_start,
         MAX(period_end_time)            AS last_seen,
         MAX(CASE WHEN result_state IS NOT NULL THEN result_state END)     AS result_state,
         MAX(CASE WHEN result_state IS NOT NULL THEN termination_code END) AS termination_code,
         -- end row only; NULL before Dec 2025 and 0 for every multi-task job (doc), so 0 = not reported
         NULLIF(MAX(run_duration_seconds), 0)       AS run_s_reported,
         NULLIF(MAX(setup_duration_seconds), 0)     AS setup_s,
         NULLIF(MAX(queue_duration_seconds), 0)     AS queue_s,
         NULLIF(MAX(execution_duration_seconds), 0) AS execution_s,
         NULLIF(MAX(cleanup_duration_seconds), 0)   AS cleanup_s
  FROM run_rows
  GROUP BY workspace_id, job_id, run_id, prev_attempt_end
),
run_obs AS (
  -- the run's latest attempt, timed on its own: a repair is not measured across the wait before it
  SELECT workspace_id, job_id, run_id,
         MIN(attempt_start) OVER (PARTITION BY workspace_id, job_id, run_id) AS run_start,
         last_seen, result_state, termination_code,
         run_s_reported, setup_s, queue_s, execution_s, cleanup_s,
         (result_state IS NULL) AS in_flight,
         -- reported duration when the column is there, else wall clock (a lower bound)
         COALESCE(run_s_reported,
                  timestampdiff(SECOND, attempt_start,
                                CASE WHEN result_state IS NULL THEN current_timestamp()
                                     ELSE last_seen END)) AS run_s
  FROM run_agg
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id, run_id ORDER BY attempt_start DESC) = 1
),
task_obs AS (
  SELECT workspace_id, job_id, job_run_id, task_key,
         MAX(result_state) AS task_result_state,
         -- first compute id of the task; NULL on serverless and on pre-Dec-2025 rows
         MAX(try_element_at(compute_ids, 1)) AS cluster_id,
         COALESCE(NULLIF(MAX(execution_duration_seconds), 0),
                  timestampdiff(SECOND, MIN(period_start_time),
                                CASE WHEN MAX(result_state) IS NULL THEN current_timestamp()
                                     ELSE MAX(period_end_time) END)) AS task_s
  FROM `system`.`lakeflow`.`job_task_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
  GROUP BY workspace_id, job_id, job_run_id, task_key
),
task_ranked AS (
  SELECT workspace_id, job_id, job_run_id, task_key, task_result_state, task_s, cluster_id,
         ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id, job_run_id
                            ORDER BY task_s DESC, task_key) AS task_rank
  FROM task_obs
),
task_top AS (
  SELECT workspace_id, job_id, job_run_id,
         COUNT(*)                                                   AS tasks_seen,
         SUM(CASE WHEN task_result_state IS NULL THEN 1 ELSE 0 END) AS tasks_in_flight,
         MAX(CASE WHEN task_rank = 1 THEN task_key END)             AS top_task_key,
         MAX(CASE WHEN task_rank = 1
                  THEN COALESCE(task_result_state, 'RUNNING') END)  AS top_task_state,
         MAX(CASE WHEN task_rank = 1 THEN task_s END)               AS top_task_s,
         MAX(CASE WHEN task_rank = 1 THEN cluster_id END)           AS top_task_cluster_id,
         -- the three slowest tasks by name, longest first; CONCAT_WS drops the missing ranks
         CONCAT_WS(', ',
           MAX(CASE WHEN task_rank = 1 THEN CONCAT(task_key, '=', ROUND(task_s / 3600.0, 2), 'h') END),
           MAX(CASE WHEN task_rank = 2 THEN CONCAT(task_key, '=', ROUND(task_s / 3600.0, 2), 'h') END),
           MAX(CASE WHEN task_rank = 3 THEN CONCAT(task_key, '=', ROUND(task_s / 3600.0, 2), 'h') END)
         )                                                          AS top_3_tasks
  FROM task_ranked
  GROUP BY workspace_id, job_id, job_run_id
),
latest_jobs AS (
  -- system.lakeflow.jobs is SCD2: one row per change, so take the newest per job.
  -- Deleted jobs are kept (a long run of a since-deleted job still deserves its name).
  SELECT workspace_id, job_id, name AS job_name
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
),
ws AS (
  -- workspace name + the base URL the deep links are built from (the stored URL ends in '/')
  SELECT workspace_id, workspace_name,
         regexp_replace(workspace_url, '/+$', '') AS base_url
  FROM `system`.`access`.`workspaces_latest`
),
job_norm AS (
  -- the same job's own normal, from the finished runs in the window
  SELECT workspace_id, job_id,
         COUNT(*)                          AS finished_runs_in_window,
         percentile(run_s, 0.5)     AS run_s_p50
  FROM run_obs
  WHERE result_state IS NOT NULL
  GROUP BY workspace_id, job_id
)
SELECT r.workspace_id,
       w.workspace_name,                                         -- needs system.access; see caveats
       r.job_id,
       j.job_name,                                               -- NULL for submit/workflow runs
       r.run_id,
       r.run_start,
       CASE WHEN r.in_flight THEN NULL ELSE r.last_seen END      AS run_end,
       r.in_flight,
       ROUND(r.run_s / 3600.0, 2)                                AS run_hours,
       (r.run_s_reported IS NULL)                                AS run_s_is_lower_bound,
       r.setup_s, r.queue_s, r.execution_s, r.cleanup_s,
       -- how much of the run was waiting to start rather than working; NULL when the phase
       -- columns are not reported (every multi-task job - see caveats), never a false 0
       CASE WHEN r.setup_s IS NULL AND r.queue_s IS NULL THEN NULL
            ELSE ROUND(100.0 * (COALESCE(r.setup_s, 0) + COALESCE(r.queue_s, 0))
                       / NULLIF(r.run_s, 0), 1) END              AS wait_share_pct,
       t.tasks_seen, t.tasks_in_flight,
       t.top_task_key, t.top_task_state,
       t.top_3_tasks,
       ROUND(t.top_task_s / 3600.0, 2)                           AS top_task_hours,
       ROUND(100.0 * t.top_task_s / NULLIF(r.run_s, 0), 1)       AS top_task_share_pct,
       ROUND(n.run_s_p50 / 3600.0, 2)                            AS job_p50_hours,
       ROUND(r.run_s / NULLIF(n.run_s_p50, 0), 1)                AS x_vs_job_p50,
       n.finished_runs_in_window,
       r.result_state, r.termination_code,
       -- deep links; all three are NULL without system.access (see caveats)
       CONCAT(w.base_url, '/jobs/', r.job_id)                     AS job_url,
       CONCAT(w.base_url, '/jobs/', r.job_id, '/runs/', r.run_id) AS run_url,
       t.top_task_cluster_id,
       CONCAT(w.base_url, '/compute/clusters/',
              t.top_task_cluster_id)                              AS top_task_cluster_url,
       -- status: worst-first band on wall-clock run hours, with a still-running run past the
       -- bound treated as CRITICAL because it is the one you can still stop (field heuristic;
       -- 2 / 6).
       CASE
         WHEN r.run_s IS NULL                      THEN 'NOT_ASSESSED'
         WHEN r.run_s >= 6 * 3600    THEN 'CRITICAL'
         WHEN r.in_flight                          THEN 'CRITICAL'
         ELSE 'WARN'
       END AS status
FROM run_obs r
LEFT JOIN task_top t
  ON  r.workspace_id = t.workspace_id
  AND r.job_id       = t.job_id
  AND r.run_id       = t.job_run_id
LEFT JOIN job_norm n
  ON  r.workspace_id = n.workspace_id
  AND r.job_id       = n.job_id
LEFT JOIN latest_jobs j
  ON  r.workspace_id = j.workspace_id
  AND r.job_id       = j.job_id
LEFT JOIN ws w
  ON  r.workspace_id = w.workspace_id
WHERE r.run_s >= 2 * 3600
   OR r.run_s IS NULL
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
         run_hours DESC
) q
"""
CHECKS["lakeflow_retries_repairs"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_retries_repairs.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_retries_repairs.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH run_rows AS (
  SELECT workspace_id, job_id, run_id, period_end_time, result_state, trigger_type
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())
),
per_run AS (
  SELECT workspace_id, job_id, run_id,
         SUM(CASE WHEN result_state IS NOT NULL THEN 1 ELSE 0 END) AS attempt_rows,   -- one end row per attempt
         MAX(CASE WHEN upper(trigger_type) LIKE '%RETRY%' THEN 1 ELSE 0 END) AS is_retry_run
  FROM run_rows
  GROUP BY workspace_id, job_id, run_id
  HAVING SUM(CASE WHEN result_state IS NOT NULL THEN 1 ELSE 0 END) > 0
),
task_ends AS (
  SELECT workspace_id, job_run_id, task_key, period_end_time
  FROM `system`.`lakeflow`.`job_task_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())
    AND result_state IS NOT NULL
),
task_attempts AS (
  -- the attempt of its run a task end row belongs to: how many of the run's end rows came before it
  SELECT t.workspace_id, t.job_run_id, t.task_key, t.period_end_time, COUNT(r.run_id) AS run_attempt
  FROM task_ends t
  LEFT JOIN run_rows r
    ON r.workspace_id = t.workspace_id AND r.run_id = t.job_run_id
   AND r.result_state IS NOT NULL AND r.period_end_time < t.period_end_time
  GROUP BY t.workspace_id, t.job_run_id, t.task_key, t.period_end_time
),
task_retries AS (
  -- within one attempt of a run, a task's end rows beyond its first are automatic retries
  SELECT workspace_id, job_run_id, SUM(task_rows - 1) AS task_retries
  FROM (
    SELECT workspace_id, job_run_id, task_key, run_attempt, COUNT(*) AS task_rows
    FROM task_attempts
    GROUP BY workspace_id, job_run_id, task_key, run_attempt
  ) a
  GROUP BY workspace_id, job_run_id
),
runs AS (
  SELECT pr.*, pr.attempt_rows - 1 AS repairs, COALESCE(tr.task_retries, 0) AS task_retries
  FROM per_run pr
  LEFT JOIN task_retries tr ON tr.workspace_id = pr.workspace_id AND tr.job_run_id = pr.run_id
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
cost_rollup AS (
  -- Pre-aggregated cost per (workspace_id, job_id). job_id is not globally unique, so we key on
  -- workspace_id + job_id. Window mirrors this query's trailing window via usage_date.
  SELECT u.workspace_id,
         u.usage_metadata.job_id                          AS job_id,
         SUM(u.usage_quantity)                            AS net_dbus,
         SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS est_usd_list,
         CASE
           WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                         THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END                                               AS price_basis
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.job_id IS NOT NULL
    AND u.usage_date >= dateadd(day, -30, current_date())
    AND u.usage_date <  current_date()
  GROUP BY u.workspace_id, u.usage_metadata.job_id
)
SELECT pr.workspace_id, pr.job_id,
       COUNT(*)             AS distinct_runs,
       SUM(pr.attempt_rows)    AS total_attempt_rows,
       SUM(pr.repairs)         AS repairs,
       SUM(pr.task_retries)    AS task_retries,
       SUM(pr.is_retry_run)    AS retry_runs,
       SUM(pr.repairs + pr.task_retries + pr.is_retry_run) AS total_retries,
       SUM(CASE WHEN pr.repairs + pr.task_retries + pr.is_retry_run > 0 THEN 1 ELSE 0 END) AS runs_with_retry,
       COALESCE(MAX(cr.net_dbus), 0)     AS net_dbus,
       COALESCE(MAX(cr.est_usd_list), 0) AS est_usd_list,
       COALESCE(MAX(cr.price_basis), 'priced') AS price_basis,
       -- status: worst-first band on total retries in the window (field heuristic; 5 / 20).
       CASE
         WHEN SUM(pr.repairs + pr.task_retries + pr.is_retry_run) >= 20 THEN 'CRITICAL'
         WHEN SUM(pr.repairs + pr.task_retries + pr.is_retry_run) >= 5 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM runs pr
LEFT JOIN cost_rollup cr
  ON pr.workspace_id = cr.workspace_id AND pr.job_id = cr.job_id
GROUP BY pr.workspace_id, pr.job_id
ORDER BY total_retries DESC
) q
"""
CHECKS["lakeflow_jobs_no_timeout"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_jobs_no_timeout.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_jobs_no_timeout.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH latest_jobs AS (
  SELECT workspace_id, job_id, name AS job_name, timeout_seconds, delete_time, change_time,
         -- a RUN_DURATION_SECONDS/GREATER_THAN health rule only notifies, it never stops a run;
         -- health_rules holds at most a couple of entries, so three positions is enough headroom
         COALESCE((try_element_at(health_rules, 1).metric = 'RUN_DURATION_SECONDS' AND try_element_at(health_rules, 1).operator = 'GREATER_THAN')
          OR (try_element_at(health_rules, 2).metric = 'RUN_DURATION_SECONDS' AND try_element_at(health_rules, 2).operator = 'GREATER_THAN')
          OR (try_element_at(health_rules, 3).metric = 'RUN_DURATION_SECONDS' AND try_element_at(health_rules, 3).operator = 'GREATER_THAN'), FALSE) AS has_duration_health_rule
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),
population_floor AS (
  -- earliest change_time, over this workspace's FULL job SCD history, at which any job row already
  -- shows a non-NULL timeout_seconds -- the point after which a NULL is assumed to mean "explicitly
  -- no bound", not "not yet populated".
  SELECT workspace_id, MIN(change_time) AS population_floor_time
  FROM `system`.`lakeflow`.`jobs`
  WHERE timeout_seconds IS NOT NULL
  GROUP BY workspace_id
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
cost_rollup AS (
  -- Per (workspace_id, job_id) DBUs + effective-list $ over the cost-lookback window. job_id is NOT
  -- globally unique -> keyed on workspace_id + job_id.
  SELECT u.workspace_id,
         u.usage_metadata.job_id AS job_id,
         SUM(u.usage_quantity)                            AS net_dbus,
         SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS est_usd_list,
         CASE
           WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                         THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END                                               AS price_basis
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.job_id IS NOT NULL
    AND u.usage_date >= date_sub(current_date(), 30)
    AND u.usage_date <  current_date()
  GROUP BY u.workspace_id, u.usage_metadata.job_id
),
judged AS (
  SELECT lj.workspace_id, lj.job_id, lj.job_name, lj.timeout_seconds, lj.has_duration_health_rule,
         (lj.timeout_seconds IS NULL OR lj.timeout_seconds = 0) AS no_timeout,
         CASE
           WHEN lj.timeout_seconds IS NULL
                AND (pf.population_floor_time IS NULL OR lj.change_time < pf.population_floor_time)
             THEN 'not_populated'
           WHEN lj.timeout_seconds IS NULL
             THEN 'after_population'
         END AS timeout_null_reason
  FROM latest_jobs lj
  LEFT JOIN population_floor pf ON pf.workspace_id = lj.workspace_id
  WHERE lj.delete_time IS NULL
)
SELECT j.workspace_id, w.workspace_name, j.job_id, j.job_name, j.timeout_seconds,
       j.no_timeout, j.has_duration_health_rule AS has_duration_warning, j.timeout_null_reason,
       ROUND(COALESCE(cr.net_dbus, 0), 2)     AS net_dbus,
       ROUND(COALESCE(cr.est_usd_list, 0), 2) AS est_usd_list,
       COALESCE(cr.price_basis, 'priced')     AS price_basis,
       -- status: a flagged job (no_timeout) reads WARN, or CRITICAL when its own spend over the
       -- cost-lookback window is also high (200) -- a job with a timeout is OK; a
       -- duration health rule only warns, so it does not count. NOT_ASSESSED when the NULL itself predates population, not a confirmed gap.
       CASE
         WHEN j.no_timeout AND j.timeout_null_reason = 'not_populated' THEN 'NOT_ASSESSED'
         WHEN j.no_timeout AND COALESCE(cr.est_usd_list, 0) >= 200 THEN 'CRITICAL'
         WHEN j.no_timeout THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE WHEN j.no_timeout AND j.timeout_null_reason = 'not_populated' THEN 'timeout_not_populated' END AS not_assessed_reason
FROM judged j
LEFT JOIN cost_rollup cr ON cr.workspace_id = j.workspace_id AND cr.job_id = j.job_id
LEFT JOIN `system`.`access`.`workspaces_latest` w ON w.workspace_id = j.workspace_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         est_usd_list DESC, j.workspace_id, j.job_id
) q
"""
CHECKS["lakeflow_job_tasks_no_timeout"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_job_tasks_no_timeout.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_job_tasks_no_timeout.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH latest_jobs AS (
  SELECT workspace_id, job_id, name AS job_name, delete_time, timeout_seconds AS job_timeout_seconds
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),
latest_tasks AS (
  SELECT workspace_id, job_id, task_key, timeout_seconds, delete_time, change_time,
         -- a RUN_DURATION_SECONDS/GREATER_THAN health rule only notifies, it never stops a task;
         -- health_rules holds at most a couple of entries, so three positions is enough headroom
         COALESCE((try_element_at(health_rules, 1).metric = 'RUN_DURATION_SECONDS' AND try_element_at(health_rules, 1).operator = 'GREATER_THAN')
          OR (try_element_at(health_rules, 2).metric = 'RUN_DURATION_SECONDS' AND try_element_at(health_rules, 2).operator = 'GREATER_THAN')
          OR (try_element_at(health_rules, 3).metric = 'RUN_DURATION_SECONDS' AND try_element_at(health_rules, 3).operator = 'GREATER_THAN'), FALSE) AS has_duration_health_rule
  FROM `system`.`lakeflow`.`job_tasks`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id, task_key ORDER BY change_time DESC) = 1
),
population_floor AS (
  SELECT workspace_id, MIN(change_time) AS population_floor_time
  FROM `system`.`lakeflow`.`job_tasks`
  WHERE timeout_seconds IS NOT NULL
  GROUP BY workspace_id
),
scoped_tasks AS (
  -- mark tasks whose parent job is deleted or has no current jobs row at all
  SELECT lt.workspace_id, lt.job_id, lj.job_name, lt.task_key, lt.timeout_seconds, lt.change_time,
         lt.has_duration_health_rule, lj.job_timeout_seconds,
         -- the job's own timeout stops the whole run, every task with it
         COALESCE(lj.job_timeout_seconds, 0) > 0 AS job_bounded,
         (lj.job_id IS NULL OR lj.delete_time IS NOT NULL) AS parent_gone
  FROM latest_tasks lt
  LEFT JOIN latest_jobs lj ON lj.workspace_id = lt.workspace_id AND lj.job_id = lt.job_id
  WHERE lt.delete_time IS NULL
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
cost_rollup AS (
  SELECT u.workspace_id,
         u.usage_metadata.job_id                          AS job_id,
         SUM(u.usage_quantity)                            AS net_dbus,
         SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS est_usd_list,
         CASE
           WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                         THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END                                               AS price_basis
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.job_id IS NOT NULL
    AND u.usage_date >= date_add(current_date(), -30)
    AND u.usage_date <  current_date()
  GROUP BY u.workspace_id, u.usage_metadata.job_id
),
judged AS (
  SELECT st.workspace_id, st.job_id, st.job_name, st.task_key, st.timeout_seconds,
         st.has_duration_health_rule, st.parent_gone, st.job_timeout_seconds,
         NOT st.parent_gone AND (st.timeout_seconds IS NULL OR st.timeout_seconds = 0)
           AND NOT st.job_bounded AS no_timeout,
         CASE
           WHEN NOT st.parent_gone AND st.timeout_seconds IS NULL AND NOT st.job_bounded
                AND (pf.population_floor_time IS NULL OR st.change_time < pf.population_floor_time)
             THEN 'not_populated'
           WHEN NOT st.parent_gone AND st.timeout_seconds IS NULL AND NOT st.job_bounded
             THEN 'after_population'
         END AS timeout_null_reason
  FROM scoped_tasks st
  LEFT JOIN population_floor pf ON pf.workspace_id = st.workspace_id
)
SELECT j.workspace_id, w.workspace_name, j.job_id, j.job_name, j.task_key, j.timeout_seconds,
       j.job_timeout_seconds, j.no_timeout, j.has_duration_health_rule AS has_duration_warning, j.timeout_null_reason,
       j.parent_gone,
       ROUND(COALESCE(cr.net_dbus, 0), 2)     AS net_dbus,
       ROUND(COALESCE(cr.est_usd_list, 0), 2) AS est_usd_list,
       COALESCE(cr.price_basis, 'priced')     AS price_basis,
       -- status: a flagged task (no_timeout) reads WARN, or CRITICAL when its parent job's own spend
       -- over the cost-lookback window is also high (200). An orphaned-parent task,
       -- or one whose NULL predates population, is NOT_ASSESSED - nobody can act on either.
       CASE
         WHEN j.parent_gone THEN 'NOT_ASSESSED'
         WHEN j.no_timeout AND j.timeout_null_reason = 'not_populated' THEN 'NOT_ASSESSED'
         WHEN j.no_timeout AND COALESCE(cr.est_usd_list, 0) >= 200 THEN 'CRITICAL'
         WHEN j.no_timeout THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN j.parent_gone THEN 'orphaned_parent'
         WHEN j.no_timeout AND j.timeout_null_reason = 'not_populated' THEN 'timeout_not_populated'
       END AS not_assessed_reason
FROM judged j
LEFT JOIN cost_rollup cr ON cr.workspace_id = j.workspace_id AND cr.job_id = j.job_id
LEFT JOIN `system`.`access`.`workspaces_latest` w ON w.workspace_id = j.workspace_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         est_usd_list DESC, j.workspace_id, j.job_id, j.task_key
) q
"""
CHECKS["lakeflow_health_rule_coverage"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_health_rule_coverage.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_health_rule_coverage.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
WITH latest_jobs AS (
  SELECT workspace_id, job_id, name AS job_name, health_rules, delete_time
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
)
SELECT lj.workspace_id, lj.job_id, lj.job_name,
       CASE WHEN lj.health_rules IS NOT NULL THEN CARDINALITY(lj.health_rules) END AS health_rule_count,
       -- status: a populated row with no rule reads WARN; a NULL column (not yet populated) is
       -- NOT_ASSESSED, never a false-clean OK or a false "no rule configured".
       CASE
         WHEN lj.health_rules IS NULL THEN 'NOT_ASSESSED'
         WHEN CARDINALITY(lj.health_rules) = 0 THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE WHEN lj.health_rules IS NULL THEN 'health_rules_not_populated' END AS not_assessed_reason
FROM latest_jobs lj
WHERE lj.delete_time IS NULL
ORDER BY CASE status WHEN 'WARN' THEN 0 WHEN 'NOT_ASSESSED' THEN 1 ELSE 2 END, lj.workspace_id, lj.job_id
) q
"""
CHECKS["lakeflow_job_ownership_orphans"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_job_ownership_orphans.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_job_ownership_orphans.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
WITH latest_jobs AS (
  SELECT workspace_id, job_id, name AS job_name, creator_user_name, run_as_user_name, trigger_type, paused, delete_time
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
),
norm AS (
  SELECT workspace_id, job_id, job_name,
         (upper(COALESCE(creator_user_name, '')) = '__REDACTED__'
            OR upper(COALESCE(run_as_user_name, '')) = '__REDACTED__') AS identity_redacted,
         CASE WHEN creator_user_name IS NULL
                OR trim(creator_user_name) = ''
                OR upper(creator_user_name) = '__REDACTED__'
              THEN NULL ELSE creator_user_name END AS creator_user_name,
         CASE WHEN run_as_user_name IS NULL
                OR trim(run_as_user_name) = ''
                OR upper(run_as_user_name) = '__REDACTED__'
              THEN NULL ELSE run_as_user_name END  AS run_as_user_name,
         (trigger_type IS NOT NULL AND trim(trigger_type) <> ''
            AND NOT COALESCE(paused, FALSE)) AS is_scheduled
  FROM latest_jobs
  WHERE delete_time IS NULL
),
judged AS (
  SELECT workspace_id, job_id, job_name, creator_user_name, run_as_user_name, is_scheduled, identity_redacted,
         -- run-as kind: a service-principal application-id UUID, a human login, or unknown
         -- (NULL run_as_user_name -- caught separately by principal_missing/identity_not_recorded below)
         CASE
           WHEN run_as_user_name RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
             THEN 'SERVICE_PRINCIPAL'
           WHEN run_as_user_name IS NOT NULL THEN 'HUMAN'
           ELSE NULL
         END AS run_as_kind,
         -- one identity recorded but the other is gone: nobody clearly owns or runs the job.
         -- Excludes a redacted identity (masked, not missing) and a job with NEITHER identity
         -- recorded (identity_not_recorded below, an unpopulated column, not a gap).
         (NOT identity_redacted AND ((creator_user_name IS NULL) <> (run_as_user_name IS NULL))) AS principal_missing,
         (creator_user_name IS NULL AND run_as_user_name IS NULL) AS identity_not_recorded
  FROM norm
)
SELECT j.workspace_id, w.workspace_name, j.job_id, j.job_name,
       j.creator_user_name AS creator_user_name,
       j.run_as_user_name AS run_as_user_name,
       j.run_as_kind, j.is_scheduled, j.identity_redacted, j.principal_missing, j.identity_not_recorded,
       -- status: a scheduled human run-as is the worse pattern (CRITICAL, matching the double weight
       -- this check has always given it); a manual human run-as or a missing principal is WARN;
       -- neither identity recorded at all is NOT_ASSESSED, not a false-clean OK.
       CASE
         WHEN j.identity_not_recorded THEN 'NOT_ASSESSED'
         WHEN j.run_as_kind = 'HUMAN' AND j.is_scheduled THEN 'CRITICAL'
         WHEN j.run_as_kind = 'HUMAN' THEN 'WARN'
         WHEN j.principal_missing THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE WHEN j.identity_not_recorded THEN 'identity_not_recorded' END AS not_assessed_reason
FROM judged j
LEFT JOIN `system`.`access`.`workspaces_latest` w ON w.workspace_id = j.workspace_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         j.workspace_id, j.job_id
) q
"""
CHECKS["lakeflow_tasks_near_timeout"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_tasks_near_timeout.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_tasks_near_timeout.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH latest_tasks AS (
  SELECT workspace_id, job_id, task_key, timeout_seconds, delete_time
  FROM `system`.`lakeflow`.`job_tasks`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id, task_key ORDER BY change_time DESC
  ) = 1
),
latest_jobs AS (
  SELECT workspace_id, job_id, name AS job_name
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),
task_end AS (
  SELECT workspace_id, job_id, task_key, run_id, execution_duration_seconds
  FROM `system`.`lakeflow`.`job_task_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())   -- drop incomplete current day
    AND result_state IS NOT NULL                                   -- task end row only
),
agg AS (
  SELECT t.workspace_id, t.job_id, t.task_key,
         lt.timeout_seconds,
         COUNT(DISTINCT t.run_id)                              AS task_runs,
         MAX(t.execution_duration_seconds)                     AS max_exec_s,
         percentile(t.execution_duration_seconds, 0.95) AS exec_s_p95,
         -- runs whose execution reached >= 0.8 of a CONFIGURED (>0) timeout but
         -- stayed under it -- mutually exclusive with runs_over_timeout, so the two never overlap.
         SUM(CASE WHEN lt.timeout_seconds > 0
                   AND t.execution_duration_seconds >= 0.8 * lt.timeout_seconds
                   AND t.execution_duration_seconds <  lt.timeout_seconds
                  THEN 1 ELSE 0 END) AS runs_near_timeout,
         -- runs that ran AT/PAST the configured timeout (should have been killed)
         SUM(CASE WHEN lt.timeout_seconds > 0
                   AND t.execution_duration_seconds >= lt.timeout_seconds
                  THEN 1 ELSE 0 END) AS runs_over_timeout,
         -- an explicit 0-second timeout is "no bound configured" (a deliberate setting, not a gap).
         SUM(CASE WHEN lt.timeout_seconds = 0 THEN 1 ELSE 0 END) AS runs_no_timeout_bound,
         -- degradation buckets: a NULL timeout / NULL execution duration is "column not yet
         -- populated" (not assessed), distinct from an explicit 0-second timeout above.
         SUM(CASE WHEN lt.timeout_seconds IS NULL THEN 1 ELSE 0 END) AS runs_no_task_timeout,
         SUM(CASE WHEN t.execution_duration_seconds IS NULL THEN 1 ELSE 0 END) AS runs_exec_null
  FROM task_end t
  LEFT JOIN latest_tasks lt
    ON  t.workspace_id = lt.workspace_id
    AND t.job_id       = lt.job_id
    AND t.task_key     = lt.task_key
  GROUP BY t.workspace_id, t.job_id, t.task_key, lt.timeout_seconds
)
SELECT a.workspace_id, a.job_id, j.job_name, w.workspace_name, a.task_key, a.timeout_seconds,
       a.task_runs, a.max_exec_s, a.exec_s_p95,
       a.runs_near_timeout, a.runs_over_timeout, a.runs_no_timeout_bound,
       a.runs_no_task_timeout, a.runs_exec_null,
       -- status: worst-first band on near/over-timeout run count (field heuristic;
       -- 3 / 10), degraded to NOT_ASSESSED when either
       -- NULL bucket accounts for a material share of the task's runs.
       CASE
         WHEN a.runs_no_task_timeout >= 0.5 * a.task_runs THEN 'NOT_ASSESSED'
         WHEN a.runs_exec_null       >= 0.5 * a.task_runs THEN 'NOT_ASSESSED'
         WHEN (a.runs_near_timeout + a.runs_over_timeout) >= 10 THEN 'CRITICAL'
         WHEN (a.runs_near_timeout + a.runs_over_timeout) >= 3 THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN a.runs_no_task_timeout >= 0.5 * a.task_runs THEN 'timeout_not_populated'
         WHEN a.runs_exec_null       >= 0.5 * a.task_runs THEN 'exec_duration_not_populated'
       END AS not_assessed_reason
FROM agg a
LEFT JOIN latest_jobs j ON j.workspace_id = a.workspace_id AND j.job_id = a.job_id
LEFT JOIN `system`.`access`.`workspaces_latest` w ON w.workspace_id = a.workspace_id
ORDER BY (a.runs_near_timeout + a.runs_over_timeout) DESC
) q
"""
CHECKS["lakeflow_job_cost_summary"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_job_cost_summary.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/jobs_pipelines/lakeflow_job_cost_summary.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH run_usage AS (
  -- Same per-run attribution and price join as lakeflow_job_run_cost's own run_usage CTE
  -- (usage_metadata.job_id/job_run_id, DEC-66.1 effective-list price) - see that query's header.
  SELECT u.workspace_id,
         u.usage_metadata.job_id     AS job_id,
         u.usage_metadata.job_run_id AS job_run_id,
         SUM(u.usage_quantity)                                          AS net_run_dbus,
         MAX(lp.currency_code)                                          AS currency_code,
         SUM(u.usage_quantity * lp.list_rate)                           AS net_run_list_cost,
         CASE
           WHEN SUM(CASE WHEN lp.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                         THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN lp.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END AS price_basis
  FROM `system`.`billing`.`usage` u
  LEFT JOIN (
    SELECT sku_name, cloud, currency_code, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective (post-promotion) list price - DEC-66.1
    FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
  ) lp
    ON u.sku_name = lp.sku_name
   AND u.cloud    = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_date >= dateadd(day, -30, current_date())
    AND u.usage_date < current_date()
    AND upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.job_id IS NOT NULL
    AND u.usage_metadata.job_run_id IS NOT NULL
  GROUP BY u.workspace_id, u.usage_metadata.job_id, u.usage_metadata.job_run_id
),
job_totals AS (
  SELECT workspace_id, job_id,
         COUNT(DISTINCT job_run_id)               AS runs,
         SUM(net_run_dbus)                        AS net_job_dbus,
         SUM(net_run_list_cost)                   AS net_list_cost,
         MAX(currency_code)                       AS currency_code,
         percentile(net_run_dbus, 0.5)      AS median_run_dbus,
         MAX(net_run_dbus)                        AS max_run_dbus,
         percentile(net_run_list_cost, 0.5) AS est_median_usd_list,
         MAX(net_run_list_cost)                   AS est_max_usd_list,
         CASE
           WHEN SUM(CASE WHEN price_basis = 'unpriced' THEN 1 ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN price_basis = 'priced' THEN 1 ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END AS price_basis
  FROM run_usage
  GROUP BY workspace_id, job_id
),
latest_jobs AS (
  -- system.lakeflow.jobs is SCD2: one row per change, so take the newest per job.
  SELECT workspace_id, job_id, name AS job_name
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
)
SELECT t.workspace_id,
       t.job_id,
       j.job_name,                                                -- NULL for submit/workflow runs
       t.runs,
       t.net_job_dbus,
       t.currency_code,
       t.net_list_cost,
       COALESCE(t.price_basis, 'priced') AS price_basis,
       t.median_run_dbus,
       t.max_run_dbus,
       t.est_median_usd_list,
       t.est_max_usd_list
FROM job_totals t
LEFT JOIN latest_jobs j
  ON  j.workspace_id = t.workspace_id
  AND j.job_id       = t.job_id
ORDER BY t.net_list_cost DESC NULLS LAST, t.net_job_dbus DESC
) q
"""
CHECKS["lakeflow_job_run_cost"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_job_run_cost.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/jobs_pipelines/lakeflow_job_run_cost.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH run_usage AS (
  -- Net DBUs and effective-list dollar estimate per job run (DEC-66.1: pricing.effective_list.default,
  -- the exact price join T-69A settled on). Only usage rows carrying BOTH job_id AND job_run_id.
  SELECT u.workspace_id,
         u.usage_metadata.job_id     AS job_id,
         u.usage_metadata.job_run_id AS job_run_id,
         SUM(u.usage_quantity)                                          AS net_run_dbus,
         SUM(CASE WHEN u.product_features.is_serverless = TRUE
                  THEN u.usage_quantity ELSE 0 END)                     AS serverless_dbus,
         SUM(CASE WHEN u.product_features.is_serverless = TRUE
                  THEN 0 ELSE u.usage_quantity END)                     AS classic_dbus,
         MAX(lp.currency_code)                                          AS currency_code,
         SUM(u.usage_quantity * lp.list_rate)                           AS net_list_cost,
         CASE
           WHEN SUM(CASE WHEN lp.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                         THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN lp.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END AS price_basis
  FROM `system`.`billing`.`usage` u
  LEFT JOIN (
    SELECT sku_name, cloud, currency_code, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective (post-promotion) list price - DEC-66.1
    FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
  ) lp
    ON u.sku_name = lp.sku_name
   AND u.cloud    = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_date >= dateadd(day, -30, current_date())
    AND u.usage_date < current_date()
    AND upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.job_id IS NOT NULL
    AND u.usage_metadata.job_run_id IS NOT NULL
  GROUP BY u.workspace_id, u.usage_metadata.job_id, u.usage_metadata.job_run_id
),
run_rows AS (
  -- Every observed timeline row (including in-progress, NULL-result_state slices) for a run that
  -- already has billed usage in run_usage above - no window lower bound on the timeline side (see
  -- caveats): a run started well before 30 still keeps its TRUE run_start, and a run
  -- sliced hourly past about an hour keeps every one of its observed slices.
  SELECT t.workspace_id, t.job_id, t.run_id, t.period_start_time, t.period_end_time,
         t.result_state, t.termination_code
  FROM `system`.`lakeflow`.`job_run_timeline` t
  JOIN (SELECT DISTINCT workspace_id, job_id, job_run_id FROM run_usage) k
    ON  t.workspace_id = k.workspace_id
    AND t.job_id       = k.job_id
    AND t.run_id       = k.job_run_id
),
run_span AS (
  -- The run's TRUE first observed slice (never just its first in-window one); last_slice_start is
  -- used only to detect an unclosed final attempt below.
  SELECT workspace_id, job_id, run_id,
         MIN(period_start_time) AS run_start,
         MAX(period_start_time) AS last_slice_start
  FROM run_rows
  GROUP BY workspace_id, job_id, run_id
),
run_last_end AS (
  -- The run's LAST attempt's own end row only (highest period_end_time among rows that actually
  -- ended) - never MAX(result_state)/MAX(termination_code), which compares attempts' text
  -- alphabetically and can pair one attempt's code with a different attempt's state.
  SELECT workspace_id, job_id, run_id, result_state, termination_code,
         period_end_time AS last_end
  FROM run_rows
  WHERE result_state IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id, run_id ORDER BY period_end_time DESC
  ) = 1
),
run_state AS (
  -- One row per run that has at least one job_run_timeline row (a run with none simply has no row
  -- here, so it LEFT JOINs to NULL below - genuinely unknown, never "in flight"). in_flight is TRUE
  -- when the run has no landed end row at all, OR its latest observed slice started at/after its
  -- last end row's own end time (a new attempt already running after an earlier attempt's outcome
  -- landed - a repair in progress); result_state/termination_code are then NULL too, since that
  -- earlier attempt's outcome is not this run's current state.
  SELECT s.workspace_id, s.job_id, s.run_id, s.run_start,
         (e.run_id IS NULL OR s.last_slice_start >= e.last_end) AS in_flight,
         CASE WHEN e.run_id IS NULL OR s.last_slice_start >= e.last_end THEN NULL
              ELSE e.result_state END AS result_state,
         CASE WHEN e.run_id IS NULL OR s.last_slice_start >= e.last_end THEN NULL
              ELSE e.termination_code END AS termination_code
  FROM run_span s
  LEFT JOIN run_last_end e
    ON  e.workspace_id = s.workspace_id
    AND e.job_id       = s.job_id
    AND e.run_id       = s.run_id
),
latest_jobs AS (
  -- system.lakeflow.jobs is SCD2: one row per change, so take the newest per job.
  SELECT workspace_id, job_id, name AS job_name
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
)
SELECT d.workspace_id,
       d.job_id,
       j.job_name,                                                -- NULL for submit/workflow runs
       d.job_run_id,
       m.run_start,
       m.in_flight,
       m.result_state,
       m.termination_code,
       d.net_run_dbus,
       d.currency_code,
       d.net_list_cost,
       COALESCE(d.price_basis, 'priced') AS price_basis,
       CASE
         WHEN d.classic_dbus = 0 AND d.serverless_dbus > 0 THEN 'serverless'
         WHEN d.serverless_dbus = 0 AND d.classic_dbus > 0 THEN 'classic'
         WHEN d.serverless_dbus > 0 AND d.classic_dbus > 0 THEN 'mixed'
         ELSE NULL
       END AS compute_type
FROM run_usage d
LEFT JOIN run_state m
  ON  m.workspace_id = d.workspace_id
  AND m.job_id       = d.job_id
  AND m.run_id       = d.job_run_id
LEFT JOIN latest_jobs j
  ON  j.workspace_id = d.workspace_id
  AND j.job_id       = d.job_id
ORDER BY d.net_list_cost DESC NULLS LAST, d.net_run_dbus DESC
LIMIT 100000
) q
"""
CHECKS["lakeflow_job_recent_runs"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_job_recent_runs.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/jobs_pipelines/lakeflow_job_recent_runs.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH all_rows AS (
  -- No day cutoff: a run whose end row landed today is a finished run in this listing.
  -- A repair keeps the run_id: rows after an earlier end row belong to the next attempt.
  SELECT workspace_id, job_id, run_id, period_start_time, period_end_time,
         result_state, termination_code,
         NULLIF(run_duration_seconds, 0) AS run_s_reported,   -- 0 = not reported on a multi-task job
         MAX(CASE WHEN result_state IS NOT NULL THEN period_end_time END) OVER (
           PARTITION BY workspace_id, job_id, run_id ORDER BY period_start_time
           ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_attempt_end
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
),
attempts AS (
  SELECT workspace_id, job_id, run_id,
         MIN(period_start_time)                                              AS attempt_start,
         MAX(CASE WHEN result_state IS NOT NULL THEN period_end_time END)    AS attempt_end,
         MAX(CASE WHEN result_state IS NOT NULL THEN result_state END)       AS result_state,
         MAX(CASE WHEN result_state IS NOT NULL THEN termination_code END)   AS termination_code,
         MAX(CASE WHEN result_state IS NOT NULL THEN run_s_reported END)     AS run_s_reported
  FROM all_rows
  GROUP BY workspace_id, job_id, run_id, prev_attempt_end
),
run_obs AS (
  -- the run's latest attempt: timed on its own, in flight while it has no end row
  SELECT workspace_id, job_id, run_id,
         MIN(attempt_start) OVER (PARTITION BY workspace_id, job_id, run_id)  AS run_start,
         attempt_start                                                        AS final_start,
         attempt_end                                                          AS run_end,
         result_state, termination_code, run_s_reported,
         COUNT(attempt_end) OVER (PARTITION BY workspace_id, job_id, run_id)  AS attempts,
         (attempt_end IS NULL)                                                AS in_flight,
         COALESCE(run_s_reported,
                  timestampdiff(SECOND, attempt_start, COALESCE(attempt_end, current_timestamp()))) AS run_s
  FROM attempts
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id, run_id ORDER BY attempt_start DESC) = 1
),
task_final AS (
  -- each task's last result in the run: a retried or repaired task that then succeeded is not failing
  SELECT workspace_id, job_id, job_run_id, task_key, result_state, termination_code
  FROM `system`.`lakeflow`.`job_task_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
    AND result_state IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id, job_run_id, task_key ORDER BY period_end_time DESC) = 1
),
failing_tasks AS (
  SELECT workspace_id, job_id, job_run_id,
         concat_ws(', ', collect_set(task_key)) AS failing_task_keys,
         MAX(termination_code)                  AS failing_task_termination_code
  FROM task_final
  WHERE result_state IN ('FAILED', 'ERROR', 'TIMED_OUT')
  GROUP BY workspace_id, job_id, job_run_id
),
run_cost AS (
  -- the exact per-run attribution and effective-list price join lakeflow_job_run_cost uses
  -- (usage_metadata.job_id/job_run_id, DEC-66.1, usage_unit matched)
  SELECT u.workspace_id,
         u.usage_metadata.job_id     AS job_id,
         u.usage_metadata.job_run_id AS job_run_id,
         SUM(u.usage_quantity)                AS net_dbus,
         SUM(u.usage_quantity * lp.list_rate) AS net_list_cost
  FROM `system`.`billing`.`usage` u
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective (post-promotion) list price - DEC-66.1
    FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
  ) lp
    ON u.sku_name = lp.sku_name
   AND u.cloud    = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_date >= dateadd(day, -30, current_date())
    AND u.usage_date < current_date()
    AND upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.job_id IS NOT NULL
    AND u.usage_metadata.job_run_id IS NOT NULL
  GROUP BY u.workspace_id, u.usage_metadata.job_id, u.usage_metadata.job_run_id
),
sql_errors AS (
  -- de-valued (emails / string literals stripped) error text of this run's own failed SQL
  -- statements, same de-value pattern query_failed_queries_daily uses
  SELECT workspace_id,
         query_source.job_info.job_run_id AS job_run_id,
         MAX(
           regexp_replace(
             regexp_replace(error_message, '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}', '<email>'),
             concat(chr(39), '[^', chr(39), ']*', chr(39)), '?'
           )
         ) AS sql_error_sample
  FROM `system`.`query`.`history`
  WHERE start_time >= dateadd(day, -30, current_date())
    AND execution_status = 'FAILED'
    AND query_source.job_info.job_run_id IS NOT NULL
  GROUP BY workspace_id, query_source.job_info.job_run_id
),
latest_jobs AS (
  SELECT workspace_id, job_id, name AS job_name
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),
ranked AS (
  SELECT r.*,
         ROW_NUMBER() OVER (PARTITION BY r.workspace_id, r.job_id ORDER BY r.run_start DESC) AS run_rank
  FROM run_obs r
)
SELECT r.workspace_id,
       w.workspace_name,
       r.job_id,
       j.job_name,                                                  -- NULL for submit/workflow runs
       r.run_id,
       r.run_start,
       CASE WHEN r.in_flight THEN NULL ELSE r.run_end END           AS run_end,
       r.in_flight,
       ROUND(r.run_s / 60.0, 1)                                     AS run_minutes,
       -- still running, or its first row is in the window's first hour (it may have started before)
       (r.in_flight OR (r.run_s_reported IS NULL
         AND r.final_start < current_date() - INTERVAL 30 DAYS + INTERVAL 1 HOURS)) AS run_duration_is_lower_bound,
       r.result_state,
       r.termination_code,
       r.attempts,
       CASE WHEN r.result_state IN ('FAILED', 'ERROR', 'TIMED_OUT') THEN ft.failing_task_keys END AS failing_task_keys,
       CASE WHEN r.result_state IN ('FAILED', 'ERROR', 'TIMED_OUT') THEN ft.failing_task_termination_code END AS failing_task_termination_code,
       ROUND(rc.net_dbus, 2)                                        AS net_dbus,
       ROUND(rc.net_list_cost, 2)                                   AS net_list_cost,
       se.sql_error_sample
FROM ranked r
LEFT JOIN failing_tasks ft
  ON r.workspace_id = ft.workspace_id AND r.job_id = ft.job_id AND r.run_id = ft.job_run_id
LEFT JOIN run_cost rc
  ON r.workspace_id = rc.workspace_id AND r.job_id = rc.job_id AND r.run_id = rc.job_run_id
LEFT JOIN sql_errors se
  ON r.workspace_id = se.workspace_id AND r.run_id = se.job_run_id
LEFT JOIN latest_jobs j ON r.workspace_id = j.workspace_id AND r.job_id = j.job_id
LEFT JOIN `system`.`access`.`workspaces_latest` w ON r.workspace_id = w.workspace_id
WHERE r.run_rank <= 10
ORDER BY r.workspace_id, r.job_id, r.run_start DESC
) q
"""
CHECKS["lakeflow_job_compute_pressure"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_job_compute_pressure.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/jobs_pipelines/lakeflow_job_compute_pressure.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH task_runs AS (
  -- one row per TASK RUN starting in the window, any duration (run_id here is the task run id)
  SELECT workspace_id, job_id, job_run_id,
         run_id                              AS task_run_id,
         task_key,
         MIN(period_start_time)              AS task_start,
         MAX(period_end_time)                AS task_last_seen,
         MAX(result_state)                   AS task_result_state,
         NULLIF(MAX(execution_duration_seconds), 0) AS task_exec_s,
         -- first compute id of the task; NULL on serverless and on pre-Dec-2025 rows
         MAX(try_element_at(compute_ids, 1)) AS cluster_id
  FROM `system`.`lakeflow`.`job_task_run_timeline`
  WHERE period_start_time >= dateadd(day, -LEAST(30, 90), current_date())
  GROUP BY workspace_id, job_id, job_run_id, run_id, task_key
),
task_obs AS (
  SELECT workspace_id, job_id, job_run_id, task_run_id, task_key, task_start, cluster_id,
         -- a task that never ran: SKIPPED / BLOCKED, or the doc's zero-length row
         (task_result_state IN ('SKIPPED', 'BLOCKED')
          OR (task_result_state IS NOT NULL AND task_start = task_last_seen)) AS task_not_executed,
         -- the telemetry window: the task's observed wall clock, open-ended while in flight
         CASE WHEN task_result_state IS NULL THEN current_timestamp() ELSE task_last_seen END AS task_end,
         COALESCE(task_exec_s,
                  timestampdiff(SECOND, task_start,
                                CASE WHEN task_result_state IS NULL THEN current_timestamp()
                                     ELSE task_last_seen END)) AS task_s
  FROM task_runs
),
task_slices AS (
  -- every node-minute of each executed task run's cluster that overlaps that task run's window
  SELECT t.workspace_id, t.job_id, t.job_run_id, t.task_key, t.cluster_id,
         n.instance_id, n.start_time, n.driver,
         n.cpu_user_percent + n.cpu_system_percent AS cpu_pct,
         n.cpu_wait_percent, n.mem_used_percent, n.mem_swap_percent,
         n.network_received_bytes
  FROM task_obs t
  JOIN `system`.`compute`.`node_timeline` n
    ON  n.workspace_id = t.workspace_id
    AND n.cluster_id   = t.cluster_id
    AND n.start_time   <  t.task_end
    AND n.end_time     >  t.task_start
  WHERE n.start_time >= dateadd(day, -LEAST(30, 90), current_date())
    AND CASE WHEN t.task_not_executed THEN 0 ELSE 1 END = 1
),
job_slices AS (
  -- the dedupe: parallel tasks of one run on one shared cluster count each node-minute once
  SELECT DISTINCT workspace_id, job_id, job_run_id, cluster_id, instance_id, start_time, driver,
         cpu_pct, cpu_wait_percent, mem_used_percent, mem_swap_percent, network_received_bytes
  FROM task_slices
),
latest_clusters AS (
  -- system.compute.clusters is SCD2: the newest row per cluster (deleted clusters kept)
  SELECT workspace_id, cluster_id, cluster_source, driver_node_type, worker_node_type,
         worker_count, max_autoscale_workers
  FROM `system`.`compute`.`clusters`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC
  ) = 1
),
latest_jobs AS (
  -- system.lakeflow.jobs is SCD2: one row per change, so take the newest per job
  SELECT workspace_id, job_id, name AS job_name
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
),
node_mem AS (
  -- memory and cores per node type (account-level reference table)
  SELECT node_type, MAX(memory_mb) AS memory_mb, MAX(core_count) AS core_count
  FROM `system`.`compute`.`node_types`
  GROUP BY node_type
),
run_starts AS (
  -- per run: its first task start and how many distinct clusters its task runs recorded
  SELECT workspace_id, job_id, job_run_id,
         MIN(task_start)            AS run_start,
         COUNT(DISTINCT cluster_id) AS run_clusters
  FROM task_obs
  GROUP BY workspace_id, job_id, job_run_id
),
run_nodes AS (
  -- one row per node of each run's cluster, to count workers and measure skew; a worker that ran
  -- under half as long as the run's longest-lived worker (added late by autoscale) is left out of skew
  SELECT workspace_id, job_id, job_run_id, cluster_id, instance_id, driver,
         AVG(cpu_pct) AS node_cpu_avg,
         COUNT(*) >= 0.5 * MAX(CASE WHEN driver THEN NULL ELSE COUNT(*) END) OVER (
           PARTITION BY workspace_id, job_id, job_run_id, cluster_id) AS full_run_node
  FROM job_slices
  GROUP BY workspace_id, job_id, job_run_id, cluster_id, instance_id, driver
),
run_minutes AS (
  -- each deduplicated node-minute on its minute boundary, to count the workers running at once
  SELECT workspace_id, job_id, job_run_id, cluster_id, instance_id, driver,
         date_trunc('MINUTE', start_time) AS minute_start
  FROM job_slices
),
run_minute_workers AS (
  SELECT workspace_id, job_id, job_run_id, cluster_id, minute_start,
         COUNT(DISTINCT CASE WHEN driver THEN NULL ELSE instance_id END) AS workers_now
  FROM run_minutes
  GROUP BY workspace_id, job_id, job_run_id, cluster_id, minute_start
),
run_peak AS (
  -- the most workers running at once: churned or replaced instances are never counted twice
  SELECT workspace_id, job_id, job_run_id, cluster_id, MAX(workers_now) AS worker_nodes_peak
  FROM run_minute_workers
  GROUP BY workspace_id, job_id, job_run_id, cluster_id
),
run_cpu AS (
  -- the (run, cluster)'s own worker CPU p50: only a CPU-bound cluster can make the job at_ceiling
  SELECT workspace_id, job_id, job_run_id, cluster_id,
         percentile(CASE WHEN driver THEN NULL ELSE cpu_pct END, 0.5) AS pair_cpu_p50
  FROM job_slices
  GROUP BY workspace_id, job_id, job_run_id, cluster_id
),
run_cluster_agg AS (
  SELECT n.workspace_id, n.job_id, n.job_run_id, n.cluster_id,
         MAX(s.run_start)                                       AS run_start,
         MAX(k.worker_nodes_peak)                               AS worker_nodes_seen,
         MAX(u.pair_cpu_p50)                                    AS pair_cpu_p50,
         MAX(CASE WHEN n.driver OR NOT n.full_run_node THEN NULL ELSE n.node_cpu_avg END)
           - MIN(CASE WHEN n.driver OR NOT n.full_run_node THEN NULL ELSE n.node_cpu_avg END) AS worker_cpu_spread_pct,
         -- the ceiling autoscale can reach, else the fixed size (latest SCD2 shape)
         MAX(COALESCE(c.max_autoscale_workers, c.worker_count)) AS workers_configured
  FROM run_nodes n
  LEFT JOIN latest_clusters c
    ON  c.workspace_id = n.workspace_id
    AND c.cluster_id   = n.cluster_id
  LEFT JOIN run_starts s
    ON  s.workspace_id = n.workspace_id
    AND s.job_id       = n.job_id
    AND s.job_run_id   = n.job_run_id
  LEFT JOIN run_peak k
    ON  k.workspace_id = n.workspace_id
    AND k.job_id       = n.job_id
    AND k.job_run_id   = n.job_run_id
    AND k.cluster_id   = n.cluster_id
  LEFT JOIN run_cpu u
    ON  u.workspace_id = n.workspace_id
    AND u.job_id       = n.job_id
    AND u.job_run_id   = n.job_run_id
    AND u.cluster_id   = n.cluster_id
  GROUP BY n.workspace_id, n.job_id, n.job_run_id, n.cluster_id
),
run_cluster AS (
  -- a single-node cluster (0 workers configured) has no worker ceiling to reach: NULL, not TRUE
  SELECT workspace_id, job_id, job_run_id, cluster_id, run_start, worker_nodes_seen,
         worker_cpu_spread_pct, workers_configured,
         CASE WHEN workers_configured IS NULL OR workers_configured = 0 THEN NULL
              ELSE worker_nodes_seen >= workers_configured END AS hit_ceiling,
         -- 1 = this run's cluster was itself CPU-bound (its own worker CPU p50 at/above 70)
         CASE WHEN pair_cpu_p50 >= 70 THEN 1 ELSE 0 END AS cpu_bound,
         -- 1 = this run's cluster was CPU-bound AND at its ceiling: the case with a scale-out lever
         CASE WHEN workers_configured IS NULL OR workers_configured = 0 THEN 0
              WHEN worker_nodes_seen >= workers_configured
               AND pair_cpu_p50 >= 70 THEN 1
              ELSE 0 END                                AS sets_ceiling
  FROM run_cluster_agg
),
ceiling AS (
  -- per job: did ANY CPU-bound run's cluster reach its configured maximum (NULL = no ceiling known)
  SELECT workspace_id, job_id,
         CASE WHEN MAX(CASE WHEN hit_ceiling IS NULL THEN 0 ELSE 1 END) = 0 THEN NULL
              ELSE MAX(sets_ceiling) = 1
         END                        AS at_ceiling,
         MAX(worker_cpu_spread_pct) AS worker_cpu_spread_pct
  FROM run_cluster
  GROUP BY workspace_id, job_id
),
ceiling_ref AS (
  -- the reference (run, cluster) for the reason text: the one that set at_ceiling, else a
  -- CPU-bound one, else any - the most workers running at once first. A light cluster at its fixed
  -- size must never stand in for the CPU-bound cluster the verdict is about.
  SELECT workspace_id, job_id,
         worker_nodes_seen  AS worker_nodes_seen_max,
         workers_configured AS workers_configured_max
  FROM run_cluster
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id
    ORDER BY sets_ceiling DESC, cpu_bound DESC, worker_nodes_seen DESC, run_start DESC, cluster_id
  ) = 1
),
profile AS (
  -- the job's pooled hardware profile over its deduplicated node-minutes
  SELECT workspace_id, job_id,
         COUNT(DISTINCT job_run_id)                                AS runs_with_telemetry,
         COUNT(*)                                                  AS node_minutes,
         SUM(CASE WHEN driver THEN 0 ELSE 1 END)                   AS worker_minutes,
         SUM(CASE WHEN driver THEN 1 ELSE 0 END)                   AS driver_minutes,
         AVG(CASE WHEN driver THEN NULL ELSE cpu_pct END)          AS worker_cpu_avg_pct,
         percentile(CASE WHEN driver THEN NULL ELSE cpu_pct END, 0.5) AS worker_cpu_p50_pct,
         percentile(CASE WHEN driver THEN NULL ELSE cpu_pct END, 0.9) AS worker_cpu_p90_pct,
         AVG(CASE WHEN driver THEN NULL ELSE cpu_wait_percent END) AS worker_cpu_wait_avg_pct,
         AVG(CASE WHEN driver THEN NULL ELSE mem_used_percent END) AS worker_mem_avg_pct,
         percentile(CASE WHEN driver THEN NULL ELSE mem_used_percent END, 0.9) AS worker_mem_p90_pct,
         MAX(CASE WHEN driver THEN NULL ELSE mem_used_percent END) AS worker_mem_peak_pct,
         percentile(CASE WHEN driver THEN NULL ELSE mem_swap_percent END, 0.9) AS worker_swap_p90_pct,
         MAX(CASE WHEN driver THEN NULL ELSE mem_swap_percent END) AS worker_swap_peak_pct,
         AVG(CASE WHEN driver THEN cpu_pct END)                    AS driver_cpu_avg_pct,
         percentile(CASE WHEN driver THEN mem_used_percent END, 0.9) AS driver_mem_p90_pct,
         percentile(CASE WHEN driver THEN mem_swap_percent END, 0.9) AS driver_swap_p90_pct,
         SUM(network_received_bytes)                               AS network_received_bytes
  FROM job_slices
  GROUP BY workspace_id, job_id
),
driver_load AS (
  -- the drivers the DRIVER rule may judge: those of run clusters that had workers. A single-node
  -- cluster's only node is doing its own work (task_cluster_utilization's NOT single_node guard,
  -- which it applies per task run); pooled per job it would read as a busy driver over idle workers.
  SELECT s.workspace_id, s.job_id,
         AVG(CASE WHEN s.driver AND k.worker_nodes_peak > 0 THEN s.cpu_pct END) AS driver_cpu_avg_with_workers_pct
  FROM job_slices s
  LEFT JOIN run_peak k
    ON  k.workspace_id = s.workspace_id
    AND k.job_id       = s.job_id
    AND k.job_run_id   = s.job_run_id
    AND k.cluster_id   = s.cluster_id
  GROUP BY s.workspace_id, s.job_id
),
task_hours AS (
  SELECT workspace_id, job_id, task_key, SUM(task_s) AS task_key_s
  FROM task_obs
  GROUP BY workspace_id, job_id, task_key
),
task_heat AS (
  -- per task_key and NOT deduplicated: where the worker memory was high
  SELECT s.workspace_id, s.job_id, s.task_key,
         percentile(CASE WHEN s.driver THEN NULL ELSE s.mem_used_percent END, 0.9) AS task_mem_p90,
         MAX(h.task_key_s) AS task_key_s
  FROM task_slices s
  LEFT JOIN task_hours h
    ON  h.workspace_id = s.workspace_id
    AND h.job_id       = s.job_id
    AND h.task_key     = s.task_key
  GROUP BY s.workspace_id, s.job_id, s.task_key
),
hottest AS (
  -- NULL when no task's windows had worker memory (a single-node job): nothing to rank tasks by
  SELECT workspace_id, job_id,
         CASE WHEN task_mem_p90 IS NULL THEN NULL ELSE task_key END AS hottest_task_key,
         task_mem_p90 AS hottest_task_mem_p90_pct
  FROM task_heat
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY task_mem_p90 DESC, task_key_s DESC, task_key
  ) = 1
),
run_roll AS (
  SELECT t.workspace_id, t.job_id,
         COUNT(DISTINCT t.job_run_id)                                   AS runs_seen,
         COUNT(DISTINCT CASE WHEN t.task_not_executed THEN NULL
                             ELSE t.job_run_id END)                     AS runs_executed,
         COUNT(DISTINCT CASE WHEN t.task_not_executed THEN NULL
                             WHEN t.cluster_id IS NULL THEN NULL
                             ELSE t.job_run_id END)                     AS runs_with_cluster,
         COUNT(DISTINCT t.task_key)                                     AS task_keys_seen,
         SUM(t.task_s)                                                  AS task_s_total,
         COUNT(DISTINCT t.cluster_id)                                   AS clusters_seen,
         -- over the clusters used; NULL when none of them has a configuration row
         CASE WHEN MAX(CASE WHEN c.cluster_id IS NULL THEN 0 ELSE 1 END) = 0 THEN NULL
              ELSE MAX(CASE WHEN c.cluster_source IN ('UI', 'API') THEN 1 ELSE 0 END) = 1
         END                                                            AS on_all_purpose
  FROM task_obs t
  LEFT JOIN latest_clusters c
    ON  c.workspace_id = t.workspace_id
    AND c.cluster_id   = t.cluster_id
  GROUP BY t.workspace_id, t.job_id
),
run_last AS (
  SELECT workspace_id, job_id,
         MAX(run_start)    AS last_run_start,
         MAX(run_clusters) AS max_clusters_per_run
  FROM run_starts
  GROUP BY workspace_id, job_id
),
job_telemetry_clusters AS (
  SELECT DISTINCT workspace_id, job_id, cluster_id
  FROM job_slices
),
job_classic AS (
  -- the compute ids of the job that are classic clusters: a configuration row or node telemetry
  -- (a SQL task records its warehouse id in compute_ids, which has neither)
  SELECT DISTINCT t.workspace_id, t.job_id, t.cluster_id
  FROM task_obs t
  LEFT JOIN latest_clusters c
    ON  c.workspace_id = t.workspace_id
    AND c.cluster_id   = t.cluster_id
  LEFT JOIN job_telemetry_clusters s
    ON  s.workspace_id = t.workspace_id
    AND s.job_id       = t.job_id
    AND s.cluster_id   = t.cluster_id
  WHERE t.cluster_id IS NOT NULL
    AND (c.cluster_id IS NOT NULL OR s.cluster_id IS NOT NULL)
),
latest_cluster AS (
  -- the classic cluster of the latest-starting task run that recorded one, else any recorded id
  -- (ties: cluster_id)
  SELECT t.workspace_id, t.job_id, t.cluster_id AS latest_cluster_id
  FROM task_obs t
  LEFT JOIN job_classic k
    ON  k.workspace_id = t.workspace_id
    AND k.job_id       = t.job_id
    AND k.cluster_id   = t.cluster_id
  WHERE t.cluster_id IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY t.workspace_id, t.job_id
    ORDER BY CASE WHEN k.cluster_id IS NULL THEN 1 ELSE 0 END, t.task_start DESC, t.cluster_id
  ) = 1
),
judged AS (
  SELECT r.*,
         l.last_run_start, l.max_clusters_per_run,
         lc.latest_cluster_id,
         p.runs_with_telemetry, p.node_minutes, p.worker_minutes, p.driver_minutes,
         p.worker_cpu_avg_pct, p.worker_cpu_p50_pct, p.worker_cpu_p90_pct,
         p.worker_cpu_wait_avg_pct, p.worker_mem_avg_pct, p.worker_mem_p90_pct,
         p.worker_mem_peak_pct, p.worker_swap_p90_pct, p.worker_swap_peak_pct,
         p.driver_cpu_avg_pct, p.driver_mem_p90_pct, p.driver_swap_p90_pct,
         p.network_received_bytes,
         g.at_ceiling, g.worker_cpu_spread_pct,
         f.worker_nodes_seen_max, f.workers_configured_max,
         h.hottest_task_key, h.hottest_task_mem_p90_pct,
         d.driver_cpu_avg_with_workers_pct,
         (COALESCE(p.worker_minutes, 0) = 0 AND COALESCE(p.driver_minutes, 0) > 0) AS single_node,
         -- the numbers the verdict is judged on: the workers', or the driver's on a single-node cluster
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_minutes ELSE p.driver_minutes END AS judged_minutes,
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_cpu_avg_pct ELSE p.driver_cpu_avg_pct END AS eff_cpu_avg,
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_cpu_p50_pct ELSE p.driver_cpu_avg_pct END AS eff_cpu_p50,
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_mem_p90_pct ELSE p.driver_mem_p90_pct END AS eff_mem_p90,
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_swap_p90_pct ELSE p.driver_swap_p90_pct END AS eff_swap_p90,
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_cpu_wait_avg_pct ELSE NULL END AS eff_cpu_wait
  FROM run_roll r
  LEFT JOIN run_last l
    ON  l.workspace_id = r.workspace_id
    AND l.job_id       = r.job_id
  LEFT JOIN latest_cluster lc
    ON  lc.workspace_id = r.workspace_id
    AND lc.job_id       = r.job_id
  LEFT JOIN profile p
    ON  p.workspace_id = r.workspace_id
    AND p.job_id       = r.job_id
  LEFT JOIN ceiling g
    ON  g.workspace_id = r.workspace_id
    AND g.job_id       = r.job_id
  LEFT JOIN ceiling_ref f
    ON  f.workspace_id = r.workspace_id
    AND f.job_id       = r.job_id
  LEFT JOIN hottest h
    ON  h.workspace_id = r.workspace_id
    AND h.job_id       = r.job_id
  LEFT JOIN driver_load d
    ON  d.workspace_id = r.workspace_id
    AND d.job_id       = r.job_id
),
hinted AS (
  SELECT j.*,
         -- machine-level inference, in this precedence (field heuristics; see caveats)
         CASE
           WHEN j.runs_executed = 0                                                   THEN NULL
           WHEN j.judged_minutes IS NULL OR j.judged_minutes < 60            THEN NULL
           WHEN j.eff_mem_p90 >= 85
             OR COALESCE(j.eff_swap_p90, 0) >= 10                        THEN 'MEMORY'
           WHEN COALESCE(j.worker_cpu_spread_pct, 0) >= 40                 THEN 'SKEW'
           WHEN j.eff_cpu_p50 >= 70                                         THEN 'CPU'
           WHEN COALESCE(j.eff_cpu_wait, 0) >= 20                       THEN 'IO_WAIT'
           WHEN j.eff_cpu_avg < 20
            AND NOT j.single_node
            AND COALESCE(j.driver_cpu_avg_with_workers_pct, 0) >= 70        THEN 'DRIVER'
           WHEN j.eff_cpu_avg < 20                                       THEN 'IDLE'
           ELSE 'NONE'
         END AS pressure
  FROM judged j
)
SELECT h.workspace_id,
       h.job_id,
       j.job_name,                                                -- NULL for submit/workflow runs
       h.runs_seen,
       COALESCE(h.runs_with_telemetry, 0)                         AS runs_with_telemetry,
       h.task_keys_seen,
       ROUND(h.task_s_total / 3600.0, 2)                          AS task_hours_total,
       h.last_run_start,
       h.clusters_seen,
       h.max_clusters_per_run,
       h.on_all_purpose,
       h.latest_cluster_id,
       c.cluster_source,
       c.worker_node_type,
       c.driver_node_type,
       ROUND(m.memory_mb / 1024.0, 1)                             AS worker_memory_gb,
       m.core_count                                               AS worker_cores,
       h.workers_configured_max,
       h.worker_nodes_seen_max,
       h.at_ceiling,
       h.single_node,
       h.node_minutes,
       h.worker_minutes,
       ROUND(h.worker_cpu_avg_pct, 1)                             AS worker_cpu_avg_pct,
       ROUND(h.worker_cpu_p50_pct, 1)                             AS worker_cpu_p50_pct,
       ROUND(h.worker_cpu_p90_pct, 1)                             AS worker_cpu_p90_pct,
       ROUND(h.worker_cpu_spread_pct, 1)                          AS worker_cpu_spread_pct,
       ROUND(h.worker_cpu_wait_avg_pct, 1)                        AS worker_cpu_wait_avg_pct,
       ROUND(h.worker_mem_avg_pct, 1)                             AS worker_mem_avg_pct,
       ROUND(h.worker_mem_p90_pct, 1)                             AS worker_mem_p90_pct,
       ROUND(h.worker_mem_peak_pct, 1)                            AS worker_mem_peak_pct,
       ROUND(h.worker_swap_p90_pct, 1)                            AS worker_swap_p90_pct,
       ROUND(h.worker_swap_peak_pct, 1)                           AS worker_swap_peak_pct,
       ROUND(h.driver_cpu_avg_pct, 1)                             AS driver_cpu_avg_pct,
       ROUND(h.driver_mem_p90_pct, 1)                             AS driver_mem_p90_pct,
       ROUND(h.network_received_bytes / 1e9, 2)                   AS network_received_gb,
       h.hottest_task_key,
       ROUND(h.hottest_task_mem_p90_pct, 1)                       AS hottest_task_mem_p90_pct,
       h.pressure,
       -- the lever: one enum, its words written once in the header's actions ladder
       CASE
         WHEN h.pressure IS NULL                  THEN NULL
         WHEN h.pressure = 'MEMORY'               THEN 'SCALE_UP_MEMORY'
         WHEN h.pressure = 'CPU' AND h.at_ceiling THEN 'SCALE_OUT'
         WHEN h.pressure = 'SKEW'                 THEN 'FIX_SKEW'
         WHEN h.pressure = 'DRIVER'               THEN 'DISTRIBUTE_WORK'
         WHEN h.pressure = 'IO_WAIT'              THEN 'FIX_IO'
         WHEN h.pressure = 'IDLE'                 THEN 'SCALE_DOWN'
         ELSE 'NONE'
       END AS scaling_hint,
       -- the WHY, built from the row's own numbers (whole-number percentages). Every figure is
       -- COALESCEd to 'n/a' as a string: Databricks' CONCAT returns NULL when any argument is NULL
       -- (DuckDB's skips it), and a judged row must always carry its reason. A single-node job is
       -- judged on the driver's numbers, so its reason names them as the driver's.
       CASE
         WHEN h.pressure IS NULL THEN NULL
         ELSE CONCAT(
           CASE h.pressure
             WHEN 'MEMORY' THEN CONCAT(CASE WHEN h.single_node THEN 'driver memory p90 '
                                            ELSE 'worker memory p90 ' END,
                                       COALESCE(CAST(CAST(ROUND(h.eff_mem_p90, 0) AS BIGINT) AS STRING), 'n/a'),
                                       '% (threshold ', 85, '%), swap p90 ',
                                       CAST(CAST(ROUND(COALESCE(h.eff_swap_p90, 0), 0) AS BIGINT) AS STRING),
                                       '% (threshold ', 10, '%)',
                                       CASE WHEN h.hottest_task_mem_p90_pct IS NULL THEN ''
                                            ELSE CONCAT('; hottest task ', COALESCE(h.hottest_task_key, 'n/a'))
                                       END)
             WHEN 'CPU' THEN CASE
               WHEN h.single_node THEN CONCAT('driver CPU avg ',
                                              COALESCE(CAST(CAST(ROUND(h.eff_cpu_p50, 0) AS BIGINT) AS STRING), 'n/a'),
                                              '% (threshold ', 70, '%) on a single-node cluster')
               ELSE CONCAT('worker CPU p50 ',
                           COALESCE(CAST(CAST(ROUND(h.eff_cpu_p50, 0) AS BIGINT) AS STRING), 'n/a'),
                           '% (threshold ', 70, '%), up to ',
                           COALESCE(CAST(h.worker_nodes_seen_max AS STRING), 'n/a'),
                           CASE WHEN h.workers_configured_max IS NULL OR h.workers_configured_max = 0 THEN ''
                                ELSE CONCAT(' of ', CAST(h.workers_configured_max AS STRING)) END,
                           ' workers seen',
                           -- keyed on the quoted cluster's own configuration first, so the ending
                           -- never calls a cluster with an unknown or zero ceiling "headroom"
                           CASE WHEN h.workers_configured_max IS NULL THEN ' -- cluster config not found'
                                WHEN h.workers_configured_max = 0 THEN ' -- no worker ceiling configured'
                                WHEN h.at_ceiling THEN ' -- at ceiling'
                                ELSE ' -- headroom' END)
             END
             WHEN 'SKEW' THEN CONCAT('hottest-coldest worker CPU gap ',
                                     COALESCE(CAST(CAST(ROUND(h.worker_cpu_spread_pct, 0) AS BIGINT) AS STRING), 'n/a'),
                                     '% in the worst run (threshold ', 40, '%)')
             WHEN 'DRIVER' THEN CONCAT('driver CPU ',
                                       COALESCE(CAST(CAST(ROUND(h.driver_cpu_avg_with_workers_pct, 0) AS BIGINT) AS STRING), 'n/a'),
                                       '% while workers averaged ',
                                       COALESCE(CAST(CAST(ROUND(h.eff_cpu_avg, 0) AS BIGINT) AS STRING), 'n/a'), '%')
             WHEN 'IO_WAIT' THEN CONCAT('worker CPU wait ',
                                        COALESCE(CAST(CAST(ROUND(h.eff_cpu_wait, 0) AS BIGINT) AS STRING), 'n/a'),
                                        '% (threshold ', 20, '%)')
             WHEN 'IDLE' THEN CONCAT(CASE WHEN h.single_node THEN 'the driver averaged '
                                          ELSE 'workers averaged ' END,
                                     COALESCE(CAST(CAST(ROUND(h.eff_cpu_avg, 0) AS BIGINT) AS STRING), 'n/a'),
                                     '% CPU (threshold ', 20, '%)')
             ELSE CONCAT('no threshold crossed: ',
                         CASE WHEN h.single_node THEN 'driver CPU avg ' ELSE 'CPU p50 ' END,
                         COALESCE(CAST(CAST(ROUND(h.eff_cpu_p50, 0) AS BIGINT) AS STRING), 'n/a'),
                         CASE WHEN h.single_node THEN '%, driver memory p90 ' ELSE '%, memory p90 ' END,
                         COALESCE(CAST(CAST(ROUND(h.eff_mem_p90, 0) AS BIGINT) AS STRING), 'n/a'),
                         '%, swap p90 ',
                         CAST(CAST(ROUND(COALESCE(h.eff_swap_p90, 0), 0) AS BIGINT) AS STRING), '%')
           END,
           CASE WHEN h.on_all_purpose THEN ' -- on an all-purpose cluster shared with other work'
                ELSE '' END)
       END AS pressure_reason,
       -- why a job could not be judged (NULL when it could)
       CASE
         WHEN h.pressure IS NOT NULL                        THEN NULL
         WHEN h.runs_executed = 0                           THEN 'task_not_executed'
         WHEN h.runs_with_cluster = 0                       THEN 'no_cluster_recorded'
         WHEN h.node_minutes IS NULL OR h.node_minutes = 0  THEN 'no_node_timeline_rows'
         ELSE 'too_few_slices'
       END AS not_assessed_reason,
       -- status: a WARN must have a lever, so CPU-bound with headroom is OK.
       -- A job with no telemetry is NOT_ASSESSED - "could not look", never "nothing there".
       CASE
         WHEN h.pressure IS NULL                                  THEN 'NOT_ASSESSED'
         WHEN h.pressure = 'MEMORY'                               THEN 'CRITICAL'
         WHEN h.pressure IN ('SKEW', 'DRIVER', 'IO_WAIT', 'IDLE') THEN 'WARN'
         WHEN h.pressure = 'CPU' AND h.at_ceiling                 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM hinted h
LEFT JOIN latest_clusters c
  ON  h.workspace_id      = c.workspace_id
  AND h.latest_cluster_id = c.cluster_id
LEFT JOIN node_mem m
  ON  c.worker_node_type = m.node_type
LEFT JOIN latest_jobs j
  ON  h.workspace_id = j.workspace_id
  AND h.job_id       = j.job_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         task_hours_total DESC,
         workspace_id, job_id
LIMIT 100000
) q
"""
CHECKS["task_cluster_utilization"] = r"""
-- generated from dbt/models/databricks_direct/compute/d_task_cluster_utilization.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/compute/task_cluster_utilization.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH run_agg AS (
  SELECT workspace_id, job_id, run_id,
         MIN(period_start_time)    AS run_start,
         MAX(period_end_time)      AS last_seen,
         MAX(result_state)         AS result_state,       -- NULL until the run's end row lands
         -- end row only; NULL before Dec 2025 and 0 for every multi-task job (doc), so 0 = not reported
         NULLIF(MAX(run_duration_seconds), 0) AS run_s_reported
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -LEAST(30, 90), current_date())
  GROUP BY workspace_id, job_id, run_id
),
run_obs AS (
  SELECT workspace_id, job_id, run_id,
         (result_state IS NULL) AS in_flight,
         COALESCE(run_s_reported,
                  timestampdiff(SECOND, run_start,
                                CASE WHEN result_state IS NULL THEN current_timestamp()
                                     ELSE last_seen END)) AS run_s
  FROM run_agg
),
long_runs AS (
  -- the same run bound as lakeflow_long_running_runs, so the two queries compose
  SELECT workspace_id, job_id, run_id, in_flight, run_s
  FROM run_obs
  WHERE run_s >= 2 * 3600
     OR run_s IS NULL
),
task_agg AS (
  -- one row per TASK RUN of a long run (run_id here is the task run id)
  SELECT t.workspace_id, t.job_id, t.job_run_id,
         t.run_id                              AS task_run_id,
         t.task_key,
         MIN(t.period_start_time)              AS task_start,
         MAX(t.period_end_time)                AS task_last_seen,
         MAX(t.result_state)                   AS task_result_state,
         NULLIF(MAX(t.execution_duration_seconds), 0) AS task_exec_s,
         -- first compute id of the task; NULL on serverless and on pre-Dec-2025 rows
         MAX(try_element_at(t.compute_ids, 1)) AS cluster_id,
         -- guarded: size(NULL) is -1 outside ANSI mode, and a -1 would read as a count
         MAX(CASE WHEN t.compute_ids IS NULL THEN NULL ELSE size(t.compute_ids) END) AS compute_ids_count
  FROM `system`.`lakeflow`.`job_task_run_timeline` t
  JOIN long_runs r
    ON  r.workspace_id = t.workspace_id
    AND r.job_id       = t.job_id
    AND r.run_id       = t.job_run_id
  WHERE t.period_start_time >= dateadd(day, -LEAST(30, 90), current_date())
  GROUP BY t.workspace_id, t.job_id, t.job_run_id, t.run_id, t.task_key
),
task_obs AS (
  SELECT workspace_id, job_id, job_run_id, task_run_id, task_key, task_start, task_result_state,
         cluster_id, compute_ids_count,
         -- a task that never ran: SKIPPED / BLOCKED, or the doc's zero-length row
         (task_result_state IN ('SKIPPED', 'BLOCKED')
          OR (task_result_state IS NOT NULL AND task_start = task_last_seen)) AS task_not_executed,
         -- the telemetry window: the task's observed wall clock, open-ended while in flight
         CASE WHEN task_result_state IS NULL THEN current_timestamp() ELSE task_last_seen END AS task_end,
         COALESCE(task_exec_s,
                  timestampdiff(SECOND, task_start,
                                CASE WHEN task_result_state IS NULL THEN current_timestamp()
                                     ELSE task_last_seen END)) AS task_s
  FROM task_agg
),
slices AS (
  -- every node-minute of the task's cluster that overlaps the task's window
  SELECT t.workspace_id, t.job_run_id, t.task_run_id, t.cluster_id,
         n.instance_id, n.driver,
         n.cpu_user_percent + n.cpu_system_percent AS cpu_pct,
         n.cpu_wait_percent, n.mem_used_percent, n.mem_swap_percent,
         n.network_sent_bytes, n.network_received_bytes
  FROM task_obs t
  JOIN `system`.`compute`.`node_timeline` n
    ON  n.workspace_id = t.workspace_id
    AND n.cluster_id   = t.cluster_id
    AND n.start_time   <  t.task_end
    AND n.end_time     >  t.task_start
  WHERE n.start_time >= dateadd(day, -LEAST(30, 90), current_date())
),
per_node AS (
  -- one row per node of the task's cluster, to measure skew across workers
  SELECT workspace_id, job_run_id, task_run_id, cluster_id, instance_id, driver,
         AVG(cpu_pct) AS node_cpu_avg
  FROM slices
  GROUP BY workspace_id, job_run_id, task_run_id, cluster_id, instance_id, driver
),
skew AS (
  SELECT workspace_id, job_run_id, task_run_id, cluster_id,
         COUNT(*)                                           AS nodes_seen,
         SUM(CASE WHEN driver THEN 0 ELSE 1 END)            AS worker_nodes_seen,
         MAX(CASE WHEN driver THEN NULL ELSE node_cpu_avg END)
           - MIN(CASE WHEN driver THEN NULL ELSE node_cpu_avg END) AS worker_cpu_spread_pct
  FROM per_node
  GROUP BY workspace_id, job_run_id, task_run_id, cluster_id
),
profile AS (
  SELECT workspace_id, job_run_id, task_run_id, cluster_id,
         COUNT(*)                                                  AS node_minutes,
         SUM(CASE WHEN driver THEN 0 ELSE 1 END)                   AS worker_minutes,
         SUM(CASE WHEN driver THEN 1 ELSE 0 END)                   AS driver_minutes,
         AVG(CASE WHEN driver THEN NULL ELSE cpu_pct END)          AS worker_cpu_avg_pct,
         percentile(CASE WHEN driver THEN NULL ELSE cpu_pct END, 0.5) AS worker_cpu_p50_pct,
         percentile(CASE WHEN driver THEN NULL ELSE cpu_pct END, 0.9) AS worker_cpu_p90_pct,
         MAX(CASE WHEN driver THEN NULL ELSE cpu_pct END)          AS worker_cpu_peak_pct,
         AVG(CASE WHEN driver THEN NULL ELSE cpu_wait_percent END) AS worker_cpu_wait_avg_pct,
         AVG(CASE WHEN driver THEN NULL ELSE mem_used_percent END) AS worker_mem_avg_pct,
         percentile(CASE WHEN driver THEN NULL ELSE mem_used_percent END, 0.9) AS worker_mem_p90_pct,
         MAX(CASE WHEN driver THEN NULL ELSE mem_used_percent END) AS worker_mem_peak_pct,
         percentile(CASE WHEN driver THEN NULL ELSE mem_swap_percent END, 0.9) AS worker_swap_p90_pct,
         MAX(CASE WHEN driver THEN NULL ELSE mem_swap_percent END) AS worker_swap_peak_pct,
         AVG(CASE WHEN driver THEN cpu_pct END)                    AS driver_cpu_avg_pct,
         MAX(CASE WHEN driver THEN cpu_pct END)                    AS driver_cpu_peak_pct,
         percentile(CASE WHEN driver THEN mem_used_percent END, 0.9) AS driver_mem_p90_pct,
         MAX(CASE WHEN driver THEN mem_used_percent END)           AS driver_mem_peak_pct,
         percentile(CASE WHEN driver THEN mem_swap_percent END, 0.9) AS driver_swap_p90_pct,
         MAX(CASE WHEN driver THEN mem_swap_percent END)           AS driver_swap_peak_pct,
         SUM(network_sent_bytes)                                   AS network_sent_bytes,
         SUM(network_received_bytes)                               AS network_received_bytes
  FROM slices
  GROUP BY workspace_id, job_run_id, task_run_id, cluster_id
),
all_task_windows AS (
  -- every task run in the window with its first cluster, to count cluster sharing
  SELECT workspace_id, run_id AS task_run_id,
         MAX(try_element_at(compute_ids, 1)) AS cluster_id,
         MIN(period_start_time)              AS t_start,
         CASE WHEN MAX(result_state) IS NULL THEN current_timestamp()
              ELSE MAX(period_end_time) END  AS t_end
  FROM `system`.`lakeflow`.`job_task_run_timeline`
  WHERE period_start_time >= dateadd(day, -LEAST(30, 90), current_date())
  GROUP BY workspace_id, run_id
),
sharing AS (
  SELECT t.workspace_id, t.job_run_id, t.task_run_id,
         COUNT(o.task_run_id) AS overlapping_task_runs
  FROM task_obs t
  LEFT JOIN all_task_windows o
    ON  o.workspace_id = t.workspace_id
    AND o.cluster_id   = t.cluster_id
    AND o.task_run_id <> t.task_run_id
    AND o.t_end        >  o.t_start          -- a never-run task (zero-length row) shares nothing
    AND o.t_start      <  t.task_end
    AND o.t_end        >  t.task_start
  GROUP BY t.workspace_id, t.job_run_id, t.task_run_id
),
latest_clusters AS (
  -- system.compute.clusters is SCD2: the newest row per cluster (deleted clusters kept)
  SELECT workspace_id, cluster_id, cluster_name, cluster_source, driver_node_type, worker_node_type,
         worker_count, min_autoscale_workers, max_autoscale_workers, dbr_version
  FROM `system`.`compute`.`clusters`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC
  ) = 1
),
latest_jobs AS (
  -- system.lakeflow.jobs is SCD2: one row per change, so take the newest per job
  SELECT workspace_id, job_id, name AS job_name
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
),
ws AS (
  -- workspace name + the base URL the deep links are built from (the stored URL ends in '/')
  SELECT workspace_id, workspace_name,
         regexp_replace(workspace_url, '/+$', '') AS base_url
  FROM `system`.`access`.`workspaces_latest`
),
judged AS (
  SELECT t.*, r.run_s, r.in_flight,
         p.node_minutes, p.worker_minutes, p.driver_minutes,
         k.nodes_seen, k.worker_nodes_seen, k.worker_cpu_spread_pct,
         p.worker_cpu_avg_pct, p.worker_cpu_p50_pct, p.worker_cpu_p90_pct, p.worker_cpu_peak_pct,
         p.worker_cpu_wait_avg_pct, p.worker_mem_avg_pct, p.worker_mem_p90_pct,
         p.worker_mem_peak_pct, p.worker_swap_p90_pct, p.worker_swap_peak_pct,
         p.driver_cpu_avg_pct, p.driver_cpu_peak_pct, p.driver_mem_p90_pct, p.driver_mem_peak_pct,
         p.driver_swap_p90_pct, p.driver_swap_peak_pct, p.network_sent_bytes, p.network_received_bytes,
         s.overlapping_task_runs,
         (COALESCE(p.worker_minutes, 0) = 0 AND COALESCE(p.driver_minutes, 0) > 0) AS single_node,
         -- the numbers the hint is judged on: the workers', or the driver's on a single-node cluster
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_minutes ELSE p.driver_minutes END AS judged_minutes,
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_cpu_avg_pct ELSE p.driver_cpu_avg_pct END AS eff_cpu_avg,
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_cpu_p50_pct ELSE p.driver_cpu_avg_pct END AS eff_cpu_p50,
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_mem_p90_pct ELSE p.driver_mem_p90_pct END AS eff_mem_p90,
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_swap_p90_pct ELSE p.driver_swap_p90_pct END AS eff_swap_p90,
         CASE WHEN COALESCE(p.worker_minutes, 0) > 0 THEN p.worker_cpu_wait_avg_pct ELSE NULL END AS eff_cpu_wait
  FROM task_obs t
  JOIN long_runs r
    ON  r.workspace_id = t.workspace_id
    AND r.job_id       = t.job_id
    AND r.run_id       = t.job_run_id
  LEFT JOIN profile p
    ON  p.workspace_id = t.workspace_id
    AND p.job_run_id   = t.job_run_id
    AND p.task_run_id  = t.task_run_id
  LEFT JOIN skew k
    ON  k.workspace_id = t.workspace_id
    AND k.job_run_id   = t.job_run_id
    AND k.task_run_id  = t.task_run_id
  LEFT JOIN sharing s
    ON  s.workspace_id = t.workspace_id
    AND s.job_run_id   = t.job_run_id
    AND s.task_run_id  = t.task_run_id
),
hinted AS (
  SELECT j.*,
         -- machine-level inference, in this precedence (field heuristics; see caveats)
         CASE
           WHEN j.task_not_executed                                                THEN NULL
           WHEN j.judged_minutes IS NULL OR j.judged_minutes < 60        THEN NULL
           WHEN j.eff_mem_p90 >= 85 OR COALESCE(j.eff_swap_p90, 0) >= 10 THEN 'MEMORY_PRESSURE'
           WHEN COALESCE(j.worker_cpu_spread_pct, 0) >= 40             THEN 'SKEWED'
           WHEN j.eff_cpu_p50 >= 70                                     THEN 'COMPUTE_BOUND'
           WHEN COALESCE(j.eff_cpu_wait, 0) >= 20                   THEN 'IO_WAIT'
           WHEN j.eff_cpu_avg < 20
            AND NOT j.single_node
            AND COALESCE(j.driver_cpu_avg_pct, 0) >= 70                 THEN 'DRIVER_BOUND'
           WHEN j.eff_cpu_avg < 20                                   THEN 'WAITING_OR_IDLE'
           ELSE 'MIXED'
         END AS bottleneck_hint
  FROM judged j
)
SELECT h.workspace_id,
       w.workspace_name,                                          -- needs system.access; see caveats
       h.job_id,
       j.job_name,                                                -- NULL for submit/workflow runs
       h.job_run_id,
       ROUND(h.run_s / 3600.0, 2)                                 AS run_hours,
       h.in_flight,
       h.task_key,
       h.task_run_id,
       COALESCE(h.task_result_state, 'RUNNING')                   AS task_state,
       h.task_start,
       ROUND(h.task_s / 3600.0, 2)                                AS task_hours,
       h.cluster_id,
       h.compute_ids_count,
       c.cluster_name, c.cluster_source,
       c.worker_node_type, c.driver_node_type,
       c.worker_count                                             AS worker_count_configured,
       c.min_autoscale_workers, c.max_autoscale_workers, c.dbr_version,
       h.nodes_seen, h.worker_nodes_seen, h.node_minutes, h.worker_minutes, h.single_node,
       ROUND(h.worker_cpu_avg_pct, 1)                             AS worker_cpu_avg_pct,
       ROUND(h.worker_cpu_p50_pct, 1)                             AS worker_cpu_p50_pct,
       ROUND(h.worker_cpu_p90_pct, 1)                             AS worker_cpu_p90_pct,
       ROUND(h.worker_cpu_peak_pct, 1)                            AS worker_cpu_peak_pct,
       ROUND(h.worker_cpu_wait_avg_pct, 1)                        AS worker_cpu_wait_avg_pct,
       ROUND(h.worker_cpu_spread_pct, 1)                          AS worker_cpu_spread_pct,
       ROUND(h.worker_mem_avg_pct, 1)                             AS worker_mem_avg_pct,
       ROUND(h.worker_mem_p90_pct, 1)                             AS worker_mem_p90_pct,
       ROUND(h.worker_mem_peak_pct, 1)                            AS worker_mem_peak_pct,
       ROUND(h.worker_swap_p90_pct, 1)                            AS worker_swap_p90_pct,
       ROUND(h.worker_swap_peak_pct, 1)                           AS worker_swap_peak_pct,
       ROUND(h.driver_cpu_avg_pct, 1)                             AS driver_cpu_avg_pct,
       ROUND(h.driver_cpu_peak_pct, 1)                            AS driver_cpu_peak_pct,
       ROUND(h.driver_mem_peak_pct, 1)                            AS driver_mem_peak_pct,
       ROUND(h.network_sent_bytes / 1e9, 2)                       AS network_sent_gb,
       ROUND(h.network_received_bytes / 1e9, 2)                   AS network_received_gb,
       h.overlapping_task_runs,
       h.bottleneck_hint,
       -- why a task could not be judged (NULL when it could)
       CASE
         WHEN h.bottleneck_hint IS NOT NULL THEN NULL
         WHEN h.task_not_executed           THEN 'task_not_executed'
         WHEN h.cluster_id IS NULL          THEN 'no_cluster_recorded'
         WHEN h.node_minutes IS NULL        THEN 'no_node_timeline_rows'
         ELSE 'too_few_slices'
       END                                                        AS not_assessed_reason,
       -- deep links; all three are NULL without system.access (see caveats)
       CONCAT(w.base_url, '/jobs/', h.job_id, '/runs/', h.job_run_id) AS run_url,
       CONCAT(w.base_url, '/compute/clusters/', h.cluster_id)     AS cluster_url,
       CONCAT(w.base_url, '/compute/clusters/', h.cluster_id, '/sparkUi') AS spark_ui_url,
       -- status: worst-first band on the hint (field heuristics; every threshold is a header param).
       -- A task with no telemetry is NOT_ASSESSED - "could not look", never "nothing there".
       CASE
         WHEN h.bottleneck_hint IS NULL                                   THEN 'NOT_ASSESSED'
         WHEN h.bottleneck_hint = 'MEMORY_PRESSURE'                       THEN 'CRITICAL'
         WHEN h.bottleneck_hint IN ('SKEWED', 'IO_WAIT', 'DRIVER_BOUND', 'WAITING_OR_IDLE') THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM hinted h
LEFT JOIN latest_clusters c
  ON  h.workspace_id = c.workspace_id
  AND h.cluster_id   = c.cluster_id
LEFT JOIN latest_jobs j
  ON  h.workspace_id = j.workspace_id
  AND h.job_id       = j.job_id
LEFT JOIN ws w
  ON  h.workspace_id = w.workspace_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         task_hours DESC,
         workspace_id, job_id, job_run_id, task_key
LIMIT 100000
) q
"""
CHECKS["lakeflow_job_oversized"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_job_oversized.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/jobs_pipelines/lakeflow_job_oversized.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH task_runs AS (
  -- one row per TASK RUN starting in the window (run_id here is the task run id)
  SELECT workspace_id, job_id, job_run_id,
         run_id                              AS task_run_id,
         task_key,
         MIN(period_start_time)              AS task_start,
         MAX(period_end_time)                AS task_last_seen,
         MAX(result_state)                   AS task_result_state,
         -- first compute id of the task; NULL on serverless and on pre-Dec-2025 rows
         MAX(try_element_at(compute_ids, 1)) AS cluster_id
  FROM `system`.`lakeflow`.`job_task_run_timeline`
  WHERE period_start_time >= dateadd(day, -LEAST(30, 90), current_date())
  GROUP BY workspace_id, job_id, job_run_id, run_id, task_key
),
task_obs AS (
  SELECT workspace_id, job_id, job_run_id, task_run_id, task_key, task_start, cluster_id,
         -- a task that never ran: SKIPPED / BLOCKED, or the doc's zero-length row
         (task_result_state IN ('SKIPPED', 'BLOCKED')
          OR (task_result_state IS NOT NULL AND task_start = task_last_seen)) AS task_not_executed,
         -- the telemetry window: the task's observed wall clock, open-ended while in flight
         CASE WHEN task_result_state IS NULL THEN current_timestamp() ELSE task_last_seen END AS task_end
  FROM task_runs
),
job_task_agg AS (
  SELECT workspace_id, job_id,
         COUNT(DISTINCT CASE WHEN task_not_executed THEN NULL ELSE job_run_id END) AS runs_executed,
         COUNT(DISTINCT CASE WHEN task_not_executed THEN NULL
                             WHEN cluster_id IS NULL THEN NULL
                             ELSE job_run_id END)                                  AS runs_with_cluster
  FROM task_obs
  GROUP BY workspace_id, job_id
),
task_slices AS (
  -- every node-minute of each executed task run's cluster that overlaps that task run's window
  SELECT t.workspace_id, t.job_id, t.job_run_id, t.cluster_id,
         n.instance_id, n.start_time, n.driver,
         n.cpu_user_percent + n.cpu_system_percent AS cpu_pct,
         n.mem_used_percent, n.mem_swap_percent
  FROM task_obs t
  JOIN `system`.`compute`.`node_timeline` n
    ON  n.workspace_id = t.workspace_id
    AND n.cluster_id   = t.cluster_id
    AND n.start_time   <  t.task_end
    AND n.end_time     >  t.task_start
  WHERE n.start_time >= dateadd(day, -LEAST(30, 90), current_date())
    AND NOT t.task_not_executed
),
job_slices AS (
  -- the dedupe: parallel tasks of one run on one shared cluster count each node-minute once
  SELECT DISTINCT workspace_id, job_id, job_run_id, cluster_id, instance_id, start_time, driver,
         cpu_pct, mem_used_percent, mem_swap_percent
  FROM task_slices
),
profile AS (
  -- the job's pooled hardware profile over its deduplicated node-minutes
  SELECT workspace_id, job_id,
         COUNT(DISTINCT job_run_id)                                AS runs_with_telemetry,
         SUM(CASE WHEN driver THEN 0 ELSE 1 END)                   AS worker_minutes,
         SUM(CASE WHEN driver THEN 1 ELSE 0 END)                   AS driver_minutes,
         percentile(CASE WHEN driver THEN NULL ELSE cpu_pct END, 0.9)          AS worker_cpu_p90_pct,
         percentile(CASE WHEN driver THEN NULL ELSE mem_used_percent END, 0.9) AS worker_mem_p90_pct,
         MAX(CASE WHEN driver THEN NULL ELSE cpu_pct END)                      AS worker_cpu_peak_pct,
         MAX(CASE WHEN driver THEN NULL ELSE mem_used_percent END)             AS worker_mem_peak_pct,
         MAX(CASE WHEN driver THEN NULL ELSE mem_swap_percent END)             AS worker_swap_peak_pct
  FROM job_slices
  GROUP BY workspace_id, job_id
),
latest_clusters AS (
  -- system.compute.clusters is SCD2: the newest row per cluster (deleted clusters kept)
  SELECT workspace_id, cluster_id, cluster_source, worker_node_type, worker_count, max_autoscale_workers
  FROM `system`.`compute`.`clusters`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC
  ) = 1
),
latest_jobs AS (
  -- system.lakeflow.jobs is SCD2: one row per change, so take the newest per job
  SELECT workspace_id, job_id, name AS job_name
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
),
node_mem AS (
  -- memory and cores per node type (account-level reference table)
  SELECT node_type, MAX(memory_mb) AS memory_mb, MAX(core_count) AS core_count
  FROM `system`.`compute`.`node_types`
  GROUP BY node_type
),
latest_cluster AS (
  -- the classic cluster (one this query actually has node telemetry for) of the
  -- latest-starting task run (ties: cluster_id)
  SELECT t.workspace_id, t.job_id, t.cluster_id AS latest_cluster_id
  FROM task_obs t
  JOIN (SELECT DISTINCT workspace_id, job_id, cluster_id FROM job_slices) s
    ON  s.workspace_id = t.workspace_id
    AND s.job_id       = t.job_id
    AND s.cluster_id   = t.cluster_id
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY t.workspace_id, t.job_id ORDER BY t.task_start DESC, t.cluster_id
  ) = 1
),
run_starts AS (
  SELECT workspace_id, job_id, job_run_id, MIN(task_start) AS run_start
  FROM task_obs
  GROUP BY workspace_id, job_id, job_run_id
),
run_minutes AS (
  -- each deduplicated node-minute on its minute boundary, to count the workers running at once
  SELECT workspace_id, job_id, job_run_id, cluster_id, instance_id, driver,
         date_trunc('MINUTE', start_time) AS minute_start
  FROM job_slices
),
run_minute_workers AS (
  SELECT workspace_id, job_id, job_run_id, cluster_id, minute_start,
         COUNT(DISTINCT CASE WHEN driver THEN NULL ELSE instance_id END) AS workers_now
  FROM run_minutes
  GROUP BY workspace_id, job_id, job_run_id, cluster_id, minute_start
),
run_peak AS (
  -- the most workers running at once per run+cluster: churned or replaced instances never double-count
  SELECT workspace_id, job_id, job_run_id, cluster_id, MAX(workers_now) AS worker_nodes_peak
  FROM run_minute_workers
  GROUP BY workspace_id, job_id, job_run_id, cluster_id
),
worker_ref AS (
  -- the job's BUSIEST run+cluster (most workers running at once; ties: latest run, then
  -- cluster_id): workers configured vs seen for the suggested_action lever
  SELECT k.workspace_id, k.job_id,
         k.worker_nodes_peak                                AS worker_nodes_seen_max,
         COALESCE(c.max_autoscale_workers, c.worker_count)  AS workers_configured_max
  FROM run_peak k
  LEFT JOIN run_starts s
    ON  s.workspace_id = k.workspace_id AND s.job_id = k.job_id AND s.job_run_id = k.job_run_id
  LEFT JOIN latest_clusters c
    ON  c.workspace_id = k.workspace_id AND c.cluster_id = k.cluster_id
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY k.workspace_id, k.job_id
    ORDER BY k.worker_nodes_peak DESC, s.run_start DESC, k.cluster_id
  ) = 1
),
judged AS (
  SELECT a.workspace_id, a.job_id, a.runs_executed, a.runs_with_cluster,
         p.runs_with_telemetry, p.worker_cpu_p90_pct, p.worker_mem_p90_pct,
         p.worker_cpu_peak_pct, p.worker_mem_peak_pct, p.worker_swap_peak_pct,
         (COALESCE(p.worker_minutes, 0) = 0 AND COALESCE(p.driver_minutes, 0) > 0) AS single_node,
         lc.latest_cluster_id,
         w.worker_nodes_seen_max, w.workers_configured_max
  FROM job_task_agg a
  LEFT JOIN profile p
    ON  p.workspace_id = a.workspace_id AND p.job_id = a.job_id
  LEFT JOIN latest_cluster lc
    ON  lc.workspace_id = a.workspace_id AND lc.job_id = a.job_id
  LEFT JOIN worker_ref w
    ON  w.workspace_id = a.workspace_id AND w.job_id = a.job_id
),
sized AS (
  SELECT j.*,
         CASE
           WHEN j.runs_executed = 0                                                THEN NULL
           WHEN j.runs_with_cluster = 0                                            THEN NULL
           WHEN j.runs_with_telemetry IS NULL OR j.runs_with_telemetry < 3 THEN NULL
           WHEN j.single_node                                                      THEN FALSE
           -- memory on its peak and no swap: a smaller node must still hold the busiest minute
           ELSE (j.worker_cpu_p90_pct < 30 AND j.worker_mem_peak_pct < 40
                 AND COALESCE(j.worker_swap_peak_pct, 0) < 1)
         END AS oversized
  FROM judged j
),
measured_clusters AS (
  SELECT DISTINCT workspace_id, job_id, job_run_id, cluster_id FROM job_slices
),
cost_usage AS (
  -- the exact per-run attribution and effective-list price join lakeflow_job_run_cost /
  -- lakeflow_job_cost_summary use (usage_metadata.job_id/job_run_id, DEC-66.1), kept to the classic
  -- clusters whose node use was measured: only their size can change, not serverless or the rest
  SELECT u.workspace_id,
         u.usage_metadata.job_id     AS job_id,
         u.usage_metadata.job_run_id AS job_run_id,
         SUM(u.usage_quantity * lp.list_rate)                          AS net_run_list_cost,
         CASE
           WHEN SUM(CASE WHEN lp.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                         THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN lp.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END AS price_basis
  FROM `system`.`billing`.`usage` u
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective (post-promotion) list price - DEC-66.1
    FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
  ) lp
    ON u.sku_name = lp.sku_name
   AND u.cloud    = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_date >= dateadd(day, -30, current_date())
    AND u.usage_date < current_date()
    AND upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.job_id IS NOT NULL
    AND u.usage_metadata.job_run_id IS NOT NULL
    AND EXISTS (SELECT 1 FROM measured_clusters m
                WHERE m.workspace_id = u.workspace_id AND m.job_id = u.usage_metadata.job_id
                  AND m.job_run_id = u.usage_metadata.job_run_id AND m.cluster_id = u.usage_metadata.cluster_id)
  GROUP BY u.workspace_id, u.usage_metadata.job_id, u.usage_metadata.job_run_id
),
job_cost AS (
  SELECT workspace_id, job_id,
         SUM(net_run_list_cost) AS job_list_cost_usd,
         CASE
           WHEN SUM(CASE WHEN price_basis = 'unpriced' THEN 1 ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN price_basis = 'priced' THEN 1 ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END AS price_basis
  FROM cost_usage
  GROUP BY workspace_id, job_id
),
priced AS (
  SELECT s.*, c.job_list_cost_usd, c.price_basis
  FROM sized s
  LEFT JOIN job_cost c
    ON  c.workspace_id = s.workspace_id AND c.job_id = s.job_id
),
savings AS (
  SELECT p.*,
         CASE
           WHEN p.oversized IS NULL THEN NULL
           WHEN NOT p.oversized THEN 0
           WHEN p.job_list_cost_usd IS NULL THEN NULL
           WHEN p.worker_cpu_p90_pct < 30 / 2.0
            AND p.worker_mem_peak_pct < 40 / 2.0 THEN p.job_list_cost_usd * 0.5
           ELSE p.job_list_cost_usd * 0.25
         END AS est_saving_usd
  FROM priced p
)
SELECT sv.workspace_id,
       sv.job_id,
       jb.job_name,                                              -- NULL for submit/workflow runs
       sv.runs_with_telemetry,
       sv.single_node,
       sv.latest_cluster_id,
       lc2.cluster_source,
       lc2.worker_node_type,
       ROUND(nm.memory_mb / 1024.0, 1)                           AS worker_memory_gb,
       nm.core_count                                              AS worker_cores,
       sv.workers_configured_max,
       sv.worker_nodes_seen_max,
       ROUND(sv.worker_cpu_p90_pct, 1)                           AS worker_cpu_p90_pct,
       ROUND(sv.worker_mem_p90_pct, 1)                           AS worker_mem_p90_pct,
       ROUND(sv.worker_cpu_peak_pct, 1)                          AS worker_cpu_peak_pct,
       ROUND(sv.worker_mem_peak_pct, 1)                          AS worker_mem_peak_pct,
       ROUND(sv.worker_swap_peak_pct, 1)                         AS worker_swap_peak_pct,
       sv.oversized,
       ROUND(sv.job_list_cost_usd, 2)                            AS job_list_cost_usd,
       sv.price_basis,
       ROUND(sv.est_saving_usd, 2)                               AS est_saving_usd_list,
       CASE
         WHEN sv.oversized IS NOT TRUE THEN NULL
         WHEN sv.worker_nodes_seen_max IS NOT NULL AND sv.workers_configured_max IS NOT NULL
          AND sv.worker_nodes_seen_max < sv.workers_configured_max THEN 'fewer workers / lower autoscale max'
         ELSE 'one node size down'
       END                                                        AS suggested_action,
       CASE
         WHEN sv.oversized IS NOT NULL             THEN NULL
         WHEN sv.runs_executed = 0                 THEN 'task_not_executed'
         WHEN sv.runs_with_cluster = 0              THEN 'no_cluster_recorded'
         WHEN sv.runs_with_telemetry IS NULL
           OR sv.runs_with_telemetry = 0            THEN 'no_node_timeline_rows'
         ELSE 'too_few_runs'
       END                                                        AS not_assessed_reason,
       CASE
         WHEN sv.oversized IS NULL                       THEN 'NOT_ASSESSED'
         WHEN COALESCE(sv.est_saving_usd, 0) >= 200 THEN 'CRITICAL'
         WHEN COALESCE(sv.est_saving_usd, 0) >= 25 THEN 'WARN'
         ELSE 'OK'
       END                                                        AS status
FROM savings sv
LEFT JOIN latest_jobs jb
  ON  jb.workspace_id = sv.workspace_id AND jb.job_id = sv.job_id
LEFT JOIN latest_clusters lc2
  ON  lc2.workspace_id = sv.workspace_id AND lc2.cluster_id = sv.latest_cluster_id
LEFT JOIN node_mem nm
  ON  nm.node_type = lc2.worker_node_type
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         est_saving_usd_list DESC NULLS LAST,
         workspace_id, job_id
LIMIT 100000
) q
"""
CHECKS["lakeflow_pipeline_cost"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_pipeline_cost.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_pipeline_cost.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH pipe_dbus AS (
  -- DBUs billed to each pipeline over the window. Net across ALL record_types; DBU only.
  -- Split maintenance DBUs out via dlt_maintenance_id so they are reported separately.
  SELECT workspace_id,
         usage_metadata.dlt_pipeline_id AS pipeline_id,
         SUM(usage_quantity) AS net_pipeline_dbus,
         SUM(CASE WHEN usage_metadata.dlt_maintenance_id IS NOT NULL
                  THEN usage_quantity ELSE 0 END) AS net_maintenance_dbus
  FROM `system`.`billing`.`usage`
  WHERE usage_date >= dateadd(day, -30, current_date())
    AND usage_date < current_date()
    AND upper(usage_unit) = 'DBU'
    AND usage_metadata.dlt_pipeline_id IS NOT NULL
  GROUP BY workspace_id, usage_metadata.dlt_pipeline_id
),
pipe_list AS (
  -- Effective-list-price estimate (DEC-66.1): SUM(usage_quantity * list_rate) per pipeline.
  -- Never a negotiated or billed dollar - no negotiated-rate source exists in system.billing.
  SELECT u.workspace_id,
         u.usage_metadata.dlt_pipeline_id AS pipeline_id,
         MAX(lp.currency_code) AS currency_code,
         SUM(u.usage_quantity * lp.list_rate) AS net_list_cost,
         SUM(CASE WHEN u.usage_metadata.dlt_maintenance_id IS NOT NULL
                  THEN u.usage_quantity * lp.list_rate ELSE 0 END) AS net_maintenance_list_cost,
         CASE
           WHEN SUM(CASE WHEN lp.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                         THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN lp.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END AS price_basis
  FROM `system`.`billing`.`usage` u
  LEFT JOIN (
    SELECT sku_name, cloud, currency_code, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective (post-promotion) list price - DEC-66.1
    FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
  ) lp
    ON u.sku_name = lp.sku_name
   AND u.cloud    = lp.cloud
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_date >= dateadd(day, -30, current_date())
    AND u.usage_date < current_date()
    AND upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.dlt_pipeline_id IS NOT NULL
  GROUP BY u.workspace_id, u.usage_metadata.dlt_pipeline_id
),
pipe_meta AS (
  -- SCD2 latest row per (workspace_id, pipeline_id): names-not-ids + type + continuous flag.
  SELECT workspace_id, pipeline_id, name AS pipeline_name, pipeline_type,
         settings.continuous AS is_continuous
  FROM `system`.`lakeflow`.`pipelines`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC
  ) = 1
),
pipe_updates_any AS (
  -- every update row seen in the window, ended or not -- tells "no update activity at all" apart
  -- from "an update is running but has not ended yet" before pipe_updates (end rows only) is read.
  SELECT workspace_id, pipeline_id, COUNT(*) AS update_rows_any
  FROM `system`.`lakeflow`.`pipeline_update_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
  GROUP BY workspace_id, pipeline_id
),
pipe_updates AS (
  -- Over-refresh signal (distinct updates + total active seconds) and the delivered-vs-not split,
  -- both end-row-only (result_state IS NOT NULL).
  SELECT workspace_id, pipeline_id,
         COUNT(*)                                                             AS update_end_rows,
         COUNT(DISTINCT update_id)                                            AS updates,
         SUM(CASE WHEN result_state = 'COMPLETED' THEN 1 ELSE 0 END)          AS updates_completed,
         SUM(CASE WHEN result_state IN ('FAILED', 'CANCELED') THEN 1 ELSE 0 END) AS updates_failed_or_canceled
  FROM `system`.`lakeflow`.`pipeline_update_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())
    AND result_state IS NOT NULL   -- update end row only
  GROUP BY workspace_id, pipeline_id
),
pipe_active AS (
  -- every hourly slice counts: an update over an hour writes one row per hour, the result on the last
  SELECT workspace_id, pipeline_id,
         SUM(unix_timestamp(period_end_time) - unix_timestamp(period_start_time)) AS active_seconds_total
  FROM `system`.`lakeflow`.`pipeline_update_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())
  GROUP BY workspace_id, pipeline_id
),
ws_timeline AS (
  -- workspaces with any update row: elsewhere (another region, or none exported) nothing can be judged
  SELECT DISTINCT workspace_id
  FROM `system`.`lakeflow`.`pipeline_update_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
),
combined AS (
  SELECT d.workspace_id, d.pipeline_id,
         d.net_pipeline_dbus, d.net_maintenance_dbus,
         l.currency_code, l.net_list_cost, l.net_maintenance_list_cost,
         COALESCE(l.price_basis, 'priced')          AS price_basis,
         m.pipeline_name, m.pipeline_type, COALESCE(m.is_continuous, FALSE) AS is_continuous,
         COALESCE(up.updates, 0)                    AS updates,
         COALESCE(up.update_end_rows, 0)            AS update_end_rows,
         COALESCE(ua.update_rows_any, 0)            AS update_rows_any,
         COALESCE(up.updates_completed, 0)          AS updates_completed,
         COALESCE(up.updates_failed_or_canceled, 0) AS updates_failed_or_canceled,
         COALESCE(pa.active_seconds_total, 0)       AS active_seconds_total,
         (wt.workspace_id IS NULL)                  AS no_timeline_rows,
         -- in_flight_only: the window saw update activity but no update has ended yet -- cannot yet
         -- tell "delivering" from "nothing delivered".
         (COALESCE(ua.update_rows_any, 0) > 0 AND COALESCE(up.update_end_rows, 0) = 0) AS in_flight_only,
         -- nothing_delivered: either no update activity in the window at all, or every ended update
         -- failed or was canceled (COMPLETED count of 0 alone is not enough -- an update still in
         -- flight also has 0 completed, which is why in_flight_only is checked first).
         (
           (COALESCE(up.update_end_rows, 0) = 0 AND COALESCE(ua.update_rows_any, 0) = 0)
           OR (COALESCE(up.update_end_rows, 0) > 0
               AND COALESCE(up.updates_failed_or_canceled, 0) = up.update_end_rows)
         ) AS nothing_delivered
  FROM pipe_dbus d
  LEFT JOIN pipe_list l         ON d.workspace_id = l.workspace_id AND d.pipeline_id = l.pipeline_id
  LEFT JOIN pipe_meta m         ON d.workspace_id = m.workspace_id AND d.pipeline_id = m.pipeline_id
  LEFT JOIN pipe_updates up     ON d.workspace_id = up.workspace_id AND d.pipeline_id = up.pipeline_id
  LEFT JOIN pipe_updates_any ua ON d.workspace_id = ua.workspace_id AND d.pipeline_id = ua.pipeline_id
  LEFT JOIN pipe_active pa      ON d.workspace_id = pa.workspace_id AND d.pipeline_id = pa.pipeline_id
  LEFT JOIN ws_timeline wt      ON d.workspace_id = wt.workspace_id
)
SELECT c.workspace_id,
       c.pipeline_id,
       c.pipeline_name AS pipeline_name,
       c.pipeline_type, c.is_continuous,
       c.net_pipeline_dbus, c.net_maintenance_dbus,
       c.currency_code, c.net_list_cost, c.net_maintenance_list_cost,
       c.price_basis,
       c.updates, c.update_end_rows, c.update_rows_any,
       c.updates_completed, c.updates_failed_or_canceled,
       c.active_seconds_total,
       c.in_flight_only, c.nothing_delivered,
       (c.net_pipeline_dbus >= 500) AS is_high_spend,
       CASE
         WHEN c.is_continuous THEN NULL
         WHEN c.in_flight_only THEN NULL
         WHEN c.no_timeline_rows THEN NULL
         WHEN c.nothing_delivered THEN ROUND(c.net_list_cost - COALESCE(c.net_maintenance_list_cost, 0), 2)
         ELSE 0
       END AS est_wasted_usd_list,
       -- status: CRITICAL/WARN now require the nothing-delivered signal at 2000, not
       -- size alone (field heuristic; 500 / 2000).
       CASE
         WHEN c.is_continuous THEN 'NOT_ASSESSED'    -- continuous pipeline: no discrete completed-update signal
         WHEN c.in_flight_only THEN 'NOT_ASSESSED'   -- update still running at window end: too early to call
         WHEN c.no_timeline_rows THEN 'NOT_ASSESSED' -- no update rows in its workspace: cannot tell delivered
         WHEN c.net_pipeline_dbus >= 2000 AND c.nothing_delivered THEN 'CRITICAL'
         WHEN c.net_pipeline_dbus >= 2000 THEN 'WARN'
         WHEN c.net_pipeline_dbus >= 500 AND c.nothing_delivered THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN c.is_continuous THEN 'continuous_pipeline'
         WHEN c.in_flight_only THEN 'update_in_flight'
         WHEN c.no_timeline_rows THEN 'no_timeline_rows'
       END AS not_assessed_reason
FROM combined c
WHERE c.net_pipeline_dbus > 0
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         c.net_pipeline_dbus DESC
) q
"""
CHECKS["lakeflow_pipeline_update_failures_retries"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_pipeline_update_failures_retries.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_pipeline_update_failures_retries.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH end_rows AS (
  SELECT workspace_id, pipeline_id, update_id, request_id, update_type,
         trigger_type, result_state, period_start_time, period_end_time
  FROM `system`.`lakeflow`.`pipeline_update_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())
    AND result_state IS NOT NULL          -- end row only for updates >1h
)
SELECT e.workspace_id, e.pipeline_id, e.update_type, e.trigger_type, e.result_state,
       COUNT(DISTINCT e.update_id) AS updates,
       SUM(CASE WHEN e.result_state = 'FAILED'             THEN 1 ELSE 0 END) AS failed_update_rows,
       SUM(CASE WHEN e.trigger_type = 'RETRY_ON_FAILURE'   THEN 1 ELSE 0 END) AS retry_triggered_rows,
       -- status: worst-first band on failed update rows per group (field heuristic; 3 / 10).
       CASE
         WHEN SUM(CASE WHEN e.result_state = 'FAILED' THEN 1 ELSE 0 END) >= 10 THEN 'CRITICAL'
         WHEN SUM(CASE WHEN e.result_state = 'FAILED' THEN 1 ELSE 0 END) >= 3 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM end_rows e
GROUP BY e.workspace_id, e.pipeline_id, e.update_type, e.trigger_type, e.result_state
ORDER BY failed_update_rows DESC
) q
"""
CHECKS["lakeflow_pipelines_inventory_tier"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_pipelines_inventory_tier.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_pipelines_inventory_tier.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
WITH latest_pipelines AS (
  SELECT workspace_id, pipeline_id, name AS pipeline_name, pipeline_type, settings, delete_time,
         settings.serverless  AS setting_serverless,
         settings.development AS setting_development,
         settings.continuous  AS setting_continuous,
         settings.photon      AS setting_photon,
         settings.edition     AS setting_edition,
         settings.channel     AS setting_channel
  FROM `system`.`lakeflow`.`pipelines`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC) = 1
)
SELECT p.workspace_id, w.workspace_name, p.pipeline_id, p.pipeline_name, p.pipeline_type,
       p.setting_serverless, p.setting_development, p.setting_continuous, p.setting_photon,
       p.setting_edition, p.setting_channel
FROM latest_pipelines p
LEFT JOIN `system`.`access`.`workspaces_latest` w ON w.workspace_id = p.workspace_id
WHERE p.delete_time IS NULL
ORDER BY p.workspace_id, p.pipeline_type, p.pipeline_name
) q
"""
CHECKS["query_provenance_by_source"] = r"""
-- generated from dbt/models/databricks_direct/performance/d_query_provenance_by_source.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/performance/query_provenance_by_source.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT workspace_id,
       compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
       CASE WHEN executed_by IS NULL              THEN 'unknown'
            WHEN executed_by LIKE '%@%'           THEN 'user'
            ELSE 'service_principal' END AS identity_type,
       executed_by AS executed_by,
       -- same precedence as query_top_by_cost and query_slices, so a statement with several origins
       -- (an alert run by a job) gets one label on every page: what ran it first, the editor last
       CASE WHEN query_source.job_info.job_id     IS NOT NULL THEN 'job'
            WHEN query_source.pipeline_info.pipeline_id IS NOT NULL THEN 'pipeline'
            WHEN query_source.dashboard_id        IS NOT NULL THEN 'dashboard'
            WHEN query_source.legacy_dashboard_id IS NOT NULL THEN 'legacy_dashboard'
            WHEN query_source.genie_space_id      IS NOT NULL THEN 'genie'
            WHEN query_source.alert_id            IS NOT NULL THEN 'alert'
            WHEN query_source.notebook_id         IS NOT NULL THEN 'notebook'
            WHEN query_source.sql_query_id        IS NOT NULL THEN 'sql_editor'
            ELSE 'other' END AS source_kind,
       query_source.job_info.job_id AS job_id,
       query_source.dashboard_id    AS dashboard_id,
       query_source.notebook_id     AS notebook_id,
       COUNT(*) AS query_count,
       SUM(execution_duration_ms) AS execution_duration_ms_sum,
       SUM(total_duration_ms)     AS total_duration_ms_sum,
       SUM(read_bytes)            AS read_bytes_sum
FROM `system`.`query`.`history`
WHERE start_time >= current_date() - INTERVAL 30 DAYS
  AND start_time < current_date()
GROUP BY workspace_id, compute.type, compute.warehouse_id, source_kind,
         CASE WHEN executed_by IS NULL              THEN 'unknown'
              WHEN executed_by LIKE '%@%'           THEN 'user'
              ELSE 'service_principal' END,
         executed_by,
         query_source.job_info.job_id, query_source.dashboard_id, query_source.notebook_id
ORDER BY workspace_id, compute_type, source_kind
) q
"""
CHECKS["audit_self_cost"] = r"""
-- generated from dbt/models/databricks_direct/performance/d_audit_self_cost.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/performance/audit_self_cost.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT
    workspace_id,
    statement_type,
    COUNT(*)                                   AS query_count,
    SUM(total_duration_ms) / 1000.0            AS total_duration_secs,
    SUM(COALESCE(total_task_duration_ms, 0)) / 1000.0 AS total_task_secs,
    COUNT(DISTINCT executed_by)                AS distinct_principals,
    MIN(start_time)                            AS first_query_time,
    MAX(start_time)                            AS last_query_time
FROM `system`.`query`.`history`
WHERE start_time >= dateadd(day, -30, current_date())
  -- the export's own query tag (fixed key audit_app) or client-application name, as
  -- cost_audit_self_usage matches them; statement text would also match any query naming such a schema
  AND (client_application ILIKE '%Crosshire%' OR array_contains(map_keys(query_tags), 'audit_app'))
GROUP BY workspace_id, statement_type
ORDER BY workspace_id, statement_type
) q
"""
CHECKS["query_costly_statements_grouped"] = r"""
-- generated from dbt/models/databricks_direct/performance/d_query_costly_statements_grouped.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/performance/query_costly_statements_grouped.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH devalued AS (
  SELECT
    statement_id,
    workspace_id,
    statement_type,
    compute.warehouse_id AS warehouse_id,
    start_time,
    execution_duration_ms,
    (statement_text IS NULL OR statement_text = '<REDACTED>') AS is_redacted,
    regexp_replace(
      regexp_replace(statement_text, '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}', '<email>'),
      concat(chr(39), '[^', chr(39), ']*', chr(39)), '?'
    ) AS statement_text
  FROM `system`.`query`.`history`
  WHERE start_time >= dateadd(DAY, -30, current_date())
    AND start_time <  current_date()
    AND execution_status = 'FINISHED'
    AND from_result_cache = false
    AND execution_duration_ms > 0
),
fp AS (
  SELECT
    -- a redacted statement has no text to shape - key it by its own statement_id so it is never
    -- pooled with another redacted statement (they'd otherwise all sha2 to the same placeholder text).
    -- numbers are masked too, so WHERE batch_id = 1001 and = 1002 share one shape
    CASE WHEN is_redacted THEN concat('statement:', statement_id)
         ELSE sha2(
           regexp_replace(
             regexp_replace(statement_text, '(^|[^A-Za-z0-9_.])[0-9]+([.][0-9]+)?', '$1?'),
             '[?]( *, *[?])+', '?'),
           256)
    END AS statement_fingerprint,
    *
  FROM devalued
)
SELECT
  workspace_id,
  statement_fingerprint,
  MAX(is_redacted)                           AS is_redacted,
  MAX(statement_type)                        AS statement_type,
  MIN(statement_text)                        AS sample_statement_text,
  COUNT(*)                                   AS runs,
  SUM(execution_duration_ms)                 AS total_exec_ms,
  CAST(AVG(execution_duration_ms) AS BIGINT) AS avg_exec_ms,
  MAX(execution_duration_ms)                 AS max_exec_ms,
  COUNT(DISTINCT warehouse_id)               AS distinct_warehouses,
  MIN(start_time)                            AS first_seen,
  MAX(start_time)                            AS last_seen,
  -- status: worst-first band on cumulative execution time for the shape (field heuristic).
  CASE
    WHEN SUM(execution_duration_ms) >= 5 * 3600 * 1000 THEN 'CRITICAL'
    WHEN SUM(execution_duration_ms) >= 1 * 3600 * 1000 THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM fp
GROUP BY workspace_id, statement_fingerprint
ORDER BY total_exec_ms DESC
LIMIT 500
) q
"""
CHECKS["query_costly_statements"] = r"""
-- generated from dbt/models/databricks_direct/performance/d_query_costly_statements.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/performance/query_costly_statements.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH devalued AS (
  SELECT
    workspace_id,
    statement_id,
    statement_type,
    -- executed_by: DEC-66.3 identity format (executed_by_user_id, or when NULL the first 8 hex chars of
    -- sha2(lower(trim(executed_by)), 256), + ' ' + first-2 + '***'; NULL/'__REDACTED__'/GUID kept as-is).
    executed_by                                    AS executed_by,
    compute.warehouse_id                   AS warehouse_id,
    compute.type                           AS compute_type,
    start_time,
    execution_duration_ms,
    waiting_for_compute_duration_ms,
    total_task_duration_ms,
    read_bytes,
    read_files,
    pruned_files,
    read_partitions,
    read_rows,
    produced_rows,
    spilled_local_bytes,
    shuffle_read_bytes,
    from_result_cache,
    -- statement_text de-valued: strip emails, then replace every single-quoted literal with '?'
    -- (chr(39) is the single quote). Keeps the query shape, removes literal data values.
    regexp_replace(
      regexp_replace(statement_text, '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}', '<email>'),
      concat(chr(39), '[^', chr(39), ']*', chr(39)), '?'
    )                                      AS statement_text
  FROM `system`.`query`.`history`
  WHERE start_time >= dateadd(DAY, -30, current_date())
    AND start_time <  current_date()
    AND execution_status = 'FINISHED'
    AND from_result_cache = false
    AND execution_duration_ms > 0
),
base AS (
  -- numbers are masked too, so WHERE batch_id = 1001 and = 1002 share one shape
  SELECT d.*, sha2(
           regexp_replace(
             regexp_replace(d.statement_text, '(^|[^A-Za-z0-9_.])[0-9]+([.][0-9]+)?', '$1?'),
             '[?]( *, *[?])+', '?'),
           256) AS statement_fingerprint
  FROM devalued d
),
wh_total AS (
  -- every statement's execution time on the warehouse, failed and canceled ones included
  SELECT compute.warehouse_id AS warehouse_id, SUM(execution_duration_ms) AS wh_exec_ms
  FROM `system`.`query`.`history`
  WHERE start_time >= dateadd(DAY, -30, current_date())
    AND start_time <  current_date()
    AND from_result_cache = false
    AND execution_duration_ms > 0
  GROUP BY compute.warehouse_id
)
SELECT
  b.*,
  -- per-warehouse framing: this statement's share of its warehouse's total execution time.
  b.execution_duration_ms / NULLIF(t.wh_exec_ms, 0) AS pct_of_warehouse_exec_ms,
  ROW_NUMBER() OVER (PARTITION BY b.warehouse_id ORDER BY b.execution_duration_ms DESC)                 AS exec_rank_in_warehouse,
  -- status: worst-first band on within-warehouse execution share (field heuristic).
  CASE
    WHEN b.execution_duration_ms / NULLIF(t.wh_exec_ms, 0) >= 0.25 THEN 'CRITICAL'
    WHEN b.execution_duration_ms / NULLIF(t.wh_exec_ms, 0) >= 0.1 THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM base b
LEFT JOIN wh_total t ON t.warehouse_id IS NOT DISTINCT FROM b.warehouse_id
ORDER BY b.execution_duration_ms DESC
LIMIT 1000
) q
"""
CHECKS["query_per_query_estimate_lane"] = r"""
-- generated from dbt/models/databricks_direct/performance/d_query_per_query_estimate_lane.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/performance/query_per_query_estimate_lane.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT date_trunc('HOUR', start_time) AS usage_hour, workspace_id,
       compute.warehouse_id AS warehouse_id, compute.type AS compute_type,
       executed_by AS executed_by,
       statement_id, statement_type,
       execution_duration_ms, waiting_for_compute_duration_ms, total_task_duration_ms,
       read_bytes, total_duration_ms
FROM `system`.`query`.`history`
WHERE start_time >= current_date() - INTERVAL 30 DAYS
  AND start_time < current_date()
  AND execution_status = 'FINISHED'
  AND from_result_cache = false
  AND execution_duration_ms > 0
  AND compute.warehouse_id IS NOT NULL
ORDER BY usage_hour, warehouse_id, statement_id
) q
"""
CHECKS["query_local_spillage"] = r"""
-- generated from dbt/models/databricks_direct/performance/d_query_local_spillage.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/performance/query_local_spillage.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT date(start_time) AS day, workspace_id, compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
       executed_by AS executed_by,
       COUNT(*) AS spilling_query_count,
       SUM(spilled_local_bytes) AS spilled_local_bytes_sum,
       MAX(spilled_local_bytes) AS spilled_local_bytes_max,
       SUM(total_duration_ms)     AS total_duration_ms_sum,
       SUM(execution_duration_ms) AS execution_duration_ms_sum,
       SUM(shuffle_read_bytes)    AS shuffle_read_bytes_sum,
       -- the warehouse's spill that day across every user: the bands are per warehouse and day
       SUM(SUM(spilled_local_bytes)) OVER (
         PARTITION BY date(start_time), workspace_id, compute.warehouse_id) AS warehouse_day_spilled_bytes,
       -- status: worst-first band on the warehouse's daily local spill (field heuristic; 1 / 10).
       CASE
         WHEN SUM(SUM(spilled_local_bytes)) OVER (
                PARTITION BY date(start_time), workspace_id, compute.warehouse_id) >= 10 * 1e9 THEN 'CRITICAL'
         WHEN SUM(SUM(spilled_local_bytes)) OVER (
                PARTITION BY date(start_time), workspace_id, compute.warehouse_id) >= 1 * 1e9 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`query`.`history`
WHERE start_time >= current_date() - INTERVAL 30 DAYS
  AND start_time < current_date()
  AND spilled_local_bytes > 0
GROUP BY date(start_time), workspace_id, compute.type, compute.warehouse_id, executed_by
ORDER BY spilled_local_bytes_sum DESC
) q
"""
CHECKS["query_shuffle_write_amplification"] = r"""
-- generated from dbt/models/databricks_direct/performance/d_query_shuffle_write_amplification.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/performance/query_shuffle_write_amplification.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT date(start_time) AS day, workspace_id, compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
       executed_by AS executed_by,
       statement_type,
       COUNT(*) AS query_count,
       SUM(shuffle_read_bytes) AS shuffle_read_bytes_sum,
       SUM(written_bytes)      AS written_bytes_sum,
       SUM(written_rows)       AS written_rows_sum,
       SUM(written_files)      AS written_files_sum,
       SUM(read_bytes)         AS read_bytes_sum,
       -- status: worst-first band on shuffle volume OR small-file write amplification (field heuristic;
       -- 50 / 200 / 32 / 8); a few small
       -- files are normal, so the file-size rule waits for 100 files.
       CASE
         WHEN SUM(shuffle_read_bytes) >= 200 * 1e9
           OR (SUM(written_files) >= 100 AND SUM(written_bytes) / SUM(written_files) < 8 * 1e6) THEN 'CRITICAL'
         WHEN SUM(shuffle_read_bytes) >= 50 * 1e9
           OR (SUM(written_files) >= 100 AND SUM(written_bytes) / SUM(written_files) < 32 * 1e6) THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`query`.`history`
WHERE start_time >= current_date() - INTERVAL 30 DAYS
  AND start_time < current_date()
  AND (shuffle_read_bytes > 0 OR written_bytes > 0)
GROUP BY date(start_time), workspace_id, compute.type, compute.warehouse_id, executed_by, statement_type
ORDER BY shuffle_read_bytes_sum DESC
) q
"""
CHECKS["query_pruning_effectiveness"] = r"""
-- generated from dbt/models/databricks_direct/performance/d_query_pruning_effectiveness.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/performance/query_pruning_effectiveness.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH latest_wh AS (
  SELECT warehouse_id, warehouse_name
  FROM `system`.`compute`.`warehouses`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
),
daily AS (
  SELECT date(start_time) AS day, workspace_id, compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
         executed_by AS executed_by,
         statement_type,
         COUNT(*) AS query_count,
         SUM(pruned_files)    AS pruned_files_sum,
         SUM(read_files)      AS read_files_sum,
         SUM(read_partitions) AS read_partitions_sum,
         SUM(read_bytes)      AS read_bytes_sum,
         SUM(read_rows)       AS read_rows_sum
  FROM `system`.`query`.`history`
  WHERE start_time >= current_date() - INTERVAL 30 DAYS
    AND start_time < current_date()
    AND (pruned_files > 0 OR read_files > 0)
  GROUP BY date(start_time), workspace_id, compute.type, compute.warehouse_id, executed_by, statement_type
),
scored AS (
  SELECT d.*,
         d.pruned_files_sum * 1.0 / NULLIF(d.pruned_files_sum + d.read_files_sum, 0) AS pruning_ratio,
         -- day_status: the raw per-day band before the recurrence cap (see enriched/status below).
         CASE
           WHEN d.pruned_files_sum + d.read_files_sum = 0 THEN 'NOT_ASSESSED'
           WHEN d.read_bytes_sum < 1073741824 THEN 'NOT_ASSESSED'
           WHEN d.pruned_files_sum * 1.0 / NULLIF(d.pruned_files_sum + d.read_files_sum, 0) < 0.2 THEN 'CRITICAL'
           WHEN d.pruned_files_sum * 1.0 / NULLIF(d.pruned_files_sum + d.read_files_sum, 0) < 0.5 THEN 'WARN'
           ELSE 'OK'
         END AS day_status
  FROM daily d
),
enriched AS (
  SELECT s.*,
         COUNT(*) OVER (PARTITION BY s.workspace_id, s.compute_type, s.warehouse_id, s.executed_by, s.statement_type) AS days_seen,
         SUM(CASE WHEN s.day_status IN ('WARN', 'CRITICAL') THEN 1 ELSE 0 END)
           OVER (PARTITION BY s.workspace_id, s.compute_type, s.warehouse_id, s.executed_by, s.statement_type) AS days_flagged
  FROM scored s
)
SELECT e.day, e.workspace_id, e.compute_type, e.warehouse_id, lw.warehouse_name,
       e.executed_by, e.statement_type,
       e.query_count, e.pruned_files_sum, e.read_files_sum, e.read_partitions_sum,
       e.read_bytes_sum, e.read_rows_sum,
       ROUND(e.pruning_ratio, 3) AS pruning_ratio,
       e.days_seen, e.days_flagged,
       -- status: a day below 1073741824 is already NOT_ASSESSED (day_status), and a CRITICAL day
       -- that is a one-off (days_flagged below 2) is capped to WARN.
       CASE
         WHEN e.day_status = 'CRITICAL' AND e.days_flagged < 2 THEN 'WARN'
         ELSE e.day_status
       END AS status
FROM enriched e
LEFT JOIN latest_wh lw ON lw.warehouse_id = e.warehouse_id
ORDER BY e.pruning_ratio ASC NULLS LAST
) q
"""
CHECKS["query_cache_coldstart"] = r"""
-- generated from dbt/models/databricks_direct/performance/d_query_cache_coldstart.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/performance/query_cache_coldstart.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH latest_wh AS (
  SELECT warehouse_id, warehouse_name
  FROM `system`.`compute`.`warehouses`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
),
agg AS (
  SELECT date(start_time) AS day, workspace_id, compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
         COUNT(*) AS query_count,
         SUM(CASE WHEN from_result_cache THEN 1 ELSE 0 END) AS result_cache_hit_count,
         -- rows this average is actually over: scanned real bytes and were not themselves a cache hit.
         SUM(CASE WHEN read_bytes > 0 AND NOT COALESCE(from_result_cache, false) THEN 1 ELSE 0 END) AS scanned_query_count,
         AVG(read_io_cache_percent) AS read_io_cache_percent_avg,
         SUM(CASE WHEN read_bytes > 0 AND NOT COALESCE(from_result_cache, false)
                  THEN read_io_cache_percent * read_bytes END) AS weighted_num,
         -- a NULL cache percent is unknown, not 0% cached: it stays out of both sides
         SUM(CASE WHEN read_bytes > 0 AND NOT COALESCE(from_result_cache, false)
                   AND read_io_cache_percent IS NOT NULL THEN read_bytes END) AS weighted_den,
         SUM(CASE WHEN read_bytes > 0 AND NOT COALESCE(from_result_cache, false)
                   AND read_io_cache_percent < 50 THEN 1 ELSE 0 END) AS low_io_cache_count,
         SUM(waiting_for_compute_duration_ms) AS waiting_for_compute_ms_sum,
         SUM(compilation_duration_ms)         AS compilation_duration_ms_sum
  FROM `system`.`query`.`history`
  WHERE start_time >= current_date() - INTERVAL 30 DAYS
    AND start_time < current_date()
  GROUP BY date(start_time), workspace_id, compute.type, compute.warehouse_id
)
SELECT a.day, a.workspace_id, a.compute_type, a.warehouse_id, lw.warehouse_name,
       a.query_count, a.result_cache_hit_count, a.scanned_query_count,
       ROUND(a.read_io_cache_percent_avg, 1)                AS read_io_cache_percent_avg,
       ROUND(a.weighted_num / NULLIF(a.weighted_den, 0), 1) AS read_io_cache_percent_weighted_avg,
       a.low_io_cache_count, a.waiting_for_compute_ms_sum, a.compilation_duration_ms_sum,
       -- status: worst-first band on the read_bytes-weighted IO-cache hit rate (field heuristic;
       -- 50 / 20). NOT_ASSESSED when nothing in the group scanned
       -- data - the old plain average could dilute such a group into a false OK.
       CASE
         WHEN a.scanned_query_count = 0 THEN 'NOT_ASSESSED'
         WHEN a.weighted_num / NULLIF(a.weighted_den, 0) < 20 THEN 'CRITICAL'
         WHEN a.weighted_num / NULLIF(a.weighted_den, 0) < 50 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM agg a
LEFT JOIN latest_wh lw ON lw.warehouse_id = a.warehouse_id
ORDER BY read_io_cache_percent_weighted_avg ASC NULLS LAST
) q
"""
CHECKS["query_failed_statements_grouped"] = r"""
-- generated from dbt/models/databricks_direct/performance/d_query_failed_statements_grouped.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/performance/query_failed_statements_grouped.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH stmts AS (
  SELECT q.workspace_id, q.compute.warehouse_id AS warehouse_id, q.statement_id,
         q.execution_status, q.error_message, q.query_source, q.start_time
  FROM `system`.`query`.`history` q
  WHERE q.start_time >= current_date() - INTERVAL 30 DAYS
    AND q.start_time < current_date()
    AND q.execution_status IN ('FAILED', 'CANCELED')
),
classed AS (
  SELECT s.*,
         -- leading [ERROR_CLASS] token: pattern built via chr(92) (backslash), not a literal
         -- '[[]...[]]' char-class - Java regex (Databricks) reads a literal '[' inside a class as
         -- opening a nested class and throws "Unclosed character class".
         CASE
           WHEN s.error_message IS NULL OR s.error_message = '' THEN 'unclassified'
           WHEN regexp_extract(s.error_message, concat('^', chr(92), '[([A-Za-z0-9_.]+)', chr(92), ']'), 1) = '' THEN 'unclassified'
           ELSE regexp_extract(s.error_message, concat('^', chr(92), '[([A-Za-z0-9_.]+)', chr(92), ']'), 1)
         END AS error_class,
         CASE
           WHEN s.query_source.sql_query_id IS NOT NULL              THEN 'saved query'
           WHEN s.query_source.dashboard_id IS NOT NULL              THEN 'dashboard'
           WHEN s.query_source.legacy_dashboard_id IS NOT NULL       THEN 'dashboard'
           WHEN s.query_source.genie_space_id IS NOT NULL            THEN 'Genie space'
           WHEN s.query_source.alert_id IS NOT NULL                  THEN 'alert'
           WHEN s.query_source.job_info.job_id IS NOT NULL           THEN 'job'
           WHEN s.query_source.notebook_id IS NOT NULL               THEN 'notebook'
           WHEN s.query_source.pipeline_info.pipeline_id IS NOT NULL THEN 'pipeline'
         END AS source_kind,
         CASE
           WHEN s.query_source.sql_query_id IS NOT NULL              THEN s.query_source.sql_query_id
           WHEN s.query_source.dashboard_id IS NOT NULL              THEN s.query_source.dashboard_id
           WHEN s.query_source.legacy_dashboard_id IS NOT NULL       THEN s.query_source.legacy_dashboard_id
           WHEN s.query_source.genie_space_id IS NOT NULL            THEN s.query_source.genie_space_id
           WHEN s.query_source.alert_id IS NOT NULL                  THEN s.query_source.alert_id
           WHEN s.query_source.job_info.job_id IS NOT NULL           THEN s.query_source.job_info.job_id
           WHEN s.query_source.notebook_id IS NOT NULL               THEN s.query_source.notebook_id
           WHEN s.query_source.pipeline_info.pipeline_id IS NOT NULL THEN s.query_source.pipeline_info.pipeline_id
         END AS source_id
  FROM stmts s
),
sample AS (
  -- one representative statement per group: its most recent
  SELECT workspace_id, warehouse_id, execution_status, error_class, source_kind, source_id,
         statement_id AS sample_statement_id,
         regexp_replace(
           regexp_replace(error_message, '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}', '<email>'),
           concat(chr(39), '[^', chr(39), ']*', chr(39)), '?'
         ) AS error_message_sample
  FROM classed
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id, execution_status, error_class,
                                          source_kind, source_id
                             ORDER BY start_time DESC, statement_id DESC) = 1
)
SELECT c.workspace_id, c.warehouse_id, c.execution_status, c.error_class, c.source_kind, c.source_id,
       COUNT(*) AS statements,
       MIN(c.start_time) AS first_seen,
       MAX(c.start_time) AS last_seen,
       sm.sample_statement_id,
       sm.error_message_sample,
       CASE
         WHEN c.execution_status = 'CANCELED'  THEN 'OK'
         WHEN COUNT(*) >= 50     THEN 'CRITICAL'
         WHEN COUNT(*) >= 10     THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM classed c
JOIN sample sm
  ON  sm.workspace_id = c.workspace_id AND sm.warehouse_id IS NOT DISTINCT FROM c.warehouse_id
  AND sm.execution_status = c.execution_status AND sm.error_class = c.error_class
  AND sm.source_kind IS NOT DISTINCT FROM c.source_kind
  AND sm.source_id   IS NOT DISTINCT FROM c.source_id
GROUP BY c.workspace_id, c.warehouse_id, c.execution_status, c.error_class, c.source_kind, c.source_id,
         sm.sample_statement_id, sm.error_message_sample
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, statements DESC
) q
"""
CHECKS["query_warehouse_pressure"] = r"""
-- generated from dbt/models/databricks_direct/performance/d_query_warehouse_pressure.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/performance/query_warehouse_pressure.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH stmts AS (
  -- SQL-warehouse statements in the window; serverless and classic-pipeline statements carry no
  -- warehouse_id and are excluded. A NULL duration or byte count reads as 0 (query_queuing_waits).
  SELECT workspace_id,
         compute.warehouse_id                         AS warehouse_id,
         compute.type                                 AS compute_type,
         execution_status,
         date(start_time)                             AS stmt_day,
         COALESCE(spilled_local_bytes, 0)             AS spilled_local_bytes,
         COALESCE(execution_duration_ms, 0)           AS execution_ms,
         COALESCE(total_duration_ms, 0)               AS total_ms,
         COALESCE(read_bytes, 0)                      AS read_bytes,
         COALESCE(shuffle_read_bytes, 0)              AS shuffle_read_bytes,
         COALESCE(waiting_at_capacity_duration_ms, 0) AS waiting_at_capacity_ms,
         COALESCE(waiting_for_compute_duration_ms, 0) AS waiting_for_compute_ms
  FROM `system`.`query`.`history`
  WHERE start_time >= current_date() - INTERVAL 30 DAYS
    AND start_time < current_date()
    AND compute.warehouse_id IS NOT NULL
),
daily AS (
  -- local spill per warehouse per calendar day, for the worst-day volume band
  SELECT workspace_id, warehouse_id, stmt_day,
         SUM(spilled_local_bytes) AS spilled_day_bytes
  FROM stmts
  GROUP BY workspace_id, warehouse_id, stmt_day
),
worst_day AS (
  SELECT workspace_id, warehouse_id,
         MAX(spilled_day_bytes) AS spilled_local_bytes_max_day
  FROM daily
  GROUP BY workspace_id, warehouse_id
),
per_wh AS (
  SELECT workspace_id, warehouse_id,
         MIN(compute_type)                                                    AS compute_type,
         COUNT(*)                                                             AS query_count,
         SUM(CASE WHEN execution_status = 'FINISHED' THEN 1 ELSE 0 END)       AS finished_count,
         -- a spilling statement is one that reached 100 MB (10^6 bytes) of spill
         SUM(CASE WHEN spilled_local_bytes > 0
                   AND spilled_local_bytes >= 100 * 1e6
                  THEN 1 ELSE 0 END)                                         AS spilling_query_count,
         SUM(spilled_local_bytes)                                             AS spilled_local_bytes_sum,
         -- execution time of the spilling statements: the memory verdict is time-weighted
         SUM(CASE WHEN spilled_local_bytes > 0
                   AND spilled_local_bytes >= 100 * 1e6
                  THEN execution_ms ELSE 0 END)                              AS spill_exec_ms,
         SUM(execution_ms)                                                    AS execution_ms_sum,
         SUM(total_ms)                                                        AS total_duration_ms_sum,
         SUM(read_bytes)                                                      AS read_bytes_sum,
         SUM(shuffle_read_bytes)                                              AS shuffle_read_bytes_sum,
         SUM(CASE WHEN waiting_at_capacity_ms > 0 THEN 1 ELSE 0 END)          AS queued_at_capacity_count,
         SUM(waiting_at_capacity_ms)                                          AS waiting_at_capacity_ms_sum,
         SUM(CASE WHEN waiting_for_compute_ms > 0 THEN 1 ELSE 0 END)          AS waited_for_compute_count,
         SUM(waiting_for_compute_ms)                                          AS waiting_for_compute_ms_sum
  FROM stmts
  GROUP BY workspace_id, warehouse_id
),
latest_wh AS (
  -- system.compute.warehouses is SCD2: the newest row per warehouse, deleted warehouses KEPT
  -- (a warehouse deleted since still has statements in the window)
  SELECT warehouse_id, warehouse_type, warehouse_size, min_clusters, max_clusters,
         auto_stop_minutes,
         (delete_time IS NOT NULL) AS warehouse_deleted
  FROM `system`.`compute`.`warehouses`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY warehouse_id ORDER BY change_time DESC
  ) = 1
),
events AS (
  -- the most clusters the warehouse ran at once in the window, and how often it scaled
  SELECT warehouse_id,
         MAX(cluster_count)                                                          AS max_cluster_count_seen,
         SUM(CASE WHEN event_type IN ('SCALED_UP', 'SCALED_DOWN') THEN 1 ELSE 0 END) AS scaling_events
  FROM `system`.`compute`.`warehouse_events`
  WHERE event_time >= current_date() - INTERVAL 30 DAYS
    AND event_time < current_date()
  GROUP BY warehouse_id
),
measured AS (
  SELECT p.*,
         d.spilled_local_bytes_max_day,
         w.warehouse_type, w.warehouse_size, w.min_clusters, w.max_clusters, w.auto_stop_minutes,
         w.warehouse_deleted,
         e.max_cluster_count_seen, e.scaling_events,
         -- unrounded shares: every band below is judged on these, and they are rounded (1 dp)
         -- only in the final SELECT, so a true 9.96% never crosses a 10% threshold
         p.spill_exec_ms / NULLIF(p.execution_ms_sum, 0) * 100                   AS spill_time_raw,
         p.waiting_at_capacity_ms_sum / NULLIF(p.total_duration_ms_sum, 0) * 100 AS capacity_wait_raw,
         -- context only, never a verdict (query_cache_coldstart is the cold-start finding)
         p.waiting_for_compute_ms_sum / NULLIF(p.total_duration_ms_sum, 0) * 100 AS cold_start_wait_raw,
         p.spilling_query_count * 100.0 / p.query_count                         AS spill_query_raw,
         -- did the warehouse reach its maximum clusters? A warehouse with no autoscale range
         -- (min_clusters >= max_clusters) always runs at its maximum; otherwise the events decide,
         -- and NULL = not seen (no event, or no config)
         CASE WHEN w.max_clusters IS NOT NULL AND w.min_clusters >= w.max_clusters THEN TRUE
              WHEN e.max_cluster_count_seen IS NULL OR w.max_clusters IS NULL THEN NULL
              ELSE e.max_cluster_count_seen >= w.max_clusters END                        AS at_ceiling
  FROM per_wh p
  LEFT JOIN worst_day d
    ON  d.workspace_id = p.workspace_id
    AND d.warehouse_id = p.warehouse_id
  LEFT JOIN latest_wh w
    ON  w.warehouse_id = p.warehouse_id
  LEFT JOIN events e
    ON  e.warehouse_id = p.warehouse_id
),
banded AS (
  SELECT m.*,
         -- memory: the time-weighted spill share, or the worst single day's spill volume
         CASE
           WHEN m.spill_time_raw >= 25
             OR m.spilled_local_bytes_max_day >= 10 * 1e9 THEN 'CRITICAL'
           WHEN m.spill_time_raw >= 10
             OR m.spilled_local_bytes_max_day >= 1 * 1e9 THEN 'WARN'
           ELSE 'OK'
         END AS mem_band,
         -- concurrency: the share of statement time queued at capacity
         CASE
           WHEN m.capacity_wait_raw >= 20 THEN 'CRITICAL'
           WHEN m.capacity_wait_raw >= 5 THEN 'WARN'
           ELSE 'OK'
         END AS cap_band
  FROM measured m
),
judged AS (
  SELECT b.*,
         -- the verdict (field heuristic); NULL = too few statements to judge
         CASE
           WHEN b.query_count < 20                                               THEN NULL
           WHEN b.mem_band IN ('WARN', 'CRITICAL') AND b.cap_band IN ('WARN', 'CRITICAL') THEN 'MEMORY_AND_CAPACITY'
           WHEN b.mem_band IN ('WARN', 'CRITICAL')                                         THEN 'MEMORY'
           WHEN b.cap_band IN ('WARN', 'CRITICAL')                                         THEN 'CAPACITY'
           ELSE 'NONE'
         END AS pressure,
         -- the WHY, from the row's own numbers: whole percentages, GB to one decimal, and the
         -- threshold of the band the row actually reached (CRITICAL quotes the critical values)
         CONCAT(COALESCE(CAST(ROUND(b.spill_time_raw, 0) AS BIGINT), 0),
                '% of execution time was in statements that spilled to local disk (threshold ',
                CASE WHEN b.mem_band = 'CRITICAL' THEN 25 ELSE 10 END,
                '%); worst day spilled ',
                ROUND(b.spilled_local_bytes_max_day / 1e9, 1), ' GB (threshold ',
                CASE WHEN b.mem_band = 'CRITICAL' THEN 10 ELSE 1 END,
                ' GB)') AS mem_clause,
         CONCAT(COALESCE(CAST(ROUND(b.capacity_wait_raw, 0) AS BIGINT), 0),
                '% of statement time was spent queued at capacity (threshold ',
                CASE WHEN b.cap_band = 'CRITICAL' THEN 20 ELSE 5 END,
                '%); ',
                CASE WHEN b.max_clusters IS NOT NULL AND b.min_clusters >= b.max_clusters
                     THEN CONCAT('no autoscale range (min_clusters ', b.min_clusters,
                                 ', max_clusters ', b.max_clusters, ')')
                     WHEN b.max_cluster_count_seen IS NULL OR b.max_clusters IS NULL
                     THEN 'clusters seen unknown'
                     ELSE CONCAT('up to ', b.max_cluster_count_seen, ' of ', b.max_clusters,
                                 ' clusters seen')
                END) AS cap_clause,
         -- a NONE row's shares are below their WARN thresholds; one that rounds up to its threshold
         -- is written "just under <threshold>", so the reason never prints the threshold itself
         CONCAT('no threshold crossed: ',
                CASE WHEN ROUND(COALESCE(b.spill_time_raw, 0), 0) >= 10
                     THEN CONCAT('just under ', 10)
                     ELSE CAST(CAST(ROUND(COALESCE(b.spill_time_raw, 0), 0) AS BIGINT) AS STRING)
                END,
                '% of execution time spilled, ',
                CASE WHEN ROUND(COALESCE(b.capacity_wait_raw, 0), 0) >= 5
                     THEN CONCAT('just under ', 5)
                     ELSE CAST(CAST(ROUND(COALESCE(b.capacity_wait_raw, 0), 0) AS BIGINT) AS STRING)
                END,
                '% of statement time queued') AS none_clause
  FROM banded b
)
SELECT j.workspace_id,
       j.warehouse_id,
       j.compute_type,
       j.warehouse_type,
       j.warehouse_size,
       j.min_clusters,
       j.max_clusters,
       j.auto_stop_minutes,
       j.warehouse_deleted,
       j.query_count,
       j.finished_count,
       j.spilling_query_count,
       ROUND(j.spill_query_raw, 1)                     AS spill_query_pct,
       ROUND(j.spill_time_raw, 1)                      AS spill_time_pct,
       ROUND(j.spilled_local_bytes_sum / 1e9, 2)       AS spilled_local_gb_sum,
       ROUND(j.spilled_local_bytes_max_day / 1e9, 2)   AS spilled_local_gb_max_day,
       ROUND(j.read_bytes_sum / 1e9, 2)                AS read_gb_sum,
       ROUND(j.shuffle_read_bytes_sum / 1e9, 2)        AS shuffle_read_gb_sum,
       j.queued_at_capacity_count,
       ROUND(j.capacity_wait_raw, 1)                   AS capacity_wait_pct,
       ROUND(j.waiting_at_capacity_ms_sum / 1000.0, 0) AS waiting_at_capacity_s_sum,
       j.waited_for_compute_count,
       ROUND(j.cold_start_wait_raw, 1)                 AS cold_start_wait_pct,
       ROUND(j.waiting_for_compute_ms_sum / 1000.0, 0) AS waiting_for_compute_s_sum,
       ROUND(j.execution_ms_sum / 1000.0, 0)           AS execution_s_sum,
       ROUND(j.total_duration_ms_sum / 1000.0, 0)      AS total_duration_s_sum,
       j.max_cluster_count_seen,
       j.scaling_events,
       j.at_ceiling,
       j.pressure,
       -- the lever (see actions): size for memory; clusters at the ceiling, a warm cluster below it
       CASE
         WHEN j.pressure = 'MEMORY'                        THEN 'SCALE_UP'
         WHEN j.pressure = 'MEMORY_AND_CAPACITY' AND NOT j.at_ceiling THEN 'SCALE_UP_THEN_WARM'
         WHEN j.pressure = 'MEMORY_AND_CAPACITY'           THEN 'SCALE_UP_THEN_OUT'
         WHEN j.pressure = 'CAPACITY' AND NOT j.at_ceiling THEN 'KEEP_WARM'
         WHEN j.pressure = 'CAPACITY'                      THEN 'SCALE_OUT'
         WHEN j.pressure = 'NONE'                          THEN 'NONE'
         ELSE NULL
       END AS scaling_hint,
       CASE
         WHEN j.pressure = 'MEMORY'              THEN j.mem_clause
         WHEN j.pressure = 'CAPACITY'            THEN j.cap_clause
         WHEN j.pressure = 'MEMORY_AND_CAPACITY' THEN CONCAT(j.mem_clause, '; ', j.cap_clause)
         WHEN j.pressure = 'NONE'                THEN j.none_clause
         ELSE NULL
       END AS pressure_reason,
       -- why a warehouse could not be judged (NULL when it could)
       CASE WHEN j.pressure IS NULL THEN 'too_few_statements' ELSE NULL END AS not_assessed_reason,
       -- status: the worse of the memory and concurrency bands; too few statements is NOT_ASSESSED
       CASE
         WHEN j.pressure IS NULL                                 THEN 'NOT_ASSESSED'
         WHEN j.mem_band = 'CRITICAL' OR j.cap_band = 'CRITICAL' THEN 'CRITICAL'
         WHEN j.mem_band = 'WARN' OR j.cap_band = 'WARN'         THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM judged j
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         spill_time_pct DESC,
         capacity_wait_pct DESC,
         workspace_id, warehouse_id
LIMIT 100000
) q
"""
CHECKS["query_workload_mix_hours"] = r"""
-- generated from dbt/models/databricks_direct/performance/d_query_workload_mix_hours.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/performance/query_workload_mix_hours.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT date(start_time) AS day, workspace_id, hour(start_time) AS hour_of_day,
       compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
       statement_type,
       executed_by AS executed_by,
       COUNT(*) AS query_count,
       SUM(CASE WHEN execution_status = 'FINISHED' THEN 1 ELSE 0 END) AS finished_count,
       SUM(total_duration_ms)     AS total_duration_ms_sum,
       SUM(execution_duration_ms) AS execution_duration_ms_sum,
       SUM(read_bytes)            AS read_bytes_sum,
       SUM(produced_rows)         AS produced_rows_sum
FROM `system`.`query`.`history`
WHERE start_time >= current_date() - INTERVAL 30 DAYS
  AND start_time < current_date()
GROUP BY date(start_time), workspace_id, hour(start_time), compute.type, compute.warehouse_id, statement_type, executed_by
ORDER BY day, hour_of_day, workspace_id, compute_type, warehouse_id
) q
"""
CHECKS["query_task_statement_breakdown"] = r"""
-- generated from dbt/models/databricks_direct/performance/d_query_task_statement_breakdown.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/performance/query_task_statement_breakdown.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH run_agg AS (
  SELECT workspace_id, job_id, run_id,
         MIN(period_start_time)    AS run_start,
         MAX(period_end_time)      AS last_seen,
         MAX(result_state)         AS result_state,       -- NULL until the run's end row lands
         -- end row only; NULL before Dec 2025 and 0 for every multi-task job (doc), so 0 = not reported
         NULLIF(MAX(run_duration_seconds), 0) AS run_s_reported
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
  GROUP BY workspace_id, job_id, run_id
),
run_obs AS (
  SELECT workspace_id, job_id, run_id, run_start, result_state,
         (result_state IS NULL) AS in_flight,
         COALESCE(run_s_reported,
                  timestampdiff(SECOND, run_start,
                                CASE WHEN result_state IS NULL THEN current_timestamp()
                                     ELSE last_seen END)) AS run_s
  FROM run_agg
),
long_runs AS (
  -- the same run bound as lakeflow_long_running_runs, so the two queries compose
  SELECT workspace_id, job_id, run_id, run_start, in_flight, run_s
  FROM run_obs
  WHERE run_s >= 2 * 3600
     OR run_s IS NULL
),
task_agg AS (
  -- one row per TASK RUN (run_id here is the task run id; job_run_id is the parent run)
  SELECT t.workspace_id, t.job_id, t.job_run_id,
         t.run_id                                          AS task_run_id,
         t.task_key,
         MIN(t.period_start_time)                          AS task_start,
         MAX(t.period_end_time)                            AS task_last_seen,
         MAX(t.result_state)                               AS task_result_state,
         NULLIF(MAX(t.execution_duration_seconds), 0)      AS task_exec_s,
         -- first compute id of the task; NULL on serverless and on pre-Dec-2025 rows
         MAX(try_element_at(t.compute_ids, 1))             AS task_cluster_id,
         MAX(try_element_at(t.compute, 1).type)            AS task_compute_type_raw,
         MAX(try_element_at(t.compute, 1).warehouse_id)    AS task_warehouse_id
  FROM `system`.`lakeflow`.`job_task_run_timeline` t
  JOIN long_runs r
    ON  r.workspace_id = t.workspace_id
    AND r.job_id       = t.job_id
    AND r.run_id       = t.job_run_id
  WHERE t.period_start_time >= dateadd(day, -30, current_date())
  GROUP BY t.workspace_id, t.job_id, t.job_run_id, t.run_id, t.task_key
),
known_clusters AS (
  -- system.compute.clusters holds classic (all-purpose, job, pipeline) clusters only
  SELECT DISTINCT workspace_id, cluster_id
  FROM `system`.`compute`.`clusters`
),
task_obs AS (
  SELECT a.workspace_id, a.job_id, a.job_run_id, a.task_run_id, a.task_key,
         a.task_start, a.task_last_seen, a.task_result_state, a.task_cluster_id, a.task_compute_type_raw,
         COALESCE(a.task_exec_s,
                  timestampdiff(SECOND, a.task_start,
                                CASE WHEN a.task_result_state IS NULL THEN current_timestamp()
                                     ELSE a.task_last_seen END))     AS task_s,
         -- a task that never ran: SKIPPED / BLOCKED, or the doc's zero-length row
         -- (an in-flight task has no result yet: FALSE, so it still counts as not assessed)
         COALESCE(a.task_result_state IN ('SKIPPED', 'BLOCKED')
          OR (a.task_result_state IS NOT NULL AND a.task_start = a.task_last_seen), FALSE) AS task_not_executed,
         -- in flight, or ended so recently that its statements may not have landed yet
         (a.task_result_state IS NULL
          OR a.task_last_seen >= dateadd(hour, -1, current_timestamp())) AS sql_may_be_incomplete,
         -- structural compute classification; see caveats
         CASE
           WHEN a.task_cluster_id IS NOT NULL AND k.cluster_id IS NOT NULL THEN 'CLASSIC_CLUSTER'
           WHEN a.task_warehouse_id IS NOT NULL                             THEN 'SQL_WAREHOUSE'
           WHEN a.task_cluster_id IS NULL                                   THEN 'SERVERLESS_OR_UNRECORDED'
           ELSE 'UNRESOLVED'
         END                                                          AS task_compute_kind
  FROM task_agg a
  LEFT JOIN known_clusters k
    ON  a.workspace_id   = k.workspace_id
    AND a.task_cluster_id = k.cluster_id
),
stmts AS (
  -- the nested query_source path is extracted to scalars HERE, before any join or GROUP BY;
  -- the semi-joins keep the scan to the long runs' own statements
  SELECT workspace_id, statement_id, statement_type, execution_status,
         query_source.job_info.job_id          AS src_job_id,
         query_source.job_info.job_run_id      AS src_job_run_id,
         query_source.job_info.job_task_run_id AS src_task_run_id,
         compute.type                          AS stmt_compute_type,
         start_time, end_time,
         total_duration_ms, execution_duration_ms, compilation_duration_ms,
         waiting_for_compute_duration_ms, waiting_at_capacity_duration_ms, total_task_duration_ms,
         read_bytes, spilled_local_bytes, shuffle_read_bytes,
         (statement_text IS NULL OR statement_text = '<REDACTED>') AS text_redacted,
         -- statement_text de-valued exactly as query_costly_statements does: strip emails, then
         -- replace every single-quoted literal with '?' (chr(39) is the single quote)
         regexp_replace(
           regexp_replace(statement_text, '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}', '<email>'),
           concat(chr(39), '[^', chr(39), ']*', chr(39)), '?'
         )                                     AS statement_text_devalued
  FROM `system`.`query`.`history`
  WHERE start_time >= dateadd(day, -30, current_date())
    AND (query_source.job_info.job_task_run_id IN (SELECT task_run_id FROM task_obs)
         OR query_source.job_info.job_run_id   IN (SELECT run_id FROM long_runs))
),
stmt_fp AS (
  SELECT s.*,
         -- the recipe shared with query_costly_statements (string literals only): for lookups
         sha2(s.statement_text_devalued, 256) AS statement_fingerprint,
         -- the grouping key: bare numbers masked too, and runs of ?, ?, ? collapsed to one ?;
         -- with <REDACTED> text no shape exists, so each statement is its own key
         CASE WHEN s.text_redacted THEN concat('statement:', s.statement_id)
              ELSE sha2(
                     regexp_replace(
                       regexp_replace(s.statement_text_devalued,
                                      '(^|[^A-Za-z0-9_.])[0-9]+([.][0-9]+)?', '$1?'),
                       '[?]( *, *[?])+', '?'),
                     256)
         END                                   AS shape_fingerprint,
         -- statements with no task run id are bucketed per run
         CASE WHEN s.src_task_run_id IS NULL THEN s.src_job_run_id END AS orphan_job_run_id
  FROM stmts s
),
stmt_shape AS (
  -- one row per (task run OR run bucket, shape)
  SELECT workspace_id, src_task_run_id, orphan_job_run_id, shape_fingerprint,
         MAX(src_job_id)                                             AS src_job_id,
         MAX(src_job_run_id)                                         AS src_job_run_id,
         MAX(text_redacted)                                          AS text_redacted,
         COUNT(*)                                                    AS statement_count,
         SUM(CASE WHEN execution_status <> 'FINISHED' THEN 1 ELSE 0 END) AS statements_not_finished,
         COUNT(DISTINCT statement_fingerprint)                       AS literal_variants_in_shape,
         MIN(statement_fingerprint)                                  AS statement_fingerprint,
         MIN(statement_type)                                         AS statement_type,
         MIN(statement_id)                                           AS sample_statement_id,
         MIN(stmt_compute_type)                                      AS stmt_compute_type,
         MIN(start_time)                                             AS first_start_time,
         MAX(end_time)                                               AS last_end_time,
         SUM(total_duration_ms)                                      AS total_ms,
         SUM(execution_duration_ms)                                  AS execution_ms,
         SUM(compilation_duration_ms)                                AS compilation_ms,
         SUM(waiting_for_compute_duration_ms)                        AS waiting_for_compute_ms,
         SUM(waiting_at_capacity_duration_ms)                        AS waiting_at_capacity_ms,
         SUM(total_task_duration_ms)                                 AS total_task_ms,
         SUM(read_bytes)                                             AS read_bytes,
         SUM(spilled_local_bytes)                                    AS spilled_local_bytes,
         SUM(shuffle_read_bytes)                                     AS shuffle_read_bytes,
         MIN(statement_text_devalued)                                AS statement_text_devalued
  FROM stmt_fp
  GROUP BY workspace_id, src_task_run_id, orphan_job_run_id, shape_fingerprint
),
task_sql AS (
  -- per task run: how much SQL was captured at all
  SELECT workspace_id, src_task_run_id,
         SUM(statement_count) AS task_statement_count,
         SUM(total_ms)        AS task_sql_ms
  FROM stmt_shape
  WHERE src_task_run_id IS NOT NULL
  GROUP BY workspace_id, src_task_run_id
),
joined AS (
  -- tasks with their statement shapes; tasks with none survive (NOT_ASSESSED); statements that
  -- matched no task run of a long run survive too (UNATTRIBUTED, pinned to the run)
  SELECT COALESCE(t.workspace_id, s.workspace_id) AS workspace_id,
         COALESCE(t.job_id, s.src_job_id)         AS job_id,
         COALESCE(t.job_run_id, s.src_job_run_id) AS job_run_id,
         t.task_run_id, t.task_key, t.task_start, t.task_result_state, t.task_s,
         t.task_not_executed, t.sql_may_be_incomplete,
         t.task_cluster_id, t.task_compute_type_raw,
         CASE WHEN t.task_run_id IS NULL THEN 'UNATTRIBUTED' ELSE t.task_compute_kind END AS task_compute_kind,
         s.src_task_run_id AS stmt_task_run_id,
         s.shape_fingerprint, s.statement_fingerprint, s.literal_variants_in_shape, s.text_redacted,
         s.statement_count, s.statements_not_finished, s.statement_type,
         s.sample_statement_id, s.stmt_compute_type, s.first_start_time, s.last_end_time,
         s.total_ms, s.execution_ms, s.compilation_ms, s.waiting_for_compute_ms,
         s.waiting_at_capacity_ms, s.total_task_ms, s.read_bytes, s.spilled_local_bytes,
         s.shuffle_read_bytes, s.statement_text_devalued
  FROM task_obs t
  FULL OUTER JOIN stmt_shape s
    ON  t.workspace_id = s.workspace_id
    AND t.task_run_id  = s.src_task_run_id
),
scoped AS (
  -- keep only the long runs (drops statements whose fallback run id is unknown or not long)
  SELECT j.*, r.run_s, r.in_flight,
         -- share denominator: the task's observed seconds, or the run's for UNATTRIBUTED rows
         COALESCE(j.task_s, r.run_s) AS share_denominator_s
  FROM joined j
  JOIN long_runs r
    ON  r.workspace_id = j.workspace_id
    AND r.run_id       = j.job_run_id
),
run_cov AS (
  -- per run: task runs seen, task runs with no captured SQL (never-run tasks excluded), and
  -- statements that exist but could not be pinned to a task
  SELECT workspace_id, job_run_id,
         COUNT(DISTINCT task_run_id)                                   AS run_tasks_seen,
         COUNT(DISTINCT CASE WHEN shape_fingerprint IS NULL AND NOT task_not_executed
                             THEN task_run_id END)                     AS run_tasks_not_assessed,
         SUM(CASE WHEN task_run_id IS NULL THEN statement_count ELSE 0 END) AS run_unattributed_statements
  FROM scoped
  GROUP BY workspace_id, job_run_id
),
ranked AS (
  SELECT sc.*,
         100.0 * sc.total_ms / 1000.0 / NULLIF(sc.share_denominator_s, 0) AS share_of_task_pct_raw,
         ROW_NUMBER() OVER (PARTITION BY sc.workspace_id, sc.job_run_id,
                                         COALESCE(sc.task_run_id, sc.stmt_task_run_id)
                            ORDER BY sc.total_ms DESC, sc.shape_fingerprint) AS stmt_rank_in_task
  FROM scoped sc
),
latest_jobs AS (
  -- system.lakeflow.jobs is SCD2: one row per change, so take the newest per job
  SELECT workspace_id, job_id, name AS job_name
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
)
SELECT k.workspace_id,
       k.job_id,
       j.job_name,                                                    -- NULL for submit/workflow runs
       k.job_run_id,
       ROUND(k.run_s / 3600.0, 2)                                     AS run_hours,
       k.in_flight,
       c.run_tasks_seen,
       c.run_tasks_not_assessed,
       c.run_unattributed_statements,
       k.task_key,                                                    -- NULL = UNATTRIBUTED
       k.task_run_id,
       k.stmt_task_run_id,                                            -- what the statement itself claims
       k.task_start,
       COALESCE(k.task_result_state, CASE WHEN k.task_run_id IS NOT NULL THEN 'RUNNING' END) AS task_state,
       ROUND(k.task_s / 3600.0, 2)                                    AS task_hours,
       k.task_compute_kind,
       k.task_compute_type_raw,
       k.task_cluster_id,
       CASE WHEN k.task_run_id IS NULL THEN NULL ELSE COALESCE(q.task_statement_count, 0) END AS task_statement_count,
       ROUND(100.0 * q.task_sql_ms / 1000.0 / NULLIF(k.task_s, 0), 1) AS task_sql_share_pct,
       k.sql_may_be_incomplete,
       CASE WHEN k.shape_fingerprint IS NULL THEN NULL ELSE k.stmt_rank_in_task END AS stmt_rank_in_task,
       k.shape_fingerprint,
       k.statement_fingerprint,
       k.literal_variants_in_shape,
       k.text_redacted,
       k.statement_count,
       k.statements_not_finished,
       k.statement_type,
       k.sample_statement_id,
       k.stmt_compute_type,
       k.first_start_time,
       k.last_end_time,
       k.total_ms,
       k.execution_ms,
       k.compilation_ms,
       k.waiting_for_compute_ms,
       k.waiting_at_capacity_ms,
       k.total_task_ms,
       k.read_bytes,
       k.spilled_local_bytes,
       k.shuffle_read_bytes,
       ROUND(k.share_of_task_pct_raw, 1)                              AS share_of_task_pct,
       k.statement_text_devalued,
       -- why a row could not be judged (NULL when it could)
       CASE
         WHEN k.shape_fingerprint IS NOT NULL AND k.share_of_task_pct_raw IS NULL
                                                             THEN 'task_seconds_unknown'
         WHEN k.shape_fingerprint IS NOT NULL AND k.total_ms < 60000
                                                             THEN 'below_duration_floor'
         WHEN k.shape_fingerprint IS NOT NULL
              AND k.share_of_task_pct_raw < 0.3 * 100
              AND k.sql_may_be_incomplete                    THEN 'statements_may_still_be_landing'
         WHEN k.shape_fingerprint IS NOT NULL                THEN NULL
         WHEN k.task_not_executed                            THEN 'task_not_executed'
         WHEN k.task_compute_kind = 'CLASSIC_CLUSTER'        THEN 'classic_cluster_not_in_query_history'
         WHEN k.sql_may_be_incomplete                        THEN 'statements_may_still_be_landing'
         ELSE 'no_statements_captured'
       END                                                            AS not_assessed_reason,
       -- status: worst-first band on the shape's share of its task's observed seconds (field
       -- heuristic; 0.3 / 0.6), gated by 60000 so a short statement
       -- inside a slightly-longer task cannot read CRITICAL on share alone. A task with no visible
       -- SQL, or whose SQL may still be landing, is NOT_ASSESSED - "could not look", never "nothing there".
       CASE
         WHEN k.shape_fingerprint IS NULL                              THEN 'NOT_ASSESSED'
         WHEN k.share_of_task_pct_raw IS NULL                          THEN 'NOT_ASSESSED'
         WHEN k.total_ms < 60000                                THEN 'NOT_ASSESSED'
         WHEN k.share_of_task_pct_raw >= 0.6 * 100        THEN 'CRITICAL'
         WHEN k.share_of_task_pct_raw >= 0.3 * 100        THEN 'WARN'
         WHEN k.sql_may_be_incomplete                                  THEN 'NOT_ASSESSED'
         ELSE 'OK'
       END AS status
FROM ranked k
LEFT JOIN run_cov c
  ON  k.workspace_id = c.workspace_id
  AND k.job_run_id   = c.job_run_id
LEFT JOIN task_sql q
  ON  k.workspace_id = q.workspace_id
  AND k.task_run_id  = q.src_task_run_id
LEFT JOIN latest_jobs j
  ON  k.workspace_id = j.workspace_id
  AND k.job_id       = j.job_id
WHERE k.stmt_rank_in_task <= 10
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         share_of_task_pct DESC,
         workspace_id, job_id, job_run_id, task_key, task_run_id, stmt_task_run_id, stmt_rank_in_task
) q
"""
CHECKS["perf_queue_by_hour"] = r"""
-- generated from dbt/models/databricks_direct/performance/d_perf_queue_by_hour.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/performance/perf_queue_by_hour.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT date(start_time) AS usage_date,
       hour(start_time) AS usage_hour,
       concat(workspace_id, ':', compute.warehouse_id) AS warehouse_key,
       workspace_id,
       compute.warehouse_id AS warehouse_id,
       COUNT(*) AS runs,
       SUM(CASE WHEN COALESCE(waiting_at_capacity_duration_ms, 0) > 0 THEN 1 ELSE 0 END) AS queued_runs,
       ROUND(SUM(COALESCE(waiting_at_capacity_duration_ms, 0)) / 1000.0, 1) AS slot_wait_s,
       SUM(CASE WHEN COALESCE(waiting_for_compute_duration_ms, 0) > 0 THEN 1 ELSE 0 END) AS provision_runs,
       ROUND(SUM(COALESCE(waiting_for_compute_duration_ms, 0)) / 1000.0, 1) AS provision_s
FROM `system`.`query`.`history`
WHERE start_time >= current_date() - INTERVAL 30 DAYS
  AND start_time < current_date()
  AND compute.warehouse_id IS NOT NULL
GROUP BY date(start_time), hour(start_time), workspace_id, compute.warehouse_id
ORDER BY usage_date DESC, usage_hour, warehouse_key
) q
"""
CHECKS["cost_account_prices_raw"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_account_prices_raw.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_account_prices_raw.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT price_start_time, price_end_time, account_id, sku_name, cloud, currency_code, usage_unit,
       CAST(pricing.default AS STRING) AS pricing_default
FROM `system`.`billing`.`list_prices`
ORDER BY sku_name, cloud, currency_code, price_start_time
) q
"""
CHECKS["pricing_list_prices_raw"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_pricing_list_prices_raw.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/pricing_list_prices_raw.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT price_start_time, price_end_time, account_id, sku_name, cloud, currency_code, usage_unit,
       CAST(pricing.default       AS STRING) AS pricing_default,
       CAST(pricing.effective_list AS STRING) AS pricing_effective_list_json,
       CAST(pricing.promotional   AS STRING) AS pricing_promotional_json
FROM `system`.`billing`.`list_prices`
ORDER BY sku_name, cloud, currency_code, price_start_time
) q
"""
CHECKS["cost_audit_self_usage"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_audit_self_usage.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_audit_self_usage.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH matched AS (
  SELECT workspace_id, compute.warehouse_id AS warehouse_id, statement_id,
         client_application, total_duration_ms, start_time
  FROM `system`.`query`.`history`
  WHERE start_time >= date_sub(current_date(), 30)
    AND start_time < current_date()
    AND compute.warehouse_id IS NOT NULL
    -- the export's query tag (fixed key audit_app), or its client-application name
    AND (client_application ILIKE '%Crosshire%' OR array_contains(map_keys(query_tags), 'audit_app'))
),
warehouse_all AS (
  SELECT workspace_id, compute.warehouse_id AS warehouse_id,
         SUM(total_duration_ms) AS all_duration_ms
  FROM `system`.`query`.`history`
  WHERE start_time >= date_sub(current_date(), 30)
    AND start_time < current_date()
    AND compute.warehouse_id IS NOT NULL
  GROUP BY workspace_id, compute.warehouse_id
),
matched_agg AS (
  SELECT workspace_id, warehouse_id,
         COUNT(*) AS matched_statement_count,
         SUM(total_duration_ms) AS matched_duration_ms,
         MIN(start_time) AS first_query_time,
         MAX(start_time) AS last_query_time,
         MAX(client_application) AS matched_client_application
  FROM matched
  GROUP BY workspace_id, warehouse_id
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
wh_usd AS (
  SELECT u.workspace_id, u.usage_metadata.warehouse_id AS warehouse_id,
         SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS warehouse_usd_list,
         CASE WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                            THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
              WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
              ELSE 'priced' END AS price_basis
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.warehouse_id IS NOT NULL
    AND u.usage_date >= date_sub(current_date(), 30)
    AND u.usage_date < current_date()
  GROUP BY u.workspace_id, u.usage_metadata.warehouse_id
),
wh_name AS (
  SELECT warehouse_id, warehouse_name
  FROM `system`.`compute`.`warehouses`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
)
SELECT m.workspace_id,
       m.warehouse_id,
       n.warehouse_name,
       m.matched_client_application,
       m.matched_statement_count,
       ROUND(m.matched_duration_ms / 1000.0, 1) AS matched_duration_secs,
       ROUND(a.all_duration_ms / 1000.0, 1) AS warehouse_all_duration_secs,
       ROUND(m.matched_duration_ms * 100.0 / NULLIF(a.all_duration_ms, 0), 1) AS matched_share_pct,
       ROUND(w.warehouse_usd_list, 2) AS est_usd_list,
       CASE WHEN w.price_basis IN ('priced', 'free') AND a.all_duration_ms > 0
            THEN ROUND(w.warehouse_usd_list * m.matched_duration_ms / a.all_duration_ms, 2) END AS est_audit_usd_list,
       w.price_basis,
       m.first_query_time, m.last_query_time
FROM matched_agg m
LEFT JOIN warehouse_all a ON a.workspace_id = m.workspace_id AND a.warehouse_id = m.warehouse_id
LEFT JOIN wh_usd w        ON w.workspace_id = m.workspace_id AND w.warehouse_id = m.warehouse_id
LEFT JOIN wh_name n       ON n.warehouse_id = m.warehouse_id
ORDER BY est_audit_usd_list DESC NULLS LAST, m.workspace_id, m.warehouse_id
) q
"""
CHECKS["overview_daily_dbu_trend"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_overview_daily_dbu_trend.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/overview_daily_dbu_trend.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT
    workspace_id,
    usage_date,
    SUM(usage_quantity) AS net_dbus
FROM `system`.`billing`.`usage`
WHERE usage_unit = 'DBU'
  AND usage_date >= dateadd(day, -30, current_date())
  AND usage_date < current_date()
GROUP BY workspace_id, usage_date
ORDER BY usage_date
) q
"""
CHECKS["overview_dbu_by_sku"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_overview_dbu_by_sku.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/overview_dbu_by_sku.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT
    workspace_id,
    sku_name,
    billing_origin_product,
    SUM(usage_quantity) AS net_dbus
FROM `system`.`billing`.`usage`
WHERE usage_unit = 'DBU'
  AND usage_date >= dateadd(day, -30, current_date())
  AND usage_date < current_date()
GROUP BY workspace_id, sku_name, billing_origin_product
ORDER BY net_dbus DESC
) q
"""
CHECKS["overview_list_prices_raw"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_overview_list_prices_raw.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/overview_list_prices_raw.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT
    price_start_time,
    price_end_time,
    account_id,
    sku_name,
    cloud,
    currency_code,
    usage_unit,
    CAST(pricing.default AS STRING) AS pricing_default,
    CAST(pricing.effective_list AS STRING) AS pricing_effective_list_json,
    CAST(pricing.promotional AS STRING) AS pricing_promotional_json
FROM `system`.`billing`.`list_prices`
) q
"""
CHECKS["overview_serverless_classic_split"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_overview_serverless_classic_split.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/overview_serverless_classic_split.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH usage_rows AS (
    SELECT workspace_id, usage_quantity, product_features, custom_tags,
           CASE WHEN (product_features.is_serverless = TRUE OR upper(sku_name) LIKE '%SERVERLESS%'
               OR billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH', 'GENIE', 'AI_FUNCTIONS',
                                             'AI_GATEWAY', 'AGENT_BRICKS', 'LAKEBASE', 'APPS'))
                THEN TRUE ELSE FALSE END AS on_serverless
    FROM `system`.`billing`.`usage`
    WHERE usage_unit = 'DBU'
      AND usage_date >= dateadd(day, -30, current_date())
      AND usage_date < current_date()
)
SELECT
    workspace_id,
    SUM(CASE WHEN on_serverless THEN usage_quantity ELSE 0 END) AS serverless_net_dbus,
    SUM(CASE WHEN on_serverless THEN 0 ELSE usage_quantity END) AS classic_net_dbus,
    ROUND(100.0 * SUM(CASE WHEN on_serverless THEN usage_quantity ELSE 0 END)
          / NULLIF(SUM(usage_quantity), 0), 1) AS serverless_pct,
    SUM(CASE WHEN product_features.is_photon = TRUE THEN usage_quantity ELSE 0 END) AS photon_net_dbus,
    ROUND(100.0 * SUM(CASE WHEN product_features.is_photon = TRUE THEN usage_quantity ELSE 0 END)
          / NULLIF(SUM(usage_quantity), 0), 1) AS photon_pct,
    SUM(CASE WHEN upper(product_features.performance_target) = 'PERFORMANCE_OPTIMIZED' THEN usage_quantity ELSE 0 END) AS perf_optimized_net_dbus,
    SUM(CASE WHEN cardinality(map_keys(custom_tags)) = 0 THEN usage_quantity ELSE 0 END) AS untagged_net_dbus,
    SUM(usage_quantity) AS total_net_dbus
FROM usage_rows
GROUP BY workspace_id
) q
"""
CHECKS["overview_spend_estimate"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_overview_spend_estimate.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/overview_spend_estimate.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT
    workspace_id,
    SUM(u.usage_quantity) AS total_net_dbus,
    'USD' AS currency_code,
    ROUND(SUM(u.usage_quantity * lp.list_rate), 2) AS net_list_cost_usd,
    'list' AS dbu_price_source,
    -- price_basis: distinguishes a real $0 (free-usage SKUs, no list_prices row by design) from
    -- a coverage gap (a priced SKU with no matching list_prices row, which understates the total).
    CASE
      WHEN SUM(CASE WHEN lp.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                    THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
      WHEN SUM(CASE WHEN lp.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
      ELSE 'priced'
    END AS price_basis
FROM `system`.`billing`.`usage` u
LEFT JOIN (
  SELECT sku_name, cloud, currency_code, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
) lp
  ON u.sku_name = lp.sku_name
 AND u.cloud = lp.cloud
 AND u.usage_unit = lp.usage_unit
 AND u.usage_end_time >= lp.price_start_time
 AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
WHERE u.usage_unit = 'DBU'
  AND u.usage_date >= dateadd(day, -30, current_date())
  AND u.usage_date < current_date()
GROUP BY workspace_id
) q
"""
CHECKS["compute_serving_endpoint_cost_status"] = r"""
-- generated from dbt/models/databricks_direct/serving_ai/d_compute_serving_endpoint_cost_status.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/serving_ai/compute_serving_endpoint_cost_status.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH entities AS (   -- served_entities is change-history -> collapse to the latest config row per entity
  SELECT workspace_id, endpoint_id, endpoint_name, served_entity_id, served_entity_name,
         entity_type, entity_name, entity_version, latest_change_time, endpoint_config_version, current_version
  FROM (
    SELECT se.workspace_id, se.endpoint_id, se.endpoint_name, se.served_entity_id,
           se.served_entity_name, se.entity_type, se.entity_name, se.entity_version, se.endpoint_config_version,
           MAX(se.endpoint_config_version) OVER (PARTITION BY se.workspace_id, se.endpoint_id) AS current_version,
           MAX(se.change_time) OVER (PARTITION BY se.workspace_id, se.endpoint_id, se.served_entity_id) AS latest_change_time,
           ROW_NUMBER() OVER (PARTITION BY se.workspace_id, se.endpoint_id, se.served_entity_id ORDER BY se.change_time DESC) AS _rn
    FROM `system`.`serving`.`served_entities` se
  ) WHERE _rn = 1
),
entity_agg AS (   -- one row per endpoint - never fanned out by served-entity count
  SELECT workspace_id, endpoint_id,
         MAX(endpoint_name)                                                     AS endpoint_name,
         COUNT(*)                                                               AS served_entity_count,
         array_join(collect_set(entity_type), ', ')                             AS entity_types,
         array_join(collect_set(COALESCE(entity_name, served_entity_name)), ', ') AS entity_names,
         MAX(latest_change_time)                                                AS latest_change_time,
         MAX(CASE WHEN _erank = 1 THEN served_entity_id   END)                  AS primary_served_entity_id,
         MAX(CASE WHEN _erank = 1 THEN served_entity_name END)                  AS primary_served_entity_name,
         MAX(CASE WHEN _erank = 1 THEN entity_type        END)                  AS primary_entity_type,
         MAX(CASE WHEN _erank = 1 THEN entity_name        END)                  AS primary_entity_name,
         MAX(CASE WHEN _erank = 1 THEN entity_version     END)                  AS primary_entity_version
  FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY workspace_id, endpoint_id ORDER BY latest_change_time DESC) AS _erank
    FROM entities
    -- the entities in the current config only; retired versions are not served any more
    WHERE endpoint_config_version = current_version OR current_version IS NULL
  )
  GROUP BY workspace_id, endpoint_id
),
ent_map AS (   -- served_entity_id -> endpoint_id, so endpoint_usage (which has no endpoint_id) can roll up to endpoint level
  SELECT DISTINCT workspace_id, endpoint_id, served_entity_id FROM entities
),
usage_entity AS (   -- per served entity, inside the analysis window
  SELECT eu.workspace_id, eu.served_entity_id,
         COUNT(*) AS total_requests,
         MAX(CAST(eu.request_time AS DATE)) AS last_request_date
  FROM `system`.`serving`.`endpoint_usage` eu
  WHERE eu.request_time >= dateadd(day, -30, current_date())
    AND eu.request_time <  current_date()
  GROUP BY eu.workspace_id, eu.served_entity_id
),
usage_endpoint AS (   -- requests rolled up to endpoint, inside the analysis window
  SELECT m.workspace_id, m.endpoint_id,
         SUM(ue.total_requests) AS ep_requests,
         MAX(ue.last_request_date) AS ep_last_request_date
  FROM usage_entity ue
  JOIN ent_map m ON ue.workspace_id = m.workspace_id AND ue.served_entity_id = m.served_entity_id
  GROUP BY m.workspace_id, m.endpoint_id
),
usage_ever AS (   -- ANY tracked row over the full retention window -> distinguishes "tracking off" from "idle in window"
  SELECT m.workspace_id, m.endpoint_id, COUNT(*) AS ever_requests
  FROM `system`.`serving`.`endpoint_usage` eu
  JOIN ent_map m ON eu.workspace_id = m.workspace_id AND eu.served_entity_id = m.served_entity_id
  WHERE eu.request_time >= dateadd(day, -90, current_date())
  GROUP BY m.workspace_id, m.endpoint_id
),
retention_history AS (   -- how far the account's OWN endpoint_usage history actually reaches back
  SELECT workspace_id, MIN(request_time) AS earliest_request_time
  FROM `system`.`serving`.`endpoint_usage`
  GROUP BY workspace_id
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
cost AS (   -- billing per (workspace, endpoint, product); VS carries endpoint_name, model serving carries endpoint_id -> COALESCE
  SELECT u.workspace_id,
         u.usage_metadata.endpoint_id                     AS endpoint_id,
         u.usage_metadata.endpoint_name                   AS endpoint_name_billing,
         u.billing_origin_product                         AS product,
         SUM(u.usage_quantity)                            AS net_dbus,
         SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS est_usd_list,
         CASE
           WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                         THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END                                               AS price_basis
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND (u.usage_metadata.endpoint_id IS NOT NULL OR u.usage_metadata.endpoint_name IS NOT NULL)
    AND u.usage_date >= dateadd(day, -30, current_date())
    AND u.usage_date <  current_date()
  GROUP BY u.workspace_id, u.usage_metadata.endpoint_id, u.usage_metadata.endpoint_name, u.billing_origin_product
),
cost_ep AS (   -- collapse products -> one row per endpoint (total cost), keep the product list
  SELECT workspace_id,
         COALESCE(endpoint_id, endpoint_name_billing)              AS endpoint_key,
         MAX(endpoint_id)                                          AS endpoint_id,
         MAX(endpoint_name_billing)                                AS endpoint_name_billing,
         SUM(net_dbus)                                             AS net_dbus,
         SUM(est_usd_list)                                         AS est_usd_list,
         array_join(collect_set(product), ', ')                    AS products,
         MAX(CASE WHEN product = 'VECTOR_SEARCH' THEN 1 ELSE 0 END) AS is_vs,
         CASE
           WHEN SUM(CASE WHEN price_basis = 'unpriced' THEN 1 ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN price_basis = 'priced'   THEN 1 ELSE 0 END) = 0
                AND SUM(CASE WHEN price_basis = 'free' THEN 1 ELSE 0 END) > 0 THEN 'free'
           ELSE 'priced'
         END                                                        AS price_basis
  FROM cost GROUP BY workspace_id, COALESCE(endpoint_id, endpoint_name_billing)
),
scored AS (
  SELECT
    ce.workspace_id,
    w.workspace_name,
    ce.endpoint_id,
    COALESCE(ent.endpoint_name, ce.endpoint_name_billing) AS endpoint_name,
    ce.products,
    ent.primary_served_entity_id                           AS served_entity_id,
    ent.primary_served_entity_name                         AS served_entity_name,
    ent.primary_entity_type                                AS entity_type,
    ent.primary_entity_name                                AS entity_name,
    ent.primary_entity_version                             AS entity_version,
    ent.latest_change_time,
    ROUND(ce.net_dbus, 1)                                  AS net_dbus,
    ROUND(ce.est_usd_list, 2)                              AS est_usd_list,
    ce.price_basis                                         AS price_basis,
    -- est_wasted_usd_list (P3-WASTEUSD): mirrors the status CASE's own CRITICAL branch below
    -- exactly - a confirmed-idle endpoint's WHOLE measured spend is waste; NULL (not 0) where idle
    -- cannot be confirmed (Vector Search / untracked / tracking-off / insufficient history); 0 for
    -- real, if low, traffic. Never divided: one row per endpoint now, so nothing to double-count.
    CASE
      WHEN ce.is_vs = 1                                            THEN NULL
      WHEN ent.endpoint_id IS NULL                                THEN NULL
      WHEN COALESCE(uev.ever_requests, 0) = 0 AND ce.net_dbus > 0  THEN NULL
      -- a missing price would read as a smaller waste; unknown stays NULL, never an undercount
      WHEN ce.price_basis = 'unpriced'                             THEN NULL
      WHEN COALESCE(uep.ep_requests, 0) = 0 AND ce.net_dbus > 0    THEN ROUND(ce.est_usd_list, 2)
      ELSE 0
    END                                                     AS est_wasted_usd_list,
    COALESCE(pue.total_requests, 0)                        AS entity_requests_window,
    pue.last_request_date                                  AS entity_last_request_date,
    COALESCE(uep.ep_requests, 0)                           AS endpoint_requests_window,
    uep.ep_last_request_date                               AS endpoint_last_request_date,
    uep.ep_last_request_date                               AS ep_last_request_date,  -- kept: old column name
    -- Is the "0 requests" real, or just untracked? (free-text detail; the enum band is `status`)
    CASE
      WHEN ce.is_vs = 1                                            THEN 'Vector Search - no serving-table telemetry (see cost_vector_search_spend)'
      WHEN ent.endpoint_id IS NULL                                THEN 'bills but not in served_entities (config unchanged for over a year, or not a model-serving endpoint) - check it is used'
      WHEN COALESCE(uev.ever_requests, 0) = 0 AND ce.net_dbus > 0
           AND rh.earliest_request_time IS NOT NULL
           AND datediff(current_date(), rh.earliest_request_time) < 90
        THEN 'not enough endpoint_usage history to judge tracking (retention shorter than the lookback)'
      WHEN COALESCE(uev.ever_requests, 0) = 0 AND ce.net_dbus > 0  THEN 'usage tracking likely OFF - spend but zero tracked rows in retention'
      WHEN COALESCE(uep.ep_requests, 0) = 0                        THEN 'tracking ON - idle in window (had traffic within retention)'
      ELSE                                                             'tracking ON - active'
    END AS tracking_status,
    CASE
      WHEN ce.is_vs = 1                                            THEN 'not_serving_telemetry'
      WHEN COALESCE(uev.ever_requests, 0) = 0 AND ce.net_dbus > 0
           AND rh.earliest_request_time IS NOT NULL
           AND datediff(current_date(), rh.earliest_request_time) < 90
        THEN 'insufficient_retention_history'
    END AS not_assessed_reason,
    rh.earliest_request_time,
    CASE WHEN rh.earliest_request_time IS NULL THEN NULL
         ELSE datediff(current_date(), rh.earliest_request_time) END AS retention_days_available,
    90                                        AS retention_days_required,
    ent.served_entity_count,
    ent.entity_types,
    ent.entity_names,
    ent.primary_served_entity_id,
    ent.primary_served_entity_name,
    ent.primary_entity_type,
    ent.primary_entity_name,
    ent.primary_entity_version,
    CASE
      WHEN ce.is_vs = 1                                                THEN 'NOT_ASSESSED'   -- Vector Search: no serving telemetry
      -- served_entities keeps 365 days and writes only on config changes: an endpoint untouched
      -- for a year drops out of it while still billing
      WHEN ent.endpoint_id IS NULL AND ce.net_dbus > 0                THEN 'WARN'
      WHEN COALESCE(uev.ever_requests, 0) = 0 AND ce.net_dbus > 0
           AND rh.earliest_request_time IS NOT NULL
           AND datediff(current_date(), rh.earliest_request_time) < 90     THEN 'NOT_ASSESSED'  -- genuinely-tracked, too new to judge
      WHEN COALESCE(uev.ever_requests, 0) = 0 AND ce.net_dbus > 0     THEN 'WARN'            -- spend but tracking off (no history at all, or not enough): enable tracking, do not call it idle
      WHEN COALESCE(uep.ep_requests, 0) = 0 AND ce.net_dbus > 0       THEN 'CRITICAL'        -- tracked + spend + zero requests = truly idle
      WHEN COALESCE(uep.ep_requests, 0) <= 10         THEN 'WARN'
      ELSE 'OK'
    END AS status
  FROM cost_ep ce
  LEFT JOIN entity_agg     ent ON ent.workspace_id = ce.workspace_id AND ent.endpoint_id = ce.endpoint_id
  LEFT JOIN usage_entity   pue ON pue.workspace_id = ce.workspace_id AND pue.served_entity_id = ent.primary_served_entity_id
  LEFT JOIN usage_endpoint uep ON uep.workspace_id = ce.workspace_id AND uep.endpoint_id = ce.endpoint_id
  LEFT JOIN usage_ever     uev ON uev.workspace_id = ce.workspace_id AND uev.endpoint_id = ce.endpoint_id
  LEFT JOIN retention_history rh ON rh.workspace_id = ce.workspace_id
  LEFT JOIN `system`.`access`.`workspaces_latest` w ON w.workspace_id = ce.workspace_id
)
SELECT *
FROM scored
ORDER BY
  CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
  net_dbus DESC, endpoint_requests_window ASC
LIMIT 100000
) q
"""
CHECKS["compute_serving_dormant_endpoints"] = r"""
-- generated from dbt/models/databricks_direct/serving_ai/d_compute_serving_dormant_endpoints.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/serving_ai/compute_serving_dormant_endpoints.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH history AS (
  SELECT se.*,
         MAX(se.endpoint_config_version) OVER (PARTITION BY se.workspace_id, se.endpoint_id) AS current_version,
         MAX(se.endpoint_delete_time) OVER (PARTITION BY se.workspace_id, se.endpoint_id) AS deleted_time,
         MAX(se.change_time) OVER (PARTITION BY se.workspace_id, se.endpoint_id) AS latest_change_time,
         ROW_NUMBER() OVER (
           PARTITION BY se.workspace_id, se.endpoint_id, se.served_entity_id ORDER BY se.change_time DESC
         ) AS _rn
  FROM `system`.`serving`.`served_entities` se
),
entity_map AS (
  -- every entity the endpoint ever served, so traffic to a retired version still counts
  SELECT DISTINCT workspace_id, endpoint_id, served_entity_id FROM history
),
traffic AS (
  SELECT m.workspace_id, m.endpoint_id,
         SUM(CASE WHEN eu.request_time >= dateadd(day, -30, current_date())
                   AND eu.request_time < current_date() THEN 1 ELSE 0 END) AS total_requests,
         MAX(CASE WHEN eu.request_time < current_date() THEN CAST(eu.request_time AS DATE) END) AS last_request_date,
         -- any tracked request in the table's retention shows usage tracking is on
         COUNT(*) AS tracked_requests_ever
  FROM entity_map m
  JOIN `system`.`serving`.`endpoint_usage` eu
    ON eu.workspace_id = m.workspace_id AND eu.served_entity_id = m.served_entity_id
  GROUP BY m.workspace_id, m.endpoint_id
),
billed AS (
  SELECT workspace_id, usage_metadata.endpoint_id AS endpoint_id, SUM(usage_quantity) AS billed_dbus
  FROM `system`.`billing`.`usage`
  WHERE usage_metadata.endpoint_id IS NOT NULL
    AND upper(usage_unit) = 'DBU'
    AND usage_date >= dateadd(day, -30, current_date())
    AND usage_date < current_date()
  GROUP BY workspace_id, usage_metadata.endpoint_id
),
per_endpoint AS (
  -- one row per endpoint; only the entities in its current config are counted
  SELECT workspace_id, endpoint_id,
         MAX(endpoint_name) AS endpoint_name,
         COUNT(DISTINCT CASE WHEN endpoint_config_version = current_version OR current_version IS NULL
                             THEN served_entity_id END) AS served_entity_count,
         MAX(latest_change_time) AS latest_change_time,
         MAX(deleted_time) AS endpoint_delete_time,
         MIN(CASE WHEN entity_type = 'FOUNDATION_MODEL' THEN 1 ELSE 0 END) AS all_foundation
  FROM history
  WHERE _rn = 1
  GROUP BY workspace_id, endpoint_id
),
judged AS (
  SELECT pe.*,
         COALESCE(t.total_requests, 0) AS total_requests,
         t.last_request_date,
         COALESCE(t.tracked_requests_ever, 0) > 0 AS usage_tracked,
         COALESCE(b.billed_dbus, 0) AS billed_dbus
  FROM per_endpoint pe
  LEFT JOIN traffic t ON t.workspace_id = pe.workspace_id AND t.endpoint_id = pe.endpoint_id
  LEFT JOIN billed b ON b.workspace_id = pe.workspace_id AND b.endpoint_id = pe.endpoint_id
)
SELECT
  workspace_id,
  endpoint_id,
  endpoint_name,
  served_entity_count,
  latest_change_time,
  CAST(latest_change_time AS DATE) AS latest_change_date,
  total_requests,
  last_request_date,
  usage_tracked,
  ROUND(billed_dbus, 2) AS billed_dbus,
  CASE
    WHEN total_requests >= 10 THEN 'OK'
    WHEN NOT usage_tracked THEN 'NOT_ASSESSED'
    WHEN latest_change_time >= dateadd(day, -30, current_date()) THEN 'NOT_ASSESSED'
    WHEN billed_dbus = 0 THEN 'OK'   -- idle, but it costs nothing while idle
    WHEN total_requests = 0 THEN 'CRITICAL'
    ELSE 'WARN'
  END AS status,
  CASE
    WHEN total_requests >= 10 THEN NULL
    WHEN NOT usage_tracked THEN 'usage_tracking_off'
    WHEN latest_change_time >= dateadd(day, -30, current_date()) THEN 'changed_in_window'
  END AS not_assessed_reason
FROM judged
WHERE endpoint_delete_time IS NULL
  AND NOT (endpoint_name LIKE 'databricks-%' AND all_foundation = 1)
ORDER BY
  CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
  billed_dbus DESC, total_requests ASC
) q
"""
CHECKS["cost_failed_statement_waste"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_failed_statement_waste.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_failed_statement_waste.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
stmts AS (
  SELECT q.workspace_id, q.compute.warehouse_id AS warehouse_id, q.statement_id, q.execution_status,
         q.start_time, COALESCE(q.end_time, q.update_time, q.start_time) AS end_time,
         COALESCE(q.total_task_duration_ms, q.execution_duration_ms, 0) AS weight_ms
  FROM `system`.`query`.`history` q
  WHERE q.start_time >= current_date() - INTERVAL 30 DAYS
    AND q.start_time < current_date()
    AND q.compute.warehouse_id IS NOT NULL
),
attr AS (
  SELECT a.workspace_id,
         a.usage_metadata.warehouse_id       AS warehouse_id,
         a.usage_metadata.dbsql_statement_id AS statement_id,
         SUM(a.active_usage_quantity)                  AS dbus,
         SUM(a.active_usage_quantity * p.list_rate)    AS usd,
         SUM(CASE WHEN p.list_rate IS NULL AND upper(a.sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN a.active_usage_quantity ELSE 0 END) AS unpriced_q
  FROM `system`.`billing`.`attributed_usage` a
  LEFT JOIN price p
    ON  a.sku_name = p.sku_name AND a.cloud = p.cloud AND a.usage_unit = p.usage_unit
    AND a.end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR a.end_time < p.price_end_time)
  WHERE a.usage_date >= current_date() - INTERVAL 30 DAYS
    AND a.usage_date < current_date()
    AND upper(a.usage_unit) = 'DBU'
    AND a.usage_metadata.dbsql_statement_id IS NOT NULL
  GROUP BY a.workspace_id, a.usage_metadata.warehouse_id, a.usage_metadata.dbsql_statement_id
),
wh_attr AS (
  SELECT workspace_id, warehouse_id, COUNT(*) AS attributed_statements
  FROM attr GROUP BY workspace_id, warehouse_id
),
bill_hour AS (
  SELECT u.workspace_id, u.usage_metadata.warehouse_id AS warehouse_id,
         date_trunc('HOUR', u.usage_start_time) AS usage_hour,
         SUM(u.usage_quantity)                              AS hour_dbus,
         SUM(u.usage_quantity * COALESCE(p.list_rate, 0))   AS hour_usd,
         SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN u.usage_quantity ELSE 0 END)         AS hour_unpriced_q
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.warehouse_id IS NOT NULL
    AND u.usage_date >= current_date() - INTERVAL 30 DAYS
    AND u.usage_date < current_date()
  GROUP BY u.workspace_id, u.usage_metadata.warehouse_id, date_trunc('HOUR', u.usage_start_time)
),
wh_bill AS (
  SELECT workspace_id, warehouse_id, SUM(hour_usd) AS usd, SUM(hour_unpriced_q) AS unpriced_q
  FROM bill_hour
  GROUP BY workspace_id, warehouse_id
),
stmt_hours AS (
  -- a statement spanning hours is weighted into each by its overlap
  SELECT s.workspace_id, s.warehouse_id, s.statement_id, s.execution_status, bh.usage_hour,
         s.weight_ms *
         CASE WHEN date_trunc('HOUR', s.start_time) = date_trunc('HOUR', s.end_time) THEN 1.0
              ELSE (LEAST(unix_millis(s.end_time), unix_millis(bh.usage_hour) + 3600000)
                      - GREATEST(unix_millis(s.start_time), unix_millis(bh.usage_hour))) * 1.0
                   / GREATEST(unix_millis(s.end_time) - unix_millis(s.start_time), 1)
         END AS hour_weight_ms
  FROM stmts s
  JOIN bill_hour bh
    ON  bh.workspace_id = s.workspace_id AND bh.warehouse_id = s.warehouse_id
    AND bh.usage_hour BETWEEN date_trunc('HOUR', s.start_time) AND date_trunc('HOUR', s.end_time)
),
hour_totals AS (
  SELECT workspace_id, warehouse_id, usage_hour, SUM(hour_weight_ms) AS total_weight_ms
  FROM stmt_hours
  GROUP BY workspace_id, warehouse_id, usage_hour
),
hour_split AS (
  SELECT sh.workspace_id, sh.warehouse_id,
         SUM(CASE WHEN sh.execution_status = 'FAILED'
                  THEN bh.hour_dbus * sh.hour_weight_ms / ht.total_weight_ms END)       AS failed_dbus,
         SUM(CASE WHEN sh.execution_status = 'FAILED'
                  THEN bh.hour_usd * sh.hour_weight_ms / ht.total_weight_ms END)        AS failed_usd,
         SUM(CASE WHEN sh.execution_status = 'FAILED'
                  THEN bh.hour_unpriced_q * sh.hour_weight_ms / ht.total_weight_ms END) AS failed_unpriced_q,
         SUM(CASE WHEN sh.execution_status = 'CANCELED'
                  THEN bh.hour_usd * sh.hour_weight_ms / ht.total_weight_ms END)        AS canceled_usd,
         COUNT(DISTINCT CASE WHEN sh.execution_status = 'FAILED' AND sh.hour_weight_ms > 0
                             THEN sh.statement_id END)                                   AS failed_ran
  FROM stmt_hours sh
  JOIN hour_totals ht
    ON  ht.workspace_id = sh.workspace_id AND ht.warehouse_id = sh.warehouse_id
    AND ht.usage_hour = sh.usage_hour AND ht.total_weight_ms > 0
  JOIN bill_hour bh
    ON  bh.workspace_id = sh.workspace_id AND bh.warehouse_id = sh.warehouse_id
    AND bh.usage_hour = sh.usage_hour
  GROUP BY sh.workspace_id, sh.warehouse_id
),
per_wh AS (
  SELECT s.workspace_id, s.warehouse_id,
         COUNT(*) AS statements,
         SUM(CASE WHEN s.execution_status = 'FAILED'   THEN 1 ELSE 0 END) AS failed_statements,
         SUM(CASE WHEN s.execution_status = 'CANCELED' THEN 1 ELSE 0 END) AS canceled_statements,
         SUM(CASE WHEN s.execution_status = 'FAILED' AND a.statement_id IS NOT NULL THEN 1 ELSE 0 END) AS failed_with_cost,
         SUM(CASE WHEN s.execution_status = 'FAILED'   THEN a.dbus END)       AS failed_dbus_raw,
         SUM(CASE WHEN s.execution_status = 'FAILED'   THEN a.usd END)        AS failed_usd,
         SUM(CASE WHEN s.execution_status = 'FAILED'   THEN a.unpriced_q END) AS failed_unpriced_q,
         SUM(CASE WHEN s.execution_status = 'CANCELED' THEN a.usd END)        AS canceled_usd,
         SUM(a.usd)        AS attributed_usd,
         SUM(a.unpriced_q) AS unpriced_q
  FROM stmts s
  LEFT JOIN attr a ON a.workspace_id = s.workspace_id AND a.statement_id = s.statement_id
  GROUP BY s.workspace_id, s.warehouse_id
),
judged AS (
  SELECT p.*,
         CASE WHEN w.warehouse_id IS NOT NULL THEN 'statement'
              WHEN b.warehouse_id IS NOT NULL THEN 'run_time_share' END AS cost_basis,
         b.usd AS bill_usd, b.unpriced_q AS bill_unpriced_q,
         h.failed_dbus AS h_failed_dbus, h.failed_usd AS h_failed_usd,
         h.failed_unpriced_q AS h_failed_unpriced_q, h.canceled_usd AS h_canceled_usd, h.failed_ran
  FROM per_wh p
  LEFT JOIN wh_attr w    ON w.workspace_id = p.workspace_id AND w.warehouse_id = p.warehouse_id
  LEFT JOIN wh_bill b    ON b.workspace_id = p.workspace_id AND b.warehouse_id = p.warehouse_id
  LEFT JOIN hour_split h ON h.workspace_id = p.workspace_id AND h.warehouse_id = p.warehouse_id
  WHERE p.failed_statements > 0
),
priced AS (
  SELECT j.*,
         CASE WHEN cost_basis = 'statement' THEN failed_with_cost ELSE COALESCE(failed_ran, 0) END AS failed_used,
         CASE WHEN cost_basis = 'statement' THEN COALESCE(failed_dbus_raw, 0)
              ELSE COALESCE(h_failed_dbus, 0) END                                    AS failed_dbus_v,
         CASE WHEN cost_basis = 'statement' THEN COALESCE(failed_usd, 0)
              ELSE COALESCE(h_failed_usd, 0) END                                     AS waste_raw,
         CASE WHEN cost_basis = 'statement' THEN COALESCE(canceled_usd, 0)
              ELSE COALESCE(h_canceled_usd, 0) END                                   AS canceled_raw,
         CASE WHEN cost_basis = 'statement' THEN attributed_usd ELSE bill_usd END     AS base_usd,
         CASE WHEN cost_basis = 'statement' THEN COALESCE(unpriced_q, 0)
              ELSE COALESCE(bill_unpriced_q, 0) END                                  AS base_unpriced_q,
         CASE WHEN cost_basis IS NULL THEN 'no_billing_rows'
              WHEN cost_basis = 'statement' AND failed_unpriced_q > 0 THEN 'unpriced'
              WHEN cost_basis = 'run_time_share' AND h_failed_unpriced_q > 0 THEN 'unpriced'
         END AS na_reason
  FROM judged j
)
SELECT workspace_id, warehouse_id, statements, failed_statements, canceled_statements,
       failed_used AS failed_with_cost,
       CASE WHEN na_reason IS NULL OR na_reason = 'unpriced' THEN ROUND(failed_dbus_v, 2) END AS failed_dbus,
       CASE WHEN na_reason IS NULL THEN ROUND(waste_raw, 2) END                     AS est_wasted_usd_list,
       CASE WHEN na_reason IS NULL THEN ROUND(canceled_raw, 2) END                  AS est_canceled_usd_list,
       CASE WHEN na_reason IS NULL AND base_unpriced_q = 0
            THEN ROUND(COALESCE(base_usd, 0), 2) END                                AS est_attributed_usd_list,
       CASE WHEN na_reason IS NULL AND base_unpriced_q = 0
            THEN ROUND(waste_raw * 100.0 / NULLIF(base_usd, 0), 1) END              AS failed_cost_share_pct,
       CASE WHEN na_reason = 'no_billing_rows' THEN NULL
            WHEN base_unpriced_q > 0 THEN 'unpriced' ELSE 'priced' END              AS price_basis,
       cost_basis,
       CASE
         WHEN na_reason = 'no_billing_rows' THEN
           'failed statements found, but the warehouse has no billed usage in the window, so there is nothing to price'
         WHEN na_reason = 'unpriced' THEN
           'the failed statements'' usage has no list price, so there is no dollar figure (failed_dbus is shown)'
         WHEN cost_basis = 'run_time_share' THEN
           CONCAT_WS('; ',
             CONCAT(failed_statements, ' of ', statements, ' statements failed'),
             CONCAT(failed_used, ' of them ran in billed hours, priced at their share of the task time in those hours (no per-statement cost in billing.attributed_usage)'),
             CASE WHEN canceled_statements > 0
                  THEN CONCAT(canceled_statements, ' canceled, not counted as waste') END)
         ELSE
           CONCAT_WS('; ',
             CONCAT(failed_statements, ' of ', statements, ' statements failed'),
             CONCAT(failed_used, ' of them used billed compute'),
             CASE WHEN canceled_statements > 0
                  THEN CONCAT(canceled_statements, ' canceled, not counted as waste') END)
       END      AS waste_reason,
       na_reason      AS not_assessed_reason,
       CASE
         WHEN na_reason IS NOT NULL          THEN 'NOT_ASSESSED'
         WHEN waste_raw >= 50   THEN 'CRITICAL'
         WHEN waste_raw >= 5   THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM priced
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         est_wasted_usd_list DESC NULLS LAST, failed_statements DESC, workspace_id, warehouse_id
) q
"""
CHECKS["cost_dbsql_allocation_gap"] = r"""
-- generated from dbt/models/databricks_direct/cost/d_cost_dbsql_allocation_gap.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_dbsql_allocation_gap.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH raw_side AS (
  SELECT 'raw' AS source_kind,
         u.workspace_id, u.usage_metadata.warehouse_id AS warehouse_id,
         u.usage_date, u.cloud, u.billing_origin_product, u.usage_unit,
         SUM(u.usage_quantity) AS net_usage_quantity
  FROM `system`.`billing`.`usage` u
  WHERE u.usage_date >= dateadd(day, -30, current_date())
    AND u.usage_date < current_date()
    AND upper(u.usage_unit) = 'DBU'
    AND u.billing_origin_product = 'SQL'   -- DBSQL scope ONLY (attributed_usage doesn't cover jobs/DLT)
  GROUP BY u.workspace_id, u.usage_metadata.warehouse_id, u.usage_date, u.cloud, u.billing_origin_product, u.usage_unit
),
attributed_side AS (
  SELECT 'attributed' AS source_kind,
         a.workspace_id, a.usage_metadata.warehouse_id AS warehouse_id,
         a.usage_date, a.cloud, a.billing_origin_product, a.usage_unit,
         SUM(a.active_usage_quantity) AS net_usage_quantity
  FROM `system`.`billing`.`attributed_usage` a
  WHERE a.usage_date >= dateadd(day, -30, current_date())
    AND a.usage_date < current_date()
    AND upper(a.usage_unit) = 'DBU'
    AND a.billing_origin_product = 'SQL'   -- same DBSQL scope on both sides
  GROUP BY a.workspace_id, a.usage_metadata.warehouse_id, a.usage_date, a.cloud, a.billing_origin_product, a.usage_unit
),
both_sides AS (
  SELECT * FROM raw_side
  UNION ALL
  SELECT * FROM attributed_side
)
SELECT source_kind, workspace_id, warehouse_id, usage_date, cloud, billing_origin_product, usage_unit, net_usage_quantity,
       -- coverage_status: window-wide flag, same value on every row (see caveats).
       CASE
         WHEN SUM(CASE WHEN source_kind = 'attributed' THEN 1 ELSE 0 END) OVER () = 0
              THEN 'NOT_ASSESSED'
         ELSE 'assessed'
       END AS coverage_status
FROM both_sides
ORDER BY usage_date DESC, cloud, billing_origin_product, source_kind
) q
"""
CHECKS["compute_serving_endpoint_usage"] = r"""
-- generated from dbt/models/databricks_direct/serving_ai/d_compute_serving_endpoint_usage.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/serving_ai/compute_serving_endpoint_usage.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH entities AS (
  -- served_entities is change-history; keep only the latest config row per entity.
  SELECT
    workspace_id,
    endpoint_id,
    endpoint_name,
    served_entity_id,
    served_entity_name,
    entity_type,
    entity_name,
    entity_version
  FROM (
    SELECT
      se.*,
      ROW_NUMBER() OVER (
        PARTITION BY se.workspace_id, se.endpoint_id, se.served_entity_id
        ORDER BY se.change_time DESC
      ) AS _rn
    FROM `system`.`serving`.`served_entities` se
  )
  WHERE _rn = 1
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
cost_rollup AS (
  -- Pre-aggregated billing cost per serving endpoint per day (billing key = usage_metadata.endpoint_id).
  -- Grouped by (workspace_id, endpoint_id, usage_date) to match this query's endpoint/day join grain;
  -- billing.usage cannot break cost down to served_entity_id. Unique per key so the LEFT JOIN below is safe.
  SELECT u.workspace_id,
         u.usage_metadata.endpoint_id                     AS endpoint_id,
         u.usage_date                                     AS usage_date,
         SUM(u.usage_quantity)                            AS net_dbus,
         SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS est_usd_list,
         CASE
           WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                         THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END                                               AS price_basis
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.endpoint_id IS NOT NULL
    AND u.usage_date >= dateadd(day, -30, current_date())
    AND u.usage_date <  current_date()
  GROUP BY u.workspace_id, u.usage_metadata.endpoint_id, u.usage_date
),
finding AS (
  SELECT
    CAST(eu.request_time AS DATE)        AS usage_date,
    eu.workspace_id                      AS workspace_id,
    ent.endpoint_id                      AS endpoint_id,
    ent.endpoint_name AS endpoint_name,
    eu.served_entity_id                  AS served_entity_id,
    ent.served_entity_name AS served_entity_name,
    ent.entity_type                      AS entity_type,
    ent.entity_name                      AS entity_name,
    -- status_code is the HTTP-status column on endpoint_usage; classify 2xx as success,
    -- everything else as error. endpoint_usage is one row per request, so counts are
    -- COUNT(*)/CASE-based rather than a summed request_count column.
    SUM(CASE WHEN eu.status_code BETWEEN 200 AND 299 THEN 1 ELSE 0 END) AS success_requests,
    SUM(CASE WHEN eu.status_code IS NOT NULL AND NOT (eu.status_code BETWEEN 200 AND 299)
             THEN 1 ELSE 0 END)                                         AS error_requests,
    COUNT(*)                                                            AS total_requests,
    -- Token throughput is a separate magnitude from request counts (never summed with them).
    SUM(COALESCE(eu.input_token_count, 0))                             AS input_tokens,
    SUM(COALESCE(eu.output_token_count, 0))                            AS output_tokens
  FROM `system`.`serving`.`endpoint_usage` eu
  LEFT JOIN entities ent
    ON  eu.workspace_id     = ent.workspace_id
    AND eu.served_entity_id = ent.served_entity_id
  WHERE eu.request_time >= dateadd(day, -30, current_date())
    AND eu.request_time <  current_date()
  GROUP BY
    CAST(eu.request_time AS DATE),
    eu.workspace_id,
    ent.endpoint_id,
    ent.endpoint_name,
    eu.served_entity_id,
    ent.served_entity_name,
    ent.entity_type,
    ent.entity_name
),
ep_names AS (
  SELECT DISTINCT workspace_id, endpoint_id, endpoint_name FROM entities
)
SELECT
  COALESCE(finding.usage_date, cr.usage_date)       AS usage_date,
  COALESCE(finding.workspace_id, cr.workspace_id)   AS workspace_id,
  COALESCE(finding.endpoint_id, cr.endpoint_id)     AS endpoint_id,
  COALESCE(finding.endpoint_name, en.endpoint_name) AS endpoint_name,
  finding.served_entity_id,
  finding.served_entity_name,
  finding.entity_type,
  finding.entity_name,
  COALESCE(finding.success_requests, 0) AS success_requests,
  COALESCE(finding.error_requests, 0)   AS error_requests,
  COALESCE(finding.total_requests, 0)   AS total_requests,
  COALESCE(finding.input_tokens, 0)     AS input_tokens,
  COALESCE(finding.output_tokens, 0)    AS output_tokens,
  -- cost columns: the endpoint/day bill split by each served entity's share of that day's requests,
  -- so summing rows gives the bill once; a billed day with no tracked request is one row holding
  -- the whole bill, and a day with requests but no billing row has no cost (NULL), not $0
  CASE WHEN finding.usage_date IS NULL THEN cr.net_dbus
       ELSE cr.net_dbus * finding.total_requests
            / SUM(finding.total_requests) OVER (PARTITION BY finding.workspace_id, finding.endpoint_id, finding.usage_date)
  END AS net_dbus,
  CASE WHEN finding.usage_date IS NULL THEN cr.est_usd_list
       ELSE cr.est_usd_list * finding.total_requests
            / SUM(finding.total_requests) OVER (PARTITION BY finding.workspace_id, finding.endpoint_id, finding.usage_date)
  END AS est_usd_list,
  COALESCE(cr.price_basis, 'no_billing_row') AS price_basis
FROM finding
FULL OUTER JOIN cost_rollup cr
  ON  finding.workspace_id = cr.workspace_id
  AND finding.endpoint_id  = cr.endpoint_id
  AND finding.usage_date   = cr.usage_date
LEFT JOIN ep_names en
  ON en.workspace_id = cr.workspace_id AND en.endpoint_id = cr.endpoint_id
ORDER BY usage_date DESC, total_requests DESC
) q
"""
CHECKS["access_vector_search_traffic"] = r"""
-- generated from dbt/models/databricks_direct/governance_access/d_access_vector_search_traffic.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_vector_search_traffic.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT event_date,
       workspace_id,
       action_name,
       -- the audit log names the index in request_params.name; endpoint_name is optional
       COALESCE(request_params['name'], request_params['index_name'], request_params['endpoint_name']) AS vector_search_key,
       -- endpoint_name kept, unchanged, for compatibility -- no longer the group key (MAX per group).
       MAX(request_params['endpoint_name']) AS endpoint_name,
       COUNT(*) AS event_count,
       COUNT(DISTINCT COALESCE(request_params['name'], request_params['index_name'])) AS distinct_index_names
FROM `system`.`access`.`audit`
WHERE service_name = 'vectorSearch'
  AND action_name IN (
        'queryVectorIndex', 'queryVectorIndexNextPage',
        'queryVectorIndexRouteOptimized', 'queryVectorIndexNextPageRouteOptimized', 'scanVectorIndex',
        'scanVectorIndexRouteOptimized')
  AND event_date >= dateadd(day, -30, current_date())
  AND event_date < current_date()
GROUP BY event_date, workspace_id, action_name, COALESCE(request_params['name'], request_params['index_name'], request_params['endpoint_name'])
ORDER BY event_date DESC, vector_search_key, action_name
) q
"""
CHECKS["serving_endpoint_traffic_by_endpoint"] = r"""
-- generated from dbt/models/databricks_direct/serving_ai/d_serving_endpoint_traffic_by_endpoint.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/serving_ai/serving_endpoint_traffic_by_endpoint.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH entities AS (
  SELECT workspace_id, endpoint_id, endpoint_name, served_entity_id
  FROM (
    SELECT
      se.workspace_id, se.endpoint_id, se.endpoint_name, se.served_entity_id,
      ROW_NUMBER() OVER (
        PARTITION BY se.workspace_id, se.endpoint_id, se.served_entity_id
        ORDER BY se.change_time DESC
      ) AS _rn
    FROM `system`.`serving`.`served_entities` se
  )
  WHERE _rn = 1
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
cost_rollup AS (
  -- Pre-aggregated to EXACTLY this query's join grain (endpoint_id) so the LEFT JOIN is strictly 1:1
  -- and never multiplies result rows. endpoint_id is a globally-unique GUID -> keyed on id alone.
  SELECT
    u.usage_metadata.endpoint_id                     AS endpoint_id,
    SUM(u.usage_quantity)                            AS net_dbus,
    SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS est_usd_list,
    CASE
      WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                    THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
      WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
      ELSE 'priced'
    END                                               AS price_basis
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.endpoint_id IS NOT NULL
    AND u.usage_date >= dateadd(day, -30, current_date())
    AND u.usage_date <  current_date()
  GROUP BY u.usage_metadata.endpoint_id
)
SELECT
  ent.workspace_id                                  AS workspace_id,
  ent.endpoint_id                                   AS endpoint_id,
  ent.endpoint_name AS endpoint_name,
  COUNT(*)                                          AS request_count,
  SUM(CASE WHEN eu.status_code BETWEEN 200 AND 299 THEN 1 ELSE 0 END) AS success_requests,
  SUM(CASE WHEN eu.status_code IS NOT NULL AND NOT (eu.status_code BETWEEN 200 AND 299) THEN 1 ELSE 0 END) AS error_requests,
  SUM(COALESCE(eu.input_token_count, 0))            AS input_tokens,
  SUM(COALESCE(eu.output_token_count, 0))           AS output_tokens,
  MAX(CAST(eu.request_time AS DATE))                AS last_request_date,
  -- cost columns: rollup is constant within each endpoint group (1:1), surfaced via MAX() so the
  -- existing per-endpoint grain and COUNT(*) semantics are unchanged.
  -- no billing row (billing lag, or billed under another key) is no cost figure, not a priced $0
  MAX(cr.net_dbus)                                  AS net_dbus,
  MAX(cr.est_usd_list)                              AS est_usd_list,
  COALESCE(MAX(cr.price_basis), 'no_billing_row')   AS price_basis
FROM `system`.`serving`.`endpoint_usage` eu
LEFT JOIN entities ent
  ON  eu.workspace_id     = ent.workspace_id
  AND eu.served_entity_id = ent.served_entity_id
LEFT JOIN cost_rollup cr
  ON  ent.endpoint_id = cr.endpoint_id
WHERE eu.request_time >= dateadd(day, -30, current_date())
  AND eu.request_time <  current_date()
GROUP BY ent.workspace_id, ent.endpoint_id, ent.endpoint_name
ORDER BY request_count DESC, endpoint_id
) q
"""
CHECKS["compute_ai_gateway_usage"] = r"""
-- generated from dbt/models/databricks_direct/serving_ai/d_compute_ai_gateway_usage.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/serving_ai/compute_ai_gateway_usage.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT
  CAST(g.event_time AS DATE)           AS usage_date,
  g.workspace_id                       AS workspace_id,
  g.endpoint_name AS endpoint_name,
  g.requester                                  AS requester,
  COUNT(*)                                                                          AS total_requests,
  -- 2xx == success; 429 == rate-limit/quota; other == error (unconfirmed mapping, see caveats).
  SUM(CASE WHEN g.status_code BETWEEN 200 AND 299 THEN 1 ELSE 0 END)                AS success_requests,
  SUM(CASE WHEN g.status_code = 429 THEN 1 ELSE 0 END)                              AS rate_limited_requests,
  SUM(CASE WHEN g.status_code IS NOT NULL
            AND NOT (g.status_code BETWEEN 200 AND 299)
            AND g.status_code <> 429 THEN 1 ELSE 0 END)                            AS error_requests,
  -- Token throughput is its own magnitude, never blended with request counts.
  SUM(COALESCE(g.input_tokens, 0))                                                  AS input_tokens,
  SUM(COALESCE(g.output_tokens, 0))                                                 AS output_tokens,
  -- Latency via percentile (exact) -- percentile_approx differs between the two build paths.
  percentile(g.latency_ms, 0.5)                                                    AS p50_latency_ms,
  percentile(g.latency_ms, 0.95)                                                   AS p95_latency_ms,
  MAX(g.latency_ms)                                                                AS max_latency_ms
FROM `system`.`ai_gateway`.`usage` g
WHERE g.event_time >= dateadd(day, -30, current_date())
  AND g.event_time <  current_date()
GROUP BY
  CAST(g.event_time AS DATE),
  g.workspace_id,
  g.endpoint_name,
  g.requester
ORDER BY usage_date DESC, total_requests DESC
) q
"""
CHECKS["compute_last_activity"] = r"""
-- generated from dbt/models/databricks_direct/compute/d_compute_last_activity.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/compute/compute_last_activity.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
WITH warehouses AS (
  SELECT workspace_id, warehouse_id, warehouse_name, created_at
  FROM (
    SELECT workspace_id, warehouse_id, warehouse_name, delete_time,
           MIN(change_time) OVER (PARTITION BY workspace_id, warehouse_id) AS created_at,
           ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id ORDER BY change_time DESC) AS rn
    FROM `system`.`compute`.`warehouses`
  ) w
  WHERE rn = 1 AND delete_time IS NULL
),
clusters AS (
  SELECT workspace_id, cluster_id, cluster_name, create_time
  FROM (
    SELECT workspace_id, cluster_id, cluster_name, create_time, delete_time, cluster_source,
           ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC) AS rn
    FROM `system`.`compute`.`clusters`
  ) c
  WHERE rn = 1 AND delete_time IS NULL
    AND COALESCE(cluster_source, '') NOT IN ('JOB', 'PIPELINE', 'PIPELINE_MAINTENANCE', 'SQL', 'MODELS')
),
billed AS (
  SELECT workspace_id,
         usage_metadata.warehouse_id AS warehouse_id,
         usage_metadata.cluster_id   AS cluster_id,
         MAX(usage_date)             AS last_billed_date
  FROM `system`.`billing`.`usage`
  WHERE usage_quantity > 0
  GROUP BY workspace_id, usage_metadata.warehouse_id, usage_metadata.cluster_id
),
queried AS (
  SELECT workspace_id, compute.warehouse_id AS warehouse_id,
         MAX(CAST(start_time AS DATE)) AS last_query_date
  FROM `system`.`query`.`history`
  GROUP BY workspace_id, compute.warehouse_id
),
started AS (
  SELECT workspace_id, warehouse_id, MAX(CAST(event_time AS DATE)) AS last_start_date
  FROM `system`.`compute`.`warehouse_events`
  WHERE event_type IN ('STARTING', 'RUNNING')
  GROUP BY workspace_id, warehouse_id
),
job_runs AS (
  SELECT workspace_id, MAX(CAST(period_start_time AS DATE)) AS last_job_run_date
  FROM `system`.`lakeflow`.`job_run_timeline`
  GROUP BY workspace_id
),
audited AS (
  SELECT workspace_id,
         MAX(CASE WHEN user_identity.email LIKE '%@%' THEN event_date END) AS last_user_event_date
  FROM `system`.`access`.`audit`
  GROUP BY workspace_id
),
workspace_keys AS (
  SELECT workspace_id FROM `system`.`access`.`workspaces_latest` WHERE workspace_id IS NOT NULL
  UNION
  -- a cancelled workspace leaves the workspace list but not the bill: billing ids only stand in
  -- when the list itself is empty (not exported)
  SELECT workspace_id FROM billed
  WHERE workspace_id IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM `system`.`access`.`workspaces_latest`)
),
all_rows AS (
  SELECT 'warehouse' AS object_type, w.workspace_id, w.warehouse_id, CAST(NULL AS STRING) AS cluster_id,
         w.warehouse_name AS name, CAST(w.created_at AS DATE) AS created_date,
         q.last_query_date, s.last_start_date, CAST(NULL AS DATE) AS last_job_run_date,
         CAST(NULL AS DATE) AS last_user_event_date, b.last_billed_date,
         CAST(NULL AS BOOLEAN) AS audit_seen
  FROM warehouses w
  LEFT JOIN queried q ON q.workspace_id = w.workspace_id AND q.warehouse_id = w.warehouse_id
  LEFT JOIN started s ON s.workspace_id = w.workspace_id AND s.warehouse_id = w.warehouse_id
  LEFT JOIN (
    SELECT workspace_id, warehouse_id, MAX(last_billed_date) AS last_billed_date
    FROM billed WHERE warehouse_id IS NOT NULL GROUP BY workspace_id, warehouse_id
  ) b ON b.workspace_id = w.workspace_id AND b.warehouse_id = w.warehouse_id
  UNION ALL
  SELECT 'cluster', c.workspace_id, CAST(NULL AS STRING), c.cluster_id,
         c.cluster_name, CAST(c.create_time AS DATE),
         CAST(NULL AS DATE), CAST(NULL AS DATE), CAST(NULL AS DATE), CAST(NULL AS DATE), b.last_billed_date,
         CAST(NULL AS BOOLEAN)
  FROM clusters c
  LEFT JOIN (
    SELECT workspace_id, cluster_id, MAX(last_billed_date) AS last_billed_date
    FROM billed WHERE cluster_id IS NOT NULL GROUP BY workspace_id, cluster_id
  ) b ON b.workspace_id = c.workspace_id AND b.cluster_id = c.cluster_id
  UNION ALL
  SELECT 'workspace', k.workspace_id, CAST(NULL AS STRING), CAST(NULL AS STRING),
         n.workspace_name, CAST(n.create_time AS DATE),
         q.last_query_date, CAST(NULL AS DATE), j.last_job_run_date, a.last_user_event_date, b.last_billed_date,
         a.workspace_id IS NOT NULL
  FROM workspace_keys k
  LEFT JOIN `system`.`access`.`workspaces_latest` n ON n.workspace_id = k.workspace_id
  LEFT JOIN (
    SELECT workspace_id, MAX(last_query_date) AS last_query_date FROM queried GROUP BY workspace_id
  ) q ON q.workspace_id = k.workspace_id
  LEFT JOIN job_runs j ON j.workspace_id = k.workspace_id
  LEFT JOIN audited a ON a.workspace_id = k.workspace_id
  LEFT JOIN (
    SELECT workspace_id, MAX(last_billed_date) AS last_billed_date FROM billed GROUP BY workspace_id
  ) b ON b.workspace_id = k.workspace_id
),
dated AS (
  SELECT *,
         -- a warehouse is in use when it runs queries or starts; one billed with neither is not
         CASE WHEN object_type = 'warehouse' THEN GREATEST(last_query_date, last_start_date)
              ELSE GREATEST(last_query_date, last_start_date, last_job_run_date, last_user_event_date,
                            last_billed_date)
         END AS last_activity_date
  FROM all_rows
)
SELECT object_type, workspace_id, warehouse_id, cluster_id, name, created_date,
       last_query_date, last_start_date, last_job_run_date, last_user_event_date, last_billed_date,
       audit_seen, last_activity_date,
       datediff(current_date(), last_activity_date) AS days_since_activity,
       CASE
         WHEN last_activity_date IS NULL AND created_date >= dateadd(day, -90, current_date())
              THEN 'NOT_ASSESSED'
         WHEN last_activity_date IS NULL THEN 'WARN'
         WHEN datediff(current_date(), last_activity_date) >= 90 THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN last_activity_date IS NULL AND created_date >= dateadd(day, -90, current_date())
              THEN 'created_recently'
       END AS not_assessed_reason
FROM dated
ORDER BY CASE status WHEN 'WARN' THEN 0 WHEN 'OK' THEN 1 ELSE 2 END,
         last_activity_date ASC NULLS FIRST, object_type, workspace_id, name
) q
"""
CHECKS["lakeflow_job_run_changes"] = r"""
-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_job_run_changes.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/jobs_pipelines/lakeflow_job_run_changes.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH all_rows AS (
  -- A repair keeps the run_id: rows after an earlier end row belong to the next attempt.
  SELECT workspace_id, job_id, run_id, period_start_time, period_end_time, result_state,
         NULLIF(queue_duration_seconds, 0) AS queue_s_reported,
         NULLIF(setup_duration_seconds, 0) AS setup_s_reported,
         NULLIF(run_duration_seconds, 0)   AS run_s_reported,
         MAX(CASE WHEN result_state IS NOT NULL THEN period_end_time END) OVER (
           PARTITION BY workspace_id, job_id, run_id ORDER BY period_start_time
           ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_attempt_end
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
),
attempts AS (
  SELECT workspace_id, job_id, run_id,
         MIN(period_start_time)                                              AS attempt_start,
         MAX(CASE WHEN result_state IS NOT NULL THEN period_end_time END)    AS attempt_end,
         MAX(CASE WHEN result_state IS NOT NULL THEN result_state END)       AS result_state,
         MAX(CASE WHEN result_state IS NOT NULL THEN queue_s_reported END)   AS queue_s_reported,
         MAX(CASE WHEN result_state IS NOT NULL THEN setup_s_reported END)   AS setup_s_reported,
         MAX(CASE WHEN result_state IS NOT NULL THEN run_s_reported END)     AS run_s_reported
  FROM all_rows
  GROUP BY workspace_id, job_id, run_id, prev_attempt_end
),
run_final AS (
  -- one row per RUN on its latest attempt, timed on that attempt alone, once it ended before today
  SELECT * FROM (
    SELECT workspace_id, job_id, run_id,
           MIN(attempt_start) OVER (PARTITION BY workspace_id, job_id, run_id) AS run_start,
           attempt_end AS run_end, result_state AS final_result_state, queue_s_reported, setup_s_reported,
           COALESCE(run_s_reported, timestampdiff(SECOND, attempt_start, attempt_end)) AS run_s
    FROM attempts
    QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id, run_id ORDER BY attempt_start DESC) = 1
  ) f
  WHERE run_end IS NOT NULL AND run_end < date_trunc('DAY', current_timestamp())
),
run_cluster AS (
  -- first compute id of the run's latest-starting task (max_by picks it by period_start_time, not
  -- an arbitrary task's); NULL on serverless and pre-Dec-2025 rows
  SELECT workspace_id, job_id, job_run_id AS run_id,
         max_by(try_element_at(compute_ids, 1), period_start_time) AS cluster_id
  FROM `system`.`lakeflow`.`job_task_run_timeline`
  WHERE period_start_time >= dateadd(day, -30, current_date())
  GROUP BY workspace_id, job_id, job_run_id
),
ranked AS (
  SELECT f.*, c.cluster_id,
         ROW_NUMBER() OVER (
           PARTITION BY f.workspace_id, f.job_id ORDER BY f.run_start DESC, f.run_id DESC
         ) AS recency_rank
  FROM run_final f
  LEFT JOIN run_cluster c
    ON  c.workspace_id = f.workspace_id AND c.job_id = f.job_id AND c.run_id = f.run_id
),
job_p90 AS (
  SELECT workspace_id, job_id, percentile(run_s, 0.9) AS run_s_p90
  FROM ranked
  GROUP BY workspace_id, job_id
),
latest AS (
  SELECT * FROM ranked WHERE recency_rank = 1
),
candidate AS (
  -- the latest run either failed, or ran above the job's own p90 of every completed run
  SELECT l.workspace_id, l.job_id, l.run_id AS latest_run_id, l.run_start AS latest_start,
         l.cluster_id AS latest_cluster_id, l.recency_rank AS latest_rank,
         l.queue_s_reported AS latest_queue_s, l.setup_s_reported AS latest_setup_s,
         l.run_s AS latest_run_s
  FROM latest l
  JOIN job_p90 p ON p.workspace_id = l.workspace_id AND p.job_id = l.job_id
  WHERE l.final_result_state IN ('FAILED', 'ERROR', 'TIMED_OUT')
     OR (p.run_s_p90 IS NOT NULL AND l.run_s > p.run_s_p90)
),
baseline_pick AS (
  -- the nearest SUCCEEDED run strictly before the latest one; MIN(recency_rank) = most recent
  SELECT c.workspace_id, c.job_id, MIN(r.recency_rank) AS baseline_rank
  FROM candidate c
  JOIN ranked r
    ON  r.workspace_id = c.workspace_id AND r.job_id = c.job_id
    AND r.final_result_state = 'SUCCEEDED' AND r.recency_rank > c.latest_rank
  GROUP BY c.workspace_id, c.job_id
),
no_baseline AS (
  -- a candidate job with no SUCCEEDED run anywhere in the window: a marker row instead of no row
  -- at all, so the caller can tell "no successful run" apart from "not a candidate, nothing wrong"
  SELECT c.workspace_id, c.job_id, c.latest_run_id
  FROM candidate c
  LEFT JOIN baseline_pick bp ON bp.workspace_id = c.workspace_id AND bp.job_id = c.job_id
  WHERE bp.job_id IS NULL
),
baseline AS (
  SELECT r.workspace_id, r.job_id, r.run_id AS baseline_run_id, r.run_start AS baseline_start,
         r.cluster_id AS baseline_cluster_id
  FROM ranked r
  JOIN baseline_pick bp
    ON  bp.workspace_id = r.workspace_id AND bp.job_id = r.job_id AND bp.baseline_rank = r.recency_rank
),
good5 AS (
  -- the job's last 5 SUCCEEDED runs strictly before the latest run, for a robust duration baseline
  SELECT r.workspace_id, r.job_id, r.queue_s_reported, r.setup_s_reported, r.run_s,
         ROW_NUMBER() OVER (PARTITION BY r.workspace_id, r.job_id ORDER BY r.run_start DESC) AS good_rank
  FROM ranked r
  JOIN candidate c
    ON  c.workspace_id = r.workspace_id AND c.job_id = r.job_id
    AND r.final_result_state = 'SUCCEEDED' AND r.recency_rank > c.latest_rank
),
good5_median AS (
  SELECT workspace_id, job_id,
         percentile(queue_s_reported, 0.5) AS median_queue_s,
         percentile(setup_s_reported, 0.5) AS median_setup_s,
         percentile(run_s, 0.5)            AS median_run_s
  FROM good5
  WHERE good_rank <= 5
  GROUP BY workspace_id, job_id
),
pair AS (
  -- one row per candidate job that has a baseline (a job with none is left out - see caveats)
  SELECT c.workspace_id, c.job_id, c.latest_run_id, c.latest_start, c.latest_cluster_id,
         c.latest_queue_s, c.latest_setup_s, c.latest_run_s,
         b.baseline_run_id, b.baseline_start, b.baseline_cluster_id,
         g.median_queue_s, g.median_setup_s, g.median_run_s
  FROM candidate c
  JOIN baseline b ON b.workspace_id = c.workspace_id AND b.job_id = c.job_id
  LEFT JOIN good5_median g ON g.workspace_id = c.workspace_id AND g.job_id = c.job_id
),
run_points AS (
  -- the two points (baseline, latest) of every candidate job, unpivoted - every as-of join below
  -- runs once against this shared set instead of twice (once per point)
  SELECT workspace_id, job_id, 'baseline' AS which, baseline_run_id AS run_id,
         baseline_start AS run_start, baseline_cluster_id AS cluster_id
  FROM pair
  UNION ALL
  SELECT workspace_id, job_id, 'latest', latest_run_id, latest_start, latest_cluster_id
  FROM pair
),
cluster_asof AS (
  -- the system.compute.clusters row valid AT the run's own start (SCD2 as-of), not the cluster's
  -- current shape; a job cluster's first row lands after its run starts, so else its first row
  SELECT p.workspace_id, p.job_id, p.which, c.dbr_version, c.worker_node_type,
         CASE WHEN c.max_autoscale_workers IS NOT NULL
                   AND c.max_autoscale_workers <> COALESCE(c.min_autoscale_workers, c.max_autoscale_workers)
              THEN CONCAT(CAST(COALESCE(c.min_autoscale_workers, 0) AS STRING), '-', CAST(c.max_autoscale_workers AS STRING))
              WHEN c.worker_count IS NOT NULL THEN CAST(c.worker_count AS STRING)
              ELSE NULL END AS worker_shape
  FROM run_points p
  LEFT JOIN `system`.`compute`.`clusters` c
    ON  c.workspace_id = p.workspace_id AND c.cluster_id = p.cluster_id
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY p.workspace_id, p.job_id, p.which
    ORDER BY CASE WHEN c.change_time <= p.run_start THEN 0 ELSE 1 END,
             CASE WHEN c.change_time <= p.run_start THEN c.change_time END DESC,
             c.change_time
  ) = 1
),
job_asof AS (
  -- the job definition's own SCD2 row valid AT the run's own start
  SELECT p.workspace_id, p.job_id, p.which, j.change_time AS job_version
  FROM run_points p
  LEFT JOIN `system`.`lakeflow`.`jobs` j
    ON  j.workspace_id = p.workspace_id AND j.job_id = p.job_id AND j.change_time <= p.run_start
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY p.workspace_id, p.job_id, p.which ORDER BY j.change_time DESC
  ) = 1
),
lineage_tables AS (
  -- this run's own distinct source tables (entity_run_id = the run's run_id); event_time bound to
  -- the check's own window so this scans recent lineage only, same as every other source here
  SELECT DISTINCT p.workspace_id, p.job_id, p.which,
         concat_ws('.', l.source_table_catalog, l.source_table_schema, l.source_table_name) AS source_table
  FROM run_points p
  JOIN `system`.`access`.`table_lineage` l
    ON  l.workspace_id = p.workspace_id AND l.entity_run_id = p.run_id
    AND l.event_time >= dateadd(day, -30, current_date())
  WHERE l.source_table_full_name IS NOT NULL
),
lineage_asof AS (
  SELECT workspace_id, job_id, which,
         -- array_join (not concat_ws on an array -- DuckDB stringifies it as "[a, b]" instead of joining)
         array_join(array_sort(collect_set(source_table)), ', ') AS upstream
  FROM lineage_tables
  GROUP BY workspace_id, job_id, which
),
input_bytes_table AS (
  -- each source table's own latest snapshot at/before the run's date
  SELECT lt.workspace_id, lt.job_id, lt.which, m.active_bytes,
         ROW_NUMBER() OVER (
           PARTITION BY lt.workspace_id, lt.job_id, lt.which, lt.source_table ORDER BY m.snapshot_date DESC
         ) AS rn
  FROM lineage_tables lt
  JOIN run_points p ON p.workspace_id = lt.workspace_id AND p.job_id = lt.job_id AND p.which = lt.which
  JOIN `system`.`storage`.`table_metrics_history` m
    ON  m.catalog_name = split_part(lt.source_table, '.', 1)
    AND m.schema_name   = split_part(lt.source_table, '.', 2)
    AND m.table_name    = split_part(lt.source_table, '.', 3)
    AND m.snapshot_date <= date(p.run_start)
    AND m.snapshot_date >= dateadd(day, -30, current_date())
),
bytes_asof AS (
  SELECT workspace_id, job_id, which, SUM(active_bytes) AS input_bytes
  FROM input_bytes_table
  WHERE rn = 1
  GROUP BY workspace_id, job_id, which
),
cluster_wide AS (
  SELECT workspace_id, job_id,
         MAX(CASE WHEN which = 'baseline' THEN dbr_version END)     AS baseline_dbr_version,
         MAX(CASE WHEN which = 'latest'   THEN dbr_version END)     AS latest_dbr_version,
         MAX(CASE WHEN which = 'baseline' THEN worker_node_type END) AS baseline_worker_node_type,
         MAX(CASE WHEN which = 'latest'   THEN worker_node_type END) AS latest_worker_node_type,
         MAX(CASE WHEN which = 'baseline' THEN worker_shape END)     AS baseline_worker_shape,
         MAX(CASE WHEN which = 'latest'   THEN worker_shape END)     AS latest_worker_shape
  FROM cluster_asof
  GROUP BY workspace_id, job_id
),
job_wide AS (
  SELECT workspace_id, job_id,
         MAX(CASE WHEN which = 'baseline' THEN job_version END) AS baseline_job_version,
         MAX(CASE WHEN which = 'latest'   THEN job_version END) AS latest_job_version
  FROM job_asof
  GROUP BY workspace_id, job_id
),
lineage_wide AS (
  SELECT workspace_id, job_id,
         MAX(CASE WHEN which = 'baseline' THEN upstream END) AS baseline_upstream,
         MAX(CASE WHEN which = 'latest'   THEN upstream END) AS latest_upstream
  FROM lineage_asof
  GROUP BY workspace_id, job_id
),
bytes_wide AS (
  SELECT workspace_id, job_id,
         MAX(CASE WHEN which = 'baseline' THEN input_bytes END) AS baseline_input_bytes,
         MAX(CASE WHEN which = 'latest'   THEN input_bytes END) AS latest_input_bytes
  FROM bytes_asof
  GROUP BY workspace_id, job_id
),
latest_jobs AS (
  SELECT workspace_id, job_id, name AS job_name
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),
base AS (
  SELECT p.workspace_id, p.job_id, jn.job_name, p.baseline_run_id, p.latest_run_id,
         cw.baseline_dbr_version, cw.latest_dbr_version,
         cw.baseline_worker_node_type, cw.latest_worker_node_type,
         cw.baseline_worker_shape, cw.latest_worker_shape,
         jw.baseline_job_version, jw.latest_job_version,
         lw.baseline_upstream, lw.latest_upstream,
         bw.baseline_input_bytes, bw.latest_input_bytes,
         p.median_queue_s, p.latest_queue_s, p.median_setup_s, p.latest_setup_s,
         p.median_run_s, p.latest_run_s
  FROM pair p
  LEFT JOIN cluster_wide cw ON cw.workspace_id = p.workspace_id AND cw.job_id = p.job_id
  LEFT JOIN job_wide jw     ON jw.workspace_id = p.workspace_id AND jw.job_id = p.job_id
  LEFT JOIN lineage_wide lw ON lw.workspace_id = p.workspace_id AND lw.job_id = p.job_id
  LEFT JOIN bytes_wide bw   ON bw.workspace_id = p.workspace_id AND bw.job_id = p.job_id
  LEFT JOIN latest_jobs jn  ON jn.workspace_id = p.workspace_id AND jn.job_id = p.job_id
),
changes AS (
SELECT workspace_id, job_id, job_name, 'job_definition' AS attribute, baseline_run_id, latest_run_id,
       'prior_success' AS baseline_kind,
       CAST(baseline_job_version AS STRING) AS baseline_value, CAST(latest_job_version AS STRING) AS latest_value,
       (baseline_job_version IS NOT NULL AND latest_job_version IS NOT NULL
        AND baseline_job_version <> latest_job_version) AS changed
FROM base
UNION ALL
SELECT workspace_id, job_id, job_name, 'runtime_version', baseline_run_id, latest_run_id, 'prior_success',
       baseline_dbr_version, latest_dbr_version,
       (baseline_dbr_version IS NOT NULL AND latest_dbr_version IS NOT NULL
        AND baseline_dbr_version <> latest_dbr_version)
FROM base
UNION ALL
SELECT workspace_id, job_id, job_name, 'worker_node_type', baseline_run_id, latest_run_id, 'prior_success',
       baseline_worker_node_type, latest_worker_node_type,
       (baseline_worker_node_type IS NOT NULL AND latest_worker_node_type IS NOT NULL
        AND baseline_worker_node_type <> latest_worker_node_type)
FROM base
UNION ALL
SELECT workspace_id, job_id, job_name, 'worker_count', baseline_run_id, latest_run_id, 'prior_success',
       baseline_worker_shape, latest_worker_shape,
       (baseline_worker_shape IS NOT NULL AND latest_worker_shape IS NOT NULL
        AND baseline_worker_shape <> latest_worker_shape)
FROM base
UNION ALL
SELECT workspace_id, job_id, job_name, 'upstream_tables', baseline_run_id, latest_run_id, 'prior_success',
       baseline_upstream, latest_upstream,
       (baseline_upstream IS NOT NULL AND latest_upstream IS NOT NULL AND baseline_upstream <> latest_upstream)
FROM base
UNION ALL
SELECT workspace_id, job_id, job_name, 'input_bytes', baseline_run_id, latest_run_id, 'prior_success',
       CAST(baseline_input_bytes AS STRING), CAST(latest_input_bytes AS STRING),
       (baseline_input_bytes IS NOT NULL AND latest_input_bytes IS NOT NULL
        AND baseline_input_bytes <> latest_input_bytes)
FROM base
UNION ALL
SELECT workspace_id, job_id, job_name, 'queue_s', baseline_run_id, latest_run_id, 'prior_success',
       CAST(ROUND(median_queue_s, 0) AS STRING), CAST(latest_queue_s AS STRING),
       (median_queue_s IS NOT NULL AND latest_queue_s IS NOT NULL AND ROUND(median_queue_s, 0) <> latest_queue_s)
FROM base
UNION ALL
SELECT workspace_id, job_id, job_name, 'setup_s', baseline_run_id, latest_run_id, 'prior_success',
       CAST(ROUND(median_setup_s, 0) AS STRING), CAST(latest_setup_s AS STRING),
       (median_setup_s IS NOT NULL AND latest_setup_s IS NOT NULL AND ROUND(median_setup_s, 0) <> latest_setup_s)
FROM base
UNION ALL
SELECT workspace_id, job_id, job_name, 'run_s', baseline_run_id, latest_run_id, 'prior_success',
       CAST(ROUND(median_run_s, 0) AS STRING), CAST(latest_run_s AS STRING),
       (median_run_s IS NOT NULL AND latest_run_s IS NOT NULL AND ROUND(median_run_s, 0) <> latest_run_s)
FROM base
UNION ALL
SELECT nb.workspace_id, nb.job_id, jn.job_name, CAST(NULL AS STRING) AS attribute,
       CAST(NULL AS STRING) AS baseline_run_id, nb.latest_run_id,
       'no_success_in_window' AS baseline_kind,
       CAST(NULL AS STRING) AS baseline_value, CAST(NULL AS STRING) AS latest_value,
       CAST(NULL AS BOOLEAN) AS changed
FROM no_baseline nb
LEFT JOIN latest_jobs jn ON jn.workspace_id = nb.workspace_id AND jn.job_id = nb.job_id
)
SELECT * FROM changes
ORDER BY changed DESC, workspace_id, job_id,
         CASE attribute
           WHEN 'job_definition'    THEN 0
           WHEN 'runtime_version'   THEN 1
           WHEN 'worker_node_type'  THEN 2
           WHEN 'worker_count'      THEN 3
           WHEN 'upstream_tables'   THEN 4
           WHEN 'input_bytes'       THEN 5
           WHEN 'queue_s'           THEN 6
           WHEN 'setup_s'           THEN 7
           ELSE 8
         END
) q
"""
CHECKS["query_top_by_cost"] = r"""
-- generated from dbt/models/databricks_direct/performance/d_query_top_by_cost.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/performance/query_top_by_cost.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
bill_hour AS (
  -- one row per SQL warehouse per billed hour: its list-price dollars in that hour
  SELECT u.workspace_id, u.usage_metadata.warehouse_id AS warehouse_id,
         date_trunc('HOUR', u.usage_start_time) AS usage_hour,
         SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS hour_usd
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.warehouse_id IS NOT NULL
    AND u.usage_date >= current_date() - INTERVAL 30 DAYS
    AND u.usage_date < current_date()
  GROUP BY u.workspace_id, u.usage_metadata.warehouse_id, date_trunc('HOUR', u.usage_start_time)
),
attr_stmt AS (
  -- Databricks' own attribution per statement: its active DBUs, never the warehouse's idle time;
  -- unknown (NULL) when any of it has no list price
  SELECT a.workspace_id, a.usage_metadata.warehouse_id AS warehouse_id,
         a.usage_metadata.dbsql_statement_id AS statement_id,
         CASE WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(a.sku_name) NOT LIKE '%FREE_USAGE%'
                            THEN 1 ELSE 0 END) > 0 THEN NULL
              ELSE SUM(a.active_usage_quantity * COALESCE(p.list_rate, 0)) END AS attributed_usd
  FROM `system`.`billing`.`attributed_usage` a
  LEFT JOIN price p
    ON  a.sku_name = p.sku_name AND a.cloud = p.cloud AND a.usage_unit = p.usage_unit
    AND a.end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR a.end_time < p.price_end_time)
  WHERE upper(a.usage_unit) = 'DBU'
    AND a.usage_metadata.dbsql_statement_id IS NOT NULL
    AND a.usage_date >= current_date() - INTERVAL 30 DAYS
    AND a.usage_date < current_date()
  GROUP BY a.workspace_id, a.usage_metadata.warehouse_id, a.usage_metadata.dbsql_statement_id
),
attr_wh AS (
  -- the warehouses Databricks attributes statements on in the window
  SELECT DISTINCT workspace_id, warehouse_id FROM attr_stmt
),
wh_total AS (
  -- each warehouse's own total window spend - the denominator for share_of_warehouse_cost
  SELECT workspace_id, warehouse_id, SUM(hour_usd) AS warehouse_usd_list
  FROM bill_hour
  GROUP BY workspace_id, warehouse_id
),
stmts AS (
  SELECT q.workspace_id,
         q.compute.warehouse_id                          AS warehouse_id,
         q.statement_id,
         q.statement_type,
         q.statement_text,
         q.executed_by,
         q.query_source,
         q.start_time,
         COALESCE(q.end_time, q.update_time, q.start_time) AS end_time,
         COALESCE(q.total_task_duration_ms, q.execution_duration_ms, 0) AS weight_ms,
         COALESCE(q.execution_duration_ms, 0)             AS execution_ms,
         COALESCE(q.read_bytes, 0)                        AS read_bytes,
         COALESCE(q.spilled_local_bytes, 0)               AS spilled_local_bytes,
         COALESCE(q.read_files, 0)                        AS read_files,
         COALESCE(q.pruned_files, 0)                      AS pruned_files,
         CASE WHEN q.from_result_cache THEN 1 ELSE 0 END  AS cached
  FROM `system`.`query`.`history` q
  WHERE q.start_time >= current_date() - INTERVAL 30 DAYS
    AND q.start_time < current_date()
    AND q.compute.warehouse_id IS NOT NULL
),
stmt_hours AS (
  -- every billed hour a statement's own [start_time, end_time) overlaps, in milliseconds so a
  -- sub-second statement still gets weight (unix_timestamp truncates to whole seconds and would
  -- floor a sub-second statement to zero overlap) - full weight when it fits in one hour, otherwise
  -- its share of that hour by overlap_ms over duration_ms - a 3-hour query is priced across all 3
  -- hours, not billed whole to its start hour
  SELECT s.workspace_id, s.warehouse_id, s.statement_id, bh.usage_hour,
         GREATEST(unix_millis(s.end_time) - unix_millis(s.start_time), 1) AS duration_ms,
         CASE WHEN date_trunc('HOUR', s.start_time) = date_trunc('HOUR', s.end_time)
                THEN GREATEST(unix_millis(s.end_time) - unix_millis(s.start_time), 1)
              ELSE LEAST(unix_millis(s.end_time), unix_millis(bh.usage_hour) + 3600000)
                     - GREATEST(unix_millis(s.start_time), unix_millis(bh.usage_hour))
         END AS overlap_ms
  FROM stmts s
  JOIN bill_hour bh
    ON  bh.workspace_id = s.workspace_id AND bh.warehouse_id = s.warehouse_id
    AND bh.usage_hour BETWEEN date_trunc('HOUR', s.start_time) AND date_trunc('HOUR', s.end_time)
),
stmt_hour_weight AS (
  SELECT sh.workspace_id, sh.warehouse_id, sh.statement_id, sh.usage_hour,
         s.weight_ms * sh.overlap_ms / sh.duration_ms AS hour_weight_ms
  FROM stmt_hours sh
  JOIN stmts s
    ON  s.workspace_id = sh.workspace_id AND s.warehouse_id = sh.warehouse_id
    AND s.statement_id = sh.statement_id
),
hour_totals AS (
  -- total statement weight per warehouse per hour - the denominator for each statement-hour's share
  SELECT workspace_id, warehouse_id, usage_hour, SUM(hour_weight_ms) AS total_weight_ms
  FROM stmt_hour_weight
  GROUP BY workspace_id, warehouse_id, usage_hour
),
stmt_cost AS (
  -- each statement's total cost: its share of every billed hour it overlaps, summed
  SELECT w.workspace_id, w.warehouse_id, w.statement_id,
         SUM(CASE WHEN ht.total_weight_ms > 0 AND bh.hour_usd IS NOT NULL
                  THEN bh.hour_usd * w.hour_weight_ms / ht.total_weight_ms END) AS stmt_cost_usd
  FROM stmt_hour_weight w
  JOIN hour_totals ht
    ON  ht.workspace_id = w.workspace_id AND ht.warehouse_id = w.warehouse_id AND ht.usage_hour = w.usage_hour
  LEFT JOIN bill_hour bh
    ON  bh.workspace_id = w.workspace_id AND bh.warehouse_id = w.warehouse_id AND bh.usage_hour = w.usage_hour
  GROUP BY w.workspace_id, w.warehouse_id, w.statement_id
),
costed AS (
  SELECT s.*,
         CASE WHEN aw.warehouse_id IS NOT NULL
              THEN CASE WHEN ast.statement_id IS NULL THEN 0 ELSE ast.attributed_usd END
              ELSE sc.stmt_cost_usd END AS stmt_cost_usd,
         CASE WHEN aw.warehouse_id IS NOT NULL THEN 'attributed_usage' ELSE 'hour_split' END AS cost_basis
  FROM stmts s
  LEFT JOIN stmt_cost sc
    ON  sc.workspace_id = s.workspace_id AND sc.warehouse_id = s.warehouse_id
    AND sc.statement_id = s.statement_id
  LEFT JOIN attr_wh aw
    ON  aw.workspace_id = s.workspace_id AND aw.warehouse_id = s.warehouse_id
  LEFT JOIN attr_stmt ast
    ON  ast.workspace_id = s.workspace_id AND ast.statement_id = s.statement_id
),
hashed AS (
  -- the de-valued-text sha2 hash (query_costly_statements_grouped's own recipe), NULL when the
  -- text is not real (never NULL, never '<REDACTED>')
  SELECT c.*,
         CASE WHEN c.statement_text IS NULL OR c.statement_text = '<REDACTED>' THEN NULL
              ELSE sha2(
           regexp_replace(
             regexp_replace(regexp_replace(
                  regexp_replace(c.statement_text, '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}', '<email>'),
                  concat(chr(39), '[^', chr(39), ']*', chr(39)), '?'), '(^|[^A-Za-z0-9_.])[0-9]+([.][0-9]+)?', '$1?'),
             '[?]( *, *[?])+', '?'),
           256)
         END AS text_hash
  FROM costed c
),
origin AS (
  SELECT h.*,
         CASE
           WHEN h.text_hash IS NOT NULL                            THEN 'text hash'
           WHEN h.query_source.job_info.job_id IS NOT NULL         THEN 'job'
           WHEN h.query_source.pipeline_info.pipeline_id IS NOT NULL THEN 'pipeline'
           WHEN h.query_source.dashboard_id IS NOT NULL            THEN 'dashboard'
           WHEN h.query_source.legacy_dashboard_id IS NOT NULL     THEN 'dashboard'
           WHEN h.query_source.genie_space_id IS NOT NULL          THEN 'Genie space'
           WHEN h.query_source.alert_id IS NOT NULL                THEN 'alert'
           WHEN h.query_source.notebook_id IS NOT NULL             THEN 'notebook'
           WHEN h.query_source.sql_query_id IS NOT NULL            THEN 'saved query'
           ELSE 'ad-hoc, text hidden'
         END AS group_kind,
         CASE
           WHEN h.text_hash IS NOT NULL                        THEN h.text_hash
           WHEN h.query_source.job_info.job_id IS NOT NULL        THEN h.query_source.job_info.job_id
           WHEN h.query_source.pipeline_info.pipeline_id IS NOT NULL THEN h.query_source.pipeline_info.pipeline_id
           WHEN h.query_source.dashboard_id IS NOT NULL        THEN h.query_source.dashboard_id
           WHEN h.query_source.legacy_dashboard_id IS NOT NULL THEN h.query_source.legacy_dashboard_id
           WHEN h.query_source.genie_space_id IS NOT NULL      THEN h.query_source.genie_space_id
           WHEN h.query_source.alert_id IS NOT NULL            THEN h.query_source.alert_id
           WHEN h.query_source.notebook_id IS NOT NULL              THEN h.query_source.notebook_id
           WHEN h.query_source.sql_query_id IS NOT NULL        THEN h.query_source.sql_query_id
           ELSE
             h.executed_by
         END AS group_id,
         -- the group's real origin, independent of group_kind - a "text hash" group still names
         -- its dashboard/job/notebook/etc. source here when one is captured; NULL only when the
         -- statement is genuinely ad-hoc with no query_source at all
         CASE
           WHEN h.query_source.job_info.job_id IS NOT NULL           THEN 'job'
           WHEN h.query_source.pipeline_info.pipeline_id IS NOT NULL THEN 'pipeline'
           WHEN h.query_source.dashboard_id IS NOT NULL              THEN 'dashboard'
           WHEN h.query_source.legacy_dashboard_id IS NOT NULL       THEN 'dashboard'
           WHEN h.query_source.genie_space_id IS NOT NULL            THEN 'Genie space'
           WHEN h.query_source.alert_id IS NOT NULL                  THEN 'alert'
           WHEN h.query_source.notebook_id IS NOT NULL               THEN 'notebook'
           WHEN h.query_source.sql_query_id IS NOT NULL              THEN 'saved query'
         END AS source_kind,
         CASE
           WHEN h.query_source.job_info.job_id IS NOT NULL           THEN h.query_source.job_info.job_id
           WHEN h.query_source.pipeline_info.pipeline_id IS NOT NULL THEN h.query_source.pipeline_info.pipeline_id
           WHEN h.query_source.dashboard_id IS NOT NULL              THEN h.query_source.dashboard_id
           WHEN h.query_source.legacy_dashboard_id IS NOT NULL       THEN h.query_source.legacy_dashboard_id
           WHEN h.query_source.genie_space_id IS NOT NULL            THEN h.query_source.genie_space_id
           WHEN h.query_source.alert_id IS NOT NULL                  THEN h.query_source.alert_id
           WHEN h.query_source.notebook_id IS NOT NULL               THEN h.query_source.notebook_id
           WHEN h.query_source.sql_query_id IS NOT NULL              THEN h.query_source.sql_query_id
         END AS source_id
  FROM hashed h
),
user_agg AS (
  SELECT workspace_id, warehouse_id, group_kind, group_id, statement_type, executed_by,
         COUNT(*) AS user_runs
  FROM origin
  GROUP BY workspace_id, warehouse_id, group_kind, group_id, statement_type, executed_by
),
top_user_pick AS (
  -- the group's most frequent user, masked to DEC-66.3's format
  SELECT workspace_id, warehouse_id, group_kind, group_id, statement_type,
         executed_by AS top_user
  FROM user_agg
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id, group_kind, group_id, statement_type
                             ORDER BY user_runs DESC, executed_by) = 1
),
sample_pick AS (
  -- one representative statement per group: its latest run, still in Databricks' query history -
  -- its own source names the group's
  -- real origin too, even for a "text hash" group grouped across statements from more than one
  -- source
  SELECT workspace_id, warehouse_id, group_kind, group_id, statement_type,
         statement_id AS sample_statement_id, source_kind, source_id
  FROM origin
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id, group_kind, group_id, statement_type
                             ORDER BY start_time DESC, statement_id) = 1
),
agg AS (
  SELECT workspace_id, warehouse_id, group_kind, group_id, statement_type,
         COUNT(*)                                    AS runs,
         -- COALESCE so a group whose runs all carry a NULL identity still counts as 1 user, not 0
         -- (COUNT(DISTINCT executed_by) alone drops every NULL from the count)
         COUNT(DISTINCT COALESCE(executed_by, '__no_identity__')) AS distinct_users,
         SUM(stmt_cost_usd)                           AS est_cost_usd_list,
         MAX(cost_basis)                              AS cost_basis,
         SUM(execution_ms)                            AS total_exec_ms,
         CAST(AVG(execution_ms) AS BIGINT)            AS avg_exec_ms,
         MAX(execution_ms)                            AS max_exec_ms,
         CAST(percentile(execution_ms, 0.5) AS BIGINT)  AS p50_duration_ms,
         CAST(percentile(execution_ms, 0.95) AS BIGINT) AS p95_duration_ms,
         SUM(read_bytes)                              AS read_bytes,
         SUM(spilled_local_bytes)                     AS spilled_local_bytes,
         SUM(read_files)                              AS read_files,
         SUM(pruned_files)                            AS pruned_files,
         ROUND(SUM(cached) * 1.0 / COUNT(*), 3)       AS from_result_cache_share,
         MIN(start_time)                              AS first_seen,
         MAX(start_time)                              AS last_seen
  FROM origin
  GROUP BY workspace_id, warehouse_id, group_kind, group_id, statement_type
)
SELECT a.workspace_id,
       a.warehouse_id,
       a.group_kind,
       a.group_id,
       sp.source_kind,
       sp.source_id,
       a.statement_type,
       a.runs,
       a.distinct_users,
       tu.top_user,
       ROUND(a.est_cost_usd_list, 2) AS est_cost_usd_list,
       a.cost_basis,
       CASE WHEN wt.warehouse_usd_list > 0
            THEN ROUND(a.est_cost_usd_list / wt.warehouse_usd_list, 3) END AS share_of_warehouse_cost,
       a.total_exec_ms,
       a.avg_exec_ms,
       a.max_exec_ms,
       a.p50_duration_ms,
       a.p95_duration_ms,
       a.read_bytes,
       a.spilled_local_bytes,
       a.read_files,
       a.pruned_files,
       a.from_result_cache_share,
       sp.sample_statement_id,
       a.first_seen,
       a.last_seen,
       CASE
         WHEN a.est_cost_usd_list >= 200
           OR (wt.warehouse_usd_list > 0 AND a.est_cost_usd_list / wt.warehouse_usd_list >= 0.4)
           THEN 'CRITICAL'
         WHEN a.est_cost_usd_list >= 50
           OR (wt.warehouse_usd_list > 0 AND a.est_cost_usd_list / wt.warehouse_usd_list >= 0.2)
           THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM agg a
JOIN top_user_pick tu
  ON  tu.workspace_id = a.workspace_id AND tu.warehouse_id = a.warehouse_id
  AND tu.group_kind = a.group_kind AND tu.group_id IS NOT DISTINCT FROM a.group_id
  AND tu.statement_type IS NOT DISTINCT FROM a.statement_type
JOIN sample_pick sp
  ON  sp.workspace_id = a.workspace_id AND sp.warehouse_id = a.warehouse_id
  AND sp.group_kind = a.group_kind AND sp.group_id IS NOT DISTINCT FROM a.group_id
  AND sp.statement_type IS NOT DISTINCT FROM a.statement_type
LEFT JOIN wh_total wt
  ON  wt.workspace_id = a.workspace_id AND wt.warehouse_id = a.warehouse_id
ORDER BY est_cost_usd_list DESC NULLS LAST, a.runs DESC, a.workspace_id, a.warehouse_id,
         a.group_kind, a.group_id
LIMIT 5000
) q
"""
CHECKS["query_slices"] = r"""
-- generated from dbt/models/databricks_direct/performance/d_query_slices.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/performance/query_slices.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
attr AS (
  SELECT a.usage_metadata.dbsql_statement_id AS statement_id,
         SUM(a.active_usage_quantity)               AS dbus,
         SUM(a.active_usage_quantity * p.list_rate) AS usd
  FROM `system`.`billing`.`attributed_usage` a
  LEFT JOIN price p
    ON  a.sku_name = p.sku_name AND a.cloud = p.cloud AND a.usage_unit = p.usage_unit
    AND a.end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR a.end_time < p.price_end_time)
  WHERE a.usage_date >= current_date() - INTERVAL 30 DAYS
    AND upper(a.usage_unit) = 'DBU'
    AND a.usage_metadata.dbsql_statement_id IS NOT NULL
  GROUP BY a.usage_metadata.dbsql_statement_id
),
stmts AS (
  SELECT q.workspace_id, q.compute.warehouse_id AS warehouse_id, date(q.start_time) AS usage_date,
         CASE WHEN q.query_source.job_info.job_id IS NOT NULL THEN 'job'
              WHEN q.query_source.pipeline_info.pipeline_id IS NOT NULL THEN 'pipeline'
              WHEN q.query_source.dashboard_id IS NOT NULL OR q.query_source.legacy_dashboard_id IS NOT NULL THEN 'dashboard'
              WHEN q.query_source.genie_space_id IS NOT NULL THEN 'genie'
              WHEN q.query_source.alert_id IS NOT NULL THEN 'alert'
              WHEN q.query_source.notebook_id IS NOT NULL THEN 'notebook'
              WHEN q.query_source.sql_query_id IS NOT NULL OR q.client_application ILIKE '%sql editor%' THEN 'sql_editor'
              WHEN q.client_application IS NOT NULL THEN 'tool'
              ELSE 'other' END AS origin,
         q.query_source.job_info.job_id AS job_id,
         q.query_source.pipeline_info.pipeline_id AS pipeline_id,
         q.query_source.notebook_id AS notebook_id,
         to_json(q.query_tags) AS query_tags,
         CASE WHEN q.execution_status = 'FAILED' THEN 1 ELSE 0 END AS failed,
         COALESCE(q.total_duration_ms, 0) / 1000.0 AS total_s,
         (COALESCE(q.waiting_at_capacity_duration_ms, 0) + COALESCE(q.waiting_for_compute_duration_ms, 0)) / 1000.0 AS queued_s,
         COALESCE(q.execution_duration_ms, 0) / 1000.0 AS exec_s,
         COALESCE(q.waiting_at_capacity_duration_ms, 0) / 1000.0 AS slot_wait_s,
         COALESCE(q.waiting_for_compute_duration_ms, 0) / 1000.0 AS provision_s,
         COALESCE(q.spilled_local_bytes, 0) / 1e9 AS spilled_gb,
         COALESCE(q.shuffle_read_bytes, 0) / 1e9 AS shuffle_gb,
         COALESCE(q.read_bytes, 0) / 1e9 AS read_gb,
         COALESCE(q.read_bytes, 0) * COALESCE(q.read_io_cache_percent, 0) / 100.0 / 1e9 AS cache_read_gb,
         CASE WHEN q.from_result_cache THEN 1 ELSE 0 END AS result_cache_runs,
         COALESCE(q.result_fetch_duration_ms, 0) / 1000.0 AS fetch_s,
         a.dbus, a.usd
  FROM `system`.`query`.`history` q
  LEFT JOIN attr a ON a.statement_id = q.statement_id
  WHERE q.start_time >= current_date() - INTERVAL 30 DAYS
    AND q.start_time < current_date()
    AND q.compute.warehouse_id IS NOT NULL
)
SELECT usage_date,
       'warehouse' AS resource_type,
       concat(workspace_id, ':', warehouse_id) AS resource_key,
       workspace_id, warehouse_id, origin, job_id, pipeline_id, notebook_id, query_tags,
       COUNT(*) AS runs,
       SUM(failed) AS failed,
       ROUND(SUM(total_s), 1) AS total_s,
       ROUND(SUM(queued_s), 1) AS queued_s,
       ROUND(SUM(exec_s), 1) AS exec_s,
       ROUND(SUM(slot_wait_s), 1) AS slot_wait_s,
       ROUND(SUM(provision_s), 1) AS provision_s,
       ROUND(SUM(spilled_gb), 6) AS spilled_gb,
       ROUND(SUM(shuffle_gb), 6) AS shuffle_gb,
       ROUND(SUM(read_gb), 6) AS read_gb,
       ROUND(SUM(cache_read_gb), 6) AS cache_read_gb,
       SUM(result_cache_runs) AS result_cache_runs,
       ROUND(SUM(fetch_s), 1) AS fetch_s,
       ROUND(SUM(dbus), 6) AS attributed_dbus,
       ROUND(SUM(usd), 4) AS attributed_usd_list
FROM stmts
GROUP BY usage_date, workspace_id, warehouse_id, origin, job_id, pipeline_id, notebook_id, query_tags
ORDER BY usage_date DESC, workspace_id, warehouse_id, runs DESC
) q
"""
CHECKS["access_broad_grants"] = r"""
-- generated from dbt/models/databricks_direct/governance_access/d_access_broad_grants.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/governance_access/access_broad_grants.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
WITH table_grants AS (
  SELECT
    'TABLE'                                                    AS securable_type,
    CONCAT(TABLE_CATALOG, '.', TABLE_SCHEMA, '.', TABLE_NAME)  AS securable,
    GRANTEE                                                    AS raw_grantee,
    PRIVILEGE_TYPE                                             AS privilege_type,
    (INHERITED_FROM IS NOT NULL)                               AS is_inherited,
    INHERITED_FROM                                             AS inherited_from,
    TABLE_CATALOG                                              AS cat,
    TABLE_SCHEMA                                               AS sch
  FROM `system`.`information_schema`.`table_privileges`
),
catalog_grants AS (
  SELECT
    'CATALOG'                       AS securable_type,
    CATALOG_NAME                    AS securable,
    GRANTEE                         AS raw_grantee,
    PRIVILEGE_TYPE                  AS privilege_type,
    CAST(NULL AS BOOLEAN)           AS is_inherited,   -- not tracked by this source; see caveats
    CAST(NULL AS STRING)            AS inherited_from,
    CATALOG_NAME                    AS cat,
    CAST(NULL AS STRING)            AS sch
  FROM `system`.`information_schema`.`catalog_privileges`
),
schema_grants AS (
  SELECT
    'SCHEMA'                                AS securable_type,
    CONCAT(CATALOG_NAME, '.', SCHEMA_NAME)  AS securable,
    GRANTEE                                 AS raw_grantee,
    PRIVILEGE_TYPE                          AS privilege_type,
    CAST(NULL AS BOOLEAN)                   AS is_inherited,   -- not tracked by this source; see caveats
    CAST(NULL AS STRING)                    AS inherited_from,
    CATALOG_NAME                            AS cat,
    SCHEMA_NAME                             AS sch
  FROM `system`.`information_schema`.`schema_privileges`
),
grants AS (
  SELECT * FROM table_grants
  UNION ALL
  SELECT * FROM catalog_grants
  UNION ALL
  SELECT * FROM schema_grants
),
own_grants AS (
  SELECT g.*
  FROM grants g
  LEFT JOIN (SELECT DISTINCT cat, raw_grantee, privilege_type FROM catalog_grants) pc
    ON g.securable_type <> 'CATALOG' AND pc.cat = g.cat
   AND pc.raw_grantee = g.raw_grantee AND pc.privilege_type = g.privilege_type
  LEFT JOIN (SELECT DISTINCT cat, sch, raw_grantee, privilege_type FROM schema_grants) ps
    ON g.securable_type = 'TABLE' AND ps.cat = g.cat AND ps.sch = g.sch
   AND ps.raw_grantee = g.raw_grantee AND ps.privilege_type = g.privilege_type
  WHERE pc.cat IS NULL AND ps.cat IS NULL
),
classified AS (
  SELECT g.*,
         -- field heuristic, no dedicated principal-type column exists in either source (caveats)
         CASE
           WHEN g.raw_grantee IS NULL OR g.raw_grantee = '__REDACTED__'                    THEN 'UNKNOWN'
           WHEN LOWER(TRIM(g.raw_grantee)) IN ('account users', 'all account users', 'users') THEN 'ALL_USERS'
           WHEN g.raw_grantee RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN 'SERVICE_PRINCIPAL'
           WHEN g.raw_grantee LIKE '%@%'                                                   THEN 'USER'
           ELSE 'GROUP'
         END AS grantee_type
  FROM own_grants g
),
flagged AS (
  SELECT c.*,
         (c.privilege_type IN ('ALL PRIVILEGES', 'ALL_PRIVILEGES'))                 AS is_all_privileges,
         (c.grantee_type = 'ALL_USERS')                                             AS is_all_users,
         -- MANAGE lets the holder grant itself anything and drop the object, whoever holds it
         (c.grantee_type <> 'ALL_USERS' AND c.privilege_type IN ('MANAGE', 'OWNERSHIP')) AS is_manage,
         -- write, ANY scope: MODIFY, any CREATE_* privilege, or ALL PRIVILEGES. Judged on its own
         -- (no scope limit) so a write grant to every user in the account is CRITICAL no matter
         -- where it is granted; see is_write_at_catalog below for the scope-limited rule that
         -- still applies to a named (non all-users) principal.
         (c.privilege_type IN ('MODIFY', 'MANAGE', 'OWNERSHIP')
           OR c.privilege_type LIKE 'CREATE%'
           OR c.privilege_type IN ('ALL PRIVILEGES', 'ALL_PRIVILEGES'))             AS is_write,
         -- write, catalog or schema scope (caveats): same privilege set, but only counts as broad
         -- on its own (for a named, non all-users principal) when granted at CATALOG or SCHEMA
         -- scope -- a schema-level write grant reaches every table the schema holds now and
         -- later, the same leverage as a catalog-level one.
         (c.securable_type IN ('CATALOG', 'SCHEMA')
           AND (c.privilege_type = 'MODIFY'
             OR c.privilege_type LIKE 'CREATE%'
             OR c.privilege_type IN ('ALL PRIVILEGES', 'ALL_PRIVILEGES')))          AS is_write_at_catalog_or_schema
  FROM classified c
)
SELECT
  f.securable_type,
  f.securable,
  f.grantee_type,
  CASE
    WHEN f.raw_grantee IS NULL OR f.raw_grantee = '__REDACTED__' THEN f.raw_grantee
    WHEN f.grantee_type = 'SERVICE_PRINCIPAL'                    THEN f.raw_grantee
    WHEN f.grantee_type = 'USER'
      THEN (
        f.raw_grantee
      )
    ELSE f.raw_grantee   -- GROUP or ALL_USERS: not personal data, shown in full
  END AS grantee,
  f.privilege_type,
  f.is_inherited,
  f.inherited_from,
  CASE
    WHEN f.is_all_privileges AND f.grantee_type = 'USER'      THEN 'ALL PRIVILEGES granted to a person, not a group'
    WHEN f.is_all_users AND f.is_write                       THEN 'write privilege granted to every user in the account'
    WHEN f.is_all_privileges                                THEN 'ALL PRIVILEGES granted to a group or service principal'
    WHEN f.is_all_users                                      THEN 'granted to every user in the account'
    WHEN f.is_manage                                         THEN 'MANAGE (near-ownership) privilege granted to a named principal'
    WHEN f.is_write_at_catalog_or_schema                      THEN 'write privilege granted at catalog or schema level'
    ELSE NULL
  END AS reason,
  -- status: a WARN must have a lever, same discipline as every other verdict in this library.
  -- ALL PRIVILEGES to a person, or a write privilege granted to everyone (any scope), is the
  -- highest-leverage case; ALL PRIVILEGES to a group is usually an admin group.
  CASE
    WHEN f.is_all_privileges AND f.grantee_type = 'USER' THEN 'CRITICAL'
    WHEN f.is_all_users AND f.is_write            THEN 'CRITICAL'
    WHEN f.is_all_privileges OR f.is_all_users OR f.is_manage OR f.is_write_at_catalog_or_schema THEN 'WARN'
    ELSE 'OK'   -- unreachable: the WHERE clause below only ever admits a broad row
  END AS status
FROM flagged f
WHERE f.is_all_privileges OR f.is_all_users OR f.is_manage OR f.is_write_at_catalog_or_schema
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
         securable_type, securable, privilege_type, grantee
LIMIT 100000
) q
"""
CHECKS["access_grants_inventory"] = r"""
-- generated from dbt/models/databricks_direct/governance_access/d_access_grants_inventory.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_grants_inventory.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT object_scope, PRIVILEGE_TYPE,
       GRANTEE AS GRANTEE,
       grant_count, distinct_objects
FROM (
  SELECT 'TABLE' AS object_scope, PRIVILEGE_TYPE, GRANTEE,
         COUNT(*) AS grant_count,
         COUNT(DISTINCT TABLE_CATALOG || '.' || TABLE_SCHEMA || '.' || TABLE_NAME) AS distinct_objects
  FROM `system`.`information_schema`.`table_privileges`
  GROUP BY 1, 2, 3
  UNION ALL
  SELECT 'CATALOG' AS object_scope, PRIVILEGE_TYPE, GRANTEE,
         COUNT(*) AS grant_count,
         COUNT(DISTINCT CATALOG_NAME) AS distinct_objects
  FROM `system`.`information_schema`.`catalog_privileges`
  GROUP BY 1, 2, 3
)
ORDER BY object_scope, PRIVILEGE_TYPE, GRANTEE
) q
"""
CHECKS["access_grants_inventory_extended"] = r"""
-- generated from dbt/models/databricks_direct/governance_access/d_access_grants_inventory_extended.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_grants_inventory_extended.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT 'SCHEMA' AS object_scope, PRIVILEGE_TYPE,
       GRANTEE AS GRANTEE,
       COUNT(*) AS grant_count
FROM `system`.`information_schema`.`schema_privileges` GROUP BY 1,2,GRANTEE
UNION ALL
SELECT 'CONNECTION', PRIVILEGE_TYPE,
       GRANTEE,
       COUNT(*)
FROM `system`.`information_schema`.`connection_privileges` GROUP BY 1,2,GRANTEE
UNION ALL
SELECT 'CREDENTIAL', PRIVILEGE_TYPE,
       GRANTEE,
       COUNT(*)
FROM `system`.`information_schema`.`credential_privileges` GROUP BY 1,2,GRANTEE
UNION ALL
SELECT 'EXTERNAL_LOCATION', PRIVILEGE_TYPE,
       GRANTEE,
       COUNT(*)
FROM `system`.`information_schema`.`external_location_privileges` GROUP BY 1,2,GRANTEE
ORDER BY object_scope, PRIVILEGE_TYPE, GRANTEE
) q
"""
CHECKS["access_runas_escalation"] = r"""
-- generated from dbt/models/databricks_direct/governance_access/d_access_runas_escalation.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_runas_escalation.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH pair_history AS (
  -- Every (run_by, run_as) pair this table has EVER seen, over its FULL retention (not just
  -- 30) - used to tell a genuinely new pair from one that simply didn't run earlier in a
  -- short window. Heavier than the rest of this query; see caveats.
  SELECT identity_metadata.run_by AS run_by, identity_metadata.run_as AS run_as,
         MIN(event_time) AS ever_first_event_time
  FROM `system`.`access`.`audit`
  WHERE identity_metadata.run_by IS NOT NULL
    AND identity_metadata.run_as IS NOT NULL
    AND identity_metadata.run_by <> identity_metadata.run_as
  GROUP BY identity_metadata.run_by, identity_metadata.run_as
),
audit_start AS (
  -- with audit history starting inside the window (a 30-day export), every pair looks new
  SELECT MIN(event_date) AS first_audit_date FROM `system`.`access`.`audit`
),
window_pairs AS (
  SELECT workspace_id, service_name, action_name,
         identity_metadata.run_by AS run_by,
         identity_metadata.run_as AS run_as,
         COUNT(*) AS event_count,
         MIN(event_time) AS first_event_time,
         MAX(event_time) AS last_event_time
  FROM `system`.`access`.`audit`
  WHERE identity_metadata.run_by IS NOT NULL
    AND identity_metadata.run_as IS NOT NULL
    AND identity_metadata.run_by <> identity_metadata.run_as
    AND event_date >= current_date() - INTERVAL 30 DAYS
    AND event_date < current_date()
  GROUP BY workspace_id, service_name, action_name, identity_metadata.run_by, identity_metadata.run_as
),
classified AS (
  -- Shape (service-principal vs human) and novelty, not raw event volume, drive status below.
  SELECT w.*,
         w.run_by RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' AS run_by_is_service_principal,
         w.run_as RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' AS run_as_is_service_principal,
         CASE WHEN st.first_audit_date >= current_date() - INTERVAL 30 DAYS THEN NULL
              ELSE (h.ever_first_event_time IS NULL
                    OR h.ever_first_event_time >= current_date() - INTERVAL 30 DAYS)
         END AS newly_seen_pair
  FROM window_pairs w
  CROSS JOIN audit_start st
  LEFT JOIN pair_history h ON h.run_by = w.run_by AND h.run_as = w.run_as
)
SELECT workspace_id, service_name, action_name,
       run_by AS run_by,
       run_as AS run_as,
       CASE WHEN run_by_is_service_principal AND NOT run_as_is_service_principal THEN 'service_principal_to_user'
            WHEN NOT run_by_is_service_principal AND run_as_is_service_principal THEN 'user_to_service_principal'
            WHEN NOT run_by_is_service_principal AND NOT run_as_is_service_principal THEN 'user_to_user'
            ELSE 'service_principal_to_service_principal' END AS delegation_kind,
       newly_seen_pair,
       event_count, first_event_time, last_event_time,
       -- status: band on NOVELTY and SHAPE, not raw volume or delegation kind alone. A known pair
       -- (any kind) is OK; a newly seen pair is WARN, or CRITICAL when it is a human assuming
       -- another identity either direction. Zero rows does not mean OK - see caveats.
       CASE
         WHEN newly_seen_pair IS NULL THEN 'NOT_ASSESSED'
         WHEN newly_seen_pair
              AND ((run_by_is_service_principal AND NOT run_as_is_service_principal)
                   OR (NOT run_by_is_service_principal AND NOT run_as_is_service_principal))
              THEN 'CRITICAL'
         WHEN newly_seen_pair THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM classified
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END, event_count DESC
) q
"""
CHECKS["access_login_concentration"] = r"""
-- generated from dbt/models/databricks_direct/governance_access/d_access_login_concentration.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_login_concentration.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH base AS (
  SELECT workspace_id,
         COALESCE(user_identity.email, user_identity.subject_name) AS raw_identity,
         source_ip_address, service_name, action_name, event_time, response
  FROM `system`.`access`.`audit`
  WHERE service_name = 'accounts'
    AND action_name RLIKE '(?i)login|authenticat'
    AND event_date >= current_date() - INTERVAL 30 DAYS
    AND event_date < current_date()
  QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY event_time DESC) = 1
),
principal_ips AS (
  SELECT workspace_id, raw_identity, COUNT(DISTINCT source_ip_address) AS distinct_source_ips
  FROM base
  GROUP BY workspace_id, raw_identity
)
SELECT b.workspace_id,
       b.raw_identity AS principal,
       b.source_ip_address, b.service_name, b.action_name,
       COUNT(*) AS event_count,
       SUM(CASE WHEN b.response.status_code = 200 THEN 1 ELSE 0 END) AS success_count,
       SUM(CASE WHEN b.response.status_code IS NOT NULL AND b.response.status_code <> 200 THEN 1 ELSE 0 END) AS non_success_count,
       SUM(CASE WHEN b.response.status_code IS NULL THEN 1 ELSE 0 END) AS unknown_status_count,
       MAX(pi.distinct_source_ips) AS distinct_source_ips,
       MIN(b.event_time) AS first_event_time, MAX(b.event_time) AS last_event_time,
       -- status: worst-first band on REAL non-success (failed) auth attempts per principal+IP+action; a NULL status never counts (field heuristic; 5 / 20).
       CASE
         WHEN SUM(CASE WHEN b.response.status_code IS NOT NULL AND b.response.status_code <> 200 THEN 1 ELSE 0 END) >= 20 THEN 'CRITICAL'
         WHEN SUM(CASE WHEN b.response.status_code IS NOT NULL AND b.response.status_code <> 200 THEN 1 ELSE 0 END) >= 5 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM base b
LEFT JOIN principal_ips pi
  ON  b.workspace_id IS NOT DISTINCT FROM pi.workspace_id
  AND b.raw_identity  IS NOT DISTINCT FROM pi.raw_identity
GROUP BY b.workspace_id, b.raw_identity, b.source_ip_address, b.service_name, b.action_name
ORDER BY non_success_count DESC
) q
"""
CHECKS["access_network_inbound_denials"] = r"""
-- generated from dbt/models/databricks_direct/governance_access/d_access_network_inbound_denials.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_network_inbound_denials.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT workspace_id, policy_outcome, rule_label, request_path,
       authenticated_as AS authenticated_as,
       source.ip AS source_ip,
       COUNT(*) AS denial_count,
       MIN(event_time) AS first_event_time, MAX(event_time) AS last_event_time,
       -- status: worst-first band on inbound denial volume per rule+path+principal+IP (field heuristic; 10 / 50).
       CASE
         WHEN COUNT(*) >= 50 THEN 'CRITICAL'
         WHEN COUNT(*) >= 10 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`access`.`inbound_network`
WHERE event_time >= current_timestamp() - INTERVAL 30 DAYS
GROUP BY 1, 2, 3, 4, authenticated_as, 6
ORDER BY denial_count DESC
) q
"""
CHECKS["access_network_outbound_denials"] = r"""
-- generated from dbt/models/databricks_direct/governance_access/d_access_network_outbound_denials.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_network_outbound_denials.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT workspace_id, network_source_type, destination_type, access_type, destination,
       dns_event.rcode                AS dns_rcode,
       storage_event.rejection_reason AS storage_rejection_reason,
       COUNT(*) AS denial_count,
       MIN(event_time) AS first_event_time, MAX(event_time) AS last_event_time,
       -- status: worst-first band on outbound denial volume per workspace+source+destination combo (field heuristic; 10 / 50).
       CASE
         -- dry-run denials are logged, not blocked: a policy being tried out, not broken egress
         WHEN upper(access_type) = 'DRY_RUN_DENIAL' THEN 'OK'
         WHEN COUNT(*) >= 50 THEN 'CRITICAL'
         WHEN COUNT(*) >= 10 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`access`.`outbound_network`
WHERE event_time >= current_timestamp() - INTERVAL 30 DAYS
GROUP BY 1, 2, 3, 4, 5, 6, 7
ORDER BY denial_count DESC
) q
"""
CHECKS["access_admin_role_change_events"] = r"""
-- generated from dbt/models/databricks_direct/governance_access/d_access_admin_role_change_events.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_admin_role_change_events.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT workspace_id, service_name, action_name,
       actor AS actor,
       granted_to, permission, workspace_name,
       event_count, failed_event_count, distinct_source_ips, workspace_token_iplist_actions,
       first_event_time, last_event_time,
       -- status: a handful of action names change the account/admin role itself and read CRITICAL
       -- at any count; every other allow-listed action keeps the worst-first volume band
       -- (field heuristic; 20 / 100).
       CASE
         WHEN action_name IN ('changeAccountOwner', 'setAccountAdmin', 'removeAccountAdmin', 'setAdmin', 'removeAdmin') THEN 'CRITICAL'
         -- A token permission grant: WARN anywhere, CRITICAL in prod (the prod words of env_patterns).
         WHEN action_name = 'changeDbTokenAcl'
              AND lower(workspace_name) RLIKE '(^|[^a-z0-9])(prod|prd|production|live)([^a-z0-9]|$)' THEN 'CRITICAL'
         WHEN action_name = 'changeDbTokenAcl' THEN 'WARN'
         WHEN event_count >= 100 THEN 'CRITICAL'
         WHEN event_count >= 20 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM (
  -- Unity Catalog events are logged at account level (workspace 0); the workspace they came from
  -- is in request_params
  SELECT CASE WHEN service_name = 'unityCatalog' AND workspace_id = '0'
              THEN COALESCE(request_params['workspace_id'], workspace_id) ELSE workspace_id END AS workspace_id,
         service_name, action_name,
         COALESCE(user_identity.email, user_identity.subject_name) AS actor,
         -- Who a token permission went to and at what level, as the event records them.
         CASE WHEN action_name = 'changeDbTokenAcl' THEN request_params['targetUserId'] END AS granted_to,
         CASE WHEN action_name = 'changeDbTokenAcl' THEN request_params['aclPermissionSet'] END AS permission,
         COUNT(*) AS event_count,
         SUM(CASE WHEN response.status_code IS NOT NULL AND response.status_code <> 200 THEN 1 ELSE 0 END) AS failed_event_count,
         COUNT(DISTINCT source_ip_address) AS distinct_source_ips,
         -- CATEGORY (5b), UNCONFIRMED (see caveats): every action_name this row's (5b) branch
         -- matched, so a reader can check each one before trusting it.
         array_join(collect_set(CASE WHEN service_name = 'workspace' AND action_name RLIKE '(?i)token|ipaccesslist' AND NOT action_name RLIKE '^mint'
                                      THEN action_name END), ', ')            AS workspace_token_iplist_actions,
         MIN(event_time) AS first_event_time, MAX(event_time) AS last_event_time
  FROM (
    -- Deduped by event_id (latest event_time wins) before any aggregation, so a replayed/retried
    -- audit event counts once.
    SELECT *
    FROM `system`.`access`.`audit`
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
         OR (service_name = 'workspace' AND action_name RLIKE '(?i)token|ipaccesslist'
             AND NOT action_name RLIKE '^mint')   -- mintOAuthToken is sign-in, ~24 a day per service principal
          )
      AND event_date >= current_date() - INTERVAL 30 DAYS
      AND event_date < current_date()
    QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY event_time DESC) = 1
  )
  GROUP BY 1, 2, 3, 4, 5, 6
) a
LEFT JOIN (SELECT workspace_id AS ws_id, workspace_name FROM `system`.`access`.`workspaces_latest`) w
  ON w.ws_id = a.workspace_id
ORDER BY event_count DESC
) q
"""
CHECKS["access_classification_coverage"] = r"""
-- generated from dbt/models/databricks_direct/governance_access/d_access_classification_coverage.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/governance_access/access_classification_coverage.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
WITH inventory AS (
  -- one row per real, current non-VIEW table this account holds (dedup: information_schema.tables
  -- is already unique per table, DISTINCT here only protects the join below if that ever changes)
  SELECT DISTINCT table_catalog AS catalog_name, table_schema AS schema_name, table_name
  FROM `system`.`information_schema`.`tables`
  WHERE table_type <> 'VIEW'
    AND table_catalog NOT IN ('system', '__databricks_internal')   -- pipeline backing tables; classification can't be enabled there
    AND table_schema <> 'information_schema'
),
catalog_tables AS (
  SELECT catalog_name,
         COUNT(*) AS tables_total
  FROM inventory
  GROUP BY catalog_name
),
classified AS (
  SELECT DISTINCT catalog_name, schema_name, table_name
  FROM `system`.`data_classification`.`results`
),
classified_in_inventory AS (
  SELECT i.catalog_name, i.schema_name, i.table_name
  FROM inventory i
  INNER JOIN classified c
    ON  c.catalog_name = i.catalog_name
    AND c.schema_name  = i.schema_name
    AND c.table_name   = i.table_name
),
classified_by_catalog AS (
  SELECT catalog_name,
         COUNT(*) AS tables_classified
  FROM classified_in_inventory
  GROUP BY catalog_name
),
results_rows AS (
  SELECT COUNT(*) AS n_rows
  FROM `system`.`data_classification`.`results`
),
joined AS (
  SELECT ct.catalog_name,
         ct.tables_total,
         COALESCE(cc.tables_classified, 0) AS tables_classified
  FROM catalog_tables ct
  LEFT JOIN classified_by_catalog cc
    ON cc.catalog_name = ct.catalog_name
)
SELECT
  j.catalog_name,
  j.tables_total,
  j.tables_classified,
  -- NULL account-wide, not per catalog, when system.data_classification.results has zero rows
  -- anywhere in the whole snapshot
  CASE WHEN r.n_rows = 0 THEN NULL
       ELSE ROUND(100.0 * j.tables_classified / NULLIF(j.tables_total, 0), 1)
  END AS covered_pct,
  CASE
    WHEN r.n_rows = 0 THEN 'NOT_ASSESSED'
    WHEN 100.0 * j.tables_classified / NULLIF(j.tables_total, 0) < 80 THEN 'WARN'
    ELSE 'OK'
  END AS status,
  CASE WHEN r.n_rows = 0 THEN 'classification_table_empty'
       ELSE NULL
  END AS not_assessed_reason
FROM joined j
CROSS JOIN results_rows r
ORDER BY CASE status WHEN 'NOT_ASSESSED' THEN 1 WHEN 'WARN' THEN 2 ELSE 3 END,
         covered_pct ASC NULLS LAST,
         catalog_name
) q
"""
CHECKS["access_classified_unmasked"] = r"""
-- generated from dbt/models/databricks_direct/governance_access/d_access_classified_unmasked.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_classified_unmasked.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
WITH abac_masks AS (
  -- ABAC column-mask policies and where they are attached (metastore, catalog, schema or table)
  SELECT on_securable_type, catalog_name, schema_name, securable_name
  FROM `system`.`information_schema`.`abac_policy_definitions`
  WHERE policy_type = 'COLUMN_MASK'
),
sensitive AS (
  -- current tables only: a view takes its mask from the base table, and a dropped table needs none
  SELECT r.catalog_name, r.schema_name, r.table_name, r.column_name, r.class_tag, r.confidence
  FROM `system`.`data_classification`.`results` r
  JOIN `system`.`information_schema`.`tables` it
    ON  it.table_catalog = r.catalog_name
    AND it.table_schema  = r.schema_name
    AND it.table_name    = r.table_name
    AND COALESCE(upper(it.table_type), '') NOT IN ('VIEW', 'MATERIALIZED_VIEW', 'METRIC_VIEW')
  WHERE r.confidence = 'HIGH'
    AND r.class_tag IS NOT NULL
),
abac_per_table AS (
  -- a policy covers every table below where it is attached
  SELECT t.catalog_name, t.schema_name, t.table_name, count(*) AS abac_mask_policies
  FROM (SELECT DISTINCT catalog_name, schema_name, table_name FROM sensitive) t
  JOIN abac_masks am
    ON am.on_securable_type = 'METASTORE'
    OR (am.on_securable_type = 'CATALOG' AND am.catalog_name = t.catalog_name)
    OR (am.on_securable_type = 'SCHEMA' AND am.catalog_name = t.catalog_name AND am.schema_name = t.schema_name)
    OR (am.on_securable_type = 'TABLE' AND am.catalog_name = t.catalog_name AND am.schema_name = t.schema_name
        AND am.securable_name = t.table_name)
  GROUP BY t.catalog_name, t.schema_name, t.table_name
),
classified AS (
  SELECT dc.catalog_name, dc.schema_name, dc.table_name, dc.column_name, dc.class_tag, dc.confidence,
         cm.mask_name AS mask_name,
         COALESCE(ap.abac_mask_policies, 0) AS abac_mask_policies
  FROM sensitive dc
  LEFT JOIN `system`.`information_schema`.`column_masks` cm
    ON  cm.table_catalog = dc.catalog_name
    AND cm.table_schema  = dc.schema_name
    AND cm.table_name    = dc.table_name
    AND cm.column_name   = dc.column_name
  LEFT JOIN abac_per_table ap
    ON  ap.catalog_name = dc.catalog_name
    AND ap.schema_name  = dc.schema_name
    AND ap.table_name   = dc.table_name
)
SELECT catalog_name, schema_name, table_name, column_name, class_tag, confidence,
       mask_name,
       abac_mask_policies,
       CASE WHEN mask_name IS NULL AND abac_mask_policies = 0 THEN true ELSE false END AS is_unmasked,
       CASE WHEN mask_name IS NULL AND abac_mask_policies > 0 THEN 'abac_policy_in_scope' END AS not_assessed_reason,
       -- status: a mask set on the column is OK; an ABAC policy above it can't be judged per column
       CASE WHEN mask_name IS NOT NULL THEN 'OK'
            WHEN abac_mask_policies > 0 THEN 'NOT_ASSESSED'
            ELSE 'CRITICAL' END AS status
FROM classified
ORDER BY is_unmasked DESC, catalog_name, schema_name, table_name, column_name
) q
"""
CHECKS["access_sensitive_table_reads"] = r"""
-- generated from dbt/models/databricks_direct/governance_access/d_access_sensitive_table_reads.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/governance_access/access_sensitive_table_reads.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH sensitive_table_tags AS (
  SELECT DISTINCT catalog_name, schema_name, table_name, 'table_tag' AS basis
  FROM `system`.`information_schema`.`table_tags`
  WHERE (tag_name RLIKE '(?i)(pii|sensitiv|confidential|gdpr|personal|secret|restricted)' AND lower(trim(COALESCE(tag_value, ''))) NOT IN ('false', 'no', 'n', '0', 'none', 'public', 'internal', 'low'))
     OR lower(trim(tag_value)) IN ('pii', 'sensitive', 'confidential', 'restricted', 'secret', 'gdpr', 'personal', 'phi', 'pci')
),
sensitive_column_tags AS (
  SELECT DISTINCT catalog_name, schema_name, table_name, 'column_tag' AS basis
  FROM `system`.`information_schema`.`column_tags`
  WHERE (tag_name RLIKE '(?i)(pii|sensitiv|confidential|gdpr|personal|secret|restricted)' AND lower(trim(COALESCE(tag_value, ''))) NOT IN ('false', 'no', 'n', '0', 'none', 'public', 'internal', 'low'))
     OR lower(trim(tag_value)) IN ('pii', 'sensitive', 'confidential', 'restricted', 'secret', 'gdpr', 'personal', 'phi', 'pci')
),
sensitive_classification AS (
  SELECT DISTINCT catalog_name, schema_name, table_name, 'classification' AS basis
  FROM `system`.`data_classification`.`results`
  WHERE class_tag IS NOT NULL
),
sensitive_union AS (
  SELECT catalog_name, schema_name, table_name, basis FROM sensitive_table_tags
  UNION ALL
  SELECT catalog_name, schema_name, table_name, basis FROM sensitive_column_tags
  UNION ALL
  SELECT catalog_name, schema_name, table_name, basis FROM sensitive_classification
),
sensitive_tables AS (
  -- one row per table the account has flagged sensitive by any of the three sources above
  SELECT catalog_name, schema_name, table_name,
         array_join(collect_set(basis), ', ') AS sensitivity_basis
  FROM sensitive_union
  GROUP BY catalog_name, schema_name, table_name
),
sensitive_table_count AS (
  SELECT COUNT(*) AS n FROM sensitive_tables
),
table_reads AS (
  -- every table_lineage row that proves a READ of its source table (see caveats: SOURCE)
  SELECT workspace_id,
         source_table_catalog AS catalog_name,
         source_table_schema  AS schema_name,
         source_table_name    AS table_name,
         created_by,
         event_time,
         event_date
  FROM `system`.`access`.`table_lineage`
  WHERE source_table_name IS NOT NULL
    AND source_table_catalog <> 'system'
    AND (direct_access IS NULL OR direct_access = true)
    AND event_date >= dateadd(day, -30, current_date())
    AND event_date < current_date()
),
table_owners AS (
  SELECT table_catalog AS catalog_name, table_schema AS schema_name, table_name, table_owner
  FROM `system`.`information_schema`.`tables`
),
reads_agg AS (
  SELECT r.workspace_id, r.catalog_name, r.schema_name, r.table_name, r.created_by,
         COUNT(*)                     AS read_count,
         MIN(r.event_time)            AS first_read_time,
         MAX(r.event_time)            AS last_read_time,
         COUNT(DISTINCT r.event_date) AS distinct_read_days,
         MAX(s.sensitivity_basis)     AS sensitivity_basis
  FROM table_reads r
  JOIN sensitive_tables s
    ON  s.catalog_name = r.catalog_name
    AND s.schema_name  = r.schema_name
    AND s.table_name   = r.table_name
  GROUP BY r.workspace_id, r.catalog_name, r.schema_name, r.table_name, r.created_by
),
judged AS (
  SELECT a.workspace_id, a.catalog_name, a.schema_name, a.table_name, a.created_by,
         a.read_count, a.first_read_time, a.last_read_time, a.distinct_read_days,
         a.sensitivity_basis,
         o.table_owner,
         -- raw, unmasked comparison so masking (below) never hides a real owner match
         CASE
           WHEN o.table_owner IS NOT NULL AND a.created_by IS NOT NULL
            AND lower(trim(o.table_owner)) = lower(trim(a.created_by))
           THEN 1 ELSE 0
         END AS is_owner
  FROM reads_agg a
  LEFT JOIN table_owners o
    ON  o.catalog_name = a.catalog_name
    AND o.schema_name  = a.schema_name
    AND o.table_name   = a.table_name
),
combined AS (
  SELECT j.workspace_id AS workspace_id,
         j.catalog_name AS table_catalog,
         j.schema_name  AS table_schema,
         j.table_name   AS table_name,
         j.sensitivity_basis,
         j.created_by AS reader,
         j.table_owner AS table_owner,
         CAST(j.read_count AS BIGINT)         AS read_count,
         j.first_read_time,
         j.last_read_time,
         CAST(j.distinct_read_days AS BIGINT) AS distinct_read_days,
         -- status: an owner is never flagged; a non-owner over 20 reads is WARN.
         -- lineage leaves created_by NULL or 'System-User' when no person is recorded
         CASE
           WHEN j.created_by IS NULL OR lower(trim(j.created_by)) = 'system-user' THEN 'NOT_ASSESSED'
           WHEN j.is_owner = 1        THEN 'OK'
           WHEN j.read_count > 20 THEN 'WARN'
           ELSE 'OK'
         END AS status,
         CASE WHEN j.created_by IS NULL OR lower(trim(j.created_by)) = 'system-user'
              THEN 'reader_not_recorded' END AS not_assessed_reason
  FROM judged j

  UNION ALL

  -- the one account-wide sentinel row: nothing anywhere has ever been tagged or classified
  -- sensitive, so nothing below could have been judged in the first place (never silently OK).
  -- No natural workspace to tie it to, so workspace_id is NULL -- it drops out under a workspace
  -- filter rather than falsely claiming to speak for one particular workspace.
  SELECT
    CAST(NULL AS STRING)    AS workspace_id,
    CAST(NULL AS STRING)    AS table_catalog,
    CAST(NULL AS STRING)    AS table_schema,
    CAST(NULL AS STRING)    AS table_name,
    CAST(NULL AS STRING)    AS sensitivity_basis,
    CAST(NULL AS STRING)    AS reader,
    CAST(NULL AS STRING)    AS table_owner,
    CAST(0 AS BIGINT)       AS read_count,
    CAST(NULL AS TIMESTAMP) AS first_read_time,
    CAST(NULL AS TIMESTAMP) AS last_read_time,
    CAST(0 AS BIGINT)       AS distinct_read_days,
    'NOT_ASSESSED'          AS status,
    'no_sensitivity_signal' AS not_assessed_reason
  FROM sensitive_table_count
  WHERE n = 0
)
SELECT workspace_id, table_catalog, table_schema, table_name, sensitivity_basis, reader, table_owner,
       read_count, first_read_time, last_read_time, distinct_read_days, status, not_assessed_reason
FROM combined
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         read_count DESC,
         table_catalog, table_schema, table_name, reader
) q
"""
CHECKS["access_pii_outside_tables"] = r"""
-- generated from dbt/models/databricks_direct/governance_access/d_access_pii_outside_tables.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_pii_outside_tables.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT
  object_type, catalog_name, schema_name, object_name, object_detail, finding, tags,
  CASE
    WHEN is_sensitive = 1 AND is_external = 1 THEN 'CRITICAL'
    WHEN untagged     = 1 AND is_external = 1 THEN 'CRITICAL'
    WHEN is_sensitive = 1                     THEN 'WARN'
    WHEN untagged     = 1                     THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM (
  -- VOLUMES: sensitive-tagged (known PII files) or untagged (blind spot)
  SELECT
    'VOLUME'          AS object_type,
    v.VOLUME_CATALOG  AS catalog_name,
    v.VOLUME_SCHEMA   AS schema_name,
    v.VOLUME_NAME     AS object_name,
    v.VOLUME_TYPE     AS object_detail,
    CASE WHEN upper(v.VOLUME_TYPE) = 'EXTERNAL' THEN 1 ELSE 0 END AS is_external,
    COALESCE(vt.is_sensitive, 0)                                 AS is_sensitive,
    CASE WHEN COALESCE(vt.tag_count, 0) = 0 THEN 1 ELSE 0 END    AS untagged,
    vt.tag_names                                                AS tags,
    CASE WHEN COALESCE(vt.is_sensitive, 0) = 1 THEN 'sensitive-tagged (PII files outside governed tables)'
         WHEN COALESCE(vt.tag_count, 0) = 0    THEN 'untagged (unclassified file store)'
         ELSE 'tagged (non-sensitive)' END                     AS finding
  FROM `system`.`information_schema`.`volumes` v
  LEFT JOIN (
    SELECT CATALOG_NAME, SCHEMA_NAME, VOLUME_NAME,
           COUNT(*) AS tag_count,
           MAX(CASE WHEN (TAG_NAME RLIKE '(?i)(pii|sensitiv|confidential|gdpr|personal|secret|restricted)' AND lower(trim(COALESCE(TAG_VALUE, ''))) NOT IN ('false', 'no', 'n', '0', 'none', 'public', 'internal', 'low'))
                      OR lower(trim(TAG_VALUE)) IN ('pii', 'sensitive', 'confidential', 'restricted', 'secret', 'gdpr', 'personal', 'phi', 'pci')
                    THEN 1 ELSE 0 END) AS is_sensitive,
           array_join(collect_set(TAG_NAME), ', ') AS tag_names
    FROM `system`.`information_schema`.`volume_tags`
    GROUP BY CATALOG_NAME, SCHEMA_NAME, VOLUME_NAME
  ) vt
    ON vt.CATALOG_NAME = v.VOLUME_CATALOG AND vt.SCHEMA_NAME = v.VOLUME_SCHEMA AND vt.VOLUME_NAME = v.VOLUME_NAME
  UNION ALL
  -- SCHEMAS: only those carrying a sensitivity tag (a namespace flagged sensitive)
  SELECT
    'SCHEMA'          AS object_type,
    st.CATALOG_NAME   AS catalog_name,
    st.SCHEMA_NAME    AS schema_name,
    st.SCHEMA_NAME    AS object_name,
    'schema tag'      AS object_detail,
    0                 AS is_external,
    1                 AS is_sensitive,
    0                 AS untagged,
    array_join(collect_set(concat(st.TAG_NAME, '=', st.TAG_VALUE)), ', ') AS tags,
    'sensitive-tagged schema (namespace flagged sensitive)'              AS finding
  FROM `system`.`information_schema`.`schema_tags` st
  WHERE (st.TAG_NAME RLIKE '(?i)(pii|sensitiv|confidential|gdpr|personal|secret|restricted)' AND lower(trim(COALESCE(st.TAG_VALUE, ''))) NOT IN ('false', 'no', 'n', '0', 'none', 'public', 'internal', 'low'))
     OR lower(trim(st.TAG_VALUE)) IN ('pii', 'sensitive', 'confidential', 'restricted', 'secret', 'gdpr', 'personal', 'phi', 'pci')
  GROUP BY st.CATALOG_NAME, st.SCHEMA_NAME
)
WHERE is_sensitive = 1 OR untagged = 1   -- findings only (drop tagged-but-non-sensitive volumes)
ORDER BY
  CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
  object_type, catalog_name, schema_name, object_name
LIMIT 100000
) q
"""
CHECKS["access_data_classification_inventory"] = r"""
-- generated from dbt/models/databricks_direct/governance_access/d_access_data_classification_inventory.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_data_classification_inventory.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT catalog_name, schema_name, table_name, column_name, class_tag, confidence, data_type,
       MAX(frequency)            AS max_frequency,
       MAX(latest_detected_time) AS latest_detected_time,
       MIN(first_detected_time)  AS first_detected_time
FROM `system`.`data_classification`.`results`
WHERE class_tag IS NOT NULL
GROUP BY 1, 2, 3, 4, 5, 6, 7
ORDER BY catalog_name, schema_name, table_name, column_name
) q
"""
CHECKS["access_column_masks_inventory"] = r"""
-- generated from dbt/models/databricks_direct/governance_access/d_access_column_masks_inventory.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_column_masks_inventory.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT table_catalog, table_schema, table_name, column_name,
       mask_name, using_columns
FROM `system`.`information_schema`.`column_masks`
ORDER BY table_catalog, table_schema, table_name, column_name
) q
"""
CHECKS["access_row_filters_inventory"] = r"""
-- generated from dbt/models/databricks_direct/governance_access/d_access_row_filters_inventory.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_row_filters_inventory.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT table_catalog, table_schema, table_name,
       filter_name, target_columns
FROM `system`.`information_schema`.`row_filters`
ORDER BY table_catalog, table_schema, table_name
) q
"""
CHECKS["access_tags_inventory"] = r"""
-- generated from dbt/models/databricks_direct/governance_access/d_access_tags_inventory.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_tags_inventory.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT 'COLUMN' AS object_scope, TAG_NAME, TAG_VALUE,
       COUNT(*) AS tagged_object_count
FROM `system`.`information_schema`.`column_tags`
GROUP BY 1, 2, 3
UNION ALL
SELECT 'TABLE' AS object_scope, TAG_NAME, TAG_VALUE,
       COUNT(*) AS tagged_object_count
FROM `system`.`information_schema`.`table_tags`
GROUP BY 1, 2, 3
UNION ALL
SELECT 'SCHEMA' AS object_scope, TAG_NAME, TAG_VALUE,
       COUNT(*) AS tagged_object_count
FROM `system`.`information_schema`.`schema_tags`
GROUP BY 1, 2, 3
UNION ALL
SELECT 'VOLUME' AS object_scope, TAG_NAME, TAG_VALUE,
       COUNT(*) AS tagged_object_count
FROM `system`.`information_schema`.`volume_tags`
GROUP BY 1, 2, 3
ORDER BY object_scope, TAG_NAME, TAG_VALUE
) q
"""
CHECKS["access_table_lineage_blast_radius"] = r"""
-- generated from dbt/models/databricks_direct/governance_access/d_access_table_lineage_blast_radius.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_table_lineage_blast_radius.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH lineage_window AS (
  SELECT workspace_id, source_table_full_name, target_table_full_name, source_type, target_type,
         entity_type, direct_access, created_by, event_time
  FROM `system`.`access`.`table_lineage`
  WHERE event_date >= current_date() - INTERVAL 30 DAYS
    AND event_date < current_date()
    -- Exclude accesses this table cannot tie to a catalog table on EITHER side (path-based, JDBC,
    -- unresolved) rather than pooling every one of them into one fake NULL-name "edge" (see caveats).
    AND (source_table_full_name IS NOT NULL OR target_table_full_name IS NOT NULL)
),
source_reach AS (
  SELECT source_table_full_name, COUNT(DISTINCT created_by) AS distinct_principals
  FROM lineage_window
  WHERE source_table_full_name IS NOT NULL
  GROUP BY source_table_full_name
)
SELECT lw.workspace_id, lw.source_table_full_name, lw.target_table_full_name, lw.source_type, lw.target_type,
       lw.entity_type, lw.direct_access,
       CASE WHEN lw.source_type IS NOT NULL AND lw.target_type IS NULL THEN 'READ'
            WHEN lw.target_type IS NOT NULL AND lw.source_type IS NULL THEN 'WRITE'
            WHEN lw.source_type IS NOT NULL AND lw.target_type IS NOT NULL THEN 'READ_WRITE'
            ELSE 'UNKNOWN' END AS access_class,
       COUNT(*) AS event_count,
       COUNT(DISTINCT lw.created_by) AS distinct_principals,
       MAX(sr.distinct_principals) AS source_distinct_principals,
       MAX(lw.event_time) AS last_event_time,
       -- status: worst-first band on the source table's own total reach, this edge's own count
       -- for a WRITE row with no source to roll up to (field heuristic; 10 / 50).
       CASE
         WHEN COALESCE(MAX(sr.distinct_principals), COUNT(DISTINCT lw.created_by)) >= 50 THEN 'CRITICAL'
         WHEN COALESCE(MAX(sr.distinct_principals), COUNT(DISTINCT lw.created_by)) >= 10 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM lineage_window lw
LEFT JOIN source_reach sr ON sr.source_table_full_name = lw.source_table_full_name
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
ORDER BY distinct_principals DESC
) q
"""
CHECKS["access_column_lineage_sensitive_reach"] = r"""
-- generated from dbt/models/databricks_direct/governance_access/d_access_column_lineage_sensitive_reach.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_column_lineage_sensitive_reach.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH sensitive_tags AS (
  -- One row per SOURCE column (GROUP BY, not the raw per-tag rows): a column carrying more than
  -- one matching tag collapses here first, so the join below cannot fan the same edge out twice.
  SELECT catalog_name, schema_name, table_name, column_name,
         array_join(collect_set(concat(tag_name, '=', tag_value)), ', ') AS source_sensitivity_tags
  FROM `system`.`information_schema`.`column_tags`
  -- a sensitivity key counts unless its value says otherwise (pii=false, classification=public)
  WHERE (lower(tag_name) IN (
          'pii', 'sensitivity', 'sensitive', 'data_classification', 'classification',
          'confidentiality', 'phi', 'pci'
        )
         AND lower(trim(COALESCE(tag_value, ''))) NOT IN ('false', 'no', 'n', '0', 'none', 'public', 'internal', 'low'))
     OR lower(tag_value) IN (
          'pii', 'sensitive', 'confidential', 'restricted', 'phi', 'pci'
        )
  GROUP BY catalog_name, schema_name, table_name, column_name
)
SELECT lw.workspace_id, lw.source_table_full_name, lw.source_column_name, st.source_sensitivity_tags,
       lw.target_table_full_name, lw.target_column_name, lw.entity_type, lw.direct_access,
       COUNT(*) AS event_count,
       COUNT(DISTINCT lw.created_by) AS distinct_principals,
       MAX(lw.event_time) AS last_event_time,
       -- status: worst-first band on how many distinct principals drove this sensitive-source
       -- column-to-column edge within this workspace (field heuristic; 10 /
       -- 50).
       CASE
         WHEN COUNT(DISTINCT lw.created_by) >= 50 THEN 'CRITICAL'
         WHEN COUNT(DISTINCT lw.created_by) >= 10 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`access`.`column_lineage` lw
JOIN sensitive_tags st
  ON  lw.source_table_catalog = st.catalog_name
  AND lw.source_table_schema  = st.schema_name
  AND lw.source_table_name    = st.table_name
  AND lw.source_column_name   = st.column_name
WHERE lw.source_table_full_name IS NOT NULL
  AND lw.event_date >= current_date() - INTERVAL 30 DAYS
  AND lw.event_date < current_date()
GROUP BY lw.workspace_id, lw.source_table_full_name, lw.source_column_name, st.source_sensitivity_tags,
         lw.target_table_full_name, lw.target_column_name, lw.entity_type, lw.direct_access
ORDER BY distinct_principals DESC
) q
"""
CHECKS["access_pii_propagation_untagged"] = r"""
-- generated from dbt/models/databricks_direct/governance_access/d_access_pii_propagation_untagged.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_pii_propagation_untagged.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH lineage_window AS (
  SELECT workspace_id, source_table_catalog, source_table_schema, source_table_name, source_column_name,
         target_table_catalog, target_table_schema, target_table_name, target_column_name,
         created_by
  FROM `system`.`access`.`column_lineage`
  -- a copy made through a view is recorded as base column -> target with direct_access false, so both
  -- count; a view as target holds no copy of its own
  WHERE COALESCE(upper(target_type), '') NOT IN ('VIEW', 'MATERIALIZED_VIEW', 'METRIC_VIEW')
    AND source_column_name IS NOT NULL
    AND target_column_name IS NOT NULL
    AND source_table_catalog <> 'system'
    AND event_date >= dateadd(day, -30, current_date())
    AND event_date < current_date()
),
sensitive_tags AS (
  SELECT catalog_name, schema_name, table_name, column_name,
         array_join(sort_array(collect_set(concat(tag_name, '=', tag_value))), ', ') AS tags
  FROM `system`.`information_schema`.`column_tags`
  -- a sensitivity key counts unless its value says otherwise (pii=false, classification=public)
  WHERE (lower(tag_name) IN (
          'pii', 'sensitivity', 'sensitive', 'data_classification', 'classification',
          'confidentiality', 'phi', 'pci'
        )
         AND lower(trim(COALESCE(tag_value, ''))) NOT IN ('false', 'no', 'n', '0', 'none', 'public', 'internal', 'low'))
     OR lower(tag_value) IN (
          'pii', 'sensitive', 'confidential', 'restricted', 'phi', 'pci'
        )
  GROUP BY catalog_name, schema_name, table_name, column_name
)
SELECT lw.workspace_id,
       lw.source_table_catalog,
       lw.source_table_schema,
       lw.source_table_name,
       lw.source_column_name,
       st.tags AS source_tags,
       lw.target_table_catalog,
       lw.target_table_schema,
       lw.target_table_name,
       lw.target_column_name,
       COUNT(DISTINCT lw.created_by) AS distinct_creators,
       COUNT(*) AS event_count,
       -- status: worst-first band on how often this untagged-PII-propagation edge fired (field heuristic; 5 / 50).
       CASE
         WHEN COUNT(*) >= 50 THEN 'CRITICAL'
         WHEN COUNT(*) >= 5 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM lineage_window lw
JOIN sensitive_tags st
  ON  lw.source_table_catalog = st.catalog_name
  AND lw.source_table_schema  = st.schema_name
  AND lw.source_table_name    = st.table_name
  AND lw.source_column_name   = st.column_name
LEFT JOIN sensitive_tags tt
  ON  lw.target_table_catalog = tt.catalog_name
  AND lw.target_table_schema  = tt.schema_name
  AND lw.target_table_name    = tt.table_name
  AND lw.target_column_name   = tt.column_name
WHERE tt.column_name IS NULL          -- target carries no SENSITIVITY tag -> propagation gap
GROUP BY lw.workspace_id, lw.source_table_catalog, lw.source_table_schema, lw.source_table_name,
         lw.source_column_name, st.tags,
         lw.target_table_catalog, lw.target_table_schema, lw.target_table_name,
         lw.target_column_name
ORDER BY event_count DESC
) q
"""
CHECKS["access_views_inventory"] = r"""
-- generated from dbt/models/databricks_direct/governance_access/d_access_views_inventory.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_views_inventory.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT
  TABLE_CATALOG AS view_catalog,
  TABLE_SCHEMA  AS view_schema,
  TABLE_NAME    AS view_name,
  IS_MATERIALIZED    AS is_materialized,
  IS_UPDATABLE       AS is_updatable,
  IS_INSERTABLE_INTO AS is_insertable_into,
  SQL_PATH           AS sql_path,
  LENGTH(VIEW_DEFINITION) AS definition_chars,
  CASE WHEN upper(VIEW_DEFINITION) LIKE '%MASK%' OR upper(VIEW_DEFINITION) LIKE '%FILTER%'
       THEN true ELSE false END AS references_mask_or_filter
FROM `system`.`information_schema`.`views`
ORDER BY view_catalog, view_schema, view_name
LIMIT 100000
) q
"""
CHECKS["access_volumes_inventory"] = r"""
-- generated from dbt/models/databricks_direct/governance_access/d_access_volumes_inventory.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_volumes_inventory.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
WITH vt AS (
  SELECT CATALOG_NAME, SCHEMA_NAME, VOLUME_NAME,
         COUNT(*) AS tag_count,
         array_join(collect_set(TAG_NAME), ', ') AS tag_names
  FROM `system`.`information_schema`.`volume_tags`
  GROUP BY CATALOG_NAME, SCHEMA_NAME, VOLUME_NAME
)
SELECT
  v.VOLUME_CATALOG AS volume_catalog,
  v.VOLUME_SCHEMA  AS volume_schema,
  v.VOLUME_NAME    AS volume_name,
  v.VOLUME_TYPE    AS volume_type,
  v.VOLUME_OWNER AS volume_owner,
  v.STORAGE_LOCATION AS storage_location,
  COALESCE(vt.tag_count, 0) AS tag_count,
  vt.tag_names              AS tag_names,
  v.CREATED                 AS created,
  CASE
    WHEN COALESCE(vt.tag_count, 0) = 0 AND upper(v.VOLUME_TYPE) = 'EXTERNAL' THEN 'CRITICAL'
    WHEN COALESCE(vt.tag_count, 0) = 0                                       THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM `system`.`information_schema`.`volumes` v
LEFT JOIN vt
  ON vt.CATALOG_NAME = v.VOLUME_CATALOG AND vt.SCHEMA_NAME = v.VOLUME_SCHEMA AND vt.VOLUME_NAME = v.VOLUME_NAME
ORDER BY
  CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
  v.VOLUME_CATALOG, v.VOLUME_SCHEMA, v.VOLUME_NAME
LIMIT 100000
) q
"""
CHECKS["access_delta_sharing_exposure"] = r"""
-- generated from dbt/models/databricks_direct/governance_access/d_access_delta_sharing_exposure.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_delta_sharing_exposure.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
WITH recips AS (
  SELECT SHARE_NAME,
         COUNT(DISTINCT RECIPIENT_NAME) AS recipient_count,
         array_join(collect_set(
           RECIPIENT_NAME), ', ') AS recipients
  FROM `system`.`information_schema`.`share_recipient_privileges`
  GROUP BY SHARE_NAME
),
tbls AS (
  SELECT SHARE_NAME, COUNT(*) AS shared_table_count
  FROM `system`.`information_schema`.`table_share_usage` GROUP BY SHARE_NAME
),
schs AS (
  SELECT SHARE_NAME, COUNT(*) AS shared_schema_count
  FROM `system`.`information_schema`.`schema_share_usage` GROUP BY SHARE_NAME
)
SELECT
  s.SHARE_NAME                                              AS share_name,
  s.SHARE_OWNER AS share_owner,
  COALESCE(r.recipient_count, 0)                            AS recipient_count,
  r.recipients                                             AS recipients,
  COALESCE(t.shared_table_count, 0)                        AS shared_table_count,
  COALESCE(sc.shared_schema_count, 0)                      AS shared_schema_count,
  s.CREATED                                                AS created,
  CASE
    -- a share with no recipient exposes nothing yet, however many tables it holds
    WHEN COALESCE(r.recipient_count, 0) >= 10
      OR (COALESCE(t.shared_table_count, 0) >= 100
          AND COALESCE(r.recipient_count, 0) > 0)                        THEN 'CRITICAL'
    WHEN COALESCE(r.recipient_count, 0) >= 1       THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM `system`.`information_schema`.`shares` s
LEFT JOIN recips r  ON r.SHARE_NAME  = s.SHARE_NAME
LEFT JOIN tbls   t  ON t.SHARE_NAME  = s.SHARE_NAME
LEFT JOIN schs   sc ON sc.SHARE_NAME = s.SHARE_NAME
ORDER BY
  CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
  recipient_count DESC, shared_table_count DESC
LIMIT 100000
) q
"""
CHECKS["storage_target_table_discovery"] = r"""
-- generated from dbt/models/databricks_direct/storage/d_storage_target_table_discovery.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/storage/storage_target_table_discovery.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT
    table_catalog || '.' || table_schema || '.' || table_name AS target_table,
    table_catalog,
    table_schema,
    table_name,
    table_type,
    data_source_format
FROM `system`.`information_schema`.`tables`
WHERE table_type <> 'VIEW'
  AND table_catalog NOT IN ('system', 'samples')
  AND COALESCE(table_owner, '') <> 'System user'
ORDER BY table_catalog, table_schema, table_name
) q
"""
CHECKS["table_inventory_type"] = r"""
-- generated from dbt/models/databricks_direct/storage/d_table_inventory_type.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/storage/table_inventory_type.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT table_catalog, table_schema, table_type, data_source_format,
       COUNT(*) AS table_count
FROM `system`.`information_schema`.`tables`
WHERE table_catalog NOT IN ('system', 'samples')
  AND COALESCE(table_owner, '') <> 'System user'
GROUP BY table_catalog, table_schema, table_type, data_source_format
ORDER BY table_catalog, table_schema, table_type
) q
"""
CHECKS["access_dead_table_candidates"] = r"""
-- generated from dbt/models/databricks_direct/governance_access/d_access_dead_table_candidates.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_dead_table_candidates.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH lineage_window AS (
  SELECT *
  FROM `system`.`access`.`table_lineage`
  WHERE event_date >= dateadd(day, -30, current_date())
    AND event_date < current_date()
),
source_tables AS (
  SELECT DISTINCT
         source_table_catalog AS catalog,
         source_table_schema  AS schema,
         source_table_name    AS name
  FROM lineage_window
  WHERE source_table_name IS NOT NULL
),
inventory AS (
  SELECT table_catalog,
         table_schema,
         table_name,
         table_type,
         table_owner,
         created,
         last_altered
  FROM `system`.`information_schema`.`tables`
  WHERE table_catalog NOT IN ('system', '__databricks_internal', 'samples')   -- pipeline backing tables, Databricks samples
    AND COALESCE(table_owner, '') <> 'System user'
    AND table_schema <> 'information_schema'
    AND table_type IN ('MANAGED', 'EXTERNAL')
)
SELECT inv.table_catalog,
       inv.table_schema,
       inv.table_name,
       inv.table_type,
       inv.table_owner AS table_owner,
       inv.created,
       inv.last_altered,
       -- Age in whole days since the object was last altered (NULL -> age unknown).
       datediff(current_date(), DATE(inv.last_altered)) AS days_since_altered,
       -- status: worst-first band on staleness (field heuristic; 90 / 365). NULL age -> NOT_ASSESSED, never a finding.
       CASE
         WHEN datediff(current_date(), DATE(inv.last_altered)) IS NULL THEN 'NOT_ASSESSED'
         WHEN datediff(current_date(), DATE(inv.last_altered)) >= 365 THEN 'CRITICAL'
         WHEN datediff(current_date(), DATE(inv.last_altered)) >= 90 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM inventory inv
LEFT JOIN source_tables src
  ON  inv.table_catalog = src.catalog
  AND inv.table_schema  = src.schema
  AND inv.table_name    = src.name
WHERE src.name IS NULL          -- never appeared as a lineage source in the window
ORDER BY days_since_altered DESC NULLS LAST
) q
"""
CHECKS["storage_growth"] = r"""
-- generated from dbt/models/databricks_direct/storage/d_storage_growth.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/storage/storage_growth.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH snaps AS (
  SELECT table_id, catalog_name, schema_name, table_name, table_owner, table_dropped_time,
         snapshot_date, active_bytes
  FROM `system`.`storage`.`table_metrics_history`
  WHERE snapshot_date >= current_date() - INTERVAL 30 DAYS
    AND snapshot_date < current_date()
),
first_snap AS (
  SELECT table_id, snapshot_date AS first_snapshot_date, active_bytes AS first_bytes
  FROM snaps
  QUALIFY ROW_NUMBER() OVER (PARTITION BY table_id ORDER BY snapshot_date ASC) = 1
),
last_snap AS (
  SELECT table_id, catalog_name, schema_name, table_name, table_owner, table_dropped_time,
         snapshot_date AS last_snapshot_date, active_bytes AS last_bytes
  FROM snaps
  QUALIFY ROW_NUMBER() OVER (PARTITION BY table_id ORDER BY snapshot_date DESC) = 1
)
SELECT
  l.table_id,
  l.catalog_name,
  l.schema_name,
  l.table_name,
  CONCAT(l.catalog_name, '.', l.schema_name, '.', l.table_name) AS full_name,
  l.table_owner,
  (l.table_owner IS NULL OR l.table_owner = '') AS no_owner,
  f.first_snapshot_date,
  f.first_bytes,
  l.last_snapshot_date,
  l.last_bytes,
  l.last_bytes - f.first_bytes AS growth_bytes,
  ROUND((l.last_bytes - f.first_bytes) * 100.0 / NULLIF(f.first_bytes, 0), 1) AS growth_pct,
  (l.table_dropped_time IS NOT NULL AND l.table_dropped_time >= current_date() - INTERVAL 30 DAYS)
    AS dropped_in_window,
  CASE
    WHEN l.table_dropped_time IS NOT NULL AND l.table_dropped_time >= current_date() - INTERVAL 30 DAYS THEN 'WARN'
    WHEN f.first_bytes > 0 AND (l.last_bytes - f.first_bytes) >= 1073741824
      AND (l.last_bytes - f.first_bytes) * 100.0 / f.first_bytes >= 50
      THEN 'CRITICAL'
    WHEN f.first_bytes > 0 AND (l.last_bytes - f.first_bytes) >= 1073741824
      AND (l.last_bytes - f.first_bytes) * 100.0 / f.first_bytes >= 20
      THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM first_snap f
JOIN last_snap l ON l.table_id = f.table_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
         growth_bytes DESC NULLS LAST
LIMIT 100000
) q
"""
CHECKS["po_maintenance_cost_by_table"] = r"""
-- generated from dbt/models/databricks_direct/storage/d_po_maintenance_cost_by_table.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/storage/po_maintenance_cost_by_table.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT account_id, workspace_id, metastore_name, catalog_name, schema_name, table_id, table_name,
       operation_type, operation_status, usage_unit,
       COUNT(*)                                  AS operation_count,
       SUM(CAST(usage_quantity AS DECIMAL(38,6))) AS estimated_dbu,
       MIN(start_time) AS first_op_time, MAX(end_time) AS last_op_time,
       -- status: worst-first band; any FAILED row is CRITICAL, else banded on estimated_dbu (field heuristic; 20 / 100).
       CASE
         WHEN operation_status LIKE 'FAILED%' THEN 'CRITICAL'   -- a failure counts before its bill lands
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) IS NULL THEN 'NOT_ASSESSED'
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) >= 100 THEN 'CRITICAL'
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) >= 20 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`storage`.`predictive_optimization_operations_history`
WHERE start_time >= current_date() - INTERVAL 30 DAYS
  AND start_time < current_date()
GROUP BY account_id, workspace_id, metastore_name, catalog_name, schema_name, table_id, table_name,
         operation_type, operation_status, usage_unit
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END, estimated_dbu DESC
) q
"""
CHECKS["po_vacuum_reclaimed_bytes"] = r"""
-- generated from dbt/models/databricks_direct/storage/d_po_vacuum_reclaimed_bytes.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/storage/po_vacuum_reclaimed_bytes.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT workspace_id, catalog_name, schema_name, table_id, table_name,
       COUNT(*) AS vacuum_op_count,
       SUM(CAST(operation_metrics['number_of_deleted_files']      AS BIGINT))  AS total_deleted_files,
       SUM(CAST(operation_metrics['amount_of_data_deleted_bytes'] AS BIGINT))  AS total_deleted_bytes,
       SUM(CAST(usage_quantity AS DECIMAL(38,6)))                              AS vacuum_estimated_dbu,
       -- status: worst-first band on DBU spent with zero bytes reclaimed (field heuristic; 5 / 20).
       CASE
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) IS NULL THEN 'NOT_ASSESSED'
         WHEN SUM(CAST(operation_metrics['amount_of_data_deleted_bytes'] AS BIGINT)) = 0
              AND SUM(CAST(usage_quantity AS DECIMAL(38,6))) >= 20 THEN 'CRITICAL'
         WHEN SUM(CAST(operation_metrics['amount_of_data_deleted_bytes'] AS BIGINT)) = 0
              AND SUM(CAST(usage_quantity AS DECIMAL(38,6))) >= 5 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`storage`.`predictive_optimization_operations_history`
WHERE operation_type = 'VACUUM' AND operation_status = 'SUCCESSFUL'
  AND start_time >= current_date() - INTERVAL 30 DAYS AND start_time < current_date()
GROUP BY workspace_id, catalog_name, schema_name, table_id, table_name
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END, vacuum_estimated_dbu DESC
) q
"""
CHECKS["po_clustering_activity"] = r"""
-- generated from dbt/models/databricks_direct/storage/d_po_clustering_activity.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/storage/po_clustering_activity.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT workspace_id, catalog_name, schema_name, table_id, table_name, operation_type,
       COUNT(*) AS op_count,
       SUM(CAST(operation_metrics['number_of_removed_files']        AS BIGINT)) AS removed_files,
       SUM(CAST(operation_metrics['number_of_clustered_files']      AS BIGINT)) AS clustered_files,
       SUM(CAST(operation_metrics['amount_of_data_removed_bytes']   AS BIGINT)) AS removed_bytes,
       SUM(CAST(operation_metrics['amount_of_clustered_data_bytes'] AS BIGINT)) AS clustered_bytes,
       SUM(CAST(usage_quantity AS DECIMAL(38,6)))                              AS clustering_estimated_dbu,
       -- status: worst-first band on clustering DBU spend per table (field heuristic; 50 / 200).
       CASE
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) IS NULL THEN 'NOT_ASSESSED'
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) >= 200 THEN 'CRITICAL'
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) >= 50 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`storage`.`predictive_optimization_operations_history`
WHERE operation_type = 'CLUSTERING'
  AND start_time >= current_date() - INTERVAL 30 DAYS AND start_time < current_date()
GROUP BY workspace_id, catalog_name, schema_name, table_id, table_name, operation_type
ORDER BY clustering_estimated_dbu DESC
) q
"""
CHECKS["po_clustering_column_churn"] = r"""
-- generated from dbt/models/databricks_direct/storage/d_po_clustering_column_churn.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/storage/po_clustering_column_churn.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT workspace_id, catalog_name, schema_name, table_id, table_name,
       operation_metrics['has_column_selection_changed'] AS has_column_selection_changed,
       operation_metrics['old_clustering_columns']        AS old_clustering_columns,
       operation_metrics['new_clustering_columns']        AS new_clustering_columns,
       operation_metrics['additional_reason']             AS additional_reason,
       MAX(end_time) AS last_selection_time,
       COUNT(*)      AS selection_event_count,
       -- status: worst-first band on repeated true churn events per table (field heuristic; 2 / 5).
       -- churn per table: each change picks a new key set, so one signature rarely repeats
       SUM(CASE WHEN operation_metrics['has_column_selection_changed'] = 'true' THEN COUNT(*) ELSE 0 END) OVER (PARTITION BY workspace_id, table_id) AS table_change_count,
       CASE
         WHEN operation_metrics['has_column_selection_changed'] IS NULL THEN 'NOT_ASSESSED'
         WHEN operation_metrics['has_column_selection_changed'] = 'true' AND SUM(CASE WHEN operation_metrics['has_column_selection_changed'] = 'true' THEN COUNT(*) ELSE 0 END) OVER (PARTITION BY workspace_id, table_id) >= 5 THEN 'CRITICAL'
         WHEN operation_metrics['has_column_selection_changed'] = 'true' AND SUM(CASE WHEN operation_metrics['has_column_selection_changed'] = 'true' THEN COUNT(*) ELSE 0 END) OVER (PARTITION BY workspace_id, table_id) >= 2 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`storage`.`predictive_optimization_operations_history`
WHERE operation_type = 'AUTO_CLUSTERING_COLUMN_SELECTION'
  AND start_time >= current_date() - INTERVAL 30 DAYS AND start_time < current_date()
GROUP BY workspace_id, catalog_name, schema_name, table_id, table_name,
         operation_metrics['has_column_selection_changed'], operation_metrics['old_clustering_columns'],
         operation_metrics['new_clustering_columns'], operation_metrics['additional_reason']
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END, table_change_count DESC, selection_event_count DESC
) q
"""
CHECKS["po_data_skipping_backfill"] = r"""
-- generated from dbt/models/databricks_direct/storage/d_po_data_skipping_backfill.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/storage/po_data_skipping_backfill.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
SELECT workspace_id, catalog_name, schema_name, table_id, table_name,
       operation_metrics['added_data_skipping_columns']   AS added_data_skipping_columns,
       operation_metrics['removed_data_skipping_columns'] AS removed_data_skipping_columns,
       operation_metrics['new_data_skipping_columns']     AS new_data_skipping_columns,
       SUM(CAST(operation_metrics['amount_of_scanned_bytes'] AS BIGINT)) AS scanned_bytes,
       SUM(CAST(operation_metrics['number_of_scanned_files'] AS BIGINT)) AS scanned_files,
       MAX(end_time) AS last_event_time,
       -- status: worst-first band on scan cost with zero ADDED-column gain (field heuristic; 100 / 500).
       -- new_data_skipping_columns is not a reliable gain signal (real events don't populate it) - see caveats.
       CASE
         WHEN SUM(CAST(operation_metrics['amount_of_scanned_bytes'] AS BIGINT)) IS NULL THEN 'NOT_ASSESSED'
         WHEN operation_metrics['added_data_skipping_columns'] IS NULL
              AND operation_metrics['removed_data_skipping_columns'] IS NULL
              AND operation_metrics['new_data_skipping_columns'] IS NULL THEN 'NOT_ASSESSED'
         WHEN (operation_metrics['added_data_skipping_columns'] IS NULL OR operation_metrics['added_data_skipping_columns'] = '')
              AND SUM(CAST(operation_metrics['amount_of_scanned_bytes'] AS BIGINT)) >= 500 * 1e9 THEN 'CRITICAL'
         WHEN (operation_metrics['added_data_skipping_columns'] IS NULL OR operation_metrics['added_data_skipping_columns'] = '')
              AND SUM(CAST(operation_metrics['amount_of_scanned_bytes'] AS BIGINT)) >= 100 * 1e9 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`storage`.`predictive_optimization_operations_history`
WHERE operation_type = 'DATA_SKIPPING_COLUMN_SELECTION'
  AND start_time >= current_date() - INTERVAL 30 DAYS AND start_time < current_date()
GROUP BY workspace_id, catalog_name, schema_name, table_id, table_name,
         operation_metrics['added_data_skipping_columns'], operation_metrics['removed_data_skipping_columns'],
         operation_metrics['new_data_skipping_columns']
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END, scanned_bytes DESC
) q
"""
CHECKS["po_failure_reasons"] = r"""
-- generated from dbt/models/databricks_direct/storage/d_po_failure_reasons.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/storage/po_failure_reasons.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH ops AS (
  SELECT workspace_id, catalog_name, schema_name, table_name, table_id, operation_type, operation_status,
         usage_unit, usage_quantity, start_time
  FROM `system`.`storage`.`predictive_optimization_operations_history`
  WHERE start_time >= current_date() - INTERVAL 30 DAYS
    AND start_time < current_date()
    AND operation_status LIKE 'FAILED%'
),
agg AS (
  SELECT workspace_id, catalog_name, schema_name, table_id, table_name,
         CONCAT(catalog_name, '.', schema_name, '.', table_name) AS full_name,
         operation_status,
         COUNT(*) AS failed_operations,
         COUNT(DISTINCT operation_type) AS operation_types_failed,
         SUM(CASE WHEN usage_unit = 'ESTIMATED_DBU' THEN usage_quantity ELSE 0 END) AS estimated_dbus_spent,
         MAX(start_time) AS last_failed_at
  FROM ops
  GROUP BY workspace_id, catalog_name, schema_name, table_id, table_name, operation_status
),
scored AS (
  SELECT a.*,
    CASE a.operation_status
      WHEN 'FAILED: PRIVATE_LINK_SETUP_ERROR' THEN 'network setup blocks predictive optimization from reaching this table'
      WHEN 'FAILED: AUTO_TTL_COLUMN_DOES_NOT_EXIST_ERROR' THEN 'auto-TTL is configured to a column that does not exist on this table'
      WHEN 'FAILED: INTERNAL_ERROR' THEN 'a Databricks-side error; usually resolves on retry'
      ELSE 'operation failed; see operation_status for detail'
    END AS reason,
    CASE a.operation_status
      WHEN 'FAILED: PRIVATE_LINK_SETUP_ERROR' THEN 'fix the private link / network path between the metastore and this table''s storage'
      WHEN 'FAILED: AUTO_TTL_COLUMN_DOES_NOT_EXIST_ERROR' THEN 'point auto-TTL at a column that exists, or drop the auto-TTL setting'
      WHEN 'FAILED: INTERNAL_ERROR' THEN 'retry; if it keeps failing, open a support ticket with the operation_type and time'
      ELSE 'check the table and re-run predictive optimization manually'
    END AS fix,
    CASE
      WHEN a.failed_operations >= 20 THEN 'CRITICAL'
      WHEN a.failed_operations >= 5 THEN 'WARN'
      ELSE 'OK'
    END AS status
  FROM agg a
),
flagged AS (
  -- Every non-OK row (WARN/CRITICAL) always keeps its own row - never pooled.
  SELECT workspace_id, catalog_name, schema_name, table_id, table_name, full_name, operation_status,
         FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
         operation_types_failed, failed_operations, estimated_dbus_spent, last_failed_at,
         reason, fix, status
  FROM scored
  WHERE status != 'OK'
),
ok_ranked AS (
  SELECT scored.*,
         ROW_NUMBER() OVER (ORDER BY failed_operations DESC, table_id, operation_status) AS rn
  FROM scored
  WHERE status = 'OK'
),
ok_kept AS (
  -- The top 50 OK rows by failed_operations, kept as their own row.
  SELECT workspace_id, catalog_name, schema_name, table_id, table_name, full_name, operation_status,
         FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
         operation_types_failed, failed_operations, estimated_dbus_spent, last_failed_at,
         reason, fix, status
  FROM ok_ranked
  WHERE rn <= 50
),
ok_pooled_raw AS (
  -- Every remaining OK row beyond 50 - re-aggregated into one is_other row below.
  SELECT * FROM ok_ranked WHERE rn > 50
),
other_row AS (
  SELECT
    -- Pooled across whichever workspaces its rows came from, so it can't claim a single one.
    CAST(NULL AS STRING) AS workspace_id,
    CAST(NULL AS STRING) AS catalog_name,
    CAST(NULL AS STRING) AS schema_name,
    CAST(NULL AS STRING) AS table_id,
    CAST(NULL AS STRING) AS table_name,
    CAST(NULL AS STRING) AS full_name,
    CAST(NULL AS STRING) AS operation_status,
    TRUE                  AS is_other,
    COUNT(*)              AS pooled_count,
    SUM(operation_types_failed) AS operation_types_failed,
    SUM(failed_operations)      AS failed_operations,
    ROUND(SUM(estimated_dbus_spent), 2) AS estimated_dbus_spent,
    MAX(last_failed_at)         AS last_failed_at,
    CAST(NULL AS STRING) AS reason,
    CAST(NULL AS STRING) AS fix,
    -- Every pooled row was already status=OK on its own - a rollup of small, healthy rows is never
    -- itself a finding, so this row always reads OK regardless of the combined failure count.
    'OK' AS status
  FROM ok_pooled_raw
)
SELECT * FROM (
  SELECT * FROM flagged
  UNION ALL
  SELECT * FROM ok_kept
  UNION ALL
  SELECT * FROM other_row WHERE pooled_count > 0
) u
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
         is_other,
         failed_operations DESC
) q
"""
CHECKS["storage_po_coverage"] = r"""
-- generated from dbt/models/databricks_direct/storage/d_storage_po_coverage.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/storage/storage_po_coverage.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH latest AS (
  -- the drop time is set only on a table's last snapshot, so drop after picking it; predictive
  -- optimization runs on managed tables only, and an unknown flag is neither on nor off
  SELECT catalog_name, schema_name, table_name, table_id, active_bytes, predictive_optimization_enabled
  FROM (
    SELECT *
    FROM `system`.`storage`.`table_metrics_history`
    WHERE snapshot_date >= current_date() - INTERVAL 30 DAYS
      AND snapshot_date < current_date()
    QUALIFY ROW_NUMBER() OVER (PARTITION BY table_id ORDER BY snapshot_date DESC) = 1
  ) s
  WHERE table_dropped_time IS NULL
    AND upper(COALESCE(table_type, 'MANAGED')) = 'MANAGED'
    AND predictive_optimization_enabled IS NOT NULL
),
by_catalog AS (
  SELECT
    'catalog_summary' AS section,
    catalog_name,
    CAST(NULL AS STRING) AS schema_name,
    CAST(NULL AS STRING) AS table_id,
    CAST(NULL AS STRING) AS table_name,
    CAST(NULL AS STRING) AS full_name,
    COUNT(*) AS tables_total,
    SUM(CASE WHEN predictive_optimization_enabled = FALSE THEN 1 ELSE 0 END) AS tables_without_po,
    ROUND(SUM(CASE WHEN predictive_optimization_enabled = FALSE THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1)
      AS pct_tables_without_po,
    SUM(active_bytes) AS bytes_total,
    SUM(CASE WHEN predictive_optimization_enabled = FALSE THEN active_bytes ELSE 0 END) AS bytes_without_po,
    ROUND(SUM(CASE WHEN predictive_optimization_enabled = FALSE THEN active_bytes ELSE 0 END) * 100.0
          / NULLIF(SUM(active_bytes), 0), 1) AS pct_bytes_without_po,
    CAST(NULL AS BIGINT) AS active_bytes_of_table
  FROM latest
  GROUP BY catalog_name
),
top_tables AS (
  SELECT
    'largest_table_without_po' AS section,
    t.catalog_name, t.schema_name, t.table_id, t.table_name,
    CONCAT(t.catalog_name, '.', t.schema_name, '.', t.table_name) AS full_name,
    CAST(NULL AS BIGINT) AS tables_total,
    CAST(NULL AS BIGINT) AS tables_without_po,
    CAST(NULL AS DOUBLE) AS pct_tables_without_po,
    CAST(NULL AS BIGINT) AS bytes_total,
    CAST(NULL AS BIGINT) AS bytes_without_po,
    CAST(NULL AS DOUBLE) AS pct_bytes_without_po,
    t.active_bytes AS active_bytes_of_table
  FROM latest t
  WHERE t.predictive_optimization_enabled = FALSE
  QUALIFY ROW_NUMBER() OVER (ORDER BY t.active_bytes DESC) <= 50
),
combined AS (
  SELECT * FROM by_catalog
  UNION ALL
  SELECT * FROM top_tables
)
SELECT c.*,
  CASE
    WHEN c.section = 'largest_table_without_po' THEN 'WARN'
    WHEN c.pct_bytes_without_po >= 50 THEN 'CRITICAL'
    WHEN c.pct_bytes_without_po >= 20 THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM combined c
ORDER BY CASE c.section WHEN 'catalog_summary' THEN 0 ELSE 1 END,
         CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
         c.pct_bytes_without_po DESC NULLS LAST,
         c.active_bytes_of_table DESC NULLS LAST
) q
"""
CHECKS["storage_small_files"] = r"""
-- generated from dbt/models/databricks_direct/storage/d_storage_small_files.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/storage/storage_small_files.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH latest AS (
  -- the drop time is set only on a table's last snapshot, so drop after picking it
  SELECT catalog_name, schema_name, table_name, table_id, table_type, table_owner,
         snapshot_date, active_bytes, active_files
  FROM (
    SELECT *
    FROM `system`.`storage`.`table_metrics_history`
    WHERE snapshot_date >= current_date() - INTERVAL 30 DAYS
      AND snapshot_date < current_date()
    QUALIFY ROW_NUMBER() OVER (PARTITION BY table_id ORDER BY snapshot_date DESC) = 1
  ) s
  WHERE table_dropped_time IS NULL
)
SELECT
  l.catalog_name,
  l.schema_name,
  l.table_name,
  CONCAT(l.catalog_name, '.', l.schema_name, '.', l.table_name) AS full_name,
  l.table_id,
  l.table_type,
  l.table_owner,
  l.snapshot_date,
  l.active_bytes,
  l.active_files,
  ROUND(l.active_bytes / NULLIF(l.active_files, 0) / 1024.0 / 1024.0, 2) AS avg_file_size_mb,
  CASE
    WHEN l.active_files > 1000
         AND l.active_bytes / NULLIF(l.active_files, 0) / 1024.0 / 1024.0 < 8 THEN 'CRITICAL'
    WHEN l.active_files > 1000
         AND l.active_bytes / NULLIF(l.active_files, 0) / 1024.0 / 1024.0 < 32 THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM latest l
WHERE l.active_files > 0
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, l.active_files DESC
LIMIT 100000
) q
"""
CHECKS["access_source_table_coverage"] = r"""
-- generated from dbt/models/databricks_direct/governance_access/d_access_source_table_coverage.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/governance_access/access_source_table_coverage.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH unioned AS (
SELECT 'system' AS catalog_name, 'access' AS schema_name, 'audit' AS table_name,
       TRUE AS has_time_column, 'event_date' AS time_column_name,
       SUM(CASE WHEN event_date >= date_sub(current_date(), 30) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(event_date) AS STRING) AS min_time,
       CAST(MAX(event_date) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN event_date >= date_sub(current_date(), 30) THEN CAST(event_date AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN event_date >= date_sub(current_date(), 30) THEN CAST(event_date AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN event_date >= date_sub(current_date(), 30) THEN CAST(event_date AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(event_date) AS DATE)) AS lag_days,
       CASE WHEN COUNT(*) = 0 THEN 'CRITICAL'
            WHEN SUM(CASE WHEN event_date >= date_sub(current_date(), 30) THEN 1 ELSE 0 END) = 0 THEN 'WARN'
            WHEN COUNT(DISTINCT CASE WHEN event_date >= date_sub(current_date(), 30) THEN CAST(event_date AS DATE) END) < (30 - 1) THEN 'WARN'
            WHEN datediff(current_date(), CAST(MAX(event_date) AS DATE)) > 2 THEN 'WARN'
            ELSE 'OK' END AS status
FROM `system`.`access`.`audit`
UNION ALL
SELECT 'system' AS catalog_name, 'access' AS schema_name, 'column_lineage' AS table_name,
       TRUE AS has_time_column, 'event_date' AS time_column_name,
       SUM(CASE WHEN event_date >= date_sub(current_date(), 30) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(event_date) AS STRING) AS min_time,
       CAST(MAX(event_date) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN event_date >= date_sub(current_date(), 30) THEN CAST(event_date AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN event_date >= date_sub(current_date(), 30) THEN CAST(event_date AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN event_date >= date_sub(current_date(), 30) THEN CAST(event_date AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(event_date) AS DATE)) AS lag_days,
       'OK' AS status
FROM `system`.`access`.`column_lineage`
UNION ALL
SELECT 'system' AS catalog_name, 'access' AS schema_name, 'inbound_network' AS table_name,
       TRUE AS has_time_column, 'event_time' AS time_column_name,
       SUM(CASE WHEN event_time >= date_sub(current_timestamp(), 30) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(event_time) AS STRING) AS min_time,
       CAST(MAX(event_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN event_time >= date_sub(current_timestamp(), 30) THEN CAST(event_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN event_time >= date_sub(current_timestamp(), 30) THEN CAST(event_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN event_time >= date_sub(current_timestamp(), 30) THEN CAST(event_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(event_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM `system`.`access`.`inbound_network`
UNION ALL
SELECT 'system' AS catalog_name, 'access' AS schema_name, 'outbound_network' AS table_name,
       TRUE AS has_time_column, 'event_time' AS time_column_name,
       SUM(CASE WHEN event_time >= date_sub(current_timestamp(), 30) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(event_time) AS STRING) AS min_time,
       CAST(MAX(event_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN event_time >= date_sub(current_timestamp(), 30) THEN CAST(event_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN event_time >= date_sub(current_timestamp(), 30) THEN CAST(event_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN event_time >= date_sub(current_timestamp(), 30) THEN CAST(event_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(event_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM `system`.`access`.`outbound_network`
UNION ALL
SELECT 'system' AS catalog_name, 'access' AS schema_name, 'table_lineage' AS table_name,
       TRUE AS has_time_column, 'event_date' AS time_column_name,
       SUM(CASE WHEN event_date >= date_sub(current_date(), 30) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(event_date) AS STRING) AS min_time,
       CAST(MAX(event_date) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN event_date >= date_sub(current_date(), 30) THEN CAST(event_date AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN event_date >= date_sub(current_date(), 30) THEN CAST(event_date AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN event_date >= date_sub(current_date(), 30) THEN CAST(event_date AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(event_date) AS DATE)) AS lag_days,
       'OK' AS status
FROM `system`.`access`.`table_lineage`
UNION ALL
SELECT 'system' AS catalog_name, 'access' AS schema_name, 'workspaces_latest' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM `system`.`access`.`workspaces_latest`
UNION ALL
SELECT 'system' AS catalog_name, 'ai_gateway' AS schema_name, 'usage' AS table_name,
       TRUE AS has_time_column, 'event_time' AS time_column_name,
       SUM(CASE WHEN event_time >= date_sub(current_timestamp(), 30) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(event_time) AS STRING) AS min_time,
       CAST(MAX(event_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN event_time >= date_sub(current_timestamp(), 30) THEN CAST(event_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN event_time >= date_sub(current_timestamp(), 30) THEN CAST(event_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN event_time >= date_sub(current_timestamp(), 30) THEN CAST(event_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(event_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM `system`.`ai_gateway`.`usage`
UNION ALL
SELECT 'system' AS catalog_name, 'billing' AS schema_name, 'attributed_usage' AS table_name,
       TRUE AS has_time_column, 'usage_date' AS time_column_name,
       SUM(CASE WHEN usage_date >= date_sub(current_date(), 30) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(usage_date) AS STRING) AS min_time,
       CAST(MAX(usage_date) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN usage_date >= date_sub(current_date(), 30) THEN CAST(usage_date AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN usage_date >= date_sub(current_date(), 30) THEN CAST(usage_date AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN usage_date >= date_sub(current_date(), 30) THEN CAST(usage_date AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(usage_date) AS DATE)) AS lag_days,
       'OK' AS status
FROM `system`.`billing`.`attributed_usage`
UNION ALL
SELECT 'system' AS catalog_name, 'billing' AS schema_name, 'list_prices' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
UNION ALL
SELECT 'system' AS catalog_name, 'billing' AS schema_name, 'usage' AS table_name,
       TRUE AS has_time_column, 'usage_date' AS time_column_name,
       SUM(CASE WHEN usage_date >= date_sub(current_date(), 30) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(usage_date) AS STRING) AS min_time,
       CAST(MAX(usage_date) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN usage_date >= date_sub(current_date(), 30) THEN CAST(usage_date AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN usage_date >= date_sub(current_date(), 30) THEN CAST(usage_date AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN usage_date >= date_sub(current_date(), 30) THEN CAST(usage_date AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(usage_date) AS DATE)) AS lag_days,
       CASE WHEN COUNT(*) = 0 THEN 'CRITICAL'
            WHEN SUM(CASE WHEN usage_date >= date_sub(current_date(), 30) THEN 1 ELSE 0 END) = 0 THEN 'WARN'
            WHEN COUNT(DISTINCT CASE WHEN usage_date >= date_sub(current_date(), 30) THEN CAST(usage_date AS DATE) END) < (30 - 1) THEN 'WARN'
            WHEN datediff(current_date(), CAST(MAX(usage_date) AS DATE)) > 2 THEN 'WARN'
            ELSE 'OK' END AS status
FROM `system`.`billing`.`usage`
UNION ALL
SELECT 'system' AS catalog_name, 'compute' AS schema_name, 'clusters' AS table_name,
       TRUE AS has_time_column, 'change_time' AS time_column_name,
       SUM(CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(change_time) AS STRING) AS min_time,
       CAST(MAX(change_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN CAST(change_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN CAST(change_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN CAST(change_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(change_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM `system`.`compute`.`clusters`
UNION ALL
SELECT 'system' AS catalog_name, 'compute' AS schema_name, 'instance_events' AS table_name,
       TRUE AS has_time_column, 'event_time' AS time_column_name,
       SUM(CASE WHEN event_time >= date_sub(current_timestamp(), 30) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(event_time) AS STRING) AS min_time,
       CAST(MAX(event_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN event_time >= date_sub(current_timestamp(), 30) THEN CAST(event_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN event_time >= date_sub(current_timestamp(), 30) THEN CAST(event_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN event_time >= date_sub(current_timestamp(), 30) THEN CAST(event_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(event_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM `system`.`compute`.`instance_events`
UNION ALL
SELECT 'system' AS catalog_name, 'compute' AS schema_name, 'instance_pools' AS table_name,
       TRUE AS has_time_column, 'change_time' AS time_column_name,
       SUM(CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(change_time) AS STRING) AS min_time,
       CAST(MAX(change_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN CAST(change_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN CAST(change_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN CAST(change_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(change_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM `system`.`compute`.`instance_pools`
UNION ALL
SELECT 'system' AS catalog_name, 'compute' AS schema_name, 'node_timeline' AS table_name,
       TRUE AS has_time_column, 'start_time' AS time_column_name,
       SUM(CASE WHEN start_time >= date_sub(current_timestamp(), 30) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(start_time) AS STRING) AS min_time,
       CAST(MAX(start_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN start_time >= date_sub(current_timestamp(), 30) THEN CAST(start_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN start_time >= date_sub(current_timestamp(), 30) THEN CAST(start_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN start_time >= date_sub(current_timestamp(), 30) THEN CAST(start_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(start_time) AS DATE)) AS lag_days,
       CASE WHEN COUNT(*) = 0 THEN 'CRITICAL'
            WHEN SUM(CASE WHEN start_time >= date_sub(current_timestamp(), 30) THEN 1 ELSE 0 END) = 0 THEN 'WARN'
            WHEN datediff(current_date(), CAST(MAX(start_time) AS DATE)) > 2 THEN 'WARN'
            ELSE 'OK' END AS status
FROM `system`.`compute`.`node_timeline`
UNION ALL
SELECT 'system' AS catalog_name, 'compute' AS schema_name, 'node_types' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM `system`.`compute`.`node_types`
UNION ALL
SELECT 'system' AS catalog_name, 'compute' AS schema_name, 'warehouse_events' AS table_name,
       TRUE AS has_time_column, 'event_time' AS time_column_name,
       SUM(CASE WHEN event_time >= date_sub(current_timestamp(), 30) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(event_time) AS STRING) AS min_time,
       CAST(MAX(event_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN event_time >= date_sub(current_timestamp(), 30) THEN CAST(event_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN event_time >= date_sub(current_timestamp(), 30) THEN CAST(event_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN event_time >= date_sub(current_timestamp(), 30) THEN CAST(event_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(event_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM `system`.`compute`.`warehouse_events`
UNION ALL
SELECT 'system' AS catalog_name, 'compute' AS schema_name, 'warehouses' AS table_name,
       TRUE AS has_time_column, 'change_time' AS time_column_name,
       SUM(CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(change_time) AS STRING) AS min_time,
       CAST(MAX(change_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN CAST(change_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN CAST(change_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN CAST(change_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(change_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM `system`.`compute`.`warehouses`
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'catalog_privileges' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM `system`.`information_schema`.`catalog_privileges`
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'column_masks' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM `system`.`information_schema`.`column_masks`
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'column_tags' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM `system`.`information_schema`.`column_tags`
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'connection_privileges' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM `system`.`information_schema`.`connection_privileges`
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'credential_privileges' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM `system`.`information_schema`.`credential_privileges`
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'external_location_privileges' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM `system`.`information_schema`.`external_location_privileges`
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'row_filters' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM `system`.`information_schema`.`row_filters`
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'schema_privileges' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM `system`.`information_schema`.`schema_privileges`
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'schema_share_usage' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM `system`.`information_schema`.`schema_share_usage`
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'schema_tags' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM `system`.`information_schema`.`schema_tags`
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'share_recipient_privileges' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM `system`.`information_schema`.`share_recipient_privileges`
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'shares' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM `system`.`information_schema`.`shares`
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'table_privileges' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM `system`.`information_schema`.`table_privileges`
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'table_share_usage' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM `system`.`information_schema`.`table_share_usage`
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'table_tags' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM `system`.`information_schema`.`table_tags`
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'tables' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM `system`.`information_schema`.`tables`
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'views' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM `system`.`information_schema`.`views`
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'volume_tags' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM `system`.`information_schema`.`volume_tags`
UNION ALL
SELECT 'system' AS catalog_name, 'information_schema' AS schema_name, 'volumes' AS table_name,
       FALSE AS has_time_column, CAST(NULL AS STRING) AS time_column_name,
       COUNT(*) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(NULL AS STRING) AS min_time,
       CAST(NULL AS STRING) AS max_time,
       CAST(NULL AS BIGINT) AS days_with_data_in_window,
       CAST(NULL AS STRING) AS first_date_in_window,
       CAST(NULL AS STRING) AS last_date_in_window,
       CAST(NULL AS BIGINT) AS lag_days,
       'OK' AS status
FROM `system`.`information_schema`.`volumes`
UNION ALL
SELECT 'system' AS catalog_name, 'lakeflow' AS schema_name, 'job_run_timeline' AS table_name,
       TRUE AS has_time_column, 'period_start_time' AS time_column_name,
       SUM(CASE WHEN period_start_time >= date_sub(current_timestamp(), 30) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(period_start_time) AS STRING) AS min_time,
       CAST(MAX(period_start_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN period_start_time >= date_sub(current_timestamp(), 30) THEN CAST(period_start_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN period_start_time >= date_sub(current_timestamp(), 30) THEN CAST(period_start_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN period_start_time >= date_sub(current_timestamp(), 30) THEN CAST(period_start_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(period_start_time) AS DATE)) AS lag_days,
       CASE WHEN COUNT(*) = 0 THEN 'CRITICAL'
            WHEN SUM(CASE WHEN period_start_time >= date_sub(current_timestamp(), 30) THEN 1 ELSE 0 END) = 0 THEN 'WARN'
            WHEN datediff(current_date(), CAST(MAX(period_start_time) AS DATE)) > 2 THEN 'WARN'
            ELSE 'OK' END AS status
FROM `system`.`lakeflow`.`job_run_timeline`
UNION ALL
SELECT 'system' AS catalog_name, 'lakeflow' AS schema_name, 'job_task_run_timeline' AS table_name,
       TRUE AS has_time_column, 'period_start_time' AS time_column_name,
       SUM(CASE WHEN period_start_time >= date_sub(current_timestamp(), 30) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(period_start_time) AS STRING) AS min_time,
       CAST(MAX(period_start_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN period_start_time >= date_sub(current_timestamp(), 30) THEN CAST(period_start_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN period_start_time >= date_sub(current_timestamp(), 30) THEN CAST(period_start_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN period_start_time >= date_sub(current_timestamp(), 30) THEN CAST(period_start_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(period_start_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM `system`.`lakeflow`.`job_task_run_timeline`
UNION ALL
SELECT 'system' AS catalog_name, 'lakeflow' AS schema_name, 'job_tasks' AS table_name,
       TRUE AS has_time_column, 'change_time' AS time_column_name,
       SUM(CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(change_time) AS STRING) AS min_time,
       CAST(MAX(change_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN CAST(change_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN CAST(change_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN CAST(change_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(change_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM `system`.`lakeflow`.`job_tasks`
UNION ALL
SELECT 'system' AS catalog_name, 'lakeflow' AS schema_name, 'jobs' AS table_name,
       TRUE AS has_time_column, 'change_time' AS time_column_name,
       SUM(CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(change_time) AS STRING) AS min_time,
       CAST(MAX(change_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN CAST(change_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN CAST(change_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN CAST(change_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(change_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM `system`.`lakeflow`.`jobs`
UNION ALL
SELECT 'system' AS catalog_name, 'lakeflow' AS schema_name, 'pipeline_update_timeline' AS table_name,
       TRUE AS has_time_column, 'period_start_time' AS time_column_name,
       SUM(CASE WHEN period_start_time >= date_sub(current_timestamp(), 30) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(period_start_time) AS STRING) AS min_time,
       CAST(MAX(period_start_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN period_start_time >= date_sub(current_timestamp(), 30) THEN CAST(period_start_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN period_start_time >= date_sub(current_timestamp(), 30) THEN CAST(period_start_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN period_start_time >= date_sub(current_timestamp(), 30) THEN CAST(period_start_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(period_start_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM `system`.`lakeflow`.`pipeline_update_timeline`
UNION ALL
SELECT 'system' AS catalog_name, 'lakeflow' AS schema_name, 'pipelines' AS table_name,
       TRUE AS has_time_column, 'change_time' AS time_column_name,
       SUM(CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(change_time) AS STRING) AS min_time,
       CAST(MAX(change_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN CAST(change_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN CAST(change_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN CAST(change_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(change_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM `system`.`lakeflow`.`pipelines`
UNION ALL
SELECT 'system' AS catalog_name, 'query' AS schema_name, 'history' AS table_name,
       TRUE AS has_time_column, 'start_time' AS time_column_name,
       SUM(CASE WHEN start_time >= date_sub(current_timestamp(), 30) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(start_time) AS STRING) AS min_time,
       CAST(MAX(start_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN start_time >= date_sub(current_timestamp(), 30) THEN CAST(start_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN start_time >= date_sub(current_timestamp(), 30) THEN CAST(start_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN start_time >= date_sub(current_timestamp(), 30) THEN CAST(start_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(start_time) AS DATE)) AS lag_days,
       CASE WHEN COUNT(*) = 0 THEN 'CRITICAL'
            WHEN SUM(CASE WHEN start_time >= date_sub(current_timestamp(), 30) THEN 1 ELSE 0 END) = 0 THEN 'WARN'
            WHEN datediff(current_date(), CAST(MAX(start_time) AS DATE)) > 2 THEN 'WARN'
            ELSE 'OK' END AS status
FROM `system`.`query`.`history`
UNION ALL
SELECT 'system' AS catalog_name, 'serving' AS schema_name, 'endpoint_usage' AS table_name,
       TRUE AS has_time_column, 'request_time' AS time_column_name,
       SUM(CASE WHEN request_time >= date_sub(current_timestamp(), 30) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(request_time) AS STRING) AS min_time,
       CAST(MAX(request_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN request_time >= date_sub(current_timestamp(), 30) THEN CAST(request_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN request_time >= date_sub(current_timestamp(), 30) THEN CAST(request_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN request_time >= date_sub(current_timestamp(), 30) THEN CAST(request_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(request_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM `system`.`serving`.`endpoint_usage`
UNION ALL
SELECT 'system' AS catalog_name, 'serving' AS schema_name, 'served_entities' AS table_name,
       TRUE AS has_time_column, 'change_time' AS time_column_name,
       SUM(CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN 1 ELSE 0 END) AS rows_in_window,
       COUNT(*) AS total_row_count,
       CAST(MIN(change_time) AS STRING) AS min_time,
       CAST(MAX(change_time) AS STRING) AS max_time,
       COUNT(DISTINCT CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN CAST(change_time AS DATE) END) AS days_with_data_in_window,
       CAST(MIN(CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN CAST(change_time AS DATE) END) AS STRING) AS first_date_in_window,
       CAST(MAX(CASE WHEN change_time >= date_sub(current_timestamp(), 30) THEN CAST(change_time AS DATE) END) AS STRING) AS last_date_in_window,
       datediff(current_date(), CAST(MAX(change_time) AS DATE)) AS lag_days,
       'OK' AS status
FROM `system`.`serving`.`served_entities`
)
SELECT * FROM unioned
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, schema_name, table_name
) q
"""
CHECKS["storage_unused_table_cost"] = r"""
-- generated from dbt/models/databricks_direct/storage/d_storage_unused_table_cost.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/storage/storage_unused_table_cost.sql; fix the source query and regenerate, never edit this file.
SELECT 30 AS window_days, q.*
FROM (
WITH lineage_window AS (
  SELECT *
  FROM `system`.`access`.`table_lineage`
  WHERE event_date >= dateadd(day, -30, current_date())
    AND event_date < current_date()
),
source_tables AS (
  SELECT DISTINCT
         source_table_catalog AS catalog,
         source_table_schema  AS schema,
         source_table_name    AS name
  FROM lineage_window
  WHERE source_table_name IS NOT NULL
),
last_write AS (
  SELECT target_table_catalog AS catalog,
         target_table_schema  AS schema,
         target_table_name    AS name,
         MAX(event_time)      AS last_write_time
  FROM `system`.`access`.`table_lineage`
  WHERE target_table_name IS NOT NULL
  GROUP BY target_table_catalog, target_table_schema, target_table_name
),
inventory AS (
  SELECT table_catalog, table_schema, table_name, table_type, table_owner, last_altered,
         -- Databricks' own tables (samples, owned by System user): kept out of the list, their $ pooled below.
         (table_catalog = 'samples' OR COALESCE(table_owner, '') = 'System user') AS is_system_owned
  FROM `system`.`information_schema`.`tables`
  WHERE table_catalog <> 'system'
    AND table_schema <> 'information_schema'
    AND table_type IN ('MANAGED', 'EXTERNAL')
),
unused AS (
  SELECT inv.*
  FROM inventory inv
  LEFT JOIN source_tables src
    ON  inv.table_catalog = src.catalog
    AND inv.table_schema  = src.schema
    AND inv.table_name    = src.name
  WHERE src.name IS NULL          -- never appeared as a lineage source in the window
),
latest_size AS (
  SELECT catalog_name, schema_name, table_name, active_bytes
  FROM `system`.`storage`.`table_metrics_history`
  WHERE snapshot_date >= current_date() - INTERVAL 30 DAYS
    AND snapshot_date < current_date()
    AND table_dropped_time IS NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY catalog_name, schema_name, table_name ORDER BY snapshot_date DESC
  ) = 1
),
po_usage AS (
  SELECT catalog_name, schema_name, table_name,
         SUM(CAST(usage_quantity AS DOUBLE)) AS po_dbu_sum
  FROM `system`.`storage`.`predictive_optimization_operations_history`
  WHERE start_time >= current_date() - INTERVAL 30 DAYS
    AND start_time <  current_date()
  GROUP BY catalog_name, schema_name, table_name
),
po_billed AS (
  SELECT u.sku_name,
         u.usage_quantity                AS usage_quantity,
         u.usage_quantity * lp.list_rate AS list_cost,
         lp.list_rate                    AS list_rate
  FROM `system`.`billing`.`usage` u
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
    FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
  ) lp
    ON u.sku_name   = lp.sku_name
   AND u.cloud      = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.billing_origin_product = 'PREDICTIVE_OPTIMIZATION'
    AND u.usage_date >= dateadd(day, -30, current_date())
    AND u.usage_date <  current_date()
),
po_rate AS (
  SELECT
    -- priced DBUs only: unpriced ones in the divisor would halve the rate
    SUM(list_cost) / NULLIF(SUM(CASE WHEN list_rate IS NOT NULL THEN usage_quantity END), 0) AS dbu_rate,
    -- price_basis: 'no_po_activity' when the account has no PREDICTIVE_OPTIMIZATION billing rows
    -- in the window at all (COUNT(*) guards the SUMs below, which are NULL - not 0 - over zero
    -- rows and would otherwise fall through to the wrong branch); otherwise the same 'unpriced' /
    -- 'free' / 'priced' disclosure every other priced check here uses.
    CASE
      WHEN COUNT(*) = 0 THEN 'no_po_activity'
      WHEN SUM(CASE WHEN list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                    THEN usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
      WHEN SUM(CASE WHEN list_rate IS NOT NULL THEN usage_quantity ELSE 0 END) = 0 THEN 'free'
      ELSE 'priced'
    END AS price_basis
  FROM po_billed
),
sized AS (
  SELECT
    u.table_catalog, u.table_schema, u.table_name, u.table_type, u.table_owner, u.last_altered, u.is_system_owned,
    ls.active_bytes,
    COALESCE(po.po_dbu_sum, 0) AS po_dbu_sum,
    lw.last_write_time,
    r.dbu_rate,
    r.price_basis
  FROM unused u
  LEFT JOIN latest_size ls
    ON  u.table_catalog = ls.catalog_name
    AND u.table_schema  = ls.schema_name
    AND u.table_name    = ls.table_name
  LEFT JOIN po_usage po
    ON  u.table_catalog = po.catalog_name
    AND u.table_schema  = po.schema_name
    AND u.table_name    = po.table_name
  LEFT JOIN last_write lw
    ON  u.table_catalog = lw.catalog
    AND u.table_schema  = lw.schema
    AND u.table_name    = lw.name
  CROSS JOIN po_rate r
),
priced AS (
  SELECT
    s.*,
    CASE WHEN s.active_bytes IS NULL THEN NULL
         ELSE ROUND(s.active_bytes / POWER(1024.0, 3), 3) END AS active_gb,
    CASE WHEN s.active_bytes IS NULL THEN NULL
         ELSE ROUND(s.active_bytes / POWER(1024.0, 3) * 0.02, 2) END
      AS est_storage_usd_month,
    -- A table with zero Predictive Optimization DBUs in the window is a real, known $0, whatever
    -- the account-wide rate reads - only a NONZERO DBU count with no derivable rate is unknown.
    CASE WHEN s.po_dbu_sum = 0 THEN 0.0 ELSE ROUND(s.po_dbu_sum * s.dbu_rate, 2) END
      AS po_upkeep_usd_window
  FROM sized s
),
scored AS (
  SELECT
    p.*,
    datediff(current_date(), DATE(p.last_altered))     AS days_since_altered,
    datediff(current_date(), DATE(p.last_write_time))  AS days_since_last_write,
    CASE
      WHEN p.active_bytes IS NULL THEN NULL
      WHEN p.po_dbu_sum > 0 AND p.po_upkeep_usd_window IS NULL THEN NULL
      ELSE ROUND(p.est_storage_usd_month + COALESCE(p.po_upkeep_usd_window, 0) * 30.0 / 30, 2)
    END AS est_total_usd_month,
    -- No size on record is not a gap in the check: the table is unused, only its cost is unknown.
    CASE
      WHEN p.active_bytes IS NOT NULL AND p.po_dbu_sum > 0 AND p.po_upkeep_usd_window IS NULL
        THEN 'po_upkeep_unpriced'
    END AS not_assessed_reason
  FROM priced p
),
banded AS (
  SELECT
    sc.*,
    CASE
      WHEN sc.not_assessed_reason IS NOT NULL      THEN 'NOT_ASSESSED'
      WHEN sc.est_total_usd_month IS NULL          THEN 'OK'
      WHEN sc.est_total_usd_month >= 100 THEN 'CRITICAL'
      WHEN sc.est_total_usd_month >= 20 THEN 'WARN'
      ELSE 'OK'
    END AS status
  FROM scored sc
),
assessed AS (
  -- table_catalog/schema/name break a $ tie deterministically (same tiebreak style
  -- cost_chargeback_by_warehouse's own poolable_ranked uses), so which table lands in the pooled
  -- row is stable across runs, never arbitrary.
  SELECT b.*, ROW_NUMBER() OVER (
    ORDER BY b.est_total_usd_month DESC, b.table_catalog, b.table_schema, b.table_name
  ) AS rn
  FROM banded b
  WHERE b.est_total_usd_month IS NOT NULL AND NOT b.is_system_owned
),
unpriced_rows AS (
  SELECT b.* FROM banded b WHERE b.est_total_usd_month IS NULL AND NOT b.is_system_owned
),
kept AS (
  SELECT
    table_catalog, table_schema, table_name, table_type,
    table_owner AS table_owner,
    active_gb, est_storage_usd_month, po_upkeep_usd_window, price_basis, est_total_usd_month,
    days_since_altered, days_since_last_write,
    FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
    not_assessed_reason, status
  FROM assessed
  WHERE rn <= 500
  UNION ALL
  SELECT
    table_catalog, table_schema, table_name, table_type,
    table_owner AS table_owner,
    active_gb, est_storage_usd_month, po_upkeep_usd_window,
    CASE WHEN active_gb IS NULL THEN 'no_size' ELSE price_basis END AS price_basis,
    est_total_usd_month,
    days_since_altered, days_since_last_write,
    FALSE AS is_other, CAST(NULL AS BIGINT) AS pooled_count,
    not_assessed_reason, status
  FROM unpriced_rows
),
pooled AS (
  -- Every remaining priced table beyond 500, re-aggregated into one is_other row - always
  -- status=OK (see caveats), only ever pooling rows that were already priced and below the floor.
  SELECT
    CAST(NULL AS STRING) AS table_catalog, CAST(NULL AS STRING) AS table_schema,
    CAST(NULL AS STRING) AS table_name, CAST(NULL AS STRING) AS table_type,
    CAST(NULL AS STRING) AS table_owner,
    ROUND(SUM(active_gb), 3)              AS active_gb,
    ROUND(SUM(est_storage_usd_month), 2)  AS est_storage_usd_month,
    ROUND(SUM(po_upkeep_usd_window), 2)   AS po_upkeep_usd_window,
    MAX(price_basis)                      AS price_basis,
    ROUND(SUM(est_total_usd_month), 2)    AS est_total_usd_month,
    CAST(NULL AS BIGINT) AS days_since_altered, CAST(NULL AS BIGINT) AS days_since_last_write,
    TRUE AS is_other, COUNT(*) AS pooled_count,
    CAST(NULL AS STRING) AS not_assessed_reason,
    'OK' AS status
  FROM assessed
  WHERE rn > 500
),
system_pooled AS (
  -- Databricks-owned tables in one is_other row, table_name '(Databricks-owned)': their cost stays
  -- counted, the tables stay out of the list.
  SELECT
    CAST(NULL AS STRING) AS table_catalog, CAST(NULL AS STRING) AS table_schema,
    '(Databricks-owned)' AS table_name, CAST(NULL AS STRING) AS table_type,
    'System user' AS table_owner,
    ROUND(SUM(active_gb), 3)              AS active_gb,
    ROUND(SUM(est_storage_usd_month), 2)  AS est_storage_usd_month,
    ROUND(SUM(po_upkeep_usd_window), 2)   AS po_upkeep_usd_window,
    MAX(price_basis)                      AS price_basis,
    ROUND(SUM(est_total_usd_month), 2)    AS est_total_usd_month,
    CAST(NULL AS BIGINT) AS days_since_altered, CAST(NULL AS BIGINT) AS days_since_last_write,
    TRUE AS is_other, COUNT(*) AS pooled_count,
    CAST(NULL AS STRING) AS not_assessed_reason,
    'OK' AS status
  FROM banded
  WHERE is_system_owned
)
SELECT * FROM (
  SELECT * FROM kept
  UNION ALL
  SELECT * FROM pooled WHERE pooled_count > 0
  UNION ALL
  SELECT * FROM system_pooled WHERE pooled_count > 0
) u
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         is_other,
         est_total_usd_month DESC NULLS LAST,
         table_catalog, table_schema, table_name
) q
"""


# COMMAND ----------

# the columns a check returns, for the empty table a failure leaves behind
EMPTY = {
    "dim_workspace": "snapshot_at TIMESTAMP, workspace_id STRING, name STRING",
    "dim_warehouse": "snapshot_at TIMESTAMP, workspace_id STRING, warehouse_id STRING, warehouse_name STRING",
    "dim_job": "snapshot_at TIMESTAMP, workspace_id STRING, job_id STRING, name STRING",
    "dim_cluster": "snapshot_at TIMESTAMP, workspace_id STRING, cluster_id STRING, cluster_name STRING",
    "dim_pipeline": "snapshot_at TIMESTAMP, workspace_id STRING, pipeline_id STRING, pipeline_name STRING",
    "dim_notebook": "snapshot_at TIMESTAMP, workspace_id STRING, notebook_id STRING, notebook_path STRING",
    "compute_warehouse_idle_minutes": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `warehouse_id` STRING, `warehouse_kind` STRING, `auto_stop_minutes` INT, `start_state_known` BOOLEAN, `open_at_window_end` BOOLEAN, `running_periods` BIGINT, `up_minutes` DOUBLE, `running_minutes` DOUBLE, `busy_minutes` DOUBLE, `idle_minutes` DOUBLE, `counted_idle_minutes` DOUBLE, `counted_idle_gaps` DECIMAL(38,0), `longest_idle_gap_minutes` DOUBLE, `start_gap_minutes` DOUBLE, `between_queries_minutes` DOUBLE, `stop_tail_minutes` DOUBLE, `no_query_minutes` DOUBLE, `idle_share_pct` DOUBLE, `up_cluster_minutes` DOUBLE, `idle_cluster_minutes` DOUBLE, `max_clusters_seen` INT, `net_dbus` DOUBLE, `est_usd_list` DOUBLE, `est_wasted_usd_list` DOUBLE, `price_basis` STRING, `idle_gap_seconds` INT, `autostop_1_idle_minutes` DOUBLE, `autostop_1_cold_starts` DECIMAL(38,0), `autostop_2_idle_minutes` DOUBLE, `autostop_2_cold_starts` DECIMAL(38,0), `autostop_5_idle_minutes` DOUBLE, `autostop_5_cold_starts` DECIMAL(38,0), `autostop_10_idle_minutes` DOUBLE, `autostop_10_cold_starts` DECIMAL(38,0), `autostop_30_idle_minutes` DOUBLE, `autostop_30_cold_starts` DECIMAL(38,0), `autostop_now_idle_minutes` DOUBLE, `autostop_now_cold_starts` DECIMAL(38,0), `usd_per_cluster_minute` DOUBLE, `waste_reason` STRING, `not_assessed_reason` STRING, `status` STRING",
    "compute_idle_node_ratio": "snapshot_at TIMESTAMP, `window_days` INT, `cluster_id` STRING, `node_type` STRING, `total_slices` BIGINT, `idle_slices` DECIMAL(38,0), `idle_ratio` DOUBLE, `worker_node_type_variants` BIGINT, `avg_cpu_pct_all` DOUBLE, `peak_cpu_pct_all` DOUBLE, `avg_mem_pct_all` DOUBLE, `peak_mem_pct_all` DOUBLE, `first_slice` TIMESTAMP, `last_slice` TIMESTAMP, `net_dbus` DOUBLE, `est_usd_list` DOUBLE, `price_basis` STRING, `est_wasted_usd_list` DOUBLE, `status` STRING",
    "lakeflow_failed_jobs_wasted_dbus": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `job_id` STRING, `distinct_runs` BIGINT, `failed_runs` DECIMAL(38,0), `repaired_runs` DECIMAL(38,0), `cancelled_runs` DECIMAL(38,0), `failed_runs_unbilled` DECIMAL(38,0), `failure_rate_pct` DOUBLE, `last_failed_termination_code` STRING, `last_failed_run_id` STRING, `wasted_dbus` DOUBLE, `est_failed_usd_list` DOUBLE, `est_repair_usd_list` DOUBLE, `est_wasted_usd_list` DOUBLE, `est_cancelled_usd_list` DOUBLE, `net_dbus` DOUBLE, `est_usd_list` DOUBLE, `est_unattributed_usd_list` DOUBLE, `wasted_share_pct` DOUBLE, `price_basis` STRING, `waste_reason` STRING, `not_assessed_reason` STRING, `status` STRING",
    "instance_pools_idle_capacity": "snapshot_at TIMESTAMP, `window_days` INT, `instance_pool_id` STRING, `instance_pool_name` STRING, `node_type` STRING, `min_idle_instances` BIGINT, `max_capacity` BIGINT, `idle_instance_autotermination_minutes` BIGINT, `enable_elastic_disk` BOOLEAN, `preloaded_spark_version` STRING, `preloaded_docker_images` ARRAY<STRING>, `tags` MAP<STRING, STRING>, `aws_attributes` STRUCT<availability: STRING, zone_id: STRING, spot_bid_price_percent: BIGINT>, `azure_attributes` STRUCT<availability: STRING, spot_bid_max_price: DOUBLE>, `gcp_attributes` STRUCT<zone_id: STRING, local_ssd_count: BIGINT, availability: STRING>, `disk_spec` STRUCT<disk_type: STRING, disk_count: BIGINT, disk_size: BIGINT, disk_iops: BIGINT, disk_throughput: BIGINT>, `create_time` TIMESTAMP, `delete_time` TIMESTAMP, `change_time` TIMESTAMP, `workspace_id` STRING, `account_id` STRING, `net_dbus` DOUBLE, `est_usd_list` DOUBLE, `price_basis` STRING, `status` STRING",
    "lakeflow_jobs_on_all_purpose": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `job_id` STRING, `compute_id` STRING, `cluster_source` STRING, `task_runs` BIGINT, `net_dbus` DOUBLE, `est_usd_list` DOUBLE, `price_basis` STRING, `cost_basis` STRING, `jobs_sharing_cluster` BIGINT, `est_usd_list_share` DOUBLE, `status` STRING",
    "lakeflow_stale_zombie_jobs": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `job_id` STRING, `name` STRING, `last_run_start` TIMESTAMP, `create_time` TIMESTAMP, `capture_floor_days` BIGINT, `is_stale` INT, `net_dbus` DOUBLE, `est_usd_list` DOUBLE, `price_basis` STRING, `status` STRING, `not_assessed_reason` STRING",
    "lakeflow_pipeline_idle_tail_duration": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `pipeline_id` STRING, `pipeline_type` STRING, `setting_continuous` BOOLEAN, `setting_development` BOOLEAN, `updates` BIGINT, `active_seconds_total` DOUBLE, `net_dbus` DOUBLE, `est_usd_list` DOUBLE, `price_basis` STRING, `idle_dbus` DOUBLE, `idle_share` DOUBLE, `status` STRING",
    "cost_premium_serverless_photon": "snapshot_at TIMESTAMP, `window_days` INT, `usage_date` DATE, `cloud` STRING, `workspace_id` STRING, `sku_name` STRING, `billing_origin_product` STRING, `is_serverless` BOOLEAN, `is_photon` BOOLEAN, `jobs_tier` STRING, `sql_tier` STRING, `dlt_tier` STRING, `performance_target` STRING, `net_usage_quantity` DOUBLE",
    "compute_warehouse_cache_reuse": "snapshot_at TIMESTAMP, `window_days` INT, `warehouse_id` STRING, `warehouse_name` STRING, `warehouse_kind` STRING, `auto_stop_minutes` INT, `queries` BIGINT, `select_queries` DECIMAL(38,0), `other_statements` DECIMAL(38,0), `from_result_cache_pct` DOUBLE, `avg_read_io_cache_percent` DOUBLE, `repeated_statement_pct` DOUBLE, `median_gap_minutes` DOUBLE, `status` STRING",
    "cost_dollarized_by_sku_day": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `usage_date` DATE, `cloud` STRING, `sku_name` STRING, `billing_origin_product` STRING, `usage_type` STRING, `usage_unit` STRING, `currency_code` STRING, `net_usage_quantity` DOUBLE, `net_list_cost` DOUBLE, `price_basis` STRING",
    "cost_period_over_period": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `billing_origin_product` STRING, `est_current_usd_list` DOUBLE, `est_previous_usd_list` DOUBLE, `est_change_usd_list` DOUBLE, `change_pct` DOUBLE, `price_basis` STRING, `status` STRING, `not_assessed_reason` STRING",
    "cost_monthly_actuals": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `billing_origin_product` STRING, `month_start` DATE, `currency_code` STRING, `net_list_cost_usd` DOUBLE, `days_captured` BIGINT, `is_partial_month` BOOLEAN, `partial_reason` STRING, `price_basis` STRING, `first_day` DATE, `last_day` DATE",
    "cost_unnamed_workspaces": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `first_used` DATE, `last_used` DATE, `active_days` BIGINT, `dbus_365d` DOUBLE, `net_list_cost_usd_365d` DOUBLE, `net_list_cost_usd` DOUBLE, `lifetime_days` BIGINT, `short_lived` BOOLEAN, `status` STRING",
    "query_failed_queries_daily": "snapshot_at TIMESTAMP, `window_days` INT, `day` DATE, `workspace_id` STRING, `compute_type` STRING, `warehouse_id` STRING, `execution_status` STRING, `statement_type` STRING, `executed_by` STRING, `query_count` BIGINT, `total_duration_ms_sum` DECIMAL(38,0), `execution_duration_ms_sum` DECIMAL(38,0), `error_message_sample` STRING, `warehouse_day_type_count` DECIMAL(38,0), `status` STRING",
    "query_queuing_waits": "snapshot_at TIMESTAMP, `window_days` INT, `day` DATE, `workspace_id` STRING, `compute_type` STRING, `warehouse_id` STRING, `query_count` BIGINT, `queued_at_capacity_count` DECIMAL(38,0), `waited_for_compute_count` DECIMAL(38,0), `waiting_at_capacity_ms_sum` DECIMAL(38,0), `waiting_for_compute_ms_sum` DECIMAL(38,0), `total_duration_ms_sum` DECIMAL(38,0), `avg_wait_ms` DOUBLE, `avg_slot_wait_ms` DOUBLE, `status` STRING",
    "perf_daily_by_resource": "snapshot_at TIMESTAMP, `window_days` INT, `usage_date` DATE, `resource_type` STRING, `resource_key` STRING, `workspace_id` STRING, `resource_id` STRING, `warehouse_id` STRING, `job_id` STRING, `pipeline_id` STRING, `is_other` BOOLEAN, `pooled_count` BIGINT, `runs` DECIMAL(38,0), `failed` DECIMAL(38,0), `total_s` DOUBLE, `queued_s` DOUBLE, `exec_s` DOUBLE, `spilled_gb` DOUBLE, `task_runs` DECIMAL(38,0), `setup_s` DOUBLE, `slot_wait_s` DOUBLE, `provision_s` DOUBLE, `shuffle_gb` DOUBLE, `read_gb` DOUBLE, `cache_read_gb` DOUBLE, `result_cache_runs` DECIMAL(38,0), `fetch_s` DOUBLE, `provision_clock_s` DOUBLE, `starts` DECIMAL(38,0), `start_s` DOUBLE, `start_s_max` DOUBLE, `failed_starts` DECIMAL(38,0)",
    "lakeflow_job_reliability": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `job_id` STRING, `job_name` STRING, `runs` BIGINT, `failed_runs` DECIMAL(38,0), `failure_rate_pct` DOUBLE, `runs_with_retry` DECIMAL(38,0), `last_n_runs_considered` BIGINT, `last_n_failed` DECIMAL(38,0), `last_n_summary` STRING, `consecutive_failures` DECIMAL(38,0), `latest_run_start` TIMESTAMP, `latest_result_state` STRING, `latest_termination_code` STRING, `successful_runs` DECIMAL(38,0), `p95_success_minutes` DOUBLE, `max_success_minutes` DOUBLE, `suggested_timeout_minutes` DOUBLE, `status` STRING, `not_assessed_reason` STRING",
    "lakeflow_job_duration_regression": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `job_id` STRING, `job_name` STRING, `recent_runs` DECIMAL(38,0), `recent_runs_repaired` DECIMAL(38,0), `baseline_runs` DECIMAL(38,0), `baseline_runs_repaired` DECIMAL(38,0), `recent_median_minutes` DOUBLE, `baseline_median_minutes` DOUBLE, `slowdown_ratio` DOUBLE, `recent_queue_s` DOUBLE, `baseline_queue_s` DOUBLE, `recent_setup_s` DOUBLE, `baseline_setup_s` DOUBLE, `recent_execution_s` DOUBLE, `baseline_execution_s` DOUBLE, `grew_most` STRING, `status` STRING, `not_assessed_reason` STRING",
    "cost_chargeback_by_warehouse": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `warehouse_id` STRING, `is_other` BOOLEAN, `pooled_count` BIGINT, `billed_hours` DECIMAL(38,0), `queries` DECIMAL(38,0), `usd_per_1000_queries` DOUBLE, `usd_list` DOUBLE, `share_of_total_pct` DOUBLE, `prev_usd_list` DOUBLE, `change_usd_list` DOUBLE, `change_pct` DOUBLE, `price_basis` STRING, `status` STRING, `not_assessed_reason` STRING",
    "cost_chargeback_by_cluster": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `workspace_name` STRING, `cluster_kind` STRING, `entity_id` STRING, `name` STRING, `owner` STRING, `is_other` BOOLEAN, `pooled_count` BIGINT, `est_current_usd_list` DOUBLE, `share_of_total_pct` DOUBLE, `est_previous_usd_list` DOUBLE, `est_change_usd_list` DOUBLE, `change_pct` DOUBLE, `price_basis` STRING, `status` STRING, `not_assessed_reason` STRING",
    "cost_daily_spikes": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `billing_origin_product` STRING, `usage_date` DATE, `est_day_usd_list` DOUBLE, `est_baseline_usd_list` DOUBLE, `baseline_days_seen` BIGINT, `spike_ratio_actual` DOUBLE, `top_skus` STRING, `price_basis` STRING, `status` STRING, `not_assessed_reason` STRING",
    "cost_by_hour_of_day": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `weekday` STRING, `weekday_num` BIGINT, `hour_of_day` BIGINT, `billing_origin_product` STRING, `usd_list` DOUBLE, `price_basis` STRING, `job_runs_active` BIGINT, `queries_started` BIGINT, `hour_usd_list` DOUBLE, `window_usd_list` DOUBLE, `share_of_window_spend` DOUBLE, `status` STRING",
    "cost_totals_by_sku_day": "snapshot_at TIMESTAMP, `window_days` INT, `usage_date` DATE, `cloud` STRING, `workspace_id` STRING, `sku_name` STRING, `billing_origin_product` STRING, `usage_type` STRING, `usage_unit` STRING, `is_serverless` BOOLEAN, `net_usage_quantity` DOUBLE, `original_usage_quantity` DOUBLE, `retraction_usage_quantity` DOUBLE, `restatement_usage_quantity` DOUBLE, `record_count` BIGINT",
    "cost_workspace_names": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `workspace_name` STRING, `status` STRING",
    "cost_by_billing_origin_product": "snapshot_at TIMESTAMP, `window_days` INT, `billing_origin_product` STRING, `usage_unit` STRING, `cloud` STRING, `workspace_id` STRING, `net_usage_quantity` DOUBLE, `record_count` BIGINT",
    "cost_sku_trend_12m": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `sku_name` STRING, `billing_origin_product` STRING, `product_label` STRING, `month_start` DATE, `is_partial_month` BOOLEAN, `net_list_cost_usd` DOUBLE, `price_basis` STRING, `usd_last_3m_avg` DOUBLE, `usd_prior_3m_avg` DOUBLE, `growth_3m_pct` DOUBLE, `usd_first_3m_avg` DOUBLE, `growth_12m_pct` DOUBLE, `share_of_total_last_3m` DOUBLE, `new_this_year` BOOLEAN, `status` STRING",
    "cost_chargeback_by_allocation_tag": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `usage_unit` STRING, `allocation_key` STRING, `allocation_value` STRING, `is_missing_allocation_key` BOOLEAN, `net_usage_quantity` DOUBLE, `net_list_cost_usd` DOUBLE, `share_of_unit_pct` DOUBLE, `price_basis` STRING, `status` STRING, `not_assessed_reason` STRING",
    "cost_chargeback_by_tag": "snapshot_at TIMESTAMP, `window_days` INT, `usage_date` DATE, `cloud` STRING, `workspace_id` STRING, `billing_origin_product` STRING, `tag_key` STRING, `tag_value` STRING, `net_usage_quantity` DOUBLE, `record_count` BIGINT",
    "cost_chargeback_by_identity": "snapshot_at TIMESTAMP, `window_days` INT, `usage_date` DATE, `cloud` STRING, `workspace_id` STRING, `billing_origin_product` STRING, `identity_type` STRING, `identity_run_as` STRING, `identity_owned_by` STRING, `identity_created_by` STRING, `net_usage_quantity` DOUBLE",
    "cost_chargeback_by_workspace": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `usd_list` DOUBLE, `share_of_total_pct` DOUBLE, `prev_usd_list` DOUBLE, `change_usd_list` DOUBLE, `change_pct` DOUBLE, `price_basis` STRING, `status` STRING, `not_assessed_reason` STRING",
    "cost_chargeback_by_service": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `billing_origin_product` STRING, `service_label` STRING, `usd_list` DOUBLE, `share_of_total_pct` DOUBLE, `prev_usd_list` DOUBLE, `change_usd_list` DOUBLE, `change_pct` DOUBLE, `price_basis` STRING, `status` STRING, `not_assessed_reason` STRING",
    "cost_chargeback_by_sku": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `sku_name` STRING, `sku_name_readable` STRING, `cloud` STRING, `usage_unit` STRING, `usd_list` DOUBLE, `dbus` DOUBLE, `share_of_total_pct` DOUBLE, `prev_usd_list` DOUBLE, `change_usd_list` DOUBLE, `change_pct` DOUBLE, `price_basis` STRING, `status` STRING, `not_assessed_reason` STRING",
    "cost_chargeback_by_job": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `job_id` STRING, `job_name` STRING, `run_as` STRING, `creator_user_name` STRING, `is_other` BOOLEAN, `pooled_count` BIGINT, `est_current_usd_list` DOUBLE, `share_of_total_pct` DOUBLE, `est_previous_usd_list` DOUBLE, `est_change_usd_list` DOUBLE, `change_pct` DOUBLE, `price_basis` STRING, `status` STRING, `not_assessed_reason` STRING",
    "cost_chargeback_identity_by_source": "snapshot_at TIMESTAMP, `window_days` INT, `identity_run_as` STRING, `identity_type` STRING, `source` STRING, `workspace_id` STRING, `warehouse_id` STRING, `is_other` BOOLEAN, `pooled_count` BIGINT, `usd_list` DOUBLE, `share_of_total_pct` DOUBLE, `prev_usd_list` DOUBLE, `change_usd_list` DOUBLE, `change_pct` DOUBLE, `price_basis` STRING, `status` STRING, `not_assessed_reason` STRING",
    "cost_chargeback_by_tag_value": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `tag_key` STRING, `tag_value` STRING, `is_other` BOOLEAN, `is_untagged` BOOLEAN, `pooled_count` BIGINT, `usd_list` DOUBLE, `share_of_total_pct` DOUBLE, `prev_usd_list` DOUBLE, `change_usd_list` DOUBLE, `change_pct` DOUBLE, `price_basis` STRING, `status` STRING, `not_assessed_reason` STRING",
    "cost_chargeback_reconcile": "snapshot_at TIMESTAMP, `window_days` INT, `check_name` STRING, `workspace_id` STRING, `coverage_type` STRING, `billing_total_usd_list` DOUBLE, `view_total_usd_list` DOUBLE, `gap_usd_list` DOUBLE, `gap_pct` DOUBLE, `coverage_pct` DOUBLE, `status` STRING",
    "cost_by_job": "snapshot_at TIMESTAMP, `window_days` INT, `usage_date` DATE, `cloud` STRING, `workspace_id` STRING, `workspace_name` STRING, `billing_origin_product` STRING, `job_id` STRING, `job_name` STRING, `is_serverless` BOOLEAN, `net_usage_quantity` DOUBLE, `usd_list` DOUBLE, `price_basis` STRING, `distinct_runs` BIGINT, `status` STRING, `job_day_dbus` DOUBLE",
    "cost_by_compute_resource": "snapshot_at TIMESTAMP, `window_days` INT, `usage_date` DATE, `cloud` STRING, `workspace_id` STRING, `workspace_name` STRING, `billing_origin_product` STRING, `cluster_id` STRING, `cluster_name` STRING, `warehouse_id` STRING, `warehouse_name` STRING, `instance_pool_id` STRING, `instance_pool_name` STRING, `is_serverless` BOOLEAN, `net_usage_quantity` DOUBLE, `usd_list` DOUBLE, `price_basis` STRING, `status` STRING",
    "cost_by_notebook": "snapshot_at TIMESTAMP, `window_days` INT, `usage_date` DATE, `cloud` STRING, `workspace_id` STRING, `billing_origin_product` STRING, `notebook_id` STRING, `is_serverless` BOOLEAN, `net_usage_quantity` DOUBLE, `status` STRING",
    "cost_default_storage_dsu": "snapshot_at TIMESTAMP, `window_days` INT, `usage_date` DATE, `cloud` STRING, `workspace_id` STRING, `sku_name` STRING, `usage_type` STRING, `usage_unit` STRING, `storage_api_type` STRING, `catalog_id` STRING, `net_usage_quantity` DOUBLE",
    "cost_genai_token_gpu": "snapshot_at TIMESTAMP, `window_days` INT, `usage_date` DATE, `cloud` STRING, `workspace_id` STRING, `sku_name` STRING, `billing_origin_product` STRING, `usage_type` STRING, `usage_unit` STRING, `serving_type` STRING, `endpoint_name` STRING, `endpoint_id` STRING, `net_usage_quantity` DOUBLE, `usd_list` DOUBLE, `price_basis` STRING",
    "cost_daily_by_resource": "snapshot_at TIMESTAMP, `window_days` INT, `usage_date` DATE, `resource_type` STRING, `resource_key` STRING, `workspace_id` STRING, `resource_id` STRING, `warehouse_id` STRING, `job_id` STRING, `pipeline_id` STRING, `cluster_id` STRING, `is_other` BOOLEAN, `pooled_count` BIGINT, `dbus` DOUBLE, `usd_list` DOUBLE, `unpriced_quantity` DOUBLE, `billed_hours` DECIMAL(38,0), `billed_runs` DECIMAL(38,0)",
    "perf_failure_causes_daily": "snapshot_at TIMESTAMP, `window_days` INT, `usage_date` DATE, `resource_type` STRING, `resource_key` STRING, `workspace_id` STRING, `resource_id` STRING, `cause` STRING, `failures` BIGINT",
    "cost_usage_policy_coverage": "snapshot_at TIMESTAMP, `window_days` INT, `usage_date` DATE, `cloud` STRING, `workspace_id` STRING, `billing_origin_product` STRING, `is_serverless` BOOLEAN, `policy_coverage` STRING, `tag_coverage` STRING, `net_usage_quantity` DOUBLE, `status` STRING",
    "cost_actual_vs_list_by_sku": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `cloud` STRING, `sku_name` STRING, `usage_unit` STRING, `billing_origin_product` STRING, `net_usage_quantity` DOUBLE, `net_list_cost` DOUBLE, `status` STRING, `not_assessed_reason` STRING",
    "cost_networking_egress": "snapshot_at TIMESTAMP, `window_days` INT, `usage_date` DATE, `cloud` STRING, `workspace_id` STRING, `sku_name` STRING, `usage_type` STRING, `usage_unit` STRING, `source_region` STRING, `destination_region` STRING, `networking_client` STRING, `recipient_id` STRING, `net_usage_quantity` DOUBLE, `usd_list` DOUBLE, `price_basis` STRING",
    "cost_cloud_infra": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `usage_date` DATE, `cloud` STRING, `currency_code` STRING, `net_list_cost` DOUBLE, `record_count` BIGINT, `price_basis` STRING",
    "cost_restatement_trust_metric": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `cloud` STRING, `net_usage_quantity` DOUBLE, `original_usage_quantity` DOUBLE, `retracted_abs_quantity` DOUBLE, `restatement_usage_quantity` DOUBLE, `max_ingestion_date` DATE",
    "cost_by_serving_endpoint": "snapshot_at TIMESTAMP, `window_days` INT, `usage_date` DATE, `cloud` STRING, `workspace_id` STRING, `billing_origin_product` STRING, `endpoint_id` STRING, `endpoint_name` STRING, `usage_type` STRING, `net_usage_quantity` DOUBLE",
    "cost_serving_mode_by_endpoint": "snapshot_at TIMESTAMP, `window_days` INT, `usage_date` DATE, `workspace_id` STRING, `endpoint_id` STRING, `endpoint_name` STRING, `cloud` STRING, `sku_name` STRING, `usage_type` STRING, `usage_unit` STRING, `serving_type` STRING, `is_launch_sku` BOOLEAN, `net_usage_quantity` DOUBLE, `net_list_cost` DOUBLE, `price_basis` STRING, `status` STRING, `endpoint_day_usd` DOUBLE",
    "cost_vector_search_spend": "snapshot_at TIMESTAMP, `window_days` INT, `usage_date` DATE, `cloud` STRING, `workspace_id` STRING, `sku_name` STRING, `usage_type` STRING, `usage_unit` STRING, `endpoint_name` STRING, `endpoint_id` STRING, `currency_code` STRING, `net_usage_quantity` DOUBLE, `net_list_cost` DOUBLE, `price_basis` STRING, `status` STRING, `endpoint_day_usd` DOUBLE",
    "genie_usage": "snapshot_at TIMESTAMP, `window_days` INT, `usage_date` DATE, `period` STRING, `workspace_id` STRING, `surface` STRING, `channel` STRING, `agent_id` STRING, `identity_type` STRING, `run_as` STRING, `is_free` BOOLEAN, `dbus` DOUBLE, `usd_list` DOUBLE, `unpriced_dbus` DOUBLE, `price_basis` STRING, `previous_period_covered` BOOLEAN",
    "compute_warehouse_idle_gaps": "snapshot_at TIMESTAMP, `window_days` INT, `warehouse_id` STRING, `event_count` BIGINT, `running_seconds` DOUBLE, `starting_seconds` DOUBLE, `stopped_seconds` DOUBLE, `max_running_gap_seconds` DOUBLE, `running_events` DECIMAL(38,0), `starting_events` DECIMAL(38,0), `stopped_events` DECIMAL(38,0), `net_dbus` DOUBLE, `est_usd_list` DOUBLE, `price_basis` STRING, `status` STRING",
    "compute_warehouse_autoscale_churn": "snapshot_at TIMESTAMP, `window_days` INT, `warehouse_id` STRING, `warehouse_name` STRING, `scaled_up_events` DECIMAL(38,0), `scaled_down_events` DECIMAL(38,0), `scaling_events` DECIMAL(38,0), `scaling_events_total` DECIMAL(38,0), `churn_events` DECIMAL(38,0), `start_events` DECIMAL(38,0), `stop_events` DECIMAL(38,0), `start_stop_cycles` DECIMAL(38,0), `total_events` BIGINT, `first_event_time` TIMESTAMP, `last_event_time` TIMESTAMP, `observed_hours` DOUBLE, `running_hours` DOUBLE, `churn_events_per_hour` DOUBLE, `start_stop_cycles_per_hour` DOUBLE, `max_cluster_count` INT, `avg_cluster_count` DOUBLE, `avg_cluster_count_time_weighted` DOUBLE, `net_dbus` DOUBLE, `est_usd_list` DOUBLE, `price_basis` STRING, `status` STRING",
    "compute_warehouse_autostop_churn": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `warehouse_id` STRING, `warehouse_name` STRING, `warehouse_kind` STRING, `auto_stop_minutes` INT, `days_observed` BIGINT, `days_flagged` DECIMAL(38,0), `cold_start_risk_days` DECIMAL(38,0), `total_autostop_count` DECIMAL(38,0), `total_autostop_idle_minutes` DOUBLE, `total_est_wasted_usd_list` DOUBLE, `worst_day` TIMESTAMP, `worst_day_autostop_count` BIGINT, `worst_day_est_wasted_usd_list` DOUBLE, `status` STRING",
    "compute_warehouse_late_autostop": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `warehouse_id` STRING, `warehouse_name` STRING, `warehouse_kind` STRING, `auto_stop_minutes` INT, `auto_stops` BIGINT, `typical_wait_minutes` DOUBLE, `late_stops` DECIMAL(38,0), `late_extra_minutes` DOUBLE, `longest_wait_minutes` DOUBLE, `last_activity_at` TIMESTAMP, `stop_at` TIMESTAMP, `late_reason` STRING, `status` STRING",
    "sql_warehouse_events_activity": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `warehouse_id` STRING, `warehouse_name` STRING, `event_type` STRING, `event_count` BIGINT, `last_event_time` TIMESTAMP, `first_event_time` TIMESTAMP, `max_cluster_count` INT, `avg_cluster_count` DOUBLE, `net_dbus` DOUBLE, `status` STRING",
    "sql_warehouse_config_current": "snapshot_at TIMESTAMP, `window_days` INT, `warehouse_id` STRING, `warehouse_name` STRING, `workspace_id` STRING, `account_id` STRING, `warehouse_type` STRING, `warehouse_channel` STRING, `warehouse_size` STRING, `min_clusters` INT, `max_clusters` INT, `auto_stop_minutes` INT, `tags` MAP<STRING, STRING>, `change_time` TIMESTAMP, `delete_time` TIMESTAMP",
    "node_timeline_utilization": "snapshot_at TIMESTAMP, `window_days` INT, `cluster_id` STRING, `node_type` STRING, `driver` BOOLEAN, `minute_rows` BIGINT, `first_minute` TIMESTAMP, `last_minute` TIMESTAMP, `avg_cpu_pct` DOUBLE, `peak_cpu_pct` DOUBLE, `avg_mem_pct` DOUBLE, `peak_mem_pct` DOUBLE, `avg_cpu_wait_pct` DOUBLE, `total_network_sent_bytes` DECIMAL(38,0), `total_network_received_bytes` DECIMAL(38,0), `status` STRING, `not_assessed_reason` STRING",
    "instance_events_idle_active": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `node_type` STRING, `availability_type` STRING, `state` STRING, `event_type` STRING, `event_count` BIGINT, `instance_count` BIGINT, `first_event_time` TIMESTAMP, `last_event_time` TIMESTAMP",
    "classic_clusters_config_current": "snapshot_at TIMESTAMP, `window_days` INT, `cluster_id` STRING, `cluster_name` STRING, `owned_by` STRING, `driver_node_type` STRING, `worker_node_type` STRING, `worker_count` BIGINT, `min_autoscale_workers` BIGINT, `max_autoscale_workers` BIGINT, `auto_termination_minutes` BIGINT, `enable_elastic_disk` BOOLEAN, `cluster_source` STRING, `dbr_version` STRING, `data_security_mode` STRING, `policy_id` STRING, `driver_instance_pool_id` STRING, `worker_instance_pool_id` STRING, `tags` MAP<STRING, STRING>, `init_scripts` ARRAY<STRING>, `aws_attributes` STRUCT<first_on_demand: BIGINT, availability: STRING, zone_id: STRING, instance_profile_arn: STRING, spot_bid_price_percent: BIGINT, ebs_volume_type: STRING, ebs_volume_count: BIGINT, ebs_volume_size: BIGINT, ebs_volume_iops: BIGINT, ebs_volume_throughput: BIGINT>, `azure_attributes` STRUCT<first_on_demand: BIGINT, availability: STRING, spot_bid_max_price: DOUBLE>, `gcp_attributes` STRUCT<use_preemptible_executors: BOOLEAN, google_service_account: STRING, boot_disk_size: BIGINT, availability: STRING, zone_id: STRING, local_ssd_count: BIGINT>, `create_time` TIMESTAMP, `delete_time` TIMESTAMP, `change_time` TIMESTAMP, `workspace_id` STRING, `account_id` STRING",
    "node_types_reference": "snapshot_at TIMESTAMP, `window_days` INT, `node_type` STRING, `core_count` DOUBLE, `memory_mb` BIGINT, `gpu_count` BIGINT, `account_id` STRING",
    "compute_cluster_config_posture": "snapshot_at TIMESTAMP, `window_days` INT, `cluster_id` STRING, `workspace_id` STRING, `cluster_name` STRING, `owned_by` STRING, `cluster_source` STRING, `is_all_purpose` BOOLEAN, `dbr_version` STRING, `data_security_mode` STRING, `policy_id` STRING, `worker_count` BIGINT, `min_autoscale_workers` BIGINT, `max_autoscale_workers` BIGINT, `auto_termination_minutes` BIGINT, `flag_eol_runtime` BOOLEAN, `flag_no_isolation` BOOLEAN, `flag_no_policy` BOOLEAN, `flag_auto_term_risk` BOOLEAN, `reasons` STRING, `status` STRING, `create_time` TIMESTAMP, `change_time` TIMESTAMP",
    "compute_warehouse_config_posture": "snapshot_at TIMESTAMP, `window_days` INT, `warehouse_id` STRING, `workspace_id` STRING, `warehouse_name` STRING, `created_by` STRING, `warehouse_type` STRING, `warehouse_channel` STRING, `warehouse_size` STRING, `min_clusters` INT, `max_clusters` INT, `auto_stop_minutes` INT, `flag_autostop_risk` BOOLEAN, `flag_classic_type` BOOLEAN, `flag_preview_channel` BOOLEAN, `reasons` STRING, `status` STRING, `change_time` TIMESTAMP",
    "lakeflow_failed_runs": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `job_id` STRING, `job_name` STRING, `workspace_name` STRING, `run_type` STRING, `trigger_type` STRING, `result_state` STRING, `termination_code` STRING, `run_rows` BIGINT, `distinct_runs` BIGINT, `failed_run_rows` DECIMAL(38,0), `status` STRING",
    "lakeflow_never_started_runs": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `job_id` STRING, `job_name` STRING, `workspace_name` STRING, `termination_code` STRING, `never_started_runs_zero_length` BIGINT, `never_started_runs_invalid_config_no_tasks` BIGINT, `never_started_runs` BIGINT, `status` STRING",
    "lakeflow_succeeded_with_failed_tasks": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `job_id` STRING, `succeeded_runs` BIGINT, `succeeded_runs_with_failed_task` BIGINT, `status` STRING",
    "lakeflow_termination_taxonomy": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `job_id` STRING, `termination_code` STRING, `run_rows` BIGINT, `distinct_runs` BIGINT",
    "lakeflow_daily_state": "snapshot_at TIMESTAMP, `window_days` INT, `usage_date` DATE, `resource_type` STRING, `resource_key` STRING, `workspace_id` STRING, `resource_id` STRING, `job_id` STRING, `pipeline_id` STRING, `runs` BIGINT, `failed` DECIMAL(38,0), `skipped` DECIMAL(38,0), `timed_runs` DECIMAL(38,0), `total_run_s` DECIMAL(38,0), `max_run_s` BIGINT, `avg_run_s` DOUBLE, `median_run_s` DOUBLE, `day_state` STRING, `failed_days` DECIMAL(38,0), `slow_days` DECIMAL(38,0), `trouble_rank` BIGINT",
    "lakeflow_job_queue_time": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `job_id` STRING, `distinct_runs` BIGINT, `queue_s_total` DECIMAL(38,0), `queue_s_p95` DOUBLE, `runs_queue_null` DECIMAL(38,0), `status` STRING",
    "lakeflow_phase_cold_start": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `job_id` STRING, `runs` BIGINT, `setup_s_total` DECIMAL(38,0), `queue_s_total` DECIMAL(38,0), `execution_s_total` DECIMAL(38,0), `cleanup_s_total` DECIMAL(38,0), `run_s_total` DECIMAL(38,0), `setup_s_p95` DOUBLE, `queue_s_p95` DOUBLE, `rows_setup_null` DECIMAL(38,0), `status` STRING",
    "lakeflow_failed_cluster_starts": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `job_id` STRING, `termination_code` STRING, `failed_starts` BIGINT, `setup_s_total` DECIMAL(38,0), `first_failed_at` TIMESTAMP, `last_failed_at` TIMESTAMP",
    "lakeflow_workload_mix_hours": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `job_id` STRING, `run_type` STRING, `trigger_type` STRING, `run_day` TIMESTAMP, `distinct_runs` BIGINT, `completed_run_rows` DECIMAL(38,0), `execution_s_total` DECIMAL(38,0)",
    "lakeflow_long_running_runs": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `workspace_name` STRING, `job_id` STRING, `job_name` STRING, `run_id` STRING, `run_start` TIMESTAMP, `run_end` TIMESTAMP, `in_flight` BOOLEAN, `run_hours` DOUBLE, `run_s_is_lower_bound` BOOLEAN, `setup_s` BIGINT, `queue_s` BIGINT, `execution_s` BIGINT, `cleanup_s` BIGINT, `wait_share_pct` DOUBLE, `tasks_seen` BIGINT, `tasks_in_flight` DECIMAL(38,0), `top_task_key` STRING, `top_task_state` STRING, `top_3_tasks` STRING, `top_task_hours` DOUBLE, `top_task_share_pct` DOUBLE, `job_p50_hours` DOUBLE, `x_vs_job_p50` DOUBLE, `finished_runs_in_window` BIGINT, `result_state` STRING, `termination_code` STRING, `job_url` STRING, `run_url` STRING, `top_task_cluster_id` STRING, `top_task_cluster_url` STRING, `status` STRING",
    "lakeflow_retries_repairs": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `job_id` STRING, `distinct_runs` BIGINT, `total_attempt_rows` DECIMAL(38,0), `repairs` DECIMAL(38,0), `task_retries` DECIMAL(38,0), `retry_runs` DECIMAL(38,0), `total_retries` DECIMAL(38,0), `runs_with_retry` DECIMAL(38,0), `net_dbus` DOUBLE, `est_usd_list` DOUBLE, `price_basis` STRING, `status` STRING",
    "lakeflow_jobs_no_timeout": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `workspace_name` STRING, `job_id` STRING, `job_name` STRING, `timeout_seconds` BIGINT, `no_timeout` BOOLEAN, `has_duration_warning` BOOLEAN, `timeout_null_reason` STRING, `net_dbus` DOUBLE, `est_usd_list` DOUBLE, `price_basis` STRING, `status` STRING, `not_assessed_reason` STRING",
    "lakeflow_job_tasks_no_timeout": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `workspace_name` STRING, `job_id` STRING, `job_name` STRING, `task_key` STRING, `timeout_seconds` BIGINT, `job_timeout_seconds` BIGINT, `no_timeout` BOOLEAN, `has_duration_warning` BOOLEAN, `timeout_null_reason` STRING, `parent_gone` BOOLEAN, `net_dbus` DOUBLE, `est_usd_list` DOUBLE, `price_basis` STRING, `status` STRING, `not_assessed_reason` STRING",
    "lakeflow_health_rule_coverage": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `job_id` STRING, `job_name` STRING, `health_rule_count` BIGINT, `status` STRING, `not_assessed_reason` STRING",
    "lakeflow_job_ownership_orphans": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `workspace_name` STRING, `job_id` STRING, `job_name` STRING, `creator_user_name` STRING, `run_as_user_name` STRING, `run_as_kind` STRING, `is_scheduled` BOOLEAN, `identity_redacted` BOOLEAN, `principal_missing` BOOLEAN, `identity_not_recorded` BOOLEAN, `status` STRING, `not_assessed_reason` STRING",
    "lakeflow_tasks_near_timeout": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `job_id` STRING, `job_name` STRING, `workspace_name` STRING, `task_key` STRING, `timeout_seconds` BIGINT, `task_runs` BIGINT, `max_exec_s` BIGINT, `exec_s_p95` DOUBLE, `runs_near_timeout` DECIMAL(38,0), `runs_over_timeout` DECIMAL(38,0), `runs_no_timeout_bound` DECIMAL(38,0), `runs_no_task_timeout` DECIMAL(38,0), `runs_exec_null` DECIMAL(38,0), `status` STRING, `not_assessed_reason` STRING",
    "lakeflow_job_cost_summary": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `job_id` STRING, `job_name` STRING, `runs` BIGINT, `net_job_dbus` DOUBLE, `currency_code` STRING, `net_list_cost` DOUBLE, `price_basis` STRING, `median_run_dbus` DOUBLE, `max_run_dbus` DOUBLE, `est_median_usd_list` DOUBLE, `est_max_usd_list` DOUBLE",
    "lakeflow_job_run_cost": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `job_id` STRING, `job_name` STRING, `job_run_id` STRING, `run_start` TIMESTAMP, `in_flight` BOOLEAN, `result_state` STRING, `termination_code` STRING, `net_run_dbus` DOUBLE, `currency_code` STRING, `net_list_cost` DOUBLE, `price_basis` STRING, `compute_type` STRING",
    "lakeflow_job_recent_runs": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `workspace_name` STRING, `job_id` STRING, `job_name` STRING, `run_id` STRING, `run_start` TIMESTAMP, `run_end` TIMESTAMP, `in_flight` BOOLEAN, `run_minutes` DOUBLE, `run_duration_is_lower_bound` BOOLEAN, `result_state` STRING, `termination_code` STRING, `attempts` BIGINT, `failing_task_keys` STRING, `failing_task_termination_code` STRING, `net_dbus` DOUBLE, `net_list_cost` DOUBLE, `sql_error_sample` STRING",
    "lakeflow_job_compute_pressure": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `job_id` STRING, `job_name` STRING, `runs_seen` BIGINT, `runs_with_telemetry` BIGINT, `task_keys_seen` BIGINT, `task_hours_total` DOUBLE, `last_run_start` TIMESTAMP, `clusters_seen` BIGINT, `max_clusters_per_run` BIGINT, `on_all_purpose` BOOLEAN, `latest_cluster_id` STRING, `cluster_source` STRING, `worker_node_type` STRING, `driver_node_type` STRING, `worker_memory_gb` DOUBLE, `worker_cores` DOUBLE, `workers_configured_max` BIGINT, `worker_nodes_seen_max` BIGINT, `at_ceiling` BOOLEAN, `single_node` BOOLEAN, `node_minutes` BIGINT, `worker_minutes` DECIMAL(38,0), `worker_cpu_avg_pct` DOUBLE, `worker_cpu_p50_pct` DOUBLE, `worker_cpu_p90_pct` DOUBLE, `worker_cpu_spread_pct` DOUBLE, `worker_cpu_wait_avg_pct` DOUBLE, `worker_mem_avg_pct` DOUBLE, `worker_mem_p90_pct` DOUBLE, `worker_mem_peak_pct` DOUBLE, `worker_swap_p90_pct` DOUBLE, `worker_swap_peak_pct` DOUBLE, `driver_cpu_avg_pct` DOUBLE, `driver_mem_p90_pct` DOUBLE, `network_received_gb` DOUBLE, `hottest_task_key` STRING, `hottest_task_mem_p90_pct` DOUBLE, `pressure` STRING, `scaling_hint` STRING, `pressure_reason` STRING, `not_assessed_reason` STRING, `status` STRING",
    "task_cluster_utilization": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `workspace_name` STRING, `job_id` STRING, `job_name` STRING, `job_run_id` STRING, `run_hours` DOUBLE, `in_flight` BOOLEAN, `task_key` STRING, `task_run_id` STRING, `task_state` STRING, `task_start` TIMESTAMP, `task_hours` DOUBLE, `cluster_id` STRING, `compute_ids_count` BIGINT, `cluster_name` STRING, `cluster_source` STRING, `worker_node_type` STRING, `driver_node_type` STRING, `worker_count_configured` BIGINT, `min_autoscale_workers` BIGINT, `max_autoscale_workers` BIGINT, `dbr_version` STRING, `nodes_seen` BIGINT, `worker_nodes_seen` DECIMAL(38,0), `node_minutes` BIGINT, `worker_minutes` DECIMAL(38,0), `single_node` BOOLEAN, `worker_cpu_avg_pct` DOUBLE, `worker_cpu_p50_pct` DOUBLE, `worker_cpu_p90_pct` DOUBLE, `worker_cpu_peak_pct` DOUBLE, `worker_cpu_wait_avg_pct` DOUBLE, `worker_cpu_spread_pct` DOUBLE, `worker_mem_avg_pct` DOUBLE, `worker_mem_p90_pct` DOUBLE, `worker_mem_peak_pct` DOUBLE, `worker_swap_p90_pct` DOUBLE, `worker_swap_peak_pct` DOUBLE, `driver_cpu_avg_pct` DOUBLE, `driver_cpu_peak_pct` DOUBLE, `driver_mem_peak_pct` DOUBLE, `network_sent_gb` DOUBLE, `network_received_gb` DOUBLE, `overlapping_task_runs` BIGINT, `bottleneck_hint` STRING, `not_assessed_reason` STRING, `run_url` STRING, `cluster_url` STRING, `spark_ui_url` STRING, `status` STRING",
    "lakeflow_job_oversized": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `job_id` STRING, `job_name` STRING, `runs_with_telemetry` BIGINT, `single_node` BOOLEAN, `latest_cluster_id` STRING, `cluster_source` STRING, `worker_node_type` STRING, `worker_memory_gb` DOUBLE, `worker_cores` DOUBLE, `workers_configured_max` BIGINT, `worker_nodes_seen_max` BIGINT, `worker_cpu_p90_pct` DOUBLE, `worker_mem_p90_pct` DOUBLE, `worker_cpu_peak_pct` DOUBLE, `worker_mem_peak_pct` DOUBLE, `worker_swap_peak_pct` DOUBLE, `oversized` BOOLEAN, `job_list_cost_usd` DOUBLE, `price_basis` STRING, `est_saving_usd_list` DOUBLE, `suggested_action` STRING, `not_assessed_reason` STRING, `status` STRING",
    "lakeflow_pipeline_cost": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `pipeline_id` STRING, `pipeline_name` STRING, `pipeline_type` STRING, `is_continuous` BOOLEAN, `net_pipeline_dbus` DOUBLE, `net_maintenance_dbus` DOUBLE, `currency_code` STRING, `net_list_cost` DOUBLE, `net_maintenance_list_cost` DOUBLE, `price_basis` STRING, `updates` BIGINT, `update_end_rows` BIGINT, `update_rows_any` BIGINT, `updates_completed` DECIMAL(38,0), `updates_failed_or_canceled` DECIMAL(38,0), `active_seconds_total` DOUBLE, `in_flight_only` BOOLEAN, `nothing_delivered` BOOLEAN, `is_high_spend` BOOLEAN, `est_wasted_usd_list` DOUBLE, `status` STRING, `not_assessed_reason` STRING",
    "lakeflow_pipeline_update_failures_retries": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `pipeline_id` STRING, `update_type` STRING, `trigger_type` STRING, `result_state` STRING, `updates` BIGINT, `failed_update_rows` DECIMAL(38,0), `retry_triggered_rows` DECIMAL(38,0), `status` STRING",
    "lakeflow_pipelines_inventory_tier": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `workspace_name` STRING, `pipeline_id` STRING, `pipeline_name` STRING, `pipeline_type` STRING, `setting_serverless` BOOLEAN, `setting_development` BOOLEAN, `setting_continuous` BOOLEAN, `setting_photon` BOOLEAN, `setting_edition` STRING, `setting_channel` STRING",
    "query_provenance_by_source": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `compute_type` STRING, `warehouse_id` STRING, `identity_type` STRING, `executed_by` STRING, `source_kind` STRING, `job_id` STRING, `dashboard_id` STRING, `notebook_id` STRING, `query_count` BIGINT, `execution_duration_ms_sum` DECIMAL(38,0), `total_duration_ms_sum` DECIMAL(38,0), `read_bytes_sum` DECIMAL(38,0)",
    "audit_self_cost": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `statement_type` STRING, `query_count` BIGINT, `total_duration_secs` DOUBLE, `total_task_secs` DOUBLE, `distinct_principals` BIGINT, `first_query_time` TIMESTAMP, `last_query_time` TIMESTAMP",
    "query_costly_statements_grouped": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `statement_fingerprint` STRING, `is_redacted` BOOLEAN, `statement_type` STRING, `sample_statement_text` STRING, `runs` BIGINT, `total_exec_ms` DECIMAL(38,0), `avg_exec_ms` BIGINT, `max_exec_ms` BIGINT, `distinct_warehouses` BIGINT, `first_seen` TIMESTAMP, `last_seen` TIMESTAMP, `status` STRING",
    "query_costly_statements": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `statement_id` STRING, `statement_type` STRING, `executed_by` STRING, `warehouse_id` STRING, `compute_type` STRING, `start_time` TIMESTAMP, `execution_duration_ms` BIGINT, `waiting_for_compute_duration_ms` BIGINT, `total_task_duration_ms` BIGINT, `read_bytes` BIGINT, `read_files` BIGINT, `pruned_files` BIGINT, `read_partitions` BIGINT, `read_rows` BIGINT, `produced_rows` BIGINT, `spilled_local_bytes` BIGINT, `shuffle_read_bytes` BIGINT, `from_result_cache` BOOLEAN, `statement_text` STRING, `statement_fingerprint` STRING, `pct_of_warehouse_exec_ms` DOUBLE, `exec_rank_in_warehouse` BIGINT, `status` STRING",
    "query_per_query_estimate_lane": "snapshot_at TIMESTAMP, `window_days` INT, `usage_hour` TIMESTAMP, `workspace_id` STRING, `warehouse_id` STRING, `compute_type` STRING, `executed_by` STRING, `statement_id` STRING, `statement_type` STRING, `execution_duration_ms` BIGINT, `waiting_for_compute_duration_ms` BIGINT, `total_task_duration_ms` BIGINT, `read_bytes` BIGINT, `total_duration_ms` BIGINT",
    "query_local_spillage": "snapshot_at TIMESTAMP, `window_days` INT, `day` DATE, `workspace_id` STRING, `compute_type` STRING, `warehouse_id` STRING, `executed_by` STRING, `spilling_query_count` BIGINT, `spilled_local_bytes_sum` DECIMAL(38,0), `spilled_local_bytes_max` BIGINT, `total_duration_ms_sum` DECIMAL(38,0), `execution_duration_ms_sum` DECIMAL(38,0), `shuffle_read_bytes_sum` DECIMAL(38,0), `warehouse_day_spilled_bytes` DECIMAL(38,0), `status` STRING",
    "query_shuffle_write_amplification": "snapshot_at TIMESTAMP, `window_days` INT, `day` DATE, `workspace_id` STRING, `compute_type` STRING, `warehouse_id` STRING, `executed_by` STRING, `statement_type` STRING, `query_count` BIGINT, `shuffle_read_bytes_sum` DECIMAL(38,0), `written_bytes_sum` DECIMAL(38,0), `written_rows_sum` DECIMAL(38,0), `written_files_sum` DECIMAL(38,0), `read_bytes_sum` DECIMAL(38,0), `status` STRING",
    "query_pruning_effectiveness": "snapshot_at TIMESTAMP, `window_days` INT, `day` DATE, `workspace_id` STRING, `compute_type` STRING, `warehouse_id` STRING, `warehouse_name` STRING, `executed_by` STRING, `statement_type` STRING, `query_count` BIGINT, `pruned_files_sum` DECIMAL(38,0), `read_files_sum` DECIMAL(38,0), `read_partitions_sum` DECIMAL(38,0), `read_bytes_sum` DECIMAL(38,0), `read_rows_sum` DECIMAL(38,0), `pruning_ratio` DOUBLE, `days_seen` BIGINT, `days_flagged` DECIMAL(38,0), `status` STRING",
    "query_cache_coldstart": "snapshot_at TIMESTAMP, `window_days` INT, `day` DATE, `workspace_id` STRING, `compute_type` STRING, `warehouse_id` STRING, `warehouse_name` STRING, `query_count` BIGINT, `result_cache_hit_count` DECIMAL(38,0), `scanned_query_count` DECIMAL(38,0), `read_io_cache_percent_avg` DOUBLE, `read_io_cache_percent_weighted_avg` DOUBLE, `low_io_cache_count` DECIMAL(38,0), `waiting_for_compute_ms_sum` DECIMAL(38,0), `compilation_duration_ms_sum` DECIMAL(38,0), `status` STRING",
    "query_failed_statements_grouped": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `warehouse_id` STRING, `execution_status` STRING, `error_class` STRING, `source_kind` STRING, `source_id` STRING, `statements` BIGINT, `first_seen` TIMESTAMP, `last_seen` TIMESTAMP, `sample_statement_id` STRING, `error_message_sample` STRING, `status` STRING",
    "query_warehouse_pressure": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `warehouse_id` STRING, `compute_type` STRING, `warehouse_type` STRING, `warehouse_size` STRING, `min_clusters` INT, `max_clusters` INT, `auto_stop_minutes` INT, `warehouse_deleted` BOOLEAN, `query_count` BIGINT, `finished_count` DECIMAL(38,0), `spilling_query_count` DECIMAL(38,0), `spill_query_pct` DOUBLE, `spill_time_pct` DOUBLE, `spilled_local_gb_sum` DOUBLE, `spilled_local_gb_max_day` DOUBLE, `read_gb_sum` DOUBLE, `shuffle_read_gb_sum` DOUBLE, `queued_at_capacity_count` DECIMAL(38,0), `capacity_wait_pct` DOUBLE, `waiting_at_capacity_s_sum` DOUBLE, `waited_for_compute_count` DECIMAL(38,0), `cold_start_wait_pct` DOUBLE, `waiting_for_compute_s_sum` DOUBLE, `execution_s_sum` DOUBLE, `total_duration_s_sum` DOUBLE, `max_cluster_count_seen` INT, `scaling_events` DECIMAL(38,0), `at_ceiling` BOOLEAN, `pressure` STRING, `scaling_hint` STRING, `pressure_reason` STRING, `not_assessed_reason` STRING, `status` STRING",
    "query_workload_mix_hours": "snapshot_at TIMESTAMP, `window_days` INT, `day` DATE, `workspace_id` STRING, `hour_of_day` BIGINT, `compute_type` STRING, `warehouse_id` STRING, `statement_type` STRING, `executed_by` STRING, `query_count` BIGINT, `finished_count` DECIMAL(38,0), `total_duration_ms_sum` DECIMAL(38,0), `execution_duration_ms_sum` DECIMAL(38,0), `read_bytes_sum` DECIMAL(38,0), `produced_rows_sum` DECIMAL(38,0)",
    "query_task_statement_breakdown": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `job_id` STRING, `job_name` STRING, `job_run_id` STRING, `run_hours` DOUBLE, `in_flight` BOOLEAN, `run_tasks_seen` BIGINT, `run_tasks_not_assessed` BIGINT, `run_unattributed_statements` DECIMAL(38,0), `task_key` STRING, `task_run_id` STRING, `stmt_task_run_id` STRING, `task_start` TIMESTAMP, `task_state` STRING, `task_hours` DOUBLE, `task_compute_kind` STRING, `task_compute_type_raw` STRING, `task_cluster_id` STRING, `task_statement_count` DECIMAL(38,0), `task_sql_share_pct` DOUBLE, `sql_may_be_incomplete` BOOLEAN, `stmt_rank_in_task` BIGINT, `shape_fingerprint` STRING, `statement_fingerprint` STRING, `literal_variants_in_shape` BIGINT, `text_redacted` BOOLEAN, `statement_count` BIGINT, `statements_not_finished` DECIMAL(38,0), `statement_type` STRING, `sample_statement_id` STRING, `stmt_compute_type` STRING, `first_start_time` TIMESTAMP, `last_end_time` TIMESTAMP, `total_ms` DECIMAL(38,0), `execution_ms` DECIMAL(38,0), `compilation_ms` DECIMAL(38,0), `waiting_for_compute_ms` DECIMAL(38,0), `waiting_at_capacity_ms` DECIMAL(38,0), `total_task_ms` DECIMAL(38,0), `read_bytes` DECIMAL(38,0), `spilled_local_bytes` DECIMAL(38,0), `shuffle_read_bytes` DECIMAL(38,0), `share_of_task_pct` DOUBLE, `statement_text_devalued` STRING, `not_assessed_reason` STRING, `status` STRING",
    "perf_queue_by_hour": "snapshot_at TIMESTAMP, `window_days` INT, `usage_date` DATE, `usage_hour` BIGINT, `warehouse_key` STRING, `workspace_id` STRING, `warehouse_id` STRING, `runs` BIGINT, `queued_runs` DECIMAL(38,0), `slot_wait_s` DOUBLE, `provision_runs` DECIMAL(38,0), `provision_s` DOUBLE",
    "cost_account_prices_raw": "snapshot_at TIMESTAMP, `window_days` INT, `price_start_time` TIMESTAMP, `price_end_time` TIMESTAMP, `account_id` STRING, `sku_name` STRING, `cloud` STRING, `currency_code` STRING, `usage_unit` STRING, `pricing_default` STRING",
    "pricing_list_prices_raw": "snapshot_at TIMESTAMP, `window_days` INT, `price_start_time` TIMESTAMP, `price_end_time` TIMESTAMP, `account_id` STRING, `sku_name` STRING, `cloud` STRING, `currency_code` STRING, `usage_unit` STRING, `pricing_default` STRING, `pricing_effective_list_json` STRING, `pricing_promotional_json` STRING",
    "cost_audit_self_usage": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `warehouse_id` STRING, `warehouse_name` STRING, `matched_client_application` STRING, `matched_statement_count` BIGINT, `matched_duration_secs` DOUBLE, `warehouse_all_duration_secs` DOUBLE, `matched_share_pct` DOUBLE, `est_usd_list` DOUBLE, `est_audit_usd_list` DOUBLE, `price_basis` STRING, `first_query_time` TIMESTAMP, `last_query_time` TIMESTAMP",
    "overview_daily_dbu_trend": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `usage_date` DATE, `net_dbus` DOUBLE",
    "overview_dbu_by_sku": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `sku_name` STRING, `billing_origin_product` STRING, `net_dbus` DOUBLE",
    "overview_list_prices_raw": "snapshot_at TIMESTAMP, `window_days` INT, `price_start_time` TIMESTAMP, `price_end_time` TIMESTAMP, `account_id` STRING, `sku_name` STRING, `cloud` STRING, `currency_code` STRING, `usage_unit` STRING, `pricing_default` STRING, `pricing_effective_list_json` STRING, `pricing_promotional_json` STRING",
    "overview_serverless_classic_split": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `serverless_net_dbus` DOUBLE, `classic_net_dbus` DOUBLE, `serverless_pct` DOUBLE, `photon_net_dbus` DOUBLE, `photon_pct` DOUBLE, `perf_optimized_net_dbus` DOUBLE, `untagged_net_dbus` DOUBLE, `total_net_dbus` DOUBLE",
    "overview_spend_estimate": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `total_net_dbus` DOUBLE, `currency_code` STRING, `net_list_cost_usd` DOUBLE, `dbu_price_source` STRING, `price_basis` STRING",
    "compute_serving_endpoint_cost_status": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `workspace_name` STRING, `endpoint_id` STRING, `endpoint_name` STRING, `products` STRING, `served_entity_id` STRING, `served_entity_name` STRING, `entity_type` STRING, `entity_name` STRING, `entity_version` STRING, `latest_change_time` TIMESTAMP, `net_dbus` DOUBLE, `est_usd_list` DOUBLE, `price_basis` STRING, `est_wasted_usd_list` DOUBLE, `entity_requests_window` BIGINT, `entity_last_request_date` DATE, `endpoint_requests_window` DECIMAL(38,0), `endpoint_last_request_date` DATE, `ep_last_request_date` DATE, `tracking_status` STRING, `not_assessed_reason` STRING, `earliest_request_time` TIMESTAMP, `retention_days_available` BIGINT, `retention_days_required` INT, `served_entity_count` BIGINT, `entity_types` STRING, `entity_names` STRING, `primary_served_entity_id` STRING, `primary_served_entity_name` STRING, `primary_entity_type` STRING, `primary_entity_name` STRING, `primary_entity_version` STRING, `status` STRING",
    "compute_serving_dormant_endpoints": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `endpoint_id` STRING, `endpoint_name` STRING, `served_entity_count` BIGINT, `latest_change_time` TIMESTAMP, `latest_change_date` DATE, `total_requests` DECIMAL(38,0), `last_request_date` DATE, `usage_tracked` BOOLEAN, `billed_dbus` DOUBLE, `status` STRING, `not_assessed_reason` STRING",
    "cost_failed_statement_waste": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `warehouse_id` STRING, `statements` BIGINT, `failed_statements` DECIMAL(38,0), `canceled_statements` DECIMAL(38,0), `failed_with_cost` DECIMAL(38,0), `failed_dbus` DOUBLE, `est_wasted_usd_list` DOUBLE, `est_canceled_usd_list` DOUBLE, `est_attributed_usd_list` DOUBLE, `failed_cost_share_pct` DOUBLE, `price_basis` STRING, `cost_basis` STRING, `waste_reason` STRING, `not_assessed_reason` STRING, `status` STRING",
    "cost_dbsql_allocation_gap": "snapshot_at TIMESTAMP, `window_days` INT, `source_kind` STRING, `workspace_id` STRING, `warehouse_id` STRING, `usage_date` DATE, `cloud` STRING, `billing_origin_product` STRING, `usage_unit` STRING, `net_usage_quantity` DOUBLE, `coverage_status` STRING",
    "compute_serving_endpoint_usage": "snapshot_at TIMESTAMP, `window_days` INT, `usage_date` DATE, `workspace_id` STRING, `endpoint_id` STRING, `endpoint_name` STRING, `served_entity_id` STRING, `served_entity_name` STRING, `entity_type` STRING, `entity_name` STRING, `success_requests` DECIMAL(38,0), `error_requests` DECIMAL(38,0), `total_requests` BIGINT, `input_tokens` DECIMAL(38,0), `output_tokens` DECIMAL(38,0), `net_dbus` DOUBLE, `est_usd_list` DOUBLE, `price_basis` STRING",
    "access_vector_search_traffic": "snapshot_at TIMESTAMP, `window_days` INT, `event_date` DATE, `workspace_id` STRING, `action_name` STRING, `vector_search_key` STRING, `endpoint_name` STRING, `event_count` BIGINT, `distinct_index_names` BIGINT",
    "serving_endpoint_traffic_by_endpoint": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `endpoint_id` STRING, `endpoint_name` STRING, `request_count` BIGINT, `success_requests` DECIMAL(38,0), `error_requests` DECIMAL(38,0), `input_tokens` DECIMAL(38,0), `output_tokens` DECIMAL(38,0), `last_request_date` DATE, `net_dbus` DOUBLE, `est_usd_list` DOUBLE, `price_basis` STRING",
    "compute_ai_gateway_usage": "snapshot_at TIMESTAMP, `window_days` INT, `usage_date` DATE, `workspace_id` STRING, `endpoint_name` STRING, `requester` STRING, `total_requests` BIGINT, `success_requests` DECIMAL(38,0), `rate_limited_requests` DECIMAL(38,0), `error_requests` DECIMAL(38,0), `input_tokens` DECIMAL(38,0), `output_tokens` DECIMAL(38,0), `p50_latency_ms` DOUBLE, `p95_latency_ms` DOUBLE, `max_latency_ms` BIGINT",
    "compute_last_activity": "snapshot_at TIMESTAMP, `window_days` INT, `object_type` STRING, `workspace_id` STRING, `warehouse_id` STRING, `cluster_id` STRING, `name` STRING, `created_date` DATE, `last_query_date` DATE, `last_start_date` DATE, `last_job_run_date` DATE, `last_user_event_date` DATE, `last_billed_date` DATE, `audit_seen` BOOLEAN, `last_activity_date` DATE, `days_since_activity` BIGINT, `status` STRING, `not_assessed_reason` STRING",
    "lakeflow_job_run_changes": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `job_id` STRING, `job_name` STRING, `attribute` STRING, `baseline_run_id` STRING, `latest_run_id` STRING, `baseline_kind` STRING, `baseline_value` STRING, `latest_value` STRING, `changed` BOOLEAN",
    "query_top_by_cost": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `warehouse_id` STRING, `group_kind` STRING, `group_id` STRING, `source_kind` STRING, `source_id` STRING, `statement_type` STRING, `runs` BIGINT, `distinct_users` BIGINT, `top_user` STRING, `est_cost_usd_list` DOUBLE, `cost_basis` STRING, `share_of_warehouse_cost` DOUBLE, `total_exec_ms` DECIMAL(38,0), `avg_exec_ms` BIGINT, `max_exec_ms` BIGINT, `p50_duration_ms` BIGINT, `p95_duration_ms` BIGINT, `read_bytes` DECIMAL(38,0), `spilled_local_bytes` DECIMAL(38,0), `read_files` DECIMAL(38,0), `pruned_files` DECIMAL(38,0), `from_result_cache_share` DOUBLE, `sample_statement_id` STRING, `first_seen` TIMESTAMP, `last_seen` TIMESTAMP, `status` STRING",
    "query_slices": "snapshot_at TIMESTAMP, `window_days` INT, `usage_date` DATE, `resource_type` STRING, `resource_key` STRING, `workspace_id` STRING, `warehouse_id` STRING, `origin` STRING, `job_id` STRING, `pipeline_id` STRING, `notebook_id` STRING, `query_tags` STRING, `runs` BIGINT, `failed` DECIMAL(38,0), `total_s` DOUBLE, `queued_s` DOUBLE, `exec_s` DOUBLE, `slot_wait_s` DOUBLE, `provision_s` DOUBLE, `spilled_gb` DOUBLE, `shuffle_gb` DOUBLE, `read_gb` DOUBLE, `cache_read_gb` DOUBLE, `result_cache_runs` DECIMAL(38,0), `fetch_s` DOUBLE, `attributed_dbus` DOUBLE, `attributed_usd_list` DOUBLE",
    "access_broad_grants": "snapshot_at TIMESTAMP, `window_days` INT, `securable_type` STRING, `securable` STRING, `grantee_type` STRING, `grantee` STRING, `privilege_type` STRING, `is_inherited` BOOLEAN, `inherited_from` STRING, `reason` STRING, `status` STRING",
    "access_grants_inventory": "snapshot_at TIMESTAMP, `window_days` INT, `object_scope` STRING, `privilege_type` STRING, `GRANTEE` STRING, `grant_count` BIGINT, `distinct_objects` BIGINT",
    "access_grants_inventory_extended": "snapshot_at TIMESTAMP, `window_days` INT, `object_scope` STRING, `privilege_type` STRING, `GRANTEE` STRING, `grant_count` BIGINT",
    "access_runas_escalation": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `service_name` STRING, `action_name` STRING, `run_by` STRING, `run_as` STRING, `delegation_kind` STRING, `newly_seen_pair` BOOLEAN, `event_count` BIGINT, `first_event_time` TIMESTAMP, `last_event_time` TIMESTAMP, `status` STRING",
    "access_login_concentration": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `principal` STRING, `source_ip_address` STRING, `service_name` STRING, `action_name` STRING, `event_count` BIGINT, `success_count` DECIMAL(38,0), `non_success_count` DECIMAL(38,0), `unknown_status_count` DECIMAL(38,0), `distinct_source_ips` BIGINT, `first_event_time` TIMESTAMP, `last_event_time` TIMESTAMP, `status` STRING",
    "access_network_inbound_denials": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `policy_outcome` STRING, `rule_label` STRING, `request_path` STRING, `authenticated_as` STRING, `source_ip` STRING, `denial_count` BIGINT, `first_event_time` TIMESTAMP, `last_event_time` TIMESTAMP, `status` STRING",
    "access_network_outbound_denials": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `network_source_type` STRING, `destination_type` STRING, `access_type` STRING, `destination` STRING, `dns_rcode` INT, `storage_rejection_reason` STRING, `denial_count` BIGINT, `first_event_time` TIMESTAMP, `last_event_time` TIMESTAMP, `status` STRING",
    "access_admin_role_change_events": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `service_name` STRING, `action_name` STRING, `actor` STRING, `granted_to` STRING, `permission` STRING, `workspace_name` STRING, `event_count` BIGINT, `failed_event_count` DECIMAL(38,0), `distinct_source_ips` BIGINT, `workspace_token_iplist_actions` STRING, `first_event_time` TIMESTAMP, `last_event_time` TIMESTAMP, `status` STRING",
    "access_classification_coverage": "snapshot_at TIMESTAMP, `window_days` INT, `catalog_name` STRING, `tables_total` BIGINT, `tables_classified` BIGINT, `covered_pct` DOUBLE, `status` STRING, `not_assessed_reason` STRING",
    "access_classified_unmasked": "snapshot_at TIMESTAMP, `window_days` INT, `catalog_name` STRING, `schema_name` STRING, `table_name` STRING, `column_name` STRING, `class_tag` STRING, `confidence` STRING, `mask_name` STRING, `abac_mask_policies` BIGINT, `is_unmasked` BOOLEAN, `not_assessed_reason` STRING, `status` STRING",
    "access_sensitive_table_reads": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `table_catalog` STRING, `table_schema` STRING, `table_name` STRING, `sensitivity_basis` STRING, `reader` STRING, `table_owner` STRING, `read_count` BIGINT, `first_read_time` TIMESTAMP, `last_read_time` TIMESTAMP, `distinct_read_days` BIGINT, `status` STRING, `not_assessed_reason` STRING",
    "access_pii_outside_tables": "snapshot_at TIMESTAMP, `window_days` INT, `object_type` STRING, `catalog_name` STRING, `schema_name` STRING, `object_name` STRING, `object_detail` STRING, `finding` STRING, `tags` STRING, `status` STRING",
    "access_data_classification_inventory": "snapshot_at TIMESTAMP, `window_days` INT, `catalog_name` STRING, `schema_name` STRING, `table_name` STRING, `column_name` STRING, `class_tag` STRING, `confidence` STRING, `data_type` STRING, `max_frequency` STRING, `latest_detected_time` STRING, `first_detected_time` STRING",
    "access_column_masks_inventory": "snapshot_at TIMESTAMP, `window_days` INT, `table_catalog` STRING, `table_schema` STRING, `table_name` STRING, `column_name` STRING, `mask_name` STRING, `using_columns` STRING",
    "access_row_filters_inventory": "snapshot_at TIMESTAMP, `window_days` INT, `table_catalog` STRING, `table_schema` STRING, `table_name` STRING, `filter_name` STRING, `target_columns` STRING",
    "access_tags_inventory": "snapshot_at TIMESTAMP, `window_days` INT, `object_scope` STRING, `tag_name` STRING, `tag_value` STRING, `tagged_object_count` BIGINT",
    "access_table_lineage_blast_radius": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `source_table_full_name` STRING, `target_table_full_name` STRING, `source_type` STRING, `target_type` STRING, `entity_type` STRING, `direct_access` BOOLEAN, `access_class` STRING, `event_count` BIGINT, `distinct_principals` BIGINT, `source_distinct_principals` BIGINT, `last_event_time` TIMESTAMP, `status` STRING",
    "access_column_lineage_sensitive_reach": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `source_table_full_name` STRING, `source_column_name` STRING, `source_sensitivity_tags` STRING, `target_table_full_name` STRING, `target_column_name` STRING, `entity_type` STRING, `direct_access` BOOLEAN, `event_count` BIGINT, `distinct_principals` BIGINT, `last_event_time` TIMESTAMP, `status` STRING",
    "access_pii_propagation_untagged": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `source_table_catalog` STRING, `source_table_schema` STRING, `source_table_name` STRING, `source_column_name` STRING, `source_tags` STRING, `target_table_catalog` STRING, `target_table_schema` STRING, `target_table_name` STRING, `target_column_name` STRING, `distinct_creators` BIGINT, `event_count` BIGINT, `status` STRING",
    "access_views_inventory": "snapshot_at TIMESTAMP, `window_days` INT, `view_catalog` STRING, `view_schema` STRING, `view_name` STRING, `is_materialized` STRING, `is_updatable` STRING, `is_insertable_into` STRING, `sql_path` STRING, `definition_chars` BIGINT, `references_mask_or_filter` BOOLEAN",
    "access_volumes_inventory": "snapshot_at TIMESTAMP, `window_days` INT, `volume_catalog` STRING, `volume_schema` STRING, `volume_name` STRING, `volume_type` STRING, `volume_owner` STRING, `storage_location` STRING, `tag_count` BIGINT, `tag_names` STRING, `created` TIMESTAMP, `status` STRING",
    "access_delta_sharing_exposure": "snapshot_at TIMESTAMP, `window_days` INT, `share_name` STRING, `share_owner` STRING, `recipient_count` BIGINT, `recipients` STRING, `shared_table_count` BIGINT, `shared_schema_count` BIGINT, `created` TIMESTAMP, `status` STRING",
    "storage_target_table_discovery": "snapshot_at TIMESTAMP, `window_days` INT, `target_table` STRING, `table_catalog` STRING, `table_schema` STRING, `table_name` STRING, `table_type` STRING, `data_source_format` STRING",
    "table_inventory_type": "snapshot_at TIMESTAMP, `window_days` INT, `table_catalog` STRING, `table_schema` STRING, `table_type` STRING, `data_source_format` STRING, `table_count` BIGINT",
    "access_dead_table_candidates": "snapshot_at TIMESTAMP, `window_days` INT, `table_catalog` STRING, `table_schema` STRING, `table_name` STRING, `table_type` STRING, `table_owner` STRING, `created` TIMESTAMP, `last_altered` TIMESTAMP, `days_since_altered` BIGINT, `status` STRING",
    "storage_growth": "snapshot_at TIMESTAMP, `window_days` INT, `table_id` STRING, `catalog_name` STRING, `schema_name` STRING, `table_name` STRING, `full_name` STRING, `table_owner` STRING, `no_owner` BOOLEAN, `first_snapshot_date` DATE, `first_bytes` BIGINT, `last_snapshot_date` DATE, `last_bytes` BIGINT, `growth_bytes` BIGINT, `growth_pct` DOUBLE, `dropped_in_window` BOOLEAN, `status` STRING",
    "po_maintenance_cost_by_table": "snapshot_at TIMESTAMP, `window_days` INT, `account_id` STRING, `workspace_id` STRING, `metastore_name` STRING, `catalog_name` STRING, `schema_name` STRING, `table_id` STRING, `table_name` STRING, `operation_type` STRING, `operation_status` STRING, `usage_unit` STRING, `operation_count` BIGINT, `estimated_dbu` DECIMAL(38,6), `first_op_time` TIMESTAMP, `last_op_time` TIMESTAMP, `status` STRING",
    "po_vacuum_reclaimed_bytes": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `catalog_name` STRING, `schema_name` STRING, `table_id` STRING, `table_name` STRING, `vacuum_op_count` BIGINT, `total_deleted_files` DECIMAL(38,0), `total_deleted_bytes` DECIMAL(38,0), `vacuum_estimated_dbu` DECIMAL(38,6), `status` STRING",
    "po_clustering_activity": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `catalog_name` STRING, `schema_name` STRING, `table_id` STRING, `table_name` STRING, `operation_type` STRING, `op_count` BIGINT, `removed_files` DECIMAL(38,0), `clustered_files` DECIMAL(38,0), `removed_bytes` DECIMAL(38,0), `clustered_bytes` DECIMAL(38,0), `clustering_estimated_dbu` DECIMAL(38,6), `status` STRING",
    "po_clustering_column_churn": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `catalog_name` STRING, `schema_name` STRING, `table_id` STRING, `table_name` STRING, `has_column_selection_changed` STRING, `old_clustering_columns` STRING, `new_clustering_columns` STRING, `additional_reason` STRING, `last_selection_time` TIMESTAMP, `selection_event_count` BIGINT, `table_change_count` DECIMAL(38,0), `status` STRING",
    "po_data_skipping_backfill": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `catalog_name` STRING, `schema_name` STRING, `table_id` STRING, `table_name` STRING, `added_data_skipping_columns` STRING, `removed_data_skipping_columns` STRING, `new_data_skipping_columns` STRING, `scanned_bytes` DECIMAL(38,0), `scanned_files` DECIMAL(38,0), `last_event_time` TIMESTAMP, `status` STRING",
    "po_failure_reasons": "snapshot_at TIMESTAMP, `window_days` INT, `workspace_id` STRING, `catalog_name` STRING, `schema_name` STRING, `table_id` STRING, `table_name` STRING, `full_name` STRING, `operation_status` STRING, `is_other` BOOLEAN, `pooled_count` BIGINT, `operation_types_failed` DECIMAL(38,0), `failed_operations` DECIMAL(38,0), `estimated_dbus_spent` DOUBLE, `last_failed_at` TIMESTAMP, `reason` STRING, `fix` STRING, `status` STRING",
    "storage_po_coverage": "snapshot_at TIMESTAMP, `window_days` INT, `section` STRING, `catalog_name` STRING, `schema_name` STRING, `table_id` STRING, `table_name` STRING, `full_name` STRING, `tables_total` BIGINT, `tables_without_po` DECIMAL(38,0), `pct_tables_without_po` DOUBLE, `bytes_total` DECIMAL(38,0), `bytes_without_po` DECIMAL(38,0), `pct_bytes_without_po` DOUBLE, `active_bytes_of_table` BIGINT, `status` STRING",
    "storage_small_files": "snapshot_at TIMESTAMP, `window_days` INT, `catalog_name` STRING, `schema_name` STRING, `table_name` STRING, `full_name` STRING, `table_id` STRING, `table_type` STRING, `table_owner` STRING, `snapshot_date` DATE, `active_bytes` BIGINT, `active_files` BIGINT, `avg_file_size_mb` DOUBLE, `status` STRING",
    "access_source_table_coverage": "snapshot_at TIMESTAMP, `window_days` INT, `catalog_name` STRING, `schema_name` STRING, `table_name` STRING, `has_time_column` BOOLEAN, `time_column_name` STRING, `rows_in_window` DECIMAL(38,0), `total_row_count` BIGINT, `min_time` STRING, `max_time` STRING, `days_with_data_in_window` BIGINT, `first_date_in_window` STRING, `last_date_in_window` STRING, `lag_days` BIGINT, `status` STRING",
    "storage_unused_table_cost": "snapshot_at TIMESTAMP, `window_days` INT, `table_catalog` STRING, `table_schema` STRING, `table_name` STRING, `table_type` STRING, `table_owner` STRING, `active_gb` DOUBLE, `est_storage_usd_month` DOUBLE, `po_upkeep_usd_window` DOUBLE, `price_basis` STRING, `est_total_usd_month` DOUBLE, `days_since_altered` BIGINT, `days_since_last_write` BIGINT, `is_other` BOOLEAN, `pooled_count` BIGINT, `not_assessed_reason` STRING, `status` STRING",
}
HAS_STATUS = {
    "compute_warehouse_idle_minutes",
    "compute_idle_node_ratio",
    "lakeflow_failed_jobs_wasted_dbus",
    "instance_pools_idle_capacity",
    "lakeflow_jobs_on_all_purpose",
    "lakeflow_stale_zombie_jobs",
    "lakeflow_pipeline_idle_tail_duration",
    "compute_warehouse_cache_reuse",
    "cost_period_over_period",
    "cost_unnamed_workspaces",
    "query_failed_queries_daily",
    "query_queuing_waits",
    "lakeflow_job_reliability",
    "lakeflow_job_duration_regression",
    "cost_chargeback_by_warehouse",
    "cost_chargeback_by_cluster",
    "cost_daily_spikes",
    "cost_by_hour_of_day",
    "cost_workspace_names",
    "cost_sku_trend_12m",
    "cost_chargeback_by_allocation_tag",
    "cost_chargeback_by_workspace",
    "cost_chargeback_by_service",
    "cost_chargeback_by_sku",
    "cost_chargeback_by_job",
    "cost_chargeback_identity_by_source",
    "cost_chargeback_by_tag_value",
    "cost_chargeback_reconcile",
    "cost_by_job",
    "cost_by_compute_resource",
    "cost_by_notebook",
    "cost_usage_policy_coverage",
    "cost_actual_vs_list_by_sku",
    "cost_serving_mode_by_endpoint",
    "cost_vector_search_spend",
    "compute_warehouse_idle_gaps",
    "compute_warehouse_autoscale_churn",
    "compute_warehouse_autostop_churn",
    "compute_warehouse_late_autostop",
    "sql_warehouse_events_activity",
    "node_timeline_utilization",
    "compute_cluster_config_posture",
    "compute_warehouse_config_posture",
    "lakeflow_failed_runs",
    "lakeflow_never_started_runs",
    "lakeflow_succeeded_with_failed_tasks",
    "lakeflow_job_queue_time",
    "lakeflow_phase_cold_start",
    "lakeflow_long_running_runs",
    "lakeflow_retries_repairs",
    "lakeflow_jobs_no_timeout",
    "lakeflow_job_tasks_no_timeout",
    "lakeflow_health_rule_coverage",
    "lakeflow_job_ownership_orphans",
    "lakeflow_tasks_near_timeout",
    "lakeflow_job_compute_pressure",
    "task_cluster_utilization",
    "lakeflow_job_oversized",
    "lakeflow_pipeline_cost",
    "lakeflow_pipeline_update_failures_retries",
    "query_costly_statements_grouped",
    "query_costly_statements",
    "query_local_spillage",
    "query_shuffle_write_amplification",
    "query_pruning_effectiveness",
    "query_cache_coldstart",
    "query_failed_statements_grouped",
    "query_warehouse_pressure",
    "query_task_statement_breakdown",
    "compute_serving_endpoint_cost_status",
    "compute_serving_dormant_endpoints",
    "cost_failed_statement_waste",
    "compute_last_activity",
    "query_top_by_cost",
    "access_broad_grants",
    "access_runas_escalation",
    "access_login_concentration",
    "access_network_inbound_denials",
    "access_network_outbound_denials",
    "access_admin_role_change_events",
    "access_classification_coverage",
    "access_classified_unmasked",
    "access_sensitive_table_reads",
    "access_pii_outside_tables",
    "access_table_lineage_blast_radius",
    "access_column_lineage_sensitive_reach",
    "access_pii_propagation_untagged",
    "access_volumes_inventory",
    "access_delta_sharing_exposure",
    "access_dead_table_candidates",
    "storage_growth",
    "po_maintenance_cost_by_table",
    "po_vacuum_reclaimed_bytes",
    "po_clustering_activity",
    "po_clustering_column_churn",
    "po_data_skipping_backfill",
    "po_failure_reasons",
    "storage_po_coverage",
    "storage_small_files",
    "access_source_table_coverage",
    "storage_unused_table_cost",
}

# COMMAND ----------

import re
import time
from concurrent.futures import ThreadPoolExecutor

CATALOG = dbutils.widgets.get("catalog").strip()
if not CATALOG:
    raise ValueError("Set the catalog widget at the top, then run all.")
# Query tags mark these statements in system.query.history; the bill takes the job's or budget policy's tags.
for pair in dbutils.widgets.get("query_tags").split(","):
    key, _, value = (s.strip() for s in pair.partition("="))
    if key and value and re.fullmatch(r"[\w .-]+", key + value):
        try:
            spark.sql(f"SET QUERY_TAGS['{key}'] = '{value}'")
        except Exception as e:  # compute without query tags: carry on untagged
            print(f"query tags not set on this compute: {(str(e).splitlines() or [''])[0][:200]}")
            break
TAG_KEYS = ",".join(k.strip() for k in dbutils.widgets.get("tag_keys").split(",") if k.strip() and "'" not in k)
CHARGEBACK_KEYS = ",".join(k.strip() for k in dbutils.widgets.get("chargeback_keys").split(",") if k.strip() and "'" not in k)
CHECKS["cost_chargeback_by_allocation_tag"] = CHECKS["cost_chargeback_by_allocation_tag"].replace(
    "replace(replace(replace('costcenter,team', ' ', '')", "replace(replace(replace('" + CHARGEBACK_KEYS + "', ' ', '')")
SCHEMA = "databricks_audit_dashboard"
spark.sql(f"USE CATALOG `{CATALOG}`")
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
SNAPSHOT_AT = spark.sql("SELECT CAST(current_timestamp() AS STRING) AS t").first().t
TS = f"CAST('{SNAPSHOT_AT}' AS TIMESTAMP)"


def snapshot(name, sql):
    started = time.time()
    try:
        spark.sql(f"CREATE OR REPLACE TABLE {SCHEMA}.{name} AS\nSELECT {TS} AS snapshot_at, q.* FROM (\n{sql}\n) q")
        error = None
    except Exception as e:  # a missing grant or table: keep going, leave an empty table
        error = (str(e).strip().splitlines() or ["failed"])[0][:500]
        spark.sql(f"CREATE OR REPLACE TABLE {SCHEMA}.{name} ({EMPTY[name]})")
    seconds = round(time.time() - started, 1)
    flags = "count_if(status = 'CRITICAL'), count_if(status = 'WARN')" if name in HAS_STATUS else "0, 0"
    n, critical, warn = spark.sql(f"SELECT count(*), {flags} FROM {SCHEMA}.{name}").first()
    print(("ok  " if error is None else "NOT ") + f"{name} {seconds}s" + (f": {error}" if error else ""))
    return (name, error is None, error, n, critical, warn, name in HAS_STATUS, seconds)


log = [snapshot(n, s) for n, s in DIMS.items()]
spark.sql(
    f"CREATE OR REPLACE TABLE {SCHEMA}.snapshot_info AS SELECT {TS} AS snapshot_at, "
    f"30 AS window_days, '{TAG_KEYS}' AS tag_keys, '{CHARGEBACK_KEYS}' AS chargeback_keys, "
    f"'{','.join(CHECKS)}' AS checks"
)
with ThreadPoolExecutor(max_workers=4) as pool:
    log += list(pool.map(lambda item: snapshot(*item), CHECKS.items()))

(spark.createDataFrame(log, "query_id STRING, ok BOOLEAN, error STRING, row_count BIGINT, critical BIGINT, "
                            "warn BIGINT, has_status BOOLEAN, seconds DOUBLE")
    .selectExpr("*", f"{TS} AS snapshot_at")
    .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{SCHEMA}.snapshot_log"))
failed = [r for r in log if not r[1]]
print(f"{len(log) - len(failed)} of {len(log)} tables created; {len(failed)} not assessed (see snapshot_log)")

# COMMAND ----------

# Overview links to every dashboard of the set. Databricks gives each imported dashboard its own id,
# so they are looked up by name: after importing the dashboards, run this cell again.
CATALOG = dbutils.widgets.get("catalog").strip()
rows = []
try:
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    host = w.config.host.rstrip("/")
    latest = {}
    for d in w.lakeview.list():
        name = d.display_name or ""
        if name.startswith("Lakehouse Health ") and (name not in latest or (d.update_time or "") > (latest[name].update_time or "")):
            latest[name] = d
    rows = [(name, f"{host}/dashboardsv3/{d.dashboard_id}/published") for name, d in sorted(latest.items())]
except Exception as e:  # no SDK or no right to list dashboards: Overview shows no links
    print(f"dashboard links not read: {(str(e).splitlines() or [''])[0][:200]}")
(spark.createDataFrame(rows, "dashboard STRING, url STRING")
    .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"`{CATALOG}`.databricks_audit_dashboard.dashboard_links"))
print(f"{len(rows)} dashboards linked from Overview" + ("" if rows else "; import them, then run this cell again"))
