"""app/core/rls.py -- row-level access from an entitlements table (config/app.yml `rls:`).

Off by default. The export reads the entitlements table once into rls.entitlements; every table in
findings, dims and tags gets workspace_id, warehouse_id and env when it lacks them, and a row is
visible to a user when ONE of their grants passes every configured column. A filtered db is built
by copying into a fresh file, so no other user's rows stay in the file's bytes.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import shutil
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import duckdb
import yaml

from app.core import config as app_config

COLUMN_MAP = {"workspace": "workspace_id", "warehouse": "warehouse_id", "environment": "env"}
APPLY = ("query", "export")
FILTERED_SCHEMAS = ("findings", "dims", "tags")
ADDED_COLUMNS = ("workspace_id", "warehouse_id", "env")
ALL = "*"
TABLE_RE = re.compile(r"^[^.\s]+\.[^.\s]+\.[^.\s]+$")
DEFAULTS = {"enabled": False, "table": "main.governance.audit_entitlements", "columns": ["workspace"], "apply": "query"}
_GRANT_INDEX = {"workspace": 0, "warehouse": 1, "environment": 2}


class RlsError(app_config.ConfigError):
    """The rls block in config/app.yml is invalid."""


@dataclass(frozen=True)
class Rls:
    enabled: bool = False
    table: str = DEFAULTS["table"]
    columns: tuple[str, ...] = ("workspace",)
    apply: str = "query"


def validate(block: dict | None) -> Rls:
    if block is not None and not isinstance(block, dict):
        raise RlsError("rls in config/app.yml must be a mapping")
    raw = {**DEFAULTS, **(block or {})}
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise RlsError(f"rls in config/app.yml: unknown key {sorted(unknown)[0]}")
    if not isinstance(raw["enabled"], bool):
        raise RlsError("rls.enabled in config/app.yml must be true or false")
    cols = raw["columns"]
    if not isinstance(cols, list) or not cols or not set(cols) <= set(COLUMN_MAP):
        raise RlsError("rls.columns in config/app.yml: a non-empty list of workspace, warehouse, environment")
    if raw["apply"] not in APPLY:
        raise RlsError("rls.apply in config/app.yml: query or export")
    table = str(raw["table"] or "")
    if not TABLE_RE.match(table):
        raise RlsError("rls.table in config/app.yml: a catalog.schema.table name")
    return Rls(raw["enabled"], table, tuple(dict.fromkeys(cols)), raw["apply"])


def merged_block(path: Path, local: Path | None = None) -> dict:
    """The rls block of app.yml with the local file winning key by key."""
    block: dict = {}
    for p in (path, local):
        if p is not None and p.exists():
            got = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("rls")
            if got is not None:
                if not isinstance(got, dict):
                    raise RlsError("rls in config/app.yml must be a mapping")
                block.update(got)
    return block


def load() -> Rls:
    d = app_config._config_dir()
    return validate(merged_block(d / "app.yml", d / "app.local.yml"))


def is_app() -> bool:
    return bool(os.environ.get("DATABRICKS_APP_PORT"))


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _lit(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


# ---- the matching rule, shared by export-time and query-time filtering ----

def _row_value(col: str, alias: str) -> str:
    ref = f"{alias}.{_q(COLUMN_MAP[col])}" if alias else _q(COLUMN_MAP[col])
    if col == "workspace":
        return f"NULLIF(CAST({ref} AS VARCHAR), '0')"  # '0' is the account-level bucket, no workspace
    if col == "environment":
        return f"lower(CAST({ref} AS VARCHAR))"
    return f"CAST({ref} AS VARCHAR)"


def _grant_rows(columns, grants) -> set[tuple]:
    cols = [c for c in COLUMN_MAP if c in set(columns)]
    return {tuple(g[_GRANT_INDEX[c]] for c in cols) for g in grants}


def predicate(columns, grants, alias: str = "t") -> str:
    """SQL true for a row that one grant lets through on every configured column. `grants` are
    (workspace_id, warehouse_id, environment) tuples with '*' for all. A NULL row value passes only '*'."""
    cols = [c for c in COLUMN_MAP if c in set(columns)]
    rows = _grant_rows(columns, grants)
    if not rows:
        return "FALSE"
    if any(all(v == ALL for v in r) for r in rows):
        return "TRUE"
    values = ", ".join("(" + ", ".join(_lit(v) for v in r) + ")" for r in sorted(rows))
    names = ", ".join(f"g{i}" for i in range(len(cols)))
    tests = " AND ".join(f"(g.g{i} = '*' OR g.g{i} = {_row_value(c, alias)})" for i, c in enumerate(cols))
    return f"EXISTS (SELECT 1 FROM (VALUES {values}) g({names}) WHERE {tests})"


def unrestricted(columns, grants) -> bool:
    return predicate(columns, grants) == "TRUE"


# ---- columns every table needs ----

def _tables(con, schema: str) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = ? AND table_type = 'BASE TABLE' "
        "ORDER BY 1", [schema]).fetchall()]


def _cols(con, schema: str, table: str) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = ? AND table_name = ? "
        "ORDER BY ordinal_position", [schema, table]).fetchall()]


def add_columns(con) -> int:
    """workspace_id, warehouse_id and env on every findings, dims and tags table that lacks them:
    the workspace from dim_warehouse / dim_cluster, env from dim_workspace, NULL where unknowable.
    Recorded in rls.added so the filtered files leave them out."""
    con.execute("CREATE SCHEMA IF NOT EXISTS rls")
    con.execute("CREATE OR REPLACE TABLE rls.added (schema_name VARCHAR, table_name VARCHAR, column_name VARCHAR)")
    dims = set(_tables(con, "dims"))
    ws_env = "dim_workspace" in dims and "env" in _cols(con, "dims", "dim_workspace")
    done = 0
    for schema in FILTERED_SCHEMAS:
        for table in _tables(con, schema):
            have = set(_cols(con, schema, table))
            ref = f"{_q(schema)}.{_q(table)}"
            added = set()
            for col in ADDED_COLUMNS:
                if col not in have:
                    con.execute(f"ALTER TABLE {ref} ADD COLUMN {_q(col)} VARCHAR")
                    con.execute("INSERT INTO rls.added VALUES (?, ?, ?)", [schema, table, col])
                    added.add(col)
                    done += 1
            if "workspace_id" in added:
                for key, dim in (("warehouse_id", "dim_warehouse"), ("cluster_id", "dim_cluster")):
                    if key in have and dim in dims and (schema, table) != ("dims", dim):
                        con.execute(
                            f"UPDATE {ref} SET workspace_id = d.ws FROM (SELECT {key}, min(workspace_id) AS ws "
                            f"FROM dims.{dim} GROUP BY 1) d WHERE {ref}.{key} = d.{key} AND {ref}.workspace_id IS NULL")
            if "env" in added and ws_env:
                con.execute(
                    f"UPDATE {ref} SET env = d.env FROM (SELECT workspace_id, min(env) AS env FROM dims.dim_workspace "
                    f"GROUP BY 1) d WHERE {ref}.workspace_id = d.workspace_id AND {ref}.env IS NULL")
    return done


# ---- the entitlements table inside the db ----

def load_entitlements(con, parquet_path, info: dict | None, rls: Rls | None = None) -> str | None:
    """rls.entitlements (principal lower-cased, '*' for all) and rls.status. A table that could
    not be read leaves it empty with the reason, so nobody sees rows. Returns that reason."""
    rls = rls or load()
    info = info or {}
    con.execute("CREATE SCHEMA IF NOT EXISTS rls")
    con.execute("CREATE OR REPLACE TABLE rls.entitlements (principal VARCHAR, workspace_id VARCHAR, "
                "warehouse_id VARCHAR, environment VARCHAR)")
    error = info.get("error")
    loaded_cols: list[str] = []
    if not error:
        try:
            if parquet_path is None or not Path(parquet_path).exists():
                raise FileNotFoundError("entitlements file missing from the export")
            reader = "read_csv_auto" if Path(parquet_path).suffix.lower() == ".csv" else "read_parquet"
            src = f"{reader}(" + _lit(str(parquet_path).replace("\\", "/")) + ")"
            have = {r[0].lower(): r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
            if "principal" not in have:
                raise ValueError("the entitlements table has no principal column")
            needed = [{"workspace": "workspace_id", "warehouse": "warehouse_id", "environment": "environment"}[c]
                      for c in rls.columns]
            missing = [n for n in needed if n not in have]
            if missing:
                raise ValueError(f"the entitlements table has no {', '.join(missing)} column")

            def grant(col: str) -> str:
                if col not in have:
                    return "'*'"
                return f"coalesce(nullif(trim(CAST({_q(have[col])} AS VARCHAR)), ''), '*')"

            loaded_cols = [c for c in rls.columns]
            con.execute(
                f"INSERT INTO rls.entitlements SELECT lower(trim(CAST({_q(have['principal'])} AS VARCHAR))), "
                f"{grant('workspace_id')}, {grant('warehouse_id')}, lower({grant('environment')}) FROM {src}")
        except Exception as exc:  # noqa: BLE001 -- fail closed with the reason
            error = f"{type(exc).__name__}: {exc}"
            con.execute("DELETE FROM rls.entitlements")
    con.execute('CREATE OR REPLACE TABLE rls.status ("user" VARCHAR, enabled BOOLEAN, apply VARCHAR, '
                "error VARCHAR, loaded_at TIMESTAMP, columns VARCHAR)")
    user = (info.get("user") or "").strip().lower() or None
    con.execute("INSERT INTO rls.status VALUES (?, ?, ?, ?, now(), ?)",
                [user, True, rls.apply if is_app() else "export", error, ",".join(loaded_cols)])
    return error


def grants_for(con, user: str | None) -> list[tuple[str, str, str]]:
    """The user's grant rows. Fail closed: a load error or an unknown user has none."""
    user = (user or "").strip().lower()
    if not user:
        return []
    try:
        if con.execute("SELECT count(*) FROM rls.status WHERE error IS NOT NULL").fetchone()[0]:
            return []
        # A column added to rls.columns after the load has no real grants stored: fail closed.
        got = con.execute("SELECT columns FROM rls.status LIMIT 1").fetchone()
        if got is None or not set(_cfg().columns) <= set((got[0] or "").split(",")):
            return []
        rows = con.execute("SELECT DISTINCT workspace_id, warehouse_id, environment FROM rls.entitlements "
                           "WHERE principal = ?", [user]).fetchall()
    except duckdb.Error:
        return []
    return [tuple(r) for r in rows]


