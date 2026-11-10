"""app/api/refresh.py: export then load, switch AUDIT_DB to the new file, report failures. The two
steps are faked -- a real refresh connects to Databricks."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.api import refresh


def _wait():
    for _ in range(100):
        if not refresh.status()["running"]:
            return refresh.status()
        time.sleep(0.05)
    raise AssertionError("refresh never finished")


def test_refresh_runs_both_steps_and_switches_the_db(monkeypatch, tmp_path):
    calls = []

    def fake_call(args, timeout):
        calls.append(args[0])
        if args[0] == "tools/load_direct_results.py":
            Path(args[args.index("--db") + 1]).touch()  # the real loader writes the db file

    monkeypatch.setattr(refresh.datasource, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(refresh, "_call", fake_call)
    monkeypatch.delenv("AUDIT_DB", raising=False)
    old = tmp_path / "audit-20000101-000000.duckdb"
    old.write_text("x")
    for stamp in ("20000101-000000", "19991231-000000", "19991230-000000"):
        (tmp_path / "results" / stamp).mkdir(parents=True)

    refresh.start(15)
    s = _wait()

    assert s["step"] == "done" and s["days"] == 15 and s["error"] is None
    assert calls == ["tools/export_direct_results.py", "tools/load_direct_results.py"]
    new_db = refresh.os.environ["AUDIT_DB"]
    assert new_db.startswith(str(tmp_path)) and new_db.endswith(".duckdb")
    assert not old.exists()
    assert refresh.datasource.current_db() == Path(new_db)
    remaining = sorted(p.name for p in (tmp_path / "results").iterdir())
    assert len(remaining) == 2  # this refresh's stamp plus the previous one
    assert "19991230-000000" not in remaining


def test_refresh_switches_the_shared_connection_and_closes_the_old_one(monkeypatch, tmp_path):
    import duckdb

    from app.core import data as app_core_data

    monkeypatch.setattr(refresh.datasource, "data_dir", lambda: tmp_path)
    monkeypatch.delenv("AUDIT_DB", raising=False)
    monkeypatch.setattr(app_core_data, "share_connection", True)
    monkeypatch.setattr(app_core_data, "_SHARED_CONS", {})
    monkeypatch.setattr(app_core_data, "_RETIRED", [])

    old_db = tmp_path / "audit-old.duckdb"
    duckdb.connect(str(old_db)).close()
    refresh.datasource.set_current_db(old_db)
    in_flight = app_core_data._connect()  # populates the shared connection on the old db
    old_base = next(iter(app_core_data._SHARED_CONS.values()))

    def fake_call(args, timeout):
        if args[0] == "tools/load_direct_results.py":
            db_arg = Path(args[args.index("--db") + 1])
            duckdb.connect(str(db_arg)).close()

    monkeypatch.setattr(refresh, "_call", fake_call)
    refresh.start(7)
    s = _wait()
    assert s["step"] == "done"

    new_db = Path(refresh.os.environ["AUDIT_DB"])
    assert refresh.datasource.current_db() == new_db

    app_core_data._connect()
    assert [k[0] for k in app_core_data._SHARED_CONS] == [str(new_db)]
    # a request still reading through the old handle keeps working; the handle closes after the grace
    assert in_flight.execute("SELECT 1").fetchone() == (1,)
    monkeypatch.setattr(app_core_data, "_RETIRE_AFTER_S", -1)
    app_core_data._retire(None)
    with pytest.raises(duckdb.Error):
        old_base.execute("SELECT 1")


def test_refresh_failure_is_reported_and_keeps_the_db(monkeypatch, tmp_path):
    monkeypatch.setattr(refresh.datasource, "data_dir", lambda: tmp_path)
    monkeypatch.setenv("AUDIT_DB", "keep.duckdb")

    def boom(args, timeout):
        raise RuntimeError("no credentials")

    monkeypatch.setattr(refresh, "_call", boom)
    refresh.start(30)
    s = _wait()

    assert s["step"] == "failed" and s["error"] == "no credentials"
    assert refresh.os.environ["AUDIT_DB"] == "keep.duckdb"


@pytest.mark.parametrize("mode,in_app,expect_split", [("export", True, True), ("query", True, False), ("export", False, False)])
def test_refresh_builds_per_user_files_only_for_the_app_with_apply_export(monkeypatch, tmp_path, mode, in_app, expect_split):
    from app.core import rls

    built = []

    def fake_call(args, timeout):
        if args[0] == "tools/load_direct_results.py":
            Path(args[args.index("--db") + 1]).touch()

    def fake_sets(db):
        built.append(db)
        other = db.with_name(db.stem + ".rls-abc.duckdb")
        other.touch()
        (db.with_name(db.stem + ".rls-abc.run_results.json")).write_text("{}")
        (db.with_name(db.stem + ".rls_sets.json")).write_text('{"__none__": "%s"}' % other.name)
        return {}

    monkeypatch.setattr(refresh.datasource, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(refresh, "_call", fake_call)
    monkeypatch.setattr(rls, "mode", lambda: mode if in_app else "export")
    monkeypatch.setattr(rls, "is_app", lambda: in_app)
    monkeypatch.setattr(rls, "build_sets", fake_sets)
    monkeypatch.delenv("AUDIT_DB", raising=False)
    (tmp_path / "results").mkdir()

    refresh.start(15)
    assert _wait()["step"] == "done"
    assert bool(built) is expect_split
    if expect_split:
        names = {p.name for p in tmp_path.glob("audit-*")}
        assert any("rls-abc.duckdb" in n for n in names) and any("rls-abc.run_results.json" in n for n in names)


def test_refresh_step_that_hangs_times_out_and_fails(monkeypatch, tmp_path):
    import subprocess

    seen = []

    def hang(cmd, **kw):
        seen.append(kw["timeout"])
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(refresh.datasource, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(refresh.subprocess, "run", hang)
    refresh.start(30)
    s = _wait()

    assert seen == [refresh.EXPORT_TIMEOUT_S]
    assert s["step"] == "failed" and "timed out" in s["error"]


def _app_env(monkeypatch, tmp_path, rls_enabled):
    import os
    import shutil

    cfg = tmp_path / "config"
    shutil.copytree(os.environ["AUDIT_CONFIG_DIR"], cfg)
    (cfg / "app.local.yml").write_text(
        f"rls:\n  enabled: {str(rls_enabled).lower()}\n  apply: query\n  columns: [workspace]\n"
        "refresh:\n  admins: [Ops@X.com]\n", encoding="utf-8")
    monkeypatch.setenv("AUDIT_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8000")
    grants = {"alice@x.com": [("1111", "*", "*")], "carol@x.com": [("*", "*", "*")]}
    monkeypatch.setattr(refresh.rls, "principal_grants", lambda base, who: grants.get(who, []))
    started = []
    monkeypatch.setattr(refresh, "start", lambda days: started.append(days) or {"running": True})
    return started


def _post(email):
    from fastapi.testclient import TestClient

    from app.api.app import app

    return TestClient(app).post("/api/refresh", params={"days": 7},
                                headers={"x-forwarded-email": email} if email else {})


def test_refresh_with_rls_is_limited_to_admins_and_unrestricted_users(monkeypatch, tmp_path):
    started = _app_env(monkeypatch, tmp_path, rls_enabled=True)
    for email in ("alice@x.com", "nobody@x.com", None):
        r = _post(email)
        assert r.status_code == 403 and "refresh.admins" in r.json()["detail"]
    assert started == []
    assert _post("carol@x.com").status_code == 200
    assert _post("ops@x.com").status_code == 200
    assert started == [7, 7]


def test_refresh_without_rls_is_open_to_everyone(monkeypatch, tmp_path):
    started = _app_env(monkeypatch, tmp_path, rls_enabled=False)
    assert _post(None).status_code == 200
    assert _post("alice@x.com").status_code == 200
    assert started == [7, 7]
