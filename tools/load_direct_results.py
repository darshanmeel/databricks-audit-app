#!/usr/bin/env python3
"""Load a tools/export_direct_results.py folder (or a .zip of one) into a DuckDB file the app can
run against: findings.f_<query_id> per query, dims.* per name (empty stand-ins for one never
exported), direct_export_meta/direct_export_findings_meta, and <db>.run_results.json.

Every timestamp column is normalised to naive UTC on the way in, whether the source is parquet or
CSV and whichever tool wrote it, so a day bucket never shifts with the reading machine's clock.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import datasource  # noqa: E402
from app.core import identity as app_identity  # noqa: E402
from tools.export_direct_results import _is_windowed, apply_masking  # noqa: E402

_QUERY_ID_RE = re.compile(r"[a-z][a-z0-9_]*")
_VALID_WINDOW_DAYS = {0, 7, 30, 90}
_WINDOWED_DAYS = {7, 30, 90}  # window_aliases labels: a snapshot check's window_days is always 0


# The columns app/core/data.py's read_dim_* functions select; db_state() needs dim_workspace to exist.
DIM_STUB_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "dim_workspace": [
        ("workspace_id", "VARCHAR"), ("name", "VARCHAR"), ("url", "VARCHAR"),
        ("env", "VARCHAR"), ("env_source", "VARCHAR"), ("env_reason", "VARCHAR"),
        ("in_snapshot_region", "BOOLEAN"), ("billed_in_snapshot", "BOOLEAN"),
    ],
    "dim_job": [
        ("workspace_id", "VARCHAR"), ("job_id", "VARCHAR"), ("name", "VARCHAR"),
        ("run_as", "VARCHAR"), ("run_as_user_name", "VARCHAR"), ("creator_user_name", "VARCHAR"),
    ],
    "dim_cluster": [
        ("workspace_id", "VARCHAR"), ("cluster_id", "VARCHAR"), ("cluster_name", "VARCHAR"),
        ("cluster_source", "VARCHAR"), ("owned_by", "VARCHAR"),
    ],
    "dim_warehouse": [
        ("workspace_id", "VARCHAR"), ("warehouse_id", "VARCHAR"), ("warehouse_name", "VARCHAR"),
        ("created_by", "VARCHAR"),
    ],
    "dim_pipeline": [
        ("workspace_id", "VARCHAR"), ("pipeline_id", "VARCHAR"), ("pipeline_name", "VARCHAR"),
        ("created_by", "VARCHAR"), ("run_as", "VARCHAR"),
    ],
    "dim_notebook": [
        ("workspace_id", "VARCHAR"), ("notebook_id", "VARCHAR"), ("notebook_path", "VARCHAR"),
    ],
}
DIM_TABLE_NAMES = tuple(DIM_STUB_COLUMNS)

# tags/<name>.parquet -> tags.<name>, the tables app/core/tags.py and app/core/rollup.py read. No
# stub for a missing one -- absence is exactly what "tags not built" means to those.
TAG_TABLE_NAMES = (
    "tag_workspace", "tag_entity", "tag_index",
    "cost_unit", "cost_unit_tag", "cost_reconciliation",
    "perf_unit", "perf_unit_tag", "cost_day", "query_tag_keys", "bill_tag_dates",
)


class LoadConfigError(RuntimeError):
    """A preflight problem, reported before any file is written."""


def _sql_path(path: Path) -> str:
    """A path as the body of a single-quoted SQL literal."""
    return path.as_posix().replace("'", "''")


def db_path_allowed(path: Path) -> bool:
    """Outside the repo, or data/<name>.duckdb (git-ignored) other than the dev build's db_audit.duckdb."""
    resolved = path.resolve()
    if resolved != ROOT and ROOT not in resolved.parents:
        return True
    data_dir = (ROOT / "data").resolve()
    if resolved.parent != data_dir or resolved.suffix != ".duckdb":
        return False
    return resolved != (ROOT / "data" / "db_audit.duckdb").resolve()


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------------------------------
# Reading a results file: parquet or CSV, either way normalised to naive UTC timestamps.
# ------------------------------------------------------------------------------------------


def normalise_timestamps(table: pa.Table) -> pa.Table:
    """Every top-level timestamp column becomes tz-naive timestamp[us] holding the UTC instant,
    so a day bucket never shifts with the reading machine's local time zone."""
    arrays, fields = [], []
    for field in table.schema:
        col = table.column(field.name)
        if pa.types.is_timestamp(field.type):
            if field.type.tz is not None:
                col = pc.cast(col, pa.timestamp("us", tz="UTC"))
            col = col.cast(pa.timestamp("us"))
            fields.append(pa.field(field.name, pa.timestamp("us")))
        else:
            fields.append(field)
        arrays.append(col)
    return pa.Table.from_arrays(arrays, schema=pa.schema(fields))