def grant_key(columns, grants) -> str:
    return hashlib.sha1(repr(sorted(_grant_rows(columns, grants))).encode()).hexdigest()[:12]


# ---- what one user may select from each table ----

def _select_sql(schema: str, table: str, cols: list[str], added: set, where: str, full: bool, me: str | None,
                src: str = "src", grants=()) -> str | None:
    """The SELECT that yields `schema.table` as one user sees it, or None for a table that is not copied.
    The added columns are left out, so nothing needs hiding on screens. With me=None (a view shared by
    everyone with the same grants) the entitlements show those grants without the principal."""
    ref = f"{src}.{_q(schema)}.{_q(table)}"
    if schema in FILTERED_SCHEMAS:
        keep = ", ".join(_q(c) for c in cols if c not in added)
        return f"SELECT {keep} FROM {ref} t WHERE {where}"
    if (schema, table) == ("rls", "entitlements"):
        if me is None:
            vals = ", ".join("(" + ", ".join(_lit(v) for v in g) + ")" for g in sorted(set(map(tuple, grants)))) or "(NULL, NULL, NULL)"
            return (f"SELECT CAST(NULL AS VARCHAR) AS principal, workspace_id, warehouse_id, environment FROM {ref} "
                    f"WHERE (workspace_id, warehouse_id, environment) IN (VALUES {vals})")
        return f"SELECT * FROM {ref} WHERE principal = {_lit(me)}"
    if (schema, table) == ("rls", "added"):
        return None
    if (schema, table) == ("governance", "abac_policies"):
        # Account-level with no workspace: only a user with all workspaces sees it.
        return f"SELECT * FROM {ref} WHERE {'TRUE' if full else 'FALSE'}"
    return f"SELECT * FROM {ref}"


