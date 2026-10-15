"""tests/fixtures/waste_usd.py -- P3-WASTEUSD review fix, fixture builder for the item's own
regression case: a confirmed-idle serving endpoint that serves MORE THAN ONE entity, proving
compute_serving_endpoint_cost_status's est_wasted_usd_list splits the endpoint's waste evenly
across its served-entity rows (ROUND(ce.est_usd_list / COUNT(*) OVER (PARTITION BY
ce.workspace_id, ce.endpoint_key), 2)) instead of repeating the whole amount on every row -- the
must_fix item 3 defect a review of the original P3-WASTEUSD commit found (a two-served-entity
endpoint would otherwise have its waste counted twice when a screen sums the column over rows).

Every id this builder writes is prefixed `wu_` (no other builder uses that prefix; DEC-15). Per
the per-builder isolation model (tests/fixtures/build_fixtures.py's own module docstring), this
file is self-contained: it duplicates the INSERT shape tests/fixtures/serving_storage.py (T-22)
already established for system.serving.served_entities / system.serving.endpoint_usage /
system.billing.usage / system.billing.list_prices, rather than importing serving_storage.py,
matching every other builder's own convention (base.py only).

This builder writes its own brand-new workspace_id (WU_WS = "wu_ws9"), never one of the shared
1111/2222/3333 workspaces billing.py writes -- chosen deliberately so these rows can never shift
any *unfiltered* assertion elsewhere in the suite (test_serving_storage.py's own module docstring
and this task's own instructions both call this out): every test that reads
system.billing.usage/system.serving.* account-wide (test_overview_spend_estimate.py,
test_overview_dbu_by_sku.py, test_overview_serverless_classic_split.py,
test_overview_daily_dbu_trend.py, tests/dbutil.py's own usage_sum()) re-derives its OWN "expected"
value from the raw fixture parquet at test time (never a pasted literal), so a brand-new,
previously-absent workspace_id/sku_name/endpoint_id simply adds a new group to both sides of every
such comparison rather than changing an existing one. test_serving_storage.py's own
compute_serving_endpoint_cost_status assertions all filter to `ss_`-prefixed ids (or, for the one
whole-table check -- the net_dbus-descending "bug canary", see that module's own comment -- the
ordering is enforced by the query's own ORDER BY regardless of how many rows exist, so a new row
lands in its own correctly-sorted position and cannot break that check either).

Gate-fix follow-up: WU_WS's own system.serving.served_entities rows (below) already put it inside
the snapshot's region (dims.dim_workspace's in_snapshot_region union includes serving__served_entities),
so this workspace is never an "outside the snapshot's region" scenario -- but it originally carried
NO system.access.workspaces_latest row, leaving dim_workspace.name NULL, which broke
tests/test_api.py's test_workspaces_endpoint invariant that every real workspace has a name. Per
that same invariant (a builder that bills/registers a new in-region workspace must also name it --
see tests/fixtures/chargeback.py's identical CB_WS precedent), one access__workspaces_latest row is
added for WU_WS below.

------------------------------------------------------------------------------------------------
wu_ep_idle2 / wu_se_a / wu_se_b -- the two-served-entity confirmed-idle endpoint:

  One billing row at D(3) (inside every one of 7/30/90) for endpoint wu_ep_idle2: 10.0 DBU on this
  builder's own SKU wu_MODEL_SERVING_STANDARD, priced at $0.20/DBU (list_prices, a `price_start_
  time` far enough in the past and `price_end_time IS NULL`, matching serving_storage.py's
  list_price() shape) -> est_usd_list = ROUND(10.0 * 0.20, 2) = 2.00.

  Two served_entities rows (wu_se_a, wu_se_b), both mapping to wu_ep_idle2 -- a genuine fan-out:
  compute_serving_endpoint_cost_status's `entities` CTE dedupes to the latest row per
  (workspace_id, endpoint_id, served_entity_id), and with two DISTINCT served_entity_ids the
  LEFT JOIN from cost_ep (one row per endpoint) to entities produces two output rows for this one
  endpoint, each carrying the SAME ce.net_dbus / ce.est_usd_list before the fix (the defect) and
  HALF each after it (the fix). Both served_entity rows use a change_time well outside every
  window (SCD2 dedup is already covered by serving_storage.py's own ss_se_ok; not re-proven here).

  Four endpoint_usage rows (two per served entity), all at D(45): inside the fixed 90-day
  :retention_days window (so tracking is confirmed ON -- ever_requests=4) but outside the 30-day
  (and 7-day) :period_days analysis window (so ep_requests=0 there) -- the exact
  "confirmed idle" shape tests/fixtures/serving_storage.py's own ss_se_idlewindow/ss_ep_idlewindow
  already established (see that module's docstring), reproduced here as a SECOND, independent
  instance under this builder's own ids specifically to exercise the two-served-entity fan-out
  ss_ep_idlewindow (a single-entity endpoint) cannot exercise. At w=30 (and w=7): status=CRITICAL,
  est_wasted_usd_list = 1.00 on EACH of the two rows (half of the endpoint's 2.00), summing back to
  the endpoint's own est_usd_list. At w=90 those same D(45) requests fall inside the period window
  (45 < 90), so ep_requests=4 <= warn_low_requests(10) -> WARN, est_wasted_usd_list=0 on both rows
  -- the same CRITICAL-at-w7/30-then-WARN-at-w90 flip ss_ep_idlewindow uses, not re-asserted here
  since the fix under test is the SPLIT, not the status ladder (already covered).

  endpoint_id is non-NULL throughout (wu_ep_idle2), so this builder never adds a second row to
  test_serving_storage.py's own `byname_rows` (endpoint_id IS NULL) filter, which that module
  asserts has length exactly 1 -- per this task's own instructions.
------------------------------------------------------------------------------------------------
"""
from __future__ import annotations

