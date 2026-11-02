"""tools/build_direct_dashboards.py

Builds the AI/BI dashboards in databricks_dashboards/system_tables_direct/ (Waste & savings, Jobs
health, Workspace usage, Broad grants, Admin changes), with an HTML guide to each one's queries. Unlike the Lakehouse Health set,
these read system.* directly: no snapshot notebook, no tables of our own. Each page re-runs its
checks for the picked dates.

    python tools/build_direct_dashboards.py [--out-dir <folder>] [--query-tags k=v,k=v]
                                            [--workspace-class CLASS=PATTERN ...]

The SQL is the app's own checks (app/direct_sql), with the fixed 30-day window swapped for the
dashboard's Time Window. Rerunning writes byte-identical files.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import build_databricks_dashboards as bdd  # noqa: E402

OUT_DIR = bdd.OUT_DIR / "system_tables_direct"

START = "CAST(:time_range.min AS DATE)"
END = "date_add(:time_range.max, 1)"  # exclusive: the whole end day counts
# The checks' 30-day window, as read_check() fills it, and what replaces it.
WINDOW = [
    ("SELECT 30 AS window_days, q.*", f"SELECT datediff({END}, :time_range.min) AS window_days, q.*"),
    ("current_date() - INTERVAL 30 DAYS - INTERVAL 48 HOURS", "CAST(:time_range.min AS TIMESTAMP) - INTERVAL 48 HOURS"),
    ("current_date() - INTERVAL 30 DAYS", START),
    ("dateadd(DAY, -LEAST(30, 90), current_date())", START),
    ("dateadd(DAY, -30, current_date())", START),
    ("dateadd(day, -30, current_date())", START),
    ("date_add(current_date(), -CAST(30 AS INT))", START),
    ("date_trunc('DAY', current_timestamp())", f"CAST({END} AS TIMESTAMP)"),
    ("date_sub(current_date(), 30)", START),
    ("date_add(current_date(), -30)", START),
    ("dateadd(day, -LEAST(30, 90), current_date())", START),
    ("FLOOR(30 / 2)", f"FLOOR(datediff({END}, :time_range.min) / 2)"),
]
# Ages measured from today, not from the window: serving's 90-day request history, stale jobs' 90 days.
KEEP_TODAY = ["datediff(current_date(), rh.earliest_request_time)", "dateadd(day, -90, current_date())"]


def windowed(query_id: str) -> str:
    sql = bdd.read_check(query_id)
    for old, new in WINDOW:
        sql = sql.replace(old, new)
    for i, k in enumerate(KEEP_TODAY):
        sql = sql.replace(k, f"@@{i}@@")
    sql = sql.replace("current_date()", END)
    for i, k in enumerate(KEEP_TODAY):
        sql = sql.replace(f"@@{i}@@", k)
    return "\n".join(l for l in sql.strip().splitlines() if not l.startswith("-- generated from"))


def _indent(sql: str) -> str:
    return "\n".join(("  " + l) if l.strip() else l for l in sql.splitlines())


# Id -> name lookups, latest row per id.
NAMES = {
    "ws": """ws AS (
  SELECT CAST(workspace_id AS STRING) AS workspace_id, workspace_name
  FROM system.access.workspaces_latest
)""",
    "wh": """wh AS (
  SELECT CAST(warehouse_id AS STRING) AS warehouse_id, warehouse_name FROM system.compute.warehouses
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
)""",
    "cl": """cl AS (
  SELECT CAST(cluster_id AS STRING) AS cluster_id, CAST(workspace_id AS STRING) AS workspace_id, cluster_name
  FROM system.compute.clusters
  QUALIFY ROW_NUMBER() OVER (PARTITION BY cluster_id ORDER BY change_time DESC) = 1
)""",
    "jb": """jb AS (
  SELECT CAST(workspace_id AS STRING) AS workspace_id, CAST(job_id AS STRING) AS job_id, name
  FROM system.lakeflow.jobs
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
)""",
    "pl": """pl AS (
  SELECT CAST(workspace_id AS STRING) AS workspace_id, CAST(pipeline_id AS STRING) AS pipeline_id, name
  FROM system.lakeflow.pipelines
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id ORDER BY change_time DESC) = 1
)""",
}
NAME_NOTES = {
    "ws": "Workspace id to name.",
    "wh": "Warehouse id to name: the latest row per warehouse.",
    "cl": "Cluster id to name and workspace: the latest row per cluster.",
    "jb": "Job id to name: the latest row per job.",
    "pl": "Pipeline id to name: the latest row per pipeline.",
}

WS = "COALESCE(ws.workspace_name, CAST({t}.workspace_id AS STRING), 'Account-level')"


def _label(name: str, ws: str) -> str:
    return f"concat({name}, ' · ', {ws})"


@dataclass
class Dataset:
    key: str
    title: str
    checks: list[tuple[str, str]]  # (step name, app check id)
    names: list[str]
    body: str  # the steps after the checks and lookups, then the final SELECT
    shows: str
    used_by: str
    final: str
    steps: dict[str, str] = field(default_factory=dict)  # step -> note, for the guide
    measures: list[dict] = field(default_factory=list)
    head: str = "List price; dates from the Time Window."
    window: bool = True  # reads the Time Window
    params: list[dict] = field(default_factory=list)  # other parameters it reads

    @property
    def name(self) -> str:
        return bdd.hid("dataset:direct:" + self.key)

    def sql(self) -> str:
        parts = [f"{alias} AS (\n{_indent(windowed(check))}\n)" for alias, check in self.checks]
        parts += [NAMES[n] for n in self.names]
        head = f"-- {self.title}. {self.head}\nWITH " + "".join(p + ",\n" for p in parts)
        return head + self.body.strip("\n") + "\n"


# --------------------------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------------------------

WASTE = Dataset(
    "waste", "Possible waste: flagged resources from the 5 priced checks",
    [("idle", "compute_warehouse_idle_minutes"), ("nodes", "compute_idle_node_ratio"),
     ("failed_jobs", "lakeflow_failed_jobs_wasted_dbus"), ("failed_stmts", "cost_failed_statement_waste"),
     ("serving", "compute_serving_endpoint_cost_status")],
    ["ws", "wh", "cl", "jb"],
    f"""src AS (
  SELECT 'Warehouse idle time' AS source, 'SQL warehouse' AS kind, CAST(workspace_id AS STRING) AS workspace_id,
         CAST(warehouse_id AS STRING) AS entity_id, CAST(NULL AS STRING) AS name_hint,
         status, est_wasted_usd_list AS wasted, est_wasted_usd_list AS floor_value, window_days
  FROM idle
  UNION ALL
  SELECT 'Idle cluster nodes', 'Cluster', cl.workspace_id, CAST(n.cluster_id AS STRING), NULL,
         n.status, n.est_wasted_usd_list, n.est_wasted_usd_list, n.window_days
  FROM nodes n LEFT JOIN cl ON cl.cluster_id = CAST(n.cluster_id AS STRING)
  UNION ALL
  -- no floor on failed runs: a job that keeps failing counts even when it is cheap
  SELECT 'Failed job runs', 'Job', CAST(workspace_id AS STRING), CAST(job_id AS STRING), NULL,
         status, est_wasted_usd_list, CAST(NULL AS DOUBLE), window_days
  FROM failed_jobs
  UNION ALL
  SELECT 'Failed SQL statements', 'SQL warehouse', CAST(workspace_id AS STRING), CAST(warehouse_id AS STRING), NULL,
         status, est_wasted_usd_list, est_wasted_usd_list, window_days
  FROM failed_stmts
  UNION ALL
  SELECT 'Serving endpoints with no requests', 'Serving endpoint', CAST(workspace_id AS STRING),
         CAST(endpoint_id AS STRING), endpoint_name,
         status, est_wasted_usd_list, est_usd_list, window_days
  FROM serving
),
flagged AS (
  SELECT * FROM src
  WHERE status IN ('CRITICAL', 'WARN') AND wasted > 0
    AND (floor_value IS NULL OR floor_value >= 50.0 * window_days / 30.0)
),
named AS (
  SELECT f.source, f.kind, f.status, f.entity_id, ROUND(f.wasted, 2) AS possible_usd,
         {WS.format(t='f')} AS workspace,
         COALESCE(wh.warehouse_name, cl.cluster_name, jb.name, f.name_hint, f.entity_id) AS resource_name
  FROM flagged f
  LEFT JOIN ws ON ws.workspace_id = f.workspace_id
  LEFT JOIN wh ON f.kind = 'SQL warehouse' AND wh.warehouse_id = f.entity_id
  LEFT JOIN cl ON f.kind = 'Cluster' AND cl.cluster_id = f.entity_id
  LEFT JOIN jb ON f.kind = 'Job' AND jb.workspace_id = f.workspace_id AND jb.job_id = f.entity_id
),
ranked AS (
  SELECT *, ROW_NUMBER() OVER (ORDER BY possible_usd DESC, workspace, resource_name) AS rn FROM named
)
SELECT source, kind, workspace, status,
       concat(resource_name, ' (', entity_id, ')') AS resource,
       CASE WHEN rn <= 10 THEN {_label('resource_name', 'workspace')} ELSE 'All others' END AS top_resource,
       possible_usd,
       CASE WHEN rn <= 3 THEN possible_usd END AS top3_usd
