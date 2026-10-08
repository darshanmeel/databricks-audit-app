"""GET /api/tag_compliance: where each mandatory tag is found, per object, on the fixture db."""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.app import app  # noqa: E402

client = TestClient(app)


def _body():
    return client.get("/api/tag_compliance", params={"window": 30}).json()


def test_every_offender_says_where_each_tag_was_found():
    body = _body()
    keys = body["keys"]
    for t in body["types"]:
        if t["outcome"] != "ok":
            continue
        allowed = set(t["levels"]) | {"missing"}
        for o in t["offenders"]:
            assert set(o["source"]) == set(keys)
            for k in keys:
                assert set(o["source"][k]) <= allowed
            # A key is listed as missing exactly when some of the object's rows found it nowhere.
            assert o["missing"] == [k for k in keys if o["source"][k].get("missing")]


def test_found_counts_add_up_to_the_total_per_key():
    for t in _body()["types"]:
        if t["outcome"] != "ok":
            continue
        for k, by_level in t["found"].items():
            assert sum(by_level.values()) == t["total"], (t["kind"], k)


def test_objects_carry_their_own_tags_and_query_units_their_tag_keys():
    body = _body()
    for t in body["types"]:
        if t["outcome"] != "ok":
            continue
        for o in t["offenders"]:
            if t["kind"] == "queries":
                assert isinstance(o["query_keys"], dict)
                assert sum(o["source"][body["keys"][0]].values()) == o["count"]
            else:
                assert isinstance(o["tags"], dict)
    clusters = next(t for t in body["types"] if t["kind"] == "clusters")
    tagged = {o["id"]: o["tags"] for o in clusters["offenders"] if o["tags"]}
    assert tagged.get("tg_cl_a1") == {"tg_cc": "alpha"}


def test_per_environment_counts_add_up_to_the_type_totals():
    for t in _body()["types"]:
        if t["outcome"] != "ok":
            continue
        by_env = t["by_env"].values()
        assert sum(e["total"] for e in by_env) == t["total"], t["kind"]
        assert sum(e["any"] for e in by_env) == t["missing"][t["levels"][-1]]["any"], t["kind"]
        for k in t["found"]:
            assert sum(e["by_key"][k] for e in by_env) == t["found"][k]["missing"], (t["kind"], k)


def test_env_counts_as_environment_unless_both_are_mandatory():
    from app.core.tag_compliance import key_variants
    assert key_variants(["costcenter", "environment"]) == {"costcenter": "costcenter", "environment": "environment", "env": "environment"}
    assert key_variants(["env", "environment"]) == {"env": "env", "environment": "environment"}


def test_a_tag_on_the_bills_last_day_counts_and_carries_its_date():
    """A key on the bill's latest day is on it today; one that left the bill earlier is not. Every
    other value the key had is listed, and a bill over a week older than the data's carries its date."""
    import duckdb

    from app.core import tag_compliance

    con = duckdb.connect()
    con.execute("CREATE SCHEMA tags")
    con.execute(
        "CREATE TABLE tags.bill_tag_dates AS SELECT * FROM (VALUES "
        "('billed_warehouse', 'w1', 'wh1', 'costcenter', 'cost-center', 'cc1', ['cc0', 'cc1'], DATE '2026-09-12', DATE '2026-10-05', DATE '2026-10-05', FALSE), "
        "('billed_warehouse', 'w1', 'wh1', 'domain', 'domain', 'd1', ['d1'], DATE '2026-08-01', DATE '2026-09-01', DATE '2026-10-05', FALSE), "
        "('billed_warehouse', 'w1', 'wh2', 'costcenter', 'cost-center', 'cc2', ['cc2'], DATE '2026-06-01', DATE '2026-06-12', DATE '2026-06-12', FALSE), "
        "('billed_warehouse', 'w1', 'wh3', 'costcenter', 'cost-center', 'cc2', ['cc2'], DATE '2026-10-01', DATE '2026-10-05', DATE '2026-10-05', TRUE)"
        ") v(entity_type, workspace_id, entity_id, tag_key, raw_key, last_value, all_values, first_date, last_date, entity_last_date, from_policy)"
    )
    # wh2's value is the workspace's own: shown, but counted at the workspace level, not the bill's.
    # wh3 has the same value from a usage policy: the policy's, so it counts.
    keys, detail, stale = tag_compliance._bill(con, "billed_warehouse", {"costcenter": "costcenter", "domain": "domain"},
                                               {("w1", "costcenter"): "cc2"})
    assert keys == {("w1", "wh1"): {"costcenter"}, ("w1", "wh3"): {"costcenter"}}
    assert detail[("w1", "wh3")]["cost-center"]["policy"] is True
    assert detail[("w1", "wh1")]["cost-center"] == {"value": "cc1", "others": ["cc0"], "since": "2026-09-12", "until": None,
                                                    "now": True, "workspace": False, "policy": False}
    assert detail[("w1", "wh2")]["cost-center"]["workspace"] is True
    assert detail[("w1", "wh1")]["domain"]["now"] is False and detail[("w1", "wh1")]["domain"]["until"] == "2026-09-01"
    assert stale == {("w1", "wh2"): "2026-06-12"}


