"""Tests for tools/snapshot.py -- the exporter, driven entirely by a fake connection.

No test here opens a network connection or imports databricks-sql-connector (which is not
installed on the reference machine): `run_export()` takes a `connect_fn` and every test passes a
fake whose cursor records the statements it received and returns Arrow tables built in the test.

Loads the module under test by file path (the same pattern tests/test_ddl.py uses), so a plain
`pytest tests/test_snapshot.py -q` works without a conftest or package __init__.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import re
import sys
import types
from collections import deque
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "tools" / "snapshot.py"
DDL_MODULE_PATH = ROOT / "tests" / "fixtures" / "ddl.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


snap = _load(MODULE_PATH, "snapshot_under_test")
ddl = _load(DDL_MODULE_PATH, "ddl_for_snapshot_test")
ARROW_SCHEMA: dict[str, pa.Schema] = ddl.ARROW_SCHEMA

NOW = dt.datetime(2026, 9, 22, 10, 30, 0)          # naive UTC, fixed for every test
AS_OF_DATE = NOW.date()
_TABLE_RE = re.compile(r"FROM system\.(\w+)\.(\w+)")
_BOUNDS_RE = re.compile(r">= (?:DATE|TIMESTAMP) '(\d{4}-\d{2}-\d{2})[^']*'"
                        r"(?:.*?< (?:DATE|TIMESTAMP) '(\d{4}-\d{2}-\d{2})[^']*')?")
_WS_RE = re.compile(r"workspace_id IN \(([^)]*)\)")

LATER = dt.datetime(2026, 9, 22, 14, 0, 0)         # the same UTC day as NOW, 3.5 hours on
NEXT_DAY = dt.datetime(2026, 9, 23, 9, 0, 0)       # the next UTC day


# ------------------------------------------------------------------------------------------
# The fake connector
# ------------------------------------------------------------------------------------------
class FakeServerError(Exception):
    """Stands in for databricks.sql.exc.ServerOperationError."""


def table_key(sql: str) -> str:
    m = _TABLE_RE.search(sql)
    assert m, sql
    return f"{m.group(1)}__{m.group(2)}"


def sql_bounds(sql: str) -> tuple[dt.date | None, dt.date | None]:
    m = _BOUNDS_RE.search(sql)
    if not m:
        return None, None
    lo = dt.date.fromisoformat(m.group(1))
    hi = dt.date.fromisoformat(m.group(2)) if m.group(2) else None
    return lo, hi


def sql_workspaces(sql: str) -> list[str] | None:
    if "workspace_id IS NULL" in sql:
        return ["NULL"]
    m = _WS_RE.search(sql)
    if not m:
        return None
    return [w.strip().strip("'") for w in m.group(1).split(",")]


class FakeCursor:
    def __init__(self, conn: "FakeConnection"):
        self.conn = conn
        self._chunks: deque[pa.Table] = deque()
        self._schema: pa.Schema | None = None
        self.closed = False

    def execute(self, sql: str, parameters=None):
        self.conn.statements.append(sql)
        key = table_key(sql)
        handler = self.conn.handlers.get(key)
        if handler is None:
            result = ARROW_SCHEMA[key].empty_table()      # a live table with no rows
        else:
            result = handler(sql)                          # may raise
        chunks = result if isinstance(result, list) else [result]
        self._chunks = deque(chunks)
        self._schema = chunks[0].schema

    def fetchmany_arrow(self, n: int) -> pa.Table:
        assert n == snap.FETCH_ROWS
        if self._chunks:
            return self._chunks.popleft()
        assert self._schema is not None
        return self._schema.empty_table()

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, handlers=None):
        self.handlers = dict(handlers or {})
        self.statements: list[str] = []
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self):
        self.closed = True

    def statements_for(self, key: str) -> list[str]:
        return [s for s in self.statements if table_key(s) == key]


def run(tmp_path: Path, argv: list[str], handlers=None, now: dt.datetime = NOW,
        plan_path: Path | None = None):
    """Run the exporter into tmp_path/out with the fake; return (manifest, log lines, conn)."""
    conn = FakeConnection(handlers)
    logs: list[str] = []
    opts = snap.parse_args(["--out", str(tmp_path / "out"), *argv])
    manifest = snap.run_export(
        opts,
        connect_fn=lambda timeout: snap.Connected(conn, "fake-connector-0.0", "fakehost0000"),
        now=now,
        log=logs.append,
        plan_path=plan_path,
    )
    return manifest, logs, conn


def usage_rows(n: int, day: dt.date = dt.date(2026, 9, 1), workspace: str = "1111") -> pa.Table:
    """A billing.usage-shaped table with native complex columns, `n` rows on `day`."""
    s = ARROW_SCHEMA["billing__usage"]
    ts = dt.datetime(day.year, day.month, day.day, 10, 0, 0)
    return pa.table({
        "account_id": pa.array(["acct"] * n),
        "workspace_id": pa.array([workspace] * n),
        "record_id": pa.array([f"r{i}" for i in range(n)]),
        "sku_name": pa.array(["PREMIUM_JOBS_COMPUTE"] * n),
        "usage_start_time": pa.array([ts] * n, type=pa.timestamp("us")),
        "usage_end_time": pa.array([ts + dt.timedelta(hours=1)] * n, type=pa.timestamp("us")),
        "usage_date": pa.array([day] * n, type=pa.date32()),
        "custom_tags": pa.array([{"team": "data", "env": "prod"}] * n, type=s.field("custom_tags").type),
        "usage_quantity": pa.array([1.5] * n, type=pa.float64()),
        "usage_metadata": pa.array([{"job_id": "j1", "cluster_id": "c1"}] * n,
                                   type=s.field("usage_metadata").type),
    })


# ------------------------------------------------------------------------------------------
# The embedded predicate table (DEC-10)
# ------------------------------------------------------------------------------------------
def test_predicate_table_covers_exactly_the_sources():
    assert set(snap.PREDICATES) == set(ARROW_SCHEMA)
    assert len(snap.PREDICATES) == 49
    windowed = {k for k, p in snap.PREDICATES.items() if p.time_column}
    scd2 = {k for k, p in snap.PREDICATES.items() if p.kind == "scd2"}
    reference = {k for k, p in snap.PREDICATES.items() if p.kind == "reference"}
    assert len(windowed) == 18 and len(scd2) == 7 and len(reference) == 24
    # Every predicate column exists in the dump-derived schema of its table.
    for k in windowed:
        assert snap.PREDICATES[k].time_column in ARROW_SCHEMA[k].names, k
    # Spot checks against PLAN.md 5.2, verbatim.
    p = snap.PREDICATES
    assert (p["billing__usage"].time_column, p["billing__usage"].cap_days) == ("usage_date", 365)
    assert (p["compute__node_timeline"].time_column, p["compute__node_timeline"].cap_days) == ("start_time", 90)
    assert (p["access__inbound_network"].time_column, p["access__inbound_network"].cap_days) == ("event_time", 30)
    assert (p["storage__predictive_optimization_operations_history"].cap_days) == 180
    assert (p["serving__endpoint_usage"].time_column, p["serving__endpoint_usage"].cap_days) == ("request_time", 90)
    assert p["compute__warehouse_events"].cap_days is None
    assert p["ai_gateway__usage"].cap_days is None
    assert p["lakeflow__job_run_timeline"].time_column == "period_start_time"
    assert p["access__audit"].time_column == "event_date"
    assert p["compute__clusters"].time_column is None and p["compute__clusters"].kind == "scd2"
    assert p["information_schema__tables"].time_column is None
    assert sum(1 for k in p if k.startswith("information_schema__")) == 20


# ------------------------------------------------------------------------------------------
# Statements, selection and retention caps
# ------------------------------------------------------------------------------------------
def test_exactly_one_statement_per_requested_table(tmp_path):
    manifest, logs, conn = run(tmp_path, ["--days", "30"])
    assert len(conn.statements) == 49
    keys = sorted(table_key(s) for s in conn.statements)
    assert keys == sorted(snap.PREDICATES)
    assert all(s.startswith("SELECT * FROM system.") for s in conn.statements)
    assert all(e["state"] == "ok" for e in manifest["tables"].values())
    assert conn.closed


def test_only_and_exclude_schema_select_tables(tmp_path):
    _, _, conn = run(tmp_path, ["--only", "billing,lakeflow"])
    keys = sorted(table_key(s) for s in conn.statements)
    assert keys == sorted(k for k in snap.PREDICATES if k.split("__")[0] in ("billing", "lakeflow"))
    assert len(keys) == 9

    _, _, conn = run(tmp_path, ["--exclude-schema", "query"])
    keys = sorted(table_key(s) for s in conn.statements)
    assert "query__history" not in keys and len(keys) == 48

    with pytest.raises(snap.SnapshotConfigError, match="no tables selected"):
        run(tmp_path, ["--only", "billing", "--exclude-schema", "billing"])

    _, _, conn = run(tmp_path, ["--only", "billing.usage,compute__node_types"])
    assert sorted(table_key(s) for s in conn.statements) == ["billing__usage", "compute__node_types"]

    with pytest.raises(snap.SnapshotConfigError):
        run(tmp_path, ["--only", "nosuchschema"])
    with pytest.raises(snap.SnapshotConfigError):
        run(tmp_path, ["--exclude-schema", "nosuchschema"])


def test_retention_caps_are_applied_before_the_first_request(tmp_path):
    manifest, _, conn = run(tmp_path, ["--days", "400", "--billing-days", "500"])
    t = manifest["tables"]
    assert t["access__inbound_network"]["days_requested"] == 400
    assert t["access__inbound_network"]["days_effective"] == 30
    assert t["compute__node_timeline"]["days_effective"] == 90
    assert t["serving__endpoint_usage"]["days_effective"] == 90
    assert t["storage__predictive_optimization_operations_history"]["days_effective"] == 180
    assert t["query__history"]["days_effective"] == 365
    assert t["billing__usage"]["days_requested"] == 500 and t["billing__usage"]["days_effective"] == 365
    assert t["billing__attributed_usage"]["days_requested"] == 400
    assert t["billing__attributed_usage"]["days_effective"] == 365
    assert t["compute__warehouse_events"]["days_effective"] == 400     # no cap
    assert t["ai_gateway__usage"]["days_effective"] == 400
    assert t["compute__clusters"]["days_requested"] is None
    assert t["compute__clusters"]["days_effective"] is None
    assert t["compute__clusters"]["time_column"] is None
    assert t["compute__clusters"]["predicate"] is None
    assert manifest["days"] == 400 and manifest["billing_days"] == 500

    # The SQL sent reflects the cap and the literal type of the column.
    (inbound,) = conn.statements_for("access__inbound_network")
    assert f"event_time >= TIMESTAMP '{(AS_OF_DATE - dt.timedelta(days=30)).isoformat()} 00:00:00'" in inbound
    (usage,) = conn.statements_for("billing__usage")
    assert f"usage_date >= DATE '{(AS_OF_DATE - dt.timedelta(days=365)).isoformat()}'" in usage
    assert f"usage_date < DATE '{(AS_OF_DATE + dt.timedelta(days=1)).isoformat()}'" in usage
    (clusters,) = conn.statements_for("compute__clusters")
    assert clusters == "SELECT * FROM system.compute.clusters"
    assert t["billing__usage"]["predicate"] == usage.split(" WHERE ", 1)[1]


def test_workspace_predicate_only_where_the_column_exists(tmp_path):
    manifest, _, conn = run(tmp_path, ["--workspace", "1111", "2222"])
    assert manifest["workspace_ids"] == ["1111", "2222"]
    (usage,) = conn.statements_for("billing__usage")
    assert "workspace_id IN ('1111', '2222')" in usage
    (clusters,) = conn.statements_for("compute__clusters")
    assert clusters == "SELECT * FROM system.compute.clusters WHERE workspace_id IN ('1111', '2222')"
    for key in ("billing__list_prices", "compute__node_types", "information_schema__tables",
                "data_classification__results"):
        (stmt,) = conn.statements_for(key)
        assert "workspace_id" not in stmt, key
    with pytest.raises(snap.SnapshotConfigError):
        snap.parse_args(["--workspace", "not-a-number"])


def test_no_token_flag_exists():
    with pytest.raises(SystemExit):
        snap.build_parser().parse_args(["--token", "x"])
    options = {o for a in snap.build_parser()._actions for o in a.option_strings}
    assert "--token" not in options
    assert {"--out", "--days", "--billing-days", "--workspace", "--only", "--exclude-schema",
            "--max-rows-per-file", "--timeout", "--resume"} <= options


# ------------------------------------------------------------------------------------------
# "Returned too much data": bisection to one-day slices, then per workspace, then partial
# ------------------------------------------------------------------------------------------
BAD_DAY = AS_OF_DATE - dt.timedelta(days=2)


def _timeline_handler(bad_workspace: str):
    def handler(sql: str) -> pa.Table:
        lo, hi = sql_bounds(sql)
        assert lo is not None and hi is not None
        if (hi - lo).days > 1:
            raise FakeServerError("Query returned too much data")
        ws = sql_workspaces(sql)
        if lo == BAD_DAY and (ws is None or len(ws) != 1 or ws[0] == bad_workspace):
            raise FakeServerError("Query returned too much data")
        who = ws[0] if ws and len(ws) == 1 else "0000"
        return pa.table({
            "workspace_id": pa.array([None if who == "NULL" else who], type=pa.string()),
            "job_id": pa.array([f"job-{lo.isoformat()}"]),
            "run_id": pa.array(["r1"]),
            "period_start_time": pa.array([dt.datetime(lo.year, lo.month, lo.day, 12)],
                                          type=pa.timestamp("us")),
        })
    return handler


def test_too_much_data_bisects_then_retries_per_workspace_then_records_partial(tmp_path):
    key = "lakeflow__job_run_timeline"
    manifest, logs, conn = run(
        tmp_path, ["--days", "4", "--only", "lakeflow.job_run_timeline", "--workspace", "1111", "2222"],
        handlers={key: _timeline_handler(bad_workspace="2222")},
    )
    stmts = conn.statements_for(key)
    spans = [sql_bounds(s) for s in stmts]
    # First statement is the whole window (5 days incl. today), then halves, down to one day.
    assert spans[0] == (AS_OF_DATE - dt.timedelta(days=4), AS_OF_DATE + dt.timedelta(days=1))
    assert max((hi - lo).days for lo, hi in spans) == 5
    one_day = [s for s in stmts if (sql_bounds(s)[1] - sql_bounds(s)[0]).days == 1]
    assert {sql_bounds(s)[0] for s in one_day} == {AS_OF_DATE - dt.timedelta(days=d) for d in range(0, 5)}
    # The bad day was retried once per workspace after the combined one-day slice failed.
    bad_day_stmts = [s for s in one_day if sql_bounds(s)[0] == BAD_DAY]
    # Filtered export: the retry set is exactly the --workspace ids (the whole-window statement
    # never covered NULL rows, so no IS NULL slice is added).
    assert [sql_workspaces(s) for s in bad_day_stmts] == [["1111", "2222"], ["1111"], ["2222"]]
    assert not any("IS NULL" in s for s in stmts)

    entry = manifest["tables"][key]
    assert entry["state"] == "partial"
    assert entry["slices_failed"] == [{
        "from": BAD_DAY.isoformat(), "to": (BAD_DAY + dt.timedelta(days=1)).isoformat(),
        "workspace_id": "2222", "reason": "too_much_data", "error_class": "FakeServerError",
        "message": "Query returned too much data",
    }]
    assert entry["reason"] == "too_much_data"
    # 4 one-day slices succeeded for the combined ids + 1 for workspace 1111 on the bad day.
    assert entry["rows"] == 5
    folder = tmp_path / "out" / key
    assert not (folder / "_MISSING.json").exists()
    got = duckdb.connect().execute(
        f"select count(*), min(period_start_time), max(period_start_time) "
        f"from read_parquet('{folder.as_posix()}/*.parquet', union_by_name=true)").fetchone()
    assert got[0] == 5
    assert entry["min_time"] == dt.datetime.combine(AS_OF_DATE - dt.timedelta(days=4), dt.time(12)).isoformat()
    assert entry["max_time"] == dt.datetime.combine(AS_OF_DATE + dt.timedelta(days=0), dt.time(12)).isoformat()
    assert any("bisecting" in line for line in logs)
    assert any("retrying per workspace" in line for line in logs)


def test_per_workspace_retry_discovers_ids_when_workspace_flag_absent(tmp_path):
    key = "lakeflow__job_run_timeline"

    def workspaces_latest(sql: str) -> pa.Table:
        return pa.table({"workspace_id": pa.array(["2222", "1111"]),
                         "workspace_name": pa.array(["acme-dev", "acme-prod"])})

    manifest, logs, conn = run(
        tmp_path, ["--days", "2", "--only", "lakeflow.job_run_timeline"],
        handlers={key: _timeline_handler(bad_workspace="1111"), "access__workspaces_latest": workspaces_latest},
    )
    discovery = [s for s in conn.statements if s.startswith("SELECT DISTINCT workspace_id")]
    assert discovery == ["SELECT DISTINCT workspace_id FROM system.access.workspaces_latest"]
    bad_day_stmts = [s for s in conn.statements_for(key) if sql_bounds(s) == (BAD_DAY, BAD_DAY + dt.timedelta(days=1))]
    # Unfiltered export: discovered ids, then the account-level (workspace_id IS NULL) slice.
    assert [sql_workspaces(s) for s in bad_day_stmts] == [None, ["1111"], ["2222"], ["NULL"]]
    assert bad_day_stmts[-1].endswith(" AND workspace_id IS NULL")
    entry = manifest["tables"][key]
    assert entry["state"] == "partial"
    assert [f["workspace_id"] for f in entry["slices_failed"]] == ["1111"]
    # 3 days window -> 3 one-day slices; the bad day yields the 2222 row and the NULL row.
    assert entry["rows"] == 4
    folder = tmp_path / "out" / key
    got = duckdb.connect().execute(
        f"select count(*) filter (where workspace_id is null) "
        f"from read_parquet('{folder.as_posix()}/*.parquet', union_by_name=true)").fetchone()
    assert got[0] == 1
    # Out-of-scope workspaces_latest was not exported (only the discovery statement touched it).
    assert manifest["tables"]["access__workspaces_latest"]["reason"] == "excluded_by_flag"


def test_failed_null_workspace_slice_is_recorded_not_dropped(tmp_path):
    key = "lakeflow__job_run_timeline"

    def workspaces_latest(sql: str) -> pa.Table:
        return pa.table({"workspace_id": pa.array(["1111"])})

    manifest, _, conn = run(
        tmp_path, ["--days", "2", "--only", "lakeflow.job_run_timeline"],
        handlers={key: _timeline_handler(bad_workspace="NULL"), "access__workspaces_latest": workspaces_latest},
    )
    entry = manifest["tables"][key]
    assert entry["state"] == "partial"
    assert entry["slices_failed"] == [{
        "from": BAD_DAY.isoformat(), "to": (BAD_DAY + dt.timedelta(days=1)).isoformat(),
        "workspace_id": "NULL", "reason": "too_much_data", "error_class": "FakeServerError",
        "message": "Query returned too much data",
    }]


def test_single_workspace_id_does_not_resend_the_identical_statement(tmp_path):
    key = "lakeflow__job_run_timeline"
    manifest, _, conn = run(
        tmp_path, ["--days", "2", "--only", "lakeflow.job_run_timeline", "--workspace", "2222"],
        handlers={key: _timeline_handler(bad_workspace="2222")},
    )
    bad_day_stmts = [s for s in conn.statements_for(key) if sql_bounds(s) == (BAD_DAY, BAD_DAY + dt.timedelta(days=1))]
    assert [sql_workspaces(s) for s in bad_day_stmts] == [["2222"]]      # sent exactly once
    assert not any("SELECT DISTINCT" in s for s in conn.statements)      # no discovery either
    entry = manifest["tables"][key]
    assert entry["state"] == "partial"
    assert [f["workspace_id"] for f in entry["slices_failed"]] == [None]


def test_one_day_slice_without_workspace_column_lands_in_slices_failed(tmp_path):
    key = "billing__list_prices"   # no workspace_id column -> no per-workspace retry possible
    # Force a windowed predicate on it through the override mechanism is not allowed (it is a
    # full-history table); so use a full-history table without workspace_id, which is exactly why
    # the exporter falls straight to slices_failed there. Simulate via a too-much-data error on a
    # full table: no bisection is possible, so it must be recorded.
    def handler(sql: str):
        raise FakeServerError("Query returned too much data")

    manifest, _, conn = run(tmp_path, ["--only", "billing.list_prices"], handlers={key: handler})
    entry = manifest["tables"][key]
    assert entry["state"] == "partial"
    assert entry["rows"] == 0 and entry["files"] == 1
    assert entry["slices_failed"] == [{"from": None, "to": None, "workspace_id": None,
                                       "reason": "too_much_data", "error_class": "FakeServerError",
                                       "message": "Query returned too much data"}]
    assert len(conn.statements_for(key)) == 1
    # The folder still holds one typed parquet so the dbt source resolves.
    files = list((tmp_path / "out" / key).glob("*.parquet"))
    assert len(files) == 1 and pq.read_schema(files[0]).equals(ARROW_SCHEMA[key])


# ------------------------------------------------------------------------------------------
# Unreadable tables: _MISSING.json + empty typed parquet, readable by DuckDB with real data
# ------------------------------------------------------------------------------------------
def test_not_found_writes_missing_json_and_empty_typed_parquet(tmp_path):
    key = "serving__endpoint_usage"

    def not_found(sql: str):
        raise FakeServerError("[TABLE_OR_VIEW_NOT_FOUND] The table or view `system`.`serving`."
                              "`endpoint_usage` cannot be found. Verify the spelling.")

    manifest, logs, conn = run(tmp_path, ["--only", "serving"], handlers={key: not_found})
    entry = manifest["tables"][key]
    assert entry["state"] == "not_assessed"
    assert entry["reason"] == "table_not_found"
    assert entry["error_class"] == "FakeServerError"
    assert "cannot be found" in entry["message"]
    assert entry["rows"] == 0 and entry["files"] == 1
    folder = tmp_path / "out" / key
    missing = json.loads((folder / "_MISSING.json").read_text(encoding="utf-8"))
    assert missing["table"] == "system.serving.endpoint_usage"
    assert missing["reason"] == "table_not_found" and missing["state"] == "not_assessed"
    assert missing["error_class"] == "FakeServerError"
    assert set(missing) == {"table", "state", "error_class", "reason", "message", "as_of"}
    files = sorted(folder.glob("*.parquet"))
    assert len(files) == 1
    empty = pq.read_table(files[0])
    assert empty.num_rows == 0
    assert empty.schema.equals(ARROW_SCHEMA[key]), empty.schema
    # The sibling table in the same schema was exported normally.
    assert manifest["tables"]["serving__served_entities"]["state"] == "ok"


@pytest.mark.parametrize("message, reason, state", [
    ("[INSUFFICIENT_PERMISSIONS] User does not have SELECT on Table 'system.access.audit'.", "no_grant", "not_assessed"),
    ("PERMISSION_DENIED: not allowed", "no_grant", "not_assessed"),
    ("[SCHEMA_NOT_FOUND] The schema `system.serving` cannot be found.", "schema_not_enabled", "not_assessed"),
    ("system schema serving is not enabled for this metastore", "schema_not_enabled", "not_assessed"),
    ("[TABLE_OR_VIEW_NOT_FOUND] The table or view cannot be found.", "table_not_found", "not_assessed"),
    ("Operation timed out after 600 seconds", "timeout", "error"),
    ("boom: something unexpected", "unknown", "error"),
])
def test_error_classification_and_state(tmp_path, message, reason, state):
    key = "access__audit"

    def fail(sql: str):
        raise FakeServerError(message)

    manifest, _, _ = run(tmp_path, ["--only", "access.audit"], handlers={key: fail})
    entry = manifest["tables"][key]
    assert (entry["reason"], entry["state"]) == (reason, state)
    assert (tmp_path / "out" / key / "_MISSING.json").exists()
    assert snap.classify_error(FakeServerError(message))[1] == reason


def test_empty_typed_parquet_unions_with_real_data_in_duckdb(tmp_path):
    """DuckDB 1.5.1 + pyarrow 24: an empty ARROW_SCHEMA parquet next to a real data file of the
    same table must read as one relation through read_parquet(..., union_by_name=true)."""
    key = "billing__usage"

    def no_grant(sql: str):
        raise FakeServerError("[INSUFFICIENT_PERMISSIONS] User does not have SELECT on system.billing.usage")

    run(tmp_path, ["--only", "billing.usage"], handlers={key: no_grant})
    folder = tmp_path / "out" / key
    (empty,) = sorted(folder.glob("*.parquet"))
    assert pq.read_schema(empty).equals(ARROW_SCHEMA[key])
    # A later real export drops a data file next to it (normalised the way the exporter writes it).
    data = usage_rows(3)
    pq.write_table(data, folder / "part-00001.parquet")

    con = duckdb.connect()
    rel = f"read_parquet('{folder.as_posix()}/*.parquet', union_by_name=true)"
    assert con.execute(f"select count(*) from {rel}").fetchone()[0] == 3
    got = con.execute(
        f"select workspace_id, usage_date, usage_start_time, usage_quantity, custom_tags['team'], "
        f"usage_metadata.job_id, usage_metadata.notebook_path, record_type from {rel} limit 1").fetchone()
    assert got == ("1111", dt.date(2026, 9, 1), dt.datetime(2026, 9, 1, 10, 0), 1.5, "data", "j1", None, None)
    types = dict((r[0], r[1]) for r in con.execute(f"describe select * from {rel}").fetchall())
    assert types["usage_start_time"] == "TIMESTAMP"          # naive, UTC -- never TIMESTAMPTZ
    assert types["usage_date"] == "DATE"
    assert types["usage_quantity"] == "DOUBLE"
    assert types["custom_tags"] == "MAP(VARCHAR, VARCHAR)"
    assert types["usage_metadata"].startswith("STRUCT(")
    assert len(types) == len(ARROW_SCHEMA[key])


def test_every_folder_of_a_full_run_is_readable_by_duckdb(tmp_path):
    def not_found(sql: str):
        raise FakeServerError("[TABLE_OR_VIEW_NOT_FOUND] cannot be found")

    manifest, _, _ = run(tmp_path, ["--days", "7"], handlers={
        "billing__usage": lambda sql: usage_rows(2),
        "data_classification__results": not_found,
    })
    con = duckdb.connect()
    out = tmp_path / "out"
    folders = sorted(p.name for p in out.iterdir() if p.is_dir())
    assert folders == sorted(snap.PREDICATES)
    for key in folders:
        n = con.execute(
            f"select count(*) from read_parquet('{(out / key).as_posix()}/*.parquet', union_by_name=true)"
        ).fetchone()[0]
        assert n == manifest["tables"][key]["rows"], key
    assert manifest["tables"]["billing__usage"]["rows"] == 2
    assert manifest["tables"]["data_classification__results"]["state"] == "not_assessed"


# ------------------------------------------------------------------------------------------
# Type normalisation: timestamps and the MAP/STRUCT JSON fallback
# ------------------------------------------------------------------------------------------
def test_timestamp_columns_are_written_as_timestamp_us_utc(tmp_path):
    key = "query__history"
    utc_instant = dt.datetime(2026, 9, 1, 10, 0, 0, tzinfo=dt.timezone.utc)
    plus2 = utc_instant.astimezone(dt.timezone(dt.timedelta(hours=2)))
    table = pa.table({
        "workspace_id": pa.array(["1111"]),
        "statement_id": pa.array(["s1"]),
        "start_time": pa.array([utc_instant], type=pa.timestamp("ns", tz="Etc/UTC")),
        "end_time": pa.array([plus2], type=pa.timestamp("us", tz="+02:00")),
        "update_time": pa.array([dt.datetime(2026, 9, 1, 10, 0, 0)], type=pa.timestamp("ms")),
    })
    manifest, _, _ = run(tmp_path, ["--only", "query"], handlers={key: lambda sql: table})
    (part,) = sorted((tmp_path / "out" / key).glob("*.parquet"))
    got = pq.read_table(part)
    for col in ("start_time", "end_time", "update_time"):
        assert got.schema.field(col).type == pa.timestamp("us"), col
        assert got.column(col).to_pylist() == [dt.datetime(2026, 9, 1, 10, 0, 0)], col
    entry = manifest["tables"][key]
    assert entry["min_time"] == "2026-09-01T10:00:00" and entry["max_time"] == "2026-09-01T10:00:00"


def test_json_string_fallback_matches_native_complex_types(tmp_path):
    key = "billing__usage"
    native = usage_rows(2)
    as_json = native.set_column(
        native.schema.get_field_index("custom_tags"), "custom_tags",
        pa.array([json.dumps(dict(v)) for v in native.column("custom_tags").to_pylist()]),
    ).set_column(
        native.schema.get_field_index("usage_metadata"), "usage_metadata",
        pa.array([json.dumps({k: v for k, v in d.items() if v is not None})
                  for d in native.column("usage_metadata").to_pylist()]),
    )
    assert pa.types.is_string(as_json.schema.field("custom_tags").type)

    run(tmp_path / "a", ["--only", "billing.usage"], handlers={key: lambda sql: native})
    run(tmp_path / "b", ["--only", "billing.usage"], handlers={key: lambda sql: as_json})
    (pa_file,) = sorted((tmp_path / "a" / "out" / key).glob("*.parquet"))
    (pb_file,) = sorted((tmp_path / "b" / "out" / key).glob("*.parquet"))
    ta, tb = pq.read_table(pa_file), pq.read_table(pb_file)
    assert ta.schema.equals(tb.schema)
    assert ta.schema.field("custom_tags").type == ARROW_SCHEMA[key].field("custom_tags").type
    assert ta.schema.field("usage_metadata").type == ARROW_SCHEMA[key].field("usage_metadata").type
    assert ta.to_pylist() == tb.to_pylist()
    assert ta.column("custom_tags").to_pylist()[0] == [("team", "data"), ("env", "prod")]
    assert ta.column("usage_metadata").to_pylist()[0]["job_id"] == "j1"


def test_struct_sub_fields_unknown_to_the_dump_are_kept_on_both_paths(tmp_path):
    key = "billing__usage"
    live_type = pa.struct([("job_id", pa.string()), ("brand_new_sub_field", pa.string())])
    native = pa.table({
        "workspace_id": pa.array(["1111"]),
        "usage_date": pa.array([dt.date(2026, 9, 1)]),
        "usage_metadata": pa.array([{"job_id": "j1", "brand_new_sub_field": "kept"}], type=live_type),
    })
    as_json = native.set_column(2, "usage_metadata",
                                pa.array([json.dumps({"job_id": "j1", "brand_new_sub_field": "kept"})]))
    run(tmp_path / "a", ["--only", "billing.usage"], handlers={key: lambda sql: native})
    run(tmp_path / "b", ["--only", "billing.usage"], handlers={key: lambda sql: as_json})
    for sub in ("a", "b"):
        (part,) = sorted((tmp_path / sub / "out" / key).glob("*.parquet"))
        got = pq.read_table(part)
        st = got.schema.field("usage_metadata").type
        dump_names = [f.name for f in ARROW_SCHEMA[key].field("usage_metadata").type]
        assert [f.name for f in st][:len(dump_names)] == dump_names, sub    # dump fields first
        assert "brand_new_sub_field" in [f.name for f in st], sub            # live extra kept
        row = got.column("usage_metadata").to_pylist()[0]
        assert row["job_id"] == "j1" and row["brand_new_sub_field"] == "kept" and row["notebook_path"] is None


def test_decimal_and_null_columns_are_cast_to_the_dump_types(tmp_path):
    key = "billing__usage"
    table = pa.table({
        "workspace_id": pa.array(["1111"]),
        "usage_date": pa.array([dt.date(2026, 9, 1)]),
        "usage_quantity": pa.array([2], type=pa.decimal128(38, 18)),
        "record_type": pa.nulls(1),                       # untyped null column from the connector
        "brand_new_column": pa.array(["kept as-is"]),      # not in the dump: passes through
    })
    run(tmp_path, ["--only", "billing.usage"], handlers={key: lambda sql: table})
    (part,) = sorted((tmp_path / "out" / key).glob("*.parquet"))
    got = pq.read_table(part)
    assert got.schema.field("usage_quantity").type == pa.float64()
    assert got.schema.field("record_type").type == pa.string()
    assert got.schema.field("brand_new_column").type == pa.string()
    assert got.column("usage_quantity").to_pylist() == [2.0]


def test_max_rows_per_file_rolls_part_files(tmp_path):
    key = "billing__usage"
    chunks = [usage_rows(3), usage_rows(3), usage_rows(1)]
    manifest, _, _ = run(tmp_path, ["--only", "billing.usage", "--max-rows-per-file", "4"],
                         handlers={key: lambda sql: chunks})
    entry = manifest["tables"][key]
    assert entry["rows"] == 7
    files = sorted((tmp_path / "out" / key).glob("part-*.parquet"))
    assert entry["files"] == len(files) == 2
    assert [pq.read_metadata(f).num_rows for f in files] == [3, 4]


# ------------------------------------------------------------------------------------------
# manifest.json shape
# ------------------------------------------------------------------------------------------
def test_manifest_shape_matches_plan_5_2_exactly(tmp_path):
    manifest, _, _ = run(tmp_path, ["--days", "30", "--billing-days", "365", "--workspace", "1111"])
    on_disk = json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk == manifest
    assert list(manifest) == list(snap.TOP_LEVEL_KEYS) + ["tables"]
    assert manifest["as_of"] == "2026-09-22T10:30:00"
    assert manifest["as_of_date"] == "2026-09-22"
    assert manifest["days"] == 30 and manifest["billing_days"] == 365
    assert manifest["workspace_ids"] == ["1111"]
    assert manifest["host_fingerprint"] == "fakehost0000"
    assert manifest["connector_version"] == "fake-connector-0.0"
    assert manifest["metastore"] is None  # the fake Connected has no probe
    assert sorted(manifest["tables"]) == sorted(snap.PREDICATES)
    for key, entry in manifest["tables"].items():
        assert list(entry) == list(snap.ENTRY_KEYS), key
        assert entry["state"] in snap.STATES
        assert isinstance(entry["rows"], int) and isinstance(entry["files"], int)
        assert isinstance(entry["slices_failed"], list)
        assert isinstance(entry["elapsed_s"], float)
        assert entry["as_of"] == "2026-09-22T10:30:00", key
        assert entry["workspace_ids"] == (
            ["1111"] if "workspace_id" in ARROW_SCHEMA[key].names else []), key
    # T-05's audit_now() regex: YYYY-MM-DD[ T]HH:MM:SS with no zone suffix.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", manifest["as_of"])


def test_default_billing_days_falls_back_to_days(tmp_path):
    manifest, _, _ = run(tmp_path, ["--days", "45"])
    assert manifest["billing_days"] == 45
    assert manifest["tables"]["billing__usage"]["days_requested"] == 45


# ------------------------------------------------------------------------------------------
# T-71: the metastore the snapshot connected through
# ------------------------------------------------------------------------------------------
class _MetastoreCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, parameters=None):
        self.conn.statements.append(sql)
        if self.conn.error is not None:
            raise self.conn.error

    def fetchone(self):
        return None if self.conn.no_row else (self.conn.value,)

    def close(self):
        pass


class _MetastoreConn:
    def __init__(self, value=None, error=None, no_row=False):
        self.value, self.error, self.no_row = value, error, no_row
        self.statements: list[str] = []

    def cursor(self):
        return _MetastoreCursor(self)

    def close(self):
        pass


METASTORE_ID = "aws:us-east-1:00000000-0000-4000-8000-000000000071"
METASTORE_INFO = {"cloud": "aws", "region": "us-east-1", "id_fingerprint": "223290a343c0aa60"}


def test_parse_metastore_id_splits_cloud_and_region_and_fingerprints_the_id():
    assert snap.parse_metastore_id(METASTORE_ID) == METASTORE_INFO
    assert snap.parse_metastore_id(" " + METASTORE_ID + " ") == METASTORE_INFO
    assert snap.parse_metastore_id("azure:westeurope:0a1b2c3d-0000-4000-8000-000000000001") == {
        "cloud": "azure", "region": "westeurope", "id_fingerprint": "28dd45551bc6a147",
    }
    assert snap.parse_metastore_id("not-a-metastore-id") == {
        "cloud": None, "region": None, "id_fingerprint": "54c76cc411916e93",
    }
    assert snap.parse_metastore_id(None) is None
    assert snap.parse_metastore_id("   ") is None
    # The raw id never lands in the manifest.
    assert "00000000-0000-4000-8000-000000000071" not in json.dumps(snap.parse_metastore_id(METASTORE_ID))


def test_read_metastore_sends_one_statement_and_never_raises():
    conn = _MetastoreConn(METASTORE_ID)
    assert snap.read_metastore(conn) == METASTORE_INFO
    assert conn.statements == ["SELECT current_metastore()"]
    assert snap.read_metastore(
        _MetastoreConn(error=FakeServerError("[INSUFFICIENT_PERMISSIONS] no"))
    ) is None
    assert snap.read_metastore(_MetastoreConn(None)) is None
    assert snap.read_metastore(_MetastoreConn(no_row=True)) is None


def test_connect_records_the_metastore_of_the_connection(monkeypatch):
    fake_conn = _MetastoreConn(METASTORE_ID)
    fake_sql = types.SimpleNamespace(connect=lambda **kw: fake_conn, __version__="9.9.9")
    fake_pkg = types.ModuleType("databricks")
    fake_pkg.sql = fake_sql
    monkeypatch.setitem(sys.modules, "databricks", fake_pkg)
    monkeypatch.setitem(sys.modules, "databricks.sql", fake_sql)
    # F3: connect() now probes the host with a real socket before dialing out -- irrelevant to
    # this test's own concern (metastore recording), and there is no real network here anyway.
    monkeypatch.setattr(snap, "check_host_reachable", lambda host, timeout=15: None)
    monkeypatch.setenv(snap.ENV_HOST, "example.cloud.databricks.com")
    monkeypatch.setenv(snap.ENV_HTTP_PATH, "/sql/1.0/warehouses/abc")
    monkeypatch.setenv(snap.ENV_TOKEN, "dapi0000000000000000000000000000")
    connected = snap.connect(timeout=5, dotenv_path=None)
    assert connected.conn is fake_conn
    assert connected.connector_version == "9.9.9"
    assert connected.metastore == METASTORE_INFO
    tag = f"SET QUERY_TAGS['{snap.AUDIT_TAG_KEY}'] = '{snap._query_source()}'"
    assert fake_conn.statements == [tag, "SET TIME ZONE 'UTC'", "SELECT current_metastore()"]


def test_manifest_records_the_metastore_and_the_log_names_it(tmp_path):
    conn = FakeConnection()
    logs = []
    opts = snap.parse_args(["--out", str(tmp_path / "out"), "--days", "7"])
    manifest = snap.run_export(
        opts,
        connect_fn=lambda timeout: snap.Connected(conn, "fake-connector-0.0", "fakehost0000", METASTORE_INFO),
        now=NOW, log=logs.append, plan_path=None,
    )
    assert manifest["metastore"] == METASTORE_INFO
    on_disk = json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk == manifest
    assert list(manifest) == list(snap.TOP_LEVEL_KEYS) + ["tables"]
    assert any(l.startswith("metastore aws us-east-1:") for l in logs)
    assert len(conn.statements) == 49  # the probe runs in connect(), never through the export loop


def test_manifest_metastore_is_null_when_it_could_not_be_read(tmp_path):
    manifest, logs, _ = run(tmp_path, ["--days", "7"])
    assert manifest["metastore"] is None
    assert any(l.startswith("metastore not recorded:") for l in logs)


# ------------------------------------------------------------------------------------------
# Secrets never reach output
# ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("token", ["dapi0123456789abcdef0123456789abcdef", "plainsecretvalue987"])
def test_raw_token_never_appears_in_logs_manifest_or_missing_json(tmp_path, monkeypatch, token):
    monkeypatch.setenv(snap.ENV_TOKEN, token)

    def leaky_failure(sql: str):
        raise FakeServerError(f"PERMISSION_DENIED for Bearer {token} (token={token}) on {sql}")

    def leaky_bisect(sql: str):
        raise FakeServerError(f"Query returned too much data; token={token}")

    manifest, logs, _ = run(
        tmp_path, ["--only", "access.audit,billing.list_prices"],
        handlers={"access__audit": leaky_failure, "billing__list_prices": leaky_bisect},
    )
    assert logs, "expected log lines"
    for line in logs:
        assert token not in line, line
    manifest_text = (tmp_path / "out" / "manifest.json").read_text(encoding="utf-8")
    assert token not in manifest_text
    assert "***" in manifest["tables"]["access__audit"]["message"]
    assert token not in json.dumps(manifest)
    missing_text = (tmp_path / "out" / "access__audit" / "_MISSING.json").read_text(encoding="utf-8")
    assert token not in missing_text and "***" in missing_text
    failed = manifest["tables"]["billing__list_prices"]["slices_failed"]
    assert failed and token not in json.dumps(failed)


def test_scrub_secrets_patterns(monkeypatch):
    monkeypatch.delenv(snap.ENV_TOKEN, raising=False)
    assert snap.scrub_secrets("x dapi0123456789abcdef0123456789abcdef y") == "x *** y"
    assert snap.scrub_secrets("Authorization: Bearer abc.DEF-123") == "Authorization: Bearer ***"
    assert snap.scrub_secrets("access_token=abc password: hunter2") == "access_token=*** password: ***"
    assert snap.scrub_secrets("time_column: usage_date, rows=3") == "time_column: usage_date, rows=3"
    assert snap.scrub_secrets(None) == ""
    monkeypatch.setenv(snap.ENV_TOKEN, "s3cr3t-value")
    assert snap.scrub_secrets("failed with s3cr3t-value inside") == "failed with *** inside"


# ------------------------------------------------------------------------------------------
# connect(): env vars at call time, lazy connector import, .env, no network
# ------------------------------------------------------------------------------------------
def test_connect_reads_env_vars_at_call_time_and_never_imports_eagerly(tmp_path, monkeypatch):
    for name in snap.CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(snap.SnapshotConfigError) as exc:
        snap.connect(timeout=5, dotenv_path=tmp_path / "absent.env")
    for name in snap.CREDENTIAL_ENV_VARS:
        assert name in str(exc.value)
    assert "--token" in str(exc.value)

    # With the env present the connector is imported lazily; block it so no real import or
    # connection can happen even on a machine that has the package installed.
    monkeypatch.setitem(sys.modules, "databricks", None)
    monkeypatch.setitem(sys.modules, "databricks.sql", None)
    monkeypatch.setenv(snap.ENV_HOST, "example.cloud.databricks.com")
    monkeypatch.setenv(snap.ENV_HTTP_PATH, "/sql/1.0/warehouses/abc")
    monkeypatch.setenv(snap.ENV_TOKEN, "dapi0000000000000000000000000000")
    with pytest.raises(snap.SnapshotConfigError) as exc:
        snap.connect(timeout=5, dotenv_path=None)
    assert "pip install -r requirements.txt" in str(exc.value)
    assert "dapi" not in str(exc.value)


def test_dotenv_populates_only_the_three_credential_vars(tmp_path, monkeypatch):
    for name in snap.CREDENTIAL_ENV_VARS:
        monkeypatch.setenv(name, "")
    monkeypatch.delenv("UNRELATED_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "# comment\nexport DATABRICKS_SERVER_HOSTNAME=host.example\n"
        "DATABRICKS_HTTP_PATH='/sql/1.0/warehouses/x'\nDATABRICKS_TOKEN=\"dapiabcdefabcdefabcdefabcdef\"\n"
        "UNRELATED_KEY=nope\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(sys.modules, "databricks", None)
    with pytest.raises(snap.SnapshotConfigError) as exc:
        snap.connect(timeout=5, dotenv_path=env)
    assert "pip install" in str(exc.value)         # got past the missing-var check
    assert snap.os.environ[snap.ENV_HOST] == "host.example"
    assert snap.os.environ[snap.ENV_HTTP_PATH] == "/sql/1.0/warehouses/x"
    assert snap.os.environ[snap.ENV_TOKEN] == "dapiabcdefabcdefabcdefabcdef"
    assert "UNRELATED_KEY" not in snap.os.environ


def test_dotenv_utf16_from_powershell_echo_loads_correctly(tmp_path, monkeypatch):
    """F11: `echo X=Y > .env` in Windows PowerShell 5.1 writes UTF-16 (with a BOM) -- a plain
    `.read_text(encoding="utf-8")` raised an uncaught UnicodeDecodeError on this file instead of
    loading it."""
    monkeypatch.setenv(snap.ENV_HOST, "")
    env = tmp_path / ".env"
    env.write_bytes("DATABRICKS_SERVER_HOSTNAME=utf16.example.com\n".encode("utf-16"))
    snap._load_dotenv(env)
    assert snap.os.environ[snap.ENV_HOST] == "utf16.example.com"


def test_dotenv_utf8_bom_from_notepad_strips_the_bom(tmp_path, monkeypatch):
    """F11: Notepad's own UTF-8 default writes a BOM -- undecoded, it stuck to the first key's
    name ("﻿DATABRICKS_SERVER_HOSTNAME"), which was then reported as a missing credential."""
    monkeypatch.setenv(snap.ENV_HOST, "")
    env = tmp_path / ".env"
    env.write_bytes(b"\xef\xbb\xbfDATABRICKS_SERVER_HOSTNAME=bom.example.com\n")
    snap._load_dotenv(env)
    assert snap.os.environ[snap.ENV_HOST] == "bom.example.com"


def test_dotenv_undecodable_bytes_raises_a_clear_config_error(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b"\x80\x81\x82\x83not text")
    with pytest.raises(snap.SnapshotConfigError, match="not readable text"):
        snap._load_dotenv(env)


def test_module_never_imports_the_connector_at_import_time():
    text = MODULE_PATH.read_text(encoding="utf-8")
    top_level_imports = [l for l in text.splitlines() if l.startswith(("import ", "from "))]
    assert not any("databricks" in l for l in top_level_imports)
    assert "databricks" not in sys.modules or sys.modules["databricks"] is None


def test_host_fingerprint_is_not_the_host():
    fp = snap.host_fingerprint("adb-123.azuredatabricks.net")
    assert len(fp) == 16 and "adb" not in fp and fp == snap.host_fingerprint("ADB-123.azuredatabricks.net ")


# ------------------------------------------------------------------------------------------
# --resume
# ------------------------------------------------------------------------------------------
def _edit_manifest(out: Path, edit) -> dict:
    path = out / "manifest.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    edit(doc)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return doc


def _reused_line(key: str, as_of: str) -> str:
    return (f"{key}: reused (--resume: exported {as_of}, same as_of_date, window and "
            f"workspace filter)")


def test_resume_reuses_ok_tables_from_the_same_day_and_scope_unchanged(tmp_path):
    out = tmp_path / "out"
    # Seed: a 10:30 run with billing data; then mark three tables not-ok and drop one entry.
    run(tmp_path, ["--days", "30"], handlers={"billing__usage": lambda sql: usage_rows(4)})
    seeded = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    ok_entry = dict(seeded["tables"]["billing__usage"])
    ok_entry["elapsed_s"] = 123.456                     # a distinctive value to prove carry-over
    seeded["tables"]["billing__usage"] = ok_entry
    seeded["tables"]["query__history"]["state"] = "partial"
    seeded["tables"]["access__audit"]["state"] = "error"
    seeded["tables"]["compute__clusters"]["state"] = "not_assessed"
    del seeded["tables"]["lakeflow__jobs"]              # no entry at all -> exported normally
    seeded["as_of"] = "2000-01-01T00:00:00"             # the prior TOP-LEVEL stamp is never used
    (out / "manifest.json").write_text(json.dumps(seeded, indent=2), encoding="utf-8")
    usage_file_mtime = sorted((out / "billing__usage").glob("*.parquet"))[0].stat().st_mtime_ns

    def must_not_be_called(sql: str):
        raise AssertionError("billing.usage must not be re-requested under --resume")

    manifest, logs, conn = run(tmp_path, ["--days", "30", "--resume"], now=LATER,
                               handlers={"billing__usage": must_not_be_called})
    assert sorted(table_key(s) for s in conn.statements) == [
        "access__audit", "compute__clusters", "lakeflow__jobs", "query__history"]
    for key in ("query__history", "access__audit", "compute__clusters", "lakeflow__jobs"):
        assert manifest["tables"][key]["state"] == "ok", key
        assert manifest["tables"][key]["as_of"] == "2026-09-22T14:00:00", key
    assert ok_entry["as_of"] == "2026-09-22T10:30:00" and ok_entry["workspace_ids"] == []
    assert manifest["tables"]["billing__usage"] == ok_entry
    assert sorted((out / "billing__usage").glob("*.parquet"))[0].stat().st_mtime_ns == usage_file_mtime
    # One snapshot, one start: the oldest reused table's as_of, not the resume's own 14:00.
    assert manifest["as_of"] == "2026-09-22T10:30:00"
    assert manifest["as_of_date"] == "2026-09-22" and manifest["days"] == 30
    assert sorted(manifest["tables"]) == sorted(snap.PREDICATES)
    assert "--resume: reusing 45 table(s), re-pulling 4" in logs
    assert _reused_line("billing__usage", "2026-09-22T10:30:00") in logs
    assert "query__history: re-pulling (--resume: state was partial)" in logs
    assert "access__audit: re-pulling (--resume: state was error)" in logs
    assert "compute__clusters: re-pulling (--resume: state was not_assessed)" in logs
    assert "lakeflow__jobs: re-pulling (--resume: no entry in the existing manifest)" in logs
    assert any("; 45 reused and 4 re-pulled by --resume) -> " in line for line in logs)


def test_resume_on_a_later_day_re_pulls_every_table(tmp_path):
    run(tmp_path, ["--days", "30"], handlers={"billing__usage": lambda sql: usage_rows(4)})
    manifest, logs, conn = run(
        tmp_path, ["--days", "30", "--resume"], now=NEXT_DAY,
        handlers={"billing__usage": lambda sql: usage_rows(2, day=dt.date(2026, 9, 22))})
    assert sorted(table_key(s) for s in conn.statements) == sorted(snap.PREDICATES)
    (usage,) = conn.statements_for("billing__usage")
    assert "usage_date >= DATE '2026-08-24'" in usage and "usage_date < DATE '2026-09-24'" in usage
    assert manifest["tables"]["billing__usage"]["rows"] == 2
    assert manifest["as_of"] == "2026-09-23T09:00:00" and manifest["as_of_date"] == "2026-09-23"
    assert {e["as_of"] for e in manifest["tables"].values()} == {"2026-09-23T09:00:00"}
    assert "--resume: reusing 0 table(s), re-pulling 49" in logs
    assert ("billing__usage: re-pulling (--resume: exported for as_of_date 2026-09-22, "
            "this run is 2026-09-23)") in logs


def test_resume_with_a_different_days_re_pulls_every_windowed_table(tmp_path):
    first, _, _ = run(tmp_path, ["--days", "30"])
    manifest, logs, conn = run(tmp_path, ["--days", "60", "--resume"])
    windowed = sorted(k for k, p in snap.PREDICATES.items() if p.time_column)
    assert len(windowed) == 18
    assert sorted(table_key(s) for s in conn.statements) == windowed
    # Full-history and reference tables do not depend on --days: reused exactly as they were.
    for key in sorted(set(snap.PREDICATES) - set(windowed)):
        assert manifest["tables"][key] == first["tables"][key], key
    inbound = manifest["tables"]["access__inbound_network"]
    assert (inbound["days_requested"], inbound["days_effective"]) == (60, 30)
    assert manifest["days"] == 60 and manifest["billing_days"] == 60
    assert "--resume: reusing 31 table(s), re-pulling 18" in logs
    assert ("access__inbound_network: re-pulling (--resume: window 30->30 days, "
            "this run 60->30 days)") in logs
    assert "billing__usage: re-pulling (--resume: window 30->30 days, this run 60->60 days)" in logs


def test_resume_with_a_different_workspace_filter_re_pulls_every_filtered_table(tmp_path):
    first, _, _ = run(tmp_path, ["--days", "30", "--workspace", "1111"])
    assert first["tables"]["billing__usage"]["workspace_ids"] == ["1111"]
    assert first["tables"]["billing__list_prices"]["workspace_ids"] == []   # no workspace_id column
    manifest, logs, conn = run(tmp_path, ["--days", "30", "--resume"])
    ws_keys = sorted(k for k in snap.PREDICATES if "workspace_id" in ARROW_SCHEMA[k].names)
    assert len(ws_keys) == 26
    assert sorted(table_key(s) for s in conn.statements) == ws_keys
    assert not any("workspace_id IN" in s for s in conn.statements)
    for key in sorted(set(snap.PREDICATES) - set(ws_keys)):
        assert manifest["tables"][key] == first["tables"][key], key
    assert manifest["tables"]["billing__usage"]["workspace_ids"] == []
    assert manifest["workspace_ids"] == []
    assert "--resume: reusing 23 table(s), re-pulling 26" in logs
    assert "billing__usage: re-pulling (--resume: workspace filter 1111, this run all)" in logs


def test_resume_treats_the_same_workspace_ids_in_another_order_as_the_same_filter(tmp_path):
    run(tmp_path, ["--days", "30", "--workspace", "2222", "1111"])
    manifest, logs, conn = run(tmp_path, ["--days", "30", "--workspace", "1111", "2222", "--resume"])
    assert conn.statements == []
    assert conn.closed
    assert manifest["tables"]["billing__usage"]["workspace_ids"] == ["1111", "2222"]
    assert manifest["workspace_ids"] == ["1111", "2222"]
    assert "--resume: reusing 49 table(s), re-pulling 0" in logs
    assert any("; 49 reused and 0 re-pulled by --resume) -> " in line for line in logs)


def test_resume_re_pulls_a_table_whose_files_do_not_match_its_entry(tmp_path):
    """dbt reads the folder, so an entry is reused only when the parquet on disk is the set it
    describes. This is also what an interrupted run leaves behind: the table it was writing had
    its folder cleared and holds a half-written file with no footer, while manifest.json still
    carries that table's entry from the run before."""
    out = tmp_path / "out"
    run(tmp_path, ["--days", "30"], handlers={"billing__usage": lambda sql: usage_rows(4)})
    for f in (out / "billing__usage").glob("*.parquet"):
        f.unlink()                                                             # nothing on disk
    pq.write_table(ARROW_SCHEMA["compute__clusters"].empty_table(),
                   out / "compute__clusters" / "part-00001.parquet")             # one file too many
    (out / "lakeflow__jobs" / "part-00000.parquet").write_bytes(b"PAR1 cut short")  # no footer
    pq.write_table(pa.table({"workspace_id": pa.array(["1111"]), "statement_id": pa.array(["s1"])}),
                   out / "query__history" / "part-00000.parquet")              # 1 row, entry says 0

    manifest, logs, conn = run(tmp_path, ["--days", "30", "--resume"],
                               handlers={"billing__usage": lambda sql: usage_rows(3)})
    assert sorted(table_key(s) for s in conn.statements) == [
        "billing__usage", "compute__clusters", "lakeflow__jobs", "query__history"]
    assert "billing__usage: re-pulling (--resume: no parquet on disk)" in logs
    assert ("compute__clusters: re-pulling (--resume: 2 parquet file(s) on disk, "
            "the entry records 1)") in logs
    assert "lakeflow__jobs: re-pulling (--resume: a parquet file on disk is unreadable)" in logs
    assert "query__history: re-pulling (--resume: 1 row(s) on disk, the entry records 0)" in logs
    for key in ("billing__usage", "compute__clusters", "lakeflow__jobs", "query__history"):
        assert manifest["tables"][key]["state"] == "ok", key
    assert manifest["tables"]["billing__usage"]["rows"] == 3
    assert len(list((out / "compute__clusters").glob("*.parquet"))) == 1


