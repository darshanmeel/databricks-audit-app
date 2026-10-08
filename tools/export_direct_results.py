#!/usr/bin/env python3
"""Run the Databricks-direct queries on a SQL warehouse and save the rows for load_direct_results.py.

The compiled SQL never masks (mask_user() renders as the plain column, and a resource name is
never masked); every value arrives raw unless --mask-users masks it here, locally, or the
privacy.mask_user_identities setting is on.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import config as app_config  # noqa: E402
from app.core import data as app_core_data  # noqa: E402
from app.core import datasource  # noqa: E402
from app.core import registry as app_registry  # noqa: E402
from app.core import tag_compliance as app_tag_compliance  # noqa: E402
from app.core.identity import format_identity, masking_enabled  # noqa: E402
from tools import dbt_run  # noqa: E402
from tools import generate_models as gm  # noqa: E402
from tools import snapshot as tools_snapshot  # noqa: E402

TOOL_VERSION = "2.0.0"
DIRECT_MANIFEST_PATH = ROOT / "dbt" / "models" / "databricks_direct" / "generated_manifest.json"
DIRECT_SQL_DIR = ROOT / "app" / "direct_sql"
NAMES_SQL_DIR = DIRECT_SQL_DIR / "names"
TAGS_SQL_DIR = DIRECT_SQL_DIR / "tags"
WINDOW_MARKER = "__WINDOW_DAYS__"  # written by tools/build_direct_sql.py
QUERY_SOURCE_MARKER = "__QUERY_SOURCE__"  # the settings query_source tag, filled in at run time
# One UTC as_of for the whole run (tools/build_direct_sql.py pins audit_today()/audit_now() to
# these), never hard-coded (config/settings.yml).
AS_OF_DATE_MARKER = "__AS_OF_DATE__"
AS_OF_TS_MARKER = "__AS_OF_TS__"
SHARE_FLOOR_MARKER = "__SHARE_FLOOR__"
COVERAGE_FLOOR_MARKER = "__COVERAGE_FLOOR__"
TAG_KEY_FILTER_MARKER = "__TAG_KEY_FILTER__"
SHARE_FLOOR_PCT_MARKER = "__SHARE_FLOOR_PCT__"
COVERAGE_FLOOR_PCT_MARKER = "__COVERAGE_FLOOR_PCT__"
BUILD_SQL_HINT = "python tools/build_direct_sql.py"
SCHEMA_NAME = "audit_direct"
DIM_TABLE_NAMES = ("dim_workspace", "dim_job", "dim_cluster", "dim_warehouse", "dim_pipeline", "dim_notebook")
DIMS_NOT_GENERATED_NOTE = (
    "One or more names not exported (see error); tools/load_direct_results.py creates an empty "
    "stand-in, so those names show as raw ids."
)
# app/direct_sql/tags/*.sql: the Databricks-direct twins of dbt/models/tags/*.sql.
TAG_TABLE_NAMES = (
    "tag_workspace", "tag_entity", "tag_index",
    "cost_unit", "cost_unit_tag", "cost_reconciliation",
    "perf_unit", "perf_unit_tag", "cost_day", "query_tag_keys", "bill_tag_dates",
)
TAGS_NOT_BUILT_NOTE = (
    "One or more tag tables not exported (see error); tag search, the Tag filter and the rollup "
    "show \"not built\" until this is fixed."
)
# Sources app/direct_sql/tags/*.sql marks optional: a missing table or grant degrades just the
# rows it feeds instead of failing the whole query. Each marker is swapped for the real table when
# a probe query succeeds, or a same-shape empty stand-in when it does not.
OPTIONAL_TAG_SOURCES: dict[str, dict[str, str]] = {
    "__SRC_BILLING_ATTRIBUTED_USAGE__": {
        "table": "`system`.`billing`.`attributed_usage`",
        "empty": (
            "(SELECT CAST(NULL AS STRING) AS workspace_id, CAST(NULL AS STRING) AS record_id, "
            "CAST(NULL AS DATE) AS usage_date, CAST(NULL AS DOUBLE) AS active_usage_quantity, "
            "CAST(NULL AS STRUCT<warehouse_id: STRING>) AS usage_metadata, "
            "CAST(NULL AS STRUCT<query_tags: MAP<STRING, STRING>>) AS granular_tags WHERE FALSE)"
        ),
    },
    "__SRC_LAKEFLOW_JOBS__": {
        "table": "`system`.`lakeflow`.`jobs`",
        "empty": (
            "(SELECT CAST(NULL AS STRING) AS job_id, CAST(NULL AS STRING) AS workspace_id, "
            "CAST(NULL AS MAP<STRING, STRING>) AS tags, CAST(NULL AS TIMESTAMP) AS change_time "
            "WHERE FALSE)"
        ),
    },
    "__SRC_LAKEFLOW_PIPELINES__": {
        "table": "`system`.`lakeflow`.`pipelines`",
        "empty": (
            "(SELECT CAST(NULL AS STRING) AS pipeline_id, CAST(NULL AS STRING) AS workspace_id, "
            "CAST(NULL AS MAP<STRING, STRING>) AS tags, CAST(NULL AS TIMESTAMP) AS change_time "
            "WHERE FALSE)"
        ),
    },
    "__SRC_COMPUTE_WAREHOUSES__": {
        "table": "`system`.`compute`.`warehouses`",
        "empty": (
            "(SELECT CAST(NULL AS STRING) AS warehouse_id, CAST(NULL AS STRING) AS workspace_id, "
            "CAST(NULL AS MAP<STRING, STRING>) AS tags, CAST(NULL AS TIMESTAMP) AS change_time "
            "WHERE FALSE)"
        ),
    },
    "__SRC_COMPUTE_INSTANCE_POOLS__": {
        "table": "`system`.`compute`.`instance_pools`",
        "empty": (
            "(SELECT CAST(NULL AS STRING) AS instance_pool_id, CAST(NULL AS STRING) AS workspace_id, "
            "CAST(NULL AS MAP<STRING, STRING>) AS tags, CAST(NULL AS TIMESTAMP) AS change_time "
            "WHERE FALSE)"
        ),
    },
    "__SRC_COMPUTE_CLUSTERS__": {
        "table": "`system`.`compute`.`clusters`",
        "empty": (
            "(SELECT CAST(NULL AS STRING) AS cluster_id, CAST(NULL AS STRING) AS workspace_id, "
            "CAST(NULL AS MAP<STRING, STRING>) AS tags, CAST(NULL AS STRING) AS worker_instance_pool_id, "
            "CAST(NULL AS STRING) AS driver_instance_pool_id, CAST(NULL AS TIMESTAMP) AS change_time "
            "WHERE FALSE)"
        ),
    },
    "__SRC_UC_TABLE_TAGS__": {
        "table": "`system`.`information_schema`.`table_tags`",
        "empty": (
            "(SELECT CAST(NULL AS STRING) AS catalog_name, CAST(NULL AS STRING) AS schema_name, "
            "CAST(NULL AS STRING) AS table_name, CAST(NULL AS STRING) AS tag_name, "
            "CAST(NULL AS STRING) AS tag_value WHERE FALSE)"
        ),
    },
    "__SRC_UC_SCHEMA_TAGS__": {
        "table": "`system`.`information_schema`.`schema_tags`",
        "empty": (
            "(SELECT CAST(NULL AS STRING) AS catalog_name, CAST(NULL AS STRING) AS schema_name, "
            "CAST(NULL AS STRING) AS tag_name, CAST(NULL AS STRING) AS tag_value WHERE FALSE)"
        ),
    },
    "__SRC_UC_COLUMN_TAGS__": {
        "table": "`system`.`information_schema`.`column_tags`",
        "empty": (
            "(SELECT CAST(NULL AS STRING) AS catalog_name, CAST(NULL AS STRING) AS schema_name, "
            "CAST(NULL AS STRING) AS table_name, CAST(NULL AS STRING) AS column_name, "
            "CAST(NULL AS STRING) AS tag_name, CAST(NULL AS STRING) AS tag_value WHERE FALSE)"
        ),
    },
    "__SRC_UC_VOLUME_TAGS__": {
        "table": "`system`.`information_schema`.`volume_tags`",
        "empty": (
            "(SELECT CAST(NULL AS STRING) AS catalog_name, CAST(NULL AS STRING) AS schema_name, "
            "CAST(NULL AS STRING) AS volume_name, CAST(NULL AS STRING) AS tag_name, "
            "CAST(NULL AS STRING) AS tag_value WHERE FALSE)"
        ),
    },
    "__SRC_AI_GATEWAY_USAGE__": {
        "table": "`system`.`ai_gateway`.`usage`",
        "empty": (
            "(SELECT CAST(NULL AS STRING) AS workspace_id, CAST(NULL AS STRING) AS request_id, "
            "CAST(NULL AS STRING) AS endpoint_id, CAST(NULL AS STRING) AS endpoint_name, "
            "CAST(NULL AS TIMESTAMP) AS event_time, CAST(NULL AS MAP<STRING, STRING>) AS endpoint_tags "
            "WHERE FALSE)"
        ),
    },
}
DEFAULT_MAX_ROWS = 50_000
DEFAULT_WINDOWS_FALLBACK = 30  # only when config/settings.yml cannot be read
DEFAULT_THREADS = 4
# Sources are fully qualified to `system`; queries never need a real catalog to compile.
DEFAULT_COMPILE_CATALOG = "system"

ENV_HOST = "DATABRICKS_SERVER_HOSTNAME"
ENV_HTTP_PATH = "DATABRICKS_HTTP_PATH"
ENV_TOKEN = "DATABRICKS_TOKEN"
ENV_CATALOG = "AUDIT_DBX_CATALOG"
DOTENV_PATH = ROOT / ".env"
DOTENV_VARS = (ENV_HOST, ENV_HTTP_PATH, ENV_TOKEN, ENV_CATALOG)

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"dapi[0-9a-fA-F]{16,}[A-Za-z0-9._\-]*"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)((?:access[_-]?token|token|password|secret)\s*[=:]\s*)[^\s,;'\"`]+"),
)


def _include_today() -> bool:
    """config/settings.yml's include_today: export today's partial day too (on by default)."""
    try:
        return bool(app_config.load_settings()["include_today"])
    except Exception:  # noqa: BLE001 -- an unreadable settings.yml must not block the export
        return True


def _default_windows() -> int:
    """config/settings.yml's default_window, used as the default days of history to export."""
    try:
        return int(app_config.load_settings()["default_window"])
    except Exception:  # noqa: BLE001 -- an unreadable settings.yml must not block the export
        return DEFAULT_WINDOWS_FALLBACK


