"""app/direct_sql/tags/*.sql end to end: tools/export_direct_results.py writes tags/*.parquet
(fake connector, same pattern as tests/test_export_direct_results.py), tools/load_direct_results.py
loads them into schema `tags`, and GET /api/tags + GET /api/rollup then work against that db --
the goal stated in tasks/PICKUP.md (direct mode gets the tags.* tables a local dbt build gives).

Not covered here: the SQL files' own correctness against a live warehouse (they are hand-written,
not generated, and validated separately by parsing them in sqlglot's databricks dialect). This
test proves the export/load plumbing and the app's read path, using canned rows in place of a
Databricks result set.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import re
import sys
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


edr = _load(EXPORT_MODULE_PATH, "export_direct_results_for_tags_test")
ldr = _load(LOAD_MODULE_PATH, "load_direct_results_for_tags_test")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.app import app  # noqa: E402

client = TestClient(app)

NOW = dt.datetime(2026, 9, 25, 9, 0, 0, tzinfo=dt.timezone.utc)
FAKE_ENV = {
    "DATABRICKS_SERVER_HOSTNAME": "acme-test.cloud.databricks.com",
    "DATABRICKS_HTTP_PATH": "/sql/1.0/warehouses/abc123",
    "DATABRICKS_TOKEN": "dapi0123456789abcdef0123456789ab",
    "AUDIT_DBX_CATALOG": "test_catalog",
}
_MARKER_RE = re.compile(r"__MARKER_(\w+)__")


# ------------------------------------------------------------------------------------------
# The fake connector -- same shape as tests/test_export_direct_results.py's.
# ------------------------------------------------------------------------------------------
class FakeCursor:
    def __init__(self, conn: "FakeConnection"):
        self.conn = conn
        self._pending: pa.Table | None = None

    def execute(self, sql: str, parameters=None):
        self._pending = self.conn.handler(sql)

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
    def __init__(self, handler):
        self.handler = handler

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self):
        pass


class FakeConnected:
    def __init__(self, conn: FakeConnection):
        self.conn = conn


def make_connect_fn(handler):
    def connect_fn():
        return FakeConnected(FakeConnection(handler))

    return connect_fn


def marker_handler(datasets: dict[str, pa.Table]):
    def handler(sql: str) -> pa.Table:
        m = _MARKER_RE.search(sql)
        assert m, f"no query marker in SQL: {sql}"
        return datasets[m.group(1)]

    return handler


def fake_compile(scope_ids, roster, windows, env):
    return {qid: {"sql": f"SELECT * FROM __MARKER_{qid}__"} for qid in scope_ids}


# ------------------------------------------------------------------------------------------
# One small, self-consistent scenario: workspace ws1, warehouse wh1, tag key "cost_center" = "eng".
# ------------------------------------------------------------------------------------------
WINDOW = 30
D0 = dt.date(2026, 9, 1)


def _tags_datasets() -> dict[str, pa.Table]:
    return {
        "tag_workspace": pa.table({
            "workspace_id": ["ws1"], "tag_key": ["costcenter"], "display_key": ["cost_center"],
            "tag_value": ["eng"], "is_allocating": [True], "top_value": ["eng"], "top_values": [["eng"]],
            "share": pa.array([1.0], type=pa.float64()), "coverage": pa.array([1.0], type=pa.float64()),
            "dbus_tagged": pa.array([50.0], type=pa.float64()), "dbus_total": pa.array([50.0], type=pa.float64()),
            "reason": ["cost_center \"eng\" carries 100.0% of the cost_center-tagged DBUs"],
        }),
        "tag_entity": pa.table({
            "entity_type": ["workspace"], "workspace_id": ["ws1"], "entity_id": ["ws1"],
            "tag_key": ["costcenter"], "raw_key": ["cost_center"], "tag_value": ["eng"],
            "is_allocating": [True], "source": ["workspace_inferred"],
            "share": pa.array([1.0], type=pa.float64()), "coverage": pa.array([1.0], type=pa.float64()),
            "reason": ["cost_center \"eng\" carries 100.0% of the cost_center-tagged DBUs"],
        }),
        "tag_index": pa.table({
            "tag_key": ["costcenter"], "display_key": ["cost_center"], "raw_keys": ["cost_center"],
            "tag_value": ["eng"], "source": ["billing_custom_tags"],
            "object_count": pa.array([1], type=pa.int64()), "workspace_count": pa.array([1], type=pa.int64()),
            "dbus": pa.array([50.0], type=pa.float64()),
            "first_seen": pa.array([D0], type=pa.date32()), "last_seen": pa.array([D0], type=pa.date32()),
        }),
        "cost_unit": pa.table({
            "window_days": pa.array([WINDOW], type=pa.int32()), "unit_id": ["u1"], "workspace_id": ["ws1"],
            "compute_kind": ["warehouse"], "compute_id": ["wh1"], "work_kind": ["query"],
            "work_id": pa.array([None], type=pa.string()), "usage_unit": ["DBU"],
            "usd": pa.array([100.0], type=pa.float64()), "unpriced_quantity": pa.array([0.0], type=pa.float64()),
            "quantity": pa.array([50.0], type=pa.float64()), "record_count": pa.array([1], type=pa.int64()),
        }),
        "cost_unit_tag": pa.table({
            "window_days": pa.array([WINDOW], type=pa.int32()), "unit_id": ["u1"], "level": ["compute"],
            "tag_key": ["costcenter"], "raw_key": ["cost_center"], "tag_value": ["eng"],
            "source": ["billing:warehouse"], "usd": pa.array([100.0], type=pa.float64()),
        }),
        # No unit_usd/unit_quantity/unit_unpriced_quantity here on purpose -- load_direct_results.py
        # adds them from the loaded tags.cost_unit (see cost_reconciliation.sql's own header note).
        "cost_reconciliation": pa.table({
            "window_days": pa.array([WINDOW], type=pa.int32()), "usage_unit": ["DBU"],
            "billing_usd": pa.array([100.0], type=pa.float64()),
            "billing_quantity": pa.array([50.0], type=pa.float64()),
            "billing_unpriced_quantity": pa.array([0.0], type=pa.float64()),
            "attributed_dbus": pa.array([50.0], type=pa.float64()),
            "attributed_unmatched_dbus": pa.array([0.0], type=pa.float64()),
            "scaled_warehouse_days": pa.array([0], type=pa.int64()),
            "warehouse_idle_usd": pa.array([0.0], type=pa.float64()),
            "warehouse_unsplit_usd": pa.array([0.0], type=pa.float64()),
        }),
        "perf_unit": pa.table({
            "window_days": pa.array([WINDOW], type=pa.int32()), "unit_id": ["p1"], "workspace_id": ["ws1"],
            "compute_kind": ["warehouse"], "compute_id": ["wh1"],
            "statements": pa.array([5], type=pa.int64()), "failed_statements": pa.array([1], type=pa.int64()),
            "duration_ms": pa.array([1000], type=pa.int64()), "queue_ms": pa.array([100], type=pa.int64()),
            "spill_bytes": pa.array([0], type=pa.int64()),
        }),
        "perf_unit_tag": pa.table({
            "window_days": pa.array([WINDOW], type=pa.int32()), "unit_id": ["p1"], "level": ["compute"],
            "tag_key": ["costcenter"], "raw_key": ["cost_center"], "tag_value": ["eng"],
            "source": ["warehouse_tags"],
            "statements": pa.array([5], type=pa.int64()), "failed_statements": pa.array([1], type=pa.int64()),
            "duration_ms": pa.array([1000], type=pa.int64()), "queue_ms": pa.array([100], type=pa.int64()),
            "spill_bytes": pa.array([0], type=pa.int64()),
        }),
        "query_tag_keys": pa.table({
            "window_days": pa.array([WINDOW], type=pa.int32()), "unit_id": ["p1"],
            "own_keys": ["costcenter"],
            "statements": pa.array([5], type=pa.int64()),
        }),
        "cost_day": pa.table({
            "usage_date": pa.array([dt.date(2026, 9, 20)], type=pa.date32()), "workspace_id": ["ws1"],
            "cloud": ["AWS"], "sku_name": ["PREMIUM_SQL_PRO_COMPUTE"], "billing_origin_product": ["SQL"],
            "usage_unit": ["DBU"], "is_serverless": [False], "is_photon": [True],
            "is_perf_optimized": [False], "has_custom_tags": [True],
            "sig_id": ["0" * 32], "sig": ['[{"k":"costcenter","v":"eng","l":"compute","s":"billing:warehouse"}]'],
            "usd": pa.array([100.0], type=pa.float64()), "unpriced_quantity": pa.array([0.0], type=pa.float64()),
            "quantity": pa.array([50.0], type=pa.float64()),
            "as_of_date": pa.array([dt.date(2026, 9, 21)], type=pa.date32()),
        }),
        "bill_tag_dates": pa.table({
            "entity_type": ["warehouse"], "workspace_id": ["ws1"], "entity_id": ["wh1"],
            "tag_key": ["costcenter"], "raw_key": ["cost_center"], "last_value": ["eng"],
            "all_values": [["eng"]],
            "first_date": pa.array([D0], type=pa.date32()),
            "last_date": pa.array([dt.date(2026, 9, 20)], type=pa.date32()),
            "entity_last_date": pa.array([dt.date(2026, 9, 20)], type=pa.date32()),
            "from_policy": [False],
        }),
    }


def _cost_by_job_rows() -> pa.Table:
    return pa.table({"window_days": pa.array([WINDOW], type=pa.int64()), "workspace_id": ["ws1"],
                      "job_id": ["j1"], "job_name": ["nightly"], "net_usage_quantity": [10.0],
                      "status": ["OK"]})


def _run_export(tmp_path: Path, *, with_tags: bool) -> Path:
    datasets = {"cost_by_job": _cost_by_job_rows()}
    tags_sql: dict[str, str] = {}
    if with_tags:
        datasets.update(_tags_datasets())
        tags_sql = {name: f"SELECT * FROM __MARKER_{name}__" for name in edr.TAG_TABLE_NAMES}
    connect_fn = make_connect_fn(marker_handler(datasets))
    out_dir = tmp_path / "export"
    edr.run_export(
        out_dir, only=["cost_by_job"], max_rows=50, windows=WINDOW, threads=1,
        connect_fn=connect_fn, compile_fn=fake_compile, env=dict(FAKE_ENV), now=NOW,
        names_sql={}, tags_sql=tags_sql,
    )
    return out_dir


# ------------------------------------------------------------------------------------------
# Export: tags/*.parquet written and recorded in the manifest.
# ------------------------------------------------------------------------------------------


def test_export_writes_every_tag_table_and_records_the_manifest(tmp_path):
    out_dir = _run_export(tmp_path, with_tags=True)
    for name in edr.TAG_TABLE_NAMES:
        assert (out_dir / "tags" / f"{name}.parquet").exists()
    tags_manifest = (out_dir / "manifest.json").read_text(encoding="utf-8")
    assert '"tags"' in tags_manifest
    import json

    manifest = json.loads(tags_manifest)
    assert manifest["tags"]["available"] == list(edr.TAG_TABLE_NAMES)
    assert manifest["tags"]["missing"] == []
    assert manifest["tags"]["note"] is None


def test_export_with_no_tags_sql_writes_no_parquet(tmp_path):
    # tags/ itself still gets created, same as dims/ does with no names_sql -- but empty.
    out_dir = _run_export(tmp_path, with_tags=False)
    assert list((out_dir / "tags").glob("*.parquet")) == []


def test_windowed_tag_query_runs_once_per_source_and_loader_fills_the_alias(tmp_path):
    """A 30-day export: cost_unit's __WINDOW_DAYS__ query runs at days_used 7 and 30
    (window_sources) -- never a third time at 90, the label window_aliases_map() reuses 30's rows
    for. tools/load_direct_results.py then copies those rows under window_days=90 for
    tags.cost_unit, exactly like a windowed check's own findings.f_<id>, and
    cost_reconciliation's backfill from cost_unit still lines up window by window afterward."""
    calls: dict[str, list[int]] = {"cost_unit": [], "cost_reconciliation": []}

    def cost_unit_row(days_used: int) -> pa.Table:
        return pa.table({
            "window_days": pa.array([days_used], type=pa.int32()), "unit_id": ["u1"],
            "workspace_id": ["ws1"], "usage_unit": ["DBU"],
            "usd": pa.array([float(days_used)], type=pa.float64()),
            "quantity": pa.array([float(days_used)], type=pa.float64()),
            "unpriced_quantity": pa.array([0.0], type=pa.float64()),
        })

    def cost_reconciliation_row(days_used: int) -> pa.Table:
        return pa.table({
            "window_days": pa.array([days_used], type=pa.int32()), "usage_unit": ["DBU"],
            "billing_usd": pa.array([float(days_used)], type=pa.float64()),
        })

    def handler(sql: str) -> pa.Table:
        m = _MARKER_RE.search(sql)
        assert m, f"no query marker in SQL: {sql}"
        if m.group(1) == "cost_by_job":
            return _cost_by_job_rows()
        assert m.group(1) in calls
        wm = re.search(r"WINDOW_DAYS=(\d+)", sql)
        assert wm, f"no window days in {m.group(1)} SQL: {sql}"
        days_used = int(wm.group(1))
        calls[m.group(1)].append(days_used)
        return cost_unit_row(days_used) if m.group(1) == "cost_unit" else cost_reconciliation_row(days_used)

    out_dir = tmp_path / "export"
    edr.run_export(
        out_dir, only=["cost_by_job"], max_rows=50, windows=30, threads=1,
        connect_fn=make_connect_fn(handler), compile_fn=fake_compile, env=dict(FAKE_ENV), now=NOW,
        names_sql={},
        tags_sql={
            name: f"SELECT * FROM __MARKER_{name}__ /* WINDOW_DAYS=__WINDOW_DAYS__ */"
            for name in ("cost_unit", "cost_reconciliation")
        },
    )

    # window_sources(window_plan(30)) -- Databricks never sees a 90-day tag query for either table.
    assert sorted(calls["cost_unit"]) == [7, 30]
    assert sorted(calls["cost_reconciliation"]) == [7, 30]

    import json

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["window_aliases"] == {"90": "30"}

    result = ldr.load_folder(out_dir, tmp_path / "loaded.duckdb")
    assert {"cost_unit", "cost_reconciliation"} <= set(result["tags_available"])

    con = duckdb.connect(str(tmp_path / "loaded.duckdb"), read_only=True)
    try:
        rows = dict(con.execute("SELECT window_days, usd FROM tags.cost_unit ORDER BY window_days").fetchall())
        assert rows == {7: 7.0, 30: 30.0, 90: 30.0}  # 90 is a copy of 30's row, not its own query

        # cost_reconciliation's unit_usd backfill (from tags.cost_unit) still matches per window
        # after both tables got their window_days=90 rows copied in, not queried a second time.
        recon = dict(con.execute(
            "SELECT window_days, unit_usd FROM tags.cost_reconciliation ORDER BY window_days"
        ).fetchall())
        assert recon == {7: 7.0, 30: 30.0, 90: 30.0}
    finally:
        con.close()