def test_resume_re_pulls_a_table_whose_recorded_scope_is_missing_or_different(tmp_path):
    out = tmp_path / "out"
    run(tmp_path, ["--days", "30"])

    def edit(doc):
        t = doc["tables"]
        del t["billing__usage"]["as_of"]                          # e.g. a pre-T-74 manifest
        t["access__audit"]["as_of"] = "2026-09-21T23:59:59"       # exported the day before
        t["query__history"]["time_column"] = "end_time"           # a plan override changed since
        del t["compute__clusters"]["workspace_ids"]

    _edit_manifest(out, edit)
    manifest, logs, conn = run(tmp_path, ["--days", "30", "--resume"])
    assert sorted(table_key(s) for s in conn.statements) == [
        "access__audit", "billing__usage", "compute__clusters", "query__history"]
    assert "billing__usage: re-pulling (--resume: no valid as_of recorded for it)" in logs
    assert ("access__audit: re-pulling (--resume: exported for as_of_date 2026-09-21, "
            "this run is 2026-09-22)") in logs
    assert "query__history: re-pulling (--resume: time column end_time, this run start_time)" in logs
    assert "compute__clusters: re-pulling (--resume: no workspace filter recorded for it)" in logs
    assert manifest["tables"]["billing__usage"]["as_of"] == "2026-09-22T10:30:00"
    assert manifest["tables"]["compute__clusters"]["workspace_ids"] == []


