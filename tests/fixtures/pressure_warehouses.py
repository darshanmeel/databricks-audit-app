"""tests/fixtures/pressure_warehouses.py -- T-65 (DEC-65), the warehouse half of the pressure
verdicts: fills system.query.history, system.compute.warehouses and system.compute.warehouse_events
for the app-owned finding `query_warehouse_pressure` (app/queries/app/performance/).

Per DEC-15 this builder uses the shared pool workspace 1111 (billing.py owns its
access__workspaces_latest row -- this builder writes none, and no billing.usage row at all) and
prefixes every id it writes with `pr_`: warehouse ids `pr_wh_*`, statement ids `pr_st_*`, account
`pr_acct`, session ids `pr_sess_*`. The jobs half (`pressure_jobs.py`) is a separate builder in
workspace 9001; the two never write the same parquet file (build_fixtures.py names each file after
its builder).

Shared-table constraints this builder keeps (T-65 "Fixture constraints", verified 2026-09-23):
  - compute.warehouse_id is always a `pr_wh_*` id, except the serverless scenario (NULL warehouse,
    compute.type SERVERLESS_COMPUTE), whose 25 statements are 50 ms each so qh_cs_serverless1
    (1e12 ms) keeps its share of query_costly_statements' NULL-warehouse partition;
  - statement_text is `SELECT * FROM pr_...` (never a qh_ shape, never `databricks_audit`);
    executed_by is `pr_analyst@example.com` (masks to `e32e40e4 pr***` under DEC-66.3 -- no
    executed_by_user_id is set on these rows, so it falls back to the hash-derived id; the
    masked prefix never starts with `fr`);
    query_source is NULL (no job_info id at all);
  - volume: 551 statements over 19 warehouses + serverless, far under the ~850 in-window and ~400
    shape caps that query_costly_statements / query_costly_statements_grouped's LIMITs impose (the
    review-round-1 and round-2 scenarios run 100 ms or less each, so they are the rows such a LIMIT
    would cut);
  - warehouse_name is the warehouse id itself (`pr_wh_*`), so a name filter on `pr_` finds them.

AS_OF = 2026-09-21 12:00:00 (base.AS_OF); the dbt `test` target pins current_date() to
2026-09-21. The query's statement window is `start_time >= current_date() - INTERVAL :period_days
DAYS AND start_time < current_date()` (today excluded), and its warehouse_events window covers the
same days. Every scenario sits on DAY_A (2026-09-19) / DAY_B (2026-09-20) -- inside the last 3 days
and before 2026-09-21 00:00, so present at 7, 30 and 90 -- unless stated.

`total_duration_ms` = waiting_at_capacity + waiting_for_compute + compilation + execution on every
row. Scenarios marked "NULL zeros" write a zero wait / zero spill as NULL instead of 0, which the
query must read as 0 (query_queuing_waits' assumption).

Scenario -> expected row (header defaults: min_queries 20, a statement spills once it reaches
min_stmt_spill_mb 100 MB (10^6 bytes), spill time 10/25 %, worst-day spill 1/10 GB, capacity wait
5/20 %). Every spilling statement below spills at least 100 MB, except pr_wh_trivial_spill's:

  pr_wh_mem          40 stmts (NULL zeros): 4 spill 0.5 GB each on DAY_A with 60 s execution,
                     36 run 1 s -> spill_time_pct 87.0 (240000 / 276000 ms), worst day 2.0 GB ->
                     MEMORY / SCALE_UP / CRITICAL. SCD2 config: an old SMALL 1..4 row, then the
                     latest X_SMALL min 1 max 2; events max cluster_count 1 -> at_ceiling FALSE.
  pr_wh_bigspill     40 stmts of 1 s (NULL zeros); ONE spills 15 GB on DAY_A -> spill_time_pct 2.5
                     (below warn) but spilled_local_gb_max_day 15.0 -> MEMORY / SCALE_UP /
                     CRITICAL (the rare-but-huge case). No events; min_clusters 1 = max_clusters
                     1 -> at_ceiling TRUE (review round 2: no autoscale range).
  pr_wh_cap_ceiling  50 stmts, 700 ms execution each, 25 queued 600 ms at capacity ->
                     capacity_wait_pct 30.0 (15000 / 50000 ms); min_clusters 1 = max_clusters 1,
                     events max 1 -> at_ceiling TRUE -> CAPACITY / SCALE_OUT / CRITICAL.
  pr_wh_cap_lag      40 stmts, 920 ms each, 16 queued 200 ms -> capacity_wait_pct 8.0
                     (3200 / 40000 ms); max_clusters 4, events max 2 (RUNNING 1, SCALED_UP 2,
                     SCALED_DOWN 1 -> scaling_events 2) -> at_ceiling FALSE -> CAPACITY / KEEP_WARM /
                     WARN.
  pr_wh_both         40 stmts, 750 ms each; 12 spill 0.1 GB on DAY_A -> spill_time_pct 30.0
                     (9000 / 30000), worst day 1.2 GB; 20 queued 500 ms -> capacity_wait_pct 25.0 (10000 / 40000) ->
                     MEMORY_AND_CAPACITY / SCALE_UP_THEN_OUT / CRITICAL; max_clusters 2, events max
                     2 -> at_ceiling TRUE.
  pr_wh_cold         30 stmts, 500 ms execution + 100 ms compilation; 12 waited 1000 ms for compute
                     -> cold_start_wait_pct 40.0 (12000 / 30000), no spill, no queue -> NONE / NONE /
                     OK (cold start is context, not a verdict). 2 of the 30 are FAILED ->
                     finished_count 28.
  pr_wh_ok           30 stmts: 29 x 980 ms, 1 x 580 ms spilling 0.1 GB -> spill_time_pct 2.0
                     (580 / 29000) -> NONE / NONE / OK.
  pr_wh_few          5 stmts, all spilling 2 GB -> NOT_ASSESSED / too_few_statements.
  pr_wh_noconf       30 stmts, 900 ms each, 10 queued 300 ms -> capacity_wait_pct 10.0; NO
                     warehouses row, NO events -> CAPACITY / SCALE_OUT / WARN, at_ceiling NULL,
                     reason says `clusters seen unknown`.
  pr_wh_deleted      like pr_wh_ok; its latest warehouses row carries delete_time ->
                     warehouse_deleted TRUE, row present, NONE / OK.
  serverless         25 stmts pr_st_sls_*, warehouse_id NULL, type SERVERLESS_COMPUTE -> no row.
  pr_wh_win40        25 stmts 40 days before AS_OF, 500 ms each -> present at 90 only (NONE / OK).
  pr_wh_win10        25 stmts 10 days before AS_OF, 900 ms each, 10 queued 250 ms ->
                     capacity_wait_pct 10.0; max_clusters 2, events max 2 at the same instant ->
                     CAPACITY / SCALE_OUT / WARN at 30 and 90, absent at 7.

Review round 1 -- the memory WARN band, mixed bands and the window edges:

  pr_wh_memwarn      20 stmts of 100 ms; 3 spill 0.1 GB on DAY_A -> spill_time_pct 15.0 (300 /
                     2000), worst day 0.3 GB -> MEMORY / SCALE_UP / WARN (the share's WARN band).
  pr_wh_volwarn      20 stmts: 19 x 100 ms and ONE of 50 ms spilling 3 GB -> spill_time_pct 2.6
                     (below warn), worst day 3.0 GB -> MEMORY / SCALE_UP / WARN (the volume's WARN
                     band -- the WARN twin of pr_wh_bigspill).
  pr_wh_mixed        20 stmts of 100 ms; 3 spill 0.1 GB (spill share 15.0, memory WARN) and 10
                     queued 100 ms at capacity (1000 / 3000 -> capacity_wait_pct 33.3, CRITICAL) ->
                     MEMORY_AND_CAPACITY / SCALE_UP_THEN_WARM / CRITICAL (status is the worse band);
                     max_clusters 3, events max 1 -> at_ceiling FALSE, so the step after size is a
                     warm cluster (raise min_clusters), not more clusters (review round 2).
  pr_wh_edge         20 stmts of 100 ms, 4 queued 100 ms (400 / 2400 -> capacity_wait_pct 16.7,
                     WARN), plus ONE statement at 2026-09-21 01:00 (today, excluded: query_count 20
                     at every window); max_clusters 3; events: RUNNING 1 on DAY_A, SCALED_UP 3 at
                     AS_OF - 20 days (outside 7, inside 30) and SCALED_UP 3 today at 06:00 (outside
                     every window) -> at 7: max_cluster_count_seen 1, at_ceiling FALSE, CAPACITY /
                     KEEP_WARM / WARN; at 30 and 90: 3 of 3, at_ceiling TRUE, CAPACITY / SCALE_OUT /
                     WARN. Dropping either bound of the events window changes the window-7 row.

Review round 2 -- the fixed-size ceiling, both mixed-band directions and the unrounded shares
(every warehouse below has min_clusters 1; a warehouse with min_clusters >= max_clusters has no
autoscale range and reads at_ceiling TRUE from its configuration alone, so pr_wh_bigspill,
pr_wh_cap_ceiling, pr_wh_cold, pr_wh_few, pr_wh_deleted, pr_wh_win40, pr_wh_memwarn and
pr_wh_volwarn -- all 1..1 -- read at_ceiling TRUE whatever their events say):

  pr_wh_fixed        20 stmts of 100 ms, 4 queued 100 ms (400 / 2400 -> capacity_wait_pct 16.7,
                     WARN); min_clusters 1 = max_clusters 1 and ONE in-window event, STOPPED with
                     cluster_count 0 -> max_cluster_count_seen 0 but at_ceiling TRUE (no autoscale
                     range) -> CAPACITY / SCALE_OUT / WARN, never KEEP_WARM ("raise min_clusters"
                     is impossible when max_clusters is 1).
  pr_wh_memcrit_capwarn 20 stmts of 100 ms; 6 spill 0.1 GB (spill share 30.0, memory CRITICAL)
                     and 4 queued 100 ms (400 / 2400 -> capacity_wait_pct 16.7, capacity WARN);
                     max_clusters 2, no events -> MEMORY_AND_CAPACITY / SCALE_UP_THEN_OUT /
                     CRITICAL: the mirror of pr_wh_mixed (memory CRITICAL, capacity WARN); at_ceiling
                     NULL (unseen) keeps SCALE_UP_THEN_OUT, as CAPACITY keeps SCALE_OUT.
  pr_wh_nearwarn     20 stmts: ONE of 83 ms spilling 0.1 GB, 18 of 39 ms and one of 48 ms (exec
                     833 ms) -> true spill share 9.964 % (displayed 10.0, below the 10 % band);
                     one of the 39 ms statements also queued 44 ms and another compiled 10 ms
                     (total 887 ms) -> true capacity share 4.961 % (displayed 5.0, below the 5 %
                     band) -> NONE / NONE / OK: both bands are judged on the unrounded shares, and
                     the reason never prints a below-threshold share as the threshold itself.
  pr_wh_trivial_spill 20 stmts: 10 of 1000 ms each spilling 5 MB (below the 100 MB floor) and 10
                     clean ones of 100 ms, all on DAY_A. Counting any spilled byte would read
                     10000 / 11000 = 90.9 % -> CRITICAL; with the floor: spilling_query_count 0,
                     spill_time_pct 0.0, spilled_local_gb_sum 0.05 (every byte still summed) ->
                     NONE / NONE / OK; 1..1 -> at_ceiling TRUE.

Stdlib + duckdb only.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from base import AS_OF, write_parquet  # noqa: F401 (write_parquet re-exported for callers)

WS = "1111"
ACCOUNT_ID = "pr_acct"
GB = 1_000_000_000

DAY_A = datetime(2026, 9, 19, 9, 0, 0)
DAY_B = datetime(2026, 9, 20, 9, 0, 0)
T_WIN10 = AS_OF - timedelta(days=10)   # 2026-09-11 12:00 -- inside 30 / 90, outside 7
T_WIN40 = AS_OF - timedelta(days=40)   # 2026-08-12 12:00 -- inside 90 only

# Every warehouse id this builder writes statements for (the serverless scenario has none).
WAREHOUSE_IDS = (
    "pr_wh_mem", "pr_wh_bigspill", "pr_wh_cap_ceiling", "pr_wh_cap_lag", "pr_wh_both",
    "pr_wh_cold", "pr_wh_ok", "pr_wh_few", "pr_wh_noconf", "pr_wh_deleted", "pr_wh_win40",
    "pr_wh_win10", "pr_wh_memwarn", "pr_wh_volwarn", "pr_wh_mixed", "pr_wh_edge",
    "pr_wh_fixed", "pr_wh_memcrit_capwarn", "pr_wh_nearwarn", "pr_wh_trivial_spill",
)
WH_TYPE = "PRO"                               # warehouse_type of every warehouses row written here
TODAY_STMT_ID = "pr_st_edge_today"            # the one statement on AS_OF's own day (excluded)
T_TODAY = datetime(2026, 9, 21, 1, 0, 0)      # today, before AS_OF -- outside every window
T_EDGE_OLD = AS_OF - timedelta(days=20)       # inside 30 / 90, outside 7
SERVERLESS_PREFIX = "pr_st_sls_"
SERVERLESS_COUNT = 25

# ---------------------------------------------------------------------------------------------
# query__history column order and row helpers -- copied from tests/fixtures/query_history.py
# (`QH_COLUMNS`, `_DEFAULTS`, `_compute`, `_row`), per T-65: copy, never import across builders.
# ---------------------------------------------------------------------------------------------
QH_COLUMNS = [
    "account_id", "workspace_id", "statement_id", "executed_by", "session_id",
    "execution_status", "compute", "executed_by_user_id", "statement_text", "statement_type",
    "error_message", "client_application", "client_driver", "total_duration_ms",
    "waiting_for_compute_duration_ms", "waiting_at_capacity_duration_ms", "execution_duration_ms",
    "compilation_duration_ms", "total_task_duration_ms", "result_fetch_duration_ms",
    "start_time", "end_time", "update_time", "read_partitions", "pruned_files", "read_files",
    "read_rows", "produced_rows", "read_bytes", "read_io_cache_percent", "from_result_cache",
    "spilled_local_bytes", "written_bytes", "shuffle_read_bytes", "query_source",
    "executed_as_user_id", "executed_as", "written_rows", "written_files",
    "cache_origin_statement_id", "query_parameters", "query_tags", "pruned_files_bytes",
    "read_files_bytes",
]

_DEFAULTS = {c: None for c in QH_COLUMNS}
_DEFAULTS.update({
    "workspace_id": WS,
    "execution_status": "FINISHED",
    "statement_type": "SELECT",
    "statement_text": "SELECT 1",
    "from_result_cache": False,
    "total_duration_ms": 1000,
    "execution_duration_ms": 800,
})


def _compute(warehouse_id=None, ctype=None, cluster_id=None):
    """query__history.compute STRUCT(type, cluster_id, warehouse_id). Defaults `type` to
    SERVERLESS_COMPUTE when no warehouse_id is given, WAREHOUSE otherwise -- the only values
    system.query.history carries for these two cases (T-65 rule 10; CLASSIC_COMPUTE is a classic
    Lakeflow pipeline, which this builder never writes)."""
    if ctype is None:
        ctype = "SERVERLESS_COMPUTE" if warehouse_id is None else "WAREHOUSE"
    return {"type": ctype, "cluster_id": cluster_id, "warehouse_id": warehouse_id}


def _row(**overrides):
    unknown = set(overrides) - set(QH_COLUMNS)
    if unknown:
        raise ValueError(f"unknown query__history column(s): {sorted(unknown)}")
    r = dict(_DEFAULTS)
    r.update(overrides)
    return r


def _stmt(sid, warehouse_id, start, *, exec_ms, cap_ms=0, cold_ms=0, compile_ms=0, spill=0,
          status="FINISHED", spill_shape=False, null_zeros=False,
          ctype="WAREHOUSE", read_bytes=50_000_000, shuffle=10_000_000):
    """One statement. total_duration_ms is always the sum of the two waits, compilation and
    execution. `null_zeros` writes a zero wait / zero spill as NULL (the query reads NULL as 0).
    `spill_shape` gives the statement its own text shape (a join), distinct from the plain scan."""
    total = cap_ms + cold_ms + compile_ms + exec_ms

    def z(v):
        return None if (null_zeros and not v) else v

    table = (warehouse_id or "pr_serverless") + "_facts"
    text = (f"SELECT * FROM {table} f JOIN {table}_dims d ON f.k = d.k" if spill_shape
            else f"SELECT * FROM {table}")
    return _row(
        statement_id=sid,
        executed_by="pr_analyst@example.com",
        session_id=f"pr_sess_{warehouse_id or 'serverless'}",
        execution_status=status,
        compute=_compute(warehouse_id=warehouse_id, ctype=ctype),
        statement_text=text,
        statement_type="SELECT",
        error_message="pr statement failed" if status == "FAILED" else None,
        total_duration_ms=total,
        waiting_for_compute_duration_ms=z(cold_ms),
        waiting_at_capacity_duration_ms=z(cap_ms),
        execution_duration_ms=exec_ms,
        compilation_duration_ms=z(compile_ms),
        start_time=start,
        end_time=start + timedelta(milliseconds=total),
        update_time=start + timedelta(milliseconds=total),
        read_bytes=read_bytes,
        spilled_local_bytes=z(spill),
        shuffle_read_bytes=shuffle,
        from_result_cache=False,
    )


# ---------------------------------------------------------------------------------------------
# Scenarios (module docstring has the expected row for each).
# ---------------------------------------------------------------------------------------------
def _mem_rows():
    wh = "pr_wh_mem"
    rows = [
        _stmt(f"pr_st_mem_spill_{i:02d}", wh, DAY_A + timedelta(minutes=5 * i), exec_ms=60_000,
              spill=GB // 2, spill_shape=True, null_zeros=True, read_bytes=2 * GB, shuffle=GB)
        for i in range(4)
    ]
    for i in range(36):
        day = DAY_A if i < 18 else DAY_B
        rows.append(_stmt(f"pr_st_mem_{i:02d}", wh, day + timedelta(minutes=30 + i),
                          exec_ms=1_000, null_zeros=True))
    return rows


def _bigspill_rows():
    wh = "pr_wh_bigspill"
    rows = [_stmt("pr_st_bigspill_spill", wh, DAY_A, exec_ms=1_000, spill=15 * GB,
                  spill_shape=True, null_zeros=True, read_bytes=20 * GB, shuffle=16 * GB)]
    for i in range(39):
        day = DAY_A if i < 20 else DAY_B
        rows.append(_stmt(f"pr_st_bigspill_{i:02d}", wh, day + timedelta(minutes=10 + i),
                          exec_ms=1_000, null_zeros=True))
    return rows


def _cap_ceiling_rows():
    wh = "pr_wh_cap_ceiling"
    return [
        _stmt(f"pr_st_cap_ceiling_{i:02d}", wh, (DAY_A if i % 2 else DAY_B) + timedelta(minutes=i),
              exec_ms=700, cap_ms=600 if i < 25 else 0)
        for i in range(50)
    ]


def _cap_lag_rows():
    wh = "pr_wh_cap_lag"
    return [
        _stmt(f"pr_st_cap_lag_{i:02d}", wh, DAY_A + timedelta(minutes=2 * i),
              exec_ms=920, cap_ms=200 if i < 16 else 0)
        for i in range(40)
    ]


def _both_rows():
    wh = "pr_wh_both"
    rows = []
    for i in range(40):
        day = DAY_A if i < 20 else DAY_B
        spills = i < 12
        rows.append(_stmt(f"pr_st_both_{i:02d}", wh, day + timedelta(minutes=i), exec_ms=750,
                          spill=100_000_000 if spills else 0, spill_shape=spills,
                          cap_ms=500 if i >= 20 else 0))
    return rows


def _cold_rows():
    wh = "pr_wh_cold"
    return [
        _stmt(f"pr_st_cold_{i:02d}", wh, DAY_B + timedelta(minutes=3 * i), exec_ms=500,
              compile_ms=100, cold_ms=1_000 if i < 12 else 0,
              status="FAILED" if i >= 28 else "FINISHED")
        for i in range(30)
    ]


def _ok_like_rows(wh, tag):
    """29 x 980 ms plus one 580 ms statement spilling 0.1 GB -> spill_time_pct 2.0, NONE / OK."""
    rows = [_stmt(f"pr_st_{tag}_spill", wh, DAY_A, exec_ms=580, spill=100_000_000,
                  spill_shape=True)]
    for i in range(29):
        rows.append(_stmt(f"pr_st_{tag}_{i:02d}", wh, DAY_A + timedelta(minutes=1 + i),
                          exec_ms=980))
    return rows


def _few_rows():
    wh = "pr_wh_few"
    return [
        _stmt(f"pr_st_few_{i:02d}", wh, DAY_A + timedelta(minutes=i), exec_ms=1_000,
              spill=2 * GB, spill_shape=True)
        for i in range(5)
    ]


def _noconf_rows():
    wh = "pr_wh_noconf"
    return [
        _stmt(f"pr_st_noconf_{i:02d}", wh, DAY_B + timedelta(minutes=i), exec_ms=900,
              cap_ms=300 if i < 10 else 0)
        for i in range(30)
    ]


def _serverless_rows():
    return [
        _stmt(f"{SERVERLESS_PREFIX}{i:02d}", None, DAY_A + timedelta(minutes=i), exec_ms=50,
              ctype="SERVERLESS_COMPUTE", read_bytes=1_000_000, shuffle=0)
        for i in range(SERVERLESS_COUNT)
    ]


def _win40_rows():
    wh = "pr_wh_win40"
    return [
        _stmt(f"pr_st_win40_{i:02d}", wh, T_WIN40 + timedelta(minutes=i), exec_ms=500)
        for i in range(25)
    ]


def _win10_rows():
    wh = "pr_wh_win10"
    return [
        _stmt(f"pr_st_win10_{i:02d}", wh, T_WIN10 + timedelta(minutes=i), exec_ms=900,
              cap_ms=250 if i < 10 else 0)
        for i in range(25)
    ]


def _memwarn_rows():
    wh = "pr_wh_memwarn"
    return [
        _stmt(f"pr_st_memwarn_{i:02d}", wh, DAY_A + timedelta(minutes=i), exec_ms=100,
              spill=100_000_000 if i < 3 else 0, spill_shape=i < 3)
        for i in range(20)
    ]


def _volwarn_rows():
    wh = "pr_wh_volwarn"
    rows = [_stmt("pr_st_volwarn_spill", wh, DAY_A, exec_ms=50, spill=3 * GB, spill_shape=True,
                  read_bytes=4 * GB, shuffle=3 * GB)]
    for i in range(19):
        rows.append(_stmt(f"pr_st_volwarn_{i:02d}", wh, DAY_B + timedelta(minutes=i), exec_ms=100))
    return rows


def _mixed_rows():
    wh = "pr_wh_mixed"
    return [
        _stmt(f"pr_st_mixed_{i:02d}", wh, DAY_A + timedelta(minutes=i), exec_ms=100,
              spill=100_000_000 if i < 3 else 0, spill_shape=i < 3,
              cap_ms=100 if i >= 10 else 0)
        for i in range(20)
    ]


def _edge_rows():
    wh = "pr_wh_edge"
    rows = [
        _stmt(f"pr_st_edge_{i:02d}", wh, DAY_B + timedelta(minutes=i), exec_ms=100,
              cap_ms=100 if i < 4 else 0)
        for i in range(20)
    ]
    rows.append(_stmt(TODAY_STMT_ID, wh, T_TODAY, exec_ms=100, cap_ms=100))
    return rows


def _fixed_rows():
    wh = "pr_wh_fixed"
    return [
        _stmt(f"pr_st_fixed_{i:02d}", wh, DAY_B + timedelta(minutes=i), exec_ms=100,
              cap_ms=100 if i < 4 else 0)
        for i in range(20)
    ]


def _memcrit_capwarn_rows():
    wh = "pr_wh_memcrit_capwarn"
    return [
        _stmt(f"pr_st_memcrit_{i:02d}", wh, DAY_A + timedelta(minutes=i), exec_ms=100,
              spill=100_000_000 if i < 6 else 0, spill_shape=i < 6,
              cap_ms=100 if i >= 16 else 0)
        for i in range(20)
    ]


# pr_wh_nearwarn: execution ms per statement (the first one spills) -- 83 / 833 of execution time
# in the spilling statement, 44 ms queued of 887 ms total.
NEARWARN_EXEC_MS = [83] + [39] * 18 + [48]
NEARWARN_CAP_MS = 44
NEARWARN_COMPILE_MS = 10


def _nearwarn_rows():
    wh = "pr_wh_nearwarn"
    rows = []
    for i, ms in enumerate(NEARWARN_EXEC_MS):
        rows.append(_stmt(f"pr_st_nearwarn_{i:02d}", wh, DAY_A + timedelta(minutes=i), exec_ms=ms,
                          spill=100_000_000 if i == 0 else 0, spill_shape=i == 0,
                          cap_ms=NEARWARN_CAP_MS if i == 1 else 0,
                          compile_ms=NEARWARN_COMPILE_MS if i == 2 else 0))
    return rows


# pr_wh_trivial_spill: 5 MB per spilling statement, under the header's 100 MB floor.
TRIVIAL_SPILL_BYTES = 5_000_000


def _trivial_spill_rows():
    wh = "pr_wh_trivial_spill"
    return [
        _stmt(f"pr_st_trivial_{i:02d}", wh, DAY_A + timedelta(minutes=i),
              exec_ms=1_000 if i < 10 else 100,
              spill=TRIVIAL_SPILL_BYTES if i < 10 else 0, spill_shape=i < 10)
        for i in range(20)
    ]


def statement_rows():
    """Every query__history row this builder writes (also used by the build's own checks)."""
    return (
        _mem_rows() + _bigspill_rows() + _cap_ceiling_rows() + _cap_lag_rows() + _both_rows()
        + _cold_rows() + _ok_like_rows("pr_wh_ok", "ok")
        + _ok_like_rows("pr_wh_deleted", "deleted") + _few_rows() + _noconf_rows()
        + _serverless_rows() + _win40_rows() + _win10_rows()
        + _memwarn_rows() + _volwarn_rows() + _mixed_rows() + _edge_rows()
        + _fixed_rows() + _memcrit_capwarn_rows() + _nearwarn_rows() + _trivial_spill_rows()
    )


# ---------------------------------------------------------------------------------------------
# compute.warehouses (SCD2) and compute.warehouse_events -- the `_warehouse` / `_we` shapes
# copied from tests/fixtures/compute.py (own account id and creator, `pr_` prefixed).
# ---------------------------------------------------------------------------------------------
_WH_SQL = (
    "INSERT INTO compute__warehouses "
    "(warehouse_id, workspace_id, account_id, warehouse_name, warehouse_type, "
    "warehouse_channel, warehouse_size, min_clusters, max_clusters, auto_stop_minutes, tags, "
    "change_time, delete_time, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _warehouse(con, warehouse_id, *, name, change_time, size, min_clusters, max_clusters,
               auto_stop, workspace_id=WS, delete_time=None):
    con.execute(_WH_SQL, [
        warehouse_id, workspace_id, ACCOUNT_ID, name, WH_TYPE, "CHANNEL_NAME_CURRENT", size,
        min_clusters, max_clusters, auto_stop, {}, change_time, delete_time,
        "pr_creator@example.com",
    ])


_WE_SQL = (
    "INSERT INTO compute__warehouse_events "
    "(account_id, workspace_id, warehouse_id, event_type, cluster_count, event_time) "
    "VALUES (?,?,?,?,?,?)"
)


def _we(con, warehouse_id, event_type, event_time, *, workspace_id=WS, cluster_count=1):
    con.execute(_WE_SQL, [ACCOUNT_ID, workspace_id, warehouse_id, event_type, cluster_count, event_time])


# (warehouse_id, size, min_clusters, max_clusters, auto_stop) -- one current row each; the
# SCD2 history rows for pr_wh_mem and pr_wh_deleted are written separately below.
_CONFIG = [
    ("pr_wh_bigspill", "SMALL", 1, 1, 10),
    ("pr_wh_cap_ceiling", "SMALL", 1, 1, 10),
    ("pr_wh_cap_lag", "MEDIUM", 1, 4, 10),
    ("pr_wh_both", "SMALL", 1, 2, 10),
    ("pr_wh_cold", "SMALL", 1, 1, 5),
    ("pr_wh_ok", "MEDIUM", 1, 2, 10),
    ("pr_wh_few", "X_SMALL", 1, 1, 10),
    ("pr_wh_win40", "SMALL", 1, 1, 10),
    ("pr_wh_win10", "SMALL", 1, 2, 10),
    ("pr_wh_memwarn", "SMALL", 1, 1, 10),
    ("pr_wh_volwarn", "SMALL", 1, 1, 10),
    ("pr_wh_mixed", "SMALL", 1, 3, 10),
    ("pr_wh_edge", "SMALL", 1, 3, 10),
    ("pr_wh_fixed", "SMALL", 1, 1, 10),
    ("pr_wh_memcrit_capwarn", "SMALL", 1, 2, 10),
    ("pr_wh_nearwarn", "SMALL", 1, 2, 10),
    ("pr_wh_trivial_spill", "SMALL", 1, 1, 10),
]


def _write_config(con):
    for wh, size, lo, hi, stop in _CONFIG:
        _warehouse(con, wh, name=wh, change_time=AS_OF - timedelta(days=60),
                   size=size, min_clusters=lo, max_clusters=hi, auto_stop=stop)
    # pr_wh_mem: SCD2 -- an older, larger shape, then the latest X_SMALL 1..2 the query must read.
    _warehouse(con, "pr_wh_mem", name="pr_wh_mem_old", change_time=AS_OF - timedelta(days=60),
               size="SMALL", min_clusters=1, max_clusters=4, auto_stop=10)
    _warehouse(con, "pr_wh_mem", name="pr_wh_mem", change_time=AS_OF - timedelta(days=10),
               size="X_SMALL", min_clusters=1, max_clusters=2, auto_stop=10)
    # pr_wh_deleted: live, then deleted after its statements ran -- the query KEEPS it.
    deleted_at = DAY_B + timedelta(hours=10)
    _warehouse(con, "pr_wh_deleted", name="pr_wh_deleted", change_time=AS_OF - timedelta(days=60),
               size="SMALL", min_clusters=1, max_clusters=1, auto_stop=10)
    _warehouse(con, "pr_wh_deleted", name="pr_wh_deleted", change_time=deleted_at,
               size="SMALL", min_clusters=1, max_clusters=1, auto_stop=10,
               delete_time=deleted_at)
    # pr_wh_noconf: deliberately NO warehouses row.


def _write_events(con):
    _we(con, "pr_wh_mem", "RUNNING", DAY_A - timedelta(minutes=5), cluster_count=1)
    _we(con, "pr_wh_cap_ceiling", "RUNNING", DAY_A - timedelta(minutes=5), cluster_count=1)
    _we(con, "pr_wh_cap_lag", "RUNNING", DAY_A - timedelta(minutes=5), cluster_count=1)
    _we(con, "pr_wh_cap_lag", "SCALED_UP", DAY_A + timedelta(minutes=20), cluster_count=2)
    _we(con, "pr_wh_cap_lag", "SCALED_DOWN", DAY_A + timedelta(minutes=90), cluster_count=1)
    _we(con, "pr_wh_both", "RUNNING", DAY_A - timedelta(minutes=5), cluster_count=1)
    _we(con, "pr_wh_both", "SCALED_UP", DAY_A + timedelta(minutes=10), cluster_count=2)
    _we(con, "pr_wh_win10", "RUNNING", T_WIN10 - timedelta(minutes=5), cluster_count=1)
    _we(con, "pr_wh_win10", "SCALED_UP", T_WIN10 + timedelta(minutes=5), cluster_count=2)
    _we(con, "pr_wh_mixed", "RUNNING", DAY_A - timedelta(minutes=5), cluster_count=1)
    # pr_wh_edge: the events window's two bounds. Only the DAY_A event is inside window 7; the
    # 20-day-old one is inside 30 / 90; today's is inside none (event_time < current_date()).
    _we(con, "pr_wh_edge", "RUNNING", DAY_A - timedelta(minutes=5), cluster_count=1)
    _we(con, "pr_wh_edge", "SCALED_UP", T_EDGE_OLD, cluster_count=3)
    _we(con, "pr_wh_edge", "SCALED_UP", T_TODAY + timedelta(hours=5), cluster_count=3)
    # pr_wh_fixed: the only in-window event is a stop -- the events alone would say "0 of 1".
    _we(con, "pr_wh_fixed", "STOPPED", DAY_A + timedelta(hours=2), cluster_count=0)
    # pr_wh_ok, pr_wh_noconf, pr_wh_memcrit_capwarn, pr_wh_nearwarn: no events and an autoscale
    # range (or no config) -> max_cluster_count_seen NULL -> at_ceiling NULL ("unseen").
    # pr_wh_bigspill, pr_wh_cold, pr_wh_few, pr_wh_deleted, pr_wh_win40, pr_wh_memwarn,
    # pr_wh_volwarn, pr_wh_trivial_spill: no events either, but 1..1 -> at_ceiling TRUE from the
    # configuration.


# ---------------------------------------------------------------------------------------------
# Auto-discovered by build_fixtures.py (DEC-17).
# ---------------------------------------------------------------------------------------------
def build(con) -> None:
    rows = statement_rows()
    seen = set()
    for r in rows:
        sid = r["statement_id"]
        if sid in seen:
            raise ValueError(f"duplicate statement_id in pressure_warehouses.py fixture: {sid!r}")
        seen.add(sid)
        wh = r["compute"]["warehouse_id"]
        if not sid.startswith("pr_st_") or (wh is not None and not wh.startswith("pr_wh_")):
            raise ValueError(f"unprefixed id in pressure_warehouses.py fixture: {sid!r} / {wh!r}")
        if r["total_duration_ms"] != sum(
            (r[c] or 0) for c in ("waiting_at_capacity_duration_ms",
                                  "waiting_for_compute_duration_ms",
                                  "compilation_duration_ms", "execution_duration_ms")
        ):
            raise ValueError(f"total_duration_ms is not the sum of its parts: {sid!r}")

    cols_sql = ", ".join(f'"{c}"' for c in QH_COLUMNS)
    placeholders = ", ".join("?" for _ in QH_COLUMNS)
    insert_sql = f'INSERT INTO query__history ({cols_sql}) VALUES ({placeholders})'
    con.executemany(insert_sql, [[r[c] for c in QH_COLUMNS] for r in rows])

    _write_config(con)
    _write_events(con)