FROM ranked
ORDER BY possible_usd DESC""",
    "Every resource the five priced checks flag, with its possible waste in $.",
    "The Possible waste page.",
    "One row per flagged resource: source, \"name (id)\", kind, workspace, status, $; the top 10 keep their "
    "name for the chart, the rest read All others; the top 3 also fill top3_usd.",
    {"src": "The five checks stacked into one list: source, kind, workspace, id, status, $ and the window's days.",
     "flagged": "Only Critical or Warn rows with $ above 0 and at least $50 per 30 days (failed job runs: no floor).",
     "named": "Ids turned into names: workspace, and warehouse, cluster, job or endpoint by kind.",
     "ranked": "Numbered from the most $ down, for the top 10 and the top 3."},
)

_WH_BASE = f"""w AS (
  SELECT i.*, {WS.format(t='i')} AS workspace,
         {_label("COALESCE(wh.warehouse_name, CAST(i.warehouse_id AS STRING))", WS.format(t='i'))} AS warehouse
  FROM idle i
  LEFT JOIN wh ON wh.warehouse_id = CAST(i.warehouse_id AS STRING)
  LEFT JOIN ws ON ws.workspace_id = CAST(i.workspace_id AS STRING)
  WHERE i.running_minutes > 0
)"""
_WH_NOTE = "Warehouses that ran in the window, with workspace and warehouse names."


def _allowed(n: int) -> str:
    """A setting a warehouse can take: serverless from 1 min, pro and classic from 10, only below today's."""
    kind = "" if n >= 10 else "warehouse_kind = 'serverless' AND "
    return (f"{kind}usd_per_cluster_minute IS NOT NULL AND autostop_now_idle_minutes IS NOT NULL "
            f"AND (auto_stop_minutes = 0 OR auto_stop_minutes > {n})")


def _save(n: int) -> str:
    return (f"CASE WHEN {_allowed(n)} THEN GREATEST(autostop_now_idle_minutes - autostop_{n}_idle_minutes, 0) "
            f"* usd_per_cluster_minute END AS save_{n},\n"
            f"         CASE WHEN {_allowed(n)} THEN GREATEST(autostop_{n}_cold_starts - autostop_now_cold_starts, 0) "
            f"END AS starts_{n}")


WAREHOUSES = Dataset(
    "warehouses", "Warehouses: idle time, auto-stop and the fix, one row per warehouse",
    [("idle", "compute_warehouse_idle_minutes")], ["ws", "wh"],
    f"""{_WH_BASE},
x AS (
  SELECT w.*,
         {_save(1)},
         {_save(2)},
         {_save(5)},
         {_save(10)},
         DENSE_RANK() OVER (ORDER BY counted_idle_minutes DESC NULLS LAST, workspace_id, warehouse_id) AS rk
  FROM w
)
SELECT warehouse, workspace, warehouse_kind AS kind, status, 1 AS warehouses,
       CASE WHEN status IN ('CRITICAL', 'WARN') THEN 1 ELSE 0 END AS flagged,
       CASE WHEN auto_stop_minutes = 0 THEN 'off' ELSE CAST(auto_stop_minutes AS STRING) END AS auto_stop_now,
       CASE WHEN auto_stop_minutes IS NULL THEN 'Unknown' WHEN auto_stop_minutes = 0 THEN 'Off'
            WHEN auto_stop_minutes <= 1 THEN '01 min' WHEN auto_stop_minutes <= 2 THEN '02 min'
            WHEN auto_stop_minutes <= 5 THEN '03-05 min' WHEN auto_stop_minutes <= 10 THEN '06-10 min'
            WHEN auto_stop_minutes <= 15 THEN '11-15 min' WHEN auto_stop_minutes <= 20 THEN '16-20 min'
            WHEN auto_stop_minutes <= 30 THEN '21-30 min' ELSE '31+ min' END AS autostop_band,
       running_minutes / 60.0 AS running_hours,
       busy_minutes / 60.0 AS busy_hours,
       counted_idle_minutes / 60.0 AS idle_hours,
       stop_tail_minutes / 60.0 AS waiting_hours,
       idle_share_pct,
       est_wasted_usd_list AS idle_usd,
       est_usd_list AS total_usd,
       CASE WHEN rk <= 10 THEN warehouse ELSE 'All others' END AS top_warehouse,
       save_1, starts_1, save_2, starts_2, save_5, starts_5, save_10, starts_10,
       CASE WHEN warehouse_kind = 'serverless' THEN save_1 ELSE save_10 END AS saving_lowest_usd,
       -- the app's fix line: the biggest kind of idle time decides it
       CASE WHEN not_assessed_reason IS NOT NULL THEN 'Not assessed: ' || not_assessed_reason
            WHEN COALESCE(counted_idle_minutes, 0) <= 0 THEN 'Not idle enough to flag on time alone.'
            WHEN stop_tail_minutes > 0 AND stop_tail_minutes >= GREATEST(start_gap_minutes, between_queries_minutes, no_query_minutes)
              THEN concat('Lower auto-stop below ', auto_stop_minutes, ' min: ', ROUND(stop_tail_minutes / 60.0, 1),
                          ' h idle came after the last query, waiting to stop.')
            WHEN no_query_minutes > 0 AND no_query_minutes >= GREATEST(start_gap_minutes, between_queries_minutes)
              THEN concat(ROUND(no_query_minutes / 60.0, 1), ' h running with no query at all: find what kept it up.')
            WHEN between_queries_minutes > 0 AND between_queries_minutes >= start_gap_minutes
              THEN concat('Idle is mostly between queries (', ROUND(between_queries_minutes / 60.0, 1),
                          ' h): run its work together so it runs in fewer bursts.')
            WHEN start_gap_minutes > 0
              THEN concat(ROUND(start_gap_minutes / 60.0, 1), ' h idle before its first query each time it starts: ',
                          'a shorter auto-stop or serverless would cost less.')
            ELSE 'Check whether its schedule matches when it is queried.' END AS the_fix
FROM x
ORDER BY idle_usd DESC NULLS LAST""",
    "Per SQL warehouse: running, busy and idle hours, idle $, its auto-stop setting and band, the saving and "
    "extra cold starts at each shorter setting, and the app's one-line fix.",
    "The Warehouses page (tiles, bands, table) and the per-warehouse table on Auto-stop what-if.",
    "One row per warehouse; save_N / starts_N are the saving and extra cold starts at N minutes, empty where "
    "that setting isn't allowed.",
    {"w": _WH_NOTE,
     "x": "The saving and extra cold starts at 1, 2, 5 and 10 minutes, and a rank by idle minutes for the top 10."},
    [{"displayName": "idle_share", "description": "Idle hours over running hours",
      "expression": "try_divide(SUM(idle_hours), SUM(running_hours))"}],
)

