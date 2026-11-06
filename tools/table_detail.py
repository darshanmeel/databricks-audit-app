"""tools/table_detail.py

Size, file count and column count of every table in every catalog the export can read: DESCRIBE
DETAIL per Delta table, many at once (one connection per thread), because thousands of them one by
one take hours. Tables that haven't changed since the last export keep last run's figures.
system.storage.table_metrics_history would give the same per day, but it is often empty.

Each table described also gets DESCRIBE HISTORY (its last 100 writes, merges, deletes, OPTIMIZE and
VACUUM runs, who ran them and when) in the same pass.

With settings table_sizes source: stored, nothing is described or listed: the Data usage sizes
job's tables (dashboards sizes_schema) are read and written to the same files.

Called by tools/export_direct_results.py after the checks; writes storage/table_detail.parquet and
storage/table_history.parquet.
"""
from __future__ import annotations

import concurrent.futures
import datetime
import threading
import time
from pathlib import Path
from typing import Callable

import pyarrow as pa
import pyarrow.parquet as pq

from app.core.usage import skipped_catalog as skipped
FILE = "table_detail.parquet"

SCHEMA = pa.schema([
    ("table_catalog", pa.string()), ("table_schema", pa.string()), ("table_name", pa.string()),
    ("table_type", pa.string()), ("data_source_format", pa.string()), ("last_altered", pa.string()),
    ("column_count", pa.int64()), ("size_in_bytes", pa.int64()), ("num_files", pa.int64()),
    ("last_modified", pa.string()), ("detail", pa.string()), ("created", pa.string()), ("owner", pa.string()),
])
# detail: described | reused (unchanged since the last export) | view | not_delta | not_asked
# (over max_tables) | failed: <first line of the error>

HISTORY_FILE = "table_history.parquet"
HISTORY_LIMIT = 100  # the most recent operations kept per table
HISTORY_SCHEMA = pa.schema([
    ("table_full_name", pa.string()), ("version", pa.int64()), ("timestamp", pa.string()),
    ("operation", pa.string()), ("user_name", pa.string()), ("metrics", pa.string()),
])
# the operation metrics worth a glance, in this order; files and bytes give what OPTIMIZE and VACUUM did
_METRIC_KEYS = ("numOutputRows", "numTargetRowsInserted", "numTargetRowsUpdated", "numTargetRowsDeleted",
                "numDeletedRows", "numAddedFiles", "numRemovedFiles", "numFilesAdded", "numFilesRemoved",
                "numTargetFilesAdded", "numTargetFilesRemoved", "numFiles", "numDeletedFiles", "numFilesToDelete",
                "sizeOfDataToDelete", "numAddedBytes", "numRemovedBytes", "numTargetBytesAdded", "numTargetBytesRemoved",
                "numOutputBytes")


def _metrics(m) -> str | None:
    if not m:
        return None
    if isinstance(m, str):
        return m[:600]
    items = dict(m) if not isinstance(m, list) else dict(m)
    return ", ".join(f"{k}={items[k]}" for k in _METRIC_KEYS if items.get(k) not in (None, "", "0"))[:600] or None


def _previous_history(path: Path | None) -> dict[str, list[dict]]:
    if path is None or not path.is_file():
        return {}
    try:
        rows = pq.read_table(str(path)).to_pylist()
    except Exception:  # noqa: BLE001 -- an unreadable old file just means no history kept over
        return {}
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["table_full_name"], []).append(r)
    return out

_LIST_SQL = """
SELECT t.table_catalog, t.table_schema, t.table_name, t.table_type, t.data_source_format,
       CAST(t.last_altered AS STRING) AS last_altered, c.column_count,
       CAST(t.created AS STRING) AS created, t.table_owner AS owner
FROM {tables_from} t
LEFT JOIN (
  SELECT table_catalog, table_schema, table_name, COUNT(*) AS column_count
  FROM {columns_from}
  GROUP BY table_catalog, table_schema, table_name
) c ON c.table_catalog = t.table_catalog AND c.table_schema = t.table_schema AND c.table_name = t.table_name
WHERE t.table_schema <> 'information_schema'
"""


