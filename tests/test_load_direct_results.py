"""tools/load_direct_results.py, loading a folder a faked tools/export_direct_results.py run wrote."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import re
import sys
import zipfile
from pathlib import Path

import duckdb
import pyarrow as pa
import pytest

ROOT = Path(__file__).resolve().parent.parent
EXPORT_MODULE_PATH = ROOT / "tools" / "export_direct_results.py"
LOAD_MODULE_PATH = ROOT / "tools" / "load_direct_results.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


edr = _load(EXPORT_MODULE_PATH, "export_direct_results_for_load_test")
ldr = _load(LOAD_MODULE_PATH, "load_direct_results_under_test")

from app.core import data as app_core_data  # noqa: E402

NOW = dt.datetime(2026, 9, 25, 12, 0, 0, tzinfo=dt.timezone.utc)
FAKE_ENV = {
    "DATABRICKS_SERVER_HOSTNAME": "acme-test.cloud.databricks.com",
    "DATABRICKS_HTTP_PATH": "/sql/1.0/warehouses/abc123",
    "DATABRICKS_TOKEN": "dapi0123456789abcdef0123456789ab",
    "AUDIT_DBX_CATALOG": "test_catalog",
}

_MARKER_RE = re.compile(r"__MARKER_(\w+)__")
_LIMIT_RE = re.compile(r"LIMIT (\d+)\s*$")


# ------------------------------------------------------------------------------------------
# The fake connector, same shape as tests/test_export_direct_results.py's.
# ------------------------------------------------------------------------------------------
class FakeCursor:
    def __init__(self, conn: "FakeConnection"):
        self.conn = conn
        self._pending: pa.Table | None = None
        self.description = None

    def execute(self, sql: str, parameters=None):
        self.conn.statements.append(sql)
        result = self.conn.handler(sql)
        self._pending = result
        self.description = [(f.name,) for f in result.schema]

    def fetchmany_arrow(self, n: int):
        if self._pending is None:
            return None
        t, self._pending = self._pending, None
        return t

    def fetchall(self):
        return []

    def close(self):
        pass


class FakeConnection:
    def __init__(self, handler, statements: list[str]):
        self.handler = handler
        self.statements = statements

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self):
        pass


class FakeConnected:
    def __init__(self, conn: FakeConnection):
        self.conn = conn


def make_connect_fn(handler):
    statements: list[str] = []

    def connect_fn():
        return FakeConnected(FakeConnection(handler, statements))

    return connect_fn


def fake_compile(scope_ids, roster, windows, env):
    return {qid: {"sql": f"SELECT * FROM __MARKER_{qid}__"} for qid in scope_ids}


def marker_handler(datasets: dict[str, pa.Table], failing: set[str] = frozenset()):
    def handler(sql: str) -> pa.Table:
        m = _MARKER_RE.search(sql)
        assert m, f"no query marker in SQL: {sql}"
        qid = m.group(1)
        if qid in failing:
            raise RuntimeError("TABLE_OR_VIEW_NOT_FOUND: no such table")
        table = datasets[qid]
        lim = _LIMIT_RE.search(sql)
        n = int(lim.group(1)) if lim else table.num_rows
        return table.slice(0, n)

    return handler


def _cost_by_job_rows() -> pa.Table:
    return pa.table({
        "window_days": pa.array([7, 7, 7], type=pa.int64()),
        "workspace_id": pa.array(["1111", "1111", "2222"], type=pa.string()),
        "job_id": pa.array(["10", "20", "30"], type=pa.string()),
        "job_name": pa.array(["nightly_etl", "hourly_sync", "adhoc"], type=pa.string()),
        "net_usage_quantity": pa.array([100.0, 50.0, 10.0], type=pa.float64()),
        "status": pa.array(["CRITICAL", "WARN", "OK"], type=pa.string()),
        "owner": pa.array(["alice@example.com", "bob@example.com", None], type=pa.string()),
    })


def _dim_job_rows() -> pa.Table:
    return pa.table({
        "workspace_id": pa.array(["1111", "1111"], type=pa.string()),
        "job_id": pa.array(["10", "20"], type=pa.string()),
        "name": pa.array(["nightly_etl", "hourly_sync"], type=pa.string()),
    })


def _make_export(tmp_path: Path, *, with_names: bool = False) -> Path:
    """cost_by_job exports 3 rows; cost_by_notebook fails on Databricks."""
    datasets = {"cost_by_job": _cost_by_job_rows()}
    names_sql: dict[str, str] = {}
    if with_names:
        datasets["dim_job"] = _dim_job_rows()
        names_sql = {"dim_job": "SELECT * FROM __MARKER_dim_job__"}
    handler = marker_handler(datasets, failing={"cost_by_notebook"})
    connect_fn = make_connect_fn(handler)
    out_dir = tmp_path / "export"
    edr.run_export(
        out_dir, only=["cost_by_job", "cost_by_notebook"], max_rows=50, windows=7, threads=1,
        connect_fn=connect_fn, compile_fn=fake_compile, env=dict(FAKE_ENV), now=NOW,
        names_sql=names_sql, tags_sql={},
    )
    return out_dir


# ------------------------------------------------------------------------------------------
# load_direct_results.py's own behaviour
# ------------------------------------------------------------------------------------------


def test_db_path_inside_repo_is_refused():
    assert ldr.db_path_allowed(ROOT / "somewhere.duckdb") is False
    assert ldr.db_path_allowed(ROOT / "data" / "x.duckdb") is True
    assert ldr.db_path_allowed(ROOT / "data" / "sub" / "x.duckdb") is False
    assert ldr.db_path_allowed(ROOT / "data" / "db_audit.duckdb") is False
    assert ldr.db_path_allowed(ROOT / "data" / "x.txt") is False


def test_load_folder_requires_manifest(tmp_path):
    with pytest.raises(ldr.LoadConfigError):
        ldr.load_folder(tmp_path / "does-not-exist", tmp_path / "out.duckdb")


def test_load_folder_shape(tmp_path):
    export_dir = _make_export(tmp_path)
    result = ldr.load_folder(export_dir, tmp_path / "loaded.duckdb")

    assert "cost_by_job" in result["loaded"]
    assert "cost_by_notebook" in result["not_built"]

    con = duckdb.connect(str(tmp_path / "loaded.duckdb"), read_only=True)
    try:
        rows = con.execute('SELECT count(*) FROM findings."f_cost_by_job"').fetchone()[0]
        # A 7-day export: 3 real rows at window_days=7, plus a copy at 30 and at 90 (both alias 7).
        assert rows == 9
        finding_tables = {
            r[0] for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'findings'"
            ).fetchall()
        }
        assert "f_cost_by_notebook" not in finding_tables
        dim_tables = {
            r[0] for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'dims'"
            ).fetchall()
        }
        assert dim_tables == set(ldr.DIM_TABLE_NAMES)
        meta = con.execute("SELECT as_of, tool_version FROM direct_export_meta").fetchone()
        assert meta[1] == edr.TOOL_VERSION
    finally:
        con.close()


# ------------------------------------------------------------------------------------------
# Round trip: fake export -> load -> app.core.data
# ------------------------------------------------------------------------------------------


def test_round_trip_app_core_data_reads_loaded_and_failed_findings(tmp_path, monkeypatch):
    export_dir = _make_export(tmp_path)
    db_path = tmp_path / "loaded.duckdb"
    result = ldr.load_folder(export_dir, db_path)

    monkeypatch.setenv("AUDIT_DB", str(db_path))
    monkeypatch.setenv("AUDIT_RUN_RESULTS", str(result["run_results_path"]))
    monkeypatch.setenv("AUDIT_SNAPSHOT_MANIFEST", str(tmp_path / "no-such-manifest.json"))

    assert app_core_data.db_state()["state"] == "ready"

    cols = app_core_data.finding_columns("cost_by_job")
    assert set(cols) == {
        "window_days", "workspace_id", "job_id", "job_name", "net_usage_quantity", "status", "owner",
    }

    found = app_core_data.read_finding("cost_by_job", window_days=7)
    assert found.rows_total == 3
    assert len(found.df) == 3
    assert "alice@example.com" in set(found.df["owner"].dropna())  # not masked by default
    assert sorted(found.df["job_name"]) == ["adhoc", "hourly_sync", "nightly_etl"]

    with pytest.raises(app_core_data.FindingNotBuiltError):
        app_core_data.finding_columns("cost_by_notebook")

    ok_status = app_core_data.finding_status("cost_by_job")
    assert ok_status["status"] == "success"

    failed_status = app_core_data.finding_status("cost_by_notebook")
    assert failed_status["status"] in ("error", "not_built")
    if failed_status["status"] == "error":
        assert failed_status["message"]  # the Databricks error text, not swallowed

    assert app_core_data.direct_export_info() == {
        "as_of": "2026-09-25T12:00:00Z",
        "window_coverage": {"7": 7, "30": 7, "90": 7},
        "windows": [7, 30, 90],
        "window_aliases": {"30": "7", "90": "7"},
        "tags_sources_not_exported": [],
        "as_of_date": "2026-09-25", "includes_today": False,
    }
    # No snapshot manifest here, so the money cut-off and window end come from the export's as_of.
    assert app_core_data.cost_cutoff() == {
        "data_through": "2026-09-24", "partial_day": "2026-09-25", "partial_until": "12:00",
    }
    from app.api import app as api_app

    meta = api_app.get_meta()
    assert meta["as_of_date"] == "2026-09-25"
    assert meta["direct_export"] == {
        "as_of": "2026-09-25T12:00:00Z",
        "window_coverage": {"7": 7, "30": 7, "90": 7},
        "windows": [7, 30, 90],
        "window_aliases": {"30": "7", "90": "7"},
        "tags_sources_not_exported": [],
        "as_of_date": "2026-09-25", "includes_today": False,
    }


def test_load_folder_loads_real_names(tmp_path):
    export_dir = _make_export(tmp_path, with_names=True)
    result = ldr.load_folder(export_dir, tmp_path / "loaded.duckdb")
    assert result["dims_available"] == ["dim_job"]
    assert set(result["dims_missing"]) == {"dim_cluster", "dim_pipeline", "dim_warehouse", "dim_workspace", "dim_notebook"}

    con = duckdb.connect(str(tmp_path / "loaded.duckdb"), read_only=True)
    try:
        rows = con.execute('SELECT job_id, name FROM dims.dim_job ORDER BY job_id').fetchall()
        assert rows == [("10", "nightly_etl"), ("20", "hourly_sync")]
    finally:
        con.close()


def _dim_workspace_rows() -> pa.Table:
    return pa.table({
        "workspace_id": pa.array(["1111", "2222"], type=pa.string()),
        "name": pa.array(["prod-emea", "mystery-ws"], type=pa.string()),
        "url": pa.array(["https://a", "https://b"], type=pa.string()),
        "in_snapshot_region": pa.array([True, True], type=pa.bool_()),
        "billed_in_snapshot": pa.array([True, False], type=pa.bool_()),
    })


def test_load_folder_classifies_workspace_env_by_name(tmp_path):
    handler = marker_handler({"cost_by_job": _cost_by_job_rows(), "dim_workspace": _dim_workspace_rows()})
    out_dir = tmp_path / "export"
    edr.run_export(
        out_dir, only=["cost_by_job"], threads=1, connect_fn=make_connect_fn(handler),
        compile_fn=fake_compile, env=dict(FAKE_ENV), now=NOW,
        names_sql={"dim_workspace": "SELECT * FROM __MARKER_dim_workspace__"}, tags_sql={},
    )
    result = ldr.load_folder(out_dir, tmp_path / "loaded.duckdb")
    assert result["dims_available"] == ["dim_workspace"]

    con = duckdb.connect(str(tmp_path / "loaded.duckdb"), read_only=True)
    try:
        rows = {
            r[0]: r[1:] for r in con.execute(
                "SELECT workspace_id, env, env_source FROM dims.dim_workspace"
            ).fetchall()
        }
        assert rows["1111"] == ("prod", "name")
        assert rows["2222"] == ("unknown", "none")
    finally:
        con.close()


def test_loader_adds_one_column_set_per_top_tag(monkeypatch):
    from app.core import config as app_config

    monkeypatch.setattr(app_config, "load_top_tags", lambda: {"cost_center": "Cost center", "domain": "Domain"})
    con = duckdb.connect()
    con.execute("CREATE SCHEMA dims; CREATE SCHEMA tags")
    con.execute(
        "CREATE TABLE dims.dim_workspace AS SELECT * FROM (VALUES ('1', TRUE), ('2', TRUE), ('3', FALSE)) "
        "t(workspace_id, billed_in_snapshot)"
    )
    con.execute(
        "CREATE TABLE tags.tag_workspace AS SELECT * FROM (VALUES "
        "('1', 'costcenter', 'finance', 0.9, 0.8, 'r1'), ('2', 'costcenter', '__mixed__', 0.5, 0.8, 'r2'), "
        "('1', 'domain', '__untagged__', 1.0, 0.1, 'r3')) t(workspace_id, tag_key, tag_value, share, coverage, reason)"
    )
    ldr._add_workspace_attributes(con)
    rows = {r[0]: r[1:] for r in con.execute("SELECT workspace_id, cost_center, domain FROM dims.dim_workspace").fetchall()}
    assert rows == {"1": ("finance", "not_tagged"), "2": ("mixed", "not_tagged"), "3": ("no_usage", "no_usage")}
    assert "domain_reason" in [r[0] for r in con.execute("DESCRIBE dims.dim_workspace").fetchall()]


def test_tag_tables_load_from_parquet_with_utc_timestamps_and_window_aliases(tmp_path):
    import pyarrow.parquet as pq

    out_dir = tmp_path / "export"
    (out_dir / "tags").mkdir(parents=True)
    seen = dt.datetime(2026, 9, 24, 23, 30, tzinfo=dt.timezone(dt.timedelta(hours=-2)))
    pq.write_table(pa.table({"tag_key": ["team"], "last_seen": pa.array([seen], pa.timestamp("us", tz="-02:00"))}),
                   out_dir / "tags" / "tag_index.parquet")
    pq.write_table(pa.table({"window_days": pa.array([30], pa.int32()), "unit_id": ["u1"], "usd": [2.5]}),
                   out_dir / "tags" / "cost_unit.parquet")
    (out_dir / "manifest.json").write_text(json.dumps(
        {"findings": {}, "tags": {"available": ["tag_index", "cost_unit"]}, "window_aliases": {"90": "30"}}))

    result = ldr.load_folder(out_dir, tmp_path / "loaded.duckdb")
    assert result["tags_available"] == ["tag_index", "cost_unit"]
    con = duckdb.connect(str(tmp_path / "loaded.duckdb"), read_only=True)
    try:
        assert con.execute("SELECT last_seen, typeof(last_seen) FROM tags.tag_index").fetchone() == (
            dt.datetime(2026, 9, 25, 1, 30), "TIMESTAMP")
        assert con.execute("SELECT window_days, usd FROM tags.cost_unit ORDER BY 1").fetchall() == [(30, 2.5), (90, 2.5)]
    finally:
        con.close()


def test_truncated_flag_flows_to_run_results_and_the_api(tmp_path, monkeypatch):
    handler = marker_handler({"cost_by_job": _cost_by_job_rows()})
    connect_fn = make_connect_fn(handler)
    out_dir = tmp_path / "export"
    edr.run_export(
        out_dir, only=["cost_by_job"], max_rows=2, windows=7, threads=1, connect_fn=connect_fn,
        compile_fn=fake_compile, env=dict(FAKE_ENV), now=NOW, names_sql={}, tags_sql={},
    )
    db_path = tmp_path / "loaded.duckdb"
    result = ldr.load_folder(out_dir, db_path)
    assert result["truncated"] == ["cost_by_job"]
    assert result["max_rows"] == 2

    rr = json.loads(result["run_results_path"].read_text(encoding="utf-8"))
    message = next(r["message"] for r in rr["results"] if r["unique_id"].endswith("f_cost_by_job"))
    assert message == "kept the first 2 rows per window"

    monkeypatch.setenv("AUDIT_DB", str(db_path))
    monkeypatch.setenv("AUDIT_RUN_RESULTS", str(result["run_results_path"]))
    monkeypatch.setenv("AUDIT_SNAPSHOT_MANIFEST", str(tmp_path / "no-such-manifest.json"))

    assert app_core_data.truncation_info("cost_by_job") == {"truncated": True, "max_rows": 2}

    from fastapi.testclient import TestClient

    from app.api.app import app as fastapi_app

    client = TestClient(fastapi_app)
    body = client.get("/api/finding/cost_by_job", params={"window": 7}).json()
    assert body["outcome"] == "ok_rows"
    assert body["truncated"] is True
    assert body["truncated_max_rows"] == 2

    agg = client.get(
        "/api/finding/cost_by_job/aggregate",
        params={"window": 7, "agg": "sum", "value": "net_usage_quantity"},
    ).json()
    assert agg["outcome"] == "ok_rows"
    assert agg["truncated"] is True
    assert agg["truncated_max_rows"] == 2


def test_truncation_is_per_window_not_folded_across_the_whole_id(tmp_path, monkeypatch):
    """Only the 30d run hits --max-rows; the 7d view must not read truncated too."""
    def handler(sql: str) -> pa.Table:
        assert re.search(r"__MARKER_cost_by_job__", sql)
        days = int(re.search(r"days=(\d+)", sql).group(1))
        n = 3 if days == 7 else 20
        table = pa.table({
            "window_days": pa.array([days] * n, type=pa.int64()),
            "net_usage_quantity": pa.array(list(range(n)), type=pa.int64()),
        })
        lim = re.search(r"LIMIT (\d+)\s*$", sql)
        return table.slice(0, int(lim.group(1))) if lim else table

    def compile_with_days(scope_ids, roster, windows, env):
        return {qid: {"sql": f"SELECT * FROM __MARKER_{qid}__ -- days={windows}"} for qid in scope_ids}

    connect_fn = make_connect_fn(handler)
    out_dir = tmp_path / "export"
    edr.run_export(
        out_dir, only=["cost_by_job"], max_rows=10, windows=10, threads=1, connect_fn=connect_fn,
        compile_fn=compile_with_days, env=dict(FAKE_ENV), now=NOW, names_sql={},
    )
    db_path = tmp_path / "loaded.duckdb"
    result = ldr.load_folder(out_dir, db_path)
    assert result["truncated"] == ["cost_by_job"]  # folded id-level flag: at least one window cut

    # 90 aliases 30 (both share days_used=10 for a 10-day export): the loader copies 30's own 10
    # rows in under window_days=90, on top of the 3 real rows at 7 -- 23 rows total, no 90 query.
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        counts = dict(con.execute(
            'SELECT window_days, count(*) FROM findings."f_cost_by_job" GROUP BY window_days'
        ).fetchall())
    finally:
        con.close()
    assert counts == {7: 3, 30: 10, 90: 10}

    monkeypatch.setenv("AUDIT_DB", str(db_path))
    monkeypatch.setenv("AUDIT_RUN_RESULTS", str(result["run_results_path"]))
    monkeypatch.setenv("AUDIT_SNAPSHOT_MANIFEST", str(tmp_path / "no-such-manifest.json"))

    assert app_core_data.truncation_info("cost_by_job", 7) == {"truncated": False, "max_rows": 10}
    assert app_core_data.truncation_info("cost_by_job", 30) == {"truncated": True, "max_rows": 10}
    # 90 inherits 30's truncation too -- an alias is exactly as truncated as its source.
    assert app_core_data.truncation_info("cost_by_job", 90) == {"truncated": True, "max_rows": 10}


def test_direct_export_info_returns_none_on_connect_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIT_DB", str(tmp_path / "no-such.duckdb"))

    def boom():
        raise duckdb.IOException("simulated: could not open database file")

    monkeypatch.setattr(app_core_data, "_connect", boom)
    assert app_core_data.direct_export_info() is None


# ------------------------------------------------------------------------------------------
# A partial export (10 days of history): window_coverage in direct_export_meta, GET /api/meta's
# "windows"/"open_window", and a window the export never ran reading not_assessed.
# ------------------------------------------------------------------------------------------

_DAYS_RE = re.compile(r"days=(\d+)")


def _compile_with_days(scope_ids, roster, windows, env):
    """Bakes the marker fill (`windows`, i.e. days_used) into the SQL text, the way the real
    __WINDOW_DAYS__ replacement would -- so the fake connector can tell plan entries apart."""
    return {qid: {"sql": f"SELECT * FROM __MARKER_{qid}__ -- days={windows}"} for qid in scope_ids}


def _windowed_cost_by_job_handler(sql: str) -> pa.Table:
    m = _MARKER_RE.search(sql)
    assert m, f"no query marker in SQL: {sql}"
    days = int(_DAYS_RE.search(sql).group(1))
    return pa.table({
        "window_days": pa.array([days, days, days], type=pa.int64()),
        "workspace_id": pa.array(["1111", "1111", "2222"], type=pa.string()),
        "job_id": pa.array(["10", "20", "30"], type=pa.string()),
        "job_name": pa.array(["nightly_etl", "hourly_sync", "adhoc"], type=pa.string()),
        "net_usage_quantity": pa.array([100.0, 50.0, 10.0], type=pa.float64()),
        "status": pa.array(["CRITICAL", "WARN", "OK"], type=pa.string()),
        "owner": pa.array(["alice@example.com", "bob@example.com", None], type=pa.string()),
    })


def _make_partial_export(tmp_path: Path, *, days: int) -> Path:
    """window_plan(days) decides which of 7/30/90 actually run -- days=10 runs 7 (full) and 30
    (partial, 10 of 30 days) and never touches 90; days=5 runs only 7 (partial, 5 of 7 days)."""
    connect_fn = make_connect_fn(_windowed_cost_by_job_handler)
    out_dir = tmp_path / "export"
    edr.run_export(
        out_dir, only=["cost_by_job"], max_rows=50, windows=days, threads=1,
        connect_fn=connect_fn, compile_fn=_compile_with_days, env=dict(FAKE_ENV), now=NOW,
        names_sql={},
    )
    return out_dir


def test_loader_stores_window_coverage(tmp_path):
    export_dir = _make_partial_export(tmp_path, days=10)
    db_path = tmp_path / "loaded.duckdb"
    ldr.load_folder(export_dir, db_path)

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        row = con.execute("SELECT window_coverage, window_aliases FROM direct_export_meta").fetchone()
    finally:
        con.close()
    assert json.loads(row[0]) == {"7": 7, "30": 10, "90": 10}
    assert json.loads(row[1]) == {"90": "30"}


def _snapshot_config_rows() -> pa.Table:
    return pa.table({
        "window_days": pa.array([0, 0], type=pa.int64()),
        "warehouse_id": pa.array(["a", "b"], type=pa.string()),
    })


def _partial_export_with_snapshot_handler(sql: str) -> pa.Table:
    m = _MARKER_RE.search(sql)
    assert m, f"no query marker in SQL: {sql}"
    if m.group(1) == "sql_warehouse_config_current":
        return _snapshot_config_rows()
    return _windowed_cost_by_job_handler(sql)


def test_snapshot_check_window_days_unchanged_when_manifest_has_aliases(tmp_path):
    """A snapshot check's window_days=0 rows are never copied under an alias label."""
    connect_fn = make_connect_fn(_partial_export_with_snapshot_handler)
    out_dir = tmp_path / "export"
    edr.run_export(
        out_dir, only=["cost_by_job", "sql_warehouse_config_current"], max_rows=50, windows=10,
        threads=1, connect_fn=connect_fn, compile_fn=_compile_with_days, env=dict(FAKE_ENV),
        now=NOW, names_sql={},
    )
    db_path = tmp_path / "loaded.duckdb"
    ldr.load_folder(out_dir, db_path)

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        job_counts = dict(con.execute(
            'SELECT window_days, count(*) FROM findings."f_cost_by_job" GROUP BY window_days'
        ).fetchall())
        snapshot_counts = dict(con.execute(
            'SELECT window_days, count(*) FROM findings."f_sql_warehouse_config_current" '
            "GROUP BY window_days"
        ).fetchall())
    finally:
        con.close()
    assert job_counts == {7: 3, 30: 3, 90: 3}  # 90 aliases 30
    assert snapshot_counts == {0: 2}  # unaffected by window_aliases


