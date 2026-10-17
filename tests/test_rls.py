"""Row-level access: config, the matching rule, added columns and the filtered file."""
from __future__ import annotations

import duckdb
import pytest

from app.core import rls


def _db(path):
    con = duckdb.connect(str(path))
    for s in ("findings", "dims", "tags"):
        con.execute(f"CREATE SCHEMA {s}")
    con.execute("CREATE TABLE dims.dim_workspace AS SELECT * FROM (VALUES ('1','prod'),('2','dev')) v(workspace_id, env)")
    con.execute("CREATE TABLE dims.dim_warehouse AS SELECT * FROM (VALUES ('w1','1'),('w2','2')) v(warehouse_id, workspace_id)")
    con.execute("CREATE TABLE findings.by_ws AS SELECT * FROM (VALUES ('1','ONE_MARKER'),('2','TWO_MARKER'),"
                "(NULL,'NULL_MARKER'),('0','ZERO_MARKER')) v(workspace_id, note)")
    con.execute("CREATE TABLE findings.by_wh AS SELECT * FROM (VALUES ('w1','A_MARKER'),('w2','B_MARKER')) v(warehouse_id, note)")
    con.execute("CREATE TABLE direct_export_meta AS SELECT 'x' AS as_of")
    rls.add_columns(con)
    return con


def _entitle(tmp_path, con, rows, error=None):
    import pyarrow as pa
    import pyarrow.parquet as pq
    cols = list(zip(*rows)) if rows else [(), (), (), ()]
    pq.write_table(pa.table({n: pa.array(list(c), pa.string()) for n, c in
                             zip(("principal", "workspace_id", "warehouse_id", "environment"), cols)}),
                   str(tmp_path / "e.parquet"))
    rls.load_entitlements(con, tmp_path / "e.parquet", {"user": "ana@x.com", "error": error},
                          rls.Rls(True, "a.b.c", ("workspace", "environment"), "query"))


@pytest.mark.parametrize("block,msg", [
    ({"columns": ["cluster"]}, "columns"), ({"columns": []}, "columns"), ({"apply": "live"}, "apply"),
    ({"table": "b.c"}, "table"), ({"enabled": "yes"}, "enabled"), ({"extra": 1}, "unknown"),
])
def test_bad_config_is_refused(block, msg):
    with pytest.raises(rls.RlsError, match=msg):
        rls.validate(block)


def test_defaults():
    assert rls.validate(None) == rls.Rls(False, "main.governance.audit_entitlements", ("workspace",), "query")


def test_the_committed_config_is_off():
    assert rls.validate(rls.merged_block(rls.app_config.ROOT / "config" / "app.yml")).enabled is False


def _kept(grants, cols, rows):
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES " + ",".join(
        f"({','.join('NULL' if v is None else repr(v) for v in r)})" for r in rows) + ") v(workspace_id, env)")
    return con.execute(f"SELECT count(*) FROM t WHERE {rls.predicate(cols, grants)}").fetchone()[0]


ROWS = [("1", "prod"), ("1", "dev"), ("2", "prod"), (None, "prod"), ("0", "prod"), ("1", None)]


def test_all_columns_must_pass_in_the_same_grant_row():
    grants = [("1", "*", "dev"), ("2", "*", "prod")]
    assert _kept(grants, ("workspace", "environment"), ROWS) == 2  # 1/dev and 2/prod, not 1/prod
    assert _kept([("1", "*", "*")], ("workspace",), ROWS) == 3


def test_star_gives_everything_and_no_grant_nothing():
    assert _kept([("*", "*", "*")], ("workspace",), ROWS) == len(ROWS)
    assert _kept([], ("workspace",), ROWS) == 0


def test_null_and_zero_rows_need_star():
    assert _kept([("1", "*", "*")], ("workspace",), [(None, "p"), ("0", "p")]) == 0
    assert _kept([("*", "*", "*")], ("workspace",), [(None, "p"), ("0", "p")]) == 2
    assert _kept([("*", "*", "prod")], ("environment",), ROWS) == 4
    assert _kept([("*", "*", "PROD".lower())], ("environment",), [("1", "PROD")]) == 1