def _create_from_parquet(con: duckdb.DuckDBPyConnection, target: str, path: Path, where: str | None = None) -> list[str]:
    """CREATE `target` with DuckDB reading the file itself, so a multi-million-row table never sits
    in Python memory; timestamps become naive UTC as in normalise_timestamps. Returns the columns."""
    schema = pq.read_schema(path)
    casts = [f'CAST("{f.name}" AS TIMESTAMP) AS "{f.name}"' for f in schema if pa.types.is_timestamp(f.type)]
    replace = f" REPLACE ({', '.join(casts)})" if casts else ""
    con.execute(f"CREATE OR REPLACE TABLE {target} AS SELECT *{replace} FROM read_parquet('{_sql_path(path)}')"
                + (f" WHERE {where}" if where else ""))
    return schema.names


def _tag_entity_keys(con: duckdb.DuckDBPyConnection, entity_path: Path, index_path: Path | None, settings: dict) -> list[str] | None:
    """The tag keys tags.tag_entity keeps: settings load.tag_keys, or the load.top_tag_keys keys with
    the most spend in tag_index; mandatory and top tags always. None when that is every key anyway."""
    from app.core import config as app_config
    from app.core.tag_compliance import key_variants

    def norm(k: object) -> str:
        return re.sub(r"[ _-]+", "", str(k).lower())

    src = _sql_path(entity_path)
    every = {r[0] for r in con.execute(f"SELECT DISTINCT tag_key FROM read_parquet('{src}') WHERE tag_key IS NOT NULL").fetchall()}
    load = settings["load"]
    if load["tag_keys"]:
        chosen = [norm(k) for k in load["tag_keys"]]
    else:
        spend = ""
        if index_path is not None and index_path.suffix == ".parquet":
            spend = f"LEFT JOIN (SELECT tag_key, SUM(dbus) AS d FROM read_parquet('{_sql_path(index_path)}') GROUP BY 1) i USING (tag_key) "
        chosen = [r[0] for r in con.execute(
            f"SELECT e.tag_key FROM (SELECT tag_key, COUNT(*) AS n FROM read_parquet('{src}') WHERE tag_key IS NOT NULL GROUP BY 1) e "
            f"{spend}ORDER BY {'i.d DESC NULLS LAST, ' if spend else ''}e.n DESC, e.tag_key LIMIT {int(load['top_tag_keys'])}"
        ).fetchall()]
    always = [norm(k) for k in [*(settings.get("mandatory_tag_keys") or []), *app_config.load_top_tags()]]
    keep = set(key_variants(sorted(set(chosen) | set(always))))
    return None if every <= keep else sorted(keep)


def _add_workspace_env(table: pa.Table) -> pa.Table:
    """env/env_source/env_reason by workspace name alone (app.core.envmap.classify_env); no seed
    override or tag hint in this mode, unlike the dbt-built dim -- a documented gap, not a crash."""
    from app.core.envmap import classify_env

    names = table.column("name").to_pylist() if "name" in table.column_names else [None] * table.num_rows
    envs, sources, reasons = [], [], []
    for name in names:
        env = classify_env(name) if name else "unknown"
        if env == "unknown":
            envs.append("unknown")
            sources.append("none")
            reasons.append("no workspace name matched a known env pattern (seed and tag overrides need Local build)")
        else:
            envs.append(env)
            sources.append("name")
            reasons.append(f'workspace name "{name}" matched the {env} word list')
    table = table.append_column("env", pa.array(envs, type=pa.string()))
    table = table.append_column("env_source", pa.array(sources, type=pa.string()))
    return table.append_column("env_reason", pa.array(reasons, type=pa.string()))


def _add_workspace_attributes(con: duckdb.DuckDBPyConnection) -> None:
    """<key>/<key>_share/<key>_coverage/<key>_reason per config/tag_aliases.yml top tag, read off
    tags.tag_workspace -- the columns the dbt-built dim has, so every top tag can be a filter."""
    from app.core import config as app_config
    from app.core.tag_keys import normalize_tag_key

    cols = {r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'dims' AND table_name = 'dim_workspace'"
    ).fetchall()}
    no_usage = "d.billed_in_snapshot IS FALSE" if "billed_in_snapshot" in cols else "FALSE"
    for key in app_config.load_top_tags():
        if key in cols:
            continue
        con.execute(
            f"""
            CREATE OR REPLACE TABLE dims.dim_workspace AS
            SELECT d.*,
                   CASE WHEN h.tag_value IS NULL THEN CASE WHEN {no_usage} THEN 'no_usage' ELSE 'not_tagged' END
                        WHEN h.tag_value = '__untagged__' THEN 'not_tagged'
                        WHEN h.tag_value = '__mixed__' THEN 'mixed'
                        ELSE h.tag_value END AS "{key}",
                   h.share AS "{key}_share",
                   h.coverage AS "{key}_coverage",
                   CASE WHEN h.reason IS NOT NULL THEN h.reason
                        WHEN {no_usage} THEN 'no billed DBUs in this snapshot'
                        ELSE 'no billed usage carries this tag' END AS "{key}_reason"
            FROM dims.dim_workspace d
            LEFT JOIN (
                SELECT CAST(workspace_id AS VARCHAR) AS workspace_id, tag_value, share, coverage, reason
                FROM tags.tag_workspace
                WHERE tag_key = ?
            ) h ON h.workspace_id = CAST(d.workspace_id AS VARCHAR)
            """,
            [normalize_tag_key(key)],
        )