def test_meta_windows_reports_every_window_available_and_partial(tmp_path, monkeypatch):
    """A 10-day export: 7d is full, 30d is partial, 90d aliases 30d and is partial too."""
    export_dir = _make_partial_export(tmp_path, days=10)
    db_path = tmp_path / "loaded.duckdb"
    result = ldr.load_folder(export_dir, db_path)

    monkeypatch.setenv("AUDIT_DB", str(db_path))
    monkeypatch.setenv("AUDIT_RUN_RESULTS", str(result["run_results_path"]))
    monkeypatch.setenv("AUDIT_SNAPSHOT_MANIFEST", str(tmp_path / "no-such-manifest.json"))

    assert app_core_data.exported_window_coverage() == {7: 7, 30: 10, 90: 10}

    from app.api import app as api_app

    meta = api_app.get_meta()
    assert meta["windows"] == [
        {"days": 7, "available": True, "covered_days": 7, "partial": False},
        {"days": 30, "available": True, "covered_days": 10, "partial": True},
        {"days": 90, "available": True, "covered_days": 10, "partial": True},
    ]
    assert meta["snapshot_days"] == 10
    assert meta["default_window"] == 30
    assert meta["open_window"] == 30


def test_meta_open_window_opens_on_default_when_it_is_only_partial(tmp_path, monkeypatch):
    """A 5-day export: every window is available (7/30/90 all alias the 5-day run), so
    open_window opens on default_window (30) even though it is partial, never falls back to 7."""
    export_dir = _make_partial_export(tmp_path, days=5)
    db_path = tmp_path / "loaded.duckdb"
    result = ldr.load_folder(export_dir, db_path)

    monkeypatch.setenv("AUDIT_DB", str(db_path))
    monkeypatch.setenv("AUDIT_RUN_RESULTS", str(result["run_results_path"]))
    monkeypatch.setenv("AUDIT_SNAPSHOT_MANIFEST", str(tmp_path / "no-such-manifest.json"))

    from app.api import app as api_app

    meta = api_app.get_meta()
    assert meta["windows"][0] == {"days": 7, "available": True, "covered_days": 5, "partial": True}
    assert [w["available"] for w in meta["windows"][1:]] == [True, True]
    assert meta["default_window"] == 30
    assert meta["open_window"] == 30


