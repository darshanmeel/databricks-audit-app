"""tests/test_findings/test_compute_activity.py -- T-14.

Proves, against tests/fixtures/compute.py's own rows and only those rows, that each of the 5
built findings.f_<query_id> tables (batch B1, PLAN.md 7.2) has the right grain, the right
window_days behaviour at 7/30/90 (and [] at 0), a reachable WARN and CRITICAL where the query has
thresholds, a NOT_ASSESSED path where the fixture put one, and that net_dbus/est_usd_list on the
three cost-joined ids equal an independently-computed rollup from the raw billing.usage /
billing.list_prices parquet (never a hard-coded total, never a private constant borrowed from
another builder). Per tests/test_findings/README.md's checklist and this task's own Inputs/Steps.

Expectations are computed from tests/fixtures/compute.py's own literal ids/timestamps (imported
directly as `cp`) or from the raw fixture parquet -- the same style tests/test_findings/
test_performance.py, test_governance_access.py and test_overview_spend_estimate.py use.
`_cost_rollup()` below reproduces the model's own cost_rollup CTE (`usage_end_time >=
price_start_time AND (price_end_time IS NULL OR usage_end_time < price_end_time)`,
`pricing.effective_list.default` -- DEC-66.1's one dollar basis, the effective post-promotion
list price) straight from tests/fixtures/parquet/billing__usage and billing__list_prices, per
tests/test_findings/test_overview_spend_estimate.py:42-51's precedent -- NOT
tests/fixtures/billing.py's own `_PRICED_SKUS` constant (review fix item 2: that constant is
private to billing.py, is keyed positionally on "field 3 happens to be the current rate" with
nothing enforcing it, and reading it would make this file depend on billing.py's internals in
exactly the way this builder's own module docstring says every builder's fixture stays independent
of another builder's code).

None of these 5 query bodies select workspace_id in their own output (warehouse_id / cluster_id
are globally-unique GUIDs, so workspace_id is deliberately dropped from both the grain and the
cost-rollup join -- see each body's caveats), so dbutil.rows() is never called with
workspace_ids here; there is no such column to filter on.

sql_warehouse_events_activity's staleness bands are tested ONLY at window_days=30 (20/10/2 days
before AS_OF span 7-vs-14-vs-30 boundaries that do not all fit inside a 7-day window), matching
this task's own Inputs section.

`test_grain_uniqueness_all_ids` and `test_status_enum_for_all_ids` are the only two assertions in
this file that do NOT filter their rows down to this builder's own `cp_`-prefixed ids -- this is
intentional (review fix item 8's own note): they are meant to catch a bad row anywhere in
compute__node_timeline / compute__clusters / compute__warehouse_events, including one a future
builder introduces, not just this fixture's own rows. Both now also assert the result set is
non-empty, since neither previously would have failed on a query that silently returned nothing.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import duckdb
import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR / "fixtures"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import dbutil  # noqa: E402
import ddl  # noqa: E402
import jinja_stub  # noqa: E402
import compute as cp  # noqa: E402
from app.core.registry import by_id  # noqa: E402
from tools.generate_models import render_model  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "compute_activity.yml"
PARQUET_DIR = ROOT / "tests" / "fixtures" / "parquet"
WINDOWS = (7, 30, 90)
STATUS_VALUES = {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}

ALL_IDS = [
    "compute_warehouse_idle_gaps",
    "compute_warehouse_autoscale_churn",
    "sql_warehouse_events_activity",
    "compute_idle_node_ratio",
    "node_timeline_utilization",
]

# Window-boundary id families (module docstring in tests/fixtures/compute.py: SEC D / SEC F).
WH_WIN = {suffix: f"cp_wh_win_{suffix}" for suffix in ("t7", "t30", "t90", "ttoday", "told90")}
CL_WIN = {suffix: f"cp_cl_win_{suffix}" for suffix in ("t7", "t30", "t90", "ttoday", "told90")}


def _expect_present(win_map):
    """Lower-bound-only window semantics (no upper-bound exclusion, unlike billing.usage): t7 is
    inside every window; t30 joins at window>=30; t90 joins at window=90; ttoday (AS_OF's own
    calendar day) is inside every window; told90 (older than even 90d) is inside none."""
    return {
        7: {win_map["t7"], win_map["ttoday"]},
        30: {win_map["t7"], win_map["t30"], win_map["ttoday"]},
        90: {win_map["t7"], win_map["t30"], win_map["t90"], win_map["ttoday"]},
    }


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _cost_rollup(resource_col, resource_id, window_days):
    """Independent re-derivation of a model's cost_rollup CTE (net_dbus, est_usd_list,
    price_basis), straight from the raw billing.usage / billing.list_prices parquet -- the exact
    date-range price-join predicate compute_warehouse_idle_gaps / compute_warehouse_autoscale_churn
    / compute_idle_node_ratio all use (`u.usage_end_time >= p.price_start_time AND
    (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)`,
    `CAST(pricing.effective_list.default AS DOUBLE)` -- DEC-66.1's one dollar basis), never a rate
    constant copied from another builder (review fix item 2). Mirrors the model's own current-day
    exclusion (usage_date >= today-window_days AND usage_date < today) and its price_basis CASE
    (free/priced/unpriced)."""
    usage_glob = (PARQUET_DIR / "billing__usage" / "*.parquet").as_posix()
    price_glob = (PARQUET_DIR / "billing__list_prices" / "*.parquet").as_posix()
    lower = (cp.D0 - timedelta(days=window_days)).isoformat()
    upper = cp.D0.isoformat()
    con = duckdb.connect()
    try:
        sql = f"""
            WITH price AS (
                SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
                       CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
                FROM read_parquet('{price_glob}', union_by_name=true)
            )
            SELECT SUM(u.usage_quantity)                            AS net_dbus,
                   SUM(u.usage_quantity * COALESCE(p.list_rate, 0))  AS est_usd_list,
                   CASE
                     WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                                   THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
                     WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
                     ELSE 'priced'
                   END                                                AS price_basis
            FROM read_parquet('{usage_glob}', union_by_name=true) u
            LEFT JOIN price p
              ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
             AND u.usage_end_time >= p.price_start_time
             AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
            WHERE upper(u.usage_unit) = 'DBU'
              AND u.usage_metadata.{resource_col} = '{resource_id}'
              AND u.usage_date >= DATE '{lower}' AND u.usage_date < DATE '{upper}'
        """
        net_dbus, est_usd_list, price_basis = con.execute(sql).fetchone()
        return (net_dbus or 0.0), (est_usd_list or 0.0), (price_basis or "priced")
    finally:
        con.close()


def _raw_row_exists(schema_table, where):
    """True if at least one row matching `where` exists anywhere in the raw
    tests/fixtures/parquet/<schema_table>/*.parquet (every builder's slice, unioned by name) --
    used to prove a fixture row genuinely exists independent of any window filter (review fix
    item 1's "lower stakes" companion: an absent row and a present-but-filtered-out row look
    identical to a test that only ever checks non-membership)."""
    pattern = (PARQUET_DIR / schema_table / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        sql = f"SELECT COUNT(*) FROM read_parquet('{pattern}', union_by_name=true) WHERE {where}"
        return con.execute(sql).fetchone()[0] > 0
    finally:
        con.close()


# -------------------------------------------------------------------------------------------
# Generic checks over all 5 ids. Deliberately NOT scoped to cp_-prefixed ids -- see module
# docstring.
# -------------------------------------------------------------------------------------------
def test_grain_uniqueness_all_ids():
    grains = _grains()
    for qid in ALL_IDS:
        cols = grains[qid]
        out = dbutil.rows(qid, 30)
        assert out, qid  # a vacuously-true uniqueness check on an empty result proves nothing
        keys = [tuple(r[c] for c in cols) for r in out]
        assert len(keys) == len(set(keys)), f"{qid}: duplicate grain rows at window_days=30"


def test_window_days_zero_is_empty_for_all_ids():
    for qid in ALL_IDS:
        assert dbutil.rows(qid, 0) == [], f"{qid}: window_days=0 must be empty"


def test_status_enum_for_all_ids():
    for qid in ALL_IDS:
        for w in WINDOWS:
            out = dbutil.rows(qid, w)
            assert out, (qid, w)  # same non-emptiness guard as the grain check above
            for r in out:
                assert r["status"] in STATUS_VALUES, f"{qid} w={w}: bad status {r['status']!r}"


def test_window_boundary_told90_rows_exist_in_raw_parquet():
    """Companion to the AS_OF-date anchor checks below (review fix item 1, "lower stakes" half):
    `_expect_present` already asserts cp_wh_win_told90 / cp_cl_win_told90 are ABSENT from every
    window (7/30/90) -- which would also be true if compute.py's told90 rows were silently
    deleted, since absence proves nothing either way. Confirm directly against the raw parquet,
    bypassing every window filter, that both rows genuinely exist."""
    assert _raw_row_exists("compute__warehouse_events", "warehouse_id = 'cp_wh_win_told90'")
    assert _raw_row_exists("compute__node_timeline", "cluster_id = 'cp_cl_win_told90'")


# -------------------------------------------------------------------------------------------
# compute_warehouse_idle_gaps -- warn_idle_hours=4, crit_idle_hours=24; grain [warehouse_id].
# -------------------------------------------------------------------------------------------
def test_compute_warehouse_idle_gaps():
    out = dbutil.rows("compute_warehouse_idle_gaps", 30)
    by_wh = {r["warehouse_id"]: r for r in out
             if r["warehouse_id"] in ("cp_wh_idle_crit", "cp_wh_idle_warn", "cp_wh_single")}

    crit = by_wh["cp_wh_idle_crit"]
    assert crit["status"] == "CRITICAL"
    assert crit["max_running_gap_seconds"] == 30 * 3600

    warn = by_wh["cp_wh_idle_warn"]
    assert warn["status"] == "WARN"
    assert warn["max_running_gap_seconds"] == 6 * 3600

    not_assessed = by_wh["cp_wh_single"]
    assert not_assessed["status"] == "NOT_ASSESSED"
    assert not_assessed["max_running_gap_seconds"] is None

    # worst-first: order_by is CASE status ... , max_running_gap_seconds DESC -- CRITICAL before
    # WARN among these two known rows.
    seq = [r["status"] for r in out if r["warehouse_id"] in ("cp_wh_idle_crit", "cp_wh_idle_warn")]
    assert seq == ["CRITICAL", "WARN"], seq

    # cost rollup: net_dbus/est_usd_list/price_basis independently re-derived from the raw parquet,
    # and the AS_OF-date row (999 DBU) excluded at every window.
    expected_net = expected_est = expected_basis = None
    r = None
    for w in WINDOWS:
        expected_net, expected_est, expected_basis = _cost_rollup("warehouse_id", "cp_wh_idle_crit", w)
        assert expected_net == 40.0, (w, expected_net)  # sanity: the D(0) row really is excluded
        row = dbutil.rows("compute_warehouse_idle_gaps", w)
        r = [x for x in row if x["warehouse_id"] == "cp_wh_idle_crit"][0]
        assert r["net_dbus"] == expected_net, (w, r["net_dbus"], expected_net)
        assert abs(r["est_usd_list"] - expected_est) < 1e-6, (w, r["est_usd_list"], expected_est)
        assert r["price_basis"] == expected_basis, (w, r["price_basis"], expected_basis)

    # Anchor (review fix item 1): prove the AS_OF-date row genuinely exists in the raw parquet --
    # closes the gap where silently deleting compute.py's cp_u_wh_crit_d0 row would leave every
    # assertion above still green, since _cost_rollup already mirrors the model's own exclusion
    # clause. An UNWINDOWED sum (no usage_date filter at all) must include the excluded 999-DBU
    # row that the windowed sum above does not. If cp_u_wh_crit_d0 were removed, incl_today would
    # equal expected_net (40.0), not expected_net + 999.0, and this assert would fail.
    incl_today = dbutil.usage_sum("usage_metadata.warehouse_id = 'cp_wh_idle_crit'")
    assert incl_today == expected_net + 999.0, incl_today  # the D(0) row exists in the parquet...
    assert r["net_dbus"] < incl_today  # ...and the model's own filter drops it

    # window-boundary series: this query carries each warehouse's state in from its last event
    # before the window. t7/t30/t90/told90 are each one RUNNING event that never stops, so every
    # window sees all four as running. ttoday's event is today, which the query never reads.
    everyone = {WH_WIN["t7"], WH_WIN["t30"], WH_WIN["t90"], WH_WIN["told90"]}
    expect = {7: everyone, 30: everyone, 90: everyone}
    for w, expected_present in expect.items():
        out_w = dbutil.rows("compute_warehouse_idle_gaps", w)
        present = {r2["warehouse_id"] for r2 in out_w if r2["warehouse_id"] in WH_WIN.values()}
        assert present == expected_present, (w, present, expected_present)


# -------------------------------------------------------------------------------------------
# compute_warehouse_autoscale_churn -- min_observed_hours=1, warn=4/h, crit=10/h; grain
# [warehouse_id].
# -------------------------------------------------------------------------------------------
def test_compute_warehouse_autoscale_churn():
    out = dbutil.rows("compute_warehouse_autoscale_churn", 30)
    by_wh = {r["warehouse_id"]: r for r in out
             if r["warehouse_id"] in ("cp_wh_churn", "cp_wh_churn_warn", "cp_wh_churn_ok")}

    churn = by_wh["cp_wh_churn"]
    assert churn["status"] == "CRITICAL"
    assert churn["scaling_events"] == 18
    assert abs(churn["observed_hours"] - 1.5) < 1e-6

    churn_warn = by_wh["cp_wh_churn_warn"]
    assert churn_warn["status"] == "WARN"
    assert churn_warn["scaling_events"] == 9

    churn_ok = by_wh["cp_wh_churn_ok"]
    assert churn_ok["status"] == "OK"
    assert churn_ok["scaling_events"] == 3

    # NOT_ASSESSED: any single-event window warehouse has observed_hours=0 < min_observed_hours(1).
    single_event_row = [r for r in out if r["warehouse_id"] == WH_WIN["t7"]][0]
    assert single_event_row["status"] == "NOT_ASSESSED"
    assert single_event_row["observed_hours"] == 0.0

    # worst-first (review fix item 7): order_by is CASE status ... , scaling_events DESC --
    # CRITICAL, WARN, OK in that order among these three known rows.
    seq = [r["status"] for r in out
           if r["warehouse_id"] in ("cp_wh_churn", "cp_wh_churn_warn", "cp_wh_churn_ok")]
    assert seq == ["CRITICAL", "WARN", "OK"], seq

    expected_net = expected_est = expected_basis = None
    r = None
    for w in WINDOWS:
        expected_net, expected_est, expected_basis = _cost_rollup("warehouse_id", "cp_wh_churn", w)
        assert expected_net == 25.0, (w, expected_net)
        row = dbutil.rows("compute_warehouse_autoscale_churn", w)
        r = [x for x in row if x["warehouse_id"] == "cp_wh_churn"][0]
        assert r["net_dbus"] == expected_net, (w, r["net_dbus"], expected_net)
        assert abs(r["est_usd_list"] - expected_est) < 1e-6, (w, r["est_usd_list"], expected_est)
        assert r["price_basis"] == expected_basis, (w, r["price_basis"], expected_basis)

    # Anchor (review fix item 1): same unwindowed-sum proof as compute_warehouse_idle_gaps above.
    incl_today = dbutil.usage_sum("usage_metadata.warehouse_id = 'cp_wh_churn'")
    assert incl_today == expected_net + 999.0, incl_today
    assert r["net_dbus"] < incl_today

    expect = _expect_present(WH_WIN)
    for w, expected_present in expect.items():
        out_w = dbutil.rows("compute_warehouse_autoscale_churn", w)
        present = {r2["warehouse_id"] for r2 in out_w if r2["warehouse_id"] in WH_WIN.values()}
        assert present == expected_present, (w, present, expected_present)


# -------------------------------------------------------------------------------------------
# sql_warehouse_events_activity -- warn_stale_days=7, crit_stale_days=14; grain [warehouse_id,
# event_type]; status only on RUNNING/STARTING rows. Staleness bands tested at window_days=30
# only (per this task's Inputs section).
# -------------------------------------------------------------------------------------------
def test_sql_warehouse_events_activity():
    out = dbutil.rows("sql_warehouse_events_activity", 30)
    by_key = {(r["warehouse_id"], r["event_type"]): r for r in out}

    crit = by_key[("cp_wh_stale_crit", "RUNNING")]
    assert crit["status"] == "CRITICAL"
    assert crit["last_event_time"] == cp.AS_OF - timedelta(days=20)

    warn = by_key[("cp_wh_stale_warn", "STARTING")]
    assert warn["status"] == "WARN"
    assert warn["last_event_time"] == cp.AS_OF - timedelta(days=10)

    ok = by_key[("cp_wh_stale_ok", "RUNNING")]
    assert ok["status"] == "OK"
    assert ok["last_event_time"] == cp.AS_OF - timedelta(days=2)

    not_assessed = by_key[("cp_wh_idle_crit", "STOPPED")]
    assert not_assessed["status"] == "NOT_ASSESSED"  # STOPPED is never RUNNING/STARTING

    # worst-first: order_by is CASE status WHEN CRITICAL 0 WHEN WARN 1 WHEN OK 2 ELSE 3 END,
    # last_event_time ASC -- CRITICAL, WARN, OK, NOT_ASSESSED in that order among these four.
    seq = [r["status"] for r in out if (r["warehouse_id"], r["event_type"]) in (
        ("cp_wh_stale_crit", "RUNNING"), ("cp_wh_stale_warn", "STARTING"),
        ("cp_wh_stale_ok", "RUNNING"), ("cp_wh_idle_crit", "STOPPED"),
    )]
    assert seq == ["CRITICAL", "WARN", "OK", "NOT_ASSESSED"], seq

    expect = _expect_present(WH_WIN)
    for w, expected_present in expect.items():
        out_w = dbutil.rows("sql_warehouse_events_activity", w)
        present = {r["warehouse_id"] for r in out_w if r["warehouse_id"] in WH_WIN.values()}
        assert present == expected_present, (w, present, expected_present)


# -------------------------------------------------------------------------------------------
# compute_idle_node_ratio -- idle_cpu_pct=5, min_slices=60, warn=0.5, crit=0.8; grain
# [cluster_id].
# -------------------------------------------------------------------------------------------
def test_compute_idle_node_ratio():
    out = dbutil.rows("compute_idle_node_ratio", 30)
    by_cl = {r["cluster_id"]: r for r in out
             if r["cluster_id"] in ("cp_cl_idle", "cp_cl_warn", "cp_cl_busy", "cp_cl_small")}

    crit = by_cl["cp_cl_idle"]
    assert crit["status"] == "CRITICAL"
    assert crit["total_slices"] == 100
    assert crit["idle_slices"] == 95
    assert abs(crit["idle_ratio"] - 0.95) < 1e-6
    assert crit["avg_cpu_pct_all"] < 5.0  # matches the fixture fact "avg CPU below 5%"

    warn = by_cl["cp_cl_warn"]
    assert warn["status"] == "WARN"
    assert abs(warn["idle_ratio"] - 0.6) < 1e-6

    ok = by_cl["cp_cl_busy"]
    assert ok["status"] == "OK"
    assert ok["idle_ratio"] == 0.0

    not_assessed = by_cl["cp_cl_small"]
    assert not_assessed["status"] == "NOT_ASSESSED"
    assert not_assessed["total_slices"] == 10

    # cost rollup, cp_cl_idle: net_dbus/est_usd_list independently re-derived, AS_OF-date row
    # (999 DBU) excluded at every window.
    expected_net = expected_est = expected_basis = None
    row = None
    for w in WINDOWS:
        expected_net, expected_est, expected_basis = _cost_rollup("cluster_id", "cp_cl_idle", w)
        assert expected_net == 15.0, (w, expected_net)
        row = [x for x in dbutil.rows("compute_idle_node_ratio", w) if x["cluster_id"] == "cp_cl_idle"][0]
        assert row["net_dbus"] == expected_net, (w, row["net_dbus"], expected_net)
        assert abs(row["est_usd_list"] - expected_est) < 1e-6, (w, row["est_usd_list"], expected_est)
        assert row["price_basis"] == expected_basis, (w, row["price_basis"], expected_basis)

    # Anchor (review fix item 1): same unwindowed-sum proof as compute_warehouse_idle_gaps above.
    incl_today = dbutil.usage_sum("usage_metadata.cluster_id = 'cp_cl_idle'")
    assert incl_today == expected_net + 999.0, incl_today
    assert row["net_dbus"] < incl_today

    # worst-first: order_by is est_wasted_usd_list DESC. By construction (compute.py SEC K):
    # idle 15 DBU * 0.55 * 0.95 = 7.8375, warn 5 DBU * 0.55 * 0.6 = 1.65, busy 20 DBU * 0.55 * 0.0
    # = 0.0 -- strictly decreasing and monotonic with status here.
    seq = [r["status"] for r in out if r["cluster_id"] in ("cp_cl_idle", "cp_cl_warn", "cp_cl_busy")]
    assert seq == ["CRITICAL", "WARN", "OK"], seq

    expect = _expect_present(CL_WIN)
    for w, expected_present in expect.items():
        out_w = dbutil.rows("compute_idle_node_ratio", w)
        present = {r["cluster_id"] for r in out_w if r["cluster_id"] in CL_WIN.values()}
        assert present == expected_present, (w, present, expected_present)


# -------------------------------------------------------------------------------------------
# node_timeline_utilization -- min_slices=15 (config/thresholds.yml), oversized_cpu_pct=20, oversized_mem_pct=30; grain
# [cluster_id, node_type, driver]; status is OK | WARN | NOT_ASSESSED only (no CRITICAL branch).
# -------------------------------------------------------------------------------------------
def test_node_timeline_utilization():
    out = dbutil.rows("node_timeline_utilization", 30)
    by_cl = {r["cluster_id"]: r for r in out if r["cluster_id"] in ("cp_cl_idle", "cp_cl_busy", "cp_cl_small")}

    warn = by_cl["cp_cl_idle"]
    assert warn["status"] == "WARN"
    assert warn["minute_rows"] == 100
    assert warn["avg_cpu_pct"] < 20.0
    assert warn["avg_mem_pct"] < 30.0

    ok = by_cl["cp_cl_busy"]
    assert ok["status"] == "OK"
    assert ok["minute_rows"] == 100
    assert ok["avg_cpu_pct"] >= 20.0

    not_assessed = by_cl["cp_cl_small"]
    assert not_assessed["status"] == "NOT_ASSESSED"
    assert not_assessed["minute_rows"] == 10
    assert not_assessed["not_assessed_reason"] == "10 of 15 minutes needed"
    assert warn["not_assessed_reason"] is None

    # no CRITICAL branch anywhere in this id's body -- confirmed structurally (not just on this
    # fixture's own rows) across every window.
    for w in WINDOWS:
        out_w = dbutil.rows("node_timeline_utilization", w)
        assert not any(r["status"] == "CRITICAL" for r in out_w), (w, "unexpected CRITICAL row")

    # worst-first (T-75B review fix): order_by is `CASE status WHEN 'WARN' THEN 0 WHEN
    # 'NOT_ASSESSED' THEN 1 ELSE 2 END, avg_cpu_pct ASC` -- WARN groups first, then NOT_ASSESSED,
    # then OK, each band internally ascending by avg_cpu_pct (no CRITICAL branch exists on this
    # id). A NOT_ASSESSED group can have a HIGH avg_cpu_pct (cp_cl_small is 60%), so a plain
    # `avg_cpu_pct ASC` check across the whole list no longer describes the contract -- the status
    # band must be checked first.
    _RANK = {"WARN": 0, "NOT_ASSESSED": 1, "OK": 2}
    rank_seq = [(_RANK[r["status"]], r["avg_cpu_pct"]) for r in out]
    assert rank_seq == sorted(rank_seq), rank_seq

    expect = _expect_present(CL_WIN)
    for w, expected_present in expect.items():
        out_w = dbutil.rows("node_timeline_utilization", w)
        present = {r["cluster_id"] for r in out_w if r["cluster_id"] in CL_WIN.values()}
        assert present == expected_present, (w, present, expected_present)


# -------------------------------------------------------------------------------------------
# Synthetic, in-memory: compute_idle_node_ratio's idle_ratio must judge worker slices only, so
# a busy worker sitting next to an idle driver (normal - the driver mostly coordinates) reads
# the worker's own low idle share, not a share diluted by the driver's idle time.
# -------------------------------------------------------------------------------------------
def test_idle_node_ratio_judges_worker_slices_not_driver():
    con = duckdb.connect()
    try:
        for _name, create_sql in ddl.DDL.items():
            con.execute(create_sql)

        cluster_id = "cp_cl_workerbusy"
        start = cp.AS_OF - timedelta(days=2)
        for i in range(100):
            # driver: idle the whole time (cpu 1% < idle_cpu_pct 5%) -- normal, coordination only
            cp._nt(con, cluster_id, f"{cluster_id}_driver", start + timedelta(minutes=i),
                   driver=True, cpu_user=1.0, cpu_system=0.0, mem_used=10.0)
            # worker: busy the whole time (cpu 60% >= 5%)
            cp._nt(con, cluster_id, f"{cluster_id}_worker", start + timedelta(minutes=i),
                   driver=False, cpu_user=60.0, cpu_system=0.0, mem_used=40.0)

        spec = by_id("compute_idle_node_ratio")
        text = render_model(spec, target="duckdb")
        sql = jinja_stub.render(text, windows=(30,), source=jinja_stub.memory_source)
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        r = [x for x in rows if x["cluster_id"] == cluster_id][0]

        assert r["total_slices"] == 200          # 100 driver + 100 worker minute-slices
        assert r["idle_slices"] == 0              # the worker was never idle
        assert r["idle_ratio"] == 0.0             # would read 0.5 (WARN) if the driver's idle
        assert r["status"] == "OK"                # minutes were still counted
    finally:
        con.close()


# -------------------------------------------------------------------------------------------
# Synthetic, in-memory: compute_warehouse_idle_gaps' running time now uses the exact state logic
# compute_warehouse_idle_minutes does -- the two must agree on the same events, and a state
# carried in from before the window must give the same running_seconds at every window length.
# -------------------------------------------------------------------------------------------
def test_idle_gaps_running_time_matches_idle_minutes_and_carries_in():
    con = duckdb.connect()
    try:
        for _name, create_sql in ddl.DDL.items():
            con.execute(create_sql)

        wh_id = "cp_wh_carry_check"
        # RUNNING well before every window this test checks (30d and 90d), never stopped again --
        # the whole window must read as running, carried in from before it opened.
        cp._we(con, wh_id, "RUNNING", cp.AS_OF - timedelta(days=100))

        idle_spec = by_id("compute_warehouse_idle_minutes")
        gaps_spec = by_id("compute_warehouse_idle_gaps")
        idle_text = render_model(idle_spec, target="duckdb")
        gaps_text = render_model(gaps_spec, target="duckdb")

        running_seconds_by_window = {}
        for window_days in (30, 90):
            idle_sql = jinja_stub.render(idle_text, windows=(window_days,), source=jinja_stub.memory_source)
            gaps_sql = jinja_stub.render(gaps_text, windows=(window_days,), source=jinja_stub.memory_source)
            idle_cols_cur = con.execute(idle_sql)
            idle_cols = [d[0] for d in idle_cols_cur.description]
            idle_rows = [dict(zip(idle_cols, r)) for r in idle_cols_cur.fetchall()]
            idle_row = [r for r in idle_rows if r["warehouse_id"] == wh_id][0]

            gaps_cur = con.execute(gaps_sql)
            gaps_cols = [d[0] for d in gaps_cur.description]
            gaps_rows = [dict(zip(gaps_cols, r)) for r in gaps_cur.fetchall()]
            gaps_row = [r for r in gaps_rows if r["warehouse_id"] == wh_id][0]

            assert gaps_row["running_seconds"] == round(idle_row["running_minutes"] * 60), (
                window_days, gaps_row["running_seconds"], idle_row["running_minutes"])
            running_seconds_by_window[window_days] = gaps_row["running_seconds"]

        # carried in from before the window and never stopped: the whole window is running.
        assert running_seconds_by_window[30] == 30 * 86400
        assert running_seconds_by_window[90] == 90 * 86400
    finally:
        con.close()
