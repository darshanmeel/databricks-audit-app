"""tools/warehouse_activity.py

One SQL warehouse in detail, for Compute > Warehouses (click a warehouse). Plain statements, not checks:
- warehouse_daily: per warehouse and day, minutes it was up (warehouse events), minutes with a query
  running (query history), queries, failed queries, people and service principals, time queued;
  up minus busy is its idle time that day;
- warehouse_users: per window, warehouse and identity, queries, failed, run time and the last query;
- warehouse_tables: per window, warehouse and table, reads and writes from lineage joined to the query that
  caused them (statement_id); fails alone, with its error, when lineage has no statement_id;
- warehouse_slow: per window and warehouse, its slowest queries: who, when, how long, the first words of the text.
Cost per day comes from the cost_daily_by_resource check, idle gaps from compute_warehouse_idle_minutes.

Called by tools/export_direct_results.py after the checks; writes warehouse/<name>.parquet.
"""
from __future__ import annotations

import datetime as dt
import time
from pathlib import Path
from typing import Callable

import pyarrow as pa
import pyarrow.parquet as pq

DAILY_FILE, USERS_FILE, TABLES_FILE, SLOW_FILE = (
    "warehouse_daily.parquet", "warehouse_users.parquet", "warehouse_tables.parquet", "warehouse_slow.parquet")
DAILY_SCHEMA = pa.schema([
    ("workspace_id", pa.string()), ("warehouse_id", pa.string()), ("day", pa.string()),
    ("up_minutes", pa.float64()), ("busy_minutes", pa.float64()), ("queries", pa.int64()),
    ("failed", pa.int64()), ("people", pa.int64()), ("service_principals", pa.int64()),
    ("queued_ms", pa.int64()), ("run_ms", pa.int64()),
])
USERS_SCHEMA = pa.schema([
    ("window_days", pa.int64()), ("workspace_id", pa.string()), ("warehouse_id", pa.string()), ("identity", pa.string()),
    ("queries", pa.int64()), ("failed", pa.int64()), ("run_ms", pa.int64()), ("last_query", pa.string()),
])
TABLES_SCHEMA = pa.schema([
    ("window_days", pa.int64()), ("workspace_id", pa.string()), ("warehouse_id", pa.string()), ("table_full_name", pa.string()),
    ("reads", pa.int64()), ("writes", pa.int64()), ("people", pa.int64()), ("last_access", pa.string()),
])
SLOW_SCHEMA = pa.schema([
    ("window_days", pa.int64()), ("workspace_id", pa.string()), ("warehouse_id", pa.string()), ("statement_id", pa.string()),
    ("identity", pa.string()), ("start_time", pa.string()), ("total_ms", pa.int64()), ("queued_ms", pa.int64()),
    ("status", pa.string()), ("statement_type", pa.string()), ("text", pa.string()),
])
TABLES_MAX = 50  # per window and warehouse
SLOW_MAX = 20  # per window and warehouse
TEXT_CHARS = 200
_UUID = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
UP_EVENTS = "'STARTING', 'RUNNING', 'SCALED_UP', 'SCALED_DOWN'"


def _q(end: dt.date, days: int) -> str:
    e = f"DATE'{end.isoformat()}'"
    return f"""q AS (
  SELECT CAST(workspace_id AS STRING) AS workspace_id, compute.warehouse_id AS warehouse_id, statement_id,
         lower(executed_by) AS who, start_time, end_time, execution_status, statement_type, statement_text,
         COALESCE(total_duration_ms, 0) AS total_ms, COALESCE(execution_duration_ms, 0) AS run_ms,
         COALESCE(waiting_at_capacity_duration_ms, 0) + COALESCE(waiting_for_compute_duration_ms, 0) AS queued_ms
  FROM system.query.history
  WHERE start_time >= date_sub({e}, {days}) AND start_time < {e} AND compute.warehouse_id IS NOT NULL
)"""