def test_a_key_on_the_object_and_its_bill_is_credited_to_the_object():
    from app.core import tag_compliance

    t = tag_compliance._Tally(["costcenter"], ["own", "compute", "workspace"])
    gone, source = t.add(1, [{"costcenter"}, {"costcenter"}, set()], "prod")
    assert not gone and source == {"costcenter": "own"}
    gone, source = t.add(1, [set(), {"costcenter"}, {"costcenter"}], "prod")
    assert source == {"costcenter": "compute"}


def test_own_tag_and_bill_that_disagree_are_a_conflict():
    from app.core import tag_compliance

    variants = {"costcenter": "costcenter"}
    bill = {"cost-center": {"value": "b", "now": True}}
    out = tag_compliance._conflicts("wh", "1", "w", {"cost_center": "a"}, bill, variants)
    assert out == [{"name": "wh", "id": "1", "workspace_id": "w", "key": "costcenter", "own": "a", "bill": "b"}]
    assert tag_compliance._conflicts("wh", "1", "w", {"cost_center": "b"}, bill, variants) == []
    assert tag_compliance._conflicts("wh", "1", "w", {"cost_center": "a"}, {"cost-center": {"value": "b", "now": False}}, variants) == []


def test_a_query_takes_its_job_tag_before_its_compute_and_the_workspace_value_on_a_bill_is_the_workspaces():
    """Query tag, then the job that ran it, then the compute, then the workspace; a warehouse bill
    carrying only the workspace's own value doesn't count for the warehouse. Origins are counted."""
    import duckdb

    from app.core import tag_compliance

    con = duckdb.connect()
    con.execute("CREATE SCHEMA tags")
    con.execute("CREATE TABLE tags.tag_workspace AS SELECT * FROM (VALUES ('w1', 'costcenter', 'ws-cc', TRUE, ['ws-cc'])) "
                "v(workspace_id, tag_key, tag_value, is_allocating, top_values)")
    con.execute("CREATE TABLE tags.tag_entity AS SELECT * FROM (VALUES ('job', 'w1', 'j1', 'costcenter', 'cost_center', 'job-cc', TRUE)) "
                "v(entity_type, workspace_id, entity_id, tag_key, raw_key, tag_value, is_allocating)")
    con.execute("CREATE TABLE tags.perf_unit AS SELECT * FROM (VALUES (7, 'u1', 'w1', 'warehouse', 'wh1')) "
                "v(window_days, unit_id, workspace_id, compute_kind, compute_id)")
    con.execute("CREATE TABLE tags.query_tag_keys AS SELECT * FROM (VALUES "
                "(7, 'u1', 'costcenter', 'dashboard', NULL, NULL, NULL, 2), "
                "(7, 'u1', '', 'job', 'j1', NULL, NULL, 3), "
                "(7, 'u1', '', 'sql_editor', NULL, NULL, NULL, 5)) "
                "v(window_days, unit_id, own_keys, origin, job_id, pipeline_id, notebook_id, statements)")
    con.execute("CREATE TABLE tags.bill_tag_dates AS SELECT * FROM (VALUES "
                "('billed_warehouse', 'w1', 'wh1', 'costcenter', 'cost_center', 'ws-cc', ['ws-cc'], "
                "DATE '2026-09-01', DATE '2026-10-05', DATE '2026-10-05', FALSE)) "
                "v(entity_type, workspace_id, entity_id, tag_key, raw_key, last_value, all_values, first_date, last_date, entity_last_date, from_policy)")
    out = tag_compliance.compliance(con, ["cost_center"], 7, None)
    q = next(t for t in out["types"] if t["kind"] == "queries")
    assert q["levels"] == ["own", "job", "compute", "workspace"]
    assert q["found"]["costcenter"] == {"own": 2, "job": 3, "compute": 0, "workspace": 5, "missing": 0}
    assert q["origins"]["sql_editor"] == {"total": 5, "no_own": {"costcenter": 5}, "missing": {"costcenter": 0}}
    assert q["origins"]["job"]["no_own"] == {"costcenter": 3}
