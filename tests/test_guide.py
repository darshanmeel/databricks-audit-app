"""tests/test_guide.py -- GUIDE-SPEC (batch 2), part A only.

Covers the server-side pieces this item owns: app/core/databricks_docs.py's loader, app/api/
guide.py's build_guide()/summary_line()/first_step(), the GET /api/guide route, a static-text
contract on the UI source (web/src/components/nav_hash.ts, web/src/components/guide.tsx).

Every DB-backed test runs against tests/conftest.py's own session fixture (a real, if small,
executable registry + a real DuckDB with every findings.f_* table built), the same fixture every
other test module in this repo already trusts.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api import guide  # noqa: E402
from app.api.app import app  # noqa: E402
from app.core import databricks_docs  # noqa: E402
from app.core import registry  # noqa: E402
from tools import header_schema  # noqa: E402
from tools import snapshot as snapshot_tool  # noqa: E402

client = TestClient(app)

WEB = ROOT / "web" / "src"
GUIDE_JSX = (WEB / "components" / "guide.tsx").read_text(encoding="utf-8")
NAV_HASH_JSX = (WEB / "components" / "nav_hash.ts").read_text(encoding="utf-8")
TAB_REGISTRY_JSX = (WEB / "components" / "tab_registry.ts").read_text(encoding="utf-8")
APP_JSX = (WEB / "App.tsx").read_text(encoding="utf-8")

# The GUIDE_SECTION_SLUGS set is fixed and small (GUIDE-SPEC 3.5); loaded once for every test that
# needs a valid slug set for the loader.
GUIDE_SLUGS = frozenset(guide.GUIDE_SECTION_SLUGS)


def _real_docs_map() -> databricks_docs.DocsMap:
    return databricks_docs.load_docs(
        valid_slugs=GUIDE_SLUGS,
        valid_query_ids=frozenset(s.query_id for s in registry.load_registry()),
    )


# ---------------------------------------------------------------------------------------------
# 1. config/databricks_docs.yml -- shape, coverage, cloud fallback.
# ---------------------------------------------------------------------------------------------


def test_docs_yml_table_keys_equal_snapshot_predicates():
    """GUIDE-SPEC section 9 item 2: the yml's table keys equal tools.snapshot.build_predicates()'s
    fqns exactly -- no more, no fewer, no typo."""
    docs = _real_docs_map()
    yml_keys = {e.key for e in docs.tables()}
    predicate_fqns = {p.fqn for p in snapshot_tool.build_predicates().values()}
    assert yml_keys == predicate_fqns
    assert len(yml_keys) == 49


def test_docs_yml_every_executable_reads_table_has_a_usable_entry():
    """GUIDE-SPEC test 3: every table named in every executable finding's `reads` has a docs
    entry that is either verified for AWS or carries a `note` (the one one documented gap,
    system.billing.attributed_usage)."""
    docs = _real_docs_map()
    specs = [s for s in registry.load_registry() if s.executable]
    assert specs, "fixture registry must have at least one executable query"
    for spec in specs:
        for schema, table in spec.sources:
            key = f"system.{schema}.{table}"
            entry = docs.entries.get(key)
            assert entry is not None, f"{spec.query_id} reads {key}, which is not in the docs map"
            urls = databricks_docs.entry_urls(entry)
            aws_ok = urls["aws"] is not None and urls["aws"]["aws_fallback"] is False
            assert aws_ok or entry.note, f"{key} has neither a verified AWS url nor a note"


def test_docs_yml_every_concept_has_area_query_or_section():
    docs = _real_docs_map()
    for c in docs.concepts():
        assert c.areas or c.queries or c.guide_sections, f"{c.key} names no area, query or section"


def test_docs_yml_verified_subset_falls_back_azure_to_aws():
    """GUIDE-SPEC test 3: an entry verified [aws, gcp] resolves azure to the AWS url, marked
    aws_fallback. concept.sql_warehouse_auto_stop is one of the three entries the review left at
    verified: [aws, gcp] (GUIDE-SPEC section 9 item 3)."""
    docs = _real_docs_map()
    entry = docs.entries["concept.sql_warehouse_auto_stop"]
    assert entry.verified == ["aws", "gcp"]
    urls = databricks_docs.entry_urls(entry)
    assert urls["aws"]["aws_fallback"] is False
    assert urls["gcp"]["aws_fallback"] is False
    assert urls["azure"]["aws_fallback"] is True
    assert urls["azure"]["url"] == urls["aws"]["url"]


def test_docs_yml_unverified_table_has_no_link_and_a_note():
    """system.billing.attributed_usage: verified [] (no cloud opened), null URLs -- no link on any
    cloud, and its `note` explains why (GUIDE-SPEC section 9 item 4)."""
    docs = _real_docs_map()
    entry = docs.entries["system.billing.attributed_usage"]
    assert entry.verified == []
    urls = databricks_docs.entry_urls(entry)
    assert urls == {"aws": None, "azure": None, "gcp": None}
    assert entry.note


# ---------------------------------------------------------------------------------------------
# 2. The loader fails loudly (GUIDE-SPEC 3.4's list), using small tmp yml text -- never the real
#    config/databricks_docs.yml.
# ---------------------------------------------------------------------------------------------

_MIN_TABLE = {
    "key": "system.compute.clusters", "title": "t", "aws": "https://docs.databricks.com/aws/en/x",
    "azure": "https://learn.microsoft.com/en-us/azure/databricks/x",
    "gcp": "https://docs.databricks.com/gcp/en/x", "anchor": None, "verified": True,
}


def _minimal_raw(**overrides) -> dict:
    raw = {
        "version": 1, "default_cloud": "aws", "default_enable": "grant it",
        "system_tables": [dict(_MIN_TABLE)], "concepts": [],
    }
    raw.update(overrides)
    return raw


def test_loader_rejects_unknown_top_level_field():
    with pytest.raises(databricks_docs.DocsError):
        databricks_docs.parse_docs(_minimal_raw(bogus=1), valid_slugs=GUIDE_SLUGS, valid_query_ids=frozenset())


def test_loader_rejects_bad_url_host():
    bad = dict(_MIN_TABLE)
    bad["aws"] = "https://evil.example.com/aws/en/x"
    with pytest.raises(databricks_docs.DocsError):
        databricks_docs.parse_docs(
            _minimal_raw(system_tables=[bad]), valid_slugs=GUIDE_SLUGS, valid_query_ids=frozenset()
        )


def test_loader_rejects_http_not_https():
    bad = dict(_MIN_TABLE)
    bad["aws"] = "http://docs.databricks.com/aws/en/x"
    with pytest.raises(databricks_docs.DocsError):
        databricks_docs.parse_docs(
            _minimal_raw(system_tables=[bad]), valid_slugs=GUIDE_SLUGS, valid_query_ids=frozenset()
        )


def test_loader_rejects_badly_shaped_table_key():
    bad = dict(_MIN_TABLE)
    bad["key"] = "compute.clusters"  # missing the "system." prefix
    with pytest.raises(databricks_docs.DocsError):
        databricks_docs.parse_docs(
            _minimal_raw(system_tables=[bad]), valid_slugs=GUIDE_SLUGS, valid_query_ids=frozenset()
        )


def test_loader_rejects_badly_shaped_concept_key():
    concept = {
        "key": "sql_warehouse_auto_stop",  # missing "concept." prefix
        "title": "t", "aws": None, "azure": None, "gcp": None, "anchor": None, "verified": True,
    }
    with pytest.raises(databricks_docs.DocsError):
        databricks_docs.parse_docs(
            _minimal_raw(concepts=[concept]), valid_slugs=GUIDE_SLUGS, valid_query_ids=frozenset()
        )


def test_loader_rejects_duplicate_key():
    with pytest.raises(databricks_docs.DocsError):
        databricks_docs.parse_docs(
            _minimal_raw(system_tables=[dict(_MIN_TABLE), dict(_MIN_TABLE)]),
            valid_slugs=GUIDE_SLUGS, valid_query_ids=frozenset(),
        )


def test_loader_rejects_over_long_what():
    bad = dict(_MIN_TABLE)
    bad["what"] = "x" * 121
    with pytest.raises(databricks_docs.DocsError):
        databricks_docs.parse_docs(
            _minimal_raw(system_tables=[bad]), valid_slugs=GUIDE_SLUGS, valid_query_ids=frozenset()
        )


def test_loader_rejects_unknown_area():
    concept = {
        "key": "concept.x", "title": "t", "aws": None, "azure": None, "gcp": None,
        "anchor": None, "verified": False, "areas": ["not_a_real_area"],
    }
    with pytest.raises(databricks_docs.DocsError):
        databricks_docs.parse_docs(
            _minimal_raw(concepts=[concept]), valid_slugs=GUIDE_SLUGS, valid_query_ids=frozenset()
        )


def test_loader_rejects_unknown_guide_section_slug():
    bad = dict(_MIN_TABLE)
    bad["guide_sections"] = ["not_a_real_slug"]
    with pytest.raises(databricks_docs.DocsError):
        databricks_docs.parse_docs(
            _minimal_raw(system_tables=[bad]), valid_slugs=GUIDE_SLUGS, valid_query_ids=frozenset()
        )


def test_loader_rejects_unknown_query_id():
    concept = {
        "key": "concept.x", "title": "t", "aws": None, "azure": None, "gcp": None,
        "anchor": None, "verified": False, "queries": ["totally_made_up_query_id"],
    }
    with pytest.raises(databricks_docs.DocsError):
        databricks_docs.parse_docs(
            _minimal_raw(concepts=[concept]), valid_slugs=GUIDE_SLUGS, valid_query_ids=frozenset({"real_id"})
        )


def test_loader_reads_the_real_yml_file_without_error():
    """The real config/databricks_docs.yml, through load_docs() (mtime-cached), not parse_docs()
    directly -- this is the one test that exercises the file-reading + caching path."""
    docs = _real_docs_map()
    assert docs.version == 1
    assert docs.default_cloud == "aws"
    assert len(docs.tables()) == 49
    assert len(docs.concepts()) == 23


# ---------------------------------------------------------------------------------------------
# 3. Cloud resolution (GUIDE-SPEC 1.6 / test 4).
# ---------------------------------------------------------------------------------------------


def test_resolve_cloud_azure():
    assert databricks_docs.resolve_cloud({"metastore": {"cloud": "azure"}}, "aws") == ("azure", "snapshot")


def test_resolve_cloud_uppercase_aws():
    assert databricks_docs.resolve_cloud({"metastore": {"cloud": "AWS"}}, "aws") == ("aws", "snapshot")


def test_resolve_cloud_unknown_value_falls_back_to_default():
    assert databricks_docs.resolve_cloud({"metastore": {"cloud": "oracle"}}, "aws") == ("aws", "default")


def test_resolve_cloud_no_metastore_falls_back_to_default():
    assert databricks_docs.resolve_cloud({"metastore": None}, "aws") == ("aws", "default")
    assert databricks_docs.resolve_cloud(None, "aws") == ("aws", "default")
    assert databricks_docs.resolve_cloud({}, "aws") == ("aws", "default")


# ---------------------------------------------------------------------------------------------
# 4. build_guide() / GET /api/guide shape (GUIDE-SPEC 3.5 / test 1).
# ---------------------------------------------------------------------------------------------


def test_build_guide_finding_ids_equal_executable_registry():
    data = guide.build_guide()
    executable_ids = {s.query_id for s in registry.load_registry() if s.executable}
    guide_ids = {f["query_id"] for f in data["findings"]}
    assert guide_ids == executable_ids
    for f in data["findings"]:
        assert f["title"]
        assert f["domain"]
        assert f["summary"]


def test_build_guide_app_section():
    data = guide.build_guide()
    app_section = data["app"]
    # tests/conftest.py's FIXTURE_METASTORE_ID is "aws:us-east-1:..." -- a real snapshot cloud.
    assert app_section["cloud"] == "aws"
    assert app_section["cloud_source"] == "snapshot"
    assert app_section["window_options"] == [7, 30, 90]
    assert app_section["columns_available"] is True
    assert isinstance(app_section["default_enable"], str) and app_section["default_enable"]


def test_build_guide_app_section_counts_materiality_floors():
    """The "floors" topic (guide.tsx) names how many checks have one -- from the real
    config/materiality.yml, which is non-empty."""
    data = guide.build_guide()
    count = data["app"]["materiality_floors_configured"]
    assert isinstance(count, int) and count > 0


def test_finding_summary_carries_no_internal_id():
    """A finding whose auto-derived summary still names a DEC-/T-/P4- id (summary_line's own
    'problems' list catches this) must not leak that id into the Guide's own lede/grain text."""
    data = guide.build_guide()
    by_id = {f["query_id"]: f for f in data["findings"]}
    assert "DEC-66.1" not in by_id["lakeflow_job_oversized"]["summary"]


