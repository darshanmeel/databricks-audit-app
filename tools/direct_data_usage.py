"""tools/direct_data_usage.py

Data usage: the app's Data usage section as a dashboard. Pages Workspaces, Tables, Volumes, People.
Every table, view and volume falls in one usage class, and each workspace gets a complexity and a
usage index and a signal, by the rules in app/core/usage.py.

A catalog's home workspace is the one that reads and writes it most (lineage), as in the app. Sizes
and volume files can't be read with SQL: a job writes them into <sizes schema>.table_sizes,
table_history and volume_sizes (see collect_sizes_notebook).
"""
from __future__ import annotations

from app.core import usage as rules
from tools import build_databricks_dashboards as bdd
from tools.build_direct_dashboards import (
    END, START, Dataset, _page, bar, counter, dataset_json, field_filter, header, place, table, time_window,
)
from tools.workspace_activity import SENSITIVE_NAMES, SENSITIVE_TAGS

TITLE = "Data usage"
P = "wdata"

SIZES_SCHEMA = bdd.app_config.dashboards()["sizes_schema"]  # settings dashboards, or --sizes-schema
SIZES_TABLES = bdd.app_config.dashboards()["sizes_tables"]
LIST_FROM = bdd.app_config.table_sizes()  # tables_from, volumes_from: where the sizes job lists them
HOME_DAYS = 90  # lineage days that decide a catalog's home workspace
INACTIVE_DAYS = rules.IDLE_DAYS
READ_LOOKBACK_DAYS = 365  # lineage's retention: how far back a table's last read is looked for
WRITE_OPS = "WRITE|MERGE|UPDATE|DELETE|STREAMING UPDATE|CREATE OR REPLACE|COPY INTO|TRUNCATE|RESTORE"
MAINT_OPS = "OPTIMIZE|VACUUM|ANALYZE|REORG"
SKIP_CATALOGS = ("system", "samples", "__databricks_internal")

HISTORY_VERSIONS = 100  # per table, newest first

# app/core/usage.py's weights, by this dataset's column names
COMPLEXITY = {"views": rules.COMPLEXITY_WEIGHTS["views"], "view_columns": rules.COMPLEXITY_WEIGHTS["view_columns"],
              "tables": rules.COMPLEXITY_WEIGHTS["tables"], "table_columns": rules.COMPLEXITY_WEIGHTS["table_columns"],
              "volume_files": rules.COMPLEXITY_WEIGHTS["volume_files"], "table_bytes": rules.COMPLEXITY_WEIGHTS["size_in_bytes"]}
USAGE = dict(rules.USAGE_WEIGHTS)
CLASS_LABELS = {key: label for key, label, _meaning in rules.CLASSES}
WNR, IDLE, COOLING, ACTIVE = (CLASS_LABELS[k] for k in ("written_never_read", "idle_90d", "cooling", "active"))
CLASS_COLORS = [(WNR, "#C2362B"), (IDLE, "#8E8B84"), (COOLING, "#E0A93B"), (ACTIVE, "#2456A6")]
BAND_COLORS = [("Under 30 days", "#2456A6"), ("30-90 days", "#E0A93B"), ("90-180 days", "#D27A2C"), ("180+ days", "#C2362B")]

_UUID = "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'"
_SKIP = ", ".join(f"'{c}'" for c in SKIP_CATALOGS)


def _sized(table: str) -> str:
    return f"{SIZES_SCHEMA}.{SIZES_TABLES[table]}"


def _kind(col: str) -> str:
    return f"CASE WHEN {col} RLIKE {_UUID} THEN 'Service principal' WHEN {col} LIKE '%@%' THEN 'Person' ELSE 'Other' END"


def _homes() -> str:
    """Every catalog with its home workspace (most lineage reads and writes), and workspace names."""
    return f"""wsl AS (
  SELECT CAST(workspace_id AS STRING) AS workspace_id, workspace_name, workspace_url
  FROM system.access.workspaces_latest
),
seen AS (
  SELECT catalog, CAST(workspace_id AS STRING) AS workspace_id, COUNT(*) AS events FROM (
    SELECT workspace_id, source_table_catalog AS catalog FROM system.access.table_lineage
    WHERE event_date >= date_sub({END}, {HOME_DAYS}) AND event_date < {END}
    UNION ALL
    SELECT workspace_id, target_table_catalog FROM system.access.table_lineage
    WHERE event_date >= date_sub({END}, {HOME_DAYS}) AND event_date < {END}
  ) WHERE catalog IS NOT NULL GROUP BY 1, 2
),
home AS (
  SELECT c.catalog_name AS catalog, max_by(s.workspace_id, s.events) AS workspace_id
  FROM system.information_schema.catalogs c LEFT JOIN seen s ON s.catalog = c.catalog_name
  WHERE c.catalog_name NOT IN ({_SKIP})
  GROUP BY c.catalog_name
),
place AS (
  -- a catalog without lineage in {HOME_DAYS} days keeps its rows, under No home workspace
  SELECT h.catalog, h.workspace_id, w.workspace_url,
         COALESCE(w.workspace_name, h.workspace_id, 'No home workspace') AS workspace
  FROM home h LEFT JOIN wsl w ON w.workspace_id = h.workspace_id
)"""


def _sensitive() -> str:
    """Sensitive-looking columns: a tag, else the column name (the app's patterns); masked or not.
    Data classification results are left out: the table is missing where it is off."""
    names = "\n".join(f"         WHEN lower(column_name) RLIKE '{rx}' THEN '{k}'" for rx, k in SENSITIVE_NAMES)
    return f"""found AS (
  SELECT catalog_name AS c, schema_name AS s, table_name AS t, column_name,
         COALESCE(NULLIF(tag_value, ''), tag_name) AS pii_type, 1 AS rnk
  FROM system.information_schema.column_tags
  WHERE lower(tag_name) RLIKE '{SENSITIVE_TAGS}' AND catalog_name NOT IN ({_SKIP})
  UNION ALL
  SELECT table_catalog, table_schema, table_name, column_name,
         CASE
{names}
         END, 2
  FROM system.information_schema.columns
  WHERE table_catalog NOT IN ({_SKIP}) AND table_schema <> 'information_schema'
),
sens_col AS (
  SELECT f.c || '.' || f.s || '.' || f.t AS full_name, f.column_name, f.pii_type, cm.mask_name IS NOT NULL AS masked
  FROM found f
  LEFT JOIN system.information_schema.column_masks cm
    ON cm.table_catalog = f.c AND cm.table_schema = f.s AND cm.table_name = f.t AND cm.column_name = f.column_name
  WHERE f.pii_type IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY f.c, f.s, f.t, f.column_name ORDER BY f.rnk) = 1
),
sens AS (
  SELECT full_name, COUNT(*) AS sensitive_columns, COUNT_IF(NOT masked) AS unmasked_columns,
         concat_ws(', ', sort_array(collect_set(pii_type))) AS sensitive_types
  FROM sens_col GROUP BY 1
)"""


def _class(reads: str, writes: str, last_read: str, last_write: str, created: str) -> str:
    """app/core/usage.py's usage_class: the first rule that holds wins."""
    last = f"GREATEST({last_read}, {last_write})"
    return f"""CASE WHEN {writes} > 0 AND {reads} = 0 THEN '{WNR}'
            WHEN {reads} = 0 AND {writes} = 0 AND {last_read} IS NULL AND {last_write} IS NULL
                 AND {created} >= date_sub({END}, {rules.NEW_DAYS}) THEN '{ACTIVE}'
            WHEN {last} IS NULL OR datediff({END}, CAST({last} AS DATE)) >= {rules.IDLE_DAYS} THEN '{IDLE}'
            WHEN {reads} = 0 THEN '{COOLING}'
            ELSE '{ACTIVE}' END"""


# the table in Catalog Explorer of its home workspace; empty without one
_TABLE_LINK = ("CASE WHEN workspace_url IS NOT NULL THEN concat('https://', regexp_replace(regexp_replace(workspace_url, "
               "'^https?://', ''), '/.*$', ''), '/explore/data/', catalog, '/', schema_name, '/', name) END")
_RANK = f"CASE usage_class WHEN '{WNR}' THEN 1 WHEN '{IDLE}' THEN 2 WHEN '{COOLING}' THEN 3 ELSE 4 END"
_UNREAD = f"('{WNR}', '{IDLE}')"


def _classified() -> str:
    """Every table and view with its home workspace, size, reads and writes, class and sensitive
    columns (step c): one logic for Workspaces, Tables, Clean up first and Largest tables."""
    return f"""{_homes()},
t AS (
  SELECT table_catalog AS catalog, table_schema AS sch, table_name, table_type, table_owner, created,
         table_catalog || '.' || table_schema || '.' || table_name AS full_name
  FROM system.information_schema.tables
  WHERE table_catalog NOT IN ({_SKIP}) AND table_schema <> 'information_schema'
),
lin AS (
  SELECT source_table_full_name AS full_name, 1 AS is_read, 0 AS is_write, event_time, lower(created_by) AS who
  FROM system.access.table_lineage
  WHERE event_date >= date_sub({END}, {READ_LOOKBACK_DAYS}) AND event_date < {END} AND source_table_full_name IS NOT NULL
  UNION ALL
  SELECT target_table_full_name, 0, 1, event_time, lower(created_by) FROM system.access.table_lineage
  WHERE event_date >= date_sub({END}, {READ_LOOKBACK_DAYS}) AND event_date < {END} AND target_table_full_name IS NOT NULL
),
used AS (
  -- counts in the Time Window; last read and write over the lineage's year
  SELECT full_name,
         SUM(CASE WHEN event_time >= {START} THEN is_read ELSE 0 END) AS reads,
         SUM(CASE WHEN event_time >= {START} THEN is_write ELSE 0 END) AS writes,
         COUNT(DISTINCT CASE WHEN is_read = 1 AND event_time >= {START} THEN who END) AS readers,
         COUNT(DISTINCT CASE WHEN is_write = 1 AND event_time >= {START} THEN who END) AS writers,
         MAX(CASE WHEN is_read = 1 THEN event_time END) AS last_read,
         MAX(CASE WHEN is_write = 1 THEN event_time END) AS last_write
  FROM lin GROUP BY 1
),
hist AS (
  SELECT catalog, schema, table_name,
         MAX(CASE WHEN operation RLIKE '{WRITE_OPS}' THEN timestamp END) AS last_change,
         max_by(operation, CASE WHEN operation RLIKE '{WRITE_OPS}' THEN timestamp END) AS last_change_op,
         MAX(CASE WHEN operation RLIKE '{MAINT_OPS}' THEN timestamp END) AS last_maintenance,
         SUM(CASE WHEN timestamp >= {START} THEN rows_changed ELSE 0 END) AS rows_changed,
         SUM(CASE WHEN timestamp >= {START} THEN bytes_written ELSE 0 END) AS bytes_written
  FROM {_sized('table_history')} GROUP BY 1, 2, 3
),
po AS (
  SELECT catalog_name AS catalog, schema_name AS schema, table_name, MAX(start_time) AS last_po
  FROM system.storage.predictive_optimization_operations_history
  WHERE start_time >= date_sub({END}, {READ_LOOKBACK_DAYS})
  GROUP BY 1, 2, 3
),
{_sensitive()},
j AS (
  SELECT p.workspace, p.workspace_id, p.workspace_url, t.catalog, t.sch AS schema_name, t.table_name AS name, t.full_name,
         t.table_type, t.table_owner, t.created,
         s.size_in_bytes, s.num_files, COALESCE(u.reads, 0) AS reads, COALESCE(u.writes, 0) AS writes,
         COALESCE(u.readers, 0) AS readers, COALESCE(u.writers, 0) AS writers, u.last_read,
         GREATEST(u.last_write, h.last_change) AS last_write, h.last_change_op,
         GREATEST(h.last_maintenance, po.last_po) AS last_maintenance,
         COALESCE(h.rows_changed, 0) AS rows_changed, COALESCE(h.bytes_written, 0) AS bytes_written,
         COALESCE(se.sensitive_columns, 0) AS sensitive_columns, COALESCE(se.unmasked_columns, 0) AS unmasked_columns,
         se.sensitive_types
  FROM t
  LEFT JOIN place p ON p.catalog = t.catalog
  LEFT JOIN {_sized('table_sizes')} s ON s.catalog = t.catalog AND s.schema = t.sch AND s.table_name = t.table_name
  LEFT JOIN used u ON u.full_name = t.full_name
  LEFT JOIN hist h ON h.catalog = t.catalog AND h.schema = t.sch AND h.table_name = t.table_name
  LEFT JOIN po ON po.catalog = t.catalog AND po.schema = t.sch AND po.table_name = t.table_name
  LEFT JOIN sens se ON se.full_name = t.full_name
),
c AS (
  SELECT *, {_class('reads', 'writes', 'last_read', 'last_write', 'created')} AS usage_class FROM j
)"""


_HOME_STEPS = {
    "wsl": "Workspace id to name and URL.",
    "seen": f"Lineage reads and writes per catalog and workspace over {HOME_DAYS} days.",
    "home": "Each catalog's home workspace: the one with the most of them.",
    "place": "Each catalog with its home workspace's name and URL.",
}
_CLASS_STEPS = {
    **_HOME_STEPS,
    "t": "Every table and view, without the system catalogs.",
    "lin": f"Every read (source) and write (target) of a table over {READ_LOOKBACK_DAYS} days, from lineage.",
    "used": f"Per table: reads, writes, readers and writers in the window; last read and write over {READ_LOOKBACK_DAYS} days.",
    "hist": "Per table, from the job's DESCRIBE HISTORY: last data change and its operation, last OPTIMIZE or "
            "VACUUM, rows changed and bytes written in the window.",
    "po": "Per table: the last predictive optimization run.",
    "found": "Sensitive-looking columns: a column tag (pii, sensitive, gdpr, class.*), else the column name.",
    "sens_col": "One row per sensitive column, the tag winning over the name, and whether a column mask covers it.",
    "sens": "Per table: sensitive columns, how many are unmasked, and their types.",
    "j": "Everything per table.",
    "c": f"Its usage class: {WNR} (writes, no read in the window), {IDLE} (no read or write in "
         f"{rules.IDLE_DAYS} days), {COOLING} (no read in the window), {ACTIVE} (read in the window, or created in the "
         f"last {rules.NEW_DAYS} days).",
}


