"""The bill's tags ride over a warehouse's or cluster's own tags in the performance rollup, as they
already do for cost (tags.cost_unit_tag's compute level is the bill). Run once at load time.
"""
from __future__ import annotations

import duckdb

# A tag on the bill's last day is on it today; its value then replaces the resource's own value.
_BILL_TODAY = """
    SELECT CASE entity_type WHEN 'billed_warehouse' THEN 'warehouse' ELSE 'cluster' END AS compute_kind,
           workspace_id, entity_id, tag_key, raw_key, last_value AS tag_value
    FROM tags.bill_tag_dates
    WHERE entity_type IN ('billed_warehouse', 'billed_cluster') AND last_date >= entity_last_date
"""


def apply_bill_to_perf(con: duckdb.DuckDBPyConnection) -> int:
    """Rewrites tags.perf_unit_tag's compute level with the bill's values; returns the rows taken
    from the bill."""
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _bill_perf AS
        SELECT u.window_days, u.unit_id, 'compute' AS level, b.tag_key, b.raw_key, b.tag_value,
               'billing:' || b.compute_kind AS source,
               u.statements, u.failed_statements, u.duration_ms, u.queue_ms, u.spill_bytes
        FROM tags.perf_unit u
        JOIN ({_BILL_TODAY}) b
          ON b.workspace_id = u.workspace_id AND b.compute_kind = u.compute_kind AND b.entity_id = u.compute_id
    """)
    con.execute("""
        CREATE OR REPLACE TABLE tags.perf_unit_tag AS
        SELECT t.* FROM tags.perf_unit_tag t
        WHERE NOT (t.level = 'compute' AND EXISTS (
            SELECT 1 FROM _bill_perf b WHERE b.window_days = t.window_days AND b.unit_id = t.unit_id AND b.tag_key = t.tag_key))
        UNION ALL BY NAME
        SELECT * FROM _bill_perf
    """)
    n = con.execute("SELECT count(*) FROM _bill_perf").fetchone()[0]
    con.execute("DROP TABLE _bill_perf")
    return int(n)
