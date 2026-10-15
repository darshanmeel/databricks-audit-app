#!/usr/bin/env python3
"""tools/build_sources.py

Turns the vendored `app/queries/vendored/lineage/sources.yml` (10 `source:` blocks, 47 tables --
the exact list T-02 vendors, reproduced in tasks/T-03's Inputs "The 47-source contract") plus
APP_OWNED_TABLES (app-owned tables that back an app-owned query, never a vendored one) into
`dbt/models/sources/sources.yml`, the dbt source definitions every generated finding model
(T-06 onward) and every fixture builder (T-07 onward) reads through `{{ source(...) }}`.

Modes:
    python tools/build_sources.py            write dbt/models/sources/sources.yml
    python tools/build_sources.py --plan      also write config/snapshot_plan.yml
    python tools/build_sources.py --check     compare a fresh render against what is on disk;
                                               exit 1 and name what differs on drift

`--check` is not itself named by PLAN.md for this script (see tasks/T-05-dbt-skeleton.md
Hand-off notes, DECIDE item) -- added for parity with sync_queries.py / generate_models.py, since
`dbt/models/sources/sources.yml` is a generated, never-hand-edited artifact like their outputs.

Each table's `meta.external_location` is written as:
    read_parquet('{{ env_var('AUDIT_SNAPSHOT_DIR', <target-conditional default>) }}
    /{schema}__{name}/*.parquet', union_by_name=true)
with the `{schema}`/`{name}` placeholders left LITERAL for dbt-duckdb's own external_location
substitution to fill in per table (PLAN.md 5.5's exact form; see this task's DECIDE note on the
5.5-vs-7.3 wording difference for why 5.5, not 7.3, is taken as authoritative). The env_var(...)
default is target-conditional (`'tests/fixtures/parquet' if target.name == 'test' else
'../snapshot'`), not the single fixed `'../snapshot'` PLAN.md 5.5 shows literally -- this is a
deliberate, documented departure: tools/gate.py's full-mode `dbt_build_test` step now runs
`python tools/dbt_run.py build --target test` (GATEFIX, DEC-12), which sets AUDIT_SNAPSHOT_DIR
itself, but the target-conditional default is still required because raw `dbt parse`/`dbt
compile`/`dbt show --target test` probes elsewhere (tests/test_dbt_skeleton.py,
tests/test_envmap.py) run with no AUDIT_SNAPSHOT_DIR set at all, so the `test` target must still
resolve to the fixture parquet directory on its own. See Hand-off notes for the full record.

Also writes (in `--plan` mode) `config/snapshot_plan.yml`: one entry per table,
`{time_column: <col>|null, retention_days: <cap>|null, has_workspace_id: bool}`, seeded from the
per-table predicate map in PLAN.md 5.2 / tasks/T-04-snapshot-exporter.md's Inputs (copied
verbatim, not re-derived) plus `has_workspace_id` / `has_custom_tags` derived mechanically from
`tests/fixtures/ddl.py`'s DDL text (whether `"workspace_id"` / `"custom_tags"` appears as a column
in that table's CREATE TABLE statement) -- same "derive mechanically, do not hand-transcribe"
spirit as DEC-07/DEC-08.

Stdlib + pyyaml only.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
LINEAGE_SOURCES = ROOT / "app" / "queries" / "vendored" / "lineage" / "sources.yml"
OUT_SOURCES = ROOT / "dbt" / "models" / "sources" / "sources.yml"
OUT_PLAN = ROOT / "config" / "snapshot_plan.yml"

# The literal external_location template. {schema}/{name} stay as literal placeholders (dbt-duckdb
# substitutes them per table); the env_var(...) default is target-conditional -- see module
# docstring and this task's Hand-off notes.
EXTERNAL_LOCATION_TEMPLATE = (
    "read_parquet('{{ env_var('AUDIT_SNAPSHOT_DIR', "
    "'tests/fixtures/parquet' if target.name == 'test' else '../snapshot') "
    "| replace(\"'\", \"''\") }}"
    "/{schema}__{name}/*.parquet', union_by_name=true)"
)

# Per-table predicate map, copied verbatim from PLAN.md 5.2 / tasks/T-04-snapshot-exporter.md's
# Inputs -- do not diverge from it. Keyed "(schema, table)"; value = (time_column, retention cap
# in days), both None for "no time predicate" tables (SCD2 full-history and reference /
# point-in-time tables alike -- PLAN's table gives both groups no predicate column).
PREDICATE_MAP: dict[tuple[str, str], tuple[str | None, int | None]] = {
    ("billing", "usage"): ("usage_date", 365),
    ("billing", "attributed_usage"): ("usage_date", 365),
    ("query", "history"): ("start_time", 365),
    ("lakeflow", "job_run_timeline"): ("period_start_time", 365),
    ("lakeflow", "job_task_run_timeline"): ("period_start_time", 365),
    ("lakeflow", "pipeline_update_timeline"): ("period_start_time", 365),
    ("compute", "node_timeline"): ("start_time", 90),
    ("compute", "warehouse_events"): ("event_time", None),
    ("compute", "instance_events"): ("event_time", None),
    ("access", "audit"): ("event_date", 365),
    ("access", "column_lineage"): ("event_date", 365),
    ("access", "table_lineage"): ("event_date", 365),
    ("access", "inbound_network"): ("event_time", 30),
    ("access", "outbound_network"): ("event_time", 30),
    ("ai_gateway", "usage"): ("event_time", None),
    ("serving", "endpoint_usage"): ("request_time", 90),
    ("storage", "predictive_optimization_operations_history"): ("start_time", 180),
    # App-owned (not vendored): retention is undocumented for this table, so no cap is applied.
    ("storage", "table_metrics_history"): ("snapshot_date", None),
    # SCD2 full-history: no time predicate, a windowed export would lose unchanged entities.
    ("compute", "clusters"): (None, None),
    ("compute", "instance_pools"): (None, None),
    ("compute", "warehouses"): (None, None),
    ("lakeflow", "jobs"): (None, None),
    ("lakeflow", "job_tasks"): (None, None),
    ("lakeflow", "pipelines"): (None, None),
    ("serving", "served_entities"): (None, None),
    # reference / point-in-time tables: no time predicate, full export.
    ("billing", "list_prices"): (None, None),
    ("compute", "node_types"): (None, None),
    ("access", "workspaces_latest"): (None, None),
    ("data_classification", "results"): (None, None),
    # information_schema.* (19 tables): reference / point-in-time, no time predicate. Not
    # enumerated individually -- see predicate_for()'s fallback for this one schema.
}


def predicate_for(schema: str, table: str) -> tuple[str | None, int | None]:
    if schema == "information_schema":
        return (None, None)
    try:
        return PREDICATE_MAP[(schema, table)]
    except KeyError:
        raise SystemExit(
            f"build_sources.py: no predicate-map entry for {schema}.{table} -- PLAN.md 5.2 / "
            "T-04's Inputs must be extended, this script never guesses."
        )


def load_ddl_flags() -> dict[str, str]:
    """Returns {'<schema>__<table>': ddl_text} for has_workspace_id / has_custom_tags checks.
    Imported lazily (only needed for --plan and for populating sources.yml meta), from the file
    tests/fixtures/ddl.py that tools/build_fixture_ddl.py (T-03) generates and this repo commits.
    """
    sys.path.insert(0, str(ROOT))
    from tests.fixtures.ddl import DDL  # noqa: PLC0415

    return DDL


def load_lineage() -> list[dict]:
    if not LINEAGE_SOURCES.exists():
        raise SystemExit(f"build_sources.py: {LINEAGE_SOURCES} not found (T-02 vendors it)")
    data = yaml.safe_load(LINEAGE_SOURCES.read_text(encoding="utf-8"))
    sources = data.get("sources", [])
    if not sources:
        raise SystemExit("build_sources.py: lineage/sources.yml has no sources")
    return sources


# App-owned additions: tables that back an app-owned query (never a vendored one), so they are
# injected here rather than into the vendored library's own lineage/sources.yml. Keyed by the
# vendored source block's `name` field.
APP_OWNED_TABLES: dict[str, list[str]] = {
    "system_storage": ["table_metrics_history"],
}


def apply_app_owned(lineage_sources: list[dict]) -> list[dict]:
    """A copy of `lineage_sources` with APP_OWNED_TABLES merged into their matching source
    blocks' `tables` list (never mutating the vendored data in place)."""
    out = []
    for src in lineage_sources:
        src = dict(src)
        extra = APP_OWNED_TABLES.get(src["name"], [])
        if extra:
            existing = {t["name"] for t in src.get("tables", [])}
            src["tables"] = list(src.get("tables", [])) + [
                {"name": t} for t in extra if t not in existing
            ]
        out.append(src)
    return out


