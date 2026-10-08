"""tools/export_direct_results.py, driven by a fake connector and a fake dbt compile."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import re
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "tools" / "export_direct_results.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


edr = _load(MODULE_PATH, "export_direct_results_under_test")

NOW = dt.datetime(2026, 9, 25, 10, 0, 0, tzinfo=dt.timezone.utc)
FAKE_TOKEN = "dapi0123456789abcdef0123456789ab"
FAKE_ENV = {
    "DATABRICKS_SERVER_HOSTNAME": "acme-test.cloud.databricks.com",
    "DATABRICKS_HTTP_PATH": "/sql/1.0/warehouses/abc123",
    "DATABRICKS_TOKEN": FAKE_TOKEN,
    "AUDIT_DBX_CATALOG": "test_catalog",
}

_MARKER_RE = re.compile(r"__MARKER_(\w+)__")
_LIMIT_RE = re.compile(r"LIMIT (\d+)\s*$")


# ------------------------------------------------------------------------------------------
# The fake connector
# ------------------------------------------------------------------------------------------
class FakeCursor:
    def __init__(self, conn: "FakeConnection"):
        self.conn = conn
        self._pending: pa.Table | None = None
        self.description = None

    def execute(self, sql: str, parameters=None):
        self.conn.statements.append(sql)
        result = self.conn.handler(sql)  # a pa.Table, or raises
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
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self):
        self.closed = True


class FakeConnected:
    def __init__(self, conn: FakeConnection):
        self.conn = conn


def make_connect_fn(handler):
    """A fresh connection per call, all sharing one statements log."""
    statements: list[str] = []

    def connect_fn():
        return FakeConnected(FakeConnection(handler, statements))

    connect_fn.statements = statements
    return connect_fn


def marker_handler(datasets: dict[str, pa.Table], failing: set[str] = frozenset()):
    def handler(sql: str) -> pa.Table:
        m = _MARKER_RE.search(sql)
        assert m, f"no query marker in SQL: {sql}"
        qid = m.group(1)
        if qid in failing:
            raise RuntimeError(f"TABLE_OR_VIEW_NOT_FOUND: no such table (token={FAKE_TOKEN})")
        table = datasets[qid]
        lim = _LIMIT_RE.search(sql)
        n = int(lim.group(1)) if lim else table.num_rows
        return table.slice(0, n)

    return handler


def fake_compile(scope_ids, roster, windows, env):
    """Compiled SQL that carries a marker marker_handler() dispatches on."""
    return {qid: {"sql": f"SELECT * FROM __MARKER_{qid}__"} for qid in scope_ids}


def test_tag_session_sets_utc_time_zone_before_the_first_query():
    """run_export's default connect_fn is tools_snapshot.connect, which tags every session -- the
    same one used for a snapshot -- so current_date()/current_timestamp() read UTC on both."""
    statements: list[str] = []

    class _FakeCursor:
        def execute(self, sql, parameters=None):
            statements.append(sql)

        def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    edr.tools_snapshot._tag_session(_FakeConn())
    statements.append("SELECT 1")  # the first real query a task would run
    assert "SET TIME ZONE 'UTC'" in statements
    assert statements.index("SET TIME ZONE 'UTC'") < statements.index("SELECT 1")


# ------------------------------------------------------------------------------------------
# 1. the repo-path refusal
# ------------------------------------------------------------------------------------------


def test_out_dir_inside_repo_is_refused():
    with pytest.raises(edr.ExportConfigError, match="inside the repository"):
        edr.run_export(ROOT / "this_should_never_be_created", only=["cost_by_job"], env=FAKE_ENV)


def test_preflight_env_defaults_catalog_when_unset(monkeypatch, tmp_path):
    monkeypatch.setattr(edr, "DOTENV_PATH", tmp_path / "no-such-.env")
    monkeypatch.delenv("AUDIT_DBX_CATALOG", raising=False)
    monkeypatch.setenv("DATABRICKS_SERVER_HOSTNAME", "h")
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/x")
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-fake")
    env = edr._preflight_env()
    assert env["AUDIT_DBX_CATALOG"] == edr.DEFAULT_COMPILE_CATALOG


def test_preflight_env_normalizes_a_host_pasted_with_a_scheme(monkeypatch, tmp_path):
    monkeypatch.setattr(edr, "DOTENV_PATH", tmp_path / "no-such-.env")
    monkeypatch.setenv("DATABRICKS_SERVER_HOSTNAME", "https://acme.cloud.databricks.com/extra?x=1")
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/x")
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-fake")
    monkeypatch.setenv("AUDIT_DBX_CATALOG", "main")
    env = edr._preflight_env()
    assert env["DATABRICKS_SERVER_HOSTNAME"] == "acme.cloud.databricks.com"


# ------------------------------------------------------------------------------------------
# 2. user masking (opt-in)
# ------------------------------------------------------------------------------------------

SP_UUID = "0a1b2c3d-4e5f-6a7b-8c9d-0e1f2a3b4c5d"


def _people_and_resources() -> pa.Table:
    return pa.table({
        "owner": pa.array(["alice@example.com", SP_UUID, None], type=pa.string()),
        "sku_name": pa.array(["PREMIUM_JOBS_COMPUTE"] * 3, type=pa.string()),
        "job_name": pa.array(["nightly_etl", "alice@example.com-adhoc", "x"], type=pa.string()),
        "warehouse_name": pa.array(["bi-warehouse"] * 3, type=pa.string()),
        "workspace_name": pa.array(["prod-emea"] * 3, type=pa.string()),
        "run_as_kind": pa.array(["USER", "SERVICE_PRINCIPAL", None], type=pa.string()),
        "net_usage_quantity": pa.array([1.5, 2.5, 3.5], type=pa.float64()),
    })


def _export_people(mask_users: bool) -> pa.Table:
    handler = marker_handler({"cost_by_job": _people_and_resources()})
    cur = make_connect_fn(handler)().conn.cursor()
    table, _truncated = edr._run_capped_query(
        cur, inner_sql="SELECT * FROM __MARKER_cost_by_job__", query_id="cost_by_job",
        max_rows=20, mask_users=mask_users,
    )
    return table


def test_default_export_leaves_user_emails_untouched():
    written = _export_people(mask_users=False)
    assert written.equals(_people_and_resources())


def test_mask_users_masks_only_user_emails():
    written = _export_people(mask_users=True)
    original = _people_and_resources()

    assert written.column("owner").to_pylist() == [
        edr.format_identity("alice@example.com"), SP_UUID, None,
    ]
    for name in ("sku_name", "job_name", "warehouse_name", "workspace_name", "run_as_kind",
                 "net_usage_quantity"):
        assert written.column(name).equals(original.column(name)), name


def test_mask_users_passes_an_already_masked_value_through():
    table = pa.table({"executed_by": pa.array(["12345 a@***", "bob@example.com"], type=pa.string())})
    masked = edr.apply_masking(table).column("executed_by").to_pylist()
    assert masked == ["12345 a@***", edr.format_identity("bob@example.com")]


# ------------------------------------------------------------------------------------------
# 3. the row cap and truncated flag
# ------------------------------------------------------------------------------------------


def _rows_table(n: int) -> pa.Table:
    return pa.table({
        "window_days": pa.array([7] * n, type=pa.int64()),
        "net_usage_quantity": pa.array(list(range(n)), type=pa.int64()),
    })


def test_run_capped_query_reports_truncated():
    dataset = _rows_table(10)
    handler = marker_handler({"cost_by_job": dataset})
    connect_fn = make_connect_fn(handler)
    cur = connect_fn().conn.cursor()

    table, truncated = edr._run_capped_query(
        cur, inner_sql="SELECT * FROM __MARKER_cost_by_job__", query_id="cost_by_job", max_rows=3,
        mask_users=False,
    )
    assert truncated is True
    assert table.num_rows == 3
    # the LIMIT sent to the fake "warehouse" was max_rows + 1, so truncation could be detected
    assert connect_fn.statements[-1].strip().endswith("LIMIT 4")


def test_run_capped_query_not_truncated_when_rows_fit():
    dataset = _rows_table(3)
    handler = marker_handler({"cost_by_job": dataset})
    cur = make_connect_fn(handler)().conn.cursor()

    table, truncated = edr._run_capped_query(
        cur, inner_sql="SELECT * FROM __MARKER_cost_by_job__", query_id="cost_by_job", max_rows=20,
        mask_users=False,
    )
    assert truncated is False
    assert table.num_rows == 3


def test_run_capped_query_empty_result():
    dataset = _rows_table(0)
    handler = marker_handler({"cost_by_job": dataset})
    cur = make_connect_fn(handler)().conn.cursor()

    table, truncated = edr._run_capped_query(
        cur, inner_sql="SELECT * FROM __MARKER_cost_by_job__", query_id="cost_by_job", max_rows=20,
        mask_users=False,
    )
    assert table.num_rows == 0
    assert truncated is False


def test_run_capped_query_raises_on_zero_column_result():
    handler = marker_handler({"cost_by_job": pa.table({})})
    cur = make_connect_fn(handler)().conn.cursor()

    with pytest.raises(RuntimeError, match="no columns"):
        edr._run_capped_query(
            cur, inner_sql="SELECT * FROM __MARKER_cost_by_job__", query_id="cost_by_job",
            max_rows=20, mask_users=False,
        )


# ------------------------------------------------------------------------------------------
# window_plan: every one of the app's three windows (7/30/90) always gets a days_used, however
# short the export; window_sources/window_aliases_map say which labels actually query Databricks
# and which reuse another label's rows.
# ------------------------------------------------------------------------------------------


def test_window_plan_examples():
    assert edr.window_plan(10) == [(7, 7), (30, 10), (90, 10)]
    assert edr.window_plan(30) == [(7, 7), (30, 30), (90, 30)]
    assert edr.window_plan(45) == [(7, 7), (30, 30), (90, 45)]
    assert edr.window_plan(5) == [(7, 5), (30, 5), (90, 5)]
    assert edr.window_plan(120) == [(7, 7), (30, 30), (90, 90)]  # more than 90 adds nothing
    assert edr.window_plan(90) == [(7, 7), (30, 30), (90, 90)]
    assert edr.window_plan(7) == [(7, 7), (30, 7), (90, 7)]


def test_window_sources_and_aliases_examples():
    for days, sources, aliases in [
        (10, [(7, 7), (30, 10)], {90: 30}),
        (30, [(7, 7), (30, 30)], {90: 30}),
        (45, [(7, 7), (30, 30), (90, 45)], {}),
        (5, [(7, 5)], {30: 7, 90: 7}),
        (90, [(7, 7), (30, 30), (90, 90)], {}),
        (7, [(7, 7)], {30: 7, 90: 7}),
    ]:
        plan = edr.window_plan(days)
        assert edr.window_sources(plan) == sources, days
        assert edr.window_aliases_map(plan) == aliases, days


# ------------------------------------------------------------------------------------------
# A multi-window export: a windowed check runs once per window_sources() entry (never per label),
# each run's own row cap applies separately, and the runs are concatenated into one
# findings/<id>.parquet with window_days relabelled from days_used to the source label. A label
# that is an alias (window_aliases_map) gets no query of its own and no rows in the parquet --
# tools/load_direct_results.py copies its source's rows in at load time.
# ------------------------------------------------------------------------------------------

_DAYS_RE = re.compile(r"days=(\d+)")


def _windowed_handler(sql: str) -> pa.Table:
    """A row count that depends on the window (30d has more history than 7d), so truncation can
    differ per window; window_days is set to the SQL's own days_used, exactly what the real
    __WINDOW_DAYS__ marker would bake in."""
    assert _MARKER_RE.search(sql), f"no query marker in SQL: {sql}"
    days = int(_DAYS_RE.search(sql).group(1))
    n = 5 if days == 7 else 20
    table = pa.table({
        "window_days": pa.array([days] * n, type=pa.int64()),
        "net_usage_quantity": pa.array(list(range(n)), type=pa.int64()),
    })
    lim = _LIMIT_RE.search(sql)
    k = int(lim.group(1)) if lim else table.num_rows
    return table.slice(0, k)


def _compile_with_days(scope_ids, roster, windows, env):
    """Compiled SQL that records the marker fill (`windows`, i.e. days_used) in the text itself,
    the way the real __WINDOW_DAYS__ replacement would -- so a fake connector can serve a
    different table per plan entry, unlike the single-table fake_compile() above."""
    return {qid: {"sql": f"SELECT * FROM __MARKER_{qid}__ -- days={windows}"} for qid in scope_ids}


def test_run_export_two_window_plan_concatenates_and_caps_per_window(tmp_path):
    connect_fn = make_connect_fn(_windowed_handler)

    result = edr.run_export(
        tmp_path / "export", only=["cost_by_job"], max_rows=10, windows=10, threads=1,
        connect_fn=connect_fn, compile_fn=_compile_with_days, env=dict(FAKE_ENV), now=NOW,
        names_sql={},
    )

    manifest = result["manifest"]
    assert manifest["windows"] == [7, 30, 90]
    assert manifest["window_coverage"] == {"7": 7, "30": 10, "90": 10}
    assert manifest["window_aliases"] == {"90": "30"}  # 90 reuses 30's rows, no query of its own
    assert manifest["days"] == 10

    entry = manifest["findings"]["cost_by_job"]
    assert entry["truncated"] is True                # the 30d window alone hit the cap
    assert entry["truncated_windows"] == [30, 90]     # 90 inherits it from its source, 30
    assert entry["exported_rows"] == 5 + 10           # 7d uncapped (5) + 30d capped at max_rows (10)

    written = pq.read_table(tmp_path / "export" / "findings" / "cost_by_job.parquet")
    counts: dict[int, int] = {}
    for wd in written.column("window_days").to_pylist():
        counts[wd] = counts.get(wd, 0) + 1
    assert counts == {7: 5, 30: 10}  # no 90 in the parquet -- the loader copies it in, not the export

    report = json.loads((tmp_path / "export" / "report.json").read_text(encoding="utf-8"))
    assert report["windows"] == [7, 30, 90]
    assert report["window_coverage"] == {"7": 7, "30": 10, "90": 10}
    assert report["days"] == 10


# ------------------------------------------------------------------------------------------
# 4 & 5. manifest shape + failed-query handling (one run covers both)
# ------------------------------------------------------------------------------------------


def test_run_export_manifest_shape_and_failed_query(tmp_path):
    ok_table = _rows_table(3)
    handler = marker_handler({"cost_by_job": ok_table}, failing={"cost_by_notebook"})
    connect_fn = make_connect_fn(handler)

    result = edr.run_export(
        tmp_path / "export",
        only=["cost_by_job", "cost_by_notebook"],
        max_rows=50,
        windows=7,
        threads=1,
        connect_fn=connect_fn,
        compile_fn=fake_compile,
        env=dict(FAKE_ENV),
        now=NOW,
        names_sql={}, tags_sql={},
    )

    manifest = result["manifest"]
    assert manifest["mode"] == "query"
    assert manifest["as_of"] == "2026-09-25T10:00:00Z"
    assert manifest["max_rows"] == 50
    assert manifest["windows"] == [7, 30, 90]
    assert manifest["window_aliases"] == {"30": "7", "90": "7"}  # a 7-day export: both alias 7
    assert manifest["mask_users"] is False
    assert set(manifest) >= {
        "tool", "tool_version", "git_commit", "as_of", "mode", "windows", "catalog",
        "host_fingerprint", "max_rows", "mask_users", "findings", "dims",
    }

    ok_entry = manifest["findings"]["cost_by_job"]
    assert ok_entry["status"] == "ok"
    assert ok_entry["exported_rows"] == 3
    assert ok_entry["domain"] == "cost"
    assert ok_entry["error"] is None

    failed_entry = manifest["findings"]["cost_by_notebook"]
    assert failed_entry["status"] == "failed"
    assert failed_entry["exported_rows"] is None
    assert failed_entry["error"] is not None
    assert FAKE_TOKEN not in failed_entry["error"]
    assert "***" in failed_entry["error"]

    # a real roster id never named in --only never ran this invocation
    other_entry = manifest["findings"]["cost_cloud_infra"]
    assert other_entry["status"] == "not_run"
    assert other_entry["exported_rows"] is None

    # names_sql={} above -> no names files supplied, known-gap note present
    assert manifest["dims"]["available"] == []
    assert set(manifest["dims"]["missing"]) == set(edr.DIM_TABLE_NAMES)
    assert manifest["dims"]["note"] == edr.DIMS_NOT_GENERATED_NOTE

    assert result["exported"] == 1
    assert result["failed"] == 1
    assert result["empty"] == 0

    # report.json / report.md / findings parquet / manifest.json all landed on disk
    out_dir = tmp_path / "export"
    assert (out_dir / "manifest.json").exists()
    assert (out_dir / "report.json").exists()
    assert (out_dir / "report.md").exists()
    assert (out_dir / "findings" / "cost_by_job.parquet").exists()
    assert not (out_dir / "findings" / "cost_by_notebook.parquet").exists()

    import json as _json
    report = _json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    assert report["summary"]["by_status"]["ok"] == 1
    assert report["summary"]["by_status"]["failed"] == 1
    assert "cost_by_notebook" in report["failed_ids"]


def test_run_export_query_mode_records_no_catalog_when_stand_in_used(tmp_path):
    handler = marker_handler({"cost_by_job": _rows_table(1)})
    connect_fn = make_connect_fn(handler)
    env = dict(FAKE_ENV, AUDIT_DBX_CATALOG=edr.DEFAULT_COMPILE_CATALOG)

    result = edr.run_export(
        tmp_path / "export", only=["cost_by_job"], threads=1, connect_fn=connect_fn,
        compile_fn=fake_compile, env=env, now=NOW, names_sql={}, tags_sql={},
    )
    assert result["manifest"]["catalog"] is None


def test_run_export_records_mask_users(tmp_path):
    handler = marker_handler({"cost_by_job": _people_and_resources()})
    result = edr.run_export(
        tmp_path / "export", only=["cost_by_job"], threads=1, mask_users=True,
        connect_fn=make_connect_fn(handler), compile_fn=fake_compile, env=dict(FAKE_ENV), now=NOW,
        names_sql={}, tags_sql={},
    )
    assert result["manifest"]["mask_users"] is True
    written = pq.read_table(tmp_path / "export" / "findings" / "cost_by_job.parquet")
    assert "alice@example.com" not in written.column("owner").to_pylist()


def test_run_export_replaces_a_previous_runs_stale_files(tmp_path):
    """A second run into the same out_dir must not leave the first run's files behind -- e.g. a
    dim that exported last time but not this time, or a query dropped from --only."""
    first = marker_handler({"cost_by_job": _rows_table(1), "dim_job": _dim_job_rows()})
    edr.run_export(
        tmp_path / "export", only=["cost_by_job"], threads=1, connect_fn=make_connect_fn(first),
        compile_fn=fake_compile, env=dict(FAKE_ENV), now=NOW,
        names_sql={"dim_job": "SELECT * FROM __MARKER_dim_job__"}, tags_sql={},
    )
    assert (tmp_path / "export" / "dims" / "dim_job.parquet").exists()

    second = marker_handler({"cost_cloud_infra": _rows_table(1)})
    result = edr.run_export(
        tmp_path / "export", only=["cost_cloud_infra"], threads=1, connect_fn=make_connect_fn(second),
        compile_fn=fake_compile, env=dict(FAKE_ENV), now=NOW, names_sql={}, tags_sql={},
    )
    out_dir = tmp_path / "export"
    assert not (out_dir / "dims" / "dim_job.parquet").exists()  # this run supplied no names_sql
    assert (out_dir / "findings" / "cost_cloud_infra.parquet").exists()
    assert not (out_dir / "findings" / "cost_by_job.parquet").exists()  # not in this run's scope
    assert not out_dir.with_name("export.tmp").exists()
    assert not out_dir.with_name("export.old").exists()
    assert result["manifest"]["dims"]["available"] == []


# ------------------------------------------------------------------------------------------
# load_direct_sql: the pre-compiled files under app/direct_sql
# ------------------------------------------------------------------------------------------


def _write_sql_dir(root: Path, thresholds: dict) -> Path:
    sql_dir = root / "direct_sql"
    sql_dir.mkdir()
    (sql_dir / "thresholds.json").write_text(json.dumps(thresholds), encoding="utf-8")
    (sql_dir / "cost_by_job.sql").write_text(
        "-- generated\nSELECT __WINDOW_DAYS__ AS window_days FROM system.billing.usage "
        "WHERE usage_date >= current_date - INTERVAL __WINDOW_DAYS__ DAYS\n",
        encoding="utf-8",
    )
    return sql_dir


def test_load_direct_sql_fills_the_window_and_flags_a_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(edr.dbt_run, "load_thresholds", lambda: {})
    sql_dir = _write_sql_dir(tmp_path, {})
    out = edr.load_direct_sql(["cost_by_job", "absent_id"], {}, 15, sql_dir=sql_dir)
    assert "SELECT 15 AS window_days" in out["cost_by_job"]["sql"]
    assert "INTERVAL 15 DAYS" in out["cost_by_job"]["sql"]
    assert edr.WINDOW_MARKER not in out["cost_by_job"]["sql"]
    assert "build_direct_sql.py" in out["absent_id"]["error"]


def test_load_direct_sql_refuses_sql_built_with_other_thresholds(tmp_path, monkeypatch):
    monkeypatch.setattr(edr.dbt_run, "load_thresholds", lambda: {"cost_by_job": {"min_usd": 5}})
    sql_dir = _write_sql_dir(tmp_path, {})
    with pytest.raises(edr.ExportConfigError, match="thresholds.yml changed"):
        edr.load_direct_sql(["cost_by_job"], {}, 30, sql_dir=sql_dir)


def test_load_direct_sql_without_generated_files_says_how_to_build_them(tmp_path):
    with pytest.raises(edr.ExportConfigError, match="build_direct_sql.py"):
        edr.load_direct_sql(["cost_by_job"], {}, 30, sql_dir=tmp_path / "nothing")


def _write_sql_dir_with_as_of(root: Path, thresholds: dict) -> Path:
    sql_dir = root / "direct_sql"
    sql_dir.mkdir()
    (sql_dir / "thresholds.json").write_text(json.dumps(thresholds), encoding="utf-8")
    (sql_dir / "cost_by_job.sql").write_text(
        "-- generated\nSELECT __WINDOW_DAYS__ AS window_days FROM system.billing.usage "
        "WHERE usage_date >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS "
        "AND usage_end_time < __AS_OF_TS__\n",
        encoding="utf-8",
    )
    return sql_dir


def test_load_direct_sql_fills_the_as_of_markers_with_one_utc_now(tmp_path, monkeypatch):
    monkeypatch.setattr(edr.dbt_run, "load_thresholds", lambda: {})
    sql_dir = _write_sql_dir_with_as_of(tmp_path, {})
    out = edr.load_direct_sql(["cost_by_job"], {}, 15, sql_dir=sql_dir, now=NOW)
    sql = out["cost_by_job"]["sql"]
    assert "DATE '2026-09-25'" in sql
    assert "TIMESTAMP '2026-09-25 10:00:00'" in sql
    assert edr.AS_OF_DATE_MARKER not in sql and edr.AS_OF_TS_MARKER not in sql


# ------------------------------------------------------------------------------------------
# Names: app/direct_sql/names/*.sql, exported uncapped alongside the checks
# ------------------------------------------------------------------------------------------


def test_load_names_sql_reads_every_real_file():
    sql = edr.load_names_sql()
    assert set(sql) == set(edr.DIM_TABLE_NAMES)
    for text in sql.values():
        assert "system." in text
        assert edr.WINDOW_MARKER not in text
        assert edr.AS_OF_DATE_MARKER not in text and edr.AS_OF_TS_MARKER not in text


def test_load_names_sql_fills_the_as_of_marker_with_one_utc_now(tmp_path):
    names_dir = tmp_path / "names"
    names_dir.mkdir()
    (names_dir / "dim_cluster.sql").write_text(
        "SELECT 1 WHERE usage_date >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS\n",
        encoding="utf-8",
    )
    out = edr.load_names_sql(names_dir=names_dir, windows=10, now=NOW)
    sql = out["dim_cluster"]
    assert "DATE '2026-09-25'" in sql
    assert "10" in sql
    assert edr.AS_OF_DATE_MARKER not in sql and edr.WINDOW_MARKER not in sql


def test_cluster_names_cover_only_the_export_window():
    sql = edr.load_names_sql(windows=10)["dim_cluster"]
    assert "INTERVAL 10 DAYS" in sql
    assert "'JOB'" in sql


def _dim_job_rows() -> pa.Table:
    return pa.table({
        "workspace_id": pa.array(["1111", "1111"], type=pa.string()),
        "job_id": pa.array(["10", "20"], type=pa.string()),
        "name": pa.array(["nightly_etl", "hourly_sync"], type=pa.string()),
    })


def test_export_name_one_is_uncapped(tmp_path):
    handler = marker_handler({"dim_job": _dim_job_rows()})
    cur = make_connect_fn(handler)().conn.cursor()
    result = edr.export_name_one(
        cur, sql="SELECT * FROM __MARKER_dim_job__", mask_users=False,
        out_path=tmp_path / "dim_job.parquet",
    )
    assert result["exported_rows"] == 2
    written = pq.read_table(tmp_path / "dim_job.parquet")
    assert written.num_rows == 2
    # no LIMIT/ROW_NUMBER wrapper -- the raw names SQL goes straight to the connection
    assert cur.conn.statements[-1] == "SELECT * FROM __MARKER_dim_job__"


def test_export_names_records_missing_files_and_failures(tmp_path):
    handler = marker_handler({"dim_job": _dim_job_rows()}, failing={"dim_cluster"})
    cur = make_connect_fn(handler)().conn.cursor()
    names_sql = {
        "dim_job": "SELECT * FROM __MARKER_dim_job__",
        "dim_cluster": "SELECT * FROM __MARKER_dim_cluster__",
    }
    info = edr._export_names(cur, names_sql, tmp_path, mask_users=False)
    assert info["available"] == ["dim_job"]
    assert set(info["missing"]) == {"dim_cluster", "dim_pipeline", "dim_warehouse", "dim_workspace", "dim_notebook"}
    assert info["exported"]["dim_cluster"]["exported"] is False
    assert info["exported"]["dim_workspace"]["error"] == "no app/direct_sql/names/*.sql file"
    assert (tmp_path / "dims" / "dim_job.parquet").exists()
    assert not (tmp_path / "dims" / "dim_cluster.parquet").exists()


def test_run_export_loads_names_uncapped(tmp_path):
    handler = marker_handler({"cost_by_job": _rows_table(1), "dim_job": _dim_job_rows()})
    names_sql = {"dim_job": "SELECT * FROM __MARKER_dim_job__"}
    result = edr.run_export(
        tmp_path / "export", only=["cost_by_job"], threads=1,
        connect_fn=make_connect_fn(handler), compile_fn=fake_compile, env=dict(FAKE_ENV), now=NOW,
        names_sql=names_sql, tags_sql={},
    )
    dims = result["manifest"]["dims"]
    assert dims["available"] == ["dim_job"]
    assert (tmp_path / "export" / "dims" / "dim_job.parquet").exists()


# ------------------------------------------------------------------------------------------
# main(): --out defaults to the data folder's results/
# ------------------------------------------------------------------------------------------


def test_main_out_defaults_to_the_data_folder(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    captured = {}

    def fake_run_export(out_dir, **kwargs):
        captured["out_dir"] = out_dir
        return {
            "exported": 0, "empty": 0, "failed": 0, "truncated": 0, "total_bytes": 0,
            "out_dir": out_dir,
            "manifest": {
                "dims": {"available": [], "missing": []},
                "tags": {"available": [], "missing": [], "exported": {}},
                "window_coverage": {},
            },
        }

    monkeypatch.setattr(edr, "run_export", fake_run_export)
    rc = edr.main(["--only", "cost_by_job"])
    assert rc == 0
    assert captured["out_dir"] == edr.datasource.results_dir()
    assert captured["out_dir"] == tmp_path / "results"


def test_main_out_explicit_flag_still_works(monkeypatch, tmp_path):
    captured = {}

    def fake_run_export(out_dir, **kwargs):
        captured["out_dir"] = out_dir
        return {
            "exported": 0, "empty": 0, "failed": 0, "truncated": 0, "total_bytes": 0,
            "out_dir": out_dir,
            "manifest": {
                "dims": {"available": [], "missing": []},
                "tags": {"available": [], "missing": [], "exported": {}},
                "window_coverage": {},
            },
        }

    monkeypatch.setattr(edr, "run_export", fake_run_export)
    rc = edr.main(["--out", str(tmp_path / "custom"), "--only", "cost_by_job"])
    assert rc == 0
    assert captured["out_dir"] == (tmp_path / "custom").resolve()


def test_main_refuses_a_data_folder_inside_the_repo(monkeypatch):
    monkeypatch.setenv("AUDIT_DATA_DIR", str(ROOT / "some_stray_data_dir"))
    rc = edr.main(["--only", "cost_by_job"])
    assert rc == 2


def test_only_skips_the_names_and_tags_queries(tmp_path, monkeypatch):
    def never(*_a, **_k):
        raise AssertionError("an --only run must not run the names or tags queries")

    monkeypatch.setattr(edr, "load_names_sql", never)
    monkeypatch.setattr(edr, "load_tags_sql", never)
    handler = marker_handler({"cost_by_job": _rows_table(1)})
    result = edr.run_export(
        tmp_path / "export", only=["cost_by_job"], threads=1,
        connect_fn=make_connect_fn(handler), compile_fn=fake_compile, env=dict(FAKE_ENV), now=NOW,
    )
    assert result["manifest"]["dims"]["available"] == []
    assert (tmp_path / "export" / "findings" / "cost_by_job.parquet").exists()


def test_export_tag_keys_filters_tag_entity(tmp_path, monkeypatch):
    (tmp_path / "settings.yml").write_text(
        "export_tag_keys: [Cost Center, team-Name, \"o'x\"]\nmandatory_tag_keys: [cost_center, Environment]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AUDIT_CONFIG_DIR", str(tmp_path))
    # The mandatory and top tags (and env, which counts as environment) are kept even when export_tag_keys leaves them out.
    expected = "tag_key IN ('businessunit', 'costcenter', 'domain', 'env', 'environment', 'o''x', 'team', 'teamname')"
    assert edr.tag_key_filter_sql() == expected
    sql = edr.load_tags_sql()["tag_entity"]
    assert f"WHERE {expected}" in sql
    assert edr.TAG_KEY_FILTER_MARKER not in sql


def test_no_export_tag_keys_keeps_every_tag(tmp_path, monkeypatch):
    (tmp_path / "settings.yml").write_text("default_window: 30\n", encoding="utf-8")
    monkeypatch.setenv("AUDIT_CONFIG_DIR", str(tmp_path))
    assert "WHERE TRUE" in edr.load_tags_sql()["tag_entity"]


def test_tags_runs_only_the_named_tag_tables(tmp_path):
    handler = marker_handler({"tag_entity": _rows_table(2)})
    connect_fn = make_connect_fn(handler)
    result = edr.run_export(
        tmp_path / "export", tags=["tag_entity"], threads=1, connect_fn=connect_fn,
        compile_fn=fake_compile, env=dict(FAKE_ENV), now=NOW,
        tags_sql={"tag_entity": "SELECT * FROM __MARKER_tag_entity__", "tag_index": "SELECT * FROM __MARKER_tag_index__"},
    )
    assert (tmp_path / "export" / "tags" / "tag_entity.parquet").exists()
    assert not (tmp_path / "export" / "tags" / "tag_index.parquet").exists()
    assert not list((tmp_path / "export" / "findings").glob("*.parquet"))
    assert result["manifest"]["dims"]["available"] == []


def test_tags_rejects_an_unknown_table(tmp_path):
    with pytest.raises(edr.ExportConfigError, match="unknown tag table"):
        edr.run_export(tmp_path / "export", tags=["nope"], connect_fn=make_connect_fn(marker_handler({})),
                       compile_fn=fake_compile, env=dict(FAKE_ENV), now=NOW, tags_sql={})


def test_policies_are_listed_on_the_metastore_each_catalog_and_each_schema(tmp_path):
    def handler(sql: str) -> pa.Table:
        if "information_schema.catalogs" in sql:
            return pa.table({"catalog_name": ["main", "system", "locked"]})
        if "information_schema.schemata" in sql:
            return pa.table({"catalog_name": ["main", "main", "locked"], "schema_name": ["sales", "hr", "x"]})
        if sql == "SHOW POLICIES ON SCHEMA `main`.`hr`":
            return pa.table({"Policy Name": ["mask_ssn", "hide_eu"], "Policy Type": ["COLUMN_MASK", "ROW_FILTER"],
                             "Catalog": ["main", "main"], "Schema": ["hr", None], "Table": [None, None], "Comment": [None, "EU rows"]})
        if sql == "SHOW POLICIES ON CATALOG `main`":
            return pa.table({"Policy Name": ["hide_eu"], "Policy Type": ["ROW_FILTER"], "Catalog": ["main"], "Schema": [None], "Table": [None], "Comment": ["EU rows"]})
        if "`locked`" in sql:
            raise RuntimeError("PERMISSION_DENIED: no READ METADATA")
        return pa.table({"policy_name": pa.array([], pa.string())})

    cur = FakeConnection(handler, []).cursor()
    info = edr._export_policies(cur, tmp_path)
    assert (info["rows"], info["catalogs"], info["schemas"], info["not_readable"], info["metastore"]) == (2, 2, 3, 2, True)
    assert "token" not in (info["first_error"] or "")
    rows = pq.read_table(tmp_path / "policies" / "abac_policies.parquet").to_pylist()
    assert sorted((r["policy_name"], r["policy_type"], r["schema"], r["comment"], r["on_type"]) for r in rows) == [
        ("hide_eu", "ROW_FILTER", None, "EU rows", "CATALOG"), ("mask_ssn", "COLUMN_MASK", "hr", None, "SCHEMA")]