def _scaled(col: str) -> str:
    return f"COALESCE(ln(1 + {col}) / NULLIF(MAX(ln(1 + {col})) OVER (), 0), 0)"


def workspaces() -> Dataset:
    window = f"datediff({END}, {START})"
    cplx = " + ".join(f"{w} * {_scaled(k)}" for k, w in COMPLEXITY.items())
    use = " + ".join(f"{w} * {_scaled(k)}" for k, w in USAGE.items())
    return Dataset(
        "wd_workspaces", "Workspaces: tables by class, what each holds and how much it is used",
        [], [],
        f"""{_classified()},
cls AS (
  -- tables (not views) by usage class
  SELECT workspace_id,
         COUNT_IF(usage_class = '{WNR}' AND table_type <> 'VIEW') AS tables_written_never_read,
         COUNT_IF(usage_class = '{IDLE}' AND table_type <> 'VIEW') AS tables_idle,
         COUNT_IF(usage_class = '{COOLING}' AND table_type <> 'VIEW') AS tables_cooling,
         COUNT_IF(usage_class = '{ACTIVE}' AND table_type <> 'VIEW') AS tables_active,
         SUM(CASE WHEN usage_class IN {_UNREAD} THEN size_in_bytes ELSE 0 END) AS unread_bytes,
         SUM(reads) AS reads, SUM(writes) AS writes, MAX(last_read) AS last_read, MAX(last_write) AS last_write,
         COUNT_IF(sensitive_columns > 0) AS sensitive_tables
  FROM c WHERE workspace_id IS NOT NULL GROUP BY 1
),
cols AS (
  SELECT table_catalog, table_schema, table_name, COUNT(*) AS n FROM system.information_schema.columns
  WHERE table_schema <> 'information_schema' GROUP BY 1, 2, 3
),
held AS (
  SELECT p.workspace_id, COUNT(DISTINCT p.catalog) AS catalogs,
         COUNT_IF(t.table_type <> 'VIEW') AS tables, COUNT_IF(t.table_type = 'VIEW') AS views,
         COALESCE(SUM(CASE WHEN t.table_type <> 'VIEW' THEN cl.n END), 0) AS table_columns,
         COALESCE(SUM(CASE WHEN t.table_type = 'VIEW' THEN cl.n END), 0) AS view_columns
  FROM place p
  LEFT JOIN system.information_schema.tables t ON t.table_catalog = p.catalog AND t.table_schema <> 'information_schema'
  LEFT JOIN cols cl ON cl.table_catalog = t.table_catalog AND cl.table_schema = t.table_schema AND cl.table_name = t.table_name
  WHERE p.workspace_id IS NOT NULL
  GROUP BY 1
),
vols AS (
  SELECT p.workspace_id, COUNT(*) AS volumes, COALESCE(SUM(vs.files), 0) AS volume_files,
         COALESCE(SUM(vs.size_in_bytes), 0) AS volume_bytes
  FROM system.information_schema.volumes v
  JOIN place p ON p.catalog = v.volume_catalog
  LEFT JOIN {_sized('volume_sizes')} vs
    ON vs.catalog = v.volume_catalog AND vs.schema = v.volume_schema AND vs.volume_name = v.volume_name
  WHERE p.workspace_id IS NOT NULL
  GROUP BY 1
),
sizes AS (
  SELECT p.workspace_id, SUM(s.size_in_bytes) AS table_bytes
  FROM {_sized('table_sizes')} s JOIN place p ON p.catalog = s.catalog
  WHERE p.workspace_id IS NOT NULL
  GROUP BY 1
),
ev AS (
  -- people and service principals seen in the window: an audit event or a query
  SELECT CAST(workspace_id AS STRING) AS workspace_id, lower(COALESCE(user_identity.email, user_identity.subject_name)) AS who,
         event_date AS d, event_time AS ts, 0 AS is_query
  FROM system.access.audit WHERE event_date >= {START} AND event_date < {END}
  UNION ALL
  SELECT CAST(workspace_id AS STRING), lower(executed_by), CAST(start_time AS DATE), start_time, 1
  FROM system.query.history WHERE start_time >= {START} AND start_time < {END}
),
act AS (
  SELECT workspace_id,
         COUNT(DISTINCT CASE WHEN who LIKE '%@%' THEN who END) AS active_people,
         COUNT(DISTINCT CASE WHEN who RLIKE {_UUID} THEN who END) AS active_service_principals,
         SUM(is_query) AS queries,
         COUNT(DISTINCT CASE WHEN who LIKE '%@%' OR who RLIKE {_UUID} THEN d END) AS active_days,
         MAX(CASE WHEN is_query = 1 THEN ts END) AS last_query
  FROM ev GROUP BY 1
),
ids AS (
  SELECT workspace_id FROM act UNION SELECT workspace_id FROM held
),
base AS (
  SELECT i.workspace_id, COALESCE(w.workspace_name, i.workspace_id) AS workspace, w.workspace_url,
         COALESCE(h.catalogs, 0) AS catalogs, COALESCE(h.tables, 0) AS tables, COALESCE(h.views, 0) AS views,
         COALESCE(h.table_columns, 0) AS table_columns, COALESCE(h.view_columns, 0) AS view_columns,
         COALESCE(v.volumes, 0) AS volumes, COALESCE(v.volume_files, 0) AS volume_files,
         COALESCE(s.table_bytes, 0) AS table_bytes, COALESCE(s.table_bytes, 0) + COALESCE(v.volume_bytes, 0) AS data_bytes,
         COALESCE(a.active_people, 0) AS active_people, COALESCE(a.active_service_principals, 0) AS active_service_principals,
         COALESCE(a.queries, 0) AS queries, COALESCE(a.active_days, 0) AS active_days, a.last_query,
         COALESCE(k.tables_written_never_read, 0) AS tables_written_never_read, COALESCE(k.tables_idle, 0) AS tables_idle,
         COALESCE(k.tables_cooling, 0) AS tables_cooling, COALESCE(k.tables_active, 0) AS tables_active,
         COALESCE(k.unread_bytes, 0) AS unread_bytes, COALESCE(k.reads, 0) AS reads, COALESCE(k.writes, 0) AS writes,
         k.last_read, k.last_write, COALESCE(k.sensitive_tables, 0) AS sensitive_tables
  FROM ids i
  LEFT JOIN held h ON h.workspace_id = i.workspace_id
  LEFT JOIN vols v ON v.workspace_id = i.workspace_id
  LEFT JOIN sizes s ON s.workspace_id = i.workspace_id
  LEFT JOIN act a ON a.workspace_id = i.workspace_id
  LEFT JOIN cls k ON k.workspace_id = i.workspace_id
  LEFT JOIN wsl w ON w.workspace_id = i.workspace_id
  WHERE i.workspace_id IS NOT NULL AND i.workspace_id <> '0'
),
idx AS (
  SELECT *,
         ROUND(100 * ({cplx}), 1) AS complexity_index,
         ROUND(100 * ({use}) + 100 * {rules.ACTIVE_DAYS_WEIGHT} * LEAST(active_days, {window}) / {window}, 1) AS usage_index
  FROM base
)
SELECT workspace, workspace_id,
       concat('https://', regexp_replace(regexp_replace(COALESCE(workspace_url, ''), '^https?://', ''), '/.*$', '')) AS workspace_link,
       tables_written_never_read, tables_idle, tables_cooling, tables_active,
       ROUND(data_bytes / 1073741824.0, 2) AS data_gb, ROUND(unread_bytes / 1073741824.0, 2) AS unread_gb,
       reads, writes, CAST(date_format(last_read, 'yyyy-MM-dd') AS STRING) AS last_read,
       CAST(date_format(last_write, 'yyyy-MM-dd') AS STRING) AS last_write, sensitive_tables,
       catalogs, tables, views, table_columns, view_columns, volumes, volume_files, complexity_index,
       active_people, active_service_principals, queries, active_days,
       CAST(date_format(last_query, 'yyyy-MM-dd HH:mm') AS STRING) AS last_query, usage_index,
       CASE WHEN usage_index < 10 THEN 'Retire candidate'
            WHEN complexity_index >= 50 AND usage_index < 50 THEN 'Review'
            WHEN complexity_index < 50 THEN 'Light'
            ELSE 'Healthy' END AS signal,
       CASE WHEN usage_index < 10 OR (complexity_index >= 50 AND usage_index < 50) THEN 1 ELSE 0 END AS flagged,
       1 AS workspaces
FROM idx
ORDER BY usage_index DESC, workspace""",
        "One row per workspace: its tables by usage class, unread GB, reads and writes, sensitive tables; tables, "
        "views, columns, volumes, files and size of the catalogs it is home to; people, service principals, queries "
        "and active days in the window; a complexity and a usage index and a signal.",
        "The Workspaces page: the table, the scatter and the counters.",
        "One row per workspace, most used first.",
        {**_CLASS_STEPS,
         "cls": "Per workspace: its tables by class, unread bytes (written never read plus idle), reads, writes, last "
                "read and write, tables with sensitive columns.",
         "cols": "Columns per table and view.",
         "held": "Per workspace: catalogs, tables, views and their columns, of the catalogs it is home to.",
         "vols": "Per workspace: volumes, and their files and bytes from the job's volume_sizes.",
         "sizes": "Per workspace: bytes of its tables, from the job's table_sizes (DESCRIBE DETAIL).",
         "ev": "Audit events and queries in the window, per workspace, identity and day.",
         "act": "Per workspace: active people, service principals, queries, active days and the last query.",
         "ids": "Every workspace with activity or a catalog.",
         "base": "Everything per workspace; a missing count reads 0.",
         "idx": "The complexity and the usage index (app/core/usage.py), each input on a log scale against the "
                "largest workspace shown."},
        head="Dates from the Time Window.",
    )


def tables() -> Dataset:
    return Dataset(
        "wd_tables", "Tables and views: usage class, size, reads and writes, sensitive columns",
        [], [],
        f"""{_classified()}
SELECT workspace, catalog, schema_name, catalog || '.' || schema_name AS schema_full, full_name AS table_name,
       CASE WHEN table_type = 'VIEW' THEN 'View' ELSE 'Table' END AS kind, usage_class, {_RANK} AS class_rank,
       size_in_bytes, ROUND(size_in_bytes / 1073741824.0, 3) AS size_gb, num_files,
       CASE WHEN usage_class = '{WNR}' THEN ROUND(size_in_bytes / 1073741824.0, 3) ELSE 0 END AS gb_written_never_read,
       CASE WHEN usage_class = '{IDLE}' THEN ROUND(size_in_bytes / 1073741824.0, 3) ELSE 0 END AS gb_idle,
       CASE WHEN usage_class = '{COOLING}' THEN ROUND(size_in_bytes / 1073741824.0, 3) ELSE 0 END AS gb_cooling,
       CASE WHEN usage_class = '{ACTIVE}' THEN ROUND(size_in_bytes / 1073741824.0, 3) ELSE 0 END AS gb_active,
       reads, writes, readers, writers,
       CAST(date_format(last_read, 'yyyy-MM-dd') AS STRING) AS last_read,
       datediff({END}, CAST(last_read AS DATE)) AS days_since_read,
       CAST(date_format(last_write, 'yyyy-MM-dd') AS STRING) AS last_write, lower(last_change_op) AS last_change_op,
       CAST(date_format(last_maintenance, 'yyyy-MM-dd') AS STRING) AS last_maintenance,
       rows_changed, ROUND(bytes_written / 1073741824.0, 2) AS gb_written,
       sensitive_columns, unmasked_columns, COALESCE(sensitive_types, '') AS sensitive_types,
       CASE WHEN sensitive_columns > 0 THEN 1 ELSE 0 END AS sensitive,
       CASE WHEN sensitive_columns > 0 AND usage_class IN {_UNREAD} THEN 1 ELSE 0 END AS sensitive_unread,
       CASE WHEN unmasked_columns > 0 THEN 1 ELSE 0 END AS has_unmasked,
       CASE WHEN usage_class IN {_UNREAD} THEN ROUND(size_in_bytes / 1073741824.0, 3) ELSE 0 END AS unread_gb,
       {_TABLE_LINK} AS table_link,
       table_owner AS owner, CAST(date_format(created, 'yyyy-MM-dd') AS STRING) AS created, 1 AS objects
FROM c
ORDER BY class_rank, size_in_bytes DESC NULLS LAST, table_name""",
        "Every table and view: its home workspace, usage class, size and files (job), reads, writes, readers and "
        f"writers in the window and last read and write over {READ_LOOKBACK_DAYS} days (lineage), sensitive columns, "
        "last change and maintenance.",
        "The Tables page, and the class counters and chart on Workspaces.",
        "One row per table or view, in clean-up order: class, then size.",
        dict(_CLASS_STEPS),
        head="Dates from the Time Window.",
    )


def cleanup() -> Dataset:
    return Dataset(
        "wd_cleanup", "Clean up first: unread tables, sensitive first",
        [], [],
        f"""{_classified()}
SELECT workspace, full_name AS table_name, usage_class, ROUND(size_in_bytes / 1073741824.0, 3) AS size_gb,
       writes, writers, COALESCE(sensitive_types, '') AS sensitive_types,
       CAST(date_format(last_read, 'yyyy-MM-dd') AS STRING) AS last_read,
       CAST(date_format(last_write, 'yyyy-MM-dd') AS STRING) AS last_write
FROM c
WHERE usage_class IN {_UNREAD} AND table_type <> 'VIEW'
ORDER BY sensitive_columns > 0 DESC, {_RANK}, size_in_bytes DESC NULLS LAST, full_name
LIMIT 25""",
        "The 25 tables to clean up first: unread sensitive ones, then written never read before idle, then the biggest "
        "(app/core/usage.py's cleanup_order).",
        "Clean up first on Workspaces.",
        "Up to 25 tables.",
        dict(_CLASS_STEPS),
        head="Dates from the Time Window.",
    )


