"""Row-level access through the API: one user's rows and caches never reach another."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from app.api.app import app
from app.core import rls

QUERY = "compute_serving_dormant_endpoints"  # rows in workspaces 1111, 2222 and 3333
GRANTS = [("alice@x.com", "1111"), ("bob@x.com", "2222"), ("Carol@X.com ", "*"), ("dave@x.com", "1111")]
client = TestClient(app)


@pytest.fixture(scope="module")
def full_db(tmp_path_factory):
    import pyarrow as pa
    import pyarrow.parquet as pq
    d = tmp_path_factory.mktemp("rls_api")
    db = d / "audit-1.duckdb"
    shutil.copyfile(os.environ["AUDIT_DB"], db)
    pq.write_table(pa.table({
        "principal": [g[0] for g in GRANTS], "workspace_id": [g[1] for g in GRANTS],
        "warehouse_id": [None] * len(GRANTS), "environment": [None] * len(GRANTS)}), str(d / "e.parquet"))
    con = duckdb.connect(str(db))
    rls.add_columns(con)
    rls.load_entitlements(con, d / "e.parquet", {"user": "ops@x.com"}, rls.Rls(True, "a.b.c", ("workspace",), "query"))
    con.close()
    return db


def _env(monkeypatch, tmp_path, db, apply, enabled=True):
    cfg = tmp_path / "config"
    shutil.copytree(os.environ["AUDIT_CONFIG_DIR"], cfg)
    (cfg / "app.local.yml").write_text(
        f"rls:\n  enabled: {str(enabled).lower()}\n  apply: {apply}\n  columns: [workspace]\n", encoding="utf-8")
    monkeypatch.setenv("AUDIT_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("AUDIT_DB", str(db))
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8000")


def _who(email):
    return {"x-forwarded-email": email} if email else {}


def _workspaces(email, path=f"/api/finding/{QUERY}"):
    r = client.get(path, params={"window": 30, "limit": 5000}, headers=_who(email))
    assert r.status_code == 200, r.text
    return {str(row.get("workspace_id")) for row in r.json().get("rows", [])}


def _count(email):
    r = client.get("/api/findings", params={"window": 30}, headers=_who(email))
    assert r.status_code == 200, r.text
    return {f["query_id"]: f["row_count"] for f in r.json()["findings"]}[QUERY]


def test_query_mode_users_see_their_own_rows(monkeypatch, tmp_path, full_db):
    _env(monkeypatch, tmp_path, full_db, "query")
    everything = _workspaces("carol@x.com")
    assert {"1111", "2222"} <= everything
    assert _workspaces("alice@x.com") == {"1111"}
    assert _workspaces("BOB@x.com") == {"2222"}
    assert _workspaces("nobody@x.com") == set()
    assert _workspaces(None) == set()


def test_query_mode_caches_do_not_leak_between_users(monkeypatch, tmp_path, full_db):
    _env(monkeypatch, tmp_path, full_db, "query")
    first = [_count(u) for u in ("alice@x.com", "bob@x.com", "carol@x.com", None, "alice@x.com")]
    assert first[0] == first[4] and first[3] == 0
    assert len(set(first[:3])) == 3 and first[2] == max(first)
    dims = {u: {str(w["workspace_id"]) for w in client.get("/api/dims", headers=_who(u)).json()["warehouses"]}
            for u in ("carol@x.com", "alice@x.com", "bob@x.com")}
    assert dims["alice@x.com"] <= {"1111"} and dims["bob@x.com"] <= {"2222"}
    assert len(dims["carol@x.com"]) > len(dims["alice@x.com"])


def test_meta_reports_the_callers_access(monkeypatch, tmp_path, full_db):
    _env(monkeypatch, tmp_path, full_db, "query")
    got = client.get("/api/meta", headers=_who("alice@x.com")).json()["rls"]
    assert got == {"enabled": True, "apply": "query", "user": "alice@x.com", "granted": True, "error": None}
    assert client.get("/api/meta", headers=_who("nobody@x.com")).json()["rls"]["granted"] is False
    assert client.get("/api/meta").json()["rls"]["user"] is None


def test_disabled_changes_nothing(monkeypatch, tmp_path, full_db):
    _env(monkeypatch, tmp_path, full_db, "query", enabled=False)
    assert {"1111", "2222"} <= _workspaces(None)
    assert client.get("/api/meta").json()["rls"] == {
        "enabled": False, "apply": None, "user": None, "granted": True, "error": None}


def test_export_mode_serves_each_users_own_file(monkeypatch, tmp_path, full_db):
    _env(monkeypatch, tmp_path, full_db, "export")
    sets = rls.build_sets(full_db)
    assert sets["alice@x.com"] == sets["dave@x.com"] != sets["bob@x.com"]
    assert sets[rls.NONE_KEY] not in (sets["alice@x.com"], sets["carol@x.com"])
    assert _workspaces("alice@x.com") == {"1111"}
    assert _workspaces("bob@x.com") == {"2222"}
    assert {"1111", "2222"} <= _workspaces("carol@x.com")
    assert _workspaces("nobody@x.com") == set() and _workspaces(None) == set()
    assert _count("alice@x.com") != _count("bob@x.com")
    meta = client.get("/api/meta", headers=_who("bob@x.com")).json()["rls"]
    assert meta["apply"] == "export" and meta["granted"] is True and meta["user"] == "bob@x.com"
    assert client.get("/api/meta", headers=_who("nobody@x.com")).json()["rls"]["granted"] is False
    other = Path(full_db).with_name(sets["bob@x.com"])
    assert b"dave@x.com" not in other.read_bytes()


def test_export_mode_without_sets_serves_nothing(monkeypatch, tmp_path, full_db):
    fresh = tmp_path / "audit-2.duckdb"
    shutil.copyfile(full_db, fresh)
    _env(monkeypatch, tmp_path, fresh, "export")
    r = client.get(f"/api/finding/{QUERY}", params={"window": 30}, headers=_who("alice@x.com"))
    assert r.status_code != 200 or not r.json().get("rows")
    meta = client.get("/api/meta", headers=_who("alice@x.com"))
    assert meta.status_code != 200 or (meta.json()["rls"]["granted"] is False and "refresh" in meta.json()["rls"]["error"])


def test_every_catalog_lookup_is_limited_to_the_open_database():
    # The scoped connection attaches the full db too; an unlimited lookup would list its columns as well.
    root = Path(__file__).resolve().parent.parent / "app"
    bare = []
    for f in root.rglob("*.py"):
        lines = f.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if f.name != "rls.py" and ("information_schema.tables" in line or "information_schema.columns" in line)                     and "current_database" not in " ".join(lines[i:i + 3]) and "direct_export_meta" not in line                     and not line.lstrip().startswith(("#", "\"\"\"")) and "SELECT" in line:
                bare.append(f"{f.name}:{i + 1}")
    assert not bare
