"""tools/deploy_app.py and tools/app_start.py: settings, the staged copy, the wheel lookup and
the wheel joined again at start-up (no CLI, no network)."""
from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


dep = _load(ROOT / "tools" / "deploy_app.py", "deploy_app_under_test")
start = _load(ROOT / "tools" / "app_start.py", "app_start_under_test")

ENV = {"DATABRICKS_SERVER_HOSTNAME": "acme.cloud.databricks.com",
       "DATABRICKS_HTTP_PATH": "/sql/1.0/warehouses/abc", "DATABRICKS_TOKEN": "dapi-fake"}


def _settings(tmp_path: Path, text: str, name: str = "app.yml") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _read(tmp_path: Path, text: str) -> dict:
    return dep.read_settings(_settings(tmp_path, text), tmp_path / "no-local.yml")


def _wheel(tmp_path: Path) -> Path:
    wheel = tmp_path / "duckdb-1.5.1-cp311-cp311-manylinux_2_28_x86_64.whl"
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr("duckdb/__init__.py", "x = 1\n")
        z.writestr("filler.bin", bytes(range(256)) * 9000)  # stored, so the wheel spans three parts
    return wheel


def test_the_committed_settings_deploy_without_a_local_file(tmp_path):
    got = dep.read_settings(dep.SETTINGS, tmp_path / "no-local.yml")
    assert (got["name"], got["run_as"], got["rls"]["enabled"]) == ("lakehouse-health", "user", False)


def test_read_settings_takes_the_name_and_runs_as_the_user_by_default(tmp_path):
    assert _read(tmp_path, "{name: my-audit}") == {"name": "my-audit", "run_as": "user"}
    assert _read(tmp_path, "{name: my-audit, run_as: app}")["run_as"] == "app"


def test_the_local_file_wins_line_by_line(tmp_path):
    base = _settings(tmp_path, "{name: my-audit, run_as: user}")
    local = _settings(tmp_path, "{run_as: app}", "app.local.yml")
    assert dep.read_settings(base, local) == {"name": "my-audit", "run_as": "app"}


def test_read_settings_refuses_another_run_as(tmp_path):
    with pytest.raises(dep.DeployError, match="run_as"):
        _read(tmp_path, "{name: my-audit, run_as: admin}")


@pytest.mark.parametrize("text", ["{name: My_Audit}", "{volume: /Volumes/a/b/c}"])
def test_read_settings_refuses_a_bad_name(tmp_path, text):
    with pytest.raises(dep.DeployError, match="name"):
        _read(tmp_path, text)


def test_read_settings_names_the_missing_file(tmp_path):
    with pytest.raises(dep.DeployError, match="config/app.yml is missing"):
        dep.read_settings(tmp_path / "nope.yml", tmp_path / "no-local.yml")


def test_duckdb_pin_comes_from_requirements():
    assert dep.duckdb_pin().startswith("duckdb==")


def test_fetch_wheel_uses_a_wheel_already_there(tmp_path, monkeypatch):
    wheel = tmp_path / "duckdb-1.5.1-cp311-cp311-manylinux_2_28_x86_64.whl"
    wheel.write_bytes(b"")
    monkeypatch.setattr(dep.subprocess, "run", lambda *a, **k: pytest.fail("downloaded again"))
    assert dep.fetch_wheel(tmp_path, "duckdb==1.5.1") == wheel