# ------------------------------------------------------------------------------------------
# Load: tags.<name> tables land in the db, cost_reconciliation gets its unit_* columns back.
# ------------------------------------------------------------------------------------------


def test_load_creates_tags_schema_and_backfills_reconciliation(tmp_path):
    out_dir = _run_export(tmp_path, with_tags=True)
    result = ldr.load_folder(out_dir, tmp_path / "loaded.duckdb")
    assert set(result["tags_available"]) == set(edr.TAG_TABLE_NAMES)
    assert result["tags_missing"] == []

    con = duckdb.connect(str(tmp_path / "loaded.duckdb"), read_only=True)
    try:
        tables = {
            r[0] for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'tags'"
            ).fetchall()
        }
        assert tables == set(edr.TAG_TABLE_NAMES)
        row = con.execute(
            "SELECT unit_usd, unit_quantity, unit_unpriced_quantity FROM tags.cost_reconciliation "
            "WHERE window_days = ? AND usage_unit = 'DBU'", [WINDOW],
        ).fetchone()
        assert row == (100.0, 50.0, 0.0)  # backfilled from the loaded tags.cost_unit, not exported
    finally:
        con.close()


def test_load_without_any_tags_parquet_creates_no_tags_tables(tmp_path):
    # No tags.* table -> app/core/tags.py and app/core/rollup.py both say "not built", the same as
    # before this folder existed at all.
    out_dir = _run_export(tmp_path, with_tags=False)
    result = ldr.load_folder(out_dir, tmp_path / "loaded.duckdb")
    assert result["tags_available"] == []
    con = duckdb.connect(str(tmp_path / "loaded.duckdb"), read_only=True)
    try:
        n = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'tags'"
        ).fetchone()[0]
        assert n == 0
    finally:
        con.close()


