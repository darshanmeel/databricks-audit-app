"""app/core/data.py

Read-only DuckDB store (PLAN.md 5.6). Every public function opens a short-lived
`duckdb.connect(db_path, read_only=True)` connection and closes it before returning -- never a
held connection, never a write lock -- because tools/dbt_run.py swaps the db file out from under
a running app with os.replace() + a retry loop while the app may still be open (DEC-47 point 4:
per-call, read-only connections; the DuckDB connection itself is wrapped in st.cache_resource by
the caller, not by this module). AUDIT_DB / AUDIT_SNAPSHOT_DIR / AUDIT_SNAPSHOT_MANIFEST /
AUDIT_RUN_RESULTS are all re-read from os.environ on every call (DEC-27) -- never cached at import
time -- so tests/conftest.py (and T-33's scenario suite) can point the app at a different fixture
within one process.

DuckDB does the work, pandas only holds the displayed slice (DEC-47): every filter, the ORDER BY
and the row cap are pushed into the SQL text with bound parameters; a separate `SELECT count(*)`
(with the same filters, no LIMIT) supplies `rows_total` so a panel can say "showing <n> of
<rows_total>". MAP/STRUCT/LIST columns are selected as `to_json(col)`, never as a raw Python
object, so no nested structure ever lands in the pandas frame.

    read_finding(query_id, window_days, workspace_ids=None, env=None, tag=None,
                 limit=None, attributes=None, job_id=None, statuses=None, *,
                 tag_filter=None) -> FindingResult
    count_finding(query_id, window_days, workspace_ids=None, env=None, tag=None,
                  attributes=None, statuses=None, *, tag_filter=None) -> int
    count_finding_ex(...) -> (int, TagApplication)
                 count_finding's own twin, also returning the TagApplication (chain/applied/
                 reason) `tag_filter` produced -- callers that need scope.tag use this instead.
    finding_window_counts(specs, workspace_ids=None, env=None, attributes=None, *,
                          tag_filter=None) -> dict[query_id, dict]
                 bulk twin of count_finding for a list of (query_id, window_days) pairs, one
                 connection for the whole batch (app/api's findings-list endpoint). `attributes`
                 (T-63/DEC-60/P2-FILTERS) is the same {canonical_key: [value, ...]} contract as
                 count_finding's own -- applies only on a table with workspace_id. `tag_filter`
                 (P4-T-IDX section 5.5) applies the same chain/clause mechanism as count_finding_ex,
                 with tags.tag_entity's existence checked once for the whole batch; a query_id
                 whose chain needs it when the batch already knows it is missing comes back
                 `tag_models_not_built` instead of a tag clause, so the caller can report
                 NOT_ASSESSED rather than a silently-unfiltered count.
    aggregate_finding(query_id, window_days, group, agg, value=None, *, workspace_ids=None,
                       env=None, attributes=None, statuses=None, top=None,
                       tag_filter=None) -> AggregateResult
                 T-68: a server-side GROUP BY/aggregate over EVERY row matching the filters (no
                 LIMIT before the aggregation -- the fix for "a chart/tile summed only the first
                 `ui.max_rows` rows the browser fetched"). `group` is 0-2 real, non-complex column
                 names; `agg` is "sum" (needs `value`, a real numeric-ish column), "count" (row
                 count, `value` ignored) or "count_distinct" (needs `value`). Every group column
                 and the value column are validated against the table's OWN information_schema
                 columns before being spliced into SQL text -- the same whitelist-before-splice
                 discipline every other identifier in this module follows (see the file docstring
                 below). `statuses` applies only when the table has a "status" column (same
                 unaffected-if-absent contract as job_id/tag). Returns every group (no top-N
                 truncation server-side) unless `top` is given, in which case the caller gets the
                 top `top` groups by value desc plus one "other" bucket folding the rest -- so
                 `sum(group.value for group in groups) + (other.value or 0)` always equals the
                 TRUE total over every matching row, never a slice's total.
    P4-T-IDX: `tag_filter` (app.core.tags.TagFilter or TagFilterSet, keyword-only, additive to the
                 legacy `tag` dict/`has_tag` row-tags branch) filters a finding through
                 app.core.tags.tag_chain/tag_set_clause instead -- the search-and-chain mechanism
                 (tasks/P4-T-SPEC.md section 5.3/5.4), reaching far more tables than the row-tags
                 branch's own tag_key/tag_value columns. A bare TagFilter (today's single
                 key:value) is normalised into a one-group TagFilterSet in _build_filters; a
                 TagFilterSet ANDs several tag names, ORing each name's own values. Raises
                 TagModelsNotBuiltError when the chain needs tags.tag_entity and that table does
                 not exist -- never falls back to unfiltered rows. FindingResult.applicability and
                 AggregateResult.tag_application carry the chain/applied/reason detail
                 app/api/service.py turns into scope.tag.
    read_dim_workspace() -> pandas.DataFrame
    read_dim_job() -> pandas.DataFrame
    read_dim_cluster() -> pandas.DataFrame
    read_dim_warehouse() -> pandas.DataFrame
    read_dim_pipeline() -> pandas.DataFrame
    db_state() -> dict     P2-NOBUILD: never raises -- "no_database" | "build_incomplete" | "ready",
                 read by GET /api/status before anything else touches the database.
    snapshot_manifest() -> dict
    run_results() -> dict
    direct_export_info() -> dict | None
                 {"as_of", "window_coverage", "windows", "tags_sources_not_exported"} when
                 tools/load_direct_results.py built this db, else None.
    exported_window_coverage() -> dict[int, int] | None
                 {label: days_used} from a direct export's own window_coverage meta; None for an
                 ordinary dbt/snapshot build.
    window_status(choices) -> list[dict]
                 per label in `choices`: {"days", "available", "covered_days", "partial"} --
                 GET /api/meta's own "windows" feed.
    truncation_info(query_id, window_days=None) -> dict
                 {"truncated", "max_rows"} recorded by the same loader, per query_id (and per
                 window when only one of a windowed check's windows hit the row cap).
    classify_error(status, message) -> str
    finding_status(query_id) -> dict
    window_coverage(query_id, window_days) -> dict
                 partial/covered_days from a direct export's own window_coverage when it has one
                 for this window, else the snapshot manifest's per-source min_time coverage.
    finding_columns(query_id) -> list[str]           (T-71: the column names of findings."f_<id>")
    finding_columns_batch(query_ids) -> dict[query_id, list[str]]
                 GUIDE-SPEC 3.5: the batch twin of finding_columns above, one connection for many
                 ids -- a query_id whose findings.f_<id> is not built yet is simply absent from the
                 result (never raises), unlike the single-id form.
    region_split(workspace_ids=None, env=None, attributes=None) -> dict
                 T-71: which workspaces a selection covers, and which of them the snapshot's
                 regional system tables hold no row for (dims.dim_workspace.in_snapshot_region).
    spec_is_regional(spec) -> bool / is_regional(query_id) -> bool
                 T-71: True when the query reads at least one regional (non-GLOBAL_SOURCES) table.
    GLOBAL_SOURCES                                    (T-71: the four account-wide system tables)

query_id is never string-formatted into a filter VALUE -- every bound value (window_days,
workspace ids, resolved env workspace ids, tag key/value, the row limit) is passed as a duckdb
bind parameter, never spliced into the SQL text. query_id itself, which DuckDB has no bind
parameter for (it names a table, `findings."f_<qid>"`), is validated first: it must both match the
charset every real query_id uses and resolve through app/core/registry.by_id (the actual
whitelist -- registry.by_id raises KeyError for anything it does not recognise) before it is ever
substituted into SQL text.

Stdlib + duckdb + pandas.
"""
from __future__ import annotations

import copy
import functools
import json
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import yaml

# DuckDB's default connection, unused here, otherwise keeps a thread per core.
duckdb.execute("SET threads = 1")

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.core import config as app_config  # noqa: E402
from app.core import datasource  # noqa: E402
from app.core import finding_columns as app_finding_columns  # noqa: E402
from app.core import identity as app_identity  # noqa: E402
from app.core import materiality as app_materiality  # noqa: E402
from app.core import registry  # noqa: E402
from app.core import rls  # noqa: E402
from app.core import tags as app_core_tags  # noqa: E402  -- P4-T-IDX; tags.py imports this module
from app.core import tag_compliance as app_tag_compliance  # noqa: E402
from app.core import tag_spend as app_tag_spend  # noqa: E402
                                                            # back only lazily, see its own note

ROOT = _ROOT

_DBT_PROJECT_NAME = "databricks_audit"  # dbt/dbt_project.yml `name:` (T-05) -- unique_id prefix
_QUERY_ID_RE = re.compile(r"[a-z][a-z0-9_]*")

# meta.order_by term shapes (real values surveyed from dbt/target/manifest.json across every
# model then built, DEC-47 point 2): a comma-separated list of terms, each either (a) a bare/alias-qualified
# identifier with an optional ASC|DESC and/or NULLS FIRST|LAST, e.g. "u.usage_date DESC",
# "days_since_altered DESC NULLS LAST", or (b) the generator's own status-priority CASE
# expression, e.g. "CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END" (the THEN
# priority order varies per query -- never assume CRITICAL=0/WARN=1/NOT_ASSESSED=2/OK=3). A term
# that is neither (e.g. "(a + b) DESC", a genuine expression) invalidates the WHOLE order_by, not
# just that term, and the caller falls back to status-rank ordering.
_CASE_STATUS_RE = re.compile(
    r"^CASE\s+status\s+(?:WHEN\s+'(?:CRITICAL|WARN|OK|NOT_ASSESSED)'\s+THEN\s+\d+\s+)+"
    r"ELSE\s+\d+\s+END$",
    re.IGNORECASE,
)
_ORDER_TERM_RE = re.compile(
    r"^(?:(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\.)?"
    r"(?P<ident>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\s+(?P<dir>ASC|DESC))?"
    r"(?:\s+NULLS\s+(?P<nulls>FIRST|LAST))?$",
    re.IGNORECASE,
)


class FindingNotBuiltError(RuntimeError):
    """findings.f_<query_id> does not exist in the current db_path -- the model failed, was
    skipped, or the db just has not been (re)built yet. Callers render the NOT_ASSESSED card
    (PLAN.md 6.2) instead of crashing; call finding_status(query_id) to find out why."""


@dataclass
class FindingResult:
    df: pd.DataFrame
    rows_total: int
    applicability: dict  # {"has_workspace_id": bool, "has_tag": bool, "has_job_id": bool}
    # Materiality floor: how many of THESE rows_total rows have their floor column under `min` --
    # 0 when the query has no configured floor or the table does not carry its column (never
    # computed in that case, no extra query run). app/api/service.py's own "floor" field.
    rows_below_floor: int = 0
    # None when no workspace/env/attribute filter applies (or the check is not workspace-
    # filterable), else {"rows": N} -- rows the filter left out for having no resolvable
    # workspace (workspace_id NULL/'0', or a warehouse/cluster missing from the dims). Contract C.
    excluded_no_workspace: dict | None = None


# ---------------------------------------------------------------------------------------------
# Env-var resolution (DEC-27: fresh on every call, never cached at import time)
# ---------------------------------------------------------------------------------------------


_LEGACY_DB_PATH = ROOT / "data" / "db_audit.duckdb"


def _db_path() -> Path:
    """The db this request reads: the base db, or the signed-in user's own file under rls apply=export."""
    return rls.resolve(_base_db_path())


def _base_db_path() -> Path:
    """AUDIT_DB, else the pointer a refresh last wrote (datasource.current_db(), so a restart
    keeps serving what refresh built), else the data folder's own db once it exists, else the
    legacy dev-build db (data/db_audit.duckdb). First match wins -- no mtime comparison."""
    env = os.environ.get("AUDIT_DB")
    if env:
        return Path(env)
    current = datasource.current_db()
    if current is not None:
        return current
    data_db = datasource.db_path()
    if data_db.exists():
        return data_db
    return _LEGACY_DB_PATH


def _snapshot_dir() -> Path:
    env = os.environ.get("AUDIT_SNAPSHOT_DIR")
    return Path(env) if env else ROOT / "snapshot"


def _is_loader_built(db: Path) -> bool:
    """True when tools/load_direct_results.py built `db` -- checked without assuming its path
    (a --db elsewhere must still skip the repo's own dev snapshot), first via the cheap sidecar
    file the loader always writes, then by looking for its direct_export_meta table."""
    if datasource.sidecar(db, "run_results").exists():
        return True
    if not db.exists():
        return False
    try:
        con = duckdb.connect(str(db), read_only=True)
    except duckdb.Error:
        return False
    try:
        return bool(con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'direct_export_meta'"
        ).fetchone()[0])
    except duckdb.Error:
        return False
    finally:
        con.close()


def _snapshot_manifest_path() -> Path:
    # A loader-built db never falls back to ROOT/snapshot -- that folder belongs to a dev dbt
    # build and would misreport an unrelated snapshot as this database's own.
    env = os.environ.get("AUDIT_SNAPSHOT_MANIFEST")
    if env:
        return Path(env)
    db = _db_path()
    if _is_loader_built(db):
        return datasource.sidecar(db, "snapshot_manifest")
    return _snapshot_dir() / "manifest.json"


def _run_results_path() -> Path:
    env = os.environ.get("AUDIT_RUN_RESULTS")
    if env:
        return Path(env)
    sidecar = datasource.sidecar(_db_path(), "run_results")
    if sidecar.exists():
        return sidecar
    return ROOT / "dbt" / "target" / "run_results.json"


def _dbt_manifest_path() -> Path:
    return ROOT / "dbt" / "target" / "manifest.json"


# The API server shares one read-only connection per db file: a fresh connection per call re-read
# the catalog and dropped DuckDB's block cache, about 1 s per call. Off for tools and tests, which
# rewrite db files that an open connection would lock.
share_connection = False
_SHARED_CONS: dict = {}  # _identity_key(path) -> connection, least recently used first
_SHARED_MAX = 8
_SHARED_LOCK = threading.Lock()


def _identity_key(path: str) -> tuple:
    """(path, inode, mtime, size): changes whenever a refresh points _db_path() at a new file,
    or the same path is overwritten in place -- either way the cached connection is stale."""
    try:
        st = os.stat(path)
        return (path, st.st_ino, st.st_mtime_ns, st.st_size)
    except OSError:
        return (path, None, None, None)


def _cap(con: duckdb.DuckDBPyConnection) -> None:
    """The app reads with the same memory and thread caps as the load (settings load:), not
    DuckDB's default of 80% of RAM and every core."""
    try:
        load = app_config.load_settings()["load"]
        con.execute(f"SET memory_limit = '{load['memory_limit']}'")
        con.execute(f"SET threads = {int(load['threads'])}")
    except Exception:  # noqa: BLE001 -- a settings error is reported elsewhere; reading still works
        pass


def _connect(read_only: bool = True) -> duckdb.DuckDBPyConnection:
    if read_only and rls.mode() == "query":
        return rls.scoped_connection(_base_db_path(), rls.current_principal())
    path = str(_db_path())
    if not (share_connection and read_only):
        return duckdb.connect(path, read_only=read_only)
    key = _identity_key(path)
    with _SHARED_LOCK:
        con = _SHARED_CONS.get(key)
        if con is None:
            con = duckdb.connect(path, read_only=True)
            _cap(con)
            # Handles of other db versions go, so Windows can delete those files; a user's own file stays.
            base = _base_db_path()
            family = {str(base), *rls.set_files(base)}
            for old in [k for k in _SHARED_CONS if k[0] == key[0] or k[0] not in family]:
                _SHARED_CONS.pop(old).close()
            _SHARED_CONS[key] = con
            while len(_SHARED_CONS) > _SHARED_MAX:  # per-user files: the least recently used goes
                _SHARED_CONS.pop(next(iter(_SHARED_CONS))).close()
        else:
            _SHARED_CONS[key] = _SHARED_CONS.pop(key)
        return con.cursor()


def _file_cached(path_fn):
    """Memoize on (file, mtime, size, args): the wrapped read only changes when that file does, and
    re-running it on every request made each API call take seconds."""
    def deco(fn):
        cache: dict = {}

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            path = path_fn()
            try:
                st = path.stat()
            except OSError:
                return fn(*args, **kwargs)
            key = (str(path), st.st_mtime_ns, st.st_size, rls.scope_key(),
                   repr([a for a in args if not isinstance(a, duckdb.DuckDBPyConnection)]),
                   repr(sorted(kwargs.items())))
            if key not in cache:
                if len(cache) > 4096:
                    cache.clear()
                cache[key] = fn(*args, **kwargs)
            return copy.deepcopy(cache[key])

        wrapper.cache_clear = cache.clear
        return wrapper
    return deco


# ---------------------------------------------------------------------------------------------
# db_state -- P2-NOBUILD: the one check GET /api/status runs before anything else touches the
# database, so app/web's shell can tell "no database yet" apart from "a database exists but the
# build failed or is partial" instead of both reading as the same opaque 500 ("Could not load
# 500 Internal Server Error", the exact bug observed against a fresh checkout and, separately,
# against a build where dim_workspace failed). Never raises.
# ---------------------------------------------------------------------------------------------


def db_state() -> dict:
    """{"state": "no_database", "db_path": str} when _db_path() does not exist on disk at all
    (before the first dbt build ever ran). {"state": "build_incomplete", "db_path": str,
    "detail": str} when the file exists but dims.dim_workspace -- the one table nearly every
    other read ultimately depends on (GET /api/workspaces and, through it, the whole filter bar)
    -- cannot be read: either the file is not a valid DuckDB database at all, or the build never
    got far enough to create that table. Both look the same to a caller ("re-run the build") so
    they share one state; the `detail` string (DuckDB's own error message) is for a reader who
    wants to know exactly which. {"state": "ready", "db_path": str} otherwise.

    One finding table missing while dim_workspace built fine (one model failed, not the whole
    build) is deliberately NOT this function's concern -- that is reported per-query as
    NOT_ASSESSED (app/core/data.finding_status), which is honest at the row level and does not
    need the whole app to show a blocking screen over one bad model."""
    path = _db_path()
    if not path.exists():
        return {"state": "no_database", "db_path": str(path)}
    con = None
    try:
        con = duckdb.connect(str(path), read_only=True)
        con.execute("SELECT 1 FROM dims.dim_workspace LIMIT 1")
    except (duckdb.Error, OSError) as exc:
        return {"state": "build_incomplete", "db_path": str(path), "detail": str(exc)}
    finally:
        if con is not None:
            con.close()
    return {"state": "ready", "db_path": str(path)}


# ---------------------------------------------------------------------------------------------
# Identifier validation
# ---------------------------------------------------------------------------------------------


def _validate_query_id(query_id: str) -> registry.QuerySpec:
    if not _QUERY_ID_RE.fullmatch(query_id):
        raise ValueError(f"bad query_id {query_id!r}")
    return registry.by_id(query_id)  # raises KeyError for anything not in the real registry


# T-71: the only system tables whose rows cover the whole account. Every other source is
# regional: it holds rows only for the workspaces of the metastore region the snapshot
# connected through. access.audit is both (workspace_id '0' account-level events are global,
# workspace-level events are regional) and counts as regional.
GLOBAL_SOURCES = frozenset({
    "system.billing.usage",
    "system.billing.list_prices",
    "system.billing.attributed_usage",
    "system.access.workspaces_latest",
})


def spec_is_regional(spec: registry.QuerySpec) -> bool:
    """T-71: True when any of the query's own sources is a regional system table."""
    return any(f"system.{schema}.{table}" not in GLOBAL_SOURCES for schema, table in spec.sources)


def is_regional(query_id: str) -> bool:
    return spec_is_regional(_validate_query_id(query_id))


# ---------------------------------------------------------------------------------------------
# read_finding / count_finding
# ---------------------------------------------------------------------------------------------


@_file_cached(lambda: _db_path())
def _table_columns(con: duckdb.DuckDBPyConnection, query_id: str) -> list[tuple[str, str]]:
    rows = con.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_catalog = current_database() AND table_schema = 'findings' AND table_name = ? ORDER BY ordinal_position",
        [f"f_{query_id}"],
    ).fetchall()
    if not rows:
        raise FindingNotBuiltError(
            f'findings."f_{query_id}" does not exist in {_db_path()} -- the model failed, was '
            "skipped, or the db has not been (re)built yet; see finding_status(query_id)."
        )
    return [(r[0], r[1]) for r in rows]


def _is_complex(dtype: str) -> bool:
    d = dtype.upper()
    return d.startswith("MAP(") or d.startswith("STRUCT(") or d.endswith("[]")


def _select_list(cols: list[tuple[str, str]], query_id: str) -> str:
    """The SELECT list for read_finding. Plain columns pass through unchanged (a MAP/STRUCT/LIST
    one as to_json). When a materiality floor, a severity cap or admin_groups applies here, "status" is
    instead selected as the EFFECTIVE status -- so a below-floor CRITICAL/WARN row reads OK, and a
    capped report's CRITICAL reads WARN, everywhere a caller reads `status`, the same as it already
    does in status_counts/an aggregate sum -- with the raw value kept alongside as status_raw.
    below_floor (real for an applicable floor, else a flat FALSE) is always appended, so every
    finding row carries it whether or not this query has a floor at all."""
    col_names = [name for name, _ in cols]
    floor = app_materiality.applicable_floor(query_id, col_names)
    effective = app_materiality.status_expr(query_id, col_names) != 't."status"'
    parts = []
    for name, dtype in cols:
        if name == "status" and effective:
            parts.append(f'{app_materiality.status_expr(query_id, col_names)} AS "status"')
            parts.append('"status" AS "status_raw"')
        elif _is_complex(dtype):
            parts.append(f'to_json("{name}") AS "{name}"')
        else:
            parts.append(f'"{name}"')
    below_expr = app_materiality.below_floor_expr(query_id, col_names) if floor is not None else None
    parts.append(f'({below_expr}) AS "below_floor"' if below_expr is not None else 'FALSE AS "below_floor"')
    return ", ".join(parts)


def _attribute_canonical_keys() -> set[str]:
    """The workspace-attribute column names _build_filters may splice into a dims.dim_workspace
    WHERE clause: config/tag_aliases.yml's top tags, never the caller's query-param names."""
    return set(app_config.load_top_tags())


@dataclass
class TagApplication:
    """P4-T-IDX (section 5.5): what app/core/tags's chain/clause mechanism did for one finding
    table under a `tag_filter`. `chain_kind` is None when no `tag_filter` was given at all
    (distinct from "none", where one WAS given but this table has no column a tag can reach --
    section 4.5's `applied: false`)."""
    chain_kind: str | None = None
    chain_words: list = field(default_factory=list)
    chain_label: str | None = None
    reason: str | None = None
    select_sql: str | None = None
    select_params: list = field(default_factory=list)

    @property
    def applied(self) -> bool | None:
        return None if self.chain_kind is None else self.chain_kind != "none"


_NO_TAG_APPLICATION = TagApplication()


def workspace_filterable(col_names) -> bool:
    """A check follows the workspace, env and attribute filters when it has workspace_id, or a
    warehouse or cluster id whose own workspace dims.dim_warehouse / dim_cluster know."""
    return bool(set(col_names or []) & {"workspace_id", "warehouse_id", "cluster_id"})


def _in_workspaces(col_names, sub_sql: str) -> str:
    """`sub_sql` selects workspace ids; the clause keeps this check's rows in those workspaces,
    through the warehouse or cluster when the check has no workspace_id of its own."""
    cols = set(col_names or [])
    if "workspace_id" in cols or not cols:
        return f"workspace_id IN ({sub_sql})"
    if "warehouse_id" in cols:
        return f"warehouse_id IN (SELECT warehouse_id FROM dims.dim_warehouse WHERE workspace_id IN ({sub_sql}))"
    return f"cluster_id IN (SELECT cluster_id FROM dims.dim_cluster WHERE workspace_id IN ({sub_sql}))"


def _no_workspace_clause(col_names) -> str | None:
    """A workspace/env/attribute filter (all three apply through _in_workspaces) silently drops a
    row with no resolvable workspace -- account-level billing (workspace_id NULL or '0'), or a
    warehouse/cluster missing from its dim. This is that same row set's own WHERE text, or None
    when the check has none of workspace_id/warehouse_id/cluster_id (C1)."""
    cols = set(col_names or [])
    if "workspace_id" in cols:
        return "(workspace_id IS NULL OR workspace_id = '0')"
    if "warehouse_id" in cols:
        return "(warehouse_id IS NULL OR warehouse_id NOT IN (SELECT warehouse_id FROM dims.dim_warehouse))"
    if "cluster_id" in cols:
        return "(cluster_id IS NULL OR cluster_id NOT IN (SELECT cluster_id FROM dims.dim_cluster))"
    return None