def largest() -> Dataset:
    return Dataset(
        "wd_largest", "Largest tables: size against reads and writes",
        [], [],
        f"""{_classified()}
SELECT workspace, full_name AS table_name, usage_class, ROUND(size_in_bytes / 1073741824.0, 3) AS size_gb,
       reads, writes, readers, writers
FROM c
WHERE table_type <> 'VIEW' AND size_in_bytes IS NOT NULL
ORDER BY size_in_bytes DESC, full_name
LIMIT 25""",
        "The 25 largest tables with their reads and writes in the window: big and unread is the expensive corner.",
        "Largest tables on Workspaces.",
        "Up to 25 tables, biggest first.",
        dict(_CLASS_STEPS),
        head="Dates from the Time Window.",
    )


def volumes() -> Dataset:
    return Dataset(
        "wd_volumes", "Volumes: usage class, files, size, reads and writes",
        [], [],
        f"""{_homes()},
v AS (
  SELECT volume_catalog AS catalog, volume_schema AS sch, volume_name, volume_type, created,
         volume_catalog || '.' || volume_schema || '.' || volume_name AS full_name
  FROM system.information_schema.volumes WHERE volume_catalog NOT IN ({_SKIP})
),
ev AS (
  SELECT regexp_extract(COALESCE(request_params['path'], request_params['file_path'], request_params['directory_path']),
                        '^/Volumes/([^/]+)/([^/]+)/([^/]+)', 0) AS path_root,
         lower(COALESCE(user_identity.email, user_identity.subject_name)) AS who, event_time,
         action_name RLIKE '(?i)put|upload|create|delete|write|mkdir' AS is_write
  FROM system.access.audit
  WHERE event_date >= date_sub({END}, {READ_LOOKBACK_DAYS}) AND event_date < {END}
    AND service_name = 'filesystem' AND response.status_code = 200
),
used AS (
  SELECT replace(substr(path_root, 10), '/', '.') AS full_name,
         COUNT_IF(NOT is_write AND event_time >= {START}) AS reads, COUNT_IF(is_write AND event_time >= {START}) AS writes,
         COUNT(DISTINCT CASE WHEN NOT is_write AND event_time >= {START} THEN who END) AS readers,
         COUNT(DISTINCT CASE WHEN is_write AND event_time >= {START} THEN who END) AS writers,
         MAX(CASE WHEN NOT is_write THEN event_time END) AS last_read, MAX(CASE WHEN is_write THEN event_time END) AS last_write
  FROM ev WHERE path_root <> '' GROUP BY 1
),
j AS (
  SELECT p.workspace, v.catalog, v.sch AS schema_name, v.full_name, lower(v.volume_type) AS volume_type, v.created,
         vs.files, vs.size_in_bytes, COALESCE(u.reads, 0) AS reads, COALESCE(u.writes, 0) AS writes,
         COALESCE(u.readers, 0) AS readers, COALESCE(u.writers, 0) AS writers, u.last_read, u.last_write
  FROM v
  LEFT JOIN place p ON p.catalog = v.catalog
  LEFT JOIN {_sized('volume_sizes')} vs ON vs.catalog = v.catalog AND vs.schema = v.sch AND vs.volume_name = v.volume_name
  LEFT JOIN used u ON u.full_name = v.full_name
),
c AS (
  SELECT *, {_class('reads', 'writes', 'last_read', 'last_write', 'created')} AS usage_class FROM j
)
SELECT workspace, catalog, schema_name, full_name AS volume, volume_type, usage_class, {_RANK} AS class_rank,
       files, size_in_bytes, ROUND(size_in_bytes / 1073741824.0, 3) AS size_gb,
       reads, writes, readers, writers,
       CAST(date_format(last_read, 'yyyy-MM-dd') AS STRING) AS last_read,
       CAST(date_format(last_write, 'yyyy-MM-dd') AS STRING) AS last_write,
       CASE WHEN usage_class IN {_UNREAD} THEN ROUND(size_in_bytes / 1073741824.0, 3) ELSE 0 END AS unread_gb,
       1 AS volumes
FROM c
ORDER BY class_rank, size_in_bytes DESC NULLS LAST, volume""",
        "Every volume: its home workspace, usage class, files and size (job), file reads and writes and who did them "
        f"in the window, last read and write over {READ_LOOKBACK_DAYS} days (the audit log's file-system events).",
        "The Volumes page.",
        "One row per volume, in clean-up order.",
        {**_HOME_STEPS,
         "v": "Every volume, without the system catalogs.",
         "ev": f"File-system events under /Volumes over {READ_LOOKBACK_DAYS} days: who, when, read or write.",
         "used": "Per volume: reads, writes, readers and writers in the window; last read and write.",
         "j": "Everything per volume.",
         "c": "Its usage class, by the same rules as tables."},
        head="Dates from the Time Window.",
    )


def _people_body() -> str:
    return f"""wsl AS (
  SELECT CAST(workspace_id AS STRING) AS workspace_id, workspace_name FROM system.access.workspaces_latest
),
aev AS (
  SELECT CAST(workspace_id AS STRING) AS workspace_id, lower(COALESCE(user_identity.email, user_identity.subject_name)) AS who,
         event_date, event_time,
         service_name = 'accounts' AND action_name RLIKE '(?i)login' AND response.status_code = 200 AS is_login
  FROM system.access.audit
  WHERE event_date >= date_sub({END}, 365) AND event_date < {END}
),
seen AS (
  -- last seen over the year; logins in the window
  SELECT workspace_id, who, MAX(event_time) AS last_seen, COUNT_IF(is_login AND event_date >= {START}) AS logins,
         MAX(CASE WHEN event_date >= {START} THEN 1 ELSE 0 END) AS seen_in_window
  FROM aev GROUP BY 1, 2
),
qh AS (
  SELECT CAST(workspace_id AS STRING) AS workspace_id, lower(executed_by) AS who, COUNT(*) AS queries, MAX(start_time) AS last_query
  FROM system.query.history WHERE start_time >= {START} AND start_time < {END}
  GROUP BY 1, 2
),
lin AS (
  SELECT CAST(workspace_id AS STRING) AS workspace_id, source_table_full_name AS full_name, 1 AS is_read, 0 AS is_write,
         lower(created_by) AS who
  FROM system.access.table_lineage
  WHERE event_date >= {START} AND event_date < {END} AND source_table_full_name IS NOT NULL
  UNION ALL
  SELECT CAST(workspace_id AS STRING), target_table_full_name, 0, 1, lower(created_by)
  FROM system.access.table_lineage
  WHERE event_date >= {START} AND event_date < {END} AND target_table_full_name IS NOT NULL
),
read_any AS (SELECT DISTINCT full_name FROM lin WHERE is_read = 1),
{_sensitive()},
tw AS (
  SELECT l.workspace_id, l.who,
         COUNT(DISTINCT CASE WHEN l.is_read = 1 THEN l.full_name END) AS tables_read,
         COUNT(DISTINCT CASE WHEN l.is_write = 1 THEN l.full_name END) AS tables_written,
         SUM(l.is_read) AS reads, SUM(l.is_write) AS writes,
         COUNT(DISTINCT CASE WHEN l.is_write = 1 AND r.full_name IS NULL THEN l.full_name END) AS writes_nobody_reads,
         COUNT(DISTINCT CASE WHEN se.full_name IS NOT NULL THEN l.full_name END) AS sensitive_tables
  FROM lin l
  LEFT JOIN read_any r ON r.full_name = l.full_name
  LEFT JOIN sens se ON se.full_name = l.full_name
  GROUP BY 1, 2
),
grants AS (
  -- direct grants on tables, per grantee; through a schema, catalog or group isn't expanded
  SELECT lower(p.grantee) AS who, COUNT(DISTINCT p.table_catalog || '.' || p.table_schema || '.' || p.table_name) AS direct_grants,
         COUNT(DISTINCT CASE WHEN se.full_name IS NOT NULL THEN p.table_catalog || '.' || p.table_schema || '.' || p.table_name END) AS sensitive_grants
  FROM system.information_schema.table_privileges p
  LEFT JOIN sens se ON se.full_name = p.table_catalog || '.' || p.table_schema || '.' || p.table_name
  WHERE p.privilege_type IN ('SELECT', 'ALL PRIVILEGES') AND p.table_catalog NOT IN ({_SKIP})
  GROUP BY 1
),
ids AS (
  SELECT workspace_id, who FROM seen UNION SELECT workspace_id, who FROM qh UNION SELECT workspace_id, who FROM tw
),
k AS (
  SELECT i.workspace_id, i.who, {_kind('i.who')} AS kind,
         COALESCE(s.logins, 0) AS logins, COALESCE(q.queries, 0) AS queries,
         COALESCE(w.tables_read, 0) AS tables_read, COALESCE(w.tables_written, 0) AS tables_written,
         COALESCE(w.reads, 0) AS reads, COALESCE(w.writes, 0) AS writes,
         COALESCE(w.writes_nobody_reads, 0) AS writes_nobody_reads, COALESCE(w.sensitive_tables, 0) AS sensitive_tables,
         COALESCE(g.direct_grants, 0) AS direct_grants, COALESCE(g.sensitive_grants, 0) AS sensitive_grants,
         GREATEST(s.last_seen, q.last_query) AS last_seen,
         COALESCE(s.seen_in_window, 0) + CASE WHEN q.queries > 0 OR w.reads + w.writes > 0 THEN 1 ELSE 0 END AS seen_in_window
  FROM ids i
  LEFT JOIN seen s ON s.workspace_id = i.workspace_id AND s.who = i.who
  LEFT JOIN qh q ON q.workspace_id = i.workspace_id AND q.who = i.who
  LEFT JOIN tw w ON w.workspace_id = i.workspace_id AND w.who = i.who
  LEFT JOIN grants g ON g.who = i.who
),
people AS (
  SELECT COALESCE(n.workspace_name, k.workspace_id) AS workspace, k.who AS identity, k.kind, k.logins, k.queries,
         k.tables_read, k.tables_written, k.reads, k.writes, k.writes_nobody_reads, k.sensitive_tables,
         k.direct_grants, k.sensitive_grants,
         CAST(date_format(k.last_seen, 'yyyy-MM-dd') AS STRING) AS last_seen,
         datediff({END}, CAST(k.last_seen AS DATE)) AS days_since,
         CASE WHEN datediff({END}, CAST(k.last_seen AS DATE)) < 30 THEN 'Under 30 days'
              WHEN datediff({END}, CAST(k.last_seen AS DATE)) < 90 THEN '30-90 days'
              WHEN datediff({END}, CAST(k.last_seen AS DATE)) < 180 THEN '90-180 days'
              ELSE '180+ days' END AS last_seen_band,
         CASE WHEN datediff({END}, CAST(k.last_seen AS DATE)) >= {INACTIVE_DAYS} THEN 1 ELSE 0 END AS inactive,
         CASE WHEN k.seen_in_window > 0 THEN 1 ELSE 0 END AS active,
         CASE WHEN k.sensitive_tables > 0 THEN 1 ELSE 0 END AS touched_sensitive,
         CASE WHEN k.kind = 'Person' AND k.seen_in_window > 0 THEN 1 ELSE 0 END AS active_person,
         CASE WHEN k.kind = 'Service principal' AND k.seen_in_window > 0 THEN 1 ELSE 0 END AS active_sp,
         1 AS identities
  FROM k
  LEFT JOIN wsl n ON n.workspace_id = k.workspace_id
  WHERE k.kind <> 'Other' AND k.workspace_id IS NOT NULL AND k.workspace_id <> '0'
)"""


_PEOPLE_STEPS = {
    "wsl": "Workspace id to name.",
    "aev": "Audit events over the last year, per workspace and identity, with successful logins marked.",
    "seen": "Per workspace and identity: last seen over the year, logins in the window.",
    "qh": "Queries per workspace and identity in the window.",
    "lin": "Table reads (source) and writes (target) in the window, per workspace and identity, from lineage.",
    "read_any": "Tables anyone read in the window.",
    "found": "Sensitive-looking columns: a column tag, else the column name.",
    "sens_col": "One row per sensitive column, and whether a mask covers it.",
    "sens": "Per table: its sensitive columns.",
    "tw": "Per workspace and identity: tables read and written, writes nobody reads, sensitive tables touched.",
    "grants": "Per grantee: tables it can read through a direct SELECT or ALL PRIVILEGES grant, and how many are sensitive.",
    "ids": "Every workspace and identity seen in any of them.",
    "k": "All of it together, with the kind: a person (email) or a service principal (UUID).",
    "people": f"Per row: last seen band, inactive when not seen for {INACTIVE_DAYS}+ days, active when seen in the window.",
}


def people() -> Dataset:
    return Dataset(
        "wd_people", "People and service principals: what they read and write, last seen",
        [], [],
        f"""{_people_body()}
SELECT workspace, identity, kind, logins, queries, tables_read, tables_written, reads, writes, writes_nobody_reads,
       sensitive_tables, direct_grants, sensitive_grants, last_seen, days_since, last_seen_band, inactive, active,
       touched_sensitive, active_person, active_sp, identities
FROM people
ORDER BY writes DESC, reads DESC, queries DESC, identity""",
        "Everyone seen in a workspace over the last year or using its tables in the window: logins, queries, tables "
        "read and written (lineage), writes nobody reads, sensitive tables touched, direct grants, last seen.",
        "The People page.",
        "One row per workspace and identity, most table writes first.",
        dict(_PEOPLE_STEPS),
        head="Dates from the Time Window; last seen over a year.",
    )


def inactive() -> Dataset:
    return Dataset(
        "wd_inactive", f"Not seen for {INACTIVE_DAYS}+ days, with direct grants",
        [], [],
        f"""{_people_body()}
SELECT workspace, identity, kind, logins, queries, tables_read, tables_written, reads, writes, writes_nobody_reads,
       sensitive_tables, direct_grants, sensitive_grants, last_seen, days_since, last_seen_band, inactive, active,
       touched_sensitive, active_person, active_sp, identities
FROM people
WHERE inactive = 1
ORDER BY sensitive_grants DESC, direct_grants DESC, days_since DESC, identity""",
        f"People and service principals not seen in a workspace for {INACTIVE_DAYS}+ days within the last year, with "
        "the tables they can still read through a direct grant.",
        "Not seen for 90+ days on People.",
        "One row per workspace and identity, sensitive grants first.",
        dict(_PEOPLE_STEPS),
        head="Dates from the Time Window; last seen over a year.",
    )


