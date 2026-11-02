"""tools/workspace_activity.py

Who is active in each workspace, what they read and write, and which workspace each catalog
belongs to, for Governance > Workspaces. Plain statements, not checks:
- workspace_activity: people, service principals and queries per workspace and window;
- catalog_workspace: each catalog's lineage events per workspace (its home workspace is the one
  that reads and writes it most);
- table_access: per window, workspace and table, last read and write, reads, writes, readers and writers;
- user_access: per workspace and identity, last seen, logins, queries, tables read and written;
- user_tables: per workspace and identity, the 50 tables it used most and when;
- user_last_seen: per workspace and identity, its last audit event and last login over a year,
  so people who stopped coming show up as inactive;
- volume_access: per workspace, volume and identity, reads and writes of files under /Volumes from
  the audit log's file-system events;
- table_daily: per table and day, reads, writes and readers (lineage);
- table_maintenance: predictive optimization runs per table (compaction, vacuum, clustering);
- sensitive_columns: columns that look sensitive (a tag, data classification or the column name),
  and whether a column mask covers them. Names and types only, never values.

Reads and writes count distinct events: one query reading A and writing B and C leaves two lineage
rows for A, one read.

Called by tools/export_direct_results.py after the checks; writes usage/<name>.parquet.
"""
from __future__ import annotations

import datetime as dt
import time
from pathlib import Path
from typing import Callable

import pyarrow as pa
import pyarrow.parquet as pq

ACTIVITY_FILE = "workspace_activity.parquet"
CATALOG_FILE = "catalog_workspace.parquet"
# a service principal acts under its application id, a person under an email
_UUID = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

ACTIVITY_SCHEMA = pa.schema([
    ("window_days", pa.int64()), ("workspace_id", pa.string()), ("active_people", pa.int64()),
    ("active_service_principals", pa.int64()), ("queries", pa.int64()), ("query_identities", pa.int64()),
    ("last_activity", pa.string()), ("active_days", pa.int64()),
])
CATALOG_SCHEMA = pa.schema([("catalog", pa.string()), ("workspace_id", pa.string()), ("events", pa.int64())])
TABLE_FILE, USER_FILE, USER_TABLES_FILE = "table_access.parquet", "user_access.parquet", "user_tables.parquet"
TABLE_SCHEMA = pa.schema([
    ("window_days", pa.int64()), ("workspace_id", pa.string()), ("table_full_name", pa.string()), ("last_read", pa.string()),
    ("last_write", pa.string()), ("reads", pa.int64()), ("writes", pa.int64()), ("readers", pa.int64()),
    ("writers", pa.int64()), ("reader_names", pa.string()), ("writer_names", pa.string()),
])
NAMES_SHOWN = 3  # reader_names, writer_names: the first few, alphabetically
USER_SCHEMA = pa.schema([
    ("workspace_id", pa.string()), ("identity", pa.string()), ("last_seen", pa.string()), ("logins", pa.int64()),
    ("queries", pa.int64()), ("last_query", pa.string()), ("tables_read", pa.int64()), ("tables_written", pa.int64()),
])
USER_TABLES_SCHEMA = pa.schema([
    ("workspace_id", pa.string()), ("identity", pa.string()), ("table_full_name", pa.string()),
    ("reads", pa.int64()), ("writes", pa.int64()), ("last_access", pa.string()), ("via", pa.string()),
])
USER_TABLES_MAX = 50  # per workspace and identity
VOLUME_ACCESS_FILE = "volume_access.parquet"
VOLUME_ACCESS_SCHEMA = pa.schema([
    ("workspace_id", pa.string()), ("volume_full_name", pa.string()), ("identity", pa.string()),
    ("reads", pa.int64()), ("writes", pa.int64()), ("last_read", pa.string()), ("last_write", pa.string()),
])
TABLE_DAILY_FILE = "table_daily.parquet"
TABLE_DAILY_SCHEMA = pa.schema([
    ("table_full_name", pa.string()), ("day", pa.string()), ("reads", pa.int64()), ("writes", pa.int64()),
    ("readers", pa.int64()), ("writers", pa.int64()),
])
MAINTENANCE_FILE = "table_maintenance.parquet"
MAINTENANCE_SCHEMA = pa.schema([
    ("table_full_name", pa.string()), ("start_time", pa.string()), ("operation_type", pa.string()),
    ("operation_status", pa.string()), ("usage_quantity", pa.float64()), ("usage_unit", pa.string()),
])
LAST_SEEN_FILE = "user_last_seen.parquet"
LAST_SEEN_DAYS = 365  # the audit log's retention
LAST_SEEN_SCHEMA = pa.schema([
    ("workspace_id", pa.string()), ("identity", pa.string()), ("last_seen", pa.string()), ("last_login", pa.string()),
])
SENSITIVE_FILE = "sensitive_columns.parquet"
SENSITIVE_SCHEMA = pa.schema([
    ("table_full_name", pa.string()), ("column_name", pa.string()), ("pii_type", pa.string()),
    ("method", pa.string()), ("masked", pa.bool_()),
])
# a column tag naming it sensitive; its value (or the tag name) is the type
SENSITIVE_TAGS = "^(pii|sensitive|gdpr|class[.])"
# column name pattern -> type, first match wins (IP before postal address: ip_address)
SENSITIVE_NAMES = (
    ("e_?mail", "Email"),
    ("phone|mobile|msisdn", "Phone number"),
    ("iban|(^|_)bic($|_)|account_?(no|number)", "IBAN / bank account"),
    ("ssn|(^|_)ahv|passport|national_?id", "National ID"),
    ("(^|_)ip(_?address)?($|_)", "IP address"),
    ("address|street|(^|_)zip|postal", "Postal address"),
    ("birth|(^|_)dob($|_)", "Date of birth"),
    ("card_?(no|number|num)|(^|_)pan$", "Payment card"),
)


