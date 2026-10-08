"""tests/test_scope_disclosure.py -- what a finding discloses about its own scope.

(1) Region: tools/snapshot.py connects through one workspace, so every regional system table
holds rows only for that metastore region's workspaces while billing is account-wide.
dims.dim_workspace marks the workspaces no regional table holds a row for, and a regional finding
read for such workspaces alone is NOT_ASSESSED ("outside the snapshot's region"), never
ok_empty_filters. (2) Filter reach: every finding says whether the workspace filter reaches it
(has_workspace_id) and whether it reads regional tables (regional).

Fixture facts are re-derived by test_fixture_presence_facts from tests/fixtures/parquet: 9002
(dd-uat) appears only in access.workspaces_latest (outside, unbilled); 1111/2222/3333 are billed
and regional; 9001 is regional and unbilled. Gate-fix follow-up (P3 fixture workspaces):
cb_ws1 (tests/fixtures/chargeback.py) is billed in system.billing.usage and named via
access.workspaces_latest, but deliberately has no row in any regional presence table -- a real,
always-present "billed outside the snapshot's region" workspace (the same shape `billed_outside_env`
below simulates for 3333 in a COPY of the db, kept as its own scenario since it additionally proves
a PREVIOUSLY-in-region workspace dropping out). wu_ws9 (tests/fixtures/waste_usd.py) is billed AND
regional (it writes a system.serving.served_entities row) and named via access.workspaces_latest,
so it reads as an ordinary in-region workspace throughout. iw_ws8 (tests/fixtures/idle_waste.py,
P4-01-W1) is likewise billed AND regional (compute.warehouses, query.history) and named, so it
too reads as an ordinary in-region workspace throughout. `single_region_env` flips cb_ws1 to
in-region in a COPY of the db, to keep the no-gap single-region case covered next to the real
cb_ws1 gap. uw_crit/uw_ok/uw_warn (tests/fixtures/billing.py SEC N, cost_unnamed_workspaces) are
the same shape as cb_ws1 -- billed, outside every regional table -- except never named anywhere,
not even access.workspaces_latest; `single_region_env` flips them in-region alongside cb_ws1 too.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api import service  # noqa: E402
from app.api.app import app  # noqa: E402
from app.core import data as app_core_data  # noqa: E402
from app.core import registry  # noqa: E402

client = TestClient(app)

PARQUET = ROOT / "tests" / "fixtures" / "parquet"
REGIONAL_PRESENCE_FOLDERS = (
    "compute__clusters", "compute__warehouses", "compute__instance_pools",
    "lakeflow__jobs", "lakeflow__job_run_timeline", "lakeflow__pipelines",
    "query__history", "serving__served_entities",
    "storage__predictive_optimization_operations_history", "access__audit",
)
FIXTURE_METASTORE_ID = "aws:us-east-1:00000000-0000-4000-8000-000000000071"
FIXTURE_METASTORE = {"cloud": "aws", "region": "us-east-1", "id_fingerprint": "223290a343c0aa60"}

BILLED_REASON = (
    "billed in system.billing.usage, but none of the snapshot's regional system tables (compute, "
    "lakeflow, query, serving, storage, access audit) has a row for it: most likely it belongs to "
    "another region's metastore, which this snapshot did not connect through, or the regional "
    "schemas it uses were not exported or not enabled (see Coverage)"
)
UNBILLED_REASON = (
    "none of the snapshot's regional system tables (compute, lakeflow, query, serving, storage, "
    "access audit) has a row for it and it has no billed usage in the snapshot, so nothing places "
    "it in the snapshot's region"
)
UNKNOWN_REASON = (
    "the snapshot holds no row in any regional system table (not exported, not granted or "
    "empty), so no workspace can be placed inside or outside its region"
)
OUTSIDE_WS = "9002"
ENTRY_9002 = {"workspace_id": "9002", "name": "dd-uat", "billed": False, "reason": UNBILLED_REASON}
ENTRY_3333 = {"workspace_id": "3333", "name": "acme-uat", "billed": True, "reason": BILLED_REASON}
# cb_ws1 (tests/fixtures/chargeback.py): billed, named, deliberately outside every regional
# presence table -- a real (not simulated) "billed outside the snapshot's region" workspace.
ENTRY_CB_WS1 = {
    "workspace_id": "cb_ws1", "name": "cb-chargeback", "billed": True, "reason": BILLED_REASON,
}
# uw_crit/uw_ok/uw_warn (tests/fixtures/billing.py SEC N, cost_unnamed_workspaces): billed, no
# name anywhere (not even access.workspaces_latest), and -- like cb_ws1 -- outside every regional
# presence table: a real "deleted/inaccessible workspace" has no current regional footprint either.
ENTRY_UW_CRIT = {"workspace_id": "uw_crit", "name": None, "billed": True, "reason": BILLED_REASON}
ENTRY_UW_OK = {"workspace_id": "uw_ok", "name": None, "billed": True, "reason": BILLED_REASON}
ENTRY_UW_WARN = {"workspace_id": "uw_warn", "name": None, "billed": True, "reason": BILLED_REASON}
# tests/fixtures/chargeback_a.py: 7 of its 9 dedicated workspaces (all but WS_WH/WS_ID, which also
# write query.history/compute.clusters rows and so are in-region) are billed-only -- same shape as
# cb_ws1/uw_*, never named anywhere.
ENTRY_CBA_WS_CRIT = {"workspace_id": "cba_ws_crit", "name": None, "billed": True, "reason": BILLED_REASON}
ENTRY_CBA_WS_NA_CUR = {"workspace_id": "cba_ws_na_cur", "name": None, "billed": True, "reason": BILLED_REASON}
ENTRY_CBA_WS_OK = {"workspace_id": "cba_ws_ok", "name": None, "billed": True, "reason": BILLED_REASON}
ENTRY_CBA_WS_SKU = {"workspace_id": "cba_ws_sku", "name": None, "billed": True, "reason": BILLED_REASON}
ENTRY_CBA_WS_SVC = {"workspace_id": "cba_ws_svc", "name": None, "billed": True, "reason": BILLED_REASON}
ENTRY_CBA_WS_WARN = {"workspace_id": "cba_ws_warn", "name": None, "billed": True, "reason": BILLED_REASON}
ENTRY_CBA_WS_WIN = {"workspace_id": "cba_ws_win", "name": None, "billed": True, "reason": BILLED_REASON}
# tests/fixtures/genie.py: its two workspaces are billed-only (Genie usage reads only the bill),
# named via access.workspaces_latest.
ENTRY_GN_WS_DEV = {"workspace_id": "gn_ws_dev", "name": "genie-dev", "billed": True, "reason": BILLED_REASON}
ENTRY_GN_WS_PROD = {"workspace_id": "gn_ws_prod", "name": "genie-prod", "billed": True, "reason": BILLED_REASON}
# Sorted, billed-outside-only gap entries shared by every test below that lists the whole gap.
CBA_GAP_ENTRIES = [
    ENTRY_CBA_WS_CRIT, ENTRY_CBA_WS_NA_CUR, ENTRY_CBA_WS_OK, ENTRY_CBA_WS_SKU, ENTRY_CBA_WS_SVC,
    ENTRY_CBA_WS_WARN, ENTRY_CBA_WS_WIN, ENTRY_GN_WS_DEV, ENTRY_GN_WS_PROD,
]

REGIONAL_WS_QUERY_ID = "classic_clusters_config_current"
REGIONAL_WS_WINDOWED_QUERY_ID = "lakeflow_failed_runs"
REGIONAL_WS_3333_QUERY_ID = "lakeflow_health_rule_coverage"
# P4-FIXES28: cost_by_job now also reads system.lakeflow.jobs (regional) to resolve job names, so
# it no longer qualifies as a global-sources-only, has-workspace_id example; swapped for a query
# that still reads only GLOBAL_SOURCES.
GLOBAL_WS_QUERY_ID = "cost_dollarized_by_sku_day"
REGIONAL_NO_WS_QUERY_ID = "access_grants_inventory"


# ------------------------------------------------------------------------------------------
# Fixture facts, re-derived (never assumed)
# ------------------------------------------------------------------------------------------
def _distinct_ids(folder: str, extra: str = "") -> set[str]:
    pattern = (PARQUET / folder / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"SELECT DISTINCT workspace_id FROM read_parquet('{pattern}', union_by_name=true) "
            f"WHERE workspace_id IS NOT NULL {extra}"
        ).fetchall()
    finally:
        con.close()
    return {str(r[0]) for r in rows}


def test_fixture_presence_facts():
    union: set[str] = set()
    for folder in REGIONAL_PRESENCE_FOLDERS:
        extra = "AND workspace_id <> '0'" if folder == "access__audit" else ""
        union |= _distinct_ids(folder, extra)
    # wu_ws9 (tests/fixtures/waste_usd.py) writes a serving__served_entities row, and iw_ws8
    # (tests/fixtures/idle_waste.py, P4-01-W1) writes compute__warehouses and query__history rows
    # -- both are folders in this union's own REGIONAL_PRESENCE_FOLDERS -- so both are real
    # in-region workspaces.
    # P4-T-ROLL (tasks/P4-T-SPEC.md section 7.6): tagworld's three workspaces each write at least
    # one regional-presence row (compute.warehouses/clusters/instance_pools, lakeflow.jobs/
    # job_run_timeline/pipelines, query.history), so all three are in-region.
    # tbc_ws1 (top_by_cost.py) writes query__history rows; 9101 (oversized_jobs.py) writes
    # lakeflow.jobs + compute.clusters rows; 9301 (hour_of_day.py) writes job_run_timeline +
    # query.history rows -- all in-region.
    # cba_ws_wh/cba_ws_id (chargeback_a.py) write query.history rows (cba_ws_id also
    # compute.clusters) -- both in-region. cc_ws1 (compute_coverage.py) writes compute.warehouses
    # and query.history rows -- in-region too. 7705 (cases_jobs.py) writes job_run_timeline rows:
    # in-region, never billed.
    assert union == {
        "1111", "2222", "3333", "7705", "9001", "9101", "9301", "cba_ws_id", "cba_ws_wh", "cc_ws1",
        "iw_ws8", "tbc_ws1", "tg_ws_a", "tg_ws_m", "tg_ws_s", "wu_ws9",
    }
    # cb_ws1 (tests/fixtures/chargeback.py) has no row in any regional presence table above, so it is
    # billed but outside the union. uw_crit/uw_ok/uw_warn (tests/fixtures/billing.py SEC N) are the
    # same shape, and never named anywhere -- cost_unnamed_workspaces' own fixture. 7 of
    # chargeback_a.py's 9 dedicated workspaces (all but cba_ws_wh/cba_ws_id) are the same
    # billed-only shape.
    assert _distinct_ids("billing__usage") == {
        "1111", "2222", "3333", "9101", "9301", "cb_ws1", "cba_ws_crit", "cba_ws_id",
        "cba_ws_na_cur", "cba_ws_ok", "cba_ws_sku", "cba_ws_svc", "cba_ws_warn", "cba_ws_wh",
        "cba_ws_win", "cc_ws1", "gn_ws_dev", "gn_ws_prod", "iw_ws8", "tbc_ws1", "tg_ws_a", "tg_ws_m",
        "tg_ws_s", "uw_crit", "uw_ok", "uw_warn", "wu_ws9",
    }
    assert "9002" in _distinct_ids("access__workspaces_latest")


def test_dim_workspace_marks_region_presence_and_spend():
    df = app_core_data.read_dim_workspace().set_index("workspace_id")
    expected = {
        "1111": (True, True), "2222": (True, True), "3333": (True, True),
        "7705": (True, False), "9001": (True, False), "9002": (False, False),
        "cb_ws1": (False, True), "iw_ws8": (True, True), "wu_ws9": (True, True),
        # P4-T-ROLL (tasks/P4-T-SPEC.md section 7.6): tagworld's three workspaces, in-region and billed.
        "tg_ws_a": (True, True), "tg_ws_m": (True, True), "tg_ws_s": (True, True),
        # tests/fixtures/billing.py SEC N (cost_unnamed_workspaces): billed, outside region, same
        # shape as cb_ws1.
        "uw_crit": (False, True), "uw_ok": (False, True), "uw_warn": (False, True),
        # top_by_cost.py, oversized_jobs.py and hour_of_day.py: in-region and billed.
        "tbc_ws1": (True, True), "9101": (True, True), "9301": (True, True),
        # chargeback_a.py: cba_ws_wh/cba_ws_id are in-region (query.history, plus compute.clusters
        # for cba_ws_id); the other 7 are billed-only, same shape as cb_ws1/uw_*.
        "cba_ws_wh": (True, True), "cba_ws_id": (True, True),
        "cba_ws_crit": (False, True), "cba_ws_na_cur": (False, True), "cba_ws_ok": (False, True),
        "cba_ws_sku": (False, True), "cba_ws_svc": (False, True), "cba_ws_warn": (False, True),
        "cba_ws_win": (False, True),
        # genie.py: billed-only, same shape as cb_ws1/uw_*.
        "gn_ws_dev": (False, True), "gn_ws_prod": (False, True),
        # compute_coverage.py: in-region (compute.warehouses, query.history) and billed.
        "cc_ws1": (True, True),
    }
    assert set(df.index) == set(expected)
    for ws, (in_region, billed) in expected.items():
        assert bool(df.loc[ws, "in_snapshot_region"]) is in_region, ws
        assert bool(df.loc[ws, "billed_in_snapshot"]) is billed, ws


# Every domain other than "cost" reads at least one regional table, and every "cost"-domain query
# read only GLOBAL_SOURCES tables -- until P4-01-W2's cost_failed_statement_waste, whose SCOPE
# note says it prices failed SQL statements from system.query.history (a regional table; the
# query's own header caveats say "Regional (query.history)"). Named here, not folded into the
# blanket domain check, so a future cost-domain query that becomes accidentally regional still
# trips this test unless it is deliberately added to this set. cost_by_hour_of_day is the same
# shape: it also reads system.lakeflow.job_run_timeline and system.query.history alongside billing.
# cost_by_job/cost_by_compute_resource (P4-FIXES28): now resolve names via system.lakeflow.jobs /
# system.compute.{clusters,warehouses,instance_pools} respectively, both regional tables.
# cost_audit_self_usage reads system.query.history and system.compute.warehouses (both regional)
# to attribute this app's own audit-query spend. cost_chargeback_by_cluster/by_job resolve owner
# names via system.lakeflow.jobs/pipelines and system.compute.clusters; cost_chargeback_by_
# warehouse and cost_chargeback_identity_by_source read system.query.history for the same reason
# cost_failed_statement_waste does (identity/duration attribution, a regional table).
REGIONAL_COST_EXCEPTIONS = frozenset({
    "cost_failed_statement_waste", "cost_by_hour_of_day", "cost_by_job", "cost_by_compute_resource",
    "cost_audit_self_usage", "cost_chargeback_by_cluster", "cost_chargeback_by_job",
    "cost_chargeback_by_warehouse", "cost_chargeback_identity_by_source",
})


def test_is_regional_follows_the_account_wide_sources():
    assert app_core_data.GLOBAL_SOURCES == frozenset({
        "system.billing.usage", "system.billing.list_prices",
        "system.billing.attributed_usage", "system.access.workspaces_latest",
    })
    for s in registry.load_registry():
        if not s.executable:
            continue
        expected_regional = s.domain != "cost" or s.query_id in REGIONAL_COST_EXCEPTIONS
        assert app_core_data.spec_is_regional(s) is expected_regional, s.query_id
        assert app_core_data.is_regional(s.query_id) is app_core_data.spec_is_regional(s)


# ------------------------------------------------------------------------------------------
# region_split on the fixture db
# ------------------------------------------------------------------------------------------
def test_region_split_on_the_fixture_db():
    assert app_core_data.region_split() == {
        "filtered": False,
        # tagworld's three workspaces, tbc_ws1 (top_by_cost.py), uw_crit/uw_ok/uw_warn
        # (billing.py SEC N), 9101/9301 (oversized_jobs.py / hour_of_day.py), chargeback_a.py's 9
        # workspaces and cc_ws1 (compute_coverage.py), sorted.
        "selected": [
            "1111", "2222", "3333", "7705", "9001", "9002", "9101", "9301", "cb_ws1", "cba_ws_crit",
            "cba_ws_id", "cba_ws_na_cur", "cba_ws_ok", "cba_ws_sku", "cba_ws_svc", "cba_ws_warn",
            "cba_ws_wh", "cba_ws_win", "cc_ws1", "gn_ws_dev", "gn_ws_prod", "iw_ws8", "tbc_ws1",
            "tg_ws_a", "tg_ws_m", "tg_ws_s", "uw_crit", "uw_ok", "uw_warn", "wu_ws9",
        ],
        "outside": [
            {"workspace_id": "9002", "name": "dd-uat", "billed": False},
            {"workspace_id": "cb_ws1", "name": "cb-chargeback", "billed": True},
            {"workspace_id": "cba_ws_crit", "name": None, "billed": True},
            {"workspace_id": "cba_ws_na_cur", "name": None, "billed": True},
            {"workspace_id": "cba_ws_ok", "name": None, "billed": True},
            {"workspace_id": "cba_ws_sku", "name": None, "billed": True},
            {"workspace_id": "cba_ws_svc", "name": None, "billed": True},
            {"workspace_id": "cba_ws_warn", "name": None, "billed": True},
            {"workspace_id": "cba_ws_win", "name": None, "billed": True},
            {"workspace_id": "gn_ws_dev", "name": "genie-dev", "billed": True},
            {"workspace_id": "gn_ws_prod", "name": "genie-prod", "billed": True},
            {"workspace_id": "uw_crit", "name": None, "billed": True},
            {"workspace_id": "uw_ok", "name": None, "billed": True},
            {"workspace_id": "uw_warn", "name": None, "billed": True},
        ],
        "unknown": [],
    }
    assert app_core_data.region_split(["9002"]) == {
        "filtered": True, "selected": ["9002"],
        "outside": [{"workspace_id": "9002", "name": "dd-uat", "billed": False}],
        "unknown": [],
    }
    assert app_core_data.region_split(env=["uat"])["selected"] == ["2222", "3333", "9002"]
    assert app_core_data.region_split(["not-a-real-workspace"]) == {
        "filtered": True, "selected": [], "outside": [], "unknown": [],
    }
    assert app_core_data.region_split(attributes={"cost_center": ["engineering"]}) == {
        "filtered": True, "selected": ["1111"], "outside": [], "unknown": [],
    }
    assert app_core_data.region_split(attributes={"not_a_real_key": ["x"]})["filtered"] is False


# ------------------------------------------------------------------------------------------
# region_split / region_gap on a crafted multi-region dim
# ------------------------------------------------------------------------------------------
def _tiny_dim_db(path: Path, rows: list[tuple]) -> Path:
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE SCHEMA dims")
        con.execute(
            "CREATE TABLE dims.dim_workspace (workspace_id VARCHAR, name VARCHAR, env VARCHAR, "
            "in_snapshot_region BOOLEAN, billed_in_snapshot BOOLEAN)"
        )
        con.executemany("INSERT INTO dims.dim_workspace VALUES (?, ?, ?, ?, ?)", rows)
    finally:
        con.close()
    return path


def test_region_split_and_gap_on_a_multi_region_dim(tmp_path, monkeypatch):
    db = _tiny_dim_db(tmp_path / "multi_region.duckdb", [
        ("a1", "alpha", "prod", True, True),
        ("b2", "beta", "prod", False, True),
        ("c3", "gamma", "dev", False, False),
    ])
    monkeypatch.setenv("AUDIT_DB", str(db))

    split = app_core_data.region_split()
    assert split == {
        "filtered": False, "selected": ["a1", "b2", "c3"],
        "outside": [
            {"workspace_id": "b2", "name": "beta", "billed": True},
            {"workspace_id": "c3", "name": "gamma", "billed": False},
        ],
        "unknown": [],
    }
    assert service.outside_only(split) is False

    assert service.region_gap() == [
        {"workspace_id": "b2", "name": "beta", "billed": True, "reason": BILLED_REASON},
    ]
    assert service.outside_only(app_core_data.region_split(["b2", "c3"])) is True
    assert service.outside_only(app_core_data.region_split(["a1", "b2"])) is False
    assert service.outside_only(app_core_data.region_split(env=["dev"])) is True
    assert app_core_data.region_split(env=["prod"])["selected"] == ["a1", "b2"]
    assert service.region_gap(["a1", "c3"]) == []


def test_region_unknown_when_the_snapshot_has_no_regional_rows(tmp_path, monkeypatch):
    db = _tiny_dim_db(tmp_path / "no_regional.duckdb", [
        ("a1", "alpha", "prod", None, True),
        ("b2", "beta", "dev", None, False),
    ])
    monkeypatch.setenv("AUDIT_DB", str(db))

    split = app_core_data.region_split(["a1", "b2"])
    assert split == {
        "filtered": True, "selected": ["a1", "b2"], "outside": [], "unknown": ["a1", "b2"],
    }
    assert service.outside_only(split) is False
    assert service.region_gap() == []

    r = client.get("/api/coverage")
    assert r.json()["region"]["unknown"] is True
    assert r.json()["region"]["outside"] == []

    rows = {w["workspace_id"]: w for w in client.get("/api/workspaces").json()}
    assert rows["a1"]["region_status"] == "unknown"
    assert rows["a1"]["region_reason"] == UNKNOWN_REASON


# ------------------------------------------------------------------------------------------
# Pinned words
# ------------------------------------------------------------------------------------------
def test_region_words_are_pinned():
    assert service.OUTSIDE_REGION_LABEL == "outside the snapshot's region"
    assert service.region_reason(True) == BILLED_REASON
    assert service.region_reason(False) == UNBILLED_REASON
    assert service.REGION_UNKNOWN_REASON == UNKNOWN_REASON
    assert service.workspace_region(True, True) == ("in_region", None)
    assert service.workspace_region(False, True) == ("outside_region", BILLED_REASON)
    assert service.workspace_region(False, False) == ("outside_region", UNBILLED_REASON)
    assert service.workspace_region(None, True) == ("unknown", UNKNOWN_REASON)


def test_outside_only_needs_a_filter_a_match_and_every_match_outside():
    out = [{"workspace_id": "9002", "name": "dd-uat", "billed": False}]
    assert service.outside_only(None) is False
    assert service.outside_only(
        {"filtered": False, "selected": ["9002"], "outside": out, "unknown": []}
    ) is False
    assert service.outside_only(
        {"filtered": True, "selected": [], "outside": [], "unknown": []}
    ) is False
    assert service.outside_only(
        {"filtered": True, "selected": ["9002"], "outside": out, "unknown": []}
    ) is True
    assert service.outside_only(
        {"filtered": True, "selected": ["1111", "9002"], "outside": out, "unknown": []}
    ) is False
    assert service.gap_entries(out) == [ENTRY_9002]
    assert service.billed_gap(
        {"filtered": True, "selected": ["9002"], "outside": out, "unknown": []}
    ) == []
    assert service.billed_gap(None) is None


def test_meta_and_coverage_carry_the_snapshot_metastore():
    assert hashlib.sha256(FIXTURE_METASTORE_ID.encode("utf-8")).hexdigest()[:16] == "223290a343c0aa60"
    assert client.get("/api/meta").json()["metastore"] == FIXTURE_METASTORE
    assert client.get("/api/coverage").json()["region"] == {
        "metastore": FIXTURE_METASTORE,
        "outside": [ENTRY_9002, ENTRY_CB_WS1, *CBA_GAP_ENTRIES, ENTRY_UW_CRIT, ENTRY_UW_OK, ENTRY_UW_WARN],
        "unknown": False,
        "unknown_reason": UNKNOWN_REASON,
        # F7: tests/conftest.py's synthesised manifest carries this same fixed roster (not a
        # simulated `--workspace` filter) -- the coverage endpoint passes the manifest's own
        # top-level workspace_ids straight through, unchanged.
        "workspace_ids": ["1111", "2222", "3333", "9001", "9002"],
    }


def test_workspaces_endpoint_marks_the_workspace_outside_the_region():
    rows = {w["workspace_id"]: w for w in client.get("/api/workspaces").json()}
    assert (rows["9002"]["region_status"], rows["9002"]["region_reason"]) == (
        "outside_region", UNBILLED_REASON,
    )
    assert (rows["cb_ws1"]["region_status"], rows["cb_ws1"]["region_reason"]) == (
        "outside_region", BILLED_REASON,
    )
    for ws in ("1111", "2222", "3333", "9001", "iw_ws8", "wu_ws9"):
        assert (rows[ws]["region_status"], rows[ws]["region_reason"]) == ("in_region", None)
    assert "in_snapshot_region" not in rows["1111"]
    assert "billed_in_snapshot" not in rows["1111"]


# ------------------------------------------------------------------------------------------
# Outcomes on the fixture db
# ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("query_id", [REGIONAL_WS_QUERY_ID, REGIONAL_WS_WINDOWED_QUERY_ID])
def test_regional_finding_for_outside_workspaces_only_is_not_assessed(query_id):
    eff = service.window_for(service.get_spec(query_id), 30)
    assert app_core_data.count_finding(query_id, eff) > 0
    assert app_core_data.count_finding(query_id, eff, workspace_ids=[OUTSIDE_WS]) == 0

    r = client.get(f"/api/finding/{query_id}", params={"window": 30, "workspace_ids": OUTSIDE_WS})
    body = r.json()
    assert body["outcome"] == "not_assessed"
    assert body["status_info"]["outside_region"] == {
        "label": "outside the snapshot's region", "workspaces": [ENTRY_9002],
    }
    # P4-T-IDX: scope gained "tag" (None here -- no tag_key/tag_value was given).
    assert body["scope"] == {
        "has_workspace_id": True, "regional": True, "region_gap": [], "tag": None,
    }
    assert "rows" not in body


def test_account_wide_finding_keeps_its_empty_filter_outcome():
    r = client.get(f"/api/finding/{GLOBAL_WS_QUERY_ID}", params={"window": 30, "workspace_ids": OUTSIDE_WS})
    body = r.json()
    assert body["outcome"] == "ok_empty_filters"
    assert body["scope"] == {
        "has_workspace_id": True, "regional": False, "region_gap": None, "tag": None,
    }


def test_metastore_wide_finding_is_not_filtered_and_says_so():
    unfiltered = client.get(
        f"/api/finding/{REGIONAL_NO_WS_QUERY_ID}", params={"window": 30, "limit": 5000}
    ).json()
    filtered = client.get(
        f"/api/finding/{REGIONAL_NO_WS_QUERY_ID}",
        params={"window": 30, "limit": 5000, "workspace_ids": OUTSIDE_WS},
    ).json()
    assert unfiltered["outcome"] == "ok_rows"
    assert filtered["outcome"] == "ok_rows"
    assert filtered["rows_total"] == unfiltered["rows_total"] > 0
    assert filtered["scope"] == {
        "has_workspace_id": False, "regional": True, "region_gap": None, "tag": None,
    }


def test_unfiltered_regional_finding_shows_cb_ws1s_billed_gap_on_the_fixture():
    # cb_ws1 is a real billed-outside-region workspace in the base fixture (see module docstring),
    # so an unfiltered regional finding's region_gap now names it -- but this never forces the
    # finding itself out of ok_rows (only a selection that is ENTIRELY outside the region does).
    body = client.get(f"/api/finding/{REGIONAL_WS_QUERY_ID}", params={"window": 30}).json()
    assert body["outcome"] == "ok_rows"
    assert body["scope"] == {
        "has_workspace_id": True, "regional": True,
        "region_gap": [ENTRY_CB_WS1, *CBA_GAP_ENTRIES, ENTRY_UW_CRIT, ENTRY_UW_OK, ENTRY_UW_WARN], "tag": None,
       
    }


def test_findings_list_outside_only_selection():
    body = client.get("/api/findings", params={"window": 30, "workspace_ids": OUTSIDE_WS}).json()
    assert body["region_gap"] == []
    rows = body["findings"]
    for r in rows:
        assert r["has_workspace_id"] == app_core_data.workspace_filterable(app_core_data.finding_columns(r["query_id"]))
        assert r["regional"] is app_core_data.is_regional(r["query_id"])
    regional_ws = [r for r in rows if r["regional"] and r["has_workspace_id"]]
    account_wide = [r for r in rows if not r["regional"]]
    no_ws = [r for r in rows if r["has_workspace_id"] is False]
    assert regional_ws and account_wide and no_ws
    for r in regional_ws:
        assert r["outcome"] == "not_assessed"
        assert r["not_assessed_reason"] == "outside_region"
    for r in account_wide:
        assert r["outcome"] in ("ok_rows", "ok_empty_filters")
        assert r["not_assessed_reason"] is None
    for r in no_ws:
        assert r["outcome"] == "ok_rows"
    by_id = {r["query_id"]: r for r in rows}
    assert by_id[GLOBAL_WS_QUERY_ID]["outcome"] == "ok_empty_filters"
    assert by_id[REGIONAL_WS_QUERY_ID]["outcome"] == "not_assessed"


def test_findings_list_unfiltered_has_no_outside_reason():
    # cb_ws1's billed gap (see module docstring) surfaces at the top level, but an unfiltered
    # selection is never entirely outside the region, so no individual finding is forced
    # not_assessed over it.
    body = client.get("/api/findings", params={"window": 30}).json()
    assert body["region_gap"] == [ENTRY_CB_WS1, *CBA_GAP_ENTRIES, ENTRY_UW_CRIT, ENTRY_UW_OK, ENTRY_UW_WARN]
    for r in body["findings"]:
        assert r["not_assessed_reason"] is None


def test_findings_list_names_a_source_not_ok(monkeypatch):
    monkeypatch.setenv(
        "AUDIT_SNAPSHOT_MANIFEST", str(ROOT / "tests" / "scenarios" / "manifest_blocking.json")
    )
    body = client.get("/api/findings", params={"window": 30}).json()
    by_id = {r["query_id"]: r for r in body["findings"]}
    assert by_id[GLOBAL_WS_QUERY_ID]["outcome"] == "not_assessed"
    assert by_id[GLOBAL_WS_QUERY_ID]["not_assessed_reason"] == "source_not_ok"
    assert by_id["access_admin_role_change_events"]["not_assessed_reason"] is None
    for r in body["findings"]:
        if r["outcome"] == "not_assessed":
            assert r["not_assessed_reason"] == "source_not_ok"


def test_findings_list_names_a_failed_model(tmp_path, monkeypatch):
    src = Path(os.environ["AUDIT_RUN_RESULTS"])
    data = json.loads(src.read_text(encoding="utf-8"))
    target_uid = "model.databricks_audit.f_lakeflow_failed_runs"
    found = False
    for result in data["results"]:
        if result.get("unique_id") == target_uid:
            result["status"] = "error"
            result["message"] = 'Binder Error: column "bogus" does not exist'
            found = True
    assert found, f"{target_uid} missing from the session's synthesised run_results.json"
    dest = tmp_path / "run_results_error.json"
    dest.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("AUDIT_RUN_RESULTS", str(dest))

    body = client.get("/api/findings", params={"window": 30}).json()
    by_id = {r["query_id"]: r for r in body["findings"]}
    assert by_id["lakeflow_failed_runs"]["outcome"] == "not_assessed"
    assert by_id["lakeflow_failed_runs"]["not_assessed_reason"] == "model_failed"


# ------------------------------------------------------------------------------------------
# The billed-outside case: a COPY of the built db with 3333 marked outside its region.
# ------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def billed_outside_db(tmp_path_factory):
    src = app_core_data._db_path()
    dest = tmp_path_factory.mktemp("scope_disclosure_db") / "billed_outside.duckdb"
    shutil.copy2(src, dest)
    con = duckdb.connect(str(dest))
    try:
        con.execute("UPDATE dims.dim_workspace SET in_snapshot_region = FALSE WHERE workspace_id = '3333'")
    finally:
        con.close()
    return dest


@pytest.fixture
def billed_outside_env(billed_outside_db, monkeypatch):
    monkeypatch.setenv("AUDIT_DB", str(billed_outside_db))


def test_billed_outside_workspace_is_the_gap(billed_outside_env):
    # cb_ws1 (and uw_crit/uw_ok/uw_warn) are already, independently, a billed-outside gap on the
    # base fixture (module docstring) -- billed_outside_env additionally marks 3333 outside, so it
    # now joins them too, sorted by workspace_id.
    assert client.get("/api/findings", params={"window": 30}).json()["region_gap"] == [
        ENTRY_3333, ENTRY_CB_WS1, *CBA_GAP_ENTRIES, ENTRY_UW_CRIT, ENTRY_UW_OK, ENTRY_UW_WARN,
    ]

    body = client.get(f"/api/finding/{REGIONAL_WS_QUERY_ID}", params={"window": 30}).json()
    assert body["outcome"] == "ok_rows"
    assert body["scope"]["region_gap"] == [
        ENTRY_3333, ENTRY_CB_WS1, *CBA_GAP_ENTRIES, ENTRY_UW_CRIT, ENTRY_UW_OK, ENTRY_UW_WARN,
    ]

    body = client.get(
        f"/api/finding/{REGIONAL_WS_QUERY_ID}",
        params={"window": 30, "workspace_ids": ["1111", "3333"]},
    ).json()
    assert body["outcome"] == "ok_rows"
    assert body["scope"]["region_gap"] == [ENTRY_3333]
    assert {r["workspace_id"] for r in body["rows"]} == {"1111"}

    assert client.get("/api/coverage").json()["region"]["outside"] == [
        ENTRY_3333, ENTRY_9002, ENTRY_CB_WS1, *CBA_GAP_ENTRIES, ENTRY_UW_CRIT, ENTRY_UW_OK, ENTRY_UW_WARN,
    ]

    rows = {w["workspace_id"]: w for w in client.get("/api/workspaces").json()}
    assert (rows["3333"]["region_status"], rows["3333"]["region_reason"]) == ("outside_region", BILLED_REASON)


def test_billed_outside_only_selection(billed_outside_env):
    body = client.get(
        f"/api/finding/{REGIONAL_WS_QUERY_ID}", params={"window": 30, "workspace_ids": "3333"}
    ).json()
    assert body["outcome"] == "not_assessed"
    assert body["status_info"]["outside_region"]["workspaces"] == [ENTRY_3333]
    assert body["scope"]["region_gap"] == [ENTRY_3333]

    body = client.get(
        f"/api/finding/{REGIONAL_WS_3333_QUERY_ID}", params={"window": 30, "workspace_ids": "3333"}
    ).json()
    assert body["outcome"] == "ok_rows"
    assert body["rows_total"] >= 1
    assert all(r["workspace_id"] == "3333" for r in body["rows"])
    assert body["scope"]["region_gap"] == [ENTRY_3333]

    findings = client.get(
        "/api/findings", params={"window": 30, "workspace_ids": "3333"}
    ).json()["findings"]
    by_id = {r["query_id"]: r for r in findings}
    assert by_id[REGIONAL_WS_QUERY_ID]["not_assessed_reason"] == "outside_region"
    assert by_id[REGIONAL_WS_3333_QUERY_ID]["outcome"] == "ok_rows"
    for r in findings:
        if r["regional"] and r["has_workspace_id"]:
            assert r["outcome"] not in ("ok_empty_filters", "ok_empty_window")


# ------------------------------------------------------------------------------------------
# The single-region case: a COPY of the built db with cb_ws1 marked in-region, so every billed
# workspace is regional and only the unbilled 9002 is outside (the common single-region account,
# and the base fixture's shape before P3-CHARGEBACK added cb_ws1).
# ------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def single_region_db(tmp_path_factory):
    src = app_core_data._db_path()
    dest = tmp_path_factory.mktemp("scope_disclosure_single_region") / "single_region.duckdb"
    shutil.copy2(src, dest)
    con = duckdb.connect(str(dest))
    try:
        # cb_ws1, uw_crit/uw_ok/uw_warn (billing.py SEC N) and 7 of chargeback_a.py's 9 dedicated
        # workspaces are the fixture's only billed-outside workspaces -- flipping all of them
        # in-region simulates the single-region account this test is named for (every billed
        # workspace regional, only the unbilled 9002 outside).
        con.execute(
            "UPDATE dims.dim_workspace SET in_snapshot_region = TRUE "
            "WHERE workspace_id IN ('cb_ws1', 'uw_crit', 'uw_ok', 'uw_warn', 'cba_ws_crit', "
            "'cba_ws_na_cur', 'cba_ws_ok', 'cba_ws_sku', 'cba_ws_svc', 'cba_ws_warn', 'cba_ws_win', "
            "'gn_ws_dev', 'gn_ws_prod')"
        )
    finally:
        con.close()
    return dest


@pytest.fixture
def single_region_env(single_region_db, monkeypatch):
    monkeypatch.setenv("AUDIT_DB", str(single_region_db))


def test_unfiltered_regional_finding_has_no_billed_gap_on_a_single_region_fixture(single_region_env):
    # An unbilled outside workspace (9002) never creates a gap: ok_rows, region_gap == [].
    body = client.get(f"/api/finding/{REGIONAL_WS_QUERY_ID}", params={"window": 30}).json()
    assert body["outcome"] == "ok_rows"
    assert body["scope"] == {
        "has_workspace_id": True, "regional": True, "region_gap": [], "tag": None,
    }
    listing = client.get("/api/findings", params={"window": 30}).json()
    assert listing["region_gap"] == []
    for r in listing["findings"]:
        assert r["not_assessed_reason"] is None


# ------------------------------------------------------------------------------------------
# Part B: scope.tsx borrows the API's own label -- one place words live.
# ------------------------------------------------------------------------------------------
def test_scope_jsx_uses_the_api_label():
    scope_jsx = (ROOT / "web" / "src" / "components" / "scope.tsx").read_text(encoding="utf-8")
    assert f'const OUTSIDE_REGION_LABEL = "{service.OUTSIDE_REGION_LABEL}";' in scope_jsx
    overview_tile_jsx = (ROOT / "web" / "src" / "components" / "overview_tile.tsx").read_text(encoding="utf-8")
    assert '"outside_region"' in overview_tile_jsx
