"""tests/test_findings/test_compute_warehouse_idle_minutes.py -- P4-01-W1
(tasks/P4-WASTE-SPEC.md section 8), proves findings.f_compute_warehouse_idle_minutes against
tests/fixtures/idle_waste.py's own `iw_` rows (workspace `iw_ws8`, DEC-15: every scenario
assertion filters on the `iw_` ids, never on the workspace alone).

Per tests/test_findings/README.md and the spec's own checklist:
  - grain [window_days, workspace_id, warehouse_id] unique at 7 / 30 / 90, and nothing at 0;
  - the output columns in the contract order (section 2.6), the generator's own window_days first;
  - the full scenario table (section 6.2) at window 30, plus scenario G at window 7 (the carried-in
    state from before the window);
  - waste_reason exact text for every scenario;
  - every value of the status and not_assessed_reason enums appears among the iw_ rows;
  - invariants that hold on EVERY row the model returns, across every builder in the shared
    fixture (not just iw_ rows) -- not_assessed_reason NULL iff status != NOT_ASSESSED, the minute
    columns' ordering (counted <= idle <= running <= up), the four idle-kind minutes summing back
    to counted_idle_minutes, idle_cluster_minutes <= up_cluster_minutes, est_wasted_usd_list <=
    est_usd_list, and idle_gap_seconds resolving the way dbt/macros/param.sql does;
  - worst-first physical order, read as returned;
  - net_dbus cross-checked against an independent SUM over the raw billing.usage parquet;
  - the Settings override (idle_gap_seconds = 120) changes A and C, leaves B and E unchanged --
    proven by rendering the actual generated dbt model with tests/fixtures/jinja_stub.py and
    executing it against the fixture parquet, never a second hand-translated SQL string;
  - the dollar bands flag a low-share warehouse on its idle dollars, never an unpriced one, and
    the warn_waste_usd / crit_waste_usd overrides move them;
  - no query history in the window -> NOT_ASSESSED no_query_history (running_minutes still known),
    except a warehouse with no events at all, which stays no_warehouse_events (precedence).
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR / "fixtures"))
sys.path.insert(0, str(ROOT / "tools"))
import dbutil  # noqa: E402
import ddl  # noqa: E402
import header_schema  # noqa: E402
import idle_waste as iw  # noqa: E402
import jinja_stub  # noqa: E402

QUERY_ID = "compute_warehouse_idle_minutes"
SQL_PATH = ROOT / "app" / "queries" / "app" / "compute" / f"{QUERY_ID}.sql"
MODEL_PATH = ROOT / "dbt" / "models" / "findings" / "compute" / f"f_{QUERY_ID}.sql"
PARQUET_DIR = ROOT / "tests" / "fixtures" / "parquet"
WINDOWS = (7, 30, 90)
STATUS_RANK = {"CRITICAL": 0, "WARN": 1, "NOT_ASSESSED": 2, "OK": 3}

COLUMNS = [
    "window_days",
    "workspace_id", "warehouse_id", "warehouse_kind", "auto_stop_minutes", "start_state_known",
    "open_at_window_end", "running_periods", "up_minutes", "running_minutes", "busy_minutes",
    "idle_minutes", "counted_idle_minutes", "counted_idle_gaps", "longest_idle_gap_minutes",
    "start_gap_minutes", "between_queries_minutes", "stop_tail_minutes", "no_query_minutes",
    "idle_share_pct", "up_cluster_minutes", "idle_cluster_minutes", "max_clusters_seen",
    "net_dbus", "est_usd_list", "est_wasted_usd_list", "price_basis", "idle_gap_seconds",
    "autostop_1_idle_minutes", "autostop_1_cold_starts", "autostop_2_idle_minutes",
    "autostop_2_cold_starts", "autostop_5_idle_minutes", "autostop_5_cold_starts",
    "autostop_10_idle_minutes", "autostop_10_cold_starts", "autostop_30_idle_minutes",
    "autostop_30_cold_starts", "autostop_now_idle_minutes", "autostop_now_cold_starts",
    "usd_per_cluster_minute",
    "waste_reason", "not_assessed_reason", "status",
]

# Gaps between busy stretches: A has 30 s, 30 s, 2 min and 15 min, then a 40 min tail (auto-stop
# 40); C has 1.5 min and 9.5 min, then a 25 min tail (auto-stop 25). Each gap idles up to N
# minutes; a longer one adds a cold start, except the tail, which has no next query to wait.
WHATIF_30 = {
    "iw_wh_pro": {"autostop_1_idle_minutes": 4.0, "autostop_1_cold_starts": 2,
                  "autostop_2_idle_minutes": 7.0, "autostop_2_cold_starts": 1,
                  "autostop_5_idle_minutes": 13.0, "autostop_5_cold_starts": 1,
                  "autostop_10_idle_minutes": 23.0, "autostop_10_cold_starts": 1,
                  "autostop_30_idle_minutes": 48.0, "autostop_30_cold_starts": 0,
                  "autostop_now_idle_minutes": 58.0, "autostop_now_cold_starts": 0},
    "iw_wh_sls": {"autostop_1_idle_minutes": 3.0, "autostop_1_cold_starts": 2,
                  "autostop_2_idle_minutes": 5.5, "autostop_2_cold_starts": 1,
                  "autostop_5_idle_minutes": 11.5, "autostop_5_cold_starts": 1,
                  "autostop_10_idle_minutes": 21.0, "autostop_10_cold_starts": 0,
                  "autostop_30_idle_minutes": 36.0, "autostop_30_cold_starts": 0,
                  "autostop_now_idle_minutes": 36.0, "autostop_now_cold_starts": 0},
}

WASTE_REASON = {
    "iw_wh_pro": "idle 57 of 90 running min (63%) in 3 gaps longer than 60 s; longest 40 min; "
                 "40 min after the last query, waiting for auto-stop",
    "iw_wh_multi": "idle 40 of 80 running min (50%) in 1 gap longer than 60 s; longest 40 min; "
                   "40 min after the last query, waiting for auto-stop",
    "iw_wh_sls": "idle 36 of 50 running min (72%) in 3 gaps longer than 60 s; longest 25 min; "
                 "25 min after the last query, waiting for auto-stop",
    "iw_wh_noev": "billed in the window but no start or stop events were recorded for it, so its "
                  "running time cannot be measured; if auto-stop is off it may have run the whole "
                  "window",
    "iw_wh_quiet": "idle 60 of 60 running min (100%) in 1 gap longer than 60 s; longest 60 min; "
                   "60 min in 1 running period with no query at all",
    "iw_wh_open": "idle 20 of 30 running min (67%) in 1 gap longer than 60 s; longest 20 min; "
                  "20 min after the last query, waiting for auto-stop",
    "iw_wh_carry": "idle 180 of 180 running min (100%) in 1 gap longer than 60 s; longest 180 min; "
                   "180 min in 1 running period with no query at all",
    "iw_wh_nostart": "billed in the window, but its events show no running time that can be "
                     "measured (it was already running when its first recorded event in the "
                     "window arrived)",
    "iw_wh_busy": "idle 120 of 480 running min (25%) in 2 gaps longer than 60 s; longest 60 min; "
                  "60 min after the last query, waiting for auto-stop",
    "iw_wh_big": "idle 60 of 480 running min (13%) in 1 gap longer than 60 s; longest 60 min; "
                 "60 min after the last query, waiting for auto-stop",
    "iw_wh_unpriced": "idle 60 of 480 running min (13%) in 1 gap longer than 60 s; longest 60 min; "
                      "60 min after the last query, waiting for auto-stop; no dollar figure: its "
                      "usage has no list price",
}
WASTE_REASON_CARRY_AT_7 = ("idle 60 of 60 running min (100%) in 1 gap longer than 60 s; "
                           "longest 60 min; 60 min in 1 running period with no query at all")

# The section 6.2 table at window_days = 30 (A-H are also present, and identical, at 7 and 90,
# except G -- see EXPECTED_CARRY_AT_7 below).
EXPECTED_30 = {
    "iw_wh_pro": dict(
        warehouse_kind="pro", auto_stop_minutes=40, start_state_known=True,
        open_at_window_end=False, running_periods=1, up_minutes=91.0, running_minutes=90.0,
        busy_minutes=31.0, idle_minutes=59.0, counted_idle_minutes=57.0, counted_idle_gaps=3,
        longest_idle_gap_minutes=40.0, start_gap_minutes=0.0, between_queries_minutes=17.0,
        stop_tail_minutes=40.0, no_query_minutes=0.0, idle_share_pct=63.3,
        up_cluster_minutes=91.0, idle_cluster_minutes=57.0, max_clusters_seen=1, net_dbus=18.2,
        est_usd_list=9.10, est_wasted_usd_list=5.70, price_basis="priced", idle_gap_seconds=60,
        not_assessed_reason=None, status="CRITICAL",
    ),
    "iw_wh_multi": dict(
        warehouse_kind="pro", auto_stop_minutes=50, start_state_known=True,
        open_at_window_end=False, running_periods=1, up_minutes=81.0, running_minutes=80.0,
        busy_minutes=40.0, idle_minutes=40.0, counted_idle_minutes=40.0, counted_idle_gaps=1,
        longest_idle_gap_minutes=40.0, start_gap_minutes=0.0, between_queries_minutes=0.0,
        stop_tail_minutes=40.0, no_query_minutes=0.0, idle_share_pct=50.0,
        up_cluster_minutes=101.0, idle_cluster_minutes=40.0, max_clusters_seen=2, net_dbus=20.2,
        est_usd_list=10.10, est_wasted_usd_list=4.00, price_basis="priced", idle_gap_seconds=60,
        not_assessed_reason=None, status="WARN",
    ),
    "iw_wh_sls": dict(
        warehouse_kind="serverless", auto_stop_minutes=25, start_state_known=True,
        open_at_window_end=False, running_periods=1, up_minutes=50.0, running_minutes=50.0,
        busy_minutes=14.0, idle_minutes=36.0, counted_idle_minutes=36.0, counted_idle_gaps=3,
        longest_idle_gap_minutes=25.0, start_gap_minutes=0.0, between_queries_minutes=11.0,
        stop_tail_minutes=25.0, no_query_minutes=0.0, idle_share_pct=72.0,
        up_cluster_minutes=50.0, idle_cluster_minutes=36.0, max_clusters_seen=1, net_dbus=6.0,
        est_usd_list=4.20, est_wasted_usd_list=3.02, price_basis="priced", idle_gap_seconds=60,
        not_assessed_reason=None, status="CRITICAL",
    ),
    "iw_wh_noev": dict(
        warehouse_kind="pro", auto_stop_minutes=10, start_state_known=None,
        open_at_window_end=None, running_periods=0, up_minutes=None, running_minutes=None,
        busy_minutes=None, idle_minutes=None, counted_idle_minutes=None, counted_idle_gaps=None,
        longest_idle_gap_minutes=None, start_gap_minutes=None, between_queries_minutes=None,
        stop_tail_minutes=None, no_query_minutes=None, idle_share_pct=None,
        up_cluster_minutes=None, idle_cluster_minutes=None, max_clusters_seen=None, net_dbus=2.0,
        est_usd_list=1.00, est_wasted_usd_list=None, price_basis="priced", idle_gap_seconds=60,
        not_assessed_reason="no_warehouse_events", status="NOT_ASSESSED",
    ),
    "iw_wh_quiet": dict(
        warehouse_kind="pro", auto_stop_minutes=60, start_state_known=False,
        open_at_window_end=False, running_periods=1, up_minutes=60.0, running_minutes=60.0,
        busy_minutes=0.0, idle_minutes=60.0, counted_idle_minutes=60.0, counted_idle_gaps=1,
        longest_idle_gap_minutes=60.0, start_gap_minutes=0.0, between_queries_minutes=0.0,
        stop_tail_minutes=0.0, no_query_minutes=60.0, idle_share_pct=100.0,
        up_cluster_minutes=60.0, idle_cluster_minutes=60.0, max_clusters_seen=1, net_dbus=10.0,
        est_usd_list=5.00, est_wasted_usd_list=4.00, price_basis="priced", idle_gap_seconds=60,
        not_assessed_reason=None, status="CRITICAL",
    ),
    "iw_wh_open": dict(
        warehouse_kind="pro", auto_stop_minutes=30, start_state_known=True,
        open_at_window_end=True, running_periods=1, up_minutes=31.0, running_minutes=30.0,
        busy_minutes=9.0, idle_minutes=21.0, counted_idle_minutes=20.0, counted_idle_gaps=1,
        longest_idle_gap_minutes=20.0, start_gap_minutes=0.0, between_queries_minutes=0.0,
        stop_tail_minutes=20.0, no_query_minutes=0.0, idle_share_pct=66.7,
        up_cluster_minutes=31.0, idle_cluster_minutes=20.0, max_clusters_seen=1, net_dbus=1.0,
        est_usd_list=0.50, est_wasted_usd_list=0.32, price_basis="priced", idle_gap_seconds=60,
        not_assessed_reason=None, status="OK",
    ),
    "iw_wh_carry": dict(
        warehouse_kind="pro", auto_stop_minutes=180, start_state_known=True,
        open_at_window_end=False, running_periods=1, up_minutes=180.0, running_minutes=180.0,
        busy_minutes=0.0, idle_minutes=180.0, counted_idle_minutes=180.0, counted_idle_gaps=1,
        longest_idle_gap_minutes=180.0, start_gap_minutes=0.0, between_queries_minutes=0.0,
        stop_tail_minutes=0.0, no_query_minutes=180.0, idle_share_pct=100.0,
        up_cluster_minutes=180.0, idle_cluster_minutes=180.0, max_clusters_seen=1, net_dbus=3.0,
        est_usd_list=1.50, est_wasted_usd_list=1.50, price_basis="priced", idle_gap_seconds=60,
        not_assessed_reason=None, status="CRITICAL",
    ),
    "iw_wh_nostart": dict(
        warehouse_kind="pro", auto_stop_minutes=10, start_state_known=False,
        open_at_window_end=False, running_periods=0, up_minutes=0.0, running_minutes=0.0,
        busy_minutes=0.0, idle_minutes=0.0, counted_idle_minutes=0.0, counted_idle_gaps=0,
        longest_idle_gap_minutes=0.0, start_gap_minutes=0.0, between_queries_minutes=0.0,
        stop_tail_minutes=0.0, no_query_minutes=0.0, idle_share_pct=None,
        up_cluster_minutes=0.0, idle_cluster_minutes=None, max_clusters_seen=None, net_dbus=1.0,
        est_usd_list=0.50, est_wasted_usd_list=None, price_basis="priced", idle_gap_seconds=60,
        not_assessed_reason="no_running_time_measured", status="NOT_ASSESSED",
    ),
    # Dollar bands: $160 x 7200 / 28800 = $40 at 25% idle flags WARN on dollars alone.
    "iw_wh_busy": dict(
        warehouse_kind="pro", auto_stop_minutes=60, start_state_known=True,
        open_at_window_end=False, running_periods=1, up_minutes=480.0, running_minutes=480.0,
        busy_minutes=360.0, idle_minutes=120.0, counted_idle_minutes=120.0, counted_idle_gaps=2,
        longest_idle_gap_minutes=60.0, start_gap_minutes=0.0, between_queries_minutes=60.0,
        stop_tail_minutes=60.0, no_query_minutes=0.0, idle_share_pct=25.0,
        up_cluster_minutes=480.0, idle_cluster_minutes=120.0, max_clusters_seen=1, net_dbus=320.0,
        est_usd_list=160.00, est_wasted_usd_list=40.00, price_basis="priced", idle_gap_seconds=60,
        not_assessed_reason=None, status="WARN",
    ),
    # $2112 x 3600 / 28800 = $264 at 12.5% idle flags CRITICAL on dollars alone.
    "iw_wh_big": dict(
        warehouse_kind="pro", auto_stop_minutes=60, start_state_known=True,
        open_at_window_end=False, running_periods=1, up_minutes=480.0, running_minutes=480.0,
        busy_minutes=420.0, idle_minutes=60.0, counted_idle_minutes=60.0, counted_idle_gaps=1,
        longest_idle_gap_minutes=60.0, start_gap_minutes=0.0, between_queries_minutes=0.0,
        stop_tail_minutes=60.0, no_query_minutes=0.0, idle_share_pct=12.5,
        up_cluster_minutes=480.0, idle_cluster_minutes=60.0, max_clusters_seen=1, net_dbus=4224.0,
        est_usd_list=2112.00, est_wasted_usd_list=264.00, price_basis="priced", idle_gap_seconds=60,
        not_assessed_reason=None, status="CRITICAL",
    ),
    # iw_wh_big's shape unpriced: no dollar figure, so the 12.5% share alone reads OK.
    "iw_wh_unpriced": dict(
        warehouse_kind="pro", auto_stop_minutes=60, start_state_known=True,
        open_at_window_end=False, running_periods=1, up_minutes=480.0, running_minutes=480.0,
        busy_minutes=420.0, idle_minutes=60.0, counted_idle_minutes=60.0, counted_idle_gaps=1,
        longest_idle_gap_minutes=60.0, start_gap_minutes=0.0, between_queries_minutes=0.0,
        stop_tail_minutes=60.0, no_query_minutes=0.0, idle_share_pct=12.5,
        up_cluster_minutes=480.0, idle_cluster_minutes=60.0, max_clusters_seen=1, net_dbus=4224.0,
        est_usd_list=None, est_wasted_usd_list=None, price_basis="unpriced", idle_gap_seconds=60,
        not_assessed_reason=None, status="OK",
    ),
}

# G ("iw_wh_carry") at window_days = 7: the state carried in from the last event before the window
# (RUNNING at D(8) 22:00, chosen over the same-instant STARTING by state_rank) keeps it running
# from the window's own opening, D(7) 00:00, for the whole 60-minute window.
EXPECTED_CARRY_AT_7 = dict(
    warehouse_kind="pro", auto_stop_minutes=180, start_state_known=True,
    open_at_window_end=False, running_periods=1, up_minutes=60.0, running_minutes=60.0,
    busy_minutes=0.0, idle_minutes=60.0, counted_idle_minutes=60.0, counted_idle_gaps=1,
    longest_idle_gap_minutes=60.0, start_gap_minutes=0.0, between_queries_minutes=0.0,
    stop_tail_minutes=0.0, no_query_minutes=60.0, idle_share_pct=100.0,
    up_cluster_minutes=60.0, idle_cluster_minutes=60.0, max_clusters_seen=1, net_dbus=1.0,
    est_usd_list=0.50, est_wasted_usd_list=0.50, price_basis="priced", idle_gap_seconds=60,
    not_assessed_reason=None, status="CRITICAL",
)


def _rows(window_days: int) -> list[dict]:
    """Every row of the finding, in the model's own order (no workspace filter: the invariant
    and physical-order tests below deliberately hold across every builder in the shared fixture,
    per the spec's own item 6)."""
    return dbutil.rows(QUERY_ID, window_days)