def test_interrupted_run_leaves_a_manifest_that_resume_continues_from(tmp_path):
    out = tmp_path / "out"

    def interrupt(sql: str):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, ["--only", "billing,compute"], handlers={"compute__clusters": interrupt})
    partial = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    # Everything sorted before compute__clusters was written and described; nothing after it.
    assert sorted(partial["tables"]) == sorted(k for k in snap.PREDICATES if k < "compute__clusters")
    assert all(e["state"] in ("ok", "not_assessed") for e in partial["tables"].values())
    assert partial["tables"]["billing__usage"]["state"] == "ok"

    manifest, logs, conn = run(tmp_path, ["--only", "billing,compute", "--resume"])
    assert conn.statements_for("billing__usage") == []
    assert len(conn.statements_for("compute__clusters")) == 1
    assert sorted(manifest["tables"]) == sorted(snap.PREDICATES)
    assert manifest["tables"]["compute__clusters"]["state"] == "ok"
    assert _reused_line("billing__usage", "2026-09-22T10:30:00") in logs
    assert "compute__clusters: re-pulling (--resume: no entry in the existing manifest)" in logs
    assert manifest["as_of"] == "2026-09-22T10:30:00"


def test_resume_without_existing_manifest_exports_everything(tmp_path):
    manifest, logs, conn = run(tmp_path, ["--resume"])
    assert len(conn.statements) == 49
    assert any("no existing manifest" in line for line in logs)
    assert all(e["state"] == "ok" for e in manifest["tables"].values())


