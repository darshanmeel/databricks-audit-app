"""tests/test_findings/test_genie_usage.py.

Proves findings.f_genie_usage against tests/fixtures/genie.py's own rows (`gn_` ids, workspaces
gn_ws_prod / gn_ws_dev; see that module's docstring for the scenario map): the grain, the
surface / channel / agent / identity split, current vs previous period, corrections netted, the
free tier as a real $0, an unpriced SKU, a row with no genie struct, and the product, unit and
today filters. Expected numbers come from the fixture table at W=7 (current D(1)..D(7), previous
D(8)..D(14)) and from the raw parquet via dbutil.usage_sum.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

QID = "genie_usage"
W = 7
GN_WS = ["gn_ws_prod", "gn_ws_dev"]
SP = "aaaaaaaa-1111-2222-3333-444444444444"


def _rows(window_days: int = W) -> list[dict]:
    return dbutil.rows(QID, window_days, GN_WS)


def _one(rows, **match) -> dict:
    hits = [r for r in rows if all(r[k] == v for k, v in match.items())]
    assert len(hits) == 1, (match, hits)
    return hits[0]


def test_grain_uniqueness():
    cols = yaml.safe_load((ROOT / "config" / "grains" / "genie_usage.yml").read_text(encoding="ascii"))[QID]
    out = _rows()
    assert out
    keys = [tuple(r[c] for c in cols) for r in out]
    assert len(keys) == len(set(keys)), "duplicate grain rows"


def test_totals_match_raw_usage_for_both_periods():
    out = _rows()
    for period, days in (("current", (1, 7)), ("previous", (8, 14))):
        raw = dbutil.usage_sum(
            "billing_origin_product = 'GENIE' AND upper(usage_unit) = 'DBU' "
            f"AND workspace_id IN ('gn_ws_prod', 'gn_ws_dev') "
            f"AND usage_date BETWEEN DATE '{dbutil.TEST_TODAY}' - INTERVAL {days[1]} DAY "
            f"AND DATE '{dbutil.TEST_TODAY}' - INTERVAL {days[0]} DAY"
        )
        got = sum(r["dbus"] for r in out if r["period"] == period)
        assert abs(got - raw) < 1e-6, (period, got, raw)
    assert sum(r["dbus"] for r in out if r["period"] == "current") == 13 + 20 + 40 + 7 + 3 + 8 + 6
    assert sum(r["dbus"] for r in out if r["period"] == "previous") == 12


def test_surface_channel_and_identity():
    out = _rows()
    code = _one(out, period="current", surface="GENIE_CODE", run_as="alice@example.com", is_free=False)
    # 10 + 5 on one key, less a 2 DBU retraction: corrections are netted.
    assert code["channel"] == "UI" and code["identity_type"] == "user"
    assert code["dbus"] == 13 and code["usd_list"] == 1.3 and code["price_basis"] == "priced"
    agent = _one(out, surface="GENIE_AGENTS", run_as=SP)
    assert agent["channel"] == "API" and agent["agent_id"] == "gn_agent_sales"
    assert agent["identity_type"] == "service_principal" and agent["usd_list"] == 4.0
    bob = _one(out, surface="GENIE_AGENTS", run_as="bob@example.com")
    assert bob["workspace_id"] == "gn_ws_dev" and bob["channel"] == "UI"
    assert _one(out, surface="GENIE_ONE")["run_as"] == "carol@example.com"


def test_free_unpriced_and_no_struct_rows():
    out = _rows()
    free = _one(out, is_free=True)
    assert free["dbus"] == 20 and free["usd_list"] == 0 and free["price_basis"] == "free"
    unpriced = _one(out, run_as="dave@example.com")
    assert unpriced["usd_list"] is None and unpriced["unpriced_dbus"] == 7 and unpriced["price_basis"] == "unpriced"
    bare = _one(out, surface=None)
    assert bare["channel"] is None and bare["agent_id"] is None and bare["identity_type"] == "unknown"
    assert bare["dbus"] == 3


def test_filters_and_windows():
    out = _rows()
    # MODEL_SERVING, TOKEN-unit and today's rows never count; D(20) is outside both W=7 periods.
    assert all(r["dbus"] < 999 for r in out)
    assert sum(r["dbus"] for r in out) == 13 + 20 + 40 + 7 + 3 + 8 + 6 + 12
    at30 = dbutil.rows(QID, 30, GN_WS)
    assert sum(r["dbus"] for r in at30 if r["period"] == "current") == 13 + 20 + 40 + 7 + 3 + 8 + 6 + 12 + 4
    assert all(r["previous_period_covered"] for r in out)
