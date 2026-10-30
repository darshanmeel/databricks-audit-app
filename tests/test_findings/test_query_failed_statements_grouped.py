"""tests/test_findings/test_query_failed_statements_grouped.py

Proves, against tests/fixtures/query_history.py's own `qh_efg_*` and `qh_fq_*` rows (workspace
1111), that findings.f_query_failed_statements_grouped (grain [workspace_id, warehouse_id,
execution_status, error_class, source_kind, source_id], window_days = 30, header defaults:
warn_statements 10, crit_statements 50) groups FAILED statements by error class and source,
never flags a CANCELED group regardless of count, and agrees with query_failed_queries_daily on
the total FAILED count per warehouse even though the two group differently.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "query_failed_statements_grouped.yml"
QID = "query_failed_statements_grouped"
WS = "1111"


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _rows():
    return dbutil.rows(QID, 30, workspace_ids=[WS])


def test_grain_uniqueness():
    grains = _grains()
    cols = grains[QID]
    out = dbutil.rows(QID, 30)
    assert out
    keys = [tuple(r[c] for c in cols) for r in out]
    assert len(keys) == len(set(keys)), "duplicate grain rows"


def test_window_days_zero_is_empty():
    assert dbutil.rows(QID, 0) == []


def test_status_enum():
    for r in dbutil.rows(QID, 30):
        assert r["status"] in {"OK", "WARN", "CRITICAL"}


def test_canceled_group_always_ok_however_many():
    r = next(x for x in _rows() if x["warehouse_id"] == "qh_efg_wh_crit"
             and x["execution_status"] == "CANCELED")
    assert r["statements"] == 3
    assert r["status"] == "OK"


def test_critical_group_error_class_and_source():
    r = next(x for x in _rows() if x["warehouse_id"] == "qh_efg_wh_crit"
             and x["execution_status"] == "FAILED")
    assert r["statements"] == 50
    assert r["status"] == "CRITICAL"           # 50 >= crit_statements(50)
    assert r["error_class"] == "TABLE_OR_VIEW_NOT_FOUND"
    assert r["source_kind"] == "job"
    assert r["source_id"] == "qh_efg_job_1"
    # same start_time on every row here -> tie-break is statement_id DESC (lexicographic, not
    # numeric): "qh_efg_crit_9" sorts above "qh_efg_crit_49" and every other crit_N id.
    assert r["sample_statement_id"] == "qh_efg_crit_9"


def test_unclassified_error_message_with_no_bracket_prefix():
    r = next(x for x in _rows() if x["warehouse_id"] == "qh_fq_wh_crit"
             and x["execution_status"] == "FAILED")
    assert r["error_class"] == "unclassified"  # qh_fq's error_message has no [ERROR_CLASS] prefix
    assert r["source_kind"] is None and r["source_id"] is None


def test_failed_counts_match_query_failed_queries_daily():
    # both queries read the same FAILED/CANCELED rows over the same window, grouped differently --
    # a warehouse's total FAILED count must still agree between the two.
    grouped = _rows()
    daily = dbutil.rows("query_failed_queries_daily", 30, workspace_ids=[WS])
    warehouses = {r["warehouse_id"] for r in grouped if r["execution_status"] == "FAILED"} | \
        {r["warehouse_id"] for r in daily if r["execution_status"] == "FAILED"}
    for wh in warehouses:
        grouped_total = sum(r["statements"] for r in grouped
                             if r["warehouse_id"] == wh and r["execution_status"] == "FAILED")
        daily_total = sum(r["query_count"] for r in daily
                           if r["warehouse_id"] == wh and r["execution_status"] == "FAILED")
        assert grouped_total == daily_total, (wh, grouped_total, daily_total)