def _build_filters(
    window_days: int,
    workspace_ids: list[str] | None,
    env: list[str] | str | None,
    tag: dict[str, str] | None,
    has_workspace_id: bool,
    has_tag: bool,
    attributes: dict[str, list[str]] | None = None,
    job_id: str | None = None,
    has_job_id: bool = False,
    statuses: list[str] | None = None,
    has_status: bool = False,
    *,
    tag_filter: "app_core_tags.TagFilter | app_core_tags.TagFilterSet | None" = None,
    table_sql: str | None = None,
    col_names: list[str] | None = None,
    domain: str | None = None,
    query_id: str | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
    tag_prefiltered: bool = False,
) -> tuple[str, list, "TagApplication"]:
    """Returns (where_sql, params, tag_application). The legacy positional `tag`/`has_tag`
    (DEC-60's original row-tags branch, still consumed by app/ui/data.py's Streamlit filter,
    unchanged) stay exactly as they were. The NEW tag filter (P4-T-IDX section 5.3/5.4) is
    keyword-only and additive: give `tag_filter` together with `table_sql` (the FROM clause's own
    table, aliased `t` -- data.py's own read/count/aggregate helpers always alias it that way) and
    `col_names` and `domain` to have it compiled via app.core.tags.tag_chain/tag_set_clause.
    `tag_filter` is a TagFilter (one key:value, normalised here into a one-group TagFilterSet) or a
    TagFilterSet outright (several tag names ANDed, each name's own values ORed). Raises
    TagModelsNotBuiltError when the chain needs tags.tag_entity (chain kind "entity") and that
    table does not exist in `con` -- never returns unfiltered rows under an active filter."""
    clauses = ["window_days = ?"]
    params: list = [window_days]

    if has_workspace_id and workspace_ids:
        placeholders = ", ".join("?" for _ in workspace_ids)
        clauses.append(_in_workspaces(col_names, placeholders))
        params.extend(workspace_ids)

    if has_workspace_id and env:
        envs = [env] if isinstance(env, str) else list(env)
        if envs:
            placeholders = ", ".join("?" for _ in envs)
            clauses.append(_in_workspaces(
                col_names, f"SELECT workspace_id FROM dims.dim_workspace WHERE env IN ({placeholders})"
            ))
            params.extend(envs)

    # T-63 (DEC-60 rule 1): one branch of the exact same shape as the env branch above, joining
    # workspace_id against dims.dim_workspace's new <key> column -- never the has_tag/tag_key/
    # tag_value branch below, which is a different mechanism (row-level custom_tags, DEC-60) left
    # byte-identical and still unreachable from app/api.
    if has_workspace_id and attributes:
        allowed = _attribute_canonical_keys()
        for key, values in attributes.items():
            if key not in allowed or not values:
                continue
            placeholders = ", ".join("?" for _ in values)
            clauses.append(_in_workspaces(
                col_names, f'SELECT workspace_id FROM dims.dim_workspace WHERE "{key}" IN ({placeholders})'
            ))
            params.extend(values)

    if has_tag and tag:
        tag_key = tag.get("tag_key")
        tag_value = tag.get("tag_value")
        if tag_key:
            # Spec 5.5: the legacy Streamlit filter (app/ui/filters.py) matches tag_key the same
            # normalised way the new tag search filter's row-tags branch does (app/core/tags.py's
            # _NORM_TAG_KEY_SQL) -- "Cost Center", "cost-center" and "cost_center" are one key
            # here too, not just in the new mechanism. docs/FILTERS.md already documents this.
            clauses.append("regexp_replace(lower(tag_key), '[ _-]+', '', 'g') = ?")
            params.append(app_core_tags.tag_keys.normalize_tag_key(tag_key))
        if tag_value:
            clauses.append("tag_value = ?")
            params.append(tag_value)

    # T-70: the job focus panel's own filter -- applies only when the model carries a job_id
    # column (the same "unaffected, never silently empty" contract as workspace_id/tag above), so
    # a finding with no job_id in its grain (a warehouse- or cluster-scoped one) ignores it rather
    # than erroring. This is what lets GET /api/finding scope a check to one job server-side,
    # instead of the job focus panel fetching a capped, unfiltered page and matching job_id in the
    # browser -- a job past that page's own row cap used to fall through to a false "not flagged".
    if has_job_id and job_id:
        clauses.append("job_id = ?")
        params.append(job_id)

    # T-68: a status filter so a tile that needs only flagged (or only some-status) rows can ask
    # the store for exactly those, instead of fetching an unfiltered page (capped at ui.max_rows)
    # and filtering by status in the browser -- which silently drops flagged rows sitting past the
    # cap on a large finding. Same "unaffected, never silently empty" contract as job_id/tag above:
    # applies only when the table actually has a status column.
    #
    # Materiality floor: filters against the EFFECTIVE status (app/core/materiality.status_expr),
    # not the raw column -- a below-floor CRITICAL row is not WARN/CRITICAL for this purpose, so it
    # drops out of a `statuses=["WARN","CRITICAL"]` money sum/count the same way it drops out of
    # status_counts (finding_window_counts below). The row's own exported `status` column, in a
    # plain SELECT, is never touched by this.
    if has_status and statuses:
        placeholders = ", ".join("?" for _ in statuses)
        status_col = app_materiality.status_expr(query_id or "", col_names or [])
        clauses.append(f"{status_col} IN ({placeholders})")
        params.extend(statuses)

    tag_application = _NO_TAG_APPLICATION
    if tag_filter is not None and tag_prefiltered:
        # table_sql is already app_tag_spend's re-computation over the matching dollars.
        tag_application = TagApplication(
            chain_kind="spend", chain_words=list(app_tag_spend.CHAIN_WORDS),
            chain_label=app_tag_spend.CHAIN_LABEL,
        )
    elif tag_filter is not None and col_names is not None and domain is not None:
        chain = app_core_tags.tag_chain(query_id or "", col_names, domain)
        if chain.kind == "entity" and con is not None:
            exists = con.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_catalog = current_database() AND table_schema = 'tags' AND table_name = 'tag_entity'"
            ).fetchone()[0]
            if not exists:
                raise app_core_tags.TagModelsNotBuiltError(
                    "tags.tag_entity does not exist -- rebuild to filter by tag"
                )
        # A bare TagFilter normalises into a one-group TagFilterSet so both compile the same way.
        filter_set = (
            tag_filter if isinstance(tag_filter, app_core_tags.TagFilterSet)
            else app_core_tags.TagFilterSet.single(tag_filter)
        )
        clause = app_core_tags.tag_set_clause(chain, table_sql or "", filter_set)
        if clause is None and chain.kind == "row_tags":
            # Section 5.4/7.5: tag_set_clause returns None here either for `__untagged__` against a
            # row-tags table (cost_chargeback_by_tag today) -- it lists tagged billing rows only,
            # so "untagged" is not a row it could ever show -- or for more than one tag name, since
            # one row carries exactly one (tag_key, tag_value) pair and can never match two names at
            # once. Either way: leave the finding unfiltered and mark it not-applicable (DEC-60 rule
            # 6), the same as a table the chain cannot reach at all.
            if len(filter_set.groups) > 1:
                reason = "this check lists one tag per row, so several tag names cannot be combined here"
            else:
                reason = 'this check lists tagged billing rows only, so "untagged" cannot be shown here'
            tag_application = TagApplication(chain_kind="none", reason=reason)
        else:
            tag_application = TagApplication(
                chain_kind=chain.kind,
                chain_words=chain.chain_words,
                chain_label=chain.chain_label,
                reason=chain.reason,
            )
            if clause is not None:
                clauses.append(f"({clause.where_sql})")
                params.extend(clause.params)
                tag_application.select_sql = clause.select_sql
                tag_application.select_params = clause.select_params

    return " AND ".join(clauses), params, tag_application


