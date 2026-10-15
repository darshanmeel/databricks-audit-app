"""tests/fixtures/billing.py -- batch A (T-09), the one builder every cost (23 vendored under
app/queries/vendored/cost/) and overview (5 app-owned ports under docs/ports/originals/
overview_*.sql) query's tests read from. Fills system.billing.usage, system.billing.list_prices,
system.billing.attributed_usage and system.access.workspaces_latest per tests/fixtures/ddl.py,
following the drilldown.py (T-07) file shape: a module-level build(con), AS_OF from base.py,
write_parquet re-exported for build_fixtures.py.

Per DEC-15 this builder writes the three SHARED fixture workspaces (1111 acme-prod, 2222 acme-dev,
3333 acme-uat) -- every builder except drilldown.py shares these. Every other id this file writes
(job_id, run_id, warehouse_id, cluster_id, instance_pool_id, notebook_id, endpoint_id, statement_id,
tag values, SKU names where the query does not require a literal substring) carries the `bl_`
prefix, so a later builder's own ids can never collide with this file's on the shared
billing__usage / access__workspaces_latest sources.

Rows are inserted with parameterized `?` INSERTs, one Python dict per STRUCT column and one Python
list per LIST/ARRAY column (duckdb 1.5.1 binds a dict to a STRUCT by field NAME, confirmed against
tests/fixtures/ddl.py's own DDL before writing this file) -- every STRUCT field and every top-level
column is always given an explicit key (None where the DDL allows NULL), so no column is ever
silently omitted. usage_metadata / identity_metadata / product_features defaults come from
_usage_metadata() / _identity_metadata() / _product_features(); callers override only the fields
their row group needs, so a job row never also carries a warehouse_id or an endpoint_id (PLAN.md
7.3's "denominator" warning for cost_by_compute_resource / cost_by_job).

All dates are relative to base.AS_OF = 2026-09-21 12:00:00 (`D0`); `D(n)` = D0 minus n days. Per
PLAN.md 7.3 / the task's Inputs section: usage_date rows land on D(1) .. D(10) (ten distinct
in-window days, spread one scenario group per day below), one row on D0 itself (must be excluded by
every query's `usage_date < current_date()` predicate -- see SEC K), one row at D(45) (inside the
90-day window, outside the 30-day window -- SEC K), and a dedicated D(5) vs D(20) vs D(45) vs D(0)
series (SEC K, job_id bl_win_job) proving the 7/30/90-day sums differ (30 > 7, 90 > 30) for the same
id.

------------------------------------------------------------------------------------------------
Predicate / field -> matching row id (T-12/T-13/T-10/T-11 read this table to find their fixtures)
------------------------------------------------------------------------------------------------
billing_origin_product = 'SQL'                          bl_u_sql_raw1 (usage), bl_a_att1 (attributed)
billing_origin_product = 'MODEL_SERVING'                 bl_u_ep_launch, bl_u_ep_normal, bl_u_genai_answer
billing_origin_product = 'VECTOR_SEARCH'                  bl_u_vs_serving, bl_u_vs_storage
billing_origin_product = 'DEFAULT_STORAGE'                 bl_u_storage1 (via storage_api_type, not this literal)
billing_origin_product IN ('MODEL_SERVING','VECTOR_SEARCH') all of the above four
billing_origin_product NOT IN the above (non-match)          bl_u_job_crit / bl_u_cluster_crit / bl_u_sql_raw1's siblings (JOBS/ALL_PURPOSE rows)
sku_name LIKE '%(PHOTON)%'                                bl_u_photon1 (sku bl_ALL_PURPOSE_COMPUTE_(PHOTON))
upper(sku_name) LIKE '%SERVERLESS_REAL_TIME_INFERENCE_LAUNCH%' (match) bl_u_ep_launch / (non-match) bl_u_ep_normal
usage_type IN ('TOKEN','GPU_TIME','ANSWER')                bl_u_genai_token / bl_u_genai_gpu / bl_u_genai_answer
usage_type IN ('NETWORK_BYTE','NETWORK_HOUR')               bl_u_net_bytes / bl_u_net_hours
usage_type NOT IN either set above (non-match)                bl_u_ep_launch / bl_u_ep_normal (usage_type='COMPUTE_TIME')
usage_unit = 'DBU' / upper(usage_unit) = 'DBU'             most rows (see below for non-DBU)
usage_unit non-DBU (non-match)                              bl_u_genai_token (TOKEN), bl_u_net_bytes (BYTES),
                                                             bl_u_net_hours (HOURS), bl_u_vs_storage (GB)
usage_metadata.storage_api_type IS NOT NULL                  bl_u_storage1
usage_metadata.cluster_id/warehouse_id/instance_pool_id      bl_u_cluster_crit / bl_u_cluster_warn (cluster_id),
  IS NOT NULL (>= 1 of 3)                                    bl_u_wh_ok (warehouse_id), bl_u_pool1 (instance_pool_id)
usage_metadata.job_id IS NOT NULL                            bl_u_job_crit / bl_u_job_warn / bl_u_job_ok / bl_u_win_job*
usage_metadata.notebook_id IS NOT NULL                       bl_u_nb_crit / bl_u_nb_warn / bl_u_nb_ok
usage_metadata.usage_policy_id IS NOT NULL                   bl_u_pol_usage
usage_metadata.budget_policy_id IS NOT NULL (not usage_policy_id) bl_u_pol_budget
neither usage_policy_id nor budget_policy_id, is_serverless=true bl_u_pol_none
product_features.is_serverless IS NULL (NOT_ASSESSED)        bl_u_pol_null
cardinality(map_keys(custom_tags)) > 0 (tagged)               bl_u_tag_a / bl_u_tag_b / bl_u_pol_budget
custom_tags = {} (untagged, OUTER explode -> tag_key NULL)   bl_u_tag_empty (and most other rows' default {})
record_type = 'ORIGINAL' / 'RETRACTION' / 'RESTATEMENT'      bl_u_corr_orig / bl_u_corr_retraction / bl_u_corr_restatement
identity_metadata.run_as LIKE '%@%' (email/user)              bl_u_id_email
identity_metadata.run_as RLIKE GUID pattern (service_principal) bl_u_id_sp
identity_metadata.run_as IS NULL or '__REDACTED__' (unknown)  bl_u_id_unknown
workspace_id IS NULL (account-level)                           bl_u_acct_level
price_end_time IS NULL (current) / IS NOT NULL (expired)       every priced SKU below has one of each
sku_name with NO matching list_prices row (NOT_ASSESSED path)  bl_SKU_NO_PRICE (used by bl_u_job_ok)
upper(sku_name) LIKE '%FREE_USAGE%', no matching price row (real $0, price_basis='free') bl_u_free1 (sku bl_GENIE_FREE_USAGE, D(11), acme-uat)
status CRITICAL / WARN / OK, cost_by_job (>=200/[50,200)/<50)   bl_u_job_crit(250) / bl_u_job_warn(100) / bl_u_job_ok(20)
status bands, cost_by_compute_resource (>=200/[50,200)/<50)    bl_u_cluster_crit(250) / bl_u_cluster_warn(100) / bl_u_wh_ok(20)
status bands, cost_by_notebook (>=50/[10,50)/<10)               bl_u_nb_crit(60) / bl_u_nb_warn(25) / bl_u_nb_ok(5)
7-day vs 30-day vs 90-day window differ for one id             bl_win_job runs at D(5)=5, D(20)=7, D(45)=11, D(0)=999(excluded)
usage_date = D0 (must be excluded by every query)               bl_u_win_d0
usage_date = D(45) (in 90-day, out of 30-day window)            bl_u_win_d45
attributed_usage raw-vs-attributed gap (cost_dbsql_allocation_gap) raw bl_u_sql_raw1(80) vs attributed bl_a_att1(55) -> gap 25
attributed_usage non-SQL row (filter excludes it)               bl_a_att_nonmatch
workspace_id billed with NO access__workspaces_latest row (cost_unnamed_workspaces) uw_crit / uw_warn / uw_ok (SEC N)
------------------------------------------------------------------------------------------------

Out of scope / not engineered here (recorded for the Hand-off notes, not a gap in this file):
cost_serving_mode_by_endpoint's and cost_vector_search_spend's own WARN/CRITICAL $ bands are not
targeted at specific magnitudes -- the task's Inputs section only requires status-band engineering
for cost_by_job / cost_by_compute_resource / cost_by_notebook by name, and the LEFT JOIN
NOT_ASSESSED path is proven once (bl_SKU_NO_PRICE), not once per priced query.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import duckdb

from base import AS_OF, write_parquet  # noqa: F401 (write_parquet re-exported for build_fixtures.py callers)

D0 = AS_OF.date()  # 2026-09-21 -- "today" on the pinned test target; excluded by usage_date < current_date()


def D(n: int):
    return D0 - timedelta(days=n)


ACCOUNT_ID = "bl_acct"
WS_PROD = "1111"
WS_DEV = "2222"
WS_UAT = "3333"


# ---------------------------------------------------------------------------------------------
# STRUCT defaults -- every field tests/fixtures/ddl.py's DDL gives usage_metadata / identity_metadata
# / product_features, defaulted to None (NULL) so a caller only overrides the fields its row group
# owns, and no field is ever silently missing from the INSERT.
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


# ---------------------------------------------------------------------------------------------
# Insert helpers -- one row per call, named-column INSERT, every column given explicitly.
# ---------------------------------------------------------------------------------------------
_USAGE_SQL = (
    "INSERT INTO billing__usage (account_id, workspace_id, record_id, sku_name, cloud, "
    "usage_start_time, usage_end_time, usage_date, custom_tags, usage_unit, usage_quantity, "
    "usage_metadata, identity_metadata, record_type, ingestion_date, billing_origin_product, "
    "product_features, usage_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def usage(
    con: duckdb.DuckDBPyConnection,
    record_id: str,
    *,
    workspace_id,
    sku_name: str,
    usage_date_,
    usage_quantity: float,
    billing_origin_product: str,
    usage_unit: str = "DBU",
    usage_type: str = "COMPUTE_TIME",
    record_type: str = "ORIGINAL",
    cloud: str = "aws",
    hour: int = 0,
    custom_tags: dict | None = None,
    usage_metadata: dict | None = None,
    identity_metadata: dict | None = None,
    product_features: dict | None = None,
) -> None:
    start = datetime.combine(usage_date_, datetime.min.time()) + timedelta(hours=hour)
    end = start + timedelta(hours=1)
    con.execute(
        _USAGE_SQL,
        [
            ACCOUNT_ID, workspace_id, record_id, sku_name, cloud, start, end, usage_date_,
            custom_tags if custom_tags is not None else {},
            usage_unit, usage_quantity,
            usage_metadata if usage_metadata is not None else _usage_metadata(),
            identity_metadata if identity_metadata is not None else _identity_metadata(),
            record_type, usage_date_, billing_origin_product,
            product_features if product_features is not None else _product_features(),
            usage_type,
        ],
    )


_LP_SQL = (
    "INSERT INTO billing__list_prices (account_id, price_start_time, price_end_time, sku_name, "
    "cloud, currency_code, usage_unit, pricing) VALUES (?,?,?,?,?,?,?,?)"
)


def _list_price(con, sku_name, usage_unit, *, cloud, currency_code, default_rate, effective_list_default, start, end):
    con.execute(
        _LP_SQL,
        [
            ACCOUNT_ID, start, end, sku_name, cloud, currency_code, usage_unit,
            {"default": default_rate, "promotional": {"default": None}, "effective_list": {"default": effective_list_default}},
        ],
    )


def priced_sku(con, sku_name, usage_unit, default_rate, effective_list_default, *, cloud="aws", currency_code="USD"):
    """Write both a CURRENT (price_end_time NULL) and an EXPIRED (well before every usage row's
    usage_date in this fixture, D(45) being the oldest) price row for one (sku_name, cloud,
    usage_unit) combination -- Inputs section: "for every (sku_name, cloud, usage_unit)
    combination used above, one current row ... and one expired row"."""
    _list_price(
        con, sku_name, usage_unit, cloud=cloud, currency_code=currency_code,
        default_rate=round(default_rate * 0.6, 4), effective_list_default=round(effective_list_default * 0.6, 4),
        start=AS_OF - timedelta(days=400), end=AS_OF - timedelta(days=200),
    )
    _list_price(
        con, sku_name, usage_unit, cloud=cloud, currency_code=currency_code,
        default_rate=default_rate, effective_list_default=effective_list_default,
        start=AS_OF - timedelta(days=100), end=None,
    )


