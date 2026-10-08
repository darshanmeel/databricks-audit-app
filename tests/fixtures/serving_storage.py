"""tests/fixtures/serving_storage.py -- batch G+H (T-22), fixture builder for the 9 ids this task
owns: 4 in domain serving_ai (compute_ai_gateway_usage, compute_serving_endpoint_cost_status,
compute_serving_endpoint_usage, serving_endpoint_traffic_by_endpoint) and 5 in domain storage
(po_clustering_activity, po_clustering_column_churn, po_data_skipping_backfill,
po_maintenance_cost_by_table, po_vacuum_reclaimed_bytes). Fills system.serving.served_entities,
system.serving.endpoint_usage, system.ai_gateway.usage,
system.storage.predictive_optimization_operations_history, and (supplementary, per the 3
cost/traffic queries' billing.usage JOIN) system.billing.usage. Does NOT touch
system.information_schema.tables -- none of these 9 query bodies read it (confirmed by reading
every body in app/queries/vendored/serving_ai/ and app/queries/vendored/storage/; see PLAN.md 7.2
row G+H and this task's own Inputs section).

Per DEC-15, this builder uses the shared workspaces 1111/2222/3333 (written by billing.py, not
this file) and prefixes every other id it owns with `ss_`. tests/fixtures/ports.py (T-11) already
writes `pt_`-prefixed rows into served_entities/endpoint_usage for compute_serving_dormant_endpoints
and storage_target_table_discovery -- every id below is disjoint from those (ss_ vs pt_ never
collide), and both builders' rows are unioned by name at dbt-read time (base.write_parquet /
build_fixtures.py's per-builder isolation model: each builder runs against its OWN fresh
in-memory connection, so this file's build(con) never sees ports.py's rows and vice versa; the
union only happens later, at parquet-read time, which is exactly why every builder must own a
disjoint id prefix instead of relying on row-count or connection-state isolation).

Per the same isolation model, this file writes its OWN system.billing.usage rows (own record_ids,
own SKU names, own endpoint ids) rather than importing or reusing any of tests/fixtures/billing.py's
(T-09) rows -- billing.py's build() runs against a *different* connection, so its rows are never
visible here; this file duplicates billing.py's INSERT shape (usage_metadata / identity_metadata /
product_features STRUCT defaults, one explicit key per DDL field) rather than importing billing.py,
matching the self-contained-module convention every other builder (ports.py, governance.py,
query_history.py) already follows (each imports only `from base import AS_OF, write_parquet`).

------------------------------------------------------------------------------------------------
compute_serving_endpoint_cost_status's 7-way status/tracking_status ladder (period_days=30 default,
retention_days=90 FIXED (not window-looped -- see f_compute_serving_endpoint_cost_status.sql's
generated model: only the `w` in the cost/usage_entity CTEs' date filters is windowed; the
`param(..., 'retention_days', 90)` substitution used by usage_ever is NOT), warn_low_requests=10,
top_n=200. All billing rows below land on D(3) (2026-09-18, inside every one of 7/30/90) so band
membership stays stable across windows for every id EXCEPT ss_se_idlewindow (deliberately not):

  served_entity_id / endpoint_id   band (w=30)      why
  ss_se_ok / ss_ep_ok              OK               15 requests all at D(3); >10 at every window
  ss_se_warn_low / ss_ep_warn_low  WARN (low-req)    5 requests at D(3); <=10 at every window
  ss_se_trackingoff/ss_ep_trackingoff WARN (tracking-off) 0 endpoint_usage rows EVER; bills at D(3)
  ss_se_idlewindow/ss_ep_idlewindow CRITICAL @ w=7,30; WARN @ w=90 -- 8 requests all at D(45): inside
                                    the 90-day retention (ever_requests=8) but outside the 7- and
                                    30-day period windows (ep_requests=0 there) -> CRITICAL "idle in
                                    window"; at w=90 those 8 requests ARE inside the period window,
                                    so ep_requests=8 <= warn_low_requests(10) -> WARN. A deliberate
                                    status flip, same shape as compute_serving_dormant_endpoints'
                                    (ports.py) w=7 flip.
  ss_se_win / ss_ep_win            OK (window-count check only) -- 11 requests at D(3), +1 at D(16),
                                    +1 at D(68): entity_requests_window/endpoint_requests_window =
                                    11 (w=7) / 12 (w=30) / 13 (w=90), all > 10 so status stays OK at
                                    every window -- isolates the window-boundary check from the band
                                    ladder (unlike ss_se_idlewindow, which conflates both on purpose).
  ss_ep_untracked (bills only)     NOT_ASSESSED ("bills but not in served_entities") -- billing row
                                    with usage_metadata.endpoint_id='ss_ep_untracked', NO
                                    served_entities row at all -> ent.endpoint_id IS NULL.
  ss_ep_vs (bills only, VS)        NOT_ASSESSED ("Vector Search") -- billing_origin_product=
                                    'VECTOR_SEARCH' -> is_vs=1, checked before the served_entities
                                    join even matters.
  (no endpoint_id; bills by name)  NOT_ASSESSED ("Vector Search") -- ss_bl_vs_byname carries
                                    usage_metadata.endpoint_id=NULL, endpoint_name='ss-ep-byname'
                                    only, so this row's OWN output endpoint_id is also NULL (see
                                    the `cost_ep`/COALESCE comment at ss_bl_vs_byname's own
                                    billing_usage() call in SEC S) -- exercises the
                                    `COALESCE(endpoint_id, endpoint_name_billing)` /
                                    `COALESCE(ent.endpoint_name, ce.endpoint_name_billing)` by-name
                                    paths review round 2 (item 6) found untested.

Every billing row below is priced against this builder's OWN list_prices rows (SEC S's opening
list_price() calls, ss_MODEL_SERVING_STANDARD at $0.08/DBU and ss_VECTOR_SEARCH_SERVING at
$0.09/DBU -- review round 2, item 2: without them est_usd_list read 0.0 for the whole serving
domain, since billing__list_prices held only bl_*/lf_* SKUs and the 3 billing-joined serving_ai
bodies' `LEFT JOIN price` never matched an ss_ row), so est_usd_list = usage_quantity * the row's
own rate for every ss_ row of compute_serving_endpoint_cost_status, compute_serving_endpoint_usage
and serving_endpoint_traffic_by_endpoint -- asserted directly in the test module (net_dbus is
simply each row's own billing usage_quantity, unaffected by pricing).

Predicted worst-first order at w=30 (status rank CRITICAL=0/WARN=1/NOT_ASSESSED=2/OK=3, tie-break
net_dbus DESC per the model's own ORDER BY -- see the module docstring's note on this id's own
ORDER BY bug: net_dbus DESC is actually the ONLY sort key that fires today, not a tiebreak):
ss_ep_idlewindow(9), ss_ep_warn_low(8), ss_ep_trackingoff(6), ss_ep_untracked(7), ss_ep_vs(5),
ss_ep_ok(12), ss_ep_win(10), and finally the by-name row (endpoint_id NULL, net_dbus=4) last --
net_dbus is each row's own billing usage_quantity (see SEC S below), asserted directly in the test
module as a whole-list monotonicity check (a canary that starts failing the day the ORDER BY bug
below is fixed), not re-derived here.

ss_se_ok also carries a genuine SCD2 pair (change_time D(400) then D(5), entity_version 0.9->1.0):
D(5) is INSIDE every one of the 7/30/90 windows, proving the ROW_NUMBER()...ORDER BY change_time
DESC dedup this query (and compute_serving_endpoint_usage / serving_endpoint_traffic_by_endpoint)
performs picks the latest row even when that latest row's change_time itself falls inside the
analysis window (per this task's own Inputs section). Every other served_entity_id below also
carries an SCD2 pair (D(400)/D(200), both outside every window) for general SCD2 realism.

compute_serving_endpoint_usage / serving_endpoint_traffic_by_endpoint (period_days-only, no
retention_days) are anchored on endpoint_usage (FROM endpoint_usage LEFT JOIN entities), not on
served_entities or billing -- an entity with ZERO endpoint_usage rows (ss_se_trackingoff) therefore
never appears in their output AT ALL (not a zero-count row: an ABSENT one -- the "without traffic"
case), and ss_se_idlewindow's 8 D(45) requests are wholly ABSENT at w=7/w=30 (D(45) is outside both
windows) and present (total_requests=8) only at w=90. ss_se_ok's own 15 endpoint_usage rows are
split 13 success (status_code=200) / 2 error (status_code=500) to exercise
compute_serving_endpoint_usage's success/error SUM(CASE) split (compute_ai_gateway_usage gets the
explicit 3-way 2xx/429/error split the task calls for instead -- see SEC G).
------------------------------------------------------------------------------------------------
po_* worst-first ordering note (IMPORTANT, easy to get backwards): po_clustering_column_churn,
po_data_skipping_backfill, po_maintenance_cost_by_table and po_vacuum_reclaimed_bytes each ORDER BY
`CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END, <metric> DESC`
-- i.e. NOT_ASSESSED (the implicit ELSE) sorts LAST, AFTER OK, unlike
compute_serving_endpoint_cost_status's own CASE (NOT_ASSESSED=2, OK=3, NOT_ASSESSED BEFORE OK) and
unlike every performance.yml / governance_events.yml id that uses the CRITICAL/WARN/NOT_ASSESSED/OK
convention. po_clustering_activity's ORDER BY has no CASE at all (`ORDER BY
clustering_estimated_dbu DESC`); DuckDB's default null order is NULLS LAST for both ASC and DESC
(confirmed with an in-memory sanity check against duckdb 1.5.1, matching the "query_cache_coldstart
... last on DuckDB" note in tasks/DECISIONS.md's Follow-ups section), so the NOT_ASSESSED row (NULL
clustering_estimated_dbu) sorts last there too -- consistent with the other four po_ ids' explicit
CASE. The test module's STATUS_RANK for these 5 ids is therefore
{CRITICAL:0, WARN:1, OK:2, NOT_ASSESSED:3}, NOT the CRITICAL/WARN/NOT_ASSESSED/OK convention used
everywhere else in this codebase -- verified by reading each body's own ORDER BY clause in
app/queries/vendored/storage/po_*.sql, not assumed.

Every po_* row below uses catalog_name='ss_catalog' (constant) with a distinct table_id/table_name
per scenario -- grain uniqueness never depends on catalog_name/schema_name/the operation_metrics
string fields, only on table_id, so this fixture's own scenarios can never collide with each other
or with any other builder's information_schema/storage rows (this file writes no
information_schema.tables rows at all -- none of the 9 ids read that source; see the module
docstring above).
------------------------------------------------------------------------------------------------
compute_ai_gateway_usage (period_days=30 only; NOT a finding -- no `AS status` anywhere in its
body, per DEC-08). GROUP BY is on the RAW g.endpoint_name / g.requester columns, but the SELECT
list only ever emits their masked display values -- a genuine DEC-48 masked-grain case (unlike
compute_serving_endpoint_usage / serving_endpoint_traffic_by_endpoint, whose GROUP BY / grain
already includes the raw, unmasked served_entity_id / endpoint_id, so masking endpoint_name /
served_entity_name there can never threaten uniqueness). config/grains/serving_storage.yml
therefore carries `# masked:` comments on both endpoint_name and requester for this id only.
endpoint_name still uses the old 2-char resource mask (DEC-48; it names a resource, not a person,
so DEC-66.3 does not apply to it), and this fixture avoids a real collision on it by construction:
within the single (usage_date=D(3), workspace_id=1111) group, the three raw endpoint_name values
used (ss-gw-ep1, None, qq-gw-win-ep) mask to three distinct values (ss****, NULL, qq****), so no
two raw (endpoint_name, requester) pairs in that group ever mask to the identical output tuple --
see the SEC G code below for the specific 'qq' prefix chosen for the window-boundary series and why
(an earlier draft's 'zz-gw-win-ep'/'zz_gw_win_req' DID collide with group c's 'ss-gw-ep1'/
'zz_carter' under that 2-char mask, caught by an in-memory duckdb sanity run against the real
generated model SQL, not assumed). requester is a person and is now masked under DEC-66.3's
hash-derived format instead, which makes a same-group collision between two different raw
requester values vanishingly unlikely on its own -- the endpoint_name engineering above remains
the operative guard for this id's grain uniqueness.
------------------------------------------------------------------------------------------------
"""
from __future__ import annotations

