"""Workspace tags from config/workspace.csv: workspace_id, then one column per tag key. A workspace
listed there takes its tags from the file instead of its bill, on every cloud; an empty cell is no
tag. Applied to the tag tables when results are loaded, so every screen and the tag filter see it."""
from __future__ import annotations

import csv
from pathlib import Path

import duckdb

from app.core.tag_keys import normalize_tag_key

PATH = Path(__file__).resolve().parents[2] / "config" / "workspace.csv"
SOURCE = "workspace_file"
_REASON = "set in config/workspace.csv"


def read(path: Path = PATH) -> dict[str, dict[str, str]]:
    """{workspace_id: {key as written: value}}; {} when there is no file."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    out: dict[str, dict[str, str]] = {}
    for r in rows:
        ws = str(r.get("workspace_id") or "").strip()
        if ws:
            out[ws] = {k.strip(): v.strip() for k, v in r.items()
                       if k and k.strip() != "workspace_id" and v and v.strip()}
    return out


def _columns(con: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'tags' AND table_name = ? "
        "ORDER BY ordinal_position", [table]).fetchall()]


def _insert(con: duckdb.DuckDBPyConnection, table: str, values: dict[str, str]) -> None:
    """INSERT the columns this table has from `values` (column -> SQL over _wsf)."""
    cols = [c for c in _columns(con, table) if c in values]
    con.execute(f'INSERT INTO tags."{table}" ({", ".join(cols)}) SELECT {", ".join(values[c] for c in cols)} FROM _wsf')


def apply(con: duckdb.DuckDBPyConnection, path: Path = PATH) -> int:
    """Replaces the listed workspaces' tags in the tag tables (current, history, units, entity, index); returns
    how many workspaces the file sets."""
    tags = read(path)
    if not tags or not _columns(con, "tag_workspace"):
        return 0
    rows = [(ws, normalize_tag_key(k), k, v) for ws, kv in tags.items() for k, v in kv.items() if normalize_tag_key(k)]
    ids = list(tags)
    con.execute("CREATE OR REPLACE TEMP TABLE _wsf (workspace_id VARCHAR, tag_key VARCHAR, display_key VARCHAR, tag_value VARCHAR)")
    if rows:
        con.executemany("INSERT INTO _wsf VALUES (?, ?, ?, ?)", rows)
    reason = f"'{_REASON}'"
    con.execute("DELETE FROM tags.tag_workspace WHERE CAST(workspace_id AS VARCHAR) IN (SELECT unnest(?))", [ids])
    _insert(con, "tag_workspace", {
        "workspace_id": "workspace_id", "tag_key": "tag_key", "display_key": "display_key", "tag_value": "tag_value",
        "is_allocating": "TRUE", "top_value": "tag_value", "top_values": "[tag_value]", "share": "1", "coverage": "1",
        "reason": reason})
    # the file states the workspace's tags for every date, so its history is one open row per key
    if _columns(con, "tag_workspace_history"):
        con.execute("DELETE FROM tags.tag_workspace_history WHERE CAST(workspace_id AS VARCHAR) IN (SELECT unnest(?))", [ids])
        _insert(con, "tag_workspace_history", {
            "workspace_id": "workspace_id", "tag_key": "tag_key", "display_key": "display_key", "tag_value": "tag_value",
            "is_allocating": "TRUE", "valid_from": "CAST(NULL AS DATE)", "valid_to": "CAST(NULL AS DATE)",
            "is_current": "TRUE", "top_value": "tag_value", "top_values": "[tag_value]", "share": "1", "coverage": "1",
            "reason": reason})
    for unit, metrics in (("cost_unit", ("usd",)),
                          ("perf_unit", ("statements", "failed_statements", "duration_ms", "queue_ms", "spill_bytes"))):
        if not (_columns(con, unit) and _columns(con, f"{unit}_tag")):
            continue
        con.execute(
            f"DELETE FROM tags.{unit}_tag t WHERE t.level = 'workspace' AND EXISTS (SELECT 1 FROM tags.{unit} u "
            "WHERE u.window_days = t.window_days AND u.unit_id = t.unit_id "
            "AND CAST(u.workspace_id AS VARCHAR) IN (SELECT unnest(?)))", [ids])
        con.execute(
            f"INSERT INTO tags.{unit}_tag (window_days, unit_id, level, tag_key, raw_key, tag_value, source, "
            f"{', '.join(metrics)}) SELECT u.window_days, u.unit_id, 'workspace', f.tag_key, f.display_key, f.tag_value, "
            f"'{SOURCE}', {', '.join('u.' + m for m in metrics)} FROM tags.{unit} u "
            "JOIN _wsf f ON f.workspace_id = CAST(u.workspace_id AS VARCHAR)")
    if _columns(con, "tag_entity"):
        con.execute("DELETE FROM tags.tag_entity WHERE entity_type = 'workspace' AND CAST(workspace_id AS VARCHAR) IN "
                    "(SELECT unnest(?))", [ids])
        _insert(con, "tag_entity", {
            "entity_type": "'workspace'", "workspace_id": "workspace_id", "entity_id": "workspace_id",
            "tag_key": "tag_key", "raw_key": "display_key", "tag_value": "tag_value", "is_allocating": "TRUE",
            "source": f"'{SOURCE}'", "share": "1", "coverage": "1", "reason": reason})
    if _columns(con, "tag_index"):
        con.execute("DELETE FROM tags.tag_index WHERE source = ?", [SOURCE])
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _wsf_index AS SELECT tag_key, min(display_key) AS display_key, tag_value, "
            "count(*) AS n FROM _wsf GROUP BY tag_key, tag_value")
        cols = [c for c in _columns(con, "tag_index") if c in
                ("tag_key", "display_key", "raw_keys", "tag_value", "source", "object_count", "workspace_count")]
        exprs = {"tag_key": "tag_key", "display_key": "display_key", "raw_keys": "display_key", "tag_value": "tag_value",
                 "source": f"'{SOURCE}'", "object_count": "n", "workspace_count": "n"}
        con.execute(f'INSERT INTO tags.tag_index ({", ".join(cols)}) SELECT {", ".join(exprs[c] for c in cols)} FROM _wsf_index')
        con.execute("DROP TABLE _wsf_index")
    con.execute("DROP TABLE _wsf")
    return len(ids)
