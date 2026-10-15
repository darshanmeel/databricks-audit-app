"""app/core/datasource.py's own defaults, and app/core/data.py's use of them when AUDIT_DB (and
friends) are unset -- the "no paths typed" path from export through load to the running app."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import data as app_core_data  # noqa: E402
from app.core import datasource  # noqa: E402


def test_default_data_dir_windows():
    d = datasource.default_data_dir("win32", environ={"LOCALAPPDATA": r"C:\Users\me\AppData\Local"}, home=Path("C:/Users/me"))
    assert d == Path(r"C:\Users\me\AppData\Local") / "databricks-audit"


def test_default_data_dir_windows_falls_back_without_localappdata():
    d = datasource.default_data_dir("win32", environ={}, home=Path("C:/Users/me"))
    assert d == Path("C:/Users/me") / "AppData" / "Local" / "databricks-audit"


def test_default_data_dir_macos():
    d = datasource.default_data_dir("darwin", environ={}, home=Path("/Users/me"))
    assert d == Path("/Users/me/Library/Application Support/databricks-audit")


def test_default_data_dir_linux_xdg():
    d = datasource.default_data_dir("linux", environ={"XDG_DATA_HOME": "/data"}, home=Path("/home/me"))
    assert d == Path("/data/databricks-audit")


def test_default_data_dir_linux_falls_back_without_xdg():
    d = datasource.default_data_dir("linux", environ={}, home=Path("/home/me"))
    assert d == Path("/home/me/.local/share/databricks-audit")


def test_data_dir_honours_the_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    assert datasource.data_dir() == tmp_path


def test_results_dir_and_db_path_and_sidecar(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    assert datasource.results_dir() == tmp_path / "results"
    assert datasource.db_path() == tmp_path / "audit.duckdb"
    assert datasource.sidecar(tmp_path / "x.duckdb", "source") == tmp_path / "x.source.json"


def test_inside_repo():
    assert datasource.inside_repo(datasource.ROOT / "app") is True
    assert datasource.inside_repo(datasource.ROOT.parent / "elsewhere") is False


# ------------------------------------------------------------------------------------------
# app/core/data.py: with no AUDIT_DB set, the data folder's own audit.duckdb wins once it exists.
# ------------------------------------------------------------------------------------------


def test_db_path_prefers_audit_db_env(monkeypatch, tmp_path):
    monkeypatch.delenv("AUDIT_DB", raising=False)
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    (tmp_path / "audit.duckdb").touch()
    monkeypatch.setenv("AUDIT_DB", str(tmp_path / "explicit.duckdb"))
    assert app_core_data._db_path() == tmp_path / "explicit.duckdb"


def test_db_path_falls_back_to_legacy_default_when_data_folder_db_is_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("AUDIT_DB", raising=False)
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))  # empty -- no audit.duckdb here
    assert app_core_data._db_path() == app_core_data.ROOT / "data" / "db_audit.duckdb"


def test_db_path_serves_the_data_folder_db_once_it_exists(monkeypatch, tmp_path):
    monkeypatch.delenv("AUDIT_DB", raising=False)
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    (tmp_path / "audit.duckdb").touch()
    assert app_core_data._db_path() == tmp_path / "audit.duckdb"


def test_db_path_prefers_the_data_folder_db_over_the_legacy_one_regardless_of_mtime(monkeypatch, tmp_path):
    monkeypatch.delenv("AUDIT_DB", raising=False)
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path / "data_dir"))
    (tmp_path / "data_dir").mkdir()
    data_db = tmp_path / "data_dir" / "audit.duckdb"
    legacy_db = tmp_path / "legacy" / "db_audit.duckdb"
    legacy_db.parent.mkdir()
    monkeypatch.setattr(app_core_data, "_LEGACY_DB_PATH", legacy_db)

    legacy_db.touch()
    data_db.touch()
    os.utime(data_db, (500, 500))  # older than the legacy file, but still the one served
    assert app_core_data._db_path() == data_db


def test_db_path_prefers_the_current_db_pointer_over_the_data_folder_default(monkeypatch, tmp_path):
    monkeypatch.delenv("AUDIT_DB", raising=False)
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    (tmp_path / "audit.duckdb").touch()
    pointed = tmp_path / "audit-20260101-000000.duckdb"
    pointed.touch()
    datasource.set_current_db(pointed)
    assert app_core_data._db_path() == pointed


def test_db_path_ignores_a_pointer_whose_target_is_gone(monkeypatch, tmp_path):
    monkeypatch.delenv("AUDIT_DB", raising=False)
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    (tmp_path / "audit.duckdb").touch()
    datasource.set_current_db(tmp_path / "gone.duckdb")
    assert app_core_data._db_path() == tmp_path / "audit.duckdb"


def test_set_current_db_and_current_db_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    assert datasource.current_db() is None
    db = tmp_path / "audit-20260101-000000.duckdb"
    db.touch()
    datasource.set_current_db(db)
    assert datasource.current_db() == db


def test_run_results_path_picks_up_the_matching_sidecar(monkeypatch, tmp_path):
    monkeypatch.delenv("AUDIT_RUN_RESULTS", raising=False)
    monkeypatch.delenv("AUDIT_DB", raising=False)
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    (tmp_path / "audit.duckdb").touch()
    (tmp_path / "audit.run_results.json").write_text("{}", encoding="utf-8")
    assert app_core_data._run_results_path() == tmp_path / "audit.run_results.json"


def test_run_results_path_falls_back_without_a_sidecar(monkeypatch, tmp_path):
    monkeypatch.delenv("AUDIT_RUN_RESULTS", raising=False)
    monkeypatch.delenv("AUDIT_DB", raising=False)
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    (tmp_path / "audit.duckdb").touch()  # no sidecar next to it
    assert app_core_data._run_results_path() == app_core_data.ROOT / "dbt" / "target" / "run_results.json"


def test_snapshot_manifest_path_never_leaks_the_repos_own_snapshot_for_a_loader_built_db(monkeypatch, tmp_path):
    """A db tools/load_direct_results.py built (its run_results.json sidecar exists) must never
    pick up an unrelated snapshot/manifest.json left by a dev dbt build."""
    monkeypatch.delenv("AUDIT_SNAPSHOT_MANIFEST", raising=False)
    monkeypatch.delenv("AUDIT_DB", raising=False)
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    (tmp_path / "audit.duckdb").touch()
    (tmp_path / "audit.run_results.json").write_text("{}", encoding="utf-8")
    manifest_path = app_core_data._snapshot_manifest_path()
    assert manifest_path == datasource.sidecar(tmp_path / "audit.duckdb", "snapshot_manifest")
    assert manifest_path != app_core_data.ROOT / "snapshot" / "manifest.json"


def test_snapshot_manifest_path_detects_a_loader_built_db_at_any_path(monkeypatch, tmp_path):
    """--db pointing outside the data folder still skips the repo's own snapshot, detected by the
    direct_export_meta table alone when there is no run_results.json sidecar next to it."""
    import duckdb

    monkeypatch.delenv("AUDIT_SNAPSHOT_MANIFEST", raising=False)
    db_path = tmp_path / "elsewhere.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE direct_export_meta (as_of VARCHAR)")
    con.close()
    monkeypatch.setenv("AUDIT_DB", str(db_path))
    manifest_path = app_core_data._snapshot_manifest_path()
    assert manifest_path == datasource.sidecar(db_path, "snapshot_manifest")


def test_snapshot_manifest_path_uses_the_repos_own_snapshot_for_an_ordinary_dbt_db(monkeypatch, tmp_path):
    """A plain dbt-built db (no sidecar, no direct_export_meta table) still reads the dev snapshot."""
    import duckdb

    monkeypatch.delenv("AUDIT_SNAPSHOT_MANIFEST", raising=False)
    db_path = tmp_path / "db_audit.duckdb"
    duckdb.connect(str(db_path)).close()
    monkeypatch.setenv("AUDIT_DB", str(db_path))
    assert app_core_data._snapshot_manifest_path() == app_core_data._snapshot_dir() / "manifest.json"