def test_build_guide_sections_and_areas_use_guide_section_slugs():
    data = guide.build_guide()
    assert set(data["sections"]) == GUIDE_SLUGS
    for slug, keys in data["sections"].items():
        assert isinstance(keys, list)
    for area, keys in data["areas"].items():
        assert area in databricks_docs.KNOWN_AREAS
        assert keys


def test_build_guide_columns_present_for_built_findings():
    data = guide.build_guide()
    by_id = {f["query_id"]: f for f in data["findings"]}
    sample = by_id["cost_by_job"] if "cost_by_job" in by_id else next(iter(by_id.values()))
    assert sample["columns"] is not None
    assert all({"name", "kind"} == set(c) for c in sample["columns"])


def test_build_guide_survives_a_missing_database(tmp_path, monkeypatch):
    """AUDIT_DB pointed at a path nothing has ever written -- build_guide must still return every
    finding, with columns_available False rather than a 500 (a first-run reader's own case)."""
    monkeypatch.setenv("AUDIT_DB", str(tmp_path / "no_such_file.duckdb"))
    data = guide.build_guide()
    assert data["app"]["columns_available"] is False
    assert all(f["columns"] is None for f in data["findings"])
    executable_ids = {s.query_id for s in registry.load_registry() if s.executable}
    assert {f["query_id"] for f in data["findings"]} == executable_ids