def _list_from() -> dict[str, str]:
    """Where the table and volume lists come from: settings table_sizes tables_from, columns_from, volumes_from."""
    from app.core import config

    cfg = config.table_sizes()
    return {k: cfg[k] for k in ("tables_from", "columns_from", "volumes_from")}


def _stored() -> dict[str, str] | None:
    """settings table_sizes source: stored gives the sizes job's tables (dashboards sizes_schema), describe None."""
    from app.core import config

    if config.table_sizes()["source"] != "stored":
        return None
    d = config.dashboards()
    return {k: f"{d['sizes_schema']}.{name}" for k, name in d["sizes_tables"].items()}


# the Data usage sizes job's tables (tools/direct_data_usage.py collect_sizes_notebook)
_STORED_SIZES_SQL = """
SELECT `catalog`, `schema`, table_name, size_in_bytes, num_files, last_modified, detail,
       CAST(collected_at AS STRING) AS collected_at
FROM {table_sizes}
"""
_STORED_HISTORY_SQL = """
SELECT `catalog`, `schema`, table_name, version, CAST(`timestamp` AS STRING) AS `timestamp`, operation, user_name,
       rows_changed, bytes_written, files_added, files_removed, bytes_added, bytes_removed, freed_bytes
FROM {table_history}
"""
_STORED_VOLUMES_SQL = """
SELECT s.`catalog` AS volume_catalog, s.`schema` AS volume_schema, s.volume_name, v.volume_type, s.files,
       s.size_in_bytes, s.truncated, s.detail, CAST(s.collected_at AS STRING) AS listed_at
FROM {volume_sizes} s
LEFT JOIN {volumes_from} v
  ON v.volume_catalog = s.`catalog` AND v.volume_schema = s.`schema` AND v.volume_name = s.volume_name
"""
# the job's history columns as the operation metrics DESCRIBE HISTORY gives
_STORED_METRICS = (("rows_changed", "numOutputRows"), ("files_added", "numAddedFiles"),
                   ("files_removed", "numRemovedFiles"), ("freed_bytes", "sizeOfDataToDelete"),
                   ("bytes_added", "numAddedBytes"), ("bytes_removed", "numRemovedBytes"),
                   ("bytes_written", "numOutputBytes"))


def _stored_history(h: dict) -> dict:
    metrics = ", ".join(f"{key}={h[col]}" for col, key in _STORED_METRICS if h.get(col) not in (None, 0))
    return {"table_full_name": f"{h['catalog']}.{h['schema']}.{h['table_name']}", "version": h.get("version"),
            "timestamp": h.get("timestamp"), "operation": h.get("operation"), "user_name": h.get("user_name"),
            "metrics": metrics or None}


def _q(name: str) -> str:
    return "`" + str(name).replace("`", "``") + "`"


def _rows(cur, sql: str) -> list[dict]:
    cur.execute(sql)
    names = [d[0] for d in cur.description or []]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _describable(row: dict) -> bool:
    """A Delta table, materialized view or streaming table; not a view or another format."""
    kind = (row.get("table_type") or "").upper()
    fmt = (row.get("data_source_format") or "").upper()
    return kind != "VIEW" and fmt in ("", "DELTA")


def _previous(path: Path | None) -> dict[tuple, dict]:
    """Last export's rows that were described, keyed by table."""
    if path is None or not path.is_file():
        return {}
    try:
        rows = pq.read_table(str(path)).to_pylist()
    except Exception:  # noqa: BLE001 -- an unreadable old file just means describing again
        return {}
    return {(r["table_catalog"], r["table_schema"], r["table_name"]): r for r in rows
            if r.get("detail") in ("described", "reused") and r.get("size_in_bytes") is not None}


