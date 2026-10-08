"""tests/test_findings/test_compute_warehouse_cache_reuse.py

Proves, against tests/fixtures/compute_coverage.py's own `cc_wh_cache_*` rows (workspace
cc_ws1), that findings.f_compute_warehouse_cache_reuse (grain [warehouse_id], window_days = 30,
header defaults: min_queries_for_verdict 10, warn_low_cache_share_pct 5,
warn_long_autostop_minutes 10) has the right grain and every status band: WARN (low cache share
with a long auto-stop, whether 15 or 30 minutes - this check never reads CRITICAL, a heuristic
that can be wrong for a unique-query workload), OK via too little traffic to judge, and OK via a
healthy cache-reuse share regardless of auto-stop length. Every expected number is computed by
hand from that fixture module's own scenario table (also cross-checked directly against DuckDB
in-session before this file was written).
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "compute_warehouse_cache_reuse.yml"
QID = "compute_warehouse_cache_reuse"
_CACHE_IDS = {"cc_wh_cache_warn_long", "cc_wh_cache_warn", "cc_wh_cache_ok_low", "cc_wh_cache_ok_high"}


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _rows():
    # This model's grain is [warehouse_id] only - no workspace_id column to filter on.
    return [r for r in dbutil.rows(QID, 30) if r["warehouse_id"] in _CACHE_IDS]


def _by_wh(warehouse_id: str) -> dict:
    matches = [r for r in _rows() if r["warehouse_id"] == warehouse_id]
    assert len(matches) == 1, f"expected exactly one row for {warehouse_id}, got {len(matches)}"
    return matches[0]


def test_grain_uniqueness():
    grains = _grains()
    cols = grains[QID]
    out = dbutil.rows(QID, 30)
    assert out
    keys = [tuple(r[c] for c in cols) for r in out]
    assert len(keys) == len(set(keys)), "duplicate grain rows"


def test_status_enum():
    for r in _rows():
        assert r["status"] in {"OK", "WARN"}


def test_warn_low_cache_very_long_autostop():
    r = _by_wh("cc_wh_cache_warn_long")
    assert r["auto_stop_minutes"] == 30
    assert r["queries"] == 20
    assert r["from_result_cache_pct"] == 0.0
    assert r["status"] == "WARN"


def test_warn_low_cache_long_autostop():
    r = _by_wh("cc_wh_cache_warn")
    assert r["auto_stop_minutes"] == 15
    assert r["queries"] == 20
    assert r["from_result_cache_pct"] == 0.0
    assert r["status"] == "WARN"


def test_ok_too_few_queries_to_judge():
    r = _by_wh("cc_wh_cache_ok_low")
    assert r["queries"] == 3
    assert r["from_result_cache_pct"] == 0.0
    assert r["auto_stop_minutes"] == 60  # would be CRITICAL at 10+ queries
    assert r["status"] == "OK"


def test_ok_healthy_cache_share():
    r = _by_wh("cc_wh_cache_ok_high")
    assert r["queries"] == 20
    assert r["from_result_cache_pct"] == 50.0
    assert r["auto_stop_minutes"] == 60
    assert r["status"] == "OK"
