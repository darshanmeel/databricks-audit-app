"""tools/workspace_activity.py and data.read_workspaces: who is active per workspace, each catalog's
home workspace, and the complexity and usage index per workspace."""
import datetime as dt
import json

import duckdb
import pyarrow.parquet as pq
import sqlglot

from tools import workspace_activity as wa

END = dt.date(2026, 10, 30)


def test_both_statements_are_valid_databricks_sql_with_the_window_and_end():
    sql = wa.activity_sql(END, [7, 30, 90])
    sqlglot.parse_one(sql, read="databricks")
    assert "explode(array(7, 30, 90))" in sql and "DATE'2026-10-30'" in sql and "date_sub(DATE'2026-10-30', 90)" in sql
    sqlglot.parse_one(wa.catalog_sql(END, 90), read="databricks")


class Cursor:
    def __init__(self, fail_lineage):
        self.fail_lineage, self.description, self._rows = fail_lineage, None, []

    def execute(self, sql):
        if "table_lineage" in sql:
            if self.fail_lineage:
                raise RuntimeError("[INSUFFICIENT_PERMISSIONS] lineage\nmore")
            self.description = [("catalog",), ("workspace_id",), ("events",)]
            self._rows = [("main", "1", 5)]
        else:
            cols = [f.name for f in wa.ACTIVITY_SCHEMA]
            self.description = [(c,) for c in cols]
            self._rows = [(30, "1", 3, 1, 120, 2, "2026-10-29")]

    def fetchall(self):
        return self._rows


class Connected:
    def __init__(self, fail_lineage=False):
        cur = Cursor(fail_lineage)
        self.conn = type("C", (), {"cursor": lambda self: cur, "close": lambda self: None})()


def test_export_writes_both_files_and_a_failed_statement_is_reported(tmp_path):
    info = wa.export(lambda: Connected(), tmp_path, end=END, windows=[30])
    assert info["activity"] == {"rows": 1, "error": None} and info["catalogs"]["rows"] == 1
    got = pq.read_table(str(tmp_path / "usage" / wa.ACTIVITY_FILE)).to_pylist()
    assert got[0]["active_people"] == 3 and got[0]["last_activity"] == "2026-10-29"

    info = wa.export(lambda: Connected(fail_lineage=True), tmp_path / "b", end=END, windows=[30])
    assert info["activity"]["rows"] == 1
    assert info["catalogs"]["error"] == "[INSUFFICIENT_PERMISSIONS] lineage"


def _db(tmp_path):
    db = tmp_path / "audit.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE SCHEMA usage; CREATE SCHEMA storage; CREATE SCHEMA dims")
    con.execute("CREATE TABLE usage.workspace_activity AS SELECT * FROM (VALUES "
                "(30, '1', 40, 3, 5000, 30, '2026-10-29'), (30, '2', 1, 0, 3, 1, '2026-09-01'), "
                "(30, '0', 9, 9, 9, 9, '2026-10-29'), (90, '1', 50, 4, 9000, 40, '2026-10-29')) "
                "t(window_days, workspace_id, active_people, active_service_principals, queries, query_identities, last_activity)")
    con.execute("CREATE TABLE usage.catalog_workspace AS SELECT * FROM (VALUES "
                "('sales', '1', 90), ('sales', '2', 10), ('sandbox', '2', 4)) t(catalog, workspace_id, events)")
    con.execute("CREATE TABLE usage.workspace_activity_info AS SELECT '{\"exported\": true}' AS info")
    con.execute("CREATE TABLE storage.table_detail AS SELECT * FROM (VALUES "
                "('sales', 'work', 'a', 'MANAGED', 10, 1000, 2, '2026-10-21', 'described'), "
                "('sales', 'work', 'v', 'VIEW', 5, NULL, NULL, NULL, 'view'), "
                "('sandbox', 'work', 'b', 'MANAGED', 3, 10, 1, '2026-10-01', 'described')) "
                "t(table_catalog, table_schema, table_name, table_type, column_count, size_in_bytes, num_files, last_modified, detail)")
    con.execute("CREATE TABLE dims.dim_workspace AS SELECT * FROM (VALUES "
                "('1', 'analytics-prod', 'adb-1.azuredatabricks.net'), ('2', 'sandbox', NULL)) t(workspace_id, name, url)")
    con.close()
    return db