from datetime import datetime, timedelta
from datetime import time as dtime

import duckdb

from base import AS_OF, write_parquet  # noqa: F401 (write_parquet re-exported for build_fixtures.py)

D0 = AS_OF.date()


def D(n: int):
    """D(n) = D0 - n days (a date)."""
    return D0 - timedelta(days=n)


def DT(n: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    """A full datetime at D(n) with the given time-of-day -- AS_OF-relative, never an absolute
    literal (matching every other builder's own D()/DT() convention)."""
    return datetime.combine(D(n), dtime(hour, minute, second))


# This builder's own, brand-new workspace -- never one of the shared 1111/2222/3333 (see module
# docstring: chosen so these rows can never shift an existing unfiltered assertion elsewhere).
WU_WS = "wu_ws9"
ACCOUNT_ID = "wu_acct"

WU_EP_IDLE2 = "wu_ep_idle2"
WU_SE_A = "wu_se_a"
WU_SE_B = "wu_se_b"
WU_EP_SPLIT = "wu_ep_split"
WU_SE_C = "wu_se_c"
WU_SE_D = "wu_se_d"


# =================================================================================================
# STRUCT defaults for system.billing.usage -- duplicated from tests/fixtures/serving_storage.py's
# own _usage_metadata()/_identity_metadata()/_product_features() (same DDL, same field set) rather
# than imported, per the per-builder isolation model (see module docstring above).
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
    workspace_id,
    sku_name: str,
    usage_date_,
    usage_quantity: float,
    billing_origin_product: str,
    endpoint_id: str | None,
    endpoint_name: str | None = None,
    usage_unit: str = "DBU",
    usage_type: str = "COMPUTE_TIME",
    hour: int = 0,
) -> None:
    start = datetime.combine(usage_date_, dtime(hour, 0, 0))
    end = start + timedelta(hours=1)
    con.execute(
        _USAGE_SQL,
        [
            ACCOUNT_ID, workspace_id, record_id, sku_name, "aws", start, end, usage_date_,
            {}, usage_unit, usage_quantity,
            _usage_metadata(endpoint_id=endpoint_id, endpoint_name=endpoint_name),
            _identity_metadata(),
            "ORIGINAL", usage_date_, billing_origin_product,
            _product_features(is_serverless=True, serving_type="MODEL"),
            usage_type,
        ],
    )


