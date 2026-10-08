"""tests/fixtures/query_history.py -- batch E (T-19), fills system.query.history only.

Exercises the 12 batch-E `performance` domain query ids (audit_self_cost,
query_cache_coldstart, query_costly_statements, query_costly_statements_grouped,
query_failed_queries_daily, query_local_spillage, query_per_query_estimate_lane,
query_provenance_by_source, query_pruning_effectiveness, query_queuing_waits,
query_shuffle_write_amplification, query_workload_mix_hours). The domain's 13th id,
query_task_statement_breakdown, is T-07's (tests/fixtures/drilldown.py) -- out of scope here.

Per DEC-15 this builder is NOT the drill-down port: it uses the shared workspace pool
(1111 acme-prod, 2222 acme-dev, 3333 acme-uat -- here only 1111 is actually used, kept simple)
and every literal id this module writes (statement_id, warehouse_id, job_id/dashboard_id/...
used inside query_source) carries the `qh_` prefix, disjoint from drilldown.py's `dd_`-prefixed
ids and its own 9001/9002 workspaces. drilldown.py ALSO writes system.query.history rows (its
three ported subaudit scenarios, workspace 9001, `dd_qts_` statement ids) into the same shared
source table (files under tests/fixtures/parquet/query__history/ are unioned by name) -- every
one of the 12 ids here reads the WHOLE unioned table, so a query whose GROUP BY / PARTITION BY
does not include workspace_id could in principle mix rows from both builders. The only id where
that risk is real is query_costly_statements: drilldown's statement rows always carry
compute.warehouse_id = NULL (serverless), the same NULL-warehouse pooled partition this module's
own serverless test row lands in, so that row's execution_duration_ms is set enormously large
(1e12 ms) so its within-partition share is >= the CRITICAL threshold regardless of whatever
modest durations drilldown's own rows contribute to that same NULL partition. Every other id's
GROUP BY includes workspace_id (1111 here vs 9001 for drilldown), so cross-builder mixing cannot
happen for those.

AS_OF = 2026-09-21 12:00:00 (base.AS_OF); dbt's `test` target pins audit_today() = DATE
'2026-09-21' and audit_now() = TIMESTAMP '2026-09-21 12:00:00' to the same instant
(dbt/macros/audit_time.sql). The standard window predicate used by every id except
audit_self_cost is `start_time >= current_date() - INTERVAL :period_days DAYS AND start_time <
current_date()` (today itself always excluded); audit_self_cost's own predicate is
`start_time >= dateadd(day, -:period_days, current_date())` with NO upper bound (today's
still-landing rows are meant to be included). Five reusable time anchors, relative to AS_OF,
cover every window boundary named in PLAN.md 5.7 / this task's Inputs:

    D7    AS_OF - 3d    inside the 7d window (and 30d, 90d)
    D30   AS_OF - 16d   inside the 30d window, outside the 7d window
    D90   AS_OF - 68d   inside the 90d window, outside the 30d window
    DTODAY 2026-09-21 08:00:00   on AS_OF's own calendar day -- excluded by every standard-window
                                  id (start_time < current_date() fails), but INCLUDED by
                                  audit_self_cost (no upper bound) -- this is the one id where
                                  "on AS_OF's own date" is a positive, not a negative, case.
    DOLD  AS_OF - 143d  older than the 90d window -- excluded everywhere, including
                        audit_self_cost (older than even the 90d-window lower bound; no
                        materialized window is wider than 90d).

Predicate / CASE-branch / window-boundary -> row map (statement_id prefixes; see each
`_*_rows()` function below for the literal ids and values):

  audit_self_cost
    marker filter (statement_text ILIKE '%databricks_audit%')
      match:     qh_asc_marker_{7d,30d,90d,today,old90}
      no match:  qh_asc_nomarker (in-window text without the marker -> must be excluded)
    window (no upper bound): qh_asc_marker_today is INCLUDED at every window (7/30/90);
      qh_asc_marker_old90 excluded at every window (older than even the 90d lower bound).

  query_cache_coldstart (status: warn_io_cache_pct=50, crit_io_cache_pct=20; the weighted average
      only counts rows with read_bytes>0 that are NOT a result-cache hit, so every scenario row
      below sets read_bytes>0 and from_result_cache=False to actually land in that average)
    OK        qh_cc_ok    (read_io_cache_percent=80)
    WARN      qh_cc_warn  (=35)
    CRITICAL  qh_cc_crit  (=10)
    NOT_ASSESSED  qh_cc_null (read_io_cache_percent NULL -- "no scan ran")
    window    qh_cc_win_{t7,t30,t90,ttoday,told90} (one row per anchor, distinct day -> distinct
              group; read_io_cache_percent=70 (OK) so the band is not the point of these rows)

  query_costly_statements (status: warn_wh_share=0.1, crit_wh_share=0.25; PARTITION BY warehouse)
    warehouse qh_cs_wh1: qh_cs_wh1_a (800ms, share 0.8 -> CRITICAL), qh_cs_wh1_b (200ms, share
              0.2 -> WARN) -- CRITICAL+WARN from one warehouse, share ordering == duration
              ordering within the partition (worst-first check scoped to this one warehouse).
    warehouse qh_cs_wh2: qh_cs_wh2_e (1000ms alone, share 1.0 -> CRITICAL)
    warehouse qh_cs_wh3: qh_cs_wh3_small (500ms, share ~0.053 -> OK), qh_cs_wh3_big (9000ms,
              share ~0.947 -> CRITICAL)
    serverless (warehouse_id NULL): qh_cs_serverless1 (1e12 ms, dominates the NULL partition
              regardless of drilldown's own NULL-warehouse rows -> CRITICAL)
    executed_by mask branches (of the 3, DEC-66.3): qh_cs_wh1_a NULL (passthrough), qh_cs_wh1_b
              'dana@example.com' with executed_by_user_id='8812345' set (else: real-id path,
              "8812345 da***"), qh_cs_wh2_e a GUID (passthrough), qh_cs_wh3_small 'root_operator'
              with no executed_by_user_id (else: hash-derived id), qh_cs_wh3_big '__REDACTED__'
              (passthrough)
    window    qh_cs_win_{t7,t30,t90,ttoday,told90} on warehouse qh_cs_winwh (own warehouse, not
              shared with the band rows above, so window presence/absence is unambiguous)

  query_costly_statements_grouped (status: warn_total_hours=1 (3.6e6 ms), crit=5 (1.8e7 ms);
                                    GROUP BY statement_fingerprint)
    hot shape (2 runs, same de-valued text, different quoted literal): qh_csg_hot_1 (1e7 ms),
              qh_csg_hot_2 (1e7 ms) -> total 2e7 ms -> CRITICAL
    singleton WARN: qh_csg_warn (4e6 ms -> WARN)
    singleton OK:   qh_csg_ok (1e6 ms -> OK)
    window: qh_csg_win_{t7,t30,t90,ttoday,told90}, each its own unique (literal-free) shape text
              so each anchor forms its own fingerprint/group

  query_failed_queries_daily (status: warn_failed_count=5, crit_failed_count=20)
    CRITICAL group: qh_fq_crit_0..19 on warehouse qh_fq_wh_crit (20 rows, count=20 -> CRITICAL);
              error_message carries an email + a single-quoted literal to exercise the de-valuing
              regex chain
    WARN group: qh_fq_warn_0..5 on warehouse qh_fq_wh_warn (6 rows, count=6 -> WARN)
    OK group:   qh_fq_ok_0..1 on warehouse qh_fq_wh_ok (2 rows, count=2 -> OK)
    window: qh_fq_win_{t7,t30,t90,ttoday,told90} on warehouse qh_fq_win (1 row/day -> distinct
              groups by date)

  query_failed_statements_grouped (status: warn_statements=10, crit_statements=50 -- this id's
  own thresholds, not query_failed_queries_daily's)
    CRITICAL group: qh_efg_crit_0..49 on warehouse qh_efg_wh_crit (50 FAILED rows, one shared
              [TABLE_OR_VIEW_NOT_FOUND] error_class, job source -> statements=50 -> CRITICAL)
    OK (canceled): qh_efg_cancel_0..2 on the same warehouse (3 CANCELED rows, same job source ->
              always OK, however many -- a cancel is never counted as a failure)

  query_local_spillage (status: warn_spill_gb=1 (1e9 B), crit_spill_gb=10 (1e10 B))
    OK       qh_ls_ok   (5e8 bytes, executed_by NULL -> passthrough)
    WARN     qh_ls_warn (2e9 bytes, executed_by 'dana@example.com' with executed_by_user_id
              '8812345' set -> DEC-66.3 real-id path, "8812345 da***", proving MAX(...) works)
    CRITICAL qh_ls_crit (1.5e10 bytes, executed_by '__REDACTED__' -> passthrough)
    window   qh_ls_win_{t7,t30,t90,ttoday,told90} (spilled_local_bytes just above the warn band,
              1e9+1, on warehouse qh_ls_win)

  query_per_query_estimate_lane (row-level; no status; filter: FINISHED, not cached, duration>0,
                                  warehouse_id NOT NULL)
    included: qh_pel_ok1, qh_pel_ok2
    excluded (one row per excluded predicate):
      qh_pel_excl_wh      warehouse_id NULL (serverless)      -> excluded
      qh_pel_excl_status  execution_status = 'FAILED'         -> excluded
      qh_pel_excl_cached  from_result_cache = true             -> excluded
      qh_pel_excl_zero    execution_duration_ms = 0            -> excluded
    window: qh_pel_win_{t7,t30,t90,ttoday,told90} (warehouse set, FINISHED, not cached, exec>0)

  query_provenance_by_source (no status; one row per source_kind CASE branch)
    job              qh_prov_job1       (query_source.job_info.job_id set)
    dashboard        qh_prov_dash1      (query_source.dashboard_id set)
    legacy_dashboard qh_prov_legacy1    (query_source.legacy_dashboard_id set)
    notebook         qh_prov_nb1        (query_source.notebook_id set)
    alert            qh_prov_alert1     (query_source.alert_id set)
    genie            qh_prov_genie1     (query_source.genie_space_id set)
    sql_editor       qh_prov_sqlq1      (query_source.sql_query_id set)
    other            qh_prov_other1     (query_source entirely NULL -- every subfield absent)
    window: qh_prov_win_{t7,t30,t90,ttoday,told90}, each its own unique job_id (source=job) so
              each anchor forms its own group despite no date column in this id's GROUP BY

  query_pruning_effectiveness (status: warn_prune_ratio=0.5, crit_prune_ratio=0.2,
                                min_read_bytes=1073741824 (1 GiB), min_recurring_flagged_days=2;
                                ratio = pruned/(pruned+read); WHERE excludes pruned=0 AND read=0;
                                every row's read_bytes is above min_read_bytes so status actually
                                bands instead of reading NOT_ASSESSED for being too small to judge)
    CRITICAL qh_pe_crit (pruned=1, read=9 -> ratio 0.1) x2 days (qh_pe_crit, qh_pe_crit_recur) so
              days_flagged=2 clears :min_recurring_flagged_days and the day stays CRITICAL instead
              of being capped to WARN as a one-off
    WARN     qh_pe_warn (pruned=3, read=7 -> ratio 0.3)
    OK       qh_pe_ok   (pruned=8, read=2 -> ratio 0.8)
    excluded-by-WHERE: qh_pe_zero (pruned=0, read=0 -> must not appear as a row AT ALL, in any
              window, not even as NOT_ASSESSED)
    window: qh_pe_win_{t7,t30,t90,ttoday,told90} (pruned=5, read=5, ratio 0.5, read_bytes=500
              (below min_read_bytes, always NOT_ASSESSED) -- band is not the point of these rows,
              only day-window presence is)

  query_queuing_waits (status: warn_queue_secs=60 (60000ms), crit_queue_secs=600 (600000ms);
                        combined waiting_at_capacity_duration_ms + waiting_for_compute_duration_ms)
    OK       qh_qw_ok   (10000 + 5000 = 15000ms)
    WARN     qh_qw_warn (50000 + 20000 = 70000ms)
    CRITICAL qh_qw_crit (400000 + 300000 = 700000ms)
    window: qh_qw_win_{t7,t30,t90,ttoday,told90} (1000 + 1000 = 2000ms, band not the point)

  query_shuffle_write_amplification (status: warn_shuffle_gb=50 (5e10 B), crit_shuffle_gb=200
      (2e11 B), warn_avg_file_mb=32 (3.2e7 B), crit_avg_file_mb=8 (8e6 B); triggered by EITHER
      the shuffle volume OR the small-file average, whichever is worse)
    CRITICAL via shuffle:    qh_sw_shuffle_crit  (shuffle=2.5e11, written_files=0)
    WARN via shuffle:        qh_sw_shuffle_warn  (shuffle=6e10, written_files=0)
    OK:                      qh_sw_ok            (shuffle=1e9, written_files=10,
                                                    written_bytes=1e9 -> avg 100MB/file)
    CRITICAL via small files (shuffle low): qh_sw_smallfile_crit (written_files=1000,
                                                    written_bytes=5e9 -> avg 5MB/file)
    WARN via small files (shuffle low):     qh_sw_smallfile_warn (written_files=100,
                                                    written_bytes=2e9 -> avg 20MB/file)
    window: qh_sw_win_{t7,t30,t90,ttoday,told90} (shuffle=1e6, written_files=0 -> OK, band not
              the point)

  query_workload_mix_hours (no status; GROUP BY includes hour(start_time))
    window: qh_wm_win_{t7,t30,t90,ttoday,told90}
    hour-of-day distinctness on the SAME day/warehouse: qh_wm_hour_early (hour 3),
              qh_wm_hour_late (hour 20) -- proves hour_of_day is part of the grain
    execution_status mix: qh_wm_failed (execution_status='FAILED', to separate finished_count
              from query_count within its own group)

Stdlib + duckdb only.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from base import AS_OF, write_parquet  # noqa: F401 (write_parquet re-exported for callers)

# DEC-15: shared workspace pool (this task uses 1111 only, kept simple -- billing.py, T-09, owns
# the actual access__workspaces_latest rows for 1111/2222/3333, never this builder).
WS = "1111"

# ---------------------------------------------------------------------------------------------
# Time anchors (module docstring above explains each).
# ---------------------------------------------------------------------------------------------
D7 = AS_OF - timedelta(days=3)
D30 = AS_OF - timedelta(days=16)
D90 = AS_OF - timedelta(days=68)
DTODAY = datetime(2026, 9, 21, 8, 0, 0)
DOLD = AS_OF - timedelta(days=143)

# ---------------------------------------------------------------------------------------------
# query__history column order (matches tests/fixtures/ddl.py's ARROW_SCHEMA['query__history']
# exactly, field-for-field) and the row-building helpers.
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
    SERVERLESS_COMPUTE when no warehouse_id is given, PRO_WAREHOUSE otherwise."""
    if ctype is None:
        ctype = "SERVERLESS_COMPUTE" if warehouse_id is None else "PRO_WAREHOUSE"
    return {"type": ctype, "cluster_id": cluster_id, "warehouse_id": warehouse_id}