def test_build_guide_on_a_direct_export_database(tmp_path, monkeypatch):
    """No snapshot manifest, a direct_export_meta row: the Guide dates the data by the export."""
    import duckdb

    db_path = tmp_path / "direct.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE direct_export_meta (as_of VARCHAR)")
    con.execute("INSERT INTO direct_export_meta VALUES ('2026-09-25T12:00:00Z')")
    con.close()
    monkeypatch.setenv("AUDIT_DB", str(db_path))
    monkeypatch.setenv("AUDIT_SNAPSHOT_MANIFEST", str(tmp_path / "no-such-manifest.json"))
    app_section = guide.build_guide()["app"]
    assert app_section["as_of"] == "2026-09-25T12:00:00Z"
    assert app_section["direct_export"] == {
        "as_of": "2026-09-25T12:00:00Z", "window_coverage": None, "windows": None,
        "window_aliases": {}, "tags_sources_not_exported": [],
        "as_of_date": None, "includes_today": False,
    }


def test_api_guide_route_matches_build_guide():
    resp = client.get("/api/guide")
    assert resp.status_code == 200
    body = resp.json()
    direct = guide.build_guide()
    assert {f["query_id"] for f in body["findings"]} == {f["query_id"] for f in direct["findings"]}
    assert body["app"]["cloud"] == "aws"