def test_open_window_still_falls_back_for_an_older_manifest_missing_a_window(tmp_path, monkeypatch):
    """The not_exported fallback stays for an export made before window_aliases existed: a
    window_coverage missing a label entirely (never every-label-present, as a fresh export always
    is now) still reads that label as unavailable, and open_window still falls back off it."""
    db_path = tmp_path / "old_export.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE direct_export_meta (as_of VARCHAR, windows VARCHAR, window_coverage VARCHAR)"
    )
    con.execute(
        "INSERT INTO direct_export_meta VALUES ('2026-09-25T12:00:00Z', '[7]', '{\"7\": 7}')"
    )
    con.close()
    monkeypatch.setenv("AUDIT_DB", str(db_path))
    monkeypatch.setenv("AUDIT_SNAPSHOT_MANIFEST", str(tmp_path / "no-such-manifest.json"))

    from app.api import app as api_app

    meta = api_app.get_meta()
    assert meta["windows"] == [
        {"days": 7, "available": True, "covered_days": 7, "partial": False},
        {"days": 30, "available": False, "covered_days": None, "partial": False},
        {"days": 90, "available": False, "covered_days": None, "partial": False},
    ]
    assert meta["default_window"] == 30
    assert meta["open_window"] == 7  # 30 was never exported -- open on the largest one that was