def daily() -> Dataset:
    return Dataset(
        "wd_daily", "Reads and writes per day, per table",
        [], [],
        f"""{_homes()}
SELECT l.day, p.workspace, l.full_name AS table_name, l.direction, COUNT(*) AS events
FROM (
  SELECT event_date AS day, source_table_full_name AS full_name, source_table_catalog AS catalog, 'Reads' AS direction
  FROM system.access.table_lineage
  WHERE event_date >= {START} AND event_date < {END} AND source_table_full_name IS NOT NULL
  UNION ALL
  SELECT event_date, target_table_full_name, target_table_catalog, 'Writes'
  FROM system.access.table_lineage
  WHERE event_date >= {START} AND event_date < {END} AND target_table_full_name IS NOT NULL
) l
LEFT JOIN place p ON p.catalog = l.catalog
WHERE l.catalog NOT IN ({_SKIP})
GROUP BY 1, 2, 3, 4""",
        "Table reads and writes per day in the window, per table and its home workspace (lineage events).",
        "The daily charts on Overview, Workspace detail and Table detail.",
        "One row per day, table and direction.",
        dict(_HOME_STEPS),
        head="Dates from the Time Window.",
    )


def schemas() -> Dataset:
    return Dataset(
        "wd_schemas", "Schemas: what each holds and how it is used",
        [], [],
        f"""{_classified()},
cols AS (
  SELECT c.table_catalog || '.' || c.table_schema AS schema_full,
         COUNT_IF(t.table_type <> 'VIEW') AS table_columns, COUNT_IF(t.table_type = 'VIEW') AS view_columns
  FROM system.information_schema.columns c
  JOIN system.information_schema.tables t
    ON t.table_catalog = c.table_catalog AND t.table_schema = c.table_schema AND t.table_name = c.table_name
  WHERE c.table_catalog NOT IN ({_SKIP}) AND c.table_schema <> 'information_schema'
  GROUP BY 1
),
vols AS (
  SELECT v.volume_catalog || '.' || v.volume_schema AS schema_full, COUNT(*) AS volumes,
         COALESCE(SUM(vs.files), 0) AS volume_files, COALESCE(SUM(vs.size_in_bytes), 0) AS volume_bytes
  FROM system.information_schema.volumes v
  LEFT JOIN {_sized('volume_sizes')} vs
    ON vs.catalog = v.volume_catalog AND vs.schema = v.volume_schema AND vs.volume_name = v.volume_name
  WHERE v.volume_catalog NOT IN ({_SKIP})
  GROUP BY 1
),
sch AS (
  SELECT workspace, catalog || '.' || schema_name AS schema_full,
         COUNT_IF(table_type <> 'VIEW') AS tables, COUNT_IF(table_type = 'VIEW') AS views,
         COUNT_IF(usage_class = '{WNR}' AND table_type <> 'VIEW') AS tables_written_never_read,
         COUNT_IF(usage_class = '{IDLE}' AND table_type <> 'VIEW') AS tables_idle,
         COUNT_IF(usage_class = '{COOLING}' AND table_type <> 'VIEW') AS tables_cooling,
         COUNT_IF(usage_class = '{ACTIVE}' AND table_type <> 'VIEW') AS tables_active,
         COALESCE(SUM(size_in_bytes), 0) AS table_bytes,
         SUM(CASE WHEN usage_class IN {_UNREAD} THEN size_in_bytes ELSE 0 END) AS unread_bytes,
         SUM(reads) AS reads, SUM(writes) AS writes, COUNT_IF(sensitive_columns > 0) AS sensitive_tables
  FROM c GROUP BY 1, 2
)
SELECT s.workspace, s.schema_full, s.tables, s.views, s.tables_written_never_read, s.tables_idle, s.tables_cooling,
       s.tables_active, COALESCE(cl.table_columns, 0) AS table_columns, COALESCE(cl.view_columns, 0) AS view_columns,
       COALESCE(v.volumes, 0) AS volumes, COALESCE(v.volume_files, 0) AS volume_files,
       ROUND((s.table_bytes + COALESCE(v.volume_bytes, 0)) / 1073741824.0, 2) AS size_gb,
       ROUND(COALESCE(s.unread_bytes, 0) / 1073741824.0, 2) AS unread_gb, s.reads, s.writes, s.sensitive_tables
FROM sch s
LEFT JOIN cols cl ON cl.schema_full = s.schema_full
LEFT JOIN vols v ON v.schema_full = s.schema_full
ORDER BY size_gb DESC, s.schema_full""",
        "Every schema with its home workspace: tables by usage class, columns, views, volumes, files, size, unread GB, "
        "reads, writes, sensitive tables.",
        "By schema on Workspace detail.",
        "One row per schema, biggest first.",
        {**_CLASS_STEPS,
         "cols": "Table and view columns per schema.",
         "vols": "Volumes per schema, with files and bytes from the job.",
         "sch": "Per schema: tables and views, tables by class, bytes, unread bytes, reads, writes, sensitive tables."},
        head="Dates from the Time Window.",
    )


def access() -> Dataset:
    return Dataset(
        "wd_access", "Who read and wrote which table",
        [], [],
        f"""{_classified()}
SELECT l.who AS identity, {_kind('l.who')} AS kind, c.workspace, c.full_name AS table_name, c.usage_class,
       SUM(l.is_read) AS reads, SUM(l.is_write) AS writes,
       CASE WHEN SUM(l.is_read) > 0 AND SUM(l.is_write) > 0 THEN 'Reads and writes'
            WHEN SUM(l.is_write) > 0 THEN 'Writes' ELSE 'Reads' END AS access,
       CAST(date_format(MAX(l.event_time), 'yyyy-MM-dd') AS STRING) AS last_access,
       COALESCE(c.sensitive_types, '') AS sensitive_types, 1 AS pairs
FROM lin l
JOIN c ON c.full_name = l.full_name
WHERE l.event_time >= {START} AND l.who IS NOT NULL
GROUP BY l.who, c.workspace, c.full_name, c.usage_class, c.sensitive_types
ORDER BY SUM(l.is_write) + SUM(l.is_read) DESC, identity""",
        "Each person or service principal with each table they read or wrote in the window: counts, last use, the "
        "table's class and sensitive columns.",
        "Who reads and writes it on Table detail; the tables they used on Person detail.",
        "One row per identity and table.",
        dict(_CLASS_STEPS),
        head="Dates from the Time Window.",
    )


def edges() -> Dataset:
    return Dataset(
        "wd_edges", "Where each table comes from and where it goes",
        [], [],
        f"""{_classified()},
e AS (
  SELECT source_table_full_name AS src, target_table_full_name AS dst, COUNT(*) AS events
  FROM system.access.table_lineage
  WHERE event_date >= {START} AND event_date < {END}
    AND source_table_full_name IS NOT NULL AND target_table_full_name IS NOT NULL
    AND source_table_full_name <> target_table_full_name
  GROUP BY 1, 2
)
SELECT e.dst AS table_name, 'Upstream: it reads from' AS direction, e.src AS other_table, c.usage_class AS other_class, e.events
FROM e LEFT JOIN c ON c.full_name = e.src
UNION ALL
SELECT e.src, 'Downstream: it feeds', e.dst, c.usage_class, e.events
FROM e LEFT JOIN c ON c.full_name = e.dst""",
        "Each table's upstream (the tables written into it) and downstream (the tables it is written into) in the "
        "window, with the other table's class: a downstream nobody reads means the job building it can stop.",
        "Where it comes from, where it goes on Table detail.",
        "One row per table, direction and other table.",
        {**_CLASS_STEPS, "e": "Table-to-table lineage edges in the window, with their event counts."},
        head="Dates from the Time Window.",
    )


# Delta's OPTIMIZE (also ZORDER and liquid clustering), predictive optimization's compaction and clustering
COMPACT_OPS = "^(OPTIMIZE|COMPACTION|CLUSTERING)"
FULL_SCAN_FILES = 50  # a read that pruned none of at least this many files read the whole table
QUERY_TEXT_CHARS = 300
_GB = "1073741824.0"


def _po_metric(*keys: str) -> str:
    return "COALESCE(" + ", ".join(f"try_cast(operation_metrics['{k}'] AS BIGINT)" for k in keys) + ")"


def history() -> Dataset:
    return Dataset(
        "wd_changes", "Changes and maintenance per table",
        [], [],
        f"""h AS (
  SELECT catalog || '.' || schema || '.' || table_name AS table_name, timestamp AS ts, operation, 'Delta' AS source,
         user_name AS who, rows_changed, files_added, files_removed, bytes_added, bytes_removed, freed_bytes,
         CAST(NULL AS DOUBLE) AS dbu, CAST(NULL AS STRING) AS status
  FROM {_sized('table_history')}
  WHERE timestamp >= {START} AND timestamp < {END}
),
po AS (
  SELECT catalog_name || '.' || schema_name || '.' || table_name, start_time, operation_type, 'Predictive optimization',
         'predictive optimization', CAST(NULL AS BIGINT),
         {_po_metric('number_of_output_files', 'number_of_added_files')},
         {_po_metric('number_of_compacted_files', 'number_of_removed_files', 'number_of_clustered_files', 'number_of_deleted_files')},
         {_po_metric('amount_of_output_data_bytes')},
         {_po_metric('amount_of_data_compacted_bytes', 'amount_of_data_removed_bytes')},
         {_po_metric('amount_of_data_deleted_bytes')},
         CASE WHEN usage_unit = 'ESTIMATED_DBU' THEN CAST(usage_quantity AS DOUBLE) END, operation_status
  FROM system.storage.predictive_optimization_operations_history
  WHERE start_time >= {START} AND start_time < {END} AND table_name IS NOT NULL
),
a AS (SELECT * FROM h UNION ALL SELECT * FROM po)
SELECT table_name, CAST(date_format(ts, 'yyyy-MM-dd HH:mm') AS STRING) AS changed_at, operation, source, who,
       COALESCE(status, '') AS status, rows_changed, files_added, files_removed,
       ROUND(bytes_added / {_GB}, 3) AS gb_added, ROUND(bytes_removed / {_GB}, 3) AS gb_removed,
       ROUND(freed_bytes / {_GB}, 3) AS gb_freed, ROUND(dbu, 3) AS dbu,
       CASE WHEN upper(operation) RLIKE '{COMPACT_OPS}' THEN 1 ELSE 0 END AS compactions,
       CASE WHEN upper(operation) RLIKE '{COMPACT_OPS}' THEN files_removed END AS files_merged,
       CASE WHEN upper(operation) RLIKE '{COMPACT_OPS}' THEN files_added END AS files_after
FROM a
ORDER BY table_name, ts DESC""",
        f"Every write, merge, OPTIMIZE and VACUUM (the sizes job's DESCRIBE HISTORY, last {HISTORY_VERSIONS} per table) "
        "and every predictive optimization run in the window: who, rows, files and bytes added and removed, "
        "bytes VACUUM freed, predictive optimization's DBUs.",
        "Changes and maintenance, and the compaction counters, on Table detail.",
        "One row per operation, newest first.",
        {"h": "The sizes job's DESCRIBE HISTORY rows in the window.",
         "po": "Predictive optimization's runs in the window, with the files and bytes from its metrics.",
         "a": "Both in one list."},
        head="Dates from the Time Window. History only as far back as its last 100 versions.",
    )


def _statements(lo: str) -> str:
    """Query statements from lo to the window's end and the tables each read or wrote (lineage by
    statement_id); a statement that reads and writes a table counts as a write."""
    return f"""st AS (
  SELECT statement_id, table_name, CASE WHEN MAX(is_write) = 1 THEN 'Writes' ELSE 'Reads' END AS access
  FROM (
    SELECT statement_id, source_table_full_name AS table_name, 0 AS is_write FROM system.access.table_lineage
    WHERE event_date >= {lo} AND event_date < {END} AND statement_id IS NOT NULL AND source_table_full_name IS NOT NULL
    UNION ALL
    SELECT statement_id, target_table_full_name, 1 FROM system.access.table_lineage
    WHERE event_date >= {lo} AND event_date < {END} AND statement_id IS NOT NULL AND target_table_full_name IS NOT NULL
  ) x
  GROUP BY 1, 2
),
q AS (
  SELECT statement_id, CAST(workspace_id AS STRING) AS workspace_id, lower(executed_by) AS who, start_time, statement_type,
         COALESCE(total_duration_ms, 0) AS ms, read_bytes, read_files, pruned_files, compute.warehouse_id AS warehouse_id,
         statement_text
  FROM system.query.history
  WHERE start_time >= {lo} AND start_time < {END}
)"""


_STMT_STEPS = {"st": "Each query statement and the tables it read or wrote (lineage rows with a statement_id).",
               "q": "Query history: who, when, time, bytes and files read and pruned."}


def table_queries() -> Dataset:
    return Dataset(
        "wd_table_queries", "Each table's slowest queries",
        [], [],
        f"""{_statements(START)},
wsl AS (SELECT CAST(workspace_id AS STRING) AS workspace_id, workspace_name FROM system.access.workspaces_latest),
r AS (
  SELECT st.table_name, st.access, q.*, ROW_NUMBER() OVER (PARTITION BY st.table_name ORDER BY q.ms DESC, q.statement_id) AS rnk
  FROM st JOIN q ON q.statement_id = st.statement_id
)
SELECT r.table_name, CAST(date_format(r.start_time, 'yyyy-MM-dd HH:mm') AS STRING) AS started, r.who, r.statement_type, r.access,
       ROUND(r.ms / 1000.0, 1) AS seconds, ROUND(r.read_bytes / {_GB}, 2) AS gb_read, r.read_files AS files_read,
       r.pruned_files AS files_pruned,
       CASE WHEN r.access = 'Reads' AND COALESCE(r.pruned_files, 0) = 0 AND COALESCE(r.read_files, 0) >= {FULL_SCAN_FILES}
            THEN 'Full scan' ELSE '' END AS scan,
       COALESCE(w.workspace_name, r.workspace_id) AS workspace, r.warehouse_id,
       left(regexp_replace(r.statement_text, '\\s+', ' '), {QUERY_TEXT_CHARS}) AS query
FROM r LEFT JOIN wsl w ON w.workspace_id = r.workspace_id
WHERE r.rnk <= 10
ORDER BY r.table_name, r.ms DESC""",
        f"Per table, the 10 slowest queries in the window that read or wrote it: who, time, bytes, files read and "
        f"pruned. Full scan: {FULL_SCAN_FILES}+ files read, none pruned. Bytes and files are the whole query's.",
        "Slowest queries on Table detail.",
        "Up to 10 rows per table.",
        {**_STMT_STEPS, "wsl": "Workspace names.", "r": "Each table's queries, slowest first."},
        head="Dates from the Time Window.",
    )