WINDOW_LABELS = (7, 30, 90)


def window_plan(days: int) -> list[tuple[int, int]]:
    """Every one of WINDOW_LABELS with its days_used = min(label, days) -- every app window shows
    data, even one longer than the exported history (days=10 -> [(7,7),(30,10),(90,10)])."""
    return [(w, min(w, days)) for w in WINDOW_LABELS]


def window_sources(plan: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """One (label, days_used) per DISTINCT days_used in `plan`, at the smallest label that has it -- the labels a windowed check actually queries."""
    source_label_of_days_used: dict[int, int] = {}
    for label, days_used in plan:
        source_label_of_days_used.setdefault(days_used, label)
    return [(label, days_used) for days_used, label in source_label_of_days_used.items()]


def window_aliases_map(plan: list[tuple[int, int]]) -> dict[int, int]:
    """{alias label: source label} for every label whose days_used a smaller label already covers, e.g. {90: 30} for a 30-day export."""
    source_label_of_days_used = {days_used: label for label, days_used in window_sources(plan)}
    return {
        label: source_label_of_days_used[days_used]
        for label, days_used in plan
        if source_label_of_days_used[days_used] != label
    }


def _is_windowed(query_id: str) -> bool:
    """True iff this query's SQL carries the __WINDOW_DAYS__ marker (app_registry's own flag,
    set from the query header's period_days param -- see tools/build_direct_sql.py)."""
    try:
        return bool(app_registry.by_id(query_id).windowed)
    except KeyError:
        return False


class ExportConfigError(RuntimeError):
    """A whole-run problem, reported before any query runs; one query's failure is recorded instead."""


def _rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


# app/core/data.py validates order_by columns against a live table; the export trusts the header.
def _rewrite_order_by_for_export(order_by: str) -> str | None:
    terms = app_core_data._split_order_by_terms(order_by)
    if not terms:
        return None
    pieces: list[str] = []
    for term in terms:
        if app_core_data._CASE_STATUS_RE.match(term):
            pieces.append(term)
            continue
        m = app_core_data._ORDER_TERM_RE.match(term)
        if not m:
            return None  # an expression term: drop the whole order_by, never part of it
        piece = f"`{m.group('ident')}`"
        if m.group("dir"):
            piece += f" {m.group('dir').upper()}"
        if m.group("nulls"):
            piece += f" NULLS {m.group('nulls').upper()}"
        pieces.append(piece)
    return ", ".join(pieces)


def order_by_sql(query_id: str) -> str:
    """'ORDER BY <clause>' from the query's registry header, or ''."""
    try:
        spec = app_registry.by_id(query_id)
    except KeyError:
        return ""
    order_text = gm.extract_order_by(gm.runner_body(spec.body))
    if not order_text:
        return ""
    rewritten = _rewrite_order_by_for_export(order_text)
    return f"ORDER BY {rewritten}" if rewritten else ""


# Person columns only; resource names such as sku_name, job_name or warehouse_name never match.
_PERSON_COLUMN_RE = re.compile(
    r"(^|_)(owner|owned_by|creator|created_by|run_as|run_by|executed_by|actor|principal|user|"
    r"user_name|authenticated_as|grantee|requester|granted_by|email)(_|$)",
    re.IGNORECASE,
)
# format_identity output or the vendored SQL's own mask: "<id> <2 chars>***".
_ALREADY_MASKED_RE = re.compile(r"^\S+ .{0,2}\*\*\*$")


def _mask_user(value: str) -> str:
    # Databricks user names are emails; groups, service principals and kinds stay readable.
    if "@" not in value or _ALREADY_MASKED_RE.match(value):
        return value
    return format_identity(value)


def apply_masking(table: pa.Table) -> pa.Table:
    """Mask user emails in person columns with format_identity; every other value passes through."""
    columns = []
    for field in table.schema:
        col = table.column(field.name)
        is_text = pa.types.is_string(field.type) or pa.types.is_large_string(field.type)
        if is_text and _PERSON_COLUMN_RE.search(field.name):
            col = pa.array(
                [None if v is None else _mask_user(v) for v in col.to_pylist()], type=field.type
            )
        columns.append(col)
    return pa.Table.from_arrays(columns, schema=table.schema)


def _cutoff(now: dt.datetime, include_today: bool) -> dt.datetime:
    """The first day the export leaves out: today, or tomorrow when today's partial day is kept."""
    return now + dt.timedelta(days=1) if include_today else now


def _as_of_sql(now: dt.datetime, include_today: bool = False) -> tuple[str, str]:
    """(DATE 'YYYY-MM-DD', TIMESTAMP 'YYYY-MM-DD HH:MM:SS') for the run's one UTC as_of -- what
    __AS_OF_DATE__ / __AS_OF_TS__ are replaced with. Every check reads days before the DATE."""
    end = _cutoff(now, include_today)
    return f"DATE '{end.strftime('%Y-%m-%d')}'", f"TIMESTAMP '{now.strftime('%Y-%m-%d %H:%M:%S')}'"


def load_direct_sql(
    scope_ids: list[str], roster: dict[str, dict], windows: int, env: dict | None = None,
    *, sql_dir: Path | None = None, now: dt.datetime | None = None, include_today: bool = False,
) -> dict[str, dict]:
    """{query_id: {"sql": ...} | {"error": ...}} from app/direct_sql, with the window and the
    run's one UTC as_of (default: now) filled in."""
    sql_dir = sql_dir if sql_dir is not None else DIRECT_SQL_DIR
    now = now if now is not None else dt.datetime.now(dt.timezone.utc)
    as_of_date_sql, as_of_ts_sql = _as_of_sql(now, include_today)
    try:
        baked = json.loads((sql_dir / "thresholds.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExportConfigError(f"no pre-compiled SQL in {sql_dir}; run: {BUILD_SQL_HINT}") from exc
    if baked != (dbt_run.load_thresholds() or {}):
        raise ExportConfigError(f"config/thresholds.yml changed since the SQL was generated; run: {BUILD_SQL_HINT}")
    out: dict[str, dict] = {}
    for qid in scope_ids:
        path = sql_dir / f"{qid}.sql"
        if path.exists():
            sql = path.read_text(encoding="utf-8").replace(WINDOW_MARKER, str(int(windows)))
            sql = sql.replace(AS_OF_DATE_MARKER, as_of_date_sql).replace(AS_OF_TS_MARKER, as_of_ts_sql)
            if AS_OF_DATE_MARKER in sql or AS_OF_TS_MARKER in sql:
                raise ExportConfigError(f"{qid}: an as-of marker survived substitution")
            out[qid] = {"sql": sql.replace(QUERY_SOURCE_MARKER, app_config.query_source())}
        else:
            out[qid] = {"error": f"no pre-compiled SQL for this check; run: {BUILD_SQL_HINT}"}
    return out


def _table_fqn(catalog: str, schema: str, name: str) -> str:
    return f"`{catalog}`.`{schema}`.`{name}`"


def _fetch_rows_arrow(cur, sql: str) -> pa.Table | None:
    cur.execute(sql)
    chunks: list[pa.Table] = []
    while True:
        batch = cur.fetchmany_arrow(tools_snapshot.FETCH_ROWS)
        if batch is None:
            break
        chunks.append(batch)
        if batch.num_rows < tools_snapshot.FETCH_ROWS:
            break
    if not chunks:
        return None
    nonempty = [c for c in chunks if c.num_rows > 0]
    if not nonempty:
        return chunks[0]  # schema only, zero rows
    if len(nonempty) == 1:
        return nonempty[0]
    return pa.concat_tables(nonempty, promote_options="default")


def build_capped_sql(inner_sql: str, order_sql: str, max_rows: int) -> str:
    """Wrap, order and LIMIT max_rows + 1; the extra row flags truncation without a COUNT(*)."""
    sql = f"SELECT * FROM (\n{inner_sql}\n) AS direct_export_sq"
    if order_sql:
        sql += f" {order_sql}"
    sql += f" LIMIT {int(max_rows) + 1}"
    return sql


def _run_capped_query(
    cur, *, inner_sql: str, query_id: str | None, max_rows: int, mask_users: bool,
) -> tuple[pa.Table, bool]:
    """Run one capped, ordered query and return (table, truncated); raises on a Databricks-side
    failure. One call per window_plan() entry -- run_scope_jobs below relabels and concatenates
    them per id."""
    sql = build_capped_sql(inner_sql, order_by_sql(query_id), max_rows)
    table = _fetch_rows_arrow(cur, sql)
    if table is None or len(table.schema) == 0:
        raise RuntimeError("query returned no columns (0-column result)")  # DuckDB cannot read one
    truncated = table.num_rows > max_rows
    if truncated:
        table = table.slice(0, max_rows)
    if mask_users:
        table = apply_masking(table)
    return table, truncated


def _relabel_window_days(table: pa.Table, days_used: int, label: int) -> pa.Table:
    """The literal window_days column this run set to days_used, renamed to the plan label it's
    filed under; every other column is untouched."""
    if days_used == label or "window_days" not in table.column_names:
        return table
    idx = table.column_names.index("window_days")
    col = table.column("window_days")
    relabeled = pc.if_else(pc.equal(col, days_used), pa.scalar(label, type=col.type), col)
    return table.set_column(idx, table.schema.field(idx), relabeled)


def load_names_sql(
    names_dir: Path | None = None, windows: int = 30, now: dt.datetime | None = None,
    include_today: bool = False,
) -> dict[str, str]:
    """{dim name: sql text} for whichever app/direct_sql/names/<name>.sql files exist, window and
    the run's one UTC as_of (default: now) filled in."""
    names_dir = names_dir if names_dir is not None else NAMES_SQL_DIR
    now = now if now is not None else dt.datetime.now(dt.timezone.utc)
    as_of_date_sql, as_of_ts_sql = _as_of_sql(now, include_today)
    out: dict[str, str] = {}
    for name in DIM_TABLE_NAMES:
        path = names_dir / f"{name}.sql"
        if path.exists():
            sql = path.read_text(encoding="utf-8").replace(WINDOW_MARKER, str(int(windows)))
            sql = sql.replace(AS_OF_DATE_MARKER, as_of_date_sql).replace(AS_OF_TS_MARKER, as_of_ts_sql)
            if AS_OF_DATE_MARKER in sql or AS_OF_TS_MARKER in sql:
                raise ExportConfigError(f"{name}: an as-of marker survived substitution")
            out[name] = sql
    return out


def export_name_one(
    cur, *, sql: str, mask_users: bool, out_path: Path,
) -> dict:
    """Run one names query uncapped (dim_cluster can hold every job cluster; a cap would silently
    drop names on a large account) and write it to out_path."""
    table = _fetch_rows_arrow(cur, sql)
    if table is None or len(table.schema) == 0:
        raise RuntimeError("query returned no columns (0-column result)")
    if mask_users:
        table = apply_masking(table)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, str(out_path), compression="snappy")
    return {
        "exported_rows": table.num_rows,
        "columns": [{"name": f.name, "type": str(f.type)} for f in table.schema],
    }


def _export_names(cur, names_sql: dict[str, str], out_dir: Path, mask_users: bool) -> dict:
    """Run app/direct_sql/names/*.sql straight against the connection and write dims/<name>.parquet."""
    dims_dir = out_dir / "dims"
    dims_dir.mkdir(exist_ok=True)
    exported: dict[str, dict] = {}
    available: list[str] = []
    for name in DIM_TABLE_NAMES:
        sql = names_sql.get(name)
        if sql is None:
            exported[name] = {"exported": False, "error": "no app/direct_sql/names/*.sql file"}
            continue
        out_path = dims_dir / f"{name}.parquet"
        try:
            result = export_name_one(cur, sql=sql, mask_users=mask_users, out_path=out_path)
            exported[name] = {"exported": True, "error": None, **result}
            available.append(name)
        except Exception as exc:  # noqa: BLE001 -- one bad names query must not stop the rest
            exported[name] = {"exported": False, "error": str(exc)}

    missing = [n for n in DIM_TABLE_NAMES if n not in available]
    return {
        "available": available,
        "missing": missing,
        "exported": exported,
        "note": DIMS_NOT_GENERATED_NOTE if missing else None,
    }


# ABAC row-filter and column-mask policies live in Unity Catalog, metastore-wide, and no system
# table lists them: the export asks SHOW POLICIES on the metastore, every catalog and every schema
# this warehouse's workspace can see. A catalog not bound to this workspace, or not readable by
# this token (READ METADATA or MANAGE), is not covered; the manifest says how many were asked.
# Databricks names the columns "Policy Name", "Policy Type", "Catalog", "Schema", "Table", "Comment".
POLICY_COLUMNS = ("policy_name", "policy_type", "catalog", "schema", "table_name", "comment")
POLICY_SKIP_CATALOGS = {"system", "samples", "hive_metastore", "__databricks_internal"}
POLICY_SCHEMAS_MAX = 5000


def _quote_ident(name: str) -> str:
    return "`" + str(name).replace("`", "``") + "`"


def _export_policies(cur, out_dir: Path) -> dict:
    """SHOW POLICIES on the metastore, each catalog and each schema -> policies/abac_policies.parquet."""
    def rows_of(sql: str) -> list[dict]:
        t = _fetch_rows_arrow(cur, sql)
        return t.to_pylist() if t is not None else []

    try:
        catalogs = [r["catalog_name"] for r in rows_of("SELECT catalog_name FROM system.information_schema.catalogs ORDER BY 1")
                    if r["catalog_name"] not in POLICY_SKIP_CATALOGS]
        wanted = set(catalogs)
        schemas = [(r["catalog_name"], r["schema_name"]) for r in rows_of(
            "SELECT catalog_name, schema_name FROM system.information_schema.schemata "
            "WHERE schema_name <> 'information_schema' ORDER BY 1, 2") if r["catalog_name"] in wanted]
    except Exception as exc:  # noqa: BLE001 -- policies are a side table; the export goes on without them
        return {"exported": False, "error": _redact(str(exc)), "rows": 0}
    targets: list[tuple[str, tuple[str, ...]]] = [("METASTORE", ())]
    targets += [("CATALOG", (c,)) for c in catalogs]
    targets += [("SCHEMA", cs) for cs in schemas[:POLICY_SCHEMAS_MAX]]
    found: list[dict] = []
    seen: set[tuple] = set()
    errors, first_error, metastore_ok = 0, None, False
    for on_type, parts in targets:
        sql = f"SHOW POLICIES ON {on_type}" + (" " + ".".join(_quote_ident(x) for x in parts) if parts else "")
        try:
            got = rows_of(sql)
        except Exception as exc:  # noqa: BLE001 -- one securable it cannot read must not stop the rest
            if on_type != "METASTORE":
                errors += 1
                first_error = first_error or _redact(str(exc))
            continue
        metastore_ok = metastore_ok or on_type == "METASTORE"
        for r in got:
            low = {re.sub(r"\s+", "_", str(k).strip().lower()): v for k, v in r.items()}
            low.setdefault("table_name", low.get("table"))
            rec = {c: None if low.get(c) is None else str(low.get(c)) for c in POLICY_COLUMNS}
            # A policy asked again from a wider securable is listed once.
            key = tuple(rec[c] for c in POLICY_COLUMNS[:5])
            if rec["policy_name"] is not None and key in seen:
                continue
            seen.add(key)
            found.append({**rec, "on_type": on_type, "on_name": ".".join(parts) or None})
    cols = (*POLICY_COLUMNS, "on_type", "on_name")
    pol_dir = out_dir / "policies"
    pol_dir.mkdir(exist_ok=True)
    pq.write_table(pa.table({c: pa.array([r[c] for r in found], pa.string()) for c in cols}),
                   str(pol_dir / "abac_policies.parquet"), compression="snappy")
    return {"exported": True, "error": None, "rows": len(found), "metastore": metastore_ok,
            "catalogs": len(catalogs), "schemas": min(len(schemas), POLICY_SCHEMAS_MAX),
            "schemas_not_asked": max(0, len(schemas) - POLICY_SCHEMAS_MAX),
            "not_readable": errors, "first_error": first_error}


def tag_key_filter_sql() -> str:
    """settings export_tag_keys as a SQL condition on tag_key, normalised the way the tags queries
    normalise raw keys (lower case, spaces, _ and - dropped); TRUE when every tag is kept. The
    mandatory and top tags are always kept: the Tags page, the filters and Money read them."""
    settings = app_config.load_settings()
    keys = settings.get("export_tag_keys")
    if not keys:
        return "TRUE"
    keys = [*keys, *(settings.get("mandatory_tag_keys") or []), *app_config.load_top_tags()]
    norm = sorted(app_tag_compliance.key_variants([re.sub(r"[ _-]+", "", k.lower()) for k in keys]))
    return "tag_key IN (" + ", ".join("'" + k.replace("'", "''") + "'" for k in norm) + ")"


def load_tags_sql(
    tags_dir: Path | None = None, as_of: dt.datetime | None = None, include_today: bool = False,
) -> dict[str, str]:
    """{table name: sql text} for whichever app/direct_sql/tags/<name>.sql files exist,
    __AS_OF_DATE__ and the share/coverage floor markers filled in -- same shape as load_names_sql.
    __WINDOW_DAYS__ is left in place for a windowed tags query (cost_unit, perf_unit, ...):
    _export_tags fills it in once per window_sources() entry, same as a windowed check, so tags
    data exists for every window SOURCE this run exports; tools/load_direct_results.py then
    alias-copies those rows to any label window_aliases_map() reuses them for, exactly like a
    windowed check, rather than a second Databricks query replaying the same span.
    One as_of for every query (default: now) so cost_reconciliation's two queries, run minutes
    apart, never see a different "today" -- a real gap would otherwise look like a reconciliation
    mismatch. The floors come from config/settings.yml, the same readers tools/dbt_run.py feeds to
    dbt, so a customer's own tag_share_floor/tag_coverage_floor apply in direct mode too."""
    tags_dir = tags_dir if tags_dir is not None else TAGS_SQL_DIR
    as_of = as_of if as_of is not None else dt.datetime.now(dt.timezone.utc)
    as_of_sql, _ = _as_of_sql(as_of, include_today)
    share_floor = dbt_run.load_share_floor()
    coverage_floor = dbt_run.load_coverage_floor()
    out: dict[str, str] = {}
    for name in TAG_TABLE_NAMES:
        path = tags_dir / f"{name}.sql"
        if path.exists():
            text = path.read_text(encoding="utf-8").replace(AS_OF_DATE_MARKER, as_of_sql)
            text = text.replace(SHARE_FLOOR_MARKER, repr(share_floor))
            text = text.replace(COVERAGE_FLOOR_MARKER, repr(coverage_floor))
            text = text.replace(SHARE_FLOOR_PCT_MARKER, f"{share_floor * 100:.1f}")
            text = text.replace(COVERAGE_FLOOR_PCT_MARKER, f"{coverage_floor * 100:.1f}")
            text = text.replace(TAG_KEY_FILTER_MARKER, tag_key_filter_sql())
            out[name] = text
    return out


def _probe_optional_tag_sources(cur, tags_sql: dict[str, str]) -> dict[str, bool]:
    """Probe, once, every optional source actually referenced in tags_sql; True = present and
    granted. A source no query uses is never probed (keeps tests that fake other SQL untouched)."""
    combined = "\n".join(tags_sql.values())
    present: dict[str, bool] = {}
    for marker, spec in OPTIONAL_TAG_SOURCES.items():
        if marker not in combined:
            continue
        try:
            cur.execute(f"SELECT * FROM {spec['table']} LIMIT 0")
        except Exception:  # noqa: BLE001 -- absent table or ungranted source, either way: missing
            present[marker] = False
        else:
            present[marker] = True
    return present


def _substitute_optional_tag_sources(sql: str, present: dict[str, bool]) -> tuple[str, list[str]]:
    """Replace each optional-source marker in `sql` with the real table or a typed empty stand-in;
    return the substituted SQL and the fully-qualified names it substituted away."""
    missing: list[str] = []
    for marker, spec in OPTIONAL_TAG_SOURCES.items():
        if marker not in sql:
            continue
        if present.get(marker):
            sql = sql.replace(marker, spec["table"])
        else:
            sql = sql.replace(marker, spec["empty"])
            missing.append(spec["table"].replace("`", ""))
    return sql, missing


def _strip_sql_line_comments(sql: str) -> str:
    """`sql` with every `--`-to-end-of-line comment removed. Used only to tell whether a marker is
    really used in the query body, not just mentioned in a header comment -- tag_entity.sql,
    tag_index.sql and tag_workspace.sql each document, in a comment, that they do NOT take
    __WINDOW_DAYS__ ("no __WINDOW_DAYS__ marker here"), which would otherwise read as if they did."""
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def _export_tag_one(
    cur, *, sql: str, mask_users: bool, out_path: Path, sources: list[tuple[int, int]],
) -> dict:
    """Run one tags query uncapped and write it to out_path -- same as export_name_one, except a
    windowed query (the __WINDOW_DAYS__ marker still present in the query body; see load_tags_sql)
    runs once per window_sources() entry -- one query per DISTINCT days_used, not per label --
    relabelled and concatenated exactly like a windowed check's own per-source tasks. A label an
    alias reuses (window_aliases_map) gets no query here; tools/load_direct_results.py copies its
    source's rows in at load time. A non-windowed query (no marker in its body -- a whole-snapshot
    table like tag_entity, or an already-substituted `sql` such as a test's) runs once, unchanged."""
    if WINDOW_MARKER not in _strip_sql_line_comments(sql):
        return export_name_one(cur, sql=sql, mask_users=mask_users, out_path=out_path)
    tables = []
    for label, days_used in sources:
        table = _fetch_rows_arrow(cur, sql.replace(WINDOW_MARKER, str(int(days_used))))
        if table is None or len(table.schema) == 0:
            raise RuntimeError("query returned no columns (0-column result)")
        tables.append(_relabel_window_days(table, days_used, label))
    table = tables[0] if len(tables) == 1 else pa.concat_tables(tables, promote_options="default")
    if mask_users:
        table = apply_masking(table)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, str(out_path), compression="snappy")
    return {
        "exported_rows": table.num_rows,
        "columns": [{"name": f.name, "type": str(f.type)} for f in table.schema],
    }


def _export_tags(
    cur, tags_sql: dict[str, str], out_dir: Path, mask_users: bool, sources: list[tuple[int, int]],
) -> dict:
    """Run app/direct_sql/tags/*.sql straight against the connection and write tags/<name>.parquet
    -- uncapped, same as _export_names (these are already unit/value-grain, not raw system rows).
    Each query's optional sources are probed once and substituted (see OPTIONAL_TAG_SOURCES) so a
    missing table degrades only the rows it feeds instead of failing the query. `sources` is
    window_sources(plan): a windowed tags query runs once per DISTINCT days_used, never once per
    window_aliases_map() alias label."""
    tags_dir = out_dir / "tags"
    tags_dir.mkdir(exist_ok=True)
    exported: dict[str, dict] = {}
    available: list[str] = []
    present = _probe_optional_tag_sources(cur, tags_sql)
    sources_not_exported = sorted({
        spec["table"].replace("`", "") for marker, spec in OPTIONAL_TAG_SOURCES.items()
        if marker in present and not present[marker]
    })
    for name in TAG_TABLE_NAMES:
        sql = tags_sql.get(name)
        if sql is None:
            exported[name] = {"exported": False, "error": "no app/direct_sql/tags/*.sql file"}
            continue
        sql, substituted = _substitute_optional_tag_sources(sql, present)
        out_path = tags_dir / f"{name}.parquet"
        try:
            result = _export_tag_one(cur, sql=sql, mask_users=mask_users, out_path=out_path, sources=sources)
            exported[name] = {"exported": True, "error": None, **result}
            if substituted:
                exported[name]["sources_not_exported"] = substituted
            available.append(name)
        except Exception as exc:  # noqa: BLE001 -- one bad tags query must not stop the rest
            exported[name] = {"exported": False, "error": str(exc)}

    missing = [n for n in TAG_TABLE_NAMES if n not in available]
    return {
        "available": available,
        "missing": missing,
        "exported": exported,
        "sources_not_exported": sources_not_exported,
        "note": TAGS_NOT_BUILT_NOTE if missing else None,
    }


def _redact(text, token: str | None = None) -> str:
    """`text` with the live DATABRICKS_TOKEN value (when given) and any generic token/bearer/
    password-shaped substring replaced by `***`. Applied to every error message before it is
    written to the report."""
    s = "" if text is None else str(text)
    if token and len(token) >= 4:
        s = s.replace(token, "***")
    for pat in _SECRET_PATTERNS:
        s = pat.sub(lambda m: (m.group(1) if m.lastindex else "") + "***", s)
    return s


def _load_dotenv(env: dict, path: Path | None = None) -> None:
    """Populate `env` from the git-ignored .env's `KEY=VALUE` lines, without overriding a value
    `env` already has (same parsing rule, including the UTF-16/BOM handling, as
    tools/snapshot.py's own dotenv reader)."""
    path = path if path is not None else DOTENV_PATH
    if not path.exists():
        return
    try:
        raw_bytes = path.read_bytes()
    except OSError:
        return
    try:
        text = (
            raw_bytes.decode("utf-16")
            if raw_bytes[:2] in (b"\xff\xfe", b"\xfe\xff")
            else raw_bytes.decode("utf-8-sig")
        )
    except UnicodeDecodeError as exc:
        raise ExportConfigError(".env is not readable text; save it as UTF-8 (e.g. in Notepad)") from exc
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        if key not in DOTENV_VARS or env.get(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if value:
            env[key] = value


def inside_repo(path: Path) -> bool:
    resolved = path.resolve()
    return resolved == ROOT or ROOT in resolved.parents


def _preflight_env() -> dict:
    env = os.environ.copy()
    _load_dotenv(env)
    required = [ENV_HOST, ENV_HTTP_PATH, ENV_TOKEN]
    missing = [name for name in required if not env.get(name)]
    if missing:
        raise ExportConfigError(
            "missing credential env var(s): " + ", ".join(missing)
            + " (set them in the environment or in the git-ignored .env at the repo root)"
        )
    # Same cleanup as tools/snapshot.connect(), so host_fingerprint names the host it connected to.
    raw_host = env.get(ENV_HOST)
    if raw_host:
        env[ENV_HOST] = re.sub(r"^https?://", "", raw_host.strip(), flags=re.I).split("/")[0].split("?")[0]
    if not env.get(ENV_CATALOG):
        env[ENV_CATALOG] = DEFAULT_COMPILE_CATALOG
    return env


def _select_scope(roster: dict[str, dict], only: list[str] | None, domain: str | None) -> list[str]:
    if only:
        unknown = [q for q in only if q not in roster]
        if unknown:
            raise ExportConfigError(f"unknown query id(s): {unknown}")
        return list(dict.fromkeys(only))
    if domain:
        ids = sorted(qid for qid, m in roster.items() if m["domain"] == domain)
        if not ids:
            raise ExportConfigError(f"no query has domain {domain!r}")
        return ids
    return sorted(roster)


def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5,
        )
    except Exception:  # noqa: BLE001 -- git missing is not fatal, just unrecorded
        return None
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


@dataclasses.dataclass
class _Task:
    """One query run: a windowed check gets one _Task per window_plan() entry (same qid, a
    different label/days_used/sql each time); any other check gets exactly one, label=None."""

    qid: str
    label: int | None       # the window this run counts toward; None for a non-windowed check
    days_used: int | None   # the __WINDOW_DAYS__ value this run's SQL was compiled with
    sql: str


def build_tasks(
    scope_ids: list[str], sources: list[tuple[int, int]], compiled_by_days: dict[int, dict], full_days: int,
) -> tuple[list[_Task], dict[str, str]]:
    """One _Task per (query id, source entry) for a windowed query -- one query per DISTINCT
    days_used, not per label -- else one at `full_days`. A compile error for an id goes to
    `compile_errors` instead of a task -- it never partially runs."""
    tasks: list[_Task] = []
    compile_errors: dict[str, str] = {}
    for qid in scope_ids:
        entries = sources if _is_windowed(qid) else [(None, full_days)]
        for label, days_used in entries:
            c = compiled_by_days[days_used][qid]
            if "error" in c:
                compile_errors.setdefault(qid, c["error"])
                continue
            tasks.append(_Task(qid, label, days_used, c["sql"]))
    return tasks, compile_errors


def run_scope_jobs(
    tasks: list[_Task],
    *,
    max_rows: int,
    mask_users: bool,
    connect_fn,
    threads: int,
    token: str | None,
    findings_dir: Path,
) -> dict[str, dict]:
    """Run every task, `threads` at a time with one connection per thread. Each id's own task(s)
    are folded and written to findings_dir/<id>.parquet as soon as its last task finishes -- only
    one id's rows sit in memory at a time, not the whole scope's -- into {"status", "error",
    "exported_rows", "truncated", "truncated_windows", "columns", "duration_s"}. A windowed
    check's per-window tables are relabelled before concatenation; any one window failing, or a
    schema mismatch between windows, fails the whole id, never "ok, partially"."""
    lock = threading.Lock()
    thread_local = threading.local()
    connections: list = []
    pending: dict[str, list[_Task]] = {}
    for task in tasks:
        pending.setdefault(task.qid, []).append(task)
    remaining = {qid: len(ts) for qid, ts in pending.items()}
    collected: dict[str, list[tuple[_Task, dict]]] = {qid: [] for qid in pending}
    entries: dict[str, dict] = {}

    def get_cursor():
        if not hasattr(thread_local, "cur"):
            connected = connect_fn()
            with lock:
                connections.append(connected)
            thread_local.cur = connected.conn.cursor()
        return thread_local.cur

    def finalize(qid: str) -> None:
        pairs = collected.pop(qid)
        duration = round(sum(r["duration_s"] for _, r in pairs), 3)
        errors = [r["error"] for _, r in pairs if r["error"] is not None]
        if errors:
            entries[qid] = {
                "status": "failed", "exported_rows": None, "truncated": False,
                "truncated_windows": [], "columns": None, "error": errors[0], "duration_s": duration,
            }
            return
        tables = [r["table"] for _, r in pairs]
        try:
            table = tables[0] if len(tables) == 1 else pa.concat_tables(tables, promote_options="default")
        except Exception as exc:  # noqa: BLE001 -- one id's bad schema must not abort the export
            entries[qid] = {
                "status": "failed", "exported_rows": None, "truncated": False,
                "truncated_windows": [], "columns": None,
                "error": _redact(f"could not combine windows: {exc}", token), "duration_s": duration,
            }
            return
        truncated_windows = sorted(t.label for t, r in pairs if r["truncated"] and t.label is not None)
        pq.write_table(table, str(findings_dir / f"{qid}.parquet"), compression="snappy")
        entries[qid] = {
            "status": "empty" if table.num_rows == 0 else "ok",
            "exported_rows": table.num_rows,
            "truncated": any(r["truncated"] for _, r in pairs),
            "truncated_windows": truncated_windows,
            "columns": [{"name": f.name, "type": str(f.type)} for f in table.schema],
            "error": None, "duration_s": duration,
        }

    def work(i: int) -> None:
        task = tasks[i]
        t0 = time.perf_counter()
        try:
            cur = get_cursor()
            table, truncated = _run_capped_query(
                cur, inner_sql=task.sql, query_id=task.qid, max_rows=max_rows, mask_users=mask_users,
            )
            if task.label is not None:
                table = _relabel_window_days(table, task.days_used, task.label)
            result = {"table": table, "truncated": truncated, "error": None}
        except Exception as exc:  # noqa: BLE001 -- one task's failure must not stop the rest
            result = {"table": None, "truncated": False, "error": _redact(str(exc), token)}
        result["duration_s"] = time.perf_counter() - t0
        with lock:
            collected[task.qid].append((task, result))
            remaining[task.qid] -= 1
            done = remaining[task.qid] == 0
        if done:
            finalize(task.qid)  # last task for this id -- fold, write, and drop its tables now

    if tasks:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
            list(pool.map(work, range(len(tasks))))

    for connected in connections:
        try:
            connected.conn.close()
        except Exception:  # noqa: BLE001 -- best-effort cleanup only
            pass

    return entries


def build_report(findings_manifest: dict[str, dict], *, mode: str, labels: list[int],
                  window_coverage: dict[str, int], days: int,
                  started_at: str, finished_at: str) -> dict:
    rows = [{"query_id": qid, **findings_manifest[qid]} for qid in sorted(findings_manifest)]
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    failed_ids = [r["query_id"] for r in rows if r["status"] == "failed"]
    return {
        "tool": "tools/export_direct_results.py", "mode": mode, "windows": labels,
        "window_coverage": window_coverage, "days": days,
        "started_at": started_at, "finished_at": finished_at,
        "summary": {"total": len(rows), "by_status": by_status, "failed": len(failed_ids)},
        "failed_ids": failed_ids, "rows": rows,
    }


def _md_cell(text, limit: int = 120) -> str:
    s = (text or "").replace("|", "\\|").replace("\n", " ")
    return s[:limit - 3] + "..." if len(s) > limit else s


def render_window_coverage(window_coverage: dict[str, int]) -> str:
    """'7d: full, 30d: full, 90d: partial, 30 of 90 days' -- one plain line, not the dict."""
    parts = []
    for w in WINDOW_LABELS:
        covered = window_coverage.get(str(w))
        if covered is None:
            parts.append(f"{w}d: not exported")
        elif covered < w:
            parts.append(f"{w}d: partial, {covered} of {w} days")
        else:
            parts.append(f"{w}d: full")
    return ", ".join(parts)


def render_markdown(report: dict) -> str:
    s = report["summary"]
    lines = [
        "# Databricks direct export report",
        "",
        f"mode: `{report['mode']}`  windows: {report['windows']}  days: {report['days']}",
        f"coverage: {render_window_coverage(report['window_coverage'])}",
        f"started: {report.get('started_at')}  finished: {report.get('finished_at')}",
        "",
        f"**{s['total']} queries** -- " + ", ".join(f"{k}: {v}" for k, v in s["by_status"].items()),
        "",
        "| query_id | domain | status | rows | truncated | error |",
        "|---|---|---|---|---|---|",
    ]
    for r in report["rows"]:
        lines.append(
            f"| {r['query_id']} | {r.get('domain')} | {r['status']} | {r.get('exported_rows')} | "
            f"{r.get('truncated')} | {_md_cell(r.get('error'))} |"
        )
    lines.append("")
    if report["failed_ids"]:
        lines.append("Failed query ids: " + ", ".join(report["failed_ids"]))
        lines.append("")
    return "\n".join(lines) + "\n"


def run_export(
    out_dir: Path,
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    windows: int | None = None,
    threads: int = DEFAULT_THREADS,
    only: list[str] | None = None,
    domain: str | None = None,
    tags: list[str] | None = None,
    mask_users: bool = False,
    connect_fn=None,
    compile_fn=None,
    direct_manifest_path: Path | None = None,
    now: dt.datetime | None = None,
    env: dict | None = None,
    names_sql: dict[str, str] | None = None,
    tags_sql: dict[str, str] | None = None,
    include_today: bool = False,
) -> dict:
    """Run the export into out_dir (tests inject connect_fn/compile_fn/env/names_sql); built in a temp sibling folder, swapped into out_dir only on success."""
    direct_manifest_path = direct_manifest_path if direct_manifest_path is not None else DIRECT_MANIFEST_PATH
    now = now if now is not None else dt.datetime.now(dt.timezone.utc)
    windows = windows if windows is not None else _default_windows()
    days = int(windows)  # --windows is days of history now; window_plan fills 7/30/90 from it
    plan = window_plan(days)
    labels = [w for w, _ in plan]
    window_coverage = {str(w): d for w, d in plan}
    sources = window_sources(plan)
    aliases = window_aliases_map(plan)
    mode = "query"

    if datasource.inside_repo(out_dir):
        raise ExportConfigError(
            f"{out_dir} is inside the repository. This folder holds real-account Databricks "
            "data; choose a folder outside the repo."
        )
    if not direct_manifest_path.exists():
        raise ExportConfigError(
            f"{_rel(direct_manifest_path)} not found -- run tools/generate_direct_models.py first"
        )
    manifest_doc = json.loads(direct_manifest_path.read_text(encoding="utf-8"))
    roster = {m["query_id"]: m for m in manifest_doc.get("models", [])}
    if not roster:
        raise ExportConfigError(f"{_rel(direct_manifest_path)} lists no models")
    scope_ids = _select_scope(roster, only, domain)

    if env is None:
        env = _preflight_env()
    catalog = env.get(ENV_CATALOG) or None
    host = env.get(ENV_HOST)
    token = env.get(ENV_TOKEN)
    connect = connect_fn or tools_snapshot.connect

    # Build into a temp sibling folder and swap it into place only once the whole run succeeds,
    # so a stopped or crashed export never leaves a mix of this run's and an earlier run's files
    # under the same results/ folder (they'd otherwise share one manifest.json/dims/findings set).
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    work_dir = out_dir.with_name(out_dir.name + ".tmp")
    if work_dir.exists():
        shutil.rmtree(work_dir)
    findings_dir = work_dir / "findings"
    findings_dir.mkdir(parents=True)

    # One extra connection runs the names and tags queries: they read the same system tables the
    # checks do, uncapped.
    # --only/--domain runs just those checks: the names and tags queries are for a full export.
    # --tags runs just those tag tables: no checks, no names.
    partial = bool(only or domain or tags)
    names_sql = names_sql if names_sql is not None else ({} if partial else load_names_sql(windows=windows, now=now, include_today=include_today))
    if tags:
        unknown = [t for t in tags if t not in TAG_TABLE_NAMES]
        if unknown:
            raise ExportConfigError(f"unknown tag table(s): {unknown}; choose from {list(TAG_TABLE_NAMES)}")
        all_tags = tags_sql if tags_sql is not None else load_tags_sql(as_of=now, include_today=include_today)
        tags_sql = {k: v for k, v in all_tags.items() if k in tags}
        scope_ids = []
    tags_sql = tags_sql if tags_sql is not None else ({} if partial else load_tags_sql(as_of=now, include_today=include_today))
    # Names and tags run on the probe connection alongside the checks, not before them: done one
    # by one first, they held every check back for minutes.
    side: dict = {}

    def names_and_tags() -> None:
        try:
            side["dims"] = _export_names(probe.conn.cursor(), names_sql, work_dir, mask_users)
            side["tags"] = _export_tags(probe.conn.cursor(), tags_sql, work_dir, mask_users, sources)
            side["policies"] = None if partial else _export_policies(probe.conn.cursor(), work_dir)
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the main thread below
            side["error"] = exc

    # One compiled variant per DISTINCT days_used a source needs, plus `days` for the non-windowed
    # checks that run once, "as today" -- an alias label (window_aliases) never compiles or runs
    # its own query.
    # now is bound as a default so a caller's own compile_fn (tests) keeps its 4-argument shape.
    compile = compile_fn or functools.partial(load_direct_sql, now=now, include_today=include_today)
    distinct_days_used = {days} | {d for _, d in sources}
    compiled_by_days = {v: compile(scope_ids, roster, v, env) for v in distinct_days_used}
    tasks, compile_errors = build_tasks(scope_ids, sources, compiled_by_days, days)

    try:
        probe = connect()
    except tools_snapshot.SnapshotConfigError as exc:
        raise ExportConfigError(str(exc)) from exc
    side_thread = threading.Thread(target=names_and_tags, daemon=True)
    side_thread.start()
    try:
        entries_by_id = run_scope_jobs(
            tasks, max_rows=max_rows, mask_users=mask_users, connect_fn=connect, threads=threads,
            token=token, findings_dir=findings_dir,
        )
    finally:
        side_thread.join()
        try:
            probe.conn.close()
        except Exception:  # noqa: BLE001 -- best-effort cleanup only
            pass
    if "error" in side:
        raise side["error"]
    dims_info, tags_info, policies_info = side["dims"], side["tags"], side.get("policies")
    for qid, message in compile_errors.items():
        entries_by_id[qid] = {
            "status": "failed", "exported_rows": None, "truncated": False, "truncated_windows": [],
            "columns": None, "error": message, "duration_s": None,
        }
    # An alias label reused a truncated source's rows, so it is just as truncated as its source.
    for entry in entries_by_id.values():
        tw = entry.get("truncated_windows") or []
        extra = [alias for alias, source in aliases.items() if source in tw and alias not in tw]
        if extra:
            entry["truncated_windows"] = sorted(tw + extra)

    findings_manifest: dict[str, dict] = {}
    exported = empty = failed = truncated_n = 0
    for qid in sorted(roster):
        model = roster[qid]
        e = entries_by_id.get(qid)
        if e is None:
            entry = {"domain": model["domain"], "status": "not_run", "exported_rows": None,
                      "truncated": False, "truncated_windows": [], "columns": None,
                      "duration_s": None, "error": None}
        else:
            duration = e.get("duration_s")
            entry = {"domain": model["domain"], "status": e["status"],
                      "exported_rows": e.get("exported_rows"), "truncated": bool(e.get("truncated")),
                      "truncated_windows": e.get("truncated_windows") or [],
                      "columns": e.get("columns"),
                      "duration_s": round(duration, 3) if duration is not None else None,
                      "error": e.get("error")}
            if entry["status"] == "ok":
                exported += 1
            elif entry["status"] == "empty":
                empty += 1
            elif entry["status"] == "failed":
                failed += 1
            if entry["truncated"]:
                truncated_n += 1
        findings_manifest[qid] = entry

    finished_at = dt.datetime.now(dt.timezone.utc)
    report = build_report(
        findings_manifest, mode=mode, labels=labels, window_coverage=window_coverage, days=days,
        started_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        finished_at=finished_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    (work_dir / "report.json").write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    (work_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")

    # The compile-only stand-in is not a catalog this run read from.
    manifest_catalog = None if catalog == DEFAULT_COMPILE_CATALOG else catalog
    manifest_out = {
        "tool": "tools/export_direct_results.py",
        "tool_version": TOOL_VERSION,
        "git_commit": _git_commit(),
        "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        # The first day left out; with includes_today, today up to as_of is in and partial.
        "as_of_date": _cutoff(now, include_today).strftime("%Y-%m-%d"),
        "includes_today": include_today,
        "mode": mode,
        "windows": labels,
        "window_coverage": window_coverage,
        "window_aliases": {str(alias): str(source) for alias, source in aliases.items()},
        "days": days,
        "catalog": manifest_catalog,
        "host_fingerprint": tools_snapshot.host_fingerprint(host) if host else None,
        "max_rows": max_rows,
        "mask_users": mask_users,
        "findings": findings_manifest,
        "dims": dims_info,
        "tags": tags_info,
        "policies": policies_info,
    }
    (work_dir / "manifest.json").write_text(
        json.dumps(manifest_out, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )

    total_bytes = sum(p.stat().st_size for p in work_dir.rglob("*") if p.is_file())

    # The only step that touches the real out_dir: swap the finished folder in, keeping the
    # previous run intact on disk until the new one is known-good.
    old_dir = out_dir.with_name(out_dir.name + ".old")
    if old_dir.exists():
        shutil.rmtree(old_dir)
    if out_dir.exists():
        os.rename(out_dir, old_dir)
    os.rename(work_dir, out_dir)
    if old_dir.exists():
        shutil.rmtree(old_dir, ignore_errors=True)

    return {
        "manifest": manifest_out,
        "exported": exported,
        "empty": empty,
        "failed": failed,
        "truncated": truncated_n,
        "total_bytes": total_bytes,
        "out_dir": out_dir,
    }


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="export_direct_results.py",
        description=(
            "Run every Databricks-direct query on the warehouse and write the rows to a folder "
            "outside this repo."
        ),
    )
    ap.add_argument("--out", metavar="DIR",
                     help="output folder, created if missing; must be outside the repo "
                          "(default: the data folder's results/ -- see app/core/datasource.py)")
    ap.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS, metavar="N",
                     help=f"rows kept per query, worst first (default: {DEFAULT_MAX_ROWS})")
    ap.add_argument("--windows", type=int, default=None, metavar="DAYS",
                     help="days of history to export (default: config/settings.yml "
                          "default_window). The app's 7/30/90-day windows all show these days; a "
                          "window longer than DAYS shows the same rows and is marked partial. "
                          "More than 90 adds nothing -- the app has no window past 90.")
    ap.add_argument("--threads", type=int, default=DEFAULT_THREADS, metavar="N",
                     help=f"parallel Databricks statements (default: {DEFAULT_THREADS})")
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--only", nargs="+", metavar="ID", help="only these query ids (skips the names and tags queries)")
    scope.add_argument("--domain", metavar="NAME", help="only this domain (skips the names and tags queries)")
    scope.add_argument("--tags", nargs="+", metavar="TABLE",
                       help="only these tag tables, e.g. tag_entity (no checks, no names); use a separate --out folder")
    ap.add_argument("--mask-users", action="store_true",
                     help="mask user emails in person columns such as owner and run_as "
                          "(default: on when config/settings.yml's privacy.mask_user_identities is)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out_dir = Path(args.out).resolve() if args.out else datasource.results_dir()
    windows = args.windows if args.windows is not None else _default_windows()

    if args.max_rows < 0:
        print("error: --max-rows must be >= 0", file=sys.stderr)
        return 2
    if windows <= 0:
        print("error: --windows must be a positive integer", file=sys.stderr)
        return 2
    if args.threads <= 0:
        print("error: --threads must be a positive integer", file=sys.stderr)
        return 2

    try:
        result = run_export(
            out_dir, max_rows=args.max_rows, windows=windows,
            threads=args.threads, only=args.only, domain=args.domain, tags=args.tags,
            mask_users=args.mask_users or masking_enabled(), include_today=_include_today(),
        )
    except ExportConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(
        f"exported {result['exported']} finding(s), {result['empty']} empty, "
        f"{result['failed']} failed, {result['truncated']} truncated -- "
        f"{result['total_bytes']:,} bytes -- {result['out_dir']}"
    )
    print(render_window_coverage(result["manifest"]["window_coverage"]))
    if result["manifest"].get("includes_today"):
        print(f"includes today up to {result['manifest']['as_of']} (partial; billing can lag a few hours)")
    dims = result["manifest"]["dims"]
    if dims["available"]:
        print(f"dims exported: {', '.join(dims['available'])}")
    if dims["missing"]:
        print(f"dims not available in Databricks-direct form: {', '.join(dims['missing'])}")
    tags = result["manifest"]["tags"]
    if tags["available"]:
        print(f"tags exported: {', '.join(tags['available'])}")
    if tags.get("sources_not_exported"):
        print(f"tag sources not exported (degraded, not failed): {', '.join(tags['sources_not_exported'])}")
    for name in tags["missing"]:
        error = _redact((tags["exported"].get(name) or {}).get("error"))
        print(f"tags.{name} not built: {error}")
    print("real-account data: keep this folder out of the repo and do not share it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
