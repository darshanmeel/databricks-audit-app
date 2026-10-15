"""tests/fixtures/top_by_cost.py -- fixture builder for the app-owned finding
`query_top_by_cost` (app/queries/app/performance/): fills system.query.history,
system.billing.usage and system.billing.list_prices, plus the one access__workspaces_latest row
this builder's own workspace needs (DEC-15: "every real workspace has a name").

Per DEC-15 this builder is self-contained and prefixes every id it writes with `tbc_`: workspace
`tbc_ws1`, account `tbc_acct`, warehouse ids `tbc_wh_*`, statement ids `tbc_st_*`. AS_OF =
2026-09-21 12:00:00 (base.AS_OF); every scenario sits on D = 2026-09-19 (AS_OF - 2 days), well
inside the 7/30/90-day windows and before today (excluded by every window's own `< current_date()`
predicate).

The query prices a SQL warehouse's billed usage by the hour at list rate, then splits that hour's
dollars across the statements that ran in it by total_task_duration_ms, and rolls the result up by
text hash (when statement_text is real) or by query_source origin (when it is NULL or
'<REDACTED>'), or into one "ad-hoc, text hidden" bucket per warehouse and user when neither is
available. Four warehouses, one scenario each (thresholds: warn_cost_usd 50, crit_cost_usd 200,
warn_share_frac 0.2, crit_share_frac 0.4):

  tbc_wh_dash   A dashboard run 20 times, statement_text '<REDACTED>' on every run (query_source.
                dashboard_id = tbc_dash_1). One billed hour, $200 (10 DBU @ $20), and this dashboard
                group is the ONLY thing that ran in it, so it takes the whole hour's cost and 100%
                of the warehouse's own window spend -> CRITICAL on both the dollar and share bands.
                15 runs by tbc_dash_user1, 5 by tbc_dash_user2 (top_user = user1, the more frequent
                one; distinct_users = 2).

  tbc_wh_job    One job task (query_source.job_info.job_id = tbc_job_1, job_task_run_id =
                tbc_task_1 -- "+ task if available"), statement_text '<REDACTED>' (falls back to
                origin, same as a NULL text would -- REDACTED, not NULL, so this row still forms a
                fingerprint group of its own in query_costly_statements_grouped's shared,
                unscoped-by-workspace GROUP BY, instead of colliding with a NULL one). 3 runs in
                one $60 hour it has entirely to itself,
                plus two more billed hours ($70 each) with NO statements at all (background/idle
                time that is billed but never allocated to any group) -- warehouse total $200, so
                this group's $60 is 30% of it: >= warn_cost_usd and >= warn_share_frac, below both
                CRITICAL bands -> WARN. executed_by is a bare GUID (a service principal), which the
                DEC-66.3 mask passes through unchanged.

  tbc_wh_hash   Two runs of the SAME shape with different literals (`region = 'east'` /
                `region = 'west'`), real, non-redacted text -- the de-valued-text sha2 hash groups
                them into ONE row (group_kind 'text hash') even though group_id never appears in
                query_source. $2 of a $202 warehouse total (a second, statement-free $200 hour
                dilutes the share) -> OK on both bands. One run is from_result_cache (share 0.5).
                Two different users (tbc_hash_userA/B, one run each) exercise the top_user tie-break
                (equal run counts -> the alphabetically-first raw executed_by wins).

  tbc_wh_adhoc  Two statements, both statement_text '<REDACTED>' and query_source entirely NULL --
                neither a text hash nor an origin, so each falls into its own "ad-hoc, text hidden"
                bucket, one per user (tbc_adhoc_user1, tbc_adhoc_user2), proving the per-warehouse-
                and-user split. $5 of a $50 warehouse total (a second, statement-free $45 hour
                dilutes the share), split evenly -> $2.50 / 5% each -> OK.

Stdlib + duckdb only.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from base import AS_OF, write_parquet  # noqa: F401 (write_parquet re-exported for callers)

WS = "tbc_ws1"
ACCOUNT_ID = "tbc_acct"

D = (AS_OF - timedelta(days=2)).date()  # 2026-09-19: inside every window, never "today"


def DT(hour: int, minute: int = 0) -> datetime:
    return datetime(D.year, D.month, D.day, hour, minute, 0)


# ---------------------------------------------------------------------------------------------
# billing.usage / billing.list_prices -- struct defaults copied from tests/fixtures/waste_usd.py's
# own shape (same DDL, same field set), per the per-builder isolation model, not imported.
# ---------------------------------------------------------------------------------------------
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


_USAGE_SQL = (
    "INSERT INTO billing__usage (account_id, workspace_id, record_id, sku_name, cloud, "
    "usage_start_time, usage_end_time, usage_date, custom_tags, usage_unit, usage_quantity, "
    "usage_metadata, identity_metadata, record_type, ingestion_date, billing_origin_product, "
    "product_features, usage_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _billing(con, record_id: str, *, warehouse_id: str, sku_name: str, hour: int, qty: float) -> None:
    start = DT(hour)
    end = start + timedelta(hours=1)
    con.execute(
        _USAGE_SQL,
        [
            ACCOUNT_ID, WS, record_id, sku_name, "aws", start, end, D,
            {}, "DBU", qty,
            _usage_metadata(warehouse_id=warehouse_id),
            _identity_metadata(),
            "ORIGINAL", D, "SQL",
            _product_features(is_serverless=False),
            "COMPUTE_TIME",
        ],
    )


_LP_SQL = (
    "INSERT INTO billing__list_prices (account_id, price_start_time, price_end_time, sku_name, "
    "cloud, currency_code, usage_unit, pricing) VALUES (?,?,?,?,?,?,?,?)"
)


def _list_price(con, sku_name: str, rate: float) -> None:
    con.execute(
        _LP_SQL,
        [
            ACCOUNT_ID, datetime(2020, 1, 1), None, sku_name, "aws", "USD", "DBU",
            {"default": rate, "promotional": {"default": None}, "effective_list": {"default": rate}},
        ],
    )


# ---------------------------------------------------------------------------------------------
# query.history -- column order copied from tests/fixtures/query_history.py's own QH_COLUMNS.
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
    "from_result_cache": False,
})


def _qsource(*, job_id=None, job_run_id=None, job_task_run_id=None, dashboard_id=None,
             legacy_dashboard_id=None, alert_id=None, notebook_id=None, sql_query_id=None,
             genie_space_id=None, pipeline_id=None, update_id=None):
    """query__history.query_source STRUCT, full shape. Pass nothing (query_source stays a Python
    None on the row -- the whole struct NULL) for the "neither" ad-hoc branch."""
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


def _stmt(sid: str, warehouse_id: str, start: datetime, *, executed_by: str, statement_text,
          query_source=None, weight_ms: int, exec_ms: int, read_bytes: int = 1000,
          spilled_local_bytes: int = 0, cached: bool = False) -> dict:
    r = dict(_DEFAULTS)
    r.update({
        "statement_id": sid,
        "executed_by": executed_by,
        "session_id": f"tbc_sess_{warehouse_id}",
        "compute": {"type": "WAREHOUSE", "cluster_id": None, "warehouse_id": warehouse_id},
        "statement_text": statement_text,
        "query_source": query_source,
        "total_duration_ms": weight_ms,
        "total_task_duration_ms": weight_ms,
        "execution_duration_ms": exec_ms,
        "start_time": start,
        "end_time": start + timedelta(milliseconds=exec_ms),
        "update_time": start + timedelta(milliseconds=exec_ms),
        "read_bytes": read_bytes,
        "spilled_local_bytes": spilled_local_bytes,
        "from_result_cache": cached,
    })
    return r


# ---------------------------------------------------------------------------------------------
# Scenario 1: tbc_wh_dash -- a redacted dashboard run 20 times -> CRITICAL.
# ---------------------------------------------------------------------------------------------
def _dash_rows() -> list[dict]:
    wh = "tbc_wh_dash"
    src = _qsource(dashboard_id="tbc_dash_1")
    rows = []
    for i in range(20):
        user = "tbc_dash_user1@example.com" if i < 15 else "tbc_dash_user2@example.com"
        rows.append(_stmt(
            f"tbc_st_dash_{i:02d}", wh, DT(10, i * 2), executed_by=user,
            statement_text="<REDACTED>", query_source=src, weight_ms=100, exec_ms=90,
        ))
    return rows


# ---------------------------------------------------------------------------------------------
# Scenario 2: tbc_wh_job -- one job task, redacted text -> WARN.
# ---------------------------------------------------------------------------------------------
def _job_rows() -> list[dict]:
    wh = "tbc_wh_job"
    src = _qsource(job_id="tbc_job_1", job_run_id="tbc_job_1_run1", job_task_run_id="tbc_task_1")
    user = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    return [
        _stmt(f"tbc_st_job_{i}", wh, DT(11, 10 * i), executed_by=user, statement_text="<REDACTED>",
              query_source=src, weight_ms=1000, exec_ms=900, read_bytes=2000,
              spilled_local_bytes=500_000)
        for i in range(3)
    ]


# ---------------------------------------------------------------------------------------------
# Scenario 3: tbc_wh_hash -- two real-text runs, same shape, different literal -> one text-hash
# group -> OK.
# ---------------------------------------------------------------------------------------------
def _hash_rows() -> list[dict]:
    wh = "tbc_wh_hash"
    return [
        _stmt("tbc_st_hash_east", wh, DT(14, 0), executed_by="tbc_hash_userA@example.com",
              statement_text="SELECT * FROM tbc_sales WHERE region = 'east'",
              weight_ms=50, exec_ms=45, read_bytes=500, cached=False),
        _stmt("tbc_st_hash_west", wh, DT(14, 5), executed_by="tbc_hash_userB@example.com",
              statement_text="SELECT * FROM tbc_sales WHERE region = 'west'",
              weight_ms=50, exec_ms=45, read_bytes=500, cached=True),
    ]


# ---------------------------------------------------------------------------------------------
# Scenario 4: tbc_wh_adhoc -- two redacted, source-less statements, one per user -> two
# "ad-hoc, text hidden" groups -> OK.
# ---------------------------------------------------------------------------------------------
def _adhoc_rows() -> list[dict]:
    wh = "tbc_wh_adhoc"
    return [
        _stmt("tbc_st_adhoc_1", wh, DT(16, 0), executed_by="tbc_adhoc_user1@example.com",
              statement_text="<REDACTED>", weight_ms=250, exec_ms=200, read_bytes=10_000),
        _stmt("tbc_st_adhoc_2", wh, DT(16, 10), executed_by="tbc_adhoc_user2@example.com",
              statement_text="<REDACTED>", weight_ms=250, exec_ms=210, read_bytes=12_000),
    ]


# ---------------------------------------------------------------------------------------------
# billing rows for every scenario.
# ---------------------------------------------------------------------------------------------
def _write_billing(con) -> None:
    _list_price(con, "tbc_SQL_HI", 20.0)
    _list_price(con, "tbc_SQL_STD", 1.0)

    _billing(con, "tbc_bl_dash", warehouse_id="tbc_wh_dash", sku_name="tbc_SQL_HI", hour=10, qty=10.0)

    _billing(con, "tbc_bl_job_1", warehouse_id="tbc_wh_job", sku_name="tbc_SQL_STD", hour=11, qty=60.0)
    _billing(con, "tbc_bl_job_2", warehouse_id="tbc_wh_job", sku_name="tbc_SQL_STD", hour=12, qty=70.0)
    _billing(con, "tbc_bl_job_3", warehouse_id="tbc_wh_job", sku_name="tbc_SQL_STD", hour=13, qty=70.0)

    _billing(con, "tbc_bl_hash_1", warehouse_id="tbc_wh_hash", sku_name="tbc_SQL_STD", hour=14, qty=2.0)
    _billing(con, "tbc_bl_hash_2", warehouse_id="tbc_wh_hash", sku_name="tbc_SQL_STD", hour=15, qty=200.0)

    _billing(con, "tbc_bl_adhoc_1", warehouse_id="tbc_wh_adhoc", sku_name="tbc_SQL_STD", hour=16, qty=5.0)
    _billing(con, "tbc_bl_adhoc_2", warehouse_id="tbc_wh_adhoc", sku_name="tbc_SQL_STD", hour=17, qty=45.0)


_WS_SQL = (
    "INSERT INTO access__workspaces_latest "
    "(account_id, workspace_id, workspace_name, workspace_url, create_time, status) "
    "VALUES (?,?,?,?,?,?)"
)


# ---------------------------------------------------------------------------------------------
# Auto-discovered by build_fixtures.py (DEC-17).
# ---------------------------------------------------------------------------------------------
def build(con) -> None:
    con.execute(_WS_SQL, [ACCOUNT_ID, WS, "tbc-top-by-cost", None, datetime(2025, 1, 1), "RUNNING"])

    rows = _dash_rows() + _job_rows() + _hash_rows() + _adhoc_rows()
    seen = set()
    for r in rows:
        sid = r["statement_id"]
        if sid in seen:
            raise ValueError(f"duplicate statement_id in top_by_cost.py fixture: {sid!r}")
        seen.add(sid)
        if not sid.startswith("tbc_st_"):
            raise ValueError(f"unprefixed statement_id in top_by_cost.py fixture: {sid!r}")
        wh = r["compute"]["warehouse_id"]
        if not wh.startswith("tbc_wh_"):
            raise ValueError(f"unprefixed warehouse_id in top_by_cost.py fixture: {wh!r}")

    cols_sql = ", ".join(f'"{c}"' for c in QH_COLUMNS)
    placeholders = ", ".join("?" for _ in QH_COLUMNS)
    con.executemany(
        f"INSERT INTO query__history ({cols_sql}) VALUES ({placeholders})",
        [[r[c] for c in QH_COLUMNS] for r in rows],
    )

    _write_billing(con)