def test_excluded_tables_keep_existing_data_or_get_typed_empty_parquet(tmp_path):
    out = tmp_path / "out"
    run(tmp_path, ["--only", "billing.usage"], handlers={"billing__usage": lambda sql: usage_rows(2)})
    first = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert first["tables"]["billing__usage"]["state"] == "ok"
    excluded = first["tables"]["compute__clusters"]
    assert excluded["state"] == "not_assessed" and excluded["reason"] == "excluded_by_flag"
    assert (out / "compute__clusters" / "_MISSING.json").exists()
    (empty,) = sorted((out / "compute__clusters").glob("*.parquet"))
    assert pq.read_schema(empty).equals(ARROW_SCHEMA["compute__clusters"])

    # A second, differently-scoped run keeps the billing data and its manifest entry.
    second, _, conn = run(tmp_path, ["--only", "compute.clusters"])
    assert conn.statements_for("billing__usage") == []
    assert second["tables"]["billing__usage"] == first["tables"]["billing__usage"]
    assert second["tables"]["compute__clusters"]["state"] == "ok"
    assert not (out / "compute__clusters" / "_MISSING.json").exists()
    assert (out / "billing__usage").exists() and sorted((out / "billing__usage").glob("*.parquet"))


# ------------------------------------------------------------------------------------------
# config/snapshot_plan.yml override (DEC-10): optional, schema-compatible values only
# ------------------------------------------------------------------------------------------
def test_plan_override_is_optional_and_only_schema_compatible(tmp_path):
    plan = tmp_path / "snapshot_plan.yml"
    plan.write_text(
        "tables:\n"
        "  billing__usage: {time_column: usage_date, retention_days: 730, has_workspace_id: true}\n"
        "  system.query.history: {time_column: end_time, retention_days: 200, has_workspace_id: true}\n"
        "  compute.node_timeline: {time_column: no_such_column, retention_days: 45, has_workspace_id: true}\n"
        "  compute__clusters: {time_column: change_time, retention_days: 10, has_workspace_id: true}\n"
        "  access__inbound_network: {time_column: null, retention_days: -5, has_workspace_id: false}\n"
        "  nosuch__table: {time_column: x, retention_days: 1, has_workspace_id: false}\n",
        encoding="utf-8",
    )
    logs: list[str] = []
    schemas, _ = snap.load_arrow_schemas()
    preds = snap.load_plan_overrides(plan, snap.PREDICATES, schemas, logs.append)
    assert preds["billing__usage"].cap_days == 730                    # longer retention accepted
    assert preds["query__history"].time_column == "end_time"           # existing column accepted
    assert preds["query__history"].cap_days == 200
    assert preds["compute__node_timeline"].time_column == "start_time"  # unknown column ignored
    assert preds["compute__node_timeline"].cap_days == 45              # but the cap still applies
    assert preds["compute__clusters"] == snap.PREDICATES["compute__clusters"]   # full stays full
    assert preds["access__inbound_network"] == snap.PREDICATES["access__inbound_network"]
    assert "nosuch__table" not in preds
    assert any("unknown table nosuch__table" in l for l in logs)
    assert any("no_such_column" in l for l in logs)
    assert any("full-history table compute__clusters" in l for l in logs)
    assert snap.load_plan_overrides(tmp_path / "absent.yml", snap.PREDICATES, schemas, logs.append) == snap.PREDICATES

    # End to end: the override changes the cap the exporter applies.
    manifest, _, conn = run(tmp_path, ["--days", "400", "--only", "billing.usage,query.history"], plan_path=plan)
    assert manifest["tables"]["billing__usage"]["days_effective"] == 400
    assert manifest["tables"]["query__history"]["days_effective"] == 200
    (stmt,) = conn.statements_for("query__history")
    assert "end_time >= TIMESTAMP" in stmt


