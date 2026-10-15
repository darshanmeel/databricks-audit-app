#!/usr/bin/env python3
"""tests/fixtures/build_fixtures.py -- the driver script for the whole fixture framework
(PLAN.md 5.7, T-07).

    python tests/fixtures/build_fixtures.py

Discovers every builder module under tests/fixtures/ (DEC-17: any tests/fixtures/<name>.py that
defines a module-level build(con), excluding base.py, ddl.py and this file itself, imported in
sorted name order -- there is no registration file, so a builder task never edits a shared list).

Each builder runs against its OWN fresh, isolated in-memory DuckDB connection, seeded with all 47
sources from tests/fixtures/ddl.py's DDL (the authoritative schema for every source -- this
script, not any builder module, creates the 47 tables, once per builder). For every one of the 47
sources a builder's connection ends up with rows in, writes one
tests/fixtures/parquet/<schema>__<table>/<builder_name>.parquet; for every source no builder
touched, writes one empty, correctly-typed parquet from ddl.ARROW_SCHEMA instead, so
`dbt build --target test` never fails on a missing source. Prints
"fixtures written: 47 sources (<n> builders, <47 - touched> empty)".

Per-builder isolation (2026-09-22, Opus review fix -- see tasks/T-07-fixture-framework-drilldown.md
Hand-off notes "Fix 1"): an earlier version of this script ran every builder against ONE shared
connection, and detected which of the 47 sources a builder "touched" by comparing row counts
before/after that builder's own build(con) call. That broke as soon as a second builder existed
and shared a source table with an earlier one (T-09's billing.py and this batch's drilldown.py
both write access__workspaces_latest rows, per PLAN.md 7.2 / DEC-15): a later builder's own
CREATE OR REPLACE (drilldown.py's _widen_tables) could silently wipe an earlier builder's
already-written rows before build_fixtures.py ever wrote them to parquet (the before/after row
count check would then see "fewer rows than before", so no parquet is ever written for that
source, and that earlier builder's rows are permanently lost); and write_parquet's own COPY always
serializes the WHOLE current table, so a builder running AFTER another one on the SAME shared
connection would have the earlier builder's rows baked into its own parquet file too (double-
counted once dbt-duckdb unions every builder's parquet for that source by name at read time). A
fresh connection per builder (all 47 tables recreated from ddl.py each time) makes both failure
modes structurally impossible: no builder's connection ever contains another builder's rows, so
neither write_parquet's COPY nor a builder's own CREATE OR REPLACE can ever touch data that is not
its own. `_self_check_isolation()` (run on every invocation, below) proves this holds.

Stdlib + duckdb + pyarrow only.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent
PARQUET_DIR = FIXTURES_DIR / "parquet"

# Modules under tests/fixtures/ that are framework plumbing, never a builder themselves.
_NOT_BUILDERS = {"base", "ddl", "build_fixtures"}


def _load(path: Path, name: str):
    """Load a module by file path and register it in sys.modules under `name` BEFORE executing
    it, so a later-loaded module's `from base import ...` / `from ddl import ...` (bare names,
    matching the convention every module under tests/fixtures/ uses) resolves against the
    already-loaded module instead of trying to re-import a package that does not exist -- the
    same pattern tests/test_ddl.py and tests/test_translate.py use to load a module by path."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _discover_builders() -> list[tuple[str, Path]]:
    names = sorted(
        p.stem
        for p in FIXTURES_DIR.glob("*.py")
        if p.stem not in _NOT_BUILDERS and not p.stem.startswith("_")
    )
    return [(name, FIXTURES_DIR / f"{name}.py") for name in names]


def _fresh_connection(ddl_dict: dict[str, str], keys: list[str]) -> duckdb.DuckDBPyConnection:
    """A brand-new in-memory DuckDB with all 47 sources created from ddl.py's DDL, and nothing
    else -- no other builder has ever touched it."""
    con = duckdb.connect()
    for key in keys:
        con.execute(ddl_dict[key])
    return con


def _row_counts(con: duckdb.DuckDBPyConnection, keys: list[str]) -> dict[str, int]:
    return {key: con.execute(f'SELECT count(*) FROM "{key}"').fetchone()[0] for key in keys}


def _write_empty_parquet(schema: str, table: str, arrow_schema: pa.Schema) -> Path:
    out_dir = PARQUET_DIR / f"{schema}__{table}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "empty.parquet"
    empty_table = pa.Table.from_pylist([], schema=arrow_schema)
    pq.write_table(empty_table, out_path)
    return out_path


