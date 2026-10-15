"""tests/test_findings/test_cost_chargeback_by_job.py.

Proves, against tests/fixtures/chargeback_b.py's own rows (`cb2_j_*`, account-level usage --
workspace_id IS NULL, same DEC-15 pattern tests/fixtures/finops.py uses; see that module's
docstring for the full scenario table), that findings.f_cost_chargeback_by_job (grain
[workspace_id, job_id], windowed on :period_days) has: the right grain, every status band (WARN,
CRITICAL via percent AND via brand-new spend, OK via the $ floor), NOT_ASSESSED on either side when
that side's whole spend could not be priced at all, price_basis (priced/unpriced/free), a
partially-unpriced job still getting a real verdict (not NOT_ASSESSED), record_type correction
netting, job_name/run_as/creator_user_name resolution (and their absence for a job with no
system.lakeflow.jobs row), and the current day (D0) being excluded from every sum.

Every expected number below is computed by hand from tests/fixtures/chargeback_b.py's own scenario
table (reproduced in that file's module docstring), at the w=7 primary window
(current=[D7..D1], previous=[D14..D8]), the same window finops.py/chargeback.py use.
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

GRAINS_PATH = ROOT / "config" / "grains" / "cost_chargeback_by_job.yml"
QID = "cost_chargeback_by_job"
W = 7


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _jobs(window_days: int = W) -> dict:
    # account-level usage (workspace_id IS NULL) -- dbutil.rows() cannot filter IS NULL, so this
    # reads every row and filters in Python, same as finops.py's own test files do.
    return {
        r["job_id"]: r
        for r in dbutil.rows(QID, window_days)
        # The pooled is_other row (job_id NULL) appears once the account has more than top_n jobs.
        if r["workspace_id"] is None and not r["is_other"] and r["job_id"].startswith("cb2_j_")
    }


def test_grain_uniqueness():
    grains = _grains()
    cols = grains[QID]
    out = [
        r for r in dbutil.rows(QID, W)
        if r["workspace_id"] is None and not r["is_other"] and r["job_id"].startswith("cb2_j_")
    ]
    assert out
    keys = [tuple(r[c] for c in cols) for r in out]
    assert len(keys) == len(set(keys)), "duplicate grain rows"


def test_status_enum():
    valid = {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}
    for r in _jobs().values():
        assert r["status"] in valid
        assert r["window_days"] == W


def test_warn_via_percent_and_name_resolved():
    r = _jobs()["cb2_j_warn"]
    assert r["est_current_usd_list"] == 130.0
    assert r["est_previous_usd_list"] == 100.0
    assert r["est_change_usd_list"] == 30.0
    assert r["change_pct"] == 30.0
    assert r["status"] == "WARN"
    assert r["price_basis"] == "priced"
    assert r["job_name"] == "Warn ETL"
    assert r["run_as"] == "svc-warn@example.com"
    assert r["creator_user_name"] == "alice"


def test_today_excluded_from_current():
    """cb2_j_warn also carries a D0 (today) row of 9999 -- it must never show up in
    est_current_usd_list (still 130.0, not 10129.0)."""
    assert _jobs()["cb2_j_warn"]["est_current_usd_list"] == 130.0


def test_critical_via_percent():
    r = _jobs()["cb2_j_crit"]
    assert r["est_current_usd_list"] == 200.0
    assert r["est_previous_usd_list"] == 100.0
    assert r["change_pct"] == 100.0
    assert r["status"] == "CRITICAL"


def test_ok_via_floor():
    r = _jobs()["cb2_j_floor"]
    assert r["est_current_usd_list"] == 15.0
    assert r["status"] == "OK"


def test_critical_via_brand_new_spend():
    r = _jobs()["cb2_j_new"]
    assert r["est_current_usd_list"] == 80.0
    assert r["est_previous_usd_list"] == 0.0
    assert r["change_pct"] is None
    assert r["status"] == "CRITICAL"
    assert r["not_assessed_reason"] is None


def test_not_assessed_current_period_unpriced():
    r = _jobs()["cb2_j_unpriced_cur"]
    assert r["est_current_usd_list"] is None
    assert r["est_previous_usd_list"] == 100.0
    assert r["status"] == "NOT_ASSESSED"
    assert r["not_assessed_reason"] == "current_period_unpriced"
    assert r["price_basis"] == "unpriced"


def test_not_assessed_previous_period_unpriced():
    r = _jobs()["cb2_j_unpriced_prev"]
    assert r["est_current_usd_list"] == 100.0
    assert r["est_previous_usd_list"] is None
    assert r["status"] == "NOT_ASSESSED"
    assert r["not_assessed_reason"] == "previous_period_unpriced"
    assert r["price_basis"] == "unpriced"


def test_free_usage_is_ok_real_zero():
    r = _jobs()["cb2_j_free"]
    assert r["est_current_usd_list"] == 0.0
    assert r["est_previous_usd_list"] == 0.0
    assert r["price_basis"] == "free"
    assert r["status"] == "OK"
    assert r["not_assessed_reason"] is None


def test_correction_nets_out():
    r = _jobs()["cb2_j_correction"]
    assert r["est_current_usd_list"] == 80.0
    assert r["est_previous_usd_list"] == 80.0
    assert r["change_pct"] == 0.0
    assert r["status"] == "OK"


def test_noname_job_has_null_identity_but_real_verdict():
    r = _jobs()["cb2_j_noname"]
    assert r["job_name"] is None
    assert r["run_as"] is None
    assert r["creator_user_name"] is None
    assert r["est_current_usd_list"] == 50.0
    assert r["change_pct"] == 25.0
    assert r["status"] == "WARN"  # boundary, inclusive


def test_partially_unpriced_job_still_gets_a_verdict():
    r = _jobs()["cb2_j_partial"]
    assert r["est_current_usd_list"] == 60.0
    assert r["est_previous_usd_list"] == 60.0
    assert r["change_pct"] == 0.0
    assert r["price_basis"] == "unpriced"
    assert r["status"] == "OK"
    assert r["not_assessed_reason"] is None