def export(connect_fn: Callable, out_dir: Path, *, previous: Path | None = None, threads: int = 16,
           max_tables: int = 20000, redact: Callable[[str], str] = str,
           log: Callable[[str], None] = print, history: bool = True) -> dict:
    """List every table, DESCRIBE DETAIL the changed Delta ones `threads` at a time, write
    out_dir/storage/table_detail.parquet; returns what it did for the manifest."""
    t0 = time.perf_counter()
    stored = _stored()
    source = {"source": "describe" if stored is None else "stored",
              "stored_from": stored and stored["table_sizes"]}
    try:
        lister = connect_fn()
    except Exception as exc:  # noqa: BLE001 -- table sizes are a side table; the export goes on
        return {"exported": False, "error": redact(str(exc)), **source}
    try:
        cur = lister.conn.cursor()
        tables = [r for r in _rows(cur, _LIST_SQL.format(**_list_from())) if not skipped(r["table_catalog"])]
        if stored is not None:
            sizes = {(r["catalog"], r["schema"], r["table_name"]): r for r in _rows(cur, _STORED_SIZES_SQL.format(**stored))}
            hist, history_error = [], None
            if history:
                try:
                    hist = [_stored_history(h) for h in _rows(cur, _STORED_HISTORY_SQL.format(**stored))]
                except Exception as exc:  # noqa: BLE001 -- no history is fine; the sizes still count
                    history_error = (redact(str(exc)).splitlines() or [""])[0][:200]
                    log(f"table history: not read from {stored['table_history']}: {history_error}")
    except Exception as exc:  # noqa: BLE001
        return {"exported": False, "error": redact(str(exc)), **source}
    finally:
        try:
            lister.conn.close()
        except Exception:  # noqa: BLE001
            pass

    if stored is not None:
        out = []
        for r in tables:
            row = {**{f.name: None for f in SCHEMA}, **{k: r.get(k) for k in
                   ("table_catalog", "table_schema", "table_name", "table_type", "data_source_format",
                    "last_altered", "column_count", "created", "owner")}}
            s = sizes.get((r["table_catalog"], r["table_schema"], r["table_name"]))
            if not _describable(r):
                row["detail"] = "view" if (r.get("table_type") or "").upper() == "VIEW" else "not_delta"
            elif s is None:  # the job didn't describe it: over its max_tables, or newer than its last run
                row["detail"] = "not_asked"
            else:
                row.update(size_in_bytes=s.get("size_in_bytes"), num_files=s.get("num_files"),
                           last_modified=s.get("last_modified"), detail=s.get("detail") or "described")
            out.append(row)
        collected = max((s["collected_at"] for s in sizes.values() if s.get("collected_at")), default=None)
        log(f"table sizes: {len(tables)} tables, read from {stored['table_sizes']} (collected {collected})")
        return _write_tables(out, hist, out_dir, **source, collected_at=collected, history_error=history_error,
                             history_from=stored["table_history"], seconds=round(time.perf_counter() - t0, 1))

    prev = _previous(previous)
    prev_hist = _previous_history(previous.with_name(HISTORY_FILE) if previous else None)
    hist: list[dict] = []
    out: list[dict] = []
    todo: list[dict] = []
    for r in tables:
        key = (r["table_catalog"], r["table_schema"], r["table_name"])
        row = {**{f.name: None for f in SCHEMA}, **{k: r.get(k) for k in
               ("table_catalog", "table_schema", "table_name", "table_type", "data_source_format",
                "last_altered", "column_count", "created", "owner")}}
        old = prev.get(key)
        if not _describable(r):
            row["detail"] = "view" if (r.get("table_type") or "").upper() == "VIEW" else "not_delta"
        elif old is not None and old.get("last_altered") == r.get("last_altered") and r.get("last_altered"):
            row.update(size_in_bytes=old["size_in_bytes"], num_files=old["num_files"],
                       last_modified=old["last_modified"], detail="reused")
            hist.extend(prev_hist.get(".".join(key), []))
        else:
            todo.append(row)
        out.append(row)
    # the most recently changed first, so a cap leaves out the ones least likely to have grown
    todo.sort(key=lambda r: r.get("last_altered") or "", reverse=True)
    for row in todo[max_tables:]:
        row["detail"] = "not_asked"
    todo = todo[:max_tables]
    log(f"table sizes: {len(tables)} tables, {len(todo)} to describe, "
        f"{sum(1 for r in out if r['detail'] == 'reused')} unchanged since the last export")

    local = threading.local()
    lock = threading.Lock()
    conns: list = []
    done = [0]

    def describe(row: dict) -> None:
        try:
            if not hasattr(local, "cur"):
                c = connect_fn()
                with lock:
                    conns.append(c)
                local.cur = c.conn.cursor()
            got = _rows(local.cur, "DESCRIBE DETAIL " + ".".join(
                _q(row[k]) for k in ("table_catalog", "table_schema", "table_name")))
            d = got[0] if got else {}
            row.update(size_in_bytes=d.get("sizeInBytes"), num_files=d.get("numFiles"),
                       last_modified=None if d.get("lastModified") is None else str(d["lastModified"]),
                       detail="described")
            if history:
                full = ".".join(str(row[k]) for k in ("table_catalog", "table_schema", "table_name"))
                try:
                    got = _rows(local.cur, "DESCRIBE HISTORY " + ".".join(
                        _q(row[k]) for k in ("table_catalog", "table_schema", "table_name")) + f" LIMIT {HISTORY_LIMIT}")
                    rows = [{"table_full_name": full, "version": h.get("version"),
                             "timestamp": None if h.get("timestamp") is None else str(h["timestamp"]),
                             "operation": h.get("operation"), "user_name": h.get("userName"),
                             "metrics": _metrics(h.get("operationMetrics"))} for h in got]
                    with lock:
                        hist.extend(rows)
                except Exception:  # noqa: BLE001 -- no history is fine; the size still counts
                    pass
        except Exception as exc:  # noqa: BLE001 -- one table it can't read must not stop the rest
            row["detail"] = "failed: " + (redact(str(exc)).splitlines() or [""])[0][:200]
        with lock:
            done[0] += 1
            if done[0] % 500 == 0:
                log(f"table sizes: {done[0]} of {len(todo)} described")

    if todo:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
            list(pool.map(describe, todo))
    for c in conns:
        try:
            c.conn.close()
        except Exception:  # noqa: BLE001
            pass
    return _write_tables(out, hist, out_dir, source="describe", threads=threads, max_tables=max_tables,
                         seconds=round(time.perf_counter() - t0, 1))


