"""tests/fixtures/chargeback.py -- the one builder for P3-CHARGEBACK's cost_chargeback_by_allocation_tag
(domain cost). Follows tests/fixtures/billing.py's / tests/fixtures/lakeflow.py's builder shape
(module-level build(con), AS_OF from base.py, write_parquet re-exported for build_fixtures.py,
parameterized `?` INSERTs, one Python dict per STRUCT column) and fills only what this one query
reads: system.billing.usage and system.billing.list_prices.

Per build_fixtures.py's per-builder isolation (each builder runs against its OWN fresh in-memory
connection, seeded with all 47 sources from ddl.py) this module never reads another builder's
rows. Unlike most builders, it does NOT reuse the three shared fixture workspaces (1111 acme-prod,
2222 acme-dev, 3333 acme-uat, written by billing.py): system.billing.usage is read by many
existing queries with no query_id filter and no builder-prefix filter (whole-table SUMs), so new
rows under a shared workspace id could silently shift an already-pinned expected total elsewhere.

Review round (P3-CHARGEBACK): the first draft of this builder gave every scenario its OWN
workspace (six of them). dims.dim_workspace (dbt/models/dims/dim_workspace.sql) takes the union of
every workspace_id it finds in system.billing.usage / access.workspaces_latest / lakeflow -- so
six new billed workspaces meant six new entries in dim_workspace (unnamed, since none had an
access.workspaces_latest row, and "outside the snapshot's region" for every one, since none had a
row in any regional presence table either), which shifted several pinned test expectations outside
this builder's own reach (tests/test_api.py's ALL_WORKSPACE_IDS / test_workspaces_endpoint,
tests/test_scope_disclosure.py's fixture-presence and region-split assertions). Per the review,
this rewrite keeps to ONE workspace, CB_WS, and separates the OK/WARN/CRITICAL/precedence/price/
window scenarios by usage_unit instead (the query's own grain already isolates
workspace_id + usage_unit into independent groups, so a distinct usage_unit per scenario gives the
same test isolation a distinct workspace would, at a much smaller footprint against workspace-keyed
pinned lists). CB_WS is given a name via one access__workspaces_latest row (matching
tests/fixtures/drilldown.py's own precedent of writing directly to that shared table for its own
new workspaces) so dim_workspace.name is not NULL. It deliberately has no row in any regional
presence table, so it is the base fixture's one real "billed, outside the snapshot's region"
workspace (dims.dim_workspace in_snapshot_region FALSE, billed_in_snapshot TRUE). Gate-fix
decision (449da51): this shape is kept on purpose. It covers the dbt-built billed-outside path end
to end, and the alternatives are worse. Account-level rows (workspace_id NULL, finops.py's
pattern) would merge this builder's DBU group with finops.py's untagged account-level DBU rows. A
regional presence row would add a cb_ws1 row to regional findings that other tests pin.
tests/test_api.py (ALL_WORKSPACE_IDS) and tests/test_scope_disclosure.py (presence facts,
dim_workspace, region_split, /api/coverage outside, region_gap) include cb_ws1. The unfiltered test
db therefore shows the region banner naming cb-chargeback.

Every id this file writes (record_id, sku_name, workspace_id, usage_unit, tag values) carries the
`cb_` prefix, so nothing here can collide with any other builder's ids on the shared
billing__usage / billing__list_prices / access__workspaces_latest sources. (Two scenario rows
deliberately carry a non-cb_-prefixed tag VALUE -- env=prod and owner=cb_alice's "owner" KEY --
precisely to prove the missing-key bucket also catches "other tags present, just not the
allocation key"; see cb_r_a_missing / cb_r_c_missing below.)

------------------------------------------------------------------------------------------------
Scenario map (one usage_unit per scenario group, all inside the single workspace CB_WS; all
usage_date=D(1) unless noted; each usage_unit's own SKU is priced $1.00/unit unless noted, so a
row's dollar cost equals its usage_quantity except where noted):
------------------------------------------------------------------------------------------------
U_OK (cb_unit_ok)       cb_r_a_cc (cost_center=cb_finance, 80) + cb_r_a_missing (env=prod only, 10)
                        -> matched $80, missing $10 / $90 total = 11.1% missing -> status OK
U_WARN (cb_unit_warn)   cb_r_b_team (team=cb_growth, 70) + cb_r_b_missing (no tags at all, 30)
                        -> matched $70, missing $30 / $100 = 30.0% missing -> status WARN
U_CRIT (cb_unit_crit)   cb_r_c_cc (costcenter [no separator]=cb_ops, 50) + cb_r_c_missing
                        (owner=cb_alice only -- an unrelated tag, not cost_center/team, 50)
                        -> matched $50, missing $50 / $100 = 50.0% missing -> status CRITICAL
                        (exact :crit_missing_pct boundary, inclusive)
DBU (real unit, proves  cb_r_d_both (cost_center=cb_finance AND team=cb_other on the SAME row, 40)
usage_unit segregation  -> must pick cost_center (DEC-60 rule 4 precedence), never team; proves a
against GB below)       row with a real allocation match never lands in this group's missing
                        bucket. cb_r_d_teamonly (team=cb_solo only, 10) -> its own team-keyed
                        group. No missing-key row at all in this group -- proves the query never
                        emits a spurious is_missing_allocation_key row when none exists.
GB (real unit)          cb_r_d_gb_cc (cost_center=cb_ops, 5 -> $10 at $2.00/GB) + cb_r_d_gb_missing
                        (no tags, 5 -> $10) -> $10 matched / $20 total = 50% missing -> CRITICAL,
                        entirely separate from the DBU group above (proves usage_unit segregation:
                        DBU has no missing row/verdict, GB does).
U_PRICE                 cb_r_p_priced (cost_center=cb_finance, sku cb_SKU_PRICE_OK, 10 -> $10,
(cb_unit_price)         price_basis priced) + cb_r_p_unpriced (team=cb_ghost, sku cb_MYSTERY_SKU
                        with NO list_prices row at all, 6 -> price_basis unpriced,
                        net_list_cost_usd NULL -- this group HAS an allocation key, so its own
                        status is always OK regardless of price_basis) + cb_r_p_free (no tags --
                        missing bucket, sku cb_GENIE_FREE_USAGE with NO list_prices row, 8 ->
                        price_basis free, net_list_cost_usd NULL: SUM() of one NULL value). The
                        missing bucket's spend is entirely free-usage (unpriced_quantity=0 AND
                        priced_quantity=0), so per the review fix it reads a real $0: status OK,
                        share_of_unit_pct 0.0, not_assessed_reason None -- never NOT_ASSESSED and
                        never silently defaulted either (DEC-57/58 is about NOT_ASSESSED, not
                        about this free-is-really-$0 case, which is its own explicit branch).
U_PRICE_UNPRICED         cb_r_e_priced (cost_center=cb_finance, sku cb_SKU_PRICE2, 20 -> $20,
(cb_unit_price_unpriced) priced) + cb_r_e_missing_unpriced (no tags -- missing bucket, sku
                        cb_MYSTERY_SKU, NO list_prices row, 6 -> price_basis unpriced,
                        net_list_cost_usd NULL). Unlike U_PRICE above, the missing bucket's own
                        spend here is a genuine pricing-coverage gap (unpriced_quantity=6 > 0), not
                        free-usage, so it must still read NOT_ASSESSED
                        (missing_allocation_spend_unpriced) -- this is the case the review's fix
                        distinguishes from the free-is-OK case above; both scenarios exist
                        side by side so the SQL's free-vs-unpriced branch is exercised both ways.
U_WINDOW (cb_unit_window) cb_r_win_d5 (D(5), cost_center=cb_win, 5), cb_r_win_d20 (D(20), same key,
                        7), cb_r_win_d45 (D(45), same key, 11), cb_r_win_d0 (D0 -- today, same
                        key, 999, MUST be excluded from every window). One group (cost_center,
                        cb_win): totals 5 at window=7, 12 at window=30, 23 at window=90; 999 never
                        counted.
U_NULL_WS               cb_r_null_ws (workspace_id NULL -- account-level spend, cost_center=
(cb_unit_null_ws)       cb_finance, 15 -> $15, priced). Proves the unit_totals join is null-safe:
                        an INNER JOIN on a plain `=` never matches NULL to NULL, so a NULL-
                        workspace group used to vanish from this query's own output entirely, not
                        merely miscompute its share. This scenario's own usage_unit is unique to
                        it, so it cannot merge with any other builder's own NULL-workspace rows on
                        a different usage_unit (finops.py's own account-level pattern, e.g.).
------------------------------------------------------------------------------------------------
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import duckdb

from base import AS_OF, write_parquet  # noqa: F401 (write_parquet re-exported for build_fixtures.py)

D0 = AS_OF.date()  # 2026-09-21 -- "today" on the pinned test target


def D(n: int) -> date:
    return D0 - timedelta(days=n)


ACCOUNT_ID = "cb_acct"

# This builder's own single workspace (see the module docstring's review-round note for why this
# is one workspace, not six) -- given a name via one access__workspaces_latest row below.
CB_WS = "cb_ws1"

U_OK = "cb_unit_ok"
U_WARN = "cb_unit_warn"
U_CRIT = "cb_unit_crit"
U_PRICE = "cb_unit_price"
U_PRICE_UNPRICED = "cb_unit_price_unpriced"
U_WINDOW = "cb_unit_window"
U_NULL_WS = "cb_unit_null_ws"

SKU_OK = "cb_SKU_OK"                        # priced $1.00/unit, usage_unit U_OK
SKU_WARN = "cb_SKU_WARN"                    # priced $1.00/unit, usage_unit U_WARN
SKU_CRIT = "cb_SKU_CRIT"                    # priced $1.00/unit, usage_unit U_CRIT
SKU_PREC_A = "cb_SKU_PREC_A"                # priced $1.00/DBU
SKU_PREC_B = "cb_SKU_PREC_B"                # priced $2.00/GB
SKU_PRICE_OK = "cb_SKU_PRICE_OK"            # priced $1.00/unit, usage_unit U_PRICE
SKU_PRICE2 = "cb_SKU_PRICE2"                # priced $1.00/unit, usage_unit U_PRICE_UNPRICED
SKU_WINDOW = "cb_SKU_WINDOW"                # priced $1.00/unit, usage_unit U_WINDOW
SKU_NULL_WS = "cb_SKU_NULL_WS"              # priced $1.00/unit, usage_unit U_NULL_WS
SKU_UNPRICED = "cb_MYSTERY_SKU"             # deliberately no list_prices row at all (a real gap),
                                             # reused under U_PRICE and U_PRICE_UNPRICED
SKU_FREE = "cb_GENIE_FREE_USAGE"            # deliberately no list_prices row (name matches
                                             # '%FREE_USAGE%' -- a real $0, not a gap)


# ---------------------------------------------------------------------------------------------
# STRUCT defaults -- every field tests/fixtures/ddl.py's DDL gives usage_metadata / identity_metadata
# / product_features, defaulted to None/False so no STRUCT field is ever silently omitted from the
# INSERT (duckdb 1.5.1 binds a dict to a STRUCT by field NAME). This query reads none of these
# fields directly, so every row shares the same all-empty defaults.
# ---------------------------------------------------------------------------------------------
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
    sku_name: str,
    usage_date_: date,
    usage_quantity: float,
    custom_tags: dict | None = None,
    usage_unit: str,
    workspace_id: str = CB_WS,
    billing_origin_product: str = "ALL_PURPOSE",
    hour: int = 0,
) -> None:
    start = datetime.combine(usage_date_, datetime.min.time()) + timedelta(hours=hour)
    end = start + timedelta(hours=1)
    con.execute(
        _USAGE_SQL,
        [
            ACCOUNT_ID, workspace_id, record_id, sku_name, "aws", start, end, usage_date_,
            custom_tags if custom_tags is not None else {},
            usage_unit, usage_quantity,
            _usage_metadata(), _identity_metadata(), "ORIGINAL", usage_date_, billing_origin_product,
            _product_features(), "COMPUTE_TIME",
        ],
    )


_LP_SQL = (
    "INSERT INTO billing__list_prices (account_id, price_start_time, price_end_time, sku_name, "
    "cloud, currency_code, usage_unit, pricing) VALUES (?,?,?,?,?,?,?,?)"
)


def list_price(con: duckdb.DuckDBPyConnection, sku_name: str, usage_unit: str, effective_list_default: float) -> None:
    """A single CURRENT price row (price_end_time NULL, price_start_time well before every usage
    row in this fixture -- D(45) is the oldest) -- this builder does not need an expired-price
    scenario (already proven elsewhere), only a matching current one."""
    con.execute(
        _LP_SQL,
        [
            ACCOUNT_ID, AS_OF - timedelta(days=300), None, sku_name, "aws", "USD", usage_unit,
            {"default": effective_list_default, "promotional": {"default": None},
             "effective_list": {"default": effective_list_default}},
        ],
    )


_WS_SQL = (
    "INSERT INTO access__workspaces_latest (workspace_id, workspace_name, workspace_url) "
    "VALUES (?,?,?)"
)


# ---------------------------------------------------------------------------------------------
# Auto-discovered by build_fixtures.py (DEC-17): a module-level build(con), no registration list.
# ---------------------------------------------------------------------------------------------
def build(con: duckdb.DuckDBPyConnection) -> None:
    # ---- name CB_WS (drilldown.py's own precedent for writing directly to this shared table) --
    con.execute(_WS_SQL, [CB_WS, "cb-chargeback", "https://dbc-cb.cloud.databricks.com/"])

    # ---- list_prices: every real SKU below is priced; SKU_UNPRICED and SKU_FREE deliberately not
    list_price(con, SKU_OK, U_OK, 1.00)
    list_price(con, SKU_WARN, U_WARN, 1.00)
    list_price(con, SKU_CRIT, U_CRIT, 1.00)
    list_price(con, SKU_PREC_A, "DBU", 1.00)
    list_price(con, SKU_PREC_B, "GB", 2.00)
    list_price(con, SKU_PRICE_OK, U_PRICE, 1.00)
    list_price(con, SKU_PRICE2, U_PRICE_UNPRICED, 1.00)
    list_price(con, SKU_WINDOW, U_WINDOW, 1.00)
    list_price(con, SKU_NULL_WS, U_NULL_WS, 1.00)

    # =========================================================================================
    # U_OK -- missing share 10/90 = 11.1% -> OK (< :warn_missing_pct default 20)
    # =========================================================================================
    usage(con, "cb_r_a_cc", sku_name=SKU_OK, usage_unit=U_OK, usage_date_=D(1),
          usage_quantity=80.0, custom_tags={"cost_center": "cb_finance"})
    usage(con, "cb_r_a_missing", sku_name=SKU_OK, usage_unit=U_OK, usage_date_=D(1),
          usage_quantity=10.0, custom_tags={"env": "prod"})  # tagged, but not the allocation key

    # =========================================================================================
    # U_WARN -- missing share 30/100 = 30.0% -> WARN (>= 20, < :crit_missing_pct default 50)
    # =========================================================================================
    usage(con, "cb_r_b_team", sku_name=SKU_WARN, usage_unit=U_WARN, usage_date_=D(1),
          usage_quantity=70.0, custom_tags={"team": "cb_growth"})
    usage(con, "cb_r_b_missing", sku_name=SKU_WARN, usage_unit=U_WARN, usage_date_=D(1),
          usage_quantity=30.0, custom_tags={})  # no tags at all

    # =========================================================================================
    # U_CRIT -- missing share 50/100 = 50.0% -> CRITICAL (exact boundary, inclusive)
    # =========================================================================================
    usage(con, "cb_r_c_cc", sku_name=SKU_CRIT, usage_unit=U_CRIT, usage_date_=D(1),
          usage_quantity=50.0, custom_tags={"costcenter": "cb_ops"})  # no-separator spelling
    usage(con, "cb_r_c_missing", sku_name=SKU_CRIT, usage_unit=U_CRIT, usage_date_=D(1),
          usage_quantity=50.0, custom_tags={"owner": "cb_alice"})  # unrelated tag only

    # =========================================================================================
    # DBU / GB -- cost_center outranks team on one row; a team-only row keeps its own group; GB
    # proves usage_unit segregation within this one workspace.
    # =========================================================================================
    usage(con, "cb_r_d_both", sku_name=SKU_PREC_A, usage_unit="DBU", usage_date_=D(1),
          usage_quantity=40.0, custom_tags={"cost_center": "cb_finance", "team": "cb_other"})
    usage(con, "cb_r_d_teamonly", sku_name=SKU_PREC_A, usage_unit="DBU", usage_date_=D(1),
          usage_quantity=10.0, custom_tags={"team": "cb_solo"})
    usage(con, "cb_r_d_gb_cc", sku_name=SKU_PREC_B, usage_unit="GB", usage_date_=D(1),
          usage_quantity=5.0, custom_tags={"cost_center": "cb_ops"}, billing_origin_product="STORAGE")
    usage(con, "cb_r_d_gb_missing", sku_name=SKU_PREC_B, usage_unit="GB", usage_date_=D(1),
          usage_quantity=5.0, custom_tags={}, billing_origin_product="STORAGE")

    # =========================================================================================
    # U_PRICE -- price_basis priced/unpriced/free; the missing bucket's own spend is entirely
    # free-usage -> a real $0, status OK (review fix), never NOT_ASSESSED.
    # =========================================================================================
    usage(con, "cb_r_p_priced", sku_name=SKU_PRICE_OK, usage_unit=U_PRICE, usage_date_=D(1),
          usage_quantity=10.0, custom_tags={"cost_center": "cb_finance"})
    usage(con, "cb_r_p_unpriced", sku_name=SKU_UNPRICED, usage_unit=U_PRICE, usage_date_=D(1),
          usage_quantity=6.0, custom_tags={"team": "cb_ghost"})
    usage(con, "cb_r_p_free", sku_name=SKU_FREE, usage_unit=U_PRICE, usage_date_=D(1),
          usage_quantity=8.0, custom_tags={})  # missing bucket, FREE_USAGE-named, no price row

    # =========================================================================================
    # U_PRICE_UNPRICED -- the missing bucket's own spend is a genuine pricing-coverage gap (an
    # unpriced, non-free SKU) -> NOT_ASSESSED (missing_allocation_spend_unpriced), unlike U_PRICE.
    # =========================================================================================
    usage(con, "cb_r_e_priced", sku_name=SKU_PRICE2, usage_unit=U_PRICE_UNPRICED, usage_date_=D(1),
          usage_quantity=20.0, custom_tags={"cost_center": "cb_finance"})
    usage(con, "cb_r_e_missing_unpriced", sku_name=SKU_UNPRICED, usage_unit=U_PRICE_UNPRICED,
          usage_date_=D(1), usage_quantity=6.0, custom_tags={})

    # =========================================================================================
    # U_WINDOW -- 7/30/90-day window sums + D0 (today) exclusion, one (cost_center, cb_win)
    # group: 5 @ w=7, 12 @ w=30, 23 @ w=90; the D0 row (999) never counted at any window.
    # =========================================================================================
    usage(con, "cb_r_win_d5", sku_name=SKU_WINDOW, usage_unit=U_WINDOW, usage_date_=D(5),
          usage_quantity=5.0, custom_tags={"cost_center": "cb_win"})
    usage(con, "cb_r_win_d20", sku_name=SKU_WINDOW, usage_unit=U_WINDOW, usage_date_=D(20),
          usage_quantity=7.0, custom_tags={"cost_center": "cb_win"})
    usage(con, "cb_r_win_d45", sku_name=SKU_WINDOW, usage_unit=U_WINDOW, usage_date_=D(45),
          usage_quantity=11.0, custom_tags={"cost_center": "cb_win"})
    usage(con, "cb_r_win_d0", sku_name=SKU_WINDOW, usage_unit=U_WINDOW, usage_date_=D0,
          usage_quantity=999.0, custom_tags={"cost_center": "cb_win"})

    # =========================================================================================
    # U_NULL_WS -- account-level spend (workspace_id NULL) must still reach the unit_totals join.
    # =========================================================================================
    usage(con, "cb_r_null_ws", sku_name=SKU_NULL_WS, usage_unit=U_NULL_WS, usage_date_=D(1),
          usage_quantity=15.0, custom_tags={"cost_center": "cb_finance"}, workspace_id=None)