def test_get_guide_survives_a_broken_docs_yml(tmp_path, monkeypatch):
    bad = tmp_path / "bad_docs.yml"
    bad.write_text("version: [1, 2\nthis is not valid yaml: [", encoding="utf-8")
    monkeypatch.setattr(databricks_docs, "_PATH", bad)
    databricks_docs._CACHE = None
    try:
        resp = client.get("/api/guide")
        assert resp.status_code == 200
        body = resp.json()
        assert body["docs"] == {}
        assert body["app"]["docs_error"]
        executable_ids = {s.query_id for s in registry.load_registry() if s.executable}
        assert {f["query_id"] for f in body["findings"]} == executable_ids
    finally:
        databricks_docs._CACHE = None


# ---------------------------------------------------------------------------------------------
# 5. summary_line (GUIDE-SPEC 2.2 / test 2) -- the known, pinned allow-list of checks that need a
#    `summary:` header override (part B's own field; none exists on this branch yet). This list is
#    THIS branch's real, measured set -- not GUIDE-SPEC's own illustrative "25" (which assumed
#    P4-03's real substitute_header_params and other lanes already merged). See part_b_notes.
# ---------------------------------------------------------------------------------------------

KNOWN_SUMMARY_GAPS = frozenset({
    "access_admin_role_change_events", "access_column_lineage_sensitive_reach",
    "access_pii_outside_tables", "access_pii_propagation_untagged", "access_runas_escalation",
    "access_sensitive_table_reads", "access_table_lineage_blast_radius",
    "access_vector_search_traffic", "access_views_inventory", "access_volumes_inventory",
    "classic_clusters_config_current", "compute_cluster_config_posture",
    "compute_serving_endpoint_cost_status", "compute_warehouse_config_posture",
    "cost_actual_vs_list_by_sku", "cost_chargeback_by_allocation_tag", "cost_chargeback_by_cluster",
    "cost_chargeback_by_job", "cost_chargeback_by_service", "cost_chargeback_by_sku",
    "cost_chargeback_by_tag", "cost_chargeback_by_tag_value", "cost_chargeback_by_warehouse",
    "cost_chargeback_by_workspace", "cost_chargeback_identity_by_source",
    "cost_chargeback_reconcile", "cost_daily_spikes", "cost_period_over_period",
    "cost_sku_trend_12m", "instance_events_idle_active", "lakeflow_job_compute_pressure",
    "lakeflow_job_cost_summary", "lakeflow_job_oversized", "lakeflow_job_run_changes",
    "overview_spend_estimate", "po_failure_reasons", "po_vacuum_reclaimed_bytes",
    "query_costly_statements_grouped", "query_provenance_by_source",
    "query_task_statement_breakdown", "sql_warehouse_events_activity", "storage_po_coverage",
    "task_cluster_utilization",
})