def _write_tables(out: list[dict], hist: list[dict], out_dir: Path, **info) -> dict:
    """Write storage/table_detail.parquet and table_history.parquet; what was written, for the manifest."""
    folder = out_dir / "storage"
    folder.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(out, schema=SCHEMA), str(folder / FILE), compression="snappy")
    clean = [{f.name: (None if h.get(f.name) is None else int(h[f.name]) if f.name == "version" else str(h[f.name]))
              for f in HISTORY_SCHEMA} for h in hist]
    pq.write_table(pa.Table.from_pylist(clean, schema=HISTORY_SCHEMA), str(folder / HISTORY_FILE), compression="snappy")
    counts: dict[str, int] = {}
    for r in out:
        k = "failed" if (r["detail"] or "").startswith("failed") else r["detail"]
        counts[k] = counts.get(k, 0) + 1
    first_error = next((r["detail"][8:] for r in out if (r["detail"] or "").startswith("failed")), None)
    return {"exported": True, "error": None, "tables": len(out), **counts, "first_error": first_error,
            "history_rows": len(clean), **info}


# ------------------------------------------------------------------------------------------------
# Volumes: how many files and bytes each holds. No system table has that, so each volume is listed
# (LIST '/Volumes/...', one directory at a time), many volumes at once, up to a cap per volume.
# ------------------------------------------------------------------------------------------------

VOLUME_FILE = "volume_detail.parquet"
VOLUME_MAX_ENTRIES = 50000  # files and folders listed per volume before it reads "truncated"
VOLUME_SCHEMA = pa.schema([
    ("volume_catalog", pa.string()), ("volume_schema", pa.string()), ("volume_name", pa.string()),
    ("volume_type", pa.string()), ("files", pa.int64()), ("size_in_bytes", pa.int64()),
    ("truncated", pa.bool_()), ("detail", pa.string()), ("listed_at", pa.string()),
])
# listed_at: when these counts were listed (UTC); an unchanged volume keeps last run's counts and time
_VOLUMES_SQL = """
SELECT volume_catalog, volume_schema, volume_name, volume_type
FROM {volumes_from}
WHERE volume_schema <> 'information_schema'
"""
# the last file write per volume in the audit log since `since`; Spark writes to /Volumes paths may
# not be logged, hence the full re-list every volume_full_days
_VOLUME_WRITES_SQL = """
SELECT regexp_extract(p, '^/Volumes/([^/]+/[^/]+/[^/]+)', 1) AS v, CAST(MAX(event_time) AS STRING) AS last_write
FROM (SELECT event_time, COALESCE(request_params['path'], request_params['file_path'], request_params['directory_path']) AS p
      FROM system.access.audit
      WHERE event_date >= DATE'{since}' AND service_name = 'filesystem' AND response.status_code = 200
        AND action_name RLIKE '(?i)put|upload|create|delete|write|mkdir|move|rename')
WHERE p RLIKE '^/Volumes/[^/]+/[^/]+/[^/]+'
GROUP BY 1
"""


