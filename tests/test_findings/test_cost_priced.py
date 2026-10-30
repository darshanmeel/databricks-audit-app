"""tests/test_findings/test_cost_priced.py -- T-13. Revised after Opus review (round 2) --
see this module's own inline comments and tasks/T-13-cost-priced-tests.md's Hand-off notes for the
full list of what changed and why.

Proves, against tests/fixtures/billing.py's own rows and only those rows (the shared workspaces
1111/2222/3333 excepted -- see cost_workspace_names below), that each of the 9 built
findings.f_<query_id> tables in batch A2 (PLAN.md 7.2) has the right grain, the right window_days
behaviour, hits every status band billing.py's fixture can actually reach for that id (documented
honestly where it cannot, WITH THE OWNING TASK NAMED -- see each test's own note and the Hand-off
notes), and -- the heart of this batch -- that the price-window join in cost_actual_vs_list_by_sku
/ cost_cloud_infra / cost_dollarized_by_sku_day / cost_serving_mode_by_endpoint /
cost_vector_search_spend prices a usage row at the CURRENT list_prices row (price_end_time IS
NULL), never the EXPIRED one that also exists for the same (sku_name, cloud, usage_unit).

Every dollar/DBU expectation below is computed from the raw fixture parquet at test time (never a
pasted total, and never tests/fixtures/billing.py's own private `_PRICED_SKUS` rate table -- that
constant is positional and private to billing.py; test_compute_activity.py's review fix item 2
rejected exactly this borrowing pattern for a DIFFERENT builder's constant, and the same discipline
applies to relying on this file's own internals instead of the parquet it produces). `_rate_row` /
`_current_rate` below read tests/fixtures/parquet/billing__list_prices/*.parquet directly and apply
the CAST(pricing.default/effective_list.default AS DOUBLE) + price_end_time IS [NOT] NULL predicate
every vendored body uses, per tests/test_findings/test_compute_activity.py's precedent
(`_cost_rollup`) and tests/test_findings/test_overview_spend_estimate.py's (`_spend_by_workspace`).
`cost_cloud_infra`'s grain (usage_date, cloud, currency_code) carries no identifier column at all,
so `_cloud_infra_rollup` below is the ONLY way any test in this module reads that id's dollar
figures -- never a pinned literal -- because billing__usage / billing__list_prices are written by
FOUR builders (billing.py, lakeflow.py, compute.py, serving_storage.py; lakeflow.py also writes
billing__list_prices) and a cell can and does already mix rows from more than one of them (see
test_cost_cloud_infra's own comment).

Two price-join predicate SHAPES exist across this batch's 5 priced bodies (tasks/T-13-cost-priced-
tests.md's Inputs section, re-verified against each vendored .sql file below): cost_actual_vs_list_
by_sku alone joins on `u.usage_date >= DATE(p.price_start_time) AND (p.price_end_time IS NULL OR
u.usage_date < DATE(p.price_end_time))`; the other four join on `u.usage_end_time >=
lp.price_start_time AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)`.
All five key the join on usage_unit too (cost_cloud_infra, cost_actual_vs_list_by_sku and
(P4-47) cost_dollarized_by_sku_day already did; cost_serving_mode_by_endpoint and
cost_vector_search_spend were fixed to match, closing the fan-out gap the two used to carry when a
sku_name+cloud priced in two units -- re-verified against each .sql file).
test_price_window_join_current_not_expired_no_fanout proves the CURRENT-vs-EXPIRED predicate logic
against the raw parquet AND against the built table (added in review round 2 -- see that test's own
docstring); the genuine no-fan-out PROOF for cost_cloud_infra is test_cost_cloud_infra's own
record_count comparison against an independent COUNT(*) replica (record_count is COUNT(*) after the
LEFT JOIN, so a usage row matching two price rows inflates it) -- the dedicated test below does not
independently prove fan-out cannot happen in the model's own SQL beyond what that comparison covers.

Review round 2 also found: (1) this fixture cannot construct a usage row that falls INSIDE the
EXPIRED price row's own validity window and observe it priced at the OLD rate -- billing.py:220-229
(`priced_sku()`) leaves a 100-day dead gap between the expired window's end (AS_OF-200d) and the
current window's start (AS_OF-100d), and the oldest usage row in this fixture is D(45); this is a
genuine structural gap in tests/fixtures/billing.py (T-09), not provable here without changing that
file, which is out of T-13's owned paths -- see tasks/T-13-cost-priced-tests.md's Hand-off notes.
(2) Every "band X is not reachable" claim below except that one is an ordinary one-line fixture gap
in billing.py (a threshold billing.py's fixture values happen to fall short of), not a structural
impossibility -- corrected per-id below and in the Hand-off notes, with billing.py/T-09 named as the
owner in every case.

NOT_ASSESSED-by-unpriced-SKU is engineered once in billing.py (bl_SKU_NO_PRICE, used only by
bl_u_job_ok, billing_origin_product='JOBS') -- per billing.py's own module docstring ("the LEFT
JOIN NOT_ASSESSED path is proven once ... not once per priced query"), so it is asserted here on
cost_actual_vs_list_by_sku (status NOT_ASSESSED) and cost_dollarized_by_sku_day (NULL net_list_cost
+ NULL currency_code) -- both bodies have no billing_origin_product filter, so bl_u_job_ok's row
reaches them. cost_serving_mode_by_endpoint / cost_vector_search_spend filter to
billing_origin_product IN ('MODEL_SERVING'/'VECTOR_SEARCH') respectively, which bl_u_job_ok never
matches; billing.py's own fixture cannot reach NOT_ASSESSED on either id (a one-line fixture gap:
neither id has an unpriced SKU of its own), but the live build already shows this path IS reachable
via serving_storage.py's (T-22) unpriced ss_MODEL_SERVING_STANDARD / ss_VECTOR_SEARCH_SERVING rows
(6 and 1 NOT_ASSESSED rows respectively, confirmed against tests/db_audit_test.duckdb) -- this
module deliberately does not assert on those rows (DEC-15: assertions filter on the owning
builder's own ids; a T-22-owned row is not T-13's to pin), so this remains an honest, named fixture
gap for billing.py rather than a covered case.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import duckdb
import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR / "fixtures"))
import dbutil  # noqa: E402
import billing as bl  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "cost_priced.yml"
FINDINGS_YML = ROOT / "dbt" / "models" / "findings" / "cost" / "_findings__cost.yml"
PARQUET_DIR = ROOT / "tests" / "fixtures" / "parquet"
WINDOWS = (7, 30, 90)
STATUS_VALUES = {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}

ALL_IDS = [
    "cost_actual_vs_list_by_sku",
    "cost_cloud_infra",
    "cost_dollarized_by_sku_day",
    "cost_serving_mode_by_endpoint",
    "cost_vector_search_spend",
    "cost_account_prices_raw",
    "pricing_list_prices_raw",
    "cost_dbsql_allocation_gap",
    "cost_workspace_names",
]
WINDOWLESS_IDS = {"cost_account_prices_raw", "pricing_list_prices_raw", "cost_workspace_names"}
STATUS_IDS = {"cost_actual_vs_list_by_sku", "cost_serving_mode_by_endpoint", "cost_vector_search_spend"}

D0 = bl.D0  # audit_today() on target test -- 2026-09-21


# -------------------------------------------------------------------------------------------
# Helpers -- everything reads straight from tests/fixtures/parquet, never a hard-coded total
# and never tests/fixtures/billing.py's private _PRICED_SKUS constant.
# -------------------------------------------------------------------------------------------
def _grains() -> dict:
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


_FINDINGS_DOC = None


def _meta(query_id: str) -> dict:
    """The model's own `meta` block (order_by, params, ...) from the generated per-domain yml
    (dbt/models/findings/cost/_findings__cost.yml) -- read at test time, not copy-pasted, so an
    order_by / param-default change in the vendored SQL is caught here too."""
    global _FINDINGS_DOC
    if _FINDINGS_DOC is None:
        with open(FINDINGS_YML, "r", encoding="utf-8") as f:
            _FINDINGS_DOC = yaml.safe_load(f)
    for model in _FINDINGS_DOC["models"]:
        if model["meta"]["query_id"] == query_id:
            return model["meta"]
    raise KeyError(query_id)


def _params(query_id: str) -> dict:
    return {p["name"]: p["default"] for p in _meta(query_id)["params"]}


def _rate_row(sku_name: str, cloud: str, usage_unit: str, *, current: bool):
    """The ONE row (default_rate, list_rate, price_start_time, price_end_time) from
    billing__list_prices, filtered to the CURRENT (price_end_time IS NULL) or EXPIRED
    (price_end_time IS NOT NULL) price for (sku_name, cloud, usage_unit) -- straight from the
    fixture parquet, applying CAST(pricing.default/effective_list.default AS DOUBLE) the same way
    every priced vendored body does (test_compute_activity.py's `_cost_rollup` precedent).

    Uses fetchall() + an explicit len==1 assertion, not fetchone() (review round 2, item 12): a
    second matching current (or expired) price row for the same (sku, cloud, usage_unit) -- which
    tests/fixtures/billing.py's own priced_sku() never produces today, but nothing enforces that
    for a future fixture change -- would otherwise be silently picked at fetchone()'s discretion,
    making every downstream dollar assertion non-deterministic instead of loudly failing here."""
    glob = (PARQUET_DIR / "billing__list_prices" / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        op = "IS NULL" if current else "IS NOT NULL"
        sql = f"""
            SELECT CAST(pricing.default AS DOUBLE) AS default_rate,
                   CAST(pricing.effective_list.default AS DOUBLE) AS list_rate,
                   price_start_time, price_end_time
            FROM read_parquet('{glob}', union_by_name=true)
            WHERE sku_name = ? AND cloud = ? AND usage_unit = ? AND price_end_time {op}
        """
        rows = con.execute(sql, [sku_name, cloud, usage_unit]).fetchall()
        label = "current" if current else "expired"
        assert len(rows) == 1, (
            f"expected exactly one {label} price row for {sku_name}/{cloud}/{usage_unit}, got {len(rows)}"
        )
        return rows[0]
    finally:
        con.close()


def _current_rate(sku_name: str, cloud: str, usage_unit: str, field: str) -> float:
    """field: 'default' (dp.default_rate) or 'effective_list' (lp.list_rate)."""
    default_rate, list_rate, _, _ = _rate_row(sku_name, cloud, usage_unit, current=True)
    return default_rate if field == "default" else list_rate


def _raw_row_exists(schema_table: str, where: str) -> bool:
    """True if at least one row matching `where` exists anywhere in the raw
    tests/fixtures/parquet/<schema_table>/*.parquet -- proves a fixture row genuinely exists
    independent of any window filter (test_compute_activity.py review fix item 1 precedent)."""
    pattern = (PARQUET_DIR / schema_table / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        sql = f"SELECT COUNT(*) FROM read_parquet('{pattern}', union_by_name=true) WHERE {where}"
        return con.execute(sql).fetchone()[0] > 0
    finally:
        con.close()


def _attributed_sum(where: str) -> float:
    """SUM(active_usage_quantity) over the raw billing.attributed_usage fixture parquet, mirroring
    dbutil.usage_sum's contract for system.billing.usage (dbutil.py does not cover
    attributed_usage; this batch is the only one that reads it)."""
    pattern = (PARQUET_DIR / "billing__attributed_usage" / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        sql = f"SELECT SUM(active_usage_quantity) FROM read_parquet('{pattern}', union_by_name=true) WHERE {where}"
        result = con.execute(sql).fetchone()
        return float(result[0]) if result and result[0] is not None else 0.0
    finally:
        con.close()


def _raw_endpoint_name(record_id: str) -> str:
    pattern = (PARQUET_DIR / "billing__usage" / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        sql = f"SELECT usage_metadata.endpoint_name FROM read_parquet('{pattern}', union_by_name=true) WHERE record_id = ?"
        return con.execute(sql, [record_id]).fetchone()[0]
    finally:
        con.close()


def _asc_key(v):
    """NULLS LAST ascending sort key -- matches DuckDB's observed default null ordering on this
    built table (confirmed against findings.f_cost_cloud_infra's real row order: 'USD' precedes
    NULL currency_code within the same usage_date/cloud)."""
    return (1, "") if v is None else (0, v)


def _grain_keys(rows, cols):
    return [tuple(r[c] for c in cols) for r in rows]


def _cloud_infra_rollup(window_days: int) -> dict:
    """Independent re-derivation of cost_cloud_infra.sql's own join and GROUP BY (review round 2,
    items 2 and 3; DEC-66.1 basis switch) -- LEFT JOIN billing__usage to billing__list_prices on
    sku_name + cloud + usage_unit with the usage_end_time-in-window predicate (`u.usage_end_time >=
    p.price_start_time AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)`),
    CAST(pricing.effective_list.default AS DOUBLE) (the effective, post-promotion list price -- the
    one basis this app uses everywhere, DEC-66.1), no usage_unit filter on the outer query (every
    billing_origin_product is included, exactly like the vendored body), GROUP BY (workspace_id,
    usage_date, cloud, currency_code). Computed fresh from whatever tests/fixtures/parquet/
    billing__usage and billing__list_prices currently hold -- cost_cloud_infra's grain carries no
    OTHER identifier column, and billing__usage is written by FOUR builders (billing.py,
    lakeflow.py, compute.py, serving_storage.py; lakeflow.py also writes billing__list_prices), so
    a cell's composition is not this task's to assume. Returns {(workspace_id, usage_date, cloud,
    currency_code): (net_list_cost, record_count, price_basis)}; record_count is COUNT(*) after the
    join -- the fan-out detector: a usage row matching two price rows inflates it here in exactly
    the way it would inflate the model's own output, so a match against the built table's
    record_count is a real (not tautological) check. price_basis mirrors the model's own CASE:
    'unpriced' if any non-FREE_USAGE SKU in the cell had no matching price row, 'free' if every
    matched SKU is FREE_USAGE, else 'priced'."""
    usage_glob = (PARQUET_DIR / "billing__usage" / "*.parquet").as_posix()
    price_glob = (PARQUET_DIR / "billing__list_prices" / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        sql = f"""
            WITH price AS (
                SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time, currency_code,
                       CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
                FROM read_parquet('{price_glob}', union_by_name=true)
            )
            SELECT u.workspace_id, u.usage_date, u.cloud, p.currency_code,
                   SUM(u.usage_quantity * p.list_rate) AS net_list_cost,
                   COUNT(*) AS record_count,
                   CASE
                     WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                                   THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
                     WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
                     ELSE 'priced'
                   END AS price_basis
            FROM read_parquet('{usage_glob}', union_by_name=true) u
            LEFT JOIN price p
              ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
             AND u.usage_end_time >= p.price_start_time
             AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
            WHERE u.usage_date >= DATE '{D0.isoformat()}' - INTERVAL {int(window_days)} DAY
              AND u.usage_date < DATE '{D0.isoformat()}'
            GROUP BY u.workspace_id, u.usage_date, u.cloud, p.currency_code
        """
        return {
            (ws, d, c, cur): (cost, cnt, basis)
            for ws, d, c, cur, cost, cnt, basis in con.execute(sql).fetchall()
        }
    finally:
        con.close()


# -------------------------------------------------------------------------------------------
# Generic checks over all 9 ids.
# -------------------------------------------------------------------------------------------
def test_window_days_behavior_all_ids():
    """Windowed ids (6): empty at window_days=0, non-empty at 7/30/90 (DEC-51 / this batch's own
    fixture). Windowless/snapshot ids (3): non-empty at window_days=0, empty at 7/30/90 -- checked
    individually, not assumed, per the task's own instruction (cost_workspace_names is a snapshot
    id like the other two)."""
    for qid in ALL_IDS:
        if qid in WINDOWLESS_IDS:
            assert dbutil.rows(qid, 0) != [], f"{qid}: windowless id must have rows at window_days=0"
            for w in WINDOWS:
                assert dbutil.rows(qid, w) == [], f"{qid}: windowless id must be empty at window_days={w}"
        else:
            assert dbutil.rows(qid, 0) == [], f"{qid}: windowed id must be empty at window_days=0"
            for w in WINDOWS:
                assert dbutil.rows(qid, w) != [], f"{qid}: windowed id must have rows at window_days={w}"


def test_status_enum_where_present():
    for qid in STATUS_IDS:
        for w in WINDOWS:
            out = dbutil.rows(qid, w)
            assert out, (qid, w)
            for r in out:
                assert r["status"] in STATUS_VALUES, f"{qid} w={w}: bad status {r['status']!r}"


# -------------------------------------------------------------------------------------------
# The heart of this batch: prove the price-window join picks the CURRENT list_prices row, never
# the EXPIRED one, and never both (no fan-out) -- straight against the raw fixture parquet and
# both join-predicate shapes this batch's bodies use, independent of any one model's own SQL.
# -------------------------------------------------------------------------------------------
def test_price_window_join_current_not_expired_no_fanout():
    """bl_JOBS_COMPUTE_CLASSIC (aws, DBU) carries both a CURRENT (price_end_time IS NULL) and an
    EXPIRED (price_end_time in the past) list_prices row (tests/fixtures/billing.py's
    priced_sku()).

    Part 1 (parquet only) proves the predicate LOGIC against the raw parquet: (1) exactly one
    current + one expired row exist for this (sku, cloud, usage_unit); (2) their rates genuinely
    differ; (3) a real fixture usage row's usage_end_time (bl_u_job_crit_r1, D(1)) satisfies the
    CURRENT row's [price_start_time, NULL) window and does NOT satisfy the EXPIRED row's
    [price_start_time, price_end_time) window, under both join-predicate shapes this batch's
    bodies use. On its own this is a re-implementation, not a proof of the model's own SQL --
    flagged in review round 2 (item 2): it re-evaluates the predicate in Python against the
    fixture's own rows, so it stays green even under a model regression that never runs this
    predicate at all (e.g. dropping the "price_end_time IS NULL OR" clause from cost_cloud_infra's
    join -- that regression would leave every assertion in Part 1 untouched). Part 2 closes that
    gap by reading the BUILT tables and comparing against independent replicas of the model's own
    join computed fresh from the parquet (never a pinned literal): a dropped "IS NULL OR" clause
    would NULL out or diverge the built values Part 2 checks, so Part 2 (unlike Part 1) actually
    fails under that regression.

    The genuine no-fan-out PROOF in this module is test_cost_cloud_infra's / this test's own
    Part 2 record_count comparison against `_cloud_infra_rollup`'s COUNT(*) -- record_count is
    COUNT(*) after the LEFT JOIN, so a usage row matching two price rows inflates it identically
    in both the model and the replica, and a real fan-out bug (present in the model but not the
    replica) would make the two counts diverge. Part 1's date_match/end_time_match checks are
    necessary context (they establish that the two price rows' windows do not overlap for this
    usage row) but are not themselves a fan-out proof, since they never touch the model's output.

    NOT provable with this fixture, at all: a usage row whose usage_end_time falls INSIDE the
    EXPIRED row's own [price_start_time, price_end_time) window, observed priced at the OLD rate.
    tests/fixtures/billing.py:220-229 (`priced_sku()`) places the expired window at
    [AS_OF-400d, AS_OF-200d) and the current window at [AS_OF-100d, NULL) -- a 100-day dead gap
    with no valid price at all -- and the oldest usage row anywhere in this fixture is D(45)
    (45 days before AS_OF), nowhere near old enough to land inside [AS_OF-400d, AS_OF-200d). This
    is a genuine STRUCTURAL gap in tests/fixtures/billing.py (T-09's owned file, off limits to
    T-13), not a one-line fixture gap like the band-reachability ones documented per-id below --
    recorded in tasks/T-13-cost-priced-tests.md's Hand-off notes, not faked here.
    """
    assert _raw_row_exists("billing__usage", "record_id = 'bl_u_job_crit_r1'")

    default_current, list_current, cur_start, cur_end = _rate_row(
        "bl_JOBS_COMPUTE_CLASSIC", "aws", "DBU", current=True
    )
    default_expired, list_expired, exp_start, exp_end = _rate_row(
        "bl_JOBS_COMPUTE_CLASSIC", "aws", "DBU", current=False
    )
    assert cur_end is None
    assert exp_end is not None
    assert default_current != default_expired
    assert list_current != list_expired

    glob = (PARQUET_DIR / "billing__list_prices" / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        n = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{glob}', union_by_name=true) "
            "WHERE sku_name = 'bl_JOBS_COMPUTE_CLASSIC' AND cloud = 'aws' AND usage_unit = 'DBU'"
        ).fetchone()[0]
        assert n == 2, f"expected exactly 2 price rows (current+expired), got {n}"

        usage_glob = (PARQUET_DIR / "billing__usage" / "*.parquet").as_posix()
        usage_date, usage_end_time = con.execute(
            f"SELECT usage_date, usage_end_time FROM read_parquet('{usage_glob}', union_by_name=true) "
            "WHERE record_id = 'bl_u_job_crit_r1'"
        ).fetchone()
    finally:
        con.close()

    # cost_actual_vs_list_by_sku's own shape: usage_date >= DATE(price_start_time) AND
    # (price_end_time IS NULL OR usage_date < DATE(price_end_time))
    def date_match(price_start, price_end):
        return usage_date >= price_start.date() and (price_end is None or usage_date < price_end.date())

    assert date_match(cur_start, cur_end) is True
    assert date_match(exp_start, exp_end) is False

    # cost_cloud_infra / cost_dollarized_by_sku_day / cost_serving_mode_by_endpoint /
    # cost_vector_search_spend's shape: usage_end_time >= price_start_time AND
    # (price_end_time IS NULL OR usage_end_time < price_end_time)
    def end_time_match(price_start, price_end):
        return usage_end_time >= price_start and (price_end is None or usage_end_time < price_end)

    assert end_time_match(cur_start, cur_end) is True
    assert end_time_match(exp_start, exp_end) is False

    # --- Part 2: read the BUILT tables and prove the same thing against real model output -----
    # (a) cost_actual_vs_list_by_sku (DATE(usage_date) shape): the priced value for
    # bl_JOBS_COMPUTE_CLASSIC/aws/DBU/JOBS equals qty x CURRENT effective-list rate and is NOT
    # qty x EXPIRED effective-list rate. qty is derived fresh from the raw parquet
    # (dbutil.usage_sum), never a pinned literal -- this cell's grain already isolates a single
    # SKU/product, so no cross-builder mixing risk applies here the way it does for
    # cost_cloud_infra's coarser grain.
    # DEC-66.1 / T-69A review round 2: net_pre_promotion_cost (pricing.default, a second, non-
    # effective-list basis) was removed entirely -- net_list_cost (pricing.effective_list.default)
    # is now the only dollar column, and every row -- priced or not -- always reads status
    # NOT_ASSESSED (no negotiated-rate source exists in system.billing).
    where = (
        "sku_name = 'bl_JOBS_COMPUTE_CLASSIC' AND cloud = 'aws' AND usage_unit = 'DBU' "
        "AND billing_origin_product = 'JOBS' AND usage_date >= DATE '{}' - INTERVAL 30 DAY "
        "AND usage_date < DATE '{}'"
    ).format(D0.isoformat(), D0.isoformat())
    qty = dbutil.usage_sum(where)
    built_row = next(
        r for r in dbutil.rows("cost_actual_vs_list_by_sku", 30)
        if (r["cloud"], r["sku_name"], r["usage_unit"], r["billing_origin_product"])
        == ("aws", "bl_JOBS_COMPUTE_CLASSIC", "DBU", "JOBS")
    )
    # a regression that dropped "price_end_time IS NULL OR ..." would NULL this cell out entirely
    # (the CURRENT row's price_end_time IS NULL would then never satisfy a plain "<" comparison):
    assert built_row["net_list_cost"] is not None
    assert abs(built_row["net_list_cost"] - qty * list_current) < 1e-6
    assert abs(built_row["net_list_cost"] - qty * list_expired) > 1e-6  # NOT the expired rate
    assert "net_pre_promotion_cost" not in built_row
    assert built_row["status"] == "NOT_ASSESSED"
    assert built_row["not_assessed_reason"] == "no_negotiated_rate_source"
    assert built_row["workspace_id"] == bl.WS_PROD  # every fixture row for this SKU is WS_PROD

    # (b) cost_cloud_infra (usage_end_time shape): compare the WS_PROD/D(9)/aws/USD cell's cost AND
    # record_count against `_cloud_infra_rollup`'s independent replica of the model's own join --
    # never a pinned literal (see that helper's docstring for why this grain in particular cannot
    # be pinned). record_count is the fan-out detector: COUNT(*) after the join.
    d9 = bl.D(9)
    expected = _cloud_infra_rollup(30)
    expected_cost, expected_count, expected_basis = expected[(bl.WS_PROD, d9, "aws", "USD")]
    infra_row = next(
        r for r in dbutil.rows("cost_cloud_infra", 30)
        if r["usage_date"] == d9 and r["cloud"] == "aws" and r["currency_code"] == "USD"
        and r["workspace_id"] == bl.WS_PROD
    )
    assert abs(infra_row["net_list_cost"] - expected_cost) < 1e-6
    assert infra_row["record_count"] == expected_count
    assert infra_row["price_basis"] == expected_basis


# -------------------------------------------------------------------------------------------
# cost_actual_vs_list_by_sku -- grain [workspace_id, cloud, sku_name, usage_unit,
# billing_origin_product] (workspace_id lets this check obey the workspace/env filter);
# DEC-66.1 / T-69A CRITICAL rewrite: no negotiated-rate source exists anywhere in system.billing,
# so this id no longer bands a "realization ratio" -- every row, priced or not, always reads
# status NOT_ASSESSED with not_assessed_reason = 'no_negotiated_rate_source'. Its only params
# left is period_days (warn/crit_realization_ratio were removed with the old band). order_by is
# now plain net_list_cost DESC NULLS LAST.
# -------------------------------------------------------------------------------------------
def test_cost_actual_vs_list_by_sku():
    qid = "cost_actual_vs_list_by_sku"
    grains = _grains()[qid]
    out = dbutil.rows(qid, 30)
    assert out
    keys = _grain_keys(out, grains)
    assert len(keys) == len(set(keys)), f"{qid}: duplicate grain rows at window_days=30"

    params = _params(qid)
    assert set(params) == {"period_days"}, "warn/crit_realization_ratio were removed with the old band (DEC-66.1)"

    # every row is NOT_ASSESSED -- no negotiated-rate source exists, so this check can never
    # produce a verdict on any account, priced or not.
    for r in out:
        assert r["status"] == "NOT_ASSESSED", r
        assert r["not_assessed_reason"] == "no_negotiated_rate_source", r

    # --- current-vs-expired price join, no fan-out: bl_JOBS_COMPUTE_CLASSIC/aws/DBU/JOBS
    # aggregates 5 fixture rows (bl_u_job_crit_r1/r2, bl_u_tier_cov, the 3-row correction triad,
    # bl_u_win_d5, bl_u_win_d20) inside the 30-day window; D(45) and D(0) (999 DBU) are excluded.
    assert _raw_row_exists("billing__usage", "record_id = 'bl_u_job_crit_r1'")
    assert _raw_row_exists("billing__usage", "record_id = 'bl_u_job_crit_r2'")
    row = next(
        r for r in out
        if (r["cloud"], r["sku_name"], r["usage_unit"], r["billing_origin_product"])
        == ("aws", "bl_JOBS_COMPUTE_CLASSIC", "DBU", "JOBS")
    )
    where = (
        "sku_name = 'bl_JOBS_COMPUTE_CLASSIC' AND cloud = 'aws' AND usage_unit = 'DBU' "
        "AND billing_origin_product = 'JOBS' AND usage_date >= DATE '{}' - INTERVAL 30 DAY "
        "AND usage_date < DATE '{}'"
    ).format(D0.isoformat(), D0.isoformat())
    expected_qty = dbutil.usage_sum(where)
    assert abs(row["net_usage_quantity"] - expected_qty) < 1e-6
    assert row["workspace_id"] == bl.WS_PROD  # every fixture row for this SKU/product is WS_PROD

    list_rate = _current_rate("bl_JOBS_COMPUTE_CLASSIC", "aws", "DBU", "effective_list")
    # if the join had picked the EXPIRED row (0.6x rate, priced_sku()) instead of, or in addition
    # to, the current one, this equality would not hold -- that is the bite. net_list_cost is
    # pricing.effective_list.default -- only the STATUS semantics changed vs the pre-review-fix
    # shape, not this figure. net_pre_promotion_cost (a second, non-effective-list basis) was
    # removed entirely in review round 2 -- DEC-66.1 leaves exactly one dollar column here.
    assert abs(row["net_list_cost"] - expected_qty * list_rate) < 1e-6
    assert "net_pre_promotion_cost" not in row

    # anchor (test_compute_activity.py review fix item 1 precedent): the unwindowed sum for this
    # SKU/product must exceed the window=90 sum by exactly the D(0)=999 row's contribution --
    # proves bl_u_win_d0 genuinely exists in the raw parquet, independent of every window filter
    # above, and that the model's own filter really drops it.
    row_90 = next(
        r for r in dbutil.rows(qid, 90)
        if (r["cloud"], r["sku_name"], r["usage_unit"], r["billing_origin_product"])
        == ("aws", "bl_JOBS_COMPUTE_CLASSIC", "DBU", "JOBS")
    )
    unwindowed = dbutil.usage_sum(
        "sku_name = 'bl_JOBS_COMPUTE_CLASSIC' AND cloud = 'aws' AND usage_unit = 'DBU' "
        "AND billing_origin_product = 'JOBS'"
    )
    assert abs(unwindowed - (row_90["net_usage_quantity"] + 999.0)) < 1e-6, unwindowed
    assert row["net_usage_quantity"] < unwindowed

    # --- the unpriced SKU (bl_SKU_NO_PRICE, via bl_u_job_ok) still surfaces its cost column as
    # NULL (a priced-coverage gap, never $0) -- status is NOT_ASSESSED here too, same as every
    # other row, but for this row it is ALSO the (previously sole) reason NOT_ASSESSED could fire.
    assert _raw_row_exists("billing__usage", "record_id = 'bl_u_job_ok'")
    unpriced = next(r for r in out if r["sku_name"] == "bl_SKU_NO_PRICE")
    assert unpriced["status"] == "NOT_ASSESSED"
    assert unpriced["net_list_cost"] is None
    assert "net_pre_promotion_cost" not in unpriced
    assert unpriced["workspace_id"] == bl.WS_PROD  # bl_u_job_ok, this SKU's only fixture row
    expected_unpriced_qty = dbutil.usage_sum(
        "sku_name = 'bl_SKU_NO_PRICE' AND usage_date >= DATE '{}' - INTERVAL 30 DAY AND usage_date < DATE '{}'".format(
            D0.isoformat(), D0.isoformat()
        )
    )
    assert abs(unpriced["net_usage_quantity"] - expected_unpriced_qty) < 1e-6

    # --- worst-first ordering: order_by (read from the model's own meta, not hard-coded) is now
    # plain net_list_cost DESC NULLS LAST -- the old realization-ratio ORDER BY is gone with the band.
    assert _meta(qid)["order_by"] == "net_list_cost DESC NULLS LAST"

    def sort_key(r):
        nl = r["net_list_cost"]
        return (0 if nl is not None else 1, -(nl or 0.0))

    expected_order = sorted(out, key=sort_key)
    assert _grain_keys(out, grains) == _grain_keys(expected_order, grains)


# -------------------------------------------------------------------------------------------
# cost_cloud_infra -- grain [workspace_id, usage_date, cloud, currency_code] (workspace_id added
# so this check obeys the workspace/env filter, sourced from billing__usage -- the only side of
# the join that carries it); inventory (no status); order_by = usage_date DESC, cloud,
# currency_code (unchanged -- workspace_id narrows the grain without joining the ordering).
# usage_unit IS part of the join key, so a usage_unit with no matching price row contributes NULL,
# never the DBU rate.
#
# Beyond workspace_id this grain carries no other identifier column -- review round 2 (item 3)
# confirmed billing__usage is written by FOUR builders (billing.py 45 rows, lakeflow.py 74,
# compute.py 19, serving_storage.py 7; lakeflow.py also writes billing__list_prices), and a
# (workspace_id, usage_date, cloud, currency_code) cell can still mix more than one builder's rows
# if they happen to share a workspace_id. Every value below is therefore compared against
# `_cloud_infra_rollup`'s independent replica of the model's own join, re-derived fresh from
# whatever the parquet currently holds -- never a pinned literal (record_count/cost pins here
# passed only because a specific cell happened to be single-builder at the time they were
# written, and one new row from any builder on that workspace/date/cloud silently breaks a pin
# without ever failing the test -- exactly the hazard config/grains/cost_usage_only.yml's own
# header documents for masked-but-otherwise-identifier-free grains).
# -------------------------------------------------------------------------------------------
def test_cost_cloud_infra():
    qid = "cost_cloud_infra"
    grains = _grains()[qid]
    out = dbutil.rows(qid, 30)
    assert out
    keys = _grain_keys(out, grains)
    assert len(keys) == len(set(keys)), f"{qid}: duplicate grain rows at window_days=30"

    expected = _cloud_infra_rollup(30)

    # priced cell: WS_PROD/D(1)/aws/USD -- at minimum billing.py's own bl_u_job_crit_r1/r2/
    # bl_u_job_warn contribute here (SEC A, all WS_PROD), possibly alongside other builders'
    # priced rows on the same workspace/date; record_count doubles as the fan-out detector
    # (COUNT(*) after the LEFT JOIN).
    d1 = bl.D(1)
    assert (bl.WS_PROD, d1, "aws", "USD") in expected
    expected_cost, expected_count, expected_basis = expected[(bl.WS_PROD, d1, "aws", "USD")]
    priced_row = next(
        r for r in out if r["usage_date"] == d1 and r["cloud"] == "aws" and r["currency_code"] == "USD"
        and r["workspace_id"] == bl.WS_PROD
    )
    assert abs(priced_row["net_list_cost"] - expected_cost) < 1e-6
    assert priced_row["record_count"] == expected_count
    assert priced_row["price_basis"] == expected_basis

    # NULL-cost group: bl_u_job_ok's SKU (bl_SKU_NO_PRICE) has no price row at all -- LEFT JOIN
    # keeps the usage row but currency_code and net_list_cost are both NULL, never $0 (the bare
    # SUM, not COALESCE-wrapped, is what makes a fully-unpriced group NULL rather than 0). The
    # cell may hold other builders' unpriced rows too (record_count/cost/basis are all re-derived,
    # never assumed to be bl_u_job_ok alone). bl_SKU_NO_PRICE does not match '%FREE_USAGE%', so
    # this group's price_basis is 'unpriced', never 'free'.
    assert _raw_row_exists("billing__usage", "record_id = 'bl_u_job_ok'")
    assert (bl.WS_PROD, d1, "aws", None) in expected
    expected_null_cost, expected_null_count, expected_null_basis = expected[(bl.WS_PROD, d1, "aws", None)]
    assert expected_null_cost is None
    assert expected_null_basis == "unpriced"
    null_row = next(
        r for r in out if r["usage_date"] == d1 and r["cloud"] == "aws" and r["currency_code"] is None
        and r["workspace_id"] == bl.WS_PROD
    )
    assert null_row["net_list_cost"] is None
    assert null_row["record_count"] == expected_null_count
    assert null_row["price_basis"] == "unpriced"

    # current-vs-expired: WS_PROD/D(9)/aws/USD -- at minimum bl_u_sql_raw1 (bl_DBSQL_COMPUTE, 80
    # DBU) contributes here (SEC I, WS_PROD); do NOT assume this cell is single-row (see
    # module-level note -- lakeflow.py's unpriced lf_JOBS_COMPUTE rows on this same date land in
    # the NULL cell, not this one, but nothing prevents a future priced contribution to this exact
    # cell too).
    d9 = bl.D(9)
    assert (bl.WS_PROD, d9, "aws", "USD") in expected
    expected_d9_cost, expected_d9_count, expected_d9_basis = expected[(bl.WS_PROD, d9, "aws", "USD")]
    d9_row = next(
        r for r in out if r["usage_date"] == d9 and r["cloud"] == "aws" and r["currency_code"] == "USD"
        and r["workspace_id"] == bl.WS_PROD
    )
    assert abs(d9_row["net_list_cost"] - expected_d9_cost) < 1e-6
    assert d9_row["record_count"] == expected_d9_count
    assert d9_row["price_basis"] == expected_d9_basis

    # window exclusions: D(0) (usage_date = today) is never in any window; D(45) is out of the
    # 30-day window but inside the 90-day one (bl_u_win_d45, part of the JOBS/bl_JOBS_COMPUTE_CLASSIC
    # cell above, so its inclusion/exclusion shows up as a change to that cell's own cost, not just
    # as a new row -- checked directly here via row presence per usage_date).
    d45 = bl.D(45)
    assert all(r["usage_date"] < D0 for r in out)
    assert not any(r["usage_date"] == d45 for r in out)  # excluded at window=30
    out_90 = dbutil.rows(qid, 90)
    assert any(r["usage_date"] == d45 for r in out_90)  # included at window=90
    assert not any(r["usage_date"] == D0 for r in out_90)

    # worst-first ordering (usage_date DESC, cloud, currency_code -- NULLS LAST, matching this
    # table's own observed null ordering).
    assert _meta(qid)["order_by"] == "u.usage_date DESC, u.cloud, p.currency_code"

    def sort_key(r):
        return (-r["usage_date"].toordinal(), r["cloud"], _asc_key(r["currency_code"]))

    expected_order = sorted(out, key=sort_key)
    assert _grain_keys(out, grains) == _grain_keys(expected_order, grains)


# -------------------------------------------------------------------------------------------
# cost_dollarized_by_sku_day -- grain [workspace_id, usage_date, cloud, sku_name,
# billing_origin_product, usage_type, usage_unit, currency_code] (P4-47 added workspace_id, so the
# Cost tab's own $ tiles obey the workspace/env filter); inventory (no status); order_by =
# usage_date DESC, cloud, sku_name. P4-47 also added usage_unit to the price join (previously
# sku_name + cloud alone) -- a numeric no-op on this fixture (no sku_name+cloud here carries two
# usage_units, see the module docstring), proven correct instead by the same
# current-vs-expired/no-fan-out mechanism every other id in this batch already goes through.
# -------------------------------------------------------------------------------------------
def test_cost_dollarized_by_sku_day():
    qid = "cost_dollarized_by_sku_day"
    grains = _grains()[qid]
    out = dbutil.rows(qid, 30)
    assert out
    keys = _grain_keys(out, grains)
    assert len(keys) == len(set(keys)), f"{qid}: duplicate grain rows at window_days=30"

    # current-vs-expired, no fan-out: D(1) bl_JOBS_COMPUTE_CLASSIC/JOBS row (250 DBU alone on that
    # day -- bl_u_job_crit_r1+r2, both workspace_id=WS_PROD).
    d1 = bl.D(1)
    list_rate = _current_rate("bl_JOBS_COMPUTE_CLASSIC", "aws", "DBU", "effective_list")
    row = next(
        r for r in out
        if r["usage_date"] == d1 and r["sku_name"] == "bl_JOBS_COMPUTE_CLASSIC" and r["billing_origin_product"] == "JOBS"
    )
    assert abs(row["net_usage_quantity"] - 250.0) < 1e-6
    assert abs(row["net_list_cost"] - 250.0 * list_rate) < 1e-6
    assert row["currency_code"] == "USD"
    # P4-47: workspace_id flows through -- bl_u_job_crit_r1/r2 are both written under WS_PROD.
    assert row["workspace_id"] == bl.WS_PROD

    # NULL-cost row for the unpriced SKU (bl_SKU_NO_PRICE via bl_u_job_ok, same day) -- a genuine
    # pricing-coverage gap, never a real $0.
    unpriced = next(r for r in out if r["usage_date"] == d1 and r["sku_name"] == "bl_SKU_NO_PRICE")
    assert unpriced["net_list_cost"] is None
    assert unpriced["currency_code"] is None
    assert abs(unpriced["net_usage_quantity"] - 20.0) < 1e-6
    assert unpriced["price_basis"] == "unpriced"

    # NULL-cost row for the FREE_USAGE SKU (bl_u_free1, SEC M, D(11)) -- a real $0, told apart from
    # the coverage gap above only by price_basis (both leave net_list_cost NULL, same bare LEFT
    # JOIN-to-nothing shape).
    d11 = bl.D(11)
    free_row = next(r for r in out if r["usage_date"] == d11 and r["sku_name"] == "bl_GENIE_FREE_USAGE")
    assert free_row["net_list_cost"] is None
    assert free_row["price_basis"] == "free"
    assert abs(free_row["net_usage_quantity"] - 25.0) < 1e-6

    # a second, isolated day proves the join re-derives the rate per row, not a cached constant:
    # D(20) bl_JOBS_COMPUTE_CLASSIC/JOBS (bl_u_win_d20, 7 DBU).
    d20 = bl.D(20)
    row_d20 = next(
        r for r in out
        if r["usage_date"] == d20 and r["sku_name"] == "bl_JOBS_COMPUTE_CLASSIC" and r["billing_origin_product"] == "JOBS"
    )
    assert abs(row_d20["net_usage_quantity"] - 7.0) < 1e-6
    assert abs(row_d20["net_list_cost"] - 7.0 * list_rate) < 1e-6

    # worst-first ordering (here just usage_date DESC, cloud, sku_name -- no status to rank).
    assert _meta(qid)["order_by"] == "u.usage_date DESC, u.cloud, u.sku_name"

    def sort_key(r):
        return (-r["usage_date"].toordinal(), r["cloud"], r["sku_name"])

    expected_order = sorted(out, key=sort_key)
    assert _grain_keys(out, grains) == _grain_keys(expected_order, grains)


# -------------------------------------------------------------------------------------------
# cost_serving_mode_by_endpoint -- grain [usage_date, endpoint_id, cloud, sku_name, usage_type,
# usage_unit, serving_type, is_launch_sku, endpoint_name]; warn_endpoint_usd_per_day=50,
# crit_endpoint_usd_per_day=250 (own meta.params -- SAME names as cost_vector_search_spend's
# params but DIFFERENT defaults); order_by = net_list_cost DESC NULLS LAST; empty_if: no_activity.
# T-73 upstream fix: usage_date is now in the SELECT/GROUP BY (it used to be missing entirely, so
# the WARN/CRITICAL bands compared a whole-window total against a per-day threshold). Every
# billing.py MODEL_SERVING row below lands on the SAME single usage_date (D(4)), so this fixture's
# own dollar/quantity totals are numerically unchanged by the fix -- only the grain and the new
# usage_date column are new; the fix's effect (a multi-day endpoint no longer flagged on its
# window-wide sum) is not re-engineered here, per billing.py's own module docstring (T-09 owns
# magnitude engineering for this id's bands, not T-13).
# -------------------------------------------------------------------------------------------
def test_cost_serving_mode_by_endpoint():
    qid = "cost_serving_mode_by_endpoint"
    grains = _grains()[qid]
    out = dbutil.rows(qid, 30)
    assert out
    keys = _grain_keys(out, grains)
    assert len(keys) == len(set(keys)), f"{qid}: duplicate grain rows at window_days=30"

    params = _params(qid)
    assert params["warn_endpoint_usd_per_day"] == 50
    assert params["crit_endpoint_usd_per_day"] == 250

    bl_out = {(r["endpoint_id"], r["sku_name"], r["usage_type"]): r for r in out if str(r["endpoint_id"]).startswith("bl_")}

    # every record id this test relies on below genuinely exists in the raw parquet, and each is
    # checked against its own known qty (record id -> (endpoint key, expected net_usage_quantity)).
    anchor_rows = {
        "bl_u_ep_launch": (("bl_ep_launch", "bl_SERVERLESS_REAL_TIME_INFERENCE_LAUNCH", "COMPUTE_TIME"), 30.0),
        "bl_u_ep_normal": (("bl_ep_normal", "bl_MODEL_SERVING_STANDARD", "COMPUTE_TIME"), 20.0),
        "bl_u_genai_answer": (("bl_ep_genai", "bl_MODEL_SERVING_STANDARD", "ANSWER"), 5.0),
    }
    for rid, (endpoint_key, qty) in anchor_rows.items():
        assert _raw_row_exists("billing__usage", f"record_id = '{rid}'")
        assert abs(bl_out[endpoint_key]["net_usage_quantity"] - qty) < 1e-6, rid
        assert bl_out[endpoint_key]["usage_date"] == bl.D(4), f"{rid}: all SEC D rows land on D(4)"

    launch = bl_out[("bl_ep_launch", "bl_SERVERLESS_REAL_TIME_INFERENCE_LAUNCH", "COMPUTE_TIME")]
    assert launch["is_launch_sku"] is True
    rate = _current_rate("bl_SERVERLESS_REAL_TIME_INFERENCE_LAUNCH", "aws", "DBU", "effective_list")
    assert abs(launch["net_list_cost"] - 30.0 * rate) < 1e-6
    assert launch["status"] == "OK"

    normal = bl_out[("bl_ep_normal", "bl_MODEL_SERVING_STANDARD", "COMPUTE_TIME")]
    assert normal["is_launch_sku"] is False
    rate = _current_rate("bl_MODEL_SERVING_STANDARD", "aws", "DBU", "effective_list")
    assert abs(normal["net_list_cost"] - 20.0 * rate) < 1e-6
    assert normal["status"] == "OK"

    # WARN: bl_ep_genai / bl_MODEL_SERVING_GPU / GPU_TIME, 40 DBU at the current GPU rate.
    assert _raw_row_exists("billing__usage", "record_id = 'bl_u_genai_gpu'")
    gpu = bl_out[("bl_ep_genai", "bl_MODEL_SERVING_GPU", "GPU_TIME")]
    rate = _current_rate("bl_MODEL_SERVING_GPU", "aws", "DBU", "effective_list")
    assert abs(gpu["net_list_cost"] - 40.0 * rate) < 1e-6
    assert gpu["status"] == "WARN"
    # if the join had used the EXPIRED rate (0.6x) instead, net_list_cost would be 96.0, still
    # WARN by band but numerically wrong -- the exact-value assertion above is what actually bites.

    # endpoint_name is a resource name and is never masked.
    raw_name = _raw_endpoint_name("bl_u_ep_launch")
    assert launch["endpoint_name"] == raw_name

    # band coverage on this fixture: OK and WARN are reached. CRITICAL is a one-line FIXTURE GAP
    # in billing.py (T-09), not a structural impossibility (review round 2, item 10): this id's
    # own max net_list_cost in billing.py's rows is 160.0 (bl_ep_genai/GPU_TIME), short of
    # crit_endpoint_usd_per_day=250 by a single higher-rate or higher-quantity MODEL_SERVING row.
    # NOT_ASSESSED is ALSO a fixture gap (bl_SKU_NO_PRICE is billing_origin_product='JOBS', so it
    # never reaches this id's own filter -- billing.py has no unpriced MODEL_SERVING SKU) AND
    # demonstrably reachable: the live build already has 6 NOT_ASSESSED rows here from
    # serving_storage.py's (T-22) unpriced ss_MODEL_SERVING_STANDARD, confirmed against
    # tests/db_audit_test.duckdb -- not asserted on here (DEC-15: this module's assertions stay
    # scoped to billing.py's own bl_-prefixed ids), recorded with the owner named in
    # tasks/T-13-cost-priced-tests.md's Hand-off notes instead.
    bl_statuses = {r["status"] for r in bl_out.values()}
    assert bl_statuses == {"OK", "WARN"}

    # worst-first ordering, over the full result set (not just bl_-prefixed rows).
    assert _meta(qid)["order_by"] == "net_list_cost DESC NULLS LAST"
    expected_order = sorted(out, key=lambda r: (0 if r["net_list_cost"] is not None else 1, -(r["net_list_cost"] or 0.0)))
    assert _grain_keys(out, grains) == _grain_keys(expected_order, grains)


# -------------------------------------------------------------------------------------------
# cost_vector_search_spend -- grain [usage_date, cloud, sku_name, usage_type, usage_unit,
# endpoint_name, endpoint_id, currency_code]; warn_endpoint_usd_per_day=25,
# crit_endpoint_usd_per_day=100 (own meta.params, different defaults than the endpoint id above);
# order_by = net_list_cost DESC NULLS LAST; empty_if: no_activity, ingestion_lag.
# -------------------------------------------------------------------------------------------
def test_cost_vector_search_spend():
    qid = "cost_vector_search_spend"
    grains = _grains()[qid]
    out = dbutil.rows(qid, 30)
    assert out
    keys = _grain_keys(out, grains)
    assert len(keys) == len(set(keys)), f"{qid}: duplicate grain rows at window_days=30"

    params = _params(qid)
    assert params["warn_endpoint_usd_per_day"] == 25
    assert params["crit_endpoint_usd_per_day"] == 100

    assert _raw_row_exists("billing__usage", "record_id = 'bl_u_vs_serving'")
    assert _raw_row_exists("billing__usage", "record_id = 'bl_u_vs_storage'")
    bl_out = {r["usage_type"]: r for r in out if str(r["endpoint_id"]) == "bl_vs_ep1"}

    serving = bl_out["COMPUTE_TIME"]
    rate = _current_rate("bl_VECTOR_SEARCH_SERVING", "aws", "DBU", "effective_list")
    assert abs(serving["net_usage_quantity"] - 18.0) < 1e-6
    assert abs(serving["net_list_cost"] - 18.0 * rate) < 1e-6
    assert serving["status"] == "OK"

    storage = bl_out["STORAGE_SPACE"]
    rate = _current_rate("bl_VECTOR_SEARCH_STORAGE", "aws", "GB", "effective_list")
    assert abs(storage["net_usage_quantity"] - 3.5) < 1e-6
    assert abs(storage["net_list_cost"] - 3.5 * rate) < 1e-6
    assert storage["status"] == "OK"
    # STORAGE_SPACE (GB) and COMPUTE_TIME (DBU) both dollarize correctly against DIFFERENT
    # list_prices rows here: cost_vector_search_spend.sql's join now keys on sku_name + cloud +
    # usage_unit, same as cost_cloud_infra / cost_actual_vs_list_by_sku / cost_dollarized_by_sku_day
    # (cost_serving_mode_by_endpoint matches too), so a sku_name+cloud priced in two units cannot
    # fan out here regardless of whether the fixture happens to have one.

    raw_name = _raw_endpoint_name("bl_u_vs_serving")
    assert serving["endpoint_name"] == raw_name  # a resource name, never masked

    # band coverage: only OK is reached by billing.py's own VECTOR_SEARCH rows in this fixture.
    # WARN and CRITICAL are one-line FIXTURE GAPS in billing.py (T-09), not structural
    # impossibilities (review round 2, item 10): this id's max net_list_cost in billing.py's rows
    # is 1.8, far short of warn_endpoint_usd_per_day=25 / crit_endpoint_usd_per_day=100. This id's
    # own NOT_ASSESSED is ALSO a fixture gap (billing.py has no unpriced VECTOR_SEARCH SKU) AND
    # demonstrably reachable: the live build already has 1 NOT_ASSESSED row here from
    # serving_storage.py's (T-22) unpriced ss_VECTOR_SEARCH_SERVING, confirmed against
    # tests/db_audit_test.duckdb -- not asserted on here (DEC-15 scoping, as above), recorded with
    # the owner named in tasks/T-13-cost-priced-tests.md's Hand-off notes instead.
    bl_statuses = {r["status"] for r in bl_out.values()}
    assert bl_statuses == {"OK"}

    assert _meta(qid)["order_by"] == "net_list_cost DESC NULLS LAST"
    expected_order = sorted(out, key=lambda r: (0 if r["net_list_cost"] is not None else 1, -(r["net_list_cost"] or 0.0)))
    assert _grain_keys(out, grains) == _grain_keys(expected_order, grains)


# -------------------------------------------------------------------------------------------
# cost_account_prices_raw -- grain [sku_name, cloud, currency_code, price_start_time]; windowless;
# order_by = sku_name, cloud, currency_code, price_start_time.
# -------------------------------------------------------------------------------------------
def test_cost_account_prices_raw():
    qid = "cost_account_prices_raw"
    grains = _grains()[qid]
    out = dbutil.rows(qid, 0)
    assert out
    keys = _grain_keys(out, grains)
    assert len(keys) == len(set(keys)), f"{qid}: duplicate grain rows at window_days=0"

    rows = sorted((r for r in out if r["sku_name"] == "bl_JOBS_COMPUTE_CLASSIC"), key=lambda r: r["price_start_time"])
    assert len(rows) == 2, rows  # exactly current + expired, no more
    expired_row, current_row = rows
    assert expired_row["price_end_time"] is not None
    assert current_row["price_end_time"] is None
    default_current = _current_rate("bl_JOBS_COMPUTE_CLASSIC", "aws", "DBU", "default")
    default_expired = _rate_row("bl_JOBS_COMPUTE_CLASSIC", "aws", "DBU", current=False)[0]
    assert abs(float(current_row["pricing_default"]) - default_current) < 1e-9
    assert abs(float(expired_row["pricing_default"]) - default_expired) < 1e-9
    assert float(current_row["pricing_default"]) != float(expired_row["pricing_default"])

    assert _meta(qid)["order_by"] == "sku_name, cloud, currency_code, price_start_time"
    expected_order = sorted(out, key=lambda r: (r["sku_name"], r["cloud"], _asc_key(r["currency_code"]), r["price_start_time"]))
    assert _grain_keys(out, grains) == _grain_keys(expected_order, grains)


# -------------------------------------------------------------------------------------------
# pricing_list_prices_raw -- same grain/order as cost_account_prices_raw; also carries the full
# pricing struct (effective_list, promotional) serialized as JSON strings.
# -------------------------------------------------------------------------------------------
def test_pricing_list_prices_raw():
    qid = "pricing_list_prices_raw"
    grains = _grains()[qid]
    out = dbutil.rows(qid, 0)
    assert out
    keys = _grain_keys(out, grains)
    assert len(keys) == len(set(keys)), f"{qid}: duplicate grain rows at window_days=0"

    rows = sorted((r for r in out if r["sku_name"] == "bl_JOBS_COMPUTE_CLASSIC"), key=lambda r: r["price_start_time"])
    assert len(rows) == 2
    expired_row, current_row = rows
    assert expired_row["price_end_time"] is not None
    assert current_row["price_end_time"] is None

    list_current = _current_rate("bl_JOBS_COMPUTE_CLASSIC", "aws", "DBU", "effective_list")
    list_expired = _rate_row("bl_JOBS_COMPUTE_CLASSIC", "aws", "DBU", current=False)[1]

    def _parse_default(json_like: str) -> float:
        # DuckDB's CAST(STRUCT AS STRING) renders Python-dict-like text with single quotes
        # (`{'default': 0.22}`), not double-quoted JSON -- this regex is specific to that
        # rendering (review round 2, item 15). On a Databricks target, CAST(... AS STRING) on
        # this same struct produces real JSON with double quotes, so this parser is test-target-
        # specific; harmless today since this suite only ever runs against target `test`
        # (DuckDB), but noted so a future dev target run isn't surprised by it.
        m = re.search(r"'default':\s*([0-9.]+)", json_like)
        assert m, json_like
        return float(m.group(1))

    assert abs(_parse_default(current_row["pricing_effective_list_json"]) - list_current) < 1e-9
    assert abs(_parse_default(expired_row["pricing_effective_list_json"]) - list_expired) < 1e-9
    assert list_current != list_expired

    assert _meta(qid)["order_by"] == "sku_name, cloud, currency_code, price_start_time"
    expected_order = sorted(out, key=lambda r: (r["sku_name"], r["cloud"], _asc_key(r["currency_code"]), r["price_start_time"]))
    assert _grain_keys(out, grains) == _grain_keys(expected_order, grains)


# -------------------------------------------------------------------------------------------
# cost_dbsql_allocation_gap -- grain [source_kind, workspace_id, warehouse_id, usage_date, cloud,
# billing_origin_product, usage_unit] (workspace_id/warehouse_id added so the raw-vs-attributed gap
# can be narrowed to the actual shared warehouse driving it); UNION ALL of raw
# (system.billing.usage) and attributed (system.billing.attributed_usage) DBSQL DBU totals; no
# status; order_by = usage_date DESC, cloud, billing_origin_product, source_kind (unchanged --
# workspace_id/warehouse_id narrow the grain without joining the ordering).
# -------------------------------------------------------------------------------------------
def test_cost_dbsql_allocation_gap():
    qid = "cost_dbsql_allocation_gap"
    grains = _grains()[qid]
    out = dbutil.rows(qid, 30)
    assert out
    keys = _grain_keys(out, grains)
    assert len(keys) == len(set(keys)), f"{qid}: duplicate grain rows at window_days=30"

    assert {r["source_kind"] for r in out} == {"raw", "attributed"}

    # D(9): bl_u_sql_raw1 (raw, 80 DBU) vs bl_a_att1 (attributed, 55 DBU) -- both proven to exist
    # independent of any window/model filter. Both are WS_PROD/bl_wh_gap, and no other fixture
    # builder writes a SQL/DBU billing__usage or billing__attributed_usage row on D(9) (checked
    # against every builder's own fixture file), so `next()` below still lands on exactly one row
    # per source_kind despite the wider grain.
    assert _raw_row_exists("billing__usage", "record_id = 'bl_u_sql_raw1'")
    assert _raw_row_exists("billing__attributed_usage", "record_id = 'bl_a_att1'")
    d9 = bl.D(9)
    raw_row = next(r for r in out if r["source_kind"] == "raw" and r["usage_date"] == d9)
    att_row = next(r for r in out if r["source_kind"] == "attributed" and r["usage_date"] == d9)
    assert raw_row["cloud"] == att_row["cloud"] == "aws"
    assert raw_row["billing_origin_product"] == att_row["billing_origin_product"] == "SQL"
    assert raw_row["workspace_id"] == att_row["workspace_id"] == bl.WS_PROD
    assert raw_row["warehouse_id"] == att_row["warehouse_id"] == "bl_wh_gap"

    # expected_raw deliberately has NO sku_name filter (review round 2, item 4): the model's own
    # raw branch (cost_dbsql_allocation_gap.sql) filters only
    # `billing_origin_product = 'SQL' AND upper(usage_unit) = 'DBU'` -- this grain has no
    # identifier column, and lakeflow.py already writes its own lf_JOBS_COMPUTE rows on this same
    # date (billing_origin_product='JOBS', so they miss today's filter, but nothing pins that),
    # so a sku_name filter here would silently narrow the "independent" recomputation to less
    # than what the model actually sums, defeating the point of recomputing it independently.
    expected_raw = dbutil.usage_sum(
        f"billing_origin_product = 'SQL' AND upper(usage_unit) = 'DBU' AND cloud = '{raw_row['cloud']}' "
        f"AND usage_date = DATE '{d9.isoformat()}'"
    )
    expected_att = _attributed_sum(
        f"billing_origin_product = 'SQL' AND upper(usage_unit) = 'DBU' AND cloud = '{att_row['cloud']}' "
        f"AND usage_date = DATE '{d9.isoformat()}'"
    )
    assert abs(raw_row["net_usage_quantity"] - expected_raw) < 1e-6
    assert abs(att_row["net_usage_quantity"] - expected_att) < 1e-6
    gap = raw_row["net_usage_quantity"] - att_row["net_usage_quantity"]
    assert gap != 0  # raw != attributed -- proves UNION ALL (two independently-aggregated
    #                  source_kind rows), not a JOIN that would net them into one row. (The
    #                  attributed side needs no such generalization: only billing.py writes
    #                  billing__attributed_usage, so bl_a_att1 is the only contributor here.)

    # bl_a_att_nonmatch (billing_origin_product='JOBS') must never leak into the attributed/SQL
    # total for its own date -- both branches filter billing_origin_product='SQL' independently.
    assert _raw_row_exists("billing__attributed_usage", "record_id = 'bl_a_att_nonmatch'")
    con = duckdb.connect()
    try:
        pattern = (PARQUET_DIR / "billing__attributed_usage" / "*.parquet").as_posix()
        nonmatch_date = con.execute(
            f"SELECT usage_date FROM read_parquet('{pattern}', union_by_name=true) WHERE record_id = 'bl_a_att_nonmatch'"
        ).fetchone()[0]
    finally:
        con.close()
    assert nonmatch_date == d9  # same date as bl_a_att1 -- if the filter leaked, att_row would be 56.0, not 55.0

    # window boundary: D(9) (billing.py's own attributed date) is outside the 7-day window
    # (D0-7 = D(7)) -- bl_a_att1's own D(9) row disappears at window=7. P4-T-ROLL (tasks/
    # P4-T-SPEC.md section 7.6): narrowed from "no attributed row at all" to "none dated D(9)",
    # since tests/fixtures/tagworld.py also writes attributed rows (its own D(1), well inside a
    # 7-day window) -- this still proves the boundary bl.D(9) sits on, just no longer assumes this
    # builder is the only one with attributed rows in the fixture set.
    out_7 = dbutil.rows(qid, 7)
    assert not any(r["source_kind"] == "attributed" and r["usage_date"] == d9 for r in out_7)
    assert dbutil.rows(qid, 0) == []

    assert _meta(qid)["order_by"] == "usage_date DESC, cloud, billing_origin_product, source_kind"

    def sort_key(r):
        return (-r["usage_date"].toordinal(), r["cloud"], r["billing_origin_product"], r["source_kind"])

    expected_order = sorted(out, key=sort_key)
    assert _grain_keys(out, grains) == _grain_keys(expected_order, grains)


# -------------------------------------------------------------------------------------------
# cost_workspace_names -- grain [workspace_id]; windowless; order_by = workspace_name,
# workspace_id. tests/fixtures/drilldown.py (T-07) also writes system.access.workspaces_latest
# rows, so the full result set is not exactly the 3 billing fixture workspaces -- assert presence
# and the full ordering, never an exact row count (task's own Inputs section).
# -------------------------------------------------------------------------------------------
def test_cost_workspace_names():
    qid = "cost_workspace_names"
    grains = _grains()[qid]
    out = dbutil.rows(qid, 0)
    assert out
    keys = _grain_keys(out, grains)
    assert len(keys) == len(set(keys)), f"{qid}: duplicate grain rows at window_days=0"

    by_id = {r["workspace_id"]: r for r in out}
    for ws_id, name in ((bl.WS_PROD, "acme-prod"), (bl.WS_DEV, "acme-dev"), (bl.WS_UAT, "acme-uat")):
        assert ws_id in by_id, f"missing workspace {ws_id} ({name})"
        assert by_id[ws_id]["workspace_name"] == name
        assert by_id[ws_id]["status"] == "ACTIVE"

    assert _meta(qid)["order_by"] == "workspace_name, workspace_id"
    expected_order = sorted(out, key=lambda r: (r["workspace_name"], r["workspace_id"]))
    assert [r["workspace_id"] for r in out] == [r["workspace_id"] for r in expected_order]

    for w in WINDOWS:
        assert dbutil.rows(qid, w) == [], f"{qid}: windowless id must be empty at window_days={w}"
