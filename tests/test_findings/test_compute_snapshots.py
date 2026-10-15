"""tests/test_findings/test_compute_snapshots.py -- T-15.

Proves, against tests/fixtures/compute.py's own rows and only those rows, that each of the 5 B2
"compute snapshot" findings (`findings.f_<query_id>`, PLAN.md 7.2) has the right grain, the right
snapshot-vs-windowed `window_days` shape, the SCD2 "latest row per key, delete_time IS NULL" dedupe
where the body does that, every reachable status band by id where the body computes a status, and
the windowed (7/30/90) behaviour -- including the AS_OF-day and D(95) exclusions on the pool cost
rollup -- where the body is windowed. Per tests/test_findings/README.md's checklist and this task's
own Inputs/Steps section.

Window shape (T-15 task file Inputs, tasks/DECISIONS.md DEC-24 grain convention): 3 of the 5 ids
are SNAPSHOT queries (`classic_clusters_config_current`, `sql_warehouse_config_current`,
`node_types_reference` -- all `params: none`, no `:period_days` anywhere in the body) -- the
generator emits a single `0 AS window_days` block for these, so `dbutil.rows(id, 0)` is the only
non-empty read and `dbutil.rows(id, 30)` must be `[]`. The other 2 (`instance_pools_idle_capacity`,
`instance_events_idle_active`) both carry `:period_days` and are windowed 7/30/90 like the B1 ids --
the reverse: `dbutil.rows(id, 0) == []` and `dbutil.rows(id, w)` for w in (7, 30, 90) is where the
rows live. Getting this backwards is the single most common error in this batch (task file, verbatim).

Expectations are computed either from tests/fixtures/compute.py's own literal ids/timestamps
(imported directly as `cp`, the same module T-14's test_compute_activity.py imports) or from the raw
fixture parquet under tests/fixtures/parquet/ -- never a hard-coded total. `_pool_cost_rollup()`
below reproduces instance_pools_idle_capacity's own cost_rollup CTE (`usage_end_time >=
price_start_time AND (price_end_time IS NULL OR usage_end_time < price_end_time)`, `pricing.
default`, keyed on `(workspace_id, instance_metadata.instance_pool_id)` per the body's own caveat
that the join is kept strictly 1:1 by carrying both columns) straight from
tests/fixtures/parquet/billing__usage and billing__list_prices, matching
tests/test_findings/test_compute_activity.py's `_cost_rollup()` precedent -- not any builder's
private constant. `_instance_events_group()` reproduces instance_events_idle_active's own GROUP BY
+ `event_time >= audit_now() - INTERVAL :period_days DAYS` filter the same way.

Per tests/fixtures/compute.py's own module docstring (Hand-off / "Resources for T-15" section):
this builder wrote a fourth instance pool `cp_pool_na` whose current SCD2 row has
`min_idle_instances IS NULL` (so instance_pools_idle_capacity's `NOT_ASSESSED` branch is reachable),
a deleted SCD2 key per table (`cp_cl_deleted`, `cp_wh_deleted`, `cp_pool_deleted`, each with a
non-NULL `delete_time` on its latest row, so every `WHERE ... delete_time IS NULL` filter has a real
row to drop), `instance_events` rows spread across all 5 window-boundary anchors with varied
`state`/`event_type`/`availability_type`/`node_type` (so the 5-column grain is actually exercised),
and D(0) plus D(95) rows on the pool cost rollup (so the AS_OF-day exclusion AND the
window-boundary exclusion at w=90 both have something real to drop). Every test below that touches
one of these rows asserts the row is either present-with-the-right-values or genuinely
absent-from-output-but-present-in-raw-parquet -- never a bare `== []` that would look identical to a
row that was silently deleted from the fixture (tests/test_findings/README.md, "expectations
computed from the builder's own parquet").

`classic_clusters_config_current` and `sql_warehouse_config_current` share `system.compute.clusters`
/ `system.compute.warehouses` with other builders (`lakeflow.py` also writes `lf_`-prefixed cluster
rows per DEC-21; `drilldown.py` writes `dd_`-prefixed cluster rows). Assertions below therefore
filter to this builder's own `cp_`-prefixed ids wherever they read a specific row's field values
(matching tests/fixtures/compute.py's own id-prefix convention, DEC-15), except for the whole-table
`order_by` proofs, which are valid unfiltered: a query's own `ORDER BY <key>` orders every row it
emits regardless of which builder wrote it, so checking the *entire* returned sequence is sorted is
a strictly stronger proof than checking a filtered subsequence, not a weaker one. `instance_pools`
and `instance_events` are written by no other builder (confirmed by grep over tests/fixtures/*.py),
so `instance_pools_idle_capacity`'s assertions read the whole table without needing to filter.
Since T-65 (DEC-65), `node_types` and `warehouses` are NOT exclusive to this builder any more:
`pressure_jobs.py` writes one `pr_`-prefixed `node_types` row (it proves the jobs-pressure query's
`worker_memory_gb`), and `pressure_warehouses.py` writes `pr_wh_`-prefixed `warehouses` rows. So
`node_types_reference`'s set and order assertions filter to `cp_` node types (DEC-15), and
`sql_warehouse_config_current` reads only specific `cp_wh_` ids plus the whole-table order proof.
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
import dbutil  # noqa: E402
import compute as cp  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "compute_snapshots.yml"
PARQUET_DIR = ROOT / "tests" / "fixtures" / "parquet"
WINDOWS = (7, 30, 90)
STATUS_VALUES = {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}

SNAPSHOT_IDS = [
    "classic_clusters_config_current",
    "sql_warehouse_config_current",
    "node_types_reference",
]
WINDOWED_IDS = [
    "instance_pools_idle_capacity",
    "instance_events_idle_active",
]
ALL_IDS = SNAPSHOT_IDS + WINDOWED_IDS


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _raw_row_count(schema_table, where):
    """COUNT(*) over the raw tests/fixtures/parquet/<schema_table>/*.parquet (every builder's
    slice, unioned by name), bypassing every dbt model filter -- proves a fixture row genuinely
    exists (and how many SCD2 versions it has) independent of the built table."""
    pattern = (PARQUET_DIR / schema_table / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        sql = f"SELECT COUNT(*) FROM read_parquet('{pattern}', union_by_name=true) WHERE {where}"
        return con.execute(sql).fetchone()[0]
    finally:
        con.close()


def _raw_row_exists(schema_table, where):
    return _raw_row_count(schema_table, where) > 0


def _pool_cost_rollup(workspace_id, pool_id, window_days):
    """Independent re-derivation of instance_pools_idle_capacity's own cost_rollup CTE (net_dbus,
    est_usd_list, price_basis), straight from the raw billing.usage / billing.list_prices parquet
    -- the exact date-range price-join predicate the body uses (`u.usage_end_time >=
    p.price_start_time AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)`,
    `CAST(pricing.effective_list.default AS DOUBLE)` -- DEC-66.1's one dollar basis), filtered on
    `(workspace_id, usage_metadata.instance_pool_id)` the same way the body's `cost_rollup` CTE
    groups (`GROUP BY u.workspace_id, u.usage_metadata.instance_pool_id`) and the outer query joins
    (`cr.workspace_id = p.workspace_id AND cr.instance_pool_id = p.instance_pool_id`). Mirrors the
    body's own current-day exclusion (`usage_date >= current_date() - period_days AND usage_date <
    current_date()`) and its price_basis CASE (free/priced/unpriced)."""
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
                   SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS est_usd_list,
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
              AND u.usage_metadata.instance_pool_id = '{pool_id}'
              AND u.workspace_id = '{workspace_id}'
              AND u.usage_date >= DATE '{lower}' AND u.usage_date < DATE '{upper}'
        """
        net_dbus, est_usd_list, price_basis = con.execute(sql).fetchone()
        return (net_dbus or 0.0), (est_usd_list or 0.0), (price_basis or "priced")
    finally:
        con.close()


def _instance_events_group(node_type, availability_type, state, event_type, window_days):
    """Independent re-derivation of instance_events_idle_active's own GROUP BY + window filter
    (`event_time >= current_timestamp() - INTERVAL :period_days DAYS`, no upper bound), straight
    from the raw compute.instance_events parquet, for one (node_type, availability_type, state,
    event_type) group (workspace_id is not filtered here because every row this builder writes uses
    WS_PROD -- see tests/fixtures/compute.py SEC J)."""
    pattern = (PARQUET_DIR / "compute__instance_events" / "*.parquet").as_posix()
    lower = (cp.AS_OF - timedelta(days=window_days)).isoformat()
    con = duckdb.connect()
    try:
        sql = f"""
            SELECT COUNT(*) AS event_count, COUNT(DISTINCT instance_id) AS instance_count
            FROM read_parquet('{pattern}', union_by_name=true)
            WHERE node_type = '{node_type}' AND availability_type = '{availability_type}'
              AND state = '{state}' AND event_type = '{event_type}'
              AND event_time >= TIMESTAMP '{lower}'
        """
        return con.execute(sql).fetchone()
    finally:
        con.close()


# -------------------------------------------------------------------------------------------
# Generic checks over all 5 ids -- window shape (snapshot vs windowed), grain uniqueness.
# -------------------------------------------------------------------------------------------
def test_snapshot_ids_live_only_at_window_zero():
    for qid in SNAPSHOT_IDS:
        out0 = dbutil.rows(qid, 0)
        assert out0, qid  # non-empty guard: a vacuous check on an empty result proves nothing
        assert dbutil.rows(qid, 30) == [], f"{qid}: window_days=30 must be empty (snapshot query)"


def test_windowed_ids_live_only_at_7_30_90():
    for qid in WINDOWED_IDS:
        assert dbutil.rows(qid, 0) == [], f"{qid}: window_days=0 must be empty (windowed query)"
        for w in WINDOWS:
            out = dbutil.rows(qid, w)
            assert out, (qid, w)


def test_grain_uniqueness_all_ids():
    grains = _grains()
    for qid in SNAPSHOT_IDS:
        cols = grains[qid]
        out = dbutil.rows(qid, 0)
        assert out, qid  # a uniqueness check over an empty result proves nothing
        keys = [tuple(r[c] for c in cols) for r in out]
        assert len(keys) == len(set(keys)), f"{qid}: duplicate grain rows at window_days=0"
    for qid in WINDOWED_IDS:
        cols = grains[qid]
        out = dbutil.rows(qid, 30)
        assert out, qid
        keys = [tuple(r[c] for c in cols) for r in out]
        assert len(keys) == len(set(keys)), f"{qid}: duplicate grain rows at window_days=30"


def test_status_enum_instance_pools_idle_capacity():
    # The only one of the 5 ids with a status column at all (task file's per-query contract table:
    # the other 4 are "n/a - inventory, no status column" or, for instance_events_idle_active,
    # explicitly "no status column is computed at all").
    for w in WINDOWS:
        out = dbutil.rows("instance_pools_idle_capacity", w)
        assert out, w
        for r in out:
            assert r["status"] in STATUS_VALUES, f"w={w}: bad status {r['status']!r}"


def test_instance_events_idle_active_has_no_status_column():
    out = dbutil.rows("instance_events_idle_active", 30)
    assert out
    assert "status" not in out[0], "instance_events_idle_active must not compute a status column"


# -------------------------------------------------------------------------------------------
# classic_clusters_config_current -- snapshot, grain [cluster_id]. SCD2: latest row per cluster_id,
# WHERE rn = 1 AND delete_time IS NULL. No status column (n/a - inventory). order_by: cluster_id.
# -------------------------------------------------------------------------------------------
def test_classic_clusters_config_current():
    out = dbutil.rows("classic_clusters_config_current", 0)
    by_id = {r["cluster_id"]: r for r in out}

    # SCD2 dedupe: cp_cl_warn has 2 raw rows (change_time AS_OF-60d, worker_count=2,
    # cluster_source=UI; AS_OF-10d, worker_count=3, cluster_source=API) but the built table keeps
    # only 1, and it is the LATEST one -- not just "any one of them".
    assert _raw_row_count("compute__clusters", "cluster_id = 'cp_cl_warn'") == 2
    assert sum(1 for r in out if r["cluster_id"] == "cp_cl_warn") == 1
    warn = by_id["cp_cl_warn"]
    assert warn["worker_count"] == 3
    assert warn["cluster_source"] == "API"

    idle = by_id["cp_cl_idle"]
    assert idle["auto_termination_minutes"] == 30  # latest row; old row was 20

    busy = by_id["cp_cl_busy"]
    assert busy["worker_count"] == 6  # latest row; old row was 4
    assert busy["auto_termination_minutes"] == 45  # latest row; old row was 20

    small = by_id["cp_cl_small"]
    assert small["worker_count"] == 1

    # delete_time IS NULL filter: cp_cl_deleted has 2 raw rows (its LATEST row carries a non-NULL
    # delete_time) and is entirely absent from the built table -- not deduped to 1 row, dropped to 0.
    assert _raw_row_count("compute__clusters", "cluster_id = 'cp_cl_deleted'") == 2
    assert _raw_row_exists(
        "compute__clusters", "cluster_id = 'cp_cl_deleted' AND delete_time IS NOT NULL"
    )
    assert "cp_cl_deleted" not in by_id

    # worst-first / order_by (meta.order_by = "cluster_id"): the WHOLE returned sequence is sorted
    # by cluster_id, not just this builder's cp_ subset -- classic_clusters_config_current also
    # carries lakeflow.py's lf_-prefixed and drilldown.py's dd_-prefixed cluster rows.
    ids = [r["cluster_id"] for r in out]
    assert ids == sorted(ids), ids


# -------------------------------------------------------------------------------------------
# sql_warehouse_config_current -- snapshot, grain [warehouse_id]. SCD2: latest row per
# warehouse_id, WHERE rn = 1 AND delete_time IS NULL. No status column. order_by: warehouse_id.
# -------------------------------------------------------------------------------------------
def test_sql_warehouse_config_current():
    out = dbutil.rows("sql_warehouse_config_current", 0)
    by_id = {r["warehouse_id"]: r for r in out}

    # SCD2 dedupe: cp_wh_idle_crit has 2 raw rows (SMALL/auto_stop=10 old, MEDIUM/auto_stop=30
    # current) -- the built table keeps only the latest.
    assert _raw_row_count("compute__warehouses", "warehouse_id = 'cp_wh_idle_crit'") == 2
    assert sum(1 for r in out if r["warehouse_id"] == "cp_wh_idle_crit") == 1
    crit = by_id["cp_wh_idle_crit"]
    assert crit["warehouse_size"] == "MEDIUM"
    assert crit["auto_stop_minutes"] == 30

    warn = by_id["cp_wh_idle_warn"]
    assert warn["max_clusters"] == 4  # latest row; old row was 1

    churn = by_id["cp_wh_churn"]
    assert churn["warehouse_size"] == "MEDIUM"

    # delete_time IS NULL filter: cp_wh_deleted has 2 raw rows and is entirely absent from output.
    assert _raw_row_count("compute__warehouses", "warehouse_id = 'cp_wh_deleted'") == 2
    assert _raw_row_exists(
        "compute__warehouses", "warehouse_id = 'cp_wh_deleted' AND delete_time IS NOT NULL"
    )
    assert "cp_wh_deleted" not in by_id

    # order_by (meta.order_by = "warehouse_id"): the whole returned table, unfiltered. Since T-65
    # pressure_warehouses.py also writes pr_wh_ rows here; the query's own ORDER BY orders every row
    # it emits whichever builder wrote it, so the unfiltered sequence is the stronger proof.
    ids = [r["warehouse_id"] for r in out]
    assert ids == sorted(ids), ids


# -------------------------------------------------------------------------------------------
# node_types_reference -- snapshot, grain [node_type]. Static reference dimension, no SCD2, no
# delete_time. No status column. order_by: node_type.
# -------------------------------------------------------------------------------------------
def test_node_types_reference():
    out = dbutil.rows("node_types_reference", 0)
    # Since T-65, tests/fixtures/pressure_jobs.py also writes one pr_-prefixed node_types row, so
    # this builder's rows are the cp_-prefixed ones (DEC-15): exactly compute.py's 2 rows -- the
    # exact-set assertion over them is not vacuous.
    own = [r for r in out if r["node_type"].startswith("cp_")]
    by_type = {r["node_type"]: r for r in own}
    assert set(by_type) == {cp.NODE_A, cp.NODE_B}

    a = by_type[cp.NODE_A]
    assert a["core_count"] == 4.0
    assert a["memory_mb"] == 16384
    assert a["gpu_count"] == 0

    b = by_type[cp.NODE_B]
    assert b["core_count"] == 16.0
    assert b["memory_mb"] == 65536
    assert b["gpu_count"] == 0

    # order_by (meta.order_by = "node_type"): cp_node_type_a < cp_node_type_b lexically, in this
    # builder's own rows, and the whole returned table (every builder's rows) is sorted too.
    assert [r["node_type"] for r in own] == [cp.NODE_A, cp.NODE_B]
    all_types = [r["node_type"] for r in out]
    assert all_types == sorted(all_types), all_types


# -------------------------------------------------------------------------------------------
# instance_pools_idle_capacity -- windowed 7/30/90, grain [workspace_id, instance_pool_id]. SCD2:
# latest row per instance_pool_id, WHERE rn = 1 AND delete_time IS NULL. Status: NOT_ASSESSED if
# min_idle_instances IS NULL; CRITICAL >= crit_min_idle_instances(20); WARN >= warn_min_idle_
# instances(5); else OK. order_by: p.min_idle_instances DESC (DuckDB sorts NULLs last regardless of
# ASC/DESC, confirmed with a scratch probe -- so NOT_ASSESSED sorts after OK, not before it).
# -------------------------------------------------------------------------------------------
def test_instance_pools_idle_capacity_status_bands_and_scd2():
    out = dbutil.rows("instance_pools_idle_capacity", 30)
    by_key = {(r["workspace_id"], r["instance_pool_id"]): r for r in out}

    ok = by_key[(cp.WS_PROD, "cp_pool_ok")]
    assert ok["status"] == "OK"
    assert ok["min_idle_instances"] == 3
    assert ok["max_capacity"] == 50  # latest row; old row was max_capacity=20

    warn = by_key[(cp.WS_DEV, "cp_pool_warn")]
    assert warn["status"] == "WARN"
    assert warn["min_idle_instances"] == 10
    assert warn["node_type"] == cp.NODE_B  # latest row; old row's node_type was NODE_A

    crit = by_key[(cp.WS_UAT, "cp_pool_crit")]
    assert crit["status"] == "CRITICAL"
    assert crit["min_idle_instances"] == 25
    assert crit["node_type"] == cp.NODE_B  # latest row; old row's node_type was NODE_A

    na = by_key[(cp.WS_PROD, "cp_pool_na")]
    assert na["status"] == "NOT_ASSESSED"
    assert na["min_idle_instances"] is None  # the current SCD2 row's own value, not the old row's 1

    # status is a pure function of min_idle_instances (not window-dependent): same band at 7/30/90.
    for w in WINDOWS:
        row_w = {r["instance_pool_id"]: r["status"] for r in dbutil.rows("instance_pools_idle_capacity", w)}
        assert row_w["cp_pool_ok"] == "OK"
        assert row_w["cp_pool_warn"] == "WARN"
        assert row_w["cp_pool_crit"] == "CRITICAL"
        assert row_w["cp_pool_na"] == "NOT_ASSESSED"

    # delete_time IS NULL filter: cp_pool_deleted has 2 raw SCD2 rows and is entirely absent.
    assert _raw_row_count("compute__instance_pools", "instance_pool_id = 'cp_pool_deleted'") == 2
    assert _raw_row_exists(
        "compute__instance_pools",
        "instance_pool_id = 'cp_pool_deleted' AND delete_time IS NOT NULL",
    )
    assert "cp_pool_deleted" not in {r["instance_pool_id"] for r in out}

    # worst-first order_by: narrowed to this builder's own cp_ pools (P4-T-ROLL, tasks/
    # P4-T-SPEC.md section 7.6: tests/fixtures/tagworld.py also writes compute.instance_pools --
    # tg_pool_a1 -- and adds its own NOT_ASSESSED row, so the whole-sequence assertion can no
    # longer assume this table has no other builder's rows).
    cp_out = [r for r in out if r["instance_pool_id"].startswith("cp_pool_")]
    assert [r["status"] for r in cp_out] == ["CRITICAL", "WARN", "OK", "NOT_ASSESSED"]
    assert [r["instance_pool_id"] for r in cp_out] == [
        "cp_pool_crit", "cp_pool_warn", "cp_pool_ok", "cp_pool_na",
    ]


def test_instance_pools_idle_capacity_cost_rollup_window_and_asof_exclusion():
    # Each pool has an in-window D(5) row, an AS_OF-date D(0) row (999 DBU, must be excluded at
    # every window) and a D(95) row (50 DBU, must be excluded at every window INCLUDING 90, since
    # 95 > 90) -- tests/fixtures/compute.py SEC K / module docstring "review fix item 6".
    scenarios = [
        ("cp_pool_ok", cp.WS_PROD, 8.0),
        ("cp_pool_warn", cp.WS_DEV, 12.0),
        ("cp_pool_crit", cp.WS_UAT, 30.0),
    ]
    for pool_id, ws, expected_5d in scenarios:
        for w in WINDOWS:
            expected_net, expected_est, expected_basis = _pool_cost_rollup(ws, pool_id, w)
            # Sanity: the D(0) 999-DBU row and the D(95) 50-DBU row are excluded at every window,
            # even w=90 -- only the D(5) row ever counts.
            assert expected_net == expected_5d, (pool_id, w, expected_net)

            row = [r for r in dbutil.rows("instance_pools_idle_capacity", w)
                   if r["workspace_id"] == ws and r["instance_pool_id"] == pool_id][0]
            assert row["net_dbus"] == expected_net, (pool_id, w, row["net_dbus"], expected_net)
            assert abs(row["est_usd_list"] - expected_est) < 1e-6, (pool_id, w)
            assert row["price_basis"] == expected_basis, (pool_id, w, row["price_basis"], expected_basis)

        # Anchor: prove the D(0) and D(95) rows genuinely exist in the raw parquet (so the equality
        # above is a real exclusion, not an artefact of those rows never having been written) -- an
        # UNWINDOWED sum (dbutil.usage_sum, no usage_date filter at all) must equal D(5) + D(0) +
        # D(95) = expected_5d + 999.0 + 50.0, strictly more than any windowed net_dbus computed above.
        incl_all = dbutil.usage_sum(f"usage_metadata.instance_pool_id = '{pool_id}'")
        assert incl_all == expected_5d + 999.0 + 50.0, (pool_id, incl_all)
        assert expected_net < incl_all  # the model's own filter drops both the D(0) and D(95) rows


# -------------------------------------------------------------------------------------------
# instance_events_idle_active -- windowed 7/30/90, grain [workspace_id, node_type,
# availability_type, state, event_type]. No status column at all. order_by: event_count DESC,
# instance_count DESC.
# -------------------------------------------------------------------------------------------
def test_instance_events_idle_active_grain_and_window_growth():
    # 4 distinct groups by construction (tests/fixtures/compute.py SEC J / "review fix item 4"):
    #   ready  = (NODE_A, ON_DEMAND, INSTANCE_READY,     STATE_TRANSITION)  -- 5 anchors
    #   placed = (NODE_A, ON_DEMAND, INSTANCE_PLACED,    STATE_TRANSITION)  -- 5 anchors (+5 min)
    #   spot_l = (NODE_B, SPOT,      INSTANCE_LAUNCHING, INSTANCE_LAUNCHING) -- D7 only
    #   spot_r = (NODE_B, SPOT,      INSTANCE_READY,     STATE_TRANSITION)  -- D7 only
    groups = {
        "ready": (cp.NODE_A, "ON_DEMAND", "INSTANCE_READY", "STATE_TRANSITION"),
        "placed": (cp.NODE_A, "ON_DEMAND", "INSTANCE_PLACED", "STATE_TRANSITION"),
        "spot_launch": (cp.NODE_B, "SPOT", "INSTANCE_LAUNCHING", "INSTANCE_LAUNCHING"),
        "spot_ready": (cp.NODE_B, "SPOT", "INSTANCE_READY", "STATE_TRANSITION"),
    }
    for w in WINDOWS:
        out = dbutil.rows("instance_events_idle_active", w)
        by_key = {(r["node_type"], r["availability_type"], r["state"], r["event_type"]): r for r in out}
        assert set(by_key) == set(groups.values()), (w, set(by_key))

        for name, key in groups.items():
            expected_count, expected_instances = _instance_events_group(*key, window_days=w)
            row = by_key[key]
            assert row["event_count"] == expected_count, (name, w, row["event_count"], expected_count)
            assert row["instance_count"] == expected_instances, (name, w)
            assert row["workspace_id"] == cp.WS_PROD

        # window growth (lower-bound-only filter, no upper bound): the "ready"/"placed" groups grow
        # 2 -> 3 -> 4 as t30/t90 join at their own windows; the spot groups (D7-only) stay 1 always.
        expected_ready_count = {7: 2, 30: 3, 90: 4}[w]
        assert by_key[groups["ready"]]["event_count"] == expected_ready_count, w
        assert by_key[groups["placed"]]["event_count"] == expected_ready_count, w
        assert by_key[groups["spot_launch"]]["event_count"] == 1, w
        assert by_key[groups["spot_ready"]]["event_count"] == 1, w

    # told90 is excluded even at w=90 -- prove the anchor row genuinely exists in raw parquet so
    # "count stays at 4, not 5" above is a real exclusion, not a fixture gap.
    # The anchor pins the row into the "ready" grain group as well as merely existing: without
    # the group columns, a row that drifted into another group would still satisfy the anchor and
    # the "count stays at 4" claim above would silently stop meaning what it says.
    assert _raw_row_exists(
        "compute__instance_events",
        "instance_id = 'cp_inst_ev_ready_told90' "
        f"AND node_type = '{cp.NODE_A}' AND state = 'INSTANCE_READY' "
        "AND event_type = 'STATE_TRANSITION' AND availability_type = 'ON_DEMAND'",
    )

    # worst-first order_by (event_count DESC, instance_count DESC): tie-tolerant check -- "ready"
    # and "placed" always tie with each other (same anchor set), as do the two spot groups, so the
    # only assertable invariant is that the full (event_count, instance_count) key sequence is
    # already sorted descending, not any particular tie-break order between equal-keyed rows.
    for w in WINDOWS:
        out = dbutil.rows("instance_events_idle_active", w)
        keys = [(r["event_count"], r["instance_count"]) for r in out]
        assert keys == sorted(keys, reverse=True), (w, keys)