PARTS = Dataset(
    "warehouse_parts", "Warehouse running time, split by what it was doing",
    [("idle", "compute_warehouse_idle_minutes")], ["ws", "wh"],
    f"""{_WH_BASE},
ranked AS (
  SELECT w.*, DENSE_RANK() OVER (ORDER BY running_minutes DESC, workspace_id, warehouse_id) AS rk FROM w
  WHERE w.not_assessed_reason IS NULL
),
parts AS (
  SELECT r.*, p.part,
         CASE p.part WHEN 'Busy' THEN busy_minutes
                     WHEN 'Idle: before first query' THEN start_gap_minutes
                     WHEN 'Idle: between queries' THEN between_queries_minutes
                     WHEN 'Idle: waiting for auto-stop' THEN stop_tail_minutes
                     WHEN 'Idle: no query at all' THEN no_query_minutes
                     ELSE GREATEST(idle_minutes - counted_idle_minutes, 0) END / 60.0 AS part_hours
  FROM ranked r
  CROSS JOIN (VALUES ('Busy'), ('Idle: before first query'), ('Idle: between queries'),
                     ('Idle: waiting for auto-stop'), ('Idle: no query at all'), ('Short pauses (1 min or less)')) AS p(part)
)
SELECT CASE WHEN rk <= 10 THEN warehouse ELSE 'All others' END AS top_warehouse,
       warehouse, workspace, warehouse_kind AS kind, part, ROUND(part_hours, 2) AS hours
FROM parts
ORDER BY rk, part""",
    "Each warehouse's running hours split six ways: busy, four kinds of idle, and short pauses.",
    "The \"Where each warehouse's running time went\" chart on Warehouses.",
    "Six rows per warehouse (one per part) with hours; the 10 that ran longest keep their name.",
    {"w": _WH_NOTE,
     "ranked": "Warehouses that could be measured, ranked by running time.",
     "parts": "Six rows per warehouse: busy, idle before the first query, between queries, waiting for auto-stop, "
              "with no query at all, and short pauses of a minute or less."},
)

WHATIF = Dataset(
    "whatif", "Auto-stop what-if: one row per warehouse and shorter setting",
    [("idle", "compute_warehouse_idle_minutes")], ["ws", "wh"],
    f"""{_WH_BASE},
replay AS (
  SELECT w.*, o.minutes AS setting,
         CASE o.minutes WHEN 1 THEN autostop_1_idle_minutes WHEN 2 THEN autostop_2_idle_minutes
                        WHEN 5 THEN autostop_5_idle_minutes ELSE autostop_10_idle_minutes END AS at_idle_minutes,
         CASE o.minutes WHEN 1 THEN autostop_1_cold_starts WHEN 2 THEN autostop_2_cold_starts
                        WHEN 5 THEN autostop_5_cold_starts ELSE autostop_10_cold_starts END AS at_cold_starts
  FROM w CROSS JOIN (VALUES (1), (2), (5), (10)) AS o(minutes)
  WHERE w.not_assessed_reason IS NULL AND w.autostop_now_idle_minutes IS NOT NULL
    AND w.usd_per_cluster_minute IS NOT NULL AND w.auto_stop_minutes IS NOT NULL
    AND (w.warehouse_kind = 'serverless' OR o.minutes >= 10)
    AND (w.auto_stop_minutes = 0 OR o.minutes < w.auto_stop_minutes)
),
whatif AS (
  SELECT *,
         GREATEST(autostop_now_idle_minutes - at_idle_minutes, 0) * usd_per_cluster_minute AS saving_usd,
         GREATEST(autostop_now_idle_minutes - at_idle_minutes, 0) / 60.0 AS saved_hours,
         GREATEST(COALESCE(at_cold_starts, 0) - COALESCE(autostop_now_cold_starts, 0), 0) AS extra_starts,
         MIN(setting) OVER (PARTITION BY workspace_id, warehouse_id) AS lowest_setting
  FROM replay
  WHERE at_idle_minutes IS NOT NULL
),
steps AS (
  -- each setting's extra saving over the next longer one, so a warehouse's steps add up to its best saving
  SELECT *, saving_usd - COALESCE(LAG(saving_usd) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY setting DESC), 0)
              AS step_saving_usd
  FROM whatif
),
ranked AS (
  SELECT *, DENSE_RANK() OVER (ORDER BY best_saving DESC, workspace_id, warehouse_id) AS rk
  FROM (SELECT *, MAX(saving_usd) OVER (PARTITION BY workspace_id, warehouse_id) AS best_saving FROM steps)
)
SELECT warehouse, workspace, warehouse_kind AS kind, setting,
       concat('Down to ', lpad(CAST(setting AS STRING), 2, '0'), ' min') AS step,
       ROUND(step_saving_usd, 2) AS step_saving,
       lpad(CAST(setting AS STRING), 2, '0') || ' min' AS setting_label,
       ROUND(saving_usd, 2) AS saving, ROUND(saved_hours, 1) AS hours_saved, extra_starts,
       CASE WHEN rk <= 10 THEN warehouse ELSE 'All others' END AS top_warehouse,
       CASE WHEN warehouse_kind = 'serverless' AND setting = 1 THEN saving_usd END AS saving_serverless_1,
       CASE WHEN warehouse_kind = 'serverless' AND setting = 2 THEN saving_usd END AS saving_serverless_2,
       CASE WHEN warehouse_kind = 'serverless' AND setting = 5 THEN saving_usd END AS saving_serverless_5,
       CASE WHEN warehouse_kind <> 'serverless' AND setting = 10 THEN saving_usd END AS saving_classic_10,
       CASE WHEN warehouse_kind = 'serverless' AND setting = 1 THEN extra_starts END AS starts_serverless_1,
       CASE WHEN warehouse_kind = 'serverless' AND setting = 2 THEN extra_starts END AS starts_serverless_2,
       CASE WHEN warehouse_kind = 'serverless' AND setting = 5 THEN extra_starts END AS starts_serverless_5,
       CASE WHEN warehouse_kind <> 'serverless' AND setting = 10 THEN extra_starts END AS starts_classic_10
FROM ranked
ORDER BY saving_usd DESC""",
    "The window's real gaps between queries replayed at shorter auto-stop settings, per warehouse.",
    "The tiles and charts on Auto-stop what-if.",
    "One row per warehouse and setting it could take (serverless 1, 2, 5 min; pro and classic 10 min): saving, "
    "idle hours saved, extra cold starts, plus columns the tiles sum.",
    {"w": _WH_NOTE,
     "replay": "One row per warehouse and shorter setting it could take: serverless from 1 min, pro and classic from 10.",
     "whatif": "Per row: $ saved, idle hours saved, extra cold starts, and the warehouse's lowest setting.",
     "steps": "Each setting's extra saving over the next longer one (now, 10, 5, 2, 1 min): the steps add up.",
     "ranked": "Warehouses ranked by their best saving, for the top 10."},
)