def test_finding_reads_ok_and_partial_for_a_window_that_aliases_another(tmp_path, monkeypatch):
    """A 10-day export: 90, aliasing 30, reads 30's own rows as ok_rows, partial at 10 days."""
    export_dir = _make_partial_export(tmp_path, days=10)  # 7 and 30 ran; 90 aliases 30
    db_path = tmp_path / "loaded.duckdb"
    result = ldr.load_folder(export_dir, db_path)

    monkeypatch.setenv("AUDIT_DB", str(db_path))
    monkeypatch.setenv("AUDIT_RUN_RESULTS", str(result["run_results_path"]))
    monkeypatch.setenv("AUDIT_SNAPSHOT_MANIFEST", str(tmp_path / "no-such-manifest.json"))

    from fastapi.testclient import TestClient

    from app.api.app import app as fastapi_app

    client = TestClient(fastapi_app)

    body = client.get("/api/finding/cost_by_job", params={"window": 90}).json()
    assert body["outcome"] == "ok_rows"
    assert body["rows_total"] == 3           # the 30d window's own 3 rows, copied in under 90
    assert body["window_coverage"]["partial"] is True
    assert body["window_coverage"]["covered_days"] == 10

    listed = client.get("/api/findings", params={"window": 90}).json()["findings"]
    row = next(r for r in listed if r["query_id"] == "cost_by_job")
    assert row["outcome"] == "ok_rows"
    assert row["partial"] is True

    # The 30d window WAS exported, but only 10 of its 30 days -- ok_rows, but window_coverage and
    # the findings-list `partial` flag must both say so (never a clean, full-window read).
    partial_body = client.get("/api/finding/cost_by_job", params={"window": 30}).json()
    assert partial_body["outcome"] == "ok_rows"
    assert partial_body["window_coverage"]["partial"] is True
    assert partial_body["window_coverage"]["covered_days"] == 10

    listed_30 = client.get("/api/findings", params={"window": 30}).json()["findings"]
    row_30 = next(r for r in listed_30 if r["query_id"] == "cost_by_job")
    assert row_30["outcome"] == "ok_rows"
    assert row_30["partial"] is True