def _catalog(con, with_views: bool = False, src: str = "src"):
    """(objects, columns, added) of the attached `src` db."""
    added: dict[tuple, set] = {}
    try:
        for s, t, c in con.execute(f"SELECT schema_name, table_name, column_name FROM {src}.rls.added").fetchall():
            added.setdefault((s, t), set()).add(c)
    except duckdb.Error:
        pass
    kinds = "('BASE TABLE', 'VIEW')" if with_views else "('BASE TABLE')"
    objects = con.execute(
        f"SELECT table_schema, table_name, table_type FROM information_schema.tables WHERE table_catalog = '{src}' "
        f"AND table_schema NOT IN ('information_schema', 'pg_catalog') AND table_type IN {kinds} ORDER BY 1, 2").fetchall()
    cols: dict[tuple, list] = {}
    for s, t, c in con.execute(
            "SELECT table_schema, table_name, column_name FROM information_schema.columns "
            f"WHERE table_catalog = '{src}' ORDER BY table_schema, table_name, ordinal_position").fetchall():
        cols.setdefault((s, t), []).append(c)
    return objects, cols, added


# ---- a db holding one user's rows ----

def filter_into(src_path, dst_path, grants, user: str | None, columns=None) -> dict[str, int]:
    """Copy src into a fresh dst with only the rows `grants` let through. A fresh file, not a
    DELETE, so nothing of the other rows stays in the bytes. Returns the rows kept per
    findings/dims/tags table."""
    columns = columns or load().columns
    dst = Path(dst_path)
    for p in (dst, dst.with_name(dst.name + ".wal")):
        if p.exists():
            p.unlink()
    where = predicate(columns, grants, "t")
    full = unrestricted(columns, grants)
    me = (user or "").strip().lower()
    kept: dict[str, int] = {}
    con = duckdb.connect(str(dst))
    try:
        con.execute(f"ATTACH {_lit(src_path)} AS src (READ_ONLY)")
        objects, cols, added = _catalog(con)
        for schema, table, _ in objects:
            sel = _select_sql(schema, table, cols[(schema, table)], added.get((schema, table), set()), where, full, me)
            if sel is None:
                continue
            if schema != "main":
                con.execute(f"CREATE SCHEMA IF NOT EXISTS {_q(schema)}")
            dst_ref = f"{_q(schema)}.{_q(table)}"
            con.execute(f"CREATE TABLE {dst_ref} AS {sel}")
            if schema in FILTERED_SCHEMAS:
                kept[f"{schema}.{table}"] = con.execute(f"SELECT count(*) FROM {dst_ref}").fetchone()[0]
        con.execute("DETACH src")
        con.execute("CREATE SCHEMA IF NOT EXISTS rls")
        con.execute("CREATE OR REPLACE TABLE rls.filtered AS SELECT now() AS at")
    finally:
        con.close()
    wal = dst.with_name(dst.name + ".wal")
    if wal.exists():
        wal.unlink()
    return kept