CLUSTERS = Dataset(
    "clusters", "Clusters: idle nodes and auto-termination, one row per cluster",
    [("nodes", "compute_idle_node_ratio"), ("cfg", "classic_clusters_config_current")], ["ws"],
    f"""c AS (
  SELECT * FROM cfg WHERE delete_time IS NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY cluster_id ORDER BY change_time DESC) = 1
),
j AS (
  SELECT COALESCE(CAST(c.cluster_id AS STRING), CAST(n.cluster_id AS STRING)) AS cluster_id,
         c.cluster_name, CAST(c.workspace_id AS STRING) AS workspace_id, c.cluster_source, c.auto_termination_minutes,
         n.total_slices, n.idle_ratio, n.avg_cpu_pct_all, n.est_usd_list, n.est_wasted_usd_list, n.status
  FROM c FULL OUTER JOIN nodes n ON CAST(n.cluster_id AS STRING) = CAST(c.cluster_id AS STRING)
),
named AS (
  SELECT j.*, {WS.format(t='j')} AS workspace,
         CASE WHEN cluster_source IN ('UI', 'API') THEN 'All-purpose'
              WHEN cluster_source = 'JOB' THEN 'Job' ELSE COALESCE(initcap(cluster_source), 'Unknown') END AS cluster_kind,
         COALESCE(total_slices, 0) > 0 AS has_run
  FROM j LEFT JOIN ws ON ws.workspace_id = j.workspace_id
),
ranked AS (
  SELECT *, DENSE_RANK() OVER (ORDER BY CASE WHEN has_run AND total_slices >= 60 THEN idle_ratio END DESC NULLS LAST,
                                        cluster_id) AS rk
  FROM named WHERE cluster_kind <> 'Job'
)
SELECT concat(COALESCE(cluster_name, cluster_id), ' (', cluster_id, ')') AS cluster,
       workspace, cluster_kind AS kind, status, 1 AS clusters,
       CASE WHEN has_run THEN 'Ran' ELSE 'Did not run' END AS ran,
       CASE WHEN auto_termination_minutes IS NULL THEN 'unknown' WHEN auto_termination_minutes = 0 THEN 'off'
            ELSE CAST(auto_termination_minutes AS STRING) END AS auto_termination,
       CASE WHEN auto_termination_minutes IS NULL THEN 'Unknown' WHEN auto_termination_minutes = 0 THEN 'Off'
            WHEN auto_termination_minutes <= 15 THEN '015 min or less' WHEN auto_termination_minutes <= 30 THEN '016-030 min'
            WHEN auto_termination_minutes <= 60 THEN '031-060 min' WHEN auto_termination_minutes <= 120 THEN '061-120 min'
            ELSE '121+ min' END AS autoterm_band,
       CASE WHEN cluster_kind = 'All-purpose' AND auto_termination_minutes = 0 THEN 1 ELSE 0 END AS no_autoterm,
       ROUND(idle_ratio * 100, 1) AS idle_pct,
       ROUND(avg_cpu_pct_all, 1) AS avg_cpu_pct,
       est_wasted_usd_list AS idle_usd,
       CASE WHEN status IN ('CRITICAL', 'WARN') THEN est_wasted_usd_list END AS flagged_idle_usd,
       est_usd_list AS total_usd,
       CASE WHEN rk <= 10 AND has_run AND total_slices >= 60
            THEN {_label("COALESCE(cluster_name, cluster_id)", "workspace")} END AS top_cluster
FROM ranked
ORDER BY idle_usd DESC NULLS LAST""",
    "Per all-purpose (and other non-job) cluster: its auto-termination setting and band, whether it ran, its "
    "idle-node ratio, average CPU, idle $ and total $.",
    "The Clusters & pools page, except the pool tile and table.",
    "One row per cluster; top_cluster names the 10 with the highest idle ratio (with enough data to judge).",
    {"c": "Each cluster's current settings, deleted ones left out.",
     "j": "Settings joined to the idle-node check; FULL OUTER keeps clusters that didn't run and ones with no settings row.",
     "named": "Workspace name, kind (all-purpose, job, ...) and whether it ran.",
     "ranked": "Job clusters left out; the rest ranked by idle ratio for the top 10."},
)

POOLS = Dataset(
    "pools", "Instance pools: idle instances kept warm",
    [("pools", "instance_pools_idle_capacity")], ["ws"],
    f"""p AS (
  SELECT p.*, {WS.format(t='p')} AS workspace
  FROM pools p LEFT JOIN ws ON ws.workspace_id = CAST(p.workspace_id AS STRING)
  WHERE p.delete_time IS NULL
)
SELECT concat(instance_pool_name, ' (', instance_pool_id, ')') AS pool, workspace, node_type,
       min_idle_instances, idle_instance_autotermination_minutes AS idle_autotermination_minutes,
       est_usd_list AS total_usd, status
FROM p
ORDER BY min_idle_instances DESC""",
    "Each instance pool's standing idle floor (min idle instances) and its $.",
    "The pool tile and table on Clusters & pools.",
    "One row per pool.",
    {"p": "Live pools with their workspace name."},
)

FAILED_JOBS = Dataset(
    "failed_jobs", "Failed job runs: $ of runs that failed, per job",
    [("failed_jobs", "lakeflow_failed_jobs_wasted_dbus")], ["ws", "jb"],
    f"""f AS (
  SELECT f.*, {WS.format(t='f')} AS workspace, COALESCE(jb.name, CAST(f.job_id AS STRING)) AS job_name,
         ROW_NUMBER() OVER (ORDER BY f.est_wasted_usd_list DESC NULLS LAST, f.failed_runs DESC, f.job_id) AS rn
  FROM failed_jobs f
  LEFT JOIN ws ON ws.workspace_id = CAST(f.workspace_id AS STRING)
  LEFT JOIN jb ON jb.workspace_id = CAST(f.workspace_id AS STRING) AND jb.job_id = CAST(f.job_id AS STRING)
)
SELECT concat(job_name, ' (', job_id, ')') AS job, workspace, status,
       CASE WHEN rn <= 10 THEN {_label('job_name', 'workspace')} ELSE 'All others' END AS top_job,
       distinct_runs AS runs, failed_runs, repaired_runs, failure_rate_pct,
       est_wasted_usd_list AS wasted_usd, last_failed_termination_code AS last_failure,
       CASE WHEN status IN ('CRITICAL', 'WARN') THEN 1 ELSE 0 END AS flagged
FROM f
ORDER BY wasted_usd DESC NULLS LAST""",
    "Per job with failed or repaired runs: runs, failures, failure rate, the $ burned and the last error.",
    "The jobs half of Failed runs & queries.",
    "One row per job with failures; the 10 costliest keep their name for the chart.",
    {"f": "Workspace and job names added; ranked by wasted $ for the top 10."},
)

FAILED_STMTS = Dataset(
    "failed_statements", "Failed SQL statements: $ of statements that failed, per warehouse",
    [("failed_stmts", "cost_failed_statement_waste")], ["ws", "wh"],
    f"""f AS (
  SELECT f.*, {WS.format(t='f')} AS workspace,
         COALESCE(wh.warehouse_name, CAST(f.warehouse_id AS STRING)) AS warehouse_name,
         ROW_NUMBER() OVER (ORDER BY f.est_wasted_usd_list DESC NULLS LAST, f.failed_statements DESC, f.warehouse_id) AS rn
  FROM failed_stmts f
  LEFT JOIN ws ON ws.workspace_id = CAST(f.workspace_id AS STRING)
  LEFT JOIN wh ON wh.warehouse_id = CAST(f.warehouse_id AS STRING)
)
SELECT concat(warehouse_name, ' (', warehouse_id, ')') AS warehouse, workspace, status,
       CASE WHEN rn <= 10 THEN {_label('warehouse_name', 'workspace')} ELSE 'All others' END AS top_warehouse,
       statements, failed_statements, canceled_statements, est_wasted_usd_list AS failed_usd,
       failed_cost_share_pct, cost_basis
FROM f
ORDER BY failed_usd DESC NULLS LAST""",
    "Per warehouse with failed statements: how many failed and the $ they used.",
    "The queries half of Failed runs & queries.",
    "One row per warehouse; the 10 costliest keep their name for the chart.",
    {"f": "Workspace and warehouse names added; ranked by failed $ for the top 10."},
)