@functools.lru_cache(maxsize=1)
def _committed_order_bys() -> dict[str, str]:
    """meta.order_by per finding model, from the committed dbt model yml the manifest is built from."""
    out: dict[str, str] = {}
    for path in sorted((ROOT / "dbt" / "models" / "findings").glob("*/_findings__*.yml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        for model in doc.get("models") or []:
            name = str((model or {}).get("name") or "")
            order_by = ((model or {}).get("meta") or {}).get("order_by")
            if name.startswith("f_") and isinstance(order_by, str) and order_by.strip():
                out[name[2:]] = order_by
    return out


@_file_cached(lambda: _dbt_manifest_path())
def _model_order_by(query_id: str) -> str | None:
    """The generator-extracted ORDER BY for this model (PLAN.md 5.3), read from dbt's own
    target/manifest.json config.meta.order_by. A missing/unparsable manifest (a checkout that
    never ran dbt, or another process mid-write) falls back to the committed model yml; None only
    when neither has one, never an exception -- the caller then falls back to status-rank ordering."""
    path = _dbt_manifest_path()
    if not path.exists():
        return _committed_order_bys().get(query_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _committed_order_bys().get(query_id)
    node = data.get("nodes", {}).get(f"model.{_DBT_PROJECT_NAME}.f_{query_id}")
    if not node:
        return _committed_order_bys().get(query_id)
    meta = (node.get("config") or {}).get("meta") or {}
    order_by = meta.get("order_by")
    return order_by if isinstance(order_by, str) and order_by.strip() else None


def _split_order_by_terms(order_by: str) -> list[str]:
    """Split on top-level commas only (depth-tracking over parens and '...' string literals, so
    a comma inside a function call or a quoted status literal never splits a term)."""
    terms: list[str] = []
    depth = 0
    in_quote = False
    current: list[str] = []
    for ch in order_by:
        if in_quote:
            current.append(ch)
            if ch == "'":
                in_quote = False
            continue
        if ch == "'":
            in_quote = True
            current.append(ch)
            continue
        if ch == "(":
            depth += 1
            current.append(ch)
            continue
        if ch == ")":
            depth -= 1
            current.append(ch)
            continue
        if ch == "," and depth == 0:
            terms.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    if current:
        terms.append("".join(current).strip())
    return [t for t in terms if t]


_CASE_STATUS_PREFIX_RE = re.compile(r"^CASE\s+status\s+", re.IGNORECASE)


def _rewrite_order_by(
    order_by: str, col_lookup: dict[str, str], query_id: str, col_names: list[str]
) -> str | None:
    """Validate every comma-separated term of `order_by` and rewrite it into safe SQL text, or
    return None if any term is neither a recognised column reference nor the generator's
    status-priority CASE expression (in which case the WHOLE order_by is discarded -- never a
    partial ORDER BY spliced from an unvalidated remainder). Never bindable (ORDER BY takes no
    parameter placeholder for a column list), so every piece is checked against the table's own
    columns (case-insensitively, matching DuckDB's own unquoted-identifier resolution) or against
    a closed status/int-literal vocabulary before being spliced in -- a corrupted manifest can
    never inject arbitrary SQL here.

    Materiality floor: the status-priority CASE's own switch value is rewritten from the raw
    `status` column to app_materiality.status_expr's EFFECTIVE one, so a query with a bespoke
    priority mapping ranks a below-floor row the same way a plain status band already does."""
    terms = _split_order_by_terms(order_by)
    if not terms:
        return None
    rewritten: list[str] = []
    for term in terms:
        if _CASE_STATUS_RE.match(term):
            if "status" not in col_lookup:
                return None
            rest = term[_CASE_STATUS_PREFIX_RE.match(term).end():]
            rewritten.append(f"CASE ({app_materiality.status_expr(query_id, col_names)}) {rest}")
            continue
        m = _ORDER_TERM_RE.match(term)
        if not m:
            return None  # a genuine expression term (e.g. "(a + b) DESC") -- fall back entirely
        real_col = col_lookup.get(m.group("ident").lower())
        if real_col is None:
            return None
        piece = f'"{real_col}"'
        if m.group("dir"):
            piece += f" {m.group('dir').upper()}"
        if m.group("nulls"):
            piece += f" NULLS {m.group('nulls').upper()}"
        rewritten.append(piece)
    return ", ".join(rewritten)


def _status_rank_sql(status_sql: str) -> str:
    return (
        f"CASE ({status_sql}) WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 "
        "WHEN 'NOT_ASSESSED' THEN 2 WHEN 'OK' THEN 3 ELSE 4 END"
    )


def _order_by_clause(query_id: str, col_names: list[str]) -> str:
    """The ORDER BY the store emits, worst-first whenever the table has a status band -- on the
    EFFECTIVE (floored) status, so a row a materiality floor suppresses to OK also sorts as OK,
    never surviving the row cap below on rank alone.

    A finding table is read with `LIMIT ui.max_rows`, so the ordering decides which rows survive
    the cap, not just how they are displayed: under the body's own ORDER BY (85+ of the 107 models then built
    do not lead with the status band) a CRITICAL row past row 5000 would be dropped from a panel
    that exists to surface it. The status rank is therefore PREPENDED to the model's own order_by
    -- which keeps PLAN 6.2's "SQL order kept within a band" exactly, because the body's terms
    still order the rows inside each status group."""
    col_lookup = {c.lower(): c for c in col_names}
    has_status = "status" in col_lookup
    # A parallel sort returns equal keys in any order; every column after them keeps the row cap
    # and any "first" or "worst" pick the same on every read.
    tie = ", ".join('"' + c.replace('"', '""') + '"' for c in col_names)
    order_by = _model_order_by(query_id)
    if order_by:
        rewritten = _rewrite_order_by(order_by, col_lookup, query_id, col_names)
        if rewritten:
            if has_status and not rewritten.lstrip().upper().startswith("CASE ("):
                rank = _status_rank_sql(app_materiality.status_expr(query_id, col_names))
                return f" ORDER BY {rank}, {rewritten}, {tie}"
            return f" ORDER BY {rewritten}, {tie}"
    if has_status:
        return f" ORDER BY {_status_rank_sql(app_materiality.status_expr(query_id, col_names))}, {tie}"
    # Inventory models whose body carries no ORDER BY and no status band (the ported overview_*
    # queries): without a deterministic order the `LIMIT ui.max_rows` slice would be arbitrary and
    # could differ between two reads of the same table. DuckDB's `ORDER BY ALL` orders by every
    # selected column left to right, which is stable and costs nothing on a finding-sized table.
    return " ORDER BY ALL"


def read_finding(
    query_id: str,
    window_days: int,
    workspace_ids: list[str] | None = None,
    env: list[str] | str | None = None,
    tag: dict[str, str] | None = None,
    limit: int | None = None,
    attributes: dict[str, list[str]] | None = None,
    job_id: str | None = None,
    statuses: list[str] | None = None,
    *,
    tag_filter: "app_core_tags.TagFilter | app_core_tags.TagFilterSet | None" = None,
) -> FindingResult:
    """SELECT ... FROM findings."f_<query_id>" WHERE window_days = ? [AND workspace_id IN (...)]
    [AND workspace_id IN (SELECT ... WHERE env IN (...))]
    [AND workspace_id IN (SELECT ... WHERE "<key>" IN (...)) ...] [AND tag_key = ? [AND tag_value
    = ?]] [AND job_id = ?] [AND status IN (...)] ORDER BY <model order_by,
    falling back to status rank> LIMIT <ui.max_rows or `limit`>.
    workspace_ids/env/attributes apply only when the table has a workspace_id column (DEC-60
    rule 6: a finding with no workspace_id is unaffected, never silently empty); tag applies only
    when tag_key/tag_value columns exist; job_id (T-70, the job focus panel) applies only when a
    job_id column exists; statuses (T-68) applies only when a status column exists -- same contract, and the
    returned `applicability` flags say which.
    `attributes` is {canonical_key: [value, ...]} (T-63/DEC-60 rule 1) -- keys not present in
    config/tag_aliases.yml are silently ignored by _build_filters, never trusted as a SQL
    identifier. rows_total is a separate `count(*)` over the SAME filters, without the LIMIT
    (DEC-47 point 2). Materiality floor (see _select_list): a table a floor applies to returns the
    EFFECTIVE status as "status" (raw kept as "status_raw") and a real "below_floor" column, so a
    caller reading `status` off these rows -- for counting, sorting, or a WARN/CRITICAL sum -- gets
    the same answer status_counts/an aggregate already give; every other table still returns a
    plain "status" plus a flat below_floor=False."""
    spec = _validate_query_id(query_id)
    con = _connect()
    try:
        cols = _table_columns(con, query_id)
        col_names = [c[0] for c in cols]
        has_workspace_id = workspace_filterable(col_names)
        has_tag = {"tag_key", "tag_value"}.issubset(col_names)
        has_job_id = "job_id" in col_names
        has_status = "status" in col_names
        spend_table = app_tag_spend.table_sql(con, query_id, window_days, tag_filter)
        table = spend_table or f'findings."f_{query_id}" t'
        where_sql, params, tag_app = _build_filters(
            window_days, workspace_ids, env, tag, has_workspace_id, has_tag, attributes,
            job_id, has_job_id, statuses, has_status,
            tag_filter=tag_filter, table_sql=table, col_names=col_names, domain=spec.domain,
            query_id=query_id, con=con, tag_prefiltered=spend_table is not None,
        )

        rows_total = con.execute(f"SELECT count(*) FROM {table} WHERE {where_sql}", params).fetchone()[0]

        # Materiality floor: how many of rows_total have their floor column under `min` -- one
        # extra, cheap count(*), only when this query has a configured floor whose column the
        # table actually carries (app_materiality.below_floor_expr returns None otherwise, and no
        # query runs at all).
        below_expr = app_materiality.below_floor_expr(query_id, col_names)
        rows_below_floor = 0
        if below_expr is not None:
            rows_below_floor = int(
                con.execute(
                    f"SELECT count(*) FROM {table} WHERE {where_sql} AND {below_expr}", params
                ).fetchone()[0]
            )

        excluded_no_workspace = None
        if has_workspace_id and (workspace_ids or env or attributes):
            no_ws_sql = _no_workspace_clause(col_names)
            if no_ws_sql is not None:
                no_ws_where, no_ws_params, _ = _build_filters(
                    window_days, None, None, tag, has_workspace_id, has_tag, None,
                    job_id, has_job_id, statuses, has_status,
                    tag_filter=tag_filter, table_sql=table, col_names=col_names, domain=spec.domain,
                    query_id=query_id, con=con, tag_prefiltered=spend_table is not None,
                )
                excl_rows = int(
                    con.execute(
                        f"SELECT count(*) FROM {table} WHERE ({no_ws_where}) AND {no_ws_sql}",
                        no_ws_params,
                    ).fetchone()[0]
                )
                excluded_no_workspace = {"rows": excl_rows}

        eff_limit = limit if limit is not None else app_config.load_settings()["ui"]["max_rows"]
        select_list = _select_list(cols, query_id)
        if tag_app.select_sql:
            select_list = f"{select_list}, {tag_app.select_sql}"
        order_sql = _order_by_clause(query_id, col_names)
        sql = f"SELECT {select_list} FROM {table} WHERE {where_sql}{order_sql} LIMIT ?"
        df = con.execute(sql, [*tag_app.select_params, *params, eff_limit]).df()
        tag_unreached = _tag_unreached(con, app_core_tags.tag_chain(query_id, col_names, spec.domain), tag_filter)
    finally:
        con.close()
    return FindingResult(
        df=df,
        rows_total=int(rows_total),
        rows_below_floor=rows_below_floor,
        excluded_no_workspace=excluded_no_workspace,
        applicability={
            "has_workspace_id": has_workspace_id, "has_tag": has_tag, "has_job_id": has_job_id,
            "tag_applied": tag_app.applied, "tag_chain": tag_app.chain_words,
            "tag_chain_label": tag_app.chain_label, "tag_not_applicable_reason": tag_app.reason,
            "tag_unreached": tag_unreached,
        },
    )


# What tags.tag_entity's entity types are, in words.
_ENTITY_WORDS = {
    "statement": "queries", "job": "jobs", "billed_job": "job bills", "pipeline": "pipelines",
    "billed_pipeline": "pipeline bills", "warehouse": "warehouses", "billed_warehouse": "warehouse bills",
    "cluster": "clusters", "billed_cluster": "cluster bills", "pool": "instance pools", "billed_pool": "pool bills",
    "billed_endpoint": "serving endpoints", "billed_endpoint_name": "serving endpoints", "workspace": "workspaces",
    "uc_table": "tables", "uc_schema": "schemas", "uc_column": "columns", "uc_volume": "volumes",
}


@_file_cached(lambda: _db_path())
def _tag_key_entity_types(con: duckdb.DuckDBPyConnection, key: str) -> list[str]:
    return sorted(r[0] for r in con.execute(
        "SELECT DISTINCT entity_type FROM tags.tag_entity WHERE tag_key = ? AND is_allocating", [key]).fetchall())


def _tag_unreached(con: duckdb.DuckDBPyConnection, chain, tag_filter) -> str | None:
    """A line for a tag name that is only on objects this check's chain never looks at, so the
    filter can only empty it; None when every name can reach it."""
    if tag_filter is None or chain.kind != "entity":
        return None
    if isinstance(tag_filter, app_core_tags.TagFilter):
        tag_filter = app_core_tags.TagFilterSet.single(tag_filter)
    reach = {t.entity_type for t in chain.terms}
    lines = []
    try:
        for g in tag_filter.groups:
            on = set(_tag_key_entity_types(con, g.key))
            if on and not on & reach:
                words = sorted({_ENTITY_WORDS.get(e, e) for e in on})
                lines.append(f"{g.key} is only on {', '.join(words)}")
    except duckdb.Error:
        return None
    if not lines:
        return None
    return f"{'; '.join(lines)}. This check matches on the {chain.chain_label}, so it can't split by it."


def count_finding(
    query_id: str,
    window_days: int,
    workspace_ids: list[str] | None = None,
    env: list[str] | str | None = None,
    tag: dict[str, str] | None = None,
    attributes: dict[str, list[str]] | None = None,
    statuses: list[str] | None = None,
    *,
    tag_filter: "app_core_tags.TagFilter | app_core_tags.TagFilterSet | None" = None,
) -> int:
    """Row count only, same filter contract as read_finding (DEC-47 point 2's separate
    `count(*)`), including the `attributes` param (T-63/DEC-60 rule 1), `statuses` (T-68) and the
    new `tag_filter` (P4-T-IDX)."""
    result = count_finding_ex(
        query_id, window_days, workspace_ids, env, tag, attributes, statuses, tag_filter=tag_filter
    )
    return result[0]


def count_finding_ex(
    query_id: str,
    window_days: int,
    workspace_ids: list[str] | None = None,
    env: list[str] | str | None = None,
    tag: dict[str, str] | None = None,
    attributes: dict[str, list[str]] | None = None,
    statuses: list[str] | None = None,
    *,
    tag_filter: "app_core_tags.TagFilter | app_core_tags.TagFilterSet | None" = None,
) -> tuple[int, "TagApplication"]:
    """count_finding's own twin that also returns the TagApplication (chain/applied/reason) --
    used by app/api/service.py to fill scope.tag without a second read. count_finding() above is
    the plain, backward-compatible int-only shape every existing caller keeps using."""
    spec = _validate_query_id(query_id)
    con = _connect()
    try:
        cols = _table_columns(con, query_id)
        col_names = [c[0] for c in cols]
        has_workspace_id = workspace_filterable(col_names)
        has_tag = {"tag_key", "tag_value"}.issubset(col_names)
        has_status = "status" in col_names
        spend_table = app_tag_spend.table_sql(con, query_id, window_days, tag_filter)
        table = spend_table or f'findings."f_{query_id}" t'
        where_sql, params, tag_app = _build_filters(
            window_days, workspace_ids, env, tag, has_workspace_id, has_tag, attributes,
            statuses=statuses, has_status=has_status,
            tag_filter=tag_filter, table_sql=table, col_names=col_names, domain=spec.domain,
            query_id=query_id, con=con, tag_prefiltered=spend_table is not None,
        )
        n = int(con.execute(f"SELECT count(*) FROM {table} WHERE {where_sql}", params).fetchone()[0])
        return n, tag_app
    finally:
        con.close()


def finding_columns(query_id: str) -> list[str]:
    """T-71: the column names of findings."f_<query_id>" -- raises FindingNotBuiltError when
    the table is absent, the same contract as read_finding / count_finding."""
    _validate_query_id(query_id)
    con = _connect()
    try:
        return [name for name, _ in _table_columns(con, query_id)]
    finally:
        con.close()


def _dim_scope_count(
    con: duckdb.DuckDBPyConnection, dim_table: str, id_col: str,
    workspace_ids: list[str] | None, env: list[str] | str | None,
    attributes: dict[str, list[str]] | None,
) -> int:
    """count(DISTINCT id_col) FROM dims.<dim_table>, scoped by workspace_ids/env/attributes the
    same way _build_filters' own branches do -- for affected.total (contract B: "every object of
    that kind in the filtered scope"), against a dim table (no window_days of its own, so
    _build_filters itself does not apply -- same reasoning as region_split's own hand-rolled
    clauses)."""
    clauses: list[str] = []
    params: list = []
    if workspace_ids:
        placeholders = ", ".join("?" for _ in workspace_ids)
        clauses.append(f"workspace_id IN ({placeholders})")
        params.extend(str(w) for w in workspace_ids)
    envs = [env] if isinstance(env, str) else list(env or [])
    if envs:
        placeholders = ", ".join("?" for _ in envs)
        clauses.append(f"workspace_id IN (SELECT workspace_id FROM dims.dim_workspace WHERE env IN ({placeholders}))")
        params.extend(envs)
    if attributes:
        allowed = _attribute_canonical_keys()
        for key, values in attributes.items():
            if key not in allowed or not values:
                continue
            placeholders = ", ".join("?" for _ in values)
            clauses.append(
                f'workspace_id IN (SELECT workspace_id FROM dims.dim_workspace WHERE "{key}" IN ({placeholders}))'
            )
            params.extend(values)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        row = con.execute(f"SELECT count(DISTINCT {_entity_key_sql(id_col, True)}) FROM dims.{dim_table}{where}", params).fetchone()
    except duckdb.Error:
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def _entity_key_sql(id_col: str, has_workspace_id: bool) -> str:
    """One entity per workspace + id: job ids repeat across workspaces."""
    if not has_workspace_id:
        return f"""COALESCE(CAST("{id_col}" AS VARCHAR), '(none)')"""
    return f"""COALESCE(CAST(workspace_id AS VARCHAR), '') || ':' || COALESCE(CAST("{id_col}" AS VARCHAR), '(none)')"""


def _money_sum_sql(table: str, col: str, where: str, parent_key: str | None) -> str:
    """SUM of a check's $ column; once per parent when every row repeats its parent's spend."""
    if parent_key is None:
        return f'SELECT SUM("{col}") FROM {table} WHERE {where}'
    return f'SELECT SUM(m) FROM (SELECT {parent_key} AS k, MAX("{col}") AS m FROM {table} WHERE {where} GROUP BY 1)'


def finding_window_counts(
    specs: list[tuple[str, int]],
    workspace_ids: list[str] | None = None,
    env: list[str] | str | None = None,
    attributes: dict[str, list[str]] | None = None,
    *,
    tag_filter: "app_core_tags.TagFilter | app_core_tags.TagFilterSet | None" = None,
) -> dict[str, dict]:
    """Bulk twin of count_finding, for a LIST of (query_id, window_days) pairs, opening exactly
    ONE read-only connection for the whole batch instead of one per pair.

    `attributes` (T-63/DEC-60, wired in for P2-FILTERS) is the same {canonical_key: [value, ...]}
    contract count_finding/read_finding already honour -- applies only on a table that carries
    workspace_id, via the same _build_filters whitelist-before-splice discipline.

    Added for app/api (T-57): the findings-list endpoint reads every executable query's counts on
    every page load (one table per finding); DEC-47's one-connection-per-call model is the right shape for a
    single finding read (short-lived readers so tools/dbt_run.py's atomic file swap is never
    blocked), but paid per-call connection-open overhead once per table -- plus a separate
    information_schema round-trip per table for its column list -- made that endpoint take over
    90 seconds wall-clock on this checkout. Both are batched here: one connection and one
    information_schema query for every table's columns, still read-only, still opened fresh on
    every request (never held across requests), still built on this module's own _connect/
    _build_filters/_validate_query_id helpers -- just once each instead of ~100+ times.

    `tag_filter` (P4-T-IDX section 5.5) applies the same way to every table in the batch, via the
    same app.core.tags chain/clause mechanism read_finding/count_finding_ex already use --
    tags.tag_entity's existence is checked ONCE for the whole batch (not once per query_id, the
    same one-round-trip discipline as the column-list lookup above), rather than through
    _build_filters' own per-call check -- this function sets `effective_tag_filter` to None ahead
    of any query_id whose chain needs a missing tags.tag_entity, so `_build_filters`' own check
    never has a live tag_filter to raise on for that query_id even though `con` IS passed. A
    query_id whose own
    chain needs tags.tag_entity when that table does not exist gets no tag clause at all (it would
    reference a table that is not there) and instead comes back with `"tag_models_not_built":
    True` -- the caller's job to report NOT_ASSESSED with the tag_models_not_built reason
    (service.list_findings' contract, mirroring load_outcome's own TagModelsNotBuiltError
    handling) rather than silently returning unfiltered counts under an
    active filter.

    Same filter contract as count_finding (workspace_ids/env apply only when the table has the
    matching column). Returns {query_id: {"rows_in_window": int (window alone, no other filter),
    "rows_total": int (window + workspace_ids/env + tag_filter), "status_counts": dict[str,int] |
    None (a verified SQL GROUP BY over the SAME rows_total rows -- None when the table has no
    "status" column, or when rows_total is 0), "has_workspace_id": bool (T-71), "tag_applied":
    bool | None (None when no tag_filter was given), "tag_chain": list[str], "tag_chain_label":
    str | None, "tag_models_not_built": bool}}. A query_id absent from
    the result means
    findings.f_<query_id> does not exist in the current db -- the caller's own job to report
    NOT_ASSESSED, the same FindingNotBuiltError contract every other read path in this module
    honours, just without raising per pair here (one bad id must not abort the whole batch)."""
    out: dict[str, dict] = {}
    con = _connect()
    try:
        specs_by_id: dict[str, registry.QuerySpec] = {}
        for query_id, _ in specs:
            specs_by_id[query_id] = _validate_query_id(query_id)

        # One information_schema query for every table's column list, instead of one per table
        # (_table_columns' own per-call shape) -- this loop's real cost, once the connection
        # itself is shared, turned out to be one catalog lookup per table rather than the counts
        # themselves (T-57's own hand-off note).
        table_names = [f"f_{query_id}" for query_id, _ in specs]
        columns_by_table: dict[str, list[str]] = {}
        if table_names:
            placeholders = ", ".join("?" for _ in table_names)
            for table_name, column_name in con.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                f"WHERE table_catalog = current_database() AND table_schema = 'findings' AND table_name IN ({placeholders}) "
                "ORDER BY table_name, ordinal_position",
                table_names,
            ).fetchall():
                columns_by_table.setdefault(table_name, []).append(column_name)

        # P4-T-IDX: tags.tag_entity's existence, checked ONCE for the whole batch -- every
        # "entity"-kind chain in this batch needs the same one table, so one information_schema
        # round trip (like the column-list one above) answers it for all of them, instead of
        # _build_filters' own per-call check (bypassed below by never passing it `con`).
        tag_entity_exists = True
        if tag_filter is not None:
            tag_entity_exists = bool(
                con.execute(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_catalog = current_database() AND table_schema = 'tags' AND table_name = 'tag_entity'"
                ).fetchone()[0]
            )

        # money/affected (contract B): one discount read for the whole batch, not once per id.
        try:
            discount_pct = float(app_config.load_settings().get("discount_pct", 0.0))
        except Exception:  # pragma: no cover -- defensive, matches app/core/rollup.py's own guard
            discount_pct = 0.0

        for query_id, window_days in specs:
            col_names = columns_by_table.get(f"f_{query_id}")
            if not col_names:
                continue  # findings.f_<query_id> does not exist -- FindingNotBuiltError's contract
            has_status = "status" in col_names
            has_workspace_id = workspace_filterable(col_names)
            has_tag = {"tag_key", "tag_value"}.issubset(col_names)
            table = f'findings."f_{query_id}" t'

            rows_in_window = int(
                con.execute(f"SELECT count(*) FROM {table} WHERE window_days = ?", [window_days]).fetchone()[0]
            )

            # A chain that needs tags.tag_entity when this batch already knows it is missing gets
            # no tag clause at all (it would otherwise reference a table that does not exist) --
            # this query_id comes back `tag_models_not_built` instead, same "never silently
            # unfiltered" rule as read_finding/count_finding_ex's own TagModelsNotBuiltError.
            effective_tag_filter = tag_filter
            tag_models_not_built = False
            if tag_filter is not None:
                domain = specs_by_id[query_id].domain
                probe_chain = app_core_tags.tag_chain(query_id, col_names, domain)
                if probe_chain.kind == "entity" and not tag_entity_exists:
                    tag_models_not_built = True
                    effective_tag_filter = None

            spend_table = app_tag_spend.table_sql(con, query_id, window_days, effective_tag_filter)
            filtered_table = spend_table or table
            where_sql, params, tag_app = _build_filters(
                window_days, workspace_ids, env, None, has_workspace_id, has_tag, attributes,
                tag_filter=effective_tag_filter, table_sql=filtered_table, col_names=col_names,
                domain=specs_by_id[query_id].domain, query_id=query_id, con=con,
                tag_prefiltered=spend_table is not None,
            )
            rows_total = int(con.execute(f"SELECT count(*) FROM {filtered_table} WHERE {where_sql}", params).fetchone()[0])

            status_counts = None
            money = None
            affected = None
            if has_status and rows_total:
                # Materiality floor: grouped on the EFFECTIVE status, so a below-floor CRITICAL/
                # WARN row counts as OK here -- the same status_counts the findings table's own
                # "worst status" pill is derived from (app/api/service.list_findings).
                status_col = app_materiality.status_expr(query_id, col_names)
                status_rows = con.execute(
                    f'SELECT {status_col} AS status, count(*) FROM {filtered_table} WHERE {where_sql} '
                    f'GROUP BY {status_col}',
                    params,
                ).fetchall()
                status_counts = {status: int(n) for status, n in status_rows}

                flagged_sql = f"({where_sql}) AND ({status_col}) IN ('CRITICAL', 'WARN')"
                money_col = app_finding_columns.money_column(col_names)
                if money_col is not None:
                    col, kind = money_col
                    # A spend ranking flags nothing; its $ is the spend of every row it lists.
                    ranked = kind == "spend" and not any(s in ("CRITICAL", "WARN") for s in status_counts)
                    parent = app_finding_columns.money_parent(col_names)
                    parent_key = _entity_key_sql(parent, "workspace_id" in col_names) if parent else None
                    row = con.execute(
                        _money_sum_sql(filtered_table, col, where_sql if ranked else flagged_sql, parent_key), params
                    ).fetchone()
                    raw = float(row[0]) if row and row[0] is not None else 0.0
                    money = {"column": col, "kind": kind, "usd": round(raw * (1 - discount_pct), 2)}

                entity_col = app_finding_columns.entity_column(col_names)
                if entity_col is not None:
                    col, noun, dim_table = entity_col
                    key_sql = _entity_key_sql(col, "workspace_id" in col_names)
                    flagged_row = con.execute(
                        f"SELECT count(DISTINCT {key_sql}) FROM {filtered_table} WHERE {flagged_sql}", params
                    ).fetchone()
                    flagged = int(flagged_row[0]) if flagged_row and flagged_row[0] is not None else 0
                    own_row = con.execute(
                        f"SELECT count(DISTINCT {key_sql}) FROM {filtered_table} WHERE {where_sql}", params
                    ).fetchone()
                    own_total = int(own_row[0]) if own_row and own_row[0] is not None else 0
                    # A check that lists its OK rows too has assessed exactly those objects, so
                    # they are the base; one that lists only flagged rows is measured against
                    # every object of that kind in scope (the dim, never fewer than it saw).
                    lists_unflagged = any(s not in ("CRITICAL", "WARN") for s in status_counts)
                    if lists_unflagged or dim_table is None:
                        total = own_total
                    else:
                        dim_total = _dim_scope_count(con, dim_table, col, workspace_ids, env, attributes)
                        total = max(dim_total, own_total)
                    affected = {"column": col, "noun": noun, "flagged": flagged, "total": total}
                else:
                    # No job/pipeline/warehouse/cluster id on this table -- the check's own row is
                    # the thing being counted (a grant, a classified column, an event), so fall
                    # back to its own flagged/total row counts instead of leaving affected null.
                    flagged_row = con.execute(f'SELECT count(*) FROM {filtered_table} WHERE {flagged_sql}', params).fetchone()
                    flagged = int(flagged_row[0]) if flagged_row and flagged_row[0] is not None else 0
                    affected = {"column": None, "noun": app_finding_columns.row_noun(col_names),
                                "flagged": flagged, "total": rows_total}

            out[query_id] = {
                "rows_in_window": rows_in_window,
                "rows_total": rows_total,
                "status_counts": status_counts,
                "has_workspace_id": has_workspace_id,
                "tag_applied": tag_app.applied if tag_filter is not None else None,
                "tag_chain": tag_app.chain_words,
                "tag_chain_label": tag_app.chain_label,
                "tag_models_not_built": tag_models_not_built,
                "money": money,
                "affected": affected,
            }
    finally:
        con.close()
    return out


def finding_columns_batch(query_ids: list[str]) -> dict[str, list[str]]:
    """The batch twin of finding_columns(): one connection for every id in `query_ids`. A query_id
    whose findings.f_<id> was never built is simply absent, not a FindingNotBuiltError."""
    out: dict[str, list[str]] = {}
    if not query_ids:
        return out
    con = _connect()
    try:
        for qid in query_ids:
            _validate_query_id(qid)
        table_names = [f"f_{qid}" for qid in query_ids]
        placeholders = ", ".join("?" for _ in table_names)
        for table_name, column_name in con.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            f"WHERE table_catalog = current_database() AND table_schema = 'findings' AND table_name IN ({placeholders}) "
            "ORDER BY table_name, ordinal_position",
            table_names,
        ).fetchall():
            out.setdefault(table_name[2:], []).append(column_name)
    finally:
        con.close()
    return out


# ---------------------------------------------------------------------------------------------
# aggregate_finding -- T-68: "cost figures come from full totals, not a 5,000-row slice". A tile
# or chart that needs a SUM/COUNT over a finding's rows must never derive it by fetching
# ui.max_rows (default 5000) rows and reducing them in the browser -- on a query whose grain is
# fine (day x workspace x product x tag, e.g. cost_chargeback_by_tag), that page can be a single
# day for the first few workspaces alphabetically while the chart is presented as covering the
# whole window. GROUP BY in SQL has no such cap: it scans every row matching the filters before
# ever truncating, so the numbers this function returns are exact, whatever the underlying finding
# table's row count.
# ---------------------------------------------------------------------------------------------

_AGG_FUNCS = ("sum", "count", "count_distinct", "max")
_MAX_GROUP_COLS = 3


@dataclass
class AggregateGroup:
    key: tuple | None   # one value per `group` column, in table order; None for the "other" bucket
    value: object        # the aggregate result (sum/count), raw (undiscounted for a money column)
    row_count: int        # how many underlying detail rows fed this one group
    # T-69B review round 1: for agg="sum" only, how many of this group's row_count rows had a NULL
    # `value` column -- 0 for agg="count"/"count_distinct" (row_count already answers "how many",
    # there is no separate NULL-vs-zero question for a count). SUM ignores NULLs (they never count
    # as a $0), so a group whose 5 rows are 4 real dollar amounts and 1 unpriced NULL reports the
    # same `value` a group of 4 fully-priced rows would -- a reader summing tile totals cannot tell
    # "this total is short some unpriced rows" from "this total is complete" without this field. See
    # aggregate_finding's own docstring for the SQL.
    null_rows: int = 0


@dataclass
class AggregateResult:
    groups: list[AggregateGroup]
    other: AggregateGroup | None   # the folded remainder past `top`, or None if nothing was cut
    total_value: object             # sum(g.value for g in every group, incl. `other`) -- the TRUE total
    group_count: int                 # how many distinct groups exist BEFORE `top` folds the tail --
                                       # "N SKUs"/"N tag values" etc., not just len(groups) after the fold
    rows_total: int                  # rows matching window + workspace_ids/env/attributes (no status filter)
    matched_rows: int                 # rows matching window + every filter INCLUDING statuses -- what
                                       # the aggregate actually scanned; == sum of every group's row_count
    null_value_rows: int = 0          # sum(g.null_rows for g in every group INCLUDING the folded tail,
                                       # i.e. before `top` folds it) -- "how many of matched_rows never
                                       # entered total_value at all" (agg="sum" only; 0 otherwise)
    tag_application: "TagApplication" = field(default_factory=lambda: _NO_TAG_APPLICATION)
    floor: "app_materiality.Floor | None" = None   # this check's configured floor, or None
    excluded_no_workspace: dict | None = None   # None, or {"rows", "value"} -- contract C


def aggregate_finding(
    query_id: str,
    window_days: int,
    group: list[str],
    agg: str,
    value: str | None = None,
    *,
    workspace_ids: list[str] | None = None,
    env: list[str] | str | None = None,
    attributes: dict[str, list[str]] | None = None,
    statuses: list[str] | None = None,
    top: int | None = None,
    tag_filter: "app_core_tags.TagFilter | app_core_tags.TagFilterSet | None" = None,
    below_floor: bool = False,
) -> AggregateResult:
    """`SELECT <group...>, <agg>("<value>") AS agg_value, count(*) AS row_count FROM
    findings."f_<query_id>" WHERE <read_finding's own filter set, minus the row LIMIT>
    GROUP BY <group> ORDER BY agg_value DESC` -- no LIMIT on the group-by itself, so every group
    the table actually has is scanned and returned (unless `top` folds the tail into `other`,
    which still keeps every row's value in the total -- see AggregateResult.total_value).

    `below_floor` (contract D): when true, scans only the rows this check's materiality floor
    moved from CRITICAL/WARN to OK (app_materiality.below_floor_expr) -- "how much did the floor
    hide". A query with no applicable floor then matches nothing (never an error). Never combine
    with `statuses`: below_floor already implies the row's raw status was CRITICAL or WARN.

    `group` is 0-3 column names (_MAX_GROUP_COLS); each is validated against this table's OWN
    information_schema columns (via _table_columns, exactly the whitelist-before-splice discipline
    every other identifier in this module follows) and rejected if it names a MAP/STRUCT/LIST
    column (_is_complex) -- ValueError, never spliced unchecked. `agg` is one of _AGG_FUNCS:
    "sum"/"count_distinct"/"max" require `value` (also column-whitelisted); "count" ignores `value` and
    counts rows. `statuses` filters to those status values, same unaffected-if-no-status-column
    contract as read_finding's own `statuses` param -- applied to matched_rows/the aggregate
    itself, but NOT to `rows_total` (which mirrors read_finding's rows_total: window + workspace/
    env/attributes only, no status filter -- "how many rows exist here at all" vs "how many did
    this particular aggregate scan").

    Raises ValueError for a bad group/agg/value (the API layer turns that into a 422); anything
    else (a locked file, a genuine type mismatch DuckDB itself rejects) raises duckdb.Error/OSError
    same as every other read in this module. `tag_filter` (P4-T-IDX) applies to both `rows_total`
    and the aggregate itself, same as workspace_ids/env/attributes -- section 5.5's fix for
    "aggregate_finding currently passes None [for tag]"."""
    spec = _validate_query_id(query_id)
    if len(group) > _MAX_GROUP_COLS:
        raise ValueError(f"group accepts at most {_MAX_GROUP_COLS} columns, got {len(group)}")
    if agg not in _AGG_FUNCS:
        raise ValueError(f"agg must be one of {_AGG_FUNCS}, got {agg!r}")
    if agg in ("sum", "count_distinct", "max") and not value:
        raise ValueError(f"agg={agg!r} requires a value column")
    if top is not None and top < 1:
        raise ValueError(f"top must be >= 1, got {top}")

    con = _connect()
    try:
        cols = _table_columns(con, query_id)
        col_lookup = {name: dtype for name, dtype in cols}
        col_names = list(col_lookup)

        for g in group:
            if g not in col_lookup:
                raise ValueError(f"unknown group column {g!r} on {query_id}")
            if _is_complex(col_lookup[g]):
                raise ValueError(f"cannot group on complex column {g!r} on {query_id}")
        if value is not None and value not in col_lookup:
            raise ValueError(f"unknown value column {value!r} on {query_id}")

        has_workspace_id = workspace_filterable(col_names)
        has_tag = {"tag_key", "tag_value"}.issubset(col_names)
        has_status = "status" in col_names
        spend_table = app_tag_spend.table_sql(con, query_id, window_days, tag_filter)
        table = spend_table or f'findings."f_{query_id}" t'
        floor = app_materiality.applicable_floor(query_id, col_names)

        base_where_sql, base_params, _base_tag_app = _build_filters(
            window_days, workspace_ids, env, None, has_workspace_id, has_tag, attributes,
            tag_filter=tag_filter, table_sql=table, col_names=col_names, domain=spec.domain,
            query_id=query_id, con=con, tag_prefiltered=spend_table is not None,
        )
        rows_total = int(con.execute(f"SELECT count(*) FROM {table} WHERE {base_where_sql}", base_params).fetchone()[0])

        where_sql, params, tag_app = _build_filters(
            window_days, workspace_ids, env, None, has_workspace_id, has_tag, attributes,
            statuses=statuses, has_status=has_status,
            tag_filter=tag_filter, table_sql=table, col_names=col_names, domain=spec.domain,
            query_id=query_id, con=con, tag_prefiltered=spend_table is not None,
        )
        if below_floor:
            below_expr = app_materiality.below_floor_expr(query_id, col_names)
            if below_expr is not None:
                where_sql = f"({where_sql}) AND ({below_expr})"
            else:
                where_sql, params = "FALSE", []

        # Materiality floor: a group-by on "status" itself groups on the EFFECTIVE status (a
        # below-floor row's group moves to OK), same as every other status read in this module.
        group_sql = ", ".join(
            app_materiality.status_expr(query_id, col_names) if g == "status" else f'"{g}"'
            for g in group
        )
        if agg == "sum":
            agg_expr = f'sum("{value}")'
        elif agg == "count_distinct":
            agg_expr = f'count(DISTINCT "{value}")'
        elif agg == "max":
            agg_expr = f'max("{value}")'
        else:
            agg_expr = "count(*)"

        excluded_no_workspace = None
        if has_workspace_id and (workspace_ids or env or attributes):
            no_ws_sql = _no_workspace_clause(col_names)
            if no_ws_sql is not None:
                no_ws_where, no_ws_params, _ = _build_filters(
                    window_days, None, None, None, has_workspace_id, has_tag, None,
                    statuses=statuses, has_status=has_status,
                    tag_filter=tag_filter, table_sql=table, col_names=col_names, domain=spec.domain,
                    query_id=query_id, con=con, tag_prefiltered=spend_table is not None,
                )
                if below_floor:
                    excl_below_expr = app_materiality.below_floor_expr(query_id, col_names)
                    if excl_below_expr is not None:
                        no_ws_where = f"({no_ws_where}) AND ({excl_below_expr})"
                    else:
                        no_ws_where, no_ws_params = "FALSE", []
                excl_row = con.execute(
                    f"SELECT {agg_expr} AS agg_value, count(*) FROM {table} "
                    f"WHERE ({no_ws_where}) AND {no_ws_sql}",
                    no_ws_params,
                ).fetchone()
                excluded_no_workspace = {"rows": int(excl_row[1]), "value": excl_row[0]}

        # T-69B review round 1: agg="sum" only, count("<value>") alongside count(*) -- SQL's
        # count(col) skips NULLs the same way sum(col) does, so row_count - value_rows is exactly
        # how many of this group's rows never entered the sum at all (a real, undisclosed gap a
        # bare SUM cannot distinguish from "every row here was priced"). "count"/"count_distinct"
        # already answer "how many" directly, so no extra column for those.
        has_value_rows = agg == "sum"
        select_list = (
            (f"{group_sql}, " if group_sql else "")
            + f"{agg_expr} AS agg_value, count(*) AS row_count"
            + (f', count("{value}") AS value_rows' if has_value_rows else "")
        )
        group_by_sql = f" GROUP BY {group_sql}" if group_sql else ""
        # Groups with equal totals fall back to their key, so the order is the same on every read.
        tie_sql = f", {group_sql}" if group_sql else ""
        sql = f"SELECT {select_list} FROM {table} WHERE {where_sql}{group_by_sql} ORDER BY agg_value DESC{tie_sql}"
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()

    n = len(group)
    all_groups = []
    for r in rows:
        row_count = int(r[n + 1])
        null_rows = (row_count - int(r[n + 2])) if has_value_rows else 0
        all_groups.append(
            AggregateGroup(key=tuple(r[:n]) if n else (), value=r[n], row_count=row_count, null_rows=null_rows)
        )
    # A max is the largest group's value; every other aggregate adds up.
    fold = (lambda vals: max(vals, default=0)) if agg == "max" else sum
    total_value = fold((g.value or 0) for g in all_groups) if all_groups else 0
    matched_rows = sum(g.row_count for g in all_groups)
    null_value_rows = sum(g.null_rows for g in all_groups)

    if top is not None and len(all_groups) > top:
        head = all_groups[:top]
        tail = all_groups[top:]
        other = AggregateGroup(
            key=None,
            value=fold((g.value or 0) for g in tail),
            row_count=sum(g.row_count for g in tail),
            null_rows=sum(g.null_rows for g in tail),
        )
    else:
        head = all_groups
        other = None

    return AggregateResult(
        groups=head, other=other, total_value=total_value, group_count=len(all_groups),
        rows_total=rows_total, matched_rows=matched_rows, null_value_rows=null_value_rows,
        tag_application=tag_app, floor=floor, excluded_no_workspace=excluded_no_workspace,
    )


# ---------------------------------------------------------------------------------------------
# read_dim_workspace
# ---------------------------------------------------------------------------------------------


def read_dim_workspace() -> pd.DataFrame:
    """dims.dim_workspace: workspace_id, name, url, env, env_source, env_reason (PLAN.md 5.5),
    PLUS (T-63/DEC-60) one <key>/<key>_share/<key>_reason column set per canonical key
    config/tag_aliases.yml declared at the LAST dbt build -- `SELECT *` rather than a fixed list,
    since that key set is config-driven and dynamic, not something this module can enumerate
    ahead of a build. Existing callers (app/api/app.py's get_workspaces, app/ui/*) already read
    named columns off the frame, so the extra columns are additive and harmless to them."""
    con = _connect()
    try:
        return con.execute("SELECT * FROM dims.dim_workspace ORDER BY workspace_id").df()
    finally:
        con.close()


def tag_spend_ready() -> bool:
    """True when the spend checks can follow the full tag order (tags.cost_day is built)."""
    con = _connect()
    try:
        return app_tag_spend.available(con)
    finally:
        con.close()


def read_tag_origins(tag_filter, window_days: int, workspace_scope: set[str] | None) -> dict | None:
    """Where the dollars a tag filter keeps got their tag (app/core/tag_spend.origins)."""
    con = _connect()
    try:
        return app_tag_spend.origins(con, tag_filter, window_days, workspace_scope)
    finally:
        con.close()


def read_tag_compliance(window_days: int, workspace_scope: set[str] | None, tag_filter=None) -> dict:
    """Mandatory tags missing per object type and level (app/core/tag_compliance), over the objects
    the tag filter keeps."""
    keys = app_config.load_settings()["mandatory_tag_keys"]
    con = _connect()
    try:
        return app_tag_compliance.compliance(con, keys, window_days, workspace_scope, tag_filter)
    finally:
        con.close()


def read_abac_policies() -> dict:
    """ABAC row-filter and column-mask policies (SHOW POLICIES, governance.abac_policies) and what
    the export asked; outcome not_assessed when this export has none."""
    con = _connect()
    try:
        have = {r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_catalog = current_database() AND table_schema = 'governance'"
        ).fetchall()}
        info = None
        if "abac_policies_info" in have:
            raw = con.execute("SELECT info FROM governance.abac_policies_info LIMIT 1").fetchone()
            info = json.loads(raw[0]) if raw and raw[0] else None
        if "abac_policies" not in have:
            return {"outcome": "not_assessed", "info": info, "rows": []}
        cols = ["policy_name", "policy_type", "catalog", "schema", "table_name", "comment", "on_type", "on_name"]
        held = {r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_catalog = current_database() AND table_schema = 'governance' AND table_name = 'abac_policies'"
        ).fetchall()}
        picked = ", ".join(c if c in held else f"NULL AS {c}" for c in cols)
        rows = [dict(zip(cols, r)) for r in con.execute(
            f"SELECT {picked} FROM governance.abac_policies "
            "ORDER BY policy_type, catalog, schema, table_name, policy_name"
        ).fetchall()]
    finally:
        con.close()
    return {"outcome": "ok_rows" if rows else "ok_empty", "info": info, "rows": rows}


# Small files, as storage_small_files judges them: 1,000+ files averaging under 32 MB; under 8 MB critical.
_SMALL_FILES_SQL = ("CASE WHEN num_files >= 1000 AND size_in_bytes < num_files * 8 * 1048576 THEN 'CRITICAL' "
                    "WHEN num_files >= 1000 AND size_in_bytes < num_files * 32 * 1048576 THEN 'WARN' END")


