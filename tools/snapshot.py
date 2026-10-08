#!/usr/bin/env python3
"""tools/snapshot.py -- export the Databricks system tables into a local parquet snapshot.

This is the ONE script in this repository that is ever allowed to CONNECT to a Databricks
workspace. Nothing under app/, dbt/ or any other tools/*.py imports the connector or reads the
three credential env vars at runtime. The person running the board runs this script; agents never do.

    python tools/snapshot.py --out snapshot --days 30 [--billing-days 365] [--workspace <id> ...]
        [--only billing,lakeflow] [--exclude-schema query] [--max-rows-per-file 500000]
        [--timeout 600] [--resume]

Credentials come ONLY from the env vars DATABRICKS_SERVER_HOSTNAME / DATABRICKS_HTTP_PATH /
DATABRICKS_TOKEN (a git-ignored `.env` at the repo root may set them and is the only file read
for that purpose). They are resolved at call time inside `connect()` (DEC-27), never at import
time. There is no `--token` flag, ever. Every log line and every manifest / _MISSING.json message
passes through `scrub_secrets()` before it is printed or written.

What it writes under `--out` (default `snapshot/`, git-ignored):

    <schema>__<table>/part-NNNNN.parquet   the data (Arrow-paged, timestamps as timestamp[us] UTC)
    <schema>__<table>/_MISSING.json        only when the table could not be read at all (or was
                                            excluded by --only / --exclude-schema and has no data
                                            on disk); sits next to ONE empty parquet file whose
                                            schema is ARROW_SCHEMA["<schema>__<table>"] from
                                            tests/fixtures/ddl.py, so every dbt source resolves
    manifest.json                           the single description of what is on disk (shape below)

manifest.json (exact keys, PLAN.md 5.2 plus T-74): top level `as_of`, `as_of_date`, `days`,
`billing_days`, `workspace_ids`, `host_fingerprint`, `metastore` (T-71: `{cloud, region,
id_fingerprint}` of the metastore the connection resolves to, or null), `connector_version`, plus
`tables`: one entry
per source keyed `<schema>__<table>` with `{state: ok|partial|not_assessed|error, rows, files,
time_column, days_requested, days_effective, min_time, max_time, predicate, elapsed_s,
slices_failed, error_class, reason, message, as_of, workspace_ids}`. The per-table `as_of` is the
as_of of the run that exported that table and `workspace_ids` the workspace filter its statement
carried ([] when not filtered). --resume reuses a table only when those, its window and its files
on disk match this run (resume_mismatch); a resumed manifest's top-level `as_of` is its oldest
reused table's.

The per-table predicate map is embedded below (PREDICATE_ROWS, verbatim from PLAN.md 5.2; DEC-10);
`config/snapshot_plan.yml` (T-05's `tools/build_sources.py --plan` output) is loaded only when it
is present, as a per-table override of `time_column` / `retention_days` (see load_plan_overrides).

Dependencies: pyarrow (always), pyyaml (only when config/snapshot_plan.yml exists),
databricks-sql-connector (only inside connect(), only for a real export). The test suite drives
run_export() with a fake connection and never imports the connector.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import socket
import sys
import time
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pyarrow.types as patypes

ROOT = Path(__file__).resolve().parent.parent


# Query tag key on every export statement; its value is the query_source setting.
AUDIT_TAG_KEY = "audit_app"


def _tag_session(conn: Any) -> None:
    """Tag this session's statements in query history. A runtime without query tags keeps only the
    client-application tag, so a failure here is ignored."""
    try:
        cur = conn.cursor()
        try:
            cur.execute(f"SET QUERY_TAGS['{AUDIT_TAG_KEY}'] = '{_query_source()}'")
        finally:
            cur.close()
    except Exception:  # noqa: BLE001 -- best effort: the client-application tag still identifies it
        pass
    # current_date()/current_timestamp() must read UTC everywhere audit_today()/audit_now() pin
    # to it; a runtime that rejects SET TIME ZONE is ignored, same as the query tag above.
    try:
        cur = conn.cursor()
        try:
            cur.execute("SET TIME ZONE 'UTC'")
        finally:
            cur.close()
    except Exception:  # noqa: BLE001 -- best effort: a non-UTC session keeps working, just unpinned
        pass


def _query_source() -> str:
    if str(ROOT) not in sys.path:  # run as a script from tools/
        sys.path.insert(0, str(ROOT))
    from app.core import config as app_config

    return app_config.query_source()
DDL_MODULE_PATH = ROOT / "tests" / "fixtures" / "ddl.py"
PLAN_OVERRIDE_PATH = ROOT / "config" / "snapshot_plan.yml"
DOTENV_PATH = ROOT / ".env"

ENV_HOST = "DATABRICKS_SERVER_HOSTNAME"
ENV_HTTP_PATH = "DATABRICKS_HTTP_PATH"
ENV_TOKEN = "DATABRICKS_TOKEN"
CREDENTIAL_ENV_VARS = (ENV_HOST, ENV_HTTP_PATH, ENV_TOKEN)

FETCH_ROWS = 200_000          # cursor.fetchmany_arrow(FETCH_ROWS), PLAN.md 5.2
DEFAULT_MAX_ROWS_PER_FILE = 500_000
DEFAULT_TIMEOUT_S = 600
DEFAULT_DAYS = 30
DEFAULT_OUT = "snapshot"
MANIFEST_NAME = "manifest.json"
MISSING_NAME = "_MISSING.json"
PART_TEMPLATE = "part-{:05d}.parquet"

STATE_OK = "ok"
STATE_PARTIAL = "partial"
STATE_NOT_ASSESSED = "not_assessed"
STATE_ERROR = "error"
STATES = (STATE_OK, STATE_PARTIAL, STATE_NOT_ASSESSED, STATE_ERROR)

# Per-table manifest entry keys, in this order: PLAN.md 5.2, plus `as_of` (the as_of of the run
# that exported the table) and `workspace_ids` (the workspace filter its statement carried) --
# the recorded scope --resume compares before it reuses a table (T-74, resume_mismatch).
ENTRY_KEYS = (
    "state", "rows", "files", "time_column", "days_requested", "days_effective", "min_time",
    "max_time", "predicate", "elapsed_s", "slices_failed", "error_class", "reason", "message",
    "as_of", "workspace_ids",
)
TOP_LEVEL_KEYS = (
    "as_of", "as_of_date", "days", "billing_days", "workspace_ids", "host_fingerprint",
    "metastore", "connector_version",
)

# Every `as_of` in the manifest: UTC, no zone suffix (the form T-05's audit_now() accepts).
AS_OF_FORMAT = "%Y-%m-%dT%H:%M:%S"
_AS_OF_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

# Classified reasons (manifest `reason`, _MISSING.json `reason`). DEC-20 / DEC-39 consumers read
# these as opaque strings; keep them stable.
REASON_TABLE_NOT_FOUND = "table_not_found"
REASON_NO_GRANT = "no_grant"
REASON_SCHEMA_NOT_ENABLED = "schema_not_enabled"
REASON_TOO_MUCH_DATA = "too_much_data"
REASON_TIMEOUT = "timeout"
REASON_EXCLUDED = "excluded_by_flag"
REASON_UNKNOWN = "unknown"
# Reasons that mean "the source is unavailable" (state not_assessed) vs "the export failed"
# (state error).
NOT_ASSESSED_REASONS = frozenset({REASON_TABLE_NOT_FOUND, REASON_NO_GRANT, REASON_SCHEMA_NOT_ENABLED,
                                  REASON_EXCLUDED})

# ----------------------------------------------------------------------------------------------
# The predicate map. Do not re-derive or approximate. cap=None means "days as requested, no
# retention cap".
# kind: billing (uses --billing-days), windowed, scd2 (full history), reference (point-in-time).
# ----------------------------------------------------------------------------------------------
_INFORMATION_SCHEMA_TABLES = (
    "abac_policy_definitions", "catalog_privileges", "column_masks", "column_tags", "connection_privileges",
    "credential_privileges", "external_location_privileges", "row_filters", "schema_privileges",
    "schema_share_usage", "schema_tags", "share_recipient_privileges", "shares",
    "table_privileges", "table_share_usage", "table_tags", "tables", "views", "volume_tags",
    "volumes",
)
PREDICATE_ROWS: tuple[tuple[tuple[str, ...], str | None, int | None, str], ...] = (
    (("billing.usage",), "usage_date", 365, "billing"),
    (("billing.attributed_usage",), "usage_date", 365, "windowed"),
    (("query.history",), "start_time", 365, "windowed"),
    (("lakeflow.job_run_timeline", "lakeflow.job_task_run_timeline",
      "lakeflow.pipeline_update_timeline"), "period_start_time", 365, "windowed"),
    (("compute.node_timeline",), "start_time", 90, "windowed"),
    (("compute.warehouse_events", "compute.instance_events"), "event_time", None, "windowed"),
    (("access.audit", "access.column_lineage", "access.table_lineage"), "event_date", 365, "windowed"),
    (("access.inbound_network", "access.outbound_network"), "event_time", 30, "windowed"),
    (("ai_gateway.usage",), "event_time", None, "windowed"),
    (("serving.endpoint_usage",), "request_time", 90, "windowed"),
    (("storage.predictive_optimization_operations_history",), "start_time", 180, "windowed"),
    # App-owned (not part of the vendored 47): backs 3 app-owned storage checks. Retention is
    # undocumented for this table, so no cap is applied.
    (("storage.table_metrics_history",), "snapshot_date", None, "windowed"),
    # SCD2: the queries take the latest row per key; a windowed export would lose unchanged
    # entities -> full history.
    (("compute.clusters", "compute.instance_pools", "compute.warehouses", "lakeflow.jobs",
      "lakeflow.job_tasks", "lakeflow.pipelines", "serving.served_entities"), None, None, "scd2"),
    # Reference / point-in-time -> full.
    (("billing.list_prices", "compute.node_types", "access.workspaces_latest",
      "data_classification.results")
     + tuple(f"information_schema.{t}" for t in _INFORMATION_SCHEMA_TABLES), None, None, "reference"),
)


@dataclass(frozen=True)
class Predicate:
    schema: str
    table: str
    time_column: str | None
    cap_days: int | None
    kind: str

    @property
    def key(self) -> str:
        return f"{self.schema}__{self.table}"

    @property
    def fqn(self) -> str:
        return f"system.{self.schema}.{self.table}"


def build_predicates() -> dict[str, Predicate]:
    out: dict[str, Predicate] = {}
    for tables, col, cap, kind in PREDICATE_ROWS:
        for fq in tables:
            schema, table = fq.split(".", 1)
            p = Predicate(schema, table, col, cap, kind)
            if p.key in out:
                raise ValueError(f"duplicate predicate row for {fq}")
            out[p.key] = p
    return out


PREDICATES: dict[str, Predicate] = build_predicates()


# ----------------------------------------------------------------------------------------------
# Secrets
# ----------------------------------------------------------------------------------------------
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Databricks personal access tokens look like dapi<hex...>; scrub the whole token.
    re.compile(r"dapi[0-9a-fA-F]{16,}[A-Za-z0-9._\-]*"),
    # Bearer <token>
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"),
    # token=..., access_token: ..., password=..., secret=...
    re.compile(r"(?i)((?:access[_-]?token|token|password|secret)\s*[=:]\s*)[^\s,;'\"`]+"),
)


def scrub_secrets(text: Any) -> str:
    """Return `text` with any credential material replaced by `***`.

    The DATABRICKS_TOKEN value is resolved at call time (DEC-27) and removed wherever it appears
    verbatim; the generic token / bearer / password patterns are removed regardless. Every log
    line and every manifest / _MISSING.json message goes through this function.
    """
    s = "" if text is None else str(text)
    token = os.environ.get(ENV_TOKEN)
    if token and len(token) >= 4:
        s = s.replace(token, "***")
    for pat in _SECRET_PATTERNS:
        s = pat.sub(lambda m: (m.group(1) if m.lastindex else "") + "***", s)
    return s


# ----------------------------------------------------------------------------------------------
# Connection (the only place credentials are read)
# ----------------------------------------------------------------------------------------------
class SnapshotConfigError(RuntimeError):
    """A configuration problem that stops the run before any statement is sent."""


@dataclass
class Connected:
    conn: Any
    connector_version: str
    host_fingerprint: str
    # T-71: {cloud, region, id_fingerprint} of the metastore this connection resolves to, read
    # once by connect() (read_metastore); None when it could not be read. Every regional system
    # table in the export holds rows for this metastore's region only.
    metastore: dict[str, str | None] | None = None


def _load_dotenv(path: Path) -> None:
    """Populate the three credential env vars from a `KEY=VALUE` .env file, without overriding
    values already in the environment. Only those three keys are read; nothing else.

    F11: `echo X=Y > .env` in Windows PowerShell 5.1 writes UTF-16 (with a BOM), and Notepad's own
    UTF-8 default writes a BOM too -- a plain `.read_text(encoding="utf-8")` either raised an
    uncaught UnicodeDecodeError (UTF-16, first_run.py then called it a connector failure) or
    silently kept the BOM on the first key ("﻿DATABRICKS_SERVER_HOSTNAME", reported as
    missing). Read as bytes and decode UTF-16 when a BOM says so, UTF-8-with-optional-BOM
    otherwise; a file that is neither raises a clear SnapshotConfigError instead of guessing."""
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
        raise SnapshotConfigError(
            ".env is not readable text; save it as UTF-8 (e.g. in Notepad)"
        ) from exc
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        if key not in CREDENTIAL_ENV_VARS or os.environ.get(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if value:
            os.environ[key] = value


def host_fingerprint(host: str) -> str:
    """A short, non-reversible identifier of the workspace host for the manifest."""
    return hashlib.sha256(host.strip().lower().encode("utf-8")).hexdigest()[:16]


METASTORE_STATEMENT = "SELECT current_metastore()"


def parse_metastore_id(value: Any) -> dict[str, str | None] | None:
    """T-71. `current_metastore()` returns `<cloud>:<region>:<uuid>` (e.g. `aws:us-east-1:...`).
    Returns {"cloud", "region", "id_fingerprint"}: cloud and region as returned (they say which
    region the regional system tables cover), and the first 16 hex chars of the SHA-256 of the
    stripped, lower-cased id -- the manifest never carries the raw id, the same rule as
    host_fingerprint. A value not in the three-part form keeps only its fingerprint (cloud and
    region None). None, or an empty / blank value, returns None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parts = text.split(":")
    cloud: str | None = None
    region: str | None = None
    if len(parts) == 3 and all(p.strip() for p in parts):
        cloud, region = parts[0].strip(), parts[1].strip()
    return {
        "cloud": cloud,
        "region": region,
        "id_fingerprint": hashlib.sha256(text.lower().encode("utf-8")).hexdigest()[:16],
    }