def activity_sql(end: dt.date, windows: list[int]) -> str:
    """Per workspace and window: people and service principals seen (audit or queries), queries
    and the identities behind them, and the last day with any activity. `end` is left out."""
    days = max(windows)
    e = f"DATE'{end.isoformat()}'"
    w = ", ".join(str(int(x)) for x in sorted(set(windows)))
    return f"""
WITH w AS (SELECT explode(array({w})) AS days),
aud AS (
  SELECT CAST(workspace_id AS STRING) AS workspace_id,
         lower(COALESCE(user_identity.email, user_identity.subject_name)) AS who, MAX(event_date) AS last_day
  FROM system.access.audit
  WHERE event_date >= date_sub({e}, {days}) AND event_date < {e}
    AND workspace_id IS NOT NULL AND workspace_id <> 0
  GROUP BY 1, 2
),
qd AS (
  SELECT CAST(workspace_id AS STRING) AS workspace_id, lower(executed_by) AS who,
         CAST(start_time AS DATE) AS day, COUNT(*) AS queries
  FROM system.query.history
  WHERE start_time >= date_sub({e}, {days}) AND start_time < {e}
  GROUP BY 1, 2, 3
),
seen AS (
  SELECT workspace_id, who, MAX(last_day) AS last_day
  FROM (SELECT workspace_id, who, last_day FROM aud
        UNION ALL SELECT workspace_id, who, MAX(day) FROM qd GROUP BY workspace_id, who) x
  GROUP BY workspace_id, who
),
people AS (
  SELECT w.days, s.workspace_id,
         COUNT(DISTINCT CASE WHEN s.who LIKE '%@%' THEN s.who END) AS active_people,
         COUNT(DISTINCT CASE WHEN s.who RLIKE '{_UUID}' THEN s.who END) AS active_service_principals
  FROM seen s JOIN w ON s.last_day >= date_sub({e}, w.days)
  GROUP BY 1, 2
),
q AS (
  SELECT w.days, d.workspace_id, SUM(d.queries) AS queries, COUNT(DISTINCT d.who) AS query_identities
  FROM qd d JOIN w ON d.day >= date_sub({e}, w.days)
  GROUP BY 1, 2
),
last AS (SELECT workspace_id, MAX(last_day) AS last_activity FROM seen GROUP BY 1),
-- days with any audit event or query by a person or service principal
ad AS (
  SELECT DISTINCT CAST(workspace_id AS STRING) AS workspace_id, event_date AS day
  FROM system.access.audit
  WHERE event_date >= date_sub({e}, {days}) AND event_date < {e} AND workspace_id IS NOT NULL AND workspace_id <> 0
    AND (lower(COALESCE(user_identity.email, user_identity.subject_name)) LIKE '%@%'
         OR lower(COALESCE(user_identity.email, user_identity.subject_name)) RLIKE '{_UUID}')
  UNION
  SELECT DISTINCT workspace_id, day FROM qd
),
dd AS (
  SELECT w.days, a.workspace_id, COUNT(DISTINCT a.day) AS active_days
  FROM ad a JOIN w ON a.day >= date_sub({e}, w.days)
  GROUP BY 1, 2
)
SELECT w.days AS window_days, l.workspace_id,
       COALESCE(p.active_people, 0) AS active_people,
       COALESCE(p.active_service_principals, 0) AS active_service_principals,
       COALESCE(q.queries, 0) AS queries, COALESCE(q.query_identities, 0) AS query_identities,
       CAST(l.last_activity AS STRING) AS last_activity, COALESCE(dd.active_days, 0) AS active_days
FROM last l CROSS JOIN w
LEFT JOIN people p ON p.days = w.days AND p.workspace_id = l.workspace_id
LEFT JOIN q ON q.days = w.days AND q.workspace_id = l.workspace_id
LEFT JOIN dd ON dd.days = w.days AND dd.workspace_id = l.workspace_id
"""


