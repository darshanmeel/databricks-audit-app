"""Tests for tools/build_fixture_ddl.py and its generated tests/fixtures/ddl.py.

Loads both modules directly by file path (importlib.util.spec_from_file_location), the same
pattern tests/test_sync_queries.py (T-02) and tests/test_registry.py (T-03) use, so this stays
correct under a plain `pytest <file> -q` regardless of pytest's rootdir/sys.path behaviour.

tests/fixtures/ddl.py is itself a generated artifact (like app/queries/app/manifest.json) -- this
test suite requires it to already exist (built by `python tools/build_fixture_ddl.py`, the first
Definition-of-done command) and additionally re-derives the MISSING_FROM_DUMP set independently
of the generator's own flag, per the task's Step 4 instruction.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import duckdb
import pyarrow as pa

ROOT = Path(__file__).resolve().parent.parent
GEN_PATH = ROOT / "tools" / "build_fixture_ddl.py"
DDL_MODULE_PATH = ROOT / "tests" / "fixtures" / "ddl.py"
DUMP_PATH = ROOT / "app" / "queries" / "vendored" / "databricks_system_catalog_schema.txt"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Registered in sys.modules before exec (see tests/test_registry.py for why: a dataclass
    # pulled in transitively via app.core.registry needs this to resolve its annotations).
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


gen = _load(GEN_PATH, "build_fixture_ddl_under_test")
ddl_mod = _load(DDL_MODULE_PATH, "ddl_under_test")

# The 49-source contract, keyed "<schema>__<table>" (mirrors app.core.registry.SOURCE_CONTRACT,
# reproduced here via gen's own import of it so this test never duplicates the 49-entry literal).
ALL_KEYS = {
    f"{schema}__{table}"
    for schema, tables in gen.SOURCE_CONTRACT.items()
    for table in tables
}


def test_all_keys_present_in_ddl_and_arrow_schema():
    assert len(ALL_KEYS) == 49
    assert set(ddl_mod.DDL.keys()) == ALL_KEYS
    assert set(ddl_mod.ARROW_SCHEMA.keys()) == ALL_KEYS
    for key in ALL_KEYS:
        assert isinstance(ddl_mod.DDL[key], str) and ddl_mod.DDL[key].strip()
        assert isinstance(ddl_mod.ARROW_SCHEMA[key], pa.Schema)
        assert len(ddl_mod.ARROW_SCHEMA[key]) > 0


def test_missing_from_dump_flag_matches_an_independent_re_derivation():
    """Re-derive, independently of gen.parse_dump's own line-state-machine, which of the 47
    sources have NO detail block in the real dump: a plain whole-text scan for any line that
    ends with the qualified name after a non-trivial prefix (the dash-rule header)."""
    dump_text = DUMP_PATH.read_text(encoding="utf-8")
    dump_lines = dump_text.replace("\r\n", "\n").split("\n")

    independently_missing = set()
    for key in ALL_KEYS:
        schema, table = key.split("__", 1)
        qualified = f"system.{schema}.{table}"
        present = any(
            line.rstrip().endswith(qualified) and line.strip() != qualified
            for line in dump_lines
        )
        if not present:
            independently_missing.add(key)

    generator_missing = {k for k, v in ddl_mod.FLAGS.items() if v == "MISSING_FROM_DUMP"}

    assert independently_missing == generator_missing
    # DEC-07: derived mechanically -- assert the count comes out of the independent
    # re-derivation above, not a bare literal. The ABAC policy source is not in the dump either.
    assert len(independently_missing) == 9
    assert independently_missing == {
        "data_classification__results",
        "information_schema__abac_policy_definitions",
        "information_schema__column_masks",
        "information_schema__column_tags",
        "information_schema__connection_privileges",
        "information_schema__credential_privileges",
        "information_schema__external_location_privileges",
        "information_schema__row_filters",
        "information_schema__schema_privileges",
    }
    # Every non-flagged key really does have a detail block (the flip side of the same check).
    assert (ALL_KEYS - independently_missing).isdisjoint(generator_missing)


def test_missing_from_dump_sources_still_have_nonempty_ddl_and_schema():
    generator_missing = {k for k, v in ddl_mod.FLAGS.items() if v == "MISSING_FROM_DUMP"}
    assert generator_missing  # sanity: there is at least one
    for key in generator_missing:
        assert "CREATE TABLE" in ddl_mod.DDL[key]
        assert len(ddl_mod.ARROW_SCHEMA[key]) > 0


def test_decimal_38_18_maps_to_double():
    # system.billing.usage.usage_quantity is decimal(38,18) in the dump.
    ddl_t, pa_t = gen.spark_type_to_duckdb_and_arrow("decimal(38,18)")
    assert ddl_t == "DOUBLE"
    assert pa_t.equals(pa.float64())
    field = ddl_mod.ARROW_SCHEMA["billing__usage"].field("usage_quantity")
    assert field.type.equals(pa.float64())
    assert '"usage_quantity" DOUBLE' in ddl_mod.DDL["billing__usage"]


def test_map_string_string_maps_to_map_type():
    # system.billing.usage.custom_tags is map<string,string> in the dump.
    ddl_t, pa_t = gen.spark_type_to_duckdb_and_arrow("map<string,string>")
    assert ddl_t == "MAP(VARCHAR, VARCHAR)"
    assert pa_t.equals(pa.map_(pa.string(), pa.string()))
    field = ddl_mod.ARROW_SCHEMA["billing__usage"].field("custom_tags")
    assert pa.types.is_map(field.type)
    assert '"custom_tags" MAP(VARCHAR, VARCHAR)' in ddl_mod.DDL["billing__usage"]


def test_array_string_maps_to_duckdb_array_and_arrow_list():
    # system.compute.clusters.init_scripts is array<string> in the dump.
    ddl_t, pa_t = gen.spark_type_to_duckdb_and_arrow("array<string>")
    assert ddl_t == "VARCHAR[]"
    assert pa_t.equals(pa.list_(pa.string()))
    field = ddl_mod.ARROW_SCHEMA["compute__clusters"].field("init_scripts")
    assert pa.types.is_list(field.type)
    assert '"init_scripts" VARCHAR[]' in ddl_mod.DDL["compute__clusters"]


def test_struct_maps_to_duckdb_struct_and_arrow_struct():
    # system.billing.list_prices.pricing is
    # struct<default:decimal(38,18),promotional:struct<default:decimal(38,18)>,
    #        effective_list:struct<default:decimal(38,18)>> in the dump.
    ddl_t, pa_t = gen.spark_type_to_duckdb_and_arrow(
        "struct<default:decimal(38,18),promotional:struct<default:decimal(38,18)>,"
        "effective_list:struct<default:decimal(38,18)>>"
    )
    assert ddl_t.startswith("STRUCT(")
    assert pa.types.is_struct(pa_t)
    assert [f.name for f in pa_t] == ["default", "promotional", "effective_list"]
    field = ddl_mod.ARROW_SCHEMA["billing__list_prices"].field("pricing")
    assert pa.types.is_struct(field.type)
    # A struct field name that collides with a DuckDB reserved word ("default") must be quoted.
    assert '"default" DOUBLE' in ddl_mod.DDL["billing__list_prices"]


def test_billing_usage_ddl_has_no_truncation_marker_and_parses_in_duckdb():
    ddl_text = ddl_mod.DDL["billing__usage"]
    assert "more fields" not in ddl_text
    assert "..." not in ddl_text
    con = duckdb.connect(":memory:")
    con.execute(ddl_text)  # raises on invalid DDL
    cols = con.execute("PRAGMA table_info('billing__usage')").fetchall()
    assert len(cols) == 18  # the dump's own top-level column count for system.billing.usage


def test_all_47_ddl_statements_parse_in_duckdb():
    con = duckdb.connect(":memory:")
    for key, ddl_text in ddl_mod.DDL.items():
        con.execute(f"DROP TABLE IF EXISTS {key}")
        con.execute(ddl_text)


def test_usage_metadata_supplemented_with_query_referenced_fields_only():
    """Fields referenced as usage_metadata.<field> across the vendored bodies but absent from the
    dump's own (truncated) field list must be added, VARCHAR-typed; fields the dump already lists
    must never be dropped."""
    field_names = [f.name for f in ddl_mod.ARROW_SCHEMA["billing__usage"].field("usage_metadata").type]
    # A sample of fields the dump DOES list (must survive) and fields only a query body
    # references (must have been added) -- see tools/build_fixture_ddl.py's module docstring.
    for must_survive in ("cluster_id", "job_id", "warehouse_id", "budget_policy_id"):
        assert must_survive in field_names
    for must_be_added in ("catalog_id", "networking_client", "recipient_id", "usage_policy_id"):
        assert must_be_added in field_names
    assert len(field_names) == len(set(field_names))  # no duplicate struct field names