def table_scans() -> Dataset:
    return Dataset(
        "wd_table_scans", "Each table's reads in query history",
        [], [],
        f"""{_statements(START)}
SELECT st.table_name, COUNT(*) AS reads, ROUND(AVG(q.ms) / 1000.0, 1) AS avg_seconds,
       ROUND(SUM(COALESCE(q.read_bytes, 0)) / {_GB}, 1) AS gb_read,
       COUNT_IF(COALESCE(q.pruned_files, 0) = 0 AND COALESCE(q.read_files, 0) >= {FULL_SCAN_FILES}) AS full_scans,
       ROUND(100 * SUM(COALESCE(q.pruned_files, 0)) / NULLIF(SUM(COALESCE(q.read_files, 0)) + SUM(COALESCE(q.pruned_files, 0)), 0), 0) AS pruned_pct
FROM st JOIN q ON q.statement_id = st.statement_id
WHERE st.access = 'Reads'
GROUP BY st.table_name""",
        "Per table, its reads in query history in the window: how many, average time, bytes, how many pruned no "
        "file, and the share of files skipped.",
        "The query counters on Table detail.",
        "One row per table read.",
        dict(_STMT_STEPS),
        head="Dates from the Time Window.",
    )


def table_other() -> Dataset:
    key = ("concat_ws('|', COALESCE(CAST(entity_run_id AS STRING), CAST(entity_id AS STRING), ''), "
           "COALESCE(created_by, ''), CAST(event_time AS STRING))")
    return Dataset(
        "wd_table_other", "Reads and writes not in query history",
        [], [],
        f"""lin AS (
  SELECT statement_id, source_table_full_name AS table_name, 'Reads' AS access, entity_type, lower(created_by) AS who,
         event_time, {key} AS k
  FROM system.access.table_lineage
  WHERE event_date >= {START} AND event_date < {END} AND source_table_full_name IS NOT NULL
  UNION ALL
  SELECT statement_id, target_table_full_name, 'Writes', entity_type, lower(created_by), event_time, {key}
  FROM system.access.table_lineage
  WHERE event_date >= {START} AND event_date < {END} AND target_table_full_name IS NOT NULL
),
qs AS (SELECT DISTINCT statement_id FROM system.query.history WHERE start_time >= {START} AND start_time < {END}),
per AS (
  SELECT l.table_name, l.who, COALESCE(l.entity_type, 'unknown') AS via, l.access, COUNT(DISTINCT l.k) AS events,
         MAX(l.event_time) AS last_event
  FROM lin l LEFT JOIN qs ON qs.statement_id = l.statement_id
  WHERE qs.statement_id IS NULL
  GROUP BY 1, 2, 3, 4
)
SELECT table_name, who, via, access, events, CAST(date_format(last_event, 'yyyy-MM-dd') AS STRING) AS last_event,
       'not available' AS time_bytes_files
FROM per
QUALIFY ROW_NUMBER() OVER (PARTITION BY table_name ORDER BY events DESC, who, access, via) <= 10
ORDER BY table_name, events DESC""",
        "Per table, the reads and writes in lineage whose statement is not in query history: jobs, notebooks and "
        "pipelines on classic compute. Who, through what, how many and when; their time, bytes and files are "
        "not available.",
        "Not in query history on Table detail.",
        "Up to 10 rows per table.",
        {"lin": "Every lineage read and write in the window, with its statement_id.",
         "qs": "The statements query history has.",
         "per": "Lineage events with no statement in query history, per table, identity, entity type and access."},
        head="Dates from the Time Window.",
    )


def compaction_effect() -> Dataset:
    return Dataset(
        "wd_compaction_effect", "Reads before and after each compaction",
        [], [],
        f"""{_statements(f"date_sub({START}, 7)")},
ev AS (
  SELECT catalog || '.' || schema || '.' || table_name AS table_name, timestamp AS ts, operation
  FROM {_sized('table_history')}
  WHERE timestamp >= {START} AND timestamp < {END} AND upper(operation) RLIKE '{COMPACT_OPS}'
  UNION ALL
  SELECT catalog_name || '.' || schema_name || '.' || table_name, start_time, operation_type
  FROM system.storage.predictive_optimization_operations_history
  WHERE start_time >= {START} AND start_time < {END} AND operation_status = 'SUCCESSFUL'
    AND upper(operation_type) RLIKE '{COMPACT_OPS}'
),
r AS (SELECT st.table_name, q.start_time, q.ms, q.read_files FROM st JOIN q ON q.statement_id = st.statement_id WHERE st.access = 'Reads'),
b AS (
  SELECT ev.table_name, ev.ts, ev.operation,
         COUNT_IF(r.start_time < ev.ts) AS reads_before, COUNT_IF(r.start_time > ev.ts) AS reads_after,
         AVG(CASE WHEN r.start_time < ev.ts THEN r.ms END) AS ms_before, AVG(CASE WHEN r.start_time > ev.ts THEN r.ms END) AS ms_after,
         AVG(CASE WHEN r.start_time < ev.ts THEN r.read_files END) AS files_before,
         AVG(CASE WHEN r.start_time > ev.ts THEN r.read_files END) AS files_after
  FROM ev JOIN r ON r.table_name = ev.table_name
   AND r.start_time >= ev.ts - INTERVAL 7 DAYS AND r.start_time < ev.ts + INTERVAL 7 DAYS
  GROUP BY ev.table_name, ev.ts, ev.operation
)
SELECT table_name, CAST(date_format(ts, 'yyyy-MM-dd HH:mm') AS STRING) AS compacted_at, operation, reads_before, reads_after,
       ROUND(ms_before / 1000.0, 1) AS seconds_before, ROUND(ms_after / 1000.0, 1) AS seconds_after,
       ROUND(100 * (ms_after - ms_before) / NULLIF(ms_before, 0), 0) AS time_change_pct,
       ROUND(files_before, 0) AS files_per_read_before, ROUND(files_after, 0) AS files_per_read_after
FROM b
WHERE reads_before >= 3 AND reads_after >= 3
ORDER BY table_name, ts DESC""",
        "Per compaction in the window (OPTIMIZE, predictive compaction or clustering), the table's reads in query "
        "history in the 7 days before and after: average time and files per read. Needs 3+ reads on each side; "
        "an indication, other changes count too.",
        "Reads before and after a compaction on Table detail.",
        "One row per compaction with enough reads.",
        {**_STMT_STEPS, "ev": "Compactions in the window: the sizes job's history and predictive optimization.",
         "r": "Reads of each table in query history.", "b": "Reads in the 7 days before and after each compaction."},
        head="Dates from the Time Window; reads from 7 days before it.",
    )


def columns() -> Dataset:
    return Dataset(
        "wd_columns", "Sensitive columns per table",
        [], [],
        f"""{_sensitive()}
SELECT s.full_name AS table_name, s.column_name, s.pii_type,
       CASE WHEN s.masked THEN 'masked' ELSE 'unmasked' END AS mask, CASE WHEN s.masked THEN 0 ELSE 1 END AS unmasked, 1 AS columns
FROM sens_col s
ORDER BY table_name, unmasked DESC, s.column_name""",
        "Every sensitive-looking column: a column tag, else the column name; and whether a column mask covers it.",
        "Sensitive columns on Table detail.",
        "One row per sensitive column.",
        {"found": "Sensitive-looking columns: a column tag (pii, sensitive, gdpr, class.*), else the column name.",
         "sens_col": "One row per sensitive column, the tag winning over the name, and whether a mask covers it.",
         "sens": "Per table: its sensitive columns."},
        head="Values are never read.",
        window=False,
    )


def grants() -> Dataset:
    return Dataset(
        "wd_grants", "Direct grants per table, and whether the grantee read it",
        [], [],
        f"""{_classified()},
rd AS (
  SELECT DISTINCT full_name, who FROM lin WHERE is_read = 1 AND event_time >= {START}
)
SELECT c.full_name AS table_name, c.workspace, lower(g.grantee) AS grantee, {_kind('lower(g.grantee)')} AS kind,
       g.privilege_type AS privilege,
       CASE WHEN rd.who IS NULL THEN 'Never read in the window' ELSE 'Read it' END AS used,
       CASE WHEN rd.who IS NULL THEN 1 ELSE 0 END AS unused, 1 AS grants
FROM system.information_schema.table_privileges g
JOIN c ON c.catalog = g.table_catalog AND c.schema_name = g.table_schema AND c.name = g.table_name
LEFT JOIN rd ON rd.full_name = c.full_name AND rd.who = lower(g.grantee)
WHERE g.privilege_type IN ('SELECT', 'ALL PRIVILEGES')
ORDER BY unused DESC, table_name, grantee""",
        "Direct SELECT and ALL PRIVILEGES grants per table, and whether the grantee read the table in the window. "
        "A grant to a group shows as never read: lineage records people, not groups.",
        "Can read it on Table detail.",
        "One row per table, grantee and privilege, unused first.",
        {**_CLASS_STEPS, "rd": "Who read each table in the window."},
        head="Dates from the Time Window.",
    )


def _ranked(base: Dataset, key: str, title: str, name: str, top: int, per_workspace: bool | str, extra: str = "") -> Dataset:
    """base's rows with the most read and write events, one row per direction: base's own steps and final
    SELECT (step b), ranked (step r). per_workspace: the top of each workspace, for the pages that pick one;
    a column name: the top per value of that column."""
    at = base.body.rfind("\nSELECT ")
    per = "workspace" if per_workspace is True else per_workspace or None
    over = f"PARTITION BY {per} " if per else ""
    keep = f"workspace, {name} AS name{extra}"
    return Dataset(
        key, title, [], [],
        f"""{base.body[:at]},
b AS (
{base.body[at + 1:]}
),
r AS (
  SELECT *, ROW_NUMBER() OVER ({over}ORDER BY reads + writes DESC, {name}) AS rnk FROM b WHERE reads + writes > 0
)
SELECT {keep}, 'Reads' AS direction, reads AS events FROM r WHERE rnk <= {top}
UNION ALL
SELECT {keep}, 'Writes' AS direction, writes AS events FROM r WHERE rnk <= {top}""",
        f"The {top} with the most read and write events in the window{f' per {per}' if per else ''}, "
        f"from {base.key}: one row per direction.",
        "The most active charts.",
        f"Up to {2 * top} rows{f' per {per}' if per else ''}.",
        {**base.steps, "b": f"Every row of {base.key}.",
         "r": "Ranked by read plus write events" + (f", within each {per}." if per else ".")},
        head=base.head,
    )


def busiest_days() -> Dataset:
    """wd_daily's rows for the 10 tables of each workspace with the most events."""
    base = daily()
    at = base.body.rfind("\nSELECT ")
    return Dataset(
        "wd_daily_top", "Each workspace's busiest tables per day", [], [],
        f"""{base.body[:at]},
b AS (
{base.body[at + 1:]}
),
t AS (
  SELECT workspace, table_name, ROW_NUMBER() OVER (PARTITION BY workspace ORDER BY SUM(events) DESC, table_name) AS rnk
  FROM b GROUP BY workspace, table_name
)
SELECT b.day, b.workspace, b.table_name, b.direction, b.events
FROM b JOIN t ON t.workspace <=> b.workspace AND t.table_name = b.table_name WHERE t.rnk <= 10""",
        "wd_daily for the 10 tables of each workspace with the most read and write events in the window.",
        "Busiest tables per day on Workspace detail.",
        "One row per day, table and direction, 10 tables per workspace.",
        {**base.steps, "b": "Every row of wd_daily.", "t": "Each workspace's tables ranked by events."},
        head=base.head,
    )


def _all() -> list[Dataset]:
    return [workspaces(), tables(), cleanup(), largest(), volumes(), people(), inactive(),
            daily(), schemas(), access(), edges(), history(), columns(), grants(),
            _ranked(workspaces(), "wd_top_workspaces", "The most active workspaces", "workspace", 50, False),
            _ranked(tables(), "wd_top_tables", "The most used tables", "table_name", 15, False),
            _ranked(tables(), "wd_top_tables_ws", "Each workspace's most used tables", "table_name", 10, True),
            _ranked(people(), "wd_top_people", "The most active people", "identity", 15, False, ", kind"),
            _ranked(people(), "wd_top_people_ws", "Each workspace's most active people", "identity", 10, True, ", kind"),
            _ranked(volumes(), "wd_top_volumes_ws", "Each workspace's most active volumes", "volume", 10, True, ", usage_class"),
            busiest_days(), table_queries(), table_scans(), table_other(), compaction_effect(),
            _ranked(access(), "wd_table_people", "Each table's most active people", "identity", 10, "table_name", ", table_name, kind"),
            _ranked(access(), "wd_person_tables", "Each person's most used tables", "table_name", 10, "identity", ", identity")]


DATASETS: list[Dataset] = _all()


def configure(sizes_schema: str | None) -> None:
    """Sets where the sizes job writes, then rebuilds the queries."""
    global SIZES_SCHEMA
    SIZES_SCHEMA = sizes_schema or SIZES_SCHEMA
    DATASETS[:] = _all()


