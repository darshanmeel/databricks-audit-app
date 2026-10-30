"""tests/test_tag_rollup_models.py -- P4-T-ROLL (tasks/P4-T-SPEC.md section 6.7).

Proves rule 3 (every dollar/event counted once, the account total reconciles to billing) and the
pinned numbers of section 7.2/7.3, against the real dbt-built tests/db_audit_test.duckdb (built by
tests/fixtures/build_fixtures.py + `python tools/dbt_run.py build --target test`, the same
database every other test module in this repo reads). Expectations for the reconciliation and
tree-invariant tests are computed independently from the raw fixture parquet (never read back from
tags.cost_unit itself) -- the same "never hard-code a total, never trust the model under test to
grade its own homework" discipline tests/test_findings/*.py already follows.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import duckdb
import pytest

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS_DIR / "fixtures"))
sys.path.insert(0, str(TESTS_DIR))

import dbutil  # noqa: E402
from app.core import rollup  # noqa: E402

DB_PATH = TESTS_DIR / "db_audit_test.duckdb"
PARQUET_DIR = TESTS_DIR / "fixtures" / "parquet"
TG_WORKSPACES = ["tg_ws_a", "tg_ws_m", "tg_ws_s"]
WINDOWS = (7, 30, 90)


def _connect() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(DB_PATH), read_only=True)


def _billing_total(con, window_days: int) -> tuple[float, float, float]:
    """(priced $ total, billed quantity, unpriced quantity), independently from the raw parquet --
    the effective-list-price join every priced cost query uses (DEC-66.1), overlaps de-duplicated."""
    row = con.execute(
        f"""
        SELECT
            COALESCE(SUM(CASE WHEN lp.list_rate IS NOT NULL THEN u.usage_quantity * lp.list_rate END), 0),
            SUM(u.usage_quantity),
            COALESCE(SUM(CASE WHEN lp.list_rate IS NULL THEN u.usage_quantity END), 0)
        FROM read_parquet('{(PARQUET_DIR / "billing__usage" / "*.parquet").as_posix()}', union_by_name=true) u
        LEFT JOIN ({dbutil.deduped_list_prices_sql((PARQUET_DIR / "billing__list_prices" / "*.parquet").as_posix())}) lp
          ON u.sku_name = lp.sku_name AND u.cloud = lp.cloud AND u.usage_unit = lp.usage_unit
         AND u.usage_end_time >= lp.price_start_time
         AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
        WHERE u.usage_date >= DATE '2026-09-21' - INTERVAL {window_days} DAY
          AND u.usage_date < DATE '2026-09-21'
        """
    ).fetchone()
    return row[0], row[1], row[2]


# -------------------------------------------------------------------------------------------
# 1. Reconciliation (rule 3's proof) -- every builder's rows, not just tagworld's.
# -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("window_days", WINDOWS)
def test_reconciliation_against_raw_parquet(window_days):
    con = _connect()
    try:
        billing_usd, billing_qty, billing_unpriced = _billing_total(con, window_days)
        unit_usd, unit_qty = con.execute(
            "SELECT COALESCE(SUM(usd), 0), COALESCE(SUM(quantity), 0) FROM tags.cost_unit WHERE window_days = ?",
            [window_days],
        ).fetchone()
        recon_usd, recon_unpriced = con.execute(
            "SELECT COALESCE(SUM(billing_usd), 0), COALESCE(SUM(unit_unpriced_quantity), 0) "
            "FROM tags.cost_reconciliation WHERE window_days = ?",
            [window_days],
        ).fetchone()
    finally:
        con.close()
    assert abs(billing_usd - unit_usd) < 0.005
    assert abs(billing_usd - recon_usd) < 0.005
    assert abs(billing_qty - unit_qty) < 0.0001
    assert abs(billing_unpriced - recon_unpriced) < 0.0001


# -------------------------------------------------------------------------------------------
# 2. Tree invariants, unfiltered, for four keys at 30d.
# -------------------------------------------------------------------------------------------


def _sum_metrics(a, b):
    return {k: a.get(k, 0) + b.get(k, 0) for k in set(a) | set(b)}


def _assert_node_sums_to_children(node, tol=0.005):
    if not node.get("children"):
        return
    total = {k: 0 for k in node["metrics"]}
    for c in node["children"]:
        _assert_node_sums_to_children(c, tol)
        total = _sum_metrics(total, c["metrics"])
    for k in node["metrics"]:
        assert abs(node["metrics"][k] - total[k]) < tol, (node["id"], k, node["metrics"][k], total[k])


@pytest.mark.parametrize("tag_key", ["tgcc", "tgapp", "tgenv", "costcenter"])
def test_tree_invariants_unfiltered_30d(tag_key):
    con = _connect()
    try:
        billing_usd, _, _ = _billing_total(con, 30)
    finally:
        con.close()
    body = rollup.rollup("cost", tag_key, 30)
    assert body["outcome"] == "ok_rows"
    tree = body["tree"]
    _assert_node_sums_to_children(tree)
    assert abs(tree["metrics"]["usd"] - billing_usd) < 0.005
    by_value_total = sum(e["metrics"]["usd"] for e in body["by_value"])
    assert abs(by_value_total - tree["metrics"]["usd"]) < 0.005
    paths_total = sum(p["metrics"]["usd"] for p in body["paths"])
    assert abs(paths_total - tree["metrics"]["usd"]) < 0.005


# -------------------------------------------------------------------------------------------
# 3. Pinned numbers, tg-scoped, section 7.2.
# -------------------------------------------------------------------------------------------


def _flatten_tree(node, metrics_fn, path=()):
    """Flattens a rollup tree to {label-path: metrics_fn(node)} for every node -- so a pinned test
    can check the WHOLE tree (labels, nesting and metrics together) in one assertion, rather than
    only the few nodes a test happens to look up by hand."""
    label_path = path + (node["label"],)
    out = {label_path: metrics_fn(node)}
    for c in node.get("children", []):
        out.update(_flatten_tree(c, metrics_fn, label_path))
    return out


def _assert_flat_tree(actual, expected, tol=0.005):
    assert set(actual) == set(expected), f"label-paths differ: {sorted(set(actual) ^ set(expected))}"
    for path, expect in expected.items():
        got = actual[path]
        for g, e in zip(got, expect):
            assert abs(g - e) < tol, (path, got, expect)


def _cost_node_metrics(node):
    return (node["metrics"]["usd"], node["untagged"]["usd"], node["attributed_here"]["usd"])


def _perf_node_metrics(node):
    m = node["metrics"]
    return (m["statements"], m["failed_statements"], m["duration_ms"], m["queue_ms"], m["spill_bytes"])


# section 7.2's 30d tree, exactly -- (usd, untagged.usd, attributed_here.usd) per node.
_PINNED_COST_TREE_30D = {
    ("Account",): (672.0, 50.0, 50.0),
    ("Account", "workspace tag: alpha"): (362.0, 0.0, 16.0),
    ("Account", "workspace tag: alpha", "warehouse tag: alpha"): (150.0, 0.0, 110.0),
    ("Account", "workspace tag: alpha", "warehouse tag: alpha", "query tag: q_fin"): (40.0, 0.0, 40.0),
    ("Account", "workspace tag: alpha", "warehouse tag: alpha",
     "not split to queries (no attributed usage for that day)"): (50.0, 0.0, 0.0),
    ("Account", "workspace tag: alpha", "warehouse tag: alpha", "idle or not attributed to a query"): (30.0, 0.0, 0.0),
    ("Account", "workspace tag: alpha", "warehouse tag: alpha", "untagged queries"): (30.0, 0.0, 0.0),
    ("Account", "workspace tag: alpha", "cluster tag: alpha"): (80.0, 0.0, 80.0),
    ("Account", "workspace tag: alpha", "cluster tag: alpha",
     "no per-query cost (all-purpose or interactive)"): (80.0, 0.0, 0.0),
    ("Account", "workspace tag: alpha", "untagged warehouse"): (40.0, 0.0, 0.0),
    ("Account", "workspace tag: alpha", "untagged warehouse", "query tag: q_mkt"): (24.0, 0.0, 24.0),
    ("Account", "workspace tag: alpha", "untagged warehouse", "untagged queries"): (16.0, 0.0, 0.0),
    ("Account", "workspace tag: alpha", "warehouse tag: beta"): (25.0, 0.0, 25.0),
    ("Account", "workspace tag: alpha", "warehouse tag: beta",
     "not split to queries (no attributed usage for that day)"): (25.0, 0.0, 0.0),
    ("Account", "workspace tag: alpha", "cluster tag: poolteam"): (20.0, 0.0, 20.0),
    ("Account", "workspace tag: alpha", "cluster tag: poolteam",
     "no per-query cost (all-purpose or interactive)"): (20.0, 0.0, 0.0),
    ("Account", "workspace tag: alpha", "cluster tag: etl"): (20.0, 0.0, 0.0),
    ("Account", "workspace tag: alpha", "cluster tag: etl", "job tag: etl"): (20.0, 0.0, 20.0),
    ("Account", "workspace tag: alpha", "budget policy tag: shared"): (15.0, 0.0, 15.0),
    ("Account", "workspace tag: alpha", "budget policy tag: shared", "untagged job"): (15.0, 0.0, 0.0),
    ("Account", "workspace tag: alpha", "serverless, no budget policy"): (12.0, 0.0, 0.0),
    ("Account", "workspace tag: alpha", "serverless, no budget policy", "pipeline tag: stream"): (12.0, 0.0, 12.0),
    ("Account", "workspace tag: mixed"): (190.0, 10.0, 0.0),
    ("Account", "workspace tag: mixed", "cluster tag: red"): (100.0, 0.0, 100.0),
    ("Account", "workspace tag: mixed", "cluster tag: red",
     "no per-query cost (all-purpose or interactive)"): (100.0, 0.0, 0.0),
    ("Account", "workspace tag: mixed", "cluster tag: blue"): (80.0, 0.0, 80.0),
    ("Account", "workspace tag: mixed", "cluster tag: blue",
     "no per-query cost (all-purpose or interactive)"): (80.0, 0.0, 0.0),
    ("Account", "workspace tag: mixed", "untagged cluster"): (10.0, 10.0, 0.0),
    ("Account", "workspace tag: mixed", "untagged cluster", "untagged job"): (10.0, 10.0, 0.0),
    ("Account", "untagged workspace"): (120.0, 40.0, 0.0),
    ("Account", "untagged workspace", "untagged warehouse"): (80.0, 40.0, 0.0),
    ("Account", "untagged workspace", "untagged warehouse", "query tag: delta"): (40.0, 0.0, 40.0),
    ("Account", "untagged workspace", "untagged warehouse", "idle or not attributed to a query"): (20.0, 20.0, 0.0),
    ("Account", "untagged workspace", "untagged warehouse", "untagged queries"): (20.0, 20.0, 0.0),
    ("Account", "untagged workspace", "cluster tag: gamma"): (40.0, 0.0, 40.0),
    ("Account", "untagged workspace", "cluster tag: gamma",
     "no per-query cost (all-purpose or interactive)"): (40.0, 0.0, 0.0),
}


def test_pinned_cost_tree_30d():
    body = rollup.rollup("cost", "tg_cc", 30, workspace_ids=TG_WORKSPACES)
    flat = _flatten_tree(body["tree"], _cost_node_metrics)
    _assert_flat_tree(flat, _PINNED_COST_TREE_30D)


@pytest.mark.parametrize("window_days,cluster_alpha_usd,by_value_alpha", [(7, 60.0, 186.0), (90, 100.0, 226.0)])
def test_pinned_cost_tree_7d_90d_deltas(window_days, cluster_alpha_usd, by_value_alpha):
    """section 7.2: '7d and 90d totals differ only in alpha's compute figure: 170 and 210' --
    i.e. only tg_cl_a1 (cluster tag: alpha) moves; everything else in the 30d tree stays put."""
    body = rollup.rollup("cost", "tg_cc", window_days, workspace_ids=TG_WORKSPACES)
    flat = _flatten_tree(body["tree"], _cost_node_metrics)
    path = ("Account", "workspace tag: alpha", "cluster tag: alpha")
    assert abs(flat[path][0] - cluster_alpha_usd) < 0.005
    by_value = _by_value_map(body)
    assert abs(by_value["alpha"]["metrics"]["usd"] - by_value_alpha) < 0.005
    assert abs(by_value["alpha"]["by_level"]["compute"]["usd"] - (cluster_alpha_usd + 110.0)) < 0.005
    assert abs(by_value["alpha"]["by_level"]["workspace"]["usd"] - 16.0) < 0.005


def _by_value_map(body):
    return {e["value"]: e for e in body["by_value"]}


@pytest.mark.parametrize("window_days,total", [(7, 652.0), (30, 672.0), (90, 692.0)])
def test_pinned_cost_totals(window_days, total):
    body = rollup.rollup("cost", "tg_cc", window_days, workspace_ids=TG_WORKSPACES)
    assert body["outcome"] == "ok_rows"
    assert abs(body["total"]["usd"] - total) < 0.005
    assert body["reconciliation"]["reconciled"] is True


_PINNED_BY_VALUE_30D = {
    "alpha": {"usd": 206.0, "work": 0.0, "compute": 190.0, "workspace": 16.0, "overrides_outer": 0.0},
    "red": {"usd": 100.0, "work": 0.0, "compute": 100.0, "workspace": 0.0, "overrides_outer": 0.0},
    "blue": {"usd": 80.0, "work": 0.0, "compute": 80.0, "workspace": 0.0, "overrides_outer": 0.0},
    "q_fin": {"usd": 40.0, "work": 40.0, "compute": 0.0, "workspace": 0.0, "overrides_outer": 40.0},
    "delta": {"usd": 40.0, "work": 40.0, "compute": 0.0, "workspace": 0.0, "overrides_outer": 0.0},
    "gamma": {"usd": 40.0, "work": 0.0, "compute": 40.0, "workspace": 0.0, "overrides_outer": 0.0},
    "beta": {"usd": 25.0, "work": 0.0, "compute": 25.0, "workspace": 0.0, "overrides_outer": 25.0},
    "q_mkt": {"usd": 24.0, "work": 24.0, "compute": 0.0, "workspace": 0.0, "overrides_outer": 24.0},
    "etl": {"usd": 20.0, "work": 20.0, "compute": 0.0, "workspace": 0.0, "overrides_outer": 20.0},
    "poolteam": {"usd": 20.0, "work": 0.0, "compute": 20.0, "workspace": 0.0, "overrides_outer": 20.0},
    "shared": {"usd": 15.0, "work": 0.0, "compute": 15.0, "workspace": 0.0, "overrides_outer": 15.0},
    "stream": {"usd": 12.0, "work": 12.0, "compute": 0.0, "workspace": 0.0, "overrides_outer": 12.0},
    "__untagged__": {"usd": 50.0, "work": 0.0, "compute": 0.0, "workspace": 0.0, "overrides_outer": 0.0},
}


def test_pinned_by_value_30d():
    body = rollup.rollup("cost", "tg_cc", 30, workspace_ids=TG_WORKSPACES)
    by_value = _by_value_map(body)
    assert set(by_value) == set(_PINNED_BY_VALUE_30D)
    for value, expect in _PINNED_BY_VALUE_30D.items():
        entry = by_value[value]
        assert abs(entry["metrics"]["usd"] - expect["usd"]) < 0.005, value
        assert abs(entry["by_level"]["work"]["usd"] - expect["work"]) < 0.005, value
        assert abs(entry["by_level"]["compute"]["usd"] - expect["compute"]) < 0.005, value
        assert abs(entry["by_level"]["workspace"]["usd"] - expect["workspace"]) < 0.005, value
        assert abs(entry["overrides_outer"]["usd"] - expect["overrides_outer"]) < 0.005, value
    assert abs(sum(e["usd"] for e in _PINNED_BY_VALUE_30D.values()) - 672.0) < 0.005


def test_pinned_other_keys_30d():
    body_app = rollup.rollup("cost", "tg_app", 30, workspace_ids=TG_WORKSPACES)
    bv = _by_value_map(body_app)
    assert abs(bv["bi"]["metrics"]["usd"] - 10.0) < 0.005
    assert abs(bv["etl"]["metrics"]["usd"] - 40.0) < 0.005
    assert abs(bv["__untagged__"]["metrics"]["usd"] - 622.0) < 0.005

    body_env = rollup.rollup("cost", "tg_env", 30, workspace_ids=TG_WORKSPACES)
    bv2 = _by_value_map(body_env)
    assert abs(bv2["prod"]["metrics"]["usd"] - 150.0) < 0.005
    assert abs(bv2["__untagged__"]["metrics"]["usd"] - 522.0) < 0.005


# -------------------------------------------------------------------------------------------
# 4. Performance: pinned tree (section 7.3) and the statement-count reconciliation.
# -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("window_days,stmts,failed,dur,queue,spill", [
    (7, 9, 2, 59000, 3500, 1500),
    (30, 9, 2, 59000, 3500, 1500),
    (90, 10, 2, 66000, 3500, 1500),
])
def test_pinned_perf_totals(window_days, stmts, failed, dur, queue, spill):
    body = rollup.rollup("performance", "tg_cc", window_days, workspace_ids=TG_WORKSPACES)
    assert body["outcome"] == "ok_rows"
    t = body["total"]
    assert t["statements"] == stmts
    assert t["failed_statements"] == failed
    assert t["duration_ms"] == dur
    assert t["queue_ms"] == queue
    assert t["spill_bytes"] == spill
    assert body["reconciliation"]["reconciled"] is True


# section 7.3's tree, exactly -- (statements, failed_statements, duration_ms, queue_ms, spill_bytes)
# per node. 7d is identical to 30d (the spec says so outright), so one pinned tree covers both.
_PINNED_PERF_TREE_30D = {
    ("Account",): (9, 2, 59000, 3500, 1500),
    ("Account", "workspace tag: alpha"): (7, 1, 51000, 3500, 1000),
    ("Account", "workspace tag: alpha", "warehouse tag: alpha"): (4, 1, 38000, 1500, 1000),
    ("Account", "workspace tag: alpha", "warehouse tag: alpha", "query tag: q_fin"): (2, 0, 30000, 1000, 1000),
    ("Account", "workspace tag: alpha", "warehouse tag: alpha", "untagged queries"): (2, 1, 8000, 500, 0),
    ("Account", "workspace tag: alpha", "untagged warehouse"): (2, 0, 12000, 2000, 0),
    ("Account", "workspace tag: alpha", "untagged warehouse", "query tag: q_mkt"): (1, 0, 8000, 2000, 0),
    ("Account", "workspace tag: alpha", "untagged warehouse", "untagged queries"): (1, 0, 4000, 0, 0),
    ("Account", "workspace tag: alpha", "serverless compute (no tags)"): (1, 0, 1000, 0, 0),
    ("Account", "workspace tag: alpha", "serverless compute (no tags)", "untagged queries"): (1, 0, 1000, 0, 0),
    ("Account", "untagged workspace"): (2, 1, 8000, 0, 500),
    ("Account", "untagged workspace", "untagged warehouse"): (2, 1, 8000, 0, 500),
    ("Account", "untagged workspace", "untagged warehouse", "query tag: delta"): (1, 0, 6000, 0, 500),
    ("Account", "untagged workspace", "untagged warehouse", "untagged queries"): (1, 1, 2000, 0, 0),
}


@pytest.mark.parametrize("window_days", [7, 30])
def test_pinned_perf_tree(window_days):
    body = rollup.rollup("performance", "tg_cc", window_days, workspace_ids=TG_WORKSPACES)
    flat = _flatten_tree(body["tree"], _perf_node_metrics)
    _assert_flat_tree(flat, _PINNED_PERF_TREE_30D, tol=0.5)


def test_pinned_perf_tree_90d_q_fin_change():
    """section 7.3: at 90d, tg_stmt_10 (D(40)) adds 1 statement and 7,000 ms under q_fin -- the
    only node that moves between the 30d and 90d trees."""
    body = rollup.rollup("performance", "tg_cc", 90, workspace_ids=TG_WORKSPACES)
    flat = _flatten_tree(body["tree"], _perf_node_metrics)
    path = ("Account", "workspace tag: alpha", "warehouse tag: alpha", "query tag: q_fin")
    assert flat[path] == (3, 0, 37000, 1000, 1000)


def test_perf_statement_count_matches_query_history_unfiltered():
    con = _connect()
    try:
        hist_total = con.execute(
            f"""
            SELECT count(*)
            FROM read_parquet('{(PARQUET_DIR / "query__history" / "*.parquet").as_posix()}', union_by_name=true)
            WHERE start_time >= DATE '2026-09-21' - INTERVAL 30 DAY AND start_time < DATE '2026-09-21'
            """
        ).fetchone()[0]
        unit_total = con.execute(
            "SELECT SUM(statements) FROM tags.perf_unit WHERE window_days = 30"
        ).fetchone()[0]
    finally:
        con.close()
    assert hist_total == unit_total


# -------------------------------------------------------------------------------------------
# 5. Warehouse split specifics.
# -------------------------------------------------------------------------------------------


def test_warehouse_split_scaled_day():
    con = _connect()
    try:
        row = con.execute(
            "SELECT scaled_warehouse_days FROM tags.cost_reconciliation WHERE window_days = 30 AND usage_unit = 'DBU'"
        ).fetchone()
    finally:
        con.close()
    assert row[0] >= 1

    body = rollup.rollup("cost", "tg_cc", 30, workspace_ids=TG_WORKSPACES)

    def find(node, label_prefix):
        if node["label"].startswith(label_prefix):
            yield node
        for c in node.get("children", []):
            yield from find(c, label_prefix)

    wh_a2_nodes = [n for n in find(body["tree"], "untagged warehouse") if abs(n["metrics"]["usd"] - 40.0) < 0.005]
    assert wh_a2_nodes, "expected the tg_wh_a2 untagged-warehouse node at usd=40"
    node = wh_a2_nodes[0]
    child_labels = {c["label"]: c["metrics"]["usd"] for c in node["children"]}
    assert abs(child_labels.get("query tag: q_mkt", -1) - 24.0) < 0.005
    assert abs(child_labels.get("untagged queries", -1) - 16.0) < 0.005
    assert not any("idle" in lbl for lbl in child_labels)


def test_warehouse_a1_and_a3_not_split():
    con = _connect()
    try:
        rows = con.execute(
            "SELECT compute_id, work_kind, usd FROM tags.cost_unit "
            "WHERE window_days = 30 AND compute_id IN ('tg_wh_a1', 'tg_wh_a3')"
        ).fetchall()
    finally:
        con.close()
    by_id = {}
    for compute_id, work_kind, usd in rows:
        by_id.setdefault(compute_id, {})[work_kind] = usd
    assert abs(by_id["tg_wh_a1"]["warehouse_unsplit"] - 50.0) < 0.005
    assert abs(by_id["tg_wh_a3"]["warehouse_unsplit"] - 25.0) < 0.005


# -------------------------------------------------------------------------------------------
# 6. Sources not exported (section 6.5) -- patched manifest, via AUDIT_SNAPSHOT_MANIFEST.
# -------------------------------------------------------------------------------------------


def _patched_manifest(tmp_path, mutate):
    import os

    manifest_path = Path(os.environ["AUDIT_SNAPSHOT_MANIFEST"])
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data = copy.deepcopy(data)
    mutate(data)
    out = tmp_path / "manifest_patched.json"
    out.write_text(json.dumps(data), encoding="utf-8")
    return out


def test_attributed_usage_not_exported_degrades_but_stays_ok(tmp_path, monkeypatch):
    def mutate(data):
        data["tables"]["system.billing.attributed_usage"] = {"state": "not_exported", "rows": 0}

    patched = _patched_manifest(tmp_path, mutate)
    monkeypatch.setenv("AUDIT_SNAPSHOT_MANIFEST", str(patched))
    body = rollup.rollup("cost", "tg_cc", 30, workspace_ids=TG_WORKSPACES)
    assert body["outcome"] == "ok_rows"
    sources = {s["source"]: s for s in body["source_status"]}
    assert "system.billing.attributed_usage" in sources
    assert sources["system.billing.attributed_usage"]["effect"]


def test_billing_usage_not_exported_is_not_assessed(tmp_path, monkeypatch):
    def mutate(data):
        data["tables"]["system.billing.usage"] = {"state": "not_exported", "rows": 0}

    patched = _patched_manifest(tmp_path, mutate)
    monkeypatch.setenv("AUDIT_SNAPSHOT_MANIFEST", str(patched))
    body = rollup.rollup("cost", "tg_cc", 30, workspace_ids=TG_WORKSPACES)
    assert body["outcome"] == "not_assessed"
    assert "system.billing.usage" in body["not_assessed_reason"]
