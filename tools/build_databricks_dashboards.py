"""tools/build_databricks_dashboards.py

Builds the Lakehouse Health set in databricks_dashboards/: one Databricks AI/BI dashboard per app
tab (tools/dashboard_screens/<tab>.py, laid out by tools/dashboard_kit.py), the snapshot notebook
they read from, and a README. The dashboards read only the snapshot's tables, never system.*, so
opening or refreshing one never re-runs a check and every viewer sees the same numbers.

Internal ids are 8-hex hashes of a stable key, so a rerun on the same inputs writes byte-identical
files (tests/test_databricks_dashboards.py checks this).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DIRECT_SQL_DIR = REPO_ROOT / "app" / "direct_sql"
NAMES_DIR = DIRECT_SQL_DIR / "names"
OUT_DIR = REPO_ROOT / "databricks_dashboards"
SETTINGS_PATH = REPO_ROOT / "config" / "settings.yml"

# A live run's values for the markers app/direct_sql/*.sql carries: a fixed 30-day window, and
# "now" computed when the snapshot runs, so a rerun months later still means today.
WINDOW_DAYS = 30
AS_OF_DATE_SQL = "current_date()"
AS_OF_TS_SQL = "current_timestamp()"
SCHEMA_NAME = "databricks_audit_dashboard"

# Checks the snapshot runs first, in this order. cost_failed_statement_waste is last on purpose:
# it reads system.billing.attributed_usage, a grant some accounts never gave, so a failure there
# costs only that table.
CHECKS = [
    "compute_warehouse_idle_minutes",
    "compute_idle_node_ratio",
    "lakeflow_failed_jobs_wasted_dbus",
    "compute_serving_endpoint_cost_status",
    "instance_pools_idle_capacity",
    "lakeflow_jobs_on_all_purpose",
    "lakeflow_stale_zombie_jobs",
    "lakeflow_pipeline_idle_tail_duration",
    "compute_serving_dormant_endpoints",
    "cost_premium_serverless_photon",
    "compute_warehouse_cache_reuse",
    "cost_dollarized_by_sku_day",
    "cost_period_over_period",
    "cost_monthly_actuals",
    "cost_unnamed_workspaces",
    "query_failed_queries_daily",
    "query_queuing_waits",
    "perf_daily_by_resource",
    "lakeflow_job_reliability",
    "lakeflow_job_duration_regression",
    "cost_chargeback_by_warehouse",
    "cost_chargeback_by_cluster",
    "cost_failed_statement_waste",
]

# name -> source file under app/direct_sql/names/; snapshotted the same way as a check so dashboard
# datasets can always resolve a name instead of showing a bare id (SQL: OUTER, keep all rows memory;
# always-by-name memory).
DIMS = ["dim_workspace", "dim_warehouse", "dim_job", "dim_cluster", "dim_pipeline", "dim_notebook"]

MARKER_RE = re.compile(r"__[A-Z_]+__")


def fill_placeholders(sql: str, label: str) -> str:
    """A live run's values for the three app/direct_sql markers; raises if any survive."""
    sql = sql.replace("__WINDOW_DAYS__", str(WINDOW_DAYS))
    sql = sql.replace("__AS_OF_DATE__", AS_OF_DATE_SQL)
    sql = sql.replace("__AS_OF_TS__", AS_OF_TS_SQL)
    sql = sql.replace("__QUERY_SOURCE__", query_source())
    # __REDACTED__ is the value Databricks writes in place of a hidden grantee, not a marker
    left = sorted(set(MARKER_RE.findall(sql)) - {"__REDACTED__"})
    if left:
        raise SystemExit(f"{label}: unfilled marker(s) {left}")
    return sql


def read_check(query_id: str) -> str:
    text = (DIRECT_SQL_DIR / f"{query_id}.sql").read_text(encoding="utf-8")
    return fill_placeholders(text, query_id)


def read_dim(name: str) -> str:
    text = (NAMES_DIR / f"{name}.sql").read_text(encoding="utf-8")
    return fill_placeholders(text, name)


def hid(key: str) -> str:
    """Deterministic 8-hex id (hash of a stable key) -- regenerating gives the same file."""
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]


def _settings() -> dict:
    """config/settings.yml, with config/settings.local.yml's own keys merged on top -- the same
    precedence app/core/config.load_settings uses, so a discount (or brand) set the normal way,
    through the gitignored local override file, is not silently ignored here."""
    settings = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8")) or {}
    local_path = SETTINGS_PATH.parent / "settings.local.yml"
    if local_path.exists():
        local = yaml.safe_load(local_path.read_text(encoding="utf-8")) or {}
        settings = {**settings, **local}
    return settings


def brand_name() -> str:
    return _settings().get("brand", {}).get("name") or "Databricks audit"


def query_source() -> str:
    """The app's own query tag (app/core/config.query_source): settings query_source, else the
    brand with anything but letters, digits, _ and - dropped."""
    return _settings().get("query_source") or re.sub(r"[^A-Za-z0-9_-]", "", brand_name())


def discount_pct() -> float:
    """config/settings.yml discount_pct -- the SAME fraction app/core/data.py's callers multiply
    every $ figure by (1 - discount_pct); applied here in dataset SQL instead of at read time."""
    return float(_settings().get("discount_pct") or 0.0)


def tag_keys() -> list[str]:
    """The tags every resource should carry (settings mandatory_tag_keys), in that order."""
    return [str(k) for k in (_settings().get("mandatory_tag_keys") or [])]


# Who pays: the first of these tags a usage row carries charges it back (the notebook's default).
CHARGEBACK_KEYS = "cost_center,domain"
ALLOCATION_CHECK = "cost_chargeback_by_allocation_tag"
# The notebook's own queries carry these tags, so their cost reads as this set's.
QUERY_TAG_VALUE = "lakehouse_health"
QUERY_TAGS = ",".join(f"{k}={QUERY_TAG_VALUE}" for k in CHARGEBACK_KEYS.split(","))
# Overview's links to the other dashboards; the notebook looks up their ids, set at import.
LINKS_TABLE = "dashboard_links"


def allocation_keys_literal() -> str:
    """The key list baked into the allocation check, which the notebook swaps for its widget."""
    m = re.search(r"replace\(replace\(replace\('([^']*)', ' ', ''\)", read_check(ALLOCATION_CHECK))
    if not m:
        raise SystemExit(f"{ALLOCATION_CHECK}: allocation key list not found")
    return m.group(0)


def discount_factor_sql() -> str:
    """A literal `(1.0 - <d>)` to multiply straight into a dataset's own $ SELECT expressions."""
    return f"(1.0 - {discount_pct()!r})"


def query_lines(sql: str) -> list[str]:
    """queryLines = the SQL text split on real newlines, each element keeping its own trailing \\n
    (format doc: concatenation must reproduce the SQL exactly)."""
    return sql.splitlines(keepends=True)


# --------------------------------------------------------------------------------------------
# The snapshot notebook -- run by the owner only. Wraps each check/dim's own SQL unchanged, plus one
# shared snapshot_at so every table a run creates carries the same instant.
# --------------------------------------------------------------------------------------------

# Checks reading only these always-on system tables run first.
CORE_SOURCES = ("billing.usage", "billing.list_prices", "access.workspaces_latest", "query.history",
                "compute.", "lakeflow.")
_ANY_SYSTEM_TABLE_RE = re.compile(r"`?system`?\.`?([a-z_]+)`?\.`?([a-z_]+)`?")

# The dim columns the dashboards read, for the empty table a failed dim leaves behind.
_DIM_EMPTY = {
    "dim_workspace": "workspace_id STRING, name STRING",
    "dim_warehouse": "workspace_id STRING, warehouse_id STRING, warehouse_name STRING",
    "dim_job": "workspace_id STRING, job_id STRING, name STRING",
    "dim_cluster": "workspace_id STRING, cluster_id STRING, cluster_name STRING",
    "dim_pipeline": "workspace_id STRING, pipeline_id STRING, pipeline_name STRING",
    "dim_notebook": "workspace_id STRING, notebook_id STRING, notebook_path STRING",
}