def _iw(rows: list[dict]) -> list[dict]:
    return [r for r in rows if (r["warehouse_id"] or "").startswith("iw_wh_")]


def _param(name: str):
    """dbt/macros/param.sql's lookup order: thresholds.yml [qid][name] -> ['_all'][name] -> the
    header default."""
    doc = yaml.safe_load((ROOT / "config" / "thresholds.yml").read_text(encoding="utf-8")) or {}
    for scope in (doc.get(QUERY_ID) or {}, doc.get("_all") or {}):
        if isinstance(scope, dict) and name in scope:
            return scope[name]
    hdr = header_schema.parse_header(SQL_PATH.read_text(encoding="utf-8"))
    return {p["name"]: p["default"] for p in hdr["params"]}[name]


def _close(a, b, tol: float = 1e-6) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) < tol


def _assert_row(actual: dict, expected: dict) -> None:
    for key, want in expected.items():
        got = actual[key]
        if isinstance(want, float):
            assert _close(got, want), f"{key}: got {got!r}, want {want!r}"
        else:
            assert got == want, f"{key}: got {got!r}, want {want!r}"


# -------------------------------------------------------------------------------------------
# grain, columns, window 0
# -------------------------------------------------------------------------------------------
def test_grain_columns_and_window_zero():
    for w in WINDOWS:
        out = _rows(w)
        assert out, f"no rows at window {w}"
        assert all(r["window_days"] == w for r in out)
        keys = [(r["window_days"], r["workspace_id"], r["warehouse_id"]) for r in out]
        assert len(keys) == len(set(keys)), f"duplicate grain at window {w}"
        assert list(out[0].keys()) == COLUMNS, list(out[0].keys())
    assert _rows(0) == []


