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
The filter itself runs in the browser: the top bar turns a picked value into workspace ids.
"""
from __future__ import annotations

import sys
from pathlib import Path

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
WS_SCOPED_QUERY_ID = "cost_dollarized_by_sku_day"
# the one workspace whose bill carries cost_center 'engineering' today (tests/fixtures/compute_coverage.py);
# WS_PROD carried it only in March, so its current value is not_tagged
ENGINEERING_WS = "cc_ws1"
# A price list has no workspace column: a cost-centre filter must not zero it out (DEC-60 rule 6).
NO_WORKSPACE_ID_QUERY_ID = "pricing_list_prices_raw"


# -------------------------------------------------------------------------------------------
# GET /api/meta -- DEC-60 rule 5's populated-keys report.
# -------------------------------------------------------------------------------------------


def test_meta_reports_populated_and_unpopulated_attribute_keys():
    meta = client.get("/api/meta").json()
    populated = set(meta["attribute_keys_populated"])
    # cost_center: cc_ws1 'engineering' today; WS_DEV 'not_tagged', under the coverage floor.
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
        assert key in rows[ENGINEERING_WS]

    assert rows[ENGINEERING_WS]["cost_center"] == "engineering"
    assert rows[ENGINEERING_WS]["cost_center_share"] > 0.9
    # tagged in March only: the workspace's current value is not that old tag
    assert rows[bl.WS_PROD]["cost_center"] == "not_tagged"
    # 55/45 split, but tagged on under half its DBUs: the coverage floor reads it not_tagged.
    assert rows[bl.WS_DEV]["cost_center"] == "not_tagged"
    assert rows[bl.WS_UAT]["cost_center"] == "not_tagged"