def snapshot_order() -> list[str]:
    """all_checks(), those reading only CORE_SOURCES first."""
    def core_only(query_id: str) -> bool:
        tables = {f"{a}.{b}" for a, b in _ANY_SYSTEM_TABLE_RE.findall(read_check(query_id))}
        return all(t.startswith(CORE_SOURCES) for t in tables)

    checks = all_checks()
    core = [q for q in checks if core_only(q)]
    return core + [q for q in checks if q not in core]


def has_status(query_id: str) -> bool:
    return any(c.lower() == "status" for c, _ in load_columns()[query_id])


def empty_ddl(name: str) -> str:
    if name in _DIM_EMPTY:
        return "snapshot_at TIMESTAMP, " + _DIM_EMPTY[name]
    cols = ", ".join(f"`{c}` {to_spark_type(t)}" for c, t in load_columns()[name])
    return "snapshot_at TIMESTAMP, " + cols


def _py_str(sql: str) -> str:
    # a raw triple-quoted string reads like the SQL itself; JSON escaping only if it cannot hold it
    if '"""' in sql or sql.endswith("\\"):
        return json.dumps(sql)
    return 'r"""\n' + sql + '\n"""'


def build_snapshot_notebook(catalog: str | None) -> str:
    """The snapshot as a Databricks notebook: every dim and check as a table, carrying on past a
    failed check with an empty table and the reason in snapshot_log."""
    order = snapshot_order()
    cell = "\n\n# COMMAND ----------\n\n"
    head = "\n".join([
        "# Databricks notebook source",
        "# MAGIC %md",
        f"# MAGIC # {PREFIX}: snapshot",
        "# MAGIC",
        f"# MAGIC Runs every check once and saves the results in `<catalog>.{SCHEMA_NAME}`; the dashboards in this",
        "# MAGIC folder read only those tables.",
        "# MAGIC",
        "# MAGIC - Set **catalog** in the widgets above; change the tag widgets only if your keys differ.",
        "# MAGIC - **Run all**. A check that fails (usually a missing grant) leaves an empty table and its reason in",
        "# MAGIC   `snapshot_log`; the rest still run.",
        "# MAGIC - Rerun for new numbers.",
    ])
    widget = "\n".join([
        f'dbutils.widgets.text("catalog", {json.dumps(catalog or "")}, "Catalog")',
        f'dbutils.widgets.text("tag_keys", {json.dumps(",".join(tag_keys()))}, "Tags every resource should carry")',
        f'dbutils.widgets.text("chargeback_keys", {json.dumps(CHARGEBACK_KEYS)}, "Tags that say who pays, first match wins")',
        f'dbutils.widgets.text("query_tags", {json.dumps(QUERY_TAGS)}, "Tags on this notebook\'s own queries")',
    ])
    dims = "# names for the ids the checks return (app/direct_sql/names)\nDIMS = {}\n" + "".join(
        f'DIMS["{n}"] = {_py_str(read_dim(n).rstrip(chr(10)))}\n' for n in DIMS
    )
    checks = "# every check, unchanged from the app (app/direct_sql)\nCHECKS = {}\n" + "".join(
        f'CHECKS["{q}"] = {_py_str(read_check(q).rstrip(chr(10)))}\n' for q in order
    )
    empty = "# the columns a check returns, for the empty table a failure leaves behind\nEMPTY = {\n" + "".join(
        f'    "{n}": {json.dumps(empty_ddl(n))},\n' for n in list(DIMS) + order
    ) + "}\nHAS_STATUS = {\n" + "".join(f'    "{q}",\n' for q in order if has_status(q)) + "}"
    run = f'''import re
import time
from concurrent.futures import ThreadPoolExecutor

CATALOG = dbutils.widgets.get("catalog").strip()
if not CATALOG:
    raise ValueError("Set the catalog widget at the top, then run all.")
# Query tags mark these statements in system.query.history; the bill takes the job's or budget policy's tags.
for pair in dbutils.widgets.get("query_tags").split(","):
    key, _, value = (s.strip() for s in pair.partition("="))
    if key and value and re.fullmatch(r"[\\w .-]+", key + value):
        try:
            spark.sql(f"SET QUERY_TAGS['{{key}}'] = '{{value}}'")
        except Exception as e:  # compute without query tags: carry on untagged
            print(f"query tags not set on this compute: {{(str(e).splitlines() or [''])[0][:200]}}")
            break
TAG_KEYS = ",".join(k.strip() for k in dbutils.widgets.get("tag_keys").split(",") if k.strip() and "'" not in k)
CHARGEBACK_KEYS = ",".join(k.strip() for k in dbutils.widgets.get("chargeback_keys").split(",") if k.strip() and "'" not in k)
CHECKS["{ALLOCATION_CHECK}"] = CHECKS["{ALLOCATION_CHECK}"].replace(
    {json.dumps(allocation_keys_literal())}, "replace(replace(replace('" + CHARGEBACK_KEYS + "', ' ', '')")
SCHEMA = "{SCHEMA_NAME}"
spark.sql(f"USE CATALOG `{{CATALOG}}`")
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {{SCHEMA}}")
SNAPSHOT_AT = spark.sql("SELECT CAST(current_timestamp() AS STRING) AS t").first().t
TS = f"CAST('{{SNAPSHOT_AT}}' AS TIMESTAMP)"


def snapshot(name, sql):
    started = time.time()
    try:
        spark.sql(f"CREATE OR REPLACE TABLE {{SCHEMA}}.{{name}} AS\\nSELECT {{TS}} AS snapshot_at, q.* FROM (\\n{{sql}}\\n) q")
        error = None
    except Exception as e:  # a missing grant or table: keep going, leave an empty table
        error = (str(e).strip().splitlines() or ["failed"])[0][:500]
        spark.sql(f"CREATE OR REPLACE TABLE {{SCHEMA}}.{{name}} ({{EMPTY[name]}})")
    seconds = round(time.time() - started, 1)
    flags = "count_if(status = 'CRITICAL'), count_if(status = 'WARN')" if name in HAS_STATUS else "0, 0"
    n, critical, warn = spark.sql(f"SELECT count(*), {{flags}} FROM {{SCHEMA}}.{{name}}").first()
    print(("ok  " if error is None else "NOT ") + f"{{name}} {{seconds}}s" + (f": {{error}}" if error else ""))
    return (name, error is None, error, n, critical, warn, name in HAS_STATUS, seconds)


log = [snapshot(n, s) for n, s in DIMS.items()]
spark.sql(
    f"CREATE OR REPLACE TABLE {{SCHEMA}}.snapshot_info AS SELECT {{TS}} AS snapshot_at, "
    f"{WINDOW_DAYS} AS window_days, '{{TAG_KEYS}}' AS tag_keys, '{{CHARGEBACK_KEYS}}' AS chargeback_keys, "
    f"'{{','.join(CHECKS)}}' AS checks"
)
with ThreadPoolExecutor(max_workers=4) as pool:
    log += list(pool.map(lambda item: snapshot(*item), CHECKS.items()))

(spark.createDataFrame(log, "query_id STRING, ok BOOLEAN, error STRING, row_count BIGINT, critical BIGINT, "
                            "warn BIGINT, has_status BOOLEAN, seconds DOUBLE")
    .selectExpr("*", f"{{TS}} AS snapshot_at")
    .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{{SCHEMA}}.snapshot_log"))
failed = [r for r in log if not r[1]]
print(f"{{len(log) - len(failed)}} of {{len(log)}} tables created; {{len(failed)}} not assessed (see snapshot_log)")'''
    links = f'''# Overview links to every dashboard of the set. Databricks gives each imported dashboard its own id,
# so they are looked up by name: after importing the dashboards, run this cell again.
CATALOG = dbutils.widgets.get("catalog").strip()
rows = []
try:
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    host = w.config.host.rstrip("/")
    latest = {{}}
    for d in w.lakeview.list():
        name = d.display_name or ""
        if name.startswith("{PREFIX} ") and (name not in latest or (d.update_time or "") > (latest[name].update_time or "")):
            latest[name] = d
    rows = [(name, f"{{host}}/dashboardsv3/{{d.dashboard_id}}/published") for name, d in sorted(latest.items())]
except Exception as e:  # no SDK or no right to list dashboards: Overview shows no links
    print(f"dashboard links not read: {{(str(e).splitlines() or [''])[0][:200]}}")
(spark.createDataFrame(rows, "dashboard STRING, url STRING")
    .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"`{{CATALOG}}`.{SCHEMA_NAME}.{LINKS_TABLE}"))
print(f"{{len(rows)}} dashboards linked from Overview" + ("" if rows else "; import them, then run this cell again"))'''
    return head + cell + widget + cell + dims + cell + checks + cell + empty + cell + run + cell + links + "\n"