def test_plan_override_accepts_a_list_shape(tmp_path):
    plan = tmp_path / "plan.yml"
    plan.write_text(
        "- {schema: billing, table: usage, time_column: usage_date, retention_days: 900}\n"
        "- {name: access.audit, time_column: event_time, retention_days: null}\n",
        encoding="utf-8",
    )
    schemas, _ = snap.load_arrow_schemas()
    preds = snap.load_plan_overrides(plan, snap.PREDICATES, schemas, None)
    assert preds["billing__usage"].cap_days == 900
    assert preds["access__audit"].time_column == "event_time" and preds["access__audit"].cap_days == 365


def test_repo_plan_file_if_present_loads_cleanly():
    """If T-05 has already generated config/snapshot_plan.yml, the exporter must accept its
    shape without warnings about unknown tables or full-history contradictions."""
    if not snap.PLAN_OVERRIDE_PATH.exists():
        pytest.skip("config/snapshot_plan.yml not generated yet (T-05)")
    logs: list[str] = []
    schemas, _ = snap.load_arrow_schemas()
    preds = snap.load_plan_overrides(snap.PLAN_OVERRIDE_PATH, snap.PREDICATES, schemas, logs.append)
    assert set(preds) == set(snap.PREDICATES)
    assert not [l for l in logs if "warning" in l], logs


