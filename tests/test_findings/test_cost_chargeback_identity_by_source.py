"""tests/test_findings/test_cost_chargeback_identity_by_source.py.

f_cost_chargeback_identity_by_source reads system.billing.usage + system.billing.list_prices +
system.query.history + system.compute.clusters (tests/fixtures/chargeback_a.py, workspace
cba.WS_ID) and chargebacks list-priced DBU spend per (identity_run_as, source[, warehouse]),
current window vs the equal-length window right before it, split across three non-overlapping
sources (sql_warehouse/jobs/other). Every expected number below is hand-derived directly from
chargeback_a.py's own scenario map (reproduced in that file's module docstring). This file does
not attempt to exercise the per-source NOT_ASSESSED gate -- see chargeback_a.py's module docstring
for why that is not practical to isolate in a shared, multi-scenario fixture.
"""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from dbutil import rows  # noqa: E402
import chargeback_a as cba  # noqa: E402

QID = "cost_chargeback_identity_by_source"


def test_grain_is_unique_and_window_days_is_set():
    # No workspace_ids filter here: the jobs/other sources always carry workspace_id=NULL by this
    # query's own design (see its header), so a workspace filter would silently drop them.
    rows_30 = rows(QID, 30)
    assert len(rows_30) >= 1
    for row in rows_30:
        assert row["window_days"] == 30
    keys = [
        (r["identity_run_as"], r["source"], r["workspace_id"], r["warehouse_id"])
        for r in rows_30
    ]
    assert len(keys) == len(set(keys)), "duplicate (identity_run_as, source, workspace_id, warehouse_id) grain"


def test_sql_warehouse_source_duration_split_warn():
    """cba_id_wh1: 100% of both days' query.history duration attributed to
    cba_id_alice@example.com -> cur=125, prev=100 -> change 25.0% -> WARN."""
    all_rows = rows(QID, 30)
    r = next(
        x for x in all_rows
        if x["source"] == "sql_warehouse" and x["identity_run_as"] == "cba_id_alice@example.com"
    )
    assert r["warehouse_id"] == "cba_id_wh1"
    assert r["usd_list"] == 125.0
    assert r["prev_usd_list"] == 100.0
    assert r["change_pct"] == 25.0
    assert r["status"] == "WARN"
    assert r["identity_type"] == "user"
    assert r["is_other"] is False


def test_jobs_source_brand_new_spend_is_critical():
    """cba_id_job1: owned_by=cba_id_job_owner@example.com, cur=60, no previous row at all ->
    CRITICAL (brand-new spend)."""
    all_rows = rows(QID, 30)
    r = next(
        x for x in all_rows
        if x["source"] == "jobs" and x["identity_run_as"] == "cba_id_job_owner@example.com"
    )
    assert r["usd_list"] == 60.0
    assert r["prev_usd_list"] == 0.0
    assert r["status"] == "CRITICAL"
    assert r["is_other"] is False


def test_other_source_all_purpose_cluster_owned_by_fallback_ok():
    """cba_id_cluster1 (usage_metadata.cluster_id set, identity_metadata blank on the usage rows
    themselves): identity resolves through system.compute.clusters' own latest owned_by
    (cba_id_cluster_owner@example.com) -- cur=40, prev=38 -> change 5.3% -> OK."""
    all_rows = rows(QID, 30)
    r = next(
        x for x in all_rows
        if x["source"] == "other" and x["identity_run_as"] == "cba_id_cluster_owner@example.com"
    )
    assert r["usd_list"] == 40.0
    assert r["prev_usd_list"] == 38.0
    assert r["change_pct"] == 5.3
    assert r["status"] == "OK"
    assert r["is_other"] is False


def test_top_n_pooling_cap_holds_and_pooling_actually_happens():
    """25 'other'-source identities (cba_id_pool_00..24@example.com) each carry $1 current-period
    spend with no previous data -> all status=OK. :top_n defaults to 20, so at most 20 of them are
    ever individually listed, and since 25 > 20 at least one must be pooled -- see
    chargeback_a.py's module docstring for why an exact split cannot be pinned here."""
    all_rows = rows(QID, 30)
    pool_rows = [
        r for r in all_rows
        if not r["is_other"] and str(r["identity_run_as"]).startswith("cba_id_pool_")
    ]
    assert len(pool_rows) <= 20
    assert len(pool_rows) < cba.TOP_N_POOL
    for r in pool_rows:
        assert r["status"] == "OK"
        assert r["source"] == "other"