from datetime import datetime, timedelta
from datetime import time as dtime

import duckdb

from base import AS_OF, write_parquet  # noqa: F401 (write_parquet re-exported for build_fixtures.py)

D0 = AS_OF.date()  # 2026-09-21


def D(n: int):
    """D(n) = D0 - n days (a date)."""
    return D0 - timedelta(days=n)


def DT(n: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    """A full datetime at D(n) with the given time-of-day -- every timestamp column this file
    writes stays AS_OF-relative (never an absolute literal), matching ports.py / billing.py."""
    return datetime.combine(D(n), dtime(hour, minute, second))


# Shared fixture workspaces (billing.py writes these rows; per DEC-15 every builder except
# drilldown.py shares them without re-writing them itself).
WS_PROD = "1111"
WS_DEV = "2222"
WS_UAT = "3333"

ACCOUNT_ID = "ss_acct"

# Window-boundary anchors used throughout this file (storage AND serving_ai sections alike):
# D(3) is inside every one of 7/30/90; D(16) is inside 30/90 only; D(68) is inside 90 only.
WIN_7 = 3
WIN_30 = 16
WIN_90 = 68


# =================================================================================================
# STRUCT defaults for system.billing.usage -- duplicated from tests/fixtures/billing.py's own
# _usage_metadata()/_identity_metadata()/_product_features() (same DDL, same field set) rather than
# imported, per the per-builder isolation model explained in the module docstring above: every
# field tests/fixtures/ddl.py's DDL declares is given an explicit key, defaulted to None, so a
# caller only overrides the fields its own row needs and no column is ever silently omitted.
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


# billing__list_prices -- this builder's OWN priced SKUs (ss_MODEL_SERVING_STANDARD,
# ss_VECTOR_SEARCH_SERVING), disjoint from billing.py's bl_* SKUs and lakeflow.py's lf_* SKUs
# (DEC-15 sanctions a builder owning its own SKUs; lakeflow.py's own list_price() helper is the
# precedent this mirrors). Without this, `LEFT JOIN price` in all 3 billing-joined serving_ai
# bodies never matches an ss_ row and est_usd_list reads 0.0 for the whole serving domain --
# found in review round 2 (item 2) and fixed here, one current (price_end_time NULL) row per SKU
# (this file has no need to prove the current-vs-expired price-window join itself -- that is
# T-13's test_cost_priced.py's job -- so, unlike billing.py's fuller priced_sku(), only a current
# row is written per SKU, matching lakeflow.py's own list_price() shape exactly).
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
# system.serving.served_entities / system.serving.endpoint_usage insert helpers.
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
            served_entity_id, ACCOUNT_ID, workspace_id, "ss_creator", endpoint_name, endpoint_id,
            served_entity_name, entity_type, entity_name or served_entity_name, entity_version,
            endpoint_config_version, None,
            None, None, None, None,  # external/foundation/custom/feature_spec model configs: unused
            change_time, None,
        ],
    )