def _scatter(key: str, d: Dataset) -> dict:
    """Complexity (x) by usage (y), one point per workspace, sized by its data."""
    q = {"name": bdd.MAIN_QUERY, "query": {"datasetName": d.name, "disaggregated": True,
         "fields": [bdd.bare_field(c) for c in ("complexity_index", "usage_index", "data_gb", "signal", "workspace")]}}
    return {"widget": {"name": bdd.hid("widget:scatter:" + key), "queries": [q], "spec": {
        "version": 3, "widgetType": "scatter",
        "encodings": {
            "x": {"fieldName": "complexity_index", "displayName": "Complexity index", "scale": {"type": "quantitative"}},
            "y": {"fieldName": "usage_index", "displayName": "Usage index", "scale": {"type": "quantitative"}},
            "size": {"fieldName": "data_gb", "displayName": "Data GB", "scale": {"type": "quantitative"}},
            "color": {"fieldName": "signal", "displayName": "Signal", "scale": {"type": "categorical",
                      "mappings": [{"value": v, "color": c} for v, c in SIGNAL_COLORS]}},
            "label": {"show": False},
        },
        "frame": {"title": "Complexity against usage", "showTitle": True, "showDescription": True,
                  "description": "One bubble per workspace, bubble = data held. Bottom right first: built a lot, used little."},
    }}}


def _bubbles(key: str, d: Dataset, x: tuple, y: tuple, size: tuple, color: tuple, colors, title: str, desc: str) -> dict:
    """One bubble per row: x, y and size are (column, label); colour is (column, label)."""
    fields = list(dict.fromkeys([x[0], y[0], size[0], color[0]]))
    c = {"fieldName": color[0], "displayName": color[1], "scale": {"type": "categorical"}}
    if colors:
        c["scale"]["mappings"] = [{"value": v, "color": k} for v, k in colors]
    q = {"name": bdd.MAIN_QUERY, "query": {"datasetName": d.name, "disaggregated": True, "fields": [bdd.bare_field(f) for f in fields]}}
    return {"widget": {"name": bdd.hid("widget:bubbles:" + key), "queries": [q], "spec": {
        "version": 3, "widgetType": "scatter",
        "encodings": {
            "x": {"fieldName": x[0], "displayName": x[1], "scale": {"type": "quantitative"}},
            "y": {"fieldName": y[0], "displayName": y[1], "scale": {"type": "quantitative"}},
            "size": {"fieldName": size[0], "displayName": size[1], "scale": {"type": "quantitative"}},
            "color": c, "label": {"show": False},
        },
        "frame": {"title": title, "showTitle": True, "showDescription": True, "description": desc},
    }}}


def _heat(key: str, d: Dataset, row: str, row_title: str, title: str, desc: str) -> dict:
    """Rows against days, darker = more read and write events."""
    q = {"name": bdd.MAIN_QUERY, "query": {"datasetName": d.name, "disaggregated": False, "fields": [
        bdd.bare_field("day"), bdd.bare_field(row), {"name": "sum(events)", "expression": "SUM(`events`)"}]}}
    return {"widget": {"name": bdd.hid("widget:heat:" + key), "queries": [q], "spec": {
        "version": 3, "widgetType": "heatmap",
        "encodings": {
            "x": {"fieldName": "day", "displayName": "Day", "scale": {"type": "categorical"}},
            "y": {"fieldName": row, "displayName": row_title, "scale": {"type": "categorical"}},
            "color": {"fieldName": "sum(events)", "displayName": "Read and write events",
                      "scale": {"type": "quantitative", "domain": {"min": 0},
                      "colorRamp": {"mode": "custom-sequential", "colors": {"start": "#EFF3FF", "end": "#2456A6"}}}}, "label": {"show": False},
        },
        "frame": {"title": title, "showTitle": True, "showDescription": True, "description": desc},
    }}}


def _pie(key: str, d: Dataset, cat: str, measure: str, title: str, desc: str, colors=None) -> dict:
    m = bdd.measure_name(measure, "SUM")
    color = {"fieldName": cat, "displayName": cat.replace("_", " ").capitalize(), "scale": {"type": "categorical"}}
    if colors:
        color["scale"]["mappings"] = [{"value": v, "color": c} for v, c in colors]
    return {"widget": {"name": bdd.hid("widget:pie:" + key),
                       "queries": [bdd.chart_query(f"q_{bdd.hid('qry:' + key)}", d.name, [cat], measure, "SUM")],
                       "spec": {"version": 3, "widgetType": "pie",
                                "encodings": {"angle": {"fieldName": m, "displayName": measure, "scale": {"type": "quantitative"}},
                                              "color": color, "label": {"show": True}},
                                "frame": {"title": title, "showTitle": True, "description": desc, "showDescription": True}}}}


def _daily(key: str, d: Dataset, title: str, desc: str) -> dict:
    w = bdd.bar_widget(key, d.name, "day", "events", title, category_label="Day", value_label="Events",
                       color_col="direction", temporal=True, width=1, height=1, x=0, y=0)
    w["widget"]["spec"]["frame"].update({"description": desc, "showDescription": True})
    return _colored(w, [("Reads", "#2456A6"), ("Writes", "#D27A2C")])


def _colored(w: dict, colors: list[tuple[str, str]]) -> dict:
    """A bar's colour series with fixed colours: the same class reads the same everywhere."""
    enc = w["widget"]["spec"]["encodings"]["color"]
    enc["scale"]["mappings"] = [{"value": v, "color": c} for v, c in colors]
    enc["legend"]["hideTitle"] = True
    return w


def _counter(key, d, col, title, desc, color=None):
    w = counter(key, d, col, title, desc)
    if color:
        w["widget"]["spec"]["encodings"]["value"]["style"]["color"] = color
    return w


def _pick(key: str, col: str, title: str, datasets: list[Dataset]) -> dict:
    """A single-select filter: the detail pages show the one picked."""
    w = field_filter(key, col, title, datasets)
    w["widget"]["spec"]["widgetType"] = "filter-single-select"
    return w


def _links(w: dict, *fields: str) -> dict:
    """Show these URL columns as an Open link."""
    for spec in w["widget"]["spec"]["encodings"]["columns"]:
        if spec["fieldName"] in fields:
            spec.update(displayAs="link", linkUrlTemplate="{{ @ }}", linkTextTemplate="Open", linkTitleTemplate="{{ @ }}")
    return w


CLASS_NOTE = (f"{WNR}: written in the Time Window, not read in it. {IDLE}: no read or write in the last {rules.IDLE_DAYS} days, whatever the Time Window. "
              f"{COOLING}: not read in the Time Window, used within {rules.IDLE_DAYS} days. {ACTIVE}: read in the Time Window.")
SIGNAL_COLORS = [("Retire candidate", "#C2362B"), ("Review", "#E0A93B"), ("Light", "#8E8B84"), ("Healthy", "#2456A6")]

WS_COLS = [("workspace", "Workspace", "text"), ("workspace_link", "Open", "text"),
           ("tables_written_never_read", WNR, "int"), ("tables_idle", IDLE, "int"),
           ("tables_cooling", COOLING, "int"), ("tables_active", ACTIVE, "int"),
           ("data_gb", "GB", "num"), ("unread_gb", "Unread GB", "num"),
           ("reads", "Reads", "int"), ("writes", "Writes", "int"), ("last_read", "Last read", "text"),
           ("last_write", "Last write", "text"), ("sensitive_tables", "PII tables", "int"),
           ("catalogs", "Catalogs", "int"), ("tables", "Tables", "int"), ("views", "Views", "int"),
           ("table_columns", "Table columns", "int"), ("view_columns", "View columns", "int"),
           ("volumes", "Volumes", "int"), ("volume_files", "Volume files", "int"),
           ("complexity_index", "Complexity", "num"), ("active_people", "People", "int"),
           ("active_service_principals", "SPs", "int"), ("queries", "Queries", "int"),
           ("active_days", "Active days", "int"), ("last_query", "Last query", "text"),
           ("usage_index", "Usage", "num"), ("signal", "Signal", "text")]
TABLE_COLS = [("table_name", "Table", "text"), ("table_link", "Open", "text"), ("workspace", "Workspace", "text"),
              ("kind", "Kind", "text"), ("usage_class", "Class", "text"), ("size_gb", "GB", "num"), ("num_files", "Files", "int"),
              ("reads", "Reads", "int"), ("writes", "Writes", "int"), ("readers", "Readers", "int"), ("writers", "Writers", "int"),
              ("last_read", "Last read", "text"), ("days_since_read", "Days since read", "int"),
              ("last_write", "Last write", "text"), ("last_change_op", "Last change", "text"),
              ("sensitive_types", "Sensitive columns", "text"), ("unmasked_columns", "Unmasked", "int"),
              ("last_maintenance", "Last OPTIMIZE / VACUUM", "text"), ("rows_changed", "Rows changed", "int"),
              ("gb_written", "GB written", "num"), ("owner", "Owner", "text"), ("created", "Created", "text")]
PEOPLE_COLS = [("identity", "Who", "text"), ("kind", "Kind", "text"), ("workspace", "Workspace", "text"),
               ("queries", "Queries", "int"), ("tables_read", "Tables read", "int"), ("tables_written", "Tables written", "int"),
               ("reads", "Reads", "int"), ("writes", "Writes", "int"), ("writes_nobody_reads", "Writes nobody reads", "int"),
               ("sensitive_tables", "Sensitive tables", "int"), ("direct_grants", "Direct grants", "int"),
               ("logins", "Logins", "int"), ("last_seen", "Last seen", "text")]
ACCESS_COLS = [("identity", "Who", "text"), ("kind", "Kind", "text"), ("table_name", "Table", "text"),
               ("workspace", "Workspace", "text"), ("access", "Access", "text"), ("reads", "Reads", "int"),
               ("writes", "Writes", "int"), ("last_access", "Last used", "text"), ("usage_class", "Table class", "text"),
               ("sensitive_types", "Sensitive", "text")]


# what each table is for, one line under its title
TABLE_NOTES = {
    "Every workspace, most used first": "Tables by class, what it holds, how much work happens in it, and the signal. Open: the workspace.",
    "Clean up first": "Sensitive and unread first, then written never read before idle, then the biggest.",
    "Largest tables: reads against writes": "Big and unread is the expensive corner.",
    "By schema": "Tables by class, columns, views, volumes and size per schema: whole schemas to clean up.",
    "Its tables, clean-up order": "Written never read first, then idle, cooling, active. Open: in Catalog Explorer.",
    "Its people and service principals": "Most table writes first; writes nobody reads point at jobs to pause.",
    "Every table and view, clean-up order": "Reads and writes: lineage in the window; last read and write: a year. Open: in Catalog Explorer.",
    "The table": "Its class, size, reads and writes, sensitive columns, last change and maintenance. Open: in Catalog Explorer.",
    "Who reads and writes it": "Who to ask before you change or drop it.",
    "Who can read it (direct grants)": "Never read in the window: a grant to remove. A group's grant reads never used: lineage records people.",
    "Sensitive columns": "A column tag, else the column name. Unmasked: add a column mask.",
    "Where it comes from, where it goes": "A downstream nobody reads: the job building it can stop.",
    "Changes and maintenance": "The sizes job's DESCRIBE HISTORY (last 100 versions, so older ones are not available) "
                               "and predictive optimization in the window. OPTIMIZE frees no storage: VACUUM does. "
                               "DBUs are predictive optimization's own estimate.",
    "Reads before and after a compaction": "Reads in query history 7 days each side, 3+ on both. An indication: other changes count too.",
    "Slowest queries": "Query history: SQL warehouses and serverless only. Bytes and files are the whole query's: a join "
                       f"shows them on each table. Full scan: {FULL_SCAN_FILES}+ files read, none pruned: filter on a clustering column.",
    "Not in query history": "Jobs, notebooks and pipelines on classic compute: in lineage with who and when; time, bytes "
                            "and files are not available.",
    "Every volume, clean-up order": "Spark jobs reading a volume on a cluster may not be in the audit log: check before you drop.",
    "Who reads and who writes": "Most table writes first. Writes nobody reads: tables they wrote that nobody read.",
    f"Not seen for {INACTIVE_DAYS}+ days, still with access (direct grants)": "Remove from the workspace or its groups; sensitive first.",
    "Where they work": "Logins, queries and last seen per workspace.",
    "The tables they read or wrote": "In the window, most used first, with each table's class.",
}


