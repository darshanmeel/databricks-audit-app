"""tests/fixtures/finops.py -- P3-FINOPS.

Fills system.billing.usage / system.billing.list_prices for the three app-owned cost queries this
item adds: cost_period_over_period (grain [workspace_id, billing_origin_product], windowed),
cost_monthly_actuals (grain [workspace_id, billing_origin_product, month_start], windowless),
cost_daily_spikes (grain [workspace_id, billing_origin_product, usage_date], windowed on
:period_days for the scanned days only -- :baseline_days is a fixed 14-day lookback regardless of
window); and, added later (same file, same account-level convention), cost_sku_trend_12m (grain
[sku_name, month_start], windowless, always the last 12 full calendar months plus the current
partial one) - see `_build_sku_trend_12m` and its own scenario table below. That section reaches
back to Sept 2025 (further than any other scenario here), which is why it registers its own list
price via `_trend_list_price` rather than the shared `list_price()` helper (fixed to a 250-day
start, too short for a 12-month trend).

Own id namespace, account-level usage (DEC-15 amendment pattern -- see tests/fixtures/
pressure_jobs.py's / tests/fixtures/drilldown.py's own 9001/9002 pool): workspace_id is always
NULL, never a dedicated workspace id and never the shared 1111/2222/3333 pool.

Review round 2 (P3-FINOPS review fixes, must_fix 9): this module originally wrote every row to a
dedicated workspace "fo_ws1". That workspace joins dims.dim_workspace (ids_usage) with a NULL name
and in_snapshot_region FALSE (billed but outside the region, an accidental side effect never
intended by this builder), which broke five existing tests that enumerate the whole workspace
universe: tests/test_scope_disclosure.py's test_fixture_presence_facts (`_distinct_ids
('billing__usage') == {'1111','2222','3333'}`), test_dim_workspace_marks_region_presence_and_spend,
test_region_split_on_the_fixture_db, and tests/test_api.py's test_workspaces_endpoint and
test_all_workspaces_selected_by_default -- files this item's lane is not permitted to edit. Account-
level usage (workspace_id NULL) is the preferred fix the review identified: dim_workspace,
int_workspace_tag_hints and test_scope_disclosure.py's own _distinct_ids() helper all filter
`WHERE workspace_id IS NOT NULL`, so a NULL-workspace row is invisible to every one of those five
tests, exactly like the app's own existing account-level billing pattern (PLAN.md 5.7,
`GLOBAL_WS_QUERY_ID` in test_scope_disclosure.py) already is. It also exercises the null-safe
(`IS NOT DISTINCT FROM`) joins cost_daily_spikes.sql's own review fix added, so account-level usage
gets a real baseline/drivers match instead of always reading no_baseline_history.

billing.py's own builder writes to the shared 1111/2222/3333 pool, and several existing tests
(tests/test_findings/test_overview_spend_estimate.py, test_cost_priced.py) read that shared pool's
billing.usage rows workspace-wide, with no product/sku filter, to compute expected totals -- this
module's own account-level rows never touch that pool, so they cannot shift those totals (and the
few tests that DO scan every workspace_id in billing.usage, e.g. test_overview_spend_estimate.py,
derive their own expectations fresh from the raw parquet each run, so a new NULL group lands
identically on both the expected and actual sides). This builder's own three test files read these
rows by filtering `workspace_id is None and billing_origin_product.startswith("fo_...")` in Python,
never by a workspace_ids= filter (dbutil.rows() has no way to ask for `IS NULL`; passing `[None]`
would silently match nothing, since `x IN (NULL)` is never true in SQL). Every id this module
writes -- account_id, record_id, sku_name and billing_origin_product (the queries' own row
identity, standing in for a job_id/pipeline_id in other builders) -- carries the `fo_` prefix.

All dates are relative to base.AS_OF = 2026-09-21 12:00:00 (`D0`); `D(n)` = D0 minus n days, except
_build_monthly_actuals's own calendar-month rows, which use explicit `date(2026, M, D)` literals
(month grouping is easier to read that way than a day-offset).

price_start_time on every registered SKU is AS_OF - 250 days (list_price() below), current
(price_end_time NULL) -- every usage row in this file lands within that window; the one deliberate
exception is each scenario's own "ancient anchor" row, D(250), placed exactly one day inside that
250-day price validity window so it still resolves the same way if ever priced, though none of the
three queries' own current/previous/candidate date filters actually reach that far back at any
tested window (max lookback used is 2*90 = 180 days for cost_period_over_period's previous window
at window_days=90) -- the anchor exists only to give system.billing.usage.MIN(usage_date) a row
old enough to satisfy the DEC-64 coverage check (cost_period_over_period's `earliest` CTE, no date
filter of its own) without ever contributing a dollar to any current/previous/candidate sum.

------------------------------------------------------------------------------------------------
Scenario -> billing_origin_product map (tests/test_findings/test_cost_*.py read this table)
------------------------------------------------------------------------------------------------
cost_period_over_period (all at usage_date D(3)=current / D(10)=previous unless noted; w=7 primary)
  fo_ppp_warn        current=130, previous=100 (+30.0%)          -> WARN
  fo_ppp_crit        current=200, previous=100 (+100.0%)         -> CRITICAL (pct)
  fo_ppp_ok_floor     current=40 (<50 floor), previous=5          -> OK (floor overrides the pct)
  fo_ppp_crit_new     current=80, previous=0 (no prior-window row) -> CRITICAL (brand-new spend)
  fo_ppp_crit_new_short_history  usage only at D(2)=100, no anchor  -> CRITICAL (brand-new spend):
                                    this product's OWN earliest usage is only 2 days old, but
                                    DEC-64 coverage is now account-wide (review fix), not per
                                    product -- every OTHER scenario's D(250)/D(180) row already
                                    covers the account as a whole, so this still reads a real
                                    verdict, never NOT_ASSESSED just for lacking its own history.
  fo_ppp_unpriced     current=60 priced + 15 qty unpriced SKU      -> NOT_ASSESSED
                                    (current_period_unpriced), price_basis='unpriced': any unpriced
                                    usage on a side withholds a verdict on it, even a side that is
                                    mostly priced.
  fo_ppp_unpriced_current  current=40 qty, ENTIRELY unpriced SKU; previous=100 priced
                                    -> NOT_ASSESSED (current_period_unpriced): the current side has
                                    no dollar figure at all, priced or a real free $0, to judge.
  fo_ppp_unpriced_previous  current=100 priced; previous=40 qty, ENTIRELY unpriced SKU
                                    -> NOT_ASSESSED (previous_period_unpriced): same gap, the other side.
  fo_ppp_free         current/previous both FREE_USAGE-named only  -> price_basis='free'; OK at
                                    every window, and est_current_usd_list/est_previous_usd_list
                                    are both a real 0.0 at every window (P4-47: a side with no
                                    matching rows, or whose rows are all FREE_USAGE, resolves to a
                                    genuine $0, never NULL -- only a side that is genuinely
                                    unpriced, like fo_ppp_unpriced_current/_previous above, stays
                                    NULL and reads NOT_ASSESSED).
  fo_ppp_correction    ORIGINAL 100 + RETRACTION -20 = net 80      -> OK, proves corrections net out
  fo_ppp_daily        uniform $10/day, D(1)..D(180)                -> OK at every window (7/30/90);
                                                                        proves current/previous both
                                                                        scale with window (70/300/900)
cost_monthly_actuals (windowless, but usage_date < current_date() same as every other cost query;
                       month_start is the first of the calendar month)
  fo_ma_cur    Sept 2026: D(0) EXCLUDED (today, in-flight) + D(1)+D(3)+D(5) counted, net=35.00
                                                                    -> is_partial_month=TRUE
                                                                        (partial_reason='month_in_progress')
  fo_ma_past   Aug 2026: 3 distinct days, net=50.00                -> is_partial_month=FALSE
  fo_ma_unpriced  Aug 2026: 1 priced (8.00) + 1 unpriced day        -> price_basis='unpriced'
  fo_ma_free   Aug 2026: 1 FREE_USAGE-only day                     -> price_basis='free', net NULL
  fo_ma_multi  July 2026 (net=5.00) AND Aug 2026 (net=9.00)         -> two month rows, one product
cost_daily_spikes (baseline_days=14 fixed; candidate day D(1) unless noted; w=7 primary)
  fo_ds_warn        baseline 14x$20/day (D2..D15), candidate $50 (3 SKUs) -> WARN, ratio=2.5. At
                       w=30/90, D(15) (one of its own baseline days) also becomes its own scanned
                       candidate day, but its $20 never clears :min_spend_usd=50 -> no second row
                       (the $ floor is checked before the no-baseline case -- review fix).
  fo_ds_notassessed  candidate D(3)=$100, zero baseline history          -> NOT_ASSESSED
  fo_ds_nospike     baseline 14x$20/day, candidate $25 (ratio 1.25, also below the $ floor) -> absent
  fo_ds_floor       baseline 14x$1/day, candidate $10 (ratio 10x)         -> absent (below $ floor)
  fo_ds_unpriced    baseline 14x$20/day, candidate $60 priced+unpriced    -> WARN, price_basis='unpriced'
  fo_ds_free_na     candidate D(1) FREE_USAGE-only ($0), zero baseline    -> absent (a real $0 day
                       never clears :min_spend_usd, checked before the no-baseline case)
  fo_ds_shortbaseline  ONE baseline day (D(2)=$5), candidate $50 (ratio 10x, clears both the floor
                       and :spike_ratio) -> NOT_ASSESSED (short_baseline), never WARN: a median over
                       fewer than :min_baseline_days=3 days is too thin to band a real verdict on,
                       even though the raw ratio alone would have cleared the bar.
------------------------------------------------------------------------------------------------
cost_sku_trend_12m (windowless, always the last 12 full calendar months -- Sept 2025 through Aug
2026 -- plus the current partial month, Sept 2026; one $1.00/DBU row per calendar month so
usage_quantity IS the dollar amount, one sku_name per scenario so GROUP BY sku_name never mixes two
scenarios together)
  fo_trend_double  1000/mo first 3 (Sep-Nov'25) and prior 3 (Mar-May'26), 2000/mo last 3
                     (Jun-Aug'26) -> growth_3m_pct=growth_12m_pct=100.0% -> CRITICAL. Also carries
                     one Sept 2026 (current, partial-month) row of 9999 -- proves is_partial_month
                     and that the partial month never enters any 3-month average.
  fo_trend_warn60  500/mo first 3 and prior 3, 800/mo last 3 -> growth_3m_pct=growth_12m_pct=
                     +60.0% -> WARN (>=50%, <100%)
  fo_trend_new_vs  0 in first 3 AND prior 3 (no rows at all before Jun'26), 700/mo last 3 ->
                     usd_prior_3m_avg=usd_first_3m_avg=0 -> growth_3m_pct/growth_12m_pct NULL
                     (divide-by-zero avoided) -> new_this_year=TRUE, usd_last_3m_avg(700) >=
                     :new_sku_min_usd(500) -> WARN
  fo_trend_flat    600/mo in all three buckets -> growth_3m_pct=growth_12m_pct=0.0% -> OK (proves
                     a $ amount above every floor still reads OK when growth is flat)
------------------------------------------------------------------------------------------------

CROSS-QUERY BLEED-THROUGH (expected, not a bug): all three queries read the whole billing.usage
table with no billing_origin_product filter, so every scenario's rows also surface as their own
(correctly computed) row in the OTHER two queries' output -- e.g. fo_ds_warn's candidate-day usage
also becomes a real cost_period_over_period row for that product, and every scenario's D(250)
anchor also becomes its own cost_monthly_actuals January row (windowless -- the only date filter
is the same `usage_date < current_date()` cut-off every other cost query uses, and D(250) is
comfortably inside it). None of this is wrong data; each test file below asserts only on the product names its own
query's scenario table lists above, and the tests are unaffected by the extra rows another query's
scenarios coincidentally add to a query they were not designed to test. The same holds in reverse
for cost_sku_trend_12m's own four fo_trend_* SKUs above: because that query has no product/sku
filter of its own either, EVERY other scenario in this file (and every other builder's own
billing.usage rows) that falls inside its 12-month-plus-partial window becomes its own extra
(correctly computed) row there too -- test_cost_sku_trend_12m.py filters to sku_name.startswith
("fo_trend_") and never asserts on, or needs, the account-wide total those extra rows contribute to
(share_of_total_last_3m is checked only by relative order among the four fo_trend_* SKUs, never by
its absolute value, for exactly this reason).

Stdlib + duckdb only.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import duckdb

from base import AS_OF, write_parquet  # noqa: F401 (write_parquet re-exported for build_fixtures.py)

D0 = AS_OF.date()  # 2026-09-21


def D(n: int) -> date:
    return D0 - timedelta(days=n)


ACCOUNT_ID = "fo_acct"
WS = None  # account-level usage (never a dedicated workspace -- review fix, see module docstring).

# ---------------------------------------------------------------------------------------------
# STRUCT defaults -- mirrors tests/fixtures/billing.py's own field lists exactly (same DDL, same
# duckdb dict-binds-by-field-name requirement); none of the three queries under test reference
# usage_metadata / identity_metadata / product_features at all, so every field here stays NULL.
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
# Insert helpers -- one row per call, matching tests/fixtures/billing.py's own INSERT shape.
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
    return f"fo_u_{_seq[0]:04d}"


def usage(
    con: duckdb.DuckDBPyConnection,
    *,
    sku_name: str,
    usage_date_: date,
    usage_quantity: float,
    billing_origin_product: str,
    record_type: str = "ORIGINAL",
    hour: int = 0,
    cloud: str = "aws",
) -> None:
    """One system.billing.usage row, workspace_id = WS (None -- account-level, see module
    docstring), usage_unit fixed to 'DBU' (matching list_price()'s own registered usage_unit --
    the three queries under test join on usage_unit too, unlike lakeflow_job_run_cost's
    DBU-filtered/no-usage_unit-join shape)."""
    start = datetime.combine(usage_date_, datetime.min.time()) + timedelta(hours=hour)
    end = start + timedelta(hours=1)
    con.execute(
        _USAGE_SQL,
        [
            ACCOUNT_ID, WS, _next_record_id(), sku_name, cloud, start, end, usage_date_, {},
            "DBU", usage_quantity, _usage_metadata(), _identity_metadata(), record_type,
            usage_date_, billing_origin_product, _product_features(), "COMPUTE_TIME",
        ],
    )


_LP_SQL = (
    "INSERT INTO billing__list_prices (account_id, price_start_time, price_end_time, sku_name, "
    "cloud, currency_code, usage_unit, pricing) VALUES (?,?,?,?,?,?,?,?)"
)


def list_price(con: duckdb.DuckDBPyConnection, sku_name: str, effective_list_default: float) -> None:
    """One CURRENT (price_end_time NULL) system.billing.list_prices row, price_start_time =
    AS_OF - 250 days -- inside every usage row's usage_end_time in this file, including each
    scenario's own D(250) ancient anchor. pricing.default is set equal to effective_list_default
    (never read by any of the three queries under test, which all read
    pricing.effective_list.default only, per DEC-66.1)."""
    con.execute(
        _LP_SQL,
        [
            ACCOUNT_ID, AS_OF - timedelta(days=250), None, sku_name, "aws", "USD", "DBU",
            {
                "default": effective_list_default,
                "promotional": {"default": None},
                "effective_list": {"default": effective_list_default},
            },
        ],
    )


def _trend_list_price(con: duckdb.DuckDBPyConnection, sku_name: str, rate: float) -> None:
    """One CURRENT list_prices row starting 400 days before AS_OF -- comfortably before
    cost_sku_trend_12m's own 12-months-plus-partial window (~385 days back at AS_OF), which the
    shared `list_price()` helper's fixed 250-day start does not reach (its oldest months would
    otherwise join to no price row at all and read price_basis='unpriced', not 'priced')."""
    con.execute(
        _LP_SQL,
        [
            ACCOUNT_ID, AS_OF - timedelta(days=400), None, sku_name, "aws", "USD", "DBU",
            {"default": rate, "promotional": {"default": None}, "effective_list": {"default": rate}},
        ],
    )


def _anchor(con: duckdb.DuckDBPyConnection, product: str) -> None:
    """A tiny priced row at D(250), far outside the current/previous window at every tested
    window (max lookback = 2*90 = 180 days), so it only ever affects the unrestricted `snapshot`
    CTE. Review fix (must_fix 2): DEC-64 coverage moved from a per-row `earliest` CTE to one
    account-wide `snapshot` CTE, so this row (like every other scenario's own D(250)/D(180) row)
    now contributes to the ONE shared coverage signal every cost_period_over_period row reads,
    not just this call's own `product` -- it is kept, per scenario, for readability (each block
    stays self-contained) even though, post-fix, a single such row anywhere would cover every
    scenario. hour=13 (not the default 0) so usage_end_time (14:00 on D(250)) lands after
    list_price()'s own price_start_time (AS_OF - 250 days = D(250) 12:00:00) on the same
    calendar day -- otherwise this row would itself read price_basis='unpriced' (harmless for
    the two windowed queries, which never see it at all, but it also becomes its own real row
    in the windowless cost_monthly_actuals output -- see that query's own scenario table above,
    which asserts nothing about these anchor rows either way, but a cleanly-priced anchor is
    less confusing for a future reader than a spuriously "unpriced" one)."""
    usage(con, sku_name="fo_SKU_A", usage_date_=D(250), usage_quantity=1.0,
          billing_origin_product=product, hour=13)


# =================================================================================================
# cost_period_over_period (w=7 primary: current=[D7..D1], previous=[D14..D8])
# =================================================================================================
def _build_period_over_period(con: duckdb.DuckDBPyConnection) -> None:
    # fo_ppp_warn: current=130, previous=100 -> +30.0% -> WARN (>=25%, <50%)
    _anchor(con, "fo_ppp_warn")
    usage(con, sku_name="fo_SKU_A", usage_date_=D(3), usage_quantity=130.0, billing_origin_product="fo_ppp_warn")
    usage(con, sku_name="fo_SKU_A", usage_date_=D(10), usage_quantity=100.0, billing_origin_product="fo_ppp_warn")

    # fo_ppp_crit: current=200, previous=100 -> +100.0% -> CRITICAL (>=50%)
    _anchor(con, "fo_ppp_crit")
    usage(con, sku_name="fo_SKU_A", usage_date_=D(3), usage_quantity=200.0, billing_origin_product="fo_ppp_crit")
    usage(con, sku_name="fo_SKU_A", usage_date_=D(10), usage_quantity=100.0, billing_origin_product="fo_ppp_crit")

    # fo_ppp_ok_floor: current=40 (< :min_spend_usd=50) -> OK regardless of the 700% swing
    _anchor(con, "fo_ppp_ok_floor")
    usage(con, sku_name="fo_SKU_A", usage_date_=D(3), usage_quantity=40.0, billing_origin_product="fo_ppp_ok_floor")
    usage(con, sku_name="fo_SKU_A", usage_date_=D(10), usage_quantity=5.0, billing_origin_product="fo_ppp_ok_floor")

    # fo_ppp_crit_new: current=80, previous window has NO usage at all (previous_cost=0) but the
    # anchor covers earliest_usage_date -> CRITICAL via the "brand-new spend" branch, change_pct NULL.
    _anchor(con, "fo_ppp_crit_new")
    usage(con, sku_name="fo_SKU_A", usage_date_=D(3), usage_quantity=80.0, billing_origin_product="fo_ppp_crit_new")

    # fo_ppp_crit_new_short_history: deliberately NO anchor of its own -- only usage at D(2). Under
    # the OLD per-row coverage check this read NOT_ASSESSED (its own earliest usage did not reach
    # back to D(14), w=7's previous-window start). Review fix (must_fix 2): coverage is now
    # account-wide, and every OTHER scenario's own anchor already covers the account as a whole,
    # so this still gets a real verdict -- CRITICAL via the brand-new-spend branch (previous_cost=0,
    # change_pct NULL), never NOT_ASSESSED just because this one product personally lacks history.
    usage(con, sku_name="fo_SKU_A", usage_date_=D(2), usage_quantity=100.0,
          billing_origin_product="fo_ppp_crit_new_short_history")

    # fo_ppp_unpriced: current = 60 priced + 15 qty on an unpriced SKU; previous = 60 priced.
    # NOT_ASSESSED (current_period_unpriced), price_basis='unpriced': the current side has SOME
    # unpriced usage, so its total understates the true figure and no verdict is judged on it, even
    # though most of the side did price.
    _anchor(con, "fo_ppp_unpriced")
    usage(con, sku_name="fo_SKU_A", usage_date_=D(3), usage_quantity=60.0, billing_origin_product="fo_ppp_unpriced")
    usage(con, sku_name="fo_PPP_UNPRICED", usage_date_=D(3), usage_quantity=15.0, billing_origin_product="fo_ppp_unpriced")
    usage(con, sku_name="fo_SKU_A", usage_date_=D(10), usage_quantity=60.0, billing_origin_product="fo_ppp_unpriced")

    # P4-47: fo_ppp_unpriced_current -- the current side has NO priced usage at all (every SKU in
    # it is unpriced and none is FREE_USAGE), so current_cost's own SUM is genuinely NULL, never a
    # real $0 -> NOT_ASSESSED (current_period_unpriced), even though the previous side is a real,
    # fully-priced 100.
    _anchor(con, "fo_ppp_unpriced_current")
    usage(con, sku_name="fo_PPP_UNPRICED_CUR", usage_date_=D(3), usage_quantity=40.0, billing_origin_product="fo_ppp_unpriced_current")
    usage(con, sku_name="fo_SKU_A", usage_date_=D(10), usage_quantity=100.0, billing_origin_product="fo_ppp_unpriced_current")

    # P4-47: fo_ppp_unpriced_previous -- the mirror image: the PREVIOUS side is entirely unpriced
    # -> NOT_ASSESSED (previous_period_unpriced), proving the gap is caught on either side, not
    # only the current one.
    _anchor(con, "fo_ppp_unpriced_previous")
    usage(con, sku_name="fo_SKU_A", usage_date_=D(3), usage_quantity=100.0, billing_origin_product="fo_ppp_unpriced_previous")
    usage(con, sku_name="fo_PPP_UNPRICED_PREV", usage_date_=D(10), usage_quantity=40.0, billing_origin_product="fo_ppp_unpriced_previous")

    # fo_ppp_free: current and previous both entirely on a FREE_USAGE-named SKU (never registered
    # with list_price) -> a real $0, never a coverage gap, price_basis='free', OK at every window
    # (review fix must_fix 3: COALESCE(current_cost, 0) < :min_spend_usd, so a NULL current-period
    # sum reads OK too, not CRITICAL). P4-47: a FREE_USAGE-only side's own SUM is NULL (list_cost =
    # qty * NULL, since a FREE_USAGE SKU is never registered with list_price()) but its own
    # unpriced_quantity is 0 (FREE_USAGE SKUs are excluded from that count by design), so it
    # resolves to a real 0.0 at every window -- w=7 (D(3) current, D(10) previous) and w=30/90
    # (both D(3) and D(10) fall in the current bucket, so the previous side has no matching rows at
    # all, which is a different reason for the same real-$0 outcome) alike.
    _anchor(con, "fo_ppp_free")
    usage(con, sku_name="fo_PPP_FREE_USAGE", usage_date_=D(3), usage_quantity=999.0, billing_origin_product="fo_ppp_free")
    usage(con, sku_name="fo_PPP_FREE_USAGE", usage_date_=D(10), usage_quantity=500.0, billing_origin_product="fo_ppp_free")

    # fo_ppp_correction: current = ORIGINAL 100 + RETRACTION -20 (net 80, $80); previous = 80 ->
    # 0% change -> OK. Proves record_type is never filtered -- a RETRACTION nets against its
    # ORIGINAL the same way SUM(usage_quantity) always would.
    _anchor(con, "fo_ppp_correction")
    usage(con, sku_name="fo_SKU_A", usage_date_=D(3), usage_quantity=100.0, billing_origin_product="fo_ppp_correction")
    usage(con, sku_name="fo_SKU_A", usage_date_=D(3), usage_quantity=-20.0, billing_origin_product="fo_ppp_correction",
          record_type="RETRACTION")
    usage(con, sku_name="fo_SKU_A", usage_date_=D(10), usage_quantity=80.0, billing_origin_product="fo_ppp_correction")

    # fo_ppp_daily: uniform $10/day, D(1)..D(180) -- proves est_current_usd_list/
    # est_previous_usd_list both scale with window_days (70/70 at w=7, 300/300 at w=30,
    # 900/900 at w=90), 0% change -> OK at every window. D(180) is its own coverage anchor (exactly
    # covers w=90's previous-window start, D0-180).
    for offset in range(1, 181):
        usage(con, sku_name="fo_SKU_DAILY", usage_date_=D(offset), usage_quantity=10.0,
              billing_origin_product="fo_ppp_daily")


# =================================================================================================
# cost_monthly_actuals (windowless -- whole history, no :period_days)
# =================================================================================================
def _build_monthly_actuals(con: duckdb.DuckDBPyConnection) -> None:
    # fo_ma_cur: Sept 2026 (D0's own month) -- D(0) is written but EXCLUDED (today, still in
    # flight); D(1)+D(3)+D(5) count, net=35.00, days_captured=3, first_day=D(5), last_day=D(1).
    # is_partial_month=TRUE. D(0) proves this query EXCLUDES today's row, same as the other two.
    usage(con, sku_name="fo_SKU_A", usage_date_=D(0), usage_quantity=7.0, billing_origin_product="fo_ma_cur")
    usage(con, sku_name="fo_SKU_A", usage_date_=D(1), usage_quantity=10.0, billing_origin_product="fo_ma_cur")
    usage(con, sku_name="fo_SKU_A", usage_date_=D(3), usage_quantity=20.0, billing_origin_product="fo_ma_cur")
    usage(con, sku_name="fo_SKU_A", usage_date_=D(5), usage_quantity=5.0, billing_origin_product="fo_ma_cur")

    # fo_ma_past: August 2026, 3 distinct days, net=50.00, is_partial_month=FALSE.
    usage(con, sku_name="fo_SKU_A", usage_date_=date(2026, 8, 5), usage_quantity=15.0, billing_origin_product="fo_ma_past")
    usage(con, sku_name="fo_SKU_A", usage_date_=date(2026, 8, 10), usage_quantity=25.0, billing_origin_product="fo_ma_past")
    usage(con, sku_name="fo_SKU_A", usage_date_=date(2026, 8, 20), usage_quantity=10.0, billing_origin_product="fo_ma_past")

    # fo_ma_unpriced: August 2026, 1 priced day (8.00) + 1 unpriced day -> net=8.00 (unpriced
    # ignored), days_captured=2, price_basis='unpriced'.
    usage(con, sku_name="fo_MA_UNPRICED", usage_date_=date(2026, 8, 12), usage_quantity=5.0, billing_origin_product="fo_ma_unpriced")
    usage(con, sku_name="fo_SKU_A", usage_date_=date(2026, 8, 13), usage_quantity=8.0, billing_origin_product="fo_ma_unpriced")

    # fo_ma_free: August 2026, 1 day entirely on a FREE_USAGE-named SKU -> net=NULL,
    # days_captured=1, price_basis='free'.
    usage(con, sku_name="fo_MA_FREE_USAGE", usage_date_=date(2026, 8, 14), usage_quantity=100.0, billing_origin_product="fo_ma_free")

    # fo_ma_multi: usage in TWO different calendar months for the same product -> two output
    # rows. July 2026 (net=5.00, 1 day), August 2026 (net=9.00, 1 day); both is_partial_month=FALSE.
    usage(con, sku_name="fo_SKU_A", usage_date_=date(2026, 7, 10), usage_quantity=5.0, billing_origin_product="fo_ma_multi")
    usage(con, sku_name="fo_SKU_A", usage_date_=date(2026, 8, 10), usage_quantity=9.0, billing_origin_product="fo_ma_multi")


# =================================================================================================
# cost_daily_spikes (baseline_days=14 fixed; candidate day visible at every tested window)
# =================================================================================================
def _build_daily_spikes(con: duckdb.DuckDBPyConnection) -> None:
    # fo_ds_warn: 14-day trailing baseline at $20/day (D(2)..D(15)), candidate D(1) = $50 across 3
    # SKUs (A=$30, B=$15, C=$5) -> ratio 2.5 (>=2.0), $50 >= floor -> WARN. top_skus exercises all
    # three drivers. At w=30/90 the window also scans D(15) itself (one of THIS scenario's own
    # baseline days) as its own candidate day; its trailing 14 days (D(16)..D(29)) have no usage,
    # but its $20 day cost never clears :min_spend_usd=50, so the $ floor (checked before the
    # no-baseline case -- review fix must_fix 4) keeps it out entirely: only the D(1) WARN row
    # survives at every window.
    for offset in range(2, 16):
        usage(con, sku_name="fo_SKU_A", usage_date_=D(offset), usage_quantity=20.0, billing_origin_product="fo_ds_warn")
    usage(con, sku_name="fo_SKU_A", usage_date_=D(1), usage_quantity=30.0, billing_origin_product="fo_ds_warn")
    usage(con, sku_name="fo_DS_B", usage_date_=D(1), usage_quantity=15.0, billing_origin_product="fo_ds_warn")
    usage(con, sku_name="fo_DS_C", usage_date_=D(1), usage_quantity=5.0, billing_origin_product="fo_ds_warn")

    # fo_ds_notassessed: candidate D(3) = $100, NO usage at all in its own trailing 14-day window
    # (D(4)..D(17)) -> baseline_days_seen=0 -> NOT_ASSESSED.
    usage(con, sku_name="fo_SKU_A", usage_date_=D(3), usage_quantity=100.0, billing_origin_product="fo_ds_notassessed")

    # fo_ds_nospike: same $20/day baseline, candidate D(1) = $25 -> ratio 1.25 (<2.0, also below
    # :min_spend_usd=50) -> no row at all (DEC-57: only flagged/NOT_ASSESSED days are emitted).
    for offset in range(2, 16):
        usage(con, sku_name="fo_SKU_A", usage_date_=D(offset), usage_quantity=20.0, billing_origin_product="fo_ds_nospike")
    usage(con, sku_name="fo_SKU_A", usage_date_=D(1), usage_quantity=25.0, billing_origin_product="fo_ds_nospike")

    # fo_ds_floor: $1/day baseline (median=1), candidate D(1) = $10 -> ratio 10x clears
    # :spike_ratio easily, but $10 < :min_spend_usd=50 -> no row (the dollar floor, not the ratio,
    # blocks it -- distinct from fo_ds_nospike above).
    for offset in range(2, 16):
        usage(con, sku_name="fo_SKU_A", usage_date_=D(offset), usage_quantity=1.0, billing_origin_product="fo_ds_floor")
    usage(con, sku_name="fo_SKU_A", usage_date_=D(1), usage_quantity=10.0, billing_origin_product="fo_ds_floor")

    # fo_ds_unpriced: $20/day baseline, candidate D(1) = 60 priced + 10 qty unpriced -> day_cost=60
    # (unpriced ignored), ratio 3.0 -> WARN, price_basis='unpriced'.
    for offset in range(2, 16):
        usage(con, sku_name="fo_SKU_A", usage_date_=D(offset), usage_quantity=20.0, billing_origin_product="fo_ds_unpriced")
    usage(con, sku_name="fo_SKU_A", usage_date_=D(1), usage_quantity=60.0, billing_origin_product="fo_ds_unpriced")
    usage(con, sku_name="fo_DS_UNPRICED", usage_date_=D(1), usage_quantity=10.0, billing_origin_product="fo_ds_unpriced")

    # fo_ds_free_na: candidate D(1) entirely on a FREE_USAGE-named SKU, zero baseline history ->
    # day_cost NULL (a real $0, never a gap). Review fix (must_fix 4): the $ floor is checked
    # BEFORE the no-baseline case, and COALESCE(day_cost, 0) < :min_spend_usd is true for a real
    # $0 day -> status OK -> no row at all, at every window (never NOT_ASSESSED, which would
    # wrongly suggest this $0 day is worth a second look).
    usage(con, sku_name="fo_DS_FREE_USAGE", usage_date_=D(1), usage_quantity=50.0, billing_origin_product="fo_ds_free_na")

    # fo_ds_shortbaseline: exactly ONE trailing baseline day (D(2)=$5), candidate D(1)=$50 ->
    # baseline_days_seen=1, below :min_baseline_days=3 -> NOT_ASSESSED (short_baseline). The raw
    # ratio (10x) and the $ floor would both clear on their own -- proves the baseline-size check
    # withholds a verdict even when the number alone looks like a clean spike.
    usage(con, sku_name="fo_SKU_A", usage_date_=D(2), usage_quantity=5.0, billing_origin_product="fo_ds_shortbaseline")
    usage(con, sku_name="fo_SKU_A", usage_date_=D(1), usage_quantity=50.0, billing_origin_product="fo_ds_shortbaseline")


# =================================================================================================
# cost_sku_trend_12m (windowless -- always the last 12 full calendar months plus the current
# partial month; see the module docstring's own scenario table for the numbers). $1.00/DBU on every
# scenario's own SKU (via _trend_list_price, priced from AS_OF-400 days) so usage_quantity IS the
# dollar amount, one row per calendar month.
# =================================================================================================
def _build_sku_trend_12m(con: duckdb.DuckDBPyConnection) -> None:
    # fo_trend_double: 1000/mo Sep-Nov'25 (first 3) and Mar-May'26 (prior 3), 2000/mo Jun-Aug'26
    # (last 3) -> growth_3m_pct = growth_12m_pct = +100.0% -> CRITICAL (>=100%, usd_last_3m_avg=2000
    # >= :crit_min_usd=1000). One extra Sept 2026 (current, partial-month) row of 9999 proves
    # is_partial_month=TRUE and that the partial month never enters any 3-month average.
    for d in (date(2025, 9, 15), date(2025, 10, 15), date(2025, 11, 15),
              date(2026, 3, 15), date(2026, 4, 15), date(2026, 5, 15)):
        usage(con, sku_name="fo_trend_double", usage_date_=d, usage_quantity=1000.0,
              billing_origin_product="MODEL_SERVING")
    for d in (date(2026, 6, 15), date(2026, 7, 15), date(2026, 8, 15)):
        usage(con, sku_name="fo_trend_double", usage_date_=d, usage_quantity=2000.0,
              billing_origin_product="MODEL_SERVING")
    usage(con, sku_name="fo_trend_double", usage_date_=date(2026, 9, 10), usage_quantity=9999.0,
          billing_origin_product="MODEL_SERVING")

    # fo_trend_warn60: 500/mo first 3 and prior 3, 800/mo last 3 -> growth_3m_pct =
    # growth_12m_pct = +60.0% -> WARN (>=50%, <100%; usd_last_3m_avg=800 >= :warn_min_usd=500).
    for d in (date(2025, 9, 15), date(2025, 10, 15), date(2025, 11, 15),
              date(2026, 3, 15), date(2026, 4, 15), date(2026, 5, 15)):
        usage(con, sku_name="fo_trend_warn60", usage_date_=d, usage_quantity=500.0,
              billing_origin_product="SQL")
    for d in (date(2026, 6, 15), date(2026, 7, 15), date(2026, 8, 15)):
        usage(con, sku_name="fo_trend_warn60", usage_date_=d, usage_quantity=800.0,
              billing_origin_product="SQL")

    # fo_trend_new_vs: no usage at all before Jun'26 (first 3 AND prior 3 both a real 0.0), 700/mo
    # last 3 -> usd_prior_3m_avg = usd_first_3m_avg = 0 -> growth_3m_pct/growth_12m_pct NULL
    # (NULLIF avoids the divide-by-zero) -> new_this_year=TRUE, usd_last_3m_avg=700 >=
    # :new_sku_min_usd=500 -> WARN via the new-this-year branch alone (growth_3m_pct is NULL, so it
    # can never itself clear :warn_growth_pct/:crit_growth_pct).
    for d in (date(2026, 6, 15), date(2026, 7, 15), date(2026, 8, 15)):
        usage(con, sku_name="fo_trend_new_vs", usage_date_=d, usage_quantity=700.0,
              billing_origin_product="VECTOR_SEARCH")

    # fo_trend_flat: 600/mo in all three buckets -> growth_3m_pct = growth_12m_pct = 0.0% -> OK,
    # proving a $ amount above every WARN/CRITICAL floor still reads OK when growth is flat.
    for d in (date(2025, 9, 15), date(2025, 10, 15), date(2025, 11, 15),
              date(2026, 3, 15), date(2026, 4, 15), date(2026, 5, 15),
              date(2026, 6, 15), date(2026, 7, 15), date(2026, 8, 15)):
        usage(con, sku_name="fo_trend_flat", usage_date_=d, usage_quantity=600.0,
              billing_origin_product="JOBS")


# =================================================================================================
# build(con) -- auto-discovered by build_fixtures.py (DEC-17).
# =================================================================================================
def build(con: duckdb.DuckDBPyConnection) -> None:
    list_price(con, "fo_SKU_A", 1.00)
    list_price(con, "fo_SKU_DAILY", 1.00)
    list_price(con, "fo_DS_B", 1.00)
    list_price(con, "fo_DS_C", 1.00)
    # fo_PPP_UNPRICED, fo_MA_UNPRICED, fo_DS_UNPRICED and every *_FREE_USAGE-named SKU are
    # deliberately never registered with list_price().
    for sku in ("fo_trend_double", "fo_trend_warn60", "fo_trend_new_vs", "fo_trend_flat"):
        _trend_list_price(con, sku, 1.00)

    _build_period_over_period(con)
    _build_monthly_actuals(con)
    _build_daily_spikes(con)
    _build_sku_trend_12m(con)
