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

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.core import config as app_config  # noqa: E402
from app.core import datasource  # noqa: E402
from app.core import finding_columns as app_finding_columns  # noqa: E402
from app.core import identity as app_identity  # noqa: E402
from app.core import materiality as app_materiality  # noqa: E402
from app.core import registry  # noqa: E402
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
_SHARED_CON: duckdb.DuckDBPyConnection | None = None
_SHARED_KEY: tuple | None = None
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
    global _SHARED_CON, _SHARED_KEY
    path = str(_db_path())
    if not (share_connection and read_only):
        return duckdb.connect(path, read_only=read_only)
    key = _identity_key(path)
    with _SHARED_LOCK:
        if _SHARED_CON is None or _SHARED_KEY != key:
            new_con = duckdb.connect(path, read_only=True)
            _cap(new_con)
            old_con = _SHARED_CON
            _SHARED_CON, _SHARED_KEY = new_con, key
            if old_con is not None:
                old_con.close()  # drop the old file's handle so Windows can delete it
        return _SHARED_CON.cursor()


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
            key = (str(path), st.st_mtime_ns, st.st_size,
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
        "WHERE table_schema = 'findings' AND table_name = ? ORDER BY ordinal_position",
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
                "WHERE table_schema = 'tags' AND table_name = 'tag_entity'"
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
        },
    )


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
                f"WHERE table_schema = 'findings' AND table_name IN ({placeholders}) "
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
                    "WHERE table_schema = 'tags' AND table_name = 'tag_entity'"
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
            f"WHERE table_schema = 'findings' AND table_name IN ({placeholders}) "
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


def read_tag_compliance(window_days: int, workspace_scope: set[str] | None) -> dict:
    """Mandatory tags missing per object type and level (app/core/tag_compliance)."""
    keys = app_config.load_settings()["mandatory_tag_keys"]
    con = _connect()
    try:
        return app_tag_compliance.compliance(con, keys, window_days, workspace_scope)
    finally:
        con.close()


def read_abac_policies() -> dict:
    """ABAC row-filter and column-mask policies (SHOW POLICIES, governance.abac_policies) and what
    the export asked; outcome not_assessed when this export has none."""
    con = _connect()
    try:
        have = {r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'governance'"
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
            "WHERE table_schema = 'governance' AND table_name = 'abac_policies'"
        ).fetchall()}
        picked = ", ".join(c if c in held else f"NULL AS {c}" for c in cols)
        rows = [dict(zip(cols, r)) for r in con.execute(
            f"SELECT {picked} FROM governance.abac_policies "
            "ORDER BY policy_type, catalog, schema, table_name, policy_name"
        ).fetchall()]
    finally:
        con.close()
    return {"outcome": "ok_rows" if rows else "ok_empty", "info": info, "rows": rows}


def read_workspace_tags(workspace_scope: set[str] | None) -> list[dict] | None:
    """Every tag key seen on each workspace's bill, with the value the app counts for it
    (tags.tag_workspace); None when this export has no tag tables."""
    con = _connect()
    try:
        built = con.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'tags' AND table_name = 'tag_workspace'"
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
    return [
        dict(zip(cols, r)) for r in rows
        if workspace_scope is None or str(r[0]) in workspace_scope
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
    """dims.dim_notebook: workspace_id, notebook_id, notebook_path (direct exports only)."""
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
    name_lookup = _optional_name_sources(spec) if manifest_available else {}
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
