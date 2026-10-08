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


def _settings(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "app.local.yml"
    p.write_text(text, encoding="utf-8")
    return p


def _wheel(tmp_path: Path) -> Path:
    wheel = tmp_path / "duckdb-1.5.1-cp311-cp311-manylinux_2_28_x86_64.whl"
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr("duckdb/__init__.py", "x = 1\n")
        z.writestr("filler.bin", bytes(range(256)) * 9000)  # stored, so the wheel spans three parts
    return wheel


def test_read_settings_takes_the_name(tmp_path):
    assert dep.read_settings(_settings(tmp_path, "name: my-audit\n")) == {"name": "my-audit"}


@pytest.mark.parametrize("text", ["name: My_Audit\n", "volume: /Volumes/a/b/c\n"])
def test_read_settings_refuses_a_bad_name(tmp_path, text):
    with pytest.raises(dep.DeployError, match="name"):
        dep.read_settings(_settings(tmp_path, text))


def test_read_settings_names_the_missing_file(tmp_path):
    with pytest.raises(dep.DeployError, match="app.local.yml is missing"):
        dep.read_settings(tmp_path / "nope.yml")


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
