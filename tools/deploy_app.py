#!/usr/bin/env python3
"""tools/deploy_app.py -- deploys this app as a Databricks App.

    python tools/deploy_app.py

Reads `name` and `run_as` from config/app.yml (config/app.local.yml, git-ignored, wins), and the
host, HTTP path and token from .env or the environment, as the export does. Needs the Databricks CLI, no CLI login. The
app installs nothing from PyPI or a volume: the DuckDB wheel ships in the app folder in parts under
10 MB (the Apps file limit) that tools/app_start.py joins at start-up, and everything else it uses
is pre-installed in Databricks Apps. docs/databricks-app.md.
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

SETTINGS = ROOT / "config" / "app.yml"
LOCAL_SETTINGS = ROOT / "config" / "app.local.yml"
COPY_DIRS = ("app", "config", "dbt", "tools")
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", "*.duckdb", "*.wal", "target", "target_*", "logs",
                              ".user.yml", "app.local.yml")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,29}$")
# Who the app queries as: the token in .env (a user's or a service principal's), or the app's own
# service principal, which stores no token.
RUN_AS = ("user", "app")
WAREHOUSE_RE = re.compile(r"/warehouses/([0-9A-Za-z]+)/?$")
# The system schemas the checks read; information_schema is readable by everyone.
SYSTEM_SCHEMAS = ("access", "billing", "compute", "lakeflow", "query", "serving", "storage",
                  "data_classification", "ai_gateway")
# Databricks Apps run Python 3.11 on Linux x86_64.
WHEEL_PYTHON = "3.11"
WHEEL_PLATFORM = "manylinux_2_28_x86_64"
# Databricks Apps refuse any file over 10 MB.
PART_BYTES = 9 * 1024 * 1024


class DeployError(Exception):
    pass


def read_settings(path: Path = SETTINGS, local: Path = LOCAL_SETTINGS) -> dict:
    if not path.exists():
        raise DeployError("config/app.yml is missing; see docs/databricks-app.md")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if local.exists():
        raw.update(yaml.safe_load(local.read_text(encoding="utf-8")) or {})
    name = str(raw.get("name") or "")
    if not NAME_RE.match(name):
        raise DeployError("name in config/app.yml: 2-30 lowercase letters, numbers or dashes")
    run_as = str(raw.get("run_as") or "user")
    if run_as not in RUN_AS:
        raise DeployError("run_as in config/app.yml: user (the token in .env) or app (the app's own service principal)")
    return {"name": name, "run_as": run_as}


def warehouse_resource(http_path: str) -> dict:
    """The SQL warehouse as an app resource, which gives the app's service principal CAN USE on it."""
    m = WAREHOUSE_RE.search(http_path.strip())
    if not m:
        raise DeployError("run_as: app needs a SQL warehouse HTTP path (/sql/1.0/warehouses/<id>)")
    return {"name": "sql-warehouse", "sql_warehouse": {"id": m.group(1), "permission": "CAN_USE"}}


def has_warehouse(app: dict, resource: dict) -> bool:
    wanted = resource["sql_warehouse"]["id"]
    return any((r.get("sql_warehouse") or {}).get("id") == wanted for r in app.get("resources") or [])


def grant_sql(principal: str) -> list[str]:
    """What a metastore admin runs so the app's service principal can read the system tables."""
    return [f"GRANT USE CATALOG ON CATALOG system TO `{principal}`;"] + [
        f"GRANT USE SCHEMA, SELECT ON SCHEMA system.{s} TO `{principal}`;" for s in SYSTEM_SCHEMAS
    ]


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


def stage(folder: Path, env: dict, wheel: Path, run_as: str = "user") -> None:
    """A fresh copy of what the app runs, with its own app.yaml and the wheel in parts. Only
    run_as user puts the token in app.yaml."""
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
        "env": [{"name": k, "value": env[k]} for k in (edr.ENV_HOST, edr.ENV_HTTP_PATH)
                + ((edr.ENV_TOKEN,) if run_as == "user" else ())],
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
        name, run_as = settings["name"], settings["run_as"]
        env = edr._preflight_env()
        token = env.get(edr.ENV_TOKEN)
        if not token:
            raise DeployError("deploying needs DATABRICKS_TOKEN in .env or the environment")
        resources = [warehouse_resource(env[edr.ENV_HTTP_PATH])] if run_as == "app" else []
        cli_env = {**os.environ, "DATABRICKS_HOST": f"https://{env[edr.ENV_HOST]}", "DATABRICKS_TOKEN": token}
        cli_env.pop("DATABRICKS_CONFIG_PROFILE", None)
        base = datasource.data_dir() / "app-deploy"

        wheel = fetch_wheel(base / "wheels", duckdb_pin())
        local = base / name
        stage(local, env, wheel, run_as)
        user = json.loads(cli(["current-user", "me", "-o", "json"], cli_env, token))["userName"]
        folder = f"/Workspace/Users/{user}/{name}"
        print(f"code -> {folder}", flush=True)
        cli(["sync", str(local), folder], cli_env, token)

        app = get_app(name, cli_env, token)
        if app is None:
            print(f"creating app {name} (a few minutes)", flush=True)
            cli(["apps", "create", "--json", json.dumps({"name": name, **({"resources": resources} if resources else {})})],
                cli_env, token)
        elif resources and not has_warehouse(app, resources[0]):
            print(f"giving app {name} the warehouse", flush=True)
            cli(["apps", "update", name, "--json", json.dumps({"name": name, "resources": resources})], cli_env, token)
        if app is not None and (app.get("compute_status") or {}).get("state") == "STOPPED":
            print(f"starting app {name}", flush=True)
            cli(["apps", "start", name], cli_env, token)
        print("deploying (a few minutes)", flush=True)
        cli(["apps", "deploy", name, "--source-code-path", folder], cli_env, token)
        app = get_app(name, cli_env, token) or {}
        if run_as == "app":
            principal = app.get("service_principal_client_id") or "<the app's service principal>"
            print(f"the app queries as its service principal {app.get('service_principal_name') or principal}."
                  " Once, a metastore admin runs:")
            for line in grant_sql(principal):
                print(f"  {line}")
        url = app.get("url") or "the app's page in Compute > Apps"
        print(f"done: open {url}, pick the days and click Get")
        return 0
    except (DeployError, edr.ExportConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