# -------------------------------------------------------------------------------------------
# the full scenario table at 30, and G at 7
# -------------------------------------------------------------------------------------------
def test_scenarios_at_30():
    by_wh = {r["warehouse_id"]: r for r in _iw(_rows(30))}
    assert set(by_wh) == set(EXPECTED_30), sorted(by_wh)
    for wh, expected in EXPECTED_30.items():
        _assert_row(by_wh[wh], expected)
        assert by_wh[wh]["waste_reason"] == WASTE_REASON[wh], wh


def test_scenario_g_carried_state_at_7():
    by_wh = {r["warehouse_id"]: r for r in _iw(_rows(7))}
    assert "iw_wh_carry" in by_wh
    _assert_row(by_wh["iw_wh_carry"], EXPECTED_CARRY_AT_7)
    assert by_wh["iw_wh_carry"]["waste_reason"] == WASTE_REASON_CARRY_AT_7


def test_scenarios_present_at_90_match_30():
    """A-H (D(1)-D(3), D(7)/D(8)) are all inside 90 days too, and unchanged from the 30-day row
    (no scenario here sits on the 30-vs-90 boundary)."""
    by30 = {r["warehouse_id"]: r for r in _iw(_rows(30))}
    by90 = {r["warehouse_id"]: r for r in _iw(_rows(90))}
    for wh in EXPECTED_30:
        a = {k: v for k, v in by30[wh].items() if k != "window_days"}
        b = {k: v for k, v in by90[wh].items() if k != "window_days"}
        assert b == a, wh