def read_table_sizes(top: int = 100) -> dict:
    """DESCRIBE DETAIL of every table (storage.table_detail): totals, size per schema, the biggest
    tables and the small-file ones, and what the export did; not_assessed when this export has none."""
    con = _connect()
    try:
        have = {r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_catalog = current_database() AND table_schema = 'storage'"
        ).fetchall()}
        info = None
        if "table_detail_info" in have:
            raw = con.execute("SELECT info FROM storage.table_detail_info LIMIT 1").fetchone()
            info = json.loads(raw[0]) if raw and raw[0] else None
        if "table_detail" not in have:
            vi = con.execute("SELECT info FROM storage.volume_detail_info LIMIT 1").fetchone() \
                if "volume_detail_info" in have else None
            return {"outcome": "not_assessed", "info": info, "volume_info": json.loads(vi[0]) if vi and vi[0] else None}

        def rows(sql: str) -> list[dict]:
            cur = con.execute(sql)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

        totals = rows(
            "SELECT COUNT(*) FILTER (WHERE detail <> 'view') AS tables, COUNT(*) FILTER (WHERE detail = 'view') AS views, "
            "COUNT(size_in_bytes) AS sized, SUM(size_in_bytes) AS size_in_bytes, SUM(num_files) AS num_files, "
            "SUM(column_count) AS columns, COUNT(*) FILTER (WHERE detail = 'not_asked') AS not_asked, "
            "COUNT(*) FILTER (WHERE detail LIKE 'failed%') AS failed, COUNT(*) FILTER (WHERE detail = 'not_delta') AS not_delta, "
            f"COUNT(*) FILTER (WHERE {_SMALL_FILES_SQL} IS NOT NULL) AS small_files "
            "FROM storage.table_detail")[0]
        by_schema = rows(
            "SELECT table_catalog, table_schema, COUNT(*) FILTER (WHERE detail <> 'view') AS tables, "
            "COUNT(*) FILTER (WHERE detail = 'view') AS views, SUM(column_count) AS columns, "
            "SUM(size_in_bytes) AS size_in_bytes, SUM(num_files) AS num_files "
            "FROM storage.table_detail GROUP BY ALL ORDER BY size_in_bytes DESC NULLS LAST, tables DESC LIMIT 500")
        biggest = rows(
            "SELECT table_catalog || '.' || table_schema || '.' || table_name AS full_name, table_type, "
            "size_in_bytes, num_files, column_count, last_modified, "
            "ROUND(size_in_bytes / NULLIF(num_files, 0) / 1048576.0, 1) AS avg_file_mb, "
            f"{_SMALL_FILES_SQL} AS small_files "
            f"FROM storage.table_detail WHERE size_in_bytes IS NOT NULL ORDER BY size_in_bytes DESC LIMIT {int(top)}")
        failed = rows(
            "SELECT table_catalog, table_schema, COUNT(*) AS tables, MIN(substr(detail, 9)) AS error "
            "FROM storage.table_detail WHERE detail LIKE 'failed%' GROUP BY ALL ORDER BY tables DESC, 1, 2 LIMIT 200")
        small = rows(
            "SELECT table_catalog || '.' || table_schema || '.' || table_name AS full_name, size_in_bytes, num_files, "
            "ROUND(size_in_bytes / NULLIF(num_files, 0) / 1048576.0, 1) AS avg_file_mb, "
            f"{_SMALL_FILES_SQL} AS status FROM storage.table_detail WHERE {_SMALL_FILES_SQL} IS NOT NULL "
            f"ORDER BY num_files DESC LIMIT {int(top)}")
    finally:
        con.close()
    vol = vol_info = None
    con = _connect()
    try:
        if con.execute("SELECT 1 FROM information_schema.tables WHERE table_catalog = current_database() "
                       "AND table_schema = 'storage' AND table_name = 'volume_detail_info'").fetchone():
            raw = con.execute("SELECT info FROM storage.volume_detail_info LIMIT 1").fetchone()
            vol_info = json.loads(raw[0]) if raw and raw[0] else None
        if con.execute("SELECT 1 FROM information_schema.tables WHERE table_catalog = current_database() "
                       "AND table_schema = 'storage' AND table_name = 'volume_detail'").fetchone():
            r = con.execute("SELECT COUNT(*), SUM(files), SUM(size_in_bytes), COUNT(*) FILTER (WHERE detail LIKE 'failed%'), "
                            "COUNT(*) FILTER (WHERE truncated) FROM storage.volume_detail").fetchone()
            vol = {"volumes": r[0], "files": int(r[1] or 0), "size_in_bytes": int(r[2] or 0), "failed": r[3], "truncated": r[4]}
    finally:
        con.close()
    return {"outcome": "ok_rows" if totals["tables"] else "ok_empty", "info": info, "totals": totals, "volumes": vol,
            "volume_info": vol_info,
            "by_schema": by_schema, "biggest": biggest, "small_files": small, "not_described": failed}


def _catalog_homes(con, have) -> dict[str, tuple[str, float]]:
    """catalog -> (home workspace, its share of the catalog's lineage events). Pinned in
    config/workspace.csv's catalogs column when listed there; else the workspace that reads and
    writes the catalog most."""
    from app.core import workspace_file

    home: dict[str, tuple[str, float]] = {}
    if ("usage", "catalog_workspace") in have:
        for cat, ws, share in con.execute(
                "SELECT catalog, workspace_id, events / SUM(events) OVER (PARTITION BY catalog) FROM usage.catalog_workspace "
                "QUALIFY ROW_NUMBER() OVER (PARTITION BY catalog ORDER BY events DESC, workspace_id) = 1").fetchall():
            home[str(cat)] = (str(ws), float(share))
    for cat, ws in workspace_file.catalogs().items():
        home[cat] = (ws, 1.0)
    return home


def _columns(con, schema: str, table: str) -> set[str]:
    return {r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_catalog = current_database() "
        "AND table_schema = ? AND table_name = ?", [schema, table]).fetchall()}


def _windowed(con, have, table: str, window: int | None) -> tuple[str | None, int | None]:
    """usage.<table> for one window (the smallest exported one covering `window`; None = the largest),
    as a FROM clause; an export from before windows has one set of rows."""
    if ("usage", table) not in have:
        return None, None
    if "window_days" not in _columns(con, "usage", table):
        return f"usage.{table}", None
    got = [r[0] for r in con.execute(f"SELECT DISTINCT window_days FROM usage.{table} ORDER BY 1").fetchall()]
    if not got:
        return f"usage.{table}", None
    used = max(got) if window is None else min((w for w in got if w >= window), default=max(got))
    return f"(SELECT * FROM usage.{table} WHERE window_days = {int(used)})", used


def _table_access(con, have, window: int | None) -> tuple[str | None, int | None]:
    return _windowed(con, have, "table_access", window)


def _table_classes(con, have, window: int, end) -> dict[str, dict]:
    """Every table (storage.table_detail) and every table lineage saw, with its usage class in the
    window (app/core/usage.py), reads and writes summed over workspaces, readers, writers, size."""
    from app.core import usage

    src, _used = _table_access(con, have, window)
    acc: dict[str, dict] = {}
    if src:
        cols = _columns(con, "usage", "table_access")
        w = "SUM(writers)" if "writers" in cols else "NULL"
        names = "MAX(reader_names), MAX(writer_names)" if "reader_names" in cols else "NULL, NULL"
        for name, lr, lw, r, wr, rd, wt, rn, wn in con.execute(
                f"SELECT table_full_name, MAX(last_read), MAX(last_write), SUM(reads), SUM(writes), SUM(readers), {w}, "
                f"{names} FROM {src} GROUP BY 1").fetchall():
            acc[str(name)] = {"last_read": lr, "last_write": lw, "reads": int(r or 0), "writes": int(wr or 0),
                              "readers": int(rd or 0), "writers": None if wt is None else int(wt),
                              "reader_names": rn, "writer_names": wn}
    out: dict[str, dict] = {}
    if ("storage", "table_detail") in have:
        has = _columns(con, "storage", "table_detail")
        created = "created" if "created" in has else "NULL"
        owner = "owner" if "owner" in has else "NULL"
        for name, kind, size, files, detail, born, who in con.execute(
                f"SELECT table_catalog || '.' || table_schema || '.' || table_name, table_type, size_in_bytes, num_files, "
                f"detail, {created}, {owner} FROM storage.table_detail").fetchall():
            out[str(name)] = {"table_type": kind, "size_in_bytes": size, "num_files": files, "detail": detail,
                              "created": born, "owner": who}
    for name in acc:
        out.setdefault(name, {"table_type": None, "size_in_bytes": None, "num_files": None, "detail": None,
                              "created": None, "owner": None})
    # system, sample and Databricks' own internal catalogs are not the customer's tables
    out = {n: t for n, t in out.items() if not usage.skipped_catalog(n.split(".")[0])}
    blank = {"last_read": None, "last_write": None, "reads": 0, "writes": 0, "readers": 0, "writers": None,
             "reader_names": None, "writer_names": None}
    for name, t in out.items():
        t.update(acc.get(name, blank))
        t["usage_class"], t["new"] = usage.usage_class(t["reads"], t["writes"], t["last_read"], t["last_write"],
                                                       t["created"], end)
        t["view"] = (t.get("table_type") or "").upper() == "VIEW" or t.get("detail") == "view"
    return out


def _sensitive(con, have) -> dict[str, list[dict]]:
    """table -> its sensitive columns (usage.sensitive_columns)."""
    if ("usage", "sensitive_columns") not in have:
        return {}
    out: dict[str, list[dict]] = {}
    for name, col, kind, method, masked in con.execute(
            "SELECT table_full_name, column_name, pii_type, method, masked FROM usage.sensitive_columns "
            "ORDER BY 1, 2").fetchall():
        out.setdefault(str(name), []).append({"column_name": col, "pii_type": kind, "method": method,
                                              "masked": bool(masked)})
    return out


def _tag_kept_workspaces(con, tag_filter) -> set[str] | None:
    """The workspaces whose own tags (tags.tag_workspace) the tag filter keeps; None = no tag filter."""
    keep = app_tag_compliance._matcher(tag_filter)
    if not keep:
        return None
    if not con.execute("SELECT 1 FROM information_schema.tables WHERE table_catalog = current_database() "
                       "AND table_schema = 'tags' AND table_name = 'tag_workspace'").fetchone():
        return set()
    vals: dict[str, dict] = {}
    every: set[str] = set()
    for ws, key, value, ok in con.execute(
            "SELECT workspace_id, tag_key, tag_value, is_allocating FROM tags.tag_workspace").fetchall():
        every.add(str(ws))
        if ok:
            vals.setdefault(str(ws), {})[key] = value
    return {ws for ws in every if keep([vals.get(ws, {})])}


def read_workspaces(window: int, workspace_scope: set[str] | None, tag_filter=None) -> dict:
    """One row per workspace: who is active (usage.workspace_activity) and what it holds (the tables
    of the catalogs whose home workspace it is, storage.table_detail), with a complexity and a usage
    index; not_assessed when this export has no activity."""
    con = _connect()
    try:
        have = {(r[0], r[1]) for r in con.execute(
            "SELECT table_schema, table_name FROM information_schema.tables WHERE table_catalog = current_database()"
        ).fetchall()}
        info = None
        if ("usage", "workspace_activity_info") in have:
            raw = con.execute("SELECT info FROM usage.workspace_activity_info LIMIT 1").fetchone()
            info = json.loads(raw[0]) if raw and raw[0] else None
        if ("usage", "workspace_activity") not in have:
            return {"outcome": "not_assessed", "info": info, "rows": []}
        windows = [r[0] for r in con.execute(
            "SELECT DISTINCT window_days FROM usage.workspace_activity ORDER BY 1").fetchall()]
        used = min((w for w in windows if w >= window), default=max(windows, default=window))
        days_col = "active_days" if "active_days" in _columns(con, "usage", "workspace_activity") else "NULL"
        act = {str(r[0]): r[1:] for r in con.execute(
            "SELECT workspace_id, active_people, active_service_principals, queries, query_identities, last_activity, "
            f"{days_col} FROM usage.workspace_activity WHERE window_days = ?", [used]).fetchall()}
        # each catalog's home workspace: pinned in config/workspace.csv, else the most lineage events
        home = _catalog_homes(con, have)
        held: dict[str, dict] = {}
        sized = ("storage", "table_detail") in have
        if sized:
            for cat, tables, views, cols, vcols, size, files in con.execute(
                    "SELECT table_catalog, COUNT(*) FILTER (WHERE detail <> 'view'), COUNT(*) FILTER (WHERE detail = 'view'), "
                    "SUM(column_count), SUM(column_count) FILTER (WHERE detail = 'view'), SUM(size_in_bytes), SUM(num_files) "
                    "FROM storage.table_detail GROUP BY 1").fetchall():
                if str(cat) not in home:
                    continue
                h = held.setdefault(home[str(cat)][0], {"catalogs": [], "tables": 0, "views": 0, "columns": 0,
                                                        "view_columns": 0, "size_in_bytes": 0, "num_files": 0})
                h["catalogs"].append(str(cat))
                for k, v in zip(("tables", "views", "columns", "view_columns", "size_in_bytes", "num_files"),
                                (tables, views, cols, vcols, size, files)):
                    h[k] += int(v or 0)
        vols: dict[str, dict] = {}
        if ("storage", "volume_detail") in have:
            for cat, n, files, size in con.execute(
                    "SELECT volume_catalog, COUNT(*), SUM(files), SUM(size_in_bytes) FROM storage.volume_detail GROUP BY 1").fetchall():
                if str(cat) in home:
                    v = vols.setdefault(home[str(cat)][0], {"volumes": 0, "volume_files": 0, "volume_bytes": 0})
                    v["volumes"] += int(n or 0)
                    v["volume_files"] += int(files or 0)
                    v["volume_bytes"] += int(size or 0)
        names: dict[str, tuple] = {}
        if ("dims", "dim_workspace") in have:
            cols = {r[0] for r in con.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_catalog = current_database() "
                "AND table_schema = 'dims' AND table_name = 'dim_workspace'").fetchall()}
            env = "env" if "env" in cols else "NULL"
            names = {str(r[0]): (r[1], r[2], r[3]) for r in con.execute(
                f"SELECT workspace_id, name, url, {env} FROM dims.dim_workspace").fetchall()}
        kept = _tag_kept_workspaces(con, tag_filter)
        # the held tables read in their home workspace, per workspace
        read_held: dict[str, int] = {}
        ta, _ta_window = _table_access(con, have, used)
        if ta:
            for ws, cat, n in con.execute(
                    "SELECT workspace_id, split_part(table_full_name, '.', 1), COUNT(*) FILTER (WHERE reads > 0) "
                    f"FROM {ta} GROUP BY 1, 2").fetchall():
                if home.get(str(cat), (None,))[0] == str(ws):
                    read_held[str(ws)] = read_held.get(str(ws), 0) + int(n or 0)
        user_rows = con.execute(
            "SELECT identity, workspace_id, last_seen, logins, queries, tables_read, tables_written FROM usage.user_access"
        ).fetchall() if ("usage", "user_access") in have else []
        seen_rows = con.execute(
            "SELECT identity, workspace_id, last_seen, last_login FROM usage.user_last_seen"
        ).fetchall() if ("usage", "user_last_seen") in have else []
        # each held table's usage class, summed per home workspace
        classes = _table_classes(con, have, used, _seen_end(info))
        sensitive = _sensitive(con, have)
    finally:
        con.close()
    from app.core import usage

    mix: dict[str, dict] = {}
    for name, tb in classes.items():
        ws = home.get(name.split(".")[0], (None,))[0]
        if ws is None or tb["view"]:
            continue
        m = mix.setdefault(ws, {"classes": {k: 0 for k, _l, _m in usage.CLASSES}, "unread_bytes": 0, "reads": 0,
                                "writes": 0, "last_read": None, "last_write": None, "sensitive_tables": 0})
        m["classes"][tb["usage_class"]] += 1
        if tb["usage_class"] in usage.UNREAD_CLASSES:
            m["unread_bytes"] += int(tb["size_in_bytes"] or 0)
        m["reads"] += tb["reads"]
        m["writes"] += tb["writes"]
        m["last_read"] = max(filter(None, [m["last_read"], tb["last_read"]]), default=None)
        m["last_write"] = max(filter(None, [m["last_write"], tb["last_write"]]), default=None)
        m["sensitive_tables"] += 1 if name in sensitive else 0
    ids = (set(act) | set(held)) - {"0"}
    rows = []
    for ws in sorted(ids):
        if (workspace_scope is not None and ws not in workspace_scope) or (kept is not None and ws not in kept):
            continue
        a = act.get(ws) or (0, 0, 0, 0, None, None)
        h = held.get(ws) or {"catalogs": [], "tables": 0, "views": 0, "columns": 0, "view_columns": 0,
                             "size_in_bytes": 0, "num_files": 0}
        name, url, env = names.get(ws, (None, None, None))
        v = vols.get(ws) or {"volumes": 0, "volume_files": 0, "volume_bytes": 0}
        rows.append({"workspace_id": ws, "workspace_name": name or ws, "url": url, "env": env, **v,
                     "active_people": int(a[0] or 0), "active_service_principals": int(a[1] or 0),
                     "queries": int(a[2] or 0), "query_identities": int(a[3] or 0), "last_activity": a[4],
                     "active_days": None if a[5] is None else int(a[5]),
                     **{k: h[k] for k in ("tables", "views", "columns", "view_columns", "size_in_bytes", "num_files")},
                     "table_columns": h["columns"] - h["view_columns"],
                     **(mix.get(ws) or {"classes": None, "unread_bytes": None, "reads": None, "writes": None,
                                        "last_read": None, "last_write": None, "sensitive_tables": None}),
                     "catalogs": sorted(h["catalogs"]),
                     "tables_read": min(read_held.get(ws, 0), h["tables"]),
                     "tables_not_read": max(h["tables"] - read_held.get(ws, 0), 0)})
    for r, c, u in zip(rows, usage.complexity_index(rows), usage.usage_index(rows, used)):
        r["complexity_index"] = c if sized else None
        r["usage_index"] = u
        r["signal"] = usage.signal(r["complexity_index"], u)
    rows.sort(key=lambda r: (-(r["usage_index"] or 0), r["workspace_name"]))
    # everyone across the workspaces shown: how many of them they use, and how much
    shown = {r["workspace_id"]: r["workspace_name"] for r in rows}
    people: dict[str, dict] = {}
    for who, ws, last, logins, queries, t_read, t_written in user_rows:
        if str(ws) not in shown or not who:
            continue
        p = people.setdefault(who, {"identity": who, "kind": _identity_kind(who), "workspaces": [], "last_seen": None,
                                    "logins": 0, "queries": 0, "tables_read": 0, "tables_written": 0})
        p["workspaces"].append(shown[str(ws)])
        p["last_seen"] = max(filter(None, [p["last_seen"], last]), default=None)
        for k, v in (("logins", logins), ("queries", queries), ("tables_read", t_read), ("tables_written", t_written)):
            p[k] += int(v or 0)
    top_people = sorted(people.values(), key=lambda p: (-p["queries"], -p["logins"], p["identity"]))[:500]
    # last seen over a year: bands overall and per workspace, and who has been away longest
    end = _seen_end(info)
    by_id: dict[str, dict] = {}
    ws_bands: dict[str, dict[str, int]] = {}
    for who, ws, last, login in seen_rows:
        kind = _identity_kind(who)
        if str(ws) not in shown or kind not in ("Person", "Service principal"):
            continue
        band = _band(_days_since(last, end))
        if kind == "Person":
            wb = ws_bands.setdefault(str(ws), {b[0]: 0 for b in SEEN_BANDS})
            wb[band] += 1
        p = by_id.setdefault(who, {"identity": who, "kind": kind, "last_seen": None, "last_login": None, "workspaces": []})
        p["workspaces"].append(shown[str(ws)])
        p["last_seen"] = max(filter(None, [p["last_seen"], last]), default=None)
        p["last_login"] = max(filter(None, [p["last_login"], login]), default=None)
    for r in rows:
        r["seen_bands"] = ws_bands.get(r["workspace_id"], {b[0]: 0 for b in SEEN_BANDS})
    bands = [{"band": name, "people": 0, "service_principals": 0} for name, _lo, _hi in SEEN_BANDS]
    for p in by_id.values():
        p["days"] = _days_since(p["last_seen"], end)
        p["band"] = _band(p["days"])
        b = next(x for x in bands if x["band"] == p["band"])
        b["people" if p["kind"] == "Person" else "service_principals"] += 1
    inactive = sorted((p for p in by_id.values() if (p["days"] or 0) >= INACTIVE_DAYS),
                      key=lambda p: (-(p["days"] or 0), p["identity"]))
    return {"outcome": "ok_rows" if rows else "ok_empty", "info": info, "window_days": used, "sized": sized,
            "weights": {"complexity": usage.COMPLEXITY_WEIGHTS, "usage": usage.USAGE_WEIGHTS,
                        "active_days": usage.ACTIVE_DAYS_WEIGHT},
            "classes": [{"key": k, "label": label, "meaning": meaning} for k, label, meaning in usage.CLASSES],
            "rows": rows,
            "people": top_people, "people_total": len(people), "last_seen": bool(seen_rows),
            "seen_bands": bands, "inactive": inactive[:500], "inactive_total": len(inactive),
            "inactive_days": INACTIVE_DAYS}


# Days since an identity was last seen: the bands the Workspaces screens use. 90+ reads inactive.
SEEN_BANDS = (("Under 30 days", 0, 30), ("30-90 days", 30, 90), ("90-180 days", 90, 180), ("180+ days", 180, None))
INACTIVE_DAYS = 90


def _band(days: int | None) -> str:
    if days is None:
        return SEEN_BANDS[-1][0]
    return next(name for name, lo, hi in SEEN_BANDS if days >= lo and (hi is None or days < hi))


def _days_since(last: str | None, end) -> int | None:
    import datetime as _dt

    if not last:
        return None
    try:
        return (end - _dt.date.fromisoformat(str(last)[:10])).days
    except ValueError:
        return None


def _lookback(info_row) -> int | None:
    """How far back the export looked for a table's last read, from usage.workspace_activity_info."""
    try:
        info = json.loads(info_row[0]) if info_row and info_row[0] else {}
    except (TypeError, ValueError):
        return None
    return info.get("read_lookback_days") or max(info.get("windows") or [0]) or None


def _seen_end(info: dict | None):
    import datetime as _dt

    try:
        return _dt.date.fromisoformat((info or {}).get("end"))
    except (TypeError, ValueError):
        return _dt.date.today()


def _identity_kind(who: str | None) -> str:
    import re

    if not who:
        return "Unknown"
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", who):
        return "Service principal"
    return "Person" if "@" in who else "Other"


def read_workspace_schemas(workspace_id: str | None, window: int = 30, top: int = 50) -> dict:
    """Each schema of the catalogs one workspace holds (every workspace when None, the biggest `top`):
    tables, views and their columns, volumes and their files, size, and its tables by usage class;
    not_assessed without table sizes."""
    from app.core import usage

    con = _connect()
    try:
        have = {(r[0], r[1]) for r in con.execute(
            "SELECT table_schema, table_name FROM information_schema.tables WHERE table_catalog = current_database()"
        ).fetchall()}
        if ("storage", "table_detail") not in have:
            return {"outcome": "not_assessed", "schemas": [], "catalogs": []}
        info_raw = con.execute("SELECT info FROM usage.workspace_activity_info LIMIT 1").fetchone() \
            if ("usage", "workspace_activity_info") in have else None
        end = _seen_end(json.loads(info_raw[0]) if info_raw and info_raw[0] else None)
        homes = {c: ws for c, (ws, _s) in _catalog_homes(con, have).items()}
        catalogs = sorted(c for c, ws in homes.items() if workspace_id is None or ws == str(workspace_id))
        _home, names, _envs = _home_and_names(con, have)
        tbl = con.execute(
            "SELECT table_catalog, table_schema, COUNT(*) FILTER (WHERE detail IS DISTINCT FROM 'view'), "
            "COUNT(*) FILTER (WHERE detail = 'view'), SUM(column_count) FILTER (WHERE detail IS DISTINCT FROM 'view'), "
            "SUM(column_count) FILTER (WHERE detail = 'view'), SUM(size_in_bytes) FROM storage.table_detail "
            "WHERE list_contains(?, table_catalog) GROUP BY 1, 2", [catalogs]).fetchall()
        vol = con.execute(
            "SELECT volume_catalog, volume_schema, COUNT(*), SUM(files), SUM(size_in_bytes) FROM storage.volume_detail "
            "WHERE list_contains(?, volume_catalog) GROUP BY 1, 2", [catalogs]).fetchall() \
            if ("storage", "volume_detail") in have else []
        classes = _table_classes(con, have, window, end)
    finally:
        con.close()
    blank = {"tables": 0, "views": 0, "table_columns": 0, "view_columns": 0, "size_in_bytes": 0,
             "volumes": 0, "volume_files": 0, "volume_bytes": 0}
    by: dict[tuple, dict] = {}
    for c, s, t, v, tc, vc, size in tbl:
        by.setdefault((c, s), dict(blank)).update({"tables": int(t or 0), "views": int(v or 0), "table_columns": int(tc or 0),
                                                    "view_columns": int(vc or 0), "size_in_bytes": int(size or 0)})
    for c, s, n, files, size in vol:
        by.setdefault((c, s), dict(blank)).update({"volumes": int(n or 0), "volume_files": int(files or 0),
                                                    "volume_bytes": int(size or 0)})
    for name, t in classes.items():
        parts = name.split(".")
        if len(parts) == 3 and (parts[0], parts[1]) in by and not t["view"]:
            mix = by[(parts[0], parts[1])].setdefault("classes", {})
            mix[t["usage_class"]] = mix.get(t["usage_class"], 0) + 1
    schemas = [{"table_catalog": c, "table_schema": s, "classes": {}, "workspace_name": names.get(homes.get(c), homes.get(c)), **r}
               for (c, s), r in by.items()]
    schemas.sort(key=lambda r: (-(r["size_in_bytes"] + r["volume_bytes"]), r["table_catalog"], r["table_schema"]))
    total = len(schemas)
    if workspace_id is None:
        schemas = schemas[:top]
    return {"outcome": "ok_rows" if schemas else "ok_empty", "catalogs": catalogs, "schemas": schemas, "total": total,
            "classes": [k for k, _l, _m in usage.CLASSES]}


def read_person_tables(identity: str, window: int = 30) -> dict:
    """The tables one identity read or wrote, over every workspace: reads, writes, last use, the
    workspaces it used them in, each table's usage class and whether it holds sensitive columns."""
    con = _connect()
    try:
        have = {(r[0], r[1]) for r in con.execute(
            "SELECT table_schema, table_name FROM information_schema.tables WHERE table_catalog = current_database()"
        ).fetchall()}
        if ("usage", "user_tables") not in have:
            return {"outcome": "not_assessed", "rows": []}
        info_raw = con.execute("SELECT info FROM usage.workspace_activity_info LIMIT 1").fetchone() \
            if ("usage", "workspace_activity_info") in have else None
        end = _seen_end(json.loads(info_raw[0]) if info_raw and info_raw[0] else None)
        ut, _w = _windowed(con, have, "user_tables", window)
        used = con.execute(f"SELECT table_full_name, workspace_id, reads, writes, last_access FROM {ut} "
                           "WHERE identity = ?", [identity]).fetchall()
        classes = _table_classes(con, have, window, end)
        sensitive = _sensitive(con, have)
        _home, names, _envs = _home_and_names(con, have)
    finally:
        con.close()
    out: dict[str, dict] = {}
    for name, ws, r, w, last in used:
        t = out.setdefault(str(name), {"table_full_name": str(name), "reads": 0, "writes": 0, "last_access": None,
                                       "workspaces": set()})
        t["reads"] += int(r or 0)
        t["writes"] += int(w or 0)
        t["last_access"] = max(filter(None, [t["last_access"], last]), default=None)
        t["workspaces"].add(names.get(str(ws), str(ws)))
    rows = [{**t, "workspaces": sorted(t["workspaces"]), "usage_class": (classes.get(n) or {}).get("usage_class"),
             "sensitive": sorted({c["pii_type"] for c in sensitive.get(n, [])})} for n, t in out.items()]
    rows.sort(key=lambda t: (-(t["reads"] + t["writes"]), t["table_full_name"]))
    return {"outcome": "ok_rows" if rows else "ok_empty", "rows": rows, "as_of": end.isoformat()}