_AU_SQL = (
    "INSERT INTO billing__attributed_usage (record_id, usage_metadata, identity_metadata, "
    "start_time, end_time, usage_date, usage_unit, active_usage_quantity, granular_tags, "
    "usage_record_ids, account_id, workspace_id, sku_name, cloud, billing_origin_product, "
    "custom_tags) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def attributed(
    con,
    record_id: str,
    *,
    workspace_id,
    sku_name: str,
    usage_date_,
    active_usage_quantity: float,
    billing_origin_product: str,
    usage_unit: str = "DBU",
    cloud: str = "aws",
    hour: int = 0,
    dbsql_statement_id=None,
    warehouse_id=None,
    client_application=None,
    executed_by=None,
    query_tags: dict | None = None,
    usage_record_ids: list | None = None,
    custom_tags: dict | None = None,
) -> None:
    start = datetime.combine(usage_date_, datetime.min.time()) + timedelta(hours=hour)
    end = start + timedelta(hours=1)
    con.execute(
        _AU_SQL,
        [
            record_id,
            {"dbsql_statement_id": dbsql_statement_id, "warehouse_id": warehouse_id, "client_application": client_application},
            {"executed_by": executed_by},
            start, end, usage_date_, usage_unit, active_usage_quantity,
            {"query_tags": query_tags if query_tags is not None else {}},
            usage_record_ids if usage_record_ids is not None else [],
            ACCOUNT_ID, workspace_id, sku_name, cloud, billing_origin_product,
            custom_tags if custom_tags is not None else {},
        ],
    )