def dashboard() -> dict:
    W, T, C, L, V, U, I, D, S, A, E, H, K, G, TW, TT, TTW, TP, TPW, TVW, DT, TQ, TS, TO, EF, TPP, PT = DATASETS
    rw = lambda key, d, cat_t, title, desc: _colored(  # noqa: E731
        bar(key, d, "name", cat_t, "events", "Events", title, desc, money=False, color="direction"),
        [("Reads", "#2456A6"), ("Writes", "#D27A2C")])
    filt = lambda key, ds, extra=(): (1, [time_window(f"{key}:time", ds),  # noqa: E731
                                         field_filter(f"{key}:ws", "workspace", "Workspace", ds),
                                         *[field_filter(f"{key}:{col}", col, t, ds) for col, t in extra]])
    gb = lambda key, col, title, desc, color: _counter(key, T, col, title, desc, color)  # noqa: E731
    by_class = lambda key, d, cat, cat_t, m, m_t, title, desc: _colored(  # noqa: E731
        bar(key, d, cat, cat_t, m, m_t, title, desc, money=False, color="usage_class"), CLASS_COLORS)

    overview = place([
        (2, [header("du:ov:head", "Overview",
                    "Where the data sits by how it is used, who reads and writes it, and what to clean up, for the "
                    "workspaces and the window picked above. Every chart follows the filters.")]),
        filt("du:ov", [W, T, V, U, D, TW, TT, TP]),
        (3, [_counter("du:ov:gb", W, "data_gb", "Data · GB", "Tables and volume files"),
             _counter("du:ov:unread", W, "unread_gb", "Unread · GB", f"{WNR} plus {IDLE.lower()}", "#C2362B"),
             _counter("du:ov:reads", W, "reads", "Table reads", "Lineage, in the window", "#2456A6"),
             _counter("du:ov:writes", W, "writes", "Table writes", "Lineage, in the window", "#D27A2C"),
             _counter("du:ov:people", W, "active_people", "Active people", "Distinct per workspace, summed"),
             _counter("du:ov:flag", W, "flagged", "Retire / review", "Usage under 10, or built but little used", "#C2362B")]),
        (8, [_pie("du:ov:pie_gb", T, "usage_class", "size_gb", "GB by class",
                  "How much of the data is read, cooling, idle or written and never read", CLASS_COLORS),
             _pie("du:ov:pie_n", T, "usage_class", "objects", "Tables and views by class",
                  "The same by count: many small unread tables still cost upkeep", CLASS_COLORS)]),
        (7, [_daily("du:ov:daily", D, "Table reads and writes per day", "Lineage events in the window")]),
        (10, [rw("du:ov:top_ws", TW, "Workspace", "Most active workspaces",
                 "Table read and write events in the window, by home workspace; pick one on Workspace detail"),
              _bubbles("du:ov:rw", W, ("reads", "Table reads"), ("writes", "Table writes"), ("active_people", "Active people"),
                       ("signal", "Signal"), SIGNAL_COLORS, "Workspaces: reads against writes",
                       "One bubble per workspace, bubble = active people. Bottom right: read, rarely written. Top left: written, rarely read.")]),
        (9, [_heat("du:ov:heat", D, "workspace", "Workspace", "Activity per workspace per day",
                   "Darker = more table read and write events")]),
        (10, [rw("du:ov:top_t", TT, "Table", "Most used tables", "The 15 tables with the most read and write events"),
              rw("du:ov:top_p", TP, "Who", "Most active people",
                 "The 15 people and service principals with the most table events, per workspace")]),
        (10, [by_class("du:ov:ws_n", T, "workspace", "Workspace", "objects", "Tables and views", "Tables in use, by workspace",
                       "Active of all: blue = read in the window"),
              by_class("du:ov:vol_n", V, "workspace", "Workspace", "volumes", "Volumes", "Volumes in use, by workspace",
                       "Active of all: blue = a file read in the window")]),
        (12, [by_class("du:ov:ws_gb", T, "workspace", "Workspace", "size_gb", "GB", "Where the data sits, by workspace", CLASS_NOTE),
             by_class("du:ov:schema_gb", T, "schema_full", "Schema", "unread_gb", "Unread GB", "Unread GB, by schema",
                      "Written never read plus idle: the clean-up list")]),
        (12, [by_class("du:ov:pii", T, "workspace", "Workspace", "sensitive", "Tables", "Tables with sensitive columns, by class",
                      "Sensitive and unread is the first thing to clean up"),
             by_class("du:ov:vol", V, "workspace", "Workspace", "size_gb", "GB", "Volumes: GB by class, by workspace",
                      "File reads and writes from the audit log")]),
        (12, [_colored(bar("du:ov:bands", U, "workspace", "Workspace", "identities", "People and SPs",
                           "Everyone by last seen, by workspace",
                           "Red and orange: people who stopped coming; check their access on People",
                           money=False, color="last_seen_band"), BAND_COLORS)]),
        (5, [bdd.text_widget("du:ov:how", [
            "### How it is measured\n",
            f"- **Classes:** {CLASS_NOTE} A table created in the last {rules.NEW_DAYS} days counts as active.\n",
            "- **Complexity (0-100):** each input log-scaled against the largest workspace shown, `100 · ln(1+x) / ln(1+max)`: "
            "views 20%, view columns 15%, tables 20%, table columns 15%, volume files 10%, table size 20%.\n",
            "- **Usage (0-100):** active people 35%, active service principals 15%, queries 30% (log-scaled); active days ÷ window days 20%.\n",
            "- **Signal:** usage under 10 = Retire candidate; complexity 50+ and usage under 50 = Review; complexity under 50 = Light; else Healthy.\n",
            "- **Home workspace:** a catalog counts for the workspace that reads and writes it most in lineage.\n"],
            12, 5, 0, 0)]),
    ])
    workspaces_page = place([
        (2, [header("du:ws:head", "Workspaces",
                    "Every workspace: its tables by class, what it holds, how much work happens in it, and its signal. "
                    "Open a workspace, or pick it on Workspace detail.")]),
        filt("du:ws", [W, C, L]),
        (12, [(_pie("du:ws:pie_sig", W, "signal", "workspaces", "Workspaces by signal",
                    "Retire candidate: usage under 10. Review: built a lot (complexity 50+), used little (under 50).",
                    SIGNAL_COLORS), 4),
              (_scatter("du:ws:scatter", W), 8)]),
        (12, [_colored(bar("du:ws:usage", W, "workspace", "Workspace", "usage_index", "Usage index", "Usage index by workspace",
                           "Shortest bars first to review; colour = signal", money=False, color="signal"), SIGNAL_COLORS),
              _colored(bar("du:ws:unread", W, "workspace", "Workspace", "unread_gb", "Unread GB", "Unread GB by workspace",
                           "Written never read plus idle tables: what clean-up would free", money=False, color="signal"),
                       SIGNAL_COLORS)]),
        (12, [_links(table("du:ws:table", W, WS_COLS, "Every workspace, most used first"), "workspace_link")]),
        (10, [table("du:ws:cleanup", C, [
            ("table_name", "Table", "text"), ("workspace", "Workspace", "text"), ("usage_class", "Class", "text"),
            ("size_gb", "GB", "num"), ("sensitive_types", "Sensitive", "text"), ("writers", "Writers", "int"),
            ("last_read", "Last read", "text"), ("last_write", "Last write", "text")], "Clean up first"),
             table("du:ws:largest", L, [
            ("table_name", "Table", "text"), ("workspace", "Workspace", "text"), ("usage_class", "Class", "text"),
            ("size_gb", "GB", "num"), ("reads", "Reads", "int"), ("writes", "Writes", "int"),
            ("readers", "Readers", "int"), ("writers", "Writers", "int")], "Largest tables: reads against writes")]),
    ])
    ws_detail = place([
        (2, [header("du:wd:head", "Workspace detail",
                    "Pick one workspace: its schemas, its tables by class, its reads and writes per day, and its people.")]),
        (1, [time_window("du:wd:time", [W, S, T, D, U, TTW, TPW, TVW, DT]),
             _pick("du:wd:ws", "workspace", "Workspace", [W, S, T, D, U, TTW, TPW, TVW, DT])]),
        (3, [_counter("du:wd:gb", W, "data_gb", "Data · GB", "Tables and volume files"),
             _counter("du:wd:unread", W, "unread_gb", "Unread · GB", f"{WNR} plus {IDLE.lower()}", "#C2362B"),
             _counter("du:wd:tables", W, "tables", "Tables", "In its home catalogs"),
             _counter("du:wd:people", W, "active_people", "Active people", "In the window"),
             _counter("du:wd:cplx", W, "complexity_index", "Complexity", "0-100"),
             _counter("du:wd:usage", W, "usage_index", "Usage", "0-100")]),
        (10, [table("du:wd:schemas", S, [
            ("schema_full", "Schema", "text"), ("tables", "Tables", "int"),
            ("tables_written_never_read", WNR, "int"), ("tables_idle", IDLE, "int"),
            ("tables_cooling", COOLING, "int"), ("tables_active", ACTIVE, "int"),
            ("table_columns", "Columns", "int"), ("views", "Views", "int"), ("view_columns", "View columns", "int"),
            ("volumes", "Volumes", "int"), ("volume_files", "Volume files", "int"), ("size_gb", "GB", "num"),
            ("unread_gb", "Unread GB", "num"), ("reads", "Reads", "int"), ("writes", "Writes", "int"),
            ("sensitive_tables", "PII tables", "int")], "By schema")]),
        (12, [by_class("du:wd:sch_gb", T, "schema_full", "Schema", "size_gb", "GB", "GB by class, per schema", CLASS_NOTE),
             _daily("du:wd:daily", D, "Reads and writes per day", "Its tables, lineage events")]),
        (10, [_bubbles("du:wd:rw", T, ("reads", "Reads"), ("writes", "Writes"), ("readers", "Readers"), ("usage_class", "Class"),
                       CLASS_COLORS, "Its tables: reads against writes", "Bubble = readers, colour = class. Top left: written, rarely read."),
              _bubbles("du:wd:size", T, ("size_gb", "GB"), ("reads", "Reads"), ("readers", "Readers"), ("usage_class", "Class"),
                       CLASS_COLORS, "Its tables: size against reads",
                       "Every table, idle ones on the bottom line. Right and low: big, little used.")]),
        (10, [rw("du:wd:top_t", TTW, "Table", "Most used tables", "Its 10 tables with the most read and write events"),
              rw("du:wd:top_p", TPW, "Who", "Most active people", "Its 10 people and service principals with the most table events")]),
        (9, [(_heat("du:wd:heat", DT, "table_name", "Table", "Busiest tables per day",
                    "Its 10 busiest tables; darker = more events"), 8),
             (rw("du:wd:top_v", TVW, "Volume", "Most active volumes", "File reads and writes"), 4)]),
        (10, [_links(table("du:wd:tables", T, TABLE_COLS, "Its tables, clean-up order"), "table_link")]),
        (10, [table("du:wd:people", U, PEOPLE_COLS, "Its people and service principals")]),
    ])
    tables_page = place([
        (2, [header("du:tb:head", "Tables",
                    "Every table and view in one usage class: what is read, what is written but never read, and what "
                    "sits idle. Open one in Catalog Explorer, or pick it on Table detail.")]),
        (1, [time_window("du:tb:time", [T, L, TTW]), field_filter("du:tb:ws", "workspace", "Workspace", [T, L, TTW]),
             field_filter("du:tb:schema_full", "schema_full", "Schema", [T]),
             field_filter("du:tb:usage_class", "usage_class", "Class", [T, L])]),
        (3, [gb("du:tb:wnr", "gb_written_never_read", f"{WNR} · GB", "Pause the writer", "#C2362B"),
             gb("du:tb:idle", "gb_idle", f"{IDLE} · GB", "Archive or drop", "#45433E"),
             gb("du:tb:cool", "gb_cooling", f"{COOLING} · GB", "Ask the owner", "#6E4B00"),
             gb("du:tb:act", "gb_active", f"{ACTIVE} · GB", "Read in the window", "#1D4A8F")]),
        (12, [_pie("du:tb:pie_n", T, "usage_class", "objects", "Tables and views by class",
                   "Count per class; the counters above give the GB", CLASS_COLORS),
              by_class("du:tb:schema", T, "schema_full", "Schema", "size_gb", "GB", "GB by class, per schema",
                       "Where the unread data sits: schemas to clean up as a whole"),
              by_class("du:tb:pii", T, "workspace", "Workspace", "sensitive", "Tables", "Tables with sensitive columns, by class",
                       "Sensitive and unread: clean up first")]),
        (12, [by_class("du:tb:largest", L, "table_name", "Table", "size_gb", "GB", "The 25 largest tables, by class",
                       "Big and unread is the expensive corner")]),
        (10, [rw("du:tb:top", TTW, "Table", "Most active tables",
                 "The 10 with the most read and write events in each workspace; pick a workspace above for its own")]),
        (14, [_links(table("du:tb:table", T, TABLE_COLS, "Every table and view, clean-up order"), "table_link")]),
    ])
    table_detail = place([
        (2, [header("du:td:head", "Table detail",
                    "Pick one table: its reads and writes per day, who reads and writes it, who can read it but "
                    "doesn't, its sensitive columns, where it comes from and goes, its changes and maintenance, and "
                    "its slowest queries.")]),
        (1, [time_window("du:td:time", [T, D, A, E, G, H, TQ, TS, TO, EF, TPP]),
             _pick("du:td:t", "table_name", "Table", [T, D, A, E, H, K, G, TQ, TS, TO, EF, TPP])]),
        (3, [_counter("du:td:gb", T, "size_gb", "Size · GB", "From DESCRIBE DETAIL"),
             _counter("du:td:reads", T, "reads", "Reads", "In the window", "#2456A6"),
             _counter("du:td:writes", T, "writes", "Writes", "In the window", "#D27A2C"),
             _counter("du:td:readers", T, "readers", "Readers", "Distinct, in the window"),
             _counter("du:td:writers", T, "writers", "Writers", "Distinct, in the window"),
             _counter("du:td:unused", G, "unused", "Unused grants", "Can read it, never did in the window", "#C2362B")]),
        (3, [_counter("du:td:comp", H, "compactions", "Compactions", "OPTIMIZE and predictive, in the window"),
             _counter("du:td:merged", H, "files_merged", "Files merged", "By those compactions"),
             _counter("du:td:freed", H, "gb_freed", "GB freed by VACUUM", "In the window", "#1D7A46"),
             _counter("du:td:q", TS, "reads", "Reads in query history", "SQL warehouses and serverless"),
             _counter("du:td:sec", TS, "avg_seconds", "Seconds per read", "Average"),
             _counter("du:td:scan", TS, "full_scans", "Full scans", f"{FULL_SCAN_FILES}+ files read, none pruned", "#C2362B")]),
        (4, [_links(table("du:td:facts", T, TABLE_COLS, "The table"), "table_link")]),
        (7, [_daily("du:td:daily", D, "Reads and writes per day", "Lineage events")]),
        (8, [rw("du:td:top", TPP, "Who", "Most active people", "Its 10 people and service principals with the most read and write events")]),
        (8, [table("du:td:who", A, ACCESS_COLS, "Who reads and writes it"),
             table("du:td:grants", G, [("grantee", "Grantee", "text"), ("kind", "Kind", "text"),
                                        ("privilege", "Privilege", "text"), ("used", "Used", "text")],
                   "Who can read it (direct grants)")]),
        (8, [table("du:td:cols", K, [("column_name", "Column", "text"), ("pii_type", "Type", "text"), ("mask", "Mask", "text")],
                   "Sensitive columns"),
             table("du:td:edges", E, [("direction", "Direction", "text"), ("other_table", "Table", "text"),
                                      ("other_class", "Its class", "text"), ("events", "Events", "int")],
                   "Where it comes from, where it goes")]),
        (9, [table("du:td:changes", H, [("changed_at", "When", "text"), ("operation", "Operation", "text"),
                                         ("source", "Source", "text"), ("who", "Who", "text"), ("status", "Status", "text"),
                                         ("rows_changed", "Rows", "int"), ("files_added", "Files added", "int"),
                                         ("files_removed", "Files removed", "int"), ("gb_added", "GB added", "num"),
                                         ("gb_removed", "GB removed", "num"), ("gb_freed", "GB freed", "num"),
                                         ("dbu", "DBU (est.)", "num")],
                   "Changes and maintenance")]),
        (6, [table("du:td:effect", EF, [("compacted_at", "Compacted", "text"), ("operation", "Operation", "text"),
                                         ("reads_before", "Reads before", "int"), ("reads_after", "Reads after", "int"),
                                         ("seconds_before", "Seconds before", "num"), ("seconds_after", "Seconds after", "num"),
                                         ("time_change_pct", "Time change %", "int"),
                                         ("files_per_read_before", "Files per read before", "int"),
                                         ("files_per_read_after", "Files per read after", "int")],
                   "Reads before and after a compaction")]),
        (10, [table("du:td:queries", TQ, [("started", "When", "text"), ("who", "Who", "text"), ("statement_type", "Type", "text"),
                                           ("access", "Access", "text"), ("seconds", "Seconds", "num"), ("gb_read", "GB read", "num"),
                                           ("files_read", "Files read", "int"), ("files_pruned", "Pruned", "int"),
                                           ("scan", "Scan", "text"), ("workspace", "Workspace", "text"), ("query", "Query", "text")],
                    "Slowest queries")]),
        (7, [table("du:td:other", TO, [("who", "Who", "text"), ("via", "Through", "text"), ("access", "Access", "text"),
                                        ("events", "Events", "int"), ("last_event", "Last", "text"),
                                        ("time_bytes_files", "Time, bytes, files", "text")],
                   "Not in query history")]),
    ])
    volumes_page = place([
        (2, [header("du:vol:head", "Volumes",
                    "Every volume in one usage class: files and size from the daily listing, file reads and writes from "
                    "the audit log.")]),
        filt("du:vol", [V, TVW], [("usage_class", "Class")]),
        (3, [_counter("du:vol:n", V, "volumes", "Volumes", "Every catalog"),
             _counter("du:vol:files", V, "files", "Files", "From the daily listing"),
             _counter("du:vol:gb", V, "size_gb", "Size · GB", "From the daily listing"),
             _counter("du:vol:unread", V, "unread_gb", "Not read · GB", f"{WNR}, or {IDLE.lower()}", "#C2362B")]),
        (8, [_pie("du:vol:pie_gb", V, "usage_class", "size_gb", "GB by class",
                  "How much of the files are read, idle or written and never read", CLASS_COLORS),
             _pie("du:vol:pie_type", V, "volume_type", "size_gb", "GB, managed or external",
                  "External: storage Databricks doesn't clean up for you")]),
        (12, [by_class("du:vol:gb_cls", V, "workspace", "Workspace", "size_gb", "GB", "GB by class, by workspace", CLASS_NOTE),
             by_class("du:vol:n_cls", V, "workspace", "Workspace", "volumes", "Volumes", "Volumes by class, by workspace",
                      "File reads and writes through the Files API, UI or SQL on /Volumes")]),
        (8, [rw("du:vol:top", TVW, "Volume", "Most active volumes",
                "The 10 with the most file reads and writes in each workspace; pick a workspace above for its own")]),
        (10, [table("du:vol:table", V, [
            ("volume", "Volume", "text"), ("workspace", "Workspace", "text"), ("volume_type", "Type", "text"),
            ("usage_class", "Class", "text"), ("files", "Files", "int"), ("size_gb", "GB", "num"),
            ("reads", "File reads", "int"), ("writes", "File writes", "int"), ("readers", "Readers", "int"),
            ("writers", "Writers", "int"), ("last_read", "Last read", "text"), ("last_write", "Last write", "text")],
            "Every volume, clean-up order")]),
    ])
    people_page = place([
        (2, [header("du:usr:head", "People",
                    "Who reads and writes, who writes what nobody reads, who touched sensitive data, and who stopped "
                    "coming but can still read. Pick one person on Person detail.")]),
        filt("du:usr", [U, I, TPW], [("kind", "Kind")]),
        (3, [_counter("du:usr:people", U, "active_person", "Active people", "Signed in, queried or used a table"),
             _counter("du:usr:sps", U, "active_sp", "Active service principals", "In the window"),
             _counter("du:usr:pii", U, "touched_sensitive", "Touched sensitive data", "Read or wrote a table with sensitive columns", "#5B2FA6"),
             _counter("du:usr:inactive", U, "inactive", f"Inactive {INACTIVE_DAYS}d+", "Seen in the last year, not lately", "#C2362B")]),
        (9, [rw("du:usr:top", TPW, "Who", "Most active people",
                "The 10 with the most table events in each workspace; pick a workspace above for its own")]),
        (10, [table("du:usr:table", U, PEOPLE_COLS, "Who reads and who writes")]),
        (9, [table("du:usr:inactive_table", I, [
            ("identity", "Who", "text"), ("kind", "Kind", "text"), ("workspace", "Workspace", "text"),
            ("last_seen", "Last seen", "text"), ("days_since", "Days", "int"),
            ("direct_grants", "Tables they can read", "int"), ("sensitive_grants", "Of them sensitive", "int")],
            f"Not seen for {INACTIVE_DAYS}+ days, still with access (direct grants)")]),
    ])
    person_detail = place([
        (2, [header("du:pd:head", "Person detail",
                    "Pick one person or service principal: where they work, and every table they read or wrote.")]),
        (1, [time_window("du:pd:time", [U, A, PT]), _pick("du:pd:who", "identity", "Who", [U, A, PT])]),
        (3, [_counter("du:pd:queries", U, "queries", "Queries", "In the window"),
             _counter("du:pd:read", U, "tables_read", "Tables read", "In the window", "#2456A6"),
             _counter("du:pd:written", U, "tables_written", "Tables written", "In the window", "#D27A2C"),
             _counter("du:pd:wnr", U, "writes_nobody_reads", "Writes nobody reads", "Tables", "#C2362B"),
             _counter("du:pd:pii", U, "sensitive_tables", "Sensitive tables", "Read or written", "#5B2FA6"),
             _counter("du:pd:grants", U, "direct_grants", "Direct grants", "Tables they can read")]),
        (6, [table("du:pd:where", U, [("workspace", "Workspace", "text"), ("logins", "Logins", "int"),
                                       ("queries", "Queries", "int"), ("last_seen", "Last seen", "text"),
                                       ("days_since", "Days since", "int")], "Where they work")]),
        (8, [rw("du:pd:top", PT, "Table", "Most used tables", "Their 10 tables with the most read and write events")]),
        (10, [table("du:pd:tables", A, ACCESS_COLS, "The tables they read or wrote")]),
    ])
    for layout in (workspaces_page, ws_detail, tables_page, table_detail, volumes_page, people_page, person_detail):
        for item in layout:
            spec = item["widget"].get("spec") or {}
            title = (spec.get("frame") or {}).get("title")
            if spec.get("widgetType") == "table" and title in TABLE_NOTES:
                spec["frame"].update(description=TABLE_NOTES[title], showDescription=True)
    return bdd.tag_dashboard_queries({
        "datasets": [dataset_json(d) for d in DATASETS],
        "pages": [_page("overview", "Overview", overview, prefix=P),
                  _page("workspaces", "Workspaces", workspaces_page, prefix=P),
                  _page("workspace_detail", "Workspace detail", ws_detail, prefix=P),
                  _page("tables", "Tables", tables_page, prefix=P),
                  _page("table_detail", "Table detail", table_detail, prefix=P),
                  _page("volumes", "Volumes", volumes_page, prefix=P),
                  _page("users", "People", people_page, prefix=P),
                  _page("person_detail", "Person detail", person_detail, prefix=P)],
    }, TITLE)