def test_summary_line_passes_or_is_on_the_known_allow_list():
    specs = [s for s in registry.load_registry() if s.executable]
    failing = set()
    for spec in specs:
        result = guide.summary_line(spec)
        assert result["text"], f"{spec.query_id} has an empty summary"
        if result["problems"]:
            failing.add(spec.query_id)
    # Exact-set equality: this fails loudly both when a NEW check needs the allow-list (grows it
    # unnoticed) and when a fixed check should have been dropped from it (shrinks silently).
    assert failing == KNOWN_SUMMARY_GAPS


def test_summary_line_passing_checks_are_at_most_140_chars_and_start_right():
    specs = [s for s in registry.load_registry() if s.executable and s.query_id not in KNOWN_SUMMARY_GAPS]
    for spec in specs:
        result = guide.summary_line(spec)
        assert result["problems"] == []
        assert result["text"].startswith("Each row is")
        assert len(result["text"]) <= 140


def test_summary_line_combo_transform():
    """GUIDE-SPEC 2.2 item 2's worked example (section 2.6): a (x, y, z) combination -> one x, y
    and z, in plain words."""
    spec = registry.by_id("lakeflow_failed_runs")
    result = guide.summary_line(spec)
    # P4-FIXES28: grain gained job_id, so this now reads "one job", not "one workspace".
    assert result["text"].startswith("Each row is one job, run type, trigger type, result state and termination code")