def read_metastore(conn: Any) -> dict[str, str | None] | None:
    """T-71. Run METASTORE_STATEMENT on an open connection and parse it. Never raises: a
    workspace without Unity Catalog, a missing grant or a connector quirk returns None, and the
    manifest then records `metastore: null` ("region not recorded") instead of failing the
    export."""
    try:
        cur = conn.cursor()
        try:
            cur.execute(METASTORE_STATEMENT)
            row = cur.fetchone()
        finally:
            try:
                cur.close()
            except Exception:  # noqa: BLE001 - closing a dead cursor must not mask the result
                pass
    except Exception:  # noqa: BLE001 - the probe is best effort; the export does not depend on it
        return None
    if not row:
        return None
    return parse_metastore_id(row[0])


def describe_metastore(info: dict | None) -> str:
    """One phrase for the log line: "aws us-east-1", or why there is none."""
    if not info:
        return "not recorded"
    if info.get("region"):
        return f"{info.get('cloud') or '?'} {info['region']}"
    return "id not in <cloud>:<region>:<uuid> form"


_USE_DEFAULT_DOTENV = object()


def check_host_reachable(host: str, timeout: int = 15) -> None:
    """A wrong or unreachable hostname otherwise sits silent for the connector's own ~900s retry
    loop (F3) -- fail in `timeout` seconds instead, with a message that tells a typo (DNS lookup
    failure) apart from a network problem (VPN/firewall/proxy). Returns at once, doing nothing,
    when an HTTPS proxy is configured (HTTPS_PROXY/https_proxy): a direct socket probe to the
    workspace host is meaningless when traffic actually goes through a proxy, and the connector
    itself already knows how to reach it through one."""
    if os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"):
        return
    try:
        socket.create_connection((host, 443), timeout).close()
    except socket.gaierror:
        raise SnapshotConfigError(
            f"host not found: {host} -- check DATABRICKS_SERVER_HOSTNAME (no https://, no path)"
        )
    except OSError as exc:
        raise SnapshotConfigError(
            f"cannot reach {host}:443 ({exc}) -- check VPN, firewall or proxy"
        )


