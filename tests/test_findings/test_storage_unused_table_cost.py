"""tests/test_findings/test_storage_unused_table_cost.py

Proves findings.f_storage_unused_table_cost (grain [table_catalog, table_schema, table_name],
windowed on :period_days) against tests/fixtures/storage.py's own `_build_unused_table_cost`
scenario (`stg_utc_*`, catalog stg_utc_catalog): the dead-table rule itself (a lineage SOURCE hit
excludes a table, same rule access_dead_table_candidates uses - not re-proven here), the $ bands
(CRITICAL/WARN/OK off est_total_usd_month), a Predictive-Optimization-upkeep-driven CRITICAL versus
a storage-driven one, the NOT_ASSESSED path for a missing size snapshot, days_since_altered /
days_since_last_write (including the NULL-if-never-written case), and the :top_n=500 pooling
mechanic (is_other=true, always status=OK).

Not re-tested here (see access_dead_table_candidates' own suite instead, since it is the exact same
shared rule): the lineage-source window boundary and the 7/30/90-day differential - this file
queries window_days=30 only.

tests/fixtures/storage.py also writes information_schema.tables / access.table_lineage rows shared
with tests/fixtures/governance.py's own gv_* dead-table scenario (both write the same two DuckDB
tables); every assertion below filters to table_catalog == "stg_utc_catalog" (or, for the pooled
row, is_other) first, per DEC-15's "assertions filter on the builder's own ids" rule.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "storage_unused_table_cost.yml"
QID = "storage_unused_table_cost"
WINDOW = 30


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _all() -> list[dict]:
    return dbutil.rows(QID, WINDOW)


def _own() -> list[dict]:
    return [r for r in _all() if r["table_catalog"] == "stg_utc_catalog"]


def _by_name() -> dict:
    return {r["table_name"]: r for r in _own()}


def test_grain_uniqueness():
    cols = _grains()[QID]
    out = _all()
    assert out
    keys = [tuple(r[c] for c in cols) for r in out]
    assert len(keys) == len(set(keys)), "duplicate grain rows"


def test_status_enum():
    valid = {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}
    for r in _all():
        assert r["status"] in valid


def test_alive_table_excluded():
    assert "stg_utc_alive" not in _by_name(), "appears as a lineage source -> excluded"


# =================================================================================================
# stg_utc_crit: 5000 GiB storage ($100.00 exactly) + zero Predictive Optimization DBUs (a real,
# known $0 upkeep) -> est_total_usd_month=100.00 -> CRITICAL (>=:crit_usd_month=100). Written to
# (lineage TARGET) 45 days ago -> days_since_last_write=45.
# =================================================================================================
def test_storage_driven_critical():
    r = _by_name()["stg_utc_crit"]
    assert r["table_type"] == "MANAGED"
    assert r["table_owner"] == "stg_utc_crit_owner@example.com", "mask_user() is off by default"
    assert r["active_gb"] == 5000.0
    assert r["est_storage_usd_month"] == 100.0
    assert r["po_upkeep_usd_window"] == 0.0
    assert r["price_basis"] == "priced"
    assert r["est_total_usd_month"] == 100.0
    assert r["days_since_altered"] == 200
    assert r["days_since_last_write"] == 45
    assert r["not_assessed_reason"] is None
    assert r["status"] == "CRITICAL"


# =================================================================================================
# stg_utc_warn: 1500 GiB -> $30.00 -> WARN (>=20, <100). Never written to on record ->
# days_since_last_write is NULL.
# =================================================================================================
def test_storage_driven_warn_and_never_written():
    r = _by_name()["stg_utc_warn"]
    assert r["est_storage_usd_month"] == 30.0
    assert r["est_total_usd_month"] == 30.0
    assert r["days_since_altered"] == 120
    assert r["days_since_last_write"] is None
    assert r["status"] == "WARN"


def test_ok():
    r = _by_name()["stg_utc_ok"]
    assert r["active_gb"] == 500.0
    assert r["est_storage_usd_month"] == 10.0
    assert r["est_total_usd_month"] == 10.0
    assert r["days_since_altered"] == 10
    assert r["status"] == "OK"


# =================================================================================================
# stg_utc_po_driven: 10 GiB storage ($0.20) + 150 Predictive Optimization DBUs in the window, at
# the $1.00/DBU blended rate the fixture's own billing.usage/list_prices rows establish (100 DBU
# billed at $1.00/DBU) -> po_upkeep_usd_window = 150 * 1.00 = $150.00 -> est_total_usd_month =
# 0.20 + 150.00 * 30/30 = 150.20 -> CRITICAL via upkeep, not storage.
# =================================================================================================
def test_po_upkeep_driven_critical():
    r = _by_name()["stg_utc_po_driven"]
    assert r["active_gb"] == 10.0
    assert r["est_storage_usd_month"] == 0.2
    assert r["po_upkeep_usd_window"] == 150.0
    assert r["price_basis"] == "priced"
    assert r["est_total_usd_month"] == 150.2
    assert r["status"] == "CRITICAL"


# =================================================================================================
# stg_utc_no_snapshot: no table_metrics_history snapshot and no Predictive Optimization activity
# at all -> size unknown (not a real zero) -> listed, not priced (OK, no_size), never pooled.
# =================================================================================================
def test_missing_snapshot_is_listed_not_priced():
    r = _by_name()["stg_utc_no_snapshot"]
    assert r["active_gb"] is None
    assert r["est_storage_usd_month"] is None
    assert r["po_upkeep_usd_window"] == 0.0, "zero Predictive Optimization DBUs is a real, known $0"
    assert r["est_total_usd_month"] is None
    assert r["price_basis"] == "no_size"
    assert r["not_assessed_reason"] is None
    assert r["status"] == "OK"
    assert r["is_other"] is False, "an unsized row always keeps its own row, never pooled"


# =================================================================================================
# Pooling: 4 named priced tables above (crit/warn/ok/po_driven) + 497 stg_utc_fill_#### tables
# (1 GiB = $0.02 each, always ranked below the $10.00 stg_utc_ok row) = 501 priced tables total -
# one more than :top_n=500 -> exactly 500 kept as their own row, exactly 1 pooled into a single
# is_other=true row that always reads status=OK.
# =================================================================================================
def test_pooling_keeps_top_500_and_pools_the_rest():
    # The pooled row's own table_catalog is NULL (never "stg_utc_catalog"), so it is gathered from
    # _all() by is_other alone - safe here since no other builder in this repo writes both an
    # information_schema.tables row AND a matching table_metrics_history row for the same table
    # name, so no OTHER builder can ever contribute a priced (non-NOT_ASSESSED) candidate of its
    # own for this query to pool.
    kept = [r for r in _own() if r["est_total_usd_month"] is not None and not r["is_other"]]
    pooled = [r for r in _all() if r["is_other"]]
    priced = kept + pooled
    assert len(priced) == 501
    assert len(kept) == 500
    assert len(pooled) == 1
    other = pooled[0]
    assert other["pooled_count"] == 1
    assert other["status"] == "OK", "a pooled row is never flagged, however large the combined $"
    assert other["table_catalog"] is None and other["table_name"] is None
    # the 1 pooled table is a $0.02 filler, never one of the four named (larger-$) ones above
    kept_names = {k["table_name"] for k in kept}
    for name in ("stg_utc_crit", "stg_utc_warn", "stg_utc_ok", "stg_utc_po_driven"):
        assert name in kept_names, f"{name} must be kept, not pooled"
