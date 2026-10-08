"""tests/test_findings/test_storage_po_coverage.py

Proves findings.f_storage_po_coverage (grain [section, catalog_name, schema_name, table_id,
table_name], windowed on :period_days) against tests/fixtures/storage.py's own `stg_poc_*` rows:
the two sections (catalog_summary, largest_table_without_po) told apart by `section`, the
OK/WARN/CRITICAL bands off pct_bytes_without_po, that largest_table_without_po rows are always
WARN regardless of their own catalog's band, and worst-first order within catalog_summary.

See tests/fixtures/storage.py's own `_build_po_coverage` docstring for the exact scenario table
this file's expectations are computed from, and its module docstring for why an extra, always-OK
"stg_catalog" catalog_summary row is expected bleed-through from the storage_small_files/
storage_growth scenarios and is not asserted on here.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "storage_po_coverage.yml"
QID = "storage_po_coverage"
WINDOW = 30


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _own() -> list[dict]:
    return [r for r in dbutil.rows(QID, WINDOW) if r["catalog_name"].startswith("stg_poc_")]


def _catalog_summary() -> dict:
    return {r["catalog_name"]: r for r in _own() if r["section"] == "catalog_summary"}


def _top_tables() -> list[dict]:
    return [r for r in _own() if r["section"] == "largest_table_without_po"]


def test_grain_uniqueness():
    cols = _grains()[QID]
    out = dbutil.rows(QID, WINDOW)
    assert out
    keys = [tuple(r[c] for c in cols) for r in out]
    assert len(keys) == len(set(keys)), "duplicate grain rows"


def test_status_enum():
    valid = {"OK", "WARN", "CRITICAL"}
    for r in _own():
        assert r["status"] in valid


def test_catalog_all_po_on_reads_ok():
    r = _catalog_summary()["stg_poc_cat_ok"]
    assert r["tables_total"] == 2
    assert r["tables_without_po"] == 0
    assert r["pct_bytes_without_po"] == 0.0
    assert r["status"] == "OK"


def test_catalog_30pct_without_po_reads_warn():
    r = _catalog_summary()["stg_poc_cat_warn"]
    assert r["bytes_total"] == 1000
    assert r["bytes_without_po"] == 300
    assert r["pct_bytes_without_po"] == 30.0
    assert r["status"] == "WARN"


def test_catalog_80pct_without_po_reads_critical():
    r = _catalog_summary()["stg_poc_cat_crit"]
    assert r["bytes_total"] == 1000
    assert r["bytes_without_po"] == 800
    assert r["pct_bytes_without_po"] == 80.0
    assert r["status"] == "CRITICAL"


def test_largest_table_without_po_always_warn_biggest_first():
    top = _top_tables()
    ids = [r["table_id"] for r in top]
    assert ids == ["stg_poc_crit_b", "stg_poc_warn_b"]
    for r in top:
        assert r["status"] == "WARN"
    assert top[0]["active_bytes_of_table"] == 800
    assert top[1]["active_bytes_of_table"] == 300


def test_top_tables_never_include_a_po_on_table():
    ids = {r["table_id"] for r in _top_tables()}
    assert "stg_poc_ok_a" not in ids
    assert "stg_poc_crit_a" not in ids  # PO on -- never a candidate, even in the CRITICAL catalog


def test_catalog_summary_worst_first_order():
    summary = [r for r in _own() if r["section"] == "catalog_summary"]
    ranks = {"CRITICAL": 0, "WARN": 1, "OK": 2}
    order = [ranks[r["status"]] for r in summary]
    assert order == sorted(order)