def _yaml_dq(value: str) -> str:
    """Double-quoted YAML scalar (the template has no double-quote or backslash chars)."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _yaml_opt_str(value: str | None) -> str:
    return "null" if value is None else value


def _yaml_opt_int(value: int | None) -> str:
    return "null" if value is None else str(value)


def _yaml_bool(value: bool) -> str:
    return "true" if value else "false"


def build_table_meta(schema: str, table: str, ddl: dict[str, str]) -> dict:
    time_column, retention_days = predicate_for(schema, table)
    key = f"{schema}__{table}"
    ddl_text = ddl.get(key, "")
    return {
        "external_location": EXTERNAL_LOCATION_TEMPLATE,
        "time_column": time_column,
        "retention_days": retention_days,
        "has_workspace_id": '"workspace_id"' in ddl_text,
        "has_custom_tags": '"custom_tags"' in ddl_text,
    }


def render_sources_yml(lineage_sources: list[dict], ddl: dict[str, str]) -> tuple[str, int, int]:
    lines = [
        "# GENERATED by tools/build_sources.py from app/queries/vendored/lineage/sources.yml.",
        "# Do not hand-edit; run: python tools/build_sources.py",
        "version: 2",
        "",
        "sources:",
    ]
    n_sources = 0
    n_tables = 0
    for src in lineage_sources:
        name = src["name"]
        database = src["database"]
        schema = src["schema"]
        tables = sorted(t["name"] for t in src.get("tables", []))
        n_sources += 1
        lines.append(f"  - name: {name}")
        lines.append(f"    database: {database}")
        lines.append(f"    schema: {schema}")
        lines.append("    tables:")
        for table in tables:
            n_tables += 1
            meta = build_table_meta(schema, table, ddl)
            lines.append(f"      - name: {table}")
            lines.append("        meta:")
            lines.append(f"          external_location: {_yaml_dq(meta['external_location'])}")
            lines.append(f"          time_column: {_yaml_opt_str(meta['time_column'])}")
            lines.append(f"          retention_days: {_yaml_opt_int(meta['retention_days'])}")
            lines.append(f"          has_workspace_id: {_yaml_bool(meta['has_workspace_id'])}")
            lines.append(f"          has_custom_tags: {_yaml_bool(meta['has_custom_tags'])}")
    lines.append("")
    return "\n".join(lines), n_sources, n_tables


def render_snapshot_plan(lineage_sources: list[dict], ddl: dict[str, str]) -> tuple[str, int]:
    lines = [
        "# GENERATED by tools/build_sources.py --plan from the per-table predicate map",
        "# (PLAN.md 5.2 / tasks/T-04-snapshot-exporter.md Inputs), plus has_workspace_id derived",
        "# mechanically from tests/fixtures/ddl.py's DDL text. Hand-tunable afterwards --",
        "# tools/snapshot.py (DEC-10) only reads this to override per-table days/predicate",
        "# choices, it never hard-requires this file to exist.",
    ]
    entries: list[tuple[str, dict]] = []
    for src in lineage_sources:
        schema = src["schema"]
        for t in src.get("tables", []):
            table = t["name"]
            key = f"{schema}__{table}"
            time_column, retention_days = predicate_for(schema, table)
            ddl_text = ddl.get(key, "")
            entries.append(
                (
                    key,
                    {
                        "time_column": time_column,
                        "retention_days": retention_days,
                        "has_workspace_id": '"workspace_id"' in ddl_text,
                    },
                )
            )
    for key, plan in sorted(entries, key=lambda kv: kv[0]):
        lines.append(f"{key}:")
        lines.append(f"  time_column: {_yaml_opt_str(plan['time_column'])}")
        lines.append(f"  retention_days: {_yaml_opt_int(plan['retention_days'])}")
        lines.append(f"  has_workspace_id: {_yaml_bool(plan['has_workspace_id'])}")
    lines.append("")
    return "\n".join(lines), len(entries)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true", help="also write config/snapshot_plan.yml")
    ap.add_argument(
        "--check", action="store_true", help="compare against what is on disk, exit 1 on drift"
    )
    args = ap.parse_args(argv)

    lineage_sources = apply_app_owned(load_lineage())
    ddl = load_ddl_flags()
    content, n_sources, n_tables = render_sources_yml(lineage_sources, ddl)

    if args.check:
        if not OUT_SOURCES.exists():
            print(f"build_sources.py --check: {OUT_SOURCES} does not exist; run without --check")
            return 1
        existing = OUT_SOURCES.read_text(encoding="utf-8")
        if existing == content:
            print(f"sources.yml in sync ({n_sources} sources, {n_tables} tables)")
            return 0
        existing_lines = existing.splitlines()
        new_lines = content.splitlines()
        max_len = max(len(existing_lines), len(new_lines))
        diffs: list[tuple[int, str, str]] = []
        for i in range(max_len):
            old_line = existing_lines[i] if i < len(existing_lines) else "<missing>"
            new_line = new_lines[i] if i < len(new_lines) else "<missing>"
            if old_line != new_line:
                diffs.append((i + 1, old_line, new_line))
        diff_count = len(diffs)
        print(
            f"sources.yml is OUT OF SYNC ({diff_count} differing/extra lines) -- "
            "run: python tools/build_sources.py"
        )
        rel = OUT_SOURCES.relative_to(ROOT).as_posix()
        shown = diffs[:5]
        for line_no, old_line, new_line in shown:
            print(f"  {rel}:{line_no}: on disk:   {old_line!r}")
            print(f"  {rel}:{line_no}: generated: {new_line!r}")
        if diff_count > len(shown):
            print(f"  ... {diff_count - len(shown)} more differing lines")
        return 1

    OUT_SOURCES.parent.mkdir(parents=True, exist_ok=True)
    with OUT_SOURCES.open("w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    print(f"wrote dbt/models/sources/sources.yml ({n_sources} sources, {n_tables} tables)")

    if args.plan:
        plan_content, n_plan = render_snapshot_plan(lineage_sources, ddl)
        OUT_PLAN.parent.mkdir(parents=True, exist_ok=True)
        with OUT_PLAN.open("w", encoding="utf-8", newline="\n") as f:
            f.write(plan_content)
        print(f"wrote snapshot_plan.yml ({n_plan} tables)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
