"""tests/fixtures/storage.py -- fixture builder for po_failure_reasons (reads
system.storage.predictive_optimization_operations_history, the same source
tests/fixtures/serving_storage.py's 5 po_* ids already use) plus storage_small_files /
storage_growth / storage_po_coverage (reads system.storage.table_metrics_history, metastore-level,
no workspace_id); and, added later (same file), storage_unused_table_cost (reads
information_schema.tables + access.table_lineage, same dead-table rule as
tests/fixtures/governance.py's access_dead_table_candidates scenario, plus its own
table_metrics_history/predictive_optimization_operations_history/billing.usage rows for the $ side
-- see `_build_unused_table_cost` and its own scenario comments below).

Own id namespace (DEC-15 amendment pattern): every id below is prefixed `stg_pofr_` (po_failure_
reasons), `stg_sf_`/`stg_gr_`/`stg_poc_` (the table_metrics_history checks) or `stg_utc_`
(storage_unused_table_cost), disjoint from `ss_` (serving_storage.py), `pt_` (ports.py), `fo_`/
`iw_`/`gv_`/etc. Reuses the shared workspace id "1111" (DEC-15).

`tmh()`'s default catalog_name/schema_name ("stg_catalog"/"stg_schema") are shared by every
_build_small_files/_build_growth row that does not pass its own -- so storage_po_coverage's
catalog_summary always carries one extra "stg_catalog" row (always OK, predictive optimization on)
that is bleed-through from those two builders, not a po_coverage scenario of its own; its own test
file filters it out rather than asserting on it.

Stdlib + duckdb only.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import duckdb

from base import AS_OF

ACCOUNT_ID = "stg_acct"
WS = "1111"  # shared workspace id (DEC-15)

TODAY = AS_OF.date()  # 2026-09-21, matches audit_today()


def D(days_ago: int) -> date:
    return TODAY - timedelta(days=days_ago)

_PO_SQL = (
    "INSERT INTO storage__predictive_optimization_operations_history "
    "(account_id, workspace_id, start_time, end_time, metastore_name, metastore_id, catalog_name, "
    "schema_name, table_name, table_id, operation_type, operation_id, operation_status, "
    "operation_metrics, usage_unit, usage_quantity) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def po_op(
    con: duckdb.DuckDBPyConnection,
    *,
    table_id: str,
    table_name: str,
    op_suffix: str,
    operation_status: str,
    start_time: datetime,
    usage_quantity: float | None,
    usage_unit: str = "ESTIMATED_DBU",
    operation_type: str = "CLUSTERING",
    catalog_name: str = "stg_catalog",
    schema_name: str = "stg_schema",
) -> None:
    con.execute(
        _PO_SQL,
        [
            ACCOUNT_ID, WS, start_time, start_time + timedelta(minutes=15),
            "stg_metastore", "stg_metastore_id", catalog_name, schema_name, table_name, table_id,
            operation_type, f"stg_op_{table_id}_{op_suffix}", operation_status, {},
            usage_unit, usage_quantity,
        ],
    )


# =================================================================================================
# po_failure_reasons (grain [catalog_name, schema_name, table_id, table_name, operation_status]).
# Every op starts at D(2)'s 10:00, inside every window. SUCCESSFUL ops must never surface (the
# query's own WHERE operation_status LIKE 'FAILED%' filters them out at the source).
#   stg_pofr_a  2 x FAILED: INTERNAL_ERROR                       (<5)   -> OK
#   stg_pofr_b  7 x FAILED: PRIVATE_LINK_SETUP_ERROR              (>=5, <20) -> WARN
#   stg_pofr_c  25 x FAILED: AUTO_TTL_COLUMN_DOES_NOT_EXIST_ERROR (>=20) -> CRITICAL
#   stg_pofr_d  6 x FAILED: SOME_OTHER_ERROR (unmapped)           (>=5, <20) -> WARN, generic
#               reason/fix (proves the ELSE branch, not one of the 3 named reasons)
#   stg_pofr_e  1 x SUCCESSFUL                                    -> never appears (status filter)
# stg_pofr_a's 2 ops also prove the ESTIMATED_DBU-only sum: one op at 1.0 ESTIMATED_DBU, one at
# 999 in a different usage_unit that must NOT be added into estimated_dbus_spent (stays 1.0, not
# 1000.0), while still counting toward failed_operations (2, not 1).
# =================================================================================================
def _build_po_failure_reasons(con: duckdb.DuckDBPyConnection) -> None:
    t0 = datetime(TODAY.year, TODAY.month, TODAY.day) - timedelta(days=2) + timedelta(hours=10)  # D(2) 10:00

    po_op(con, table_id="stg_pofr_a", table_name="pofr_a", op_suffix="0",
          operation_status="FAILED: INTERNAL_ERROR", start_time=t0, usage_quantity=1.0)
    po_op(con, table_id="stg_pofr_a", table_name="pofr_a", op_suffix="1",
          operation_status="FAILED: INTERNAL_ERROR", start_time=t0, usage_quantity=999.0,
          usage_unit="OTHER_UNIT")

    for i in range(7):
        po_op(con, table_id="stg_pofr_b", table_name="pofr_b", op_suffix=str(i),
              operation_status="FAILED: PRIVATE_LINK_SETUP_ERROR", start_time=t0, usage_quantity=1.0)

    for i in range(25):
        po_op(con, table_id="stg_pofr_c", table_name="pofr_c", op_suffix=str(i),
              operation_status="FAILED: AUTO_TTL_COLUMN_DOES_NOT_EXIST_ERROR", start_time=t0,
              usage_quantity=0.5)

    for i in range(6):
        po_op(con, table_id="stg_pofr_d", table_name="pofr_d", op_suffix=str(i),
              operation_status="FAILED: SOME_OTHER_ERROR", start_time=t0, usage_quantity=1.0)

    po_op(con, table_id="stg_pofr_e", table_name="pofr_e", op_suffix="0",
          operation_status="SUCCESSFUL", start_time=t0, usage_quantity=1.0)


# =================================================================================================
# storage_small_files / storage_growth / storage_po_coverage -- system.storage.table_metrics_
# history, one row per table_id per snapshot_date, metastore-level (no workspace_id).
# =================================================================================================
_TMH_TABLE = "storage__table_metrics_history"
_TMH_SQL = (
    f"INSERT INTO {_TMH_TABLE} "
    "(account_id, metastore_id, catalog_name, schema_name, table_name, table_id, table_type, "
    "table_owner, table_creation_time, table_dropped_time, snapshot_date, active_bytes, "
    "active_files, predictive_optimization_enabled) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def tmh(
    con: duckdb.DuckDBPyConnection,
    *,
    table_id: str,
    table_name: str,
    snapshot_date_: date,
    active_bytes: int,
    active_files: int,
    catalog_name: str = "stg_catalog",
    schema_name: str = "stg_schema",
    table_type: str = "MANAGED",
    table_owner: str | None = "stg_owner",
    table_creation_time: datetime | None = None,
    table_dropped_time: datetime | None = None,
    predictive_optimization_enabled: bool = True,
) -> None:
    con.execute(
        _TMH_SQL,
        [
            ACCOUNT_ID, "stg_metastore_id", catalog_name, schema_name, table_name, table_id,
            table_type, table_owner, table_creation_time or datetime(2025, 1, 1),
            table_dropped_time, snapshot_date_, active_bytes, active_files,
            predictive_optimization_enabled,
        ],
    )


_MB = 1024 * 1024
_GB = 1024 * _MB


def _build_small_files(con: duckdb.DuckDBPyConnection) -> None:
    tmh(con, table_id="stg_sf_ok", table_name="sf_ok", snapshot_date_=D(2),
        active_bytes=100 * 1 * _MB, active_files=100)
    tmh(con, table_id="stg_sf_warn", table_name="sf_warn", snapshot_date_=D(2),
        active_bytes=1500 * 20 * _MB, active_files=1500)
    tmh(con, table_id="stg_sf_crit", table_name="sf_crit", snapshot_date_=D(2),
        active_bytes=1500 * 4 * _MB, active_files=1500)
    tmh(con, table_id="stg_sf_dropped", table_name="sf_dropped", snapshot_date_=D(2),
        active_bytes=1500 * 4 * _MB, active_files=1500,
        table_dropped_time=datetime(TODAY.year, TODAY.month, TODAY.day) - timedelta(days=3))
    tmh(con, table_id="stg_sf_latest_wins", table_name="sf_latest_wins", snapshot_date_=D(10),
        active_bytes=2000 * 2 * _MB, active_files=2000)
    tmh(con, table_id="stg_sf_latest_wins", table_name="sf_latest_wins", snapshot_date_=D(2),
        active_bytes=50 * 50 * _MB, active_files=50)


def _build_po_coverage(con: duckdb.DuckDBPyConnection) -> None:
    tmh(con, table_id="stg_poc_ok_a", table_name="poc_ok_a", catalog_name="stg_poc_cat_ok",
        snapshot_date_=D(2), active_bytes=1000, active_files=10, predictive_optimization_enabled=True)
    tmh(con, table_id="stg_poc_ok_b", table_name="poc_ok_b", catalog_name="stg_poc_cat_ok",
        snapshot_date_=D(2), active_bytes=1000, active_files=10, predictive_optimization_enabled=True)
    tmh(con, table_id="stg_poc_warn_a", table_name="poc_warn_a", catalog_name="stg_poc_cat_warn",
        snapshot_date_=D(2), active_bytes=700, active_files=10, predictive_optimization_enabled=True)
    tmh(con, table_id="stg_poc_warn_b", table_name="poc_warn_b", catalog_name="stg_poc_cat_warn",
        snapshot_date_=D(2), active_bytes=300, active_files=10, predictive_optimization_enabled=False)
    tmh(con, table_id="stg_poc_crit_a", table_name="poc_crit_a", catalog_name="stg_poc_cat_crit",
        snapshot_date_=D(2), active_bytes=200, active_files=10, predictive_optimization_enabled=True)
    tmh(con, table_id="stg_poc_crit_b", table_name="poc_crit_b", catalog_name="stg_poc_cat_crit",
        snapshot_date_=D(2), active_bytes=800, active_files=10, predictive_optimization_enabled=False)


def _build_growth(con: duckdb.DuckDBPyConnection) -> None:
    def pair(table_id: str, table_name: str, first_bytes: int, last_bytes: int, **kw) -> None:
        tmh(con, table_id=table_id, table_name=table_name, snapshot_date_=D(7),
            active_bytes=first_bytes, active_files=10, **kw)
        tmh(con, table_id=table_id, table_name=table_name, snapshot_date_=D(2),
            active_bytes=last_bytes, active_files=10, **kw)

    pair("stg_gr_ok", "gr_ok", 1000, 1000, table_owner="stg_owner")
    # Growth must clear :min_growth_bytes (1 GiB) to be judged at all.
    pair("stg_gr_warn", "gr_warn", 4 * _GB, 5 * _GB, table_owner="stg_owner")
    pair("stg_gr_crit", "gr_crit", 4 * _GB, 8 * _GB, table_owner="stg_owner")
    pair("stg_gr_dropped", "gr_dropped", 1000, 1000, table_owner="stg_owner",
         table_dropped_time=datetime(TODAY.year, TODAY.month, TODAY.day) - timedelta(days=3))
    pair("stg_gr_noowner", "gr_noowner", 1000, 1000, table_owner=None)
    # The drop lands mid-window: the FIRST snapshot's own table_dropped_time is still NULL (the
    # table was alive then), only the LAST snapshot carries it -- must still read
    # dropped_in_window=True/WARN, which needs table_dropped_time read from the last snapshot, not
    # the first (a first-snapshot read sees NULL here and misses the drop entirely).
    tmh(con, table_id="stg_gr_dropped_late", table_name="gr_dropped_late", snapshot_date_=D(7),
        active_bytes=1000, active_files=10, table_owner="stg_owner")
    tmh(con, table_id="stg_gr_dropped_late", table_name="gr_dropped_late", snapshot_date_=D(2),
        active_bytes=1000, active_files=10, table_owner="stg_owner",
        table_dropped_time=datetime(TODAY.year, TODAY.month, TODAY.day) - timedelta(days=3))


# =================================================================================================
# storage_unused_table_cost -- own catalog/schema so its information_schema.tables /
# access.table_lineage rows never collide with tests/fixtures/governance.py's gv_catalog ones (both
# builders write the same two shared DuckDB tables). warn_usd_month=20, crit_usd_month=100,
# storage_usd_per_gb_month=0.02 (header defaults), top_n=500.
#   stg_utc_alive        HAS a lineage SOURCE row in the window -> excluded entirely (same dead-
#                          table rule access_dead_table_candidates uses)
#   stg_utc_crit         5000 GiB * $0.02/GB-month = $100.00 exactly -> CRITICAL, storage alone; a
#                          write (lineage TARGET) 45 days ago (unwindowed) -> days_since_last_write=45
#   stg_utc_warn         1500 GiB -> $30.00 -> WARN, storage alone; never written to on record ->
#                          days_since_last_write NULL
#   stg_utc_ok           500 GiB -> $10.00 -> OK
#   stg_utc_po_driven    10 GiB ($0.20) + 150 Predictive Optimization DBUs in the window at the
#                          $1.00/DBU blended rate ($150.00) -> est_total_usd_month=150.20 -> CRITICAL
#                          via upkeep, not storage
#   stg_utc_no_snapshot  no table_metrics_history snapshot, no Predictive Optimization activity ->
#                          OK, price_basis no_size - listed, size unknown, not a real zero
#   stg_utc_fill_####    497 identical 1 GiB ($0.02) filler tables, so the account has 501 priced
#                          (non-NOT_ASSESSED) unused tables total - one more than :top_n=500 -
#                          proving the pooling mechanic: exactly 500 kept, exactly 1 pooled
# Not engineered here (see access_dead_table_candidates' own suite instead, same shared rule): the
# lineage-source window boundary and the 7/30/90-day differential - this file only exercises
# window_days=30.
# =================================================================================================
UTC_CATALOG = "stg_utc_catalog"
UTC_SCHEMA = "stg_utc_schema"
_GIB = 1024 ** 3
_UTC_FILL_COUNT = 497

_UTC_IS_TABLES_SQL = (
    "INSERT INTO information_schema__tables (table_catalog, table_schema, table_name, table_type, "
    "is_insertable_into, commit_action, table_owner, comment, created, created_by, last_altered, "
    "last_altered_by, data_source_format, storage_sub_directory, storage_path) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _utc_table(
    con: duckdb.DuckDBPyConnection, table_name: str, *, owner: str | None, last_altered: datetime,
    table_type: str = "MANAGED",
) -> None:
    con.execute(
        _UTC_IS_TABLES_SQL,
        [
            UTC_CATALOG, UTC_SCHEMA, table_name, table_type, "YES", None, owner, None,
            datetime(2025, 1, 1), owner, last_altered, owner, "DELTA", None, None,
        ],
    )


_UTC_LINEAGE_SQL = (
    "INSERT INTO access__table_lineage (account_id, metastore_id, workspace_id, entity_type, "
    "entity_id, entity_run_id, source_table_full_name, source_table_catalog, source_table_schema, "
    "source_table_name, source_path, source_type, target_table_full_name, target_table_catalog, "
    "target_table_schema, target_table_name, target_path, target_type, created_by, event_time, "
    "event_date, record_id, event_id, statement_id, entity_metadata, direct_access) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)

_utc_seq = [0]


def _utc_next_id() -> str:
    _utc_seq[0] += 1
    return f"stg_utc_evt_{_utc_seq[0]}"


def _utc_lineage_read(con: duckdb.DuckDBPyConnection, event_time: datetime, table_name: str) -> None:
    """Proves the table is ALIVE: a lineage SOURCE row for (UTC_CATALOG, UTC_SCHEMA, table_name)."""
    full = f"{UTC_CATALOG}.{UTC_SCHEMA}.{table_name}"
    con.execute(
        _UTC_LINEAGE_SQL,
        [
            ACCOUNT_ID, "stg_metastore_id", WS, "NOTEBOOK", None, None,
            full, UTC_CATALOG, UTC_SCHEMA, table_name, None, "TABLE",
            f"{UTC_CATALOG}.{UTC_SCHEMA}.stg_utc_reader", UTC_CATALOG, UTC_SCHEMA, "stg_utc_reader",
            None, "TABLE", "stg_utc_writer", event_time, event_time.date(), None, _utc_next_id(),
            None, None, True,
        ],
    )


def _utc_lineage_write(con: duckdb.DuckDBPyConnection, event_time: datetime, table_name: str) -> None:
    """Proves a WRITE (target) happened. Source left fully NULL - an INSERT ... VALUES-style write
    captures no source at all, the same gap access_dead_table_candidates' own caveats document."""
    full = f"{UTC_CATALOG}.{UTC_SCHEMA}.{table_name}"
    con.execute(
        _UTC_LINEAGE_SQL,
        [
            ACCOUNT_ID, "stg_metastore_id", WS, "NOTEBOOK", None, None,
            None, None, None, None, None, None,
            full, UTC_CATALOG, UTC_SCHEMA, table_name, None, "TABLE",
            "stg_utc_writer", event_time, event_time.date(), None, _utc_next_id(), None, None, True,
        ],
    )


