"""Where the app's data lives when nobody sets AUDIT_DB or --out/--db by hand: one folder outside
the repo, so a refresh never risks committing real-account data by accident.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DB_NAME = "audit.duckdb"


def default_data_dir(platform: str = sys.platform, environ: dict | None = None,
                      home: Path | None = None) -> Path:
    """The OS default data folder: LOCALAPPDATA on Windows (not Roaming, so OneDrive skips it),
    Application Support on macOS, XDG_DATA_HOME (or ~/.local/share) elsewhere."""
    environ = environ if environ is not None else os.environ
    home = home if home is not None else Path.home()
    if platform == "win32":
        base = environ.get("LOCALAPPDATA")
        return (Path(base) if base else home / "AppData" / "Local") / "databricks-audit"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "databricks-audit"
    xdg = environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else home / ".local" / "share") / "databricks-audit"


def data_dir() -> Path:
    env = os.environ.get("AUDIT_DATA_DIR")
    return Path(env) if env else default_data_dir()


def inside_repo(path: Path) -> bool:
    resolved = path.resolve()
    return resolved == ROOT or ROOT in resolved.parents


def results_dir() -> Path:
    return data_dir() / "results"


def db_path() -> Path:
    return data_dir() / DB_NAME


def sidecar(db: Path, name: str) -> Path:
    return db.with_name(f"{db.stem}.{name}.json")


def _current_db_pointer() -> Path:
    return data_dir() / "current_db.txt"


def set_current_db(path: Path) -> None:
    """Record `path` as the db a refresh just built, so the next app start (not just this
    process) opens it -- an atomic write/replace so a reader never sees a half-written pointer."""
    pointer = _current_db_pointer()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    tmp = pointer.with_name(pointer.name + ".tmp")
    tmp.write_text(str(Path(path).resolve()), encoding="utf-8")
    os.replace(tmp, pointer)


def current_db() -> Path | None:
    """The db set_current_db() last pointed at, or None when there is no pointer or its target
    is gone (a stale pointer must never outrank an actually-existing default)."""
    try:
        raw = _current_db_pointer().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    target = Path(raw)
    return target if target.exists() else None