def connect(timeout: int = DEFAULT_TIMEOUT_S, dotenv_path: Any = _USE_DEFAULT_DOTENV) -> Connected:
    """Open the one connection this repo ever makes to Databricks.

    Credentials are read here and only here, at call time, from the three env vars (optionally
    populated from the git-ignored .env at the repo root; pass dotenv_path=None to skip that).
    The connector is imported lazily so the module (and the test suite) never depends on it
    being installed. It also sends one statement of its own, `SELECT current_metastore()`
    (read_metastore), so the manifest can say which region the regional tables cover.
    """
    if dotenv_path is _USE_DEFAULT_DOTENV:
        dotenv_path = DOTENV_PATH
    if dotenv_path is not None:
        _load_dotenv(Path(dotenv_path))
    missing = [name for name in CREDENTIAL_ENV_VARS if not os.environ.get(name)]
    if missing:
        raise SnapshotConfigError(
            "missing credential env vars: " + ", ".join(missing)
            + " (set them in the environment or in the git-ignored .env; there is no --token flag)"
        )
    host = os.environ[ENV_HOST]
    http_path = os.environ[ENV_HTTP_PATH]
    token = os.environ[ENV_TOKEN]
    try:
        from databricks import sql as dbsql  # lazy: databricks-sql-connector
    except ImportError as exc:
        raise SnapshotConfigError(
            "databricks-sql-connector is not installed; run: pip install -r requirements.txt"
        ) from exc

    # A host pasted with a scheme (https://...) or a trailing path, or a Git-Bash-rewritten
    # DATABRICKS_HTTP_PATH (MSYS_NO_PATHCONV turning "/sql/1.0/..." into a filesystem path),
    # otherwise reaches the connector's own ~900s retry loop with nothing printed first (F3) --
    # catch the common shapes here, in seconds, before that loop ever starts.
    host = re.sub(r"^https?://", "", host.strip(), flags=re.I).split("/")[0].split("?")[0]
    if "xxxxxxxx" in host or token == "dapi-your-token-here":
        raise SnapshotConfigError(
            ".env still has the example values from .env.example; fill in your own"
        )
    if not http_path.strip().startswith("/"):
        raise SnapshotConfigError(
            "DATABRICKS_HTTP_PATH must start with / (e.g. /sql/1.0/warehouses/<id>); in Git Bash "
            "put it in .env or set MSYS_NO_PATHCONV=1"
        )
    print(f"connecting to {host} ...", file=sys.stderr, flush=True)
    check_host_reachable(host)

    conn = dbsql.connect(
        server_hostname=host,
        http_path=http_path,
        access_token=token,
        _use_arrow_native_complex_types=True,
        _socket_timeout=timeout,
        # Tags client_application in system.query.history so cost_audit_self_usage can find this
        # export's own statements without also matching dbt-databricks or another connector tool.
        user_agent_entry=_query_source(),
        # Connector 3.4, pre-installed in Databricks Apps, reads only the old name.
        _user_agent_entry=_query_source(),
    )
    _tag_session(conn)
    version = str(getattr(dbsql, "__version__", "unknown"))
    return Connected(conn=conn, connector_version=version, host_fingerprint=host_fingerprint(host),
                     metastore=read_metastore(conn))


# ----------------------------------------------------------------------------------------------
# Error classification
# ----------------------------------------------------------------------------------------------
TOO_MUCH_DATA_RE = re.compile(
    r"too much data|result size|RESULT_SIZE|exceed(?:s|ed)? (?:the )?max(?:imum)?|max(?:imum)? result",
    re.I,
)
SCHEMA_NOT_ENABLED_RE = re.compile(r"SCHEMA_NOT_FOUND|not enabled|is disabled", re.I)
NOT_FOUND_RE = re.compile(
    r"TABLE_OR_VIEW_NOT_FOUND|cannot be found|not found|does not exist|no such table", re.I
)
NO_GRANT_RE = re.compile(
    r"INSUFFICIENT_PERMISSIONS|PERMISSION_DENIED|does not have|not authorized|unauthori[sz]ed"
    r"|access denied|permission|forbidden",
    re.I,
)
TIMEOUT_RE = re.compile(r"timed?\s?out|timeout", re.I)


def classify_error(exc: BaseException) -> tuple[str, str, str]:
    """Return (error_class, reason, scrubbed message) for an exception raised by a statement."""
    message = scrub_secrets(str(exc)) or scrub_secrets(repr(exc))
    error_class = type(exc).__name__
    if TOO_MUCH_DATA_RE.search(message):
        reason = REASON_TOO_MUCH_DATA
    elif SCHEMA_NOT_ENABLED_RE.search(message):
        reason = REASON_SCHEMA_NOT_ENABLED
    elif NOT_FOUND_RE.search(message):
        reason = REASON_TABLE_NOT_FOUND
    elif NO_GRANT_RE.search(message):
        reason = REASON_NO_GRANT
    elif TIMEOUT_RE.search(message):
        reason = REASON_TIMEOUT
    else:
        reason = REASON_UNKNOWN
    return error_class, reason, message


def state_for_reason(reason: str) -> str:
    return STATE_NOT_ASSESSED if reason in NOT_ASSESSED_REASONS else STATE_ERROR


# ----------------------------------------------------------------------------------------------
# Schemas (tests/fixtures/ddl.py, generated by T-03 -- read-only input here)
# ----------------------------------------------------------------------------------------------
def load_arrow_schemas(path: Path = DDL_MODULE_PATH) -> tuple[dict[str, pa.Schema], dict[str, str]]:
    """Load ARROW_SCHEMA and FLAGS from the generated tests/fixtures/ddl.py by file path."""
    if not path.exists():
        raise SnapshotConfigError(
            f"{path} not found; run `python tools/build_fixture_ddl.py` first (T-03)"
        )
    spec = importlib.util.spec_from_file_location("snapshot_ddl_module", path)
    if spec is None or spec.loader is None:
        raise SnapshotConfigError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    schemas = dict(getattr(mod, "ARROW_SCHEMA"))
    flags = dict(getattr(mod, "FLAGS", {}))
    return schemas, flags