# --------------------------------------------------------------------------------------------
# Datasets -- plain SELECTs over <schema>.* snapshot tables only. Aggregation, materiality floors,
# the discount and per-card filters all live here, never in the check SQL (which the snapshot ran
# unchanged).
# --------------------------------------------------------------------------------------------

WORKSPACE_PARAM = {
    "displayName": "Workspace",
    "keyword": "workspace",
    "dataType": "STRING",
    "complexType": "MULTI",
    "defaultSelection": {"values": {"dataType": "STRING", "values": [{"value": "all"}]}},
}

# CASE mapping shared by every dataset that breaks spend down by product (app's own labels).
PRODUCT_LABEL_CASE_SQL = """CASE {col}
           WHEN 'DBSQL' THEN 'Databricks SQL' WHEN 'SQL' THEN 'SQL' WHEN 'DLT' THEN 'DLT'
           WHEN 'INTERACTIVE' THEN 'Serverless notebooks' WHEN 'ALL_PURPOSE' THEN 'All-purpose clusters'
           WHEN 'JOBS' THEN 'Jobs' WHEN 'MODEL_SERVING' THEN 'Model serving'
           ELSE COALESCE(initcap(replace({col}, '_', ' ')), '(unlabeled product)')
         END"""


def waste_cte(schema: str) -> str:
    """The 5 priced waste checks, floored, discounted and named -- shared by every Waste & savings
    dataset so the floor logic (config/materiality.yml) and the discount (config/settings.yml
    discount_pct) each live in one place. 50.0 * window_days / 30.0 mirrors app/core/materiality.py's
    USD-floor scaling; the floor itself always compares the RAW (undiscounted) dollar figure, same
    as materiality.py -- only the figures this CTE hands callers (`wasted`) are discounted.

    `named_all` carries every priced row (not just the ones over the floor, unlike `named`) with its
    workspace resolved, for the below-floor and overlap notes; `named` stays the floored, flagged,
    ranked set the KPIs/tables use. Idle cluster nodes gets its workspace from dim_cluster (never
    "Account-level" just because compute_idle_node_ratio carries no workspace_id of its own) -- the
    same cluster->workspace mapping app/core/data.py's _in_workspaces already filters it through."""
    disc = discount_factor_sql()
    return f"""WITH src AS (
  SELECT 'Warehouse idle time' AS source_label, workspace_id, warehouse_id AS entity_id, 'warehouse' AS entity_kind,
         status, ROUND(est_wasted_usd_list * {disc}, 2) AS wasted, est_wasted_usd_list AS floor_value, window_days
  FROM {schema}.compute_warehouse_idle_minutes
  UNION ALL
  SELECT 'Idle cluster nodes', dc.workspace_id, w.cluster_id, 'cluster',
         w.status, ROUND(w.est_wasted_usd_list * {disc}, 2), w.est_wasted_usd_list, w.window_days
  FROM {schema}.compute_idle_node_ratio w
  LEFT JOIN {schema}.dim_cluster dc ON dc.cluster_id = w.cluster_id
  UNION ALL
  SELECT 'Compute burned on failed runs', workspace_id, job_id, 'job',
         status, ROUND(est_wasted_usd_list * {disc}, 2), CAST(NULL AS DOUBLE), window_days
  FROM {schema}.lakeflow_failed_jobs_wasted_dbus
  UNION ALL
  SELECT 'Compute burned on failed statements', workspace_id, warehouse_id, 'warehouse',
         status, ROUND(est_wasted_usd_list * {disc}, 2), est_wasted_usd_list, window_days
  FROM {schema}.cost_failed_statement_waste
  UNION ALL
  SELECT 'Endpoints billed with no requests', workspace_id, endpoint_id, 'endpoint',
         status, ROUND(est_wasted_usd_list * {disc}, 2), est_usd_list, window_days
  FROM {schema}.compute_serving_endpoint_cost_status
),
eff AS (
  SELECT *,
         CASE WHEN status IN ('CRITICAL', 'WARN') AND floor_value IS NOT NULL
                   AND floor_value < 50.0 * window_days / 30.0
              THEN 'OK' ELSE status END AS eff_status
  FROM src
),
named_all AS (
  SELECT e.*, COALESCE(ws.name, e.workspace_id, 'Account-level') AS workspace_name
  FROM eff e
  LEFT JOIN {schema}.dim_workspace ws ON ws.workspace_id = e.workspace_id
),
flagged AS (
  SELECT source_label, workspace_id, entity_id, entity_kind, wasted
  FROM eff
  WHERE eff_status IN ('CRITICAL', 'WARN') AND wasted > 0
),
named AS (
  SELECT f.source_label, f.entity_kind, f.wasted,
         COALESCE(ws.name, f.workspace_id, 'Account-level') AS workspace_name,
         COALESCE(dw.warehouse_name, dc.cluster_name, dj.name, ep.endpoint_name, f.entity_id) AS resource_name
  FROM flagged f
  LEFT JOIN {schema}.dim_workspace ws ON ws.workspace_id = f.workspace_id
  LEFT JOIN {schema}.dim_warehouse dw ON f.entity_kind = 'warehouse' AND dw.warehouse_id = f.entity_id
  LEFT JOIN {schema}.dim_cluster dc ON f.entity_kind = 'cluster' AND dc.cluster_id = f.entity_id
  LEFT JOIN {schema}.dim_job dj ON f.entity_kind = 'job' AND dj.workspace_id = f.workspace_id AND dj.job_id = f.entity_id
  LEFT JOIN {schema}.compute_serving_endpoint_cost_status ep
    ON f.entity_kind = 'endpoint' AND ep.workspace_id = f.workspace_id AND ep.endpoint_id = f.entity_id
)
"""


def autostop_cte(schema: str) -> str:
    """Per-warehouse auto-stop saving at the lowest setting Databricks allows: 1 minute on
    serverless (through the API; the UI stops at 5), 10 on pro and classic. 0 means auto-stop is
    off, which always saves if idle minutes exist. Carries the extra cold starts that setting
    causes. Only compute_warehouse_idle_minutes's own columns."""
    disc = discount_factor_sql()
    return f"""WITH w AS (
  SELECT m.*, COALESCE(dw.warehouse_name, m.warehouse_id) AS resource_name,
         COALESCE(ws.name, m.workspace_id, 'Account-level') AS workspace_name
  FROM {schema}.compute_warehouse_idle_minutes m
  LEFT JOIN {schema}.dim_warehouse dw ON dw.warehouse_id = m.warehouse_id
  LEFT JOIN {schema}.dim_workspace ws ON ws.workspace_id = m.workspace_id
),
autostop AS (
  SELECT *,
         CASE WHEN warehouse_kind = 'serverless' THEN 1 ELSE 10 END AS target_minutes,
         CASE WHEN warehouse_kind = 'serverless' THEN autostop_1_idle_minutes ELSE autostop_10_idle_minutes END AS target_idle_minutes,
         CASE WHEN warehouse_kind = 'serverless' THEN autostop_1_cold_starts ELSE autostop_10_cold_starts END AS target_cold_starts
  FROM w
),
saving AS (
  SELECT *,
         CASE WHEN auto_stop_minutes IS NOT NULL AND (auto_stop_minutes = 0 OR auto_stop_minutes > target_minutes)
                   AND autostop_now_idle_minutes IS NOT NULL AND target_idle_minutes IS NOT NULL
                   AND usd_per_cluster_minute IS NOT NULL
              THEN ROUND((autostop_now_idle_minutes - target_idle_minutes) * usd_per_cluster_minute * {disc}, 2)
         END AS saving_usd,
         GREATEST(COALESCE(target_cold_starts, 0) - COALESCE(autostop_now_cold_starts, 0), 0) AS extra_cold_starts
  FROM autostop
)
"""


