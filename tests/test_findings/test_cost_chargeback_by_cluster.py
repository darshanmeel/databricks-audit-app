"""tests/test_findings/test_cost_chargeback_by_cluster.py.

Proves, against tests/fixtures/chargeback_b.py's own rows (`cb2_cl_*`, account-level usage --
workspace_id IS NULL, see that module's docstring), that findings.f_cost_chargeback_by_cluster
(grain [workspace_id, cluster_kind, entity_id], windowed on :period_days) has: the right grain, the
three-way cluster_kind split (all_purpose / job_cluster / pipeline), the dlt_pipeline_id-checked-
first precedence fix (a row carrying BOTH dlt_pipeline_id and job_id must classify as 'pipeline',
never 'job_cluster'), name/owner resolution per kind, every status band (WARN, CRITICAL via
percent AND via brand-new spend, OK), and the current day (D0) excluded from every sum.

workspace_name is NOT exercised here: this builder's rows are account-level (workspace_id NULL,
the DEC-15 pattern), and system.access.workspaces_latest never carries a NULL workspace_id, so the
IS NOT DISTINCT FROM join to it can never match for these rows -- workspace_name reads NULL for
every row below by construction, not a bug in the query. The join itself is mechanically identical
to the already-tested job_name/run_as join in test_cost_chargeback_by_job.py.

Every expected number below is computed by hand from tests/fixtures/chargeback_b.py's own scenario
table, at the w=7 primary window (current=[D7..D1], previous=[D14..D8]).
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR / "fixtures"))
import dbutil  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "cost_chargeback_by_cluster.yml"
QID = "cost_chargeback_by_cluster"
W = 7
MY_ENTITY_IDS = {
    "cb2_cl_ap1", "cb2_cl_jc1", "cb2_cl_pipe1", "cb2_cl_pipe2", "cb2_cl_na",
}


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _rows(window_days: int = W) -> dict:
    # account-level usage (workspace_id IS NULL) -- filtered in Python, same as finops.py's tests.
    return {
        r["entity_id"]: r
        for r in dbutil.rows(QID, window_days)
        if r["workspace_id"] is None and r["entity_id"] in MY_ENTITY_IDS
    }


def test_grain_uniqueness():
    grains = _grains()
    cols = grains[QID]
    out = [r for r in dbutil.rows(QID, W) if r["workspace_id"] is None and r["entity_id"] in MY_ENTITY_IDS]
    assert len(out) == 5
    keys = [tuple(r[c] for c in cols) for r in out]
    assert len(keys) == len(set(keys)), "duplicate grain rows"


def test_status_enum_and_window():
    for r in _rows().values():
        assert r["status"] in {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}
        assert r["window_days"] == W


def test_all_purpose_kind_and_naming():
    r = _rows()["cb2_cl_ap1"]
    assert r["cluster_kind"] == "all_purpose"
    assert r["name"] == "AP Warn Cluster"
    assert r["owner"] == "owner-ap@example.com"
    assert r["est_current_usd_list"] == 130.0
    assert r["est_previous_usd_list"] == 100.0
    assert r["change_pct"] == 30.0
    assert r["status"] == "WARN"


def test_today_excluded_from_all_purpose_current():
    """cb2_cl_ap1 also carries a D0 (today) row of 9999 -- must never show up in
    est_current_usd_list (still 130.0)."""
    assert _rows()["cb2_cl_ap1"]["est_current_usd_list"] == 130.0


def test_job_cluster_kind_rolled_up_to_job_and_naming():
    r = _rows()["cb2_cl_jc1"]
    assert r["cluster_kind"] == "job_cluster"
    assert r["name"] == "JC Crit Job"
    assert r["owner"] == "svc-jc@example.com"
    assert r["est_current_usd_list"] == 200.0
    assert r["est_previous_usd_list"] == 100.0
    assert r["change_pct"] == 100.0
    assert r["status"] == "CRITICAL"


def test_pipeline_kind_and_naming():
    r = _rows()["cb2_cl_pipe1"]
    assert r["cluster_kind"] == "pipeline"
    assert r["name"] == "OK Pipeline"
    assert r["owner"] == "svc-pipe@example.com"
    assert r["est_current_usd_list"] == 50.0
    assert r["est_previous_usd_list"] == 48.0
    assert r["change_pct"] == 4.2
    assert r["status"] == "OK"


def test_pipeline_id_checked_before_job_id():
    """cb2_cl_pipe2's usage row carries BOTH dlt_pipeline_id=cb2_cl_pipe2 and a job_id -- it must
    classify as 'pipeline' keyed by cb2_cl_pipe2, never 'job_cluster' keyed by the job_id (which
    must not appear as its own entity_id anywhere in this result at all)."""
    r = _rows()["cb2_cl_pipe2"]
    assert r["cluster_kind"] == "pipeline"
    assert r["name"] == "Precedence Pipeline"
    assert r["est_current_usd_list"] == 30.0
    assert r["est_previous_usd_list"] == 0.0
    assert r["status"] == "CRITICAL"  # brand-new spend
    ids = {r2["entity_id"] for r2 in dbutil.rows(QID, W) if r2["workspace_id"] is None}
    assert "cb2_cl_pipe2_job_should_be_ignored" not in ids


def test_workspace_name_is_null_for_account_level_rows():
    for r in _rows().values():
        assert r["workspace_name"] is None


def test_not_assessed_current_period_unpriced():
    r = _rows()["cb2_cl_na"]
    assert r["cluster_kind"] == "all_purpose"
    assert r["status"] == "NOT_ASSESSED"
    assert r["not_assessed_reason"] == "current_period_unpriced"
    assert r["est_current_usd_list"] is None
