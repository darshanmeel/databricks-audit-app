"""tests/fixtures/hour_of_day.py -- the scenarios cost_by_hour_of_day is tested on.

Writes, in workspace 9301 only (a fresh workspace, not shared with any other builder), all on the
SAME UTC calendar day (DAY0 = AS_OF - 2 days) so every row lands on one weekday and the
weekday-collapsing peak flag needs no cross-day bookkeeping to reason about:

  billing__usage, billing__list_prices, lakeflow__job_run_timeline, query__history

One SKU (hod_STANDARD_COMPUTE) priced at exactly $1.00 / DBU, so usage_quantity IS the dollar
amount -- no rounding to track through the price join. Total window spend for this workspace is
$1000, split across hour-of-day slots (thresholds at their header defaults: warn_hour_share_pct
15, crit_hour_share_pct 25):

  hour 2   $300 (JOBS $200 + ALL_PURPOSE $100, two product rows, one slot) -> share 30% -> CRITICAL
           also carries 3 distinct job runs active (job_runs_active), broadcast on BOTH product rows
  hour 9   $180 (SQL)                                                      -> share 18% -> WARN
           also carries 4 queries started (queries_started)
  hour 5, 11, 16, 20  $100 each (JOBS)                                     -> share 10% -> OK
  hour 23  $120 (JOBS)                                                     -> share 12% -> OK
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta

import duckdb

from base import AS_OF, write_parquet  # noqa: F401 (write_parquet re-exported for build_fixtures.py)

WS = "9301"
ACCOUNT_ID = "hod_acct"
DAY = timedelta(days=1)
H = timedelta(hours=1)
DAY0 = (AS_OF - 2 * DAY).date()   # inside every window (7/30/90), one fixed weekday


def _ts(hour: int) -> datetime:
    return datetime.combine(DAY0, dtime(hour, 0, 0))


# ---------------------------------------------------------------------------------------------
# billing.usage / list_prices -- the exact struct shapes tests/fixtures/ddl.py's DDL expects
# (mirrors tests/fixtures/billing.py's own helpers; duplicated locally so this builder stays
# self-contained, per the convention every module under tests/fixtures/ follows).
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


def _usage_row(con, record_id, *, hour, product, usd):
    start = _ts(hour)
    con.execute(_USAGE_SQL, [
        ACCOUNT_ID, WS, record_id, "hod_STANDARD_COMPUTE", "aws", start, start + H, DAY0, {},
        "DBU", usd, _usage_metadata(), _identity_metadata(),
        "ORIGINAL", DAY0, product, _product_features(), "COMPUTE_TIME",
    ])


_LP_SQL = (
    "INSERT INTO billing__list_prices (account_id, price_start_time, price_end_time, sku_name, "
    "cloud, currency_code, usage_unit, pricing) VALUES (?,?,?,?,?,?,?,?)"
)


def _price(con):
    con.execute(_LP_SQL, [
        ACCOUNT_ID, AS_OF - 400 * DAY, None, "hod_STANDARD_COMPUTE", "aws", "USD", "DBU",
        {"default": 1.0, "promotional": {"default": None}, "effective_list": {"default": 1.0}},
    ])


_RUN_SQL = (
    "INSERT INTO lakeflow__job_run_timeline "
    "(workspace_id, job_id, run_id, period_start_time, period_end_time, result_state) "
    "VALUES (?,?,?,?,?,?)"
)
_QH_SQL = (
    "INSERT INTO query__history (workspace_id, statement_id, start_time) VALUES (?,?,?)"
)


_WS_SQL = (
    "INSERT INTO access__workspaces_latest (workspace_id, workspace_name, workspace_url) "
    "VALUES (?,?,?)"
)


def build(con: duckdb.DuckDBPyConnection) -> None:
    # Named, so dim_workspace.name is never NULL for this workspace (waste_usd.py's / chargeback.py's
    # identical precedent).
    con.execute(_WS_SQL, [WS, "hod-hourly-cost", "https://dbc-hod.cloud.databricks.com/"])
    _price(con)

    # ---- hour 2: $300 across two products -> share 30% -> CRITICAL ----
    _usage_row(con, "hod_u_h2_jobs", hour=2, product="JOBS", usd=200.0)
    _usage_row(con, "hod_u_h2_ap", hour=2, product="ALL_PURPOSE", usd=100.0)
    for i in range(3):
        con.execute(_RUN_SQL, [WS, "hod_job_h2", f"hod_run_h2_{i}",
                                _ts(2) + i * timedelta(minutes=5),
                                _ts(2) + timedelta(minutes=20 + i * 5), "SUCCEEDED"])

    # ---- hour 9: $180 -> share 18% -> WARN ----
    _usage_row(con, "hod_u_h9_sql", hour=9, product="SQL", usd=180.0)
    for i in range(4):
        con.execute(_QH_SQL, [WS, f"hod_stmt_h9_{i}", _ts(9) + i * timedelta(minutes=10)])

    # ---- hours 5, 11, 16, 20: $100 each -> share 10% -> OK ----
    for h in (5, 11, 16, 20):
        _usage_row(con, f"hod_u_h{h}", hour=h, product="JOBS", usd=100.0)

    # ---- hour 23: $120 -> share 12% -> OK ----
    _usage_row(con, "hod_u_h23", hour=23, product="JOBS", usd=120.0)