# ---------------------------------------------------------------------------------------------
# 6. first_step (GUIDE-SPEC 2.3 / test 2).
# ---------------------------------------------------------------------------------------------

FIRST_STEP_NO_ACTIONS_ALLOWED = frozenset({"cost_actual_vs_list_by_sku"})


def test_first_step_shape_for_every_is_finding():
    specs = [s for s in registry.load_registry() if s.executable and s.is_finding]
    for spec in specs:
        sub = guide._substituted(spec)
        step = guide.first_step(sub["actions"])
        if step is None:
            assert spec.query_id in FIRST_STEP_NO_ACTIONS_ALLOWED, (
                f"{spec.query_id} has is_finding but no usable first_step, and is not on the allow-list"
            )
            continue
        assert step["text"]
        assert len(step["text"]) <= 103
        assert step["tier"] in ("free", "config", "spend")


def test_first_step_matches_panel_worked_example():
    """lakeflow_failed_jobs_wasted_dbus' first step, verbatim."""
    spec = registry.by_id("lakeflow_failed_jobs_wasted_dbus")
    sub = guide._substituted(spec)
    step = guide.first_step(sub["actions"])
    assert step == {
        "text": "read last failed termination code and fix the root cause in the job's code or settings",
        "full": "read last_failed_termination_code and fix the root cause in the job's code or settings; "
                "for repaired runs, find what failed first",
        "tier": "free",
    }


def test_substituted_replaces_a_param_token_in_read_this():
    spec = registry.by_id("lakeflow_job_reliability")
    read_this = guide._substituted(spec)["read_this"]
    assert ":last_n_runs" not in read_this
    assert "10 (default)" in read_this


# ---------------------------------------------------------------------------------------------
# 6b. period_kind -- the article's window chip must never call a fixed-period check (a calendar-
# month grain) a "snapshot", and must never call a real snapshot "its own period".
# ---------------------------------------------------------------------------------------------


def test_period_kind_days_for_every_windowed_check():
    specs = [s for s in registry.load_registry() if s.executable and s.windowed]
    assert specs
    for spec in specs:
        assert guide.period_kind(spec) == "days"


def test_period_kind_fixed_is_exactly_the_known_allow_list():
    """The only non-windowed checks with a real (non-day) period today. A new one must be added
    here deliberately, not silently fall back to reading as a timeless snapshot."""
    specs = [s for s in registry.load_registry() if s.executable and not s.windowed]
    fixed = {s.query_id for s in specs if guide.period_kind(s) == "fixed"}
    assert fixed == guide.FIXED_PERIOD_QUERY_IDS
    snapshot = {s.query_id for s in specs if guide.period_kind(s) == "snapshot"}
    assert snapshot == {s.query_id for s in specs} - fixed


# ---------------------------------------------------------------------------------------------
# 6c. _clean_prose -- ids/shouting stripped for the Guide's own reference pages; every column
# name (snake_case) is left exactly as the header wrote it, for guide.tsx to put in code font.
# ---------------------------------------------------------------------------------------------


def test_clean_prose_drops_lone_parenthetical_id():
    assert guide._clean_prose("Reuses the same join (DEC-66.1) and price_basis CASE.") == (
        "Reuses the same join and price_basis CASE."
    )


def test_clean_prose_drops_inline_id_and_capitalizes():
    out = guide._clean_prose("DEC-66.1 - the effective list price only. rule T-71 established.")
    assert "DEC-66.1" not in out
    assert "T-71" not in out
    assert out == "The effective list price only. Rule established."


def test_clean_prose_double_dash_becomes_em_dash():
    assert guide._clean_prose("it's a snapshot -- not a period") == "It's a snapshot — not a period"