def read_activity(window: int, workspace_scope: set[str] | None, workspace_id: str | None = None) -> dict:
    """Read and write events in the window, and how much is active: per workspace (or, with
    workspace_id, inside one) active tables, people and volumes of all, events per day, the most
    used tables and the most active people. Events count where they happened; a table's daily
    events go to the workspace that used it most when the export has no workspace per day."""
    import datetime as _dt

    con = _connect()
    try:
        have = {(r[0], r[1]) for r in con.execute(
            "SELECT table_schema, table_name FROM information_schema.tables WHERE table_catalog = current_database()"
        ).fetchall()}
        src, _used = _table_access(con, have, window)
        if not src:
            return {"outcome": "not_assessed"}
        info_raw = con.execute("SELECT info FROM usage.workspace_activity_info LIMIT 1").fetchone() \
            if ("usage", "workspace_activity_info") in have else None
        info = json.loads(info_raw[0]) if info_raw and info_raw[0] else None
        end = _seen_end(info)
        since = (end - _dt.timedelta(days=window)).isoformat()

        def q(sql: str, ok: bool = True) -> list:
            return con.execute(sql).fetchall() if ok else []
        access = q(f"SELECT workspace_id, table_full_name, SUM(reads), SUM(writes), SUM(readers), SUM(writers) FROM {src} "
                   "GROUP BY 1, 2 HAVING SUM(reads) + SUM(writes) > 0")
        held = q("SELECT table_catalog, table_catalog || '.' || table_schema || '.' || table_name, size_in_bytes "
                 "FROM storage.table_detail WHERE detail IS DISTINCT FROM 'view'", ("storage", "table_detail") in have)
        vols = q("SELECT volume_catalog, volume_catalog || '.' || volume_schema || '.' || volume_name FROM storage.volume_detail",
                 ("storage", "volume_detail") in have)
        va, _w = _windowed(con, have, "volume_access", window)
        vol_use = q(f"SELECT workspace_id, volume_full_name, identity, reads, writes, GREATEST(last_read, last_write) "
                    f"FROM {va}", va is not None)
        seen = q("SELECT workspace_id, identity, last_seen FROM usage.user_last_seen", ("usage", "user_last_seen") in have) \
            + q("SELECT workspace_id, identity, last_seen FROM usage.user_access", ("usage", "user_access") in have)
        ut, _w = _windowed(con, have, "user_tables", window)
        used = q(f"SELECT workspace_id, identity, table_full_name, reads, writes, last_access FROM {ut}", ut is not None)
        # per workspace when the export has it, else one row per table
        has_ws = ("usage", "table_daily") in have and "workspace_id" in _columns(con, "usage", "table_daily")
        daily = q(f"SELECT table_full_name, {'workspace_id' if has_ws else 'NULL'}, day, reads, writes FROM usage.table_daily "
                  f"WHERE day >= '{since}' AND day < '{end}'", ("usage", "table_daily") in have)
        classes = _table_classes(con, have, window, end) if workspace_id else {}
        home, names, envs = _home_and_names(con, have)
        load = _query_load(con, have, since, end.isoformat())
    finally:
        con.close()
    from app.core.usage import skipped_catalog

    def ours(full_name) -> bool:
        return bool(full_name) and not skipped_catalog(str(full_name).split(".")[0])
    access = [a for a in access if ours(a[1])]
    held = [h for h in held if ours(h[1])]
    vols = [v for v in vols if ours(v[1])]
    vol_use = [v for v in vol_use if ours(v[1])]
    used = [u for u in used if ours(u[2])]
    daily = [d for d in daily if ours(d[0])]
    size = {t: b for _c, t, b in held}
    load = {t: v for t, v in load.items() if ours(t)}

    def keep(ws) -> bool:
        return ws is not None and (workspace_scope is None or str(ws) in workspace_scope) \
            and (workspace_id is None or str(ws) == str(workspace_id))

    def name(ws) -> str:
        return names.get(str(ws), str(ws))

    def blank() -> dict:
        return {"reads": 0, "writes": 0, "tables": set(), "tables_all": set(), "people": set(), "people_all": set(),
                "volumes": set(), "volumes_all": set(), "file_reads": 0, "file_writes": 0}
    ws_: dict[str, dict] = {}

    def at(ws) -> dict:
        return ws_.setdefault(str(ws), blank())
    tbl: dict[tuple, dict] = {}
    best: dict[str, tuple] = {}  # table -> (events, workspace) of the workspace that used it most
    for ws, t, r, w, rd, wt in access:
        ev = int(r or 0) + int(w or 0)
        if ev > best.get(t, (0, None))[0]:
            best[t] = (ev, str(ws))
        if not keep(ws):
            continue
        a = at(ws)
        a["reads"] += int(r or 0)
        a["writes"] += int(w or 0)
        a["tables"].add(t)
        a["tables_all"].add(t)
        tbl[(str(ws), t)] = {"table_full_name": t, "workspace_id": str(ws), "workspace_name": name(ws), "reads": int(r or 0),
                             "writes": int(w or 0), "people": int(rd or 0) + int(wt or 0)}
    for cat, t, _b in held:
        if keep(home.get(cat)):
            at(home[cat])["tables_all"].add(t)
    for cat, v in vols:
        if keep(home.get(cat)):
            at(home[cat])["volumes_all"].add(v)
    vol: dict[str, dict] = {}
    for ws, v, who, r, w, last in vol_use:
        if not keep(ws):
            continue
        a = at(ws)
        a["volumes_all"].add(v)
        if last and str(last)[:10] >= since:
            a["volumes"].add(v)
            a["file_reads"] += int(r or 0)
            a["file_writes"] += int(w or 0)
            x = vol.setdefault(v, {"volume_full_name": v, "reads": 0, "writes": 0, "people": set()})
            x["reads"] += int(r or 0)
            x["writes"] += int(w or 0)
            x["people"].add(who)
    last_seen: dict[tuple, str | None] = {}
    for ws, who, last in seen:
        if who and keep(ws) and _identity_kind(who) in ("Person", "Service principal"):
            day = str(last)[:10] if last else None
            last_seen[(str(ws), who)] = max(filter(None, [last_seen.get((str(ws), who)), day]), default=None)
    for (ws, who), last in last_seen.items():
        at(ws)["people_all"].add(who)
        if last and last >= since:
            at(ws)["people"].add(who)
    ppl: dict[str, dict] = {}
    for ws, who, t, r, w, last in used:
        if not who or not keep(ws) or not last or str(last)[:10] < since:
            continue
        p = ppl.setdefault(who, {"identity": who, "kind": _identity_kind(who), "reads": 0, "writes": 0, "tables": set(),
                                 "workspaces": set()})
        p["reads"] += int(r or 0)
        p["writes"] += int(w or 0)
        p["tables"].add(t)
        p["workspaces"].add(name(ws))
        at(ws)["people"].add(who)
        at(ws)["people_all"].add(who)
    days = [(end - _dt.timedelta(days=window - i)).isoformat() for i in range(window)]
    pos = {d: i for i, d in enumerate(days)}

    def where(t, ws) -> str | None:
        """The workspace a daily row counts for: its own, else the one that used the table most."""
        return str(ws) if ws is not None else best.get(t, (0, None))[1]

    def heat(key_of) -> dict:
        out: dict[str, list] = {}
        for t, ws, day, r, w in daily:
            k, i = key_of(t, where(t, ws)), pos.get(str(day)[:10])
            if k is not None and i is not None:
                out.setdefault(k, [0] * window)[i] += int(r or 0) + int(w or 0)
        return out

    top_tables = sorted(tbl.values(), key=lambda x: (-(x["reads"] + x["writes"]), x["table_full_name"]))
    top_people = sorted(({**p, "tables": len(p["tables"]), "workspaces": sorted(p["workspaces"])} for p in ppl.values()),
                        key=lambda p: (-(p["reads"] + p["writes"]), p["identity"]))
    rows = sorted(({"workspace_id": ws, "workspace_name": name(ws), "env": envs.get(ws), "reads": a["reads"], "writes": a["writes"],
                    "file_reads": a["file_reads"], "file_writes": a["file_writes"],
                    "tables_active": len(a["tables"]), "tables": len(a["tables_all"]), "people_active": len(a["people"]),
                    "people": len(a["people_all"]), "volumes_active": len(a["volumes"]), "volumes": len(a["volumes_all"])}
                   for ws, a in ws_.items()),
                  key=lambda r: (-(r["reads"] + r["writes"] + r["file_reads"] + r["file_writes"]), r["workspace_name"]))

    def every(k: str) -> int:
        return len(set().union(*[a[k] for a in ws_.values()])) if ws_ else 0
    out = {"outcome": "ok_rows" if rows else "ok_empty", "as_of": end.isoformat(), "window": window, "days": days,
           "totals": {"reads": sum(r["reads"] for r in rows), "writes": sum(r["writes"] for r in rows),
                      "tables_active": every("tables"), "tables": every("tables_all"), "people_active": every("people"),
                      "people": every("people_all"), "volumes_active": every("volumes"), "volumes": every("volumes_all")},
           "workspaces": rows, "top_tables": top_tables[:10], "top_people": top_people[:10],
           "heavy_tables": _heaviest(load, size, lambda t: home.get(t.split(".")[0]) or where(t, None), keep, name)}
    if workspace_id is None:
        h = heat(lambda t, ws: ws)
        out["heat"] = [{"key": r["workspace_id"], "label": r["workspace_name"], "cells": h.get(r["workspace_id"], [0] * window)}
                       for r in rows]
        return out
    a = ws_.get(str(workspace_id)) or blank()
    mix: dict[str, int] = {}
    for t in a["tables_all"]:
        c = (classes.get(t) or {}).get("usage_class") or "idle_90d"
        mix[c] = mix.get(c, 0) + 1
    per_day = [{"day": d, "reads": 0, "writes": 0} for d in days]
    for t, ws, day, r, w in daily:
        i = pos.get(str(day)[:10])
        if where(t, ws) == str(workspace_id) and i is not None:
            per_day[i]["reads"] += int(r or 0)
            per_day[i]["writes"] += int(w or 0)
    h = heat(lambda t, ws: t if ws == str(workspace_id) else None)
    out.update({"workspace_name": name(workspace_id), "classes": mix, "daily": per_day,
                # every table of the workspace, idle ones too: big and unused is the point
                "bubbles": sorted(({**(tbl.get((str(workspace_id), t)) or {"table_full_name": t, "reads": 0, "writes": 0, "people": 0}),
                                    "size_in_bytes": (classes.get(t) or {}).get("size_in_bytes"),
                                    "usage_class": (classes.get(t) or {}).get("usage_class")} for t in a["tables_all"]),
                                  key=lambda x: (-(x["reads"] + x["writes"]), -(x["size_in_bytes"] or 0), x["table_full_name"]))[:200],
                "people_unseen": len(a["people_all"] - a["people"]),
                "heat": [{"key": t["table_full_name"], "label": t["table_full_name"], "cells": h.get(t["table_full_name"], [0] * window)}
                         for t in top_tables[:10]],
                "volumes": sorted(({**v, "people": len(v["people"])} for v in vol.values()),
                                  key=lambda v: (-(v["reads"] + v["writes"]), v["volume_full_name"]))[:10]})
    return out


# what the Heaviest tables chart can rank by; each keeps its own top so a switch has rows
HEAVY_BY = ("query_ms", "read_bytes", "spilled_bytes", "full_scans", "size_in_bytes")


def _query_load(con, have, since: str, end: str) -> dict[str, dict]:
    """Per table, the queries reading and writing it in [since, end): their time, bytes read and
    written, spill and full scans (usage.table_query_daily; write and spill columns from newer exports)."""
    if ("usage", "table_query_daily") not in have:
        return {}
    cols = _columns(con, "usage", "table_query_daily")
    extra = ", ".join(c if c in cols else f"0 AS {c}" for c in ("spilled_bytes", "write_queries", "write_ms", "written_bytes"))
    got = con.execute(
        "SELECT table_full_name, SUM(queries), SUM(total_ms), SUM(read_bytes), SUM(full_scans), SUM(spilled_bytes), "
        "SUM(write_queries), SUM(write_ms), SUM(written_bytes) FROM (SELECT table_full_name, queries, total_ms, read_bytes, "
        f"full_scans, {extra} FROM usage.table_query_daily WHERE day >= ? AND day < ?) GROUP BY 1", [since, end]).fetchall()
    return {str(t): {"reads": int(r or 0), "read_ms": int(rm or 0), "read_bytes": int(rb or 0), "full_scans": int(fs or 0),
                     "spilled_bytes": int(sp or 0), "writes": int(w or 0), "write_ms": int(wm or 0), "written_bytes": int(wb or 0)}
            for t, r, rm, rb, fs, sp, w, wm, wb in got}


def _heaviest(load: dict[str, dict], size: dict[str, int | None], ws_of, keep, name, top: int = 10) -> list[dict]:
    """The tables queries spend the most on, for the Heaviest tables chart: the top `top` by each of
    HEAVY_BY, query time being read plus write time."""
    rows = []
    for t in set(load) | {t for t, b in size.items() if b}:
        ws = ws_of(t)
        if not keep(ws):
            continue
        x = load.get(t) or {"reads": 0, "read_ms": 0, "read_bytes": 0, "full_scans": 0, "spilled_bytes": 0,
                             "writes": 0, "write_ms": 0, "written_bytes": 0}
        rows.append({**x, "table_full_name": t, "workspace_id": str(ws), "workspace_name": name(ws),
                     "query_ms": x["read_ms"] + x["write_ms"], "size_in_bytes": size.get(t)})
    keys = set()
    for k in HEAVY_BY:
        keys |= {r["table_full_name"] for r in sorted(rows, key=lambda r: (-(r[k] or 0), r["table_full_name"]))[:top] if r[k]}
    return sorted((r for r in rows if r["table_full_name"] in keys), key=lambda r: (-r["query_ms"], r["table_full_name"]))


def read_volumes(workspace_scope: set[str] | None, tag_filter=None, window: int = 30) -> dict:
    """Every volume (storage.volume_detail) with its home workspace, files and size, and who read and
    wrote files in it (usage.volume_access), plus everyone who used a volume; not_assessed when
    this export has neither."""
    con = _connect()
    try:
        have = {(r[0], r[1]) for r in con.execute(
            "SELECT table_schema, table_name FROM information_schema.tables WHERE table_catalog = current_database()"
        ).fetchall()}
        listed = ("storage", "volume_detail") in have
        accessed = ("usage", "volume_access") in have
        if not (listed or accessed):
            return {"outcome": "not_assessed", "volumes": [], "people": []}

        def rows(sql: str) -> list[dict]:
            cur = con.execute(sql)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

        home = {c: ws for c, (ws, _share) in _catalog_homes(con, have).items()}
        names: dict[str, str] = {}
        envs: dict[str, str] = {}
        if ("dims", "dim_workspace") in have:
            has_env = con.execute("SELECT 1 FROM information_schema.columns WHERE table_catalog = current_database() "
                                  "AND table_schema = 'dims' AND table_name = 'dim_workspace' AND column_name = 'env'").fetchone()
            for wid, name, env in con.execute(
                    f"SELECT workspace_id, name, {'env' if has_env else 'NULL'} FROM dims.dim_workspace").fetchall():
                names[str(wid)], envs[str(wid)] = name, env
        vols = rows("SELECT volume_catalog || '.' || volume_schema || '.' || volume_name AS volume_full_name, volume_catalog, "
                    "volume_type, files, size_in_bytes, truncated, detail FROM storage.volume_detail") if listed else []
        longest, _w = _windowed(con, have, "volume_access", None)
        picked, _w = _windowed(con, have, "volume_access", window)
        access = rows("SELECT workspace_id, volume_full_name, identity, reads, writes, last_read, last_write "
                      f"FROM {longest}") if accessed else []
        counts = {(str(r[0]), r[1], r[2]): (r[3], r[4]) for r in con.execute(
            f"SELECT workspace_id, volume_full_name, identity, reads, writes FROM {picked}").fetchall()} if accessed else {}
        for a in access:
            a["reads"], a["writes"] = counts.get((str(a["workspace_id"]), a["volume_full_name"], a["identity"]), (0, 0))
        kept = _tag_kept_workspaces(con, tag_filter)
        info_raw = con.execute("SELECT info FROM usage.workspace_activity_info LIMIT 1").fetchone() \
            if ("usage", "workspace_activity_info") in have else None
    finally:
        con.close()
    end = _seen_end(json.loads(info_raw[0]) if info_raw and info_raw[0] else None)

    def in_scope(ws: str | None) -> bool:
        if ws is None:
            return workspace_scope is None and kept is None
        return (workspace_scope is None or ws in workspace_scope) and (kept is None or ws in kept)

    from app.core.usage import skipped_catalog

    vols = [v for v in vols if not skipped_catalog(v["volume_catalog"])]
    access = [a for a in access if in_scope(str(a["workspace_id"])) and not skipped_catalog(a["volume_full_name"].split(".")[0])]
    by_vol: dict[str, dict] = {}
    for v in vols:
        ws = home.get(str(v["volume_catalog"]))
        if not in_scope(ws):
            continue
        by_vol[v["volume_full_name"]] = {**v, "workspace_id": ws, "workspace_name": names.get(ws, ws) if ws else None,
                                         "env": envs.get(ws) if ws else None,
                                         "reads": 0, "writes": 0, "users": set(), "last_read": None, "last_write": None}
    for a in access:
        v = by_vol.get(a["volume_full_name"])
        if v is None:  # used, but not listed (no access to list it, or created since)
            v = by_vol.setdefault(a["volume_full_name"], {
                "volume_full_name": a["volume_full_name"], "volume_catalog": a["volume_full_name"].split(".")[0],
                "volume_type": None, "files": None, "size_in_bytes": None, "truncated": False, "detail": "not listed",
                "workspace_id": str(a["workspace_id"]), "workspace_name": names.get(str(a["workspace_id"])),
                "env": envs.get(str(a["workspace_id"])),
                "reads": 0, "writes": 0, "users": set(), "last_read": None, "last_write": None})
        v["reads"] += int(a["reads"] or 0)
        v["writes"] += int(a["writes"] or 0)
        if (a["reads"] or 0) + (a["writes"] or 0):  # a user in the window; older use only dates the class
            v["users"].add(a["identity"])
        if a["reads"]:
            v.setdefault("reader_set", set()).add(a["identity"])
        if a["writes"]:
            v.setdefault("writer_set", set()).add(a["identity"])
        v["last_read"] = max(filter(None, [v["last_read"], a["last_read"]]), default=None)
        v["last_write"] = max(filter(None, [v["last_write"], a["last_write"]]), default=None)
    from app.core import usage

    out = []
    for v in by_vol.values():
        users = v.pop("users")
        cls, _new = usage.usage_class(v["reads"], v["writes"], v["last_read"], v["last_write"], None, end)
        readers, writers = len(v.pop("reader_set", ())), len(v.pop("writer_set", ()))
        out.append({**v, "users": len(users), "readers": readers, "writers": writers, "usage_class": cls})
    out.sort(key=lambda v: (-(v["size_in_bytes"] or 0), v["volume_full_name"]))
    people: dict[str, dict] = {}
    for a in access:
        if not (a["reads"] or 0) + (a["writes"] or 0):
            continue
        p = people.setdefault(a["identity"], {"identity": a["identity"], "kind": _identity_kind(a["identity"]),
                                              "volumes": set(), "reads": 0, "writes": 0, "last_access": None})
        p["volumes"].add(a["volume_full_name"])
        p["reads"] += int(a["reads"] or 0)
        p["writes"] += int(a["writes"] or 0)
        p["last_access"] = max(filter(None, [p["last_access"], a["last_read"], a["last_write"]]), default=None)
    people_out = sorted(({**p, "volumes": len(p["volumes"])} for p in people.values()),
                        key=lambda p: (-(p["reads"] + p["writes"]), p["identity"]))
    return {"outcome": "ok_rows" if out else "ok_empty", "listed": listed, "accessed": accessed, "as_of": end.isoformat(),
            "volumes": out, "people": people_out[:500], "people_total": len(people_out)}


def read_volume_users(volume_full_name: str, window: int = 30) -> dict:
    """Who read or wrote files in one volume in the window, by workspace."""
    con = _connect()
    try:
        have = {(r[0], r[1]) for r in con.execute(
            "SELECT table_schema, table_name FROM information_schema.tables WHERE table_catalog = current_database()"
        ).fetchall()}
        src, _w = _windowed(con, have, "volume_access", window)
        if src is None:
            return {"outcome": "not_assessed", "rows": []}
        cur = con.execute(f"SELECT identity, workspace_id, reads, writes, last_read, last_write FROM {src} "
                          "WHERE volume_full_name = ? AND reads + writes > 0 ORDER BY reads + writes DESC, identity",
                          [volume_full_name])
        cols = [d[0] for d in cur.description]
        out = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        con.close()
    for r in out:
        r["kind"] = _identity_kind(r["identity"])
    return {"outcome": "ok_rows" if out else "ok_empty", "rows": out}


def _home_and_names(con, have) -> tuple[dict, dict, dict]:
    """catalog -> home workspace; workspace -> name; workspace -> env."""
    home = {c: ws for c, (ws, _share) in _catalog_homes(con, have).items()}
    names: dict[str, str] = {}
    envs: dict[str, str] = {}
    if ("dims", "dim_workspace") in have:
        has_env = con.execute("SELECT 1 FROM information_schema.columns WHERE table_catalog = current_database() "
                              "AND table_schema = 'dims' AND table_name = 'dim_workspace' AND column_name = 'env'").fetchone()
        for wid, name, env in con.execute(
                f"SELECT workspace_id, name, {'env' if has_env else 'NULL'} FROM dims.dim_workspace").fetchall():
            names[str(wid)], envs[str(wid)] = name, env
    return home, names, envs


def read_tables(workspace_scope: set[str] | None, tag_filter=None, limit: int = 1000, window: int = 30) -> dict:
    """Every table and view with its home workspace and env, size, files, reads and writes across
    workspaces in the window (lineage), readers, writers, usage class and sensitive columns, in
    attention order (class, then size); not_assessed without table sizes."""
    from app.core import usage

    con = _connect()
    try:
        have = {(r[0], r[1]) for r in con.execute(
            "SELECT table_schema, table_name FROM information_schema.tables WHERE table_catalog = current_database()"
        ).fetchall()}
        if ("storage", "table_detail") not in have:
            return {"outcome": "not_assessed", "rows": []}
        home, names, envs = _home_and_names(con, have)
        cur = con.execute(
            "SELECT table_catalog || '.' || table_schema || '.' || table_name AS table_full_name, table_catalog, "
            "table_type, data_source_format, column_count, last_modified FROM storage.table_detail")
        names_ = [d[0] for d in cur.description]
        facts = {r[0]: dict(zip(names_, r)) for r in cur.fetchall()}
        info_raw = con.execute("SELECT info FROM usage.workspace_activity_info LIMIT 1").fetchone() \
            if ("usage", "workspace_activity_info") in have else None
        info = json.loads(info_raw[0]) if info_raw and info_raw[0] else None
        _src, used = _table_access(con, have, window)
        classes = _table_classes(con, have, window, _seen_end(info))
        sensitive = _sensitive(con, have)
        unreadable = con.execute(
            "SELECT table_catalog, table_schema, COUNT(*), MIN(substr(detail, 9)) FROM storage.table_detail "
            "WHERE detail LIKE 'failed%' GROUP BY ALL ORDER BY 3 DESC LIMIT 200").fetchall()
        kept = _tag_kept_workspaces(con, tag_filter)
    finally:
        con.close()
    out = []
    for name, tb in classes.items():
        f = facts.get(name) or {"table_full_name": name, "table_catalog": name.split(".")[0], "table_type": None,
                                "data_source_format": None, "column_count": None, "last_modified": None}
        ws = home.get(str(f["table_catalog"]))
        if workspace_scope is not None and ws not in workspace_scope:
            continue
        if kept is not None and ws not in kept:
            continue
        cols = sensitive.get(name, [])
        out.append({**f, **tb, "table_full_name": name, "workspace_id": ws,
                    "workspace_name": names.get(ws, ws) if ws else None, "env": envs.get(ws) if ws else None,
                    "sensitive": [c["pii_type"] for c in cols], "sensitive_masked": sum(1 for c in cols if c["masked"])})
    out.sort(key=lambda r: (usage.CLASS_RANK[r["usage_class"]], -(r["size_in_bytes"] or 0), r["table_full_name"]))
    counts = {k: sum(1 for r in out if r["usage_class"] == k) for k, _l, _m in usage.CLASSES}
    sens = [r for r in out if r["sensitive"]]
    # totals over every table; the list carries the biggest `limit` of each class, so no class is cut off
    totals = {"tables": len(out), "size_in_bytes": sum(r["size_in_bytes"] or 0 for r in out),
              "reads": sum(r["reads"] or 0 for r in out), "writes": sum(r["writes"] or 0 for r in out),
              "sized": sum(1 for r in out if r["size_in_bytes"] is not None),
              "sensitive_tables": len(sens),
              "sensitive_unread": sum(1 for r in sens if r["usage_class"] in usage.UNREAD_CLASSES),
              "sensitive_masked": sum(1 for r in sens if r["sensitive_masked"]),
              "sensitive_outside_prod": sum(1 for r in sens if r["env"] and r["env"] not in ("prod", "unknown"))}
    shown = [r for k, _l, _m in usage.CLASSES for r in [x for x in out if x["usage_class"] == k][:limit]]
    largest = sorted((r for r in out if not r["view"] and r["size_in_bytes"]), key=lambda r: -r["size_in_bytes"])[:9]
    return {"outcome": "ok_rows" if out else "ok_empty", "total": len(out), "window_days": used or window,
            "sensitive_assessed": ("usage", "sensitive_columns") in have,
            "as_of": _seen_end(info).isoformat(), "totals": totals, "per_class_limit": limit,
            "classes": [{"key": k, "label": label, "meaning": meaning, "tables": counts[k],
                         "size_in_bytes": sum(r["size_in_bytes"] or 0 for r in out if r["usage_class"] == k)}
                        for k, label, meaning in usage.CLASSES],
            "sensitive_tables": len(sens),
            "clean_up_first": sorted((r for r in out if r["usage_class"] in usage.UNREAD_CLASSES and not r["view"]),
                                     key=usage.cleanup_order)[:6],
            "largest": largest,
            "most_used": sorted((r for r in out if r["reads"] + r["writes"]), key=lambda r: (-(r["reads"] + r["writes"]), r["table_full_name"]))[:10],
            "not_described": [{"table_catalog": c, "table_schema": s, "tables": n, "error": e} for c, s, n, e in unreadable],
            "rows": shown}