# ----------------------------------------------------------------------------------------------
# Optional override: config/snapshot_plan.yml (T-05 output; never required -- DEC-10)
# ----------------------------------------------------------------------------------------------
def _normalise_plan_key(key: str) -> str | None:
    k = str(key).strip()
    if k.startswith("system."):
        k = k[len("system."):]
    if "__" in k:
        schema, _, table = k.partition("__")
    elif "." in k:
        schema, _, table = k.partition(".")
    else:
        return None
    if not schema or not table:
        return None
    return f"{schema}__{table}"


def load_plan_overrides(
    path: Path = PLAN_OVERRIDE_PATH,
    predicates: dict[str, Predicate] | None = None,
    schemas: dict[str, pa.Schema] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Predicate]:
    """Return a copy of `predicates` with the per-table overrides from config/snapshot_plan.yml
    applied, or the predicates unchanged when the file does not exist.

    Accepted shapes: a mapping keyed `<schema>__<table>`, `<schema>.<table>` or
    `system.<schema>.<table>` (optionally nested under a top-level `tables:` key), or a list of
    entries each carrying `schema`/`table` (or `name`) keys. Each entry may carry
    `time_column`, `retention_days`, `has_workspace_id`. Only schema-compatible values are
    applied: `time_column` must exist in the table's ARROW_SCHEMA and the embedded default must
    already be windowed (a plan cannot turn a full-history table into a windowed one or vice
    versa); `retention_days` must be a positive integer (it replaces the cap, longer or shorter --
    account-level configurable retention makes both legitimate); `has_workspace_id` is ignored
    because the ARROW_SCHEMA is the verification source. Everything else is logged and ignored.
    """
    preds = dict(predicates if predicates is not None else PREDICATES)
    emit = log or (lambda _msg: None)
    if not path.exists():
        return preds
    try:
        import yaml  # pyyaml, in requirements.txt
    except ImportError:
        emit(f"warning: {path.name} present but pyyaml is not installed; overrides ignored")
        return preds
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - a broken override file must not stop the export
        emit(f"warning: could not parse {path.name} ({type(exc).__name__}); overrides ignored")
        return preds

    entries: list[tuple[str, dict]] = []
    body = doc.get("tables", doc) if isinstance(doc, dict) else doc
    if isinstance(body, dict):
        for k, v in body.items():
            if isinstance(v, dict):
                nk = _normalise_plan_key(k)
                if nk:
                    entries.append((nk, v))
    elif isinstance(body, list):
        for v in body:
            if not isinstance(v, dict):
                continue
            if v.get("schema") and v.get("table"):
                entries.append((f"{v['schema']}__{v['table']}", v))
            elif v.get("name"):
                nk = _normalise_plan_key(v["name"])
                if nk:
                    entries.append((nk, v))

    applied = 0
    for key, entry in entries:
        if key not in preds:
            emit(f"warning: {path.name} names unknown table {key}; ignored")
            continue
        base = preds[key]
        new = base
        if "time_column" in entry and entry["time_column"] is not None:
            col = str(entry["time_column"])
            names = schemas[key].names if schemas and key in schemas else None
            if base.time_column is None:
                emit(f"warning: {path.name} sets time_column for full-history table {key}; ignored")
            elif names is not None and col not in names:
                emit(f"warning: {path.name} time_column {col} not in {key} schema; ignored")
            elif col != base.time_column:
                new = replace(new, time_column=col)
        if "retention_days" in entry and entry["retention_days"] is not None:
            rd = entry["retention_days"]
            if base.time_column is None:
                emit(f"warning: {path.name} sets retention_days for full-history table {key}; ignored")
            elif isinstance(rd, bool) or not isinstance(rd, int) or rd <= 0:
                emit(f"warning: {path.name} retention_days for {key} is not a positive int; ignored")
            elif rd != base.cap_days:
                new = replace(new, cap_days=rd)
        if new != base:
            preds[key] = new
            applied += 1
            emit(f"override from {path.name}: {key} time_column={new.time_column} cap={new.cap_days}")
    if applied:
        emit(f"applied {applied} override(s) from {path.name}")
    return preds


# ----------------------------------------------------------------------------------------------
# Selection (--only / --exclude-schema) and CLI
# ----------------------------------------------------------------------------------------------
def _split_csv(values: Iterable[str] | None) -> list[str]:
    out: list[str] = []
    for v in values or []:
        for part in str(v).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def select_tables(
    predicates: dict[str, Predicate],
    only: Iterable[str] | None,
    exclude_schema: Iterable[str] | None,
) -> list[str]:
    """Return the sorted `<schema>__<table>` keys in scope. `only` accepts schema names and
    `schema.table` / `schema__table` names; `exclude_schema` accepts schema names. Unknown
    names raise SnapshotConfigError so a typo never silently exports nothing."""
    known_schemas = {p.schema for p in predicates.values()}
    keys = sorted(predicates)
    only_list = _split_csv(only)
    if only_list:
        chosen: set[str] = set()
        for item in only_list:
            if item in known_schemas:
                chosen.update(k for k in keys if predicates[k].schema == item)
                continue
            nk = _normalise_plan_key(item)
            if nk in predicates:
                chosen.add(nk)
                continue
            raise SnapshotConfigError(f"--only: unknown schema or table {item!r}")
        keys = [k for k in keys if k in chosen]
    for schema in _split_csv(exclude_schema):
        if schema not in known_schemas:
            raise SnapshotConfigError(f"--exclude-schema: unknown schema {schema!r}")
        keys = [k for k in keys if predicates[k].schema != schema]
    return keys


_WORKSPACE_ID_RE = re.compile(r"^[0-9]+$")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="snapshot.py",
        description="Export the Databricks system tables to a local parquet snapshot "
        "(credentials only from DATABRICKS_SERVER_HOSTNAME / DATABRICKS_HTTP_PATH / "
        "DATABRICKS_TOKEN; there is no --token flag).",
    )
    ap.add_argument("--out", default=DEFAULT_OUT, help="snapshot folder (default: snapshot)")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help="window in days for time-predicated tables (default: 30)")
    ap.add_argument("--billing-days", type=int, default=None,
                    help="window for billing.usage (default: --days; capped at 365)")
    ap.add_argument("--workspace", action="extend", nargs="+", default=[], metavar="ID",
                    help="restrict tables that carry workspace_id to these workspace ids")
    ap.add_argument("--only", action="append", default=[], metavar="SCHEMAS",
                    help="comma-separated schemas (or schema.table) to export; default all")
    ap.add_argument("--exclude-schema", action="append", default=[], metavar="SCHEMAS",
                    help="comma-separated schemas to skip (e.g. query)")
    ap.add_argument("--max-rows-per-file", type=int, default=DEFAULT_MAX_ROWS_PER_FILE,
                    help="roll to a new part-*.parquet after this many rows (default: 500000)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S,
                    help="connector socket timeout in seconds (default: 600)")
    ap.add_argument("--resume", action="store_true",
                    help="reuse a table from the existing manifest at --out only when it is ok, "
                    "was exported the same UTC day with the same window and --workspace filter, "
                    "and its parquet on disk matches its entry; pull every other table again")
    return ap


@dataclass
class Options:
    out: Path
    days: int
    billing_days: int | None = None
    workspace_ids: list[str] = field(default_factory=list)
    only: list[str] = field(default_factory=list)
    exclude_schema: list[str] = field(default_factory=list)
    max_rows_per_file: int = DEFAULT_MAX_ROWS_PER_FILE
    timeout: int = DEFAULT_TIMEOUT_S
    resume: bool = False