def test_autostop_whatif_replays_query_gaps():
    by_wh = {r["warehouse_id"]: r for r in _iw(_rows(30))}
    for wh, expected in WHATIF_30.items():
        _assert_row(by_wh[wh], expected)


# -------------------------------------------------------------------------------------------
# enums
# -------------------------------------------------------------------------------------------
def test_enums_appear():
    iw_rows_30 = _iw(_rows(30))
    assert {r["status"] for r in iw_rows_30} >= {"CRITICAL", "WARN", "OK", "NOT_ASSESSED"}
    assert {r["not_assessed_reason"] for r in iw_rows_30 if r["not_assessed_reason"]} == {
        "no_warehouse_events", "no_running_time_measured"}


# -------------------------------------------------------------------------------------------
# invariants on every row, every builder
# -------------------------------------------------------------------------------------------
def test_invariants_every_row():
    idle_gap_seconds = _param("idle_gap_seconds")
    for w in WINDOWS:
        for r in _rows(w):
            assert (r["not_assessed_reason"] is None) == (r["status"] != "NOT_ASSESSED"), r
            assert r["idle_gap_seconds"] == idle_gap_seconds, r
            if r["est_wasted_usd_list"] is not None and r["est_usd_list"] is not None:
                assert r["est_wasted_usd_list"] <= r["est_usd_list"] + 0.01, r
            if r["idle_cluster_minutes"] is not None and r["up_cluster_minutes"] is not None:
                assert r["idle_cluster_minutes"] <= r["up_cluster_minutes"] + 0.05, r
            if r["not_assessed_reason"] is None:
                assert r["counted_idle_minutes"] <= r["idle_minutes"] + 0.05, r
                assert r["idle_minutes"] <= r["running_minutes"] + 0.05, r
                assert r["running_minutes"] <= r["up_minutes"] + 0.05, r
                parts = (r["start_gap_minutes"] + r["between_queries_minutes"]
                         + r["stop_tail_minutes"] + r["no_query_minutes"])
                assert abs(parts - r["counted_idle_minutes"]) <= 0.25, r
            if r["autostop_1_idle_minutes"] is not None:
                idle = [r[f"autostop_{n}_idle_minutes"] for n in (1, 5, 10, 30)]
                starts = [r[f"autostop_{n}_cold_starts"] for n in (1, 5, 10, 30)]
                assert idle == sorted(idle) and starts == sorted(starts, reverse=True), r
            rate, idle_cm = r["usd_per_cluster_minute"], r["idle_cluster_minutes"]
            if rate is not None and idle_cm and r["est_wasted_usd_list"] is not None:
                assert abs(rate * idle_cm - r["est_wasted_usd_list"]) <= 0.01 + 0.0001 * idle_cm, r