SERVING = Dataset(
    "serving", "Serving endpoints: $ and requests, per endpoint",
    [("serving", "compute_serving_endpoint_cost_status")], ["ws"],
    f"""e AS (
  SELECT e.*, COALESCE(e.workspace_name, ws.workspace_name, CAST(e.workspace_id AS STRING)) AS workspace,
         CASE WHEN COALESCE(e.endpoint_requests_window, 0) = 0 THEN e.est_usd_list END AS no_request_usd
  FROM serving e LEFT JOIN ws ON ws.workspace_id = CAST(e.workspace_id AS STRING)
),
ranked AS (
  -- spend with no requests first: that is the waste; then the rest by cost
  SELECT *, ROW_NUMBER() OVER (ORDER BY no_request_usd DESC NULLS LAST, est_usd_list DESC NULLS LAST, endpoint_id) AS rn
  FROM e
)
SELECT concat(COALESCE(endpoint_name, endpoint_id), ' (', COALESCE(endpoint_id, endpoint_name), ')') AS endpoint,
       workspace, status, tracking_status AS tracking,
       CASE WHEN rn <= 10 THEN {_label('COALESCE(endpoint_name, endpoint_id)', 'workspace')} ELSE 'All others' END AS top_endpoint,
       CASE WHEN COALESCE(endpoint_requests_window, 0) > 0 THEN 'Has requests' ELSE 'No requests' END AS traffic,
       CASE WHEN COALESCE(endpoint_requests_window, 0) > 0 THEN 1 ELSE 0 END AS with_traffic,
       COALESCE(endpoint_requests_window, 0) AS requests, endpoint_last_request_date AS last_request,
       est_usd_list AS total_usd, est_wasted_usd_list AS wasted_usd,
       no_request_usd,
       CASE WHEN COALESCE(endpoint_requests_window, 0) = 0 AND est_usd_list > 0 THEN 1 ELSE 0 END AS billed_no_requests
FROM ranked
ORDER BY rn""",
    "Per model-serving endpoint: its $, requests in the window, request tracking and status.",
    "The Serving endpoints page, except the dormant table.",
    "One row per endpoint, the most $ with no requests first; those 10 keep their name for the chart.",
    {"e": "Workspace name added, and the endpoint's $ when it had no requests.",
     "ranked": "Ranked by $ with no requests, then by $, for the top 10."},
)

DORMANT = Dataset(
    "dormant_endpoints", "Dormant serving endpoints: no traffic",
    [("dormant", "compute_serving_dormant_endpoints")], ["ws"],
    f"""d AS (
  SELECT d.*, {WS.format(t='d')} AS workspace
  FROM dormant d LEFT JOIN ws ON ws.workspace_id = CAST(d.workspace_id AS STRING)
)
SELECT concat(COALESCE(endpoint_name, endpoint_id), ' (', endpoint_id, ')') AS endpoint, workspace, status,
       total_requests AS requests, last_request_date AS last_request, billed_dbus,
       CASE WHEN usage_tracked THEN 'on' ELSE 'off' END AS tracking, not_assessed_reason
FROM d
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, billed_dbus DESC NULLS LAST""",
    "Serving endpoints with no traffic, and whether request tracking is on.",
    "The dormant table on Serving endpoints.",
    "One row per endpoint, worst first.",
    {"d": "Workspace name added."},
)

STALE = Dataset(
    "stale_jobs", "Stale jobs: no run for a long time",
    [("stale", "lakeflow_stale_zombie_jobs")], ["ws"],
    f"""s AS (
  SELECT s.*, {WS.format(t='s')} AS workspace
  FROM stale s LEFT JOIN ws ON ws.workspace_id = CAST(s.workspace_id AS STRING)
)
SELECT concat(COALESCE(name, CAST(job_id AS STRING)), ' (', job_id, ')') AS job, workspace, status,
       CAST(CAST(last_run_start AS DATE) AS STRING) AS last_run, CAST(CAST(create_time AS DATE) AS STRING) AS created,
       est_usd_list AS total_usd, CASE WHEN status IN ('CRITICAL', 'WARN') THEN 1 ELSE 0 END AS flagged
FROM s
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, last_run NULLS FIRST""",
    "Jobs with no recent run: Warn after 30 days, Critical after 90.",
    "The stale jobs table on Flagged, not priced.",
    "One row per job, worst first.",
    {"s": "Workspace name added."},
)

ALL_PURPOSE = Dataset(
    "jobs_on_all_purpose", "Jobs on all-purpose clusters",
    [("ap", "lakeflow_jobs_on_all_purpose")], ["ws", "jb", "cl"],
    f"""a AS (
  SELECT a.*, {WS.format(t='a')} AS workspace,
         COALESCE(jb.name, a.job_id) AS job_name, COALESCE(cl.cluster_name, a.compute_id) AS cluster_name
  FROM ap a
  LEFT JOIN ws ON ws.workspace_id = CAST(a.workspace_id AS STRING)
  LEFT JOIN jb ON jb.workspace_id = CAST(a.workspace_id AS STRING) AND jb.job_id = a.job_id
  LEFT JOIN cl ON cl.cluster_id = a.compute_id
  WHERE a.job_id IS NOT NULL
)
SELECT concat(job_name, ' (', job_id, ')') AS job, concat(cluster_name, ' (', compute_id, ')') AS cluster,
       workspace, status, cluster_source, task_runs, est_usd_list AS total_usd, jobs_sharing_cluster,
       CASE WHEN status IN ('CRITICAL', 'WARN') THEN 1 ELSE 0 END AS flagged
FROM a
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, total_usd DESC NULLS LAST""",
    "Jobs whose tasks ran on all-purpose clusters (interactive pricing) instead of job clusters.",
    "The jobs-on-all-purpose table on Flagged, not priced.",
    "One row per job and cluster, worst first.",
    {"a": "Workspace, job and cluster names added; rows without a job left out."},
)

PIPELINES = Dataset(
    "pipeline_idle_tail", "Pipelines: compute billed outside update time",
    [("pipes", "lakeflow_pipeline_idle_tail_duration")], ["ws", "pl"],
    f"""p AS (
  SELECT p.*, {WS.format(t='p')} AS workspace, COALESCE(pl.name, CAST(p.pipeline_id AS STRING)) AS pipeline_name
  FROM pipes p
  LEFT JOIN ws ON ws.workspace_id = CAST(p.workspace_id AS STRING)
  LEFT JOIN pl ON pl.workspace_id = CAST(p.workspace_id AS STRING) AND pl.pipeline_id = CAST(p.pipeline_id AS STRING)
)
SELECT concat(pipeline_name, ' (', pipeline_id, ')') AS pipeline, workspace, status, updates,
       ROUND(idle_dbus, 1) AS idle_dbus, ROUND(idle_share * 100, 1) AS idle_pct, est_usd_list AS total_usd,
       CASE WHEN status IN ('CRITICAL', 'WARN') THEN 1 ELSE 0 END AS flagged
FROM p
ORDER BY idle_dbus DESC""",
    "Per pipeline: DBUs billed in hours with no update running (the idle tail).",
    "The pipelines table on Flagged, not priced.",
    "One row per pipeline, most idle DBUs first.",
    {"p": "Workspace and pipeline names added."},
)

DATASETS = [WASTE, WAREHOUSES, PARTS, WHATIF, CLUSTERS, POOLS, FAILED_JOBS, FAILED_STMTS, SERVING, DORMANT,
            STALE, ALL_PURPOSE, PIPELINES]


# --------------------------------------------------------------------------------------------
# Widgets: the Lakehouse Health builders (shapes that import cleanly), on its 12-column grid
# --------------------------------------------------------------------------------------------

GRID = 12
TIME_PARAM = {"displayName": "time_range", "keyword": "time_range", "dataType": "DATE", "complexType": "RANGE",
              "defaultSelection": {"range": {"dataType": "DATE", "min": {"value": "now-30d/d"},
                                             "max": {"value": "now-1d/d"}}}}