# ---- serving: who is asking, which mode, which db ----

_principal: contextvars.ContextVar = contextvars.ContextVar("rls_principal", default=None)
NONE_KEY = "__none__"
_CFG_CACHE: dict = {}
_LOCK = threading.Lock()


def set_principal(email: str | None):
    return _principal.set(email)


def reset_principal(token) -> None:
    _principal.reset(token)


def current_principal() -> str:
    return (_principal.get() or "").strip().lower()


def _cfg() -> Rls:
    """load(), re-read only when app.yml or app.local.yml changes."""
    d = app_config._config_dir()
    stamp = tuple((p.stat().st_mtime_ns if p.exists() else None) for p in (d / "app.yml", d / "app.local.yml"))
    key = (str(d), stamp)
    if _CFG_CACHE.get("key") != key:
        _CFG_CACHE.update(key=key, rls=load())
    return _CFG_CACHE["rls"]


def mode() -> str:
    """off | export (a local run, or an app with apply=export) | query (an app with apply=query)."""
    cfg = _cfg()
    if not cfg.enabled:
        return "off"
    return cfg.apply if is_app() else "export"


def scope_key() -> tuple:
    """Part of every server-side cache key, so one user's answer is never served to another."""
    return ("rls", current_principal()) if mode() == "query" else ()


