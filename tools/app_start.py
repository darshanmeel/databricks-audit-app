#!/usr/bin/env python3
"""tools/app_start.py -- the Databricks App's start command (tools/deploy_app.py writes it into app.yaml).

App files are at most 10 MB and the app installs nothing from PyPI or a volume, so the DuckDB
wheel ships in parts under wheels/. This joins them, unpacks the wheel next to the code and starts
the app; children such as the export inherit the path. README.md, "Run as a Databricks App".
"""
from __future__ import annotations

import os
import runpy
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PARTS = ROOT / "wheels"
TARGET = ROOT / ".pydeps"


def unpack(parts: Path = PARTS, target: Path = TARGET) -> None:
    chunks = sorted(parts.glob("*.whl.part*"))
    if not chunks or (target / "duckdb").is_dir():
        return
    wheel = parts / chunks[0].name.split(".part", 1)[0]
    with wheel.open("wb") as out:
        for c in chunks:
            out.write(c.read_bytes())
    with zipfile.ZipFile(wheel) as z:
        z.extractall(target)
    wheel.unlink()


def main() -> None:
    unpack()
    paths = [str(TARGET), str(ROOT)]
    sys.path[:0] = paths
    os.environ["PYTHONPATH"] = os.pathsep.join(paths + [p for p in [os.environ.get("PYTHONPATH")] if p])
    runpy.run_module("app.api", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
