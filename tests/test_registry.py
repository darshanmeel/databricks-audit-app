"""Tests for app/core/registry.py.

Loads the module directly by file path (importlib.util.spec_from_file_location), the same
pattern tests/test_sync_queries.py (T-02) uses for tools/sync_queries.py -- this keeps the test
correct under a plain `pytest <file> -q` invocation regardless of pytest's rootdir/sys.path
behaviour (which, with no conftest.py or __init__.py anywhere in this repo yet, does not
guarantee the repo root is importable as a package root).

Ground truth (see tasks/T-03-registry-and-fixture-ddl.md "Inputs" and DECISIONS.md DEC-07/DEC-08,
current vendored tree as of 2026-09-22): 103 vendored queries; exactly 3 non-executable
(storage_breakdown_analyze, iceberg_uniform_metadata, table_props_time_travel_config); 42 with
`healthy: n/a - ...`; of the remaining 61, cost_premium_serverless_photon has real `healthy:`
text but no `AS status` column (not a finding); so 60 findings.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = ROOT / "app" / "core" / "registry.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("registry_under_test", REGISTRY_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # registry.py's QuerySpec is a dataclass under `from __future__ import annotations`; the
    # dataclasses module resolves annotation strings via sys.modules[cls.__module__], so this
    # module must be registered in sys.modules BEFORE exec_module runs, or dataclass() raises
    # AttributeError on a None module lookup.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


registry = _load_module()

EXPECTED_NON_EXECUTABLE = {
    "storage_breakdown_analyze",
    "iceberg_uniform_metadata",
    "table_props_time_travel_config",
}


def test_load_registry_counts_103_vendored_queries():
    specs = registry.load_registry()
    vendored = [s for s in specs if s.origin == "vendored"]
    assert len(vendored) == 103
    assert {s.origin for s in specs} <= {"vendored", "app"}  # app-owned ports arrive with T-10/T-11
    assert len({s.query_id for s in specs}) == len(specs)  # every query_id unique across both trees


def test_exactly_3_non_executable_and_ids_match():
    specs = registry.load_registry()
    non_exec = {s.query_id for s in specs if not s.executable}
    assert len(non_exec) == 3
    assert non_exec == EXPECTED_NON_EXECUTABLE
    # runnable=False alone under-counts (per Inputs: checking runnable alone yields 1, not 3).
    non_runnable = {s.query_id for s in specs if not s.runnable}
    assert non_runnable == {"storage_breakdown_analyze"}
    assert len(non_runnable) != len(non_exec)


def test_every_sources_pair_is_in_the_source_contract():
    specs = registry.load_registry()
    checked = 0
    for s in specs:
        for schema, table in s.sources:
            assert schema in registry.SOURCE_CONTRACT, f"{s.query_id}: unknown schema {schema!r}"
            assert table in registry.SOURCE_CONTRACT[schema], (
                f"{s.query_id}: unknown table {schema}.{table!r}"
            )
            checked += 1
    assert checked > 0  # sanity: the loop above actually ran over real pairs


def test_every_next_token_resolves():
    specs = registry.load_registry()
    ids = {s.query_id for s in specs}
    checked = 0
    for s in specs:
        for n in s.next:
            assert n["query_id"] in ids, f"{s.query_id}: next target {n['query_id']!r} unresolved"
            checked += 1
    assert checked > 0


def test_is_finding_false_for_non_executable_ids():
    specs = registry.load_registry()
    by_id = {s.query_id: s for s in specs}
    for qid in EXPECTED_NON_EXECUTABLE:
        assert by_id[qid].is_finding is False, qid


def test_is_finding_counterexample_cost_premium_serverless_photon():
    spec = registry.by_id("cost_premium_serverless_photon")
    # Real healthy: text (not the "n/a -" sentinel) but no "AS status" column in the body.
    assert not spec.healthy.strip().lower().startswith("n/a -")
    assert "AS status" not in spec.body
    assert spec.is_finding is False


def test_is_finding_true_for_a_known_finding():
    spec = registry.by_id("cost_by_job")
    assert "AS status" in spec.body
    assert not spec.healthy.strip().lower().startswith("n/a -")
    assert spec.is_finding is True


def test_derived_counts_match_dec08():
    """Mechanically recompute DEC-08's counts from the loaded specs, then check them against the
    documented ground truth -- never assert a bare literal without this derivation."""
    specs = [s for s in registry.load_registry() if s.origin == "vendored"]  # DEC-08 counts the vendored tree
    assert len(specs) == 103

    inventory_ids = {s.query_id for s in specs if s.healthy.strip().lower().startswith("n/a -")}
    assert len(inventory_ids) == 42

    finding_ids = {s.query_id for s in specs if s.is_finding}
    assert len(finding_ids) == 60

    # Every non-inventory query that is still not a finding is the one documented counterexample.
    counterexample_ids = [
        s.query_id for s in specs if s.query_id not in inventory_ids and s.query_id not in finding_ids
    ]
    assert counterexample_ids == ["cost_premium_serverless_photon"]

    # Cross-check: inventory + findings + counterexamples accounts for all 103, no overlap.
    assert inventory_ids.isdisjoint(finding_ids)
    assert len(inventory_ids) + len(finding_ids) + len(counterexample_ids) == 103


def test_by_id_raises_keyerror_for_unknown_id():
    try:
        registry.by_id("this_query_id_does_not_exist")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError")


def test_body_strips_header_and_trailing_semicolon():
    spec = registry.by_id("storage_breakdown_analyze")
    assert not spec.body.startswith("--")
    assert not spec.body.rstrip().endswith(";")
    assert spec.body.startswith("ANALYZE TABLE")


def test_windowed_true_iff_period_days_param_present():
    for s in registry.load_registry():
        has_period_days = any(p["name"] == "period_days" for p in s.params)
        assert s.windowed == has_period_days, s.query_id


def test_path_and_origin_shape():
    spec = registry.by_id("cost_by_job")
    assert spec.path == "app/queries/vendored/cost/cost_by_job.sql"
    assert spec.origin == "vendored"


def test_caveats_parsed_from_sql_file_not_manifest():
    spec = registry.by_id("cost_by_job")
    assert spec.caveats  # non-empty for a real query
    # manifest.json entries never carry a `caveats` key (Inputs) -- confirm the source .sql file
    # really does have a caveats: line, independent of the registry's own parsing.
    text = (ROOT / spec.path).read_text(encoding="utf-8")
    assert any(line.startswith("-- caveats:") for line in text.splitlines())


def test_app_manifest_absent_is_tolerated():
    # app/queries/app/manifest.json does not exist until T-10/T-11 -- load_registry() must not
    # raise, and must not silently invent any "app"-origin specs.
    specs = registry.load_registry()
    assert not any(s.origin == "app" for s in specs) or (ROOT / "app/queries/app/manifest.json").exists()