# --------------------------------------------------------------------------------------------
# Widget builders -- shapes from docs/lakeview-dashboard-format.md and real imports: a table lists
# its dataset's rows (disaggregated), a chart or counter aggregates one measure.
# --------------------------------------------------------------------------------------------

CURRENCY_FORMAT = {
    "type": "number-currency",
    "currencyCode": "USD",
    "abbreviation": "compact",
    "decimalPlaces": {"type": "max", "places": 0},
}


def bare_field(col: str) -> dict:
    return {"name": col, "expression": f"`{col}`"}


# A chart, counter or table reads the query named main_query; a real export names it nothing else.
MAIN_QUERY = "main_query"


def query_block(query_name: str, dataset_name: str, fields: list[str]) -> dict:
    """A table's query: every row as it is."""
    return {
        "name": MAIN_QUERY,
        "query": {
            "datasetName": dataset_name,
            "fields": [bare_field(c) for c in fields],
            "disaggregated": True,
        },
    }


def measure_name(col: str, agg: str) -> str:
    return f"{agg.lower()}({col})"


def chart_query(query_name: str, dataset_name: str, groups: list[str], measure: str, agg: str = "SUM") -> dict:
    """A chart's or counter's query, as a real export writes it: grouping fields bare, the one
    measure aggregated, disaggregated false."""
    return {
        "name": MAIN_QUERY,
        "query": {
            "datasetName": dataset_name,
            "fields": [bare_field(c) for c in groups]
            + [{"name": measure_name(measure, agg), "expression": f"{agg}(`{measure}`)"}],
            "disaggregated": False,
        },
    }


def text_widget(key: str, lines: list[str], width: int, height: int, x: int, y: int) -> dict:
    return {
        "widget": {"name": hid(f"widget:text:{key}"), "multilineTextboxSpec": {"lines": lines}},
        "position": {"x": x, "y": y, "width": width, "height": height},
    }


def counter_widget(
    key: str,
    dataset_name: str,
    value_col: str,
    title: str,
    *,
    target_col: str | None = None,
    currency: bool = False,
    color: str = "#1c1b18",
    agg: str = "SUM",
    width: int,
    height: int,
    x: int,
    y: int,
) -> dict:
    qname = f"q_{hid('qry:' + key)}"
    value_enc: dict = {
        "fieldName": measure_name(value_col, agg),
        "displayName": title,
        "style": {"bold": True, "fontSize": 32, "color": color},
    }
    if currency:
        value_enc["format"] = dict(CURRENCY_FORMAT)
    encodings = {"value": value_enc}
    if target_col:
        encodings["target"] = {
            "fieldName": target_col,
            "displayName": target_col,
            "style": {"fontSize": 14, "italic": True, "color": {"themeColorType": "fontColor"}},
        }
    return {
        "widget": {
            "name": hid(f"widget:counter:{key}"),
            "queries": [chart_query(qname, dataset_name, [target_col] if target_col else [], value_col, agg)],
            "spec": {
                "version": 2,
                "widgetType": "counter",
                "encodings": encodings,
                "frame": {"title": title, "showTitle": True},
            },
        },
        "position": {"x": x, "y": y, "width": width, "height": height},
    }


def bar_widget(
    key: str,
    dataset_name: str,
    category_col: str,
    value_col: str,
    title: str,
    *,
    category_label: str,
    value_label: str,
    color_col: str | None = None,
    temporal: bool = False,
    currency: bool = False,
    agg: str = "SUM",
    width: int,
    height: int,
    x: int,
    y: int,
) -> dict:
    """category_col is the grouping axis (categorical, or temporal when temporal=True); value_col
    is the quantitative measure. docs/lakeview-dashboard-format.md: every temporal bar in the
    verified sample has x=temporal, y=quantitative (never the reverse) -- a non-temporal bar keeps
    the quantitative value on x and the category on y, sorted by "x-reversed" (largest first)."""
    qname = f"q_{hid('qry:' + key)}"
    value_enc: dict = {"fieldName": measure_name(value_col, agg), "displayName": value_label,
                       "scale": {"type": "quantitative"}}
    if currency:
        value_enc["format"] = dict(CURRENCY_FORMAT)
    if temporal:
        encodings = {
            "x": {"fieldName": category_col, "displayName": category_label, "scale": {"type": "temporal"}},
            "y": value_enc,
            "label": {"show": False},
        }
    else:
        encodings = {
            "x": value_enc,
            "y": {"fieldName": category_col, "displayName": category_label,
                  "scale": {"type": "categorical", "sort": {"by": "x-reversed"}}},
            "label": {"show": False},
        }
    spec: dict = {
        "version": 3,
        "widgetType": "bar",
        "encodings": encodings,
        "frame": {"title": title, "showTitle": True},
    }
    if color_col:
        encodings["color"] = {
            "fieldName": color_col,
            "displayName": color_col,
            "legend": {"position": "bottom"},
            "scale": {"type": "categorical"},
        }
        spec["mark"] = {"layout": "stack"}
    return {
        "widget": {
            "name": hid(f"widget:bar:{key}"),
            "queries": [chart_query(qname, dataset_name, [category_col] + ([color_col] if color_col else []),
                                    value_col, agg)],
            "spec": spec,
        },
        "position": {"x": x, "y": y, "width": width, "height": height},
    }


def _table_string_column(field: str, title: str, order: int, *, align: str = "left") -> dict:
    return {
        "fieldName": field, "title": title, "type": "string", "displayAs": "string",
        "order": order, "visible": True, "alignContent": align,
        "allowHTML": False, "allowSearch": True, "useMonospaceFont": False,
        "preserveWhitespace": False, "highlightLinks": False,
        "booleanValues": ["false", "true"], "dateTimeFormat": "YYYY-MM-DD",
        "linkUrlTemplate": "{{ @ }}", "linkTextTemplate": "{{ @ }}", "linkTitleTemplate": "{{ @ }}",
        "linkOpenInNewTab": True, "imageUrlTemplate": "{{ @ }}", "imageTitleTemplate": "{{ @ }}",
        "imageWidth": "", "imageHeight": "",
    }


def _table_number_column(field: str, title: str, order: int, *, number_format: str = "0") -> dict:
    return {
        "fieldName": field, "title": title, "type": "float", "displayAs": "number",
        "order": order, "visible": True, "alignContent": "right", "numberFormat": number_format,
        "allowHTML": False, "preserveWhitespace": False, "highlightLinks": False,
        "booleanValues": ["false", "true"], "useMonospaceFont": False,
        "linkUrlTemplate": "{{ @ }}", "linkTextTemplate": "{{ @ }}", "linkTitleTemplate": "{{ @ }}",
        "linkOpenInNewTab": True, "imageUrlTemplate": "{{ @ }}", "imageTitleTemplate": "{{ @ }}",
        "imageWidth": "", "imageHeight": "",
    }


def table_widget(
    key: str,
    dataset_name: str,
    columns: list[tuple[str, str, str]],  # (field, title, "string"|"number")
    title: str,
    *,
    width: int,
    height: int,
    x: int,
    y: int,
) -> dict:
    fields = [c[0] for c in columns]
    qname = f"q_{hid('qry:' + key)}"
    col_specs = []
    for i, (field, col_title, kind) in enumerate(columns):
        if kind == "number":
            col_specs.append(_table_number_column(field, col_title, i))
        else:
            col_specs.append(_table_string_column(field, col_title, i))
    return {
        "widget": {
            "name": hid(f"widget:table:{key}"),
            "queries": [query_block(qname, dataset_name, fields)],
            "spec": {
                "version": 1,
                "widgetType": "table",
                "allowHTMLByDefault": False,
                "condensed": False,
                "withRowNumber": False,
                "itemsPerPage": 20,
                "paginationSize": "default",
                "invisibleColumns": [],  # required on import, even when empty
                "encodings": {"columns": col_specs},
                "frame": {**({"title": title} if title else {}), "showTitle": bool(title), "showDescription": False},
            },
        },
        "position": {"x": x, "y": y, "width": width, "height": height},
    }


