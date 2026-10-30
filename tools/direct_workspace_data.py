"""tools/direct_workspace_data.py

Workspace data and usage: the app's Governance > Workspaces as a dashboard. One row per workspace,
with what the catalogs it is home to hold (tables, views, columns, volumes, files, size), who uses it,
a complexity and a usage index, and their scatter. Pages: Table usage, Volume usage, User activity.

A catalog's home workspace is the one that reads and writes it most (lineage), as in the app. Sizes
and volume files can't be read with SQL: a job writes them into <sizes schema>.table_sizes,
table_history and volume_sizes (see collect_sizes_notebook).
"""
from __future__ import annotations

from tools import build_databricks_dashboards as bdd
from tools.build_direct_dashboards import (
    END, START, Dataset, _page, bar, counter, dataset_json, field_filter, header, place, table, time_window,
)

TITLE = "Workspace data and usage"
P = "wdata"

SIZES_SCHEMA = "main.workspace_data"  # build_direct_dashboards.py --sizes-schema
HOME_DAYS = 90  # lineage days that decide a catalog's home workspace
INACTIVE_DAYS = 90
READ_LOOKBACK_DAYS = 365  # lineage's retention: how far back a table's last read is looked for
WRITE_OPS = "WRITE|MERGE|UPDATE|DELETE|STREAMING UPDATE|CREATE OR REPLACE|COPY INTO|TRUNCATE|RESTORE"
MAINT_OPS = "OPTIMIZE|VACUUM|ANALYZE|REORG"
SKIP_CATALOGS = ("system", "samples", "__databricks_internal")

HISTORY_VERSIONS = 100  # per table, newest first

# as the app (app/core/data.py): each input on a log scale against the largest workspace shown
COMPLEXITY = {"tables": 0.25, "views": 0.15, "columns": 0.2, "table_bytes": 0.25, "volume_files": 0.15}
USAGE = {"active_people": 0.4, "active_service_principals": 0.2, "queries": 0.4}

_UUID = "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'"
_SKIP = ", ".join(f"'{c}'" for c in SKIP_CATALOGS)


def _sized(table: str) -> str:
    return f"{SIZES_SCHEMA}.{table}"


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
  SELECT h.catalog, h.workspace_id,
         COALESCE(w.workspace_name, h.workspace_id, 'No home workspace') AS workspace
  FROM home h LEFT JOIN wsl w ON w.workspace_id = h.workspace_id
)"""


def _who() -> str:
    """Identities seen in each workspace: audit events and queries in the window."""
    return f"""aud AS (
  SELECT CAST(workspace_id AS STRING) AS workspace_id,
         lower(COALESCE(user_identity.email, user_identity.subject_name)) AS who,
         MAX(event_time) AS last_event,
         COUNT_IF(service_name = 'accounts' AND action_name RLIKE '(?i)login' AND response.status_code = 200) AS logins
  FROM system.access.audit
  WHERE event_date >= {START} AND event_date < {END}
  GROUP BY 1, 2
),
qh AS (
  SELECT CAST(workspace_id AS STRING) AS workspace_id, lower(executed_by) AS who,
         COUNT(*) AS queries, MAX(start_time) AS last_query
  FROM system.query.history
  WHERE start_time >= {START} AND start_time < {END}
  GROUP BY 1, 2
),
who AS (
  SELECT COALESCE(a.workspace_id, q.workspace_id) AS workspace_id, COALESCE(a.who, q.who) AS who,
         COALESCE(a.logins, 0) AS logins, COALESCE(q.queries, 0) AS queries,
         GREATEST(a.last_event, q.last_query) AS last_seen,
         CASE WHEN COALESCE(a.who, q.who) RLIKE {_UUID} THEN 'Service principal'
              WHEN COALESCE(a.who, q.who) LIKE '%@%' THEN 'Person' ELSE 'Other' END AS kind
  FROM aud a FULL OUTER JOIN qh q ON q.workspace_id = a.workspace_id AND q.who = a.who
)"""


def _index(weights: dict[str, float]) -> str:
    """0-100; an input no workspace has drops out, so the rest still reach 100 (as the app)."""
    num = " + ".join(f"{w} * COALESCE(ln(1 + {k}) / NULLIF(MAX(ln(1 + {k})) OVER (), 0), 0)" for k, w in weights.items())
    den = " + ".join(f"CASE WHEN MAX({k}) OVER () > 0 THEN {w} ELSE 0 END" for k, w in weights.items())
    return f"COALESCE(ROUND(100 * ({num}) / NULLIF({den}, 0), 1), 0)"


def workspaces() -> Dataset:
    return Dataset(
        "wd_workspaces", "Workspaces: what each holds and how much it is used",
        [], [],
        f"""{_homes()},