def _qsource(job_id=None, job_run_id=None, job_task_run_id=None, dashboard_id=None,
             legacy_dashboard_id=None, alert_id=None, notebook_id=None, sql_query_id=None,
             genie_space_id=None, pipeline_id=None, update_id=None):
    """query__history.query_source STRUCT, full shape (every leaf field explicit, per
    tests/fixtures/ddl.py). Pass None (the module-level helper is never called at all, i.e.
    query_source stays NULL on the row) for the "other" source_kind branch -- dot access on a
    NULL struct evaluates to NULL for every subfield, which is exactly what that branch needs."""
    return {
        "job_info": {"job_id": job_id, "job_run_id": job_run_id, "job_task_run_id": job_task_run_id},
        "legacy_dashboard_id": legacy_dashboard_id,
        "dashboard_id": dashboard_id,
        "alert_id": alert_id,
        "notebook_id": notebook_id,
        "sql_query_id": sql_query_id,
        "genie_space_id": genie_space_id,
        "pipeline_info": {"pipeline_id": pipeline_id, "update_id": update_id},
    }


def _row(**overrides):
    unknown = set(overrides) - set(QH_COLUMNS)
    if unknown:
        raise ValueError(f"unknown query__history column(s): {sorted(unknown)}")
    r = dict(_DEFAULTS)
    r.update(overrides)
    return r