FORMATS = {"money": "$0,0", "int": "0,0", "num": "0,0.[0]", "pct": "0.[0]"}
PLAIN = {"type": "number-plain", "abbreviation": "compact", "decimalPlaces": {"type": "max", "places": 0}}
PERCENT = {"type": "number-percent", "decimalPlaces": {"type": "max", "places": 0}}


def dataset_json(d: Dataset) -> dict:
    out = {"name": d.name, "displayName": d.title,
           "queryLines": [l + "\n" for l in d.sql().rstrip("\n").split("\n")],
           "parameters": ([TIME_PARAM] if d.window else []) + d.params}
    if d.measures:
        out["columns"] = d.measures
    return out


def time_window(key: str, datasets: list[Dataset]) -> dict:
    queries = [{"name": f"q_{bdd.hid('time:' + key + d.key)}",
                "query": {"datasetName": d.name, "parameters": [{"name": "time_range", "keyword": "time_range"}],
                          "disaggregated": False}} for d in datasets]
    return {"widget": {
        "name": bdd.hid("widget:time:" + key), "queries": queries,
        "spec": {"version": 2, "widgetType": "filter-date-range-picker",
                 "encodings": {"fields": [{"parameterName": "time_range", "queryName": q["name"]} for q in queries]},
                 "selection": {"defaultSelection": TIME_PARAM["defaultSelection"]},
                 "frame": {"showTitle": True, "title": "Time Window"}}}}


def field_filter(key: str, col: str, title: str, datasets: list[Dataset]) -> dict:
    queries = [{"name": f"q_{bdd.hid('field:' + key + d.key)}",
                "query": {"datasetName": d.name, "disaggregated": False,
                          "fields": [bdd.bare_field(col),
                                     {"name": f"{col}_associativity",
                                      "expression": "COUNT_IF(`associative_filter_predicate_group`)"}]}}
               for d in datasets]
    return {"widget": {
        "name": bdd.hid("widget:field:" + key), "queries": queries,
        "spec": {"version": 2, "widgetType": "filter-multi-select",
                 "encodings": {"fields": [{"fieldName": col, "displayName": title, "queryName": q["name"]}
                                          for q in queries]},
                 "frame": {"showTitle": True, "title": title}}}}


def header(key: str, title: str, line: str) -> dict:
    return bdd.text_widget(key, [f"# **{title}**\n", "---\n", "\n", f"**{line}**\n"], GRID, 2, 0, 0)


def counter(key: str, d: Dataset, col: str, title: str, desc: str, money: bool = False, agg: str = "SUM") -> dict:
    w = bdd.counter_widget(key, d.name, col, title, currency=money, agg=agg, width=1, height=1, x=0, y=0)
    if not money:
        w["widget"]["spec"]["encodings"]["value"]["format"] = dict(PLAIN)
    w["widget"]["spec"]["frame"].update({"description": desc, "showDescription": True})
    return w


def measure_counter(key: str, d: Dataset, measure: str, title: str, desc: str) -> dict:
    w = counter(key, d, measure, title, desc)
    f = w["widget"]["queries"][0]["query"]["fields"][0]
    f.update({"name": f"measure({measure})", "expression": f"MEASURE(`{measure}`)"})
    w["widget"]["spec"]["encodings"]["value"].update({"fieldName": f["name"], "format": dict(PERCENT)})
    return w


def bar(key: str, d: Dataset, cat: str, cat_title: str, col: str, val_title: str, title: str, desc: str,
        money: bool = True, color: str | None = None, natural: bool = False, agg: str = "SUM") -> dict:
    w = bdd.bar_widget(key, d.name, cat, col, title, category_label=cat_title, value_label=val_title,
                       color_col=color, currency=money, agg=agg, width=1, height=1, x=0, y=0)
    spec = w["widget"]["spec"]
    if natural:
        spec["encodings"]["y"]["scale"]["sort"] = {"by": "natural-order"}
    if not money:
        spec["encodings"]["x"]["format"] = dict(PLAIN)
    spec["frame"].update({"description": desc, "showDescription": True})
    return w


def table(key: str, d: Dataset, cols: list[tuple[str, str, str]], title: str) -> dict:
    """cols: (field, title, kind) with kind text, money, int, num or pct."""
    spec = [(f, t, "string" if k == "text" else "number", FORMATS.get(k)) for f, t, k in cols]
    return bdd.data_table_widget(key, d.name, spec, title, x=0, y=0, width=1, height=1)