_WS_SQL = (
    "INSERT INTO access__workspaces_latest (account_id, workspace_id, workspace_name, "
    "workspace_url, create_time, status) VALUES (?,?,?,?,?,?)"
)


def workspace(con, workspace_id: str, name: str) -> None:
    con.execute(_WS_SQL, [ACCOUNT_ID, workspace_id, name, None, AS_OF - timedelta(days=500), "ACTIVE"])


# ---------------------------------------------------------------------------------------------
# SKUs -- (sku_name, usage_unit, cloud) -> (default_rate, effective_list_default). Every combo
# used by a DBU/priced row below gets a current + expired list_prices row via priced_sku(), except
# bl_SKU_NO_PRICE (deliberately unpriced -- see SEC A; a genuine pricing-coverage gap,
# price_basis='unpriced'), the two networking SKUs (no query prices them; cost_dollarized_by_sku_day
# / cost_cloud_infra just see a legitimate coverage gap there), and bl_GENIE_FREE_USAGE (SEC M --
# deliberately unpriced BY DESIGN, not a gap: its name matches '%FREE_USAGE%' so price_basis='free').
# ---------------------------------------------------------------------------------------------
_PRICED_SKUS = [
    ("bl_JOBS_COMPUTE_CLASSIC", "DBU", "aws", 0.15, 0.22),
    ("bl_JOBS_COMPUTE_SERVERLESS", "DBU", "aws", 0.40, 0.55),
    ("bl_ALL_PURPOSE_COMPUTE", "DBU", "aws", 0.55, 0.65),
    ("bl_ALL_PURPOSE_COMPUTE_(PHOTON)", "DBU", "aws", 0.65, 0.75),
    ("bl_DBSQL_COMPUTE", "DBU", "aws", 0.22, 0.30),
    ("bl_NOTEBOOK_COMPUTE", "DBU", "aws", 0.55, 0.65),
    ("bl_MODEL_SERVING_STANDARD", "DBU", "aws", 0.07, 0.10),
    ("bl_SERVERLESS_REAL_TIME_INFERENCE_LAUNCH", "DBU", "aws", 0.07, 0.10),
    ("bl_MODEL_SERVING_TOKEN", "TOKEN", "aws", 0.0001, 0.00015),
    ("bl_MODEL_SERVING_GPU", "DBU", "aws", 3.50, 4.00),
    ("bl_VECTOR_SEARCH_SERVING", "DBU", "aws", 0.07, 0.10),
    ("bl_VECTOR_SEARCH_STORAGE", "GB", "aws", 0.33, 0.40),
    ("bl_DEFAULT_STORAGE_TIER1", "DBU", "aws", 0.022, 0.03),
]