# ---------------------------------------------------------------------------------------------
# audit_self_cost -- :period_days only, no status; filter start_time >= today-period (NO upper
# bound) AND statement_text ILIKE '%databricks!_audit%' ESCAPE '!' (T-75B review fix: a backslash
# escape does not parse as Databricks SQL, so '!' is used instead -- ordinary on both engines).
# GROUP BY workspace_id,
# statement_type -- every row below shares (WS, 'SELECT') so all 6 land in one group per window
# (or are excluded). qh_asc_wildcard_decoy (T-75B) proves the '_' escape: before that fix, ILIKE's
# unescaped '_' wildcard would have matched ANY single character in that position, so this row's
# "databricksXaudit" text (deliberately NOT containing the literal marker) would have wrongly
# counted -- the fixed query must exclude it exactly like qh_asc_nomarker.
# ---------------------------------------------------------------------------------------------
def _audit_self_cost_rows():
    rows = []
    for sid, ts, dur, task_dur, who in [
        ("qh_asc_marker_7d", D7, 1000, 800, "alice"),
        ("qh_asc_marker_30d", D30, 2000, 1600, "bob"),
        ("qh_asc_marker_90d", D90, 3000, 2400, "alice"),
        ("qh_asc_marker_today", DTODAY, 4000, 3200, "carol"),
        ("qh_asc_marker_old90", DOLD, 5000, 4000, "dave"),
    ]:
        rows.append(_row(
            statement_id=sid, statement_type="SELECT", executed_by=who, start_time=ts,
            total_duration_ms=dur, total_task_duration_ms=task_dur,
            statement_text=f"-- databricks_audit run marker {sid}\nSELECT 1",
            compute=_compute(warehouse_id="qh_asc_wh"),
        ))
    rows.append(_row(
        statement_id="qh_asc_nomarker", statement_type="SELECT", executed_by="erin",
        start_time=D7, total_duration_ms=9999, total_task_duration_ms=8000,
        statement_text="SELECT * FROM some_ordinary_table",
        compute=_compute(warehouse_id="qh_asc_wh"),
    ))
    rows.append(_row(
        statement_id="qh_asc_wildcard_decoy", statement_type="SELECT", executed_by="frank",
        start_time=D7, total_duration_ms=1234, total_task_duration_ms=1000,
        statement_text="SELECT 'databricksXaudit' -- looks like the marker only under an unescaped '_' wildcard",
        compute=_compute(warehouse_id="qh_asc_wh"),
    ))
    return rows


