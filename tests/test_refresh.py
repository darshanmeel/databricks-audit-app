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

    def fake_call(args):
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
    monkeypatch.setattr(app_core_data, "_SHARED_CON", None)
    monkeypatch.setattr(app_core_data, "_SHARED_KEY", None)

    old_db = tmp_path / "audit-old.duckdb"
    duckdb.connect(str(old_db)).close()
    refresh.datasource.set_current_db(old_db)
    app_core_data._connect()  # populates the shared connection on the old db
    old_base = app_core_data._SHARED_CON

    def fake_call(args):
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
    assert app_core_data._SHARED_KEY[0] == str(new_db)
    assert app_core_data._SHARED_CON is not old_base
    with pytest.raises(duckdb.Error):
        old_base.execute("SELECT 1")


def test_refresh_failure_is_reported_and_keeps_the_db(monkeypatch, tmp_path):
    monkeypatch.setattr(refresh.datasource, "data_dir", lambda: tmp_path)
    monkeypatch.setenv("AUDIT_DB", "keep.duckdb")

    def boom(args):
        raise RuntimeError("no credentials")

    monkeypatch.setattr(refresh, "_call", boom)
    refresh.start(30)
    s = _wait()

    assert s["step"] == "failed" and s["error"] == "no credentials"
    assert refresh.os.environ["AUDIT_DB"] == "keep.duckdb"