# STRUCT defaults mirror tests/fixtures/finops.py's own field lists exactly (same DDL, same
# duckdb dict-binds-by-field-name requirement); this query never references usage_metadata /
# identity_metadata / product_features, so every field stays NULL/False.
def _utc_usage_metadata():
    return {
        "cluster_id": None, "job_id": None, "warehouse_id": None, "instance_pool_id": None,
        "node_type": None, "job_run_id": None, "notebook_id": None, "dlt_pipeline_id": None,
        "endpoint_name": None, "endpoint_id": None, "dlt_update_id": None, "dlt_maintenance_id": None,
        "run_name": None, "job_name": None, "notebook_path": None, "central_clean_room_id": None,
        "source_region": None, "destination_region": None, "app_id": None, "app_name": None,
        "metastore_id": None, "private_endpoint_name": None, "storage_api_type": None,
        "budget_policy_id": None, "ai_runtime_pool_id": None, "catalog_id": None,
        "networking_client": None, "recipient_id": None, "usage_policy_id": None,
    }


def _utc_identity_metadata():
    return {"run_as": None, "created_by": None, "owned_by": None, "run_by": None}


def _utc_product_features():
    return {
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


_UTC_USAGE_SQL = (
    "INSERT INTO billing__usage (account_id, workspace_id, record_id, sku_name, cloud, "
    "usage_start_time, usage_end_time, usage_date, custom_tags, usage_unit, usage_quantity, "
    "usage_metadata, identity_metadata, record_type, ingestion_date, billing_origin_product, "
    "product_features, usage_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _utc_po_usage(con: duckdb.DuckDBPyConnection, *, sku_name: str, usage_quantity: float, usage_date_: date) -> None:
    """One system.billing.usage row with billing_origin_product='PREDICTIVE_OPTIMIZATION' - the
    account-wide side of the query's own blended $/DBU rate (see the query's confidence_note)."""
    start = datetime(usage_date_.year, usage_date_.month, usage_date_.day)
    con.execute(
        _UTC_USAGE_SQL,
        [
            ACCOUNT_ID, WS, f"stg_utc_u_{_utc_next_id()}", sku_name, "aws", start,
            start + timedelta(hours=1), usage_date_, {}, "DBU", usage_quantity,
            _utc_usage_metadata(), _utc_identity_metadata(), "ORIGINAL", usage_date_,
            "PREDICTIVE_OPTIMIZATION", _utc_product_features(), "COMPUTE_TIME",
        ],
    )


_UTC_LP_SQL = (
    "INSERT INTO billing__list_prices (account_id, price_start_time, price_end_time, sku_name, "
    "cloud, currency_code, usage_unit, pricing) VALUES (?,?,?,?,?,?,?,?)"
)


def _utc_list_price(con: duckdb.DuckDBPyConnection, sku_name: str, rate: float) -> None:
    """One CURRENT (price_end_time NULL) list_prices row starting 400 days before AS_OF, well
    before every usage row this file writes."""
    con.execute(
        _UTC_LP_SQL,
        [
            ACCOUNT_ID, AS_OF - timedelta(days=400), None, sku_name, "aws", "USD", "DBU",
            {"default": rate, "promotional": {"default": None}, "effective_list": {"default": rate}},
        ],
    )


def _build_unused_table_cost(con: duckdb.DuckDBPyConnection) -> None:
    t_win = datetime(TODAY.year, TODAY.month, TODAY.day) - timedelta(days=2) + timedelta(hours=10)

    _utc_table(con, "stg_utc_alive", owner="stg_utc_alive_owner", last_altered=AS_OF - timedelta(days=5))
    _utc_lineage_read(con, t_win, "stg_utc_alive")

    _utc_table(con, "stg_utc_crit", owner="stg_utc_crit_owner@example.com",
               last_altered=AS_OF - timedelta(days=200))
    tmh(con, table_id="stg_utc_crit_id", table_name="stg_utc_crit", snapshot_date_=D(2),
        active_bytes=5000 * _GIB, active_files=10, catalog_name=UTC_CATALOG, schema_name=UTC_SCHEMA)
    _utc_lineage_write(con, AS_OF - timedelta(days=45), "stg_utc_crit")

    _utc_table(con, "stg_utc_warn", owner="11112222-3333-4444-5555-666677778888",
               last_altered=AS_OF - timedelta(days=120))
    tmh(con, table_id="stg_utc_warn_id", table_name="stg_utc_warn", snapshot_date_=D(2),
        active_bytes=1500 * _GIB, active_files=10, catalog_name=UTC_CATALOG, schema_name=UTC_SCHEMA)

    _utc_table(con, "stg_utc_ok", owner="__REDACTED__", last_altered=AS_OF - timedelta(days=10))
    tmh(con, table_id="stg_utc_ok_id", table_name="stg_utc_ok", snapshot_date_=D(2),
        active_bytes=500 * _GIB, active_files=10, catalog_name=UTC_CATALOG, schema_name=UTC_SCHEMA)

    _utc_table(con, "stg_utc_po_driven", owner="stg_utc_po_owner", last_altered=AS_OF - timedelta(days=30))
    tmh(con, table_id="stg_utc_po_id", table_name="stg_utc_po_driven", snapshot_date_=D(2),
        active_bytes=10 * _GIB, active_files=5, catalog_name=UTC_CATALOG, schema_name=UTC_SCHEMA)
    po_op(con, table_id="stg_utc_po_id", table_name="stg_utc_po_driven", op_suffix="0",
          operation_status="SUCCESSFUL", start_time=t_win, usage_quantity=150.0,
          catalog_name=UTC_CATALOG, schema_name=UTC_SCHEMA)
    _utc_po_usage(con, sku_name="stg_UTC_PO_SKU", usage_quantity=100.0, usage_date_=D(2))
    _utc_list_price(con, "stg_UTC_PO_SKU", 1.00)

    _utc_table(con, "stg_utc_no_snapshot", owner="stg_utc_ns_owner", last_altered=AS_OF - timedelta(days=60))

    for i in range(_UTC_FILL_COUNT):
        name = f"stg_utc_fill_{i:04d}"
        _utc_table(con, name, owner="stg_utc_fill_owner", last_altered=AS_OF - timedelta(days=15))
        tmh(con, table_id=f"{name}_id", table_name=name, snapshot_date_=D(2),
            active_bytes=1 * _GIB, active_files=1, catalog_name=UTC_CATALOG, schema_name=UTC_SCHEMA)


# =================================================================================================
# build(con) -- auto-discovered by build_fixtures.py (DEC-17).
# =================================================================================================
def build(con: duckdb.DuckDBPyConnection) -> None:
    _build_po_failure_reasons(con)
    _build_small_files(con)
    _build_po_coverage(con)
    _build_growth(con)
    _build_unused_table_cost(con)
