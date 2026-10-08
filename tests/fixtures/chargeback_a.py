"""tests/fixtures/chargeback_a.py -- the builder for this file's five new cost_chargeback_by_*
checks (domain cost): cost_chargeback_by_warehouse, cost_chargeback_identity_by_source,
cost_chargeback_by_service, cost_chargeback_by_sku, cost_chargeback_by_workspace. Follows
tests/fixtures/chargeback.py's own builder shape (module-level build(con), AS_OF from base.py,
write_parquet re-exported for build_fixtures.py, parameterized `?` INSERTs, one Python dict per
STRUCT column) and fills only what these five queries read: system.billing.usage,
system.billing.list_prices, system.query.history and system.compute.clusters.

Every id this file writes (workspace_id, warehouse_id, sku_name, identity/owned_by strings) carries
the `cba_` prefix, disjoint from every other builder's own prefix on the same shared tables. Each
of the five checks gets its OWN dedicated workspace (WS_WH/WS_SVC/WS_SKU/WS_ID) or dedicated
workspace-per-scenario (WS_OK/WS_WARN/WS_CRIT/... for cost_chargeback_by_workspace itself) so one
check's scenario rows never accidentally land inside another check's own scenario bucket by
sharing a workspace_id, sku_name or billing_origin_product value it did not choose on purpose.
Every row also carries a unique identity_metadata.owned_by string (never left blank/'unknown')
purely so cost_chargeback_identity_by_source's three sources - which read this same
system.billing.usage table account-wide - never merge two unrelated scenarios' dollars into one
shared 'unknown' bucket; only the rows that are THIS builder's own identity scenarios deliberately
leave identity unset.

Two checks (cost_chargeback_by_warehouse, cost_chargeback_identity_by_source) declare a :top_n
pooling cutoff (default 20): a real finding (WARN/CRITICAL) always keeps its own row, and OK and
NOT_ASSESSED rows beyond the top :top_n by spend each get pooled into their own is_other=true row
(one per status - see each query's own caveats). This builder proves the CAP holds for OK rows
(pooling actually happens once more than :top_n small OK rows exist)
by writing 25 deliberately tiny ($1, current-only, no previous data) OK-status rows per check
rather than asserting an exact pooled_count or exact "other" total - the pooling stage ranks
EVERY OK row for that query account-wide, so an exact split can only be pinned inside a builder
that owns the whole account's OK-status rows for that query, which this one does not (another
fixture module could add more). What IS provable regardless of any sibling builder:
(a) at most :top_n of this builder's own tiny rows are ever individually listed (the cap can never
be exceeded), and (b) since 25 > :top_n=20, at least one of them is never individually listed (real
pooling occurred); the corresponding test file asserts exactly these two bounds.

cost_chargeback_identity_by_source's per-source NOT_ASSESSED gate (current_period_unpriced /
previous_period_unpriced) is computed once per source, from that source's OWN raw usage rows,
account-wide (see that query's own caveats) - not per identity - so it cannot be exercised by a
single scenario the way the other four checks' per-row gates can: the gate only fires when EVERY
row of that source, across the WHOLE account and window side, was unpriced, which this builder's
other (properly priced) sql_warehouse/jobs/other rows structurally prevent. This builder therefore
does not attempt a NOT_ASSESSED scenario for that one query; its own test file covers OK/WARN/
CRITICAL across all three sources instead.

------------------------------------------------------------------------------------------------
Scenario map (thresholds shared by all five checks: warn_increase_pct=25, crit_increase_pct=50,
min_spend_usd=20, top_n=20; "current" rows are D(5), "previous" rows D(40), unless noted):
------------------------------------------------------------------------------------------------
cost_chargeback_by_warehouse (workspace WS_WH, billing_origin_product='SQL', usage_unit='DBU')
  cba_wh_ok      cur=30, prev=28   -> change  7.1%  -> OK   (+3 query.history rows -> queries=3,
                                                              usd_per_1000_queries=10000.0)
  cba_wh_warn    cur=125, prev=100 -> change 25.0%  -> WARN (exact boundary, inclusive)
  cba_wh_crit    cur=200, prev=100 -> change100.0%  -> CRITICAL
  cba_wh_new     cur=50, no previous row at all      -> CRITICAL (brand-new spend, eff_prev=0)
  cba_wh_na_cur  cur unpriced (40, no list_price row), prev priced (30) -> NOT_ASSESSED
                 (current_period_unpriced)
  cba_wh_na_prev cur priced (50), prev unpriced (20, no list_price row) -> NOT_ASSESSED
                 (previous_period_unpriced)
  cba_wh_win     D(5)=5, D(20)=7, D(45)=11, D0=999(excluded) -> 5 @ w=7, 12 @ w=30, 23 @ w=90
  cba_wh_pool_00..24 (25 warehouses) cur=1 each, no previous -> OK, :top_n pooling cap proof

cost_chargeback_identity_by_source (workspace WS_ID)
  sql_warehouse  cba_id_wh1: cur=125, prev=100, 100% of both days' query.history duration
                 attributed to cba_id_alice@example.com -> WARN
  jobs           cba_id_job1 (usage_metadata.job_id set): cur=60, no previous ->
                 owned_by=cba_id_job_owner@example.com -> CRITICAL (brand-new)
  other          cba_id_cluster1 (usage_metadata.cluster_id set, identity_metadata blank):
                 system.compute.clusters gives it owned_by=cba_id_cluster_owner@example.com (the
                 all-purpose fallback) -- cur=40, prev=38 -> change 5.3% -> OK
  other (pool)   cba_id_pool_00..24 (25 identities, owned_by set directly) cur=1 each, no
                 previous -> OK, :top_n pooling cap proof (see module docstring)

cost_chargeback_by_service (workspace WS_SVC; each scenario its own SYNTHETIC
billing_origin_product - this query groups by billing_origin_product alone, account-wide, so a
real Databricks product name like 'JOBS' risks silently merging with another builder's own fixture)
  CBA_SVC_OK     cur=40, prev=38   -> change  5.3%  -> OK
  CBA_SVC_WARN   cur=125, prev=100 -> change 25.0%  -> WARN
  CBA_SVC_CRIT   cur=300, prev=100 -> change200.0%  -> CRITICAL
  CBA_SVC_NA     cur unpriced (40), prev priced (30) -> NOT_ASSESSED (current_period_unpriced)
  NULL (unattributed) cur=15 only (< min_spend_usd) -> OK, kept as its own "Unattributed" line

cost_chargeback_by_sku (workspace WS_SKU; each scenario its own sku_name)
  cba_SKU_A (DBU)     cur=40, prev=38   -> change  5.3%  -> OK
  cba_SKU_B (DBU)     cur=125, prev=100 -> change 25.0%  -> WARN
  cba_SKU_C (DBU)     cur=200, prev=100 -> change100.0%  -> CRITICAL
  cba_SKU_NA1 (DBU)   cur=40 (priced), prev=20 (list_price starts AFTER D(40), so the previous
                      row has no matching price) -> NOT_ASSESSED (previous_period_unpriced)
  cba_SKU_STORAGE (GB) cur=100, prev=95 -> change 5.3% -> OK; dbus is NULL (non-DBU unit)

cost_chargeback_by_workspace (one dedicated workspace per scenario)
  cba_ws_ok      cur=40, prev=38   -> change  5.3%  -> OK
  cba_ws_warn    cur=125, prev=100 -> change 25.0%  -> WARN
  cba_ws_crit    cur=200, prev=100 -> change100.0%  -> CRITICAL
  cba_ws_na_cur  cur unpriced (40), prev priced (30) -> NOT_ASSESSED (current_period_unpriced)
  NULL (account-level) cur=15 only (< min_spend_usd) -> OK, kept as its own line
  cba_ws_win     D(5)=5, D(20)=7, D(45)=11, D0=999(excluded) -> 5 @ w=7, 12 @ w=30, 23 @ w=90
------------------------------------------------------------------------------------------------
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import duckdb

from base import AS_OF, write_parquet  # noqa: F401 (write_parquet re-exported for build_fixtures.py)

D0 = AS_OF.date()


def D(n: int) -> date:
    return D0 - timedelta(days=n)


ACCOUNT_ID = "cba_acct"

# ---- dedicated workspaces, one per check (or per scenario for cost_chargeback_by_workspace) ----
WS_WH = "cba_ws_wh"
WS_ID = "cba_ws_id"
WS_SVC = "cba_ws_svc"
WS_SKU = "cba_ws_sku"
WS_OK = "cba_ws_ok"
WS_WARN = "cba_ws_warn"
WS_CRIT = "cba_ws_crit"
WS_NA_CUR = "cba_ws_na_cur"
WS_WIN = "cba_ws_win"

TOP_N_POOL = 25  # > the :top_n default (20) on both pooled checks, to prove the cap holds


# ---------------------------------------------------------------------------------------------
# STRUCT defaults -- same shape tests/fixtures/chargeback.py already uses (ddl.py's own DDL);
# every field defaulted to None/False, then overridden only where a scenario needs it.
# ---------------------------------------------------------------------------------------------
def _usage_metadata(*, cluster_id=None, job_id=None, warehouse_id=None):
    return {
        "cluster_id": cluster_id, "job_id": job_id, "warehouse_id": warehouse_id,
        "instance_pool_id": None, "node_type": None, "job_run_id": None, "notebook_id": None,
        "dlt_pipeline_id": None, "endpoint_name": None, "endpoint_id": None, "dlt_update_id": None,
        "dlt_maintenance_id": None, "run_name": None, "job_name": None, "notebook_path": None,
        "central_clean_room_id": None, "source_region": None, "destination_region": None,
        "app_id": None, "app_name": None, "metastore_id": None, "private_endpoint_name": None,
        "storage_api_type": None, "budget_policy_id": None, "ai_runtime_pool_id": None,
        "catalog_id": None, "networking_client": None, "recipient_id": None, "usage_policy_id": None,
    }


def _identity_metadata(*, owned_by=None, run_as=None):
    return {"run_as": run_as, "created_by": None, "owned_by": owned_by, "run_by": None}


def _product_features():
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


# ---------------------------------------------------------------------------------------------
# billing.usage / billing.list_prices insert helpers
# ---------------------------------------------------------------------------------------------
_USAGE_SQL = (
    "INSERT INTO billing__usage (account_id, workspace_id, record_id, sku_name, cloud, "
    "usage_start_time, usage_end_time, usage_date, custom_tags, usage_unit, usage_quantity, "
    "usage_metadata, identity_metadata, record_type, ingestion_date, billing_origin_product, "
    "product_features, usage_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)

_seq = [0]


def _next_record_id() -> str:
    _seq[0] += 1
    return f"cba_r_{_seq[0]:04d}"


def usage(
    con: duckdb.DuckDBPyConnection,
    *,
    sku_name: str,
    usage_date_: date,
    usage_quantity: float,
    usage_unit: str = "DBU",
    workspace_id: str | None,
    billing_origin_product: str | None = "ALL_PURPOSE",
    warehouse_id: str | None = None,
    job_id: str | None = None,
    cluster_id: str | None = None,
    owned_by: str | None = None,
) -> None:
    start = datetime.combine(usage_date_, datetime.min.time())
    end = start + timedelta(hours=1)
    con.execute(
        _USAGE_SQL,
        [
            ACCOUNT_ID, workspace_id, _next_record_id(), sku_name, "aws", start, end, usage_date_,
            {}, usage_unit, usage_quantity,
            _usage_metadata(cluster_id=cluster_id, job_id=job_id, warehouse_id=warehouse_id),
            _identity_metadata(owned_by=owned_by),
            "ORIGINAL", usage_date_, billing_origin_product, _product_features(), "COMPUTE_TIME",
        ],
    )


_LP_SQL = (
    "INSERT INTO billing__list_prices (account_id, price_start_time, price_end_time, sku_name, "
    "cloud, currency_code, usage_unit, pricing) VALUES (?,?,?,?,?,?,?,?)"
)


def list_price(
    con: duckdb.DuckDBPyConnection,
    sku_name: str,
    usage_unit: str,
    effective_list_default: float = 1.00,
    *,
    price_start_time: datetime | None = None,
    price_end_time: datetime | None = None,
) -> None:
    if price_start_time is None:
        price_start_time = AS_OF - timedelta(days=300)
    con.execute(
        _LP_SQL,
        [
            ACCOUNT_ID, price_start_time, price_end_time, sku_name, "aws", "USD", usage_unit,
            {"default": effective_list_default, "promotional": {"default": None},
             "effective_list": {"default": effective_list_default}},
        ],
    )


# ---------------------------------------------------------------------------------------------
# query.history insert helper (cost_chargeback_by_warehouse's queries/usd_per_1000_queries, and
# cost_chargeback_identity_by_source's sql_warehouse duration split)
# ---------------------------------------------------------------------------------------------
_QH_COLUMNS = [
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
_QH_DEFAULTS = {c: None for c in _QH_COLUMNS}
_QH_DEFAULTS.update({
    "account_id": ACCOUNT_ID, "execution_status": "FINISHED", "statement_type": "SELECT",
    "statement_text": "SELECT 1", "from_result_cache": False, "total_duration_ms": 1000,
})
_QH_SQL = (
    'INSERT INTO query__history (' + ", ".join(f'"{c}"' for c in _QH_COLUMNS) + ') VALUES ('
    + ", ".join("?" for _ in _QH_COLUMNS) + ")"
)
_qh_seq = [0]


def query_history_row(
    con: duckdb.DuckDBPyConnection,
    *,
    workspace_id: str,
    warehouse_id: str,
    start_time: datetime,
    executed_by: str | None = None,
    total_duration_ms: int = 1000,
) -> None:
    _qh_seq[0] += 1
    row = dict(_QH_DEFAULTS)
    row.update({
        "workspace_id": workspace_id,
        "statement_id": f"cba_qh_{_qh_seq[0]:04d}",
        "executed_by": executed_by,
        "compute": {"type": "PRO_WAREHOUSE", "cluster_id": None, "warehouse_id": warehouse_id},
        "total_duration_ms": total_duration_ms,
        "start_time": start_time,
        "end_time": start_time + timedelta(milliseconds=total_duration_ms),
    })
    con.execute(_QH_SQL, [row[c] for c in _QH_COLUMNS])


# ---------------------------------------------------------------------------------------------
# compute.clusters insert helper (cost_chargeback_identity_by_source's all-purpose owned_by
# fallback) -- only the columns that query's own latest_clusters CTE reads plus the SCD2 key.
# ---------------------------------------------------------------------------------------------
_CL_SQL = (
    "INSERT INTO compute__clusters "
    "(account_id, workspace_id, cluster_id, cluster_name, owned_by, create_time, delete_time, "
    "driver_node_type, worker_node_type, worker_count, min_autoscale_workers, "
    "max_autoscale_workers, auto_termination_minutes, enable_elastic_disk, tags, cluster_source, "
    "init_scripts, aws_attributes, azure_attributes, gcp_attributes, driver_instance_pool_id, "
    "worker_instance_pool_id, dbr_version, change_time, change_date, data_security_mode, "
    "policy_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def cluster(con: duckdb.DuckDBPyConnection, *, workspace_id: str, cluster_id: str, owned_by: str) -> None:
    change_time = AS_OF - timedelta(days=200)
    con.execute(_CL_SQL, [
        ACCOUNT_ID, workspace_id, cluster_id, "cba-cluster", owned_by,
        change_time - timedelta(days=1), None, "i3.xlarge", "i3.xlarge", 2, None, None, 60, True,
        {}, "UI", [], None, None, None, None, None, "14.3.x-scala2.12", change_time,
        change_time.date(), "SINGLE_USER", None,
    ])


# ---------------------------------------------------------------------------------------------
# Auto-discovered by build_fixtures.py (DEC-17): a module-level build(con), no registration list.
# ---------------------------------------------------------------------------------------------
def build(con: duckdb.DuckDBPyConnection) -> None:
    # =========================================================================================
    # cost_chargeback_by_warehouse (WS_WH, billing_origin_product='SQL', usage_unit='DBU')
    # =========================================================================================
    list_price(con, "cba_SKU_WH", "DBU", 1.00)

    usage(con, sku_name="cba_SKU_WH", usage_date_=D(5), usage_quantity=30.0,
          workspace_id=WS_WH, billing_origin_product="SQL", warehouse_id="cba_wh_ok",
          owned_by="cba_wh_ok_owner")
    usage(con, sku_name="cba_SKU_WH", usage_date_=D(40), usage_quantity=28.0,
          workspace_id=WS_WH, billing_origin_product="SQL", warehouse_id="cba_wh_ok",
          owned_by="cba_wh_ok_owner")
    for i in range(3):
        query_history_row(con, workspace_id=WS_WH, warehouse_id="cba_wh_ok",
                           start_time=datetime.combine(D(5), datetime.min.time()) + timedelta(hours=i),
                           executed_by="cba_wh_ok_user@example.com")

    usage(con, sku_name="cba_SKU_WH", usage_date_=D(5), usage_quantity=125.0,
          workspace_id=WS_WH, billing_origin_product="SQL", warehouse_id="cba_wh_warn",
          owned_by="cba_wh_warn_owner")
    usage(con, sku_name="cba_SKU_WH", usage_date_=D(40), usage_quantity=100.0,
          workspace_id=WS_WH, billing_origin_product="SQL", warehouse_id="cba_wh_warn",
          owned_by="cba_wh_warn_owner")

    usage(con, sku_name="cba_SKU_WH", usage_date_=D(5), usage_quantity=200.0,
          workspace_id=WS_WH, billing_origin_product="SQL", warehouse_id="cba_wh_crit",
          owned_by="cba_wh_crit_owner")
    usage(con, sku_name="cba_SKU_WH", usage_date_=D(40), usage_quantity=100.0,
          workspace_id=WS_WH, billing_origin_product="SQL", warehouse_id="cba_wh_crit",
          owned_by="cba_wh_crit_owner")

    usage(con, sku_name="cba_SKU_WH", usage_date_=D(5), usage_quantity=50.0,
          workspace_id=WS_WH, billing_origin_product="SQL", warehouse_id="cba_wh_new",
          owned_by="cba_wh_new_owner")

    list_price(con, "cba_SKU_WH_NA1", "DBU", 1.00)
    usage(con, sku_name="cba_SKU_WH_UNPRICED_A", usage_date_=D(5), usage_quantity=40.0,
          workspace_id=WS_WH, billing_origin_product="SQL", warehouse_id="cba_wh_na_cur",
          owned_by="cba_wh_na_cur_owner")
    usage(con, sku_name="cba_SKU_WH_NA1", usage_date_=D(40), usage_quantity=30.0,
          workspace_id=WS_WH, billing_origin_product="SQL", warehouse_id="cba_wh_na_cur",
          owned_by="cba_wh_na_cur_owner")

    list_price(con, "cba_SKU_WH_NA2", "DBU", 1.00)
    usage(con, sku_name="cba_SKU_WH_NA2", usage_date_=D(5), usage_quantity=50.0,
          workspace_id=WS_WH, billing_origin_product="SQL", warehouse_id="cba_wh_na_prev",
          owned_by="cba_wh_na_prev_owner")
    usage(con, sku_name="cba_SKU_WH_UNPRICED_B", usage_date_=D(40), usage_quantity=20.0,
          workspace_id=WS_WH, billing_origin_product="SQL", warehouse_id="cba_wh_na_prev",
          owned_by="cba_wh_na_prev_owner")

    for d, qty in [(D(5), 5.0), (D(20), 7.0), (D(45), 11.0), (D0, 999.0)]:
        usage(con, sku_name="cba_SKU_WH", usage_date_=d, usage_quantity=qty,
              workspace_id=WS_WH, billing_origin_product="SQL", warehouse_id="cba_wh_win",
              owned_by="cba_wh_win_owner")

    for i in range(TOP_N_POOL):
        usage(con, sku_name="cba_SKU_WH", usage_date_=D(5), usage_quantity=1.0,
              workspace_id=WS_WH, billing_origin_product="SQL",
              warehouse_id=f"cba_wh_pool_{i:02d}", owned_by=f"cba_wh_pool_{i:02d}_owner")

    # =========================================================================================
    # cost_chargeback_identity_by_source (WS_ID)
    # =========================================================================================
    list_price(con, "cba_SKU_ID_WH", "DBU", 1.00)
    usage(con, sku_name="cba_SKU_ID_WH", usage_date_=D(5), usage_quantity=125.0,
          workspace_id=WS_ID, billing_origin_product="SQL", warehouse_id="cba_id_wh1")
    usage(con, sku_name="cba_SKU_ID_WH", usage_date_=D(40), usage_quantity=100.0,
          workspace_id=WS_ID, billing_origin_product="SQL", warehouse_id="cba_id_wh1")
    query_history_row(con, workspace_id=WS_ID, warehouse_id="cba_id_wh1",
                       start_time=datetime.combine(D(5), datetime.min.time()),
                       executed_by="cba_id_alice@example.com", total_duration_ms=500)
    query_history_row(con, workspace_id=WS_ID, warehouse_id="cba_id_wh1",
                       start_time=datetime.combine(D(40), datetime.min.time()),
                       executed_by="cba_id_alice@example.com", total_duration_ms=500)

    list_price(con, "cba_SKU_ID_JOB", "DBU", 1.00)
    usage(con, sku_name="cba_SKU_ID_JOB", usage_date_=D(5), usage_quantity=60.0,
          workspace_id=WS_ID, billing_origin_product="JOBS", job_id="cba_id_job1",
          owned_by="cba_id_job_owner@example.com")

    cluster(con, workspace_id=WS_ID, cluster_id="cba_id_cluster1",
            owned_by="cba_id_cluster_owner@example.com")
    list_price(con, "cba_SKU_ID_OTHER", "DBU", 1.00)
    usage(con, sku_name="cba_SKU_ID_OTHER", usage_date_=D(5), usage_quantity=40.0,
          workspace_id=WS_ID, billing_origin_product="ALL_PURPOSE", cluster_id="cba_id_cluster1")
    usage(con, sku_name="cba_SKU_ID_OTHER", usage_date_=D(40), usage_quantity=38.0,
          workspace_id=WS_ID, billing_origin_product="ALL_PURPOSE", cluster_id="cba_id_cluster1")

    for i in range(TOP_N_POOL):
        usage(con, sku_name="cba_SKU_ID_OTHER", usage_date_=D(5), usage_quantity=1.0,
              workspace_id=WS_ID, billing_origin_product="ALL_PURPOSE",
              owned_by=f"cba_id_pool_{i:02d}@example.com")

    # =========================================================================================
    # cost_chargeback_by_service (WS_SVC; each scenario its own billing_origin_product)
    # =========================================================================================
    # Synthetic, cba_-prefixed billing_origin_product values (never a real Databricks product
    # name): cost_chargeback_by_service groups by billing_origin_product ALONE, account-wide, so a
    # real name like 'JOBS' could be shared with another builder's own chargeback fixture and
    # silently combine dollars into one row neither builder controls alone.
    list_price(con, "cba_SKU_SVC_OK", "DBU", 1.00)
    usage(con, sku_name="cba_SKU_SVC_OK", usage_date_=D(5), usage_quantity=40.0,
          workspace_id=WS_SVC, billing_origin_product="CBA_SVC_OK", owned_by="cba_svc_ok_owner")
    usage(con, sku_name="cba_SKU_SVC_OK", usage_date_=D(40), usage_quantity=38.0,
          workspace_id=WS_SVC, billing_origin_product="CBA_SVC_OK", owned_by="cba_svc_ok_owner")

    list_price(con, "cba_SKU_SVC_WARN", "DBU", 1.00)
    usage(con, sku_name="cba_SKU_SVC_WARN", usage_date_=D(5), usage_quantity=125.0,
          workspace_id=WS_SVC, billing_origin_product="CBA_SVC_WARN", owned_by="cba_svc_warn_owner")
    usage(con, sku_name="cba_SKU_SVC_WARN", usage_date_=D(40), usage_quantity=100.0,
          workspace_id=WS_SVC, billing_origin_product="CBA_SVC_WARN", owned_by="cba_svc_warn_owner")

    list_price(con, "cba_SKU_SVC_CRIT", "DBU", 1.00)
    usage(con, sku_name="cba_SKU_SVC_CRIT", usage_date_=D(5), usage_quantity=300.0,
          workspace_id=WS_SVC, billing_origin_product="CBA_SVC_CRIT", owned_by="cba_svc_crit_owner")
    usage(con, sku_name="cba_SKU_SVC_CRIT", usage_date_=D(40), usage_quantity=100.0,
          workspace_id=WS_SVC, billing_origin_product="CBA_SVC_CRIT", owned_by="cba_svc_crit_owner")

    list_price(con, "cba_SKU_SVC_OK2", "DBU", 1.00)
    usage(con, sku_name="cba_SKU_SVC_UNPRICED", usage_date_=D(5), usage_quantity=40.0,
          workspace_id=WS_SVC, billing_origin_product="CBA_SVC_NA", owned_by="cba_svc_na_owner")
    usage(con, sku_name="cba_SKU_SVC_OK2", usage_date_=D(40), usage_quantity=30.0,
          workspace_id=WS_SVC, billing_origin_product="CBA_SVC_NA", owned_by="cba_svc_na_owner")

    list_price(con, "cba_SKU_SVC_UNATTR", "DBU", 1.00)
    usage(con, sku_name="cba_SKU_SVC_UNATTR", usage_date_=D(5), usage_quantity=15.0,
          workspace_id=WS_SVC, billing_origin_product=None, owned_by="cba_svc_unattr_owner")

    # =========================================================================================
    # cost_chargeback_by_sku (WS_SKU; each scenario its own sku_name)
    # =========================================================================================
    list_price(con, "cba_SKU_A", "DBU", 1.00)
    usage(con, sku_name="cba_SKU_A", usage_date_=D(5), usage_quantity=40.0,
          workspace_id=WS_SKU, owned_by="cba_sku_a_owner")
    usage(con, sku_name="cba_SKU_A", usage_date_=D(40), usage_quantity=38.0,
          workspace_id=WS_SKU, owned_by="cba_sku_a_owner")

    list_price(con, "cba_SKU_B", "DBU", 1.00)
    usage(con, sku_name="cba_SKU_B", usage_date_=D(5), usage_quantity=125.0,
          workspace_id=WS_SKU, owned_by="cba_sku_b_owner")
    usage(con, sku_name="cba_SKU_B", usage_date_=D(40), usage_quantity=100.0,
          workspace_id=WS_SKU, owned_by="cba_sku_b_owner")

    list_price(con, "cba_SKU_C", "DBU", 1.00)
    usage(con, sku_name="cba_SKU_C", usage_date_=D(5), usage_quantity=200.0,
          workspace_id=WS_SKU, owned_by="cba_sku_c_owner")
    usage(con, sku_name="cba_SKU_C", usage_date_=D(40), usage_quantity=100.0,
          workspace_id=WS_SKU, owned_by="cba_sku_c_owner")

    # cba_SKU_NA1's price only STARTS 20 days ago -- the D(40) (previous) row predates it and is
    # unpriced, while the D(5) (current) row is priced -> NOT_ASSESSED (previous_period_unpriced).
    list_price(con, "cba_SKU_NA1", "DBU", 1.00, price_start_time=AS_OF - timedelta(days=20))
    usage(con, sku_name="cba_SKU_NA1", usage_date_=D(5), usage_quantity=40.0,
          workspace_id=WS_SKU, owned_by="cba_sku_na_owner")
    usage(con, sku_name="cba_SKU_NA1", usage_date_=D(40), usage_quantity=20.0,
          workspace_id=WS_SKU, owned_by="cba_sku_na_owner")

    list_price(con, "cba_SKU_STORAGE", "GB", 1.00)
    usage(con, sku_name="cba_SKU_STORAGE", usage_date_=D(5), usage_quantity=100.0,
          usage_unit="GB", workspace_id=WS_SKU, billing_origin_product="STORAGE",
          owned_by="cba_sku_storage_owner")
    usage(con, sku_name="cba_SKU_STORAGE", usage_date_=D(40), usage_quantity=95.0,
          usage_unit="GB", workspace_id=WS_SKU, billing_origin_product="STORAGE",
          owned_by="cba_sku_storage_owner")

    # =========================================================================================
    # cost_chargeback_by_workspace (one dedicated workspace per scenario)
    # =========================================================================================
    list_price(con, "cba_SKU_WS_OK", "DBU", 1.00)
    usage(con, sku_name="cba_SKU_WS_OK", usage_date_=D(5), usage_quantity=40.0,
          workspace_id=WS_OK, owned_by="cba_ws_ok_owner")
    usage(con, sku_name="cba_SKU_WS_OK", usage_date_=D(40), usage_quantity=38.0,
          workspace_id=WS_OK, owned_by="cba_ws_ok_owner")

    list_price(con, "cba_SKU_WS_WARN", "DBU", 1.00)
    usage(con, sku_name="cba_SKU_WS_WARN", usage_date_=D(5), usage_quantity=125.0,
          workspace_id=WS_WARN, owned_by="cba_ws_warn_owner")
    usage(con, sku_name="cba_SKU_WS_WARN", usage_date_=D(40), usage_quantity=100.0,
          workspace_id=WS_WARN, owned_by="cba_ws_warn_owner")

    list_price(con, "cba_SKU_WS_CRIT", "DBU", 1.00)
    usage(con, sku_name="cba_SKU_WS_CRIT", usage_date_=D(5), usage_quantity=200.0,
          workspace_id=WS_CRIT, owned_by="cba_ws_crit_owner")
    usage(con, sku_name="cba_SKU_WS_CRIT", usage_date_=D(40), usage_quantity=100.0,
          workspace_id=WS_CRIT, owned_by="cba_ws_crit_owner")

    list_price(con, "cba_SKU_WS_OK2", "DBU", 1.00)
    usage(con, sku_name="cba_SKU_WS_UNPRICED", usage_date_=D(5), usage_quantity=40.0,
          workspace_id=WS_NA_CUR, owned_by="cba_ws_na_cur_owner")
    usage(con, sku_name="cba_SKU_WS_OK2", usage_date_=D(40), usage_quantity=30.0,
          workspace_id=WS_NA_CUR, owned_by="cba_ws_na_cur_owner")

    list_price(con, "cba_SKU_WS_ACCT", "DBU", 1.00)
    usage(con, sku_name="cba_SKU_WS_ACCT", usage_date_=D(5), usage_quantity=15.0,
          workspace_id=None, owned_by="cba_ws_acct_owner")

    list_price(con, "cba_SKU_WS_WIN", "DBU", 1.00)
    for d, qty in [(D(5), 5.0), (D(20), 7.0), (D(45), 11.0), (D0, 999.0)]:
        usage(con, sku_name="cba_SKU_WS_WIN", usage_date_=d, usage_quantity=qty,
              workspace_id=WS_WIN, owned_by="cba_ws_win_owner")
