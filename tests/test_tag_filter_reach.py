"""The tag filter on the Tags page, workspace tags from config/workspace.csv, and the line a check
shows when the picked tag can't reach it."""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import data, tag_compliance, workspace_file  # noqa: E402
from app.core.tags import ChainTerm, TagChain, TagFilterSet  # noqa: E402


def _set(*entries):
    return TagFilterSet.from_params(None, None, list(entries))


def test_matcher_first_level_wins_and_across_names():
    keep = tag_compliance._matcher(_set("costcenter:fin", "env:prod", "env:dev"))
    assert keep([{"costcenter": "fin"}, {"env": "prod"}])
    assert keep([{"costcenter": "fin", "env": "dev"}, {"env": "uat"}])
    assert not keep([{"costcenter": "ops"}, {"costcenter": "fin", "env": "prod"}])
    assert not keep([{"costcenter": "fin"}])


def test_matcher_any_value_and_untagged():
    any_value = tag_compliance._matcher(_set("costcenter"))
    assert any_value([{}, {"costcenter": "x"}]) and not any_value([{}, {}])
    untagged = tag_compliance._matcher(_set("costcenter:__untagged__"))
    assert untagged([{}, {"env": "prod"}]) and not untagged([{"costcenter": "x"}])
    assert tag_compliance._matcher(None) is None


def test_bill_value_copied_from_workspace_is_the_workspaces():
    detail = {"Cost Center": {"value": "fin", "now": True}, "team": {"value": "a", "now": True, "policy": True},
              "env": {"value": "prod", "now": False}}
    ws = {"costcenter": "fin", "team": "a"}
    assert tag_compliance._bill_vals(detail, ws) == {"team": "a"}
    assert tag_compliance._bill_vals(detail, {}) == {"costcenter": "fin", "team": "a"}


def _tag_tables(con):
    con.execute("CREATE SCHEMA tags")
    con.execute("CREATE TABLE tags.tag_workspace (workspace_id VARCHAR, tag_key VARCHAR, display_key VARCHAR, "
                "tag_value VARCHAR, is_allocating BOOLEAN, top_value VARCHAR, share DOUBLE, coverage DOUBLE, reason VARCHAR)")
    con.execute("CREATE TABLE tags.tag_entity (entity_type VARCHAR, workspace_id VARCHAR, entity_id VARCHAR, tag_key VARCHAR, "
                "raw_key VARCHAR, tag_value VARCHAR, is_allocating BOOLEAN, source VARCHAR, share DOUBLE, coverage DOUBLE, reason VARCHAR)")
    con.execute("CREATE TABLE tags.tag_index (tag_key VARCHAR, display_key VARCHAR, raw_keys VARCHAR, tag_value VARCHAR, "
                "source VARCHAR, object_count BIGINT, workspace_count BIGINT, dbus DOUBLE)")
    con.execute("INSERT INTO tags.tag_workspace VALUES ('1', 'env', 'env', 'dev', TRUE, 'dev', 1, 1, NULL), "
                "('2', 'env', 'env', 'prod', TRUE, 'prod', 1, 1, NULL)")
    con.execute("INSERT INTO tags.tag_entity VALUES ('workspace', '1', '1', 'env', 'env', 'dev', TRUE, 'workspace_inferred', 1, 1, NULL), "
                "('statement', '1', 's1', 'purpose', 'purpose', 'etl', TRUE, 'query_tags', 1, 1, NULL)")


def test_workspace_file_replaces_listed_workspaces_only(tmp_path):
    csv_path = tmp_path / "workspace.csv"
    csv_path.write_text("workspace_id,Cost Center,env\n1,finance,\n", encoding="utf-8")
    con = duckdb.connect()
    _tag_tables(con)
    assert workspace_file.apply(con, csv_path) == 1
    assert con.execute("SELECT tag_key, tag_value FROM tags.tag_workspace WHERE workspace_id = '1'").fetchall() == [("costcenter", "finance")]
    assert con.execute("SELECT tag_value FROM tags.tag_workspace WHERE workspace_id = '2'").fetchall() == [("prod",)]
    assert con.execute("SELECT tag_key, tag_value, source FROM tags.tag_entity WHERE entity_type = 'workspace'").fetchall() \
        == [("costcenter", "finance", "workspace_file")]
    assert con.execute("SELECT tag_key, tag_value, object_count FROM tags.tag_index WHERE source = 'workspace_file'").fetchall() \
        == [("costcenter", "finance", 1)]
    assert workspace_file.apply(con, tmp_path / "missing.csv") == 0


def test_unreached_tag_names_where_it_is(monkeypatch):
    con = duckdb.connect()
    _tag_tables(con)
    monkeypatch.setattr(data, "_tag_key_entity_types", lambda c, key: sorted(
        r[0] for r in c.execute("SELECT DISTINCT entity_type FROM tags.tag_entity WHERE tag_key = ? AND is_allocating", [key]).fetchall()))
    chain = TagChain(kind="entity", terms=[ChainTerm("compute", "warehouse tag", "warehouse", 't."warehouse_id"'),
                                           ChainTerm("workspace", "workspace tag", "workspace", 't."workspace_id"')])
    line = data._tag_unreached(con, chain, _set("purpose:etl"))
    assert line.startswith("purpose is only on queries.") and "warehouse tag, then workspace tag" in line
    assert data._tag_unreached(con, chain, _set("env:dev")) is None
    assert data._tag_unreached(con, chain, None) is None