def read_table(full_name: str, window: int = 30) -> dict:
    """One table: its facts, reads and writes per day, who used it, its changes and maintenance
    (DESCRIBE HISTORY and predictive optimization) with what they freed and how reads changed after
    a compaction, and its slowest queries."""
    con = _connect()
    try:
        have = {(r[0], r[1]) for r in con.execute(
            "SELECT table_schema, table_name FROM information_schema.tables WHERE table_catalog = current_database()"
        ).fetchall()}

        def rows(sql: str, params: list) -> list[dict]:
            cur = con.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

        extra = "".join(f", {c}" for c in ("created", "owner")
                        if ("storage", "table_detail") in have and c in _columns(con, "storage", "table_detail"))
        facts = rows("SELECT table_catalog, table_schema, table_name, table_type, data_source_format, column_count, "
                     f"size_in_bytes, num_files, last_modified, last_altered, detail{extra} FROM storage.table_detail "
                     "WHERE table_catalog || '.' || table_schema || '.' || table_name = ?", [full_name]
                     ) if ("storage", "table_detail") in have else []
        home, names, envs = _home_and_names(con, have)
        # readers and writers add up over workspaces: one person on two workspaces the same day counts twice
        daily = rows("SELECT day, SUM(reads) AS reads, SUM(writes) AS writes, SUM(readers) AS readers, SUM(writers) AS writers "
                     "FROM usage.table_daily WHERE table_full_name = ? GROUP BY day ORDER BY day",
                     [full_name]) if ("usage", "table_daily") in have else []
        ut, _w = _windowed(con, have, "user_tables", window)
        # a grant is unused only when its holder read nothing in the longest window
        longest, _w = _windowed(con, have, "user_tables", None)
        ever = {r[0] for r in con.execute(f"SELECT DISTINCT identity FROM {longest} WHERE table_full_name = ? AND reads > 0",
                                          [full_name]).fetchall()} if longest else set()
        via = "string_agg(DISTINCT via, ', ')" if ("usage", "user_tables") in have \
            and "via" in _columns(con, "usage", "user_tables") else "NULL"
        users = rows("SELECT identity, SUM(reads) AS reads, SUM(writes) AS writes, MAX(last_access) AS last_access, "
                     f"{via} AS via FROM {ut} WHERE table_full_name = ? GROUP BY 1 ORDER BY 2 + 3 DESC",
                     [full_name]) if ut else []
        history = rows("SELECT version, timestamp, operation, user_name, metrics FROM storage.table_history "
                       "WHERE table_full_name = ? ORDER BY version DESC", [full_name]
                       ) if ("storage", "table_history") in have else []
        po_metrics = "metrics" if ("usage", "table_maintenance") in have and "metrics" in _columns(con, "usage", "table_maintenance") else "NULL AS metrics"
        maint = rows("SELECT start_time, operation_type, operation_status, usage_quantity, usage_unit, "
                     f"{po_metrics} FROM usage.table_maintenance WHERE table_full_name = ? ORDER BY start_time DESC", [full_name]
                     ) if ("usage", "table_maintenance") in have else []
        tq, _w = _windowed(con, have, "table_queries", window)
        queries = rows(f"SELECT * EXCLUDE (window_days, table_full_name) FROM {tq} WHERE table_full_name = ? "
                       "ORDER BY total_ms DESC, statement_id LIMIT 10", [full_name]) if tq else []
        to, _w = _windowed(con, have, "table_other_access", window)
        other = rows(f"SELECT access, via, identity, events, last_event, table_events FROM {to} WHERE table_full_name = ? "
                     "ORDER BY events DESC, identity", [full_name]) if to else []
        query_daily = rows("SELECT day, queries, total_ms, read_bytes, read_files, pruned_files, full_scans "
                           "FROM usage.table_query_daily WHERE table_full_name = ? ORDER BY day", [full_name]
                           ) if ("usage", "table_query_daily") in have else []
        urls = dict(con.execute("SELECT CAST(workspace_id AS VARCHAR), url FROM dims.dim_workspace").fetchall()) \
            if queries and ("dims", "dim_workspace") in have else {}
        wh_names = dict(con.execute("SELECT warehouse_id, warehouse_name FROM dims.dim_warehouse").fetchall())             if queries and ("dims", "dim_warehouse") in have else {}
        access = rows("SELECT MAX(last_read) AS last_read, MAX(last_write) AS last_write FROM usage.table_access "
                      "WHERE table_full_name = ?", [full_name]) if ("usage", "table_access") in have else []
        info_row = con.execute("SELECT info FROM usage.workspace_activity_info").fetchone() \
            if ("usage", "workspace_activity_info") in have else None
        sensitive = _sensitive(con, have).get(full_name, [])
        every = _table_classes(con, have, window, _seen_end(json.loads(info_row[0]) if info_row and info_row[0] else None))
        cls = every.get(full_name)
        edges = ("usage", "table_edges") in have
        upstream = rows("SELECT source AS name, events, last_event FROM usage.table_edges WHERE target = ? "
                        "ORDER BY events DESC, source LIMIT 10", [full_name]) if edges else []
        downstream = rows("SELECT target AS name, events, last_event FROM usage.table_edges WHERE source = ? "
                          "ORDER BY events DESC, target LIMIT 10", [full_name]) if edges else []
        grants = rows("SELECT grantee, privilege FROM usage.table_grants WHERE table_full_name = ? ORDER BY grantee",
                      [full_name]) if ("usage", "table_grants") in have else None
    finally:
        con.close()
    f = facts[0] if facts else {}
    ws = home.get(str(f.get("table_catalog") or full_name.split(".")[0]))
    for node in upstream + downstream:
        node["usage_class"] = (every.get(node["name"]) or {}).get("usage_class")
    unused_grants = None if grants is None else [g for g in grants if g["grantee"] not in ever]
    rows_changed = _rows_changed(history, window, _seen_end(json.loads(info_row[0]) if info_row and info_row[0] else None))
    for u in users:
        u["kind"] = _identity_kind(u["identity"])
    for q in queries:
        q["workspace_name"] = names.get(str(q["workspace_id"]), q["workspace_id"])
        q["warehouse_name"] = wh_names.get(q["warehouse_id"]) or q["warehouse_id"]
        q["workspace_url"] = urls.get(str(q["workspace_id"]))
    end = _seen_end(json.loads(info_row[0]) if info_row and info_row[0] else None)
    timeline = _timeline(history, maint, query_daily)
    return {"outcome": "ok_rows" if (facts or daily or history) else "ok_empty", "table_full_name": full_name,
            "timeline": timeline, "upkeep": {**_upkeep(timeline, window, end), "lineage_writes": cls["writes"] if cls else None},
            "queries": queries, "scans": _scans(query_daily, window, end), "other": other,
            "other_events": other[0]["table_events"] if other else 0, "has_other": to is not None,
            "has_queries": ("usage", "table_query_daily") in have,
            "facts": f, "workspace_name": names.get(ws, ws) if ws else None, "env": envs.get(ws) if ws else None,
            "daily": daily, "users": users, "history": history, "maintenance": maint, "sensitive": sensitive,
            "usage_class": cls["usage_class"] if cls else None, "new": bool(cls and cls["new"]),
            "reads": cls["reads"] if cls else None, "writes": cls["writes"] if cls else None,
            "readers": cls["readers"] if cls else None, "writers": cls["writers"] if cls else None,
            "reader_names": cls["reader_names"] if cls else None, "writer_names": cls["writer_names"] if cls else None,
            "upstream": upstream, "downstream": downstream, "has_edges": edges, "grants": grants,
            "unused_grants": unused_grants, "rows_changed": rows_changed,
            "as_of": _seen_end(json.loads(info_row[0]) if info_row and info_row[0] else None).isoformat(),
            "lineage": ("usage", "table_daily") in have, "has_history": ("storage", "table_history") in have,
            "has_maintenance": ("usage", "table_maintenance") in have,
            "last_read": (access[0] if access else {}).get("last_read"),
            "last_write": (access[0] if access else {}).get("last_write"),
            "read_lookback_days": _lookback(info_row)}


_ROW_KEYS = ("numTargetRowsInserted", "numTargetRowsUpdated", "numTargetRowsDeleted", "numDeletedRows")
# operations that rewrite small files into big ones: Delta's OPTIMIZE (also ZORDER and liquid
# clustering), predictive optimization's compaction and clustering
_COMPACT = ("OPTIMIZE", "COMPACTION", "CLUSTERING")
# DESCRIBE HISTORY operations that change a table's rows
_WRITE_OPS = ("WRITE", "MERGE", "UPDATE", "DELETE", "STREAMING UPDATE", "COPY INTO", "TRUNCATE", "RESTORE",
              "CREATE TABLE AS SELECT", "CREATE OR REPLACE TABLE AS SELECT", "REPLACE TABLE AS SELECT",
              "CREATE OR REPLACE TABLE", "REPLACE TABLE")


def _first(m: dict, keys: tuple) -> int | None:
    for k in keys:
        v = str(m.get(k, "")).strip()
        if v.isdigit():
            return int(v)
    return None


def _timeline(history: list[dict], maint: list[dict], query_daily: list[dict]) -> list[dict]:
    """DESCRIBE HISTORY and predictive optimization runs as one list, newest first, each with the
    files and bytes it added and removed, and, after a compaction, reads in the 7 days before and after."""
    out = []
    for h in history:
        m = dict(x.split("=", 1) for x in str(h.get("metrics") or "").split(", ") if "=" in x)
        op = str(h.get("operation") or "")
        out.append({"when": h.get("timestamp"), "operation": op, "by": "delta", "who": h.get("user_name"),
                    "version": h.get("version"), "status": None, "metrics": h.get("metrics"),
                    "rows": _first(m, ("numOutputRows",)) or (sum(int(m[k]) for k in _ROW_KEYS if str(m.get(k, "")).isdigit()) or None),
                    "files_added": _first(m, ("numAddedFiles", "numFilesAdded", "numTargetFilesAdded", "numFiles")),
                    "files_removed": _first(m, ("numRemovedFiles", "numFilesRemoved", "numTargetFilesRemoved", "numDeletedFiles", "numFilesToDelete")),
                    "bytes_added": _first(m, ("numAddedBytes", "numTargetBytesAdded", "numOutputBytes")),
                    "bytes_removed": _first(m, ("numRemovedBytes", "numTargetBytesRemoved")),
                    "freed_bytes": _first(m, ("sizeOfDataToDelete",)) if op.startswith("VACUUM") else None, "dbu": None})
    for p in maint:
        try:
            m = json.loads(p["metrics"]) if p.get("metrics") else {}
        except (TypeError, ValueError):
            m = {}
        op = str(p.get("operation_type") or "")
        out.append({"when": p.get("start_time"), "operation": op, "by": "predictive", "who": "predictive optimization",
                    "version": None, "status": p.get("operation_status"),
                    "metrics": ", ".join(f"{k}={v}" for k, v in m.items()) or None, "rows": None,
                    "files_added": _first(m, ("number_of_output_files", "number_of_added_files")),
                    "files_removed": _first(m, ("number_of_compacted_files", "number_of_removed_files", "number_of_clustered_files", "number_of_deleted_files")),
                    "bytes_added": _first(m, ("amount_of_output_data_bytes",)),
                    "bytes_removed": _first(m, ("amount_of_data_compacted_bytes", "amount_of_data_removed_bytes")),
                    "freed_bytes": _first(m, ("amount_of_data_deleted_bytes",)),
                    "dbu": p.get("usage_quantity") if p.get("usage_unit") in (None, "ESTIMATED_DBU") else None})
    out.sort(key=lambda e: str(e["when"] or ""), reverse=True)
    for e in out:
        if any(e["operation"].upper().startswith(c) for c in _COMPACT) and (e["status"] in (None, "SUCCESSFUL")):
            e["effect"] = _effect(query_daily, str(e["when"] or "")[:10])
    return out


def _effect(query_daily: list[dict], day: str) -> dict | None:
    """Reads of the table in the 7 days before a compaction against the 7 days after it (its own day
    left out); None with fewer than 3 reads on either side. An indication: other changes count too."""
    import datetime as _dt

    if not day or not query_daily:
        return None
    d0 = _dt.date.fromisoformat(day)

    def side(lo: int, hi: int) -> dict | None:
        got = [r for r in query_daily if lo <= (_dt.date.fromisoformat(str(r["day"])[:10]) - d0).days <= hi]
        n = sum(r["queries"] or 0 for r in got)
        if n < 3:
            return None
        return {"queries": n, "avg_ms": sum(r["total_ms"] or 0 for r in got) / n,
                "files_per_read": sum(r["read_files"] or 0 for r in got) / n,
                "full_scans": sum(r["full_scans"] or 0 for r in got)}
    before, after = side(-7, -1), side(1, 7)
    return {"before": before, "after": after} if before and after else None


def _upkeep(timeline: list[dict], window: int, end) -> dict:
    """In the window: compactions and the files they merged, vacuums and the bytes they deleted,
    predictive optimization's DBUs, and how far back the history reaches."""
    import datetime as _dt

    since = (end - _dt.timedelta(days=window)).isoformat()
    inside = [e for e in timeline if str(e["when"] or "")[:10] >= since]
    comp = [e for e in inside if any(e["operation"].upper().startswith(c) for c in _COMPACT)]
    vac = [e for e in inside if e["operation"].upper().startswith("VACUUM")]
    delta = [e for e in timeline if e["by"] == "delta"]
    return {"compactions": len(comp), "files_merged": sum(e["files_removed"] or 0 for e in comp),
            "files_after": sum(e["files_added"] or 0 for e in comp),
            "vacuums": len({str(e["when"])[:10] for e in vac}), "freed_bytes": sum(e["freed_bytes"] or 0 for e in vac),
            "files_deleted": sum(e["files_removed"] or 0 for e in vac if e["operation"].upper() in ("VACUUM END", "VACUUM")),
            "writes": sum(1 for e in inside if e["operation"].upper() in _WRITE_OPS),
            "history_in_window": sum(1 for e in inside if e["by"] == "delta"),
            "dbu": round(sum(e["dbu"] or 0 for e in inside), 2),
            "history_since": min((str(e["when"])[:10] for e in delta if e["when"]), default=None),
            "history_ops": len(delta)}


def _scans(query_daily: list[dict], window: int, end) -> dict | None:
    """Reads in the window: how many, their time, and how many pruned no file."""
    import datetime as _dt

    since = (end - _dt.timedelta(days=window)).isoformat()
    got = [r for r in query_daily if str(r["day"])[:10] >= since]
    n = sum(r["queries"] or 0 for r in got)
    if not n:
        return None
    files, pruned = sum(r["read_files"] or 0 for r in got), sum(r["pruned_files"] or 0 for r in got)
    return {"queries": n, "avg_ms": sum(r["total_ms"] or 0 for r in got) / n, "read_bytes": sum(r["read_bytes"] or 0 for r in got),
            "full_scans": sum(r["full_scans"] or 0 for r in got),
            "pruned_share": round(100 * pruned / (files + pruned), 1) if files + pruned else None}


def _rows_changed(history: list[dict], window: int, end) -> int | None:
    """Rows inserted, updated and deleted in the window, from DESCRIBE HISTORY's operation metrics."""
    import datetime as _dt

    if not history:
        return None
    since = (end - _dt.timedelta(days=window)).isoformat()
    total = 0
    for h in history:
        if str(h.get("timestamp") or "")[:10] < since:
            continue
        m = dict(x.split("=", 1) for x in str(h.get("metrics") or "").split(", ") if "=" in x)
        n = sum(int(m[k]) for k in _ROW_KEYS if str(m.get(k, "")).isdigit()) or \
            (int(m["numOutputRows"]) if str(m.get("numOutputRows", "")).isdigit() else 0)
        total += n
    return total


def read_people(window: int, workspace_scope: set[str] | None) -> dict:
    """Everyone who queried, signed in or used a table: workspaces, queries, tables read and written,
    reads and writes, tables they write that nobody reads, sensitive tables they touched, last seen;
    and who has not been seen for 90+ days but still holds a direct SELECT."""
    from app.core import usage

    con = _connect()
    try:
        have = {(r[0], r[1]) for r in con.execute(
            "SELECT table_schema, table_name FROM information_schema.tables WHERE table_catalog = current_database()"
        ).fetchall()}
        if ("usage", "user_access") not in have:
            return {"outcome": "not_assessed", "people": [], "inactive": []}
        info_raw = con.execute("SELECT info FROM usage.workspace_activity_info LIMIT 1").fetchone() \
            if ("usage", "workspace_activity_info") in have else None
        info = json.loads(info_raw[0]) if info_raw and info_raw[0] else None
        end = _seen_end(info)
        access = con.execute("SELECT workspace_id, identity, last_seen, queries, tables_read, tables_written "
                             "FROM usage.user_access").fetchall()
        ut, _w = _windowed(con, have, "user_tables", window)
        used = con.execute(f"SELECT workspace_id, identity, table_full_name, reads, writes FROM {ut}").fetchall() if ut else []
        seen = con.execute("SELECT workspace_id, identity, last_seen FROM usage.user_last_seen"
                           ).fetchall() if ("usage", "user_last_seen") in have else []
        grants = con.execute("SELECT grantee, table_full_name FROM usage.table_grants"
                             ).fetchall() if ("usage", "table_grants") in have else None
        classes = _table_classes(con, have, window, end)
        sensitive = _sensitive(con, have)
        _home, names, _envs = _home_and_names(con, have)
    finally:
        con.close()
    keep = lambda ws: workspace_scope is None or str(ws) in workspace_scope  # noqa: E731
    wo = {n for n, tb in classes.items() if tb["usage_class"] == "written_never_read"}
    people: dict[str, dict] = {}

    def person(who: str) -> dict:
        return people.setdefault(who, {"identity": who, "kind": _identity_kind(who), "workspaces": set(), "queries": 0,
                                       "tables_read": 0, "tables_written": 0, "reads": 0, "writes": 0,
                                       "writes_nobody_reads": set(), "sensitive_tables": set(), "last_seen": None})
    for ws, who, last, q, tr, tw in access:
        if not who or not keep(ws):
            continue
        p = person(who)
        p["workspaces"].add(names.get(str(ws), str(ws)))
        p["queries"] += int(q or 0)
        p["tables_read"] += int(tr or 0)
        p["tables_written"] += int(tw or 0)
        p["last_seen"] = max(filter(None, [p["last_seen"], last]), default=None)
    for ws, who, name, r, w in used:
        if not who or not keep(ws):
            continue
        p = person(who)
        p["reads"] += int(r or 0)
        p["writes"] += int(w or 0)
        if w and name in wo:
            p["writes_nobody_reads"].add(name)
        if name in sensitive:
            p["sensitive_tables"].add(name)
    out = []
    for p in people.values():
        out.append({**p, "workspaces": sorted(p["workspaces"]), "writes_nobody_reads": sorted(p["writes_nobody_reads"]),
                    "sensitive_tables": sorted(p["sensitive_tables"])})
    out.sort(key=lambda p: (-p["writes"], -p["reads"], -p["queries"], p["identity"]))
    # not seen for 90+ days: what they can still read directly, sensitive tables first
    can = {}
    for grantee, name in grants or []:
        can.setdefault(str(grantee), []).append(name)
    last: dict[tuple, str] = {}
    for ws, who, when in seen:
        if who and keep(ws) and _identity_kind(who) in ("Person", "Service principal"):
            last[(str(ws), who)] = max(filter(None, [last.get((str(ws), who)), when]), default=None)
    inactive = []
    for (ws, who), when in last.items():
        days = _days_since(when, end)
        if days is None or days < INACTIVE_DAYS:
            continue
        tables = sorted(can.get(who, []), key=lambda n: (n not in sensitive, n))
        inactive.append({"identity": who, "kind": _identity_kind(who), "workspace": names.get(ws, ws), "last_seen": when,
                         "days": days, "can_read": tables[:10], "can_read_total": len(tables),
                         "sensitive_can_read": [n for n in tables if n in sensitive]})
    inactive.sort(key=lambda r: (-(r["days"] or 0), r["identity"]))
    # everyone seen in the last year, by when they were last seen anywhere
    anywhere: dict[str, str] = {}
    for (_ws, who), when in last.items():
        anywhere[who] = max(filter(None, [anywhere.get(who), when]), default=None)
    seen_bands = [{"band": name, "people": 0, "service_principals": 0} for name, _lo, _hi in SEEN_BANDS]
    for who, when in anywhere.items():
        b = next(x for x in seen_bands if x["band"] == _band(_days_since(when, end)))
        b["people" if _identity_kind(who) == "Person" else "service_principals"] += 1
    total_writes = sum(p["writes"] for p in out)
    sp_writes = sum(p["writes"] for p in out if p["kind"] == "Service principal")
    return {"outcome": "ok_rows" if out else "ok_empty", "people": out[:500], "total": len(out), "as_of": end.isoformat(),
            "active_people": sum(1 for p in out if p["kind"] == "Person"),
            "service_principals": sum(1 for p in out if p["kind"] == "Service principal"),
            "sp_write_share": round(100 * sp_writes / total_writes, 1) if total_writes else None,
            "touched_sensitive": sum(1 for p in out if p["sensitive_tables"]),
            "inactive": inactive[:500], "inactive_days": INACTIVE_DAYS, "grants": grants is not None,
            "seen_bands": seen_bands,
            "classes": [k for k, _l, _m in usage.CLASSES]}


def _discount() -> float:
    try:
        return float(app_config.load_settings().get("discount_pct", 0.0))
    except Exception:  # noqa: BLE001 -- a bad settings file shows list price
        return 0.0


def _check_rows(con, have, qid: str, window: int, where: str = "TRUE", params: list | None = None) -> list[dict]:
    """A check's rows for the exported window closest to `window` (findings.f_<qid>)."""
    if ("findings", f"f_{qid}") not in have:
        return []
    got = [r[0] for r in con.execute(f'SELECT DISTINCT window_days FROM findings."f_{qid}" ORDER BY 1').fetchall()]
    if not got:
        return []
    used = min(got, key=lambda w: (abs(int(w) - window), -int(w)))
    cur = con.execute(f'SELECT * FROM findings."f_{qid}" WHERE window_days = ? AND ({where})', [used, *(params or [])])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def read_warehouses(window: int, workspace_scope: set[str] | None) -> dict:
    """Every SQL warehouse with its spend (cost_daily_by_resource), up, busy and idle minutes, queries and
    users in the window (warehouse.warehouse_daily), most spend first; not_assessed without either."""
    import datetime as _dt

    con = _connect()
    try:
        have = {(r[0], r[1]) for r in con.execute(
            "SELECT table_schema, table_name FROM information_schema.tables WHERE table_catalog = current_database()"
        ).fetchall()}
        cost = _check_rows(con, have, "cost_daily_by_resource", window, "resource_type = 'warehouse'")
        daily, users = [], {}
        if ("warehouse", "warehouse_daily") in have:
            end = con.execute("SELECT MAX(day) FROM warehouse.warehouse_daily").fetchone()[0]
            if end:
                since = (_dt.date.fromisoformat(str(end)[:10]) - _dt.timedelta(days=window - 1)).isoformat()
                daily = con.execute(
                    "SELECT workspace_id, warehouse_id, SUM(up_minutes), SUM(busy_minutes), SUM(queries), SUM(failed) "
                    "FROM warehouse.warehouse_daily WHERE day >= ? GROUP BY 1, 2", [since]).fetchall()
        if ("warehouse", "warehouse_users") in have:
            users = {str(w): int(n) for w, n in con.execute(
                "SELECT warehouse_id, COUNT(DISTINCT identity) FROM warehouse.warehouse_users GROUP BY 1").fetchall()}
        names = {str(w): (str(ws), n) for ws, w, n in con.execute(
            "SELECT workspace_id, warehouse_id, warehouse_name FROM dims.dim_warehouse").fetchall()
        } if ("dims", "dim_warehouse") in have else {}
        ws_names = {str(w): n for w, n in con.execute("SELECT workspace_id, name FROM dims.dim_workspace").fetchall()
                    } if ("dims", "dim_workspace") in have else {}
    finally:
        con.close()
    if not cost and not daily:
        return {"outcome": "not_assessed", "rows": []}
    rows: dict[str, dict] = {}

    def row(ws, wh) -> dict:
        wh = str(wh)
        return rows.setdefault(wh, {"warehouse_id": wh, "workspace_id": str(ws) if ws is not None else names.get(wh, (None,))[0],
                                    "usd_list": 0.0, "up_minutes": None, "busy_minutes": None, "idle_minutes": None,
                                    "queries": None, "failed": None, "users": users.get(wh)})
    for c in cost:
        if c.get("warehouse_id"):
            r = row(c.get("workspace_id"), c["warehouse_id"])
            r["usd_list"] += float(c.get("usd_list") or 0)
    for ws, wh, up, busy, q, failed in daily:
        r = row(ws, wh)
        r.update(up_minutes=float(up or 0), busy_minutes=float(busy or 0), idle_minutes=max(float(up or 0) - float(busy or 0), 0),
                 queries=int(q or 0), failed=int(failed or 0))
    out = []
    for r in rows.values():
        if workspace_scope is not None and r["workspace_id"] not in workspace_scope:
            continue
        r["warehouse_name"] = names.get(r["warehouse_id"], (None, None))[1] or r["warehouse_id"]
        r["workspace_name"] = ws_names.get(r["workspace_id"], r["workspace_id"])
        out.append(r)
    out.sort(key=lambda r: (-r["usd_list"], -(r["queries"] or 0), r["warehouse_name"]))
    return {"outcome": "ok_rows" if out else "ok_empty", "detail": bool(daily), "discount_pct": _discount(), "rows": out}


