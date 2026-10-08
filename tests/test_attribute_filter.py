"""tests/test_attribute_filter.py -- T-63 (DEC-60): the API-layer half of the workspace
tag-attribute filter (cost_center/team/business_unit/domain).

Placement note: the task's own owns-list names `tests/test_api/test_attribute_filter.py`, but
`tests/test_api.py` already exists as a FILE (tasks/T-57-web-vertical-slice.md) -- a package
directory of the same name cannot coexist with it. Per the task's own fallback instruction this
file lives at `tests/test_attribute_filter.py` instead.

FastAPI TestClient coverage, same fixture session as tests/test_api.py (tests/conftest.py's
autouse `_app_env`, AUDIT_DB=tests/db_audit_test.duckdb built by
`python tests/fixtures/build_fixtures.py && python tools/dbt_run.py build --target test`).
Proves, against tests/fixtures/billing.py's "SEC L (T-63)" fixture rows:
  - GET /api/meta reports which canonical keys are actually populated (DEC-60 rule 5).
  - GET /api/workspaces carries the new <key>/<key>_share/<key>_reason columns.
  - a cost_center filter on a finding that carries workspace_id returns exactly that workspace's
    rows (DEC-60 rule 1).
  - the same filter does not reduce a finding with no workspace_id column at all (DEC-60 rule 6,
    the four-outcome rule).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from app.api.app import app  # noqa: E402
import billing as bl  # noqa: E402

client = TestClient(app)

# A finding that carries workspace_id, with real rows on WS_PROD (cost_by_job -- also
# tests/test_api.py's own PLAIN_QUERY_ID, chosen for the same reason: no account-level row to
# confuse the comparison).
WS_SCOPED_QUERY_ID = "cost_by_job"
# A price list has no workspace column: a cost-centre filter must not zero it out (DEC-60 rule 6).
NO_WORKSPACE_ID_QUERY_ID = "pricing_list_prices_raw"


# -------------------------------------------------------------------------------------------
# GET /api/meta -- DEC-60 rule 5's populated-keys report.
# -------------------------------------------------------------------------------------------


def test_meta_reports_populated_and_unpopulated_attribute_keys():
    meta = client.get("/api/meta").json()
    populated = set(meta["attribute_keys_populated"])
    # cost_center: SEC L's own fixture rows (WS_PROD 'engineering', WS_DEV 'not_tagged', under the coverage floor).
    assert "cost_center" in populated
    # business_unit / domain: no fixture anywhere ever tags either -- "no account is known to
    # carry these tags" (DEC-60 rule 5) must build cleanly and report them as NOT populated, so
    # the UI hides their pickers entirely rather than showing an always-empty dropdown.
    assert "business_unit" not in populated
    assert "domain" not in populated


def test_meta_lists_top_tags_with_names_in_file_order():
    from app.core import config as app_config

    meta = client.get("/api/meta").json()
    assert meta["top_tags"] == [{"key": k, "label": v} for k, v in app_config.load_top_tags().items()]
    assert meta["top_tags"][0] == {"key": "cost_center", "label": "Cost center"}
    assert len(meta["attribute_keys_populated"]) <= app_config.TOP_TAGS_SHOWN


# -------------------------------------------------------------------------------------------
# GET /api/workspaces -- the new columns.
# -------------------------------------------------------------------------------------------


def test_workspaces_endpoint_carries_attribute_columns():
    rows = {w["workspace_id"]: w for w in client.get("/api/workspaces").json()}
    for key in ("cost_center", "cost_center_share", "cost_center_reason"):
        assert key in rows[bl.WS_PROD]

    assert rows[bl.WS_PROD]["cost_center"] == "engineering"
    assert rows[bl.WS_PROD]["cost_center_share"] > 0.9
    # 55/45 split, but tagged on under half its DBUs: the coverage floor reads it not_tagged.
    assert rows[bl.WS_DEV]["cost_center"] == "not_tagged"
    assert rows[bl.WS_UAT]["cost_center"] == "not_tagged"


# -------------------------------------------------------------------------------------------
# DEC-60 rule 1 -- a cost_center filter on a finding that carries workspace_id.
# -------------------------------------------------------------------------------------------


def test_attribute_filter_matches_the_explicit_workspace_filter():
    """?cost_center=engineering resolves to exactly WS_PROD (the only 'engineering' workspace)
    -- so it must return the identical row set an explicit workspace_ids=[WS_PROD] filter does,
    on a finding whose grain has nothing to do with cost_center at all."""
    by_attribute = client.get(
        f"/api/finding/{WS_SCOPED_QUERY_ID}",
        params={"window": 30, "cost_center": "engineering", "limit": 5000},
    ).json()
    by_workspace = client.get(
        f"/api/finding/{WS_SCOPED_QUERY_ID}",
        params={"window": 30, "workspace_ids": [bl.WS_PROD], "limit": 5000},
    ).json()

    assert by_attribute["outcome"] == by_workspace["outcome"] == "ok_rows"
    assert by_attribute["rows_total"] == by_workspace["rows_total"] > 0
    ids_by_attribute = {r["workspace_id"] for r in by_attribute["rows"]}
    ids_by_workspace = {r["workspace_id"] for r in by_workspace["rows"]}
    assert ids_by_attribute == ids_by_workspace == {bl.WS_PROD}


def test_attribute_filter_excludes_non_matching_workspaces():
    """The same filter, scoped to workspaces that are NOT 'engineering', must exclude WS_PROD's
    rows entirely -- proving the filter is a real join, not a no-op that happens to pass the
    previous test by coincidence."""
    r = client.get(
        f"/api/finding/{WS_SCOPED_QUERY_ID}",
        params={
            "window": 30,
            "workspace_ids": [bl.WS_DEV, bl.WS_UAT],
            "cost_center": "engineering",
            "limit": 5000,
        },
    ).json()
    assert r["outcome"] == "ok_empty_filters"
    assert r["rows_in_window"] > 0  # the window has rows -- the FILTER excludes all of them


# -------------------------------------------------------------------------------------------
# DEC-60 rule 6 -- the four-outcome rule is untouched: a finding with no workspace_id is
# unaffected by this filter.
# -------------------------------------------------------------------------------------------


def test_attribute_filter_does_not_reduce_a_finding_with_no_workspace_id():
    unfiltered = client.get(
        f"/api/finding/{NO_WORKSPACE_ID_QUERY_ID}", params={"window": 30, "limit": 5000}
    ).json()
    filtered = client.get(
        f"/api/finding/{NO_WORKSPACE_ID_QUERY_ID}",
        params={"window": 30, "cost_center": "engineering", "limit": 5000},
    ).json()

    assert unfiltered["outcome"] == "ok_rows"
    assert unfiltered["rows_total"] > 0
    assert filtered["outcome"] == unfiltered["outcome"]
    assert filtered["rows_total"] == unfiltered["rows_total"]
    assert filtered["rows"] == unfiltered["rows"]


# -------------------------------------------------------------------------------------------
# An unknown canonical key never becomes a SQL identifier (app/core/data._attribute_canonical_keys'
# whitelist) -- a query param that is not a configured canonical key is simply ignored, not a 500.
# -------------------------------------------------------------------------------------------


def test_unknown_attribute_query_param_is_ignored_not_an_error():
    r = client.get(
        f"/api/finding/{WS_SCOPED_QUERY_ID}",
        params={"window": 30, "not_a_real_canonical_key": "drop table x", "limit": 5000},
    )
    assert r.status_code == 200