def catalog_sql(end: dt.date, days: int) -> str:
    """Lineage events (reads and writes) per catalog and workspace."""
    e = f"DATE'{end.isoformat()}'"
    return f"""
SELECT catalog, CAST(workspace_id AS STRING) AS workspace_id, COUNT(*) AS events
FROM (
  SELECT workspace_id, source_table_catalog AS catalog FROM system.access.table_lineage
  WHERE event_date >= date_sub({e}, {days}) AND event_date < {e}
  UNION ALL
  SELECT workspace_id, target_table_catalog FROM system.access.table_lineage
  WHERE event_date >= date_sub({e}, {days}) AND event_date < {e}
) x
WHERE catalog IS NOT NULL AND workspace_id IS NOT NULL
GROUP BY 1, 2
"""


def _lineage(e: str, days: int) -> str:
    """Every read (source) and write (target) of a table in the window, with who, when, through what
    (entity_type) and an event key k: the same run, identity and time is one event."""
    key = ("concat_ws('|', COALESCE(CAST(entity_run_id AS STRING), CAST(entity_id AS STRING), ''), "
           "COALESCE(created_by, ''), CAST(event_time AS STRING))")
    return f"""lin AS (
  SELECT CAST(workspace_id AS STRING) AS workspace_id, source_table_full_name AS table_full_name,
         1 AS is_read, 0 AS is_write, event_time, lower(created_by) AS who, entity_type, {key} AS k
  FROM system.access.table_lineage
  WHERE event_date >= date_sub({e}, {days}) AND event_date < {e} AND source_table_full_name IS NOT NULL
  UNION ALL
  SELECT CAST(workspace_id AS STRING), target_table_full_name, 0, 1, event_time, lower(created_by), entity_type, {key}
  FROM system.access.table_lineage
  WHERE event_date >= date_sub({e}, {days}) AND event_date < {e} AND target_table_full_name IS NOT NULL
)"""


READ_LOOKBACK_DAYS = 365  # lineage's retention: last read and write look back this far


def table_access_sql(end: dt.date, windows: list[int]) -> str:
    """Per table and window: last read and write over the lineage's year; reads, writes, readers and
    writers in the window, and the first few of each by name."""
    e = f"DATE'{end.isoformat()}'"
    ws = ", ".join(str(int(x)) for x in sorted(set(windows)))
    since = f"date_sub({e}, w.days)"
    return f"""
WITH {_lineage(e, max(max(windows), READ_LOOKBACK_DAYS))},
w AS (SELECT explode(array({ws})) AS days),
last AS (
  SELECT workspace_id, table_full_name,
         CAST(MAX(CASE WHEN is_read = 1 THEN event_time END) AS STRING) AS last_read,
         CAST(MAX(CASE WHEN is_write = 1 THEN event_time END) AS STRING) AS last_write
  FROM lin WHERE workspace_id IS NOT NULL GROUP BY 1, 2
),
win AS (
  SELECT w.days, l.workspace_id, l.table_full_name,
         COUNT(DISTINCT CASE WHEN l.is_read = 1 THEN l.k END) AS reads,
         COUNT(DISTINCT CASE WHEN l.is_write = 1 THEN l.k END) AS writes,
         COUNT(DISTINCT CASE WHEN l.is_read = 1 THEN l.who END) AS readers,
         COUNT(DISTINCT CASE WHEN l.is_write = 1 THEN l.who END) AS writers,
         concat_ws(', ', slice(array_sort(collect_set(CASE WHEN l.is_read = 1 THEN l.who END)), 1, {NAMES_SHOWN})) AS reader_names,
         concat_ws(', ', slice(array_sort(collect_set(CASE WHEN l.is_write = 1 THEN l.who END)), 1, {NAMES_SHOWN})) AS writer_names
  FROM lin l JOIN w ON l.event_time >= {since}
  WHERE l.workspace_id IS NOT NULL
  GROUP BY 1, 2, 3
)
SELECT w.days AS window_days, a.workspace_id, a.table_full_name, a.last_read, a.last_write,
       COALESCE(n.reads, 0) AS reads, COALESCE(n.writes, 0) AS writes, COALESCE(n.readers, 0) AS readers,
       COALESCE(n.writers, 0) AS writers, n.reader_names, n.writer_names
FROM last a CROSS JOIN w
LEFT JOIN win n ON n.days = w.days AND n.workspace_id = a.workspace_id AND n.table_full_name = a.table_full_name
"""