def read_warehouse(warehouse_id: str, window: int) -> dict:
    """One SQL warehouse: spend per day (cost_daily_by_resource), up, busy and idle minutes and queries
    per day (warehouse.warehouse_daily), idle gaps and what-ifs (compute_warehouse_idle_minutes),
    pressure (query_warehouse_pressure), its settings, who uses it, the tables it touches and its
    slowest queries."""
    import datetime as _dt

    con = _connect()
    try:
        have = {(r[0], r[1]) for r in con.execute(
            "SELECT table_schema, table_name FROM information_schema.tables WHERE table_catalog = current_database()"
        ).fetchall()}

        def rows(sql: str, params: list) -> list[dict]:
            cur = con.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

        wh = [warehouse_id]
        cost = _check_rows(con, have, "cost_daily_by_resource", window, "resource_type = 'warehouse' AND warehouse_id = ?", wh)
        idle = _check_rows(con, have, "compute_warehouse_idle_minutes", window, "warehouse_id = ?", wh)
        pressure = _check_rows(con, have, "query_warehouse_pressure", window, "warehouse_id = ?", wh)
        config = _check_rows(con, have, "sql_warehouse_config_current", window, "warehouse_id = ?", wh)
        daily, users, tables, slow, info = [], [], [], [], None
        if ("warehouse", "warehouse_daily") in have:
            end = con.execute("SELECT MAX(day) FROM warehouse.warehouse_daily").fetchone()[0]
            since = (_dt.date.fromisoformat(str(end)[:10]) - _dt.timedelta(days=window - 1)).isoformat() if end else "9999"
            daily = rows("SELECT day, up_minutes, busy_minutes, queries, failed, people, service_principals, queued_ms, run_ms "
                         "FROM warehouse.warehouse_daily WHERE warehouse_id = ? AND day >= ? ORDER BY day", [warehouse_id, since])
        if ("warehouse", "warehouse_users") in have:
            users = rows("SELECT identity, queries, failed, run_ms, last_query FROM warehouse.warehouse_users "
                         "WHERE warehouse_id = ? ORDER BY queries DESC, identity LIMIT 200", wh)
        if ("warehouse", "warehouse_tables") in have:
            tables = rows("SELECT table_full_name, reads, writes, people, last_access FROM warehouse.warehouse_tables "
                          "WHERE warehouse_id = ? ORDER BY reads + writes DESC, table_full_name", wh)
        if ("warehouse", "warehouse_slow") in have:
            slow = rows("SELECT statement_id, identity, start_time, total_ms, queued_ms, status, statement_type, text "
                        "FROM warehouse.warehouse_slow WHERE warehouse_id = ? ORDER BY total_ms DESC", wh)
        if ("warehouse", "warehouse_activity_info") in have:
            raw = con.execute("SELECT info FROM warehouse.warehouse_activity_info LIMIT 1").fetchone()
            info = json.loads(raw[0]) if raw and raw[0] else None
        name = con.execute("SELECT warehouse_name, workspace_id FROM dims.dim_warehouse WHERE warehouse_id = ?", wh
                           ).fetchone() if ("dims", "dim_warehouse") in have else None
    finally:
        con.close()
    for d in daily:
        d["idle_minutes"] = max(float(d["up_minutes"] or 0) - float(d["busy_minutes"] or 0), 0)
    for u in users:
        u["kind"] = _identity_kind(u["identity"])
    cost_by_day: dict[str, float] = {}
    for c in cost:
        day = str(c.get("usage_date"))[:10]
        cost_by_day[day] = cost_by_day.get(day, 0.0) + float(c.get("usd_list") or 0)
    found = bool(cost or daily or idle or users)
    return {"outcome": "ok_rows" if found else "ok_empty", "warehouse_id": warehouse_id, "discount_pct": _discount(),
            "warehouse_name": (name[0] if name else None) or warehouse_id,
            "workspace_id": str(name[1]) if name else None,
            "cost_by_day": [{"day": d, "usd_list": v} for d, v in sorted(cost_by_day.items())],
            "daily": daily, "idle": idle[0] if idle else None, "pressure": pressure[0] if pressure else None,
            "config": config[0] if config else None, "users": users, "tables": tables, "slow": slow,
            "tables_error": ((info or {}).get("tables") or {}).get("error"), "detail": ("warehouse", "warehouse_daily") in have}


def read_workspace_tags(workspace_scope: set[str] | None, tag_filter=None) -> list[dict] | None:
    """Every tag key seen on each workspace's bill, with the value the app counts for it
    (tags.tag_workspace), for the workspaces whose own tags the tag filter keeps; None when this
    export has no tag tables."""
    con = _connect()
    try:
        built = con.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_catalog = current_database() AND table_schema = 'tags' AND table_name = 'tag_workspace'"
        ).fetchone()
        if not built:
            return None
        rows = con.execute(
            "SELECT workspace_id, display_key, tag_key, tag_value, top_value, is_allocating, share, coverage "
            "FROM tags.tag_workspace ORDER BY workspace_id, lower(display_key)"
        ).fetchall()
    finally:
        con.close()
    cols = ("workspace_id", "tag_key", "norm_key", "tag_value", "top_value", "is_valid", "share", "coverage")
    keep = app_tag_compliance._matcher(tag_filter)
    if keep:
        vals: dict[str, dict] = {}
        for r in rows:
            if r[5]:
                vals.setdefault(str(r[0]), {})[r[2]] = r[3]
        kept = {ws for ws in {str(r[0]) for r in rows} if keep([vals.get(ws, {})])}
    return [
        dict(zip(cols, r)) for r in rows
        if (workspace_scope is None or str(r[0]) in workspace_scope) and (not keep or str(r[0]) in kept)
    ]


def region_split(
    workspace_ids: list[str] | None = None,
    env: list[str] | str | None = None,
    attributes: dict[str, list[str]] | None = None,
) -> dict:
    """_region_split, cached per db file unless attribute filters apply (their allowed keys come
    from config, which can change while the db does not)."""
    if attributes:
        return _region_split(workspace_ids, env, attributes)
    return _region_split_no_attributes(workspace_ids, env)


@_file_cached(lambda: _db_path())
def _region_split_no_attributes(workspace_ids, env) -> dict:
    return _region_split(workspace_ids, env, None)


def _region_split(
    workspace_ids: list[str] | None = None,
    env: list[str] | str | None = None,
    attributes: dict[str, list[str]] | None = None,
) -> dict:
    """T-71: which workspaces a selection covers, and which of them the snapshot's regional
    system tables hold no row for (dims.dim_workspace.in_snapshot_region = FALSE).

    The same filter semantics as _build_filters, applied to dims.dim_workspace itself: every
    given filter must match (workspace_ids AND env AND each whitelisted attribute key; a key
    outside config/tag_aliases.yml is ignored, never spliced). Returns
    {"filtered": bool, "selected": [workspace_id, ...],
     "outside": [{"workspace_id", "name", "billed"}, ...], "unknown": [workspace_id, ...]},
    every list ordered by workspace_id. `filtered` is False when no workspace-level filter is
    active, and `selected` is then every dim row. A workspace_ids value with no dim row is
    simply not selected. in_snapshot_region NULL (the snapshot holds no regional row at all)
    lands in `unknown`, never in `outside`."""
    con = _connect()
    try:
        clauses: list[str] = []
        params: list = []
        if workspace_ids:
            clauses.append(f"workspace_id IN ({', '.join('?' for _ in workspace_ids)})")
            params.extend(str(w) for w in workspace_ids)
        envs = [env] if isinstance(env, str) else list(env or [])
        if envs:
            clauses.append(f"env IN ({', '.join('?' for _ in envs)})")
            params.extend(envs)
        if attributes:
            allowed = _attribute_canonical_keys()
            for key, values in attributes.items():
                if key not in allowed or not values:
                    continue
                clauses.append(f'"{key}" IN ({", ".join("?" for _ in values)})')
                params.extend(values)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = con.execute(
            "SELECT workspace_id, name, in_snapshot_region, billed_in_snapshot "
            f"FROM dims.dim_workspace{where} ORDER BY workspace_id",
            params,
        ).fetchall()
    finally:
        con.close()
    return {
        "filtered": bool(clauses),
        "selected": [str(r[0]) for r in rows],
        "outside": [
            {"workspace_id": str(r[0]), "name": r[1], "billed": bool(r[3])}
            for r in rows
            if r[2] is False
        ],
        "unknown": [str(r[0]) for r in rows if r[2] is None],
    }


# ---------------------------------------------------------------------------------------------
# read_dim_job / read_dim_cluster / read_dim_warehouse / read_dim_pipeline (T-40) -- the object-
# name dims app/ui/data.add_names() joins onto a finding frame. Same shape as read_dim_workspace
# above -- a short-lived read-only connection (each opens/closes its own unless a caller passes
# one in via `con`, GET /api/dims's own speed-lane optimisation), no caching in here, and NO
# special-casing for "the dim is not built yet" -- an unbuilt/missing dims.* table raises
# duckdb.CatalogException like any other missing table, and callers (app/ui/data.add_names) are
# the ones that catch that and degrade gracefully (this module's own contract, PLAN.md 5.6, never
# swallows a DuckDB error itself).
#
# T-66B / DEC-66.3: each of these four dims also carries owner/run-as identity columns UNMASKED at
# the DuckDB level (DEC-55) -- the source system tables do not mask them, unlike
# identity_metadata.run_as, which every vendored finding query partial-masks in-SQL (DEC-48). Every
# such column is masked to DEC-66.3's format (app/core/identity.format_identity, T-66A's own SQL
# CASE reproduced in Python) BEFORE the frame is returned, so every caller -- today only
# app/api/app.py's GET /api/dims, which the Jobs tab's job-focus panel reads verbatim -- gets an
# already-safe frame with no unmasked identity left to forget to mask downstream. dim_job's own
# run_as/run_as_user_name pair is the one exception to "no real id available": system.lakeflow.
# jobs.run_as IS itself a real per-row user/service-principal id at the source (the same id/name
# pairing T-66A's SQL side COALESCEs for query.history executed_by_user_id/executed_by), so
# read_dim_job below uses its own helper, _mask_dim_job, instead of the generic
# _mask_identity_columns every other reader uses.
# ---------------------------------------------------------------------------------------------


def _mask_identity_columns(df: pd.DataFrame, columns: tuple[str, ...]) -> pd.DataFrame:
    """Mask `columns` of `df` in place to DEC-66.3's format and return it, only when
    privacy.mask_user_identities is on (off leaves every value raw). No real per-row id is
    available for any of these columns (dim_cluster.owned_by, dim_warehouse.created_by,
    dim_pipeline.created_by/run_as) -- see app/core/identity's own module docstring for exactly
    why, and tasks/DECISIONS.md T-66B not_done -- so every value here takes format_identity's
    hash-fallback branch, same as passing real_id=None explicitly. dim_job is masked by
    _mask_dim_job below instead, because its run_as column does carry a real id."""
    if not app_identity.masking_enabled():
        return df
    for col in columns:
        if col in df.columns:
            df[col] = df[col].map(app_identity.format_identity)
    return df


_USER_ID_RE = re.compile(r"^[0-9]+$")


def _mask_dim_job(df: pd.DataFrame) -> pd.DataFrame:
    """Mask dim_job's three identity columns to DEC-66.3's format and return `df` in place, only
    when privacy.mask_user_identities is on (off leaves every value raw).

    Unlike the other three dims (_mask_identity_columns, hash-fallback only), dim_job's own
    `run_as` column IS a real per-row Databricks user/service-principal id at the source
    (system.lakeflow.jobs.run_as -- "the ID of the user or service principal whose permissions
    are used for the job run"; run_as_user_name is that principal's display name/email) -- the
    same id/name pairing T-66A's SQL side COALESCEs for query.history executed_by_user_id /
    executed_by. So, row by row:
      - when run_as is a bare numeric user id, run_as_user_name is masked WITH that id as
        real_id (never the hash fallback), and run_as itself is left as that bare id -- the very
        value DEC-66.3 displays, not a second, different masked code for the same person on the
        line above it;
      - otherwise (run_as is NULL, a service-principal GUID, or -- unexpectedly -- something
        else) run_as is masked on its own (format_identity: NULL stays NULL, a GUID passes
        through, anything else gets hashed) and run_as_user_name gets no real_id.
    creator_user_name has no real id available in-lane (system.lakeflow.jobs.creator_id exists at
    the source but dbt/models/dims/dim_job.sql does not select it -- out of this item's Lane B
    scope, see tasks/DECISIONS.md T-66B not_done) and is masked unchanged, same as before.
    """
    if not app_identity.masking_enabled():
        return df
    new_run_as = []
    new_run_as_user_name = []
    for run_as, run_as_user_name in zip(df["run_as"], df["run_as_user_name"]):
        real_id = run_as if isinstance(run_as, str) and _USER_ID_RE.match(run_as) else None
        new_run_as_user_name.append(app_identity.format_identity(run_as_user_name, real_id=real_id))
        new_run_as.append(run_as if real_id is not None else app_identity.format_identity(run_as))
    df["run_as"] = new_run_as
    df["run_as_user_name"] = new_run_as_user_name
    df["creator_user_name"] = df["creator_user_name"].map(app_identity.format_identity)
    return df


def read_dim_job(con: duckdb.DuckDBPyConnection | None = None) -> pd.DataFrame:
    """dims.dim_job: workspace_id, job_id, name, run_as, run_as_user_name, creator_user_name
    (T-40). The three identity columns are masked to DEC-66.3's format (T-66B) before this
    returns -- run_as/run_as_user_name via _mask_dim_job (run_as carries a real per-row id),
    creator_user_name via the hash fallback -- see the module comment above.

    `con`: reuse an open connection instead of opening/closing a new one -- GET /api/dims passes
    one shared connection across all four read_dim_* calls to avoid four separate connection
    opens (speed lane, ~0.3s of the old 13-25s was just that)."""
    owns_con = con is None
    if owns_con:
        con = _connect()
    try:
        df = con.execute(
            "SELECT workspace_id, job_id, name, run_as, run_as_user_name, creator_user_name "
            "FROM dims.dim_job ORDER BY workspace_id, job_id"
        ).df()
    finally:
        if owns_con:
            con.close()
    return _mask_dim_job(df)


def read_dim_cluster(con: duckdb.DuckDBPyConnection | None = None) -> pd.DataFrame:
    """dims.dim_cluster: workspace_id, cluster_id, cluster_name, cluster_source, owned_by (T-40).
    cluster_id is a globally-unique GUID (the vendored library's own caveats) -- callers join on
    cluster_id alone, never workspace_id + cluster_id. owned_by is masked to DEC-66.3's format
    (T-66B) before this returns -- see the module comment above. `con`: see read_dim_job above."""
    owns_con = con is None
    if owns_con:
        con = _connect()
    try:
        df = con.execute(
            "SELECT workspace_id, cluster_id, cluster_name, cluster_source, owned_by "
            "FROM dims.dim_cluster ORDER BY cluster_id"
        ).df()
    finally:
        if owns_con:
            con.close()
    return _mask_identity_columns(df, ("owned_by",))


def read_dim_warehouse(con: duckdb.DuckDBPyConnection | None = None) -> pd.DataFrame:
    """dims.dim_warehouse: workspace_id, warehouse_id, warehouse_name, created_by (T-40).
    warehouse_id is a globally-unique GUID (the vendored library's own caveats) -- callers join on
    warehouse_id alone, never workspace_id + warehouse_id. created_by is masked to DEC-66.3's
    format (T-66B) before this returns -- see the module comment above. `con`: see read_dim_job
    above."""
    owns_con = con is None
    if owns_con:
        con = _connect()
    try:
        df = con.execute(
            "SELECT workspace_id, warehouse_id, warehouse_name, created_by "
            "FROM dims.dim_warehouse ORDER BY warehouse_id"
        ).df()
    finally:
        if owns_con:
            con.close()
    return _mask_identity_columns(df, ("created_by",))


def read_dim_pipeline(con: duckdb.DuckDBPyConnection | None = None) -> pd.DataFrame:
    """dims.dim_pipeline: workspace_id, pipeline_id, pipeline_name, created_by, run_as (T-40).
    pipeline_id is unique only within a workspace -- callers join on (workspace_id, pipeline_id).
    created_by/run_as are masked to DEC-66.3's format (T-66B) before this returns -- see the
    module comment above. `con`: see read_dim_job above."""
    owns_con = con is None
    if owns_con:
        con = _connect()
    try:
        df = con.execute(
            "SELECT workspace_id, pipeline_id, pipeline_name, created_by, run_as "
            "FROM dims.dim_pipeline ORDER BY workspace_id, pipeline_id"
        ).df()
    finally:
        if owns_con:
            con.close()
    return _mask_identity_columns(df, ("created_by", "run_as"))


def read_dim_notebook(con: duckdb.DuckDBPyConnection | None = None) -> pd.DataFrame:
    """dims.dim_notebook: workspace_id, notebook_id, notebook_path."""
    owns_con = con is None
    if owns_con:
        con = _connect()
    try:
        return con.execute(
            "SELECT workspace_id, notebook_id, notebook_path "
            "FROM dims.dim_notebook ORDER BY workspace_id, notebook_id"
        ).df()
    finally:
        if owns_con:
            con.close()


# ---------------------------------------------------------------------------------------------
# snapshot_manifest
# ---------------------------------------------------------------------------------------------

_UNAVAILABLE_MANIFEST = {
    "available": False,
    "as_of": None,
    "as_of_date": None,
    "days": None,
    "billing_days": None,
    "workspace_ids": [],
    "host_fingerprint": None,
    "metastore": None,
    "connector_version": None,
    "tables": {},
}


def _canonical_manifest_tables(tables: dict) -> dict:
    """Normalise snapshot-manifest table keys to "system.<schema>.<table>".

    tools/snapshot.py writes the manifest keyed by the parquet folder name, "<schema>__<table>"
    (tools/check_snapshot.py reads it that way, and so does the on-disk layout). The app keys
    everything else on the dotted source name: ENABLE_HINTS, RETENTION_DAYS, the dependent-finding
    counts on the Coverage page, and the lookups in finding_status / window_coverage. Before this
    normalisation those lookups missed on every real snapshot, so `state` fell through to its
    "not_assessed" default and EVERY finding rendered as NOT_ASSESSED with "Source not ok" -- on a
    snapshot whose sources were in fact fine. The fixture manifest synthesised by tests/conftest.py
    used the dotted spelling, which is why no test caught it (DEC-54).

    Both spellings are accepted: the test scenario artefacts under tests/test_app/scenarios/ are
    written dotted, and a key that is neither shape is passed through untouched rather than
    silently dropped.
    """
    out: dict = {}
    for key, info in tables.items():
        if not key.startswith("system.") and "__" in key:
            schema, table = key.split("__", 1)
            key = f"system.{schema}.{table}"
        out[key] = info
    return out


def snapshot_manifest() -> dict:
    """Parse the manifest json at AUDIT_SNAPSHOT_MANIFEST (test harness override) or
    <AUDIT_SNAPSHOT_DIR or snapshot/>/manifest.json (dev). Shape (PLAN.md 5.2): as_of, as_of_date,
    days, billing_days, workspace_ids, host_fingerprint, metastore ({cloud, region,
    id_fingerprint} or null, T-71), connector_version, and `tables` keyed by
    "system.<schema>.<table>" -> {state, rows, files, time_column, days_requested, days_effective,
    min_time, max_time, predicate, elapsed_s, slices_failed, error_class, reason, message}.
    Missing/unparsable file (no tools/snapshot.py run yet in this dev checkout, or a scenario
    fixture path that does not exist) -> {"available": False, ...all-empty}, never an exception."""
    path = _snapshot_manifest_path()
    if not path.exists():
        return dict(_UNAVAILABLE_MANIFEST)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(_UNAVAILABLE_MANIFEST)
    out = dict(_UNAVAILABLE_MANIFEST)
    out["available"] = True
    for key in ("as_of", "as_of_date", "days", "billing_days", "workspace_ids", "host_fingerprint", "metastore", "connector_version"):
        if key in data:
            out[key] = data[key]
    out["tables"] = _canonical_manifest_tables(data.get("tables", {}) or {})
    return out


@_file_cached(lambda: _db_path())
def direct_export_info() -> dict | None:
    """{"as_of", "window_coverage", "windows", "window_aliases", "tags_sources_not_exported"} from
    direct_export_meta, which only tools/load_direct_results.py writes; else None. window_coverage
    is {"<label>": days_used} (an older manifest loaded before this column existed reads as None,
    same as no manifest key); `windows` is the manifest's own labels list (e.g. [30]),
    exported_window_coverage()'s fallback for exactly that older-manifest case. window_aliases is
    {"<alias label>": "<source label>"} for a label the export reused another's rows for instead of
    querying Databricks again (empty for a full 90-day export). tags_sources_not_exported is the
    "system.schema.table" names a direct export could not read and substituted an empty stand-in
    for. An older db missing any of these columns reads them back as None/empty, not an error."""
    con = None
    try:
        con = _connect()
        cols = {r[1] for r in con.execute("PRAGMA table_info('direct_export_meta')").fetchall()}
        select_cols = [
            col if col in cols else "NULL"
            for col in ("as_of", "window_coverage", "windows", "window_aliases", "tags_sources_not_exported",
                        "as_of_date", "includes_today")
        ]
        row = con.execute(f"SELECT {', '.join(select_cols)} FROM main.direct_export_meta LIMIT 1").fetchone()
    except duckdb.Error:  # no such table on an ordinary build, or the db is missing/locked
        return None
    finally:
        if con is not None:
            con.close()
    if not row:
        return None
    as_of, coverage_json, windows_json, aliases_json, sources_json, as_of_date, includes_today = row
    coverage = json.loads(coverage_json) if coverage_json else None
    windows = json.loads(windows_json) if windows_json else None
    try:
        aliases = json.loads(aliases_json) if aliases_json else {}
    except ValueError:
        aliases = {}
    try:
        sources = json.loads(sources_json) if sources_json else []
    except ValueError:
        sources = []
    return {
        "as_of": as_of,
        "window_coverage": coverage,
        "windows": windows,
        "window_aliases": aliases,
        "tags_sources_not_exported": sources,
        # The first day left out (a range's exclusive end); with includes_today, today is in, partial.
        "as_of_date": as_of_date,
        "includes_today": bool(includes_today),
    }


# "not passed" (fetch it) vs "passed as None" (confirmed: no direct export) for the pre-fetch
# params below -- None alone can't carry both meanings.
_UNSET = object()


@_file_cached(lambda: _db_path())
def _measured_window_days(labels) -> dict[int, int]:
    """{label: actual distinct usage_date span} measured straight off cost_dollarized_by_sku_day
    (every export carries it) -- an older manifest's `windows` list only names which labels were
    RUN, not how many days of usage_date each one actually captured, so trusting label==days-
    covered turned a real 10-day export stored under window 30 into a false "30d, fully covered"
    (every page reading snapshot_days inherited the lie). Empty on any read problem (no `findings`
    schema, an export missing this one id, a locked/corrupt db) -- callers fall back to the label
    itself, same as before this existed, never a crash."""
    con = None
    try:
        con = _connect()
        rows = con.execute(
            "SELECT window_days, MIN(usage_date), MAX(usage_date) FROM findings.f_cost_dollarized_by_sku_day "
            "WHERE window_days = ANY(?) GROUP BY window_days",
            [[int(w) for w in labels]],
        ).fetchall()
    except duckdb.Error:
        return {}
    finally:
        if con is not None:
            con.close()
    out = {}
    for label, lo, hi in rows:
        if lo is None or hi is None:
            continue
        out[int(label)] = (hi - lo).days + 1
    return out


def exported_window_coverage(direct: dict | None = _UNSET) -> dict[int, int] | None:
    """{label: days_used} from a direct export's own window_coverage meta, keys as ints; None for
    an ordinary dbt/snapshot build (every WINDOW_CHOICES label is always fully built there). A
    direct export whose manifest predates window_coverage falls back to the real span measured off
    its own data (_measured_window_days) -- only a label that measuring could not confirm reads as
    fully covered, never every one blindly. `direct`, when given, is an already-read
    direct_export_info() result (None included) -- skips a second connection."""
    if direct is _UNSET:
        direct = direct_export_info()
    if direct is None:
        return None
    coverage = direct.get("window_coverage")
    if coverage:
        return {int(label): days_used for label, days_used in coverage.items()}
    windows = direct.get("windows")
    if isinstance(windows, list) and windows:
        measured = _measured_window_days(windows)
        return {int(w): measured.get(int(w), int(w)) for w in windows}
    return None


def window_status(choices: tuple[int, ...], direct: dict | None = _UNSET) -> list[dict]:
    """Per label in `choices` (service.WINDOW_CHOICES): {"days", "available", "covered_days",
    "partial"} -- the window control's own feed. An ordinary dbt/snapshot build has every label
    fully available; a direct export reports exactly what its own window_coverage says was run,
    so a window it never exported reads available=False rather than a silent empty read. `direct`,
    when given, is an already-read direct_export_info() result (None included) -- skips a second
    connection."""
    coverage = exported_window_coverage(direct)
    out = []
    for w in choices:
        if coverage is None:
            out.append({"days": w, "available": True, "covered_days": w, "partial": False})
            continue
        covered = coverage.get(w)
        if covered is None:
            out.append({"days": w, "available": False, "covered_days": None, "partial": False})
        else:
            out.append({"days": w, "available": True, "covered_days": covered, "partial": covered < w})
    return out


_NO_TRUNCATION = {"truncated": False, "max_rows": None}


@_file_cached(lambda: _db_path())
def truncation_info(query_id: str, window_days: int | None = None) -> dict:
    """{"truncated", "max_rows"} recorded by tools/load_direct_results.py for this query_id from
    the export's own row cap; _NO_TRUNCATION when this database was not built that way (an
    ordinary dbt build has no such cap) or the id was never loaded. `window_days`, when given,
    answers for that window alone against truncated_windows (e.g. only the 90d run hit the cap) --
    an older db without that column, or a non-windowed check (never window-specific), still
    answers with the id's one overall flag."""
    con = None
    try:
        con = _connect()
        try:
            row = con.execute(
                "SELECT truncated, max_rows, truncated_windows FROM main.direct_export_findings_meta "
                "WHERE query_id = ?",
                [query_id],
            ).fetchone()
        except duckdb.Error:
            # An older direct_export_findings_meta has no truncated_windows column at all.
            row = con.execute(
                "SELECT truncated, max_rows, NULL FROM main.direct_export_findings_meta WHERE query_id = ?",
                [query_id],
            ).fetchone()
    except duckdb.Error:
        return dict(_NO_TRUNCATION)
    finally:
        if con is not None:
            con.close()
    if not row:
        return dict(_NO_TRUNCATION)
    truncated, max_rows, windows_json = row
    windows = json.loads(windows_json) if windows_json else None
    if window_days is not None and windows:
        return {"truncated": window_days in windows, "max_rows": max_rows}
    return {"truncated": bool(truncated), "max_rows": max_rows}


# ---------------------------------------------------------------------------------------------
# run_results / classify_error / finding_status
# ---------------------------------------------------------------------------------------------

_MODEL_UNIQUE_ID_RE = re.compile(r"^model\.[^.]+\.f_(?P<qid>[a-z][a-z0-9_]*)$")


def classify_error(status: str, message: str | None) -> str:
    """Classify one dbt run_results.json result into the four documented classes (PLAN.md 5.6):
    catalog-missing, binder/schema drift, parser/translation gap, skipped upstream -- or "ok" for
    a successful build. The three error sub-classes are told apart by DuckDB's own error-message
    prefix ("Catalog Error", "Binder Error", "Parser Error" -- real DuckDB error classes, verified
    against a real target/run_results.json's field names per this task's own DECIDE note); an
    `error` status whose message matches none of them still needs a class, so it defaults to
    binder/schema drift (translation/schema mismatches are this project's most common real
    failure mode -- PLAN.md 5.4)."""
    if status in ("success", "pass"):
        return "ok"
    if status == "skipped":
        return "skipped upstream"
    text = (message or "").lower()
    if "catalog error" in text or "does not exist" in text:
        return "catalog-missing"
    if "parser error" in text or "syntax error" in text:
        return "parser/translation gap"
    if "binder error" in text:
        return "binder/schema drift"
    return "binder/schema drift"