def test_exported_window_coverage_falls_back_to_windows_when_null(tmp_path, monkeypatch):
    """An export made before window_coverage existed only has `windows` (the exporter's own
    labels) -- that window must still read as fully covered, never a clean-empty 7d/90d."""
    db_path = tmp_path / "old_export.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE direct_export_meta (as_of VARCHAR, windows VARCHAR, window_coverage VARCHAR)")
    con.execute("INSERT INTO direct_export_meta VALUES ('2026-09-25T12:00:00Z', '[30]', NULL)")
    con.close()
    monkeypatch.setenv("AUDIT_DB", str(db_path))
    monkeypatch.setenv("AUDIT_SNAPSHOT_MANIFEST", str(tmp_path / "no-such-manifest.json"))

    assert app_core_data.exported_window_coverage() == {30: 30}

    from app.api import app as api_app

    meta = api_app.get_meta()
    assert meta["windows"] == [
        {"days": 7, "available": False, "covered_days": None, "partial": False},
        {"days": 30, "available": True, "covered_days": 30, "partial": False},
        {"days": 90, "available": False, "covered_days": None, "partial": False},
    ]


# ------------------------------------------------------------------------------------------
# Import mode: a results folder made elsewhere (no manifest.json, CSV files), and a .zip of one
# ------------------------------------------------------------------------------------------


