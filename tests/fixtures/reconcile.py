"""tests/fixtures/reconcile.py -- the only dedicated fixture data cost_chargeback_reconcile needs
(everything else it checks holds algebraically regardless of fixture content, per
tests/test_findings/test_cost_chargeback_reconcile.py's own docstring). Adds the one scenario that
makes its real 'full' check - price_join_fanout - actually fire at both WARN and CRITICAL, so the
fixture suite proves it CAN read those, not only OK.

Every id here carries the `rec_` prefix (DEC-15). Usage is account-level (workspace_id NULL, the
same DEC-15 amendment pattern chargeback_b.py uses) rather than a new dedicated workspace, since
this check never groups by workspace_id anyway and a new real workspace_id would shift the pinned
workspace universe tests/test_api.py's /api/workspaces enumerates exactly.

price_join_fanout (sku rec_SKU_FANOUT, usage_unit rec_DBU - a dedicated unit, never the literal
'DBU', so this scenario is invisible to the several other tests that independently sum the
account's real DBU total): two OVERLAPPING, both-open-ended list_prices rows ($1.00 and $2.00) for
the same sku/cloud/usage_unit - a data-quality issue on the system table itself, not something this
app writes. One usage row (quantity 1,000) then matches BOTH price rows in the plain (non-deduped)
join, inflating the view total by exactly 1,000 * $1.00 = $1,000 over the de-duplicated
(most-recent-price-wins) total. Against this account's own varying per-window billing total (larger
windows carry more of every OTHER builder's own history), that fixed $1,000 gap reads CRITICAL at
the narrow 7-day window (a smaller total to divide into) and WARN at the wider 30/90-day windows (a
larger total dilutes the same absolute gap) - both real bands from one scenario, with no need for a
second, larger-magnitude fixture that would only inflate every other cost check's own account-wide
total along with it.

by_user was deliberately NOT given a dedicated fixture: the natural way to trigger its gap - one
usage row billed on both a warehouse and a job - also gets classified into exactly one
tags.cost_unit bucket by that model's own priority-ordered classification, so such a row would
silently disagree with tags.cost_reconciliation's own independent billing total (a real, separate
gap in that model, not in scope here) rather than exercising cost_chargeback_reconcile in
isolation. by_user's own test checks it stays OK with no dedicated data instead.

Stdlib + duckdb only.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import duckdb

from base import AS_OF, write_parquet  # noqa: F401 (write_parquet re-exported for build_fixtures.py)

D0 = AS_OF.date()


def D(n: int) -> date:
    return D0 - timedelta(days=n)


ACCOUNT_ID = "rec_acct"
WS = None  # account-level usage (see module docstring)

_USAGE_SQL = (
    "INSERT INTO billing__usage (account_id, workspace_id, record_id, sku_name, cloud, "
    "usage_start_time, usage_end_time, usage_date, custom_tags, usage_unit, usage_quantity, "
    "usage_metadata, identity_metadata, record_type, ingestion_date, billing_origin_product, "
    "product_features, usage_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
_rid = 0


def _next_record_id() -> str:
    global _rid
    _rid += 1
    return f"rec_rec_{_rid}"


def _usage_metadata():
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


def _identity_metadata():
    return {"run_as": None, "created_by": None, "owned_by": None, "run_by": None}


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


UNIT = "rec_DBU"  # dedicated usage_unit, never the literal 'DBU' - see module docstring


def usage(con, *, sku_name: str, usage_date_: date, usage_quantity: float) -> None:
    start = datetime.combine(usage_date_, datetime.min.time())
    end = start + timedelta(hours=1)
    con.execute(
        _USAGE_SQL,
        [
            ACCOUNT_ID, WS, _next_record_id(), sku_name, "aws", start, end, usage_date_,
            {}, UNIT, usage_quantity,
            _usage_metadata(), _identity_metadata(), "ORIGINAL", usage_date_, "ALL_PURPOSE",
            _product_features(), "COMPUTE_TIME",
        ],
    )


def list_price(con, sku_name: str, rate: float, start_offset_days: int) -> None:
    con.execute(
        "INSERT INTO billing__list_prices (account_id, price_start_time, price_end_time, "
        "sku_name, cloud, currency_code, usage_unit, pricing) VALUES (?,?,?,?,?,?,?,?)",
        [
            ACCOUNT_ID, AS_OF - timedelta(days=start_offset_days), None, sku_name, "aws", "USD", UNIT,
            {"default": rate, "promotional": {"default": None}, "effective_list": {"default": rate}},
        ],
    )


def build(con: duckdb.DuckDBPyConnection) -> None:
    # price_join_fanout: two overlapping, both-open-ended price rows for the same sku/cloud/unit.
    list_price(con, "rec_SKU_FANOUT", 1.00, start_offset_days=60)
    list_price(con, "rec_SKU_FANOUT", 2.00, start_offset_days=50)
    usage(con, sku_name="rec_SKU_FANOUT", usage_date_=D(3), usage_quantity=1_000.0)
