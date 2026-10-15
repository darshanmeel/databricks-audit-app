"""tests/test_api_rollup.py -- P4-T-ROLL (tasks/P4-T-SPEC.md section 6.7 test 7).

GET /api/rollup and GET /api/rollup/keys, against the same tests/db_audit_test.duckdb every other
API test reads (tests/conftest.py's session fixture).
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.app import app  # noqa: E402

client = TestClient(app)

TG_WORKSPACES = ["tg_ws_a", "tg_ws_m", "tg_ws_s"]


def test_bad_area_is_422():
    r = client.get("/api/rollup", params={"area": "x", "tag_key": "tg_cc"})
    assert r.status_code == 422


def test_missing_tag_key_is_422():
    r = client.get("/api/rollup", params={"area": "cost"})
    assert r.status_code == 422


def test_empty_after_normalising_tag_key_is_422():
    r = client.get("/api/rollup", params={"tag_key": "___"})
    assert r.status_code == 422


def test_bad_window_is_422():
    r = client.get("/api/rollup", params={"tag_key": "tg_cc", "window": 14})
    assert r.status_code == 422


def test_selected_value_matches_pinned_number():
    r = client.get("/api/rollup", params={
        "tag_key": "tg_cc", "tag_value": "alpha", "window": 30, "workspace_ids": TG_WORKSPACES,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["selected"] is not None
    assert abs(body["selected"]["metrics"]["usd"] - 206.0) < 0.005


def test_selected_is_null_with_no_tag_value():
    r = client.get("/api/rollup", params={"tag_key": "tg_cc", "window": 30, "workspace_ids": TG_WORKSPACES})
    body = r.json()
    assert body["selected"] is None


def test_untagged_tag_value_selects_the_untagged_entry():
    r = client.get("/api/rollup", params={
        "tag_key": "tg_cc", "tag_value": "__untagged__", "window": 30, "workspace_ids": TG_WORKSPACES,
    })
    body = r.json()
    assert body["selected"] is not None
    assert body["selected"]["value"] == "__untagged__"
    assert abs(body["selected"]["metrics"]["usd"] - 50.0) < 0.005


def test_rollup_keys_lists_tgcc_first_among_tg_keys():
    r = client.get("/api/rollup/keys", params={"area": "cost", "window": 30})
    assert r.status_code == 200
    keys = r.json()["keys"]
    tg_keys = [k for k in keys if k["tag_key"].startswith("tg")]
    assert tg_keys, "expected at least one tg* key"
    assert tg_keys[0]["tag_key"] == "tgcc"


def test_rollup_keys_tgcc_not_fanned_out_by_workspace_count():
    # tags.tag_workspace has one row per (workspace, key); tgcc is carried by 3 tg workspaces.
    # rollup_keys must not multiply each cost_unit_tag row's dollars by that count (section 7.2's
    # unscoped tgcc figures, since tgcc exists only on tg workspaces): compute 530, work 136 --
    # not 1590 / 408 (x3).
    r = client.get("/api/rollup/keys", params={"area": "cost", "window": 30})
    assert r.status_code == 200
    keys = {k["tag_key"]: k for k in r.json()["keys"]}
    assert abs(keys["tgcc"]["compute"]["usd"] - 530.0) < 0.005
    assert abs(keys["tgcc"]["work"]["usd"] - 136.0) < 0.005


def test_rollup_keys_bad_area_is_422():
    r = client.get("/api/rollup/keys", params={"area": "nope"})
    assert r.status_code == 422


def test_performance_area_has_no_dollars():
    r = client.get("/api/rollup", params={"tag_key": "tg_cc", "area": "performance", "window": 30, "workspace_ids": TG_WORKSPACES})
    body = r.json()
    assert body["primary_metric"] == "statements"
    assert "usd" not in body["total"]
    assert body["total"]["statements"] == 9


def test_ok_empty_window_for_a_key_seen_only_outside_this_scope():
    # tg keys exist only on tg_* workspaces -- scoping to a non-tg workspace with real spend must
    # read ok_rows (an all-untagged total) or an empty outcome, never crash. A workspace filter is
    # active here, so an empty read is ok_empty_filters (C26), not ok_empty_window.
    r = client.get("/api/rollup", params={"tag_key": "tg_cc", "window": 30, "workspace_ids": ["1111"]})
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] in ("ok_rows", "ok_empty_filters")
    if body["outcome"] == "ok_rows":
        untagged = next((e for e in body["by_value"] if e["value"] == "__untagged__"), None)
        assert untagged is not None
        assert abs(untagged["metrics"]["usd"] - body["total"]["usd"]) < 0.005


# -------------------------------------------------------------------------------------------
# tag (repeatable, C26) -- the SAME active tag filter GET /api/finding's aggregate applies,
# distinct from tag_value above (which marks a `selected` entry in THIS key's own tree).
# -------------------------------------------------------------------------------------------


def test_tag_filter_narrows_the_total():
    unfiltered = client.get(
        "/api/rollup", params={"tag_key": "tg_cc", "window": 30, "workspace_ids": TG_WORKSPACES}
    ).json()
    filtered = client.get("/api/rollup", params={
        "tag_key": "tg_cc", "window": 30, "workspace_ids": TG_WORKSPACES, "tag": "tg_cc:alpha",
    }).json()
    assert filtered["outcome"] == "ok_rows"
    assert abs(filtered["total"]["usd"] - 206.0) < 0.005  # test_selected_value_matches_pinned_number's own figure
    assert filtered["total"]["usd"] < unfiltered["total"]["usd"]


def test_tag_filter_matching_nothing_is_ok_empty_filters():
    r = client.get("/api/rollup", params={
        "tag_key": "tg_cc", "window": 30, "workspace_ids": TG_WORKSPACES,
        "tag": "tg_cc:no_such_value_at_all",
    })
    assert r.status_code == 200
    assert r.json()["outcome"] == "ok_empty_filters"


# -------------------------------------------------------------------------------------------
# tag_value (repeatable): a single value keeps marking `selected` exactly as before; several
# additionally OR-mark every matching entry into `selected_values`.
# -------------------------------------------------------------------------------------------


def test_rollup_single_tag_value_selected_is_unchanged():
    r = client.get("/api/rollup", params={
        "tag_key": "tg_cc", "tag_value": "alpha", "window": 30, "workspace_ids": TG_WORKSPACES,
    })
    body = r.json()
    assert body["selected"] is not None
    assert abs(body["selected"]["metrics"]["usd"] - 206.0) < 0.005
    # One tag_value given -- selected_values is just [selected], the same entry named twice.
    assert [e["value"] for e in body["selected_values"]] == ["alpha"]


def test_rollup_several_tag_values_or_selects_every_match():
    r = client.get("/api/rollup", params={
        "tag_key": "tg_cc", "tag_value": ["alpha", "__untagged__"], "window": 30,
        "workspace_ids": TG_WORKSPACES,
    })
    body = r.json()
    assert body["selected"]["value"] == "alpha"  # the first repeated value, same as before
    values = {e["value"] for e in body["selected_values"]}
    assert values == {"alpha", "__untagged__"}


# -------------------------------------------------------------------------------------------
# GET /api/rollup/top -- the by-tag view: top values by $ (cost) or query time (performance),
# levels kept separate, one untagged row, more_count past `top`.
# -------------------------------------------------------------------------------------------


def test_top_bad_area_is_422():
    r = client.get("/api/rollup/top", params={"area": "x", "tag_key": "tg_cc"})
    assert r.status_code == 422


def test_top_missing_tag_key_is_422():
    r = client.get("/api/rollup/top", params={"area": "cost"})
    assert r.status_code == 422


def test_top_cost_levels_never_merged_for_the_same_value():
    # section 7.5's own etl scenario: tg_cl_a2 (a cluster, billed with tg_cc=etl) and tg_job_a1
    # (the job whose own tags carry tg_cc=etl) must be TWO rows, not one blended "etl" row.
    r = client.get("/api/rollup/top", params={
        "tag_key": "tg_cc", "window": 30, "workspace_ids": TG_WORKSPACES, "top": 100,
    })
    assert r.status_code == 200
    body = r.json()
    by_level_value = {(row["level"], row["value"]): row["metrics"]["usd"] for row in body["rows"]}
    assert abs(by_level_value[("cluster", "etl")] - 20.0) < 0.005
    assert abs(by_level_value[("job", "etl")] - 20.0) < 0.005
    # "alpha" sits on three different levels at once -- workspace (inferred), warehouse (tg_wh_a1)
    # and cluster (tg_cl_a1) -- each its own row, none of them the sum of the other two.
    assert abs(by_level_value[("workspace", "alpha")] - 362.0) < 0.005
    assert abs(by_level_value[("warehouse", "alpha")] - 150.0) < 0.005
    assert abs(by_level_value[("cluster", "alpha")] - 80.0) < 0.005
    assert set(row["level"] for row in body["rows"]) <= set(body["levels"])


def test_top_cost_includes_a_billing_line_row():
    # tg_job_a2 (serverless, budget policy tg_bp_1) carries tg_cc=shared straight off the bill --
    # not a named warehouse or cluster, so it reads "billing line".
    r = client.get("/api/rollup/top", params={
        "tag_key": "tg_cc", "window": 30, "workspace_ids": TG_WORKSPACES, "top": 100,
    })
    body = r.json()
    row = next(r for r in body["rows"] if r["level"] == "billing line" and r["value"] == "shared")
    assert abs(row["metrics"]["usd"] - 15.0) < 0.005


def test_top_untagged_row_and_more_count():
    r = client.get("/api/rollup/top", params={
        "tag_key": "tg_cc", "window": 30, "workspace_ids": TG_WORKSPACES, "top": 5,
    })
    body = r.json()
    assert len(body["rows"]) == 5
    assert body["untagged"]["value"] == "__untagged__"
    assert abs(body["untagged"]["metrics"]["usd"] - 50.0) < 0.005
    assert body["group_count"] == 15
    assert body["more_count"] == 10


def test_top_performance_ranks_by_query_time():
    r = client.get("/api/rollup/top", params={
        "tag_key": "tg_cc", "area": "performance", "window": 30, "workspace_ids": TG_WORKSPACES,
    })
    body = r.json()
    assert body["primary_metric"] == "duration_ms"
    assert "usd" not in body["rows"][0]["metrics"]
    # workspace/alpha (7 statements, 51s) outranks warehouse/alpha (4 statements, 38s).
    assert body["rows"][0]["level"] == "workspace"
    assert body["rows"][0]["value"] == "alpha"