# ---------------------------------------------------------------------------------------------
# query_cache_coldstart -- GROUP BY date(start_time), workspace_id, compute.type,
# compute.warehouse_id. warn_io_cache_pct=50, crit_io_cache_pct=20.
# ---------------------------------------------------------------------------------------------
def _cache_coldstart_rows():
    rows = []
    # read_bytes>0 and from_result_cache=False on every scenario row: the weighted average and
    # scanned_query_count only count rows that actually scanned real bytes and were not
    # themselves a result-cache hit.
    for sid, wh, pct in [
        ("qh_cc_ok", "qh_cc_wh_ok", 80.0),
        ("qh_cc_warn", "qh_cc_wh_warn", 35.0),
        ("qh_cc_crit", "qh_cc_wh_crit", 10.0),
    ]:
        rows.append(_row(
            statement_id=sid, start_time=D7, compute=_compute(warehouse_id=wh),
            read_io_cache_percent=pct, from_result_cache=False, read_bytes=1_000_000,
            waiting_for_compute_duration_ms=100, compilation_duration_ms=20,
        ))
    # qh_cc_null: no scan ran at all -- read_bytes=0 keeps it out of scanned_query_count too.
    rows.append(_row(
        statement_id="qh_cc_null", start_time=D7, compute=_compute(warehouse_id="qh_cc_wh_null"),
        read_io_cache_percent=None, from_result_cache=False, read_bytes=0,
        waiting_for_compute_duration_ms=100, compilation_duration_ms=20,
    ))
    for sid, ts in [
        ("qh_cc_win_t7", D7), ("qh_cc_win_t30", D30), ("qh_cc_win_t90", D90),
        ("qh_cc_win_ttoday", DTODAY), ("qh_cc_win_told90", DOLD),
    ]:
        rows.append(_row(
            statement_id=sid, start_time=ts, compute=_compute(warehouse_id="qh_cc_win"),
            read_io_cache_percent=70.0, from_result_cache=False, read_bytes=1_000_000,
            waiting_for_compute_duration_ms=50, compilation_duration_ms=10,
        ))
    return rows


# ---------------------------------------------------------------------------------------------
# query_costly_statements -- row-level, PARTITION BY warehouse_id, ranked by
# execution_duration_ms DESC within warehouse. warn_wh_share=0.1, crit_wh_share=0.25.
# ---------------------------------------------------------------------------------------------
def _costly_statements_rows():
    rows = [
        _row(statement_id="qh_cs_wh1_a", start_time=D7, execution_duration_ms=800,
             compute=_compute(warehouse_id="qh_cs_wh1"), executed_by=None,
             statement_text="SELECT * FROM t1"),
        _row(statement_id="qh_cs_wh1_b", start_time=D7, execution_duration_ms=200,
             compute=_compute(warehouse_id="qh_cs_wh1"), executed_by="dana@example.com",
             executed_by_user_id="8812345", statement_text="SELECT * FROM t2"),
        _row(statement_id="qh_cs_wh2_e", start_time=D7, execution_duration_ms=1000,
             compute=_compute(warehouse_id="qh_cs_wh2"),
             executed_by="11112222-3333-4444-5555-666677778888",
             statement_text="SELECT * FROM t3"),
        _row(statement_id="qh_cs_wh3_small", start_time=D7, execution_duration_ms=500,
             compute=_compute(warehouse_id="qh_cs_wh3"), executed_by="root_operator",
             statement_text="SELECT * FROM t4"),
        _row(statement_id="qh_cs_wh3_big", start_time=D7, execution_duration_ms=9000,
             compute=_compute(warehouse_id="qh_cs_wh3"), executed_by="__REDACTED__",
             statement_text="SELECT * FROM t5"),
        _row(statement_id="qh_cs_serverless1", start_time=D7, execution_duration_ms=1_000_000_000_000,
             compute=_compute(warehouse_id=None), executed_by=None,
             statement_text="SELECT * FROM t6"),
    ]
    for sid, ts in [
        ("qh_cs_win_t7", D7), ("qh_cs_win_t30", D30), ("qh_cs_win_t90", D90),
        ("qh_cs_win_ttoday", DTODAY), ("qh_cs_win_told90", DOLD),
    ]:
        rows.append(_row(
            statement_id=sid, start_time=ts, execution_duration_ms=100,
            compute=_compute(warehouse_id="qh_cs_winwh"), executed_by=None,
            statement_text=f"SELECT * FROM t_{sid}",
        ))
    return rows


