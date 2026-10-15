"""tests/test_api_tags.py -- P4-T-IDX (tasks/P4-T-SPEC.md section 5.8, item 7). FastAPI
TestClient coverage for GET /api/tags and the tag_key/tag_value params on GET /api/finding/{id}
and its /aggregate twin, against the session fixture (tests/conftest.py), which includes the
tagworld fixture (tests/fixtures/tagworld.py).
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.api.app import app  # noqa: E402
from app.core import data as app_core_data  # noqa: E402

client = TestClient(app)


def test_tags_search_cc_returns_tgcc_with_untagged_and_any():
    r = client.get("/api/tags", params={"search": "cc"})
    assert r.status_code == 200
    body = r.json()
    keys = {k["tag_key"]: k for k in body["keys"]}
    assert "tgcc" in keys
    tgcc = keys["tgcc"]
    assert tgcc["untagged"] == {"tag_value": "__untagged__", "label": "untagged"}
    assert tgcc["any"] == {"tag_value": None, "label": "any value"}
    assert tgcc["display_key"] == "tg_cc"


def test_tags_search_by_value_returns_the_owning_key_with_that_value_first():
    r = client.get("/api/tags", params={"search": "q_fin"})
    assert r.status_code == 200
    body = r.json()
    keys = {k["tag_key"]: k for k in body["keys"]}
    assert "tgcc" in keys
    values = keys["tgcc"]["values"]
    assert values, "expected at least one value for tgcc"
    assert values[0]["tag_value"] == "q_fin"


def test_tags_all_values_of_one_key():
    r = client.get("/api/tags", params={"tag_key": "tg_cc"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["keys"]) == 1
    assert body["keys"][0]["tag_key"] == "tgcc"
    values = {v["tag_value"] for v in body["keys"][0]["values"]}
    assert {"alpha", "q_fin", "etl", "shared"} <= values


def test_tags_key_only_empty_value_reads_as_dunder_empty():
    r = client.get("/api/tags", params={"tag_key": "tg_pii"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["keys"]) == 1
    values = {v["tag_value"] for v in body["keys"][0]["values"]}
    assert "__empty__" in values


def test_finding_tag_value_without_tag_key_is_422():
    r = client.get("/api/finding/query_costly_statements", params={"tag_value": "alpha"})
    assert r.status_code == 422


def test_finding_tag_key_normalising_to_empty_is_422():
    r = client.get("/api/finding/query_costly_statements", params={"tag_key": "___"})
    assert r.status_code == 422


def test_finding_tag_applied_true_on_query_costly_statements():
    r = client.get(
        "/api/finding/query_costly_statements",
        params={"window": 30, "tag_key": "tg_cc", "tag_value": "alpha"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["tag"] == {"tag_key": "tgcc", "display_key": "tgcc", "tag_value": "alpha"}
    assert body["scope"]["tag"]["applied"] is True
    assert body["scope"]["tag"]["chain"] == ["query", "compute", "workspace"]
    ids = {row["statement_id"] for row in body["rows"]}
    assert ids == {"tg_stmt_4", "tg_stmt_6", "tg_stmt_9"}


def test_finding_tag_filter_reaches_lakeflow_jobs_no_timeout_by_job_tag():
    # lakeflow_jobs_no_timeout is one row per job now (job-grain, not workspace-grain), so a job
    # tag reaches it directly -- chain[0] == "job" (job tag tried before the job's workspace tag),
    # and filtering by one job's own tag value returns strictly fewer rows than the unfiltered set.
    unfiltered = client.get("/api/finding/lakeflow_jobs_no_timeout", params={"window": 30})
    assert unfiltered.status_code == 200
    unfiltered_body = unfiltered.json()

    r = client.get(
        "/api/finding/lakeflow_jobs_no_timeout",
        params={"window": 30, "tag_key": "tg_cc", "tag_value": "etl"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["scope"]["tag"]["applied"] is True
    assert body["scope"]["tag"]["chain"][0] == "job"
    assert len(body["rows"]) < len(unfiltered_body["rows"])
    ids = {row["job_id"] for row in body["rows"]}
    assert ids == {"tg_job_a1"}


def test_finding_tag_applied_false_on_a_price_list():
    # A price list has no column a tag can reach.
    r = client.get(
        "/api/finding/pricing_list_prices_raw",
        params={"window": 30, "tag_key": "tg_cc", "tag_value": "alpha"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["scope"]["tag"]["applied"] is False
    assert body["scope"]["tag"]["reason"]


def test_aggregate_under_tag_filter_sums_only_matching_rows():
    unfiltered = client.get(
        "/api/finding/lakeflow_job_reliability/aggregate",
        params={"window": 30, "agg": "count"},
    ).json()
    filtered = client.get(
        "/api/finding/lakeflow_job_reliability/aggregate",
        params={"window": 30, "agg": "count", "tag_key": "tg_cc", "tag_value": "etl"},
    ).json()
    assert filtered["outcome"] == "ok_rows"
    # Pinned (section 5.8 item 6): tg_cc=etl matches only tg_job_a1's own single row (section
    # 7.5's own chain -- lakeflow_job_reliability is one row per job_id).
    assert filtered["matched_rows"] == 1
    assert filtered["matched_rows"] < unfiltered["matched_rows"]
    assert filtered["scope"]["tag"]["applied"] is True


# -------------------------------------------------------------------------------------------
# GET /api/findings -- section 5.5/5.8 item 7: the bulk list's own row/status counts, tag_applied
# and tag_chain_label reflect an active tag filter, not just a single finding's read/count/
# aggregate.
# -------------------------------------------------------------------------------------------


def test_findings_list_tag_applied_matches_section_7_5():
    r = client.get("/api/findings", params={"window": 30, "tag_key": "tg_cc", "tag_value": "alpha"})
    assert r.status_code == 200
    body = r.json()
    assert body["tag"] == {"tag_key": "tgcc", "display_key": "tgcc", "tag_value": "alpha"}
    by_id = {f["query_id"]: f for f in body["findings"]}

    not_applicable = by_id["pricing_list_prices_raw"]
    assert not_applicable["tag_applied"] is False
    assert not_applicable["outcome"] == "ok_rows"  # unfiltered, never silently emptied

    # A spend check is re-computed over the dollars whose most specific tag matches.
    dollarized = by_id["cost_dollarized_by_sku_day"]
    assert dollarized["tag_applied"] is True
    assert dollarized["tag_chain"] == ["query", "job", "pipeline", "compute", "workspace"]

    costly = by_id["query_costly_statements"]
    assert costly["tag_applied"] is True
    assert costly["tag_chain"] == ["query", "compute", "workspace"]
    assert costly["row_count"] == 3  # tg_stmt_4, tg_stmt_6, tg_stmt_9 -- section 7.5's own alpha set


def test_findings_list_row_tags_untagged_is_not_applicable_not_silently_empty():
    # Section 4.5/7.5, DEC-60 rule 6: cost_chargeback_by_tag lists tagged billing rows only, so
    # `__untagged__` is not a filter this table can answer -- the list must say so (tag_applied
    # false) rather than reporting a row_count of 0 that looks like "nothing is untagged".
    r = client.get(
        "/api/findings", params={"window": 30, "tag_key": "tg_cc", "tag_value": "__untagged__"}
    )
    assert r.status_code == 200
    row = {f["query_id"]: f for f in r.json()["findings"]}["cost_chargeback_by_tag"]
    assert row["tag_applied"] is False
    assert row["outcome"] == "ok_rows"
    assert row["row_count"] > 0


# -------------------------------------------------------------------------------------------
# Several tag names (repeatable `tag=<key>[:<value>]`): OR within one name, AND across names,
# additive to the legacy tag_key/tag_value pair above (which keeps meaning exactly what it does).
# -------------------------------------------------------------------------------------------


def test_finding_tag_param_or_within_a_name_matches_the_union():
    r = client.get(
        "/api/finding/cost_by_compute_resource",
        params={"window": 30, "tag": ["tg_cc:alpha", "tg_cc:etl"]},
    )
    assert r.status_code == 200
    ids = {row.get("cluster_id") or row.get("warehouse_id") or row.get("instance_pool_id") for row in r.json()["rows"]}
    assert ids == {"tg_wh_a1", "tg_wh_a2", "tg_cl_a1", "tg_cl_a2"}


def test_finding_tag_param_and_across_names_narrows_to_the_intersection():
    # tg_wh_a1 alone carries both tg_cc=alpha and tg_env=prod (tg_wh_a2 has neither).
    r = client.get(
        "/api/finding/compute_warehouse_config_posture",
        params={"tag": ["tg_cc:alpha", "tg_env:prod"]},
    )
    assert r.status_code == 200
    assert {row["warehouse_id"] for row in r.json()["rows"]} == {"tg_wh_a1"}


def test_finding_tag_param_bare_key_is_all():
    with_pair = client.get(
        "/api/finding/cost_by_compute_resource", params={"window": 30, "tag_key": "tg_cc"}
    ).json()
    with_bare = client.get(
        "/api/finding/cost_by_compute_resource", params={"window": 30, "tag": ["tg_cc"]}
    ).json()
    assert {r["cluster_id"] or r["warehouse_id"] or r["instance_pool_id"] for r in with_pair["rows"]} == \
           {r["cluster_id"] or r["warehouse_id"] or r["instance_pool_id"] for r in with_bare["rows"]}


def test_finding_tag_param_bad_entry_is_422():
    r = client.get("/api/finding/query_costly_statements", params={"tag": ["___:x"]})
    assert r.status_code == 422


def test_finding_tag_json_stays_the_legacy_shape_for_a_single_pair():
    # "today's single key:value keeps working": one tag_key/tag_value pair, or one bare `tag`
    # entry naming exactly one value, echoes the EXACT old {tag_key, display_key, tag_value} shape
    # -- no `groups` key -- so an existing caller's own equality check never sees a new field.
    r = client.get(
        "/api/finding/query_costly_statements",
        params={"window": 30, "tag_key": "tg_cc", "tag_value": "alpha"},
    )
    assert r.json()["tag"] == {"tag_key": "tgcc", "display_key": "tgcc", "tag_value": "alpha"}


def test_finding_tag_json_carries_groups_for_several_names():
    r = client.get(
        "/api/finding/compute_warehouse_config_posture",
        params={"tag": ["tg_cc:alpha", "tg_env:prod"]},
    )
    tag = r.json()["tag"]
    assert tag["groups"] == [
        {"tag_key": "tgcc", "display_key": "tgcc", "values": ["alpha"]},
        {"tag_key": "tgenv", "display_key": "tgenv", "values": ["prod"]},
    ]


def test_aggregate_tag_param_or_within_a_name():
    r = client.get(
        "/api/finding/lakeflow_job_reliability/aggregate",
        params={"window": 30, "agg": "count", "tag": ["tg_cc:etl", "tg_cc:shared"]},
    )
    body = r.json()
    assert body["outcome"] == "ok_rows"
    # tg_job_a1 (etl) + tg_job_a2 (shared, via billing:budget_policy) -- one row each.
    assert body["matched_rows"] == 2


def test_findings_list_tag_param_and_across_names_reduces_row_count():
    r = client.get(
        "/api/findings",
        params={"window": 30, "tag": ["tg_cc:alpha", "tg_env:prod"]},
    )
    assert r.status_code == 200
    row = {f["query_id"]: f for f in r.json()["findings"]}["compute_warehouse_config_posture"]
    assert row["tag_applied"] is True
    assert row["row_count"] == 1


def test_findings_list_missing_tag_index_is_not_assessed(tmp_path, monkeypatch):
    src = os.environ.get("AUDIT_DB", str(ROOT / "tests" / "db_audit_test.duckdb"))
    db_copy = tmp_path / "no_tag_entity.duckdb"
    shutil.copy(src, db_copy)
    c = duckdb.connect(str(db_copy))
    try:
        c.execute("DROP TABLE tags.tag_entity")
    finally:
        c.close()
    monkeypatch.setenv("AUDIT_DB", str(db_copy))
    assert app_core_data._db_path() == Path(db_copy)  # sanity: the env var actually took

    r = client.get("/api/findings", params={"window": 30, "tag_key": "tg_cc", "tag_value": "alpha"})
    assert r.status_code == 200
    row = {f["query_id"]: f for f in r.json()["findings"]}["query_costly_statements"]
    assert row["outcome"] == "not_assessed"
    assert row["not_assessed_reason"] == "tag_models_not_built"