def parse_args(argv: list[str] | None = None) -> Options:
    ns = build_parser().parse_args(argv)
    if ns.days <= 0:
        raise SnapshotConfigError("--days must be a positive integer")
    if ns.billing_days is not None and ns.billing_days <= 0:
        raise SnapshotConfigError("--billing-days must be a positive integer")
    if ns.max_rows_per_file <= 0:
        raise SnapshotConfigError("--max-rows-per-file must be a positive integer")
    ws: list[str] = []
    for w in ns.workspace:
        w = str(w).strip()
        if not _WORKSPACE_ID_RE.match(w):
            raise SnapshotConfigError(f"--workspace: {w!r} is not a numeric workspace id")
        if w not in ws:
            ws.append(w)
    return Options(
        out=Path(ns.out),
        days=ns.days,
        billing_days=ns.billing_days,
        workspace_ids=ws,
        only=list(ns.only),
        exclude_schema=list(ns.exclude_schema),
        max_rows_per_file=ns.max_rows_per_file,
        timeout=ns.timeout,
        resume=bool(ns.resume),
    )


# ----------------------------------------------------------------------------------------------
# Arrow normalisation
# ----------------------------------------------------------------------------------------------
def _is_complex(t: pa.DataType) -> bool:
    return patypes.is_map(t) or patypes.is_struct(t) or patypes.is_list(t) or patypes.is_large_list(t)


def _merge_struct_type(live: pa.DataType, target: pa.DataType) -> pa.DataType:
    """The target's fields in its order, each merged recursively, plus any live-only fields
    appended: casting to this type fills dump-known fields the connector lacks with null and
    never drops a sub-field the dump does not know (pyarrow's struct cast would)."""
    if not (patypes.is_struct(live) and patypes.is_struct(target)):
        return target
    live_fields = {f.name: f for f in live}
    fields: list[pa.Field] = []
    for tf in target:
        lf = live_fields.get(tf.name)
        fields.append(pa.field(tf.name, _merge_struct_type(lf.type, tf.type) if lf is not None else tf.type))
    target_names = {f.name for f in target}
    fields.extend(pa.field(lf.name, lf.type) for lf in live if lf.name not in target_names)
    return pa.struct(fields)


def _json_to_complex(col: pa.ChunkedArray | pa.Array, target: pa.DataType) -> pa.Array:
    """The json.loads fallback: a MAP/STRUCT/LIST column the connector returned as JSON strings
    is parsed value by value and rebuilt with the dump-derived Arrow type."""
    values: list[Any] = []
    for v in col.to_pylist():
        if v is None or v == "":
            values.append(None)
        else:
            values.append(json.loads(v))
    if patypes.is_struct(target):
        # Keep JSON keys the dump does not know (pa.array(..., type=target) would drop them).
        try:
            inferred = pa.array(values).type
        except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError):
            inferred = target
        return pa.array(values, type=_merge_struct_type(inferred, target))
    return pa.array(values, type=target)


def normalise_batch(
    batch: pa.Table,
    target: pa.Schema | None,
    cast_to_target: bool,
    warn: Callable[[str], None],
    warned: set[str],
) -> pa.Table:
    """Normalise one Arrow batch before it is written:
    - every top-level timestamp column -> timestamp[us] with no timezone, holding the UTC instant
      (tz-aware values are converted to UTC; naive values are taken as UTC already);
    - when `cast_to_target` (the table has a real dump-derived schema): a string column whose
      target type is MAP/STRUCT/LIST is parsed with json.loads; any other column whose type
      differs from the target is cast (safe cast; on failure the connector's type is kept and a
      warning is logged once per column).
    Columns absent from the target schema are kept as-is (union_by_name tolerates them)."""
    columns: list[pa.ChunkedArray | pa.Array] = []
    fields: list[pa.Field] = []
    for i, fld in enumerate(batch.schema):
        col = batch.column(i)
        t = fld.type
        tfield = target.field(fld.name) if (target is not None and fld.name in target.names) else None
        if patypes.is_timestamp(t):
            if t.unit != "us" or t.tz is not None:
                col = col.cast(pa.timestamp("us"), safe=False)
        elif cast_to_target and tfield is not None and not tfield.type.equals(t):
            tt = tfield.type
            try:
                if (patypes.is_string(t) or patypes.is_large_string(t)) and _is_complex(tt):
                    col = _json_to_complex(col, tt)
                elif patypes.is_timestamp(tt):
                    col = col.cast(pa.timestamp("us"), safe=False)
                elif patypes.is_null(t):
                    col = pa.nulls(len(col), type=tt)
                elif patypes.is_struct(t) and patypes.is_struct(tt):
                    col = col.cast(_merge_struct_type(t, tt), safe=True)
                else:
                    col = col.cast(tt, safe=True)
            except (pa.ArrowInvalid, pa.ArrowNotImplementedError, pa.ArrowTypeError, ValueError,
                    TypeError) as exc:
                if fld.name not in warned:
                    warned.add(fld.name)
                    warn(f"column {fld.name}: kept connector type {t} (cast to {tt} failed: "
                         f"{type(exc).__name__})")
        columns.append(col)
        fields.append(pa.field(fld.name, col.type, nullable=True))
    return pa.Table.from_arrays(columns, schema=pa.schema(fields))