def test_read_workspaces_joins_activity_with_the_catalogs_each_workspace_holds(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_DB", str(_db(tmp_path)))
    from app.core import data

    got = data.read_workspaces(30, None)
    assert got["outcome"] == "ok_rows" and got["window_days"] == 30 and got["sized"]
    rows = {r["workspace_id"]: r for r in got["rows"]}
    assert set(rows) == {"1", "2"}  # workspace 0 (the account console) left out
    one, two = rows["1"], rows["2"]
    assert one["workspace_name"] == "analytics-prod" and one["active_people"] == 40 and one["queries"] == 5000
    # sales reads and writes mostly from workspace 1, so it is held there
    assert one["catalogs"] == ["sales"] and one["tables"] == 1 and one["views"] == 1 and one["size_in_bytes"] == 1000
    assert two["catalogs"] == ["sandbox"] and two["size_in_bytes"] == 10
    assert one["usage_index"] == 100.0 and one["complexity_index"] == 100.0
    assert 0 < two["usage_index"] < one["usage_index"]
    assert got["rows"][0]["workspace_id"] == "1"  # most used first


def test_read_workspaces_keeps_only_the_workspaces_in_scope_and_takes_the_next_window(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_DB", str(_db(tmp_path)))
    from app.core import data

    assert [r["workspace_id"] for r in data.read_workspaces(30, {"2"})["rows"]] == ["2"]
    assert data.read_workspaces(60, None)["window_days"] == 90


def test_read_workspaces_is_not_assessed_without_activity(tmp_path, monkeypatch):
    db = tmp_path / "empty.duckdb"
    duckdb.connect(str(db)).close()
    monkeypatch.setenv("AUDIT_DB", str(db))
    from app.core import data

    assert data.read_workspaces(30, None) == {"outcome": "not_assessed", "info": None, "rows": []}
    assert json.dumps(data.read_workspaces(30, None))  # the API can return it


def test_the_detail_statements_are_valid_databricks_sql():
    for f in (wa.table_access_sql, wa.user_access_sql, wa.user_tables_sql):
        sqlglot.parse_one(f(END, 90), read="databricks")
    assert f"<= {wa.USER_TABLES_MAX}" in wa.user_tables_sql(END, 90)


def test_identities_are_masked_when_masking_is_on(tmp_path):
    class Cur:
        description, _rows = None, []

        def execute(self, sql):
            if "AS identity" in sql and "tables_written" in sql:
                cols = [f.name for f in wa.USER_SCHEMA]
                self.description = [(c,) for c in cols]
                self._rows = [("1", "alice@example.com", "2026-10-29", 2, 5, "2026-10-29", 1, 0)]
            else:
                self.description, self._rows = [("x",)], []

        def fetchall(self):
            return self._rows

    cur = Cur()
    conn = type("C", (), {"cursor": lambda self: cur, "close": lambda self: None})()
    wa.export(lambda: type("K", (), {"conn": conn})(), tmp_path, end=END, windows=[30], mask=lambda v: "masked")
    got = pq.read_table(str(tmp_path / "usage" / wa.USER_FILE)).to_pylist()
    assert got[0]["identity"] == "masked" and got[0]["queries"] == 5


def _detail_db(tmp_path):
    db = _db(tmp_path)
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE usage.table_access AS SELECT * FROM (VALUES "
                "('1', 'sales.work.a', '2026-10-20', '2026-10-21', 7, 2, 3), "
                "('1', 'shared.ref.codes', '2026-10-01', NULL, 4, 0, 1)) "
                "t(workspace_id, table_full_name, last_read, last_write, reads, writes, readers)")
    con.execute("CREATE TABLE usage.user_access AS SELECT * FROM (VALUES "
                "('1', 'alice@example.com', '2026-10-29', 3, 40, '2026-10-29', 2, 1), "
                "('1', '1b2c3d4e-0000-1111-2222-333344445555', '2026-10-28', 0, 9, '2026-10-28', 0, 1)) "
                "t(workspace_id, identity, last_seen, logins, queries, last_query, tables_read, tables_written)")
    con.execute("CREATE TABLE usage.user_tables AS SELECT * FROM (VALUES "
                "('1', 'alice@example.com', 'sales.work.a', 5, 1, '2026-10-21')) "
                "t(workspace_id, identity, table_full_name, reads, writes, last_access)")
    con.execute("INSERT INTO storage.table_detail VALUES ('sales', 'locked', 'c', 'MANAGED', 4, NULL, NULL, NULL, 'failed: [PERMISSION_DENIED] no')")
    con.close()
    return db


def test_workspace_detail_lists_its_tables_with_reads_and_its_people(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_DB", str(_detail_db(tmp_path)))
    from app.core import data

    got = data.read_workspace_detail("1")
    assert got["catalogs"] == ["sales"] and got["lineage"] and got["sized"]
    tables = {t["table_full_name"]: t for t in got["tables"]}
    # the catalog's own tables, plus a table only used here
    assert set(tables) == {"sales.work.a", "sales.work.v", "sales.locked.c", "shared.ref.codes"}
    a = tables["sales.work.a"]
    assert a["size_in_bytes"] == 1000 and a["reads"] == 7 and a["last_read"] == "2026-10-20" and a["readers"] == 3
    assert tables["sales.work.v"]["view"] and tables["sales.locked.c"]["reads"] == 0
    kinds = {u["identity"]: u["kind"] for u in got["users"]}
    assert kinds == {"alice@example.com": "Person", "1b2c3d4e-0000-1111-2222-333344445555": "Service principal"}
    assert got["not_described"] == [{"table_catalog": "sales", "table_schema": "locked", "tables": 1,
                                     "error": "[PERMISSION_DENIED] no"}]
    assert data.read_user_tables("1", "alice@example.com")["rows"][0]["table_full_name"] == "sales.work.a"
    assert data.read_table_sizes()["not_described"][0]["table_schema"] == "locked"


def test_last_seen_bands_and_inactive_people_overall_and_per_workspace(tmp_path, monkeypatch):
    db = _detail_db(tmp_path)
    con = duckdb.connect(str(db))
    con.execute("""CREATE OR REPLACE TABLE usage.workspace_activity_info AS SELECT '{"exported": true, "end": "2026-10-30"}' AS info""")
    con.execute("CREATE TABLE usage.user_last_seen AS SELECT * FROM (VALUES "
                "('1', 'alice@example.com', '2026-10-29', '2026-10-29'), "
                "('1', 'bob@example.com', '2026-06-01', '2026-05-01'), "
                "('2', 'bob@example.com', '2026-07-15', NULL), "
                "('1', '1b2c3d4e-0000-1111-2222-333344445555', '2026-03-01', NULL), "
                "('1', 'system-user', '2026-01-01', NULL)) t(workspace_id, identity, last_seen, last_login)")
    con.close()
    monkeypatch.setenv("AUDIT_DB", str(db))
    from app.core import data

    got = data.read_workspaces(30, None)
    bands = {b["band"]: (b["people"], b["service_principals"]) for b in got["seen_bands"]}
    # bob counts once, by his latest day anywhere (Jul 15: 107 days); system-user left out
    assert bands == {"Under 30 days": (1, 0), "30-90 days": (0, 0), "90-180 days": (1, 0), "180+ days": (0, 1)}
    assert [(p["identity"], p["days"]) for p in got["inactive"]] == [
        ("1b2c3d4e-0000-1111-2222-333344445555", 243), ("bob@example.com", 107)]
    assert sorted(next(p for p in got["inactive"] if p["identity"] == "bob@example.com")["workspaces"]) == ["analytics-prod", "sandbox"]
    one = next(r for r in got["rows"] if r["workspace_id"] == "1")
    assert one["seen_bands"]["Under 30 days"] == 1 and one["seen_bands"]["90-180 days"] == 1

    detail = data.read_workspace_detail("1")
    assert [r["identity"] for r in detail["inactive"]] == ["1b2c3d4e-0000-1111-2222-333344445555", "bob@example.com"]
    assert next(r for r in detail["inactive"] if r["identity"] == "bob@example.com")["days"] == 151


def test_volumes_and_their_access_and_the_schemas_of_a_workspace(tmp_path, monkeypatch):
    db = _detail_db(tmp_path)
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE storage.volume_detail AS SELECT * FROM (VALUES "
                "('sales', 'work', 'raw', 'MANAGED', 10, 5000, false, 'listed'), "
                "('sales', 'work', 'old', 'EXTERNAL', 2, 70, false, 'listed')) "
                "t(volume_catalog, volume_schema, volume_name, volume_type, files, size_in_bytes, truncated, detail)")
    con.execute("CREATE TABLE usage.volume_access AS SELECT * FROM (VALUES "
                "('1', 'sales.work.raw', 'alice@example.com', 3, 1, '2026-10-20', '2026-10-21'), "
                "('1', 'other.x.y', 'bob@example.com', 1, 0, '2026-10-02', NULL)) "
                "t(workspace_id, volume_full_name, identity, reads, writes, last_read, last_write)")
    con.close()
    monkeypatch.setenv("AUDIT_DB", str(db))
    from app.core import data

    got = data.read_volumes(None)
    vols = {v["volume_full_name"]: v for v in got["volumes"]}
    assert vols["sales.work.raw"]["workspace_name"] == "analytics-prod" and vols["sales.work.raw"]["reads"] == 3
    assert vols["sales.work.old"]["reads"] == 0 and vols["sales.work.old"]["users"] == 0
    assert vols["other.x.y"]["detail"] == "not listed"  # used but never listed
    assert {p["identity"] for p in got["people"]} == {"alice@example.com", "bob@example.com"}
    assert data.read_volume_users("sales.work.raw")["rows"][0]["identity"] == "alice@example.com"
    assert [v["volume_full_name"] for v in data.read_volumes({"2"})["volumes"]] == []

    work = next(s for s in data.read_workspace_detail("1")["schemas"] if s["table_schema"] == "work")
    assert (work["tables"], work["views"], work["table_columns"], work["view_columns"]) == (1, 1, 10, 5)
    assert (work["volumes"], work["volume_files"], work["volume_bytes"]) == (2, 12, 5070)
    one = next(r for r in data.read_workspaces(30, None)["rows"] if r["workspace_id"] == "1")
    assert one["volumes"] == 2 and one["volume_files"] == 12


def test_every_table_and_one_tables_detail(tmp_path, monkeypatch):
    db = _detail_db(tmp_path)
    con = duckdb.connect(str(db))
    con.execute("ALTER TABLE storage.table_detail ADD COLUMN data_source_format VARCHAR")
    con.execute("ALTER TABLE storage.table_detail ADD COLUMN last_altered VARCHAR")
    con.execute("CREATE TABLE usage.table_daily AS SELECT * FROM (VALUES "
                "('sales.work.a', '2026-10-20', 5, 1, 2, 1), ('sales.work.a', '2026-10-21', 2, 1, 1, 1)) "
                "t(table_full_name, day, reads, writes, readers, writers)")
    con.execute("CREATE TABLE storage.table_history AS SELECT * FROM (VALUES "
                "('sales.work.a', 3, '2026-10-21 10:00', 'OPTIMIZE', 'etl-sp', 'numAddedFiles=1'), "
                "('sales.work.a', 2, '2026-10-20 10:00', 'MERGE', 'alice@example.com', NULL)) "
                "t(table_full_name, version, timestamp, operation, user_name, metrics)")
    con.execute("CREATE TABLE usage.table_maintenance AS SELECT * FROM (VALUES "
                "('sales.work.a', '2026-10-22 01:00', 'VACUUM', 'SUCCESSFUL', 0.4, 'ESTIMATED_DBU')) "
                "t(table_full_name, start_time, operation_type, operation_status, usage_quantity, usage_unit)")
    con.close()
    monkeypatch.setenv("AUDIT_DB", str(db))
    from app.core import data

    every = data.read_tables(None)
    a = next(t for t in every["rows"] if t["table_full_name"] == "sales.work.a")
    assert a["workspace_name"] == "analytics-prod" and a["reads"] == 7 and a["size_in_bytes"] == 1000
    assert data.read_tables({"2"})["rows"] == [r for r in data.read_tables({"2"})["rows"] if r["workspace_id"] == "2"]

    t = data.read_table("sales.work.a")
    assert t["workspace_name"] == "analytics-prod" and t["facts"]["size_in_bytes"] == 1000
    assert [r["day"] for r in t["daily"]] == ["2026-10-20", "2026-10-21"]
    assert [h["operation"] for h in t["history"]] == ["OPTIMIZE", "MERGE"]
    assert t["maintenance"][0]["operation_type"] == "VACUUM"
    assert t["users"][0]["identity"] == "alice@example.com"


def test_catalogs_pinned_in_workspace_csv_win_over_lineage(tmp_path, monkeypatch):
    from app.core import data, workspace_file

    csv_path = tmp_path / "workspace.csv"
    csv_path.write_text("workspace_id,space,catalogs\n2,team-a,sales; other\n", encoding="utf-8")
    monkeypatch.setattr(workspace_file, "PATH", csv_path)
    assert workspace_file.read(csv_path) == {"2": {"space": "team-a"}}  # catalogs is not a tag
    assert workspace_file.catalogs(csv_path) == {"sales": "2", "other": "2"}
    monkeypatch.setenv("AUDIT_DB", str(_db(tmp_path)))
    rows = {r["workspace_id"]: r for r in data.read_workspaces(30, None)["rows"]}
    # sales is read mostly from workspace 1, but the file pins it to 2
    assert rows["2"]["catalogs"] == ["sales", "sandbox"] and rows["1"]["catalogs"] == []
    assert data.read_workspace_detail("2")["catalogs"] == ["other", "sales", "sandbox"]
