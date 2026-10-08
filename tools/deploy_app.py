#!/usr/bin/env python3
"""tools/deploy_app.py -- deploys this app as a Databricks App.

    python tools/deploy_app.py

Reads `name` from the git-ignored config/app.local.yml, and the host, HTTP path and token from
.env or the environment, as the export does. Needs the Databricks CLI, no CLI login. The app
installs nothing from PyPI or a volume: the DuckDB wheel ships in the app folder in parts under
10 MB (the Apps file limit) that tools/app_start.py joins at start-up, and everything else it uses
is pre-installed in Databricks Apps. README.md, "Run as a Databricks App".
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import datasource  # noqa: E402
from tools import export_direct_results as edr  # noqa: E402

SETTINGS = ROOT / "config" / "app.local.yml"
COPY_DIRS = ("app", "config", "dbt", "tools")
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", "*.duckdb", "*.wal", "target", "logs",
                              ".user.yml", "app.local.yml")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,29}$")
# Databricks Apps run Python 3.11 on Linux x86_64.
WHEEL_PYTHON = "3.11"
WHEEL_PLATFORM = "manylinux_2_28_x86_64"
# Databricks Apps refuse any file over 10 MB.
PART_BYTES = 9 * 1024 * 1024


class DeployError(Exception):
    pass


def read_settings(path: Path = SETTINGS) -> dict:
    if not path.exists():
        raise DeployError("config/app.local.yml is missing; see README.md, \"Run as a Databricks App\"")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    name = str(raw.get("name") or "")
    if not NAME_RE.match(name):
        raise DeployError("name in config/app.local.yml: 2-30 lowercase letters, numbers or dashes")
    return {"name": name}


def duckdb_pin(requirements: Path = ROOT / "requirements.txt") -> str:
    for line in requirements.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("duckdb=="):
            return line.strip()
    raise DeployError("requirements.txt has no duckdb== line")


def fetch_wheel(folder: Path, pin: str) -> Path:
    """The DuckDB wheel for the app's Python, downloaded once into `folder`."""
    pattern = f"duckdb-{pin.split('==', 1)[1]}-cp311-*.whl"
    if not sorted(folder.glob(pattern)):
        folder.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-m", "pip", "download", pin, "--no-deps", "--only-binary=:all:",
               "--python-version", WHEEL_PYTHON, "--platform", WHEEL_PLATFORM, "-d", str(folder)]
        subprocess.run(cmd, check=False)
    found = sorted(folder.glob(pattern))
    if not found:
        raise DeployError(f"could not download {pin}; put its Linux x86_64 Python 3.11 wheel in {folder}")
    return found[-1]


def stage(folder: Path, env: dict, wheel: Path) -> None:
    """A fresh copy of what the app runs, with its own app.yaml and the wheel in parts."""
    folder.mkdir(parents=True, exist_ok=True)
    for p in folder.iterdir():
        if p.name == ".databricks":  # the CLI's sync state, so the next sync sends only changes
            continue
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
    for d in COPY_DIRS:
        shutil.copytree(ROOT / d, folder / d, ignore=SKIP)
    data = wheel.read_bytes()
    (folder / "wheels").mkdir()
    for i in range(0, len(data), PART_BYTES):
        (folder / "wheels" / f"{wheel.name}.part{i // PART_BYTES:02d}").write_bytes(data[i:i + PART_BYTES])
    app_yaml = {
        "command": ["python", "tools/app_start.py"],
        "env": [{"name": k, "value": env[k]} for k in (edr.ENV_HOST, edr.ENV_HTTP_PATH, edr.ENV_TOKEN)],
    }
    (folder / "app.yaml").write_text(yaml.safe_dump(app_yaml, sort_keys=False), encoding="utf-8", newline="\n")

def cli(args: list[str], env: dict, token: str) -> str:
    exe = shutil.which("databricks")
    if not exe:
        raise DeployError("the Databricks CLI is not installed: https://docs.databricks.com/dev-tools/cli/install.html")
    proc = subprocess.run([exe, *args], env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = edr._redact((proc.stderr or proc.stdout or "").strip(), token)
        raise DeployError(f"databricks {' '.join(args[:2])} failed: {detail}")
    return proc.stdout


def get_app(name: str, env: dict, token: str) -> dict | None:
    try:
        return json.loads(cli(["apps", "get", name, "-o", "json"], env, token))
    except DeployError as exc:
        if "not exist" in str(exc).lower() or "not found" in str(exc).lower():
            return None
        raise


def main() -> int:
    try:
        settings = read_settings()
        name = settings["name"]
        env = edr._preflight_env()
        token = env[edr.ENV_TOKEN]
        cli_env = {**os.environ, "DATABRICKS_HOST": f"https://{env[edr.ENV_HOST]}", "DATABRICKS_TOKEN": token}
        cli_env.pop("DATABRICKS_CONFIG_PROFILE", None)
        base = datasource.data_dir() / "app-deploy"

        wheel = fetch_wheel(base / "wheels", duckdb_pin())
        local = base / name
        stage(local, env, wheel)
        user = json.loads(cli(["current-user", "me", "-o", "json"], cli_env, token))["userName"]
        folder = f"/Workspace/Users/{user}/{name}"
        print(f"code -> {folder}", flush=True)
        cli(["sync", str(local), folder], cli_env, token)

        app = get_app(name, cli_env, token)
        if app is None:
            print(f"creating app {name} (a few minutes)", flush=True)
            cli(["apps", "create", "--json", json.dumps({"name": name})], cli_env, token)
        elif (app.get("compute_status") or {}).get("state") == "STOPPED":
            print(f"starting app {name}", flush=True)
            cli(["apps", "start", name], cli_env, token)
        print("deploying (a few minutes)", flush=True)
        cli(["apps", "deploy", name, "--source-code-path", folder], cli_env, token)
        url = (get_app(name, cli_env, token) or {}).get("url") or "the app's page in Compute > Apps"
        print(f"done: open {url}, pick the days and click Get")
        return 0
    except (DeployError, edr.ExportConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