_UNAVAILABLE_RUN_RESULTS = {"available": False, "generated_at": None, "models": {}}


def run_results() -> dict:
    """Parse dbt's target/run_results.json (or AUDIT_RUN_RESULTS override): {"available": bool,
    "generated_at": str | None, "models": {<query_id>: {"status", "error_class" (None on
    success), "message", "unique_id"}}}. Non-model results (dbt "sql_operation"/test/seed
    entries) are skipped -- only unique_ids shaped "model.<project>.f_<query_id>" are kept.
    Missing/unparsable file -> {"available": False, "models": {}}, never an exception (dbt may
    not have built anything yet, or another process may be mid-write to this same file)."""
    path = _run_results_path()
    if not path.exists():
        return dict(_UNAVAILABLE_RUN_RESULTS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(_UNAVAILABLE_RUN_RESULTS)

    generated_at = ((data.get("metadata") or {}).get("generated_at"))
    models: dict[str, dict] = {}
    for r in data.get("results", []):
        uid = r.get("unique_id", "") or ""
        m = _MODEL_UNIQUE_ID_RE.match(uid)
        if not m:
            continue
        qid = m.group("qid")
        status = r.get("status", "")
        message = r.get("message")
        models[qid] = {
            "status": status,
            "error_class": None if status in ("success", "pass") else classify_error(status, message),
            "message": message,
            "unique_id": uid,
        }
    return {"available": True, "generated_at": generated_at, "models": models}


# ---------------------------------------------------------------------------------------------
# direct_source_states / coverage_reason -- Coverage & Gaps' own source rollup for a loader-built
# direct export (no snapshot_manifest.json at all -- tools/load_direct_results.py never writes
# one). A missing manifest used to make every source read "not captured in this snapshot" even
# when the export read almost all of them fine; this derives read/empty/missing straight from
# run_results.json instead, which a direct export always writes (one success/error row per check).
# ---------------------------------------------------------------------------------------------

# Reason codes mirror tools/snapshot.py's own classify_error() vocabulary (app/core never imports
# tools/ -- see app/core/registry.py's own module docstring -- so the patterns are duplicated by
# value here, not by import).
_SCHEMA_NOT_ENABLED_RE = re.compile(r"schema_not_found|not enabled|is disabled", re.I)
_TABLE_NOT_FOUND_RE = re.compile(r"table_or_view_not_found|does not exist|no such table", re.I)
_NO_GRANT_RE = re.compile(
    r"insufficient_permissions|permission_denied|does not have|not authorized|unauthori[sz]ed"
    r"|access denied|permission|forbidden",
    re.I,
)
_TIMEOUT_RE = re.compile(r"timed?\s?out|timeout", re.I)

# reason code -> (plain label, the fix a reader can act on).
# A grant alone cannot turn on a disabled system table or Public-Preview feature -- the feature/
# schema must be enabled first (account console or REST API), and only then does SELECT do anything.
COVERAGE_REASON_FIX: dict[str, tuple[str, str]] = {
    "schema_not_enabled": ("system table not enabled", "Enable the feature/system schema first, then an account admin grants USE SCHEMA and SELECT on it."),
    "table_not_found": ("system table not enabled", "Enable the feature/system schema first, then an account admin grants USE SCHEMA and SELECT on it."),
    "no_grant": ("no permission to read it", "Grant access: ask an account admin for SELECT on this table."),
    "timeout": ("timed out reading it", "Re-run the export with a shorter window or fewer workspaces."),
    "not_exported": ("not run in this export", "Re-run the export -- this check produced no result last time."),
    "unknown": ("failed in the export", "Re-run the export and check its own error output for this check."),
}


def coverage_reason(message: str | None) -> str:
    """A run_results message -> one of COVERAGE_REASON_FIX's keys. `message` is None for a check
    absent from run_results entirely (never attempted, e.g. added to the registry after the export
    ran) -- "not_exported", never "unknown" (there's no error to classify, just an absence)."""
    if message is None:
        return "not_exported"
    if _SCHEMA_NOT_ENABLED_RE.search(message):
        return "schema_not_enabled"
    if _TABLE_NOT_FOUND_RE.search(message):
        return "table_not_found"
    if _NO_GRANT_RE.search(message):
        return "no_grant"
    if _TIMEOUT_RE.search(message):
        return "timeout"
    return "unknown"


# A direct export's own raw-table reads (billing.usage, query.history, both lineage tables) are
# filtered in-SQL to the export's configured window, never "as of now" -- unlike a dim-style source
# (workspaces, jobs, clusters, ...), which always reads the account's current state. "Covers" must
# say which one a source is, not assume every direct-mode source is a live snapshot.
_TRAILING_WINDOW_SOURCES = {
    "system.billing.usage", "system.query.history",
    "system.access.table_lineage", "system.access.column_lineage",
}


def direct_source_states(dependents: dict[str, list[str]], results: dict | None = None) -> dict[str, dict]:
    """Coverage & Gaps' per-source state for a loader-built direct export, keyed like
    snapshot_manifest()'s own "tables": {"state" ("ok" or "not_assessed"), "rows" (the summed row
    count of every reading check that succeeded, None when none did), "reason", "message",
    "dependent_query_ids", "checks_ok"/"checks_total" (how many of this source's reading checks
    actually ran, since "rows" is a sum across checks, not the source table's own row count),
    "covers" ("window" for a source read as a trailing window, else "current"), "window_days" (the
    window size, only set when "covers" is "window")}. `dependents` is the coverage route's own
    table -> [query_id, ...] map (registry "reads", every executable check that names this
    source); `results` lets the caller pass an already-read run_results() instead of a second file
    read.

    A source is "ok" the moment ANY reading check succeeded, however many rows it returned --
    "empty" is for the caller to derive from rows == 0, the same rule the sources panel already
    applies to a snapshot-mode "ok" source. It is "not_assessed" only when EVERY reading check
    failed or was never run this export -- never the "every source unread" a missing manifest used
    to default to."""
    results = run_results() if results is None else results
    models = results.get("models", {})
    export = direct_export_info()
    window_days = max(export["windows"]) if export and export.get("windows") else None
    qid_rows: dict[str, int] = {}
    con = None
    if any(models.get(qid, {}).get("status") in ("success", "pass") for ids in dependents.values() for qid in ids):
        try:
            con = _connect()
        except duckdb.Error:
            con = None

    def rows_for(qid: str) -> int | None:
        if qid in qid_rows:
            return qid_rows[qid]
        if con is None:
            return None
        try:
            n = int(con.execute(f'SELECT count(*) FROM findings."f_{qid}"').fetchone()[0])
        except duckdb.Error:
            return None
        qid_rows[qid] = n
        return n

    out: dict[str, dict] = {}
    try:
        for key, qids in dependents.items():
            rows_sum = 0
            ok_count = 0
            rows_known = False
            fail_message = None
            for qid in qids:
                model = models.get(qid)
                status = model.get("status") if model else None
                if status not in ("success", "pass"):
                    if fail_message is None and ok_count == 0:
                        fail_message = model.get("message") if model else None
                    continue
                ok_count += 1
                n = rows_for(qid)
                if n is not None:
                    rows_sum += n
                    rows_known = True
            any_ok = ok_count > 0
            is_window = key in _TRAILING_WINDOW_SOURCES
            out[key] = {
                "state": "ok" if any_ok else "not_assessed",
                "rows": (rows_sum if rows_known else None) if any_ok else None,
                "reason": None if any_ok else coverage_reason(fail_message),
                "message": None if any_ok else fail_message,
                "dependent_query_ids": qids,
                "checks_ok": ok_count,
                "checks_total": len(qids),
                "covers": "window" if is_window else "current",
                "window_days": window_days if is_window else None,
            }
    finally:
        if con is not None:
            con.close()
    return out


def checks_not_ok(
    specs: list[registry.QuerySpec], results: dict | None = None, manifest: dict | None = None
) -> list[dict]:
    """Coverage & Gaps' "Couldn't check" feed: one entry per `specs` (the executable registry)
    not_assessed_status() (below) flags as not_built, source_not_ok or build_failed -- never a
    check that merely reads `partial` (still assessed) -- as {"query_id", "title", "reason",
    "reason_label", "fix", "message", "sources"}, for the Couldn't-check panel to group by
    (reason_label, fix). The SAME function -- and so the same reason vocabulary -- also drives
    /api/findings' per-row not_assessed field, so the two screens never name one gap two ways.
    Works the same for a direct export (the message is the Databricks error the export hit) and an
    ordinary dbt/snapshot build (dbt's own build error, mostly "unknown" here). table_exists (one
    batched information_schema lookup) keeps a query_id whose table WAS built outside dbt off this
    list even when run_results.json has no entry for it -- a DB error here just falls back to the
    old run_results-only read, same fail-soft rule this whole endpoint already follows."""
    results = run_results() if results is None else results
    manifest = snapshot_manifest() if manifest is None else manifest
    models = results.get("models", {})
    try:
        table_cols = finding_columns_batch([s.query_id for s in specs])
    except (duckdb.Error, OSError):
        table_cols = None
    out: list[dict] = []
    for spec in specs:
        table_exists = (spec.query_id in table_cols) if table_cols is not None else None
        info = not_assessed_status(spec, results, manifest, table_exists=table_exists)
        if info is None or info["code"] is None:
            continue  # clean, or merely partial (still assessed) -- not a "couldn't check"
        model = models.get(spec.query_id)
        out.append({
            "query_id": spec.query_id,
            "title": spec.title,
            "reason": info["code"],
            "reason_label": info["label"],
            "fix": info["fix"],
            "message": model.get("message") if model else None,
            "sources": [f"system.{schema}.{table}" for schema, table in spec.sources],
        })
    return out


def truncated_query_ids() -> list[str]:
    """query_ids direct_export_findings_meta.truncated marks true (a direct export's own row cap
    hit, tools/load_direct_results.py) -- [] on an ordinary dbt/snapshot build, which has no such
    table, or when the db has no rows to say so."""
    con = None
    try:
        con = _connect()
        rows = con.execute(
            "SELECT query_id FROM main.direct_export_findings_meta WHERE truncated ORDER BY query_id"
        ).fetchall()
    except duckdb.Error:
        return []
    finally:
        if con is not None:
            con.close()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------------------------
# finding_status -- P2-PARTIAL: which of a query's own sources are load-bearing (missing/bad
# leaves NOT_ASSESSED the only honest verdict) versus a name-lookup this app's own SQL joins in
# purely to attach a human-readable "<x>_name" next to an id it already has from elsewhere. A
# blocked source used to be all-or-nothing: ANY source not "ok" (including a table the snapshot
# exported only PART of) greyed out the WHOLE finding, even when the finding's real data was
# untouched. See _optional_name_sources below for the exact rule.
# ---------------------------------------------------------------------------------------------

# The five system tables this app's dims layer (app/ui/data.py's own _ID_NAME_SPECS, T-40) also
# resolves through dims.dim_* -- reproduced here because a handful of app-owned/vendored queries
# (e.g. lakeflow_job_reliability.sql, lakeflow_job_compute_pressure.sql) join the RAW system
# table in-SQL for the exact same reason: one LEFT JOIN that adds a single display column and
# touches nothing else about the finding's own verdict (see each query's own caveats, e.g.
# lakeflow_job_reliability.sql: "job_name comes from system.lakeflow.jobs ... and is NULL for
# one-time SUBMIT_RUN/WORKFLOW_RUN executions, which never write to that table").
_NAME_LOOKUP_SOURCES: dict[str, str] = {
    "system.access.workspaces_latest": "workspace_name",
    "system.lakeflow.jobs": "job_name",
    "system.lakeflow.pipelines": "pipeline_name",
    "system.compute.clusters": "cluster_name",
    "system.compute.warehouses": "warehouse_name",
}


# Sources a query reads only to widen its answer: missing, it runs without them and says what it
# left out (tools/export_direct_results.py swaps them for an empty table the same way).
WIDENING_SOURCES: dict[str, dict[str, str]] = {
    "access_sensitive_table_reads": {
        "system.data_classification.results": "Tables flagged only by data classification are not counted",
    },
    "query_top_by_cost": {
        "system.billing.attributed_usage": "Statements are priced by splitting each billed hour, idle time included",
    },
    "query_slices": {
        "system.billing.attributed_usage": "Attributed DBUs and dollars are empty",
    },
    "cost_failed_statement_waste": {
        "system.billing.attributed_usage": "Failed statements are priced by their share of each billed hour",
    },
}


def _optional_name_sources(spec: registry.QuerySpec) -> dict[str, str]:
    """{"system.<schema>.<table>": "<x>_name"} for every one of `spec`'s own sources this call
    classifies as name-lookup-only for THIS query -- never account-wide, since the very same
    table is the load-bearing (only) source of other queries (lakeflow_health_rule_coverage.sql's
    sole source IS system.lakeflow.jobs). A source only qualifies when BOTH:
      1. `spec` has at least one OTHER source outside this five-table set -- a query whose entire
         source list is name-lookup tables has nothing else to compute the finding from, so it is
         never waived there; and
      2. the matching "<x>_name" column is actually referenced in `spec.body` (a plain regex over
         the already-parsed, in-memory SQL text -- no DuckDB connection needed). This is what a
         query that reads e.g. system.compute.clusters for real compute-pressure math but emits
         no cluster_name column fails, correctly keeping clusters required there
         (lakeflow_job_compute_pressure.sql reads clusters for max_autoscale_workers, not names).
    Not a perfect signal (a query could in principle read a name-lookup table for its name AND
    some other real column), but every query in this registry that reads one of these five tables
    alongside a genuine other source does so for its name column alone."""
    source_keys = {f"system.{schema}.{table}" for schema, table in spec.sources}
    out: dict[str, str] = {}
    for key, name_col in _NAME_LOOKUP_SOURCES.items():
        if key not in source_keys:
            continue
        if not any(other not in _NAME_LOOKUP_SOURCES for other in source_keys if other != key):
            continue  # no OTHER, non-name-lookup source -- this table IS the finding's own data
        if not re.search(rf"\b{re.escape(name_col)}\b", spec.body):
            continue  # this query never actually outputs <x>_name -- not a naming role here
        out[key] = name_col
    return out


def finding_status(query_id: str, results: dict | None = None, manifest: dict | None = None) -> dict:
    """Combine run_results() (did the model build?) with snapshot_manifest() (are its sources
    ok?) for one query_id: {"query_id", "status" ("not_built" when absent from run_results),
    "error_class", "message",
    "blocking_sources": [{"source", "state", "reason", "message"}, ...] -- a REQUIRED source
        whose snapshot state is neither "ok" nor "partial" (see below); the model built or not,
        this alone forces NOT_ASSESSED (PLAN.md 5.6: "reported first even when the model itself
        built on an empty parquet").
    "degraded_sources": [{"source", "state", "reason", "message", "degrades"}, ...] -- a source
        _optional_name_sources classified as name-lookup-only for this query and whose state is
        neither "ok" nor "partial". Never blocks: dbt's own LEFT JOIN already leaves the matching
        "<x>_name" column (named in "degrades") NULL, which callers already render as a bare id.
    "partial_sources": [{"source", "reason", "message", "missing_days", "slices_failed"}, ...] --
        ANY source (required or name-lookup) whose state IS "partial" (tools/snapshot.py: some
        day/workspace slices of that table failed to export, most of it did not). Never blocks
        either -- the finding is judged on whatever the snapshot DID capture; "missing_days" is
        every calendar day in each failed slice's half-open [from, to) range (tools/snapshot.py
        records slices that way; a multi-day slice -- e.g. a bisected retry -- spans more than one
        day), sorted and deduplicated; "slices_failed" the manifest's own full record of each one
        (from/to/workspace_id/reason/message).
    Sources are checked regardless of whether the model itself built.

    `results`/`manifest` let a caller looping over many query_ids (list_findings) read
    run_results.json and the snapshot manifest ONCE and pass the same dicts into every call,
    instead of this function re-reading and re-parsing both files per query_id -- with no global
    cache, so the read-fresh-on-every-request rule (DEC-27) still holds; a single-query caller
    (every other caller in this repo) omits them and gets the previous one-read-per-call
    behaviour."""
    spec = _validate_query_id(query_id)
    results = run_results() if results is None else results
    manifest = snapshot_manifest() if manifest is None else manifest

    # Absent metadata is NOT evidence of failure. When no snapshot manifest exists at all -- a
    # fresh checkout, a zip re-extracted without the git-ignored snapshot/ folder -- we know
    # nothing about source state, and claiming every source is "not_assessed" turned every single
    # finding into a grey NOT_ASSESSED card against a database that plainly had data (observed
    # 2026-09-22: "0/107 models ok" while the finding tables were fully populated). NOT_ASSESSED
    # must mean "we know we could not look", never "we have no metadata". With no manifest we
    # report no blocking/degraded/partial sources and let the tables speak for themselves;
    # `manifest_available` tells a caller to say "unknown" rather than "fine".
    blocking_sources = []
    degraded_sources = []
    partial_sources = []
    manifest_available = manifest.get("available")
    name_lookup = {**_optional_name_sources(spec), **WIDENING_SOURCES.get(query_id, {})} if manifest_available else {}
    for schema, table in spec.sources if manifest_available else []:
        key = f"system.{schema}.{table}"
        info = manifest["tables"].get(key)
        state = (info or {}).get("state", "not_assessed")
        if state == "ok":
            continue
        if state == "partial":
            slices_failed = (info or {}).get("slices_failed") or []
            missing_days: set[str] = set()
            for s in slices_failed:
                start = _parse_date(s.get("from"))
                if start is None:
                    continue  # no "from" (or unparseable) -- nothing to expand
                stop = _parse_date(s.get("to")) or (start + timedelta(days=1))
                d = start
                while d < stop:
                    missing_days.add(d.isoformat())
                    d += timedelta(days=1)
            partial_sources.append(
                {
                    "source": key,
                    "reason": (info or {}).get("reason"),
                    "message": (info or {}).get("message"),
                    "missing_days": sorted(missing_days),
                    "slices_failed": slices_failed,
                }
            )
            continue
        entry = {
            "source": key,
            "state": state,
            "reason": (info or {}).get("reason"),
            "message": (info or {}).get("message"),
        }
        if key in name_lookup:
            entry["degrades"] = name_lookup[key]
            degraded_sources.append(entry)
        else:
            blocking_sources.append(entry)

    model = results["models"].get(query_id)
    if model is None:
        status, error_class, message = "not_built", None, None
    else:
        status, error_class, message = model["status"], model["error_class"], model["message"]

    return {
        "query_id": query_id,
        "status": status,
        "error_class": error_class,
        "message": message,
        "blocking_sources": blocking_sources,
        "degraded_sources": degraded_sources,
        "partial_sources": partial_sources,
        "manifest_available": bool(manifest.get("available")),
        "run_results_available": bool(results.get("available")),
    }


# ---------------------------------------------------------------------------------------------
# not_assessed_status -- the ONE vocabulary /api/findings (per requested window/filters) and
# /api/coverage (build-wide, no window) both build a NOT_ASSESSED reason from, so a check that
# could not be judged names the same gap the same way on both screens.
# ---------------------------------------------------------------------------------------------

NOT_EXPORTED_FIX = ("not run in this export", "Re-run the export -- this window was never captured.")
OUTSIDE_REGION_FIX = (
    "outside the snapshot's region",
    "Re-run the export/snapshot from a workspace in this region, or select workspaces it covers.",
)
ALL_ROWS_NOT_ASSESSED_FIX = (
    "every row not assessed",
    "This check could not judge any row in scope -- see its own confidence note.",
)
NOT_BUILT_FIX = ("not in this export", "Re-run the export; this check is newer than it.")


def not_assessed_status(
    spec: "registry.QuerySpec",
    results: dict | None = None,
    manifest: dict | None = None,
    *,
    window_days: int | None = None,
    exported_coverage: dict[int, int] | None = None,
    outside_region: bool = False,
    all_rows_not_assessed: bool = False,
    table_exists: bool | None = None,
) -> dict | None:
    """None when `spec` is cleanly assessed; else {"code", "label", "fix", "partial"}.

    code is one of not_built (missing from run_results, table_exists is not True), source_not_ok (a blocking source),
    build_failed (the model itself errored/was skipped -- coverage_reason's own sub-reason picks
    label/fix), not_exported (a windowed check this window's export never ran), outside_region, or
    all_rows_not_assessed (every row in scope reads NOT_ASSESSED) -- label/fix set, partial False.
    `partial` is True with code/label/fix all None for a check that built and reads fine but the
    snapshot only partially covered its sources or this window -- still assessed, just incomplete
    (never set together with a code, matching every caller's own not_assessed_reason/partial rule).
    None means fully clean.

    `window_days`/`exported_coverage` are the caller's own per-request window and
    exported_window_coverage() -- omitted (both None), not_exported and the window-partial check
    are skipped, which is exactly right for /api/coverage (no window in scope). Likewise
    `outside_region`/`all_rows_not_assessed` are the caller's own live-data verdicts (region_split/
    status_counts) -- False when the caller has no such scope to check.

    `table_exists` is the caller's own live check for findings.f_<query_id> (e.g. membership in a
    finding_window_counts batch) -- True suppresses not_built even when run_results.json has no
    entry for this model, since a table built outside dbt (a direct export) is real, not missing.
    None/False (the default) keeps the old run_results-only read, for a caller with no live table
    check of its own (e.g. /api/coverage)."""
    results = run_results() if results is None else results
    manifest = snapshot_manifest() if manifest is None else manifest
    info = finding_status(spec.query_id, results, manifest)

    if info["status"] == "not_built" and table_exists is not True:
        label, fix = NOT_BUILT_FIX
        return {"code": "not_built", "label": label, "fix": fix, "partial": False}
    if info["blocking_sources"]:
        reason = info["blocking_sources"][0].get("reason") or "unknown"
        label, fix = COVERAGE_REASON_FIX.get(reason, COVERAGE_REASON_FIX["unknown"])
        return {"code": "source_not_ok", "label": label, "fix": fix, "partial": False}
    if info["status"] in ("error", "skipped"):
        label, fix = COVERAGE_REASON_FIX[coverage_reason(info["message"])]
        return {"code": "build_failed", "label": label, "fix": fix, "partial": False}
    if (
        spec.windowed and window_days is not None and exported_coverage is not None
        and window_days not in exported_coverage
    ):
        label, fix = NOT_EXPORTED_FIX
        return {"code": "not_exported", "label": label, "fix": fix, "partial": False}
    if outside_region:
        label, fix = OUTSIDE_REGION_FIX
        return {"code": "outside_region", "label": label, "fix": fix, "partial": False}
    if all_rows_not_assessed:
        label, fix = ALL_ROWS_NOT_ASSESSED_FIX
        return {"code": "all_rows_not_assessed", "label": label, "fix": fix, "partial": False}
    window_partial = (
        spec.windowed and window_days is not None and exported_coverage is not None
        and exported_coverage.get(window_days, window_days) < window_days
    )
    if info["degraded_sources"] or info["partial_sources"] or window_partial:
        return {"code": None, "label": None, "fix": None, "partial": True}
    return None


# ---------------------------------------------------------------------------------------------
# window_coverage
# ---------------------------------------------------------------------------------------------


def _parse_date(value: object) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def window_coverage(query_id: str, window_days: int, exported: dict[int, int] | None = _UNSET) -> dict:
    """Compare each of query_id's own sources' `min_time` (snapshot_manifest()) to
    `as_of_date - window_days`: {"query_id", "window_days", "as_of_date", "partial": bool,
    "sources": {"<source>": {"state", "min_time", "partial"}}}. A source is `partial` when its
    captured history does not reach back far enough for the requested window (min_time is later
    than as_of_date - window_days) -- the snapshot is shorter than the window, not that the
    account has no data before then.

    A direct export (exported_window_coverage() not None and covering window_days) answers from
    that instead: `partial` = covered < window_days plus `covered_days`, no per-source detail (a
    direct export has no snapshot manifest to read `min_time` from) -- so a 10-day export read at
    30d says so here too, not just in meta.windows. `exported`, when given, is an already-read
    exported_window_coverage() result (None included) -- skips a second connection."""
    spec = _validate_query_id(query_id)
    if exported is _UNSET:
        exported = exported_window_coverage()
    if exported is not None and window_days in exported:
        covered = exported[window_days]
        return {
            "query_id": query_id,
            "window_days": window_days,
            "as_of_date": None,
            "partial": covered < window_days,
            "covered_days": covered,
            "sources": {},
        }

    manifest = snapshot_manifest()
    as_of_date = _parse_date(manifest.get("as_of_date"))
    needed_start = as_of_date - timedelta(days=window_days) if as_of_date else None

    sources: dict[str, dict] = {}
    partial = False
    for schema, table in spec.sources:
        key = f"system.{schema}.{table}"
        info = manifest["tables"].get(key, {})
        min_time = _parse_date(info.get("min_time"))
        source_partial = bool(needed_start is not None and min_time is not None and min_time > needed_start)
        sources[key] = {"state": info.get("state"), "min_time": info.get("min_time"), "partial": source_partial}
        partial = partial or source_partial

    return {
        "query_id": query_id,
        "window_days": window_days,
        "as_of_date": manifest.get("as_of_date"),
        "partial": partial,
        "sources": sources,
    }


# ---------------------------------------------------------------------------------------------
# cost_cutoff
# ---------------------------------------------------------------------------------------------


def cost_cutoff() -> dict:
    """The one money cut-off: complete days through the day before the snapshot's as_of, or the
    direct export's when there is no snapshot. {"data_through", "partial_day", "partial_until"
    (HH:MM of as_of)}, all None when neither exists."""
    manifest = snapshot_manifest()
    empty = {"data_through": None, "partial_day": None, "partial_until": None}
    if manifest.get("available"):
        as_of_raw = manifest.get("as_of")
        as_of_date = _parse_date(manifest.get("as_of_date"))
    else:
        direct = direct_export_info()
        as_of_raw = direct["as_of"] if direct else None
        as_of_date = _parse_date(as_of_raw)
    if as_of_date is None:
        return empty
    partial_until = None
    if as_of_raw:
        try:
            partial_until = datetime.fromisoformat(str(as_of_raw).replace("Z", "+00:00")).strftime("%H:%M")
        except ValueError:
            partial_until = None
    return {
        "data_through": (as_of_date - timedelta(days=1)).isoformat(),
        "partial_day": as_of_date.isoformat(),
        "partial_until": partial_until,
    }
