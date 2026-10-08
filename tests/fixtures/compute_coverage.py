"""tests/fixtures/compute_coverage.py -- fixture builder for four app-owned findings ported from
drafts/: `compute_warehouse_autostop_churn` and `compute_warehouse_cache_reuse`
(app/queries/app/compute/), `cost_audit_self_usage` (app/queries/app/cost/, replaces the shipped
library query `audit_self_cost`, which always returns empty -- config/library_corrections.yml,
audit_self_cost-undocumented-marker-and-wildcard-escape), and `access_source_table_coverage`
(app/queries/app/governance_access/, a per-system-table health inventory).

Every id this builder writes is prefixed `cc_` (no other builder uses that prefix; DEC-15). Its
own workspace is CC_WS = "cc_ws1", account `cc_acct`, cloud "aws". One access__workspaces_latest
row names CC_WS, satisfying tests/test_api.py's "every real workspace has a name" invariant.

`D(n)` / `DT(n, h, m, s)` are relative to `base.AS_OF` (2026-09-21 12:00), like every other
builder's own convention: D(0) = 2026-09-21 (today), D(3) = 2026-09-18. Struct-default helpers
are copied from tests/fixtures/waste_usd.py's own shape (same DDL), not imported, per the
per-builder isolation model (tests/fixtures/build_fixtures.py's own module docstring).

=== access_source_table_coverage ===

This query's grain is the SYSTEM TABLE itself (schema_name, table_name) -- it aggregates the
WHOLE table account-wide, so unlike every other builder's own scenario rows, there is no
scenario-id column to filter by: tests/fixtures/governance.py and tests/fixtures/sensitive.py
also write to access.audit, access.table_lineage and information_schema.shares for their own,
unrelated scenarios, so those three tables' real row counts include that bleed-through and are
NOT exact numbers this module controls. tests/test_findings/test_access_source_table_coverage.py
therefore checks this query's status-derivation logic against every row's own observed columns
(self-consistency invariants), not hardcoded per-table counts, except for column_lineage and
table_privileges below, where this module's own rows are enough on their own to guarantee the
outcome regardless of what any other builder adds.

All seven scenarios are asserted at window_days = 30 only (the window this file's test reads).
`current_timestamp()`/`current_date()` are pinned to audit_now()/audit_today() = 2026-09-21
12:00:00 in the `test` dbt target, so a day D(29) row at hour 0 (2026-08-23 00:00) is always
safely inside a 30-day window (window_start = 2026-08-22 12:00), while D(30) is borderline and
D(31)+ is always outside -- every insert below stays at or inside D(29) to avoid that boundary.

  access.audit                    this module contributes nothing; governance.py's own rows
                                   decide its real status, so it is not asserted on here.
  information_schema.shares       this module contributes nothing; governance.py's own rows
                                   decide its real status, so it is not asserted on here.
  access.table_lineage            one row at D(400) only, well outside any window -- proves
                                   total_row_count >= 1, but governance.py/sensitive.py's own
                                   rows decide whether it reads WARN or OK, so status is not
                                   asserted on here either.
  access.inbound_network          two rows, D(1) and D(15) only (2 of 30 days) -- this module's
                                   own contribution alone would read WARN via the "gap inside the
                                   window" rule, but another builder's fresher rows can still push
                                   it to OK, so only total_row_count >= 2 is asserted.
  access.outbound_network         one row/day for D(5)..D(29) (25 days; D(0)-D(4) silent) -- this
                                   module's own contribution alone would read WARN via the gap
                                   rule, but not asserted on for the same reason as inbound_network.
  access.column_lineage           one row/day for D(0)..D(29) (30 of 30 possible window days,
                                   the most recent at D(0)) -- gap-free and fresh regardless of
                                   any other builder's rows (they can only add days already
                                   covered, never remove one), so OK is a real invariant here.
  information_schema.table_privileges  three rows (no time column) -- guarantees total_row_count
                                   > 0 regardless of any other builder, so OK is a real invariant.

=== compute_warehouse_autostop_churn (window_days = 30, header defaults: warn_long_autostop_
minutes 10, warn_daily_autostops 5, crit_daily_autostops 10, warn_daily_restarts 5) ===

  cc_wh_crit      classic, auto_stop_minutes=10, D(3): 10 auto-stop cycles (15 min idle wait
                  each, >= 10-1) -> autostop_count=10 >= crit(10) with auto_stop_minutes >=
                  warn_long(10) -> CRITICAL.
  cc_wh_warn      pro, auto_stop_minutes=15, D(3): 6 cycles (20 min idle wait each) ->
                  autostop_count=6, in [warn(5), crit(10)) -> WARN.
  cc_wh_ok_short  classic, auto_stop_minutes=5 (below warn_long_autostop_minutes 10), D(3): 8
                  cycles (10 min idle wait each, still >= 5-1) -> autostop_count=8 would be WARN
                  by count alone, but auto_stop_minutes is too short to flag -> OK.
  cc_wh_cold      classic, auto_stop_minutes=10, D(3): 5 cycles with only a 3-minute idle wait
                  each (< 10-1, never counted as an auto-stop) -> autostop_count=0 -> OK, but 5
                  STARTING events that day >= warn_daily_restarts(5) -> cold_start_risk TRUE.
  cc_wh_noquery   classic, auto_stop_minutes=10, D(3): one bare STOPPED event with no query
                  history at all -> last_query_finished_at is NULL -> excluded from
                  autostop_count (0) -> OK; proves the "no prior query found" exclusion.

  Every scenario's events land on D(3) only, so the query's one-row-per-warehouse rollup has
  days_observed=1 and worst_day/worst_day_* equal to that single day's own numbers above.

=== compute_warehouse_cache_reuse (window_days = 30, header defaults: min_queries_for_verdict
10, warn_low_cache_share_pct 5, warn_long_autostop_minutes 10; never CRITICAL) ===

  cc_wh_cache_warn_long  20 queries D(3), 0 from cache (0%), auto_stop_minutes=30 -> WARN.
  cc_wh_cache_warn     20 queries D(3), 0 from cache (0%), auto_stop_minutes=15 -> WARN.
  cc_wh_cache_ok_low   3 queries D(3) (< min_queries_for_verdict), 0 from cache, auto_stop_
                       minutes=60 -> OK (too little signal, regardless of share).
  cc_wh_cache_ok_high  20 queries D(3), 10 from cache (50%, >= 5%), auto_stop_minutes=60 -> OK.

=== cost_audit_self_usage (window_days = 30; coverage-only, no status column) ===

  cc_wh_audit_match   D(3): two statements with a matching client_application (1000ms + 2000ms)
                      and one with an unrelated one (3000ms) -> matched_statement_count=2,
                      matched_duration_secs=3.0, warehouse_all_duration_secs=6.0,
                      matched_share_pct=50.0; 10 DBU billed at $1/DBU -> est_usd_list=10.0,
                      est_audit_usd_list=5.0 (10.0 * 3000/6000), price_basis='priced'.
  cc_wh_audit_nobill  D(3): one matching statement (1000ms) and one non-matching (1000ms), no
                      billing.usage rows at all -> matched_share_pct=50.0, est_usd_list and
                      est_audit_usd_list both NULL, price_basis NULL.
  cc_wh_audit_none    D(3): one statement only, client_application never matches -> no `matched`
                      row at all -> this warehouse_id has NO row in the output.

Stdlib + duckdb only.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from datetime import time as dtime

import duckdb

from base import AS_OF, write_parquet  # noqa: F401 (write_parquet re-exported for build_fixtures.py)

D0 = AS_OF.date()

CC_WS = "cc_ws1"
ACCOUNT_ID = "cc_acct"


def D(n: int) -> date:
    """D(n) = D0 - n days (a date). D(0) is today."""
    return D0 - timedelta(days=n)


def DT(n: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    return datetime.combine(D(n), dtime(hour, minute, second))


def _hm(total_minutes: int) -> tuple[int, int]:
    return divmod(total_minutes, 60)


# =================================================================================================
# STRUCT defaults for system.billing.usage -- copied from tests/fixtures/waste_usd.py's own shape.
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


_WS_SQL = (
    "INSERT INTO access__workspaces_latest "
    "(account_id, workspace_id, workspace_name, workspace_url, create_time, status) "
    "VALUES (?,?,?,?,?,?)"
)


def _write_workspace(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(_WS_SQL, [ACCOUNT_ID, CC_WS, "cc-workspace", None, datetime(2026, 1, 1), "RUNNING"])


# =================================================================================================
# billing.usage / billing.list_prices
# =================================================================================================
_USAGE_SQL = (
    "INSERT INTO billing__usage (account_id, workspace_id, record_id, sku_name, cloud, "
    "usage_start_time, usage_end_time, usage_date, custom_tags, usage_unit, usage_quantity, "
    "usage_metadata, identity_metadata, record_type, ingestion_date, billing_origin_product, "
    "product_features, usage_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def billing_usage(con: duckdb.DuckDBPyConnection, record_id: str, *, warehouse_id: str,
                   sku_name: str, usage_date_: date, hour: int, usage_quantity: float) -> None:
    start = datetime.combine(usage_date_, dtime(hour, 0, 0))
    end = start + timedelta(hours=1)
    con.execute(
        _USAGE_SQL,
        [
            ACCOUNT_ID, CC_WS, record_id, sku_name, "aws", start, end, usage_date_,
            {}, "DBU", usage_quantity,
            _usage_metadata(warehouse_id=warehouse_id),
            _identity_metadata(),
            "ORIGINAL", usage_date_, "SQL",
            _product_features(is_serverless=False),
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


def warehouse(con: duckdb.DuckDBPyConnection, warehouse_id: str, *, wtype: str,
              auto_stop: int) -> None:
    con.execute(
        _WH_SQL,
        [
            warehouse_id, CC_WS, ACCOUNT_ID, warehouse_id, wtype, "CHANNEL_NAME_CURRENT", "M",
            1, 1, auto_stop, {}, datetime(2026, 8, 1), None, "cc_owner@example.com",
        ],
    )


_WE_SQL = (
    "INSERT INTO compute__warehouse_events "
    "(account_id, workspace_id, warehouse_id, event_type, cluster_count, event_time) "
    "VALUES (?,?,?,?,?,?)"
)


def we(con: duckdb.DuckDBPyConnection, warehouse_id: str, event_type: str, event_time: datetime,
       cluster_count: int) -> None:
    con.execute(_WE_SQL, [ACCOUNT_ID, CC_WS, warehouse_id, event_type, cluster_count, event_time])


# =================================================================================================
# query.history
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
         end: datetime, *, total_duration_ms: int | None = None, from_result_cache: bool = False,
         read_io_cache_percent: int = 0, client_application: str | None = None,
         statement_text: str = "SELECT 1 /* cc */") -> None:
    row = {c: None for c in QH_COLUMNS}
    dur = total_duration_ms if total_duration_ms is not None else int((end - start).total_seconds() * 1000)
    row.update({
        "account_id": ACCOUNT_ID,
        "workspace_id": CC_WS,
        "statement_id": statement_id,
        "executed_by": "cc_analyst@example.com",
        "session_id": f"cc_sess_{warehouse_id}",
        "execution_status": "FINISHED",
        "compute": {"type": "WAREHOUSE", "cluster_id": None, "warehouse_id": warehouse_id},
        "statement_text": statement_text,
        "statement_type": "SELECT",
        "client_application": client_application,
        "total_duration_ms": dur,
        "waiting_for_compute_duration_ms": 0,
        "waiting_at_capacity_duration_ms": 0,
        "execution_duration_ms": dur,
        "compilation_duration_ms": 0,
        "start_time": start,
        "end_time": end,
        "update_time": end,
        "read_bytes": 0,
        "read_io_cache_percent": read_io_cache_percent,
        "from_result_cache": from_result_cache,
        "spilled_local_bytes": 0,
        "shuffle_read_bytes": 0,
    })
    con.execute(_QH_INSERT, [row[c] for c in QH_COLUMNS])


# =================================================================================================
# compute_warehouse_autostop_churn -- one auto-stop cycle: STARTING, a short query, an idle wait,
# STOPPED. offset_min is the cycle's start, in minutes from midnight on day D(day_n).
# =================================================================================================
def _cycle(con: duckdb.DuckDBPyConnection, wh_id: str, day_n: int, offset_min: int,
           idle_minutes: int, *, query_minutes: int = 4, seq: int = 0) -> None:
    h0, m0 = _hm(offset_min)
    we(con, wh_id, "STARTING", DT(day_n, h0, m0), 0)
    qs = offset_min + 1
    qh, qm = _hm(qs)
    qe = qs + query_minutes
    qeh, qem = _hm(qe)
    stmt(con, f"cc_q_{wh_id}_{seq}", wh_id, DT(day_n, qh, qm), DT(day_n, qeh, qem))
    stop_off = qe + idle_minutes
    sh, sm = _hm(stop_off)
    we(con, wh_id, "STOPPED", DT(day_n, sh, sm), 0)


def _write_autostop_churn(con: duckdb.DuckDBPyConnection) -> None:
    warehouse(con, "cc_wh_crit", wtype="CLASSIC", auto_stop=10)
    for i in range(10):
        _cycle(con, "cc_wh_crit", 3, i * 40, 15, seq=i)

    warehouse(con, "cc_wh_warn", wtype="PRO", auto_stop=15)
    for i in range(6):
        _cycle(con, "cc_wh_warn", 3, i * 60, 20, seq=i)

    warehouse(con, "cc_wh_ok_short", wtype="CLASSIC", auto_stop=5)
    for i in range(8):
        _cycle(con, "cc_wh_ok_short", 3, i * 40, 10, seq=i)

    warehouse(con, "cc_wh_cold", wtype="CLASSIC", auto_stop=10)
    for i in range(5):
        _cycle(con, "cc_wh_cold", 3, i * 40, 3, seq=i)

    warehouse(con, "cc_wh_noquery", wtype="CLASSIC", auto_stop=10)
    we(con, "cc_wh_noquery", "STOPPED", DT(3, 1, 0), 0)


# =================================================================================================
# compute_warehouse_cache_reuse -- n_queries evenly spaced on day D(3), n_cached served from
# result cache (the first n_cached of them).
# =================================================================================================
def _write_cache_reuse_wh(con: duckdb.DuckDBPyConnection, wh_id: str, *, wtype: str,
                           auto_stop: int, n_queries: int, n_cached: int) -> None:
    warehouse(con, wh_id, wtype=wtype, auto_stop=auto_stop)
    for i in range(n_queries):
        h, m = _hm(i * 10)
        start = DT(3, h, m)
        end = start + timedelta(minutes=1)
        stmt(con, f"cc_cache_{wh_id}_{i}", wh_id, start, end, from_result_cache=(i < n_cached),
             statement_text="SELECT 1 /* cc_cache */")


def _write_cache_reuse(con: duckdb.DuckDBPyConnection) -> None:
    _write_cache_reuse_wh(con, "cc_wh_cache_warn_long", wtype="PRO", auto_stop=30, n_queries=20, n_cached=0)
    _write_cache_reuse_wh(con, "cc_wh_cache_warn", wtype="PRO", auto_stop=15, n_queries=20, n_cached=0)
    _write_cache_reuse_wh(con, "cc_wh_cache_ok_low", wtype="PRO", auto_stop=60, n_queries=3, n_cached=0)
    _write_cache_reuse_wh(con, "cc_wh_cache_ok_high", wtype="PRO", auto_stop=60, n_queries=20, n_cached=10)


# =================================================================================================
# cost_audit_self_usage
# =================================================================================================
_MATCHED_UA = "PyDatabricksSqlConnector/3.1.0 (Python 3.11.0) CrosshireAudit"
_OTHER_UA = "DatabricksJDBCDriver/2.6.36"
# Same underlying connector as _MATCHED_UA, but no CrosshireAudit marker -- a stand-in for
# dbt-databricks or another Python-connector tool this check must not also count as the audit.
_OTHER_CONNECTOR_UA = "databricks-sql-connector/2.9.3"


def _write_audit_self_usage(con: duckdb.DuckDBPyConnection) -> None:
    wh = "cc_wh_audit_match"
    warehouse(con, wh, wtype="PRO", auto_stop=10)
    stmt(con, "cc_au_m1", wh, DT(3, 1, 0), DT(3, 1, 1), total_duration_ms=1000,
         client_application=_MATCHED_UA)
    stmt(con, "cc_au_m2", wh, DT(3, 2, 0), DT(3, 2, 2), total_duration_ms=2000,
         client_application=_MATCHED_UA)
    stmt(con, "cc_au_o1", wh, DT(3, 3, 0), DT(3, 3, 3), total_duration_ms=3000,
         client_application=_OTHER_CONNECTOR_UA)
    billing_usage(con, "cc_bl_au1", warehouse_id=wh, sku_name="cc_SQL_PRO", usage_date_=D(3),
                  hour=1, usage_quantity=10.0)
    list_price(con, "cc_SQL_PRO", 1.0)

    wh2 = "cc_wh_audit_nobill"
    warehouse(con, wh2, wtype="PRO", auto_stop=10)
    stmt(con, "cc_au_n1", wh2, DT(3, 1, 0), DT(3, 1, 1), total_duration_ms=1000,
         client_application=_MATCHED_UA)
    stmt(con, "cc_au_n2", wh2, DT(3, 2, 0), DT(3, 2, 1), total_duration_ms=1000,
         client_application=_OTHER_UA)

    wh3 = "cc_wh_audit_none"
    warehouse(con, wh3, wtype="PRO", auto_stop=10)
    stmt(con, "cc_au_x1", wh3, DT(3, 1, 0), DT(3, 1, 1), total_duration_ms=500,
         client_application=_OTHER_UA)


# =================================================================================================
# access_source_table_coverage -- seven system tables no other builder writes to (see module
# docstring for why each one is safe to own exactly).
# =================================================================================================
_AUDIT_SQL = (
    "INSERT INTO access__audit (account_id, workspace_id, version, event_time, event_date, "
    "source_ip_address, user_agent, session_id, user_identity, service_name, action_name, "
    "request_id, request_params, response, audit_level, event_id, identity_metadata) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _table_lineage_row(con: duckdb.DuckDBPyConnection, n: int, hour: int = 6) -> None:
    con.execute(
        "INSERT INTO access__table_lineage (account_id, metastore_id, workspace_id, entity_type, "
        "entity_id, entity_run_id, source_table_full_name, source_table_catalog, "
        "source_table_schema, source_table_name, source_path, source_type, "
        "target_table_full_name, target_table_catalog, target_table_schema, target_table_name, "
        "target_path, target_type, created_by, event_time, event_date, record_id, event_id, "
        "statement_id, entity_metadata, direct_access) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ACCOUNT_ID, "cc_meta", CC_WS, "NOTEBOOK", "cc_nb", None,
            "cc.s.src", "cc", "s", "src", None, "TABLE",
            "cc.s.tgt", "cc", "s", "tgt", None, "TABLE",
            "cc_owner@example.com", DT(n, hour), D(n), f"cc_tl_{n}", f"cc_tl_ev_{n}", None,
            None, False,
        ],
    )


def _inbound_row(con: duckdb.DuckDBPyConnection, n: int, hour: int = 6) -> None:
    con.execute(
        "INSERT INTO access__inbound_network (account_id, workspace_id, event_id, "
        "request_path, source, authenticated_as, event_time, policy_outcome, rule_label) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [ACCOUNT_ID, CC_WS, f"cc_in_{n}", "/api", {"ip": "10.0.0.1"}, None, DT(n, hour), "ALLOW", None],
    )


def _outbound_row(con: duckdb.DuckDBPyConnection, n: int, hour: int = 6) -> None:
    con.execute(
        "INSERT INTO access__outbound_network (account_id, workspace_id, destination_type, "
        "destination, dns_event, storage_event, event_time, access_type, event_id, "
        "network_source_type) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [ACCOUNT_ID, CC_WS, "DNS", "example.com", None, None, DT(n, hour), "ALLOW", f"cc_out_{n}", None],
    )


def _column_lineage_row(con: duckdb.DuckDBPyConnection, n: int, hour: int = 6) -> None:
    con.execute(
        "INSERT INTO access__column_lineage (account_id, metastore_id, workspace_id, "
        "entity_type, entity_id, entity_run_id, source_table_full_name, source_table_catalog, "
        "source_table_schema, source_table_name, source_path, source_type, source_column_name, "
        "target_table_full_name, target_table_catalog, target_table_schema, target_table_name, "
        "target_path, target_type, target_column_name, created_by, event_time, event_date, "
        "record_id, event_id, statement_id, entity_metadata, direct_access) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ACCOUNT_ID, "cc_meta", CC_WS, "NOTEBOOK", "cc_nb", None,
            "cc.s.src", "cc", "s", "src", None, "TABLE", "col_a",
            "cc.s.tgt", "cc", "s", "tgt", None, "TABLE", "col_a",
            "cc_owner@example.com", DT(n, hour), D(n), f"cc_cl_{n}", f"cc_cl_ev_{n}", None,
            None, False,
        ],
    )


def _write_source_table_coverage(con: duckdb.DuckDBPyConnection) -> None:
    # access.audit and information_schema.shares stay completely empty -> CRITICAL (both variants).

    _table_lineage_row(con, 400)  # rows_in_window = 0, real history -> WARN (rule 1)

    for n in (1, 15):  # 2 of 30 days, lag = 1 -> WARN via the gap rule alone
        _inbound_row(con, n)

    for n in range(5, 30):  # D(5)..D(29): 25 days, gap AND lag (5) both fire
        _outbound_row(con, n)

    for n in range(0, 30):  # D(0)..D(29): 30 days, gap-free and fresh -> OK
        _column_lineage_row(con, n)

    for i in range(3):
        con.execute(
            "INSERT INTO information_schema__table_privileges (grantor, grantee, table_catalog, "
            "table_schema, table_name, privilege_type, is_grantable, inherited_from) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ["cc_owner@example.com", f"cc_grantee_{i}@example.com", "cc", "s", "t", "SELECT", "NO", None],
        )


def build(con: duckdb.DuckDBPyConnection) -> None:
    _write_workspace(con)
    _write_autostop_churn(con)
    _write_cache_reuse(con)
    _write_audit_self_usage(con)
    _write_source_table_coverage(con)