def test_stage_writes_app_yaml_and_the_wheel_in_parts_and_keeps_sync_state(tmp_path, monkeypatch):
    monkeypatch.setattr(dep, "PART_BYTES", 1024 * 1024)
    wheel = _wheel(tmp_path)
    out = tmp_path / "stage"
    (out / ".databricks").mkdir(parents=True)
    (out / "stale.txt").write_text("old", encoding="utf-8")
    dep.stage(out, ENV, wheel)

    app_yaml = yaml.safe_load((out / "app.yaml").read_text(encoding="utf-8"))
    assert app_yaml["command"] == ["python", "tools/app_start.py"]
    assert {e["name"]: e["value"] for e in app_yaml["env"]} == ENV
    assert not (out / "requirements.txt").exists()
    parts = sorted((out / "wheels").iterdir())
    assert len(parts) == 3 and all(p.stat().st_size <= 1024 * 1024 for p in parts)
    assert b"".join(p.read_bytes() for p in parts) == wheel.read_bytes()
    assert (out / ".databricks").is_dir() and not (out / "stale.txt").exists()
    for d in dep.COPY_DIRS:
        assert (out / d).is_dir()
    assert (out / "app" / "web" / "dist" / "index.html").exists()
    assert not list(out.rglob("__pycache__")) and not (out / "config" / "app.local.yml").exists()


def test_app_start_joins_the_parts_and_unpacks_the_wheel_once(tmp_path, monkeypatch):
    monkeypatch.setattr(dep, "PART_BYTES", 1024 * 1024)
    out = tmp_path / "stage"
    dep.stage(out, ENV, _wheel(tmp_path))
    target = tmp_path / "deps"
    start.unpack(out / "wheels", target)
    assert (target / "duckdb" / "__init__.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not list((out / "wheels").glob("*.whl"))
    (target / "duckdb" / "__init__.py").write_text("kept", encoding="utf-8")
    start.unpack(out / "wheels", target)
    assert (target / "duckdb" / "__init__.py").read_text(encoding="utf-8") == "kept"


def test_stage_as_the_app_stores_no_token(tmp_path):
    out = tmp_path / "stage"
    dep.stage(out, ENV, _wheel(tmp_path), "app")
    app_yaml = yaml.safe_load((out / "app.yaml").read_text(encoding="utf-8"))
    assert [e["name"] for e in app_yaml["env"]] == ["DATABRICKS_SERVER_HOSTNAME", "DATABRICKS_HTTP_PATH"]
    assert "dapi-fake" not in (out / "app.yaml").read_text(encoding="utf-8")


def test_the_app_gets_the_warehouse_of_the_http_path():
    res = dep.warehouse_resource("/sql/1.0/warehouses/abc123")
    assert res == {"name": "sql-warehouse", "sql_warehouse": {"id": "abc123", "permission": "CAN_USE"}}
    assert dep.has_warehouse({"resources": [{"name": "x", "sql_warehouse": {"id": "abc123"}}]}, res)
    assert not dep.has_warehouse({}, res)
    with pytest.raises(dep.DeployError, match="SQL warehouse"):
        dep.warehouse_resource("/sql/protocolv1/o/1/0123-cluster")


def test_grants_cover_every_system_schema_for_the_principal():
    lines = dep.grant_sql("abc-id")
    assert lines[0] == "GRANT USE CATALOG ON CATALOG system TO `abc-id`;"
    assert "GRANT USE SCHEMA, SELECT ON SCHEMA system.billing TO `abc-id`;" in lines
    assert len(lines) == 1 + len(dep.SYSTEM_SCHEMAS)


def test_the_rls_block_merges_key_by_key_and_reaches_the_staged_config(tmp_path):
    base = _settings(tmp_path, "{name: my-audit, rls: {enabled: false, columns: [workspace]}}")
    local = _settings(tmp_path, "{rls: {enabled: true}}", "app.local.yml")
    got = dep.read_settings(base, local)
    assert got["rls"] == {"enabled": True, "columns": ["workspace"]}
    out = tmp_path / "out"
    dep.stage(out, ENV, _wheel(tmp_path), "user", got)
    assert yaml.safe_load((out / "config" / "app.yml").read_text())["rls"]["enabled"] is True
    assert any("audit_entitlements" in line for line in dep.grant_sql("sp", "main.governance.audit_entitlements"))


def test_a_bad_rls_block_stops_the_deploy(tmp_path):
    with pytest.raises(dep.DeployError, match="apply"):
        _read(tmp_path, "{name: my-audit, rls: {apply: live}}")
