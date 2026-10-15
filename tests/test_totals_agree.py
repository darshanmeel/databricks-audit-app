"""tests/test_totals_agree.py -- K14 (review round). Totals must agree across screens: Overview,
Cost chargeback, Money and the period-over-period report all derive from the SAME priced billing
rows (system.billing.usage at the effective list price), so their totals must be the same number,
not four numbers that happen to be close. Filtered partitions plus the excluded-no-workspace slice
must add back up to the unfiltered total (C1), and a workspace with a partial window must show
fewer days than the window label promises (the zero-day case the UI fills in, never a silent 0).

Runs against the session fixture (tests/conftest.py) -- no new rows; every check here already has
real data in the fixture (NULL-workspace account-level spend included).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.app import app  # noqa: E402

client = TestClient(app)

BASE_QUERY_ID = "cost_dollarized_by_sku_day"
BASE_VALUE_COL = "net_list_cost"
WORKSPACE_ID = "1111"


def _agg_total(query_id: str, value_col: str, window: int, **params) -> float:
    body = client.get(
        f"/api/finding/{query_id}/aggregate",
        params={"window": window, "agg": "sum", "value": value_col, **params},
    ).json()
    assert body["outcome"] == "ok_rows", (query_id, body.get("outcome"))
    return body["total_value"] or 0.0


@pytest.mark.parametrize("window", [7, 30, 90])
def test_cost_totals_agree_across_screens(window):
    base_total = _agg_total(BASE_QUERY_ID, BASE_VALUE_COL, window)

    # Both reports round their own row to the cent before summing (cost_dollarized_by_sku_day
    # never rounds a row) -- a generous absolute tolerance absorbs that accumulated rounding, not
    # a real mismatch (which would be off by whole categories of spend, not fractions of a cent
    # per row).
    chargeback_total = _agg_total("cost_chargeback_by_workspace", "usd_list", window)
    assert chargeback_total == pytest.approx(base_total, rel=1e-4, abs=1.0)

    period_total = _agg_total("cost_period_over_period", "est_current_usd_list", window)
    assert period_total == pytest.approx(base_total, rel=1e-4, abs=1.0)

    rollup = client.get("/api/rollup", params={"area": "cost", "tag_key": "cost_center", "window": window}).json()
    if rollup["outcome"] == "ok_rows":
        assert rollup["total"]["usd"] == pytest.approx(base_total, rel=1e-4, abs=1.0)


@pytest.mark.parametrize("window", [7, 30, 90])
def test_env_partitions_plus_excluded_equal_the_unfiltered_total(window):
    unfiltered = _agg_total(BASE_QUERY_ID, BASE_VALUE_COL, window)

    workspaces = client.get("/api/workspaces").json()
    envs = sorted({w["env"] for w in workspaces if w.get("env")})
    if not envs:
        pytest.skip("no workspace carries an env in this fixture")

    partitioned = 0.0
    excluded_value = None
    for env in envs:
        body = client.get(
            f"/api/finding/{BASE_QUERY_ID}/aggregate",
            params={"window": window, "agg": "sum", "value": BASE_VALUE_COL, "env": env},
        ).json()
        if body["outcome"] != "ok_rows":
            continue
        partitioned += body["total_value"] or 0.0
        if excluded_value is None:
            assert body["excluded_no_workspace"] is not None
            excluded_value = body["excluded_no_workspace"]["value"] or 0.0

    assert excluded_value is not None
    assert partitioned + excluded_value == pytest.approx(unfiltered, rel=1e-6, abs=0.5)


def test_workspace_1111_has_fewer_than_thirty_days_of_rows_at_thirty_days():
    body = client.get(
        f"/api/finding/{BASE_QUERY_ID}/aggregate",
        params={
            "window": 30, "agg": "sum", "value": BASE_VALUE_COL,
            "workspace_ids": WORKSPACE_ID, "group": "usage_date",
        },
    ).json()
    assert body["outcome"] == "ok_rows"
    assert 0 < len(body["groups"]) < 30