# -------------------------------------------------------------------------------------------
# worst-first physical order
# -------------------------------------------------------------------------------------------
def test_worst_first_order():
    for w in WINDOWS:
        out = _rows(w)
        ranks = [STATUS_RANK[r["status"]] for r in out]
        assert ranks == sorted(ranks), (w, [(r["warehouse_id"], r["status"]) for r in out])


# -------------------------------------------------------------------------------------------
# independent re-derivation of net_dbus from the raw billing.usage parquet
# -------------------------------------------------------------------------------------------
def test_net_dbus_matches_raw_parquet():
    by_wh = {r["warehouse_id"]: r for r in _iw(_rows(30))}
    for wh in ("iw_wh_pro", "iw_wh_multi", "iw_wh_sls", "iw_wh_quiet", "iw_wh_open"):
        want = dbutil.usage_sum(
            f"usage_metadata.warehouse_id = '{wh}' AND usage_date >= DATE '2026-08-22' "
            "AND usage_date < DATE '2026-09-21'"
        )
        assert _close(by_wh[wh]["net_dbus"], want, tol=1e-6), (wh, by_wh[wh]["net_dbus"], want)
    # F: only its D(1) bucket is inside the window; its D(0) (today) bucket is excluded.
    open_want = dbutil.usage_sum(
        "usage_metadata.warehouse_id = 'iw_wh_open' AND usage_date >= DATE '2026-08-22' "
        "AND usage_date < DATE '2026-09-21'"
    )
    assert _close(by_wh["iw_wh_open"]["net_dbus"], open_want)
    assert open_want == 1.0  # the D(0) row (1.0 DBU) is excluded; only D(1)'s 1.0 counts