def user_access_sql(end: dt.date, days: int) -> str:
    e = f"DATE'{end.isoformat()}'"
    return f"""
WITH {_lineage(e, days)},
a AS (
  SELECT CAST(workspace_id AS STRING) AS workspace_id,
         lower(COALESCE(user_identity.email, user_identity.subject_name)) AS who, MAX(event_time) AS last_event,
         COUNT_IF(service_name = 'accounts' AND action_name RLIKE '(?i)login' AND response.status_code = 200) AS logins
  FROM system.access.audit
  WHERE event_date >= date_sub({e}, {days}) AND event_date < {e} AND workspace_id IS NOT NULL AND workspace_id <> 0
  GROUP BY 1, 2
),
q AS (
  SELECT CAST(workspace_id AS STRING) AS workspace_id, lower(executed_by) AS who, COUNT(*) AS queries,
         MAX(start_time) AS last_query
  FROM system.query.history
  WHERE start_time >= date_sub({e}, {days}) AND start_time < {e}
  GROUP BY 1, 2
),
l AS (
  SELECT workspace_id, who, COUNT(DISTINCT CASE WHEN is_read = 1 THEN table_full_name END) AS tables_read,
         COUNT(DISTINCT CASE WHEN is_write = 1 THEN table_full_name END) AS tables_written, MAX(event_time) AS last_table
  FROM lin GROUP BY 1, 2
),
k AS (
  SELECT workspace_id, who FROM a UNION SELECT workspace_id, who FROM q UNION SELECT workspace_id, who FROM l
)
SELECT k.workspace_id, k.who AS identity,
       CAST(GREATEST(a.last_event, q.last_query, l.last_table) AS STRING) AS last_seen,
       COALESCE(a.logins, 0) AS logins, COALESCE(q.queries, 0) AS queries, CAST(q.last_query AS STRING) AS last_query,
       COALESCE(l.tables_read, 0) AS tables_read, COALESCE(l.tables_written, 0) AS tables_written
FROM k
LEFT JOIN a ON a.workspace_id = k.workspace_id AND a.who = k.who
LEFT JOIN q ON q.workspace_id = k.workspace_id AND q.who = k.who
LEFT JOIN l ON l.workspace_id = k.workspace_id AND l.who = k.who
WHERE k.workspace_id IS NOT NULL AND k.who IS NOT NULL
"""


def user_tables_sql(end: dt.date, days: int) -> str:
    e = f"DATE'{end.isoformat()}'"
    return f"""
WITH {_lineage(e, days)},
per AS (
  SELECT workspace_id, who, table_full_name, COUNT(DISTINCT CASE WHEN is_read = 1 THEN k END) AS reads,
         COUNT(DISTINCT CASE WHEN is_write = 1 THEN k END) AS writes, MAX(event_time) AS last_access,
         concat_ws(', ', array_sort(collect_set(entity_type))) AS via
  FROM lin WHERE workspace_id IS NOT NULL AND who IS NOT NULL
  GROUP BY 1, 2, 3
)
SELECT workspace_id, who AS identity, table_full_name, reads, writes, CAST(last_access AS STRING) AS last_access, via
FROM per
QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, who ORDER BY reads + writes DESC, table_full_name) <= {USER_TABLES_MAX}
"""