# billing__list_prices -- this builder's OWN priced SKU (wu_MODEL_SERVING_STANDARD), disjoint from
# every other builder's SKU names (DEC-15 sanctions a builder owning its own SKUs).
_LP_SQL = (
    "INSERT INTO billing__list_prices (account_id, price_start_time, price_end_time, sku_name, "
    "cloud, currency_code, usage_unit, pricing) VALUES (?,?,?,?,?,?,?,?)"
)


def list_price(con: duckdb.DuckDBPyConnection, sku_name: str, default_rate: float, effective_list_default: float) -> None:
    con.execute(
        _LP_SQL,
        [
            ACCOUNT_ID, AS_OF - timedelta(days=300), None, sku_name, "aws", "USD", "DBU",
            {"default": default_rate, "promotional": {"default": None}, "effective_list": {"default": effective_list_default}},
        ],
    )


# =================================================================================================
# system.serving.served_entities / system.serving.endpoint_usage insert helpers -- duplicated from
# tests/fixtures/serving_storage.py's own shape (see module docstring above).
# =================================================================================================
_SE_SQL = (
    "INSERT INTO serving__served_entities "
    "(served_entity_id, account_id, workspace_id, created_by, endpoint_name, endpoint_id, "
    "served_entity_name, entity_type, entity_name, entity_version, endpoint_config_version, task, "
    "external_model_config, foundation_model_config, custom_model_config, feature_spec_config, "
    "change_time, endpoint_delete_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def served_entity(
    con: duckdb.DuckDBPyConnection,
    *,
    served_entity_id: str,
    workspace_id: str,
    endpoint_id: str,
    endpoint_name: str,
    served_entity_name: str,
    change_time: datetime,
    entity_type: str = "FOUNDATION_MODEL",
    entity_name: str | None = None,
    entity_version: str = "1.0",
    endpoint_config_version: int = 1,
) -> None:
    con.execute(
        _SE_SQL,
        [
            served_entity_id, ACCOUNT_ID, workspace_id, "wu_creator", endpoint_name, endpoint_id,
            served_entity_name, entity_type, entity_name or served_entity_name, entity_version,
            endpoint_config_version, None,
            None, None, None, None,  # external/foundation/custom/feature_spec model configs: unused
            change_time, None,
        ],
    )


_EU_SQL = (
    "INSERT INTO serving__endpoint_usage "
    "(account_id, workspace_id, served_entity_id, client_request_id, databricks_request_id, "
    "requester, status_code, request_time, input_token_count, output_token_count, "
    "input_character_count, output_character_count, usage_context, request_streaming) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def endpoint_usage_row(
    con: duckdb.DuckDBPyConnection,
    *,
    workspace_id: str,
    served_entity_id: str,
    req_suffix: str,
    request_time: datetime,
    status_code: int = 200,
    input_tokens: int = 100,
    output_tokens: int = 50,
    requester: str = "wu_requester",
) -> None:
    con.execute(
        _EU_SQL,
        [
            ACCOUNT_ID, workspace_id, served_entity_id,
            f"wu_req_{req_suffix}", f"wu_dbreq_{req_suffix}",
            requester, status_code, request_time,
            input_tokens, output_tokens, input_tokens * 5, output_tokens * 5,
            {}, False,
        ],
    )


_WS_SQL = (
    "INSERT INTO access__workspaces_latest (workspace_id, workspace_name, workspace_url) "
    "VALUES (?,?,?)"
)