def _previous_volumes(path: Path | None) -> dict[str, dict]:
    """Last run's volumes that were listed, by catalog/schema/name."""
    if path is None or not path.is_file():
        return {}
    try:
        rows = pq.read_table(str(path)).to_pylist()
    except Exception:  # noqa: BLE001 -- an unreadable old file just means listing again
        return {}
    return {"/".join((r["volume_catalog"], r["volume_schema"], r["volume_name"])): r for r in rows
            if r.get("detail") == "listed" and r.get("listed_at")}


def _sq(text: str) -> str:
    return "'" + str(text).replace("\\", "\\\\").replace("'", "\'") + "'"


def _list_volume(cur, root: str, max_entries: int) -> tuple[int, int, bool]:
    """Files and bytes under `root`, walking folders breadth first; True when it hit the cap."""
    files = size = seen = 0
    todo = [root]
    while todo:
        path = todo.pop(0)
        for r in _rows(cur, f"LIST {_sq(path)}"):
            seen += 1
            name = str(r.get("name") or r.get("path") or "")
            if name.endswith("/"):
                todo.append(str(r.get("path") or (path.rstrip("/") + "/" + name)))
            else:
                files += 1
                size += int(r.get("size") or 0)
            if seen >= max_entries:
                return files, size, True
    return files, size, False