# ----------------------------------------------------------------------------------------------
# Parquet writing
# ----------------------------------------------------------------------------------------------
class PartWriter:
    """Writes Arrow batches for one table into part-NNNNN.parquet files, rolling to a new file
    after `max_rows` rows or when a batch's schema cannot be reconciled with the open file."""

    def __init__(self, folder: Path, max_rows: int):
        self.folder = folder
        self.max_rows = max_rows
        self._writer: pq.ParquetWriter | None = None
        self._schema: pa.Schema | None = None
        self._rows_in_file = 0
        self.files = 0
        self.rows = 0

    def _open(self, schema: pa.Schema) -> None:
        self._close_current()
        path = self.folder / PART_TEMPLATE.format(self.files)
        self._writer = pq.ParquetWriter(str(path), schema, compression="snappy")
        self._schema = schema
        self._rows_in_file = 0
        self.files += 1

    def _close_current(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None
            self._schema = None

    def write(self, table: pa.Table) -> None:
        n = table.num_rows
        if self._writer is None:
            self._open(table.schema)
        elif n == 0:
            return
        elif self._rows_in_file > 0 and self._rows_in_file + n > self.max_rows:
            self._open(table.schema)
        assert self._writer is not None and self._schema is not None
        if not table.schema.equals(self._schema):
            try:
                table = table.select(self._schema.names).cast(self._schema)
            except (pa.ArrowInvalid, pa.ArrowNotImplementedError, pa.ArrowTypeError, KeyError):
                self._open(table.schema)
        assert self._writer is not None
        self._writer.write_table(table)
        self._rows_in_file += n
        self.rows += n

    def close(self) -> None:
        self._close_current()


def write_empty_parquet(folder: Path, schema: pa.Schema) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / PART_TEMPLATE.format(0)
    pq.write_table(schema.empty_table(), str(path), compression="snappy")
    return path


def clear_table_folder(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for p in folder.iterdir():
        if p.is_file() and (p.suffix == ".parquet" or p.name == MISSING_NAME):
            p.unlink()


def _has_parquet(folder: Path) -> bool:
    return folder.is_dir() and any(folder.glob("*.parquet"))


# ----------------------------------------------------------------------------------------------
# SQL building and slices
# ----------------------------------------------------------------------------------------------
# Marker for the per-workspace retry slice that fetches account-level rows (workspace_id IS NULL,
# e.g. billing.usage account rows). Workspace ids are numeric, so it cannot collide.
NULL_WORKSPACE = "NULL"


@dataclass(frozen=True)
class Slice:
    lo: dt.date | None          # inclusive lower bound (None for full tables)
    hi: dt.date | None          # exclusive upper bound
    workspace_id: str | None = None   # None: not per-workspace; NULL_WORKSPACE: IS NULL slice

    @property
    def span_days(self) -> int:
        if self.lo is None or self.hi is None:
            return 0
        return (self.hi - self.lo).days

    def halves(self) -> tuple["Slice", "Slice"]:
        assert self.lo is not None and self.hi is not None and self.span_days > 1
        mid = self.lo + dt.timedelta(days=self.span_days // 2)
        return (replace(self, hi=mid), replace(self, lo=mid))

    def describe(self) -> dict[str, Any]:
        return {
            "from": self.lo.isoformat() if self.lo else None,
            "to": self.hi.isoformat() if self.hi else None,
            "workspace_id": self.workspace_id,
        }


def _time_literal(col_type: pa.DataType | None, day: dt.date) -> str:
    if col_type is not None and patypes.is_date(col_type):
        return f"DATE '{day.isoformat()}'"
    return f"TIMESTAMP '{day.isoformat()} 00:00:00'"


def build_where(
    pred: Predicate,
    sl: Slice,
    workspace_ids: list[str],
    has_workspace_col: bool,
    col_type: pa.DataType | None,
) -> str | None:
    parts: list[str] = []
    if pred.time_column is not None and sl.lo is not None:
        parts.append(f"{pred.time_column} >= {_time_literal(col_type, sl.lo)}")
        if sl.hi is not None:
            parts.append(f"{pred.time_column} < {_time_literal(col_type, sl.hi)}")
    if has_workspace_col:
        if sl.workspace_id == NULL_WORKSPACE:
            parts.append("workspace_id IS NULL")
        else:
            ids = [sl.workspace_id] if sl.workspace_id is not None else workspace_ids
            if ids:
                quoted = ", ".join(f"'{w}'" for w in ids)
                parts.append(f"workspace_id IN ({quoted})")
    return " AND ".join(parts) if parts else None


def build_statement(pred: Predicate, where: str | None) -> str:
    sql = f"SELECT * FROM {pred.fqn}"
    if where:
        sql += f" WHERE {where}"
    return sql


# ----------------------------------------------------------------------------------------------
# The export
# ----------------------------------------------------------------------------------------------
class _TimeStats:
    def __init__(self, column: str | None):
        self.column = column
        self.min: Any = None
        self.max: Any = None

    def update(self, table: pa.Table) -> None:
        if self.column is None or self.column not in table.column_names or table.num_rows == 0:
            return
        col = table.column(self.column)
        if col.null_count == len(col):
            return
        mm = pc.min_max(col).as_py()
        lo, hi = mm["min"], mm["max"]
        if lo is not None and (self.min is None or lo < self.min):
            self.min = lo
        if hi is not None and (self.max is None or hi > self.max):
            self.max = hi

    @staticmethod
    def fmt(v: Any) -> str | None:
        if v is None:
            return None
        if isinstance(v, dt.datetime):
            if v.tzinfo is not None:
                v = v.astimezone(dt.timezone.utc).replace(tzinfo=None)
            return v.replace(microsecond=0).isoformat(timespec="seconds")
        if isinstance(v, dt.date):
            return v.isoformat()
        return str(v)


def new_entry(**values: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "state": STATE_ERROR, "rows": 0, "files": 0, "time_column": None, "days_requested": None,
        "days_effective": None, "min_time": None, "max_time": None, "predicate": None,
        "elapsed_s": 0.0, "slices_failed": [], "error_class": None, "reason": None, "message": None,
        "as_of": None, "workspace_ids": [],
    }
    for k, v in values.items():
        if k not in entry:
            raise KeyError(k)
        entry[k] = v
    return entry


def _ordered_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {k: entry.get(k) for k in ENTRY_KEYS}


def effective_days(pred: Predicate, opts: Options) -> tuple[int | None, int | None]:
    """(days_requested, days_effective) for a table: billing.usage takes --billing-days when
    given; the retention cap is applied before the first request."""
    if pred.time_column is None:
        return None, None
    requested = opts.days
    if pred.kind == "billing" and opts.billing_days is not None:
        requested = opts.billing_days
    effective = requested if pred.cap_days is None else min(requested, pred.cap_days)
    return requested, effective


def applied_workspace_ids(schema: pa.Schema | None, workspace_ids: list[str]) -> list[str]:
    """The workspace filter a table's statement carries, as recorded in its manifest entry: the
    --workspace ids, sorted, when the table has a workspace_id column; [] (not filtered)
    otherwise, or when --workspace was not given."""
    if schema is None or "workspace_id" not in schema.names:
        return []
    return sorted(str(w) for w in workspace_ids)


def _workspace_label(ids: list[str]) -> str:
    return ",".join(ids) if ids else "all"


def resume_mismatch(
    prior: dict[str, Any] | None,
    folder: Path,
    pred: Predicate,
    schema: pa.Schema | None,
    opts: Options,
    as_of_date: dt.date,
) -> str | None:
    """Why --resume must pull this table again, or None when its existing export may be reused.

    Reuse only what this run would itself have written (T-74): an `ok` entry exported on this
    run's as_of date (UTC), with the same time column, the same requested and effective window
    and the same workspace filter, whose folder still holds exactly the parquet files and rows
    the entry records. The checks run in this order; the first that fails is the reason.
    """
    if prior is None:
        return "no entry in the existing manifest"
    if prior.get("state") != STATE_OK:
        return f"state was {prior.get('state')}"
    prior_as_of = prior.get("as_of")
    if not isinstance(prior_as_of, str) or not _AS_OF_RE.fullmatch(prior_as_of):
        return "no valid as_of recorded for it"
    if prior_as_of[:10] != as_of_date.isoformat():
        return f"exported for as_of_date {prior_as_of[:10]}, this run is {as_of_date.isoformat()}"
    if prior.get("time_column") != pred.time_column:
        return f"time column {prior.get('time_column')}, this run {pred.time_column}"
    requested, effective = effective_days(pred, opts)
    if (prior.get("days_requested"), prior.get("days_effective")) != (requested, effective):
        return (f"window {prior.get('days_requested')}->{prior.get('days_effective')} days, "
                f"this run {requested}->{effective} days")
    prior_ws = prior.get("workspace_ids")
    if not isinstance(prior_ws, list):
        return "no workspace filter recorded for it"
    prior_ws = sorted(str(w) for w in prior_ws)
    applied = applied_workspace_ids(schema, opts.workspace_ids)
    if prior_ws != applied:
        return (f"workspace filter {_workspace_label(prior_ws)}, "
                f"this run {_workspace_label(applied)}")
    files = sorted(folder.glob("*.parquet"))
    if not files:
        return "no parquet on disk"
    if len(files) != prior.get("files"):
        return f"{len(files)} parquet file(s) on disk, the entry records {prior.get('files')}"
    try:
        rows = sum(pq.read_metadata(str(f)).num_rows for f in files)
    except (OSError, pa.ArrowException):
        return "a parquet file on disk is unreadable"
    if rows != prior.get("rows"):
        return f"{rows} row(s) on disk, the entry records {prior.get('rows')}"
    return None


class Exporter:
    def __init__(
        self,
        opts: Options,
        connected: Connected,
        predicates: dict[str, Predicate],
        schemas: dict[str, pa.Schema],
        flags: dict[str, str],
        now: dt.datetime,
        log: Callable[[str], None],
    ):
        self.opts = opts
        self.conn = connected.conn
        self.predicates = predicates
        self.schemas = schemas
        self.flags = flags
        self.now = now
        self.as_of_date = now.date()
        self.as_of = now.strftime(AS_OF_FORMAT)
        self._log = log
        self._known_workspaces: list[str] | None = None

    # -- helpers ------------------------------------------------------------------------------
    def log(self, msg: str) -> None:
        self._log(scrub_secrets(msg))

    def _iter_batches(self, sql: str) -> Iterable[pa.Table]:
        cur = self.conn.cursor()
        try:
            cur.execute(sql)
            while True:
                batch = cur.fetchmany_arrow(FETCH_ROWS)
                if batch is None:
                    break
                yield batch
                if batch.num_rows == 0:
                    break
        finally:
            try:
                cur.close()
            except Exception:  # noqa: BLE001 - closing a dead cursor must not mask the real error
                pass

    def known_workspace_ids(self) -> list[str]:
        """Workspace ids for the per-workspace retry when --workspace was not given: from
        access.workspaces_latest, else from billing.usage within the requested window. Queried
        lazily, at most once per run, only on the bisection path."""
        if self.opts.workspace_ids:
            return list(self.opts.workspace_ids)
        if self._known_workspaces is not None:
            return self._known_workspaces
        start = self.as_of_date - dt.timedelta(days=max(self.opts.days, self.opts.billing_days or 0))
        candidates = (
            "SELECT DISTINCT workspace_id FROM system.access.workspaces_latest",
            "SELECT DISTINCT workspace_id FROM system.billing.usage "
            f"WHERE usage_date >= DATE '{start.isoformat()}'",
        )
        found: list[str] = []
        for sql in candidates:
            try:
                for batch in self._iter_batches(sql):
                    if batch.num_rows and "workspace_id" in batch.column_names:
                        for v in batch.column("workspace_id").to_pylist():
                            if v is not None and str(v) not in found:
                                found.append(str(v))
            except Exception as exc:  # noqa: BLE001 - discovery is best effort
                _, reason, message = classify_error(exc)
                self.log(f"workspace discovery via {sql.split(' FROM ')[1].split(' ')[0]} failed "
                         f"({reason}): {message}")
                continue
            if found:
                break
        self._known_workspaces = sorted(found)
        self.log(f"workspace discovery: {len(self._known_workspaces)} id(s)")
        return self._known_workspaces

    # -- one table --------------------------------------------------------------------------
    def export_table(self, key: str) -> dict[str, Any]:
        pred = self.predicates[key]
        folder = self.opts.out / key
        schema = self.schemas.get(key)
        cast_to_target = schema is not None and self.flags.get(key) != "MISSING_FROM_DUMP"
        has_ws = schema is not None and "workspace_id" in schema.names
        col_type = None
        if schema is not None and pred.time_column and pred.time_column in schema.names:
            col_type = schema.field(pred.time_column).type
        requested, effective = effective_days(pred, self.opts)

        clear_table_folder(folder)
        t0 = time.perf_counter()
        entry = new_entry(time_column=pred.time_column, days_requested=requested,
                          days_effective=effective, as_of=self.as_of,
                          workspace_ids=applied_workspace_ids(schema, self.opts.workspace_ids))

        if pred.time_column is None:
            initial = Slice(None, None)
        else:
            assert effective is not None
            initial = Slice(self.as_of_date - dt.timedelta(days=effective),
                            self.as_of_date + dt.timedelta(days=1))
        entry["predicate"] = build_where(pred, initial, self.opts.workspace_ids, has_ws, col_type)
        self.log(f"{key}: exporting (window {requested}->{effective} days on {pred.time_column})"
                 if pred.time_column else f"{key}: exporting (full, {pred.kind})")

        writer = PartWriter(folder, self.opts.max_rows_per_file)
        stats = _TimeStats(pred.time_column)
        warned: set[str] = set()
        failed: list[dict[str, Any]] = []
        pending: deque[Slice] = deque([initial])
        statements = 0
        while pending:
            sl = pending.popleft()
            where = build_where(pred, sl, self.opts.workspace_ids, has_ws, col_type)
            sql = build_statement(pred, where)
            statements += 1
            gen = self._iter_batches(sql)
            try:
                first = True
                for batch in gen:
                    if batch.num_rows == 0 and not first:
                        continue
                    first = False
                    norm = normalise_batch(batch, schema, cast_to_target,
                                           lambda m: self.log(f"{key}: {m}"), warned)
                    stats.update(norm)
                    writer.write(norm)
            except Exception as exc:  # noqa: BLE001 - every failure is classified and recorded
                error_class, reason, message = classify_error(exc)
                if reason == REASON_TOO_MUCH_DATA:
                    if sl.span_days > 1:
                        left, right = sl.halves()
                        self.log(f"{key}: too much data for {sl.describe()}; bisecting")
                        pending.appendleft(right)
                        pending.appendleft(left)
                        continue
                    if sl.workspace_id is None and has_ws and len(self.opts.workspace_ids) != 1:
                        # (a single --workspace id is already the slice's predicate: retrying
                        # it would re-send the identical statement, so fall through to failed)
                        ws_ids = self.known_workspace_ids()
                        if ws_ids:
                            retry = list(ws_ids)
                            if not self.opts.workspace_ids:
                                # Unfiltered export: the whole-window statement also covered
                                # account-level rows (workspace_id IS NULL); keep them.
                                retry.append(NULL_WORKSPACE)
                            self.log(f"{key}: too much data for one-day slice {sl.describe()}; "
                                     f"retrying per workspace ({len(retry)} slices)")
                            for w in reversed(retry):
                                pending.appendleft(replace(sl, workspace_id=w))
                            continue
                    rec = sl.describe()
                    rec.update({"reason": reason, "error_class": error_class, "message": message})
                    failed.append(rec)
                    self.log(f"{key}: slice still too much data after bisection: {sl.describe()}")
                    continue
                if sl == initial:
                    # The whole-table statement failed for a non-size reason: the table cannot
                    # be read -> _MISSING.json + empty typed parquet, never a dropped source.
                    writer.close()
                    clear_table_folder(folder)
                    entry.update(self._missing(key, folder, reason, error_class, message))
                    entry["elapsed_s"] = round(time.perf_counter() - t0, 3)
                    self.log(f"{key}: {entry['state']} ({reason}): {message}")
                    return _ordered_entry(entry)
                rec = sl.describe()
                rec.update({"reason": reason, "error_class": error_class, "message": message})
                failed.append(rec)
                self.log(f"{key}: slice {sl.describe()} failed ({reason}): {message}")
            finally:
                gen.close()
        writer.close()
        if writer.files == 0:
            # Nothing came back (not even an empty first batch): still leave one typed file so
            # read_parquet('<folder>/*.parquet') resolves.
            write_empty_parquet(folder, schema if schema is not None else pa.schema([]))
            writer.files = 1
        entry.update({
            "state": STATE_PARTIAL if failed else STATE_OK,
            "rows": writer.rows,
            "files": writer.files,
            "min_time": _TimeStats.fmt(stats.min),
            "max_time": _TimeStats.fmt(stats.max),
            "slices_failed": failed,
            "elapsed_s": round(time.perf_counter() - t0, 3),
        })
        if failed:
            entry["reason"] = REASON_TOO_MUCH_DATA if all(
                f.get("reason") == REASON_TOO_MUCH_DATA for f in failed) else failed[0].get("reason")
            entry["message"] = f"{len(failed)} slice(s) failed; see slices_failed"
        self.log(f"{key}: {entry['state']} rows={writer.rows} files={writer.files} "
                 f"statements={statements} elapsed={entry['elapsed_s']}s")
        return _ordered_entry(entry)

    def _missing(self, key: str, folder: Path, reason: str, error_class: str | None,
                 message: str) -> dict[str, Any]:
        """Write _MISSING.json + one empty typed parquet; return the entry fields to merge."""
        schema = self.schemas.get(key)
        state = state_for_reason(reason)
        write_empty_parquet(folder, schema if schema is not None else pa.schema([]))
        doc = {
            "table": self.predicates[key].fqn,
            "state": state,
            "error_class": error_class,
            "reason": reason,
            "message": scrub_secrets(message),
            "as_of": self.as_of,
        }
        _write_json(folder / MISSING_NAME, doc)
        return {"state": state, "rows": 0, "files": 1, "error_class": error_class,
                "reason": reason, "message": scrub_secrets(message)}

    def excluded_entry(self, key: str, existing: dict[str, Any] | None) -> dict[str, Any]:
        """An out-of-scope table (--only / --exclude-schema): keep the existing manifest entry
        when it has data on disk; otherwise write _MISSING.json + empty parquet so dbt sources
        still resolve, and record not_assessed / excluded_by_flag."""
        folder = self.opts.out / key
        if existing is not None and _has_parquet(folder):
            return _ordered_entry(existing)
        pred = self.predicates[key]
        requested, effective = effective_days(pred, self.opts)
        entry = new_entry(time_column=pred.time_column, days_requested=requested,
                          days_effective=effective, as_of=self.as_of,
                          workspace_ids=applied_workspace_ids(self.schemas.get(key),
                                                              self.opts.workspace_ids))
        clear_table_folder(folder)
        entry.update(self._missing(
            key, folder, REASON_EXCLUDED, None,
            "excluded by --only / --exclude-schema; no export attempted in this run"))
        return _ordered_entry(entry)


def _write_json(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=True)
        fh.write("\n")
    os.replace(tmp, path)


def load_manifest(out: Path) -> dict[str, Any] | None:
    path = out / MANIFEST_NAME
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("tables"), dict):
        return None
    return doc


def _default_log(msg: str) -> None:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")
    print(f"{stamp} {scrub_secrets(msg)}", file=sys.stderr, flush=True)


def run_export(
    opts: Options,
    connect_fn: Callable[[int], Connected] | None = None,
    now: dt.datetime | None = None,
    log: Callable[[str], None] = _default_log,
    predicates: dict[str, Predicate] | None = None,
    plan_path: Path | None = PLAN_OVERRIDE_PATH,
    ddl_path: Path = DDL_MODULE_PATH,
) -> dict[str, Any]:
    """Run the export and return the manifest that was written to `opts.out / manifest.json`.

    `connect_fn(timeout)` must return a Connected; tests pass a fake, the CLI uses connect()
    (resolved here at call time). `now` fixes `as_of` (UTC); default is the wall clock.
    """
    def emit(msg: str) -> None:
        log(scrub_secrets(msg))

    if connect_fn is None:
        connect_fn = connect
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is not None:
        now = now.astimezone(dt.timezone.utc).replace(tzinfo=None)
    schemas, flags = load_arrow_schemas(ddl_path)
    preds = dict(predicates if predicates is not None else PREDICATES)
    if plan_path is not None:
        preds = load_plan_overrides(plan_path, preds, schemas, emit)
    selected = select_tables(preds, opts.only, opts.exclude_schema)
    if not selected:
        raise SnapshotConfigError("no tables selected")

    existing = load_manifest(opts.out)
    existing_tables: dict[str, Any] = dict(existing["tables"]) if existing else {}
    if opts.resume and existing is None:
        emit("--resume: no existing manifest; exporting every table")

    connected = connect_fn(opts.timeout)     # nothing is written to --out before this succeeds
    opts.out.mkdir(parents=True, exist_ok=True)
    as_of = now.strftime(AS_OF_FORMAT)
    emit(f"connected (connector {connected.connector_version}, host {connected.host_fingerprint}); "
         f"{len(selected)} of {len(preds)} tables in scope, as_of {as_of}")
    emit(f"metastore {describe_metastore(connected.metastore)}: regional system tables cover this "
         "metastore's region only; billing covers the whole account")
    exporter = Exporter(opts, connected, preds, schemas, flags, now, log)

    # --resume (T-74): decide once, before anything is exported, which in-scope tables are reused
    # as they are and which are pulled again. A table is reused only when this run would have
    # written the same thing (resume_mismatch); nothing from another run is mixed in.
    reused: dict[str, str] = {}      # key -> the as_of of the run that exported it
    repull: dict[str, str] = {}      # key -> why it is pulled again
    if opts.resume:
        for key in selected:
            prior = existing_tables.get(key)
            why = resume_mismatch(prior, opts.out / key, preds[key], schemas.get(key), opts,
                                  now.date())
            if why is None:
                reused[key] = str(prior["as_of"])
            else:
                repull[key] = why
        emit(f"--resume: reusing {len(reused)} table(s), re-pulling {len(repull)}")
    # One snapshot, one start: every reused table was exported on this as_of date, and the
    # snapshot's as_of is the oldest of them, so a resumed snapshot reads like one run that began
    # when its oldest table was read (a table may hold data newer than as_of in any run, since
    # tables are read one after another; never older).
    snapshot_as_of = min([as_of, *reused.values()])

    tables: dict[str, Any] = {}

    def write_manifest() -> dict[str, Any]:
        # Written after every table, not only at the end: an interrupted run leaves a manifest
        # that describes what is on disk, and --resume picks up from it. Tables this run has not
        # reached yet keep their previous entry while their folder still holds parquet.
        merged = dict(tables)
        for k, prior in existing_tables.items():
            if k not in merged and k in preds and _has_parquet(opts.out / k):
                merged[k] = _ordered_entry(prior)
        doc: dict[str, Any] = {
            "as_of": snapshot_as_of,
            "as_of_date": now.date().isoformat(),
            "days": opts.days,
            "billing_days": opts.billing_days if opts.billing_days is not None else opts.days,
            "workspace_ids": list(opts.workspace_ids),
            "host_fingerprint": connected.host_fingerprint,
            "metastore": connected.metastore,
            "connector_version": connected.connector_version,
            "tables": {k: merged[k] for k in sorted(merged)},
        }
        _write_json(opts.out / MANIFEST_NAME, doc)
        return doc

    try:
        for key in sorted(preds):
            prior = existing_tables.get(key)
            if key not in selected:
                tables[key] = exporter.excluded_entry(key, prior)
            elif key in reused:
                tables[key] = _ordered_entry(prior)
                emit(f"{key}: reused (--resume: exported {reused[key]}, same as_of_date, window "
                     f"and workspace filter)")
            else:
                if key in repull:
                    emit(f"{key}: re-pulling (--resume: {repull[key]})")
                tables[key] = exporter.export_table(key)
            write_manifest()
    finally:
        try:
            connected.conn.close()
        except Exception:  # noqa: BLE001
            pass

    manifest = write_manifest()
    counts = {s: sum(1 for e in tables.values() if e["state"] == s) for s in STATES}
    resume_note = (f"; {len(reused)} reused and {len(repull)} re-pulled by --resume"
                   if opts.resume else "")
    emit(f"snapshot written: {len(tables)} tables (ok {counts[STATE_OK]}, partial "
         f"{counts[STATE_PARTIAL]}, not_assessed {counts[STATE_NOT_ASSESSED]}, error "
         f"{counts[STATE_ERROR]}{resume_note}) -> {opts.out / MANIFEST_NAME}")
    return manifest


def main(argv: list[str] | None = None) -> int:
    try:
        opts = parse_args(argv)
        run_export(opts)
    except SnapshotConfigError as exc:
        print(f"error: {scrub_secrets(str(exc))}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted: manifest.json describes the tables finished so far; "
              "re-run the same command with --resume to continue (a table is reused only when "
              "it was exported the same UTC day with the same --days, --billing-days and "
              "--workspace; the rest are pulled again)", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - a raw traceback could carry request headers
        print(f"error: {type(exc).__name__}: {scrub_secrets(str(exc))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
