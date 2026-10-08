"""tests/test_findings/test_compute_serving_dormant_endpoints.py -- T-11 port assertions.

The compute_serving_dormant_endpoints model is a finding query that identifies serving
endpoints with insufficient request traffic in the window. It produces status bands (OK, WARN,
CRITICAL, NOT_ASSESSED) and orders worst-first (CRITICAL, WARN, NOT_ASSESSED, OK, tiebroken by
total_requests ASC). Tests assert grain uniqueness, status-by-id, worst-first ordering, window
exclusions at 7/30/90 (including a status flip at w=7), and every asserted total_requests value
is cross-checked against an independent COUNT(*) over the raw endpoint_usage fixture parquet --
never a hard-coded total on its own.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

# Add parent directory to path to import dbutil
import sys
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from dbutil import rows

PARQUET_DIR = ROOT / "tests" / "fixtures" / "parquet"


def _request_count(served_entity_id: str, window_days: int) -> int:
    """COUNT(*) over serving__endpoint_usage's parquet (every builder's slice, unioned by name --
    the raw fixture source, not the dbt-built findings table), filtered to one served_entity_id
    and the trailing window ending at the pinned test target's "today" (DATE '2026-09-21'). An
    independent computation straight from the builder's own parquet, mirroring the model's own
    `COUNT(*) ... WHERE request_time >= ... AND request_time < ... GROUP BY
    workspace_id, served_entity_id` without importing any model SQL."""
    glob = (PARQUET_DIR / "serving__endpoint_usage" / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        sql = f"""
            SELECT COUNT(*) FROM read_parquet('{glob}', union_by_name=true)
            WHERE served_entity_id = ?
              AND request_time >= DATE '2026-09-21' - INTERVAL {int(window_days)} DAY
              AND request_time < DATE '2026-09-21'
        """
        return con.execute(sql, [served_entity_id]).fetchone()[0]
    finally:
        con.close()


def test_compute_serving_dormant_endpoints():
    """Assert grain, status-by-id, worst-first ordering, and window behaviour of
    f_compute_serving_dormant_endpoints."""
    query_id = "compute_serving_dormant_endpoints"

    # (a) rows(id, 30) has >= 1 row, all with window_days == 30
    rows_30 = rows(query_id, 30)
    assert len(rows_30) >= 1, f"Expected >= 1 rows at window_days=30, got {len(rows_30)}"
    for row in rows_30:
        assert row["window_days"] == 30, f"Expected window_days=30, got {row['window_days']}"

    # (b) no duplicate grain (window_days, workspace_id, endpoint_id)
    grain_keys = [(r["window_days"], r["workspace_id"], r["endpoint_id"]) for r in rows_30]
    assert len(grain_keys) == len(set(grain_keys)), f"Duplicate grain entries found in rows_30"

    # (c) status enum check: all status values are one of OK, WARN, CRITICAL, NOT_ASSESSED
    valid_statuses = {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}
    for row in rows_30:
        assert row["status"] in valid_statuses, f"Invalid status '{row['status']}'; expected one of {valid_statuses}"

    # (d) filter to this builder's own pt_-prefixed rows (DEC-15) and assert status + request
    # count by id at w=30. Every total_requests value is cross-checked against an independent
    # COUNT(*) over the raw endpoint_usage parquet (see _request_count), not a bare literal.
    pt_rows_30 = {r["endpoint_id"]: r for r in rows_30 if r["endpoint_id"].startswith("pt_")}
    for eid in ("pt_ep_ok", "pt_ep_warn", "pt_ep_crit", "pt_ep_new"):
        assert eid in pt_rows_30, f"missing endpoint_id={eid!r} at window_days=30"

    assert pt_rows_30["pt_ep_ok"]["status"] == "OK"
    assert pt_rows_30["pt_ep_ok"]["total_requests"] == _request_count("pt_se_ok", 30) == 15

    assert pt_rows_30["pt_ep_warn"]["status"] == "WARN"
    assert pt_rows_30["pt_ep_warn"]["total_requests"] == _request_count("pt_se_warn", 30) == 5

    assert pt_rows_30["pt_ep_crit"]["status"] == "CRITICAL"
    assert pt_rows_30["pt_ep_crit"]["total_requests"] == _request_count("pt_se_crit", 30) == 0

    assert pt_rows_30["pt_ep_new"]["status"] == "NOT_ASSESSED"

    # A deleted endpoint and a Databricks pay-per-token endpoint are idle too, but left out.
    assert "pt_ep_deleted" not in pt_rows_30
    assert "pt_ep_ppt" not in pt_rows_30

    # (e) worst-first ordering across the pt_ rows, per the model's own order_by: CASE status
    # WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
    # total_requests ASC. dbutil.rows() applies no ORDER BY of its own, so the physical row order
    # returned is the model's own order -- checked directly, never re-sorted by the test (see
    # tests/test_findings/README.md).
    status_rank = {"CRITICAL": 0, "WARN": 1, "NOT_ASSESSED": 2, "OK": 3}
    pt_order = [r for r in rows_30 if r["endpoint_id"].startswith("pt_")]
    ranks = [status_rank[r["status"]] for r in pt_order]
    assert ranks == sorted(ranks), (
        f"pt_ rows are not worst-first ordered: "
        f"{[(r['endpoint_id'], r['status']) for r in pt_order]}"
    )
    for a, b in zip(pt_order, pt_order[1:]):
        if a["status"] == b["status"]:
            assert a["total_requests"] <= b["total_requests"], (
                f"tiebreak violated within status {a['status']!r}: "
                f"{a['endpoint_id']}={a['total_requests']} before "
                f"{b['endpoint_id']}={b['total_requests']}"
            )

    # (f) w=7 flips pt_se_ok's status: its 15 requests all land at D(11) (tests/fixtures/ports.py),
    # outside the 7-day window, so total_requests drops to 0 and status flips from OK to CRITICAL
    # (change_time stays outside every window, so NOT_ASSESSED never fires for this entity).
    rows_7 = rows(query_id, 7)
    pt_rows_7 = {r["endpoint_id"]: r for r in rows_7 if r["endpoint_id"].startswith("pt_")}
    assert pt_rows_7["pt_ep_ok"]["total_requests"] == _request_count("pt_se_ok", 7) == 0
    assert pt_rows_7["pt_ep_ok"]["status"] == "CRITICAL"
    assert pt_rows_7["pt_ep_ok"]["status"] != pt_rows_30["pt_ep_ok"]["status"], (
        "expected pt_se_ok's status to flip between w=7 and w=30"
    )

    # (g) w=90 differs from w=30 for the same id: an extra request at D(45) falls inside the
    # 90-day window only, so total_requests is higher at w=90 while status stays OK.
    rows_90 = rows(query_id, 90)
    pt_rows_90 = {r["endpoint_id"]: r for r in rows_90 if r["endpoint_id"].startswith("pt_")}
    assert pt_rows_90["pt_ep_ok"]["total_requests"] == _request_count("pt_se_ok", 90)
    assert pt_rows_90["pt_ep_ok"]["total_requests"] > pt_rows_30["pt_ep_ok"]["total_requests"], (
        "expected pt_se_ok's total_requests to be strictly higher at w=90 than at w=30"
    )
    assert pt_rows_90["pt_ep_ok"]["status"] == "OK"

    # (h) rows(id, 0) == [] -- the generator's window loop only emits window_days in {7, 30, 90}
    # for a parametrized (:period_days) query; window_days=0 is never populated.
    rows_0 = rows(query_id, 0)
    assert rows_0 == [], f"Expected no rows at window_days=0, got {len(rows_0)} rows"