def test_add_columns_fills_from_the_dims(tmp_path):
    con = _db(tmp_path / "a.duckdb")
    assert con.execute("SELECT workspace_id, env FROM findings.by_wh WHERE warehouse_id = 'w2'").fetchone() == ("2", "dev")
    assert con.execute("SELECT env FROM findings.by_ws WHERE workspace_id = '1'").fetchone() == ("prod",)
    assert con.execute("SELECT env FROM findings.by_ws WHERE workspace_id IS NULL").fetchone() == (None,)
    assert con.execute("SELECT count(*) FROM rls.added WHERE table_name = 'dim_workspace'").fetchone()[0] == 1  # warehouse_id only


def test_load_entitlements_normalises_and_fails_closed(tmp_path):
    con = _db(tmp_path / "a.duckdb")
    _entitle(tmp_path, con, [(" Ana@X.com ", "1", None, ""), ("bob@x.com", "*", None, "PROD")])
    assert rls.grants_for(con, "ANA@x.com") == [("1", "*", "*")]
    assert rls.grants_for(con, "bob@x.com") == [("*", "*", "prod")]
    assert rls.grants_for(con, "nobody@x.com") == [] and rls.grants_for(con, None) == []
    _entitle(tmp_path, con, [("ana@x.com", "*", None, None)], error="boom")
    assert rls.grants_for(con, "ana@x.com") == []
    assert con.execute("SELECT error FROM rls.status").fetchone()[0] == "boom"


def test_filter_into_keeps_only_entitled_rows_and_none_of_the_rest_in_the_bytes(tmp_path):
    src = tmp_path / "a.duckdb"
    con = _db(src)
    _entitle(tmp_path, con, [("ana@x.com", "1", None, None), ("bob@x.com", "2", None, None)])
    grants = rls.grants_for(con, "ana@x.com")
    con.close()
    out = tmp_path / "ana.duckdb"
    kept = rls.filter_into(src, out, grants, "ana@x.com", ("workspace",))
    assert kept["findings.by_ws"] == 1 and kept["findings.by_wh"] == 1
    assert b"TWO_MARKER" not in out.read_bytes() and b"NULL_MARKER" not in out.read_bytes() and b"B_MARKER" not in out.read_bytes()
    assert b"ONE_MARKER" in out.read_bytes()
    chk = duckdb.connect(str(out), read_only=True)
    assert [r[0] for r in chk.execute("DESCRIBE findings.by_wh").fetchall()] == ["warehouse_id", "note"]  # added columns hidden
    assert chk.execute("SELECT principal FROM rls.entitlements").fetchall() == [("ana@x.com",)]
    assert chk.execute("SELECT count(*) FROM direct_export_meta").fetchone()[0] == 1


def test_csv_entitlements_load(tmp_path):
    con = _db(tmp_path / "a.duckdb")
    (tmp_path / "e.csv").write_text("principal,workspace_id,warehouse_id,environment\nana@x.com,1,,\n", encoding="utf-8")
    rls.load_entitlements(con, tmp_path / "e.csv", {}, rls.Rls(True, "a.b.c", ("workspace",), "query"))
    assert rls.grants_for(con, "ana@x.com") == [("1", "*", "*")]


def test_a_column_added_to_the_config_after_the_load_grants_nothing(tmp_path):
    con = _db(tmp_path / "a.duckdb")
    _entitle(tmp_path, con, [("ana@x.com", "1", None, None)])
    con.execute("UPDATE rls.status SET columns = 'workspace'")
    assert rls.grants_for(con, "ana@x.com") == [("1", "*", "*")]
    con.execute("UPDATE rls.status SET columns = 'environment'")
    assert rls.grants_for(con, "ana@x.com") == []


def test_only_a_filtered_db_is_marked(tmp_path):
    src = tmp_path / "a.duckdb"
    con = _db(src)
    _entitle(tmp_path, con, [("ana@x.com", "1", None, None)])
    con.close()
    rls.filter_into(src, tmp_path / "b.duckdb", [("1", "*", "*")], "ana@x.com", ("workspace",))
    assert rls.is_filtered(tmp_path / "b.duckdb") and not rls.is_filtered(src)
