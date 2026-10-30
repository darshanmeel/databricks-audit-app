#!/usr/bin/env python3
"""Render the Databricks-direct models into plain SQL under app/direct_sql/, so the export needs no dbt.

Thresholds from config/thresholds.yml are baked in; `--check` exits 1 when a file is stale, missing or extra.
`mask_user()` renders as the plain column here -- the direct export masks locally instead
(tools/export_direct_results.py --mask-users).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.dbt_run import load_thresholds  # noqa: E402

DIRECT_MANIFEST = ROOT / "dbt" / "models" / "databricks_direct" / "generated_manifest.json"
SOURCES_YML = ROOT / "dbt" / "models" / "sources" / "sources.yml"
OUT_DIR = ROOT / "app" / "direct_sql"
THRESHOLDS_FILE = "thresholds.json"
WINDOW_MARKER = "__WINDOW_DAYS__"
QUERY_SOURCE_MARKER = "__QUERY_SOURCE__"
AS_OF_DATE_MARKER = "__AS_OF_DATE__"
AS_OF_TS_MARKER = "__AS_OF_TS__"
LIST_PRICES_MACRO = (ROOT / "dbt" / "macros" / "list_prices.sql").read_text(encoding="utf-8")


class BuildError(Exception):
    pass


def load_sources() -> dict[tuple[str, str], str]:
    doc = yaml.safe_load(SOURCES_YML.read_text(encoding="utf-8")) or {}
    out = {}
    for src in doc.get("sources", []):
        for table in src.get("tables", []):
            parts = [src.get("database", ""), src.get("schema", src["name"]), table.get("identifier", table["name"])]
            out[(src["name"], table["name"])] = ".".join(f"`{p}`" for p in parts)
    return out


def render_param(value) -> str:
    """The same text dbt/macros/param.sql writes."""
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if value is True:
        return "TRUE"
    if value is False:
        return "FALSE"
    return str(value)


def render_model(text: str, qid: str, thresholds: dict, sources: dict) -> str:
    def param(q, name, default):
        for scope in (q, "_all"):
            overrides = thresholds.get(scope) if isinstance(thresholds, dict) else None
            if isinstance(overrides, dict) and name in overrides:
                return render_param(overrides[name])
        return render_param(default)

    def var(name, default=None):
        if name == "windows":
            return [WINDOW_MARKER]
        if name == "direct_mode":
            return "run"
        if name == "query_source":
            return QUERY_SOURCE_MARKER
        raise BuildError(f"{qid}: var('{name}') has no plain-SQL value")

    def source(src, table):
        if (src, table) not in sources:
            raise BuildError(f"{qid}: unknown source('{src}', '{table}')")
        return sources[(src, table)]

    def ref(*_args):
        raise BuildError(f"{qid}: ref() is not allowed; direct models read source() only")

    def mask_user(expr, real_id=None):  # noqa: ARG001 -- the direct export masks locally instead
        return expr

    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    env.globals.update(
        config=lambda *a, **k: "", var=var, param=param, source=source, ref=ref,
        target={"type": "databricks", "name": "databricks"},
        audit_today=lambda: AS_OF_DATE_MARKER, audit_now=lambda: AS_OF_TS_MARKER,
        mask_user=mask_user,
    )
    # dbt/macros/list_prices.sql itself, rendered by the same env so its own {{ source(...) }}
    # call resolves through the `source` global just set above -- direct SQL gets dbt's own text.
    env.globals["list_prices"] = env.from_string(LIST_PRICES_MACRO).module.list_prices
    template = env.from_string(text)
    try:
        sql = template.render()
    except jinja2.UndefinedError as exc:
        raise BuildError(f"{qid}: {exc}") from exc
    if "{{" in sql or "{%" in sql:
        raise BuildError(f"{qid}: Jinja left after rendering")
    return sql.strip() + "\n"


def expected_files() -> dict[str, str]:
    manifest = json.loads(DIRECT_MANIFEST.read_text(encoding="utf-8"))
    thresholds = load_thresholds() or {}
    sources = load_sources()
    files = {}
    for model in sorted(manifest.get("models", []), key=lambda m: m["query_id"]):
        qid = model["query_id"]
        text = (ROOT / model["path"]).read_text(encoding="utf-8")
        header = f"-- generated from {model['path']} by tools/build_direct_sql.py; edit the query, never this file.\n"
        files[f"{qid}.sql"] = header + render_model(text, qid, thresholds, sources)
    files[THRESHOLDS_FILE] = json.dumps(thresholds, indent=2, sort_keys=True) + "\n"
    return files


def check() -> list[str]:
    files = expected_files()
    problems = []
    for name, content in files.items():
        path = OUT_DIR / name
        if not path.exists():
            problems.append(f"missing: {name}")
        elif path.read_bytes() != content.encode("utf-8"):
            problems.append(f"stale: {name}")
    if OUT_DIR.exists():
        for path in sorted(OUT_DIR.glob("*.sql")):
            if path.name not in files:
                problems.append(f"extra: {path.name}")
    return problems


def write() -> int:
    files = expected_files()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in OUT_DIR.glob("*.sql"):
        if path.name not in files:
            path.unlink()
    for name, content in files.items():
        (OUT_DIR / name).write_bytes(content.encode("utf-8"))
    return len(files) - 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="exit 1 when app/direct_sql is out of date")
    args = ap.parse_args(argv)
    try:
        if args.check:
            problems = check()
            for p in problems:
                print(p)
            if problems:
                print("run: python tools/build_direct_sql.py")
                return 1
            print(f"direct SQL in sync ({len(list(OUT_DIR.glob('*.sql')))} checks)")
            return 0
        print(f"wrote {write()} checks to {OUT_DIR.relative_to(ROOT).as_posix()}")
        return 0
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
