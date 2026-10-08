"""tests/fixtures/idle_waste.py -- P4-01-W1/W2 (tasks/P4-WASTE-SPEC.md section 6), fixture builder
for the app-owned findings `compute_warehouse_idle_minutes` (app/queries/app/compute/, W1) and
`cost_failed_statement_waste` (app/queries/app/cost/, W2b), and for the seven `iw_job_*` jobs the
in-place-fixed vendored `lakeflow_failed_jobs_wasted_dbus` (W2a) reads.

Every id this builder writes is prefixed `iw_` (no other builder uses that prefix; DEC-15). Its
own workspace is IW_WS = "iw_ws8", account `iw_acct`, cloud "aws" (lowercase, as every other
builder uses). One `access__workspaces_latest` row names IW_WS (chargeback.py's / waste_usd.py's
own identical precedent -- see their module docstrings), satisfying tests/test_api.py's "every
real workspace has a name" invariant. No `lakeflow__jobs` rows are written, so no hygiene or
zombie query picks these jobs up.

`D(n)` / `DT(n, h, m, s)` are relative to `base.AS_OF` (2026-09-21 12:00), like every other
builder's own convention: D(0) = 2026-09-21 (today, excluded from every window), D(1) =
2026-09-20, D(2) = 2026-09-19, D(3) = 2026-09-18, D(7) = 2026-09-14, D(8) = 2026-09-13.

Struct defaults (`_usage_metadata`, `_identity_metadata`, `_product_features`) are copied from
tests/fixtures/waste_usd.py's own shape (same DDL, same field set), not imported, per the
per-builder isolation model (tests/fixtures/build_fixtures.py's own module docstring).

Every `query__history` row this builder writes: compute {type: 'WAREHOUSE', cluster_id: NULL,
warehouse_id}, statement_type 'SELECT', statement_text 'SELECT 1 /* iw */', executed_by
`iw_analyst@example.com`, update_time = end_time, query_source NULL, from_result_cache FALSE.
`total_duration_ms = execution_duration_ms = 50` on every row (waits, compilation, spill and
bytes 0) -- compute_warehouse_idle_minutes reads only start_time/end_time/update_time for the
busy-island computation, never the duration columns, so this fixed, deliberately tiny value has
no effect on this query's own arithmetic. It exists only so these statements sort to the bottom
of every OTHER ranked performance finding that reads system.query.history account-wide (the same
convention tests/fixtures/pressure_warehouses.py's own module docstring documents), so this
builder can never shift an assertion in a test file it does not own.

Section 6.1's scenarios (window_days = 30, header defaults: idle_gap_seconds 60, min_idle_minutes
30, warn_idle_pct 30, crit_idle_pct 60) -- see tests/test_findings/test_compute_warehouse_idle_
minutes.py for the full expected-row table and the arithmetic behind each figure:

  A `iw_wh_pro`      a 60 s lead gap (not counted: exactly at the threshold), two 30 s gaps (not
                     counted), a 120 s gap (counted), overlapping statements a4/a5 merged into one
                     busy island, a 900 s gap, a 40-min tail before auto-stop -> CRITICAL.
  B `iw_wh_multi`     a 2-cluster scale-up mid-run, proving idle dollars are weighted by clusters
                     running during the gap, not by wall-clock minutes alone -> WARN.
  C `iw_wh_sls`       serverless; a STARTING/RUNNING tie at the same instant (RUNNING wins by
                     state_rank); a 90 s gap that a 120 s idle_gap_seconds override drops -> CRITICAL.
  D `iw_wh_noev`      billed, but no warehouse_events row at all -> NOT_ASSESSED no_warehouse_events.
  E `iw_wh_quiet`     running the whole window with no statement at all -> all idle; no STARTING
                     event (start_state_known FALSE); billed usage before the first event's hour is
                     excluded from the rate -> CRITICAL.
  F `iw_wh_open`      still running at the window's end (open_at_window_end TRUE); its STOPPED
                     event and its second billing bucket both fall on D(0) (today) and are excluded
                     by the window's own filters -> OK (20 counted min, below min_idle_minutes 30).
  G `iw_wh_carry`     no in-window event at all; the state carried in from the last event before the
                     window (a STARTING/RUNNING tie at D(8), RUNNING wins) keeps it running the
                     whole window with no query -> CRITICAL. At window_days = 7 the same carried-in
                     state starts the window RUNNING from D(7) 00:00.
  H `iw_wh_nostart`   billed, with events, but its only in-window events are STOPPING/STOPPED (no
                     running time is ever measured) -> NOT_ASSESSED no_running_time_measured.

The dollar bands (warn_waste_usd 20, crit_waste_usd 200), each warehouse up D(3) 10:00-18:00:

  I1 `iw_wh_busy`     25% idle (below warn_idle_pct), $40 of possible waste -> WARN.
  I2 `iw_wh_big`      12.5% idle, $264 of possible waste -> CRITICAL.
  I3 `iw_wh_unpriced` I2's shape and DBUs on a SKU with no list price: no dollar figure, so it is
                     judged on share alone -> OK.

`window_days = 0`: no rows (a windowed query).

Section 6.3's scenarios (`lakeflow__job_run_timeline` + `billing__usage` on iw_JOBS_COMPUTE,
0.30 $/DBU, D(3) unless stated) -- see tests/test_findings/test_failed_run_waste.py:

  J1 `iw_job_mixed`     2 of 5 runs failed (the more recent, DRIVER_ERROR, is last_failed_*), 2
                        succeeded, 1 cancelled (shown, not counted); plus job-only usage with no
                        run id (est_unattributed_usd_list) -> WARN.
  J2 `iw_job_repair`    one run, two attempts sharing a run id -- FAILED (1800 s) then SUCCEEDED
                        (600 s); billing tags both attempts alike, so the 0.75 failed share must
                        come from job_run_timeline wall-clock time, not from billing -> WARN.
  J3 `iw_job_broken`    4 of 4 runs FAILED; one has no billed usage at all (failed_runs_unbilled)
                        -> CRITICAL.
  J4 `iw_job_norunid`   2 of 2 runs FAILED, but the job's only usage carries no run id at all ->
                        NOT_ASSESSED no_run_id_in_billing.
  J5 `iw_job_cancelonly` cancelled + succeeded only, no failed or repaired run -> no row at all.
  J6 `iw_job_inflight`  (D(2)) a repair still in flight (no end row on its last attempt) -> no row
                        at all, at any window_days.
  J7 `iw_job_nobill`    1 of 1 runs FAILED, no billed usage for the job at all -> NOT_ASSESSED
                        no_billing_rows.

Section 6.4's scenarios (`query__history` + `billing__attributed_usage` on iw_SQL_PRO, D(2)) --
see tests/test_findings/test_cost_failed_statement_waste.py. Neither warehouse has
warehouse_events or billing.usage rows, so neither appears in compute_warehouse_idle_minutes:

  `iw_wh_fail`          2 of 4 statements FAILED, one of them (iw_st_f1) with a priced
                        attributed_usage row, the other (iw_st_f2) with none; one CANCELED
                        statement is shown and not counted -> WARN.
  `iw_wh_fail_noattr`   2 FAILED statements, no attributed_usage and no billing row for the
                        warehouse -> NOT_ASSESSED no_billing_rows.
  `iw_hf_wh`            no attributed_usage, but billed hours: each hour split across its
                        statements by task time -> WARN, cost_basis run_time_share (its own
                        prefix keeps it out of the idle-minutes scenarios).

`window_days = 0`: no rows for either W2 finding (both are windowed queries).

Stdlib + duckdb only.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from datetime import time as dtime

import duckdb

from base import AS_OF, write_parquet  # noqa: F401 (write_parquet re-exported for build_fixtures.py)

D0 = AS_OF.date()

IW_WS = "iw_ws8"
ACCOUNT_ID = "iw_acct"


def D(n: int) -> date:
    """D(n) = D0 - n days (a date). D(0) is today -- excluded from every window."""
    return D0 - timedelta(days=n)


def DT(n: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    """A full datetime at D(n) with the given time-of-day -- AS_OF-relative, never an absolute
    literal (matching every other builder's own D()/DT() convention)."""
    return datetime.combine(D(n), dtime(hour, minute, second))


# =================================================================================================
# STRUCT defaults for system.billing.usage -- duplicated from tests/fixtures/waste_usd.py's own
# _usage_metadata()/_identity_metadata()/_product_features() (same DDL, same field set) rather
# than imported, per the per-builder isolation model.
# =================================================================================================
def _usage_metadata(**over):
    base = {
        "cluster_id": None, "job_id": None, "warehouse_id": None, "instance_pool_id": None,
        "node_type": None, "job_run_id": None, "notebook_id": None, "dlt_pipeline_id": None,
        "endpoint_name": None, "endpoint_id": None, "dlt_update_id": None, "dlt_maintenance_id": None,
        "run_name": None, "job_name": None, "notebook_path": None, "central_clean_room_id": None,
        "source_region": None, "destination_region": None, "app_id": None, "app_name": None,
        "metastore_id": None, "private_endpoint_name": None, "storage_api_type": None,
        "budget_policy_id": None, "ai_runtime_pool_id": None, "catalog_id": None,
        "networking_client": None, "recipient_id": None, "usage_policy_id": None,
    }
    base.update(over)
    return base


def _identity_metadata(**over):
    base = {"run_as": None, "created_by": None, "owned_by": None, "run_by": None}
    base.update(over)
    return base


def _product_features(**over):
    base = {
        "jobs_tier": None, "sql_tier": None, "dlt_tier": None,
        "is_serverless": False, "is_photon": False, "serving_type": None,
        "networking": {"connectivity_type": None},
        "ai_runtime": {"compute_type": None},
        "model_serving": {"offering_type": None},
        "ai_gateway": {"feature_type": None},
        "serverless_gpu": {"workload_type": None},
        "agent_bricks": {"problem_type": None, "workload_type": None},
        "performance_target": None,
        "ai_functions": {"ai_function": None},
        "apps": {"compute_size": None},
        "lakeflow_connect": {"task_type": None, "zerobus_request_type": None},
        "lakebase": {"storage_type": None, "compute_type": None},
        "ai_bi_genie": {"capability_type": None, "offering_type": None},
        "genie": {"offering_type": None},
    }
    base.update(over)
    return base


# =================================================================================================
# billing.usage / billing.list_prices
# =================================================================================================
_USAGE_SQL = (
    "INSERT INTO billing__usage (account_id, workspace_id, record_id, sku_name, cloud, "
    "usage_start_time, usage_end_time, usage_date, custom_tags, usage_unit, usage_quantity, "
    "usage_metadata, identity_metadata, record_type, ingestion_date, billing_origin_product, "
    "product_features, usage_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def billing_usage(
    con: duckdb.DuckDBPyConnection,
    record_id: str,
    *,
    warehouse_id: str,
    sku_name: str,
    usage_date_: date,
    hour: int,
    usage_quantity: float,
    serverless: bool = False,
) -> None:
    start = datetime.combine(usage_date_, dtime(hour, 0, 0))
    end = start + timedelta(hours=1)
    con.execute(
        _USAGE_SQL,
        [
            ACCOUNT_ID, IW_WS, record_id, sku_name, "aws", start, end, usage_date_,
            {}, "DBU", usage_quantity,
            _usage_metadata(warehouse_id=warehouse_id),
            _identity_metadata(),
            "ORIGINAL", usage_date_, "SQL",
            _product_features(is_serverless=serverless),
            "COMPUTE_TIME",
        ],
    )


_LP_SQL = (
    "INSERT INTO billing__list_prices (account_id, price_start_time, price_end_time, sku_name, "
    "cloud, currency_code, usage_unit, pricing) VALUES (?,?,?,?,?,?,?,?)"
)


def list_price(con: duckdb.DuckDBPyConnection, sku_name: str, rate: float) -> None:
    con.execute(
        _LP_SQL,
        [
            ACCOUNT_ID, datetime(2020, 1, 1), None, sku_name, "aws", "USD", "DBU",
            {"default": rate, "promotional": {"default": None}, "effective_list": {"default": rate}},
        ],
    )


# =================================================================================================
# compute.warehouses (SCD2, one current row each) / compute.warehouse_events
# =================================================================================================
_WH_SQL = (
    "INSERT INTO compute__warehouses "
    "(warehouse_id, workspace_id, account_id, warehouse_name, warehouse_type, "
    "warehouse_channel, warehouse_size, min_clusters, max_clusters, auto_stop_minutes, tags, "
    "change_time, delete_time, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def warehouse(
    con: duckdb.DuckDBPyConnection, warehouse_id: str, *, wtype: str, size: str,
    min_clusters: int, max_clusters: int, auto_stop: int,
) -> None:
    con.execute(
        _WH_SQL,
        [
            warehouse_id, IW_WS, ACCOUNT_ID, warehouse_id, wtype, "CHANNEL_NAME_CURRENT", size,
            min_clusters, max_clusters, auto_stop, {}, datetime(2026, 8, 1), None,
            "iw_owner@example.com",
        ],
    )


_WE_SQL = (
    "INSERT INTO compute__warehouse_events "
    "(account_id, workspace_id, warehouse_id, event_type, cluster_count, event_time) "
    "VALUES (?,?,?,?,?,?)"
)


def we(con: duckdb.DuckDBPyConnection, warehouse_id: str, event_type: str, event_time: datetime,
       cluster_count: int) -> None:
    con.execute(_WE_SQL, [ACCOUNT_ID, IW_WS, warehouse_id, event_type, cluster_count, event_time])


# =================================================================================================
# query.history -- one statement row per (statement_id, warehouse_id, start, end).
# =================================================================================================
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

_QH_COLS_SQL = ", ".join('"' + c + '"' for c in QH_COLUMNS)
_QH_PLACEHOLDERS = ", ".join("?" for _ in QH_COLUMNS)
_QH_INSERT = f"INSERT INTO query__history ({_QH_COLS_SQL}) VALUES ({_QH_PLACEHOLDERS})"


def stmt(con: duckdb.DuckDBPyConnection, statement_id: str, warehouse_id: str, start: datetime,
         end: datetime, *, execution_status: str = "FINISHED", task_ms: int | None = None) -> None:
    row = {c: None for c in QH_COLUMNS}
    row.update({
        "account_id": ACCOUNT_ID,
        "workspace_id": IW_WS,
        "statement_id": statement_id,
        "executed_by": "iw_analyst@example.com",
        "session_id": f"iw_sess_{warehouse_id}",
        "execution_status": execution_status,
        "compute": {"type": "WAREHOUSE", "cluster_id": None, "warehouse_id": warehouse_id},
        "statement_text": "SELECT 1 /* iw */",
        "statement_type": "SELECT",
        # W2b (P4-01-W2): a FAILED statement carries the one error_message the module docstring
        # promises; every other status leaves it NULL.
        "error_message": "iw statement failed" if execution_status == "FAILED" else None,
        "total_duration_ms": 50,
        "waiting_for_compute_duration_ms": 0,
        "waiting_at_capacity_duration_ms": 0,
        "execution_duration_ms": 50,
        "compilation_duration_ms": 0,
        "total_task_duration_ms": task_ms,
        "start_time": start,
        "end_time": end,
        "update_time": end,
        "read_bytes": 0,
        "spilled_local_bytes": 0,
        "shuffle_read_bytes": 0,
        "from_result_cache": False,
    })
    con.execute(_QH_INSERT, [row[c] for c in QH_COLUMNS])


# =================================================================================================
# P4-01-W2 -- lakeflow.job_run_timeline (W2a) and billing.attributed_usage (W2b) helpers.
# =================================================================================================
_JRT_SQL = (
    "INSERT INTO lakeflow__job_run_timeline (account_id, workspace_id, job_id, run_id, "
    "period_start_time, period_end_time, trigger_type, result_state, run_type, run_name, "
    "compute_ids, termination_code, job_parameters, source_task_run_id, root_task_run_id, "
    "compute, termination_type, setup_duration_seconds, queue_duration_seconds, "
    "run_duration_seconds, cleanup_duration_seconds, execution_duration_seconds) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def jrt(con: duckdb.DuckDBPyConnection, job_id: str, run_id: str, start: datetime, end: datetime,
        *, result_state: str | None, termination_code: str | None = None) -> None:
    """One job_run_timeline slice. `result_state IS NOT NULL` marks it an end row (section 3.1's
    'Attempts'); a slice with `result_state=None` (J6) is still in flight. termination_type is
    INTERNAL_ERROR on a failure and NULL otherwise (section 6.3's own convention); the five
    *_duration_seconds columns are 0 throughout -- lakeflow_failed_jobs_wasted_dbus computes every
    slice's own seconds from period_start_time/period_end_time, never these columns."""
    termination_type = "INTERNAL_ERROR" if result_state in ("FAILED", "ERROR", "TIMED_OUT") else None
    con.execute(
        _JRT_SQL,
        [
            ACCOUNT_ID, IW_WS, job_id, run_id, start, end, "PERIODIC", result_state, "JOB_RUN",
            None, None, termination_code, {}, None, None, None, termination_type,
            0, 0, 0, 0, 0,
        ],
    )


def job_usage(con: duckdb.DuckDBPyConnection, record_id: str, *, job_id: str,
              job_run_id: str | None = None, sku_name: str, usage_date_: date, hour: int,
              usage_quantity: float) -> None:
    """A system.billing.usage row attributed to a job (usage_metadata.job_id), and to one of its
    runs when `job_run_id` is given -- usage with a job_id but no job_run_id is what
    lakeflow_failed_jobs_wasted_dbus reports as est_unattributed_usd_list (section 3.1)."""
    start = datetime.combine(usage_date_, dtime(hour, 0, 0))
    end = start + timedelta(hours=1)
    con.execute(
        _USAGE_SQL,
        [
            ACCOUNT_ID, IW_WS, record_id, sku_name, "aws", start, end, usage_date_,
            {}, "DBU", usage_quantity,
            _usage_metadata(job_id=job_id, job_run_id=job_run_id),
            _identity_metadata(),
            "ORIGINAL", usage_date_, "JOBS",
            _product_features(is_serverless=False),
            "COMPUTE_TIME",
        ],
    )


_AU_SQL = (
    "INSERT INTO billing__attributed_usage (record_id, usage_metadata, identity_metadata, "
    "start_time, end_time, usage_date, usage_unit, active_usage_quantity, granular_tags, "
    "usage_record_ids, account_id, workspace_id, sku_name, cloud, billing_origin_product, "
    "custom_tags) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def attributed(con: duckdb.DuckDBPyConnection, record_id: str, *, statement_id: str,
               warehouse_id: str, start: datetime, end: datetime, active_usage_quantity: float,
               sku_name: str = "iw_SQL_PRO") -> None:
    """A system.billing.attributed_usage row for one SQL statement -- section 6.4: start/end are
    the STATEMENT's own times (not an hour bucket), usage_date is start's date."""
    con.execute(
        _AU_SQL,
        [
            record_id,
            {"dbsql_statement_id": statement_id, "warehouse_id": warehouse_id, "client_application": None},
            {"executed_by": "iw_analyst@example.com"},
            start, end, start.date(), "DBU", active_usage_quantity,
            {"query_tags": {}}, [],
            ACCOUNT_ID, IW_WS, sku_name, "aws", "SQL", {},
        ],
    )


_WS_SQL = (
    "INSERT INTO access__workspaces_latest "
    "(account_id, workspace_id, workspace_name, workspace_url, create_time, status) "
    "VALUES (?,?,?,?,?,?)"
)


# =================================================================================================
# Section 6.1 scenarios.
# =================================================================================================
def _write_A(con: duckdb.DuckDBPyConnection) -> None:
    wh = "iw_wh_pro"
    we(con, wh, "STARTING", DT(2, 9, 0, 0), 0)
    we(con, wh, "RUNNING", DT(2, 9, 1, 0), 1)
    we(con, wh, "STOPPING", DT(2, 10, 31, 0), 0)
    we(con, wh, "STOPPED", DT(2, 10, 32, 0), 0)
    stmt(con, "iw_st_a1", wh, DT(2, 9, 2, 0), DT(2, 9, 5, 0))
    stmt(con, "iw_st_a2", wh, DT(2, 9, 5, 30), DT(2, 9, 8, 0))
    stmt(con, "iw_st_a3", wh, DT(2, 9, 8, 30), DT(2, 9, 10, 0))
    stmt(con, "iw_st_a4", wh, DT(2, 9, 12, 0), DT(2, 9, 20, 0))
    stmt(con, "iw_st_a5", wh, DT(2, 9, 15, 0), DT(2, 9, 25, 0))
    stmt(con, "iw_st_a6", wh, DT(2, 9, 40, 0), DT(2, 9, 51, 0))
    billing_usage(con, "iw_bl_a1", warehouse_id=wh, sku_name="iw_SQL_PRO", usage_date_=D(2), hour=9, usage_quantity=13.0)
    billing_usage(con, "iw_bl_a2", warehouse_id=wh, sku_name="iw_SQL_PRO", usage_date_=D(2), hour=10, usage_quantity=5.2)


def _write_B(con: duckdb.DuckDBPyConnection) -> None:
    wh = "iw_wh_multi"
    we(con, wh, "STARTING", DT(2, 13, 0, 0), 0)
    we(con, wh, "RUNNING", DT(2, 13, 1, 0), 1)
    we(con, wh, "SCALED_UP", DT(2, 13, 10, 0), 2)
    we(con, wh, "SCALED_DOWN", DT(2, 13, 30, 0), 1)
    we(con, wh, "STOPPING", DT(2, 14, 21, 0), 0)
    we(con, wh, "STOPPED", DT(2, 14, 22, 0), 0)
    stmt(con, "iw_st_b1", wh, DT(2, 13, 1, 0), DT(2, 13, 41, 0))
    billing_usage(con, "iw_bl_b1", warehouse_id=wh, sku_name="iw_SQL_PRO", usage_date_=D(2), hour=13, usage_quantity=16.0)
    billing_usage(con, "iw_bl_b2", warehouse_id=wh, sku_name="iw_SQL_PRO", usage_date_=D(2), hour=14, usage_quantity=4.2)


def _write_C(con: duckdb.DuckDBPyConnection) -> None:
    wh = "iw_wh_sls"
    we(con, wh, "STARTING", DT(2, 15, 0, 0), 0)
    we(con, wh, "RUNNING", DT(2, 15, 0, 0), 1)     # same instant as STARTING -- RUNNING wins (state_rank)
    we(con, wh, "STOPPING", DT(2, 15, 50, 0), 0)
    we(con, wh, "STOPPED", DT(2, 15, 50, 10), 0)
    stmt(con, "iw_st_c1", wh, DT(2, 15, 0, 0), DT(2, 15, 5, 0))
    stmt(con, "iw_st_c2", wh, DT(2, 15, 6, 30), DT(2, 15, 10, 30))
    stmt(con, "iw_st_c3", wh, DT(2, 15, 20, 0), DT(2, 15, 25, 0))
    billing_usage(con, "iw_bl_c1", warehouse_id=wh, sku_name="iw_SERVERLESS_SQL", usage_date_=D(2), hour=15,
                  usage_quantity=6.0, serverless=True)


def _write_D(con: duckdb.DuckDBPyConnection) -> None:
    wh = "iw_wh_noev"
    billing_usage(con, "iw_bl_d1", warehouse_id=wh, sku_name="iw_SQL_PRO", usage_date_=D(2), hour=8, usage_quantity=2.0)


def _write_E(con: duckdb.DuckDBPyConnection) -> None:
    wh = "iw_wh_quiet"
    we(con, wh, "RUNNING", DT(3, 8, 0, 0), 1)
    we(con, wh, "STOPPED", DT(3, 9, 0, 0), 0)
    billing_usage(con, "iw_bl_e1", warehouse_id=wh, sku_name="iw_SQL_PRO", usage_date_=D(3), hour=7, usage_quantity=2.0)
    billing_usage(con, "iw_bl_e2", warehouse_id=wh, sku_name="iw_SQL_PRO", usage_date_=D(3), hour=8, usage_quantity=8.0)


def _write_F(con: duckdb.DuckDBPyConnection) -> None:
    wh = "iw_wh_open"
    we(con, wh, "STARTING", DT(1, 23, 29, 0), 0)
    we(con, wh, "RUNNING", DT(1, 23, 30, 0), 1)
    # This STOPPED event lands on D(0) (today, 00:10): event_time < current_date() excludes it, so
    # the RUNNING period the events above open stays open at the window's end (open_at_window_end).
    we(con, wh, "STOPPED", DT(0, 0, 10, 0), 0)
    stmt(con, "iw_st_o1", wh, DT(1, 23, 31, 0), DT(1, 23, 40, 0))
    billing_usage(con, "iw_bl_f1", warehouse_id=wh, sku_name="iw_SQL_PRO", usage_date_=D(1), hour=23, usage_quantity=1.0)
    # D(0) (today) billing: excluded by usage_date < current_date().
    billing_usage(con, "iw_bl_f2", warehouse_id=wh, sku_name="iw_SQL_PRO", usage_date_=D(0), hour=0, usage_quantity=1.0)


def _write_G(con: duckdb.DuckDBPyConnection) -> None:
    wh = "iw_wh_carry"
    we(con, wh, "STARTING", DT(8, 22, 0, 0), 0)
    we(con, wh, "RUNNING", DT(8, 22, 0, 0), 1)     # same instant as STARTING -- RUNNING wins
    we(con, wh, "STOPPING", DT(7, 1, 0, 0), 0)
    we(con, wh, "STOPPED", DT(7, 1, 1, 0), 0)
    billing_usage(con, "iw_bl_g1", warehouse_id=wh, sku_name="iw_SQL_PRO", usage_date_=D(8), hour=22, usage_quantity=1.0)
    billing_usage(con, "iw_bl_g2", warehouse_id=wh, sku_name="iw_SQL_PRO", usage_date_=D(8), hour=23, usage_quantity=1.0)
    billing_usage(con, "iw_bl_g3", warehouse_id=wh, sku_name="iw_SQL_PRO", usage_date_=D(7), hour=0, usage_quantity=1.0)


def _write_H(con: duckdb.DuckDBPyConnection) -> None:
    wh = "iw_wh_nostart"
    we(con, wh, "STOPPING", DT(2, 3, 0, 0), 0)
    we(con, wh, "STOPPED", DT(2, 3, 1, 0), 0)
    billing_usage(con, "iw_bl_h1", warehouse_id=wh, sku_name="iw_SQL_PRO", usage_date_=D(2), hour=2, usage_quantity=1.0)


def _run_8h(con: duckdb.DuckDBPyConnection, wh: str, sku_name: str, dbu_per_hour: float) -> None:
    """Up and running D(3) 10:00-18:00 (a zero-length STARTING), billed every hour at one rate."""
    we(con, wh, "STARTING", DT(3, 10, 0, 0), 0)
    we(con, wh, "RUNNING", DT(3, 10, 0, 0), 1)
    we(con, wh, "STOPPING", DT(3, 18, 0, 0), 0)
    we(con, wh, "STOPPED", DT(3, 18, 1, 0), 0)
    for hour in range(10, 18):
        billing_usage(con, f"iw_bl_{wh.removeprefix('iw_wh_')}_{hour}", warehouse_id=wh,
                      sku_name=sku_name, usage_date_=D(3), hour=hour, usage_quantity=dbu_per_hour)


def _write_I1(con: duckdb.DuckDBPyConnection) -> None:
    wh = "iw_wh_busy"
    _run_8h(con, wh, "iw_SQL_PRO", 40.0)
    stmt(con, "iw_st_i1", wh, DT(3, 10, 0, 0), DT(3, 12, 0, 0))
    stmt(con, "iw_st_i2", wh, DT(3, 13, 0, 0), DT(3, 17, 0, 0))


def _write_I2(con: duckdb.DuckDBPyConnection) -> None:
    wh = "iw_wh_big"
    _run_8h(con, wh, "iw_SQL_PRO", 528.0)
    stmt(con, "iw_st_i3", wh, DT(3, 10, 0, 0), DT(3, 17, 0, 0))


def _write_I3(con: duckdb.DuckDBPyConnection) -> None:
    wh = "iw_wh_unpriced"
    _run_8h(con, wh, "iw_SQL_UNPRICED", 528.0)
    stmt(con, "iw_st_i4", wh, DT(3, 10, 0, 0), DT(3, 17, 0, 0))


_WAREHOUSES = [
    # (warehouse_id, type, size, min, max, auto_stop)
    ("iw_wh_pro", "PRO", "X_SMALL", 1, 1, 40),
    ("iw_wh_multi", "PRO", "SMALL", 1, 2, 50),
    ("iw_wh_sls", "SERVERLESS", "SMALL", 1, 1, 25),
    ("iw_wh_noev", "PRO", "X_SMALL", 1, 1, 10),
    ("iw_wh_quiet", "PRO", "X_SMALL", 1, 1, 60),
    ("iw_wh_open", "PRO", "X_SMALL", 1, 1, 30),
    ("iw_wh_carry", "PRO", "X_SMALL", 1, 1, 180),
    ("iw_wh_nostart", "PRO", "X_SMALL", 1, 1, 10),
    ("iw_wh_busy", "PRO", "LARGE", 1, 1, 60),
    ("iw_wh_big", "PRO", "4X_LARGE", 1, 1, 60),
    ("iw_wh_unpriced", "PRO", "4X_LARGE", 1, 1, 60),
]


# =================================================================================================
# Section 6.3 (P4-01-W2) -- lakeflow_failed_jobs_wasted_dbus (W2a). Seven jobs, all on D(3) except
# J6 (D(2)); run ids are "<job>_r<n>". iw_JOBS_COMPUTE is priced at 0.30 $/DBU (see build()).
# =================================================================================================
def _write_J1(con: duckdb.DuckDBPyConnection) -> None:
    """iw_job_mixed: 2 failed (r1, r2 -- the most recent failure, DRIVER_ERROR), 2 succeeded,
    1 cancelled (shown, not counted); plus job-only usage with no run id at all
    (est_unattributed_usd_list)."""
    job = "iw_job_mixed"
    jrt(con, job, f"{job}_r1", DT(3, 1, 0, 0), DT(3, 1, 10, 0), result_state="FAILED", termination_code="RUN_EXECUTION_ERROR")
    jrt(con, job, f"{job}_r2", DT(3, 2, 0, 0), DT(3, 2, 10, 0), result_state="FAILED", termination_code="DRIVER_ERROR")
    jrt(con, job, f"{job}_r3", DT(3, 3, 0, 0), DT(3, 3, 10, 0), result_state="SUCCEEDED")
    jrt(con, job, f"{job}_r4", DT(3, 4, 0, 0), DT(3, 4, 10, 0), result_state="SUCCEEDED")
    jrt(con, job, f"{job}_r5", DT(3, 5, 0, 0), DT(3, 5, 10, 0), result_state="CANCELLED")
    job_usage(con, "iw_ju_mixed_r1", job_id=job, job_run_id=f"{job}_r1", sku_name="iw_JOBS_COMPUTE", usage_date_=D(3), hour=1, usage_quantity=10.0)
    job_usage(con, "iw_ju_mixed_r2", job_id=job, job_run_id=f"{job}_r2", sku_name="iw_JOBS_COMPUTE", usage_date_=D(3), hour=2, usage_quantity=20.0)
    job_usage(con, "iw_ju_mixed_r3", job_id=job, job_run_id=f"{job}_r3", sku_name="iw_JOBS_COMPUTE", usage_date_=D(3), hour=3, usage_quantity=30.0)
    job_usage(con, "iw_ju_mixed_r4", job_id=job, job_run_id=f"{job}_r4", sku_name="iw_JOBS_COMPUTE", usage_date_=D(3), hour=4, usage_quantity=40.0)
    job_usage(con, "iw_ju_mixed_r5", job_id=job, job_run_id=f"{job}_r5", sku_name="iw_JOBS_COMPUTE", usage_date_=D(3), hour=5, usage_quantity=5.0)
    job_usage(con, "iw_ju_mixed_norun", job_id=job, job_run_id=None, sku_name="iw_JOBS_COMPUTE", usage_date_=D(3), hour=6, usage_quantity=5.0)


def _write_J2(con: duckdb.DuckDBPyConnection) -> None:
    """iw_job_repair: one run, two attempts sharing run_id -- attempt 1 (1800 s) FAILED, attempt 2
    (600 s) SUCCEEDED. Billing tags both attempts with the same run id, so the failed share
    (0.75) must come from job_run_timeline wall-clock time, not from billing alone."""
    job = "iw_job_repair"
    run = f"{job}_r1"
    jrt(con, job, run, DT(3, 10, 0, 0), DT(3, 10, 30, 0), result_state="FAILED", termination_code="RUN_EXECUTION_ERROR")
    jrt(con, job, run, DT(3, 12, 0, 0), DT(3, 12, 10, 0), result_state="SUCCEEDED")
    job_usage(con, "iw_ju_repair_1", job_id=job, job_run_id=run, sku_name="iw_JOBS_COMPUTE", usage_date_=D(3), hour=10, usage_quantity=75.0)
    job_usage(con, "iw_ju_repair_2", job_id=job, job_run_id=run, sku_name="iw_JOBS_COMPUTE", usage_date_=D(3), hour=12, usage_quantity=25.0)


def _write_J3(con: duckdb.DuckDBPyConnection) -> None:
    """iw_job_broken: 4 of 4 runs FAILED; r4 has no billed usage at all (failed_runs_unbilled)."""
    job = "iw_job_broken"
    jrt(con, job, f"{job}_r1", DT(3, 6, 0, 0), DT(3, 6, 10, 0), result_state="FAILED", termination_code="CLUSTER_ERROR")
    jrt(con, job, f"{job}_r2", DT(3, 6, 20, 0), DT(3, 6, 30, 0), result_state="FAILED", termination_code="CLUSTER_ERROR")
    jrt(con, job, f"{job}_r3", DT(3, 6, 40, 0), DT(3, 6, 50, 0), result_state="FAILED", termination_code="CLUSTER_ERROR")
    jrt(con, job, f"{job}_r4", DT(3, 7, 30, 0), DT(3, 7, 31, 0), result_state="FAILED", termination_code="WORKSPACE_RUN_LIMIT_EXCEEDED")
    job_usage(con, "iw_ju_broken_r1", job_id=job, job_run_id=f"{job}_r1", sku_name="iw_JOBS_COMPUTE", usage_date_=D(3), hour=6, usage_quantity=50.0)
    job_usage(con, "iw_ju_broken_r2", job_id=job, job_run_id=f"{job}_r2", sku_name="iw_JOBS_COMPUTE", usage_date_=D(3), hour=6, usage_quantity=50.0)
    job_usage(con, "iw_ju_broken_r3", job_id=job, job_run_id=f"{job}_r3", sku_name="iw_JOBS_COMPUTE", usage_date_=D(3), hour=6, usage_quantity=50.0)
    # r4: FAILED, no usage row -- proves failed_runs_unbilled.


def _write_J4(con: duckdb.DuckDBPyConnection) -> None:
    """iw_job_norunid: 2 of 2 runs FAILED, but the job's only usage carries job_id with no run id
    at all -- reads NOT_ASSESSED no_run_id_in_billing."""
    job = "iw_job_norunid"
    jrt(con, job, f"{job}_r1", DT(3, 9, 0, 0), DT(3, 9, 10, 0), result_state="FAILED", termination_code="RUN_EXECUTION_ERROR")
    jrt(con, job, f"{job}_r2", DT(3, 9, 30, 0), DT(3, 9, 40, 0), result_state="FAILED", termination_code="RUN_EXECUTION_ERROR")
    job_usage(con, "iw_ju_norunid", job_id=job, job_run_id=None, sku_name="iw_JOBS_COMPUTE", usage_date_=D(3), hour=9, usage_quantity=20.0)


def _write_J5(con: duckdb.DuckDBPyConnection) -> None:
    """iw_job_cancelonly: one CANCELLED run, one SUCCEEDED run -- no failed_runs and no
    repaired_runs, so the job never enters the finding at all (cancelled is shown elsewhere, never
    counted as waste)."""
    job = "iw_job_cancelonly"
    jrt(con, job, f"{job}_r1", DT(3, 11, 0, 0), DT(3, 11, 10, 0), result_state="CANCELLED")
    jrt(con, job, f"{job}_r2", DT(3, 11, 30, 0), DT(3, 11, 40, 0), result_state="SUCCEEDED")
    job_usage(con, "iw_ju_cancelonly_r1", job_id=job, job_run_id=f"{job}_r1", sku_name="iw_JOBS_COMPUTE", usage_date_=D(3), hour=11, usage_quantity=10.0)
    job_usage(con, "iw_ju_cancelonly_r2", job_id=job, job_run_id=f"{job}_r2", sku_name="iw_JOBS_COMPUTE", usage_date_=D(3), hour=11, usage_quantity=10.0)


def _write_J6(con: duckdb.DuckDBPyConnection) -> None:
    """iw_job_inflight (D(2)): one run whose last attempt has no end row yet -- a repair still in
    flight, left for a later window (no row at all, at any window_days)."""
    job = "iw_job_inflight"
    run = f"{job}_r1"
    jrt(con, job, run, DT(2, 8, 0, 0), DT(2, 8, 20, 0), result_state="FAILED", termination_code="RUN_EXECUTION_ERROR")
    jrt(con, job, run, DT(2, 9, 0, 0), DT(2, 10, 0, 0), result_state=None)
    job_usage(con, "iw_ju_inflight", job_id=job, job_run_id=run, sku_name="iw_JOBS_COMPUTE", usage_date_=D(2), hour=8, usage_quantity=10.0)


def _write_J7(con: duckdb.DuckDBPyConnection) -> None:
    """iw_job_nobill: 1 of 1 runs FAILED, no billed usage for this job at all -- NOT_ASSESSED
    no_billing_rows."""
    job = "iw_job_nobill"
    jrt(con, job, f"{job}_r1", DT(3, 14, 0, 0), DT(3, 14, 5, 0), result_state="FAILED", termination_code="RUN_EXECUTION_ERROR")


# =================================================================================================
# Section 6.4 (P4-01-W2) -- cost_failed_statement_waste (W2b), D(2). Neither warehouse has
# warehouse_events or billing.usage rows, so neither appears in W1's compute_warehouse_idle_minutes.
# =================================================================================================
def _write_W2B(con: duckdb.DuckDBPyConnection) -> None:
    wh1 = "iw_wh_fail"
    stmt(con, "iw_st_f1", wh1, DT(2, 16, 0, 0), DT(2, 16, 1, 0), execution_status="FAILED")
    stmt(con, "iw_st_f2", wh1, DT(2, 16, 2, 0), DT(2, 16, 2, 1), execution_status="FAILED")
    stmt(con, "iw_st_x1", wh1, DT(2, 16, 3, 0), DT(2, 16, 4, 0), execution_status="CANCELED")
    stmt(con, "iw_st_ok1", wh1, DT(2, 16, 5, 0), DT(2, 16, 6, 0), execution_status="FINISHED")
    attributed(con, "iw_au_1", statement_id="iw_st_f1", warehouse_id=wh1, start=DT(2, 16, 0, 0), end=DT(2, 16, 1, 0), active_usage_quantity=12.0)
    # iw_st_f2: FAILED, no attributed row at all -- it failed before using billed compute.
    attributed(con, "iw_au_2", statement_id="iw_st_x1", warehouse_id=wh1, start=DT(2, 16, 3, 0), end=DT(2, 16, 4, 0), active_usage_quantity=1.0)
    attributed(con, "iw_au_3", statement_id="iw_st_ok1", warehouse_id=wh1, start=DT(2, 16, 5, 0), end=DT(2, 16, 6, 0), active_usage_quantity=4.0)

    wh2 = "iw_wh_fail_noattr"
    stmt(con, "iw_st_f3", wh2, DT(2, 17, 0, 0), DT(2, 17, 0, 30), execution_status="FAILED")
    stmt(con, "iw_st_f4", wh2, DT(2, 17, 5, 0), DT(2, 17, 5, 30), execution_status="FAILED")
    # wh2 has no attributed_usage and no billing row -- NOT_ASSESSED no_billing_rows.

    # wh3: billed hours, no attributed_usage, so each hour is split across its statements by task time.
    wh3 = "iw_hf_wh"
    billing_usage(con, "iw_hf_bl_1", warehouse_id=wh3, sku_name="iw_SQL_PRO", usage_date_=D(2), hour=18, usage_quantity=20.0)
    billing_usage(con, "iw_hf_bl_2", warehouse_id=wh3, sku_name="iw_SQL_PRO", usage_date_=D(2), hour=19, usage_quantity=4.0)
    stmt(con, "iw_hf_st_h1", wh3, DT(2, 18, 0, 0), DT(2, 18, 10, 0), execution_status="FAILED", task_ms=300)
    stmt(con, "iw_hf_st_h2", wh3, DT(2, 18, 10, 0), DT(2, 18, 20, 0), task_ms=100)
    stmt(con, "iw_hf_st_h3", wh3, DT(2, 18, 20, 0), DT(2, 18, 30, 0), execution_status="CANCELED", task_ms=100)
    stmt(con, "iw_hf_st_h6", wh3, DT(2, 18, 40, 0), DT(2, 18, 41, 0), execution_status="FAILED", task_ms=0)
    # h4 spans 19:30-20:30; hour 20 has no bill, so only its hour-19 half (100) is priced.
    stmt(con, "iw_hf_st_h4", wh3, DT(2, 19, 30, 0), DT(2, 20, 30, 0), execution_status="FAILED", task_ms=200)
    stmt(con, "iw_hf_st_h5", wh3, DT(2, 19, 0, 0), DT(2, 19, 10, 0), task_ms=100)


def build(con: duckdb.DuckDBPyConnection) -> None:
    # Name IW_WS (chargeback.py's / waste_usd.py's own identical precedent).
    con.execute(_WS_SQL, [ACCOUNT_ID, IW_WS, "iw-waste", None, datetime(2025, 1, 1), "RUNNING"])

    list_price(con, "iw_SQL_PRO", 0.50)
    list_price(con, "iw_SERVERLESS_SQL", 0.70)
    list_price(con, "iw_JOBS_COMPUTE", 0.30)

    for wh_id, wtype, size, lo, hi, stop in _WAREHOUSES:
        warehouse(con, wh_id, wtype=wtype, size=size, min_clusters=lo, max_clusters=hi, auto_stop=stop)

    _write_A(con)
    _write_B(con)
    _write_C(con)
    _write_D(con)
    _write_E(con)
    _write_F(con)
    _write_G(con)
    _write_H(con)
    _write_I1(con)
    _write_I2(con)
    _write_I3(con)

    # P4-01-W2 -- section 6.3 (lakeflow_failed_jobs_wasted_dbus) and 6.4 (cost_failed_statement_waste).
    _write_J1(con)
    _write_J2(con)
    _write_J3(con)
    _write_J4(con)
    _write_J5(con)
    _write_J6(con)
    _write_J7(con)
    _write_W2B(con)

    # DEC-15 isolation guard: every id this builder writes must carry the iw_ prefix.
    for (sid,) in con.execute("SELECT statement_id FROM query__history WHERE workspace_id = ?", [IW_WS]).fetchall():
        if not sid.startswith("iw_"):
            raise ValueError(f"unprefixed statement_id in idle_waste.py fixture: {sid!r}")
    for (wid,) in con.execute("SELECT warehouse_id FROM compute__warehouses WHERE workspace_id = ?", [IW_WS]).fetchall():
        if not wid.startswith("iw_"):
            raise ValueError(f"unprefixed warehouse_id in idle_waste.py fixture: {wid!r}")
    for (jid,) in con.execute("SELECT job_id FROM lakeflow__job_run_timeline WHERE workspace_id = ?", [IW_WS]).fetchall():
        if not jid.startswith("iw_"):
            raise ValueError(f"unprefixed job_id in idle_waste.py fixture: {jid!r}")
    for (rid,) in con.execute("SELECT run_id FROM lakeflow__job_run_timeline WHERE workspace_id = ?", [IW_WS]).fetchall():
        if not rid.startswith("iw_"):
            raise ValueError(f"unprefixed run_id in idle_waste.py fixture: {rid!r}")