def place(rows: list[tuple[int, list]]) -> list[dict]:
    """rows of (height, widgets); a widget is a dict (equal share of the 12 columns) or (dict, width)."""
    out, y = [], 0
    for h, row in rows:
        x = 0
        for item in row:
            w, width = item if isinstance(item, tuple) else (item, GRID // len(row))
            out.append({**w, "position": {"x": x, "y": y, "width": width, "height": h}})
            x += width
        y += h
    return out


def _page(name: str, title: str, layout: list, kind: str = "PAGE_TYPE_CANVAS", prefix: str = "waste") -> dict:
    return {"name": bdd.hid(f"page:{prefix}:" + name), "displayName": title, "pageType": kind,
            "layoutVersion": "GRID_V1", "layout": layout}


# --------------------------------------------------------------------------------------------
# Waste & savings: the app's Waste tab plus the idle compute it points to
# --------------------------------------------------------------------------------------------

def waste_dashboard() -> dict:
    W, H, HP, F, C, P = WASTE, WAREHOUSES, PARTS, WHATIF, CLUSTERS, POOLS
    J, Q, E, D, S, A, PL = FAILED_JOBS, FAILED_STMTS, SERVING, DORMANT, STALE, ALL_PURPOSE, PIPELINES
    priced = place([
        (2, [header("waste:head", "Possible waste",
                    "What we pay for that does nothing: idle warehouses and nodes, failed runs and queries, endpoints "
                    "with no requests. List price; Time Window and Workspace in Global Filters.")]),
        (1, [(field_filter("waste:source", "source", "Source", [W]), 4),
             (field_filter("waste:kind", "kind", "Kind", [W]), 4)]),
        (3, [counter("waste:total", W, "possible_usd", "Possible waste", "Flagged resources, list price", True),
             counter("waste:top3", W, "top3_usd", "Top 3 resources", "The three biggest together", True),
             counter("waste:max", W, "possible_usd", "Biggest single fix", "The one to fix first", True, "MAX"),
             counter("waste:count", W, "possible_usd", "Resources flagged", "Over the floor on any source",
                     agg="COUNT")]),
        (7, [bar("waste:by_source", W, "source", "Source", "possible_usd", "Possible $",
                 "Possible waste by source", "Which kind of waste is biggest"),
             bar("waste:by_ws", W, "workspace", "Workspace", "possible_usd", "Possible $",
                 "Possible waste by workspace and source", "Where it runs", color="source")]),
        (8, [bar("waste:top", W, "top_resource", "Resource", "possible_usd", "Possible $",
                 "Top 10 resources by possible waste", "The rest in All others", color="source")]),
        (10, [table("waste:table", W, [("source", "Source", "text"), ("resource", "Resource", "text"),
                                       ("kind", "Kind", "text"), ("workspace", "Workspace", "text"),
                                       ("status", "Status", "text"), ("possible_usd", "Possible $", "money")],
                    "Every flagged resource, worst first")]),
    ])
    warehouses = place([
        (2, [header("wh:head", "Warehouses",
                    "Where each SQL warehouse's running time went, idle cost by auto-stop setting, and the fix "
                    "for each one.")]),
        (1, [(field_filter("wh:kind", "kind", "Warehouse type", [H, HP]), 4)]),
        (3, [measure_counter("wh:share", H, "idle_share", "Idle while running", "Idle hours over running hours"),
             counter("wh:idle_usd", H, "idle_usd", "Idle cost", "Idle cluster time at list price", True),
             counter("wh:waiting", H, "waiting_hours", "Waiting for auto-stop", "Hours after the last query"),
             counter("wh:flagged", H, "flagged", "Warehouses flagged", "Critical or Warn")]),
        (9, [bar("wh:split", HP, "top_warehouse", "Warehouse", "hours", "Hours",
                 "Where each warehouse's running time went", "The 10 that ran longest; the rest in All others",
                 money=False, color="part")]),
        (7, [bar("wh:bands", H, "autostop_band", "Auto-stop after", "warehouses", "Warehouses",
                 "Warehouses by auto-stop setting", "By type", money=False, color="kind", natural=True),
             bar("wh:band_cost", H, "autostop_band", "Auto-stop after", "idle_usd", "Idle $",
                 "Idle cost by auto-stop setting", "Long or no auto-stop costs most", natural=True)]),
        (10, [table("wh:table", H, [("warehouse", "Warehouse", "text"), ("kind", "Type", "text"),
                                    ("auto_stop_now", "Auto-stop, min", "text"), ("idle_share_pct", "Idle %", "pct"),
                                    ("idle_hours", "Idle h", "num"), ("waiting_hours", "Waiting to stop, h", "num"),
                                    ("idle_usd", "Idle $", "money"), ("status", "Status", "text"),
                                    ("the_fix", "The fix", "text")],
                    "Idle time by warehouse, most idle $ first")]),
    ])
    whatif = place([
        (2, [header("wf:head", "Auto-stop what-if",
                    "The window's real gaps between queries, replayed at a shorter auto-stop. Shorter saves idle $ "
                    "and adds cold starts: seconds on serverless, minutes on pro and classic.")]),
        (1, [(field_filter("wf:kind", "kind", "Warehouse type", [F, H]), 4)]),
        (3, [counter("wf:s1", F, "saving_serverless_1", "Serverless at 1 min", "Set through the API", True),
             counter("wf:s2", F, "saving_serverless_2", "Serverless at 2 min", "Set through the API", True),
             counter("wf:s5", F, "saving_serverless_5", "Serverless at 5 min", "Lowest in the UI", True),
             counter("wf:c10", F, "saving_classic_10", "Pro / classic at 10 min", "Their lowest", True)]),
        (2, [counter("wf:n1", F, "starts_serverless_1", "Extra cold starts", "Serverless at 1 min"),
             counter("wf:n2", F, "starts_serverless_2", "Extra cold starts", "Serverless at 2 min"),
             counter("wf:n5", F, "starts_serverless_5", "Extra cold starts", "Serverless at 5 min"),
             counter("wf:n10", F, "starts_classic_10", "Extra cold starts", "Pro / classic at 10 min")]),
        (7, [bar("wf:by_setting", F, "setting_label", "Auto-stop after", "saving", "Saving",
                 "Saving by auto-stop setting", "Warehouses that could take each setting", color="kind", natural=True),
             bar("wf:starts", F, "setting_label", "Auto-stop after", "extra_starts", "Extra starts",
                 "Extra cold starts by auto-stop setting", "The cost of a shorter setting",
                 money=False, color="kind", natural=True)]),
        (8, [bar("wf:steps", F, "top_warehouse", "Warehouse", "step_saving", "Saving",
                 "What each step down saves, top 10 warehouses",
                 "Each colour is the extra $ from one step down (now, 10, 5, 2, 1 min); the whole bar is the "
                 "shortest setting it can take", color="step")]),
        (10, [table("wf:table", H, [("warehouse", "Warehouse", "text"), ("kind", "Type", "text"),
                                    ("auto_stop_now", "Auto-stop now, min", "text"),
                                    ("save_1", "1 min: saves", "money"), ("starts_1", "+ starts", "int"),
                                    ("save_2", "2 min: saves", "money"), ("starts_2", "+ starts", "int"),
                                    ("save_5", "5 min: saves", "money"), ("starts_5", "+ starts", "int"),
                                    ("save_10", "10 min: saves", "money"), ("starts_10", "+ starts", "int")],
                    "What each setting saves, per warehouse (null: not allowed for its type, or not shorter than now)")]),
    ])
    clusters = place([
        (2, [header("cl:head", "Clusters & pools",
                    "Idle nodes on all-purpose clusters, auto-termination settings, and idle instances kept warm "
                    "in pools.")]),
        (3, [counter("cl:idle_usd", C, "flagged_idle_usd", "Idle clusters", "Critical or Warn, list price", True),
             counter("cl:worst", C, "idle_pct", "Worst idle-node ratio", "% of node time near 0% CPU", agg="MAX"),
             counter("cl:pools", P, "min_idle_instances", "Pool idle capacity", "Min idle instances, all pools"),
             counter("cl:noterm", C, "no_autoterm", "No auto-terminate", "All-purpose clusters with it off")]),
        (8, [bar("cl:ratio", C, "top_cluster", "Cluster", "idle_pct", "Idle-node %",
                 "Worst idle-node ratio, by cluster", "Top 10 with enough data to judge", money=False, agg="MAX")]),
        (7, [bar("cl:bands", C, "autoterm_band", "Auto-terminate after", "clusters", "Clusters",
                 "All-purpose clusters by auto-termination", "Ran in the window or not", money=False, color="ran",
                 natural=True),
             bar("cl:band_cost", C, "autoterm_band", "Auto-terminate after", "idle_usd", "Idle $",
                 "Idle cost by auto-termination", "Idle node time at list price", natural=True)]),
        (10, [table("cl:table", C, [("cluster", "Cluster", "text"), ("workspace", "Workspace", "text"),
                                    ("kind", "Kind", "text"), ("ran", "Ran", "text"),
                                    ("auto_termination", "Auto-terminate, min", "text"),
                                    ("idle_pct", "Idle-node %", "pct"), ("avg_cpu_pct", "Avg CPU %", "pct"),
                                    ("idle_usd", "Idle $", "money"), ("total_usd", "Total $", "money"),
                                    ("status", "Status", "text")],
                    "Clusters, most idle $ first")]),
        (7, [table("cl:pools_table", P, [("pool", "Pool", "text"), ("workspace", "Workspace", "text"),
                                         ("node_type", "Node type", "text"),
                                         ("min_idle_instances", "Min idle", "int"),
                                         ("idle_autotermination_minutes", "Idle auto-terminate, min", "int"),
                                         ("total_usd", "$", "money"), ("status", "Status", "text")],
                   "Instance pools, largest idle floor first")]),
    ])
    failures = place([
        (2, [header("fail:head", "Failed runs & queries", "Compute paid for job runs and SQL statements that failed.")]),
        (3, [counter("fail:jobs_usd", J, "wasted_usd", "Cost of failed runs",
                     "Failed runs plus failed repair attempts", True),
             counter("fail:jobs", J, "flagged", "Jobs flagged", "Critical or Warn"),
             counter("fail:stmts_usd", Q, "failed_usd", "Cost of failed statements", "Failed SQL statements", True),
             counter("fail:stmts", Q, "failed_statements", "Failed statements", "On all warehouses")]),
        (7, [bar("fail:top_jobs", J, "top_job", "Job", "wasted_usd", "Wasted $", "Jobs by cost of failed runs",
                 "Top 10; the rest in All others"),
             bar("fail:top_wh", Q, "top_warehouse", "Warehouse", "failed_usd", "Failed $",
                 "Warehouses by cost of failed statements", "Top 10; the rest in All others")]),
        (9, [table("fail:jobs_table", J, [("job", "Job", "text"), ("workspace", "Workspace", "text"),
                                          ("runs", "Runs", "int"), ("failed_runs", "Failed", "int"),
                                          ("repaired_runs", "Repaired", "int"), ("failure_rate_pct", "Failure %", "pct"),
                                          ("wasted_usd", "Wasted $", "money"), ("last_failure", "Last failure", "text"),
                                          ("status", "Status", "text")],
                   "Jobs with failed runs, most $ first")]),
        (8, [table("fail:stmts_table", Q, [("warehouse", "Warehouse", "text"), ("workspace", "Workspace", "text"),
                                           ("statements", "Statements", "int"), ("failed_statements", "Failed", "int"),
                                           ("canceled_statements", "Canceled", "int"),
                                           ("failed_usd", "Failed $", "money"),
                                           ("failed_cost_share_pct", "Share of $", "pct"), ("status", "Status", "text")],
                   "Warehouses with failed statements, most $ first")]),
    ])
    serving = place([
        (2, [header("ep:head", "Serving endpoints", "Model-serving endpoints: what they cost and whether anyone called them.")]),
        (3, [counter("ep:traffic", E, "with_traffic", "Endpoints with traffic", "At least one request"),
             counter("ep:requests", E, "requests", "Requests", "In the Time Window"),
             counter("ep:no_req_usd", E, "wasted_usd", "Spend with no requests", "Counted as waste", True),
             counter("ep:no_req", E, "billed_no_requests", "Billed, no requests", "Endpoints")]),
        (8, [bar("ep:top", E, "top_endpoint", "Endpoint", "total_usd", "$", "Endpoints by spend with no requests",
                 "Top 10 by $ with zero requests, then by $; the rest in All others", color="traffic")]),
        (9, [table("ep:table", E, [("endpoint", "Endpoint", "text"), ("workspace", "Workspace", "text"),
                                   ("requests", "Requests", "int"), ("last_request", "Last request", "text"),
                                   ("total_usd", "$", "money"), ("wasted_usd", "Waste $", "money"),
                                   ("tracking", "Request tracking", "text"), ("status", "Status", "text")],
                   "Endpoints, most $ with no requests first")]),
        (8, [table("ep:dormant", D, [("endpoint", "Endpoint", "text"), ("workspace", "Workspace", "text"),
                                     ("requests", "Requests", "int"), ("last_request", "Last request", "text"),
                                     ("billed_dbus", "Billed DBUs", "num"), ("tracking", "Tracking", "text"),
                                     ("status", "Status", "text")],
                   "Dormant endpoints: no traffic")]),
    ])
    unpriced = place([
        (2, [header("np:head", "Flagged, not priced",
                    "Worth fixing, but with no clean $ figure: stale jobs, jobs on all-purpose clusters, pipelines "
                    "billed outside updates.")]),
        (3, [counter("np:stale", S, "flagged", "Stale jobs", "No run in 30+ days"),
             counter("np:ap", A, "flagged", "Jobs on all-purpose", "Interactive pricing for scheduled work"),
             counter("np:pl", PL, "flagged", "Pipelines with an idle tail", "Billed outside updates")]),
        (9, [table("np:stale_table", S, [("job", "Job", "text"), ("workspace", "Workspace", "text"),
                                         ("last_run", "Last run", "text"), ("created", "Created", "text"),
                                         ("total_usd", "$", "money"), ("status", "Status", "text")],
                   "Stale jobs, worst first")]),
        (9, [table("np:ap_table", A, [("job", "Job", "text"), ("cluster", "Cluster", "text"),
                                      ("workspace", "Workspace", "text"), ("task_runs", "Task runs", "int"),
                                      ("total_usd", "$", "money"), ("jobs_sharing_cluster", "Jobs on it", "int"),
                                      ("status", "Status", "text")],
                   "Jobs on all-purpose clusters, worst first")]),
        (8, [table("np:pl_table", PL, [("pipeline", "Pipeline", "text"), ("workspace", "Workspace", "text"),
                                       ("updates", "Updates", "int"), ("idle_dbus", "Idle DBUs", "num"),
                                       ("idle_pct", "Idle %", "pct"), ("total_usd", "$", "money"),
                                       ("status", "Status", "text")],
                   "Pipelines, most idle DBUs first")]),
    ])
    filters = place([(1, [(time_window("global", DATASETS), 4)]),
                     (1, [(field_filter("global:ws", "workspace", "Workspace", DATASETS), 4)])])
    return bdd.tag_dashboard_queries({
        "datasets": [dataset_json(d) for d in DATASETS],
        "pages": [_page("priced", "Possible waste", priced), _page("warehouses", "Warehouses", warehouses),
                  _page("whatif", "Auto-stop what-if", whatif), _page("clusters", "Clusters & pools", clusters),
                  _page("failures", "Failed runs & queries", failures), _page("serving", "Serving endpoints", serving),
                  _page("unpriced", "Flagged, not priced", unpriced),
                  _page("filters", "Global Filters", filters, "PAGE_TYPE_GLOBAL_FILTERS")],
    }, "Waste & savings")


def global_filters(datasets: list[Dataset], prefix: str) -> dict:
    """Time Window on the datasets that read it, Workspace on all."""
    layout = place([(1, [(time_window("global", [d for d in datasets if d.window]), 4)]),
                    (1, [(field_filter("global:ws", "workspace", "Workspace", datasets), 4)])])
    return _page("filters", "Global Filters", layout, "PAGE_TYPE_GLOBAL_FILTERS", prefix)


def dashboards() -> list[tuple[str, str, object, list[Dataset]]]:
    """(file stem, title, builder, datasets) for each dashboard."""
    from tools import direct_governance as gov
    from tools import direct_jobs_health as jobs
    from tools import direct_data_usage as wdata

    return [("waste_savings", "Waste & savings", waste_dashboard, DATASETS),
            ("jobs_health", jobs.TITLE, jobs.dashboard, jobs.DATASETS),
            ("workspace_usage", gov.USAGE_TITLE, gov.usage_dashboard, gov.USAGE_DATASETS),
            ("broad_grants", gov.GRANTS_TITLE, gov.grants_dashboard, gov.GRANTS_DATASETS),
            ("admin_changes", gov.ADMIN_TITLE, gov.admin_dashboard, gov.ADMIN_DATASETS),
            ("data_usage", wdata.TITLE, wdata.dashboard, wdata.DATASETS)]


def build() -> dict[str, str]:
    from tools import direct_dashboard_doc as doc
    from tools import direct_guides as guides

    out = {}
    for stem, title, make, datasets in dashboards():
        dash = make()
        out[f"{stem}.lvdash.json"] = json.dumps(dash, indent=2, ensure_ascii=False) + "\n"
        out[f"{stem}.html"] = doc.build(dash, datasets, title, stem)
        if stem in guides.SHORT:
            out[f"{stem}_summary.html"] = doc.build_short(dash, datasets, title, stem)
    from tools import direct_data_usage as wdata
    out["data_usage_sizes.py"] = wdata.collect_sizes_notebook()
    return out


def main() -> int:
    from tools import direct_governance as gov
    from tools import direct_data_usage as wdata

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default=str(OUT_DIR), help="output directory (default %(default)s)")
    parser.add_argument("--query-tags", help="k=v,k=v in place of the settings' query_tags (letters, digits, _)")
    parser.add_argument("--workspace-class", action="append", default=[], metavar="CLASS=PATTERN",
                        help="Workspace usage: a workspace class and a LIKE pattern on the lower-case workspace name")
    parser.add_argument("--sizes-schema", help="Data usage: the catalog.schema its sizes job writes to")
    args = parser.parse_args()
    if args.query_tags:
        bdd.QUERY_TAGS_OVERRIDE = dict(p.split("=", 1) for p in args.query_tags.split(","))
    gov.WORKSPACE_CLASSES = [tuple(c.split("=", 1)) for c in args.workspace_class]
    wdata.configure(args.sizes_schema)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, content in build().items():
        (out / name).write_text(content, encoding="utf-8", newline="\n")
        print(out / name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
