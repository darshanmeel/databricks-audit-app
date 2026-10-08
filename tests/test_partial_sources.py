"""tests/test_partial_sources.py -- P2-PARTIAL.

Blocking sources used to be all-or-nothing (app/core/data.finding_status): ANY source of a
query not in state "ok" -- a name-lookup table this app's own SQL joins in only to attach a
display column, or a table the snapshot exported only PART of -- greyed out the WHOLE finding as
NOT_ASSESSED, even when the finding's real data was untouched. This module locks in the fix:

  * a name-lookup source (app_core_data._optional_name_sources, e.g. system.lakeflow.jobs feeding
    lakeflow_job_reliability's own job_name column) that is not "ok" lands in finding_status()'s
    "degraded_sources", never "blocking_sources" -- the finding still computes; the name column
    just falls back to the bare id (dbt's own LEFT JOIN already leaves it NULL there);
  * a source the manifest marks "partial" (some day/workspace slices failed, most did not) lands
    in "partial_sources", also never blocking -- the finding is judged on whatever DID export;
  * a source that is neither of the above and not "ok" still blocks -- NOT_ASSESSED, same as
    before this item (the "truly required source missing" case).

Runs against the same session fixture as tests/test_api.py (tests/conftest.py): AUDIT_DB is the
real built test db, so a "degraded"/"partial" scenario below still reaches a real outcome, not
just a status_info shape check.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.app import app  # noqa: E402
from app.core import data as app_core_data  # noqa: E402
from app.core import registry  # noqa: E402

client = TestClient(app)

# A query with a genuine primary source (job_run_timeline) PLUS system.lakeflow.jobs joined
# in-SQL for job_name alone (its own caveats: "job_name comes from system.lakeflow.jobs ... and
# is NULL for one-time SUBMIT_RUN/WORKFLOW_RUN executions, which never write to that table") --
# jobs is optional here.
OPTIONAL_SOURCE_QUERY_ID = "lakeflow_job_reliability"
OPTIONAL_SOURCE_KEY = "system.lakeflow.jobs"
OPTIONAL_SOURCE_MANIFEST_KEY = "lakeflow__jobs"

# A query whose ONLY source is system.lakeflow.jobs -- nothing else to compute the finding from,
# so the very same table must stay REQUIRED here even though it is one of the five name-lookup
# tables _optional_name_sources knows about.
SOLE_SOURCE_QUERY_ID = "lakeflow_health_rule_coverage"

# A query whose sole source is billing usage -- genuinely required, never a name lookup.
REQUIRED_SOURCE_QUERY_ID = "cost_by_job"
REQUIRED_SOURCE_KEY = "system.billing.usage"
REQUIRED_SOURCE_MANIFEST_KEY = "billing__usage"


def _load_manifest() -> dict:
    path = os.environ["AUDIT_SNAPSHOT_MANIFEST"]
    return json.loads(Path(path).read_text(encoding="utf-8"))


@pytest.fixture
def manifest_env(tmp_path, monkeypatch):
    """A function(mutator) -> None that writes a mutated copy of the session's own manifest.json
    (real "ok" state for every table it actually built, tests/conftest.py's own synthesis) and
    points AUDIT_SNAPSHOT_MANIFEST at it for the duration of one test -- `mutator(tables)` edits
    the dict of "<schema>__<table>" -> entry in place, same key spelling the real exporter uses
    (DEC-54)."""
    def _apply(mutator) -> None:
        data = _load_manifest()
        mutator(data["tables"])
        dest = tmp_path / "manifest_scenario.json"
        dest.write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setenv("AUDIT_SNAPSHOT_MANIFEST", str(dest))
    return _apply


# -------------------------------------------------------------------------------------------
# _optional_name_sources -- pure classification, no manifest/DB involved.
# -------------------------------------------------------------------------------------------


def test_name_lookup_source_recognised_when_another_source_exists():
    spec = registry.by_id(OPTIONAL_SOURCE_QUERY_ID)
    assert app_core_data._optional_name_sources(spec) == {OPTIONAL_SOURCE_KEY: "job_name"}


def test_sole_source_name_lookup_table_stays_required():
    spec = registry.by_id(SOLE_SOURCE_QUERY_ID)
    # jobs is this query's ONLY source -- classified as optional it would leave nothing to
    # compute the finding from, so it must not be waived.
    assert {f"system.{s}.{t}" for s, t in spec.sources} == {OPTIONAL_SOURCE_KEY}
    assert app_core_data._optional_name_sources(spec) == {}


def test_required_source_never_name_lookup():
    spec = registry.by_id(REQUIRED_SOURCE_QUERY_ID)
    assert REQUIRED_SOURCE_KEY not in app_core_data._optional_name_sources(spec)


# -------------------------------------------------------------------------------------------
# finding_status -- degraded (name-lookup) vs blocking (required) vs partial (some slices failed)
# -------------------------------------------------------------------------------------------


def test_bad_name_lookup_source_degrades_not_blocks(manifest_env):
    def mutate(tables):
        tables[OPTIONAL_SOURCE_MANIFEST_KEY]["state"] = "not_assessed"
        tables[OPTIONAL_SOURCE_MANIFEST_KEY]["reason"] = "schema_not_enabled"
        tables[OPTIONAL_SOURCE_MANIFEST_KEY]["message"] = "system.lakeflow not enabled (test)."
    manifest_env(mutate)

    info = app_core_data.finding_status(OPTIONAL_SOURCE_QUERY_ID)
    assert info["blocking_sources"] == []
    assert info["partial_sources"] == []
    assert info["degraded_sources"] == [
        {
            "source": OPTIONAL_SOURCE_KEY,
            "state": "not_assessed",
            "reason": "schema_not_enabled",
            "message": "system.lakeflow not enabled (test).",
            "degrades": "job_name",
        }
    ]


def test_bad_required_source_still_blocks(manifest_env):
    def mutate(tables):
        tables[REQUIRED_SOURCE_MANIFEST_KEY]["state"] = "error"
        tables[REQUIRED_SOURCE_MANIFEST_KEY]["reason"] = "permission_denied"
        tables[REQUIRED_SOURCE_MANIFEST_KEY]["message"] = "no SELECT grant (test)."
    manifest_env(mutate)

    info = app_core_data.finding_status(REQUIRED_SOURCE_QUERY_ID)
    assert info["degraded_sources"] == []
    assert info["partial_sources"] == []
    assert len(info["blocking_sources"]) == 1
    assert info["blocking_sources"][0] == {
        "source": REQUIRED_SOURCE_KEY,
        "state": "error",
        "reason": "permission_denied",
        "message": "no SELECT grant (test).",
    }


def test_partial_source_never_blocks_and_names_missing_days(manifest_env):
    def mutate(tables):
        tables[REQUIRED_SOURCE_MANIFEST_KEY]["state"] = "partial"
        tables[REQUIRED_SOURCE_MANIFEST_KEY]["reason"] = "too_much_data"
        tables[REQUIRED_SOURCE_MANIFEST_KEY]["message"] = "2 slice(s) failed; see slices_failed"
        tables[REQUIRED_SOURCE_MANIFEST_KEY]["slices_failed"] = [
            {"from": "2026-09-12", "to": "2026-09-13", "workspace_id": None,
             "reason": "too_much_data", "error_class": "too_much_data",
             "message": "too much data (test)"},
            {"from": "2026-09-10", "to": "2026-09-11", "workspace_id": None,
             "reason": "too_much_data", "error_class": "too_much_data",
             "message": "too much data (test)"},
        ]
    manifest_env(mutate)

    info = app_core_data.finding_status(REQUIRED_SOURCE_QUERY_ID)
    assert info["blocking_sources"] == []
    assert info["degraded_sources"] == []
    assert len(info["partial_sources"]) == 1
    partial = info["partial_sources"][0]
    assert partial["source"] == REQUIRED_SOURCE_KEY
    # sorted + deduplicated, independent of the slices_failed order in the manifest
    assert partial["missing_days"] == ["2026-09-10", "2026-09-12"]
    assert len(partial["slices_failed"]) == 2


def test_partial_source_multi_day_slice_names_every_missing_day(manifest_env):
    # tools/snapshot.py records a failed slice as a half-open [from, to) range -- a bisected
    # retry (timeout/unknown, not size) can fail for MORE than one day at a time. Review fix:
    # missing_days must expand the whole range, not just report the "from" date (which would
    # undercount a 3-day gap as 1 day).
    def mutate(tables):
        tables[REQUIRED_SOURCE_MANIFEST_KEY]["state"] = "partial"
        tables[REQUIRED_SOURCE_MANIFEST_KEY]["reason"] = "unknown"
        tables[REQUIRED_SOURCE_MANIFEST_KEY]["message"] = "1 slice(s) failed; see slices_failed"
        tables[REQUIRED_SOURCE_MANIFEST_KEY]["slices_failed"] = [
            {"from": "2026-08-25", "to": "2026-08-28", "workspace_id": None,
             "reason": "unknown", "error_class": "unknown", "message": "timeout (test)"},
        ]
    manifest_env(mutate)

    info = app_core_data.finding_status(REQUIRED_SOURCE_QUERY_ID)
    assert info["blocking_sources"] == []
    assert len(info["partial_sources"]) == 1
    assert info["partial_sources"][0]["missing_days"] == [
        "2026-08-25", "2026-08-26", "2026-08-27",
    ]


def test_no_manifest_reports_nothing_blocking_degraded_or_partial(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIT_SNAPSHOT_MANIFEST", str(tmp_path / "does-not-exist.json"))
    info = app_core_data.finding_status(OPTIONAL_SOURCE_QUERY_ID)
    assert info["manifest_available"] is False
    assert info["blocking_sources"] == []
    assert info["degraded_sources"] == []
    assert info["partial_sources"] == []


# -------------------------------------------------------------------------------------------
# API level -- a degraded/partial finding reaches a real outcome other than not_assessed, and
# carries the same info in its response body (GET /api/finding, GET /api/findings) for the front
# end to render as a note instead of a grey card.
# -------------------------------------------------------------------------------------------


def test_finding_endpoint_not_blocked_by_degraded_source(manifest_env):
    def mutate(tables):
        tables[OPTIONAL_SOURCE_MANIFEST_KEY]["state"] = "error"
        tables[OPTIONAL_SOURCE_MANIFEST_KEY]["reason"] = "permission_denied"
        tables[OPTIONAL_SOURCE_MANIFEST_KEY]["message"] = "no SELECT grant (test)."
    manifest_env(mutate)

    resp = client.get(f"/api/finding/{OPTIONAL_SOURCE_QUERY_ID}", params={"window": 30})
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] != "not_assessed"
    assert body["degraded_sources"] == [
        {
            "source": OPTIONAL_SOURCE_KEY,
            "state": "error",
            "reason": "permission_denied",
            "message": "no SELECT grant (test).",
            "degrades": "job_name",
        }
    ]
    assert body["partial_sources"] == []


def test_finding_endpoint_still_not_assessed_for_required_source(manifest_env):
    def mutate(tables):
        tables[REQUIRED_SOURCE_MANIFEST_KEY]["state"] = "error"
        tables[REQUIRED_SOURCE_MANIFEST_KEY]["reason"] = "permission_denied"
        tables[REQUIRED_SOURCE_MANIFEST_KEY]["message"] = "no SELECT grant (test)."
    manifest_env(mutate)

    resp = client.get(f"/api/finding/{REQUIRED_SOURCE_QUERY_ID}", params={"window": 30})
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "not_assessed"
    assert body["status_info"]["blocking_sources"][0]["source"] == REQUIRED_SOURCE_KEY
    assert body["status_info"]["blocking_sources"][0]["reason"] == "permission_denied"


def test_finding_endpoint_partial_source_carries_note(manifest_env):
    def mutate(tables):
        tables[REQUIRED_SOURCE_MANIFEST_KEY]["state"] = "partial"
        tables[REQUIRED_SOURCE_MANIFEST_KEY]["slices_failed"] = [
            {"from": "2026-09-10", "to": "2026-09-11", "workspace_id": None,
             "reason": "too_much_data", "error_class": "too_much_data",
             "message": "too much data (test)"},
        ]
    manifest_env(mutate)

    resp = client.get(f"/api/finding/{REQUIRED_SOURCE_QUERY_ID}", params={"window": 30})
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] != "not_assessed"
    assert len(body["partial_sources"]) == 1
    assert body["partial_sources"][0]["source"] == REQUIRED_SOURCE_KEY
    assert body["partial_sources"][0]["missing_days"] == ["2026-09-10"]


def test_findings_list_flags_partial_without_greying_out(manifest_env):
    def mutate(tables):
        tables[OPTIONAL_SOURCE_MANIFEST_KEY]["state"] = "error"
        tables[OPTIONAL_SOURCE_MANIFEST_KEY]["reason"] = "permission_denied"
    manifest_env(mutate)

    resp = client.get("/api/findings", params={"window": 30})
    assert resp.status_code == 200
    rows = {r["query_id"]: r for r in resp.json()["findings"]}
    row = rows[OPTIONAL_SOURCE_QUERY_ID]
    assert row["outcome"] != "not_assessed"
    assert row["not_assessed_reason"] is None
    assert row["partial"] is True


def test_findings_list_required_source_still_not_assessed(manifest_env):
    def mutate(tables):
        tables[REQUIRED_SOURCE_MANIFEST_KEY]["state"] = "error"
        tables[REQUIRED_SOURCE_MANIFEST_KEY]["reason"] = "permission_denied"
    manifest_env(mutate)

    resp = client.get("/api/findings", params={"window": 30})
    assert resp.status_code == 200
    rows = {r["query_id"]: r for r in resp.json()["findings"]}
    row = rows[REQUIRED_SOURCE_QUERY_ID]
    assert row["outcome"] == "not_assessed"
    assert row["not_assessed_reason"] == "source_not_ok"
    assert row["partial"] is False