def _read_csv_table(path: Path) -> pa.Table:
    """DuckDB's own CSV sniffing, with ids forced to VARCHAR so they still join to the dims."""
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        header = next(csv.reader(f), [])
    types: dict[str, str] = {}
    if "window_days" in header:
        types["window_days"] = "INTEGER"
    for col in header:
        if col == "id" or col.endswith("_id"):
            types[col] = "VARCHAR"
    types_sql = ", ".join(f"'{k}': '{v}'" for k, v in types.items())
    con = duckdb.connect()
    try:
        con.execute("SET TimeZone = 'UTC'")  # a naive/`Z` mix must not sniff as connection-local
        sql = (
            f"SELECT * FROM read_csv('{_sql_path(path)}', header=true, auto_detect=true, "
            f"sample_size=-1, nullstr='', types={{{types_sql}}})"
        )
        return con.execute(sql).to_arrow_table()
    finally:
        con.close()


def _read_table(path: Path) -> pa.Table:
    return _read_csv_table(path) if path.suffix == ".csv" else pq.read_table(path)


def _find_data_file(dirpath: Path, stem: str) -> Path | None:
    """<dirpath>/<stem>.parquet, else <dirpath>/<stem>.csv; parquet wins when both exist."""
    for ext in ("parquet", "csv"):
        candidate = dirpath / f"{stem}.{ext}"
        if candidate.exists():
            return candidate
    return None


def _validate_check_table(table: pa.Table) -> str | None:
    """None when table looks like a real check result, else the reason it was rejected."""
    if "window_days" not in table.column_names:
        return "missing column window_days"
    values = {v for v in table.column("window_days").to_pylist() if v is not None}
    if not values <= _VALID_WINDOW_DAYS:
        return "window_days must be 0, 7, 30 or 90"
    return None


# ------------------------------------------------------------------------------------------
# A results folder made elsewhere: a plain folder, or a .zip of one (results made elsewhere is
# import mode -- no manifest.json required; a check/name file is loaded on its own merits).
# ------------------------------------------------------------------------------------------


def _is_zip(path: Path) -> bool:
    return path.suffix.lower() == ".zip"