cols AS (
  SELECT table_catalog, table_schema, table_name, COUNT(*) AS n FROM system.information_schema.columns
  WHERE table_schema <> 'information_schema' GROUP BY 1, 2, 3
),
held AS (
  SELECT p.workspace_id, COUNT(DISTINCT p.catalog) AS catalogs,
         COUNT_IF(t.table_type <> 'VIEW') AS tables, COUNT_IF(t.table_type = 'VIEW') AS views,
         COALESCE(SUM(c.n), 0) AS columns
  FROM place p
  LEFT JOIN system.information_schema.tables t ON t.table_catalog = p.catalog AND t.table_schema <> 'information_schema'
  LEFT JOIN cols c ON c.table_catalog = t.table_catalog AND c.table_schema = t.table_schema AND c.table_name = t.table_name
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
{_who()},
act AS (
  SELECT workspace_id, COUNT(DISTINCT CASE WHEN kind = 'Person' THEN who END) AS active_people,
         COUNT(DISTINCT CASE WHEN kind = 'Service principal' THEN who END) AS active_service_principals,
         SUM(queries) AS queries, MAX(last_seen) AS last_activity
  FROM who GROUP BY 1
),
ids AS (
  SELECT workspace_id FROM act UNION SELECT workspace_id FROM held
),
base AS (
  SELECT i.workspace_id, COALESCE(w.workspace_name, i.workspace_id) AS workspace, w.workspace_url,
         COALESCE(h.catalogs, 0) AS catalogs, COALESCE(h.tables, 0) AS tables, COALESCE(h.views, 0) AS views,
         COALESCE(h.columns, 0) AS columns, COALESCE(v.volumes, 0) AS volumes,
         COALESCE(v.volume_files, 0) AS volume_files, COALESCE(s.table_bytes, 0) AS table_bytes,
         COALESCE(s.table_bytes, 0) + COALESCE(v.volume_bytes, 0) AS data_bytes,
         COALESCE(a.active_people, 0) AS active_people, COALESCE(a.active_service_principals, 0) AS active_service_principals,
         COALESCE(a.queries, 0) AS queries, a.last_activity
  FROM ids i
  LEFT JOIN held h ON h.workspace_id = i.workspace_id
  LEFT JOIN vols v ON v.workspace_id = i.workspace_id
  LEFT JOIN sizes s ON s.workspace_id = i.workspace_id
  LEFT JOIN act a ON a.workspace_id = i.workspace_id
  LEFT JOIN wsl w ON w.workspace_id = i.workspace_id
  WHERE i.workspace_id IS NOT NULL AND i.workspace_id <> '0'
)
SELECT workspace, workspace_id,
       concat('https://', regexp_replace(regexp_replace(COALESCE(workspace_url, ''), '^https?://', ''), '/.*$', '')) AS workspace_link,
       catalogs, tables, views, columns, volumes, volume_files, table_bytes, data_bytes,
       ROUND(data_bytes / 1073741824.0, 2) AS data_gb,
       active_people, active_service_principals, queries, CAST(last_activity AS STRING) AS last_activity,
       {_index(COMPLEXITY)} AS complexity_index,
       {_index(USAGE)} AS usage_index, 1 AS workspaces
FROM base
ORDER BY usage_index DESC, workspace""",
        "One row per workspace: tables, views, columns, volumes, files and size of the catalogs it is home to; "
        "people, service principals and queries in the window; and the two indexes.",
        "The Table usage page and the scatter.",
        "One row per workspace, most used first.",
        {"wsl": "Workspace id to name and URL.",
         "seen": f"Lineage reads and writes per catalog and workspace over {HOME_DAYS} days.",
         "home": "Each catalog's home workspace: the one with the most of them.",
         "place": "Each catalog with its home workspace's name.",
         "cols": "Columns per table and view.",
         "held": "Per workspace: catalogs, tables, views and columns of the catalogs it is home to.",
         "vols": "Per workspace: volumes, and their files and bytes from the job's volume_sizes.",
         "sizes": "Per workspace: bytes of its tables, from the job's table_sizes (DESCRIBE DETAIL).",
         "aud": "Audit events per workspace and identity in the window: last event and successful logins.",
         "qh": "Queries per workspace and identity in the window.",
         "who": "Both together: one row per workspace and identity, a person (email) or a service principal (UUID).",
         "act": "Per workspace: active people, service principals, queries and the last activity.",
         "ids": "Every workspace with activity or a catalog.",
         "base": "Everything per workspace; a missing count reads 0."},
        head="Dates from the Time Window.",
    )


def tables() -> Dataset:
    return Dataset(
        "wd_tables", "Tables and views: size, last read and write",
        [], [],
        f"""{_homes()},
t AS (
  SELECT t.table_catalog AS catalog, t.table_schema AS sch, t.table_name, t.table_type,
         t.table_catalog || '.' || t.table_schema || '.' || t.table_name AS full_name, CAST(t.last_altered AS STRING) AS last_altered
  FROM system.information_schema.tables t
  WHERE t.table_catalog NOT IN ({_SKIP}) AND t.table_schema <> 'information_schema'
),
lin AS (
  SELECT source_table_full_name AS full_name, 1 AS is_read, 0 AS is_write, event_time, created_by AS who
  FROM system.access.table_lineage
  WHERE event_date >= date_sub({END}, {READ_LOOKBACK_DAYS}) AND event_date < {END} AND source_table_full_name IS NOT NULL
  UNION ALL
  SELECT target_table_full_name, 0, 1, event_time, created_by FROM system.access.table_lineage
  WHERE event_date >= date_sub({END}, {READ_LOOKBACK_DAYS}) AND event_date < {END} AND target_table_full_name IS NOT NULL
),
used AS (
  -- last read and write over the lineage's year; counts in the Time Window
  SELECT full_name,
         SUM(CASE WHEN event_time >= {START} THEN is_read ELSE 0 END) AS reads,
         SUM(CASE WHEN event_time >= {START} THEN is_write ELSE 0 END) AS writes,
         MAX(CASE WHEN is_read = 1 THEN event_time END) AS last_read, MAX(CASE WHEN is_write = 1 THEN event_time END) AS last_write,
         COUNT(DISTINCT CASE WHEN is_read = 1 AND event_time >= {START} THEN who END) AS readers
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
)
SELECT p.workspace, t.catalog, t.sch AS schema_name, t.catalog || '.' || t.sch AS schema_full, t.full_name AS table_name,
       CASE WHEN t.table_type = 'VIEW' THEN 'View' ELSE 'Table' END AS kind,
       s.size_in_bytes, ROUND(s.size_in_bytes / 1073741824.0, 2) AS size_gb, s.num_files,
       COALESCE(u.reads, 0) AS reads, COALESCE(u.writes, 0) AS writes, COALESCE(u.readers, 0) AS readers,
       CAST(u.last_read AS STRING) AS last_read, datediff({END}, CAST(u.last_read AS DATE)) AS days_since_read,
       CASE WHEN u.last_read IS NULL THEN '4 not read in {READ_LOOKBACK_DAYS} days'
            WHEN datediff({END}, CAST(u.last_read AS DATE)) < 30 THEN '1 read in 30 days'
            WHEN datediff({END}, CAST(u.last_read AS DATE)) < 90 THEN '2 read 30-90 days ago'
            ELSE '3 read 90+ days ago' END AS read_band,
       CAST(GREATEST(u.last_write, h.last_change) AS STRING) AS last_write, lower(h.last_change_op) AS last_change_op,
       CAST(GREATEST(h.last_maintenance, po.last_po) AS STRING) AS last_maintenance,
       COALESCE(h.rows_changed, 0) AS rows_changed, ROUND(COALESCE(h.bytes_written, 0) / 1073741824.0, 2) AS gb_written,
       t.last_altered,
       CASE WHEN COALESCE(u.reads, 0) > 0 THEN 'Read' WHEN COALESCE(u.writes, 0) > 0 THEN 'Written, never read'
            ELSE 'Not used' END AS use, 1 AS objects
FROM t
LEFT JOIN place p ON p.catalog = t.catalog
LEFT JOIN {_sized('table_sizes')} s ON s.catalog = t.catalog AND s.schema = t.sch AND s.table_name = t.table_name
LEFT JOIN used u ON u.full_name = t.full_name
LEFT JOIN hist h ON h.catalog = t.catalog AND h.schema = t.sch AND h.table_name = t.table_name
LEFT JOIN po ON po.catalog = t.catalog AND po.schema = t.sch AND po.table_name = t.table_name
ORDER BY s.size_in_bytes DESC NULLS LAST, t.full_name""",
        f"Every table and view: its home workspace; size and files (job); last read and write over "
        f"{READ_LOOKBACK_DAYS} days and reads, writes and readers in the window (lineage); last change, last "
        "maintenance and rows changed (the job's DESCRIBE HISTORY, predictive optimization).",
        "The tables charts and table on Table usage.",
        "One row per table or view, biggest first.",
        {"wsl": "Workspace id to name and URL.",
         "seen": f"Lineage reads and writes per catalog and workspace over {HOME_DAYS} days.",
         "home": "Each catalog's home workspace: the one with the most of them.",
         "place": "Each catalog with its home workspace's name.",
         "t": "Every table and view, without the system catalogs.",
         "lin": f"Every read (source) and write (target) of a table over {READ_LOOKBACK_DAYS} days, from lineage.",
         "used": f"Per table: last read and write over {READ_LOOKBACK_DAYS} days; reads, writes, readers in the window.",
         "hist": "Per table, from the job's DESCRIBE HISTORY: last data change and its operation, last OPTIMIZE or "
                 "VACUUM, rows changed and bytes written in the window.",
         "po": "Per table: the last predictive optimization run (compaction, vacuum, clustering)."},
        head="Dates from the Time Window.",
    )


def volumes() -> Dataset:
    return Dataset(
        "wd_volumes", "Volumes: files, size and use",
        [], [],
        f"""{_homes()},
v AS (
  SELECT volume_catalog AS catalog, volume_schema AS sch, volume_name, volume_type,
         volume_catalog || '.' || volume_schema || '.' || volume_name AS full_name
  FROM system.information_schema.volumes WHERE volume_catalog NOT IN ({_SKIP})
),
ev AS (
  SELECT regexp_extract(COALESCE(request_params['path'], request_params['file_path'], request_params['directory_path']),
                        '^/Volumes/([^/]+)/([^/]+)/([^/]+)', 0) AS path_root,
         lower(COALESCE(user_identity.email, user_identity.subject_name)) AS who, event_time,
         action_name RLIKE '(?i)put|upload|create|delete|write|mkdir' AS is_write
  FROM system.access.audit
  WHERE event_date >= {START} AND event_date < {END} AND service_name = 'filesystem' AND response.status_code = 200
),
used AS (
  SELECT replace(substr(path_root, 10), '/', '.') AS full_name, COUNT_IF(NOT is_write) AS reads, COUNT_IF(is_write) AS writes,
         COUNT(DISTINCT who) AS users, MAX(event_time) AS last_access
  FROM ev WHERE path_root <> '' GROUP BY 1
)
SELECT p.workspace, v.catalog, v.sch AS schema_name, v.full_name AS volume, lower(v.volume_type) AS volume_type,
       vs.files, vs.size_in_bytes, ROUND(vs.size_in_bytes / 1073741824.0, 2) AS size_gb,
       COALESCE(u.reads, 0) AS reads, COALESCE(u.writes, 0) AS writes, COALESCE(u.users, 0) AS users,
       CAST(u.last_access AS STRING) AS last_access,
       CASE WHEN COALESCE(u.reads, 0) + COALESCE(u.writes, 0) > 0 THEN 'Used' ELSE 'Not used' END AS use, 1 AS volumes
FROM v
LEFT JOIN place p ON p.catalog = v.catalog
LEFT JOIN {_sized('volume_sizes')} vs ON vs.catalog = v.catalog AND vs.schema = v.sch AND vs.volume_name = v.volume_name
LEFT JOIN used u ON u.full_name = v.full_name
ORDER BY vs.size_in_bytes DESC NULLS LAST, v.full_name""",
        "Every volume: its home workspace, files and size (job), file reads and writes and their users in the "
        "window (the audit log's file-system events).",
        "The Volume usage page.",
        "One row per volume, biggest first.",
        {"wsl": "Workspace id to name and URL.",
         "seen": f"Lineage reads and writes per catalog and workspace over {HOME_DAYS} days.",
         "home": "Each catalog's home workspace: the one with the most of them.",
         "place": "Each catalog with its home workspace's name.",
         "v": "Every volume, without the system catalogs.",
         "ev": "File-system events under /Volumes in the window: who, when, read or write.",
         "used": "Per volume: reads, writes, users and the last access."},
        head="Dates from the Time Window.",
    )


def people() -> Dataset:
    return Dataset(
        "wd_people", "People and service principals: activity and last seen",
        [], [],
        f"""wsl AS (
  SELECT CAST(workspace_id AS STRING) AS workspace_id, workspace_name FROM system.access.workspaces_latest
),
{_who()},
year AS (
  SELECT CAST(workspace_id AS STRING) AS workspace_id, lower(COALESCE(user_identity.email, user_identity.subject_name)) AS who,
         MAX(event_date) AS last_seen_year
  FROM system.access.audit
  WHERE event_date >= date_sub({END}, 365) AND event_date < {END}
  GROUP BY 1, 2
),
k AS (
  SELECT y.workspace_id, y.who, y.last_seen_year, w.logins, w.queries, w.last_seen,
         CASE WHEN y.who RLIKE {_UUID} THEN 'Service principal' WHEN y.who LIKE '%@%' THEN 'Person' ELSE 'Other' END AS kind
  FROM year y LEFT JOIN who w ON w.workspace_id = y.workspace_id AND w.who = y.who
)
SELECT COALESCE(n.workspace_name, k.workspace_id) AS workspace, k.who AS identity, k.kind,
       COALESCE(k.logins, 0) AS logins, COALESCE(k.queries, 0) AS queries,
       CAST(COALESCE(k.last_seen, k.last_seen_year) AS STRING) AS last_seen,
       datediff({END}, CAST(k.last_seen_year AS DATE)) AS days_since,
       CASE WHEN datediff({END}, CAST(k.last_seen_year AS DATE)) < 30 THEN '1 under 30 days'
            WHEN datediff({END}, CAST(k.last_seen_year AS DATE)) < 90 THEN '2 30-90 days'
            WHEN datediff({END}, CAST(k.last_seen_year AS DATE)) < 180 THEN '3 90-180 days'
            ELSE '4 180+ days' END AS last_seen_band,
       CASE WHEN datediff({END}, CAST(k.last_seen_year AS DATE)) >= {INACTIVE_DAYS} THEN 1 ELSE 0 END AS inactive,
       CASE WHEN COALESCE(k.logins, 0) + COALESCE(k.queries, 0) > 0 THEN 1 ELSE 0 END AS active, 1 AS identities
FROM k
LEFT JOIN wsl n ON n.workspace_id = k.workspace_id
WHERE k.kind <> 'Other' AND k.workspace_id IS NOT NULL AND k.workspace_id <> '0'
ORDER BY days_since DESC, identity""",
        "Everyone with an audit event in a workspace over the last year: activity in the window, last seen, "
        f"and inactive when not seen for {INACTIVE_DAYS}+ days.",
        "The User activity page.",
        "One row per workspace and identity, longest away first.",
        {"wsl": "Workspace id to name.",
         "aud": "Audit events per workspace and identity in the window: last event and successful logins.",
         "qh": "Queries per workspace and identity in the window.",
         "who": "Both together, with the kind: a person (email) or a service principal (UUID).",
         "year": "Each identity's last audit event per workspace over the last year.",
         "k": "Last seen in the year joined to the window's activity."},
        head="Dates from the Time Window; last seen over a year.",
    )


DATASETS: list[Dataset] = [workspaces(), tables(), volumes(), people()]


def configure(sizes_schema: str | None) -> None:
    """Sets where the sizes job writes, then rebuilds the queries."""
    global SIZES_SCHEMA
    SIZES_SCHEMA = sizes_schema or SIZES_SCHEMA
    DATASETS[:] = [workspaces(), tables(), volumes(), people()]


def _scatter(key: str, d: Dataset) -> dict:
    """Complexity (x) by usage (y), one point per workspace, sized by its data."""
    q = {"name": bdd.MAIN_QUERY, "query": {"datasetName": d.name, "disaggregated": True,
         "fields": [bdd.bare_field(c) for c in ("complexity_index", "usage_index", "data_gb", "workspace")]}}
    return {"widget": {"name": bdd.hid("widget:scatter:" + key), "queries": [q], "spec": {
        "version": 3, "widgetType": "scatter",
        "encodings": {
            "x": {"fieldName": "complexity_index", "displayName": "Complexity index", "scale": {"type": "quantitative"}},
            "y": {"fieldName": "usage_index", "displayName": "Usage index", "scale": {"type": "quantitative"}},
            "size": {"fieldName": "data_gb", "displayName": "Data GB", "scale": {"type": "quantitative"}},
            "label": {"show": False},
        },
        "frame": {"title": "Complexity against usage", "showTitle": True,
                  "description": "Bubble = data size; right and low: built but little used", "showDescription": True},
    }}}


def dashboard() -> dict:
    S, T, V, U = DATASETS[0], DATASETS[1], DATASETS[2], DATASETS[3]
    filters = lambda key, ds: (1, [(time_window(f"{key}:time", ds), 6),  # noqa: E731
                                   (field_filter(f"{key}:ws", "workspace", "Workspace", ds), 6)])
    usage = place([
        (2, [header("wd:tab:head", "Table usage",
                    "One row per workspace: what the catalogs it is home to hold, who uses it, and a complexity and a "
                    "usage index. Sizes come from the daily job.")]),
        filters("wd:tab", [S, T]),
        (3, [counter("wd:tab:n", S, "workspaces", "Workspaces", "With activity or a catalog"),
             counter("wd:tab:size", S, "data_gb", "Data, GB", "Tables and volume files"),
             counter("wd:tab:tables", S, "tables", "Tables", "In their home catalogs"),
             counter("wd:tab:views", S, "views", "Views", "In their home catalogs")]),
        (8, [_scatter("wd:tab:scatter", S)]),
        (7, [bar("wd:tab:by_size", S, "workspace", "Workspace", "data_gb", "GB", "Data by workspace",
                 "Tables and volume files", money=False),
             bar("wd:tab:use", T, "workspace", "Workspace", "objects", "Tables and views", "Tables by last read, by workspace",
                 f"Read in 30 days, 30-90, 90+ days ago, or not in {READ_LOOKBACK_DAYS} days", money=False,
                 color="read_band")]),
        (7, [bar("wd:tab:schema", T, "schema_full", "Schema", "objects", "Tables and views", "Tables by last read, per schema",
                 "Where the unread tables sit", money=False, color="read_band"),
             bar("wd:tab:unread_gb", T, "schema_full", "Schema", "size_gb", "GB", "GB by last read, per schema",
                 "Big and unread first: the clean-up list", money=False, color="read_band")]),
        (10, [table("wd:tab:table", S, [
            ("workspace", "Workspace", "text"), ("workspace_link", "Link", "text"), ("catalogs", "Catalogs", "int"),
            ("tables", "Tables", "int"), ("views", "Views", "int"), ("columns", "Columns", "int"),
            ("volumes", "Volumes", "int"), ("volume_files", "Volume files", "int"), ("data_gb", "GB", "num"),
            ("complexity_index", "Complexity", "num"), ("active_people", "Active people", "int"),
            ("active_service_principals", "Active SPs", "int"), ("queries", "Queries", "int"),
            ("usage_index", "Usage", "num")], "Every workspace, most used first")]),
        (10, [table("wd:tab:objects", T, [
            ("workspace", "Workspace", "text"), ("table_name", "Table", "text"), ("kind", "Kind", "text"),
            ("size_gb", "GB", "num"), ("num_files", "Files", "int"), ("last_read", "Last read", "text"),
            ("days_since_read", "Days since read", "int"), ("last_write", "Last write", "text"),
            ("last_change_op", "Change", "text"), ("last_maintenance", "Last OPTIMIZE / VACUUM", "text"),
            ("rows_changed", "Rows changed", "int"), ("gb_written", "GB written", "num"), ("readers", "Readers", "int")],
            "Every table and view, biggest first")]),
    ])
    vol = place([
        (2, [header("wd:vol:head", "Volume usage",
                    "Volumes by home workspace: files and size (daily job), and who read or wrote files in them in "
                    "the window.")]),
        filters("wd:vol", [V]),
        (3, [counter("wd:vol:n", V, "volumes", "Volumes", "Every catalog"),
             counter("wd:vol:files", V, "files", "Files", "Listed by the job"),
             counter("wd:vol:gb", V, "size_gb", "Size, GB", "Listed by the job"),
             counter("wd:vol:writes", V, "writes", "File writes", "In the window")]),
        (7, [bar("wd:vol:by_size", V, "workspace", "Workspace", "size_gb", "GB", "Volume size by workspace",
                 "Listed by the job", money=False),
             bar("wd:vol:use", V, "workspace", "Workspace", "volumes", "Volumes", "Volumes used and not used, by workspace",
                 "File reads or writes in the window", money=False, color="use")]),
        (10, [table("wd:vol:table", V, [
            ("workspace", "Workspace", "text"), ("volume", "Volume", "text"), ("volume_type", "Type", "text"),
            ("files", "Files", "int"), ("size_gb", "GB", "num"), ("reads", "Reads", "int"), ("writes", "Writes", "int"),
            ("users", "Users", "int"), ("last_access", "Last access", "text"), ("use", "Use", "text")],
            "Every volume, biggest first")]),
    ])
    users = place([
        (2, [header("wd:usr:head", "User activity",
                    f"Who works in each workspace, and who stopped coming: not seen for {INACTIVE_DAYS}+ days within "
                    "the last year.")]),
        filters("wd:usr", [U]),
        (3, [counter("wd:usr:active", U, "active", "Active", "People and SPs in the window"),
             counter("wd:usr:inactive", U, "inactive", "Inactive", f"Not seen {INACTIVE_DAYS}+ days"),
             counter("wd:usr:queries", U, "queries", "Queries", "In the window"),
             counter("wd:usr:logins", U, "logins", "Logins", "In the window")]),
        (7, [bar("wd:usr:by_ws", U, "workspace", "Workspace", "active", "Active", "Active people and SPs by workspace",
                 "By kind", money=False, color="kind"),
             bar("wd:usr:bands", U, "workspace", "Workspace", "identities", "Identities", "Last seen, by workspace",
                 "Days since the last audit event", money=False, color="last_seen_band")]),
        (10, [table("wd:usr:table", U, [
            ("workspace", "Workspace", "text"), ("identity", "Who", "text"), ("kind", "Kind", "text"),
            ("logins", "Logins", "int"), ("queries", "Queries", "int"), ("last_seen", "Last seen", "text"),
            ("days_since", "Days since", "int")], "Everyone, longest away first")]),
    ])
    return bdd.tag_dashboard_queries({
        "datasets": [dataset_json(d) for d in DATASETS],
        "pages": [_page("tables", "Table usage", usage, prefix=P), _page("volumes", "Volume usage", vol, prefix=P),
                  _page("users", "User activity", users, prefix=P)],
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
    "SELECT table_catalog, table_schema, table_name FROM system.information_schema.tables "
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
            history_rows.append((t.table_catalog, t.table_schema, t.table_name, h.version, h.timestamp, h.operation,
                                 h.userName, changed, m.get("numOutputBytes", 0)))
    except Exception:  # the size is still worth keeping
        pass
    return (t.table_catalog, t.table_schema, t.table_name, d.sizeInBytes, d.numFiles, str(d.lastModified), "described", now)

with ThreadPoolExecutor(threads) as pool:
    rows = list(pool.map(describe, tables))
(spark.createDataFrame(rows, "catalog string, schema string, table_name string, size_in_bytes bigint, num_files bigint, "
                             "last_modified string, detail string, collected_at timestamp")
      .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{{out}}.table_sizes"))
(spark.createDataFrame(history_rows, "catalog string, schema string, table_name string, version bigint, timestamp timestamp, "
                                     "operation string, user_name string, rows_changed bigint, bytes_written bigint")
      .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{{out}}.table_history"))
print(f"{{len(rows)}} tables, {{sum(1 for r in rows if r[6] == 'described')}} described, {{len(history_rows)}} history rows")

# COMMAND ----------

volumes = spark.sql(
    "SELECT volume_catalog, volume_schema, volume_name FROM system.information_schema.volumes "
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
      .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{{out}}.volume_sizes"))
print(f"{{len(vrows)}} volumes")
'''