def test_load_a_hand_made_csv_folder_with_no_manifest(tmp_path):
    src = tmp_path / "hand_made"
    (src / "findings").mkdir(parents=True)
    (src / "findings" / "cost_by_job.csv").write_text(
        "window_days,workspace_id,job_id,updated_at\n"
        "7,1111,10,2026-09-25T10:00:00Z\n"
        "7,1111,20,2026-09-25 12:00:00\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "loaded.duckdb"
    result = ldr.load_folder(src, db_path)

    assert result["loaded"] == ["cost_by_job"]
    assert result["max_rows"] is None
    assert result["truncated"] == []

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            'SELECT workspace_id, job_id, updated_at FROM findings."f_cost_by_job" ORDER BY job_id'
        ).fetchall()
        assert rows[0][0] == "1111"  # workspace_id kept as VARCHAR, not sniffed to a number
        # both timestamps normalised to the same naive UTC instant, whether written with a Z or not
        assert rows[0][2] == dt.datetime(2026, 9, 25, 10, 0, 0)
        assert rows[1][2] == dt.datetime(2026, 9, 25, 12, 0, 0)
        dim_tables = {
            r[0] for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'dims'"
            ).fetchall()
        }
        assert dim_tables == set(ldr.DIM_TABLE_NAMES)
    finally:
        con.close()