# ---------------------------------------------------------------------------------------------
# query_costly_statements_grouped -- GROUP BY statement_fingerprint (sha2 of the de-valued
# text: strip emails, then single-quoted literals). warn_total_hours=1 (3.6e6ms), crit=5 (1.8e7ms).
# ---------------------------------------------------------------------------------------------
def _costly_statements_grouped_rows():
    rows = [
        _row(statement_id="qh_csg_hot_1", start_time=D7, execution_duration_ms=10_000_000,
             compute=_compute(warehouse_id="qh_csg_wh1"),
             statement_text="SELECT * FROM hot_table WHERE k = 'aaa'"),
        _row(statement_id="qh_csg_hot_2", start_time=D30, execution_duration_ms=10_000_000,
             compute=_compute(warehouse_id="qh_csg_wh2"),
             statement_text="SELECT * FROM hot_table WHERE k = 'bbb'"),
        _row(statement_id="qh_csg_warn", start_time=D7, execution_duration_ms=4_000_000,
             compute=_compute(warehouse_id="qh_csg_wh1"),
             statement_text="SELECT * FROM warm_table"),
        _row(statement_id="qh_csg_ok", start_time=D7, execution_duration_ms=1_000_000,
             compute=_compute(warehouse_id="qh_csg_wh1"),
             statement_text="SELECT * FROM cold_table"),
    ]
    for sid, ts in [
        ("qh_csg_win_t7", D7), ("qh_csg_win_t30", D30), ("qh_csg_win_t90", D90),
        ("qh_csg_win_ttoday", DTODAY), ("qh_csg_win_told90", DOLD),
    ]:
        rows.append(_row(
            statement_id=sid, start_time=ts, execution_duration_ms=500_000,
            compute=_compute(warehouse_id="qh_csg_winwh"),
            statement_text=f"SELECT 1 AS {sid}",
        ))
    return rows


# ---------------------------------------------------------------------------------------------
# query_failed_queries_daily -- GROUP BY date(start_time), workspace_id, compute.type,
# compute.warehouse_id, execution_status, statement_type, executed_by.
# warn_failed_count=5, crit_failed_count=20.
# ---------------------------------------------------------------------------------------------
def _failed_queries_daily_rows():
    rows = []
    for i in range(20):
        rows.append(_row(
            statement_id=f"qh_fq_crit_{i}", start_time=D7, execution_status="FAILED",
            compute=_compute(warehouse_id="qh_fq_wh_crit"), executed_by="fail@example.com",
            error_message="user 'fail@example.com' query failed: reason = 'timeout'",
        ))
    for i in range(6):
        rows.append(_row(
            statement_id=f"qh_fq_warn_{i}", start_time=D7, execution_status="CANCELED",
            compute=_compute(warehouse_id="qh_fq_wh_warn"), executed_by=None,
        ))
    for i in range(2):
        rows.append(_row(
            statement_id=f"qh_fq_ok_{i}", start_time=D7, execution_status="FAILED",
            compute=_compute(warehouse_id="qh_fq_wh_ok"),
            executed_by="99998888-7777-6666-5555-444433332222",
        ))
    for sid, ts in [
        ("qh_fq_win_t7", D7), ("qh_fq_win_t30", D30), ("qh_fq_win_t90", D90),
        ("qh_fq_win_ttoday", DTODAY), ("qh_fq_win_told90", DOLD),
    ]:
        rows.append(_row(
            statement_id=sid, start_time=ts, execution_status="FAILED",
            compute=_compute(warehouse_id="qh_fq_win"), executed_by=None,
        ))
    return rows


# ---------------------------------------------------------------------------------------------
# query_failed_statements_grouped -- GROUP BY workspace_id, warehouse_id, execution_status,
# error_class, source_kind, source_id. warn_statements=10, crit_statements=50 (this id's own
# thresholds, separate from query_failed_queries_daily's 5/20).
# ---------------------------------------------------------------------------------------------
def _failed_statements_grouped_rows():
    rows = []
    job_src = _qsource(job_id="qh_efg_job_1")
    for i in range(50):
        rows.append(_row(
            statement_id=f"qh_efg_crit_{i}", start_time=D7, execution_status="FAILED",
            compute=_compute(warehouse_id="qh_efg_wh_crit"), executed_by="efg@example.com",
            error_message="[TABLE_OR_VIEW_NOT_FOUND] Table or view not found: qh_efg.t",
            query_source=job_src,
        ))
    for i in range(3):
        rows.append(_row(
            statement_id=f"qh_efg_cancel_{i}", start_time=D7, execution_status="CANCELED",
            compute=_compute(warehouse_id="qh_efg_wh_crit"), executed_by="efg@example.com",
            error_message=None, query_source=job_src,
        ))
    return rows