def workspace_filter_widget(
    dashboard_key: str, lookup_dataset_name: str, targets: list[tuple[str, str]], *, x: int, y: int, width: int, height: int
) -> dict:
    """One global multi-select reaching every dataset that carries a resolved workspace name.
    targets: [(logical_dataset_key, dataset_name), ...]. Format doc: one lookup query (the option
    list, disaggregated:false, with a COUNT_IF(`associative_filter_predicate_group`) companion
    field for live option counts) + one parameter-set query per target dataset, no batching."""
    lookup_qname = f"q_{hid('lookup:workspace:' + dashboard_key)}"
    fields = [{"fieldName": "workspace_name", "displayName": "Workspace", "queryName": lookup_qname}]
    lookup_query = {
        "name": lookup_qname,
        "query": {
            "datasetName": lookup_dataset_name,
            "fields": [
                bare_field("workspace_name"),
                {"name": "workspace_name_associativity", "expression": "COUNT_IF(`associative_filter_predicate_group`)"},
            ],
            "disaggregated": False,
        },
    }
    queries = [lookup_query]
    for key, dataset_name in targets:
        qname = f"q_{hid('param:workspace:' + key)}"
        queries.append(
            {
                "name": qname,
                "query": {
                    "datasetName": dataset_name,
                    "parameters": [{"name": "workspace", "keyword": "workspace"}],
                    "disaggregated": False,
                },
            }
        )
        fields.append({"parameterName": "workspace", "queryName": qname})
    return {
        "widget": {
            "name": hid("widget:filter:workspace"),
            "queries": queries,
            "spec": {
                "version": 2,
                "widgetType": "filter-multi-select",
                "encodings": {"fields": fields},
                "frame": {"title": "Workspace", "showTitle": True},
            },
        },
        "position": {"x": x, "y": y, "width": width, "height": height},
    }


# --------------------------------------------------------------------------------------------
# uiSettings -- light/dark theme tokens copied from web/src/styles/styles.css :root blocks.
# --------------------------------------------------------------------------------------------

def build_ui_settings() -> dict:
    return {
        "theme": {
            "canvasBackgroundColor": {"light": "#f4f1ea", "dark": "#0d1015"},
            "widgetBackgroundColor": {"light": "#ffffff", "dark": "#151a21"},
            "widgetBorderColor": {"light": "#e3ded2", "dark": "#262d38"},
            "fontColor": {"light": "#1c1b18", "dark": "#e7eaee"},
            "selectionColor": {"light": "#2356a0", "dark": "#5aa9ff"},
            # --c1..--c7, --c-other, --st-crit, --st-ok (light): the app's own chart + status colors.
            "visualizationColors": [
                "#2356a0", "#d9822b", "#3e8e88", "#7a6fb0", "#9a7b4f",
                "#b8527a", "#5f7f2a", "#b9b3a5", "#a8261d", "#2f6b4b",
            ],
            "widgetHeaderAlignment": "LEFT",
        },
        "genieSpace": {"isEnabled": False, "enablementMode": "ENABLEMENT_MODE_UNSPECIFIED"},
        "applyModeEnabled": False,
    }


# --------------------------------------------------------------------------------------------
# Tab dashboards -- every app screen (web/src/components/tab_registry.ts), one page per sub-tab,
# in the app's own words (labels.ts, columns.ts), names shown as "name (id)".
# --------------------------------------------------------------------------------------------

WEB_COMPONENTS = REPO_ROOT / "web" / "src" / "components"
COLUMNS_JSON = Path(__file__).resolve().parent / "dashboard_columns.json"
TEST_DB = REPO_ROOT / "tests" / "db_audit_test.duckdb"
ROW_LIMIT = 5000

# The app's sidebar order (web/src/components/roles.ts): one dashboard per tab, one page per
# sub-tab. Each tab's screens live in tools/dashboard_screens/<tab>.py.
TABS = ["overview", "actions", "jobs", "compute", "queries", "cost", "waste", "mlai", "genie",
        "governance", "storage", "tags", "findings", "coverage"]
# a developer probe the app never shows
SKIPPED_CHECKS = {"lakeflow_termination_type_probe"}
# every file of the set shares the prefix; Databricks names an imported dashboard after its file
PREFIX = "Lakehouse Health"
NOTEBOOK_FILE = f"{PREFIX} 00 - Snapshot.py"


def dashboard_name(tab: str) -> str:
    return f"{PREFIX} {TABS.index(tab) + 1:02d} - {area_registry()[tab]['label']}"


def dashboard_file(tab: str) -> str:
    return f"{dashboard_name(tab)}.lvdash.json"
_SUBTAB_CONSTS = {
    "mlai": "MLAI_SUBTABS", "cost": "COST_SUBTABS", "compute": "COMPUTE_SUBTABS", "jobs": "JOBS_SUBTABS",
    "queries": "QUERY_SUBTABS", "governance": "GOVERNANCE_SUBTABS", "storage": "STORAGE_SUBTABS",
}
# the app's own home order: a check listed on two screens lives on the first (tab_registry.ts)
_HOME_ORDER = ["mlai", "cost", "compute", "jobs", "queries", "governance", "storage", "genie", "coverage"]


def _ts_source(name: str) -> str:
    # line comments dropped; "https://" survives because a colon sits before its slashes
    text = (WEB_COMPONENTS / name).read_text(encoding="utf-8")
    return re.sub(r"(^|\s)//[^\n]*", r"\1", text)


def _quoted_ids(text: str | None) -> list[str]:
    return re.findall(r'"([a-z0-9_]+)"', text or "")


def area_registry() -> dict[str, dict]:
    """{area: {label, scope, pages: [(key, label, ids, ref_ids)]}} from tab_registry.ts, keeping
    only checks that have a Databricks SQL file, each on its home page."""
    ts = _ts_source("tab_registry.ts")
    areas: dict[str, dict] = {}
    for m in re.finditer(r'^\s+(\w+): \{ label: "([^"]+)", scope: "([^"]+)"', ts, re.M):
        areas[m.group(1)] = {"label": m.group(2), "scope": m.group(3), "pages": []}
    for area, const in _SUBTAB_CONSTS.items():
        block = re.search(rf"const {const}: Subtab\[\] = \[(.*?)\n\];", ts, re.S).group(1)
        for chunk in re.split(r"\bkey:\s*", block)[1:]:
            key, label = re.match(r'"(\w+)",\s*label:\s*"([^"]+)"', chunk).groups()
            ids = re.search(r"\bids:\s*\[(.*?)\]", chunk, re.S)
            refs = re.search(r"\brefIds:\s*\[(.*?)\]", chunk, re.S)
            areas[area]["pages"].append(
                (key, label, _quoted_ids(ids and ids.group(1)), _quoted_ids(refs and refs.group(1)))
            )
    genie = re.search(r"genie: \{[^\n]*refIds: \[([^\]]*)\]", ts).group(1)
    areas["genie"]["pages"] = [("usage", "Genie", [], _quoted_ids(genie))]
    sources = re.search(r"const COVERAGE_SOURCES_IDS = \[([^\]]*)\]", ts).group(1)
    refs = re.search(r"const COVERAGE_REF_IDS = \[([^\]]*)\]", ts).group(1)
    areas["coverage"]["pages"] = [("sources", "Data sources", _quoted_ids(sources), _quoted_ids(refs))]

    seen: set[str] = set()
    for area in _HOME_ORDER:
        kept = []
        for key, label, ids, refs in areas[area]["pages"]:
            ids = [q for q in ids if q not in seen and (DIRECT_SQL_DIR / f"{q}.sql").exists()]
            seen.update(ids)
            refs = [q for q in refs if q not in seen and (DIRECT_SQL_DIR / f"{q}.sql").exists()]
            seen.update(refs)
            if ids or refs:
                kept.append((key, label, ids, refs))
        areas[area]["pages"] = kept
    return areas