def test_clean_prose_lowers_shouted_bool_but_keeps_column_name_case():
    out = guide._clean_prose("is_partial_month is TRUE only -- never compare.")
    assert out == "is_partial_month is true only — never compare."


def test_clean_prose_never_capitalizes_a_column_name_at_a_sentence_start():
    out = guide._clean_prose("Rule DEC-62 established. is_partial_month is TRUE only.")
    assert out.split(". ")[1].startswith("is_partial_month")


def test_clean_prose_empty_and_none_are_safe():
    assert guide._clean_prose(None) == ""
    assert guide._clean_prose("") == ""


def test_finding_json_prose_fields_carry_no_internal_ids():
    """Every id-bearing header field this app knows of (GUIDE-SPEC decision: ids are for the repo,
    not the reader) -- checked across the whole executable registry, not one hand-picked id."""
    import re

    data = guide.build_guide()
    id_re = re.compile(r"\b(?:DEC|T|P4)-\d+\b")
    for f in data["findings"]:
        for field in ("read_this", "healthy", "investigate_if", "caveats", "confidence_note"):
            assert not id_re.search(f[field] or ""), f"{f['query_id']}.{field} still carries an id"
            assert "TRUE" not in (f[field] or "") and "FALSE" not in (f[field] or ""), (
                f"{f['query_id']}.{field} still shouts TRUE/FALSE"
            )


# ---------------------------------------------------------------------------------------------
# 6d. Cloud fallback for a direct export/import build with no snapshot manifest (GUIDE-SPEC
# extension): dims.dim_workspace.url's own host, when the manifest gave no cloud at all.
# ---------------------------------------------------------------------------------------------


def _dim_workspace_db(tmp_path, url) -> Path:
    import duckdb

    con = duckdb.connect(str(tmp_path / "ws.duckdb"))
    con.execute("CREATE SCHEMA dims")
    con.execute("CREATE TABLE dims.dim_workspace (workspace_id VARCHAR, url VARCHAR)")
    con.execute("INSERT INTO dims.dim_workspace VALUES ('1', ?)", [url])
    con.close()
    return tmp_path / "ws.duckdb"