# ---------------------------------------------------------------------------------------------
# query_local_spillage -- GROUP BY date(start_time), workspace_id, compute.type,
# compute.warehouse_id, executed_by. warn_spill_gb=1 (1e9 B), crit_spill_gb=10 (1e10 B).
# ---------------------------------------------------------------------------------------------
def _local_spillage_rows():
    rows = [
        _row(statement_id="qh_ls_ok", start_time=D7, compute=_compute(warehouse_id="qh_ls_wh_ok"),
             executed_by=None, spilled_local_bytes=500_000_000, shuffle_read_bytes=1_000),
        _row(statement_id="qh_ls_warn", start_time=D7, compute=_compute(warehouse_id="qh_ls_wh_warn"),
             executed_by="dana@example.com", executed_by_user_id="8812345",
             spilled_local_bytes=2_000_000_000, shuffle_read_bytes=2_000),
        _row(statement_id="qh_ls_crit", start_time=D7, compute=_compute(warehouse_id="qh_ls_wh_crit"),
             executed_by="__REDACTED__", spilled_local_bytes=15_000_000_000,
             shuffle_read_bytes=3_000),
    ]
    for sid, ts in [
        ("qh_ls_win_t7", D7), ("qh_ls_win_t30", D30), ("qh_ls_win_t90", D90),
        ("qh_ls_win_ttoday", DTODAY), ("qh_ls_win_told90", DOLD),
    ]:
        rows.append(_row(
            statement_id=sid, start_time=ts, compute=_compute(warehouse_id="qh_ls_win"),
            executed_by=None, spilled_local_bytes=1_000_000_001, shuffle_read_bytes=500,
        ))
    return rows


# ---------------------------------------------------------------------------------------------
# query_per_query_estimate_lane -- row-level (grain: statement_id). Filter: FINISHED, not
# cached, execution_duration_ms > 0, compute.warehouse_id IS NOT NULL.
# ---------------------------------------------------------------------------------------------
def _per_query_estimate_lane_rows():
    rows = [
        _row(statement_id="qh_pel_ok1", start_time=D7, compute=_compute(warehouse_id="qh_pel_wh"),
             execution_duration_ms=1000, waiting_for_compute_duration_ms=100,
             total_task_duration_ms=4000, read_bytes=10_000, total_duration_ms=1200),
        _row(statement_id="qh_pel_ok2", start_time=D7, compute=_compute(warehouse_id="qh_pel_wh"),
             execution_duration_ms=2000, waiting_for_compute_duration_ms=200,
             total_task_duration_ms=8000, read_bytes=20_000, total_duration_ms=2400),
        _row(statement_id="qh_pel_excl_wh", start_time=D7, compute=_compute(warehouse_id=None),
             execution_duration_ms=1000),
        _row(statement_id="qh_pel_excl_status", start_time=D7, execution_status="FAILED",
             compute=_compute(warehouse_id="qh_pel_wh"), execution_duration_ms=1000),
        _row(statement_id="qh_pel_excl_cached", start_time=D7, from_result_cache=True,
             compute=_compute(warehouse_id="qh_pel_wh"), execution_duration_ms=1000),
        _row(statement_id="qh_pel_excl_zero", start_time=D7,
             compute=_compute(warehouse_id="qh_pel_wh"), execution_duration_ms=0),
    ]
    for sid, ts in [
        ("qh_pel_win_t7", D7), ("qh_pel_win_t30", D30), ("qh_pel_win_t90", D90),
        ("qh_pel_win_ttoday", DTODAY), ("qh_pel_win_told90", DOLD),
    ]:
        rows.append(_row(
            statement_id=sid, start_time=ts, compute=_compute(warehouse_id="qh_pel_winwh"),
            execution_duration_ms=500, waiting_for_compute_duration_ms=50,
            total_task_duration_ms=2000, read_bytes=5_000, total_duration_ms=600,
        ))
    return rows


# ---------------------------------------------------------------------------------------------
# query_provenance_by_source -- GROUP BY workspace_id, compute.type, compute.warehouse_id,
# source_kind, identity_type, executed_by, job_id, dashboard_id, notebook_id.
# One row per source_kind CASE branch (job > dashboard > legacy_dashboard > notebook > alert
# > genie > sql_editor > other).
# ---------------------------------------------------------------------------------------------
def _provenance_by_source_rows():
    rows = [
        _row(statement_id="qh_prov_job1", start_time=D7, executed_by="frank@example.com",
             compute=_compute(warehouse_id="qh_prov_wh"), read_bytes=1_000,
             query_source=_qsource(job_id="qh_prov_job_1", job_run_id="qh_prov_run_1")),
        _row(statement_id="qh_prov_dash1", start_time=D7, executed_by="frank@example.com",
             compute=_compute(warehouse_id="qh_prov_wh"), read_bytes=1_000,
             query_source=_qsource(dashboard_id="qh_prov_dash_1")),
        _row(statement_id="qh_prov_legacy1", start_time=D7, executed_by="frank@example.com",
             compute=_compute(warehouse_id="qh_prov_wh"), read_bytes=1_000,
             query_source=_qsource(legacy_dashboard_id="qh_prov_legacy_1")),
        _row(statement_id="qh_prov_nb1", start_time=D7, executed_by="frank@example.com",
             compute=_compute(warehouse_id="qh_prov_wh"), read_bytes=1_000,
             query_source=_qsource(notebook_id="qh_prov_nb_1")),
        _row(statement_id="qh_prov_alert1", start_time=D7, executed_by="frank@example.com",
             compute=_compute(warehouse_id="qh_prov_wh"), read_bytes=1_000,
             query_source=_qsource(alert_id="qh_prov_alert_1")),
        _row(statement_id="qh_prov_genie1", start_time=D7, executed_by="frank@example.com",
             compute=_compute(warehouse_id="qh_prov_wh"), read_bytes=1_000,
             query_source=_qsource(genie_space_id="qh_prov_genie_1")),
        _row(statement_id="qh_prov_sqlq1", start_time=D7, executed_by="frank@example.com",
             compute=_compute(warehouse_id="qh_prov_wh"), read_bytes=1_000,
             query_source=_qsource(sql_query_id="qh_prov_sqlq_1")),
        _row(statement_id="qh_prov_other1", start_time=D7, executed_by="frank@example.com",
             compute=_compute(warehouse_id="qh_prov_wh"), read_bytes=1_000,
             query_source=None),
    ]
    for sid, ts in [
        ("qh_prov_win_t7", D7), ("qh_prov_win_t30", D30), ("qh_prov_win_t90", D90),
        ("qh_prov_win_ttoday", DTODAY), ("qh_prov_win_told90", DOLD),
    ]:
        rows.append(_row(
            statement_id=sid, start_time=ts, executed_by="frank@example.com",
            compute=_compute(warehouse_id="qh_prov_winwh"), read_bytes=1_000,
            query_source=_qsource(job_id=sid),
        ))
    return rows