def _self_check_isolation(ddl, base, keys: list[str]) -> None:
    """Regression guard for the per-builder isolation fix above. A stub builder that writes rows
    to a source drilldown.py ALSO fills must produce its OWN parquet with exactly the stub's own
    row count (not drilldown's rows baked in too), and must leave drilldown's own parquet for
    that same source unchanged (not wiped or altered by the stub having run). Both write_parquet
    calls below go through a temporary redirect of base.PARQUET_DIR, so this never writes
    anything under the real tests/fixtures/parquet/. Runs on every invocation of this script (a
    handful of in-memory connections and two throwaway parquet files) so a future change to the
    isolation mechanism cannot silently regress without this script's own run failing loudly."""
    drilldown_mod = sys.modules.get("drilldown")
    if drilldown_mod is None or not hasattr(drilldown_mod, "build"):
        return  # drilldown.py not discovered in this run -- should not happen in this repo

    target_key = "access__workspaces_latest"  # a source drilldown.py also fills (DEC-15)
    schema, table = target_key.split("__", 1)
    stub_src = (
        "def build(con):\n"
        "    con.execute(\"INSERT INTO access__workspaces_latest "
        "(workspace_id, workspace_name, workspace_url) VALUES ('stub-0', 'stub', NULL)\")\n"
        "    con.execute(\"INSERT INTO access__workspaces_latest "
        "(workspace_id, workspace_name, workspace_url) VALUES ('stub-1', 'stub', NULL)\")\n"
        "    con.execute(\"INSERT INTO access__workspaces_latest "
        "(workspace_id, workspace_name, workspace_url) VALUES ('stub-2', 'stub', NULL)\")\n"
    )

    real_parquet_dir = base.PARQUET_DIR
    try:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            base.PARQUET_DIR = tmp

            con_d = _fresh_connection(ddl.DDL, keys)
            drilldown_mod.build(con_d)
            base.write_parquet(con_d, schema, table, "drilldown")
            con_d.close()

            stub_path = tmp / "_isolation_check_stub.py"
            stub_path.write_text(stub_src, encoding="utf-8")
            con_s = _fresh_connection(ddl.DDL, keys)
            stub_mod = _load(stub_path, "_isolation_check_stub")
            stub_mod.build(con_s)
            base.write_parquet(con_s, schema, table, "_isolation_check_stub")
            con_s.close()
            del sys.modules["_isolation_check_stub"]

            drilldown_parquet_rows = pq.read_table(tmp / target_key / "drilldown.parquet").num_rows
            stub_parquet_rows = pq.read_table(tmp / target_key / "_isolation_check_stub.parquet").num_rows
    finally:
        base.PARQUET_DIR = real_parquet_dir

    assert stub_parquet_rows == 3, (
        f"isolation self-check: stub builder's own parquet has {stub_parquet_rows} rows, expected 3 "
        "(a sibling builder's rows leaked in, or were lost)"
    )

    con_check = _fresh_connection(ddl.DDL, keys)
    drilldown_mod.build(con_check)
    expected_drilldown_rows = con_check.execute(f'SELECT count(*) FROM "{target_key}"').fetchone()[0]
    con_check.close()
    assert drilldown_parquet_rows == expected_drilldown_rows, (
        f"isolation self-check: drilldown's own {target_key} parquet has {drilldown_parquet_rows} "
        f"rows, expected {expected_drilldown_rows} -- a sibling builder must never change it"
    )


def main() -> int:
    ddl = _load(FIXTURES_DIR / "ddl.py", "ddl")
    base = _load(FIXTURES_DIR / "base.py", "base")

    keys = sorted(ddl.DDL)
    assert len(keys) == 49, f"expected 49 sources in tests/fixtures/ddl.py, found {len(keys)}"

    builders = _discover_builders()
    touched: set[str] = set()
    n_builders = 0
    for name, path in builders:
        mod = _load(path, name)
        if not hasattr(mod, "build"):
            continue
        n_builders += 1
        # Fresh, isolated connection per builder (see module docstring): no builder's build(con)
        # ever sees another builder's rows, and write_parquet's whole-table COPY can therefore
        # never copy anything but this builder's own rows.
        con = _fresh_connection(ddl.DDL, keys)
        mod.build(con)
        counts = _row_counts(con, keys)
        for key in keys:
            if counts[key] > 0:
                schema, table = key.split("__", 1)
                base.write_parquet(con, schema, table, name)
                touched.add(key)
        con.close()

    for key in keys:
        if key not in touched:
            schema, table = key.split("__", 1)
            _write_empty_parquet(schema, table, ddl.ARROW_SCHEMA[key])

    _self_check_isolation(ddl, base, keys)

    print(f"fixtures written: {len(keys)} sources ({n_builders} builder{'s' if n_builders != 1 else ''}, {len(keys) - len(touched)} empty)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