def served_entity_scd2(
    con: duckdb.DuckDBPyConnection,
    *,
    served_entity_id: str,
    workspace_id: str,
    endpoint_id: str,
    endpoint_name: str,
    served_entity_name: str,
    old_change_time: datetime,
    new_change_time: datetime,
    old_entity_version: str = "0.9",
    new_entity_version: str = "1.0",
    old_entity_type: str = "FOUNDATION_MODEL",
    new_entity_type: str = "FOUNDATION_MODEL",
) -> None:
    """Two served_entities rows for the same (workspace_id, endpoint_id, served_entity_id) key,
    proving every consuming query's ROW_NUMBER() ... ORDER BY change_time DESC dedup picks the
    NEW row (new_entity_version), never the old one -- per this task's own Inputs section.

    entity_version proves this for compute_serving_endpoint_cost_status (entity_version is in its
    output); it is NOT in compute_serving_endpoint_usage's own output, so ss_se_ok's own call below
    also varies entity_type between the old/new rows (review round 2, item 1: "the SCD2-dedup proof
    this id was supposed to carry currently rests on nothing" -- with old==new entity_type, a dedup
    that picked the WRONG row would be unobservable in that id's own result)."""
    served_entity(
        con, served_entity_id=served_entity_id, workspace_id=workspace_id, endpoint_id=endpoint_id,
        endpoint_name=endpoint_name, served_entity_name=served_entity_name,
        change_time=old_change_time, entity_version=old_entity_version, endpoint_config_version=1,
        entity_type=old_entity_type,
    )
    served_entity(
        con, served_entity_id=served_entity_id, workspace_id=workspace_id, endpoint_id=endpoint_id,
        endpoint_name=endpoint_name, served_entity_name=served_entity_name,
        change_time=new_change_time, entity_version=new_entity_version, endpoint_config_version=2,
        entity_type=new_entity_type,
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
    requester: str = "ss_requester",
) -> None:
    con.execute(
        _EU_SQL,
        [
            ACCOUNT_ID, workspace_id, served_entity_id,
            f"ss_req_{req_suffix}", f"ss_dbreq_{req_suffix}",
            requester, status_code, request_time,
            input_tokens, output_tokens, input_tokens * 5, output_tokens * 5,
            {}, False,
        ],
    )


# =================================================================================================
# system.ai_gateway.usage insert helper -- every ARROW_SCHEMA field given an explicit value or NULL
# (DEC-15 / this task's Inputs section, "every field ... gets a value or an explicit NULL").
# =================================================================================================
_GW_SQL = (
    "INSERT INTO ai_gateway__usage "
    "(account_id, workspace_id, request_id, schema_version, endpoint_id, endpoint_name, "
    "endpoint_tags, endpoint_metadata, event_time, latency_ms, time_to_first_byte_ms, "
    "destination_type, destination_name, destination_id, destination_model, requester, "
    "requester_type, ip_address, url, user_agent, api_type, request_tags, input_tokens, "
    "output_tokens, total_tokens, token_details, response_content_type, status_code, "
    "routing_information, invocation_id, invocation_metadata, service_type, service_id, "
    "service_name, service_tags, mcp_metadata) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def gw_usage_row(
    con: duckdb.DuckDBPyConnection,
    *,
    req_suffix: str,
    workspace_id: str,
    endpoint_name: str | None,
    requester: str | None,
    event_time: datetime,
    status_code: int | None = 200,
    input_tokens: int = 100,
    output_tokens: int = 50,
    latency_ms: int = 120,
    endpoint_id: str = "ss_gw_epid",
) -> None:
    con.execute(
        _GW_SQL,
        [
            ACCOUNT_ID, workspace_id, f"ss_gwreq_{req_suffix}", 1, endpoint_id, endpoint_name,
            {}, None,
            event_time, latency_ms, latency_ms // 2,
            "model", None, None, None, requester,
            None, None, None, None, None, {},
            input_tokens, output_tokens, input_tokens + output_tokens, None,
            None, status_code,
            None, None, None, None, None,
            None, {}, None,
        ],
    )


# =================================================================================================
# system.storage.predictive_optimization_operations_history insert helper.
# =================================================================================================
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
    operation_type: str,
    start_time: datetime,
    usage_quantity: float | None,
    op_suffix: str = "0",
    operation_status: str = "SUCCESSFUL",
    operation_metrics: dict | None = None,
    workspace_id: str = WS_PROD,
    catalog_name: str = "ss_catalog",
    schema_name: str = "ss_schema",
    usage_unit: str = "DBU",
) -> None:
    con.execute(
        _PO_SQL,
        [
            ACCOUNT_ID, workspace_id, start_time, start_time + timedelta(minutes=30),
            "ss_metastore", "ss_metastore_id", catalog_name, schema_name, table_name, table_id,
            operation_type, f"ss_op_{table_id}_{op_suffix}", operation_status,
            operation_metrics if operation_metrics is not None else {},
            usage_unit, usage_quantity,
        ],
    )