def area_checks() -> list[str]:
    """Every check the app places on a tab, in page order."""
    out: list[str] = []
    for area in area_registry().values():
        for _, _, ids, refs in area["pages"]:
            out += ids + refs
    return out


def all_checks() -> list[str]:
    """What the snapshot creates: CHECKS, every placed check, then the rest of app/direct_sql, so
    any screen can read any check."""
    out = list(CHECKS)
    out += [q for q in area_checks() if q not in out]
    out += [p.stem for p in sorted(DIRECT_SQL_DIR.glob("*.sql")) if p.stem not in out and p.stem not in SKIPPED_CHECKS]
    return out


def check_labels() -> dict[str, tuple[str, str | None]]:
    """query_id -> (title, why), the words the app shows (labels.ts)."""
    ts = (WEB_COMPONENTS / "labels.ts").read_text(encoding="utf-8")
    pat = re.compile(
        r'^\s*([a-z0-9_]+):\s*\{\s*title:\s*("(?:[^"\\]|\\.)*"),\s*why:\s*(null|"(?:[^"\\]|\\.)*")\s*\}', re.M
    )
    return {
        m.group(1): (json.loads(m.group(2)), None if m.group(3) == "null" else json.loads(m.group(3)))
        for m in pat.finditer(ts)
    }


def check_title(query_id: str, labels: dict) -> str:
    return labels[query_id][0] if query_id in labels else _humanize(query_id)


def _curated_columns() -> dict[str, tuple[str, str | None]]:
    """column -> (label, unit) from columns.ts CURATED."""
    ts = _ts_source("columns.ts")
    block = ts[ts.index("const CURATED"):]
    block = block[: block.index("\n};")]
    pat = re.compile(
        r'^\s*([a-z0-9_]+):\s*\{\s*label:\s*("(?:[^"\\]|\\.)*")(?:,\s*unit:\s*(null|"(?:[^"\\]|\\.)*"))?', re.M
    )
    out = {}
    for m in pat.finditer(block):
        unit = m.group(3)
        out[m.group(1)] = (json.loads(m.group(2)), None if unit in (None, "null") else json.loads(unit))
    return out


_WORD_FIXES = {"dbus": "DBUs", "dbu": "DBU", "usd": "$", "pct": "%", "est": "Est.", "cpu": "CPU",
               "mem": "memory", "gb": "GB", "ui": "UI", "url": "URL"}


def _humanize(name: str) -> str:
    # columns.ts humanizeColumnName
    s = re.sub(r"_id$", "", re.sub(r"_disc$", "", name)) or name
    words = []
    for w in (x for x in s.split("_") if x):
        lw = w.lower()
        if lw in _WORD_FIXES:
            words.append(_WORD_FIXES[lw])
        elif re.fullmatch(r"p(50|90|95|99)", lw):
            words.append(lw)
        else:
            words.append(lw[:1].upper() + lw[1:])
    return " ".join(words) or name


def column_title(name: str, curated: dict) -> str:
    if name in curated:
        label, unit = curated[name]
    else:
        for pat, unit in ((r"_ms$", "ms"), (r"_minutes$", "min"), (r"_seconds$", "s"), (r"(^|_)s$", "s")):
            if re.search(pat, name):
                label = _humanize(re.sub(pat, "", name) or name)
                break
        else:
            label, unit = _humanize(name), None
    return f"{label} ({unit})" if unit and not label.endswith(unit) else label


def refresh_columns() -> None:
    """tools/dashboard_columns.json: every check's output columns and types, read from the test
    DuckDB (tests/db_audit_test.duckdb, built by dbt_run.py build --target test)."""
    import duckdb

    con = duckdb.connect(str(TEST_DB), read_only=True)
    out = {}
    for path in sorted(DIRECT_SQL_DIR.glob("*.sql")):
        rows = con.execute(f'DESCRIBE findings."f_{path.stem}"').fetchall()
        out[path.stem] = [[r[0], r[1]] for r in rows]
    COLUMNS_JSON.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {COLUMNS_JSON}")


def load_columns() -> dict[str, list[tuple[str, str]]]:
    raw = json.loads(COLUMNS_JSON.read_text(encoding="utf-8"))
    return {q: [(c, t) for c, t in cols] for q, cols in raw.items()}


_INT_TYPES = {"BIGINT", "HUGEINT", "INTEGER", "SMALLINT", "TINYINT", "UBIGINT", "UINTEGER"}


def _is_number(duck_type: str) -> bool:
    return duck_type in _INT_TYPES or duck_type in {"DOUBLE", "FLOAT", "REAL"} or duck_type.startswith("DECIMAL")


def _shown_expr(col: str, duck_type: str) -> str:
    """How a column reaches a table cell: numbers as numbers, everything else as text."""
    c = f"t.`{col}`"
    if duck_type.startswith("TIMESTAMP"):
        return f"date_format({c}, 'yyyy-MM-dd HH:mm')"
    if duck_type.startswith(("STRUCT", "MAP")) or duck_type.endswith("]"):
        return f"to_json({c})"
    if _is_number(duck_type) or duck_type == "VARCHAR":
        return c
    return f"CAST({c} AS STRING)"


def _number_format(name: str, duck_type: str) -> str:
    if re.search(r"(^|_)(usd|cost)(_|$)", name):
        return "$0,0.[00]"
    return "0,0" if duck_type in _INT_TYPES else "0,0.[00]"


def to_spark_type(duck_type: str) -> str:
    """DuckDB type -> Spark DDL, for the empty table a failed check leaves behind."""
    t = duck_type.strip()
    if t.endswith("[]"):
        return f"ARRAY<{to_spark_type(t[:-2])}>"
    if t.startswith("MAP("):
        k, v = _split_top(t[4:-1])
        return f"MAP<{to_spark_type(k)}, {to_spark_type(v)}>"
    if t.startswith("STRUCT("):
        fields = []
        for part in _split_top(t[7:-1]):
            name, typ = part.strip().split(" ", 1)
            fields.append(f"{name}: {to_spark_type(typ)}")
        return f"STRUCT<{', '.join(fields)}>"
    if t.startswith("DECIMAL"):
        return t
    return {
        "VARCHAR": "STRING", "JSON": "STRING", "DOUBLE": "DOUBLE", "FLOAT": "FLOAT", "REAL": "FLOAT",
        "BIGINT": "BIGINT", "HUGEINT": "DECIMAL(38,0)", "UBIGINT": "DECIMAL(20,0)", "INTEGER": "INT",
        "UINTEGER": "BIGINT", "SMALLINT": "SMALLINT", "TINYINT": "TINYINT", "BOOLEAN": "BOOLEAN",
        "DATE": "DATE", "TIMESTAMP": "TIMESTAMP", "TIMESTAMP WITH TIME ZONE": "TIMESTAMP",
    }.get(t, "STRING")


def _split_top(s: str) -> list[str]:
    # split on commas outside parentheses
    parts, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return [p.strip() for p in parts]


def _own_order(query_id: str, shown: set[str]) -> list[str]:
    """The check's own ORDER BY, kept where it names a column the table shows (a CTAS drops it)."""
    import sqlglot
    from sqlglot import exp

    tree = sqlglot.parse_one(read_check(query_id), dialect="databricks")
    src = tree.args.get("from_")
    inner = src.this.this if src is not None and isinstance(src.this, exp.Subquery) else tree
    order = inner.args.get("order")
    keys = []
    for o in (order.expressions if order is not None else []):
        e = o.this
        if isinstance(e, exp.Column) and e.name.lower() in shown and e.name.lower() != "status":
            keys.append(f"`{e.name.lower()}` {'DESC' if o.args.get('desc') else 'ASC'} NULLS LAST")
    return keys