def volume_access_sql(end: dt.date, days: int) -> str:
    """Files read and written under /Volumes per workspace, volume and identity (file-system events)."""
    e = f"DATE'{end.isoformat()}'"
    return f"""
WITH ev AS (
  SELECT CAST(workspace_id AS STRING) AS workspace_id, event_time,
         lower(COALESCE(user_identity.email, user_identity.subject_name)) AS identity,
         COALESCE(request_params['path'], request_params['file_path'], request_params['directory_path']) AS path,
         action_name RLIKE '(?i)put|upload|create|delete|write|mkdir' AS is_write
  FROM system.access.audit
  WHERE event_date >= date_sub({e}, {days}) AND event_date < {e}
    AND service_name = 'filesystem' AND response.status_code = 200 AND workspace_id IS NOT NULL
)
SELECT workspace_id, regexp_extract(path, '^/Volumes/([^/]+)/([^/]+)/([^/]+)', 1) || '.' ||
         regexp_extract(path, '^/Volumes/([^/]+)/([^/]+)/([^/]+)', 2) || '.' ||
         regexp_extract(path, '^/Volumes/([^/]+)/([^/]+)/([^/]+)', 3) AS volume_full_name,
       identity, COUNT_IF(NOT is_write) AS reads, COUNT_IF(is_write) AS writes,
       CAST(MAX(CASE WHEN NOT is_write THEN event_time END) AS STRING) AS last_read,
       CAST(MAX(CASE WHEN is_write THEN event_time END) AS STRING) AS last_write
FROM ev
WHERE path RLIKE '^/Volumes/[^/]+/[^/]+/[^/]+'
GROUP BY 1, 2, 3
"""


def table_daily_sql(end: dt.date, days: int) -> str:
    """Reads and writes per table and day, and how many identities did each."""
    e = f"DATE'{end.isoformat()}'"
    return f"""
WITH {_lineage(e, days)}
SELECT table_full_name, CAST(CAST(event_time AS DATE) AS STRING) AS day,
       COUNT(DISTINCT CASE WHEN is_read = 1 THEN k END) AS reads, COUNT(DISTINCT CASE WHEN is_write = 1 THEN k END) AS writes,
       COUNT(DISTINCT CASE WHEN is_read = 1 THEN who END) AS readers,
       COUNT(DISTINCT CASE WHEN is_write = 1 THEN who END) AS writers
FROM lin
GROUP BY 1, 2
"""


def maintenance_sql(end: dt.date, days: int) -> str:
    """Predictive optimization's runs per table in the window."""
    e = f"DATE'{end.isoformat()}'"
    return f"""
SELECT catalog_name || '.' || schema_name || '.' || table_name AS table_full_name,
       CAST(start_time AS STRING) AS start_time, operation_type, operation_status,
       CAST(usage_quantity AS DOUBLE) AS usage_quantity, usage_unit
FROM system.storage.predictive_optimization_operations_history
WHERE start_time >= date_sub({e}, {days}) AND start_time < {e} AND table_name IS NOT NULL
"""


def last_seen_sql(end: dt.date) -> str:
    """Each identity's last audit event and last successful login per workspace, over a year."""
    e = f"DATE'{end.isoformat()}'"
    return f"""
SELECT CAST(workspace_id AS STRING) AS workspace_id,
       lower(COALESCE(user_identity.email, user_identity.subject_name)) AS identity,
       CAST(MAX(event_date) AS STRING) AS last_seen,
       CAST(MAX(CASE WHEN service_name = 'accounts' AND action_name RLIKE '(?i)login'
                      AND response.status_code = 200 THEN event_date END) AS STRING) AS last_login
FROM system.access.audit
WHERE event_date >= date_sub({e}, {LAST_SEEN_DAYS}) AND event_date < {e}
  AND workspace_id IS NOT NULL AND workspace_id <> 0
  AND COALESCE(user_identity.email, user_identity.subject_name) IS NOT NULL
GROUP BY 1, 2
"""