def test_load_folder_with_no_recognised_files_is_rejected(tmp_path):
    src = tmp_path / "empty"
    src.mkdir()
    (src / "readme.txt").write_text("nothing here", encoding="utf-8")
    with pytest.raises(ldr.LoadConfigError, match="no recognised"):
        ldr.load_folder(src, tmp_path / "out.duckdb")


def test_load_folder_rejects_a_system_tables_snapshot(tmp_path):
    src = tmp_path / "snapshot"
    src.mkdir()
    (src / "manifest.json").write_text(json.dumps({"tables": {}}), encoding="utf-8")
    with pytest.raises(ldr.LoadConfigError, match="system-tables snapshot"):
        ldr.load_folder(src, tmp_path / "out.duckdb")


def test_load_a_zip_of_an_export(tmp_path):
    export_dir = _make_export(tmp_path, with_names=True)
    zip_path = tmp_path / "results.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for path in export_dir.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(export_dir))

    db_path = tmp_path / "loaded.duckdb"
    result = ldr.load_folder(zip_path, db_path)
    assert "cost_by_job" in result["loaded"]
    assert result["dims_available"] == ["dim_job"]


def test_load_a_zip_wrapped_in_one_top_folder(tmp_path):
    export_dir = _make_export(tmp_path)
    zip_path = tmp_path / "results.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for path in export_dir.rglob("*"):
            if path.is_file():
                zf.write(path, Path("audit_results_20260925") / path.relative_to(export_dir))

    result = ldr.load_folder(zip_path, tmp_path / "loaded.duckdb")
    assert "cost_by_job" in result["loaded"]


def test_unsafe_zip_entries_are_refused(tmp_path):
    zip_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("../escape.txt", "nope")
    with pytest.raises(ldr.LoadConfigError, match="unsafe"):
        ldr.load_folder(zip_path, tmp_path / "out.duckdb")


# ------------------------------------------------------------------------------------------
# main(): src and --db default to the data folder's results/ and audit.duckdb
# ------------------------------------------------------------------------------------------