# ---------------------------------------------------------------------------------------------
# Auto-discovered by build_fixtures.py (DEC-17): a module-level build(con), no registration list.
# ---------------------------------------------------------------------------------------------
def build(con: duckdb.DuckDBPyConnection) -> None:
    # ---- workspaces_latest: exactly 3 rows (DEC-15) -----------------------------------------
    workspace(con, WS_PROD, "acme-prod")
    workspace(con, WS_DEV, "acme-dev")
    workspace(con, WS_UAT, "acme-uat")

    # ---- list_prices: current + expired for every priced SKU (see _PRICED_SKUS) -------------
    for sku_name, usage_unit, cloud, default_rate, effective_list_default in _PRICED_SKUS:
        priced_sku(con, sku_name, usage_unit, default_rate, effective_list_default, cloud=cloud)

    # =========================================================================================
    # SEC A -- cost_by_job status bands (D(1), workspace acme-prod). warn=50, crit=200 DBU/day.
    # bl_u_job_ok's SKU (bl_SKU_NO_PRICE) is deliberately unpriced -- also proves the
    # cost_actual_vs_list_by_sku / cost_dollarized_by_sku_day / cost_cloud_infra LEFT JOIN
    # NOT_ASSESSED / NULL-cost path (T-13).
    # =========================================================================================
    usage(
        con, "bl_u_job_crit_r1", workspace_id=WS_PROD, sku_name="bl_JOBS_COMPUTE_CLASSIC",
        usage_date_=D(1), usage_quantity=150.0, billing_origin_product="JOBS",
        usage_metadata=_usage_metadata(job_id="bl_job_crit", job_run_id="bl_run_crit1"),
        product_features=_product_features(is_serverless=False, jobs_tier="STANDARD"),
    )
    usage(
        con, "bl_u_job_crit_r2", workspace_id=WS_PROD, sku_name="bl_JOBS_COMPUTE_CLASSIC",
        usage_date_=D(1), usage_quantity=100.0, billing_origin_product="JOBS", hour=1,
        usage_metadata=_usage_metadata(job_id="bl_job_crit", job_run_id="bl_run_crit2"),
        product_features=_product_features(is_serverless=False, jobs_tier="STANDARD"),
    )  # bl_job_crit sums to 250 DBU on D(1) -> CRITICAL; distinct_runs = 2
    usage(
        con, "bl_u_job_warn", workspace_id=WS_PROD, sku_name="bl_JOBS_COMPUTE_SERVERLESS",
        usage_date_=D(1), usage_quantity=100.0, billing_origin_product="JOBS",
        usage_metadata=_usage_metadata(job_id="bl_job_warn", job_run_id="bl_run_warn1"),
        product_features=_product_features(is_serverless=True, performance_target="STANDARD"),
    )  # WARN band
    usage(
        con, "bl_u_job_ok", workspace_id=WS_PROD, sku_name="bl_SKU_NO_PRICE",
        usage_date_=D(1), usage_quantity=20.0, billing_origin_product="JOBS",
        usage_metadata=_usage_metadata(job_id="bl_job_ok", job_run_id="bl_run_ok1"),
        product_features=_product_features(is_serverless=False),
    )  # OK band; unpriced SKU (see module docstring)

    # =========================================================================================
    # SEC B -- cost_by_compute_resource status bands (D(2)). warn=50, crit=200 DBU/day. Each
    # resource id populates exactly one of cluster_id/warehouse_id/instance_pool_id (never more
    # than one -- see module docstring on double-counting denominators).
    # =========================================================================================
    usage(
        con, "bl_u_cluster_crit", workspace_id=WS_PROD, sku_name="bl_ALL_PURPOSE_COMPUTE",
        usage_date_=D(2), usage_quantity=250.0, billing_origin_product="ALL_PURPOSE",
        usage_metadata=_usage_metadata(cluster_id="bl_cluster_crit"),
    )
    usage(
        con, "bl_u_cluster_warn", workspace_id=WS_PROD, sku_name="bl_ALL_PURPOSE_COMPUTE",
        usage_date_=D(2), usage_quantity=100.0, billing_origin_product="ALL_PURPOSE",
        usage_metadata=_usage_metadata(cluster_id="bl_cluster_warn"),
    )
    usage(
        con, "bl_u_wh_ok", workspace_id=WS_DEV, sku_name="bl_DBSQL_COMPUTE",
        usage_date_=D(2), usage_quantity=20.0, billing_origin_product="SQL",
        usage_metadata=_usage_metadata(warehouse_id="bl_wh_ok"),
    )
    usage(
        con, "bl_u_pool1", workspace_id=WS_UAT, sku_name="bl_ALL_PURPOSE_COMPUTE",
        usage_date_=D(2), usage_quantity=15.0, billing_origin_product="ALL_PURPOSE",
        usage_metadata=_usage_metadata(instance_pool_id="bl_pool1"),
    )

    # =========================================================================================
    # SEC C -- cost_by_notebook status bands (D(3), workspace acme-uat). warn=10, crit=50 DBU/day.
    # =========================================================================================
    usage(
        con, "bl_u_nb_crit", workspace_id=WS_UAT, sku_name="bl_NOTEBOOK_COMPUTE",
        usage_date_=D(3), usage_quantity=60.0, billing_origin_product="ALL_PURPOSE",
        usage_metadata=_usage_metadata(notebook_id="bl_nb_crit", notebook_path="/Users/bl/nb_crit"),
    )
    usage(
        con, "bl_u_nb_warn", workspace_id=WS_UAT, sku_name="bl_NOTEBOOK_COMPUTE",
        usage_date_=D(3), usage_quantity=25.0, billing_origin_product="ALL_PURPOSE",
        usage_metadata=_usage_metadata(notebook_id="bl_nb_warn", notebook_path="/Users/bl/nb_warn"),
    )
    usage(
        con, "bl_u_nb_ok", workspace_id=WS_UAT, sku_name="bl_NOTEBOOK_COMPUTE",
        usage_date_=D(3), usage_quantity=5.0, billing_origin_product="ALL_PURPOSE",
        usage_metadata=_usage_metadata(notebook_id="bl_nb_ok", notebook_path="/Users/bl/nb_ok"),
    )

    # =========================================================================================
    # SEC D -- model serving + GenAI (D(4), workspace acme-prod, billing_origin_product=
    # MODEL_SERVING). cost_by_serving_endpoint, cost_serving_mode_by_endpoint (is_launch_sku),
    # cost_genai_token_gpu (usage_type IN ('TOKEN','GPU_TIME','ANSWER')).
    # =========================================================================================
    usage(
        con, "bl_u_ep_launch", workspace_id=WS_PROD, sku_name="bl_SERVERLESS_REAL_TIME_INFERENCE_LAUNCH",
        usage_date_=D(4), usage_quantity=30.0, billing_origin_product="MODEL_SERVING",
        usage_type="COMPUTE_TIME",
        usage_metadata=_usage_metadata(endpoint_id="bl_ep_launch", endpoint_name="bl_launch_ep"),
        product_features=_product_features(is_serverless=True, serving_type="MODEL"),
    )  # is_launch_sku = TRUE; also the "non-matching usage_type" row for the TOKEN/GPU_TIME/ANSWER
    #    and NETWORK_BYTE/NETWORK_HOUR filters (usage_type='COMPUTE_TIME')
    usage(
        con, "bl_u_ep_normal", workspace_id=WS_PROD, sku_name="bl_MODEL_SERVING_STANDARD",
        usage_date_=D(4), usage_quantity=20.0, billing_origin_product="MODEL_SERVING",
        usage_type="COMPUTE_TIME",
        usage_metadata=_usage_metadata(endpoint_id="bl_ep_normal", endpoint_name="bl_normal_ep"),
        product_features=_product_features(is_serverless=True, serving_type="FOUNDATION_MODEL"),
    )  # is_launch_sku = FALSE
    usage(
        con, "bl_u_genai_token", workspace_id=WS_PROD, sku_name="bl_MODEL_SERVING_TOKEN",
        usage_date_=D(4), usage_quantity=1000.0, billing_origin_product="MODEL_SERVING",
        usage_unit="TOKEN", usage_type="TOKEN",
        usage_metadata=_usage_metadata(endpoint_id="bl_ep_genai", endpoint_name="bl_genai_ep"),
        product_features=_product_features(is_serverless=True, serving_type="MODEL"),
    )
    usage(
        con, "bl_u_genai_gpu", workspace_id=WS_PROD, sku_name="bl_MODEL_SERVING_GPU",
        usage_date_=D(4), usage_quantity=40.0, billing_origin_product="MODEL_SERVING",
        usage_unit="DBU", usage_type="GPU_TIME",
        usage_metadata=_usage_metadata(endpoint_id="bl_ep_genai", endpoint_name="bl_genai_ep"),
        product_features=_product_features(is_serverless=True, serving_type="GPU_MODEL"),
    )
    usage(
        con, "bl_u_genai_answer", workspace_id=WS_PROD, sku_name="bl_MODEL_SERVING_STANDARD",
        usage_date_=D(4), usage_quantity=5.0, billing_origin_product="MODEL_SERVING",
        usage_unit="DBU", usage_type="ANSWER",
        usage_metadata=_usage_metadata(endpoint_id="bl_ep_genai", endpoint_name="bl_genai_ep"),
        product_features=_product_features(is_serverless=True, serving_type="MODEL"),
    )

    # =========================================================================================
    # SEC E -- networking + vector search (D(5), also the "inside the 7-day window" leg of the
    # window-boundary series -- see SEC K). cost_networking_egress (usage_type IN
    # ('NETWORK_BYTE','NETWORK_HOUR')), cost_vector_search_spend / cost_by_serving_endpoint
    # (billing_origin_product='VECTOR_SEARCH').
    # =========================================================================================
    usage(
        con, "bl_u_net_bytes", workspace_id=WS_DEV, sku_name="bl_NETWORK_EGRESS_BYTES",
        usage_date_=D(5), usage_quantity=500000.0, billing_origin_product="ALL_PURPOSE",
        usage_unit="BYTES", usage_type="NETWORK_BYTE",
        usage_metadata=_usage_metadata(
            source_region="us-east-1", destination_region="us-west-2",
            networking_client="workspace", recipient_id="bl_recipient1",
        ),
    )
    usage(
        con, "bl_u_net_hours", workspace_id=WS_DEV, sku_name="bl_NETWORK_EGRESS_HOURS",
        usage_date_=D(5), usage_quantity=12.0, billing_origin_product="ALL_PURPOSE",
        usage_unit="HOURS", usage_type="NETWORK_HOUR", cloud="gcp",
        usage_metadata=_usage_metadata(
            source_region=None, destination_region=None,  # always NULL on GCP per the query's caveat
            networking_client="vpc_peering", recipient_id="bl_recipient2",
        ),
    )
    usage(
        con, "bl_u_vs_serving", workspace_id=WS_PROD, sku_name="bl_VECTOR_SEARCH_SERVING",
        usage_date_=D(5), usage_quantity=18.0, billing_origin_product="VECTOR_SEARCH",
        usage_unit="DBU", usage_type="COMPUTE_TIME",
        usage_metadata=_usage_metadata(endpoint_id="bl_vs_ep1", endpoint_name="bl_vs_endpoint"),
    )
    usage(
        con, "bl_u_vs_storage", workspace_id=WS_PROD, sku_name="bl_VECTOR_SEARCH_STORAGE",
        usage_date_=D(5), usage_quantity=3.5, billing_origin_product="VECTOR_SEARCH",
        usage_unit="GB", usage_type="STORAGE_SPACE",
        usage_metadata=_usage_metadata(endpoint_id="bl_vs_ep1", endpoint_name="bl_vs_endpoint"),
    )

    # =========================================================================================
    # SEC F -- default storage + tags + identity (D(6)).
    # =========================================================================================
    usage(
        con, "bl_u_storage1", workspace_id=WS_PROD, sku_name="bl_DEFAULT_STORAGE_TIER1",
        usage_date_=D(6), usage_quantity=8.0, billing_origin_product="DEFAULT_STORAGE",
        usage_unit="DBU", usage_type="STORAGE_SPACE",
        usage_metadata=_usage_metadata(storage_api_type="TIER_1", catalog_id="bl_catalog1"),
    )  # cost_default_storage_dsu filters on storage_api_type IS NOT NULL, not this literal
    usage(
        con, "bl_u_tag_a", workspace_id=WS_PROD, sku_name="bl_ALL_PURPOSE_COMPUTE",
        usage_date_=D(6), usage_quantity=10.0, billing_origin_product="ALL_PURPOSE",
        custom_tags={"team": "a"},
    )
    usage(
        con, "bl_u_tag_b", workspace_id=WS_DEV, sku_name="bl_ALL_PURPOSE_COMPUTE",
        usage_date_=D(6), usage_quantity=12.0, billing_origin_product="ALL_PURPOSE",
        custom_tags={"team": "b"},
    )
    usage(
        con, "bl_u_tag_empty", workspace_id=WS_UAT, sku_name="bl_ALL_PURPOSE_COMPUTE",
        usage_date_=D(6), usage_quantity=7.0, billing_origin_product="ALL_PURPOSE",
        custom_tags={},
    )  # LATERAL VIEW OUTER explode(custom_tags) must still emit one tag_key=NULL row for this one
    usage(
        con, "bl_u_id_email", workspace_id=WS_PROD, sku_name="bl_ALL_PURPOSE_COMPUTE",
        usage_date_=D(6), usage_quantity=9.0, billing_origin_product="ALL_PURPOSE",
        identity_metadata=_identity_metadata(
            run_as="alice@example.com", owned_by="alice@example.com", created_by="alice@example.com",
        ),
    )  # identity_type = 'user'
    usage(
        con, "bl_u_id_sp", workspace_id=WS_PROD, sku_name="bl_ALL_PURPOSE_COMPUTE",
        usage_date_=D(6), usage_quantity=6.0, billing_origin_product="ALL_PURPOSE",
        identity_metadata=_identity_metadata(
            run_as="11111111-2222-3333-4444-555555555555",
            owned_by="11111111-2222-3333-4444-555555555555",
            created_by="11111111-2222-3333-4444-555555555555",
        ),
    )  # identity_type = 'service_principal'; kept as-is (GUID-shaped), not masked
    usage(
        con, "bl_u_id_unknown", workspace_id=WS_PROD, sku_name="bl_ALL_PURPOSE_COMPUTE",
        usage_date_=D(6), usage_quantity=4.0, billing_origin_product="ALL_PURPOSE",
        identity_metadata=_identity_metadata(run_as="__REDACTED__", owned_by=None, created_by=None),
    )  # identity_type = 'unknown'

    # =========================================================================================
    # SEC G -- Photon cross-check, serverless/classic split, tier coverage (D(7)).
    # =========================================================================================
    usage(
        con, "bl_u_photon1", workspace_id=WS_PROD, sku_name="bl_ALL_PURPOSE_COMPUTE_(PHOTON)",
        usage_date_=D(7), usage_quantity=22.0, billing_origin_product="ALL_PURPOSE",
        product_features=_product_features(is_serverless=False, is_photon=True),
    )
    usage(
        con, "bl_u_nonphoton1", workspace_id=WS_PROD, sku_name="bl_ALL_PURPOSE_COMPUTE",
        usage_date_=D(7), usage_quantity=14.0, billing_origin_product="ALL_PURPOSE",
        product_features=_product_features(is_serverless=False, is_photon=False),
    )
    usage(
        con, "bl_u_serverless1", workspace_id=WS_PROD, sku_name="bl_ALL_PURPOSE_COMPUTE",
        usage_date_=D(7), usage_quantity=9.0, billing_origin_product="ALL_PURPOSE",
        product_features=_product_features(is_serverless=True),
    )
    usage(
        con, "bl_u_classic1", workspace_id=WS_PROD, sku_name="bl_ALL_PURPOSE_COMPUTE",
        usage_date_=D(7), usage_quantity=11.0, billing_origin_product="ALL_PURPOSE",
        product_features=_product_features(is_serverless=False),
    )
    usage(
        con, "bl_u_tier_cov", workspace_id=WS_PROD, sku_name="bl_JOBS_COMPUTE_CLASSIC",
        usage_date_=D(7), usage_quantity=16.0, billing_origin_product="JOBS",
        product_features=_product_features(jobs_tier="LIGHT", sql_tier="CLASSIC", dlt_tier="CORE"),
    )

    # =========================================================================================
    # SEC H -- record_type correction pair + account-level (workspace_id NULL) row (D(8)).
    # Same key (workspace/sku/cloud/date/product/type/unit) across all three correction rows so
    # cost_totals_by_sku_day's GROUP BY combines them into one row: net_usage_quantity (110) is
    # provably different from original_usage_quantity (100).
    # =========================================================================================
    _corr_key = dict(
        workspace_id=WS_PROD, sku_name="bl_JOBS_COMPUTE_CLASSIC", usage_date_=D(8),
        billing_origin_product="JOBS", usage_type="COMPUTE_TIME", usage_unit="DBU",
    )
    usage(
        con, "bl_u_corr_orig", **_corr_key, usage_quantity=100.0, record_type="ORIGINAL",
        usage_metadata=_usage_metadata(job_id="bl_corr_job", job_run_id="bl_corr_run"),
    )
    usage(
        con, "bl_u_corr_retraction", **_corr_key, usage_quantity=-100.0, record_type="RETRACTION",
        usage_metadata=_usage_metadata(job_id="bl_corr_job", job_run_id="bl_corr_run"),
    )
    usage(
        con, "bl_u_corr_restatement", **_corr_key, usage_quantity=110.0, record_type="RESTATEMENT",
        usage_metadata=_usage_metadata(job_id="bl_corr_job", job_run_id="bl_corr_run"),
    )  # net_usage_quantity = 100 - 100 + 110 = 110 (all record_types); original_usage_quantity = 100
    usage(
        con, "bl_u_acct_level", workspace_id=None, sku_name="bl_MODEL_SERVING_STANDARD",
        usage_date_=D(8), usage_quantity=3.0, billing_origin_product="MODEL_SERVING",
        usage_type="COMPUTE_TIME",
        product_features=_product_features(is_serverless=True),
    )  # account-level: workspace_id IS NULL, kept (not dropped) per cost_totals_by_sku_day's caveat

    # =========================================================================================
    # SEC I -- cost_dbsql_allocation_gap: raw 'SQL' DBU usage vs attributed 'SQL' DBU usage on the
    # same usage_date/cloud key (D(9)). raw 80 - attributed 55 = a provable 25 DBU gap.
    # =========================================================================================
    usage(
        con, "bl_u_sql_raw1", workspace_id=WS_PROD, sku_name="bl_DBSQL_COMPUTE",
        usage_date_=D(9), usage_quantity=80.0, billing_origin_product="SQL",
        usage_metadata=_usage_metadata(warehouse_id="bl_wh_gap"),
    )
    attributed(
        con, "bl_a_att1", workspace_id=WS_PROD, sku_name="bl_DBSQL_COMPUTE",
        usage_date_=D(9), active_usage_quantity=55.0, billing_origin_product="SQL",
        dbsql_statement_id="bl_stmt1", warehouse_id="bl_wh_gap", client_application="bl_app",
        executed_by="alice@example.com", query_tags={"k": "v"}, usage_record_ids=["bl_u_sql_raw1"],
        custom_tags={"team": "a"},
    )
    attributed(
        con, "bl_a_att_nonmatch", workspace_id=WS_PROD, sku_name="bl_JOBS_COMPUTE_CLASSIC",
        usage_date_=D(9), active_usage_quantity=1.0, billing_origin_product="JOBS",
    )  # proves billing_origin_product='SQL' really excludes a non-SQL attributed_usage row

    # =========================================================================================
    # SEC J -- cost_usage_policy_coverage's three-way CASE + NOT_ASSESSED (D(10), workspace
    # acme-dev, all serverless SQL rows except the NOT_ASSESSED one, which has is_serverless=NULL
    # (not FALSE) so the query's own NOT_ASSESSED branch fires).
    # =========================================================================================
    usage(
        con, "bl_u_pol_usage", workspace_id=WS_DEV, sku_name="bl_DBSQL_COMPUTE",
        usage_date_=D(10), usage_quantity=8.0, billing_origin_product="SQL",
        custom_tags={},
        usage_metadata=_usage_metadata(usage_policy_id="bl_policy1"),
        product_features=_product_features(is_serverless=True),
    )
    usage(
        con, "bl_u_pol_budget", workspace_id=WS_DEV, sku_name="bl_DBSQL_COMPUTE",
        usage_date_=D(10), usage_quantity=6.0, billing_origin_product="SQL",
        custom_tags={"team": "a"},
        usage_metadata=_usage_metadata(budget_policy_id="bl_budget1"),
        product_features=_product_features(is_serverless=True),
    )
    usage(
        con, "bl_u_pol_none", workspace_id=WS_DEV, sku_name="bl_DBSQL_COMPUTE",
        usage_date_=D(10), usage_quantity=5.0, billing_origin_product="SQL",
        custom_tags={},
        product_features=_product_features(is_serverless=True),
    )  # WARN: is_serverless=true AND policy_coverage='none'
    usage(
        con, "bl_u_pol_null", workspace_id=WS_DEV, sku_name="bl_DBSQL_COMPUTE",
        usage_date_=D(10), usage_quantity=3.0, billing_origin_product="SQL",
        custom_tags={},
        product_features=_product_features(is_serverless=None),
    )  # NOT_ASSESSED: product_features.is_serverless IS NULL

    # =========================================================================================
    # SEC K -- dedicated window-boundary series (job bl_win_job, workspace acme-prod, priced SKU
    # bl_JOBS_COMPUTE_CLASSIC): D(5)=5 (in the 7-day window), D(20)=7 (out of 7-day, in 30-day),
    # D(45)=11 (out of 30-day, in 90-day), D(0)=999 (today -- must be excluded by every query's
    # usage_date < current_date() predicate; a bug here inflates every window's total visibly).
    # 7-day sum = 5; 30-day sum = 5+7 = 12; 90-day sum = 5+7+11 = 23 -> 30 > 7, 90 > 30.
    # =========================================================================================
    usage(
        con, "bl_u_win_d5", workspace_id=WS_PROD, sku_name="bl_JOBS_COMPUTE_CLASSIC",
        usage_date_=D(5), usage_quantity=5.0, billing_origin_product="JOBS",
        usage_metadata=_usage_metadata(job_id="bl_win_job", job_run_id="bl_win_run_d5"),
    )
    usage(
        con, "bl_u_win_d20", workspace_id=WS_PROD, sku_name="bl_JOBS_COMPUTE_CLASSIC",
        usage_date_=D(20), usage_quantity=7.0, billing_origin_product="JOBS",
        usage_metadata=_usage_metadata(job_id="bl_win_job", job_run_id="bl_win_run_d20"),
    )
    usage(
        con, "bl_u_win_d45", workspace_id=WS_PROD, sku_name="bl_JOBS_COMPUTE_CLASSIC",
        usage_date_=D(45), usage_quantity=11.0, billing_origin_product="JOBS",
        usage_metadata=_usage_metadata(job_id="bl_win_job", job_run_id="bl_win_run_d45"),
    )
    usage(
        con, "bl_u_win_d0", workspace_id=WS_PROD, sku_name="bl_JOBS_COMPUTE_CLASSIC",
        usage_date_=D(0), usage_quantity=999.0, billing_origin_product="JOBS",
        usage_metadata=_usage_metadata(job_id="bl_win_job", job_run_id="bl_win_run_d0"),
    )

    # =========================================================================================
    # SEC L (T-63, DEC-60) -- workspace tag ATTRIBUTES (cost_center), distinct from the env-ish
    # tag HINTS above (bl_u_tag_a/bl_u_tag_b, custom_tags={"team": ...}) and from the row-level
    # custom_tags filter (out of scope, DEC-60). Every row below lands on D(200) -- outside every
    # window (7/30/90) any windowed finding query ever reads, and
    # dbt/models/dims/int_workspace_tag_hints.sql is itself unwindowed (T-23's own design,
    # unchanged) -- so these rows are invisible to every windowed cost/performance finding (no
    # collateral change to any other task's asserted totals, including this workspace's own
    # existing rows above) and visible only to dims.dim_workspace's new <key>/<key>_share/
    # <key>_reason columns. Raw custom_tags KEYS are deliberately spelled three different ways
    # (mixed case, a space, a hyphen) to prove config/tag_aliases.yml's normalisation end to end
    # (DEC-60 rule 4), not just the trivial exact-spelling case.
    #
    # acme-prod (WS_PROD): cost_center "engineering" carries one enormous single row, DWARFING
    # every other builder's WS_PROD usage (in OR out of window) by many orders of magnitude,
    # against cost_center "sandbox" split across THREE tiny rows. Row COUNT favours sandbox
    # (3 > 1); summed DBUs favour engineering overwhelmingly. Proves DEC-60 rule 2 (rank by
    # spend, never rows) and doubles as the "one cost centre on ~95%+ of its DBUs" clean fixture
    # (task rule 7) -- engineering's share is far north of any plausible share floor regardless
    # of what any other builder contributes to WS_PROD's grand total.
    # Workspace tags reach classic compute only, so every row here carries a cluster or warehouse id.
    # acme-dev (WS_DEV): cost_center "marketing"/"sales" 55/45 -- below any sane share floor
    # (config/settings.yml tag_share_floor: 0.6) even in the impossible case WS_DEV has zero
    # other usage anywhere, so it must read 'mixed' regardless of what any other builder
    # contributes to WS_DEV's total.
    # acme-uat (WS_UAT): untouched here -- it already carries billed DBUs (bl_u_pool1/nb_crit/
    # nb_warn/nb_ok above) with NO cost_center tag anywhere, so it reads 'not_tagged' for free,
    # proving that state is distinct from 'mixed' without adding a single new row.
    # =========================================================================================
    usage(
        con, "bl_ta_cc_eng", workspace_id=WS_PROD, sku_name="bl_ALL_PURPOSE_COMPUTE",
        usage_metadata=_usage_metadata(cluster_id="bl_ta_cluster"), usage_date_=D(200), usage_quantity=5_000_000.0, billing_origin_product="ALL_PURPOSE",
        custom_tags={"Cost Center": "engineering"},
    )  # raw key "Cost Center" (space + mixed case) -- DEC-60 rule 4's normalisation
    usage(
        con, "bl_ta_cc_sandbox_r1", workspace_id=WS_PROD, sku_name="bl_ALL_PURPOSE_COMPUTE",
        usage_metadata=_usage_metadata(cluster_id="bl_ta_cluster"), usage_date_=D(200), usage_quantity=1.0, billing_origin_product="ALL_PURPOSE",
        custom_tags={"cost-center": "sandbox"},
    )  # raw key "cost-center" (hyphen) -- same normalisation, different separator
    usage(
        con, "bl_ta_cc_sandbox_r2", workspace_id=WS_PROD, sku_name="bl_ALL_PURPOSE_COMPUTE",
        usage_metadata=_usage_metadata(cluster_id="bl_ta_cluster"), usage_date_=D(200), usage_quantity=1.0, billing_origin_product="ALL_PURPOSE",
        custom_tags={"cost-center": "sandbox"},
    )
    usage(
        con, "bl_ta_cc_sandbox_r3", workspace_id=WS_PROD, sku_name="bl_ALL_PURPOSE_COMPUTE",
        usage_metadata=_usage_metadata(cluster_id="bl_ta_cluster"), usage_date_=D(200), usage_quantity=1.0, billing_origin_product="ALL_PURPOSE",
        custom_tags={"cost-center": "sandbox"},
    )  # sandbox: 3 rows / 3.0 DBUs total -- wins on COUNT(*), loses on SUM(DBU) to engineering
    usage(
        con, "bl_ta_cc_mkt", workspace_id=WS_DEV, sku_name="bl_DBSQL_COMPUTE",
        usage_metadata=_usage_metadata(warehouse_id="bl_ta_wh"), usage_date_=D(200), usage_quantity=55.0, billing_origin_product="SQL",
        custom_tags={"cost_center": "marketing"},
    )
    usage(
        con, "bl_ta_cc_sales", workspace_id=WS_DEV, sku_name="bl_DBSQL_COMPUTE",
        usage_metadata=_usage_metadata(warehouse_id="bl_ta_wh"), usage_date_=D(200), usage_quantity=45.0, billing_origin_product="SQL",
        custom_tags={"cost_center": "sales"},
    )  # marketing/sales 55/45 -- below any sane share floor -> 'mixed'
    usage(
        con, "bl_ta_dev_untagged", workspace_id=WS_DEV, sku_name="bl_DBSQL_COMPUTE",
        usage_metadata=_usage_metadata(warehouse_id="bl_ta_wh"), usage_date_=D(200),
        usage_quantity=120.0, billing_origin_product="SQL", custom_tags={},
    )  # keeps acme-dev's cost_center coverage under the coverage floor
    usage(
        con, "bl_ta_team_a", workspace_id=WS_PROD, sku_name="bl_ALL_PURPOSE_COMPUTE",
        usage_metadata=_usage_metadata(cluster_id="bl_ta_cluster"), usage_date_=D(200),
        usage_quantity=10.0, billing_origin_product="ALL_PURPOSE", custom_tags={"team": "a"},
    )  # acme-prod's only classic team tag: full dominance on a sliver of coverage

    # =========================================================================================
    # SEC M (T-69A review round 2, DEC-66.1) -- price_basis 'free' path: a FREE_USAGE-pattern SKU
    # deliberately gets NO priced_sku() call, so its list-price join always LEFT-JOINs to NULL --
    # exactly like bl_SKU_NO_PRICE (SEC A) EXCEPT that upper(sku_name) LIKE '%FREE_USAGE%' makes
    # this a real $0 (price_basis='free'), never a pricing-coverage gap (price_basis='unpriced').
    # D(11), workspace acme-uat: the only unused day among all four billing__usage-writing
    # builders (billing.py/lakeflow.py/compute.py/serving_storage.py), and acme-uat otherwise
    # carries only priced usage (SEC B/C above), so this row does not change any existing
    # priced/unpriced classification for that workspace. No job_id/cluster_id/warehouse_id/
    # notebook_id/endpoint_id/policy id is set, so this row is invisible to every finding that
    # filters on one of those (cost_by_job, cost_by_compute_resource, cost_by_notebook,
    # cost_serving_mode_by_endpoint, cost_vector_search_spend, compute_serving_endpoint_cost_status,
    # cost_usage_policy_coverage) -- it is visible only to the SKU/day-level rollups
    # (cost_dollarized_by_sku_day, cost_cloud_infra, cost_actual_vs_list_by_sku,
    # cost_totals_by_sku_day, cost_chargeback_by_tag's untagged row, overview_spend_estimate).
    # =========================================================================================
    usage(
        con, "bl_u_free1", workspace_id=WS_UAT, sku_name="bl_GENIE_FREE_USAGE",
        usage_date_=D(11), usage_quantity=25.0, billing_origin_product="ALL_PURPOSE",
    )

    # =========================================================================================
    # SEC N -- cost_unnamed_workspaces: three workspaces billed here but never passed to
    # workspace() above, so they carry no access__workspaces_latest row at all (unlike every
    # other workspace id in this file). Same priced SKU as SEC G/L (bl_ALL_PURPOSE_COMPUTE).
    # uw_crit: usage at D(3)/D(1), inside every 7/30/90-day window -> CRITICAL at every window.
    # uw_warn: usage at D(80)/D(45), outside the 7- and 30-day windows but inside the 90-day one,
    # and last_used (D(45)) is within 90 days regardless -> WARN at w=7/30, CRITICAL at w=90 (its
    # own D(80)/D(45) usage falls inside a 90-day window, so "spend in the window" is genuinely
    # true there too). uw_ok: usage at D(250)/D(200), outside every window and last_used is over
    # 90 days ago -> OK at every window. The shared 1111/2222/3333 workspaces (named above) must
    # never appear in this finding.
    # =========================================================================================
    usage(
        con, "bl_u_uw_crit_r1", workspace_id="uw_crit", sku_name="bl_ALL_PURPOSE_COMPUTE",
        usage_date_=D(3), usage_quantity=10.0, billing_origin_product="ALL_PURPOSE",
    )
    usage(
        con, "bl_u_uw_crit_r2", workspace_id="uw_crit", sku_name="bl_ALL_PURPOSE_COMPUTE",
        usage_date_=D(1), usage_quantity=5.0, billing_origin_product="ALL_PURPOSE",
    )
    usage(
        con, "bl_u_uw_warn_r1", workspace_id="uw_warn", sku_name="bl_ALL_PURPOSE_COMPUTE",
        usage_date_=D(80), usage_quantity=8.0, billing_origin_product="ALL_PURPOSE",
    )
    usage(
        con, "bl_u_uw_warn_r2", workspace_id="uw_warn", sku_name="bl_ALL_PURPOSE_COMPUTE",
        usage_date_=D(45), usage_quantity=4.0, billing_origin_product="ALL_PURPOSE",
    )
    usage(
        con, "bl_u_uw_ok_r1", workspace_id="uw_ok", sku_name="bl_ALL_PURPOSE_COMPUTE",
        usage_date_=D(250), usage_quantity=6.0, billing_origin_product="ALL_PURPOSE",
    )
    usage(
        con, "bl_u_uw_ok_r2", workspace_id="uw_ok", sku_name="bl_ALL_PURPOSE_COMPUTE",
        usage_date_=D(200), usage_quantity=3.0, billing_origin_product="ALL_PURPOSE",
    )