# (id column, name column a check may already carry, dim, the dim's name column, shown as)
_NAME_JOINS = [
    ("workspace_id", "workspace_name", "dim_workspace", "name", "workspace"),
    ("warehouse_id", "warehouse_name", "dim_warehouse", "warehouse_name", "warehouse"),
    ("job_id", "job_name", "dim_job", "name", "job"),
    ("cluster_id", "cluster_name", "dim_cluster", "cluster_name", "cluster"),
    ("pipeline_id", "pipeline_name", "dim_pipeline", "pipeline_name", "pipeline"),
    ("notebook_id", "notebook_path", "dim_notebook", "notebook_path", "notebook"),
]


def check_dataset(schema: str, query_id: str, cols: list[tuple[str, str]], curated: dict):
    """(sql, table columns, filters by workspace) for one check: names as "name (id)" first,
    status next, the check's own columns after; worst rows first, then the check's own order."""
    by_lower = {c.lower(): c for c, _ in cols}
    select, joins, shown = [], [], []
    hidden = {"window_days", "snapshot_at"}
    ws_name = None
    for id_col, name_col, dim, dim_name, key in _NAME_JOINS:
        if id_col not in by_lower:
            continue
        alias = f"n_{key}"
        keys = [id_col] if id_col == "workspace_id" or "workspace_id" not in by_lower else ["workspace_id", id_col]
        on = " AND ".join(f"{alias}.{k} = CAST(t.`{by_lower[k]}` AS STRING)" for k in keys)
        key_list = ", ".join(f"CAST({k} AS STRING) AS {k}" for k in keys)
        group = ", ".join(f"CAST({k} AS STRING)" for k in keys)
        joins.append(
            f"LEFT JOIN (SELECT {key_list}, max({dim_name}) AS n FROM {schema}.{dim} GROUP BY {group}) {alias}\n"
            f"  ON {on}"
        )
        name = f"COALESCE(t.`{by_lower[name_col]}`, {alias}.n)" if name_col in by_lower else f"{alias}.n"
        id_text = f"CAST(t.`{by_lower[id_col]}` AS STRING)"
        # A NULL id is either the pooled Other row or spend outside any warehouse/job/cluster.
        none = "NULL" if key == "workspace" else (
            f"CASE WHEN t.`{by_lower['is_other']}` THEN 'Other' ELSE '(no {key})' END" if "is_other" in by_lower else f"'(no {key})'")
        select.append(f"COALESCE(concat({name}, ' (', {id_text}, ')'), {id_text}, {none}) AS `{key}_shown`")
        shown.append((f"{key}_shown", key.capitalize(), "string", None))
        hidden |= {id_col, name_col}
        if key == "workspace":
            ws_name = name
    for col, typ in sorted(cols, key=lambda c: c[0].lower() != "status"):
        lc = col.lower()
        if lc in hidden:
            continue
        select.append(f"{_shown_expr(col, typ)} AS `{lc}`")
        kind = "number" if _is_number(typ) else "string"
        shown.append((lc, column_title(lc, curated), kind, _number_format(lc, typ) if kind == "number" else None))
    order = []
    if "status" in by_lower:
        order.append("CASE `status` WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END")
    order += _own_order(query_id, {s[0] for s in shown})
    sql = "SELECT\n  " + ",\n  ".join(select) + f"\nFROM {schema}.{query_id} t\n"
    sql += "".join(j + "\n" for j in joins)
    if ws_name:
        sql += f"WHERE (array_contains(:workspace, COALESCE({ws_name}, '')) OR array_contains(:workspace, 'all'))\n"
    if order:
        sql += "ORDER BY " + ", ".join(order) + "\n"
    sql += f"LIMIT {ROW_LIMIT}\n"
    return sql, shown, ws_name is not None