def test_main_defaults_src_and_db_to_the_data_folder(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    captured = {}

    def fake_load_folder(src, db_path):
        captured["src"] = src
        captured["db_path"] = db_path
        db_path.touch()  # the real load writes the file; current_db() ignores a pointer to nothing
        return {
            "db_path": db_path, "run_results_path": db_path.with_name("audit.run_results.json"),
            "loaded": [], "not_built": [], "missing_file": [], "failed_load": [],
            "failed_on_databricks": {}, "dims_available": [], "dims_missing": [],
            "dims_note": None, "tags_available": [], "tags_missing": [], "tags_note": None,
            "as_of": None, "truncated": [], "max_rows": None,
        }

    monkeypatch.setattr(ldr, "load_folder", fake_load_folder)
    rc = ldr.main([])
    assert rc == 0
    assert captured["src"] == tmp_path / "results"
    assert captured["db_path"] == tmp_path / "audit.duckdb"
    out = capsys.readouterr().out
    assert "python -m app.api" in out
    assert "AUDIT_DB=" not in out  # the data folder db needs no env var to be served
    # a load into the data folder points the app at it, so the next start opens this db
    assert ldr.datasource.current_db() == (tmp_path / "audit.duckdb").resolve()


def test_main_refuses_a_data_folder_inside_the_repo(monkeypatch, capsys):
    monkeypatch.setenv("AUDIT_DATA_DIR", str(ROOT / "some_stray_data_dir"))
    rc = ldr.main([])
    assert rc == 2
    assert "inside the repository" in capsys.readouterr().err


def test_main_leaves_the_current_db_pointer_alone_for_a_db_outside_the_data_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path / "data"))
    out_db = tmp_path / "elsewhere" / "audit.duckdb"

    def fake_load_folder(src, db_path):
        return {
            "db_path": db_path, "run_results_path": db_path.with_name("audit.run_results.json"),
            "loaded": [], "not_built": [], "missing_file": [], "failed_load": [],
            "failed_on_databricks": {}, "dims_available": [], "dims_missing": [],
            "dims_note": None, "tags_available": [], "tags_missing": [], "tags_note": None,
            "as_of": None, "truncated": [], "max_rows": None,
        }

    monkeypatch.setattr(ldr, "load_folder", fake_load_folder)
    rc = ldr.main([str(tmp_path / "results"), "--db", str(out_db)])
    assert rc == 0
    assert ldr.datasource.current_db() is None


def test_no_manifest_fills_longer_windows_from_the_data(tmp_path):
    export_dir = _make_partial_export(tmp_path, days=7)
    (export_dir / "manifest.json").unlink()
    db_path = tmp_path / "loaded.duckdb"
    ldr.load_folder(export_dir, db_path)

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        windows = {r[0] for r in con.execute('SELECT DISTINCT window_days FROM findings."f_cost_by_job"').fetchall()}
    finally:
        con.close()
    assert windows == {7, 30, 90}


def test_tag_entity_keeps_the_top_keys_by_spend_plus_mandatory_and_top_tags(tmp_path, monkeypatch):
    (tmp_path / "settings.yml").write_text("mandatory_tag_keys: [environment]\nload:\n  top_tag_keys: 1\n", encoding="utf-8")
    (tmp_path / "tag_aliases.yml").write_text("top_tags:\n  team: Team\n", encoding="utf-8")
    monkeypatch.setenv("AUDIT_CONFIG_DIR", str(tmp_path))
    import pyarrow.parquet as pq
    entity = tmp_path / "tag_entity.parquet"
    pq.write_table(pa.table({"entity_type": ["cluster"] * 5, "tag_key": ["clusterid", "clusterid", "project", "env", "team"]}), entity)
    index = tmp_path / "tag_index.parquet"
    pq.write_table(pa.table({"tag_key": ["clusterid", "project"], "dbus": [1.0, 50.0]}), index)
    from app.core import config as app_config
    con = duckdb.connect()
    # project has the most spend; env counts as the mandatory environment; team is a top tag.
    assert ldr._tag_entity_keys(con, entity, index, app_config.load_settings()) == ["env", "environment", "project", "team"]


def test_abac_policies_load_with_what_the_export_asked(tmp_path):
    import pyarrow.parquet as pq
    export_dir = _make_export(tmp_path)
    (export_dir / "policies").mkdir()
    pq.write_table(pa.table({"policy_name": ["mask_ssn"], "policy_type": ["COLUMN MASK"], "catalog": ["main"],
                             "schema": ["hr"], "comment": [None], "on_type": ["SCHEMA"], "on_name": ["main.hr"]}),
                   export_dir / "policies" / "abac_policies.parquet")
    manifest = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["policies"] = {"exported": True, "rows": 1, "catalogs": 1, "schemas": 1, "not_readable": 0}
    (export_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    ldr.load_folder(export_dir, tmp_path / "loaded.duckdb")
    con = duckdb.connect(str(tmp_path / "loaded.duckdb"), read_only=True)
    try:
        assert con.execute("SELECT policy_name, on_name FROM governance.abac_policies").fetchall() == [("mask_ssn", "main.hr")]
        assert json.loads(con.execute("SELECT info FROM governance.abac_policies_info").fetchone()[0])["catalogs"] == 1
    finally:
        con.close()
