"""tests/test_findings/test_serving_storage.py -- T-22.

Proves, against tests/fixtures/serving_storage.py's own `ss_`-prefixed rows and only those rows
(DEC-15), that each of the 9 built findings.f_<query_id> tables this task owns (4 serving_ai + 5
storage) has the right grain, the right window_days behaviour at 7/30/90 (and [] at 0), and --
for the 6 status-carrying ids -- a reachable WARN and CRITICAL (and, where the body emits one, a
reachable NOT_ASSESSED) band by id. Per tests/test_findings/README.md's checklist and this task's
own Steps/Inputs sections.

Two ids share their source tables with tests/fixtures/ports.py's (T-11) `pt_`-prefixed rows
(served_entities, endpoint_usage) -- every assertion below filters to this builder's own `ss_`
ids, never asserting over the whole result set, matching test_storage_target_table_discovery.py's
and test_compute_serving_dormant_endpoints.py's own pattern.

Two engine-level findings, discovered with an in-memory duckdb.connect() sanity run against the
REAL generated model SQL (dbt/models/findings/serving_ai/f_*.sql) before this file was written --
not assumed, not guessed at:

1. FIXED (P4-FIXES28): compute_serving_endpoint_cost_status's own `ORDER BY CASE status WHEN
   'CRITICAL' THEN 0 ...` used to not sort worst-first on DuckDB 1.5.1. The query LEFT JOINs
   system.access.workspaces_latest (aliased `w`), which has a REAL column named `status`
   (ACTIVE/etc., billing.py's own `status` argument) -- the SAME name as this query's own computed
   `status` output alias, and DuckDB resolved the unqualified `status` in ORDER BY to the JOINED
   TABLE's column, not the SELECT-list alias, so the CASE fell through to the ELSE branch for every
   row and the sort was governed by `net_dbus DESC` alone. The scored rows now come from a derived
   table (`scored`/`FROM scored ORDER BY ...`), so the unqualified `status` in ORDER BY binds to
   that derived table's own column and cannot see workspaces_latest.status at all -- worst-first
   ordering is asserted below for this id too.
2. compute_serving_endpoint_usage's `entities` CTE used to write `SELECT se.*, ROW_NUMBER() OVER
   (PARTITION BY se.workspace_id, se.endpoint_id, se.served_entity_id ORDER BY se.change_time
   DESC) AS _rn FROM system.serving.served_entities se`, then an outer `SELECT workspace_id,
   endpoint_id, endpoint_name, served_entity_id, served_entity_name, entity_type, entity_name FROM
   (...) WHERE _rn = 1` referencing those names post-wildcard. On DuckDB 1.5.1 this reproducibly
   mis-bound three of those names (workspace_id / endpoint_id / served_entity_id got each other's
   values internally -- endpoint_name/served_entity_name/entity_type/entity_name were unaffected),
   confirmed at the time against BOTH a native DuckDB table and a `read_parquet(...)`-backed view
   (matching how dbt-duckdb sources are actually registered), and confirmed deterministic in its
   WRONG-ness (not random per run, though row ORDER without an ORDER BY was). Because the
   corrupted entities.workspace_id/served_entity_id no longer equalled the real values, the outer
   query's `LEFT JOIN entities ent ON ...` never matched any row, so every column sourced from
   `ent.*` came back NULL, and (downstream) `net_dbus`/`est_usd_list` -- keyed off
   `finding.endpoint_id`, itself `ent.endpoint_id` -- came back COALESCE(...,0) = 0 too. This was
   real, reported, and fixed: T-36 (landed in the same commit as this task's own work, d2806d8)
   added `app/core/translate.py`'s `_r_row_number_star_to_qualify` rule, which rewrites this exact
   shape into `(SELECT a.* FROM src a QUALIFY ROW_NUMBER() OVER (w) = 1)` for the duckdb target --
   the generated model's DuckDB branch now reads `QUALIFY ROW_NUMBER() OVER (...) = 1` instead of
   the wildcard-then-`WHERE _rn = 1` shape, and DuckDB binds it correctly. Re-verified with an
   in-memory duckdb.connect() sanity run against the CURRENT
   dbt/models/findings/serving_ai/f_compute_serving_endpoint_usage.sql (review round 2) before
   this file was revised: ss_se_ok's row now reads exactly (2026-09-18, '1111', 'ss_ep_ok',
   'ss-ep-ok-endpoint', 'ss_se_ok', 'ss-se-ok-entity', 'FOUNDATION_MODEL', 'ss-se-ok-entity', 15, 12.0) --
   endpoint_id/endpoint_name/served_entity_name/entity_type/entity_name/net_dbus all live and
   correct. This test module therefore DOES assert all of those columns now (see
   test_compute_serving_endpoint_usage below); the description above is kept because it is an
   accurate account of a real defect this task discovered and because
   compute_serving_endpoint_cost_status's own `entities` CTE (finding 1's subject) uses an
   EXPLICIT column list, not `se.*`, and was never affected by this particular bug (finding 1's
   ORDER BY issue there is separate and still live).
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR / "fixtures"))
import dbutil  # noqa: E402
import serving_storage as ss  # noqa: E402
import waste_usd as wu  # noqa: E402 -- P3-WASTEUSD review fix's own two-served-entity builder

GRAINS_PATH = ROOT / "config" / "grains" / "serving_storage.yml"
WS_PROD = "1111"
WINDOWS = (7, 30, 90)
STATUS_VALUES = {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}

# CRITICAL/WARN/NOT_ASSESSED/OK convention (compute_serving_endpoint_cost_status's own CASE).
RANK_NA_BEFORE_OK = {"CRITICAL": 0, "WARN": 1, "NOT_ASSESSED": 2, "OK": 3}
# CRITICAL/WARN/OK/NOT_ASSESSED convention (all 5 po_* ids' own CASE / DuckDB's NULLS-LAST default
# on po_clustering_activity's plain `ORDER BY clustering_estimated_dbu DESC` -- see the module
# docstring in tests/fixtures/serving_storage.py for the verified derivation of this ranking).
RANK_NA_LAST = {"CRITICAL": 0, "WARN": 1, "OK": 2, "NOT_ASSESSED": 3}

STATUS_IDS = [
    "compute_serving_endpoint_cost_status",
    "po_clustering_activity", "po_clustering_column_churn", "po_data_skipping_backfill",
    "po_maintenance_cost_by_table", "po_vacuum_reclaimed_bytes",
]
INVENTORY_IDS = [
    "compute_ai_gateway_usage", "compute_serving_endpoint_usage", "serving_endpoint_traffic_by_endpoint",
]
ALL_IDS = [
    "compute_ai_gateway_usage", "compute_serving_endpoint_cost_status", "compute_serving_endpoint_usage",
    "serving_endpoint_traffic_by_endpoint", "po_clustering_activity", "po_clustering_column_churn",
    "po_data_skipping_backfill", "po_maintenance_cost_by_table", "po_vacuum_reclaimed_bytes",
]

# D(n) anchors -- computed from tests/fixtures/serving_storage.py's own D()/WIN_* (review round 2,
# item 8: this module is the only one of the three T-15/T-21/T-22 batch files that did not import
# its own fixture module and instead retyped these as date() literals; deriving them from `ss.D()`
# means a future rename of WIN_7/WIN_30/WIN_90 or a change to AS_OF fails this module loudly
# instead of silently testing the wrong row).
D3, D16, D45, D68 = ss.D(ss.WIN_7), ss.D(ss.WIN_30), ss.D(45), ss.D(ss.WIN_90)


def _grains():
    with open(GRAINS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _po_rows(query_id: str, w: int):
    """rows() for a po_* id, filtered to this builder's own ss_-prefixed table_id (po_* ids never
    carry a workspace_id/served_entity_id column to filter on instead)."""
    return [r for r in dbutil.rows(query_id, w) if r["table_id"].startswith("ss_")]


def _assert_worst_first(out, rank_map, metric_key):
    """Review round 2 (item 3): a filtered 3-element subsequence (`[r for r in out if
    r["table_id"] in (crit, warn, ok)]`) can look sorted by accident under DuckDB's hash-aggregate
    bucket order even with no ORDER BY at all (T-16's own finding) -- for these 5 po_* ids `out` IS
    already the whole list read from findings.f_<id> (storage__predictive_optimization_operations_
    history has exactly one builder, this one), so this checks worst-first over the WHOLE list:
    rank monotonicity (status never gets more severe later in the list) plus, within each
    consecutive same-status run, `metric_key` non-increasing (the model's own DESC secondary sort
    key) -- skipping the comparison wherever `metric_key` is None (the NOT_ASSESSED rows)."""
    ranks = [rank_map[r["status"]] for r in out]
    assert ranks == sorted(ranks), (
        f"not worst-first over the whole list: {[(r['table_id'], r['status']) for r in out]}"
    )
    for a, b in zip(out, out[1:]):
        if a["status"] == b["status"] and a[metric_key] is not None and b[metric_key] is not None:
            assert a[metric_key] >= b[metric_key], (
                f"tiebreak violated within status {a['status']!r}: "
                f"{a['table_id']}={a[metric_key]} before {b['table_id']}={b[metric_key]}"
            )


def _assert_order_desc(values):
    """values, in the order dbutil.rows() returned them (no ORDER BY of its own applied by
    dbutil), must already be non-increasing -- used for a plain `<col> DESC` order_by (no CASE
    status), over the WHOLE list dbutil.rows() returns (never a filtered subsequence -- same
    review-round-2 reasoning as _assert_worst_first above)."""
    assert values == sorted(values, reverse=True), f"not sorted DESC: {values}"


# -------------------------------------------------------------------------------------------
# Generic checks over all 9 ids.
# -------------------------------------------------------------------------------------------
def test_window_days_zero_is_empty_for_all_ids():
    for qid in ALL_IDS:
        assert dbutil.rows(qid, 0) == [], f"{qid}: window_days=0 must be empty (windowed :period_days query)"


def test_status_enum_for_status_ids():
    for qid in STATUS_IDS:
        for w in WINDOWS:
            for r in dbutil.rows(qid, w):
                assert r["status"] in STATUS_VALUES, f"{qid} w={w}: bad status {r['status']!r}"


def test_no_status_column_for_inventory_ids():
    """compute_ai_gateway_usage / compute_serving_endpoint_usage / serving_endpoint_traffic_by_
    endpoint have `healthy: n/a - inventory` and no `AS status` anywhere in their bodies (DEC-08) --
    the built table therefore has no `status` column at all."""
    for qid in INVENTORY_IDS:
        rows = dbutil.rows(qid, 30)
        assert rows, f"{qid}: expected >=1 row at window_days=30"
        assert "status" not in rows[0], f"{qid}: unexpected status column {rows[0].keys()}"


def test_grain_uniqueness_all_ids():
    grains = _grains()
    for qid in ALL_IDS:
        cols = grains[qid]
        out = dbutil.rows(qid, 30)
        keys = [tuple(r[c] for c in cols) for r in out]
        assert len(keys) == len(set(keys)), f"{qid}: duplicate grain rows at window_days=30"


# -------------------------------------------------------------------------------------------
# compute_ai_gateway_usage -- status_code 3-way split, requester identity (raw), raw
# endpoint_name, window boundary. No status column (inventory).
# -------------------------------------------------------------------------------------------
def test_compute_ai_gateway_usage():
    out = dbutil.rows("compute_ai_gateway_usage", 30, workspace_ids=[WS_PROD])
    d3_rows = [r for r in out if r["usage_date"] == D3]

    by_key = {(r["endpoint_name"], r["requester"]): r for r in d3_rows}

    # group a: 'ss-gw-ep1'/'ss_alice@example.com' -- 3x success (2xx).
    a = by_key[("ss-gw-ep1", "ss_alice@example.com")]
    assert a["total_requests"] == 3
    assert a["success_requests"] == 3
    assert a["rate_limited_requests"] == 0
    assert a["error_requests"] == 0
    assert a["input_tokens"] == 300  # 3 x 100
    assert a["output_tokens"] == 150  # 3 x 50
    assert a["max_latency_ms"] == 300
    # percentile -> quantile_cont translation, exact on both build paths (tests/test_translate.py
    # proves this at unit level; this pins it end to end on a real multi-row group).
    # quantile_cont on sorted [100, 150, 300]: p50 = the exact middle value; p95 interpolates
    # 0.95*(3-1)=1.9 of the way from index 1 (150) to index 2 (300) = 150 + 0.9*150 = 285.
    assert a["p50_latency_ms"] == 150.0
    assert a["p95_latency_ms"] == 285.0

    # group b: same endpoint, GUID requester (passthrough) -- 2x rate-limited (429).
    b = by_key[("ss-gw-ep1", "11112222-3333-4444-5555-666677778888")]
    assert b["total_requests"] == 2
    assert b["success_requests"] == 0
    assert b["rate_limited_requests"] == 2
    assert b["error_requests"] == 0

    # group c: same endpoint, 'zz_carter' -- 1x error (500: not 2xx, not 429).
    c = by_key[("ss-gw-ep1", "zz_carter")]
    assert c["total_requests"] == 1
    assert c["success_requests"] == 0
    assert c["rate_limited_requests"] == 0
    assert c["error_requests"] == 1

    # group d: same endpoint, requester NULL (passthrough) -- 1x success.
    d = by_key[("ss-gw-ep1", None)]
    assert d["total_requests"] == 1
    assert d["success_requests"] == 1

    # group e: endpoint_name NULL (passthrough), requester 'ss_dana@example.com' -- 1x success.
    e = by_key[(None, "ss_dana@example.com")]
    assert e["total_requests"] == 1
    assert e["success_requests"] == 1

    # window-boundary series: endpoint 'qq-gw-win-ep' -- a prefix used by no other row in this
    # (day, workspace) group.
    for w, expect_days in [(7, {D3}), (30, {D3, D16}), (90, {D3, D16, D68})]:
        out_w = dbutil.rows("compute_ai_gateway_usage", w, workspace_ids=[WS_PROD])
        days_present = {r["usage_date"] for r in out_w if r["endpoint_name"] == "qq-gw-win-ep"}
        assert days_present == expect_days, (w, days_present, expect_days)

    # order_by (review round 2, item 4): usage_date DESC, total_requests DESC -- over the WHOLE
    # table (ai_gateway__usage is written by no other builder, so this is already the full result;
    # this table has no `status` column at all, so there is no CASE-status key to be neutralized
    # by any name collision the way compute_serving_endpoint_cost_status's is).
    keys = [(-r["usage_date"].toordinal(), -r["total_requests"]) for r in dbutil.rows("compute_ai_gateway_usage", 90)]
    assert keys == sorted(keys), keys


# -------------------------------------------------------------------------------------------
# compute_serving_endpoint_cost_status -- 4-band ladder (OK/WARN/CRITICAL/NOT_ASSESSED), two
# distinct WARN flavors, two distinct NOT_ASSESSED flavors, a window-driven CRITICAL->WARN flip,
# and SCD2 dedup proof. Worst-first ordering is NOT asserted here -- see the module docstring
# above for the confirmed DuckDB column-collision reason (w.status shadows the output alias).
# -------------------------------------------------------------------------------------------
def test_compute_serving_endpoint_cost_status():
    def by_eid(w):
        return {r["endpoint_id"]: r for r in dbutil.rows("compute_serving_endpoint_cost_status", w)
                if r["endpoint_id"] and r["endpoint_id"].startswith("ss_")}

    r30 = by_eid(30)
    for eid in ("ss_ep_ok", "ss_ep_win", "ss_ep_warn_low", "ss_ep_trackingoff",
                "ss_ep_idlewindow", "ss_ep_untracked", "ss_ep_vs"):
        assert eid in r30, f"missing endpoint_id={eid!r} at window_days=30"

    # OK: ample traffic (15 requests, all at D(3)).
    assert r30["ss_ep_ok"]["status"] == "OK"
    assert r30["ss_ep_ok"]["net_dbus"] == 12.0
    # est_usd_list = usage_quantity * this builder's own ss_MODEL_SERVING_STANDARD rate (review
    # round 2, item 2: this was 0.0 for the whole serving domain before serving_storage.py wrote
    # its own billing__list_prices rows -- ROUND(ce.est_usd_list, 2) in the model's own SELECT).
    assert abs(r30["ss_ep_ok"]["est_usd_list"] - 12.0 * 0.10) < 1e-6
    assert r30["ss_ep_ok"]["price_basis"] == "priced"  # ss_MODEL_SERVING_STANDARD is priced (DEC-66.1)
    assert r30["ss_ep_ok"]["entity_requests_window"] == 15
    assert r30["ss_ep_ok"]["endpoint_requests_window"] == 15.0
    # SCD2 dedup proof: the NEW served_entities row (entity_version '1.0', change_time D(3), inside
    # every window) wins over the OLD one ('0.9', D(400)) via ROW_NUMBER() ... ORDER BY change_time
    # DESC.
    assert r30["ss_ep_ok"]["entity_version"] == "1.0"
    assert r30["ss_ep_ok"]["latest_change_time"] == datetime(2026, 9, 18, 0, 0, 0)

    # WARN flavor 1: low request count (5 <= warn_low_requests(10)), tracking IS on (ever_requests>0).
    assert r30["ss_ep_warn_low"]["status"] == "WARN"
    assert r30["ss_ep_warn_low"]["tracking_status"] == "tracking ON - active"
    assert r30["ss_ep_warn_low"]["entity_requests_window"] == 5

    # WARN flavor 2: usage tracking OFF (bills, zero endpoint_usage rows EVER).
    assert r30["ss_ep_trackingoff"]["status"] == "WARN"
    assert "tracking likely OFF" in r30["ss_ep_trackingoff"]["tracking_status"]
    assert r30["ss_ep_trackingoff"]["entity_requests_window"] == 0

    # NOT_ASSESSED flavor 1: bills but not in served_entities.
    assert r30["ss_ep_untracked"]["status"] == "NOT_ASSESSED"
    assert r30["ss_ep_untracked"]["served_entity_id"] is None
    assert "not in served_entities" in r30["ss_ep_untracked"]["tracking_status"]

    # NOT_ASSESSED flavor 2: Vector Search (no serving telemetry).
    assert r30["ss_ep_vs"]["status"] == "NOT_ASSESSED"
    assert "Vector Search" in r30["ss_ep_vs"]["tracking_status"]
    assert abs(r30["ss_ep_vs"]["net_dbus"] - 5.0) < 1e-6
    assert abs(r30["ss_ep_vs"]["est_usd_list"] - 5.0 * 0.12) < 1e-6

    # NOT_ASSESSED flavor 2b: Vector Search billed BY NAME, not by id (review round 2, item 6) --
    # usage_metadata.endpoint_id NULL, endpoint_name='ss-ep-byname' only. by_eid's dict is keyed on
    # endpoint_id and drops every row with endpoint_id IS NULL by construction (see its own `if
    # r["endpoint_id"] and ...` filter), so this row needs its own lookup: it is the ONLY row in
    # the whole built table (across every builder) with endpoint_id IS NULL and a non-NULL
    # products/net_dbus (confirmed by reading every other fixture builder -- none of them ever
    # writes a billing row with an endpoint_name but no endpoint_id; see
    # tests/fixtures/serving_storage.py's own comment on this row for the fuller reasoning).
    byname_rows = [r for r in dbutil.rows("compute_serving_endpoint_cost_status", 30)
                   if r["endpoint_id"] is None]
    assert len(byname_rows) == 1, byname_rows
    byname = byname_rows[0]
    assert byname["status"] == "NOT_ASSESSED"
    assert "Vector Search" in byname["tracking_status"]
    assert byname["served_entity_id"] is None
    assert byname["endpoint_name"] == "ss-ep-byname"  # a resource name, never masked
    assert abs(byname["net_dbus"] - 4.0) < 1e-6
    assert abs(byname["est_usd_list"] - 4.0 * 0.12) < 1e-6

    # Window-boundary entity: OK at every window (11/12/13 requests, always > warn_low_requests).
    assert r30["ss_ep_win"]["status"] == "OK"
    assert r30["ss_ep_win"]["entity_requests_window"] == 12

    # Deliberate status flip: CRITICAL at w=7/30 (idle in window: tracked, billed, 0 requests in
    # the period window), WARN at w=90 (those same 8 requests now fall inside the period window,
    # and 8 <= warn_low_requests(10)).
    r7 = by_eid(7)
    r90 = by_eid(90)
    assert r7["ss_ep_idlewindow"]["status"] == "CRITICAL"
    assert r30["ss_ep_idlewindow"]["status"] == "CRITICAL"
    assert r90["ss_ep_idlewindow"]["status"] == "WARN"
    assert r90["ss_ep_idlewindow"]["entity_requests_window"] == 8

    # P3-WASTEUSD: est_wasted_usd_list mirrors the status CASE's own CRITICAL branch. Confirmed
    # idle (CRITICAL) -> the endpoint's WHOLE window est_usd_list is waste (9.0 DBU * 0.10/DBU =
    # 0.9, ROUND(.,2)); the SAME endpoint at w=90 (WARN: real, if low, traffic) -> 0, never the
    # billed amount. Real-but-low traffic (OK/WARN) -> 0. Tracking-off / NOT_ASSESSED -> NULL,
    # never 0 (idle cannot be confirmed there, so no waste is claimed).
    assert abs(r7["ss_ep_idlewindow"]["est_wasted_usd_list"] - 0.9) < 1e-6
    assert abs(r30["ss_ep_idlewindow"]["est_wasted_usd_list"] - 0.9) < 1e-6
    assert r30["ss_ep_idlewindow"]["est_wasted_usd_list"] == r30["ss_ep_idlewindow"]["est_usd_list"]
    assert r90["ss_ep_idlewindow"]["est_wasted_usd_list"] == 0

    assert r30["ss_ep_ok"]["est_wasted_usd_list"] == 0
    assert r30["ss_ep_warn_low"]["est_wasted_usd_list"] == 0
    assert r30["ss_ep_win"]["est_wasted_usd_list"] == 0
    assert r30["ss_ep_trackingoff"]["est_wasted_usd_list"] is None
    assert r30["ss_ep_untracked"]["est_wasted_usd_list"] is None
    assert r30["ss_ep_vs"]["est_wasted_usd_list"] is None
    assert byname["est_wasted_usd_list"] is None

    # ss_ep_win's own requests_window differs precisely by window (11/12/13), status stays OK.
    assert r7["ss_ep_win"]["entity_requests_window"] == 11
    assert r90["ss_ep_win"]["entity_requests_window"] == 13
    for w in WINDOWS:
        assert by_eid(w)["ss_ep_win"]["status"] == "OK"

    # The two NOT_ASSESSED and two WARN flavors, and OK, are stable across every window (billing
    # always lands at D(3), inside every one of 7/30/90).
    for w in WINDOWS:
        r = by_eid(w)
        assert r["ss_ep_untracked"]["status"] == "NOT_ASSESSED"
        assert r["ss_ep_vs"]["status"] == "NOT_ASSESSED"
        assert r["ss_ep_warn_low"]["status"] == "WARN"
        assert r["ss_ep_trackingoff"]["status"] == "WARN"
        assert r["ss_ep_ok"]["status"] == "OK"

    # Worst-first over the WHOLE table (review-fix: the `scored` rows now come from a derived
    # table, so the ORDER BY's status CASE binds to this query's own status column instead of the
    # LEFT-JOINed workspaces_latest.status that used to neutralize it -- see the module docstring's
    # finding 1, now fixed). order_by is CASE status (CRITICAL/WARN/NOT_ASSESSED/OK), net_dbus
    # DESC, endpoint_requests_window ASC.
    rank = {"CRITICAL": 0, "WARN": 1, "NOT_ASSESSED": 2, "OK": 3}
    for w in WINDOWS:
        out = dbutil.rows("compute_serving_endpoint_cost_status", w)
        ranks = [rank[r["status"]] for r in out]
        assert ranks == sorted(ranks), (w, [(r["endpoint_id"], r["status"]) for r in out])
        for a, b in zip(out, out[1:]):
            if a["status"] != b["status"]:
                continue
            an, bn = a["net_dbus"], b["net_dbus"]
            if an is not None and bn is not None and an != bn:
                assert an >= bn, (w, a["endpoint_id"], b["endpoint_id"], an, bn)
            elif an == bn:
                assert a["endpoint_requests_window"] <= b["endpoint_requests_window"], (
                    w, a["endpoint_id"], b["endpoint_id"],
                )


def test_compute_serving_endpoint_cost_status_multi_entity_waste_split():
    """P4-FIXES28 review fix (entity-fan-out-and-order-by): entity_agg now rolls every served
    entity up to ONE row per (workspace_id, endpoint_id) -- an endpoint serving more than one
    entity no longer fans out at all, so est_wasted_usd_list is never split or double-counted; it
    is simply the endpoint's own whole spend, same as a single-entity endpoint.
    tests/fixtures/waste_usd.py's wu_ep_idle2 serves two entities (wu_se_a, wu_se_b) and is
    confirmed idle (CRITICAL) at w=7/30, same shape as ss_ep_idlewindow above but exercising the
    multi-entity rollup a single-entity endpoint cannot."""
    for w in (7, 30):
        wrows = [r for r in dbutil.rows("compute_serving_endpoint_cost_status", w)
                 if r["endpoint_id"] == wu.WU_EP_IDLE2]
        assert len(wrows) == 1, (w, wrows)
        r = wrows[0]
        assert r["served_entity_count"] == 2, (w, r)
        assert r["primary_served_entity_id"] in (wu.WU_SE_A, wu.WU_SE_B), (w, r)
        assert r["status"] == "CRITICAL", (w, r)
        # net_dbus=10.0 * this builder's own $0.20/DBU rate = 2.00, the whole endpoint's spend.
        assert abs(r["est_usd_list"] - 2.0) < 1e-6, (w, r["est_usd_list"])
        # est_wasted_usd_list is the WHOLE spend, never split or double-counted (one row now).
        assert abs(r["est_wasted_usd_list"] - 2.0) < 1e-6, (w, r["est_wasted_usd_list"])
        assert r["est_wasted_usd_list"] == r["est_usd_list"]

    # w=90: the same 4 D(45) requests now fall inside the period window (45 < 90) -> WARN
    # (low-but-real traffic, 4 <= warn_low_requests(10)); est_wasted_usd_list=0, never the billed
    # amount -- the same CRITICAL-at-w7/30-then-WARN-at-w90 flip ss_ep_idlewindow uses above.
    wrows90 = [r for r in dbutil.rows("compute_serving_endpoint_cost_status", 90)
               if r["endpoint_id"] == wu.WU_EP_IDLE2]
    assert len(wrows90) == 1, wrows90
    assert wrows90[0]["status"] == "WARN", wrows90[0]
    assert wrows90[0]["est_wasted_usd_list"] == 0, wrows90[0]


# -------------------------------------------------------------------------------------------
# compute_serving_endpoint_usage -- "with and without traffic" served entities, window
# inclusion/exclusion (including full absence, not a zero-count row, since this id is anchored
# FROM endpoint_usage), success/error split, SCD2-dedup proof (entity_type), and
# endpoint_id/endpoint_name/served_entity_name/entity_type/entity_name/net_dbus/est_usd_list --
# T-36's QUALIFY rewrite (see module docstring, finding 2) fixed the engine defect that used to
# NULL/zero all of these; re-verified here against the current generated model, not assumed.
# -------------------------------------------------------------------------------------------
def test_compute_serving_endpoint_usage():
    def by_entity_day(w):
        out = dbutil.rows("compute_serving_endpoint_usage", w)
        return {(r["served_entity_id"], r["usage_date"]): r for r in out
                if r["served_entity_id"] and r["served_entity_id"].startswith("ss_")}

    r7, r30, r90 = by_entity_day(7), by_entity_day(30), by_entity_day(90)

    # "WITH traffic": ss_se_ok, 15 requests at D(3) (13 success / 2 error), present at every
    # window. endpoint_id/endpoint_name/served_entity_name/entity_type/entity_name/net_dbus/
    # est_usd_list all come from the `entities` CTE / cost_rollup join T-36 fixed (finding 2) --
    # entity_type=='FOUNDATION_MODEL' (not the OLD row's 'CUSTOM_MODEL') is this id's own SCD2-
    # dedup proof (review round 2, item 1), since this id's output has no entity_version column.
    for r in (r7, r30, r90):
        row = r[("ss_se_ok", D3)]
        assert row["total_requests"] == 15
        assert row["success_requests"] == 13
        assert row["error_requests"] == 2
        assert row["input_tokens"] == 15 * 100
        assert row["output_tokens"] == 15 * 50
        assert row["endpoint_id"] == "ss_ep_ok"
        assert row["endpoint_name"] == "ss-ep-ok-endpoint"  # a resource name, never masked
        assert row["served_entity_name"] == "ss-se-ok-entity"  # a resource name, never masked
        assert row["entity_type"] == "FOUNDATION_MODEL"
        assert row["entity_name"] == "ss-se-ok-entity"
        assert row["net_dbus"] == 12.0
        assert abs(row["est_usd_list"] - 12.0 * 0.10) < 1e-6
        assert row["price_basis"] == "priced"

    # "WITHOUT traffic": ss_se_trackingoff has a served_entities row but ZERO endpoint_usage rows
    # -- this id is anchored FROM endpoint_usage, so it is wholly ABSENT from the output at every
    # window (not a zero-count row).
    for r in (r7, r30, r90):
        assert not any(k[0] == "ss_se_trackingoff" for k in r), (
            "ss_se_trackingoff (no endpoint_usage rows) must be absent, not a zero-count row"
        )

    # ss_se_idlewindow: 8 requests all at D(45) -- wholly absent at w=7/30 (D(45) outside both),
    # present with total_requests=8 only at w=90.
    assert not any(k[0] == "ss_se_idlewindow" for k in r7)
    assert not any(k[0] == "ss_se_idlewindow" for k in r30)
    idle90 = r90[("ss_se_idlewindow", D45)]
    assert idle90["total_requests"] == 8
    assert idle90["success_requests"] == 8

    # ss_se_win: window-boundary series -- 11 @ D(3), +1 @ D(16), +1 @ D(68). net_dbus is a
    # per-(endpoint, usage_date) cost_rollup, NOT a per-entity total: ss_bl_win's one billing row
    # lands on D(3) only, so the D(3) row's net_dbus is real (10.0) while the D(16)/D(68) rows --
    # real traffic, but no billing that day -- correctly read 0 via COALESCE(cr.net_dbus, 0), not
    # a repeat of the D(3) total.
    assert set(k[1] for k in r7 if k[0] == "ss_se_win") == {D3}
    assert r7[("ss_se_win", D3)]["total_requests"] == 11
    assert r7[("ss_se_win", D3)]["net_dbus"] == 10.0
    assert set(k[1] for k in r30 if k[0] == "ss_se_win") == {D3, D16}
    assert r30[("ss_se_win", D16)]["total_requests"] == 1
    assert r30[("ss_se_win", D16)]["net_dbus"] == 0.0
    assert set(k[1] for k in r90 if k[0] == "ss_se_win") == {D3, D16, D68}
    assert r90[("ss_se_win", D68)]["total_requests"] == 1

    # ss_se_warn_low: 5 requests at D(3), present at every window.
    for r in (r7, r30, r90):
        row = r[("ss_se_warn_low", D3)]
        assert row["total_requests"] == 5
        assert row["endpoint_id"] == "ss_ep_warn_low"
        assert row["net_dbus"] == 8.0

    # order_by (review round 2, item 4): usage_date DESC, total_requests DESC -- over the WHOLE
    # table (also carries tests/fixtures/ports.py's pt_ rows, unioned into the same source).
    keys = [(-r["usage_date"].toordinal(), -r["total_requests"])
            for r in dbutil.rows("compute_serving_endpoint_usage", 90)]
    assert keys == sorted(keys), keys


# -------------------------------------------------------------------------------------------
# serving_endpoint_traffic_by_endpoint -- pre-aggregated to endpoint_id grain; same "with/without
# traffic" and window-absence behaviour as compute_serving_endpoint_usage, at the endpoint (not
# entity+day) grain. Unaffected by the entities-CTE engine defect (explicit column list, no
# `se.*`) -- endpoint_id/endpoint_name/net_dbus are all asserted here.
# -------------------------------------------------------------------------------------------
def test_serving_endpoint_traffic_by_endpoint():
    def by_eid(w):
        return {r["endpoint_id"]: r for r in dbutil.rows("serving_endpoint_traffic_by_endpoint", w)
                if r["endpoint_id"] and r["endpoint_id"].startswith("ss_")}

    r7, r30, r90 = by_eid(7), by_eid(30), by_eid(90)

    for r in (r7, r30, r90):
        assert r["ss_ep_ok"]["request_count"] == 15
        assert r["ss_ep_ok"]["endpoint_name"] == "ss-ep-ok-endpoint"  # a resource name, never masked
        assert r["ss_ep_ok"]["net_dbus"] == 12.0
        assert abs(r["ss_ep_ok"]["est_usd_list"] - 12.0 * 0.10) < 1e-6
        assert r["ss_ep_ok"]["price_basis"] == "priced"
        assert r["ss_ep_warn_low"]["request_count"] == 5
        assert r["ss_ep_warn_low"]["net_dbus"] == 8.0
        assert abs(r["ss_ep_warn_low"]["est_usd_list"] - 8.0 * 0.10) < 1e-6

    # "without traffic": ss_ep_trackingoff wholly absent (0 endpoint_usage rows ever).
    for r in (r7, r30, r90):
        assert "ss_ep_trackingoff" not in r

    # ss_ep_idlewindow: absent at w=7/30, present with request_count=8 at w=90 only.
    assert "ss_ep_idlewindow" not in r7
    assert "ss_ep_idlewindow" not in r30
    assert r90["ss_ep_idlewindow"]["request_count"] == 8
    assert r90["ss_ep_idlewindow"]["net_dbus"] == 9.0

    # window-boundary: ss_ep_win's request_count differs precisely by window (11/12/13); net_dbus
    # is constant (billing always lands at D(3), inside every window).
    assert r7["ss_ep_win"]["request_count"] == 11
    assert r30["ss_ep_win"]["request_count"] == 12
    assert r90["ss_ep_win"]["request_count"] == 13
    assert r7["ss_ep_win"]["net_dbus"] == r30["ss_ep_win"]["net_dbus"] == r90["ss_ep_win"]["net_dbus"] == 10.0
    assert abs(r30["ss_ep_win"]["est_usd_list"] - 10.0 * 0.10) < 1e-6

    # order_by (review round 2, item 4): request_count DESC, endpoint_id -- over the WHOLE table
    # (also carries tests/fixtures/ports.py's pt_ rows, unioned into the same source).
    keys = [(-r["request_count"], r["endpoint_id"]) for r in dbutil.rows("serving_endpoint_traffic_by_endpoint", 90)]
    assert keys == sorted(keys), keys


# -------------------------------------------------------------------------------------------
# po_clustering_activity -- warn=50, crit=200 DBU; status: worst-first CRITICAL/WARN/OK/
# NOT_ASSESSED (no CASE in this id's own ORDER BY -- clustering_estimated_dbu DESC alone; DuckDB's
# NULLS-LAST default puts the NOT_ASSESSED (NULL DBU) row after OK -- see module docstring).
# -------------------------------------------------------------------------------------------
def test_po_clustering_activity():
    out = _po_rows("po_clustering_activity", 30)
    by_id = {r["table_id"]: r for r in out}

    assert by_id["ss_tid_clu_ok"]["status"] == "OK"
    assert by_id["ss_tid_clu_ok"]["clustering_estimated_dbu"] == 30.0
    assert by_id["ss_tid_clu_warn"]["status"] == "WARN"
    assert by_id["ss_tid_clu_warn"]["clustering_estimated_dbu"] == 80.0
    assert by_id["ss_tid_clu_crit"]["status"] == "CRITICAL"
    assert by_id["ss_tid_clu_crit"]["clustering_estimated_dbu"] == 250.0
    assert by_id["ss_tid_clu_na"]["status"] == "NOT_ASSESSED"
    assert by_id["ss_tid_clu_na"]["clustering_estimated_dbu"] is None

    # worst-first over the WHOLE list (review round 2, item 3 -- a filtered 3-element subsequence
    # proves nothing; `out` is already the whole findings.f_po_clustering_activity result, which
    # also carries this fixture's ss_tid_maint_* rows since po_maintenance_cost_by_table's own
    # test scenarios are written with operation_type='CLUSTERING' too -- verified 9-row sequence:
    # CRITICAL, WARN, WARN, OK, OK, OK, OK, NOT_ASSESSED, NOT_ASSESSED with DBU
    # 250,150,80,40,30,20,5,NULL,NULL). NOT_ASSESSED sorts LAST (after OK), per DuckDB's NULLS-LAST
    # default on this id's plain `ORDER BY clustering_estimated_dbu DESC` (no CASE status here at
    # all -- see module docstring), not the CRITICAL/WARN/NOT_ASSESSED/OK convention used
    # elsewhere in this codebase.
    _assert_worst_first(out, RANK_NA_LAST, "clustering_estimated_dbu")

    # window-boundary: 10 DBU at each of D(3)/D(16)/D(68) -> sums 10/20/30, OK throughout.
    for w, expect in [(7, 10.0), (30, 20.0), (90, 30.0)]:
        row = {r["table_id"]: r for r in _po_rows("po_clustering_activity", w)}["ss_tid_clu_win"]
        assert row["clustering_estimated_dbu"] == expect, (w, row["clustering_estimated_dbu"], expect)
        assert row["status"] == "OK"


# -------------------------------------------------------------------------------------------
# po_clustering_column_churn -- warn=2, crit=5 'true' events per identical old->new signature;
# has_column_selection_changed NULL -> NOT_ASSESSED (per this task's Inputs, verbatim).
# -------------------------------------------------------------------------------------------
def test_po_clustering_column_churn():
    out = _po_rows("po_clustering_column_churn", 30)
    by_id = {r["table_id"]: r for r in out}

    assert by_id["ss_tid_chu_ok"]["status"] == "OK"
    assert by_id["ss_tid_chu_ok"]["has_column_selection_changed"] == "false"
    assert by_id["ss_tid_chu_warn"]["status"] == "WARN"
    assert by_id["ss_tid_chu_warn"]["selection_event_count"] == 2
    assert by_id["ss_tid_chu_crit"]["status"] == "CRITICAL"
    assert by_id["ss_tid_chu_crit"]["selection_event_count"] == 5
    assert by_id["ss_tid_chu_na"]["status"] == "NOT_ASSESSED"
    assert by_id["ss_tid_chu_na"]["has_column_selection_changed"] is None

    # worst-first over the WHOLE list (review round 2, item 3): order_by is CASE status ...,
    # selection_event_count DESC. No other SEC uses AUTO_CLUSTERING_COLUMN_SELECTION, so `out`
    # holds exactly this id's own 5 scenario rows.
    _assert_worst_first(out, RANK_NA_LAST, "selection_event_count")

    # window-boundary: 1 identical-signature 'false' row at each of D(3)/D(16)/D(68) ->
    # selection_event_count 1/2/3, OK throughout ('false' never bands).
    for w, expect in [(7, 1), (30, 2), (90, 3)]:
        row = {r["table_id"]: r for r in _po_rows("po_clustering_column_churn", w)}["ss_tid_chu_win"]
        assert row["selection_event_count"] == expect, (w, row["selection_event_count"], expect)
        assert row["status"] == "OK"


# -------------------------------------------------------------------------------------------
# po_data_skipping_backfill -- warn=100GB, crit=500GB scanned with no gain (new_data_skipping_
# columns NULL/empty); a populated new_data_skipping_columns excludes the row from the band
# regardless of scan size.
# -------------------------------------------------------------------------------------------
def test_po_data_skipping_backfill():
    out = _po_rows("po_data_skipping_backfill", 30)
    by_id = {r["table_id"]: r for r in out}

    assert by_id["ss_tid_skip_ok"]["status"] == "OK"
    assert by_id["ss_tid_skip_ok"]["scanned_bytes"] == 10_000_000_000
    assert by_id["ss_tid_skip_warn"]["status"] == "WARN"
    assert by_id["ss_tid_skip_warn"]["scanned_bytes"] == 150_000_000_000
    assert by_id["ss_tid_skip_crit"]["status"] == "CRITICAL"
    assert by_id["ss_tid_skip_crit"]["scanned_bytes"] == 600_000_000_000
    # excluded from the band despite a CRITICAL-magnitude scan, because new_data_skipping_columns
    # is populated ('col_x', not NULL/empty).
    assert by_id["ss_tid_skip_gain"]["status"] == "OK"
    assert by_id["ss_tid_skip_gain"]["scanned_bytes"] == 600_000_000_000
    assert by_id["ss_tid_skip_gain"]["new_data_skipping_columns"] == "col_x"
    assert by_id["ss_tid_skip_na"]["status"] == "NOT_ASSESSED"
    assert by_id["ss_tid_skip_na"]["scanned_bytes"] is None

    # worst-first over the WHOLE list (review round 2, item 3): order_by is CASE status ...,
    # scanned_bytes DESC. No other SEC uses DATA_SKIPPING_COLUMN_SELECTION, so `out` holds exactly
    # this id's own 6 scenario rows.
    _assert_worst_first(out, RANK_NA_LAST, "scanned_bytes")

    # window-boundary: new_data_skipping_columns populated (always excluded -> OK), 10GB at each
    # of D(3)/D(16)/D(68) -> scanned_bytes sums 10e9/20e9/30e9.
    for w, expect in [(7, 10_000_000_000), (30, 20_000_000_000), (90, 30_000_000_000)]:
        row = {r["table_id"]: r for r in _po_rows("po_data_skipping_backfill", w)}["ss_tid_skip_win"]
        assert row["scanned_bytes"] == expect, (w, row["scanned_bytes"], expect)
        assert row["status"] == "OK"


# -------------------------------------------------------------------------------------------
# po_maintenance_cost_by_table -- NO operation_type filter (this fixture's other po_* scenario
# rows also land here, at THIS id's own warn=20/crit=100 thresholds -- assertions below are
# scoped to this id's own 6 dedicated table_ids, never to the whole ss_ set); FAILED status
# overrides to CRITICAL regardless of DBU.
# -------------------------------------------------------------------------------------------
def test_po_maintenance_cost_by_table():
    OWN_IDS = {
        "ss_tid_maint_ok", "ss_tid_maint_warn", "ss_tid_maint_critdbu",
        "ss_tid_maint_failed", "ss_tid_maint_na", "ss_tid_maint_win",
    }
    out = _po_rows("po_maintenance_cost_by_table", 30)
    by_id = {r["table_id"]: r for r in out if r["table_id"] in OWN_IDS}

    assert by_id["ss_tid_maint_ok"]["status"] == "OK"
    assert by_id["ss_tid_maint_ok"]["estimated_dbu"] == 5.0
    assert by_id["ss_tid_maint_warn"]["status"] == "WARN"
    assert by_id["ss_tid_maint_warn"]["estimated_dbu"] == 40.0
    assert by_id["ss_tid_maint_critdbu"]["status"] == "CRITICAL"
    assert by_id["ss_tid_maint_critdbu"]["estimated_dbu"] == 150.0
    # FAILED override: CRITICAL despite a low DBU spend (1.0, well under warn=20).
    assert by_id["ss_tid_maint_failed"]["status"] == "CRITICAL"
    assert by_id["ss_tid_maint_failed"]["estimated_dbu"] == 1.0
    assert by_id["ss_tid_maint_failed"]["operation_status"] == "FAILED: INTERNAL_ERROR"
    assert by_id["ss_tid_maint_na"]["status"] == "NOT_ASSESSED"
    assert by_id["ss_tid_maint_na"]["estimated_dbu"] is None

    # worst-first among this id's own 6 rows at w=30 (no ties in estimated_dbu at this window):
    # CRITICAL(critdbu,150) before CRITICAL(failed,1) [DBU DESC tiebreak within CRITICAL], then
    # WARN(warn,40), then OK(win,10), OK(ok,5), then NOT_ASSESSED(na) last. This filtered check is
    # the one defensible exception to review round 2 (item 3)'s "assert over the whole list" rule:
    # this id has NO operation_type filter, so its own 6 scenario rows are deliberately
    # interleaved with every other SEC's rows in the real output (see the whole-list check right
    # below), and filtering to OWN_IDS here is how this specific 6-row sub-sequence gets checked
    # at all, not a shortcut around checking the whole list.
    own_order = [r["table_id"] for r in out if r["table_id"] in OWN_IDS]
    assert own_order == [
        "ss_tid_maint_critdbu", "ss_tid_maint_failed", "ss_tid_maint_warn",
        "ss_tid_maint_win", "ss_tid_maint_ok", "ss_tid_maint_na",
    ], own_order

    # whole-list worst-first (review round 2, item 3): `out` (all 28 of this fixture's ss_ rows,
    # since this id has no operation_type filter) is worst-first as a whole, not just within
    # OWN_IDS -- verified: CRITICAL x3, WARN x6, OK x18 (with ties), NOT_ASSESSED x3 (order_by is
    # CASE status ..., estimated_dbu DESC).
    _assert_worst_first(out, RANK_NA_LAST, "estimated_dbu")

    # window-boundary: 5 DBU at each of D(3)/D(16)/D(68) -> sums 5/10/15, OK throughout.
    for w, expect in [(7, 5.0), (30, 10.0), (90, 15.0)]:
        row = {r["table_id"]: r for r in _po_rows("po_maintenance_cost_by_table", w)
               if r["table_id"] in OWN_IDS}["ss_tid_maint_win"]
        assert row["estimated_dbu"] == expect, (w, row["estimated_dbu"], expect)
        assert row["status"] == "OK"


# -------------------------------------------------------------------------------------------
# po_vacuum_reclaimed_bytes -- warn=5, crit=20 no-op DBU (amount_of_data_deleted_bytes = 0); a
# real reclaim excludes the row from the band regardless of DBU spent.
# -------------------------------------------------------------------------------------------
def test_po_vacuum_reclaimed_bytes():
    out = _po_rows("po_vacuum_reclaimed_bytes", 30)
    by_id = {r["table_id"]: r for r in out}

    assert by_id["ss_tid_vac_ok"]["status"] == "OK"
    assert by_id["ss_tid_vac_ok"]["vacuum_estimated_dbu"] == 2.0
    assert by_id["ss_tid_vac_warn"]["status"] == "WARN"
    assert by_id["ss_tid_vac_warn"]["vacuum_estimated_dbu"] == 8.0
    assert by_id["ss_tid_vac_crit"]["status"] == "CRITICAL"
    assert by_id["ss_tid_vac_crit"]["vacuum_estimated_dbu"] == 30.0
    # excluded from the band despite a CRITICAL-magnitude DBU spend (50.0), because this VACUUM
    # actually reclaimed bytes (5,000,000,000 > 0).
    assert by_id["ss_tid_vac_real"]["status"] == "OK"
    assert by_id["ss_tid_vac_real"]["vacuum_estimated_dbu"] == 50.0
    assert by_id["ss_tid_vac_real"]["total_deleted_bytes"] == 5_000_000_000
    assert by_id["ss_tid_vac_na"]["status"] == "NOT_ASSESSED"
    assert by_id["ss_tid_vac_na"]["vacuum_estimated_dbu"] is None

    # worst-first over the WHOLE list (review round 2, item 3): order_by is CASE status ...,
    # vacuum_estimated_dbu DESC. This id's own WHERE (operation_type='VACUUM' AND operation_
    # status='SUCCESSFUL') means `out` holds exactly this id's own 6 scenario rows (ss_tid_maint_
    # failed is VACUUM but FAILED, so it never qualifies here).
    _assert_worst_first(out, RANK_NA_LAST, "vacuum_estimated_dbu")

    # window-boundary: no-op VACUUM, 2 DBU at each of D(3)/D(16)/D(68) -> sums 2/4/6. A deliberate
    # secondary status flip: OK at w=7/30 (2, 4 < warn=5), WARN at w=90 (6 >= warn=5).
    for w, expect_dbu, expect_status in [(7, 2.0, "OK"), (30, 4.0, "OK"), (90, 6.0, "WARN")]:
        row = {r["table_id"]: r for r in _po_rows("po_vacuum_reclaimed_bytes", w)}["ss_tid_vac_win"]
        assert row["vacuum_estimated_dbu"] == expect_dbu, (w, row["vacuum_estimated_dbu"], expect_dbu)
        assert row["status"] == expect_status, (w, row["status"], expect_status)


def test_endpoint_usage_splits_a_days_bill_across_served_entities_by_requests():
    rows = [r for r in dbutil.rows("compute_serving_endpoint_usage", 30) if r["endpoint_id"] == wu.WU_EP_SPLIT]
    by_entity = {r["served_entity_id"]: r for r in rows}
    assert abs(by_entity[wu.WU_SE_C]["est_usd_list"] - 1.20) < 1e-6, by_entity
    assert abs(by_entity[wu.WU_SE_D]["est_usd_list"] - 0.40) < 1e-6, by_entity
    assert abs(sum(r["net_dbus"] for r in rows) - 8.0) < 1e-6, rows