# ---------------------------------------------------------------------------------------------
# query_pruning_effectiveness -- GROUP BY date(start_time), workspace_id, compute.type,
# compute.warehouse_id, executed_by, statement_type. warn_prune_ratio=0.5, crit=0.2. WHERE
# excludes rows where pruned_files = 0 AND read_files = 0.
# ---------------------------------------------------------------------------------------------
def _pruning_effectiveness_rows():
    # read_bytes is above :min_read_bytes (1 GiB) on every scored row below so status actually
    # bands instead of every row reading NOT_ASSESSED for being too small to judge. qh_pe_crit has
    # a second flagged day (qh_pe_crit_recur) so days_flagged=2 clears :min_recurring_flagged_days
    # and the day stays CRITICAL instead of being capped to WARN as a one-off.
    rows = [
        _row(statement_id="qh_pe_crit", start_time=D7, compute=_compute(warehouse_id="qh_pe_wh_crit"),
             executed_by=None, pruned_files=1, read_files=9, read_partitions=2, read_bytes=2_000_000_000,
             read_rows=100),
        _row(statement_id="qh_pe_crit_recur", start_time=D30, compute=_compute(warehouse_id="qh_pe_wh_crit"),
             executed_by=None, pruned_files=1, read_files=9, read_partitions=2, read_bytes=2_000_000_000,
             read_rows=100),
        _row(statement_id="qh_pe_warn", start_time=D7, compute=_compute(warehouse_id="qh_pe_wh_warn"),
             executed_by=None, pruned_files=3, read_files=7, read_partitions=2, read_bytes=2_000_000_000,
             read_rows=200),
        _row(statement_id="qh_pe_ok", start_time=D7, compute=_compute(warehouse_id="qh_pe_wh_ok"),
             executed_by=None, pruned_files=8, read_files=2, read_partitions=1, read_bytes=2_000_000_000,
             read_rows=300),
        _row(statement_id="qh_pe_zero", start_time=D7, compute=_compute(warehouse_id="qh_pe_wh_zero"),
             executed_by=None, pruned_files=0, read_files=0, read_partitions=0, read_bytes=0,
             read_rows=0),
    ]
    for sid, ts in [
        ("qh_pe_win_t7", D7), ("qh_pe_win_t30", D30), ("qh_pe_win_t90", D90),
        ("qh_pe_win_ttoday", DTODAY), ("qh_pe_win_told90", DOLD),
    ]:
        rows.append(_row(
            statement_id=sid, start_time=ts, compute=_compute(warehouse_id="qh_pe_win"),
            executed_by=None, pruned_files=5, read_files=5, read_partitions=1, read_bytes=500,
            read_rows=50,
        ))
    return rows


# ---------------------------------------------------------------------------------------------
# query_queuing_waits -- GROUP BY date(start_time), workspace_id, compute.type,
# compute.warehouse_id. warn_queue_secs=60 (60000ms), crit_queue_secs=600 (600000ms).
# ---------------------------------------------------------------------------------------------
def _queuing_waits_rows():
    rows = [
        _row(statement_id="qh_qw_ok", start_time=D7, compute=_compute(warehouse_id="qh_qw_wh_ok"),
             waiting_at_capacity_duration_ms=10_000, waiting_for_compute_duration_ms=5_000),
        _row(statement_id="qh_qw_warn", start_time=D7, compute=_compute(warehouse_id="qh_qw_wh_warn"),
             waiting_at_capacity_duration_ms=50_000, waiting_for_compute_duration_ms=20_000),
        _row(statement_id="qh_qw_crit", start_time=D7, compute=_compute(warehouse_id="qh_qw_wh_crit"),
             waiting_at_capacity_duration_ms=400_000, waiting_for_compute_duration_ms=300_000),
        # T-75B: a (day, warehouse) group where BOTH wait buckets are NULL on every row (e.g. a
        # SERVERLESS_COMPUTE day that reports neither bucket at all) -- SUM(a) + SUM(b) is NULL,
        # which the OLD CASE fell through to a false OK on; the fixed CASE must read NOT_ASSESSED.
        _row(statement_id="qh_qw_unreported", start_time=D7,
             compute=_compute(warehouse_id="qh_qw_wh_unreported", ctype="SERVERLESS_COMPUTE"),
             waiting_at_capacity_duration_ms=None, waiting_for_compute_duration_ms=None),
        # T-75B review fix: only ONE bucket is NULL (waiting_at_capacity never reported), but the
        # OTHER bucket alone (900_000ms = 900s) clears crit_queue_secs=600 by itself. The banded
        # CASE checks CRITICAL/WARN on COALESCE(SUM,0)+COALESCE(SUM,0) BEFORE the NULL check, so
        # this must read CRITICAL, not NOT_ASSESSED (the earlier "either SUM is NULL ->
        # NOT_ASSESSED" ordering would have masked this real CRITICAL).
        _row(statement_id="qh_qw_partial_crit", start_time=D7,
             compute=_compute(warehouse_id="qh_qw_wh_partial_crit"),
             waiting_at_capacity_duration_ms=None, waiting_for_compute_duration_ms=900_000),
    ]
    for sid, ts in [
        ("qh_qw_win_t7", D7), ("qh_qw_win_t30", D30), ("qh_qw_win_t90", D90),
        ("qh_qw_win_ttoday", DTODAY), ("qh_qw_win_told90", DOLD),
    ]:
        rows.append(_row(
            statement_id=sid, start_time=ts, compute=_compute(warehouse_id="qh_qw_win"),
            waiting_at_capacity_duration_ms=1_000, waiting_for_compute_duration_ms=1_000,
        ))
    return rows


