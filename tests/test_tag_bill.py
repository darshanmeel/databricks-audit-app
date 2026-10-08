"""app/core/tag_bill.py: the bill's tag today replaces a warehouse's own tag in the performance rollup."""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.tag_bill import apply_bill_to_perf  # noqa: E402


def test_bill_tag_today_rides_over_the_warehouse_tag():
    con = duckdb.connect()
    con.execute("CREATE SCHEMA tags")
    con.execute("CREATE TABLE tags.perf_unit AS SELECT * FROM (VALUES (30, 'u1', 'w1', 'warehouse', 'wh1', 10, 1, 100, 0, 0), "
                "(30, 'u2', 'w1', 'warehouse', 'wh2', 5, 0, 50, 0, 0)) v(window_days, unit_id, workspace_id, compute_kind, "
                "compute_id, statements, failed_statements, duration_ms, queue_ms, spill_bytes)")
    con.execute("CREATE TABLE tags.perf_unit_tag AS SELECT * FROM (VALUES "
                "(30, 'u1', 'compute', 'costcenter', 'cost_center', 'own_cc', 'warehouse_tags', 10, 1, 100, 0, 0), "
                "(30, 'u2', 'compute', 'costcenter', 'cost_center', 'kept', 'warehouse_tags', 5, 0, 50, 0, 0), "
                "(30, 'u1', 'work', 'costcenter', 'cost_center', 'query_cc', 'query_tags', 4, 0, 40, 0, 0)) "
                "v(window_days, unit_id, level, tag_key, raw_key, tag_value, source, statements, failed_statements, "
                "duration_ms, queue_ms, spill_bytes)")
    con.execute("CREATE TABLE tags.bill_tag_dates AS SELECT * FROM (VALUES "
                "('billed_warehouse', 'w1', 'wh1', 'costcenter', 'cost-center', 'bill_cc', DATE '2026-09-01', DATE '2026-10-05', DATE '2026-10-05'), "
                "('billed_warehouse', 'w1', 'wh1', 'domain', 'domain', 'd1', DATE '2026-09-01', DATE '2026-10-05', DATE '2026-10-05'), "
                "('billed_warehouse', 'w1', 'wh2', 'costcenter', 'cost-center', 'gone', DATE '2026-08-01', DATE '2026-09-01', DATE '2026-10-05')) "
                "v(entity_type, workspace_id, entity_id, tag_key, raw_key, last_value, first_date, last_date, entity_last_date)")
    assert apply_bill_to_perf(con) == 2
    rows = {(u, lv, k): (v, s) for u, lv, k, v, s in con.execute(
        "SELECT unit_id, level, tag_key, tag_value, source FROM tags.perf_unit_tag").fetchall()}
    assert rows[("u1", "compute", "costcenter")] == ("bill_cc", "billing:warehouse")
    assert rows[("u1", "compute", "domain")] == ("d1", "billing:warehouse")
    assert rows[("u1", "work", "costcenter")] == ("query_cc", "query_tags")
    assert rows[("u2", "compute", "costcenter")] == ("kept", "warehouse_tags")
