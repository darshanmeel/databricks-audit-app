"""tests/test_findings/test_cost_unnamed_workspaces.py

Proves, against tests/fixtures/billing.py's SEC N rows (uw_crit/uw_warn/uw_ok), that
findings.f_cost_unnamed_workspaces (grain [workspace_id], windowed on :period_days) finds exactly
the workspace ids billed in system.billing.usage with NO row in access.workspaces_latest, bands
them CRITICAL/WARN/OK correctly, and never shows a named, billed workspace (1111 acme-prod, billed
throughout this fixture's own D(1)/D(6)/D(7)/D(8) rows and named via workspace() in billing.py).

Every expected number below is computed by hand from billing.py's own SEC N usage rows and its
_PRICED_SKUS table (bl_ALL_PURPOSE_COMPUTE: expired rate 0.6*0.65=0.39 for usage priced before the
current price row starts at AS_OF-100 days; current rate 0.65 after that) -- see that module's own
SEC N comment for the day offsets and quantities this file's expectations are built from.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "cost_unnamed_workspaces.yml"
WINDOWS = (7, 30, 90)
QID = "cost_unnamed_workspaces"

# bl_ALL_PURPOSE_COMPUTE effective list rates (tests/fixtures/billing.py's own _PRICED_SKUS /
# priced_sku()): 0.65 current (price_start_time = AS_OF - 100 days, open-ended), 0.6 * 0.65 = 0.39
# expired (AS_OF - 400 days .. AS_OF - 200 days). billing.py's SEC N usage rows land at D(3)/D(1)
# (uw_crit) and D(80)/D(45) (uw_warn) -- all after AS_OF - 100 days, so all four price at the
# CURRENT 0.65 rate -- and D(250)/D(200) (uw_ok), both before AS_OF - 200 days, so both price at
# the EXPIRED 0.39 rate.
CURRENT_RATE = 0.65
EXPIRED_RATE = 0.39

_D0 = date(2026, 9, 21)  # tests/fixtures/base.py AS_OF.date()


def str_d(n: int) -> str:
    """The same D0 - n days billing.py's own D(n) computes, as an ISO date string -- this file
    never imports billing.py (per-builder isolation) but still checks first_used/last_used
    against the exact calendar date the fixture wrote."""
    return (_D0 - timedelta(days=n)).isoformat()


def _by_id(window_days: int) -> dict:
    return {r["workspace_id"]: r for r in dbutil.rows(QID, window_days)}


def test_grain_uniqueness():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        cols = yaml.safe_load(f)[QID]
    for w in WINDOWS:
        out = dbutil.rows(QID, w)
        assert out, w
        keys = [tuple(r[c] for c in cols) for r in out]
        assert len(keys) == len(set(keys)), f"w={w}: duplicate grain rows"


def test_window_days_zero_is_empty():
    assert dbutil.rows(QID, 0) == []


def test_status_enum():
    for r in dbutil.rows(QID, 30):
        assert r["status"] in {"OK", "WARN", "CRITICAL"}


def test_named_workspace_never_appears():
    # 1111/2222/3333 (acme-prod/dev/uat) are billed throughout this fixture and named via
    # workspace() in billing.py -- none may ever show up in an "unnamed workspace" finding.
    for w in WINDOWS:
        rows = _by_id(w)
        assert "1111" not in rows
        assert "2222" not in rows
        assert "3333" not in rows


# =====================================================================================================
# uw_crit: D(3)=10 DBU, D(1)=5 DBU, both at the current 0.65 rate, inside every 7/30/90-day window
# -> CRITICAL everywhere with the same net_list_cost_usd as the 365-day total.
# =====================================================================================================
def test_critical_band_recent_spend():
    dbus_365d = 15.0
    cost_365d = round(dbus_365d * CURRENT_RATE, 2)
    for w in WINDOWS:
        r = _by_id(w)["uw_crit"]
        assert r["first_used"].isoformat() == str_d(3)
        assert r["last_used"].isoformat() == str_d(1)
        assert r["active_days"] == 2
        assert r["dbus_365d"] == dbus_365d
        assert r["net_list_cost_usd_365d"] == cost_365d
        assert r["net_list_cost_usd"] == cost_365d, w
        assert r["lifetime_days"] == 3
        assert r["short_lived"] is True
        assert r["status"] == "CRITICAL", w


# =====================================================================================================
# uw_warn: D(80)=8 DBU, D(45)=4 DBU, both at the current 0.65 rate. Neither falls inside the 7- or
# 30-day window (no spend there -> WARN, since last_used=D(45) is within 90 days), but both fall
# inside the 90-day window (spend there -> CRITICAL) -- a real result, not a fixture artefact: a
# workspace whose last activity is 45 days ago genuinely does show spend once the window widens to
# 90 days.
# =====================================================================================================
def test_warn_band_then_critical_at_the_90_day_window():
    dbus_365d = 12.0
    cost_365d = round(dbus_365d * CURRENT_RATE, 2)
    for w in (7, 30):
        r = _by_id(w)["uw_warn"]
        assert r["net_list_cost_usd"] == 0.0, w
        assert r["status"] == "WARN", w
    r90 = _by_id(90)["uw_warn"]
    assert r90["net_list_cost_usd"] == cost_365d
    assert r90["status"] == "CRITICAL"
    # Columns that never depend on the window (365-day totals, lifetime) agree at every window.
    for w in WINDOWS:
        r = _by_id(w)["uw_warn"]
        assert r["first_used"].isoformat() == str_d(80)
        assert r["last_used"].isoformat() == str_d(45)
        assert r["active_days"] == 2
        assert r["dbus_365d"] == dbus_365d
        assert r["net_list_cost_usd_365d"] == cost_365d
        assert r["lifetime_days"] == 36
        assert r["short_lived"] is False


# =====================================================================================================
# uw_ok: D(250)=6 DBU, D(200)=3 DBU, both before the current price row starts (AS_OF - 100 days) and
# before the expired row's own end (AS_OF - 200 days at 12:00 -- both usage rows land at 01:00 on
# their own day) -> both price at the EXPIRED 0.39 rate. Outside every 7/30/90-day window, and
# last_used (D(200)) is more than 90 days ago -> OK everywhere, $0 in every window.
# =====================================================================================================
def test_ok_band_closed_down_workspace():
    dbus_365d = 9.0
    cost_365d = round(dbus_365d * EXPIRED_RATE, 2)
    for w in WINDOWS:
        r = _by_id(w)["uw_ok"]
        assert r["first_used"].isoformat() == str_d(250)
        assert r["last_used"].isoformat() == str_d(200)
        assert r["active_days"] == 2
        assert r["dbus_365d"] == dbus_365d
        assert r["net_list_cost_usd_365d"] == cost_365d
        assert r["net_list_cost_usd"] == 0.0, w
        assert r["lifetime_days"] == 51
        assert r["short_lived"] is False
        assert r["status"] == "OK", w
