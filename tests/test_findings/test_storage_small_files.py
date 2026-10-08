"""tests/test_findings/test_storage_small_files.py

Proves findings.f_storage_small_files (grain [table_id], windowed on :period_days -- one row per
table_id at its latest snapshot_date inside the window) against tests/fixtures/storage.py's own
`stg_sf_*` rows: the right grain, the OK/WARN/CRITICAL bands off active_files/avg_file_size_mb,
that a table dropped inside the window is excluded outright, and that only the latest of two
snapshots for the same table_id is scored (QUALIFY ROW_NUMBER() ... ORDER BY snapshot_date DESC).

See tests/fixtures/storage.py's own `_build_small_files` docstring for the exact scenario table
this file's expectations are computed from.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "storage_small_files.yml"
QID = "storage_small_files"
WINDOW = 30


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _own() -> list[dict]:
    return [r for r in dbutil.rows(QID, WINDOW) if r["table_id"].startswith("stg_sf_")]


def _by_table_id() -> dict:
    return {r["table_id"]: r for r in _own()}


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


def test_ok_few_files():
    r = _by_table_id()["stg_sf_ok"]
    assert r["active_files"] == 100
    assert r["avg_file_size_mb"] == 1.0
    assert r["status"] == "OK"


def test_warn_many_files_mid_avg():
    r = _by_table_id()["stg_sf_warn"]
    assert r["active_files"] == 1500
    assert r["avg_file_size_mb"] == 20.0
    assert r["status"] == "WARN"


def test_critical_many_files_small_avg():
    r = _by_table_id()["stg_sf_crit"]
    assert r["active_files"] == 1500
    assert r["avg_file_size_mb"] == 4.0
    assert r["status"] == "CRITICAL"


def test_dropped_table_excluded():
    # stg_sf_dropped would be CRITICAL by its numbers alone, but table_dropped_time falls inside
    # the window -- the query drops it outright, so it must never appear at all.
    assert "stg_sf_dropped" not in _by_table_id()


def test_latest_snapshot_wins():
    # stg_sf_latest_wins has an older D(10) snapshot that alone would read CRITICAL, and a newer
    # D(2) snapshot that reads OK. Only one row for this table_id, and it must be the latest one.
    own = _own()
    matches = [r for r in own if r["table_id"] == "stg_sf_latest_wins"]
    assert len(matches) == 1
    r = matches[0]
    assert r["active_files"] == 50
    assert r["avg_file_size_mb"] == 50.0
    assert r["status"] == "OK"


def test_worst_first_order():
    own = _own()
    ranks = {"CRITICAL": 0, "WARN": 1, "OK": 2}
    order = [ranks[r["status"]] for r in own]
    assert order == sorted(order)