# =================================================================================================
# Auto-discovered by build_fixtures.py (DEC-17): a module-level build(con), no registration list.
# =================================================================================================
def build(con: duckdb.DuckDBPyConnection) -> None:
    # =============================================================================================
    # SEC S -- served_entities / endpoint_usage / billing.usage for the 4 serving_ai ids that read
    # them (compute_serving_endpoint_cost_status, compute_serving_endpoint_usage,
    # serving_endpoint_traffic_by_endpoint). See the module docstring's band-ladder table.
    # =============================================================================================
    # This builder's own priced SKUs -- every billing_usage() call below prices against one of
    # these two (review round 2, item 2: est_usd_list was 0.0 for the whole serving domain before
    # this file wrote its own list_prices rows).
    list_price(con, "ss_MODEL_SERVING_STANDARD", default_rate=0.08, effective_list_default=0.10)
    list_price(con, "ss_VECTOR_SEARCH_SERVING", default_rate=0.09, effective_list_default=0.12)

    # ---- ss_se_ok / ss_ep_ok: OK band; "served entity WITH traffic"; SCD2 pair with the NEW row's
    # change_time (D(5)) INSIDE every window (7/30/90), per this task's Inputs section. -----------
    served_entity_scd2(
        con, served_entity_id="ss_se_ok", workspace_id=WS_PROD, endpoint_id="ss_ep_ok",
        endpoint_name="ss-ep-ok-endpoint", served_entity_name="ss-se-ok-entity",
        old_change_time=DT(400), new_change_time=DT(WIN_7),
        # entity_type differs old->new (CUSTOM_MODEL -> FOUNDATION_MODEL) as well as entity_version
        # (0.9 -> 1.0): compute_serving_endpoint_usage's own output has no entity_version column,
        # so entity_type is what proves ITS dedup picked the NEW row too (review round 2, item 1).
        old_entity_type="CUSTOM_MODEL", new_entity_type="FOUNDATION_MODEL",
    )
    # 15 requests at D(3): 13 success (200) + 2 error (500), exercising
    # compute_serving_endpoint_usage's success/error SUM(CASE) split.
    for i in range(13):
        endpoint_usage_row(
            con, workspace_id=WS_PROD, served_entity_id="ss_se_ok", req_suffix=f"ok_s{i}",
            request_time=DT(WIN_7, hour=i), status_code=200,
        )
    for i in range(2):
        endpoint_usage_row(
            con, workspace_id=WS_PROD, served_entity_id="ss_se_ok", req_suffix=f"ok_e{i}",
            request_time=DT(WIN_7, hour=13 + i), status_code=500,
        )
    billing_usage(
        con, "ss_bl_ok", workspace_id=WS_PROD, sku_name="ss_MODEL_SERVING_STANDARD",
        usage_date_=D(WIN_7), usage_quantity=12.0, billing_origin_product="MODEL_SERVING",
        endpoint_id="ss_ep_ok",
    )

    # ---- ss_se_warn_low / ss_ep_warn_low: WARN (low-request) band; 5 requests, all <=
    # warn_low_requests(10). SCD2 pair entirely outside every window. -----------------------------
    served_entity_scd2(
        con, served_entity_id="ss_se_warn_low", workspace_id=WS_PROD, endpoint_id="ss_ep_warn_low",
        endpoint_name="ss-ep-warnlow-endpoint", served_entity_name="ss-se-warnlow-entity",
        old_change_time=DT(400), new_change_time=DT(200),
    )
    for i in range(5):
        endpoint_usage_row(
            con, workspace_id=WS_PROD, served_entity_id="ss_se_warn_low", req_suffix=f"wl{i}",
            request_time=DT(WIN_7, hour=i), status_code=200,
        )
    billing_usage(
        con, "ss_bl_warn_low", workspace_id=WS_PROD, sku_name="ss_MODEL_SERVING_STANDARD",
        usage_date_=D(WIN_7), usage_quantity=8.0, billing_origin_product="MODEL_SERVING",
        endpoint_id="ss_ep_warn_low",
    )

    # ---- ss_se_trackingoff / ss_ep_trackingoff: WARN ("usage tracking likely OFF") band; ZERO
    # endpoint_usage rows EVER despite billing spend -- also the "served entity WITHOUT traffic"
    # case for compute_serving_endpoint_usage / serving_endpoint_traffic_by_endpoint (absent from
    # their output entirely, since both are anchored FROM endpoint_usage). ------------------------
    served_entity_scd2(
        con, served_entity_id="ss_se_trackingoff", workspace_id=WS_DEV, endpoint_id="ss_ep_trackingoff",
        endpoint_name="ss-ep-trackoff-endpoint", served_entity_name="ss-se-trackoff-entity",
        old_change_time=DT(400), new_change_time=DT(200),
    )
    billing_usage(
        con, "ss_bl_trackingoff", workspace_id=WS_DEV, sku_name="ss_MODEL_SERVING_STANDARD",
        usage_date_=D(WIN_7), usage_quantity=6.0, billing_origin_product="MODEL_SERVING",
        endpoint_id="ss_ep_trackingoff",
    )
    # P4-FIXES28: retention_history is workspace-wide, and tests/fixtures/ports.py also writes
    # WS_DEV endpoint_usage rows (pt_se_warn, 6 days back) -- without a row older than
    # :retention_days(90) somewhere in this workspace, the account's own captured history would
    # look too short and ss_ep_trackingoff would read NOT_ASSESSED (insufficient_retention_history)
    # instead of the confirmed "tracking off" WARN this scenario tests. Anchor row only; its own
    # served_entity_id is never otherwise referenced.
    endpoint_usage_row(
        con, workspace_id=WS_DEV, served_entity_id="ss_se_history_anchor_dev",
        req_suffix="history_anchor_dev", request_time=DT(120),
    )

    # ---- ss_se_idlewindow / ss_ep_idlewindow: CRITICAL at w=7/30, WARN at w=90 (deliberate status
    # flip) -- 8 requests all at D(45): inside the fixed 90-day retention window (ever_requests=8)
    # but outside the 7- and 30-day period windows (ep_requests=0 there). At w=90, all 8 land
    # inside the period window too: ep_requests=8 <= warn_low_requests(10) -> WARN. -----------------
    served_entity_scd2(
        con, served_entity_id="ss_se_idlewindow", workspace_id=WS_UAT, endpoint_id="ss_ep_idlewindow",
        endpoint_name="ss-ep-idle-endpoint", served_entity_name="ss-se-idle-entity",
        old_change_time=DT(400), new_change_time=DT(200),
    )
    for i in range(8):
        endpoint_usage_row(
            con, workspace_id=WS_UAT, served_entity_id="ss_se_idlewindow", req_suffix=f"idle{i}",
            request_time=DT(45, hour=i), status_code=200,
        )
    billing_usage(
        con, "ss_bl_idlewindow", workspace_id=WS_UAT, sku_name="ss_MODEL_SERVING_STANDARD",
        usage_date_=D(WIN_7), usage_quantity=9.0, billing_origin_product="MODEL_SERVING",
        endpoint_id="ss_ep_idlewindow",
    )

    # ---- ss_se_win / ss_ep_win: window-boundary check, OK band throughout (isolated from the band
    # ladder on purpose) -- 11 requests at D(3), +1 at D(16), +1 at D(68): entity/endpoint
    # requests_window = 11 (w=7) / 12 (w=30) / 13 (w=90), always > warn_low_requests(10). ----------
    served_entity_scd2(
        con, served_entity_id="ss_se_win", workspace_id=WS_PROD, endpoint_id="ss_ep_win",
        endpoint_name="ss-ep-win-endpoint", served_entity_name="ss-se-win-entity",
        old_change_time=DT(400), new_change_time=DT(200),
    )
    for i in range(11):
        endpoint_usage_row(
            con, workspace_id=WS_PROD, served_entity_id="ss_se_win", req_suffix=f"win7_{i}",
            request_time=DT(WIN_7, hour=i), status_code=200,
        )
    endpoint_usage_row(
        con, workspace_id=WS_PROD, served_entity_id="ss_se_win", req_suffix="win30",
        request_time=DT(WIN_30), status_code=200,
    )
    endpoint_usage_row(
        con, workspace_id=WS_PROD, served_entity_id="ss_se_win", req_suffix="win90",
        request_time=DT(WIN_90), status_code=200,
    )
    billing_usage(
        con, "ss_bl_win", workspace_id=WS_PROD, sku_name="ss_MODEL_SERVING_STANDARD",
        usage_date_=D(WIN_7), usage_quantity=10.0, billing_origin_product="MODEL_SERVING",
        endpoint_id="ss_ep_win",
    )

    # ---- ss_ep_untracked: NOT_ASSESSED ("bills but not in served_entities") -- billing spend, NO
    # served_entities row at all -> ent.endpoint_id IS NULL. ----------------------------------------
    billing_usage(
        con, "ss_bl_untracked", workspace_id=WS_PROD, sku_name="ss_MODEL_SERVING_STANDARD",
        usage_date_=D(WIN_7), usage_quantity=7.0, billing_origin_product="MODEL_SERVING",
        endpoint_id="ss_ep_untracked",
    )

    # ---- ss_ep_vs: NOT_ASSESSED ("Vector Search") -- billing_origin_product='VECTOR_SEARCH' ->
    # is_vs=1, no serving telemetry exists for Vector Search endpoints (checked before the
    # served_entities join even matters). ------------------------------------------------------------
    billing_usage(
        con, "ss_bl_vs", workspace_id=WS_PROD, sku_name="ss_VECTOR_SEARCH_SERVING",
        usage_date_=D(WIN_7), usage_quantity=5.0, billing_origin_product="VECTOR_SEARCH",
        endpoint_id="ss_ep_vs",
    )

    # ---- ss_bl_vs_byname: NOT_ASSESSED ("Vector Search"), billed BY NAME, not by id
    # (usage_metadata.endpoint_id NULL, endpoint_name='ss-ep-byname' only) -- review round 2 (item
    # 6): every OTHER billing row this builder writes, including ss_bl_vs above, carries a
    # non-NULL usage_metadata.endpoint_id, so the query's own
    # `cost_ep.endpoint_key = COALESCE(endpoint_id, endpoint_name_billing)` branch and the
    # `COALESCE(ent.endpoint_name, ce.endpoint_name_billing)` display fallback -- both written
    # specifically because Vector Search can bill by name alone -- were never exercised by this
    # fixture. cost_ep's own `MAX(endpoint_id) AS endpoint_id` is NULL for this row (no billing row
    # in its group ever set one), so the final SELECT's `ce.endpoint_id` -- and therefore this row's
    # own grain triple (workspace_id, endpoint_id, served_entity_id) -- comes out
    # ('1111', NULL, NULL). That NULL-endpoint_id shape is the one realistic duplicate-grain path
    # on this id (two different by-name-only Vector Search endpoints in the same workspace would
    # both land on (workspace_id, NULL, NULL) unless COALESCE's fallback to endpoint_name_billing
    # inside cost_ep's own GROUP BY keeps them apart, which it does here since it groups by
    # `COALESCE(endpoint_id, endpoint_name_billing)`, not by endpoint_id alone) -- this fixture
    # writes exactly one such row, so no actual collision occurs here, but see
    # test_compute_serving_endpoint_cost_status's own comment on this row for how the test locates
    # it (its `by_eid` dict, keyed on endpoint_id, drops any row with endpoint_id IS NULL by
    # construction -- this row needs its own lookup, not by_eid).
    billing_usage(
        con, "ss_bl_vs_byname", workspace_id=WS_PROD, sku_name="ss_VECTOR_SEARCH_SERVING",
        usage_date_=D(WIN_7), usage_quantity=4.0, billing_origin_product="VECTOR_SEARCH",
        endpoint_id=None, endpoint_name="ss-ep-byname",
    )

    # =============================================================================================
    # SEC G -- system.ai_gateway.usage for compute_ai_gateway_usage. status_code 3-way split
    # (2xx success / 429 rate-limited / other error), requester 4-branch mask, endpoint_name 2-branch
    # mask, all at D(3) (workspace acme-prod) so every scenario lands in the same (usage_date,
    # workspace_id) group and only endpoint_name/requester differentiate the output rows.
    # =============================================================================================
    # group a: endpoint 'ss-gw-ep1', requester 'ss_alice@example.com' (email mask) -- 3x success.
    gw_usage_row(
        con, req_suffix="a0", workspace_id=WS_PROD, endpoint_name="ss-gw-ep1",
        requester="ss_alice@example.com", event_time=DT(WIN_7, hour=1), status_code=200,
        latency_ms=100,
    )
    gw_usage_row(
        con, req_suffix="a1", workspace_id=WS_PROD, endpoint_name="ss-gw-ep1",
        requester="ss_alice@example.com", event_time=DT(WIN_7, hour=2), status_code=201,
        latency_ms=150,
    )
    gw_usage_row(
        con, req_suffix="a2", workspace_id=WS_PROD, endpoint_name="ss-gw-ep1",
        requester="ss_alice@example.com", event_time=DT(WIN_7, hour=3), status_code=299,
        latency_ms=300,
    )
    # group b: same endpoint, requester is a GUID (passthrough, unmasked) -- 2x rate-limited (429).
    gw_usage_row(
        con, req_suffix="b0", workspace_id=WS_PROD, endpoint_name="ss-gw-ep1",
        requester="11112222-3333-4444-5555-666677778888", event_time=DT(WIN_7, hour=4),
        status_code=429, latency_ms=50,
    )
    gw_usage_row(
        con, req_suffix="b1", workspace_id=WS_PROD, endpoint_name="ss-gw-ep1",
        requester="11112222-3333-4444-5555-666677778888", event_time=DT(WIN_7, hour=5),
        status_code=429, latency_ms=60,
    )
    # group c: same endpoint, requester 'zz_carter' (else-branch mask: no '@', not a GUID) -- 1x
    # error (500, neither 2xx nor 429).
    gw_usage_row(
        con, req_suffix="c0", workspace_id=WS_PROD, endpoint_name="ss-gw-ep1",
        requester="zz_carter", event_time=DT(WIN_7, hour=6), status_code=500, latency_ms=90,
    )
    # group d: same endpoint, requester NULL (passthrough) -- 1x success, proves the NULL branch.
    gw_usage_row(
        con, req_suffix="d0", workspace_id=WS_PROD, endpoint_name="ss-gw-ep1",
        requester=None, event_time=DT(WIN_7, hour=7), status_code=200, latency_ms=70,
    )
    # group e: endpoint_name NULL (passthrough), requester 'ss_dana@example.com' -- 1x success.
    gw_usage_row(
        con, req_suffix="e0", workspace_id=WS_PROD, endpoint_name=None,
        requester="ss_dana@example.com", event_time=DT(WIN_7, hour=8), status_code=200,
        latency_ms=80,
    )
    # window-boundary series: endpoint 'qq-gw-win-ep' (mask 'qq****') -- a 'qq' prefix used by NO
    # other endpoint_name value in this fixture, so it cannot mask-collide with group a/c's
    # 'ss-gw-ep1' (mask 'ss****') or any other row in the same (usage_date, workspace_id) group;
    # the GROUP BY is on the RAW endpoint_name/requester (DEC-48), so two DIFFERENT raw
    # endpoint_name values that mask to the SAME 2-char prefix in the SAME (day, workspace) group
    # would otherwise silently collapse this id's grain -- an earlier draft of this fixture used an
    # 'ss-gw-win-ep' endpoint here, which DID collide with group a/c's 'ss-gw-ep1' (both masking to
    # 'ss****'); caught by an in-memory duckdb sanity run against the real generated model SQL
    # before this file was handed off (see this task's Hand-off notes). requester 'qq_gw_win_req'
    # needs no such engineering: DEC-66.3's hash-derived id makes a same-group collision between
    # two different raw requester values vanishingly unlikely on its own. One row each at
    # D(3)/D(16)/D(68), plus one at D(0)
    # (today) that every query in this fixture must exclude (event_time < current_date()).
    gw_usage_row(
        con, req_suffix="win7", workspace_id=WS_PROD, endpoint_name="qq-gw-win-ep",
        requester="qq_gw_win_req", event_time=DT(WIN_7, hour=20), status_code=200,
    )
    gw_usage_row(
        con, req_suffix="win30", workspace_id=WS_PROD, endpoint_name="qq-gw-win-ep",
        requester="qq_gw_win_req", event_time=DT(WIN_30, hour=20), status_code=200,
    )
    gw_usage_row(
        con, req_suffix="win90", workspace_id=WS_PROD, endpoint_name="qq-gw-win-ep",
        requester="qq_gw_win_req", event_time=DT(WIN_90, hour=20), status_code=200,
    )
    gw_usage_row(
        con, req_suffix="wind0", workspace_id=WS_PROD, endpoint_name="qq-gw-win-ep",
        requester="qq_gw_win_req", event_time=DT(0, hour=12), status_code=200,
    )

    # =============================================================================================
    # SEC C -- po_clustering_activity (operation_type='CLUSTERING'; warn=50, crit=200 DBU).
    # =============================================================================================
    po_op(
        con, table_id="ss_tid_clu_ok", table_name="ss_tbl_clu_ok", operation_type="CLUSTERING",
        start_time=DT(WIN_7, hour=1), usage_quantity=30.0,
        operation_metrics={
            "number_of_removed_files": "10", "number_of_clustered_files": "8",
            "amount_of_data_removed_bytes": "100000000", "amount_of_clustered_data_bytes": "80000000",
        },
    )
    po_op(
        con, table_id="ss_tid_clu_warn", table_name="ss_tbl_clu_warn", operation_type="CLUSTERING",
        start_time=DT(WIN_7, hour=1), usage_quantity=80.0,
        operation_metrics={
            "number_of_removed_files": "40", "number_of_clustered_files": "30",
            "amount_of_data_removed_bytes": "400000000", "amount_of_clustered_data_bytes": "300000000",
        },
    )
    po_op(
        con, table_id="ss_tid_clu_crit", table_name="ss_tbl_clu_crit", operation_type="CLUSTERING",
        start_time=DT(WIN_7, hour=1), usage_quantity=250.0,
        operation_metrics={
            "number_of_removed_files": "900", "number_of_clustered_files": "700",
            "amount_of_data_removed_bytes": "9000000000", "amount_of_clustered_data_bytes": "7000000000",
        },
    )
    po_op(
        con, table_id="ss_tid_clu_na", table_name="ss_tbl_clu_na", operation_type="CLUSTERING",
        start_time=DT(WIN_7, hour=1), usage_quantity=None,
        operation_metrics={
            "number_of_removed_files": None, "number_of_clustered_files": None,
            "amount_of_data_removed_bytes": None, "amount_of_clustered_data_bytes": None,
        },
    )
    # window-boundary: 10 DBU at each of D(3)/D(16)/D(68) -> sums 10 (w7) / 20 (w30) / 30 (w90),
    # OK throughout (never crosses warn=50).
    for suffix, day in (("w7", WIN_7), ("w30", WIN_30), ("w90", WIN_90)):
        po_op(
            con, table_id="ss_tid_clu_win", table_name="ss_tbl_clu_win", operation_type="CLUSTERING",
            start_time=DT(day, hour=2), usage_quantity=10.0, op_suffix=suffix,
            operation_metrics={
                "number_of_removed_files": "5", "number_of_clustered_files": "4",
                "amount_of_data_removed_bytes": "50000000", "amount_of_clustered_data_bytes": "40000000",
            },
        )

    # =============================================================================================
    # SEC H -- po_clustering_column_churn (operation_type='AUTO_CLUSTERING_COLUMN_SELECTION';
    # warn=2, crit=5 'true' events per identical old->new signature).
    # =============================================================================================
    po_op(
        con, table_id="ss_tid_chu_ok", table_name="ss_tbl_chu_ok",
        operation_type="AUTO_CLUSTERING_COLUMN_SELECTION", start_time=DT(WIN_7, hour=1),
        usage_quantity=1.0,
        operation_metrics={
            "has_column_selection_changed": "false", "old_clustering_columns": "colA",
            "new_clustering_columns": "colA", "additional_reason": None,
        },
    )
    # 2 IDENTICAL-signature 'true' rows -> selection_event_count=2 -> WARN (>=2, <5).
    for i in range(2):
        po_op(
            con, table_id="ss_tid_chu_warn", table_name="ss_tbl_chu_warn",
            operation_type="AUTO_CLUSTERING_COLUMN_SELECTION", start_time=DT(WIN_7, hour=i + 1),
            usage_quantity=1.0, op_suffix=str(i),
            operation_metrics={
                "has_column_selection_changed": "true", "old_clustering_columns": "colA",
                "new_clustering_columns": "colB", "additional_reason": "recurring_write_shift",
            },
        )
    # 5 IDENTICAL-signature 'true' rows -> selection_event_count=5 -> CRITICAL (>=5).
    for i in range(5):
        po_op(
            con, table_id="ss_tid_chu_crit", table_name="ss_tbl_chu_crit",
            operation_type="AUTO_CLUSTERING_COLUMN_SELECTION", start_time=DT(WIN_7, hour=i + 1),
            usage_quantity=1.0, op_suffix=str(i),
            operation_metrics={
                "has_column_selection_changed": "true", "old_clustering_columns": "colC",
                "new_clustering_columns": "colD", "additional_reason": "recurring_write_shift",
            },
        )
    # has_column_selection_changed NULL -> NOT_ASSESSED (per this task's Inputs section, verbatim).
    po_op(
        con, table_id="ss_tid_chu_na", table_name="ss_tbl_chu_na",
        operation_type="AUTO_CLUSTERING_COLUMN_SELECTION", start_time=DT(WIN_7, hour=1),
        usage_quantity=1.0,
        operation_metrics={
            "has_column_selection_changed": None, "old_clustering_columns": None,
            "new_clustering_columns": None, "additional_reason": None,
        },
    )
    # window-boundary: 1 IDENTICAL-signature 'false' row at each of D(3)/D(16)/D(68) ->
    # selection_event_count = 1 (w7) / 2 (w30) / 3 (w90), OK throughout ('false' never bands).
    for suffix, day in (("w7", WIN_7), ("w30", WIN_30), ("w90", WIN_90)):
        po_op(
            con, table_id="ss_tid_chu_win", table_name="ss_tbl_chu_win",
            operation_type="AUTO_CLUSTERING_COLUMN_SELECTION", start_time=DT(day, hour=2),
            usage_quantity=1.0, op_suffix=suffix,
            operation_metrics={
                "has_column_selection_changed": "false", "old_clustering_columns": "colE",
                "new_clustering_columns": "colE", "additional_reason": None,
            },
        )

    # =============================================================================================
    # SEC D -- po_data_skipping_backfill (operation_type='DATA_SKIPPING_COLUMN_SELECTION';
    # warn=100GB, crit=500GB scanned with empty/NULL new_data_skipping_columns).
    # =============================================================================================
    po_op(
        con, table_id="ss_tid_skip_ok", table_name="ss_tbl_skip_ok",
        operation_type="DATA_SKIPPING_COLUMN_SELECTION", start_time=DT(WIN_7, hour=1),
        usage_quantity=1.0,
        operation_metrics={
            # new_data_skipping_columns='' (not NULL): a real event reporting no gain, not a
            # metrics-payload-carries-nothing NOT_ASSESSED case (P4-FIXES28 -- see ss_tid_skip_na
            # below for the true all-NULL shape).
            "added_data_skipping_columns": None, "removed_data_skipping_columns": None,
            "new_data_skipping_columns": "", "amount_of_scanned_bytes": "10000000000",
            "number_of_scanned_files": "5",
        },
    )  # 10 GB, no gain, under warn(100GB) -> OK
    po_op(
        con, table_id="ss_tid_skip_warn", table_name="ss_tbl_skip_warn",
        operation_type="DATA_SKIPPING_COLUMN_SELECTION", start_time=DT(WIN_7, hour=1),
        usage_quantity=1.0,
        operation_metrics={
            "added_data_skipping_columns": None, "removed_data_skipping_columns": None,
            "new_data_skipping_columns": "", "amount_of_scanned_bytes": "150000000000",
            "number_of_scanned_files": "80",
        },
    )  # 150 GB, empty-string new columns (still "no gain"), >=warn(100GB) <crit(500GB) -> WARN
    po_op(
        con, table_id="ss_tid_skip_crit", table_name="ss_tbl_skip_crit",
        operation_type="DATA_SKIPPING_COLUMN_SELECTION", start_time=DT(WIN_7, hour=1),
        usage_quantity=1.0,
        operation_metrics={
            # new_data_skipping_columns='' (not NULL) -- see ss_tid_skip_ok's own comment.
            "added_data_skipping_columns": None, "removed_data_skipping_columns": None,
            "new_data_skipping_columns": "", "amount_of_scanned_bytes": "600000000000",
            "number_of_scanned_files": "300",
        },
    )  # 600 GB, no gain, >=crit(500GB) -> CRITICAL
    po_op(
        con, table_id="ss_tid_skip_gain", table_name="ss_tbl_skip_gain",
        operation_type="DATA_SKIPPING_COLUMN_SELECTION", start_time=DT(WIN_7, hour=1),
        usage_quantity=1.0,
        operation_metrics={
            "added_data_skipping_columns": "col_x", "removed_data_skipping_columns": None,
            "new_data_skipping_columns": "col_x", "amount_of_scanned_bytes": "600000000000",
            "number_of_scanned_files": "300",
        },
    )  # 600 GB (CRITICAL-level scan) but new_data_skipping_columns IS populated -> excluded -> OK
    po_op(
        con, table_id="ss_tid_skip_na", table_name="ss_tbl_skip_na",
        operation_type="DATA_SKIPPING_COLUMN_SELECTION", start_time=DT(WIN_7, hour=1),
        usage_quantity=1.0,
        operation_metrics={
            "added_data_skipping_columns": None, "removed_data_skipping_columns": None,
            "new_data_skipping_columns": None,
            # amount_of_scanned_bytes key deliberately OMITTED -> bracket access NULL -> SUM NULL
            # -> NOT_ASSESSED.
            "number_of_scanned_files": None,
        },
    )
    # window-boundary: new_data_skipping_columns='col_w' (populated -> always excluded -> OK), 10 GB
    # at each of D(3)/D(16)/D(68) -> scanned_bytes sums 10GB (w7) / 20GB (w30) / 30GB (w90).
    for suffix, day in (("w7", WIN_7), ("w30", WIN_30), ("w90", WIN_90)):
        po_op(
            con, table_id="ss_tid_skip_win", table_name="ss_tbl_skip_win",
            operation_type="DATA_SKIPPING_COLUMN_SELECTION", start_time=DT(day, hour=2),
            usage_quantity=1.0, op_suffix=suffix,
            operation_metrics={
                "added_data_skipping_columns": "col_w", "removed_data_skipping_columns": None,
                "new_data_skipping_columns": "col_w", "amount_of_scanned_bytes": "10000000000",
                "number_of_scanned_files": "5",
            },
        )

    # =============================================================================================
    # SEC M -- po_maintenance_cost_by_table (NO operation_type filter; warn=20, crit=100 DBU;
    # operation_status LIKE 'FAILED%' forces CRITICAL regardless of DBU).
    # =============================================================================================
    po_op(
        con, table_id="ss_tid_maint_ok", table_name="ss_tbl_maint_ok", operation_type="CLUSTERING",
        operation_status="SUCCESSFUL", start_time=DT(WIN_7, hour=1), usage_quantity=5.0,
    )
    po_op(
        con, table_id="ss_tid_maint_warn", table_name="ss_tbl_maint_warn", operation_type="CLUSTERING",
        operation_status="SUCCESSFUL", start_time=DT(WIN_7, hour=1), usage_quantity=40.0,
    )
    po_op(
        con, table_id="ss_tid_maint_critdbu", table_name="ss_tbl_maint_critdbu",
        operation_type="CLUSTERING", operation_status="SUCCESSFUL", start_time=DT(WIN_7, hour=1),
        usage_quantity=150.0,
    )
    # FAILED override: usage_quantity=1.0 (would be OK by DBU alone) but operation_status LIKE
    # 'FAILED%' forces CRITICAL regardless -- the literal carries the documented embedded colon.
    po_op(
        con, table_id="ss_tid_maint_failed", table_name="ss_tbl_maint_failed", operation_type="VACUUM",
        operation_status="FAILED: INTERNAL_ERROR", start_time=DT(WIN_7, hour=1), usage_quantity=1.0,
    )
    po_op(
        con, table_id="ss_tid_maint_na", table_name="ss_tbl_maint_na", operation_type="CLUSTERING",
        operation_status="SUCCESSFUL", start_time=DT(WIN_7, hour=1), usage_quantity=None,
    )
    # window-boundary: 5 DBU at each of D(3)/D(16)/D(68) -> sums 5(w7)/10(w30)/15(w90), OK throughout.
    for suffix, day in (("w7", WIN_7), ("w30", WIN_30), ("w90", WIN_90)):
        po_op(
            con, table_id="ss_tid_maint_win", table_name="ss_tbl_maint_win",
            operation_type="MAINTENANCE", operation_status="SUCCESSFUL", start_time=DT(day, hour=2),
            usage_quantity=5.0, op_suffix=suffix,
        )

    # =============================================================================================
    # SEC V -- po_vacuum_reclaimed_bytes (operation_type='VACUUM' AND operation_status='SUCCESSFUL';
    # warn=5, crit=20 no-op DBU).
    # =============================================================================================
    po_op(
        con, table_id="ss_tid_vac_ok", table_name="ss_tbl_vac_ok", operation_type="VACUUM",
        operation_status="SUCCESSFUL", start_time=DT(WIN_7, hour=1), usage_quantity=2.0,
        operation_metrics={"number_of_deleted_files": "0", "amount_of_data_deleted_bytes": "0"},
    )
    po_op(
        con, table_id="ss_tid_vac_warn", table_name="ss_tbl_vac_warn", operation_type="VACUUM",
        operation_status="SUCCESSFUL", start_time=DT(WIN_7, hour=1), usage_quantity=8.0,
        operation_metrics={"number_of_deleted_files": "0", "amount_of_data_deleted_bytes": "0"},
    )
    po_op(
        con, table_id="ss_tid_vac_crit", table_name="ss_tbl_vac_crit", operation_type="VACUUM",
        operation_status="SUCCESSFUL", start_time=DT(WIN_7, hour=1), usage_quantity=30.0,
        operation_metrics={"number_of_deleted_files": "0", "amount_of_data_deleted_bytes": "0"},
    )
    # Real VACUUM that reclaimed bytes: usage_quantity=50.0 (CRITICAL-level DBU if it were a no-op)
    # but amount_of_data_deleted_bytes > 0 -> excluded from the band regardless -> OK.
    po_op(
        con, table_id="ss_tid_vac_real", table_name="ss_tbl_vac_real", operation_type="VACUUM",
        operation_status="SUCCESSFUL", start_time=DT(WIN_7, hour=1), usage_quantity=50.0,
        operation_metrics={"number_of_deleted_files": "500", "amount_of_data_deleted_bytes": "5000000000"},
    )
    po_op(
        con, table_id="ss_tid_vac_na", table_name="ss_tbl_vac_na", operation_type="VACUUM",
        operation_status="SUCCESSFUL", start_time=DT(WIN_7, hour=1), usage_quantity=None,
        operation_metrics={"number_of_deleted_files": "0", "amount_of_data_deleted_bytes": "0"},
    )
    # window-boundary: no-op VACUUM, 2 DBU at each of D(3)/D(16)/D(68) -> sums 2(w7)/4(w30)/6(w90).
    # OK at w=7/30 (2, 4 < warn=5); WARN at w=90 (6 >= warn=5) -- a deliberate secondary status
    # flip (verified with an in-memory duckdb sanity run against the real generated model SQL),
    # same shape as ss_se_idlewindow's CRITICAL->WARN flip in SEC S above. An earlier draft of this
    # comment claimed "OK throughout, never crosses warn=5", which was an arithmetic slip (2+2+2=6
    # >= 5) caught by that same sanity run before this file was handed off.
    for suffix, day in (("w7", WIN_7), ("w30", WIN_30), ("w90", WIN_90)):
        po_op(
            con, table_id="ss_tid_vac_win", table_name="ss_tbl_vac_win", operation_type="VACUUM",
            operation_status="SUCCESSFUL", start_time=DT(day, hour=2), usage_quantity=2.0,
            op_suffix=suffix,
            operation_metrics={"number_of_deleted_files": "0", "amount_of_data_deleted_bytes": "0"},
        )

    # Parquet is written by build_fixtures.py's own driver loop (one write_parquet call per touched
    # source, after this build(con) call returns) -- a builder never calls write_parquet itself
    # (see base.py's write_parquet docstring and billing.py's build(), which follows the same
    # contract; ports.py's build() docstring says the same).