# -------------------------------------------------------------------------------------------
# the Settings override (idle_gap_seconds) -- rendered from the real generated dbt model
# -------------------------------------------------------------------------------------------
def test_idle_gap_seconds_header_default_is_60():
    hdr = header_schema.parse_header(SQL_PATH.read_text(encoding="utf-8"))
    defaults = {p["name"]: p["default"] for p in hdr["params"]}
    assert defaults["idle_gap_seconds"] == 60
    assert defaults["warn_waste_usd"] == 20 and defaults["crit_waste_usd"] == 200


def test_waste_usd_override_moves_the_dollar_bands():
    text = MODEL_PATH.read_text(encoding="utf-8")
    sql = jinja_stub.render(
        text, windows=(30,),
        thresholds={"compute_warehouse_idle_minutes": {"warn_waste_usd": 50, "crit_waste_usd": 300}},
        source=jinja_stub.parquet_source(PARQUET_DIR.as_posix()),
    )
    con = duckdb.connect()
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        status = {row["warehouse_id"]: row["status"] for row in
                  (dict(zip(cols, r)) for r in cur.fetchall()) if row["workspace_id"] == iw.IW_WS}
    finally:
        con.close()
    assert status["iw_wh_busy"] == "OK"        # $40 < 50, 25% < 30
    assert status["iw_wh_big"] == "WARN"       # 50 <= $264 < 300
    assert status["iw_wh_pro"] == "CRITICAL"   # share bands unchanged


