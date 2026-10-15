"""tests/test_findings/test_storage_growth.py

Proves findings.f_storage_growth (grain [table_id], windowed on :period_days -- one row per
table_id with at least one snapshot in the window, comparing its first and last) against
tests/fixtures/storage.py's own `stg_gr_*` rows: the right grain, the OK/WARN/CRITICAL bands off
growth_pct, that a table dropped in the window reads WARN via the drop itself even at 0% growth,
and that no_owner is a plain flag independent of the growth verdict.

See tests/fixtures/storage.py's own `_build_growth` docstring for the exact scenario table this
file's expectations are computed from.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "storage_growth.yml"
QID = "storage_growth"
WINDOW = 30


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _own() -> list[dict]:
    return [r for r in dbutil.rows(QID, WINDOW) if r["table_id"].startswith("stg_gr_")]


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


def test_flat_reads_ok():
    r = _by_table_id()["stg_gr_ok"]
    assert r["first_bytes"] == 1000
    assert r["last_bytes"] == 1000
    assert r["growth_pct"] == 0.0
    assert r["dropped_in_window"] is False
    assert r["no_owner"] is False
    assert r["status"] == "OK"


def test_25pct_growth_reads_warn():
    r = _by_table_id()["stg_gr_warn"]
    assert r["growth_bytes"] == 1024 ** 3
    assert r["growth_pct"] == 25.0
    assert r["status"] == "WARN"


def test_100pct_growth_reads_critical():
    r = _by_table_id()["stg_gr_crit"]
    assert r["growth_bytes"] == 4 * 1024 ** 3
    assert r["growth_pct"] == 100.0
    assert r["status"] == "CRITICAL"


def test_dropped_in_window_reads_warn_even_at_zero_growth():
    r = _by_table_id()["stg_gr_dropped"]
    assert r["growth_pct"] == 0.0
    assert r["dropped_in_window"] is True
    assert r["status"] == "WARN"


def test_drop_time_from_only_the_last_snapshot_still_reads_warn():
    """A table whose table_dropped_time is NULL on its first snapshot and set only on its last
    (the drop landed mid-window) must still read dropped_in_window=True/WARN -- proves
    table_dropped_time (and the name/owner columns) are read from the LAST snapshot, not the
    first."""
    r = _by_table_id()["stg_gr_dropped_late"]
    assert r["dropped_in_window"] is True
    assert r["status"] == "WARN"
    assert r["table_name"] == "gr_dropped_late"
    assert r["table_owner"] == "stg_owner"


def test_no_owner_flag_independent_of_status():
    r = _by_table_id()["stg_gr_noowner"]
    assert r["no_owner"] is True
    assert r["growth_pct"] == 0.0
    assert r["status"] == "OK"  # flat growth, not dropped -- no_owner alone never flags


def test_worst_first_order():
    own = _own()
    ranks = {"CRITICAL": 0, "WARN": 1, "OK": 2}
    order = [ranks[r["status"]] for r in own]
    assert order == sorted(order)