def test_cloud_from_workspace_urls_azure_host(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_DB", str(_dim_workspace_db(tmp_path, "https://adb-1.4.azuredatabricks.net")))
    assert guide._cloud_from_workspace_urls() == "azure"


def test_cloud_from_workspace_urls_gcp_host(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_DB", str(_dim_workspace_db(tmp_path, "https://x.gcp.databricks.com")))
    assert guide._cloud_from_workspace_urls() == "gcp"


def test_cloud_from_workspace_urls_aws_host(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_DB", str(_dim_workspace_db(tmp_path, "https://x.cloud.databricks.com")))
    assert guide._cloud_from_workspace_urls() == "aws"


def test_cloud_from_workspace_urls_none_on_missing_table(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_DB", str(tmp_path / "no_such_file.duckdb"))
    assert guide._cloud_from_workspace_urls() is None


def test_build_guide_falls_back_to_workspace_cloud_with_no_manifest(tmp_path, monkeypatch):
    """A direct export/import build never runs tools/snapshot.py's own metastore probe -- the
    workspace's own host should still resolve the cloud instead of silently defaulting to AWS."""
    db_path = _dim_workspace_db(tmp_path, "https://adb-1.4.azuredatabricks.net")
    monkeypatch.setenv("AUDIT_DB", str(db_path))
    monkeypatch.setenv("AUDIT_SNAPSHOT_MANIFEST", str(tmp_path / "no-such-manifest.json"))
    app_section = guide.build_guide()["app"]
    assert app_section["cloud"] == "azure"
    assert app_section["cloud_source"] == "workspace"


# ---------------------------------------------------------------------------------------------
# 7. EMPTY_IF_WORDS agreement (GUIDE-SPEC 3.5).
# ---------------------------------------------------------------------------------------------


def test_empty_if_words_matches_vocab():
    assert set(guide.EMPTY_IF_WORDS) == header_schema.EMPTY_IF_VOCAB


# ---------------------------------------------------------------------------------------------
# 8. Static UI source contract (GUIDE-SPEC test 7), read as text -- tests/test_scope_disclosure.py's
#    own style, never a JS runtime. navHref's behaviour is tested in web/src (Vitest).
# ---------------------------------------------------------------------------------------------

ALL_WEB_JSX = [p for p in WEB.rglob("*") if p.suffix in (".ts", ".tsx")]


def test_app_jsx_has_guide_tab():
    assert '"guide"' in APP_JSX
    assert "<GuideTab" in APP_JSX


def test_no_databricks_doc_urls_in_any_jsx():
    """GUIDE-SPEC decision: 'Docs URLs live only in config/databricks_docs.yml. They never go in
    the UI or .py.'"""
    for path in ALL_WEB_JSX:
        text = path.read_text(encoding="utf-8")
        assert "docs.databricks.com" not in text, path
        assert "learn.microsoft.com" not in text, path


def test_no_raw_hash_tab_href_in_any_jsx():
    """GUIDE-SPEC decision 7: every deep link goes through navHref(); no literal href="#tab=...".
    nav_hash.ts itself builds the hash with URLSearchParams, never that literal."""
    for path in ALL_WEB_JSX:
        text = path.read_text(encoding="utf-8")
        assert 'href="#tab=' not in text, path


def test_guide_jsx_uses_nav_hash_and_no_second_writer():
    assert "navHref(" in GUIDE_JSX
    assert "readNavHash(" not in GUIDE_JSX  # Guide only ever writes/reads via navHref + the focus prop
    assert "writeNavHash(" not in GUIDE_JSX


def test_nav_hash_jsx_has_navhref_and_readnavhash():
    assert "function navHref(" in NAV_HASH_JSX
    assert "function readNavHash(" in NAV_HASH_JSX
    assert "replaceState" in NAV_HASH_JSX
    assert "navHref" in APP_JSX or "readNavHash" in APP_JSX  # App.tsx reads the initial hash


def test_guide_sections_jsx_matches_guide_section_slugs():
    """web/src/components/guide.tsx's GUIDE_SECTIONS slugs, in order, must equal app/api/guide.py's
    GUIDE_SECTION_SLUGS (GUIDE-SPEC test 7)."""
    import re

    block_match = re.search(r"const GUIDE_SECTIONS(?::[^=]+)? = \[(.*?)\n\];", GUIDE_JSX, re.DOTALL)
    assert block_match, "GUIDE_SECTIONS array not found in guide.tsx"
    slugs = re.findall(r'slug:\s*"([a-z_-]+)"', block_match.group(1))
    assert tuple(slugs) == guide.GUIDE_SECTION_SLUGS


def test_guide_tabs_jsx_covers_every_screen_area_exactly():
    """GUIDE_TABS (guide.tsx) has one entry per screen with a Guide page -- tab_registry.ts's own
    AREA_ORDER minus All findings, plus the executive Money page after Actions -- no more, no fewer,
    in that order."""
    import re

    order_match = re.search(r'const AREA_ORDER(?::[^=]+)? = \[(.*?)\];', TAB_REGISTRY_JSX)
    assert order_match, "AREA_ORDER not found in tab_registry.ts"
    areas = re.findall(r'"([a-z]+)"', order_match.group(1))
    expected = [k for a in areas if a != "findings" for k in ([a, "money"] if a == "actions" else [a])]
    assert expected, "AREA_ORDER parsed empty"

    block = re.search(r"const GUIDE_TABS(?::[^=]+)? = \{(.*?)\n\};", GUIDE_JSX, re.DOTALL)
    assert block, "GUIDE_TABS not found in guide.tsx"
    keys = re.findall(r"^  ([a-z_]+):\s*\{", block.group(1), re.MULTILINE)
    assert keys == expected