def test_idle_gap_seconds_override_changes_a_and_c_not_b_or_e():
    text = MODEL_PATH.read_text(encoding="utf-8")
    sql = jinja_stub.render(
        text, windows=(30,),
        thresholds={"compute_warehouse_idle_minutes": {"idle_gap_seconds": 120}},
        source=jinja_stub.parquet_source(PARQUET_DIR.as_posix()),
    )
    con = duckdb.connect()
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        by_wh = {dict(zip(cols, row))["warehouse_id"]: dict(zip(cols, row)) for row in cur.fetchall()
                 if dict(zip(cols, row))["workspace_id"] == iw.IW_WS}
    finally:
        con.close()

    a = by_wh["iw_wh_pro"]
    assert a["counted_idle_minutes"] == 55.0
    assert a["counted_idle_gaps"] == 2
    assert a["between_queries_minutes"] == 15.0
    assert _close(a["est_wasted_usd_list"], 5.50)
    assert a["idle_gap_seconds"] == 120
    assert a["waste_reason"] == (
        "idle 55 of 90 running min (61%) in 2 gaps longer than 120 s; longest 40 min; "
        "40 min after the last query, waiting for auto-stop")

    c = by_wh["iw_wh_sls"]
    assert c["counted_idle_minutes"] == 34.5
    assert c["counted_idle_gaps"] == 2
    assert _close(c["est_wasted_usd_list"], 2.90)

    # B and E: unchanged (their only gaps are already well above 120 s).
    b = by_wh["iw_wh_multi"]
    e = by_wh["iw_wh_quiet"]
    assert b["counted_idle_minutes"] == 40.0 and _close(b["est_wasted_usd_list"], 4.00)
    assert e["counted_idle_minutes"] == 60.0 and _close(e["est_wasted_usd_list"], 4.00)


