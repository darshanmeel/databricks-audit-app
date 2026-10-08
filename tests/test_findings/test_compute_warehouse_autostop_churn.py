"""tests/test_findings/test_compute_warehouse_autostop_churn.py

Proves, against tests/fixtures/compute_coverage.py's own `cc_wh_*` rows (workspace cc_ws1),
that findings.f_compute_warehouse_autostop_churn (grain [workspace_id, warehouse_id],
window_days = 30, header defaults: warn_long_autostop_minutes 10, warn_daily_autostops 5,
crit_daily_autostops 10, warn_daily_restarts 5) has the right grain and every status band:
CRITICAL (a high daily auto-stop count with a long auto-stop wait), WARN (a lower count, still a
long wait), OK despite a high count when the auto-stop is too short to matter, and the
cold_start_risk_days count firing independently of the status band. Every fixture scenario has
events on exactly one day (D(3)), so days_observed=1 and worst_day/worst_day_* equal the
window-total columns for every row here. Every expected number below is computed by hand from
that fixture module's own scenario table (also cross-checked directly against DuckDB in-session
before this file was written).
"""
from __future__ import annotations

import sys
from datetime import datetime
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
import compute_coverage as cc  # noqa: E402
from app.core.registry import by_id  # noqa: E402
from tools.generate_models import render_model  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "compute_warehouse_autostop_churn.yml"
QID = "compute_warehouse_autostop_churn"
CC_WS = "cc_ws1"


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _rows():
    return dbutil.rows(QID, 30, workspace_ids=[CC_WS])


def _by_wh(warehouse_id: str) -> dict:
    matches = [r for r in _rows() if r["warehouse_id"] == warehouse_id]
    assert len(matches) == 1, f"expected exactly one row for {warehouse_id}, got {len(matches)}"
    return matches[0]


def test_grain_uniqueness():
    grains = _grains()
    cols = grains[QID]
    out = dbutil.rows(QID, 30)
    assert out
    keys = [tuple(r[c] for c in cols) for r in out]
    assert len(keys) == len(set(keys)), "duplicate grain rows"


def test_status_enum():
    for r in _rows():
        assert r["status"] in {"OK", "WARN", "CRITICAL"}


def test_critical_high_count_long_autostop():
    r = _by_wh("cc_wh_crit")
    assert r["warehouse_kind"] == "classic"
    assert r["auto_stop_minutes"] == 10
    assert r["days_observed"] == 1
    assert r["days_flagged"] == 1
    assert r["total_autostop_count"] == 10
    assert r["worst_day_autostop_count"] == 10
    assert r["total_autostop_idle_minutes"] == 150.0
    assert r["cold_start_risk_days"] == 1
    assert r["status"] == "CRITICAL"


def test_warn_lower_count_long_autostop():
    r = _by_wh("cc_wh_warn")
    assert r["warehouse_kind"] == "pro"
    assert r["auto_stop_minutes"] == 15
    assert r["total_autostop_count"] == 6
    assert r["total_autostop_idle_minutes"] == 120.0
    assert r["cold_start_risk_days"] == 1
    assert r["status"] == "WARN"


def test_ok_short_autostop_despite_high_count():
    r = _by_wh("cc_wh_ok_short")
    assert r["auto_stop_minutes"] == 5
    assert r["total_autostop_count"] == 8  # would be WARN by count alone
    assert r["cold_start_risk_days"] == 1
    assert r["status"] == "OK"  # too short to flag


def test_cold_start_risk_without_qualifying_autostops():
    r = _by_wh("cc_wh_cold")
    assert r["total_autostop_count"] == 0
    assert r["cold_start_risk_days"] == 1
    assert r["status"] == "OK"


def test_stop_with_no_prior_query_excluded():
    r = _by_wh("cc_wh_noquery")
    assert r["total_autostop_count"] == 0
    assert r["cold_start_risk_days"] == 0
    assert r["status"] == "OK"


def test_present_in_every_standard_window():
    # D(3) is inside every one of the app's own windows (7/30/90), so the same CRITICAL row
    # shows up identically at each -- proving the query is windowed at all, not that D(3) falls
    # outside a shorter one.
    for window_days in (7, 30, 90):
        rows = [r for r in dbutil.rows(QID, window_days, workspace_ids=[CC_WS])
                if r["warehouse_id"] == "cc_wh_crit"]
        assert len(rows) == 1
        assert rows[0]["status"] == "CRITICAL"


# -------------------------------------------------------------------------------------------
# Synthetic, in-memory: idle-before-a-stop must never carry a running MAX(...) across a
# stop-then-restart boundary. A warehouse runs a 19-minute query on day 1, stops right after,
# restarts on day 2 and idles 10 minutes with no query at all before its next stop -- day 2 must
# read its own idle wait (10 minutes from its own STARTING event), never the huge gap back to
# day 1's query finish.
# -------------------------------------------------------------------------------------------
def test_idle_before_stop_does_not_carry_across_a_restart():
    con = duckdb.connect()
    try:
        for _name, create_sql in ddl.DDL.items():
            con.execute(create_sql)

        wh_id = "cc_wh_restart"
        cc.warehouse(con, wh_id, wtype="CLASSIC", auto_stop=10)
        # Day 1: start, one 19-minute query, stop right after it finishes -- idle wait 0.
        cc.we(con, wh_id, "STARTING", datetime(2026, 9, 18, 0, 0, 0), 0)
        cc.stmt(con, "cc_restart_q1", wh_id, datetime(2026, 9, 18, 0, 1, 0), datetime(2026, 9, 18, 0, 19, 0))
        cc.we(con, wh_id, "STOPPED", datetime(2026, 9, 18, 0, 19, 0), 0)
        # Day 2: restart, idle 10 minutes with no query at all, stop.
        cc.we(con, wh_id, "STARTING", datetime(2026, 9, 19, 0, 0, 0), 0)
        cc.we(con, wh_id, "STOPPED", datetime(2026, 9, 19, 0, 10, 0), 0)

        spec = by_id(QID)
        text = render_model(spec, target="duckdb")
        minutes_by_window = {}
        for window_days in (30, 90):
            sql = jinja_stub.render(text, windows=(window_days,), source=jinja_stub.memory_source)
            cur = con.execute(sql)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            r = [x for x in rows if x["warehouse_id"] == wh_id][0]
            # at most 10 idle minutes on day 2 (was ~1,401 before the fix: day 1's query finish
            # leaking forward across the day-2 restart)
            assert r["total_autostop_idle_minutes"] <= 10.0 + 1e-6, r["total_autostop_idle_minutes"]
            # no billing rows for this warehouse -> usd_per_minute is NULL -> waste stays NULL,
            # never a dollar figure bigger than its (zero, unpriced) bill
            assert r["total_est_wasted_usd_list"] is None, r["total_est_wasted_usd_list"]
            minutes_by_window[window_days] = r["total_autostop_idle_minutes"]
        assert minutes_by_window[30] == minutes_by_window[90]
    finally:
        con.close()