# ------------------------------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------------------------------
def test_main_scrubs_unexpected_connect_errors_and_exits_1(tmp_path, monkeypatch, capsys):
    token = "dapiFAKE0123456789abcdef0123456789abcdef"
    monkeypatch.setenv(snap.ENV_TOKEN, token)
    monkeypatch.setattr(snap, "DOTENV_PATH", tmp_path / "absent.env")

    def auth_failure(timeout=600, dotenv_path=None):
        raise RuntimeError(f"Error during request to server: 401 {{'Authorization': 'Bearer {token}'}}")

    monkeypatch.setattr(snap, "connect", auth_failure)
    rc = snap.main(["--out", str(tmp_path / "out"), "--days", "1"])
    captured = capsys.readouterr()
    assert rc == 1
    assert token not in captured.err and token not in captured.out
    assert "error: RuntimeError:" in captured.err and "***" in captured.err
    assert "Traceback" not in captured.err
    assert not (tmp_path / "out").exists()


def test_main_reports_config_errors_without_connecting(tmp_path, monkeypatch, capsys):
    for name in snap.CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(snap, "DOTENV_PATH", tmp_path / "absent.env")

    def refuse(timeout=600, dotenv_path=None):
        raise snap.SnapshotConfigError("missing credential env vars: DATABRICKS_TOKEN")

    monkeypatch.setattr(snap, "connect", refuse)     # resolved at call time by run_export
    rc = snap.main(["--out", str(tmp_path / "out"), "--days", "1"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "missing credential env vars" in err
    assert not (tmp_path / "out" / "manifest.json").exists()