# -------------------------------------------------------------------------------------------
# no query history at all in the window -> NOT_ASSESSED no_query_history (precedence over it:
# a warehouse with no events at all stays no_warehouse_events)
# -------------------------------------------------------------------------------------------
def test_no_query_history_in_window():
    con = duckdb.connect()
    try:
        for _name, create_sql in ddl.DDL.items():
            con.execute(create_sql)
        iw.build(con)
        con.execute("DELETE FROM query__history")

        text = MODEL_PATH.read_text(encoding="utf-8")
        sql = jinja_stub.render(text, windows=(30,), source=jinja_stub.memory_source)
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        by_wh = {row["warehouse_id"]: row for row in
                 (dict(zip(cols, r)) for r in cur.fetchall()) if row["workspace_id"] == iw.IW_WS}
    finally:
        con.close()

    for wh in ("iw_wh_pro", "iw_wh_multi", "iw_wh_sls", "iw_wh_quiet", "iw_wh_open",
               "iw_wh_nostart"):
        r = by_wh[wh]
        assert r["not_assessed_reason"] == "no_query_history", (wh, r["not_assessed_reason"])
        assert r["busy_minutes"] is None and r["idle_minutes"] is None
        assert r["counted_idle_minutes"] is None
    assert by_wh["iw_wh_pro"]["running_minutes"] == 90.0
    # No events at all: no_warehouse_events still takes precedence.
    assert by_wh["iw_wh_noev"]["not_assessed_reason"] == "no_warehouse_events"