# ------------------------------------------------------------------------------------------------
# The daily job's notebook: sizes and volume files for every catalog it can read
# ------------------------------------------------------------------------------------------------

def collect_sizes_notebook() -> str:
    return f'''# Databricks notebook source
# MAGIC %md
# MAGIC # Collect table and volume sizes
# MAGIC DESCRIBE DETAIL and DESCRIBE HISTORY on every Delta table and a file listing of every volume the
# MAGIC job can read, many at once. Writes `table_sizes`, `table_history` and `volume_sizes`, read by the
# MAGIC "{TITLE}" dashboard. Empty `catalogs` = every catalog.

# COMMAND ----------

dbutils.widgets.text("sizes_schema", "{SIZES_SCHEMA}")
dbutils.widgets.text("catalogs", "")
dbutils.widgets.text("threads", "16")
dbutils.widgets.text("max_tables", "20000")
dbutils.widgets.text("max_entries_per_volume", "50000")

# COMMAND ----------

import datetime
from concurrent.futures import ThreadPoolExecutor

out = dbutils.widgets.get("sizes_schema")
threads = int(dbutils.widgets.get("threads"))
max_tables = int(dbutils.widgets.get("max_tables"))
cap = int(dbutils.widgets.get("max_entries_per_volume"))
now = datetime.datetime.utcnow()
cats = [c.strip() for c in dbutils.widgets.get("catalogs").split(",") if c.strip()] or [
    r.catalog_name for r in spark.sql("SELECT catalog_name FROM system.information_schema.catalogs "
                                      "WHERE catalog_name NOT IN ({_SKIP})").collect()]
q = lambda s: "`" + s.replace("`", "``") + "`"
print(f"{{len(cats)}} catalogs")

# COMMAND ----------

tables = spark.sql(
    "SELECT table_catalog, table_schema, table_name FROM {LIST_FROM['tables_from']} "
    "WHERE table_type <> 'VIEW' AND (data_source_format IS NULL OR data_source_format = 'DELTA') "
    "AND table_schema <> 'information_schema' AND array_contains(:cats, table_catalog) "
    "ORDER BY last_altered DESC NULLS LAST LIMIT :n",  # the most recently changed first
    args={{"cats": cats, "n": max_tables}}).collect()

ROW_KEYS = ("numTargetRowsInserted", "numTargetRowsUpdated", "numTargetRowsDeleted", "numDeletedRows")
history_rows = []

def describe(t):
    name = ".".join(q(x) for x in (t.table_catalog, t.table_schema, t.table_name))
    try:
        d = spark.sql(f"DESCRIBE DETAIL {{name}}").collect()[0]
    except Exception as e:  # no access: kept with its error
        return (t.table_catalog, t.table_schema, t.table_name, None, None, None, "failed: " + str(e).splitlines()[0][:200], now)
    try:  # the last {HISTORY_VERSIONS} versions: what changed, when, how many rows
        for h in spark.sql(f"DESCRIBE HISTORY {{name}} LIMIT {HISTORY_VERSIONS}").collect():
            m = {{k: int(v) for k, v in (h.operationMetrics or {{}}).items() if str(v).isdigit()}}
            changed = sum(m.get(k, 0) for k in ROW_KEYS) or m.get("numOutputRows", 0)
            first = lambda *ks: next((m[k] for k in ks if k in m), None)  # noqa: E731
            history_rows.append((t.table_catalog, t.table_schema, t.table_name, h.version, h.timestamp, h.operation,
                                 h.userName, changed, m.get("numOutputBytes", 0),
                                 first("numAddedFiles", "numFilesAdded", "numTargetFilesAdded", "numFiles"),
                                 first("numRemovedFiles", "numFilesRemoved", "numTargetFilesRemoved", "numDeletedFiles", "numFilesToDelete"),
                                 first("numAddedBytes", "numTargetBytesAdded", "numOutputBytes"),
                                 first("numRemovedBytes", "numTargetBytesRemoved"),
                                 m.get("sizeOfDataToDelete") if str(h.operation).startswith("VACUUM") else None))
    except Exception:  # the size is still worth keeping
        pass
    return (t.table_catalog, t.table_schema, t.table_name, d.sizeInBytes, d.numFiles, str(d.lastModified), "described", now)

with ThreadPoolExecutor(threads) as pool:
    rows = list(pool.map(describe, tables))
(spark.createDataFrame(rows, "catalog string, schema string, table_name string, size_in_bytes bigint, num_files bigint, "
                             "last_modified string, detail string, collected_at timestamp")
      .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{{out}}.{SIZES_TABLES['table_sizes']}"))
(spark.createDataFrame(history_rows, "catalog string, schema string, table_name string, version bigint, timestamp timestamp, "
                                     "operation string, user_name string, rows_changed bigint, bytes_written bigint, "
                                     "files_added bigint, files_removed bigint, bytes_added bigint, bytes_removed bigint, "
                                     "freed_bytes bigint")
      .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{{out}}.{SIZES_TABLES['table_history']}"))
print(f"{{len(rows)}} tables, {{sum(1 for r in rows if r[6] == 'described')}} described, {{len(history_rows)}} history rows")

# COMMAND ----------

volumes = spark.sql(
    "SELECT volume_catalog, volume_schema, volume_name FROM {LIST_FROM['volumes_from']} "
    "WHERE array_contains(:cats, volume_catalog)", args={{"cats": cats}}).collect()

def walk(v):
    root = f"/Volumes/{{v.volume_catalog}}/{{v.volume_schema}}/{{v.volume_name}}/"
    files = size = seen = 0
    todo, cut = [root], False
    try:
        while todo and not cut:
            for f in dbutils.fs.ls(todo.pop(0)):
                seen += 1
                if f.isDir():
                    todo.append(f.path)
                else:
                    files += 1
                    size += f.size
                if seen >= cap:
                    cut = True
                    break
        return (v.volume_catalog, v.volume_schema, v.volume_name, files, size, cut, "listed", now)
    except Exception as e:
        return (v.volume_catalog, v.volume_schema, v.volume_name, None, None, False, "failed: " + str(e).splitlines()[0][:200], now)

with ThreadPoolExecutor(threads) as pool:
    vrows = list(pool.map(walk, volumes))
(spark.createDataFrame(vrows, "catalog string, schema string, volume_name string, files bigint, size_in_bytes bigint, "
                              "truncated boolean, detail string, collected_at timestamp")
      .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{{out}}.{SIZES_TABLES['volume_sizes']}"))
print(f"{{len(vrows)}} volumes")
'''