# ---------------------------------------------------------------------------------------------
# query_shuffle_write_amplification -- GROUP BY date(start_time), workspace_id, compute.type,
# compute.warehouse_id, executed_by, statement_type. warn_shuffle_gb=50 (5e10 B),
# crit_shuffle_gb=200 (2e11 B), warn_avg_file_mb=32 (3.2e7 B), crit_avg_file_mb=8 (8e6 B).
# ---------------------------------------------------------------------------------------------
def _shuffle_write_amplification_rows():
    rows = [
        _row(statement_id="qh_sw_shuffle_crit", start_time=D7,
             compute=_compute(warehouse_id="qh_sw_wh_shufflecrit"), executed_by=None,
             shuffle_read_bytes=250_000_000_000, written_bytes=0, written_rows=0, written_files=0,
             read_bytes=1000),
        _row(statement_id="qh_sw_shuffle_warn", start_time=D7,
             compute=_compute(warehouse_id="qh_sw_wh_shufflewarn"), executed_by=None,
             shuffle_read_bytes=60_000_000_000, written_bytes=0, written_rows=0, written_files=0,
             read_bytes=1000),
        _row(statement_id="qh_sw_ok", start_time=D7, compute=_compute(warehouse_id="qh_sw_wh_ok"),
             executed_by=None, shuffle_read_bytes=1_000_000_000, written_bytes=1_000_000_000,
             written_rows=10_000, written_files=10, read_bytes=1000),
        _row(statement_id="qh_sw_smallfile_crit", start_time=D7,
             compute=_compute(warehouse_id="qh_sw_wh_smallfilecrit"), executed_by=None,
             shuffle_read_bytes=0, written_bytes=5_000_000_000, written_rows=500_000,
             written_files=1000, read_bytes=1000),
        _row(statement_id="qh_sw_smallfile_warn", start_time=D7,
             compute=_compute(warehouse_id="qh_sw_wh_smallfilewarn"), executed_by=None,
             shuffle_read_bytes=0, written_bytes=2_000_000_000, written_rows=200_000,
             written_files=100, read_bytes=1000),
    ]
    for sid, ts in [
        ("qh_sw_win_t7", D7), ("qh_sw_win_t30", D30), ("qh_sw_win_t90", D90),
        ("qh_sw_win_ttoday", DTODAY), ("qh_sw_win_told90", DOLD),
    ]:
        rows.append(_row(
            statement_id=sid, start_time=ts, compute=_compute(warehouse_id="qh_sw_win"),
            executed_by=None, shuffle_read_bytes=1_000_000, written_bytes=0, written_rows=0,
            written_files=0, read_bytes=1000,
        ))
    return rows


# ---------------------------------------------------------------------------------------------
# query_workload_mix_hours -- GROUP BY date(start_time), workspace_id, hour(start_time),
# compute.type, compute.warehouse_id, statement_type, executed_by. No status.
# ---------------------------------------------------------------------------------------------
def _workload_mix_hours_rows():
    rows = [
        _row(statement_id="qh_wm_hour_early", start_time=D7.replace(hour=3, minute=0, second=0),
             compute=_compute(warehouse_id="qh_wm_hourwh"), executed_by=None,
             produced_rows=50, read_bytes=500),
        _row(statement_id="qh_wm_hour_late", start_time=D7.replace(hour=20, minute=0, second=0),
             compute=_compute(warehouse_id="qh_wm_hourwh"), executed_by=None,
             produced_rows=75, read_bytes=750),
        _row(statement_id="qh_wm_failed", start_time=D7, execution_status="FAILED",
             compute=_compute(warehouse_id="qh_wm_failedwh"), executed_by=None,
             produced_rows=0, read_bytes=100),
    ]
    for sid, ts in [
        ("qh_wm_win_t7", D7), ("qh_wm_win_t30", D30), ("qh_wm_win_t90", D90),
        ("qh_wm_win_ttoday", DTODAY), ("qh_wm_win_told90", DOLD),
    ]:
        rows.append(_row(
            statement_id=sid, start_time=ts, compute=_compute(warehouse_id="qh_wm_win"),
            executed_by=None, produced_rows=100, read_bytes=1000,
        ))
    return rows


# ---------------------------------------------------------------------------------------------
# Auto-discovered by build_fixtures.py (DEC-17).
# ---------------------------------------------------------------------------------------------
def build(con) -> None:
    all_rows = (
        _audit_self_cost_rows()
        + _cache_coldstart_rows()
        + _costly_statements_rows()
        + _costly_statements_grouped_rows()
        + _failed_queries_daily_rows()
        + _failed_statements_grouped_rows()
        + _local_spillage_rows()
        + _per_query_estimate_lane_rows()
        + _provenance_by_source_rows()
        + _pruning_effectiveness_rows()
        + _queuing_waits_rows()
        + _shuffle_write_amplification_rows()
        + _workload_mix_hours_rows()
    )
    seen = set()
    for r in all_rows:
        sid = r["statement_id"]
        if sid in seen:
            raise ValueError(f"duplicate statement_id in query_history.py fixture: {sid!r}")
        seen.add(sid)

    cols_sql = ", ".join(f'"{c}"' for c in QH_COLUMNS)
    placeholders = ", ".join("?" for _ in QH_COLUMNS)
    insert_sql = f'INSERT INTO query__history ({cols_sql}) VALUES ({placeholders})'
    params = [[r[c] for c in QH_COLUMNS] for r in all_rows]
    con.executemany(insert_sql, params)
