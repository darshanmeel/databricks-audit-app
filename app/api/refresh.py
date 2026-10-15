"""Refresh from Databricks: run the export, load it into a new db file, then point the app at it.

A new file each time because the running server holds the current one open; the old file stays
until the next refresh removes it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from app.core import datasource

ROOT = Path(__file__).resolve().parent.parent.parent
_lock = threading.Lock()
_state: dict = {"running": False, "step": None, "days": None, "started_at": None,
                "finished_at": None, "error": None}


def status() -> dict:
    s = dict(_state)
    s["elapsed_s"] = round(time.time() - s["started_at"]) if s["running"] and s["started_at"] else None
    return s


def start(days: int) -> dict:
    with _lock:
        if _state["running"]:
            return status()
        _state.update(running=True, step="export", days=days, started_at=time.time(),
                      finished_at=None, error=None)
    threading.Thread(target=_run, args=(days,), daemon=True).start()
    return status()


def _call(args: list[str]) -> None:
    proc = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        raise RuntimeError(" / ".join(tail) or f"exit code {proc.returncode}")


def _run(days: int) -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = datasource.data_dir()
    out = base / "results" / stamp
    db = base / f"audit-{stamp}.duckdb"
    try:
        _call(["tools/export_direct_results.py", "--windows", str(days), "--max-rows", "500000", "--out", str(out)])
        _state["step"] = "load"
        _call(["tools/load_direct_results.py", str(out), "--db", str(db)])
        previous = os.environ.get("AUDIT_DB")
        datasource.set_current_db(db)
        os.environ["AUDIT_DB"] = str(db)
        _remove_old_dbs(base, keep={str(db), previous or ""})
        _remove_old_results(base, keep={out})
        _state.update(running=False, step="done", finished_at=time.time())
    except Exception as exc:  # noqa: BLE001 -- any failure is shown on screen as-is
        _state.update(running=False, step="failed", error=str(exc), finished_at=time.time())


def _remove_old_dbs(base: Path, keep: set[str]) -> None:
    # Files from refreshes before the previous one; the previous is still open by the server.
    for f in base.glob("audit-*.duckdb"):
        if str(f) not in keep:
            try:
                f.unlink()
            except OSError:
                pass


def _remove_old_results(base: Path, keep: set[Path]) -> None:
    # Keep the newest two results/<stamp> folders (this one plus the previous refresh's).
    folders = sorted((p for p in (base / "results").glob("*") if p.is_dir()), reverse=True)
    for p in folders[2:]:
        if p not in keep:
            shutil.rmtree(p, ignore_errors=True)