# ------------------------------------------------------------------------------------------
# Round trip: GET /api/tags and GET /api/rollup work against the loaded db.
# ------------------------------------------------------------------------------------------


@pytest.fixture
def loaded_db(tmp_path, monkeypatch):
    out_dir = _run_export(tmp_path, with_tags=True)
    db_path = tmp_path / "loaded.duckdb"
    ldr.load_folder(out_dir, db_path)
    monkeypatch.setenv("AUDIT_DB", str(db_path))
    monkeypatch.setenv("AUDIT_SNAPSHOT_MANIFEST", str(tmp_path / "no-such-manifest.json"))
    return db_path


def test_api_tags_finds_the_loaded_key(loaded_db):
    r = client.get("/api/tags", params={"search": "cost_center"})
    assert r.status_code == 200
    keys = {k["tag_key"]: k for k in r.json()["keys"]}
    assert "costcenter" in keys
    assert keys["costcenter"]["display_key"] == "cost_center"
    assert {v["tag_value"] for v in keys["costcenter"]["values"]} == {"eng"}


def test_api_rollup_reads_the_loaded_cost_unit(loaded_db):
    r = client.get("/api/rollup", params={"area": "cost", "tag_key": "cost_center", "window": WINDOW})
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "ok_rows"
    assert body["total"]["usd"] == pytest.approx(100.0)
    assert body["reconciliation"]["billing_usd"] == pytest.approx(100.0)
    assert body["reconciliation"]["reconciled"] is True


def test_api_tags_not_built_without_a_tags_folder(tmp_path, monkeypatch):
    out_dir = _run_export(tmp_path, with_tags=False)
    db_path = tmp_path / "loaded.duckdb"
    ldr.load_folder(out_dir, db_path)
    monkeypatch.setenv("AUDIT_DB", str(db_path))
    monkeypatch.setenv("AUDIT_SNAPSHOT_MANIFEST", str(tmp_path / "no-such-manifest.json"))

    assert client.get("/api/tags").status_code == 503
    r = client.get("/api/rollup", params={"tag_key": "cost_center", "window": WINDOW})
    assert r.status_code == 200
    assert r.json()["outcome"] == "not_assessed"