def export_volumes(connect_fn: Callable, out_dir: Path, *, threads: int = 16,
                   max_entries: int = VOLUME_MAX_ENTRIES, previous: Path | None = None, full_days: int = 7,
                   now: datetime.datetime | None = None, redact: Callable[[str], str] = str,
                   log: Callable[[str], None] = print) -> dict:
    """List the volumes' files `threads` volumes at a time; write out_dir/storage/volume_detail.parquet.
    A volume listed in the last full_days with no file write in the audit log since keeps its counts."""
    t0 = time.perf_counter()
    stored = _stored()
    if stored is not None:
        return _stored_volumes(connect_fn, out_dir, stored, redact=redact, log=log, t0=t0)
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    prev = _previous_volumes(previous)
    cutoff = (now - datetime.timedelta(days=full_days)).strftime("%Y-%m-%d %H:%M:%S")
    fresh = {k: r for k, r in prev.items() if full_days and r["listed_at"] >= cutoff}
    try:
        lister = connect_fn()
    except Exception as exc:  # noqa: BLE001 -- a side table; the export goes on
        return {"exported": False, "error": redact(str(exc))}
    writes: dict[str, str] | None = {}
    try:
        cur = lister.conn.cursor()
        vols = [r for r in _rows(cur, _VOLUMES_SQL.format(**_list_from())) if not skipped(r["volume_catalog"])]
        if fresh:
            since = min(r["listed_at"] for r in fresh.values())[:10]
            try:
                writes = {r["v"]: r["last_write"] for r in _rows(cur, _VOLUME_WRITES_SQL.format(since=since))}
            except Exception:  # noqa: BLE001 -- no audit log access: list them all
                writes = None
    except Exception as exc:  # noqa: BLE001
        return {"exported": False, "error": redact(str(exc))}
    finally:
        try:
            lister.conn.close()
        except Exception:  # noqa: BLE001
            pass
    out: list[dict] = []
    todo: list[dict] = []
    for v in vols:
        key = "/".join(str(v[k]) for k in ("volume_catalog", "volume_schema", "volume_name"))
        old = fresh.get(key) if writes is not None else None
        if old is not None and (writes.get(key) or "") < old["listed_at"]:
            out.append({**{f.name: old.get(f.name) for f in VOLUME_SCHEMA}, "volume_type": v.get("volume_type")})
        else:
            todo.append(v)
    reused = len(out)
    log(f"volume files: listing {len(todo)} of {len(vols)} volumes, {reused} unchanged since their last listing")
    local = threading.local()
    lock = threading.Lock()
    conns: list = []

    def walk(v: dict) -> None:
        row = {f.name: None for f in VOLUME_SCHEMA}
        row.update({k: v.get(k) for k in ("volume_catalog", "volume_schema", "volume_name", "volume_type")})
        try:
            if not hasattr(local, "cur"):
                c = connect_fn()
                with lock:
                    conns.append(c)
                local.cur = c.conn.cursor()
            root = "/Volumes/" + "/".join(str(v[k]) for k in ("volume_catalog", "volume_schema", "volume_name")) + "/"
            files, size, cut = _list_volume(local.cur, root, max_entries)
            row.update(files=files, size_in_bytes=size, truncated=cut, detail="listed", listed_at=stamp)
        except Exception as exc:  # noqa: BLE001 -- one volume it can't read must not stop the rest
            row.update(truncated=False, detail="failed: " + (redact(str(exc)).splitlines() or [""])[0][:200])
        with lock:
            out.append(row)

    if todo:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
            list(pool.map(walk, todo))
    for c in conns:
        try:
            c.conn.close()
        except Exception:  # noqa: BLE001
            pass
    folder = out_dir / "storage"
    folder.mkdir(parents=True, exist_ok=True)
    out.sort(key=lambda r: (r["volume_catalog"], r["volume_schema"], r["volume_name"]))
    pq.write_table(pa.Table.from_pylist(out, schema=VOLUME_SCHEMA), str(folder / VOLUME_FILE), compression="snappy")
    failed = [r for r in out if (r["detail"] or "").startswith("failed")]
    return {"exported": True, "error": None, "volumes": len(out), "listed": len(out) - len(failed),
            "failed": len(failed), "truncated": sum(1 for r in out if r["truncated"]),
            "first_error": failed[0]["detail"][8:] if failed else None, "max_entries": max_entries,
            "reused": reused, "full_days": full_days, "seconds": round(time.perf_counter() - t0, 1)}


def _stored_volumes(connect_fn: Callable, out_dir: Path, stored: dict[str, str], *,
                    redact: Callable[[str], str], log: Callable[[str], None], t0: float) -> dict:
    """settings table_sizes source: stored. The sizes job's volume_sizes as volume_detail.parquet; nothing is listed."""
    source = {"source": "stored", "stored_from": stored["volume_sizes"]}
    try:
        conn = connect_fn()
    except Exception as exc:  # noqa: BLE001 -- a side table; the export goes on
        return {"exported": False, "error": redact(str(exc)), **source}
    try:
        sql = _STORED_VOLUMES_SQL.format(volumes_from=_list_from()["volumes_from"], **stored)
        out = [r for r in _rows(conn.conn.cursor(), sql) if not skipped(r["volume_catalog"])]
    except Exception as exc:  # noqa: BLE001
        return {"exported": False, "error": redact(str(exc)), **source}
    finally:
        try:
            conn.conn.close()
        except Exception:  # noqa: BLE001
            pass
    for r in out:
        r["truncated"] = bool(r.get("truncated"))
    out.sort(key=lambda r: (r["volume_catalog"], r["volume_schema"], r["volume_name"]))
    folder = out_dir / "storage"
    folder.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(out, schema=VOLUME_SCHEMA), str(folder / VOLUME_FILE), compression="snappy")
    failed = [r for r in out if (r["detail"] or "").startswith("failed")]
    collected = max((r["listed_at"] for r in out if r.get("listed_at")), default=None)
    log(f"volume files: {len(out)} volumes, read from {stored['volume_sizes']} (collected {collected})")
    return {"exported": True, "error": None, "volumes": len(out), "listed": len(out) - len(failed),
            "failed": len(failed), "truncated": sum(1 for r in out if r["truncated"]),
            "first_error": failed[0]["detail"][8:] if failed else None, **source, "collected_at": collected,
            "seconds": round(time.perf_counter() - t0, 1)}