def daily_sql(end: dt.date, days: int) -> str:
    """Per warehouse and day: up minutes (STARTING to STOPPED), busy minutes (any query running),
    queries, failures, identities, time queued and run."""
    e = f"DATE'{end.isoformat()}'"
    lo, hi = f"CAST(date_sub({e}, {days}) AS TIMESTAMP)", f"CAST({e} AS TIMESTAMP)"
    return f"""
WITH {_q(end, days)},
ev AS (
  SELECT CAST(workspace_id AS STRING) AS workspace_id, warehouse_id, event_type, event_time AS t,
         LEAD(event_time) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY event_time) AS nt
  FROM system.compute.warehouse_events
  WHERE event_time >= date_sub({e}, {days} + 7) AND event_time < {e}
),
seg AS (
  SELECT workspace_id, warehouse_id, GREATEST(t, {lo}) AS s, LEAST(COALESCE(nt, {hi}), {hi}) AS e
  FROM ev WHERE event_type IN ({UP_EVENTS})
),
up AS (
  SELECT workspace_id, warehouse_id, d,
         SUM(unix_timestamp(LEAST(e, CAST(date_add(d, 1) AS TIMESTAMP))) - unix_timestamp(GREATEST(s, CAST(d AS TIMESTAMP)))) / 60.0 AS up_minutes
  FROM (SELECT *, explode(sequence(to_date(s), to_date(e - INTERVAL 1 SECOND))) AS d FROM seg WHERE e > s) x
  GROUP BY 1, 2, 3
),
busy AS (
  SELECT workspace_id, warehouse_id, to_date(m) AS d, COUNT(DISTINCT m) AS busy_minutes
  FROM (SELECT workspace_id, warehouse_id,
               explode(sequence(date_trunc('MINUTE', start_time),
                                date_trunc('MINUTE', COALESCE(end_time, start_time)), INTERVAL 1 MINUTE)) AS m
        FROM q) x
  GROUP BY 1, 2, 3
),
qd AS (
  SELECT workspace_id, warehouse_id, to_date(start_time) AS d, COUNT(*) AS queries,
         COUNT_IF(execution_status = 'FAILED') AS failed,
         COUNT(DISTINCT CASE WHEN who LIKE '%@%' THEN who END) AS people,
         COUNT(DISTINCT CASE WHEN who RLIKE '{_UUID}' THEN who END) AS service_principals,
         SUM(queued_ms) AS queued_ms, SUM(run_ms) AS run_ms
  FROM q GROUP BY 1, 2, 3
),
k AS (SELECT workspace_id, warehouse_id, d FROM up UNION SELECT workspace_id, warehouse_id, d FROM qd)
SELECT k.workspace_id, k.warehouse_id, CAST(k.d AS STRING) AS day,
       ROUND(COALESCE(up.up_minutes, 0), 1) AS up_minutes, CAST(COALESCE(busy.busy_minutes, 0) AS DOUBLE) AS busy_minutes,
       COALESCE(qd.queries, 0) AS queries, COALESCE(qd.failed, 0) AS failed, COALESCE(qd.people, 0) AS people,
       COALESCE(qd.service_principals, 0) AS service_principals, COALESCE(qd.queued_ms, 0) AS queued_ms,
       COALESCE(qd.run_ms, 0) AS run_ms
FROM k
LEFT JOIN up ON up.workspace_id = k.workspace_id AND up.warehouse_id = k.warehouse_id AND up.d = k.d
LEFT JOIN busy ON busy.workspace_id = k.workspace_id AND busy.warehouse_id = k.warehouse_id AND busy.d = k.d
LEFT JOIN qd ON qd.workspace_id = k.workspace_id AND qd.warehouse_id = k.warehouse_id AND qd.d = k.d
WHERE k.d >= date_sub({e}, {days}) AND k.d < {e}
"""


def _qw(end: dt.date, windows: list[int]) -> str:
    """q once per window: each statement under every window that covers it."""
    e = f"DATE'{end.isoformat()}'"
    ws = sorted({int(x) for x in windows})
    return f"""{_q(end, max(ws))},
w AS (SELECT explode(array({", ".join(map(str, ws))})) AS days),
qw AS (SELECT w.days AS window_days, q.* FROM q JOIN w ON q.start_time >= date_sub({e}, w.days))"""


def users_sql(end: dt.date, windows: list[int]) -> str:
    return f"""
WITH {_qw(end, windows)}
SELECT window_days, workspace_id, warehouse_id, who AS identity, COUNT(*) AS queries,
       COUNT_IF(execution_status = 'FAILED') AS failed, SUM(run_ms) AS run_ms, CAST(MAX(start_time) AS STRING) AS last_query
FROM qw WHERE who IS NOT NULL
GROUP BY 1, 2, 3, 4
"""