def sensitive_columns_sql(classification: bool = True) -> str:
    """One row per sensitive-looking column: from a tag, data classification (HIGH confidence) or the
    column name, in that order of trust, and whether a column mask covers it."""
    names = "\n".join(f"      WHEN lower(column_name) RLIKE '{rx}' THEN '{kind}'" for rx, kind in SENSITIVE_NAMES)
    dc = """
  UNION ALL
  SELECT catalog_name, schema_name, table_name, column_name, class_tag, 'classification', 2
  FROM system.data_classification.results WHERE confidence = 'HIGH' AND class_tag IS NOT NULL""" if classification else ""
    return f"""
WITH found AS (
  SELECT catalog_name AS c, schema_name AS s, table_name AS t, column_name,
         COALESCE(NULLIF(tag_value, ''), tag_name) AS pii_type, 'tag' AS method, 1 AS rank
  FROM system.information_schema.column_tags WHERE lower(tag_name) RLIKE '{SENSITIVE_TAGS}'{dc}
  UNION ALL
  SELECT table_catalog, table_schema, table_name, column_name,
    CASE
{names}
    END, 'name', 3
  FROM system.information_schema.columns
  WHERE table_schema <> 'information_schema' AND table_catalog NOT IN ('system', 'samples', '__databricks_internal')
)
SELECT f.c || '.' || f.s || '.' || f.t AS table_full_name, f.column_name, f.pii_type, f.method,
       cm.mask_name IS NOT NULL AS masked
FROM found f
LEFT JOIN system.information_schema.column_masks cm
  ON cm.table_catalog = f.c AND cm.table_schema = f.s AND cm.table_name = f.t AND cm.column_name = f.column_name
WHERE f.pii_type IS NOT NULL
QUALIFY ROW_NUMBER() OVER (PARTITION BY f.c, f.s, f.t, f.column_name ORDER BY f.rank) = 1
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
    """Run every statement on one connection; each failing alone is reported, not raised. `mask`
    masks the identity column when privacy masking is on."""
    t0 = time.perf_counter()
    try:
        connected = connect_fn()
    except Exception as exc:  # noqa: BLE001 -- a side table; the export goes on without it
        return {"exported": False, "error": redact(str(exc))}
    folder = out_dir / "usage"
    folder.mkdir(parents=True, exist_ok=True)
    info: dict = {"exported": True, "windows": sorted(set(windows)), "end": end.isoformat(),
                  "read_lookback_days": max(max(windows), READ_LOOKBACK_DAYS)}
    try:
        cur = connected.conn.cursor()
        for key, sql, name, schema in (
            ("activity", activity_sql(end, windows), ACTIVITY_FILE, ACTIVITY_SCHEMA),
            ("catalogs", catalog_sql(end, max(windows)), CATALOG_FILE, CATALOG_SCHEMA),
            ("tables", table_access_sql(end, windows), TABLE_FILE, TABLE_SCHEMA),
            ("users", user_access_sql(end, max(windows)), USER_FILE, USER_SCHEMA),
            ("user_tables", user_tables_sql(end, max(windows)), USER_TABLES_FILE, USER_TABLES_SCHEMA),
            ("last_seen", last_seen_sql(end), LAST_SEEN_FILE, LAST_SEEN_SCHEMA),
            ("volume_access", volume_access_sql(end, max(windows)), VOLUME_ACCESS_FILE, VOLUME_ACCESS_SCHEMA),
            ("table_daily", table_daily_sql(end, max(windows)), TABLE_DAILY_FILE, TABLE_DAILY_SCHEMA),
            ("table_maintenance", maintenance_sql(end, max(windows)), MAINTENANCE_FILE, MAINTENANCE_SCHEMA),
            ("sensitive_columns", sensitive_columns_sql(), SENSITIVE_FILE, SENSITIVE_SCHEMA),
        ):
            try:
                try:
                    rows = _rows(cur, sql)
                except Exception:  # noqa: BLE001 -- no data classification in this account: tags and names still count
                    if key != "sensitive_columns":
                        raise
                    rows = _rows(cur, sensitive_columns_sql(classification=False))
                if mask is not None and "identity" in schema.names:
                    rows = [{**r, "identity": None if r.get("identity") is None else mask(str(r["identity"]))}
                            for r in rows]
                _write(folder, name, rows, schema)
                info[key] = {"rows": len(rows), "error": None}
            except Exception as exc:  # noqa: BLE001 -- e.g. no access to lineage: activity still counts
                info[key] = {"rows": 0, "error": (redact(str(exc)).splitlines() or [""])[0][:300]}
    finally:
        try:
            connected.conn.close()
        except Exception:  # noqa: BLE001
            pass
    info["seconds"] = round(time.perf_counter() - t0, 1)
    return info
