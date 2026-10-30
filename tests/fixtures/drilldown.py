"""tests/fixtures/drilldown.py -- batch D (T-07), ported from
the earlier subaudit suite's tests/{test_long_running_runs,
test_task_cluster_utilization,test_task_statement_breakdown}.py's own build(con) functions.

Fills the 7 source tables the three drill-down queries read:
  lakeflow__job_run_timeline, lakeflow__job_task_run_timeline, lakeflow__jobs,
  compute__clusters, compute__node_timeline, access__workspaces_latest, query__history

Per DEC-15, this batch keeps its own workspaces 9001/9002 (every other builder shares
1111/2222/3333) and every literal id it writes carries the fixed prefix `dd_`. Because all three
subaudit source files independently reuse the same small vocabulary of ids ("J1", "R1", "T_a", ...)
for THEIR OWN, mutually incompatible scenarios, and this task's build(con) runs all three against
ONE shared connection (so the parquet written per table is the union of everything all three
queries need, per PLAN.md 7.2's "D drill-down" batch row), each ported function additionally
carries its own sub-prefix on top of the batch prefix -- dd_lrr_ (lakeflow_long_running_runs),
dd_tcu_ (task_cluster_utilization), dd_qts_ (query_task_statement_breakdown) -- so the three
scenarios never collide on the shared job_run_timeline / job_task_run_timeline / clusters tables.
This is why DEC-15's own text says assertions must filter on the builder's own ids, never on
workspace alone: every one of the three finding models reads the FULL shared table, so (e.g.)
f_lakeflow_long_running_runs reports long runs from all three scenarios at once, and each test
file filters the built rows down to its own dd_<sub>_ ids before applying its assertions.

Each ported build_<query_id>(con) function is otherwise the matching subaudit file's build(con)
verbatim (same CREATE-TABLE-shaped scenario, same run()/task()/telemetry()/cluster()/stmt() helper
structure, same numbers) with exactly these changes:
  - NOW -> AS_OF (imported from base.py)
  - every literal id namespaced dd_<sub>_ per the above
  - the table's own CREATE TABLE statement is dropped (the shared, widened table already exists --
    see _widen_tables / build() below) and every INSERT names its columns explicitly, because the
    widened table has far more columns, in a different order, than the subaudit's own minimal
    CREATE TABLE
  - the table name itself is qualified "<schema>__<table>" (job_run_timeline ->
    lakeflow__job_run_timeline, etc.) to match tests/fixtures/ddl.py's / build_fixtures.py's
    naming
  - the workspaces_latest row (identical in all three subaudit files) is written once, by the
    module's own build(con), not by each ported function

A fourth function, build(con) -- the one build_fixtures.py auto-discovers per DEC-17 -- widens the
7 tables in place (CREATE OR REPLACE TABLE using tests/fixtures/ddl.py's own DDL for each, which a
column-by-column check below confirms already supersets every column any of the three ported
functions needs -- see REQUIRED_COLUMNS), writes the two workspaces_latest rows, then calls the
three ported functions in sequence.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import duckdb

from base import AS_OF, write_parquet  # noqa: F401 (write_parquet re-exported for build_fixtures.py callers)
from ddl import DDL

H = timedelta(hours=1)
M = timedelta(minutes=1)

# DEC-15: this batch's own workspaces (every other builder shares 1111/2222/3333).
WS = "9001"
WS2 = "9002"


def _ts(t: datetime) -> str:
    return "TIMESTAMP '" + t.strftime("%Y-%m-%d %H:%M:%S") + "'"


def _s(v) -> str:
    return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"


def _n(v) -> str:
    return "NULL" if v is None else str(v)


# ---------------------------------------------------------------------------------------------
# Step 2: widen the 7 shared tables to tests/fixtures/ddl.py's own (wider) DDL, verifying it is
# never missing a column one of the three ported functions below needs. `None` marks a complex
# (STRUCT) column that is checked for presence only, not type text.
# ---------------------------------------------------------------------------------------------
REQUIRED_COLUMNS: dict[str, dict[str, str | None]] = {
    "lakeflow__job_run_timeline": {
        "workspace_id": "VARCHAR", "job_id": "VARCHAR", "run_id": "VARCHAR",
        "period_start_time": "TIMESTAMP", "period_end_time": "TIMESTAMP",
        "result_state": "VARCHAR", "termination_code": "VARCHAR", "run_type": "VARCHAR",
        "compute_ids": "VARCHAR[]", "run_duration_seconds": "BIGINT",
        "setup_duration_seconds": "BIGINT", "queue_duration_seconds": "BIGINT",
        "execution_duration_seconds": "BIGINT", "cleanup_duration_seconds": "BIGINT",
    },
    "lakeflow__job_task_run_timeline": {
        "workspace_id": "VARCHAR", "job_id": "VARCHAR", "run_id": "VARCHAR",
        "job_run_id": "VARCHAR", "parent_run_id": "VARCHAR", "task_key": "VARCHAR",
        "period_start_time": "TIMESTAMP", "period_end_time": "TIMESTAMP",
        "result_state": "VARCHAR", "compute_ids": "VARCHAR[]", "compute": None,
        "setup_duration_seconds": "BIGINT", "cleanup_duration_seconds": "BIGINT",
        "execution_duration_seconds": "BIGINT",
    },
    "lakeflow__jobs": {
        "workspace_id": "VARCHAR", "job_id": "VARCHAR", "name": "VARCHAR",
        "change_time": "TIMESTAMP", "delete_time": "TIMESTAMP",
    },
    "compute__clusters": {
        "workspace_id": "VARCHAR", "cluster_id": "VARCHAR", "cluster_name": "VARCHAR",
        "cluster_source": "VARCHAR", "driver_node_type": "VARCHAR", "worker_node_type": "VARCHAR",
        "worker_count": "BIGINT", "min_autoscale_workers": "BIGINT",
        "max_autoscale_workers": "BIGINT", "dbr_version": "VARCHAR", "change_time": "TIMESTAMP",
    },
    "compute__node_timeline": {
        "workspace_id": "VARCHAR", "cluster_id": "VARCHAR", "instance_id": "VARCHAR",
        "start_time": "TIMESTAMP", "end_time": "TIMESTAMP", "driver": "BOOLEAN",
        "cpu_user_percent": "DOUBLE", "cpu_system_percent": "DOUBLE", "cpu_wait_percent": "DOUBLE",
        "mem_used_percent": "DOUBLE", "mem_swap_percent": "DOUBLE",
        "network_sent_bytes": "BIGINT", "network_received_bytes": "BIGINT", "node_type": "VARCHAR",
    },
    "access__workspaces_latest": {
        "workspace_id": "VARCHAR", "workspace_name": "VARCHAR", "workspace_url": "VARCHAR",
    },
    "query__history": {
        "workspace_id": "VARCHAR", "statement_id": "VARCHAR", "statement_type": "VARCHAR",
        "execution_status": "VARCHAR", "statement_text": "VARCHAR", "from_result_cache": "BOOLEAN",
        "compute": None, "query_source": None, "start_time": "TIMESTAMP", "end_time": "TIMESTAMP",
        "total_duration_ms": "BIGINT", "execution_duration_ms": "BIGINT",
        "compilation_duration_ms": "BIGINT", "waiting_for_compute_duration_ms": "BIGINT",
        "waiting_at_capacity_duration_ms": "BIGINT", "total_task_duration_ms": "BIGINT",
        "read_bytes": "BIGINT", "spilled_local_bytes": "BIGINT", "shuffle_read_bytes": "BIGINT",
    },
}


def _widen_tables(con: duckdb.DuckDBPyConnection) -> None:
    """CREATE OR REPLACE each of the 7 tables using tests/fixtures/ddl.py's own DDL (already the
    real, full-column-set DDL derived from the catalog dump -- a column-by-column comparison made
    while writing this module found it already a superset of everything the three ported
    subaudit builders below need, for every one of the 7 tables). Then verify that superset holds
    at runtime, raising loudly and naming the exact missing/mismatched column if ddl.py is ever
    regenerated in a way that drops one -- never a silent narrowing."""
    for key, required in REQUIRED_COLUMNS.items():
        stmt = DDL[key]
        if not stmt.startswith("CREATE TABLE "):
            raise RuntimeError(f"drilldown._widen_tables: unexpected ddl.py DDL shape for {key!r}")
        con.execute("CREATE OR REPLACE TABLE " + stmt[len("CREATE TABLE ") :])
        info = con.execute(f'PRAGMA table_info("{key}")').fetchall()
        actual = {row[1]: row[2] for row in info}
        missing = [c for c in required if c not in actual]
        if missing:
            raise RuntimeError(
                f"drilldown._widen_tables: {key} is missing required column(s) {missing} after "
                "CREATE OR REPLACE from tests/fixtures/ddl.py's own DDL -- ddl.py no longer "
                "supersets the columns this fixture's ported builders need"
            )
        mismatched = [
            (c, exp, actual[c]) for c, exp in required.items() if exp is not None and exp not in actual[c]
        ]
        if mismatched:
            raise RuntimeError(
                f"drilldown._widen_tables: {key} has mismatched column type(s) {mismatched} "
                "(expected type text not found in ddl.py's actual PRAGMA table_info type)"
            )


# ---------------------------------------------------------------------------------------------
# Ported from test_long_running_runs.py's build(con). dd_lrr_ sub-prefix.
# ---------------------------------------------------------------------------------------------
def build_long_running_runs(con: duckdb.DuckDBPyConnection) -> None:
    def run(job, rid, start, hours, state, legacy=None):
        """3 period rows; the end row carries result_state and the durations: 0 for multi-task,
        or the legacy tuple (setup, queue, execution, cleanup) with run_duration = hours for a
        single-task job."""
        end = start + hours * H
        mid = start + hours * H / 2
        rows = [(start, mid, None, None), (mid, end - 5 * M, None, None)]
        if state is not None:
            rows.append((end - 5 * M, end, state, "END"))
        for a, b, st, flag in rows:
            if flag is None:
                d = "NULL, NULL, NULL, NULL, NULL"
            elif legacy is None:
                d = "0, 0, 0, 0, 0"
            else:
                d = f"{int(hours * 3600)}, {legacy[0]}, {legacy[1]}, {legacy[2]}, {legacy[3]}"
            con.execute(
                "INSERT INTO lakeflow__job_run_timeline "
                "(workspace_id, job_id, run_id, period_start_time, period_end_time, result_state, "
                "termination_code, run_duration_seconds, setup_duration_seconds, "
                "queue_duration_seconds, execution_duration_seconds, cleanup_duration_seconds) "
                f"VALUES ('{WS}','{job}','{rid}',{_ts(a)},{_ts(b)},{_s(st)},NULL,{d})"
            )

    def task(job, jrun, trun, key, start, hours, state, compute_ids, zero_length=False):
        cids = "NULL" if compute_ids is None else "[" + ",".join(f"'{c}'" for c in compute_ids) + "]"
        if zero_length:
            rows = [(start, start, state, None)]
        else:
            end = start + hours * H
            mid = start + hours * H / 2
            rows = [(start, mid, None, None), (mid, end - 5 * M, None, None)]
            if state is not None:
                rows.append((end - 5 * M, end, state, int(hours * 3600)))
        for a, b, st, dur in rows:
            con.execute(
                "INSERT INTO lakeflow__job_task_run_timeline "
                "(workspace_id, job_id, run_id, job_run_id, task_key, period_start_time, "
                "period_end_time, result_state, compute_ids, execution_duration_seconds) "
                f"VALUES ('{WS}','{job}','{trun}','{jrun}','{key}',{_ts(a)},{_ts(b)},{_s(st)},{cids},{_n(dur)})"
            )

    # R1: multi-task job, 5 h wall clock, durations all 0 (doc) -> must still be reported with 5 h
    r1 = AS_OF - 30 * H
    run("dd_lrr_J1", "dd_lrr_R1", r1, 5, "SUCCEEDED")
    task("dd_lrr_J1", "dd_lrr_R1", "dd_lrr_T_a", "extract", r1, 1, "SUCCEEDED", None)
    task("dd_lrr_J1", "dd_lrr_R1", "dd_lrr_T_b", "transform", r1 + 1 * H, 4, "SUCCEEDED", ["dd_lrr_c1"])
    task("dd_lrr_J1", "dd_lrr_R1", "dd_lrr_T_c", "load", r1 + 4 * H, 1, "SUCCEEDED", ["dd_lrr_wh1"])
    task("dd_lrr_J1", "dd_lrr_R1", "dd_lrr_T_s", "notify", r1 + 5 * H, 0, "SKIPPED", None, zero_length=True)
    # R2: same job, multi-task, 1 h -> below the bound
    run("dd_lrr_J1", "dd_lrr_R2", AS_OF - 20 * H, 1, "SUCCEEDED")
    task("dd_lrr_J1", "dd_lrr_R2", "dd_lrr_T_r2", "extract", AS_OF - 20 * H, 1, "SUCCEEDED", None)
    # R3: multi-task, 3 h, finished -> in the job's p50 with R1 and R2
    run("dd_lrr_J1", "dd_lrr_R3", AS_OF - 50 * H, 3, "SUCCEEDED")
    task("dd_lrr_J1", "dd_lrr_R3", "dd_lrr_T_r3", "extract", AS_OF - 50 * H, 3, "SUCCEEDED", None)
    # R4: legacy single-task job, 4 h reported, with 1 h setup + 0.5 h queue -> wait_share 37.5
    run("dd_lrr_J2", "dd_lrr_R4", AS_OF - 40 * H, 4, "SUCCEEDED", legacy=(3600, 1800, 9000, 0))
    task("dd_lrr_J2", "dd_lrr_R4", "dd_lrr_T_r4", "only", AS_OF - 40 * H, 4, "SUCCEEDED", ["dd_lrr_c2"])
    # R5: multi-task, IN FLIGHT 3 h (no end row), one task in flight
    run("dd_lrr_J3", "dd_lrr_R5", AS_OF - 3 * H, 3, None)
    task("dd_lrr_J3", "dd_lrr_R5", "dd_lrr_T_r5", "long", AS_OF - 3 * H, 3, None, ["dd_lrr_c3"])

    con.execute(
        "INSERT INTO lakeflow__jobs (workspace_id, job_id, name, change_time) "
        f"VALUES ('{WS}','dd_lrr_J1','etl',{_ts(AS_OF - 400 * H)})"
    )
    con.execute(
        "INSERT INTO lakeflow__jobs (workspace_id, job_id, name, change_time) "
        f"VALUES ('{WS}','dd_lrr_J1','etl_v2',{_ts(AS_OF - 100 * H)})"
    )
    con.execute(
        "INSERT INTO lakeflow__jobs (workspace_id, job_id, name, change_time) "
        f"VALUES ('{WS}','dd_lrr_J2','legacy_job',{_ts(AS_OF - 100 * H)})"
    )


# ---------------------------------------------------------------------------------------------
# Ported from test_task_cluster_utilization.py's build(con). dd_tcu_ sub-prefix.
# ---------------------------------------------------------------------------------------------
def build_task_cluster_utilization(con: duckdb.DuckDBPyConnection) -> None:
    def run(job, rid, start, hours, state):
        end = start + hours * H
        mid = start + hours * H / 2
        rows = [(start, mid, None, None), (mid, end - 5 * M, None, None)]
        if state is not None:
            rows.append((end - 5 * M, end, state, 0))  # doc: 0 for every multi-task job
        for a, b, st, dur in rows:
            con.execute(
                "INSERT INTO lakeflow__job_run_timeline "
                "(workspace_id, job_id, run_id, period_start_time, period_end_time, result_state, "
                "run_duration_seconds) "
                f"VALUES ('{WS}','{job}','{rid}',{_ts(a)},{_ts(b)},{_s(st)},{_n(dur)})"
            )

    def task(job, jrun, trun, key, start, hours, state, compute_ids):
        end = start + hours * H
        mid = start + hours * H / 2
        cids = "NULL" if compute_ids is None else "[" + ",".join(f"'{c}'" for c in compute_ids) + "]"
        rows = [(start, mid, None, None), (mid, end - 5 * M, None, None)]
        if state is not None:
            rows.append((end - 5 * M, end, state, int(hours * 3600)))
        for a, b, st, dur in rows:
            con.execute(
                "INSERT INTO lakeflow__job_task_run_timeline "
                "(workspace_id, job_id, run_id, job_run_id, task_key, period_start_time, "
                "period_end_time, result_state, compute_ids, execution_duration_seconds) "
                f"VALUES ('{WS}','{job}','{trun}','{jrun}','{key}',{_ts(a)},{_ts(b)},{_s(st)},{cids},{_n(dur)})"
            )

    def telemetry(cluster, nodes, start, minutes):
        """nodes: list of (instance_id, driver, cpu, wait, mem, swap). One row per node per minute."""
        rows = []
        for i in range(minutes):
            a = start + i * M
            for inst, drv, cpu, wait, mem, swap in nodes:
                rows.append((WS, cluster, inst, a, a + M, drv, cpu * 0.7, cpu * 0.3, wait, mem, swap, 1000, 2000, "m5.xlarge"))
        con.executemany(
            "INSERT INTO compute__node_timeline "
            "(workspace_id, cluster_id, instance_id, start_time, end_time, driver, "
            "cpu_user_percent, cpu_system_percent, cpu_wait_percent, mem_used_percent, "
            "mem_swap_percent, network_sent_bytes, network_received_bytes, node_type) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )

    def cluster(cid, name, workers, change):
        con.execute(
            "INSERT INTO compute__clusters "
            "(workspace_id, cluster_id, cluster_name, cluster_source, driver_node_type, "
            "worker_node_type, worker_count, min_autoscale_workers, max_autoscale_workers, "
            "dbr_version, change_time) "
            f"VALUES ('{WS}','{cid}','{name}','JOB','m5.xlarge','m5.xlarge',{workers},NULL,NULL,'15.4.x',{_ts(change)})"
        )

    r1 = AS_OF - 30 * H
    run("dd_tcu_J1", "dd_tcu_R1", r1, 5, "SUCCEEDED")
    # waiting: workers ~5 % CPU for 3 h
    task("dd_tcu_J1", "dd_tcu_R1", "dd_tcu_T_wait", "wait", r1, 3, "SUCCEEDED", ["dd_tcu_c-wait"])
    telemetry("dd_tcu_c-wait", [("d", True, 10, 0, 30, 0), ("w1", False, 5, 0, 30, 0), ("w2", False, 5, 0, 30, 0)], r1, 180)
    # busy: workers 90 % for 2 h, plus 60 min of 0 % telemetry BEFORE the task that must be excluded
    task("dd_tcu_J1", "dd_tcu_R1", "dd_tcu_T_busy", "busy", r1 + 1 * H, 2, "SUCCEEDED", ["dd_tcu_c-busy"])
    telemetry("dd_tcu_c-busy", [("d", True, 20, 0, 40, 0), ("w1", False, 0, 0, 20, 0), ("w2", False, 0, 0, 20, 0)], r1, 60)
    telemetry("dd_tcu_c-busy", [("d", True, 20, 0, 40, 0), ("w1", False, 90, 0, 50, 0), ("w2", False, 90, 0, 50, 0)], r1 + 1 * H, 120)
    # skew: one hot worker, one cold
    task("dd_tcu_J1", "dd_tcu_R1", "dd_tcu_T_skew", "skew", r1, 2, "SUCCEEDED", ["dd_tcu_c-skew"])
    telemetry("dd_tcu_c-skew", [("d", True, 10, 0, 30, 0), ("w1", False, 95, 0, 60, 0), ("w2", False, 10, 0, 30, 0)], r1, 120)
    # memory pressure
    task("dd_tcu_J1", "dd_tcu_R1", "dd_tcu_T_mem", "mem", r1, 2, "SUCCEEDED", ["dd_tcu_c-mem"])
    telemetry("dd_tcu_c-mem", [("d", True, 10, 0, 30, 0), ("w1", False, 60, 0, 92, 0), ("w2", False, 60, 0, 92, 0)], r1, 120)
    # io wait
    task("dd_tcu_J1", "dd_tcu_R1", "dd_tcu_T_io", "io", r1, 2, "SUCCEEDED", ["dd_tcu_c-io"])
    telemetry("dd_tcu_c-io", [("d", True, 10, 0, 30, 0), ("w1", False, 30, 35, 40, 0), ("w2", False, 30, 35, 40, 0)], r1, 120)
    # driver bound
    task("dd_tcu_J1", "dd_tcu_R1", "dd_tcu_T_drv", "drv", r1, 2, "SUCCEEDED", ["dd_tcu_c-drv"])
    telemetry("dd_tcu_c-drv", [("d", True, 95, 0, 70, 0), ("w1", False, 5, 0, 30, 0), ("w2", False, 5, 0, 30, 0)], r1, 120)
    # single node, busy driver
    task("dd_tcu_J1", "dd_tcu_R1", "dd_tcu_T_single", "single", r1, 2, "SUCCEEDED", ["dd_tcu_c-single"])
    telemetry("dd_tcu_c-single", [("d", True, 90, 0, 50, 0)], r1, 120)
    # short task: 20 min x 2 workers = 40 worker-minutes < :min_slices 60
    task("dd_tcu_J1", "dd_tcu_R1", "dd_tcu_T_short", "short", r1, 20 / 60, "SUCCEEDED", ["dd_tcu_c-short"])
    telemetry("dd_tcu_c-short", [("d", True, 50, 0, 50, 0), ("w1", False, 50, 0, 50, 0), ("w2", False, 50, 0, 50, 0)], r1, 20)
    # never ran: SKIPPED, zero-length row (doc), on a cluster that does have telemetry
    con.execute(
        "INSERT INTO lakeflow__job_task_run_timeline "
        "(workspace_id, job_id, run_id, job_run_id, task_key, period_start_time, period_end_time, "
        "result_state, compute_ids, execution_duration_seconds) "
        f"VALUES ('{WS}','dd_tcu_J1','dd_tcu_T_skip','dd_tcu_R1','skip',{_ts(r1 + 2 * H)},{_ts(r1 + 2 * H)},"
        "'SKIPPED',['dd_tcu_c-busy'],NULL)"
    )
    # serverless: no compute ids at all
    task("dd_tcu_J1", "dd_tcu_R1", "dd_tcu_T_sls", "sls", r1, 1, "SUCCEEDED", None)
    # warehouse id: recorded, but nothing in node_timeline
    task("dd_tcu_J1", "dd_tcu_R1", "dd_tcu_T_wh", "wh", r1, 1, "SUCCEEDED", ["dd_tcu_wh1"])

    # another job's SHORT run sharing c-busy during T_busy (must count as overlapping, must not be a result row)
    r9 = r1 + 1.5 * H
    run("dd_tcu_J9", "dd_tcu_R9", r9, 0.5, "SUCCEEDED")
    task("dd_tcu_J9", "dd_tcu_R9", "dd_tcu_T_other", "other", r9, 0.5, "SUCCEEDED", ["dd_tcu_c-busy"])

    # R2: below the bound -> excluded
    r2 = AS_OF - 20 * H
    run("dd_tcu_J1", "dd_tcu_R2", r2, 1, "SUCCEEDED")
    task("dd_tcu_J1", "dd_tcu_R2", "dd_tcu_T_r2", "wait", r2, 1, "SUCCEEDED", ["dd_tcu_c-wait"])

    # R3: submit run (no jobs row), IN FLIGHT 3 h, moderate CPU -> MIXED
    r3 = AS_OF - 3 * H
    run("dd_tcu_J2", "dd_tcu_R3", r3, 3, None)
    task("dd_tcu_J2", "dd_tcu_R3", "dd_tcu_T_live", "live", r3, 3, None, ["dd_tcu_c-live"])
    telemetry("dd_tcu_c-live", [("d", True, 10, 0, 30, 0), ("w1", False, 50, 0, 50, 0), ("w2", False, 50, 0, 50, 0)], r3, 175)

    # R_old: 100 days ago, 4 h, with telemetry -> must be dropped by the 90-day cap / window
    rold = AS_OF - 100 * 24 * H
    run("dd_tcu_J1", "dd_tcu_R_old", rold, 4, "SUCCEEDED")
    task("dd_tcu_J1", "dd_tcu_R_old", "dd_tcu_T_old", "wait", rold, 4, "SUCCEEDED", ["dd_tcu_c-wait"])
    telemetry("dd_tcu_c-wait", [("w1", False, 5, 0, 30, 0)], rold, 240)

    # SCD2 clusters (c-busy resized 2 -> 4) and jobs (J1 renamed)
    for cid in ["dd_tcu_c-wait", "dd_tcu_c-skew", "dd_tcu_c-mem", "dd_tcu_c-io", "dd_tcu_c-drv", "dd_tcu_c-single", "dd_tcu_c-short", "dd_tcu_c-live"]:
        cluster(cid, f"name-{cid}", 2, AS_OF - 200 * H)
    cluster("dd_tcu_c-busy", "busy-old", 2, AS_OF - 300 * H)
    cluster("dd_tcu_c-busy", "busy-new", 4, AS_OF - 200 * H)
    con.execute(
        "INSERT INTO lakeflow__jobs (workspace_id, job_id, name, change_time) "
        f"VALUES ('{WS}','dd_tcu_J1','etl_daily',{_ts(AS_OF - 400 * H)})"
    )
    con.execute(
        "INSERT INTO lakeflow__jobs (workspace_id, job_id, name, change_time) "
        f"VALUES ('{WS}','dd_tcu_J1','etl_daily_v2',{_ts(AS_OF - 100 * H)})"
    )


# ---------------------------------------------------------------------------------------------
# Ported from test_task_statement_breakdown.py's build(con). dd_qts_ sub-prefix.
# ---------------------------------------------------------------------------------------------
def build_query_task_statement_breakdown(con: duckdb.DuckDBPyConnection) -> None:
    def run(job, rid, start, hours, state, legacy=False):
        """3 period rows; end row carries result_state; run_duration_seconds 0 unless legacy single-task."""
        end = start + hours * H
        mid = start + hours * H / 2
        rows = [(start, mid, None, None), (mid, end - 5 * M, None, None)]
        if state is not None:
            rows.append((end - 5 * M, end, state, int(hours * 3600) if legacy else 0))
        for a, b, st, dur in rows:
            con.execute(
                "INSERT INTO lakeflow__job_run_timeline "
                "(workspace_id, job_id, run_id, period_start_time, period_end_time, result_state, "
                "termination_code, run_type, compute_ids, run_duration_seconds, "
                "setup_duration_seconds, queue_duration_seconds, execution_duration_seconds, "
                "cleanup_duration_seconds) "
                f"VALUES ('{WS}','{job}','{rid}',{_ts(a)},{_ts(b)},{_s(st)},NULL,'JOB_RUN',NULL,{_n(dur)},NULL,NULL,NULL,NULL)"
            )

    def task(job, jrun, trun, key, start, hours, state, compute_ids, compute, zero_length=False):
        cids = "NULL" if compute_ids is None else "[" + ",".join(f"'{c}'" for c in compute_ids) + "]"
        comp = (
            "NULL"
            if compute is None
            else "["
            + ",".join("{'type': " + _s(t) + ", 'cluster_id': " + _s(c) + ", 'warehouse_id': " + _s(w) + "}" for t, c, w in compute)
            + "]"
        )
        if zero_length:
            rows = [(start, start, state, None)]
        else:
            end = start + hours * H
            mid = start + hours * H / 2
            rows = [(start, mid, None, None), (mid, end - 5 * M, None, None)]
            if state is not None:
                rows.append((end - 5 * M, end, state, int(hours * 3600)))
        for a, b, st, dur in rows:
            con.execute(
                "INSERT INTO lakeflow__job_task_run_timeline "
                "(workspace_id, job_id, run_id, job_run_id, parent_run_id, task_key, "
                "period_start_time, period_end_time, result_state, compute_ids, compute, "
                "setup_duration_seconds, cleanup_duration_seconds, execution_duration_seconds) "
                f"VALUES ('{WS}','{job}','{trun}','{jrun}','{jrun}','{key}',{_ts(a)},{_ts(b)},{_s(st)},{cids},{comp},NULL,NULL,{_n(dur)})"
            )

    def stmt(sid, job, jrun, trun, text, start, total_ms, status="FINISHED", ctype="SERVERLESS_COMPUTE"):
        exec_ms = int(total_ms * 0.8)
        src = "{'job_info': {'job_id': " + _s(job) + ", 'job_run_id': " + _s(jrun) + ", 'job_task_run_id': " + _s(trun) + "}, 'notebook_id': CAST(NULL AS VARCHAR)}"
        comp = "{'type': " + _s(ctype) + ", 'cluster_id': CAST(NULL AS VARCHAR), 'warehouse_id': CAST(NULL AS VARCHAR)}"
        con.execute(
            "INSERT INTO query__history "
            "(workspace_id, statement_id, statement_type, execution_status, statement_text, "
            "from_result_cache, compute, query_source, start_time, end_time, total_duration_ms, "
            "execution_duration_ms, compilation_duration_ms, waiting_for_compute_duration_ms, "
            "waiting_at_capacity_duration_ms, total_task_duration_ms, read_bytes, "
            "spilled_local_bytes, shuffle_read_bytes) "
            f"VALUES ('{WS}','{sid}','SELECT','{status}',{_s(text)},false,{comp},{src},"
            f"{_ts(start)},{_ts(start + total_ms * timedelta(milliseconds=1))},{total_ms},{exec_ms},"
            f"{int(total_ms * 0.05)},{int(total_ms * 0.1)},0,{total_ms * 4},1000,0,0)"
        )

    # ---- R1: job J1, multi-task (run_duration_seconds = 0), finished, 5 h wall clock -> examined ----
    r1 = AS_OF - 30 * H
    run("dd_qts_J1", "dd_qts_R1", r1, 5, "SUCCEEDED")
    task("dd_qts_J1", "dd_qts_R1", "dd_qts_T1", "extract", r1, 1, "FAILED", None, None)  # serverless, failed attempt
    task("dd_qts_J1", "dd_qts_R1", "dd_qts_T1b", "extract", r1 + 1 * H, 3, "SUCCEEDED", None, None)  # serverless retry
    task("dd_qts_J1", "dd_qts_R1", "dd_qts_T2", "transform", r1 + 1 * H, 4, "SUCCEEDED", ["dd_qts_c1"], [("CLASSIC", "dd_qts_c1", None)])  # classic cluster
    task("dd_qts_J1", "dd_qts_R1", "dd_qts_T3", "load", r1 + 4 * H, 1, "SUCCEEDED", ["dd_qts_wh1"], [("SQL_WAREHOUSE", None, "dd_qts_wh1")])  # warehouse, no SQL captured
    task("dd_qts_J1", "dd_qts_R1", "dd_qts_T8", "notify", r1 + 5 * H, 0, "SKIPPED", None, None, zero_length=True)  # never ran
    # statements in T1b (task_s = 3 h = 10800 s), in the DOC shape: job_run_id NULL, job_task_run_id set
    stmt("dd_qts_s-b1", "dd_qts_J1", None, "dd_qts_T1b", "SELECT * FROM big WHERE k = 'hello'", r1 + 1 * H, int(2.4 * 3600 * 1000))  # shape B 80 %
    for i in range(3):  # shape A 3 x 30 min = 50 %
        stmt(f"dd_qts_s-a{i}", "dd_qts_J1", None, "dd_qts_T1b", "INSERT INTO t SELECT * FROM src WHERE owner = 'secret@example.com'", r1 + 1 * H + i * 10 * M, 30 * 60 * 1000)
    for i, b in enumerate([17, 18, 19]):  # numeric loop: ONE shape, 3 x 2 min
        stmt(f"dd_qts_s-l{i}", "dd_qts_J1", "dd_qts_R1", "dd_qts_T1b", f"DELETE FROM t WHERE batch_id = {b} AND id IN (1, 2, 3)", r1 + 90 * M + i * M, 2 * 60 * 1000)
    stmt("dd_qts_s-c1", "dd_qts_J1", "dd_qts_R1", "dd_qts_T1b", "SELECT count(*) FROM t", r1 + 2 * H, 5 * 60 * 1000)  # shape C ~2.8 %
    stmt("dd_qts_s-d1", "dd_qts_J1", "dd_qts_R1", "dd_qts_T1b", "OPTIMIZE t", r1 + 2 * H, 60 * 1000, status="FAILED")  # shape D failed
    # statement in the failed first attempt T1
    stmt("dd_qts_s-t1", "dd_qts_J1", "dd_qts_R1", "dd_qts_T1", "SELECT 1", r1, 60 * 1000)
    # unattributed: run matches, task run id NULL (1 h against a 5 h run = 20 %)
    stmt("dd_qts_s-u1", "dd_qts_J1", "dd_qts_R1", None, "MERGE INTO tgt USING src ON tgt.id = src.id", r1 + 2 * H, 3600 * 1000)
    # unattributed: run matches, task run id names a task the timeline does not know
    stmt("dd_qts_s-g1", "dd_qts_J1", "dd_qts_R1", "dd_qts_T-ghost", "SELECT ghost FROM t", r1 + 2 * H, 30 * 60 * 1000)
    # a statement whose run AND task are unknown -> must be dropped
    stmt("dd_qts_s-x1", "dd_qts_J1", "dd_qts_R_unknown", "dd_qts_T-unknown", "SELECT 2", r1, 3600 * 1000)

    # ---- R2: job J1, finished, 1 h -> below the bound, excluded even though it has statements ----
    r2 = AS_OF - 20 * H
    run("dd_qts_J1", "dd_qts_R2", r2, 1, "SUCCEEDED")
    task("dd_qts_J1", "dd_qts_R2", "dd_qts_T4", "extract", r2, 1, "SUCCEEDED", None, None)
    stmt("dd_qts_s-r2", "dd_qts_J1", "dd_qts_R2", "dd_qts_T4", "SELECT 3", r2, 3600 * 1000)

    # ---- R3: job J2 (submit run, no jobs row), IN FLIGHT for 3 h, task on an unknown cluster id ----
    r3 = AS_OF - 3 * H
    run("dd_qts_J2", "dd_qts_R3", r3, 3, None)
    task("dd_qts_J2", "dd_qts_R3", "dd_qts_T5", "t", r3, 3, None, ["dd_qts_c-gone"], [("CLASSIC", "dd_qts_c-gone", None)])

    # ---- R4: job J3, LEGACY single-task run (run_duration_seconds populated), 6 h, 15 shapes -> :top_n ----
    r4 = AS_OF - 60 * H
    run("dd_qts_J3", "dd_qts_R4", r4, 6, "SUCCEEDED", legacy=True)
    task("dd_qts_J3", "dd_qts_R4", "dd_qts_T6", "sqltask", r4, 6, "SUCCEEDED", None, None)
    for i in range(15):
        stmt(f"dd_qts_s-m{i}", "dd_qts_J3", "dd_qts_R4", "dd_qts_T6", f"SELECT col{i} FROM t{i}", r4 + i * M, (i + 1) * 60 * 1000)

    # ---- R5: job J4, 4 h, statement_text <REDACTED> (non-admin reader) ----
    r5 = AS_OF - 80 * H
    run("dd_qts_J4", "dd_qts_R5", r5, 4, "SUCCEEDED")
    task("dd_qts_J4", "dd_qts_R5", "dd_qts_T7", "redacted", r5, 4, "SUCCEEDED", None, None)
    stmt("dd_qts_s-q1", "dd_qts_J4", None, "dd_qts_T7", "<REDACTED>", r5, 3 * 3600 * 1000)  # 75 % -> CRITICAL, per statement
    stmt("dd_qts_s-q2", "dd_qts_J4", None, "dd_qts_T7", "<REDACTED>", r5 + 3 * H, 10 * 60 * 1000)  # ~4 % -> OK

    # ---- R6: job J3, 4 h, ended 20 minutes ago -> statements may still be landing ----
    r6 = AS_OF - 4 * H - 20 * M
    run("dd_qts_J3", "dd_qts_R6", r6, 4, "SUCCEEDED")
    task("dd_qts_J3", "dd_qts_R6", "dd_qts_T9", "sqltask", r6, 4, "SUCCEEDED", None, None)
    stmt("dd_qts_s-n1", "dd_qts_J3", None, "dd_qts_T9", "SELECT big FROM t", r6, 3 * 3600 * 1000)  # 75 % -> still CRITICAL
    stmt("dd_qts_s-n2", "dd_qts_J3", None, "dd_qts_T9", "SELECT small FROM t", r6 + 3 * H, 2 * 60 * 1000)  # ~1 % -> NOT_ASSESSED (landing)

    # SCD2 jobs: J1 renamed; J3, J4 single row; J2 absent
    con.execute(
        "INSERT INTO lakeflow__jobs (workspace_id, job_id, name, change_time, delete_time) "
        f"VALUES ('{WS}','dd_qts_J1','etl_daily',{_ts(AS_OF - 400 * H)},NULL)"
    )
    con.execute(
        "INSERT INTO lakeflow__jobs (workspace_id, job_id, name, change_time, delete_time) "
        f"VALUES ('{WS}','dd_qts_J1','etl_daily_v2',{_ts(AS_OF - 100 * H)},NULL)"
    )
    con.execute(
        "INSERT INTO lakeflow__jobs (workspace_id, job_id, name, change_time, delete_time) "
        f"VALUES ('{WS}','dd_qts_J3','reporting',{_ts(AS_OF - 100 * H)},NULL)"
    )
    con.execute(
        "INSERT INTO lakeflow__jobs (workspace_id, job_id, name, change_time, delete_time) "
        f"VALUES ('{WS}','dd_qts_J4','redacted_job',{_ts(AS_OF - 100 * H)},NULL)"
    )
    # classic clusters: c1 (two SCD2 rows); c-gone is NOT here
    con.execute(
        "INSERT INTO compute__clusters (workspace_id, cluster_id, cluster_source, change_time) "
        f"VALUES ('{WS}','dd_qts_c1','JOB',{_ts(AS_OF - 300 * H)})"
    )
    con.execute(
        "INSERT INTO compute__clusters (workspace_id, cluster_id, cluster_source, change_time) "
        f"VALUES ('{WS}','dd_qts_c1','JOB',{_ts(AS_OF - 200 * H)})"
    )


# ---------------------------------------------------------------------------------------------
# Auto-discovered by build_fixtures.py (DEC-17).
# ---------------------------------------------------------------------------------------------
def build(con: duckdb.DuckDBPyConnection) -> None:
    """Widen the 7 drill-down source tables (CREATE OR REPLACE, verified superset -- see
    _widen_tables), write the two workspaces_latest rows this batch owns (once, not once per
    ported function -- all three ported functions below share the one workspace WS), then run
    all three ported subaudit builders against the shared connection in sequence, so the parquet
    written per table (by build_fixtures.py, after this call returns) is the union of everything
    all three drill-down queries need."""
    _widen_tables(con)
    con.execute(
        "INSERT INTO access__workspaces_latest (workspace_id, workspace_name, workspace_url) "
        f"VALUES ('{WS}','dd-prod','https://dbc-123.cloud.databricks.com/')"
    )
    con.execute(
        "INSERT INTO access__workspaces_latest (workspace_id, workspace_name, workspace_url) "
        f"VALUES ('{WS2}','dd-uat','https://dbc-456.cloud.databricks.com/')"
    )
    build_long_running_runs(con)
    build_task_cluster_utilization(con)
    build_query_task_statement_breakdown(con)