def build(con: duckdb.DuckDBPyConnection) -> None:
    # Name WU_WS (chargeback.py's own identical CB_WS precedent) -- see module docstring's
    # "Gate-fix follow-up" note: WU_WS is in-region (served_entities below), so it needs a name,
    # not a region exemption.
    con.execute(_WS_SQL, [WU_WS, "wu-serving", "https://dbc-wu.cloud.databricks.com/"])

    # This builder's own priced SKU.
    list_price(con, "wu_MODEL_SERVING_STANDARD", 0.20, 0.20)

    # One billing row at D(3) (inside every one of 7/30/90) -> net_dbus=10.0,
    # est_usd_list=ROUND(10.0*0.20,2)=2.00 for the whole endpoint.
    billing_usage(
        con, "wu_bl_idle2",
        workspace_id=WU_WS, sku_name="wu_MODEL_SERVING_STANDARD", usage_date_=D(3),
        usage_quantity=10.0, billing_origin_product="MODEL_SERVING",
        endpoint_id=WU_EP_IDLE2, endpoint_name="wu-ep-idle2",
    )

    # Two served entities on the SAME endpoint -> a real 2-row fan-out for
    # compute_serving_endpoint_cost_status (the defect this builder exists to catch: the whole
    # endpoint est_usd_list used to be repeated on each of these rows instead of split).
    served_entity(
        con, served_entity_id=WU_SE_A, workspace_id=WU_WS, endpoint_id=WU_EP_IDLE2,
        endpoint_name="wu-ep-idle2", served_entity_name="wu-se-a", change_time=DT(200),
    )
    served_entity(
        con, served_entity_id=WU_SE_B, workspace_id=WU_WS, endpoint_id=WU_EP_IDLE2,
        endpoint_name="wu-ep-idle2", served_entity_name="wu-se-b", change_time=DT(200),
    )

    # Four endpoint_usage rows, all at D(45): inside :retention_days (90, fixed) so tracking is
    # confirmed ON, outside :period_days at w=7/30 so this endpoint is "idle in window" there
    # (CRITICAL), and inside :period_days at w=90 (WARN, low-but-real traffic) -- the same shape
    # serving_storage.py's own ss_se_idlewindow/ss_ep_idlewindow uses, reproduced independently
    # here to exercise the two-served-entity split specifically.
    endpoint_usage_row(con, workspace_id=WU_WS, served_entity_id=WU_SE_A, req_suffix="a1", request_time=DT(45, 9))
    endpoint_usage_row(con, workspace_id=WU_WS, served_entity_id=WU_SE_A, req_suffix="a2", request_time=DT(45, 10))
    endpoint_usage_row(con, workspace_id=WU_WS, served_entity_id=WU_SE_B, req_suffix="b1", request_time=DT(45, 11))
    endpoint_usage_row(con, workspace_id=WU_WS, served_entity_id=WU_SE_B, req_suffix="b2", request_time=DT(45, 12))

    # A second endpoint whose two served entities share one billed day unevenly (3 and 1 requests):
    # compute_serving_endpoint_usage splits that day's 8 DBU / $1.60 by request share, 1.20 and 0.40.
    billing_usage(
        con, "wu_bl_split",
        workspace_id=WU_WS, sku_name="wu_MODEL_SERVING_STANDARD", usage_date_=D(5),
        usage_quantity=8.0, billing_origin_product="MODEL_SERVING",
        endpoint_id=WU_EP_SPLIT, endpoint_name="wu-ep-split",
    )
    for se, name in ((WU_SE_C, "wu-se-c"), (WU_SE_D, "wu-se-d")):
        served_entity(
            con, served_entity_id=se, workspace_id=WU_WS, endpoint_id=WU_EP_SPLIT,
            endpoint_name="wu-ep-split", served_entity_name=name, change_time=DT(200),
        )
    for i in range(3):
        endpoint_usage_row(con, workspace_id=WU_WS, served_entity_id=WU_SE_C, req_suffix=f"c{i}", request_time=DT(5, 9 + i))
    endpoint_usage_row(con, workspace_id=WU_WS, served_entity_id=WU_SE_D, req_suffix="d0", request_time=DT(5, 13))
