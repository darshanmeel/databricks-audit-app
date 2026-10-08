"""tests/test_tags_index.py -- P4-T-IDX (tasks/P4-T-SPEC.md section 5.8, items 1-3). Pins
dbt/models/tags/tag_workspace.sql, tag_entity.sql and tag_index.sql against the shared tagworld
fixture (tests/fixtures/tagworld.py) to the exact numbers in the spec's section 7.1/7.4/5.8.

Reads tests/db_audit_test.duckdb directly (read-only), the same file app/core/data.py points at
via AUDIT_DB (tests/conftest.py's autouse session fixture) -- no app/core call is needed since the
`tags` schema is not yet wired into any of it. tagworld.py's own rows only, via the `tg_` id
prefix -- other builders' rows for these tg-only keys are simply absent.

Run after:
    python tests/fixtures/build_fixtures.py
    python tools/dbt_run.py build --target test
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def con():
    db_path = os.environ.get("AUDIT_DB", str(ROOT / "tests" / "db_audit_test.duckdb"))
    c = duckdb.connect(db_path, read_only=True)
    try:
        yield c
    finally:
        c.close()


# -------------------------------------------------------------------------------------------
# 1. tag_workspace -- section 7.1 exactly.
# -------------------------------------------------------------------------------------------


def test_tag_workspace_matches_section_7_1(con):
    rows = con.execute(
        "SELECT workspace_id, tag_key, tag_value, is_allocating, "
        "round(share, 4), round(coverage, 4), dbus_tagged, dbus_total "
        "FROM tags.tag_workspace WHERE workspace_id LIKE 'tg_%' ORDER BY workspace_id, tag_key"
    ).fetchall()
    assert rows == [
        # read over billed usage without a usage policy: tg_ws_a's serverless rows count, its policy row doesn't
        ("tg_ws_a", "tgcc", "alpha", True, 0.7885, 0.7602, 260.0, 342.0),
        ("tg_ws_a", "tgenv", "__untagged__", False, 1.0, 0.4386, 150.0, 342.0),
        ("tg_ws_m", "tgcc", "__mixed__", False, 0.5556, 0.9, 90.0, 100.0),
        ("tg_ws_s", "tgcc", "__untagged__", False, 1.0, 0.2, 20.0, 100.0),
    ]


def test_tag_workspace_no_row_for_tgapp(con):
    # "No tg workspace has a row for tgapp: query tags are not on the bill" (section 7.1).
    n = con.execute(
        "SELECT count(*) FROM tags.tag_workspace WHERE workspace_id LIKE 'tg_%' AND tag_key = 'tgapp'"
    ).fetchone()[0]
    assert n == 0


# -------------------------------------------------------------------------------------------
# 2. tag_entity -- section 7.4 exactly, for tg_% entities/workspaces and key tgcc, plus tgpii.
# -------------------------------------------------------------------------------------------


def _tgcc_rows(con) -> list[tuple]:
    return con.execute(
        "SELECT entity_type, entity_id, tag_value, is_allocating, source "
        "FROM tags.tag_entity "
        "WHERE tag_key = 'tgcc' AND (entity_id LIKE 'tg_%' OR workspace_id LIKE 'tg_%') "
        "ORDER BY entity_type, entity_id"
    ).fetchall()


def test_tag_entity_tgcc_matches_section_7_4(con):
    rows = _tgcc_rows(con)
    expected = [
        ("billed_cluster", "tg_cl_a1", "alpha", True, "billing:cluster"),
        ("billed_cluster", "tg_cl_a2", "etl", True, "billing:cluster"),
        ("billed_cluster", "tg_cl_a3", "poolteam", True, "billing:cluster"),
        ("billed_cluster", "tg_cl_m1", "red", True, "billing:cluster"),
        ("billed_cluster", "tg_cl_m2", "blue", True, "billing:cluster"),
        ("billed_cluster", "tg_cl_s1", "gamma", True, "billing:cluster"),
        ("billed_job", "tg_job_a1", "etl", True, "billing:cluster"),
        ("billed_job", "tg_job_a2", "shared", True, "billing:budget_policy"),
        ("billed_pool", "tg_pool_a1", "poolteam", True, "billing:cluster"),
        ("billed_warehouse", "tg_wh_a1", "alpha", True, "billing:warehouse"),
        ("billed_warehouse", "tg_wh_a3", "beta", True, "billing:warehouse"),
        ("cluster", "tg_cl_a1", "alpha", True, "cluster_tags"),
        ("cluster", "tg_cl_a3", "poolteam", True, "pool_tags"),
        ("cluster", "tg_cl_m1", "red", True, "cluster_tags"),
        ("cluster", "tg_cl_m2", "blue", True, "cluster_tags"),
        ("cluster", "tg_cl_s1", "gamma", True, "cluster_tags"),
        ("job", "tg_job_a1", "etl", True, "job_tags"),
        ("pipeline", "tg_pl_a1", "stream", True, "pipeline_tags"),
        ("pool", "tg_pool_a1", "poolteam", True, "pool_tags"),
        ("statement", "tg_stmt_1", "q_fin", True, "query_tags"),
        ("statement", "tg_stmt_10", "q_fin", True, "query_tags"),
        ("statement", "tg_stmt_2", "q_fin", True, "query_tags"),
        ("statement", "tg_stmt_5", "q_mkt", True, "query_tags"),
        ("statement", "tg_stmt_7", "delta", True, "query_tags"),
        ("uc_schema", "tg_cat.tg_sch", "data_sch", True, "uc_schema_tags"),
        ("uc_table", "tg_cat.tg_sch.tg_tbl1", "data_fin", True, "uc_table_tags"),
        ("uc_volume", "tg_cat.tg_sch.tg_vol1", "data_vol", True, "uc_volume_tags"),
        ("warehouse", "tg_wh_a1", "alpha", True, "warehouse_tags"),
        ("warehouse", "tg_wh_a3", "beta", True, "warehouse_tags"),
        ("workspace", "tg_ws_a", "alpha", True, "workspace_inferred"),
        ("workspace", "tg_ws_m", "__mixed__", False, "workspace_inferred"),
        ("workspace", "tg_ws_s", "__untagged__", False, "workspace_inferred"),
    ]
    assert rows == expected


@pytest.mark.parametrize(
    "entity_type,entity_id",
    [
        ("warehouse", "tg_wh_a2"),
        ("warehouse", "tg_wh_s1"),
        ("cluster", "tg_cl_a2"),
        ("cluster", "tg_cl_m3"),
        ("job", "tg_job_a2"),
        ("job", "tg_job_m1"),
        ("billed_pipeline", "tg_pl_a1"),
        ("billed_job", "tg_job_m1"),
    ],
)
def test_tag_entity_no_row_where_the_spec_says_none(con, entity_type, entity_id):
    # Section 7.4's explicit "there are no rows for" list -- absence, not an __untagged__ row
    # (tags.tag_entity, like tag_workspace, only emits a row for a (entity, key) it actually saw
    # at least one tagged usage row for).
    n = con.execute(
        "SELECT count(*) FROM tags.tag_entity WHERE entity_type = ? AND entity_id = ? AND tag_key = 'tgcc'",
        [entity_type, entity_id],
    ).fetchone()[0]
    assert n == 0


def test_tag_entity_tgpii_empty_value_is_allocating(con):
    row = con.execute(
        "SELECT entity_type, entity_id, tag_value, is_allocating, source "
        "FROM tags.tag_entity WHERE tag_key = 'tgpii'"
    ).fetchall()
    assert row == [("uc_column", "tg_cat.tg_sch.tg_tbl1.col1", "", True, "uc_column_tags")]


# -------------------------------------------------------------------------------------------
# 3. tag_index -- section 5.8 item 3.
# -------------------------------------------------------------------------------------------


def test_tag_index_tgcc_display_key_and_raw_keys(con):
    rows = con.execute(
        "SELECT DISTINCT display_key, raw_keys FROM tags.tag_index WHERE tag_key = 'tgcc'"
    ).fetchall()
    assert len(rows) == 1
    display_key, raw_keys = rows[0]
    assert display_key == "tg_cc"
    raw_key_list = raw_keys.split(",")
    assert "TG-CC" in raw_key_list
    assert "tg_cc" in raw_key_list


def test_tag_index_alpha_sources(con):
    sources = set(
        r[0]
        for r in con.execute(
            "SELECT source FROM tags.tag_index WHERE tag_key = 'tgcc' AND tag_value = 'alpha'"
        ).fetchall()
    )
    assert {"billing_custom_tags", "warehouse_tags", "cluster_tags", "workspace_inferred"} <= sources


def test_tag_index_tgapp_sources_are_query_only(con):
    sources = set(
        r[0] for r in con.execute("SELECT DISTINCT source FROM tags.tag_index WHERE tag_key = 'tgapp'").fetchall()
    )
    assert sources == {"query_tags", "attributed_query_tags"}


def test_tag_index_tgpii_empty_value_source(con):
    rows = con.execute(
        "SELECT tag_value, source FROM tags.tag_index WHERE tag_key = 'tgpii'"
    ).fetchall()
    assert rows == [("", "uc_column_tags")]