def safe_unzip(zip_path: Path, dest: Path) -> None:
    """Extract zip_path into dest; refuses any entry that could write outside dest."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise LoadConfigError(f"{zip_path.name} is not a readable zip file") from exc
    with zf:
        for member in zf.namelist():
            norm = member.replace("\\", "/")
            if norm.startswith("/") or ":" in norm or ".." in norm.split("/"):
                raise LoadConfigError(f"unsafe path in {zip_path.name}: {member}")
        zf.extractall(dest)


def _content_root(root: Path) -> Path:
    """Descend into a single wrapping folder, e.g. a zip whose entries all sit under one top
    folder -- ignoring dot-entries and __MACOSX (a macOS Finder zip's own resource-fork junk)."""
    if (root / "manifest.json").exists() or any(
        (root / name).is_dir() for name in ("findings", "checks", "dims", "names", "tags")
    ):
        return root
    entries = [p for p in root.iterdir() if not p.name.startswith(".") and p.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir():
        return _content_root(entries[0])
    return root


def newest_export(results: Path) -> Path:
    """The newest finished export: results/ itself or one of its <time> folders (first_run and
    Refresh each add one), by when its manifest was written; results/ when none has finished."""
    runs = [results, *results.glob("*")] if results.is_dir() else []
    done = [p for p in runs if (p / "manifest.json").is_file()]
    return max(done, key=lambda p: (p / "manifest.json").stat().st_mtime) if done else results


def _checks_dir(root: Path) -> Path:
    return root / "findings" if (root / "findings").is_dir() else root / "checks"


def _names_dir(root: Path) -> Path:
    return root / "dims" if (root / "dims").is_dir() else root / "names"


def _tags_dir(root: Path) -> Path:
    return root / "tags"


def _newest_mtime_iso(*dirs: Path) -> str | None:
    latest: float | None = None
    for d in dirs:
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if p.is_file():
                latest = p.stat().st_mtime if latest is None else max(latest, p.stat().st_mtime)
    return dt.datetime.fromtimestamp(latest, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if latest else None


def _zip_data_as_of(zip_path: Path) -> str | None:
    """The newest ZipInfo.date_time among findings/checks/dims/names entries, read before
    extraction -- extractall does not restore an entry's own mtime, so every extracted file
    otherwise reads as "now" and a manifest-less zip would be dated the day it was loaded, not
    the day it was made."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError):
        return None
    latest: tuple | None = None
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            parts = info.filename.replace("\\", "/").split("/")
            if any(p.startswith(".") or p == "__MACOSX" for p in parts):
                continue
            if not any(seg in ("findings", "checks", "dims", "names", "tags") for seg in parts[:-1]):
                continue
            if latest is None or info.date_time > latest:
                latest = info.date_time
    if latest is None:
        return None
    return dt.datetime(*latest, tzinfo=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_run_results(
    path: Path, query_ids: list[str], failed: dict[str, str | None], as_of: str | None,
    truncated: set[str] | None = None, max_rows: int | None = None,
) -> None:
    """A dbt run_results.json shape: success per loaded id (with a truncation note when the export
    capped it), error (with its message) per failed id."""
    truncated = truncated or set()
    results = [
        {
            "status": "success",
            "message": f"kept the first {max_rows} rows per window" if qid in truncated and max_rows else None,
            "execution_time": 0.0,
            "adapter_response": {},
            "unique_id": f"model.databricks_direct_export.f_{qid}",
        }
        for qid in query_ids
    ] + [
        {
            "status": "error",
            "message": message,
            "execution_time": 0.0,
            "adapter_response": {},
            "unique_id": f"model.databricks_direct_export.f_{qid}",
        }
        for qid, message in failed.items()
    ]
    payload = {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/run-results/v6.json",
            "dbt_version": "synthesised-by-tools/load_direct_results.py",
            "generated_at": as_of or _utc_now_iso(),
        },
        "results": results,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_folder(src: Path, db_path: Path) -> dict:
    """Build db_path from src -- a results folder, or a .zip of one -- and return a summary for the CLI."""
    if not src.exists():
        raise LoadConfigError(f"{src} does not exist")

    unpacked: Path | None = None
    zip_as_of: str | None = None
    try:
        if src.is_file() and _is_zip(src):
            zip_as_of = _zip_data_as_of(src)
            unpacked = Path(tempfile.mkdtemp(prefix="audit_import_"))
            safe_unzip(src, unpacked)
            root = _content_root(unpacked)
        else:
            root = _content_root(src)
        return _load_root(root, db_path, zip_as_of=zip_as_of)
    finally:
        if unpacked is not None:
            shutil.rmtree(unpacked, ignore_errors=True)


def _infer_window_aliases(checks_dir: Path) -> dict[int, int]:
    """No manifest: the windows the check files hold decide the rest -- each missing longer window
    reuses the longest one present below it (a 7-day export fills 30 and 90), and the app marks
    those partial from the days the data actually spans."""
    present: set[int] = set()
    for path in checks_dir.glob("*.parquet") if checks_dir.is_dir() else []:
        if "window_days" in pq.read_schema(path).names:
            col = pq.read_table(path, columns=["window_days"]).column("window_days")
            present |= {int(v) for v in pc.unique(col).to_pylist() if v in _WINDOWED_DAYS}
    aliases: dict[int, int] = {}
    for label in sorted(_WINDOWED_DAYS - present):
        below = [w for w in present if w < label]
        if below:
            aliases[label] = max(below)
    return aliases


def _load_root(root: Path, db_path: Path, *, zip_as_of: str | None = None) -> dict:
    manifest_path = root / "manifest.json"
    has_manifest = manifest_path.exists()
    manifest: dict = {}
    findings_meta: dict[str, dict] = {}
    if has_manifest:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if "tables" in manifest:
            raise LoadConfigError(
                f"{root} looks like a system-tables snapshot (has a top-level 'tables' key), not "
                "a tools/export_direct_results.py results folder -- use Local build instead"
            )
        findings_meta = dict(manifest.get("findings", {}) or {})

    checks_dir = _checks_dir(root)
    names_dir = _names_dir(root)
    max_rows = manifest.get("max_rows")
    # A dim listed here is the current run's own; one left off (a stale file from an earlier,
    # partly-failed export into the same folder) must not be picked up. None (no manifest, or an
    # older manifest shape) means "load whatever is present", the pre-existing import-mode rule.
    dims_declared = (manifest.get("dims") or {}).get("available")
    dims_declared = set(dims_declared) if isinstance(dims_declared, list) else None
    mask = app_identity.masking_enabled()
    # {alias label: source label} -- absent on a manifest from before window_aliases existed.
    # A hand-edited or corrupt manifest is skipped entry by entry rather than crashing the load.
    window_aliases: dict[int, int] = {}
    for alias_label, source_label in (manifest.get("window_aliases") or {}).items():
        try:
            alias_label, source_label = int(alias_label), int(source_label)
        except (TypeError, ValueError):
            continue
        if {alias_label, source_label} <= _WINDOWED_DAYS and alias_label > source_label:
            window_aliases[alias_label] = source_label

    if not has_manifest:
        # Results made elsewhere: every recognised file is loaded as "ok"; truncation is unknown.
        if checks_dir.is_dir():
            for path in sorted(checks_dir.glob("*.parquet")) + sorted(checks_dir.glob("*.csv")):
                qid = path.stem
                if qid not in findings_meta and _QUERY_ID_RE.fullmatch(qid):
                    findings_meta[qid] = {"status": "ok"}
        if not findings_meta:
            raise LoadConfigError(f"no recognised check result files found in {root}")
        window_aliases = _infer_window_aliases(checks_dir)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_name(db_path.name + ".tmp")
    tmp_wal = tmp_path.with_name(tmp_path.name + ".wal")
    for stray in (tmp_path, tmp_wal):
        if stray.exists():
            stray.unlink()

    loaded: list[str] = []
    not_built: list[str] = []
    missing_file: list[str] = []
    failed_load: list[str] = []
    failed_on_databricks: dict[str, str | None] = {}
    truncated: set[str] = set()
    truncated_windows_by_qid: dict[str, list[int]] = {}

    try:
        con = duckdb.connect(str(tmp_path))
        try:
            con.execute("SET TimeZone = 'UTC'")
            # DuckDB's default cap is 80% of RAM, however little is free; below it, finished tables
            # go to disk instead of staying in memory, so a busy laptop doesn't run out mid-load.
            from app.core import config as app_config
            settings = app_config.load_settings()
            threads = int(settings["load"]["threads"])
            con.execute(f"SET memory_limit = '{settings['load']['memory_limit']}'")
            con.execute(f"SET threads = {threads}")
            con.execute("CREATE SCHEMA IF NOT EXISTS findings")
            con.execute("CREATE SCHEMA IF NOT EXISTS dims")

            for qid in sorted(findings_meta):
                entry = findings_meta[qid]
                if entry.get("status") not in ("ok", "empty"):
                    not_built.append(qid)
                    if entry.get("status") == "failed":
                        failed_on_databricks[qid] = entry.get("error")
                    continue
                if not _QUERY_ID_RE.fullmatch(qid):
                    missing_file.append(qid)  # not a real registry id -- refuse rather than splice it
                    continue
                data_path = _find_data_file(checks_dir, qid)
                if data_path is None:
                    missing_file.append(qid)
                    continue
                try:
                    table = normalise_timestamps(_read_table(data_path))
                    if mask:
                        table = apply_masking(table)
                except Exception as exc:  # noqa: BLE001 -- a bad file must not stop the rest of the load
                    failed_load.append(qid)
                    print(f"warning: could not read {data_path} ({exc}) -- skipped", file=sys.stderr)
                    continue
                problem = _validate_check_table(table)
                if problem:
                    failed_on_databricks[qid] = problem
                    not_built.append(qid)
                    continue
                con.register("_incoming", table)
                try:
                    con.execute(f'CREATE OR REPLACE TABLE findings."f_{qid}" AS SELECT * FROM _incoming')
                finally:
                    con.unregister("_incoming")
                if window_aliases and _is_windowed(qid):
                    # The export wrote only the source label's rows (no row duplication on
                    # Databricks); an alias label reuses them under its own window_days here.
                    for alias_label, source_label in window_aliases.items():
                        con.execute(
                            f'INSERT INTO findings."f_{qid}" SELECT * REPLACE '
                            f'({alias_label} AS window_days) FROM findings."f_{qid}" '
                            f'WHERE window_days = {source_label}'
                        )
                loaded.append(qid)
                if entry.get("truncated"):
                    truncated.add(qid)
                truncated_windows_by_qid[qid] = entry.get("truncated_windows") or []

            dims_available: list[str] = []
            for name in DIM_TABLE_NAMES:
                data_path = None
                if dims_declared is None or name in dims_declared:
                    data_path = _find_data_file(names_dir, name) if names_dir.is_dir() else None
                if data_path is not None:
                    try:
                        table = normalise_timestamps(_read_table(data_path))
                        if name == "dim_workspace" and "env" not in table.column_names:
                            table = _add_workspace_env(table)
                        con.register("_incoming", table)
                        try:
                            con.execute(f'CREATE OR REPLACE TABLE dims."{name}" AS SELECT * FROM _incoming')
                        finally:
                            con.unregister("_incoming")
                        dims_available.append(name)
                        continue
                    except Exception as exc:  # noqa: BLE001 -- a bad names file must not crash the load
                        print(
                            f"warning: could not read {data_path} ({exc}) -- using an empty "
                            f"stand-in for {name}", file=sys.stderr,
                        )
                cols_sql = ", ".join(f'"{c}" {t}' for c, t in DIM_STUB_COLUMNS[name])
                con.execute(f'CREATE OR REPLACE TABLE dims."{name}" ({cols_sql})')
            dims_missing = [n for n in DIM_TABLE_NAMES if n not in dims_available]

            # tags/<name>.parquet -> tags.<name>. No folder at all -> no schema, nothing loaded.
            tags_root = _tags_dir(root)
            tags_declared = (manifest.get("tags") or {}).get("available")
            tags_declared = set(tags_declared) if isinstance(tags_declared, list) else None
            tags_available: list[str] = []
            if tags_root.is_dir():
                con.execute("CREATE SCHEMA IF NOT EXISTS tags")
                for name in TAG_TABLE_NAMES:
                    if tags_declared is not None and name not in tags_declared:
                        continue
                    data_path = _find_data_file(tags_root, name)
                    if data_path is None:
                        continue
                    try:
                        where = None
                        if name in ("tag_entity", "bill_tag_dates") and data_path.suffix == ".parquet":
                            keep = _tag_entity_keys(con, _find_data_file(tags_root, "tag_entity") or data_path, _find_data_file(tags_root, "tag_index"), settings)
                            if keep is not None:
                                # Unity Catalog tags (governance, PII) are small and always kept.
                                where = "starts_with(entity_type, 'uc_') OR tag_key IN (" + ", ".join("'" + k.replace("'", "''") + "'" for k in keep) + ")"
                                print(f"tags.{name}: keeping {len(keep)} tag keys ({', '.join(keep)}); see load in config/settings.yml")
                        if data_path.suffix == ".parquet":
                            try:
                                columns = _create_from_parquet(con, f'tags."{name}"', data_path, where)
                            except duckdb.OutOfMemoryException:
                                # One thread needs the least memory; slower, but it finishes on a busy laptop.
                                con.execute("SET threads = 1")
                                try:
                                    columns = _create_from_parquet(con, f'tags."{name}"', data_path, where)
                                finally:
                                    con.execute(f"SET threads = {threads}")
                        else:
                            table = normalise_timestamps(_read_table(data_path))
                            con.register("_incoming", table)
                            try:
                                con.execute(f'CREATE OR REPLACE TABLE tags."{name}" AS SELECT * FROM _incoming')
                            finally:
                                con.unregister("_incoming")
                            columns = table.column_names
                        if window_aliases and "window_days" in columns:
                            # A windowed tag table (cost_unit, perf_unit, ...) carries only its
                            # window SOURCE rows, same as a windowed check's findings.f_<id> --
                            # copy them under each alias label exactly the same way. A
                            # whole-snapshot tag table (tag_workspace, tag_entity, tag_index) has
                            # no window_days column at all and is never touched here.
                            for alias_label, source_label in window_aliases.items():
                                con.execute(
                                    f'INSERT INTO tags."{name}" SELECT * REPLACE '
                                    f'({alias_label} AS window_days) FROM tags."{name}" '
                                    f'WHERE window_days = {source_label}'
                                )
                        tags_available.append(name)
                    except Exception as exc:  # noqa: BLE001 -- a bad tags file must not crash the load
                        print(
                            f"warning: could not read {data_path} ({exc}) -- tags.{name} not built",
                            file=sys.stderr,
                        )
                # cost_reconciliation ships without unit_usd/quantity/unpriced_quantity; add them
                # from the loaded cost_unit, an independent check rather than a copy of its formula.
                if "cost_reconciliation" in tags_available:
                    if "cost_unit" in tags_available:
                        con.execute(
                            'CREATE OR REPLACE TABLE tags."cost_reconciliation" AS '
                            "SELECT r.*, COALESCE(u.unit_usd, 0) AS unit_usd, "
                            "COALESCE(u.unit_quantity, 0) AS unit_quantity, "
                            "COALESCE(u.unit_unpriced_quantity, 0) AS unit_unpriced_quantity "
                            'FROM tags."cost_reconciliation" r '
                            "LEFT JOIN (SELECT window_days, usage_unit, SUM(usd) AS unit_usd, "
                            "SUM(quantity) AS unit_quantity, SUM(unpriced_quantity) AS unit_unpriced_quantity "
                            'FROM tags."cost_unit" GROUP BY window_days, usage_unit) u '
                            "ON u.window_days = r.window_days AND u.usage_unit = r.usage_unit"
                        )
                    else:
                        con.execute(
                            'CREATE OR REPLACE TABLE tags."cost_reconciliation" AS '
                            "SELECT *, CAST(0 AS DOUBLE) AS unit_usd, CAST(0 AS DOUBLE) AS unit_quantity, "
                            'CAST(0 AS DOUBLE) AS unit_unpriced_quantity FROM tags."cost_reconciliation"'
                        )
                if {"perf_unit", "perf_unit_tag", "bill_tag_dates"} <= set(tags_available):
                    from app.core.tag_bill import apply_bill_to_perf
                    print(f"tags.perf_unit_tag: {apply_bill_to_perf(con)} compute tags taken from the bill")
            tags_missing = [n for n in TAG_TABLE_NAMES if n not in tags_available] if tags_root.is_dir() else []
            if "tag_workspace" in tags_available:
                _add_workspace_attributes(con)

            # policies/abac_policies.parquet (SHOW POLICIES) -> governance.abac_policies, with what
            # the export asked in governance.abac_policies_info. No folder: an older export.
            policies_info = manifest.get("policies")
            pol_path = _find_data_file(root / "policies", "abac_policies") if (root / "policies").is_dir() else None
            if pol_path is not None or policies_info:
                con.execute("CREATE SCHEMA IF NOT EXISTS governance")
                con.execute("CREATE OR REPLACE TABLE governance.abac_policies_info AS SELECT ? AS info", [json.dumps(policies_info)])
            if pol_path is not None:
                try:
                    if pol_path.suffix == ".parquet":
                        _create_from_parquet(con, "governance.abac_policies", pol_path)
                    else:
                        table = _read_table(pol_path)
                        con.register("_incoming", table)
                        try:
                            con.execute("CREATE OR REPLACE TABLE governance.abac_policies AS SELECT * FROM _incoming")
                        finally:
                            con.unregister("_incoming")
                except Exception as exc:  # noqa: BLE001 -- a bad policies file must not crash the load
                    print(f"warning: could not read {pol_path} ({exc}) -- governance.abac_policies not built", file=sys.stderr)

            as_of = manifest.get("as_of") or zip_as_of or _newest_mtime_iso(checks_dir, names_dir, tags_root)

            con.execute(
                "CREATE OR REPLACE TABLE direct_export_meta ("
                "as_of VARCHAR, git_commit VARCHAR, tool_version VARCHAR, catalog VARCHAR, "
                "max_rows BIGINT, windows VARCHAR, window_coverage VARCHAR, window_aliases VARCHAR, "
                "tags_sources_not_exported VARCHAR, as_of_date VARCHAR, includes_today BOOLEAN)"
            )
            windows = manifest.get("windows")
            window_coverage = manifest.get("window_coverage")  # absent on an older manifest -> NULL
            window_aliases_raw = manifest.get("window_aliases")  # absent -> NULL, same rule
            tags_sources_not_exported = (manifest.get("tags") or {}).get("sources_not_exported")
            con.execute(
                "INSERT INTO direct_export_meta VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    as_of, manifest.get("git_commit"), manifest.get("tool_version"),
                    manifest.get("catalog"), max_rows,
                    json.dumps(windows) if windows is not None else None,
                    json.dumps(window_coverage) if window_coverage is not None else None,
                    json.dumps(window_aliases_raw) if window_aliases_raw is not None else None,
                    json.dumps(tags_sources_not_exported) if tags_sources_not_exported else None,
                    manifest.get("as_of_date"), bool(manifest.get("includes_today")),
                ],
            )

            con.execute(
                "CREATE OR REPLACE TABLE direct_export_findings_meta "
                "(query_id VARCHAR, truncated BOOLEAN, max_rows BIGINT, truncated_windows VARCHAR)"
            )
            if loaded:
                con.executemany(
                    "INSERT INTO direct_export_findings_meta VALUES (?, ?, ?, ?)",
                    [
                        [qid, qid in truncated, max_rows, json.dumps(truncated_windows_by_qid.get(qid, []))]
                        for qid in loaded
                    ],
                )
        finally:
            con.close()
        if tmp_wal.exists():  # DuckDB checkpoints on a clean close; a survivor would be replayed
            tmp_wal.unlink()
        os.replace(tmp_path, db_path)
    except Exception:
        for stray in (tmp_path, tmp_wal):
            if stray.exists():
                stray.unlink()
        raise
    db_wal = db_path.with_name(db_path.name + ".wal")  # a leftover from before this db existed
    if db_wal.exists():
        db_wal.unlink()

    run_results_path = datasource.sidecar(db_path, "run_results")
    _write_run_results(run_results_path, loaded, failed_on_databricks, as_of, truncated, max_rows)

    return {
        "db_path": db_path,
        "run_results_path": run_results_path,
        "loaded": loaded,
        "not_built": not_built,
        "missing_file": missing_file,
        "failed_load": failed_load,
        "failed_on_databricks": failed_on_databricks,
        "dims_available": dims_available,
        "dims_missing": dims_missing,
        "dims_note": (manifest.get("dims") or {}).get("note"),
        "tags_available": tags_available,
        "tags_missing": tags_missing,
        "tags_note": (manifest.get("tags") or {}).get("note"),
        "tags_sources_not_exported": tags_sources_not_exported,
        "as_of": as_of,
        "truncated": sorted(truncated),
        "max_rows": max_rows,
    }


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="load_direct_results.py",
        description=(
            "Load a tools/export_direct_results.py results folder, or a .zip of one made "
            "elsewhere, into a DuckDB file the app can boot against."
        ),
    )
    ap.add_argument(
        "src", metavar="SRC", nargs="?", default=None,
        help="the results folder or .zip (default: the newest export in the data folder's "
             "results/ -- see app/core/datasource.py)",
    )
    ap.add_argument(
        "--db", metavar="PATH",
        help="DuckDB file to create (replaced if it exists); must be outside the repo, or under "
             "the git-ignored data/ folder (default: the data folder's audit.duckdb)",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    src = Path(args.src).resolve() if args.src else newest_export(datasource.results_dir())
    db_path = Path(args.db).resolve() if args.db else datasource.db_path()

    if not db_path_allowed(db_path):
        print(
            f"error: {db_path} is inside the repository (outside the git-ignored data/ folder). "
            "Choose a path outside the repo, or under data/.",
            file=sys.stderr,
        )
        return 2

    try:
        result = load_folder(src, db_path)
    except LoadConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # Loaded into the data folder: point the app at it, so a stale Refresh pointer never shadows
    # this load and a plain `python -m app.api` opens what was just written.
    data_dir = datasource.data_dir().resolve()
    resolved_db = db_path.resolve()
    if resolved_db == data_dir or data_dir in resolved_db.parents:
        datasource.set_current_db(resolved_db)

    print(f"loaded {len(result['loaded'])} finding(s) into {result['db_path']}")
    if result["not_built"]:
        failed_ids = sorted(result["failed_on_databricks"])
        not_run_ids = [q for q in result["not_built"] if q not in result["failed_on_databricks"]]
        if failed_ids:
            print(f"{len(failed_ids)} failed or rejected (app shows these as failed): " + ", ".join(failed_ids))
        if not_run_ids:
            print(
                f"{len(not_run_ids)} not run this export (app shows these as not built): "
                + ", ".join(not_run_ids)
            )
    if result["missing_file"]:
        print(
            f"{len(result['missing_file'])} recorded as exported but their result file is "
            "missing, skipped: " + ", ".join(result["missing_file"])
        )
    if result["failed_load"]:
        print(
            f"{len(result['failed_load'])} had a result file but failed to load, skipped (see "
            "the warning(s) above): " + ", ".join(result["failed_load"])
        )
    if result["truncated"]:
        print(f"{len(result['truncated'])} capped at {result['max_rows']} rows per window: " + ", ".join(result["truncated"]))
    if result["dims_available"]:
        print(f"names loaded: {', '.join(result['dims_available'])}")
    if result["dims_missing"]:
        print(
            f"names not available (empty stand-ins created so the app still boots -- ids show "
            f"instead of names): {', '.join(result['dims_missing'])}"
        )
    if result["tags_available"]:
        print(f"tags loaded: {', '.join(result['tags_available'])}")
    if result["tags_missing"]:
        print(f"tags not built (tag search, the Tag filter and the rollup say so): {', '.join(result['tags_missing'])}")
    if result.get("tags_sources_not_exported"):
        print(f"tag sources not exported (degraded, not failed): {', '.join(result['tags_sources_not_exported'])}")
    print(f"run-results equivalent written to {result['run_results_path']}")
    print()
    if db_path == datasource.db_path():
        print("Run the app against this data:  python -m app.api")
    else:
        print("Run the app against this data:")
        print(f'  set "AUDIT_DB={result["db_path"]}" && python -m app.api')
        print(f'  $env:AUDIT_DB="{result["db_path"]}"; python -m app.api')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