def _sql_str(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _status_sql(schema: str, entries: list[tuple[str, str, str]]) -> str:
    """One row per check: its verdict from snapshot_log. entries: (query_id, title, page)."""
    values = ",\n       ".join(
        f"({i}, '{q}', {_sql_str(title)}, {_sql_str(page)})" for i, (q, title, page) in enumerate(entries)
    )
    return f"""SELECT c.title AS check_title,
       c.page AS page,
       CASE WHEN l.query_id IS NULL THEN 'Not run'
            WHEN NOT l.ok THEN 'Not assessed'
            WHEN l.critical > 0 THEN 'Critical'
            WHEN l.warn > 0 THEN 'Warn'
            WHEN l.has_status THEN 'OK'
            WHEN l.row_count = 0 THEN 'No rows'
            ELSE 'Listed' END AS verdict,
       l.row_count AS row_count,
       l.critical AS critical_rows,
       l.warn AS warn_rows,
       l.error AS error
FROM (VALUES
       {values}) AS c(ord, query_id, title, page)
LEFT JOIN {schema}.snapshot_log l ON l.query_id = c.query_id
ORDER BY CASE WHEN l.critical > 0 THEN 0 WHEN l.warn > 0 THEN 1 WHEN NOT l.ok THEN 2 ELSE 3 END, c.ord
"""


_STATUS_COLUMNS = [
    ("check_title", "Check", "string", None),
    ("page", "Page", "string", None),
    ("verdict", "Verdict", "string", None),
    ("critical_rows", "Critical rows", "number", "0,0"),
    ("warn_rows", "Warn rows", "number", "0,0"),
    ("row_count", "Rows", "number", "0,0"),
    ("error", "Why not assessed", "string", None),
]


def data_table_widget(key: str, dataset_name: str, columns: list, title: str, *, x: int, y: int,
                      width: int, height: int) -> dict:
    """table_widget with a number format per column: columns are (field, title, kind, format)."""
    w = table_widget(key, dataset_name, [(f, t, k) for f, t, k, _ in columns], title,
                     x=x, y=y, width=width, height=height)
    for spec, (_, _, kind, fmt) in zip(w["widget"]["spec"]["encodings"]["columns"], columns):
        if kind == "number" and fmt:
            spec["numberFormat"] = fmt
    return w


def build_tab(schema: str, tab: str) -> dict:
    """One tab's dashboard: its screen module (tools/dashboard_screens/<tab>.py) when there is one;
    any sub-tab it leaves out gets the plain page, a status table and every check's rows."""
    import importlib
    import sys

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from tools import dashboard_kit as kit

    d = kit.Dashboard(schema, tab, area_registry()[tab]["label"], dashboard_name(tab))
    if (Path(__file__).resolve().parent / "dashboard_screens" / f"{tab}.py").exists():
        importlib.import_module(f"tools.dashboard_screens.{tab}").build(d)
    return d.to_json()


# --------------------------------------------------------------------------------------------
# README
# --------------------------------------------------------------------------------------------

def system_tables_read() -> list[str]:
    """Every system table the checks and dims read, taken from their own SQL."""
    tables: set[str] = set()
    for query_id in all_checks():
        tables |= {f"system.{a}.{b}" for a, b in _ANY_SYSTEM_TABLE_RE.findall(read_check(query_id))}
    for name in DIMS:
        tables |= {f"system.{a}.{b}" for a, b in _ANY_SYSTEM_TABLE_RE.findall(read_dim(name))}
    return sorted(tables)


def build_readme(schema: str, pages: dict[str, list[str]]) -> str:
    tables_md = "\n".join(f"- `{t}`" for t in system_tables_read())
    dash_md = "\n".join(f"| `{dashboard_file(tab)}` | {', '.join(pages[tab])} |" for tab in TABS)
    keys = ",".join(tag_keys())
    tag_pairs = ", ".join(f"`{k}: {QUERY_TAG_VALUE}`" for k in CHARGEBACK_KEYS.split(","))
    first, last = dashboard_name(TABS[0])[len(PREFIX) + 1:], dashboard_name(TABS[-1])[len(PREFIX) + 1:]
    return f"""# {PREFIX}

{len(TABS)} Databricks AI/BI dashboards, one per app tab, built from the app's own checks. One notebook
runs every check once and saves the results in your catalog; the dashboards read only those results.
No app, no laptop, no token. The repo's main README lists the other two ways to see the same data.

**Contents:** [Files](#files) · [Before you start](#before-you-start) ·
[1. Run the notebook](#1-run-the-notebook) · [2. Import the dashboards](#2-import-the-dashboards) ·
[3. Publish and share](#3-publish-and-share) · [Using the dashboards](#using-the-dashboards) ·
[New numbers](#new-numbers) · [Tagging what this costs](#tagging-what-this-costs) ·
[If something fails](#if-something-fails) · [What differs from the app](#what-differs-from-the-app) ·
[System tables read](#system-tables-read)

## Files

| File | Pages |
| --- | --- |
| `{NOTEBOOK_FILE}` | Runs every check once and saves the results. Run it first. |
{dash_md}

Generated by `tools/build_databricks_dashboards.py`; regenerate rather than hand-edit.

## Before you start

- **A catalog** where you can create a schema, or an existing `{SCHEMA_NAME}` schema where you can
  create tables.
- **Serverless notebooks** or any cluster, to run the notebook.
- **A SQL warehouse** for the dashboards. A 2X-Small serverless is plenty: they read small tables.
- **`SELECT` on the system tables** [listed below](#system-tables-read). A metastore admin grants
  these. A check whose table you can't read fails alone; the rest still run.

## 1. Run the notebook

1. **Workspace** › your folder › **Import** › **File**, and pick `{NOTEBOOK_FILE}`.
2. Fill in the widgets at the top:

   | Widget | Default | What it does |
   | --- | --- | --- |
   | `catalog` | empty | Where the results go: `<catalog>.{SCHEMA_NAME}`. |
   | `tag_keys` | `{keys}` | Tags every resource should carry. The Tags dashboard lists what misses them. |
   | `chargeback_keys` | `{CHARGEBACK_KEYS}` | Tags that say who pays, first match wins. |
   | `query_tags` | `{QUERY_TAGS}` | Tags on the notebook's own queries. See [Tagging what this costs](#tagging-what-this-costs). |

   Spelling variants match: "Cost Center", "costcenter" and "cost-center" all count as cost_center.
3. Attach serverless or a cluster and click **Run all**. It runs {len(all_checks())} checks, 4 at a time,
   prints `ok` or `NOT` with the reason for each, and ends with "N of M tables created".
4. A `NOT` check leaves an empty table and its reason in `{SCHEMA_NAME}.snapshot_log`. The
   dashboards show it as "Not assessed".

## 2. Import the dashboards

1. **Dashboards** › the arrow next to **Create dashboard** › **Import dashboard from file**.
2. Pick one `.lvdash.json`. Repeat for each one you want; start with `{first}`.
3. Pick your SQL warehouse at the top of the dashboard if it asks.
4. Back in the notebook, run its last cell. `{first}` then links to every dashboard you imported.

The dashboards read `{schema}.*`. With no catalog in front, Databricks looks in the warehouse's
default catalog. If yours is another one, use files with your catalog written in:

- from the repo: `python tools/build_databricks_dashboards.py --catalog <catalog> --out-dir <folder>`
  (a folder outside the repo);
- or in a text editor: replace `{SCHEMA_NAME}.` with `<catalog>.{SCHEMA_NAME}.` in each file.

## 3. Publish and share

1. **Publish** with **Embed credentials** on. Viewers then need no grants on the schema.
2. Set no refresh schedule: the numbers only change when the notebook runs again.
3. **Share** › add people or groups with **Can view**.

## Using the dashboards

- One dashboard per app tab, one page per sub-tab.
- Each page: a header with what the figures cover, a banner with the snapshot's date and window,
  then KPIs, charts and tables, then each check's own rows, worst first.
- Under every title, one line says how the number is worked out.
- **Workspace** filter at the top narrows every page. Governance and Storage cover the whole
  metastore and ignore it.
- Names read "name · workspace" in charts and "name (id)" in tables.
- Statuses: Critical, Warn, OK, Not assessed (failed in the notebook; `snapshot_log` says why).
- Start with `{first}`: its "Every dashboard of the set" table links to all the others.
  `{dashboard_name(TABS[1])[len(PREFIX) + 1:]}` lists what to fix; `{last}` shows what didn't run or isn't covered.

## New numbers

- Run the notebook again. Every dashboard shows the new numbers when next opened.
- On a schedule: a job that runs the notebook daily or weekly, with the widget values as job
  parameters.

## Tagging what this costs

| What runs | Where its cost shows | How to tag it |
| --- | --- | --- |
| The notebook's queries | Query history | `query_tags` sets {tag_pairs} on each statement. Compute without query tags prints one line and carries on. |
| The notebook's compute | The bill | Code can't tag a bill. Run the notebook as a job with tags {tag_pairs}, or on serverless pick a budget policy with them. |
| The dashboards' queries | The warehouse's bill | A dashboard's queries can't carry tags. Publish on a warehouse tagged {tag_pairs}. |

## If something fails

| What you see | What to do |
| --- | --- |
| "Set the catalog widget at the top" | Fill in `catalog`, then Run all. |
| `PERMISSION_DENIED` on `CREATE SCHEMA` | Ask for `CREATE SCHEMA` on the catalog, or have an admin create `<catalog>.{SCHEMA_NAME}` and give you `CREATE TABLE` on it. |
| Many lines start with `NOT` | Usually `SELECT` is missing on a system schema, or the schema isn't enabled. `snapshot_log` has each reason. |
| `TABLE_OR_VIEW_NOT_FOUND` on a dashboard | It looks in the wrong catalog. Use files with your catalog in them ([step 2](#2-import-the-dashboards)). |
| A widget is empty | The check found nothing, or failed: see `{last}`. |
| Viewers see a permission error | Publish again with **Embed credentials** on. |

## What differs from the app

- Fixed {WINDOW_DAYS}-day window, list price (effective), not the invoice.
- Tags are the ones billing records (on clusters, warehouses, jobs, pipelines and budget policies);
  the app also falls back to a workspace's own tags.
- Auto-stop savings use one setting per warehouse: 1 minute on serverless, 10 on pro and classic.
- No pickers: Before & after is fixed to warehouses, the last 7 days against the rest.
- Each check table shows up to {ROW_LIMIT:,} rows.

## System tables read

{tables_md}
"""


# --------------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------------

def quote_catalog(catalog: str) -> str:
    # backticks only when the name needs them, e.g. a hyphen
    return catalog if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", catalog) else f"`{catalog}`"


def qualified_schema(catalog: str | None) -> str:
    # without a catalog the dashboard reads the schema from its default catalog
    return f"{quote_catalog(catalog)}.{SCHEMA_NAME}" if catalog else SCHEMA_NAME


def build(catalog: str | None = None) -> dict[str, object]:
    schema = qualified_schema(catalog)
    files: dict[str, object] = {NOTEBOOK_FILE: build_snapshot_notebook(catalog)}
    pages: dict[str, list[str]] = {}
    for tab in TABS:
        dash = build_tab(schema, tab)
        pages[tab] = [p["displayName"] for p in dash["pages"]]
        files[dashboard_file(tab)] = json.dumps(dash, indent=2) + "\n"
    files["README.md"] = build_readme(schema, pages)
    return files


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default=None,
                        help=f"catalog holding {SCHEMA_NAME}, written into every file (default: none; the "
                             "dashboards read their default catalog, the notebook asks)")
    parser.add_argument("--out-dir", default=str(OUT_DIR), help="output directory (default databricks_dashboards/)")
    parser.add_argument("--only", choices=TABS, help="write just this tab's dashboard")
    parser.add_argument("--refresh-columns", action="store_true",
                        help="first rewrite tools/dashboard_columns.json from tests/db_audit_test.duckdb")
    args = parser.parse_args()
    if args.refresh_columns:
        refresh_columns()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.only:
        files = {dashboard_file(args.only): json.dumps(build_tab(qualified_schema(args.catalog), args.only), indent=2) + "\n"}
    else:
        files = build(args.catalog)
    for name, content in files.items():
        (out_dir / name).write_text(content, encoding="utf-8", newline="\n")
        print(f"wrote {out_dir / name}")


if __name__ == "__main__":
    main()