def tables_sql(end: dt.date, windows: list[int]) -> str:
    """Tables read and written by this warehouse's queries: lineage rows of the same statement."""
    e = f"DATE'{end.isoformat()}'"
    days = max(windows)
    return f"""
WITH {_qw(end, windows)},
lin AS (
  SELECT statement_id, source_table_full_name AS t, 1 AS is_read, 0 AS is_write, event_time
  FROM system.access.table_lineage
  WHERE event_date >= date_sub({e}, {days}) AND event_date < {e} AND statement_id IS NOT NULL AND source_table_full_name IS NOT NULL
  UNION ALL
  SELECT statement_id, target_table_full_name, 0, 1, event_time
  FROM system.access.table_lineage
  WHERE event_date >= date_sub({e}, {days}) AND event_date < {e} AND statement_id IS NOT NULL AND target_table_full_name IS NOT NULL
),
per AS (
  SELECT q.window_days, q.workspace_id, q.warehouse_id, l.t AS table_full_name,
         COUNT(DISTINCT CASE WHEN l.is_read = 1 THEN q.statement_id END) AS reads,
         COUNT(DISTINCT CASE WHEN l.is_write = 1 THEN q.statement_id END) AS writes,
         COUNT(DISTINCT q.who) AS people, CAST(MAX(l.event_time) AS STRING) AS last_access
  FROM qw q JOIN lin l ON l.statement_id = q.statement_id
  GROUP BY 1, 2, 3, 4
)
SELECT * FROM per
QUALIFY ROW_NUMBER() OVER (PARTITION BY window_days, workspace_id, warehouse_id ORDER BY reads + writes DESC, table_full_name) <= {TABLES_MAX}
"""


def slow_sql(end: dt.date, windows: list[int]) -> str:
    return f"""
WITH {_qw(end, windows)}
SELECT window_days, workspace_id, warehouse_id, statement_id, who AS identity, CAST(start_time AS STRING) AS start_time, total_ms,
       queued_ms, execution_status AS status, statement_type, left(regexp_replace(statement_text, '\\\\s+', ' '), {TEXT_CHARS}) AS text
FROM qw
QUALIFY ROW_NUMBER() OVER (PARTITION BY window_days, workspace_id, warehouse_id ORDER BY total_ms DESC, statement_id) <= {SLOW_MAX}
"""


def _rows(cur, sql: str) -> list[dict]:
    cur.execute(sql)
    names = [d[0] for d in cur.description or []]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _write(folder: Path, name: str, rows: list[dict], schema: pa.Schema) -> None:
    clean = [{f.name: (None if r.get(f.name) is None else
                       int(r[f.name]) if pa.types.is_integer(f.type) else
                       float(r[f.name]) if pa.types.is_floating(f.type) else str(r[f.name])) for f in schema}
             for r in rows]
    pq.write_table(pa.Table.from_pylist(clean, schema=schema), str(folder / name), compression="snappy")


def export(connect_fn: Callable, out_dir: Path, *, end: dt.date, windows: list[int],
           redact: Callable[[str], str] = str, mask: Callable[[str], str] | None = None) -> dict:
    """Run every statement on one connection; each failing alone is reported, not raised."""
    t0 = time.perf_counter()
    try:
        connected = connect_fn()
    except Exception as exc:  # noqa: BLE001 -- a side table; the export goes on without it
        return {"exported": False, "error": redact(str(exc))}
    folder = out_dir / "warehouse"
    folder.mkdir(parents=True, exist_ok=True)
    days = max(windows)
    info: dict = {"exported": True, "days": days, "windows": sorted(set(windows)), "end": end.isoformat()}
    try:
        cur = connected.conn.cursor()
        for key, sql, name, schema in (
            ("daily", daily_sql(end, days), DAILY_FILE, DAILY_SCHEMA),
            ("users", users_sql(end, windows), USERS_FILE, USERS_SCHEMA),
            ("tables", tables_sql(end, windows), TABLES_FILE, TABLES_SCHEMA),
            ("slow", slow_sql(end, windows), SLOW_FILE, SLOW_SCHEMA),
        ):
            try:
                rows = _rows(cur, sql)
                if mask is not None and "identity" in schema.names:
                    rows = [{**r, "identity": None if r.get("identity") is None else mask(str(r["identity"]))} for r in rows]
                _write(folder, name, rows, schema)
                info[key] = {"rows": len(rows), "error": None}
            except Exception as exc:  # noqa: BLE001 -- e.g. lineage without statement_id: the rest still counts
                info[key] = {"rows": 0, "error": (redact(str(exc)).splitlines() or [""])[0][:300]}
    finally:
        try:
            connected.conn.close()
        except Exception:  # noqa: BLE001
            pass
    info["seconds"] = round(time.perf_counter() - t0, 1)
    return info
