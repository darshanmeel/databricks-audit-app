"""tags.cost_day and the spend checks app/core/tag_spend.py answers from it under a tag filter."""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.app import app  # noqa: E402
from app.core import data as app_core_data  # noqa: E402
from app.core import registry  # noqa: E402
from app.core import tag_spend  # noqa: E402

client = TestClient(app)

TG_WORKSPACES = ["tg_ws_a", "tg_ws_m", "tg_ws_s"]
VARIANTS = sorted(p.stem for p in tag_spend.VARIANT_DIR.glob("*.sql"))


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect(str(app_core_data._db_path()), read_only=True)
    yield c
    c.close()


@pytest.mark.parametrize("window", [7, 30, 90])
def test_cost_day_adds_up_to_the_rollup_units(con, window):
    day = con.execute(
        "SELECT sum(usd), sum(quantity) FROM tags.cost_day "
        "WHERE usage_date >= as_of_date - CAST(? AS INTEGER) AND usage_date < as_of_date", [window],
    ).fetchone()
    unit = con.execute(
        "SELECT sum(usd), sum(quantity) FROM tags.cost_unit WHERE window_days = ?", [window]
    ).fetchone()
    assert day == pytest.approx(unit)


@pytest.mark.parametrize("query_id", VARIANTS)
def test_variant_over_every_dollar_reproduces_the_check(con, query_id):
    window = 30 if registry.by_id(query_id).windowed else 0
    got = con.execute(f"SELECT * FROM {tag_spend.variant_sql(query_id, window, 'SELECT * FROM tags.cost_day')}").df()
    want = con.execute(f'SELECT * FROM findings."f_{query_id}" WHERE window_days = {window}').df()
    assert set(want.columns) <= set(got.columns)
    assert len(want) > 0
    header = (tag_spend.VARIANT_DIR / f"{query_id}.sql").read_text(encoding="utf-8")
    skipped = {c.strip() for line in header.splitlines() if line.startswith("-- not reproduced:")
               for c in line.split(":", 1)[1].split(",")}
    for col in want.columns:
        if col != "window_days" and col not in skipped and want[col].dtype.kind in "fiu":
            assert got[col].sum() == pytest.approx(want[col].sum(), rel=1e-6, abs=0.05), col
    if "status" in want.columns:
        assert got["status"].value_counts().to_dict() == want["status"].value_counts().to_dict()


def test_tag_filter_on_a_spend_check_matches_the_rollup():
    rollup = client.get("/api/rollup", params={
        "tag_key": "tg_cc", "tag_value": "alpha", "window": 30, "workspace_ids": TG_WORKSPACES,
    }).json()["selected"]["metrics"]["usd"]
    body = client.get("/api/finding/cost_chargeback_by_workspace", params={
        "window": 30, "tag_key": "tg_cc", "tag_value": "alpha", "workspace_ids": TG_WORKSPACES,
        "limit": 5000,
    }).json()
    assert body["scope"]["tag"]["applied"] is True
    assert body["scope"]["tag"]["chain"] == tag_spend.CHAIN_WORDS
    assert sum(r["usd_list"] or 0 for r in body["rows"]) == pytest.approx(rollup, abs=0.01)


def test_tag_filter_matching_nothing_leaves_no_spend():
    body = client.get("/api/finding/overview_spend_estimate", params={
        "window": 30, "tag_key": "tg_cc", "tag_value": "no_such_value_at_all",
    }).json()
    assert body["outcome"] == "ok_empty_filters"


def test_cluster_chargeback_follows_job_pipeline_and_cluster_tags():
    params = {"window": 30, "limit": 5000}
    unfiltered = client.get("/api/finding/cost_chargeback_by_cluster", params=params).json()
    body = client.get("/api/finding/cost_chargeback_by_cluster",
                      params={**params, "tag_key": "tg_cc", "tag_value": "alpha"}).json()
    assert body["scope"]["tag"]["chain"][:5] == ["job", "job", "pipeline", "pipeline", "compute"]
    assert 0 < len(body["rows"]) < len(unfiltered["rows"])


def test_compute_check_without_a_workspace_column_falls_back_to_the_workspace_tag():
    params = {"window": 30, "limit": 5000}
    unfiltered = client.get("/api/finding/compute_warehouse_idle_gaps", params=params).json()
    body = client.get("/api/finding/compute_warehouse_idle_gaps",
                      params={**params, "tag_key": "costcenter", "tag_value": "engineering"}).json()
    assert body["scope"]["tag"]["chain"] == ["compute", "workspace"]
    assert 0 < len(body["rows"]) < len(unfiltered["rows"])


@pytest.mark.parametrize("value", ["alpha", "beta"])
def test_tag_origins_split_the_filtered_dollars_like_the_rollup_levels(value):
    entry = next(e for e in client.get("/api/rollup", params={
        "tag_key": "tg_cc", "window": 30, "workspace_ids": TG_WORKSPACES,
    }).json()["by_value"] if e["value"] == value)
    body = client.get("/api/tag_origins", params={
        "window": 30, "tag": [f"tg_cc:{value}"], "workspace_ids": TG_WORKSPACES,
    }).json()
    split = {o["origin"]: o["usd"] for o in body["keys"][0]["origins"]}
    assert body["usd"] == pytest.approx(entry["metrics"]["usd"], abs=0.01)
    assert sum(split.values()) == pytest.approx(body["usd"], abs=0.05)
    work = sum(split.get(o, 0) for o in ("query", "job", "pipeline"))
    assert work == pytest.approx(entry["by_level"]["work"]["usd"], abs=0.05)
    assert split.get("workspace", 0) == pytest.approx(entry["by_level"]["workspace"]["usd"], abs=0.05)


def test_tag_origins_without_a_tag_filter_is_empty():
    assert client.get("/api/tag_origins", params={"window": 30}).json()["keys"] == []


def test_guide_says_what_the_tag_filter_reaches_for_every_built_check():
    findings = client.get("/api/guide").json()["findings"]
    reach = {f["query_id"]: f["tag_reach"] for f in findings}
    assert reach["overview_spend_estimate"]["words"] == tag_spend.CHAIN_WORDS
    assert reach["compute_warehouse_idle_gaps"]["words"] == ["compute", "workspace"]
    assert reach["pricing_list_prices_raw"]["kind"] == "none"