def _sets_path(base: Path) -> Path:
    from app.core import datasource
    return datasource.sidecar(base, "rls_sets")


def read_sets(base: Path) -> dict:
    try:
        return json.loads(_sets_path(Path(base)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def resolve(base: Path) -> Path:
    """apply=export on the app: the signed-in user's own db file, else the empty-set file."""
    if mode() != "export":
        return base
    if not is_app():
        # A local db that was not cut to the token user's rows must not be served.
        return base if is_filtered(base) else base.with_name(base.stem + ".rls-unfiltered.duckdb")
    sets = read_sets(base)
    name = sets.get(current_principal()) or sets.get(NONE_KEY)
    # No sets built yet: point at a file that does not exist rather than the unfiltered db.
    return base.with_name(name) if name else base.with_name(base.stem + ".rls-none.duckdb")


_FILTERED: dict = {}


def is_filtered(path) -> bool:
    """True when the db at `path` carries the marker filter_into writes (cached per file version)."""
    key = _identity(path)
    if key not in _FILTERED:
        try:
            con = duckdb.connect(str(path), read_only=True)
            try:
                ok = bool(con.execute("SELECT count(*) FROM rls.filtered").fetchone()[0])
            finally:
                con.close()
        except duckdb.Error:
            ok = False
        if len(_FILTERED) > 32:
            _FILTERED.clear()
        _FILTERED[key] = ok
    return _FILTERED[key]


def build_sets(full_db) -> dict:
    """One filtered db per distinct grant set (users with equal grants share it) plus an empty set for
    everyone else, and the {email: file name} map the app reads. Returns the map."""
    from app.core import datasource
    cols = load().columns
    full = Path(full_db)
    con = duckdb.connect(str(full), read_only=True)
    try:
        try:
            users = [r[0] for r in con.execute("SELECT DISTINCT principal FROM rls.entitlements").fetchall()]
        except duckdb.Error:
            users = []
        grants = {u: grants_for(con, u) for u in users}
    finally:
        con.close()
    groups: dict[str, tuple[list, list]] = {grant_key(cols, []): ([], [])}
    for user, g in grants.items():
        if g:
            groups.setdefault(grant_key(cols, g), (g, []))[1].append(user)
    mapping: dict[str, str] = {}
    for key, (g, members) in groups.items():
        dst = full.with_name(f"{full.stem}.rls-{key}.duckdb")
        filter_into(full, dst, g, None, cols)
        for side in ("run_results", "snapshot_manifest"):
            src_side = datasource.sidecar(full, side)
            if src_side.exists():
                shutil.copyfile(src_side, datasource.sidecar(dst, side))
        mapping.update({u: dst.name for u in members})
        if not g:
            mapping[NONE_KEY] = dst.name
    _sets_path(full).write_text(json.dumps(mapping, indent=1), encoding="utf-8")
    return mapping


def set_files(base: Path) -> set[str]:
    """Full paths of the per-grant-set files a db's map names."""
    base = Path(base)
    return {str(base.with_name(n)) for n in read_sets(base).values()}


_GRANTS: dict = {}
_SHARED: dict = {}  # {"ident", "con", "keys"}: one instance, so one buffer pool for every user
_STATUS: dict = {}


def _identity(path) -> tuple:
    from app.core import data
    return data._identity_key(str(path))


def principal_grants(base, principal: str) -> list:
    """Grants of `principal` in the full db at `base`, read once per db file. A failed read is not cached."""
    key = (_identity(base), principal)
    with _LOCK:
        if key in _GRANTS:
            return _GRANTS[key]
    try:
        con = duckdb.connect(str(base), read_only=True)
        try:
            got = grants_for(con, principal)
        finally:
            con.close()
    except duckdb.Error:
        return []
    with _LOCK:
        if len(_GRANTS) > 512:
            _GRANTS.clear()
        _GRANTS[key] = got
    return got


def scoped_connection(base, principal: str) -> duckdb.DuckDBPyConnection:
    """A cursor on one shared in-memory instance, switched to a catalog of views over the full db cut to
    what `principal` may see. Every reader goes through it, so no read path needs its own filter. One
    catalog per distinct grant set; the full db is attached once under an alias no reader uses."""
    from app.core import data
    cols = _cfg().columns
    ident = _identity(base)
    grants = principal_grants(base, principal)
    key = "p_" + grant_key(cols, grants)
    with _LOCK:
        if _SHARED.get("ident") != ident:
            old = _SHARED.get("con")
            if old is not None:
                old.close()  # a newer db file: drop the old handle so it can be deleted
            con = duckdb.connect(":memory:")
            data._cap(con)
            alias = "src_" + os.urandom(6).hex()
            con.execute(f"ATTACH {_lit(str(base))} AS {alias} (READ_ONLY)")
            _SHARED.clear()
            _SHARED.update(ident=ident, con=con, alias=alias, keys=set())
        con, alias = _SHARED["con"], _SHARED["alias"]
        if key not in _SHARED["keys"]:
            _build_catalog(con, alias, key, cols, grants)
            _SHARED["keys"].add(key)
        cur = con.cursor()
    cur.execute(f"USE {key}")
    return cur


def _build_catalog(con, src: str, key: str, cols, grants) -> None:
    """ATTACH an in-memory catalog `key` holding one view per object of `src`; a table the user may not
    read still gets a view, with no rows, so its name never falls through to the unfiltered db."""
    con.execute(f"ATTACH ':memory:' AS {key}")
    where = predicate(cols, grants, "t")
    full = unrestricted(cols, grants)
    objects, colmap, added = _catalog(con, with_views=True, src=src)
    for schema, table, kind in objects:
        ref = f"{src}.{_q(schema)}.{_q(table)}"
        sel = None
        if not (kind == "VIEW" and schema in FILTERED_SCHEMAS):  # a view has no added columns to filter on
            sel = _select_sql(schema, table, colmap[(schema, table)], added.get((schema, table), set()),
                              where, full, None, src, grants)
        if sel is None:
            keep = ", ".join(_q(c) for c in colmap[(schema, table)] if c not in added.get((schema, table), set()))
            sel = f"SELECT {keep} FROM {ref} WHERE FALSE"
        if schema != "main":
            con.execute(f"CREATE SCHEMA IF NOT EXISTS {key}.{_q(schema)}")
        con.execute(f"CREATE VIEW {key}.{_q(schema)}.{_q(table)} AS {sel}")


def _status(path) -> tuple[str | None, str | None]:
    """(user, error) from rls.status of the db at `path`."""
    key = _identity(path)
    if key in _STATUS:
        return _STATUS[key]
    try:
        con = duckdb.connect(str(path), read_only=True)
        try:
            user, error = con.execute('SELECT "user", error FROM rls.status LIMIT 1').fetchone()
        finally:
            con.close()
    except (duckdb.Error, TypeError):
        user, error = None, "Access rules are not loaded in this database; refresh the data."
    if len(_STATUS) > 32:
        _STATUS.clear()
    _STATUS[key] = (user, error)
    return user, error


def meta() -> dict:
    """The /api/meta field: whether access rules apply, who is asking, whether they were granted anything."""
    from app.core import data
    cfg = _cfg()
    if not cfg.enabled:
        return {"enabled": False, "apply": None, "user": None, "granted": True, "error": None}
    base = data._base_db_path()
    out = {"enabled": True, "apply": mode() if is_app() else "export"}
    if mode() == "query":
        user = current_principal()
        error = _status(base)[1]
        return {**out, "user": user or None, "granted": bool(user and principal_grants(base, user)), "error": error}
    served = resolve(base)
    if is_app():
        user = current_principal()
        error = _status(served)[1] if served.exists() else "No per-user data yet; refresh the data."
        return {**out, "user": user or None, "granted": bool(user and user in read_sets(base)), "error": error}
    user, error = _status(served)
    granted = bool(user) and not error
    if granted:
        try:
            con = duckdb.connect(str(served), read_only=True)
            try:
                granted = bool(con.execute("SELECT count(*) FROM rls.entitlements").fetchone()[0])
            finally:
                con.close()
        except duckdb.Error:
            granted = False
    return {**out, "user": user, "granted": granted, "error": error}
