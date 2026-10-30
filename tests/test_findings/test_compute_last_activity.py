"""compute_last_activity: status rules over the shared fixtures (windowless, window_days 0)."""
from __future__ import annotations

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

IDLE_DAYS = 90


def test_compute_last_activity_status_rules():
    out = dbutil.rows("compute_last_activity", 0)
    assert {r["object_type"] for r in out} == {"warehouse", "cluster", "workspace"}
    for r in out:
        if r["object_type"] == "workspace":
            assert r["workspace_id"] is not None
        days = r["days_since_activity"]
        if r["status"] == "OK":
            assert days is not None and days < IDLE_DAYS
        elif r["status"] == "WARN":
            assert days is None or days >= IDLE_DAYS
        else:
            assert r["status"] == "NOT_ASSESSED" and r["last_activity_date"] is None
            assert r["not_assessed_reason"] == "created_recently"


def test_compute_last_activity_is_latest_signal():
    for r in dbutil.rows("compute_last_activity", 0):
        # a warehouse is judged on its queries and starts only; a bill alone is not use
        cols = (("last_query_date", "last_start_date") if r["object_type"] == "warehouse" else
                ("last_query_date", "last_start_date", "last_job_run_date", "last_user_event_date", "last_billed_date"))
        dates = [r[c] for c in cols if r[c] is not None]
        assert r["last_activity_date"] == (max(dates) if dates else None)


def test_compute_last_activity_never_used_workspace_is_flagged():
    ws = {r["name"]: r for r in dbutil.rows("compute_last_activity", 0) if r["object_type"] == "workspace"}
    assert ws["dd-uat"]["last_activity_date"] is None
    assert ws["dd-uat"]["status"] == "WARN"
