"""tests/test_api.py -- the FastAPI JSON API's Definition of done.

FastAPI TestClient coverage for every app/api/app.py route, the four-outcome honesty rule
(OK-with-rows, empty-in-window, empty-by-filters, NOT_ASSESSED, plus the fifth ERROR row the
table itself carries), the "all workspaces selected by default" contract, and that an unknown
query_id 404s rather than 500s.

Every test runs against tests/conftest.py's own session fixture (AUDIT_DB=tests/
db_audit_test.duckdb, a synthesised manifest.json/run_results.json -- see that module's own
docstring), the same fixture every other test module in this repo already trusts. A handful of
tests need a DIFFERENT database/run_results state to reach an outcome the healthy fixture never
produces on its own (every one of its tables has real rows in every window, DEC-54's own
synthesis: `state: "ok"` for every source) -- those use the module-scoped `mutated_db` fixture
below (one ~90 MB copy of the session db, mutated twice, reused by every test that needs it) or a
small edited run_results.json / a deliberately corrupt db file, following the same
copy-and-monkeypatch pattern tests/test_app/test_scenarios.py already established for the
Streamlit suite.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api import app as api_app  # noqa: E402
from app.api import service  # noqa: E402
from app.api.app import app  # noqa: E402
from app.core import config as app_config  # noqa: E402
from app.core import data as app_core_data  # noqa: E402
from app.core import library_corrections as app_library_corrections  # noqa: E402
from app.core import registry  # noqa: E402

client = TestClient(app)

ALL_WORKSPACE_IDS = [
    "1111", "2222", "3333", "9001", "9002", "cb_ws1", "iw_ws8",
    # tests/fixtures/cases_jobs.py's own workspace: in region through its job runs, never billed.
    "7705",
    # tests/fixtures/top_by_cost.py's own workspace (query_top_by_cost).
    "tbc_ws1",
    # P4-T-ROLL (tasks/P4-T-SPEC.md section 7.6): tests/fixtures/tagworld.py's three workspaces.
    "tg_ws_a", "tg_ws_m", "tg_ws_s",
    # tests/fixtures/billing.py SEC N (cost_unnamed_workspaces): billed but carrying no
    # access.workspaces_latest row at all -- the one case in this fixture with genuinely no name
    # anywhere, not even a seed override.
    "uw_crit", "uw_ok", "uw_warn", "wu_ws9",
    # tests/fixtures/oversized_jobs.py (lakeflow_job_oversized) and tests/fixtures/hour_of_day.py
    # (cost_by_hour_of_day): both billed and in-region.
    "9101", "9301",
    # tests/fixtures/chargeback_a.py: one dedicated workspace per cost_chargeback_by_* check (or
    # per scenario for cost_chargeback_by_workspace); billed, never named anywhere.
    "cba_ws_crit", "cba_ws_id", "cba_ws_na_cur", "cba_ws_ok", "cba_ws_sku", "cba_ws_svc",
    "cba_ws_warn", "cba_ws_wh", "cba_ws_win",
    # tests/fixtures/compute_coverage.py: its own workspace, named via access.workspaces_latest.
    "cc_ws1",
    # tests/fixtures/genie.py: its two workspaces, billed and named via access.workspaces_latest.
    "gn_ws_dev", "gn_ws_prod",
]
UNNAMED_WORKSPACE_IDS = {
    "uw_crit", "uw_ok", "uw_warn",
    "cba_ws_crit", "cba_ws_id", "cba_ws_na_cur", "cba_ws_ok", "cba_ws_sku", "cba_ws_svc",
    "cba_ws_warn", "cba_ws_wh", "cba_ws_win",
    # tests/fixtures/cases_jobs.py's own workspace: no access.workspaces_latest row at all.
    "7705",
}

# A query whose findings.f_* table carries an account-level (workspace_id IS NULL) billing row
# (tests/fixtures' own "an account-level workspace_id NULL billing row" fixture fact, PLAN.md 5.7)
# -- the one that makes "no workspace_ids filter" a genuinely different, larger read than "every
# workspace_id passed explicitly" (SQL's own NULL-never-matches-IN semantics), which is exactly
# why app/web's front end sends NO filter (not an explicit full id list) when every workspace chip
# is on.
ACCOUNT_WIDE_QUERY_ID = "cost_chargeback_by_identity"
PLAIN_QUERY_ID = "cost_by_job"
# A query with no account-wide row at all -- "no filter" and "every id explicit" must agree
# exactly. cost_by_job (PLAIN_QUERY_ID) no longer qualifies for this one check: tests/fixtures/
# chargeback.py's own cb2_* job rows carry an account-level workspace_id IS NULL row too, for a
# different check's scenario.
NO_ACCOUNT_WIDE_QUERY_ID = "cost_by_notebook"

EMPTY_WINDOW_QUERY_ID = "cost_by_job"           # windowed=True; window=7 rows deleted below
EMPTY_WINDOW_AT = 7
MISSING_TABLE_QUERY_ID = "compute_idle_node_ratio"  # windowed=True; table dropped below
ERROR_STATUS_QUERY_ID = "lakeflow_failed_runs"      # run_results.json entry flipped to "error"


# -------------------------------------------------------------------------------------------
# Scenario fixtures -- one ~90 MB db copy (mutated_db), reused by every test in this module that
# needs a non-"ok_rows" DuckDB-level outcome; small, cheap JSON/file scenarios stay per-test.
# -------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mutated_db(tmp_path_factory):
    """One copy of the session fixture db with two mutations:

      1. findings.f_<EMPTY_WINDOW_QUERY_ID> loses every window_days=7 row -- genuinely "nothing
         in this window" (window=30/90 stay populated) -- OUTCOME_OK_EMPTY_WINDOW.
      2. findings.f_<MISSING_TABLE_QUERY_ID> is DROPped entirely, while the session's own
         run_results.json (never edited -- tests/conftest.py already synthesised it against the
         ORIGINAL db, before this copy exists) still claims that model built successfully --
         OUTCOME_NOT_ASSESSED via the FindingNotBuiltError "not_built_reason" path, distinct from
         the "run_results itself says error" path error_status_env below covers.
    """
    src = app_core_data._db_path()
    dest = tmp_path_factory.mktemp("api_scenario_db") / "scenario.duckdb"
    shutil.copy2(src, dest)
    con = duckdb.connect(str(dest))
    try:
        con.execute(f'DELETE FROM findings."f_{EMPTY_WINDOW_QUERY_ID}" WHERE window_days = {EMPTY_WINDOW_AT}')
        con.execute(f'DROP TABLE findings."f_{MISSING_TABLE_QUERY_ID}"')
    finally:
        con.close()
    return dest


@pytest.fixture
def empty_window_env(mutated_db, monkeypatch):
    monkeypatch.setenv("AUDIT_DB", str(mutated_db))


@pytest.fixture
def missing_table_env(mutated_db, monkeypatch):
    monkeypatch.setenv("AUDIT_DB", str(mutated_db))


@pytest.fixture
def error_status_env(tmp_path, monkeypatch):
    """A run_results.json copy with ERROR_STATUS_QUERY_ID's status flipped to a real dbt error
    shape -- OUTCOME_NOT_ASSESSED via the "model itself errored" branch (finding_status reads
    run_results.json only, no DuckDB connection needed for this path)."""
    src = Path(os.environ["AUDIT_RUN_RESULTS"])
    data = json.loads(src.read_text(encoding="utf-8"))
    target_uid = f"model.databricks_audit.f_{ERROR_STATUS_QUERY_ID}"
    found = False
    for result in data["results"]:
        if result.get("unique_id") == target_uid:
            result["status"] = "error"
            result["message"] = 'Binder Error: column "bogus_column" does not exist'
            found = True
    assert found, f"{target_uid} missing from the session's synthesised run_results.json"
    dest = tmp_path / "run_results_error.json"
    dest.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("AUDIT_RUN_RESULTS", str(dest))


@pytest.fixture
def corrupt_db_env(tmp_path, monkeypatch):
    """AUDIT_DB pointed at a file that exists but is not a valid DuckDB database -- the ERROR
    outcome (a local read failure), distinct from a missing table (NOT_ASSESSED)."""
    bad = tmp_path / "corrupt.duckdb"
    bad.write_bytes(b"not a duckdb file -- api ERROR-outcome scenario")
    monkeypatch.setenv("AUDIT_DB", str(bad))


# T-68's own scenario (fix_plan.txt batch 3: "the 5,100-row insert gives slice != total, and the
# aggregate groups sum to rows_total"): 5,100 extra findings.f_cost_by_job rows, all sharing one
# synthetic job_id, so a plain /api/finding fetch (capped at 5,000) provably undercounts them while
# GET .../aggregate (a real SQL GROUP BY, no LIMIT before it aggregates) does not. Built by
# duplicating one REAL row 5,100 times, explicitly listing the target table's OWN columns (read
# from information_schema, the same discipline app/core/data.py itself uses) and only overriding
# the four this scenario needs (job_id/window_days/net_usage_quantity/status) -- every other
# column, including workspace_id, is copied from that real row untouched via `tmpl."<col>"`, so no
# column-type guess is ever needed for a value this test does not care about.
FLOOD_JOB_ID = "rc_flood_job"
FLOOD_ROWS = 5100


@pytest.fixture(scope="module")
def flood_db(tmp_path_factory):
    src = app_core_data._db_path()
    dest = tmp_path_factory.mktemp("api_agg_flood_db") / "flood.duckdb"
    shutil.copy2(src, dest)
    con = duckdb.connect(str(dest))
    try:
        table = f"f_{PLAIN_QUERY_ID}"
        cols = [
            r[0]
            for r in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'findings' AND table_name = ? ORDER BY ordinal_position",
                [table],
            ).fetchall()
        ]
        overrides = {
            "window_days": "30",
            "job_id": f"'{FLOOD_JOB_ID}'",
            "net_usage_quantity": "1.0",
            "status": "'OK'",
        }
        select_list = ", ".join(overrides.get(c, f'tmpl."{c}"') for c in cols)
        con.execute(
            f'INSERT INTO findings."{table}" SELECT {select_list} '
            f'FROM (SELECT * FROM findings."{table}" LIMIT 1) AS tmpl, range({FLOOD_ROWS}) AS r'
        )
    finally:
        con.close()
    return dest


@pytest.fixture
def flood_env(flood_db, monkeypatch):
    monkeypatch.setenv("AUDIT_DB", str(flood_db))


# P2-NOBUILD scenario fixtures -- "no database yet" / "database present but the build failed or
# is partial" / "settings file invalid", the three states GET /api/status exists to tell apart.


@pytest.fixture
def no_database_env(tmp_path, monkeypatch):
    """AUDIT_DB pointed at a path nothing has ever written -- the exact state of a fresh checkout
    before the first `python tools/dbt_run.py build` has run."""
    monkeypatch.setenv("AUDIT_DB", str(tmp_path / "no_such_file.duckdb"))


@pytest.fixture(scope="module")
def no_dim_workspace_db(tmp_path_factory):
    """A real, valid copy of the session fixture db with dims.dim_workspace DROPped -- "the build
    failed or is partial" when the failure is specifically the one table GET /api/workspaces (and
    through it the whole filter bar) depends on, as opposed to corrupt_db_env's "not a database
    file at all" (both are build_incomplete; this is the other half of that state)."""
    src = app_core_data._db_path()
    dest = tmp_path_factory.mktemp("api_no_dim_workspace_db") / "no_dim_workspace.duckdb"
    shutil.copy2(src, dest)
    con = duckdb.connect(str(dest))
    try:
        con.execute("DROP TABLE dims.dim_workspace")
    finally:
        con.close()
    return dest


@pytest.fixture
def no_dim_workspace_env(no_dim_workspace_db, monkeypatch):
    monkeypatch.setenv("AUDIT_DB", str(no_dim_workspace_db))


@pytest.fixture
def bad_settings_env(tmp_path, monkeypatch):
    """AUDIT_CONFIG_DIR pointed at a fresh dir seeded with a settings.yml this test writes itself
    -- returns the dir so a test can write whatever broken content it needs before calling the
    API. Distinct from the money-label test's `cfg_dir` (that one writes a VALID settings.yml via
    app_config.save_settings; this one writes raw, deliberately-invalid text no save_settings call
    would ever produce)."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    monkeypatch.setenv("AUDIT_CONFIG_DIR", str(cfg_dir))
    return cfg_dir


# -------------------------------------------------------------------------------------------
# GET /api/status -- P2-NOBUILD. app/web's shell calls this before anything else; every other
# route's own ConfigError handling (settings_invalid) is exercised separately below.
# -------------------------------------------------------------------------------------------


def test_status_ready_on_healthy_fixture():
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "ready"
    assert body["settings_error"] is None
    assert body["database"]["state"] == "ready"


def test_status_no_database(no_database_env):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "no_database"
    assert body["settings_error"] is None
    assert body["database"]["state"] == "no_database"


def test_status_build_incomplete_on_corrupt_db(corrupt_db_env):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "build_incomplete"
    assert body["database"]["state"] == "build_incomplete"
    assert body["database"]["detail"]


def test_status_build_incomplete_when_dim_workspace_missing(no_dim_workspace_env):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "build_incomplete"
    assert "dim_workspace" in body["database"]["detail"].lower()


def test_status_settings_invalid_unknown_key_names_key_and_line(bad_settings_env):
    (bad_settings_env / "settings.yml").write_text(
        "discount_pct: 0.0\ndbt_targt: dev\ndefault_window: 30\n", encoding="utf-8", newline="\n"
    )
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "settings_invalid"
    err = body["settings_error"]
    assert err["key"] == "dbt_targt"
    assert err["line"] == 2
    assert "dbt_targt" in err["message"]


def test_status_settings_invalid_bad_value_names_key_and_line(bad_settings_env):
    (bad_settings_env / "settings.yml").write_text(
        "discount_pct: 0.0\ndbt_target: dev\ndefault_window: 14\nui:\n  max_rows: 500\n"
        "  max_chart_categories: 8\ntag_share_floor: 0.6\n",
        encoding="utf-8",
        newline="\n",
    )
    r = client.get("/api/status")
    body = r.json()
    assert body["state"] == "settings_invalid"
    assert body["settings_error"]["key"] == "default_window"
    assert body["settings_error"]["line"] == 3


def test_status_settings_invalid_nested_ui_key_names_dotted_key_and_line(bad_settings_env):
    (bad_settings_env / "settings.yml").write_text(
        "discount_pct: 0.0\ndbt_target: dev\ndefault_window: 30\nui:\n"
        "  max_rows: 500\n  max_chart_categories: -1\ntag_share_floor: 0.6\n",
        encoding="utf-8",
        newline="\n",
    )
    r = client.get("/api/status")
    body = r.json()
    assert body["state"] == "settings_invalid"
    assert body["settings_error"]["key"] == "ui.max_chart_categories"
    assert body["settings_error"]["line"] == 6


def test_status_settings_invalid_yaml_syntax_error_reports_line(bad_settings_env):
    # Not a semantic error (a known key with a bad value) -- genuinely broken YAML (bad
    # indentation), the "a typo in config/settings.yml" case in its most literal form. PyYAML's
    # own parser already knows the line; load_settings() must carry it through unchanged rather
    # than trying (and failing) to look a key up that _validate_settings never got to run against.
    (bad_settings_env / "settings.yml").write_text(
        "discount_pct: 0.0\ndbt_target: dev\n  default_window: 30\n", encoding="utf-8", newline="\n"
    )
    r = client.get("/api/status")
    body = r.json()
    assert body["state"] == "settings_invalid"
    assert body["settings_error"]["line"] == 3
    assert body["settings_error"]["key"] is None


def test_status_settings_invalid_non_string_unknown_key_still_200s(bad_settings_env):
    """Review fix (P2-NOBUILD): a YAML 1.1 unquoted `2:` key parses as the int 2, not "2" -- before
    the fix, _validate_settings handed that int through as `.key`, and load_settings()'s
    `key.partition(".")` raised AttributeError, which escaped the GET /api/status route's own
    `except ConfigError` and came back as a plain 500 -- the exact bug P2-NOBUILD removes."""
    (bad_settings_env / "settings.yml").write_text(
        "discount_pct: 0.0\n2: dev\n", encoding="utf-8", newline="\n"
    )
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "settings_invalid"
    assert body["settings_error"]["key"] == "2"


def test_status_settings_invalid_wins_over_a_missing_database(bad_settings_env, no_database_env):
    (bad_settings_env / "settings.yml").write_text("discount_pct: 2.0\n", encoding="utf-8", newline="\n")
    r = client.get("/api/status")
    body = r.json()
    # Both are broken at once -- settings_invalid is reported, not no_database, since nothing
    # else can be trusted (every other route 503s) until settings.yml itself is fixed.
    assert body["state"] == "settings_invalid"


def test_settings_brand_defaults_when_block_missing(bad_settings_env):
    """An old settings.yml with no `brand` block still loads, with the Crosshire defaults."""
    (bad_settings_env / "settings.yml").write_text(
        "discount_pct: 0.0\ndbt_target: dev\ndefault_window: 30\n", encoding="utf-8", newline="\n"
    )
    assert app_config.load_settings()["brand"] == app_config.DEFAULT_SETTINGS["brand"]


# -------------------------------------------------------------------------------------------
# A broken settings.yml also 503s (never a plain 500) on every OTHER route, not only
# GET /api/status -- app/api/app.py's own ConfigError exception handler, exercised here against
# two unrelated routes so the fix is proven to be general, not special-cased to one endpoint.
# -------------------------------------------------------------------------------------------


def test_meta_endpoint_503_structured_on_broken_settings(bad_settings_env):
    (bad_settings_env / "settings.yml").write_text("discount_pct: not_a_number\n", encoding="utf-8", newline="\n")
    r = client.get("/api/meta")
    assert r.status_code == 503
    body = r.json()
    assert body["state"] == "settings_invalid"
    assert body["key"] == "discount_pct"
    assert body["detail"]


def test_finding_detail_503_structured_on_broken_settings(bad_settings_env):
    (bad_settings_env / "settings.yml").write_text("not_a_real_key: 1\n", encoding="utf-8", newline="\n")
    r = client.get(f"/api/finding/{PLAIN_QUERY_ID}", params={"window": 30})
    assert r.status_code == 503
    body = r.json()
    assert body["state"] == "settings_invalid"
    assert body["key"] == "not_a_real_key"


# -------------------------------------------------------------------------------------------
# GET /api/workspaces -- a missing/corrupt database is a clear 503, never an opaque 500 (P2-NOBUILD:
# this route had no guard of its own; app/web's shell now checks GET /api/status first and never
# reaches this route in that state, but the guard is the honest fallback for any other caller).
# -------------------------------------------------------------------------------------------


def test_workspaces_endpoint_503_not_500_on_missing_database(no_database_env):
    r = client.get("/api/workspaces")
    assert r.status_code == 503
    assert "dims.dim_workspace" in r.json()["detail"]


def test_workspaces_endpoint_503_not_500_on_corrupt_database(corrupt_db_env):
    r = client.get("/api/workspaces")
    assert r.status_code == 503


# -------------------------------------------------------------------------------------------
# GET /api/meta
# -------------------------------------------------------------------------------------------


def test_meta_endpoint():
    n = sum(1 for s in registry.load_registry() if s.executable)
    r = client.get("/api/meta")
    assert r.status_code == 200
    body = r.json()
    assert body["window_options"] == [7, 30, 90]
    assert body["snapshot_available"] is True
    assert body["as_of"] == "2026-09-21T12:00:00"
    # 2026-09-25 fix: every money figure's shared cut-off (complete days only, through yesterday),
    # derived from this same as_of -- data_through is as_of_date minus one day, partial_day is
    # as_of_date itself (the day the snapshot only partly captured), partial_until its HH:MM.
    assert body["cost_cutoff"] == {
        "data_through": "2026-09-20",
        "partial_day": "2026-09-21",
        "partial_until": "12:00",
    }
    assert body["models"]["registry_total"] == n
    assert body["models"]["ok"] == n
    assert body["models"]["failed"] == 0
    assert body["models"]["not_built"] == 0
    # T-58: the top-N-plus-Other cap app/web's chart aggregation (hooks.ts topNWithOther) reads,
    # sourced from config.ui.max_chart_categories rather than a client-side hardcoded number.
    settings = app_config.load_settings()
    assert body["max_chart_categories"] == settings["ui"]["max_chart_categories"]
    assert body["brand"] == settings["brand"]


def test_hidden_attribution_is_hard_coded_and_off_screen():
    import app as app_pkg

    email = "darshansingh@crosshire.ch"
    assert (app_pkg.__author__, app_pkg.__email__) == ("Darshan Singh", email)
    assert (app_pkg.__url__, app_pkg.__copyright__) == ("https://crosshire.ch", "Crosshire")
    html = (ROOT / "app" / "web" / "dist" / "index.html").read_text(encoding="utf-8")
    author = re.search(r'<meta name="author" content="([^"]*)">', html)
    assert author and email in author.group(1) and "Darshan Singh" in author.group(1)
    assert '<meta name="copyright" content="Crosshire">' in html
    # Only the meta tag carries it: the visible footer is built from settings, never from this.
    assert html.count(email) == 1
    assert email not in (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")


# -------------------------------------------------------------------------------------------
# GET /api/workspaces
# -------------------------------------------------------------------------------------------


def test_workspaces_endpoint():
    r = client.get("/api/workspaces")
    assert r.status_code == 200
    rows = r.json()
    assert {w["workspace_id"] for w in rows} == set(ALL_WORKSPACE_IDS)
    for w in rows:
        assert isinstance(w["workspace_id"], str)  # PLAN.md 6.2: "IDs are strings"
        if w["workspace_id"] in UNNAMED_WORKSPACE_IDS:
            assert w["name"] is None
        else:
            assert w["name"]
        assert w["env"] in ("prod", "uat", "dev", "unknown")
        assert w["env_reason"]


# -------------------------------------------------------------------------------------------
# GET /api/dims -- T-58: dims.dim_job/cluster/warehouse/pipeline, "names, not ids" for the tab
# charts. Wraps app/core/data.read_dim_job and friends (T-40; T-66B: those four readers now mask
# every owner/run-as identity column to DEC-66.3's format before returning -- see app/core/
# identity.py and app/core/data.py's own module comment above read_dim_job).
# -------------------------------------------------------------------------------------------

_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


_USER_ID_RE = re.compile(r"^[0-9]+$")


def _expected_identity(value, real_id=None):
    """Independent re-implementation of DEC-66.3's identity format (NOT copy-pasted from
    app/core/identity.py), used to recompute an expected masked value from a RAW value read
    straight off dims.dim_job/dim_cluster/dim_warehouse/dim_pipeline (see
    test_dims_identities_match_dec_66_3_against_the_raw_dims below) -- the same
    "independently re-derive, don't reuse the code under test" discipline
    tests/test_findings/test_governance_inventories.py's own `_mask` helper documents. `real_id`
    mirrors format_identity's own COALESCE-semantics parameter: None (the default) means the
    hash-fallback branch, matching every column here except dim_job's run_as_user_name (see
    _expected_run_as_pair below, which supplies dim_job.run_as as real_id when it is one)."""
    if value is None or value == "__REDACTED__":
        return value
    if _GUID_RE.match(value):
        return value
    id_part = str(real_id) if real_id is not None else hashlib.sha256(value.strip(" ").lower().encode()).hexdigest()[:8]
    return f"{id_part} {value[:2]}***"


def _expected_run_as_pair(run_as, run_as_user_name):
    """Mirror app/core/data._mask_dim_job's own run_as/run_as_user_name pairing (T-66B review
    fix): dim_job.run_as is itself a real per-row Databricks user/service-principal id at the
    source (system.lakeflow.jobs.run_as), so when it is a bare numeric user id it becomes
    run_as_user_name's real_id (never the hash fallback) and is left as-is for run_as itself;
    otherwise run_as is masked on its own, same as any other hash-fallback column."""
    real_id = run_as if isinstance(run_as, str) and _USER_ID_RE.match(run_as) else None
    expected_run_as_user_name = _expected_identity(run_as_user_name, real_id=real_id)
    expected_run_as = run_as if real_id is not None else _expected_identity(run_as)
    return expected_run_as, expected_run_as_user_name


def _assert_identity_shaped(value, *, allow_bare_user_id=False):
    """A masked value is either None, '__REDACTED__', a bare GUID, or "<id> <2 chars>***" -- never
    a bare, unmasked '...@...' email. `allow_bare_user_id=True` (dim_job's own `run_as` column
    only, T-66B review fix) also accepts a bare numeric id with no '***' suffix: unlike every
    other identity column here, run_as IS the real id DEC-66.3 displays for the paired
    run_as_user_name, so app/core/data._mask_dim_job leaves it as that bare id rather than
    masking it a second time into a different-looking code for the same person."""
    if value is None or value == "__REDACTED__" or _GUID_RE.match(value):
        return
    if allow_bare_user_id and _USER_ID_RE.match(value):
        return
    assert "@" not in value, f"{value!r} looks like an unmasked email, not a DEC-66.3 mask"
    assert value.endswith("***"), f"{value!r} does not end in the DEC-66.3 '***' suffix"


@pytest.fixture
def mask_users_on(_app_env):
    """privacy.mask_user_identities is off by default -- this fixture turns it on for one test's
    duration and restores the session's seeded settings.yml afterwards."""
    path = _app_env["config_dir"] / "settings.yml"
    original = path.read_text(encoding="utf-8")
    app_config.save_settings({**app_config.load_settings(), "privacy": {"mask_user_identities": True}})
    try:
        yield
    finally:
        path.write_text(original, encoding="utf-8")


def test_dims_endpoint_shape():
    r = client.get("/api/dims")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"jobs", "clusters", "warehouses", "pipelines", "notebooks"}
    assert len(body["jobs"]) > 0
    assert len(body["clusters"]) > 0
    assert len(body["warehouses"]) > 0
    assert len(body["pipelines"]) > 0


def test_dim_records_ids_are_strings_and_other_values_pass_through_unchanged():
    """_dim_records is column-vectorised, not df.iterrows() (speed lane: ~17k combined dim rows
    used to take 13-25s here). id columns still render as strings and a missing id still becomes
    None (never the string "None"); a non-id column's value -- masked or raw, whichever
    app/core/data.py's own masking left it as -- passes through byte-for-byte, since _dim_records
    itself never masks anything. Every dims.* column is VARCHAR at the source (never a numeric
    dtype), so the id columns here are strings going in too -- str(val) never needs to strip a
    pandas float artifact like "1111.0"."""
    df = pd.DataFrame({
        "workspace_id": ["1111", "2222", None],
        "job_id": ["10", "20", "30"],
        "run_as_user_name": ["ab12cd34 pe***", float("nan"), "person@example.com"],
    })
    records = api_app._dim_records(df, ("workspace_id", "job_id"))
    assert records[0] == {"workspace_id": "1111", "job_id": "10", "run_as_user_name": "ab12cd34 pe***"}
    assert records[1] == {"workspace_id": "2222", "job_id": "20", "run_as_user_name": None}
    assert records[2] == {"workspace_id": None, "job_id": "30", "run_as_user_name": "person@example.com"}


def test_dim_records_empty_df_is_empty_list():
    df = pd.DataFrame({"workspace_id": [], "job_id": []})
    assert api_app._dim_records(df, ("workspace_id", "job_id")) == []


def test_dims_job_has_name_and_owner_columns_and_string_ids():
    r = client.get("/api/dims")
    jobs = r.json()["jobs"]
    job = jobs[0]
    assert isinstance(job["workspace_id"], str)  # PLAN.md 6.2: "IDs are strings"
    assert isinstance(job["job_id"], str)
    assert "name" in job
    assert "run_as" in job
    assert "run_as_user_name" in job
    assert "creator_user_name" in job


def test_dims_job_owner_columns_are_masked_when_the_setting_is_on(mask_users_on):
    jobs = client.get("/api/dims").json()["jobs"]
    for j in jobs:
        _assert_identity_shaped(j["run_as"], allow_bare_user_id=True)
        _assert_identity_shaped(j["run_as_user_name"])
        _assert_identity_shaped(j["creator_user_name"])


def test_dims_cluster_and_warehouse_carry_owner_columns():
    r = client.get("/api/dims")
    body = r.json()
    cluster = body["clusters"][0]
    assert "owned_by" in cluster
    assert isinstance(cluster["cluster_id"], str)
    warehouse = body["warehouses"][0]
    assert "created_by" in warehouse
    assert isinstance(warehouse["warehouse_id"], str)


def test_dims_cluster_and_warehouse_owner_columns_are_masked_when_the_setting_is_on(mask_users_on):
    body = client.get("/api/dims").json()
    for c in body["clusters"]:
        _assert_identity_shaped(c["owned_by"])
    for w in body["warehouses"]:
        _assert_identity_shaped(w["created_by"])


def test_dims_pipeline_carries_owner_columns():
    r = client.get("/api/dims")
    pipelines = r.json()["pipelines"]
    pipeline = pipelines[0]
    assert "created_by" in pipeline
    assert "run_as" in pipeline
    assert isinstance(pipeline["pipeline_id"], str)


def test_dims_pipeline_owner_columns_are_masked_when_the_setting_is_on(mask_users_on):
    pipelines = client.get("/api/dims").json()["pipelines"]
    for p in pipelines:
        _assert_identity_shaped(p["created_by"])
        _assert_identity_shaped(p["run_as"])


def test_dims_identities_are_raw_by_default():
    """privacy.mask_user_identities is off by default, so GET /api/dims returns the same identity
    values dims.dim_job/dim_cluster carry raw -- no hash, no '***' suffix."""
    con = duckdb.connect(str(app_core_data._db_path()), read_only=True)
    try:
        raw_jobs = con.execute(
            "SELECT workspace_id, job_id, run_as, run_as_user_name, creator_user_name "
            "FROM dims.dim_job"
        ).fetchall()
    finally:
        con.close()
    assert raw_jobs, "need dim_job rows for this assertion to mean anything"
    assert any(v and "@" in v for row in raw_jobs for v in (row[3], row[4])), (
        "fixture has no raw email to prove the pass-through against"
    )
    jobs_by_key = {(j["workspace_id"], j["job_id"]): j for j in client.get("/api/dims").json()["jobs"]}
    for workspace_id, job_id, run_as, run_as_user_name, creator_user_name in raw_jobs:
        got = jobs_by_key.get((str(workspace_id), str(job_id)))
        if got is None:
            continue
        assert got["run_as"] == run_as
        assert got["run_as_user_name"] == run_as_user_name
        assert got["creator_user_name"] == creator_user_name


def test_dims_identities_match_dec_66_3_against_the_raw_dims(mask_users_on):
    """End-to-end, with privacy.mask_user_identities on: read dims.dim_job/dim_cluster/
    dim_warehouse/dim_pipeline's RAW columns straight off the built db (bypassing app/core/data.py's
    own masking entirely, the same direct-duckdb style mutated_db/flood_db above already use),
    independently recompute each one's expected DEC-66.3 mask, and check GET /api/dims's row for
    that same id agrees -- proving the values reaching the browser are a genuine function of the
    real identity, not just shaped like one."""
    con = duckdb.connect(str(app_core_data._db_path()), read_only=True)
    try:
        raw_jobs = con.execute(
            "SELECT workspace_id, job_id, run_as, run_as_user_name, creator_user_name "
            "FROM dims.dim_job"
        ).fetchall()
        raw_clusters = con.execute("SELECT cluster_id, owned_by FROM dims.dim_cluster").fetchall()
    finally:
        con.close()
    assert raw_jobs, "need dim_job rows for this assertion to mean anything"
    assert raw_clusters, "need dim_cluster rows for this assertion to mean anything"
    # At least one raw value must be a real, unmasked identity (not already NULL/GUID/REDACTED) --
    # otherwise this test would pass trivially even with masking silently disabled. dim_job's own
    # `run_as` column happens to be NULL on every fixture row (tests/fixtures/lakeflow.py's
    # job_row() never sets it -- only run_as_user_name/creator_user_name are populated), so this
    # checks all three identity columns, not just run_as alone.
    assert any(
        v and "@" in v for row in raw_jobs for v in (row[2], row[3], row[4])
    ), "fixture has no raw email to prove masking against"

    body = client.get("/api/dims").json()
    jobs_by_key = {(j["workspace_id"], j["job_id"]): j for j in body["jobs"]}
    for workspace_id, job_id, run_as, run_as_user_name, creator_user_name in raw_jobs:
        got = jobs_by_key.get((str(workspace_id), str(job_id)))
        if got is None:
            continue
        expected_run_as, expected_run_as_user_name = _expected_run_as_pair(run_as, run_as_user_name)
        assert got["run_as"] == expected_run_as
        assert got["run_as_user_name"] == expected_run_as_user_name
        assert got["creator_user_name"] == _expected_identity(creator_user_name)

    clusters_by_id = {c["cluster_id"]: c for c in body["clusters"]}
    for cluster_id, owned_by in raw_clusters:
        got = clusters_by_id.get(str(cluster_id))
        if got is None:
            continue
        assert got["owned_by"] == _expected_identity(owned_by)


# -------------------------------------------------------------------------------------------
# GET /api/coverage -- T-58's Coverage & Gaps tab feed.
# -------------------------------------------------------------------------------------------


def test_coverage_endpoint_shape():
    r = client.get("/api/coverage")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {
        "export_mode", "manifest_available", "as_of", "as_of_date", "tables",
        "run_results_available", "generated_at", "models", "checks_not_ok",
        "truncated_query_ids", "library_corrections", "region",
    }
    assert body["manifest_available"] is True  # tests/conftest.py synthesises a full manifest
    assert body["export_mode"] == "snapshot"
    assert body["run_results_available"] is True
    assert body["truncated_query_ids"] == []  # only a direct export can hit a row cap
    n = sum(1 for s in registry.load_registry() if s.executable)
    assert len(body["models"]) == n


def test_coverage_tables_carry_dependent_query_ids():
    r = client.get("/api/coverage")
    tables = r.json()["tables"]
    # Every source cost_by_job reads (system.billing.usage) must list cost_by_job as a dependent,
    # and every source referenced by the registry must appear even if the manifest itself has no
    # real entry for it (the "not captured in this snapshot" synthetic row, app/api/app.py).
    billing_usage = tables["system.billing.usage"]
    assert "cost_by_job" in billing_usage["dependent_query_ids"]
    assert billing_usage["state"] == "ok"
    assert len(tables) >= 40  # the 47-source contract minus a few schemas no query reads alone


def test_coverage_unavailable_manifest_reports_not_assessed(monkeypatch, tmp_path):
    # A manifest path pointing nowhere (T-58's own "no snapshot captured yet" case, the default
    # state of a dev checkout that has never run tools/snapshot.py) -- every source still shows
    # up (via the registry-derived fallback), each state "not_assessed", never silently dropped.
    monkeypatch.setenv("AUDIT_SNAPSHOT_MANIFEST", str(tmp_path / "does-not-exist.json"))
    r = client.get("/api/coverage")
    body = r.json()
    assert body["manifest_available"] is False
    assert len(body["tables"]) > 0
    assert all(info["state"] == "not_assessed" for info in body["tables"].values())


# A loader-built direct export (tools/load_direct_results.py): no snapshot_manifest.json at all,
# a main.direct_export_meta/direct_export_findings_meta pair instead. DIRECT_DROPPED_QUERY_IDS
# mirror a real Databricks account with data_classification off -- every one of them reads
# system.data_classification.results and nothing else, so dropping their own finding tables and
# flipping run_results to the export's own TABLE_OR_VIEW_NOT_FOUND text reproduces the exact
# all-failed-on-one-source shape the fix targets, without touching any other check's own tables.
DIRECT_DROPPED_QUERY_IDS = (
    "access_classification_coverage", "access_classified_unmasked",
    "access_data_classification_inventory", "access_sensitive_table_reads",
)
DIRECT_TABLE_NOT_FOUND_MESSAGE = "TABLE_OR_VIEW_NOT_FOUND: system.data_classification.results"


@pytest.fixture(scope="module")
def direct_export_db(tmp_path_factory):
    src = app_core_data._db_path()
    dest = tmp_path_factory.mktemp("api_direct_export_db") / "direct.duckdb"
    shutil.copy2(src, dest)
    con = duckdb.connect(str(dest))
    try:
        for qid in DIRECT_DROPPED_QUERY_IDS:
            con.execute(f'DROP TABLE IF EXISTS findings."f_{qid}"')
        con.execute(
            "CREATE TABLE main.direct_export_meta (as_of VARCHAR, git_commit VARCHAR, "
            "tool_version VARCHAR, catalog VARCHAR, max_rows BIGINT, windows VARCHAR, "
            "window_coverage VARCHAR)"
        )
        con.execute(
            "INSERT INTO main.direct_export_meta VALUES "
            "('2026-09-25T00:00:00Z', NULL, 'test', NULL, 500000, '[30]', NULL)"
        )
        con.execute(
            "CREATE TABLE main.direct_export_findings_meta "
            "(query_id VARCHAR, truncated BOOLEAN, max_rows BIGINT, truncated_windows VARCHAR)"
        )
        con.execute(
            "INSERT INTO main.direct_export_findings_meta VALUES (?, TRUE, 500000, '[]')",
            [PLAIN_QUERY_ID],
        )
    finally:
        con.close()
    return dest


@pytest.fixture
def direct_export_env(direct_export_db, tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_DB", str(direct_export_db))
    monkeypatch.setenv("AUDIT_SNAPSHOT_MANIFEST", str(tmp_path / "does-not-exist.json"))
    src = Path(os.environ["AUDIT_RUN_RESULTS"])
    data = json.loads(src.read_text(encoding="utf-8"))
    for result in data["results"]:
        uid = result.get("unique_id", "")
        if any(uid.endswith(f".f_{qid}") for qid in DIRECT_DROPPED_QUERY_IDS):
            result["status"] = "error"
            result["message"] = DIRECT_TABLE_NOT_FOUND_MESSAGE
    dest = tmp_path / "run_results_direct.json"
    dest.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("AUDIT_RUN_RESULTS", str(dest))


def test_coverage_direct_mode_reports_export_mode(direct_export_env):
    r = client.get("/api/coverage")
    body = r.json()
    assert body["export_mode"] == "direct"
    assert body["manifest_available"] is False


def test_coverage_direct_mode_derives_state_from_run_results_not_a_missing_manifest(direct_export_env):
    tables = client.get("/api/coverage").json()["tables"]
    # Every reading check failed on this one source -- "not_assessed", with the export's own
    # plain reason, never a row count it never read.
    missing = tables["system.data_classification.results"]
    assert missing["state"] == "not_assessed"
    assert missing["reason"] == "table_not_found"
    assert missing["rows"] is None
    # A source most checks still read fine is "ok" -- a missing manifest must never drag every
    # source down to not_assessed, and its rows are a real, positive count, not a null placeholder.
    ok_source = tables["system.billing.usage"]
    assert ok_source["state"] == "ok"
    assert isinstance(ok_source["rows"], int) and ok_source["rows"] > 0


def test_coverage_checks_not_ok_grouped_by_reason_with_a_fix(direct_export_env):
    checks = client.get("/api/coverage").json()["checks_not_ok"]
    by_id = {c["query_id"]: c for c in checks}
    for qid in DIRECT_DROPPED_QUERY_IDS:
        # not_assessed_status()'s own code, the same one /api/findings' not_assessed.code carries
        # for the identical run_results error -- "table_not_found" is the underlying sub-reason
        # that picked this reason's label/fix, not the code itself any more (C28).
        assert by_id[qid]["reason"] == "build_failed"
        assert by_id[qid]["reason_label"] == "system table not enabled"
        assert by_id[qid]["message"] == DIRECT_TABLE_NOT_FOUND_MESSAGE
        assert by_id[qid]["fix"]  # a plain instruction, never blank
    # A check that built cleanly (even if it later hit its own row cap) is not a couldn't-check.
    assert PLAIN_QUERY_ID not in by_id


NOT_BUILT_QUERY_ID = "compute_warehouse_idle_minutes"  # its finding table is dropped below


@pytest.fixture(scope="module")
def not_built_db(tmp_path_factory):
    """A copy of the session db with NOT_BUILT_QUERY_ID's own finding table dropped.
    not_assessed_status()'s `not_built` code only fires once the table itself is gone
    (table_exists is not True, C28) -- a run_results.json entry alone going missing is equally
    true after a selective dbt build and is not by itself evidence the table is missing, so this
    scenario needs the table dropped for real, not just its run_results entry removed."""
    src = app_core_data._db_path()
    dest = tmp_path_factory.mktemp("api_not_built_db") / "not_built.duckdb"
    shutil.copy2(src, dest)
    con = duckdb.connect(str(dest))
    try:
        con.execute(f'DROP TABLE IF EXISTS findings."f_{NOT_BUILT_QUERY_ID}"')
    finally:
        con.close()
    return dest


@pytest.fixture
def not_built_and_error_env(not_built_db, tmp_path, monkeypatch):
    """One query_id's finding table dropped and its run_results entry removed (genuinely never
    built) and another flipped to a real dbt error -- not_assessed_status()'s two codes, the ones
    /api/findings and /api/coverage must both name the same way (C28)."""
    monkeypatch.setenv("AUDIT_DB", str(not_built_db))
    src = Path(os.environ["AUDIT_RUN_RESULTS"])
    data = json.loads(src.read_text(encoding="utf-8"))
    missing_uid = f"model.databricks_audit.f_{NOT_BUILT_QUERY_ID}"
    error_uid = f"model.databricks_audit.f_{ERROR_STATUS_QUERY_ID}"
    kept = []
    found_error = False
    for result in data["results"]:
        if result.get("unique_id") == missing_uid:
            continue
        if result.get("unique_id") == error_uid:
            result["status"] = "error"
            result["message"] = 'Binder Error: column "bogus_column" does not exist'
            found_error = True
        kept.append(result)
    data["results"] = kept
    assert found_error, f"{error_uid} missing from the session's synthesised run_results.json"
    dest = tmp_path / "run_results_not_built_and_error.json"
    dest.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("AUDIT_RUN_RESULTS", str(dest))


def test_findings_and_coverage_name_not_assessed_checks_the_same_way(not_built_and_error_env):
    findings = {r["query_id"]: r for r in client.get("/api/findings", params={"window": 30}).json()["findings"]}
    coverage = {c["query_id"]: c for c in client.get("/api/coverage").json()["checks_not_ok"]}

    for qid, code in ((NOT_BUILT_QUERY_ID, "not_built"), (ERROR_STATUS_QUERY_ID, "build_failed")):
        row_na = findings[qid]["not_assessed"]
        assert row_na is not None and row_na["code"] == code
        assert coverage[qid]["reason"] == code
        assert coverage[qid]["reason_label"] == row_na["label"]


def test_coverage_truncated_query_ids_only_in_direct_mode(direct_export_env):
    assert client.get("/api/coverage").json()["truncated_query_ids"] == [PLAIN_QUERY_ID]


def test_coverage_truncated_query_ids_empty_on_an_ordinary_build():
    assert client.get("/api/coverage").json()["truncated_query_ids"] == []


def test_coverage_reason_classifies_no_message_as_not_exported():
    # A check absent from run_results entirely (added to the registry after the export ran) has
    # no error text to classify -- "not_exported", never the catch-all "unknown".
    assert app_core_data.coverage_reason(None) == "not_exported"
    assert app_core_data.coverage_reason("PERMISSION_DENIED: user lacks SELECT") == "no_grant"
    assert app_core_data.coverage_reason("connection timed out after 30s") == "timeout"
    assert app_core_data.coverage_reason("something inexplicable happened") == "unknown"


# -------------------------------------------------------------------------------------------
# T-75A (DEC-66.2): the library-corrections register, attached to /api/findings,
# /api/finding/{id} and /api/coverage.
# -------------------------------------------------------------------------------------------


def test_coverage_carries_the_whole_library_corrections_register():
    # review round 1 (must_fix 5): the real register's content (which query_id carries an entry,
    # and that entry's status/item) is expected to change as items land -- assert SHAPE against
    # service.all_library_corrections(), never a literal status/item pinned to one query_id.
    r = client.get("/api/coverage")
    body = r.json()
    expected = service.all_library_corrections()
    assert body["library_corrections"] == expected
    assert len(expected) > 0  # the register is never empty in this repo
    for entry in expected:
        assert set(entry.keys()) == {
            "id", "query_id", "query_title", "problem", "effect", "status", "fix", "item",
        }
        assert entry["status"] in app_library_corrections.STATUS_VALUES
        assert entry["query_title"]  # a real title from the registry, never None for a known query


def test_findings_list_library_corrections_field_present_and_empty_by_default():
    r = client.get("/api/findings", params={"window": 30})
    rows = r.json()["findings"]
    # Every row carries the field, whether or not the register has an entry for it.
    assert all("library_corrections" in row for row in rows)
    by_query_id = app_library_corrections.by_query_id()
    # review round 1 (must_fix 5): pick a query_id the CURRENT register carries no entry for, at
    # runtime, rather than assuming PLAIN_QUERY_ID always has none -- the register's own content
    # is expected to change as items land.
    unlisted = next((f for f in rows if f["query_id"] not in by_query_id), None)
    if unlisted is None:
        pytest.skip("every query_id on this findings list currently carries a register entry")
    assert unlisted["library_corrections"] == []


def test_findings_list_library_corrections_field_carries_known_entry():
    # review round 1 (must_fix 5): assert against service.library_corrections_for(qid) for a
    # query_id the CURRENT register actually carries an entry for, never a literal status/item.
    by_query_id = app_library_corrections.by_query_id()
    assert by_query_id, "the register is never empty in this repo"
    qid = next(iter(by_query_id))
    expected = service.library_corrections_for(qid)
    r = client.get("/api/findings", params={"window": 30})
    rows = r.json()["findings"]
    row = next((f for f in rows if f["query_id"] == qid), None)
    if row is None:
        pytest.skip(f"{qid} is not on the /api/findings list at this window/filters")
    assert row["library_corrections"] == expected
    assert len(row["library_corrections"]) == len(by_query_id[qid])


def test_finding_detail_library_corrections_field_present_on_ok_rows():
    r = client.get("/api/finding/cost_actual_vs_list_by_sku", params={"window": 30, "limit": 3})
    body = r.json()
    assert body["outcome"] == "ok_rows"
    assert body["library_corrections"] == service.library_corrections_for("cost_actual_vs_list_by_sku")
    assert len(body["library_corrections"]) == 1
    assert body["library_corrections"][0]["query_id"] == "cost_actual_vs_list_by_sku"


def test_finding_detail_library_corrections_field_present_on_not_assessed(missing_table_env):
    r = client.get(f"/api/finding/{MISSING_TABLE_QUERY_ID}", params={"window": 30})
    body = r.json()
    assert body["outcome"] == "not_assessed"
    # T-75A (DEC-66.2): present on a not_assessed outcome too, not just ok_rows -- whatever the
    # register currently carries for this query_id (review round 1, must_fix 5: the register's own
    # content is expected to change as items land, so this is never a literal `== []`).
    assert body["library_corrections"] == service.library_corrections_for(MISSING_TABLE_QUERY_ID)


# -------------------------------------------------------------------------------------------
# tier / stars are query-level tags (T-58 addendum), surfaced on every findings-list row and
# every finding-detail header -- never a lite/standard/deep structural split (no gating, no
# separate sections; the addendum is explicit that this is a label, not an architecture).
# -------------------------------------------------------------------------------------------


def test_findings_list_carries_tier_and_stars_tags():
    r = client.get("/api/findings", params={"window": 30})
    rows = r.json()["findings"]
    assert all(row["tier"] in ("lite", "standard", "deep") for row in rows)
    assert all(isinstance(row["stars"], bool) for row in rows)
    starred = next(f for f in rows if f["query_id"] == "compute_warehouse_idle_gaps")
    assert starred["stars"] is True
    assert starred["tier"] == "standard"


def test_finding_detail_header_carries_tier_and_stars():
    r = client.get(f"/api/finding/{PLAIN_QUERY_ID}", params={"window": 30})
    header = r.json()["header"]
    assert header["tier"] in ("lite", "standard", "deep")
    assert isinstance(header["stars"], bool)


# -------------------------------------------------------------------------------------------
# GET /api/findings
# -------------------------------------------------------------------------------------------


def test_findings_list_default_window_and_status_counts():
    n = sum(1 for s in registry.load_registry() if s.executable)
    r = client.get("/api/findings", params={"window": 30})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == len(body["findings"]) == n

    row = next(f for f in body["findings"] if f["query_id"] == PLAIN_QUERY_ID)
    assert row["outcome"] == "ok_rows"
    assert row["is_finding"] is True
    assert row["row_count"] > 0
    # A verified breakdown (app.core.data.finding_window_counts' own SQL GROUP BY): the four
    # status counts sum to exactly the reported row_count, never a tally over a capped slice.
    assert set(row["status_counts"]) <= {"CRITICAL", "WARN", "OK", "NOT_ASSESSED"}
    assert sum(row["status_counts"].values()) == row["row_count"]


def test_findings_list_inventory_query_has_no_status_band():
    r = client.get("/api/findings", params={"window": 30})
    row = next(f for f in r.json()["findings"] if f["query_id"] == "classic_clusters_config_current")
    assert row["is_finding"] is False
    assert row["status_counts"] is None
    assert row["outcome"] == "ok_rows"


def test_findings_list_omitted_window_uses_settings_default():
    settings = app_config.load_settings()
    r = client.get("/api/findings")
    assert r.status_code == 200
    assert r.json()["window_days"] == settings["default_window"]


def test_findings_list_bad_window_422_not_500():
    r = client.get("/api/findings", params={"window": 14})
    assert r.status_code == 422


# -------------------------------------------------------------------------------------------
# All workspaces selected by default (explicit requirement).
# -------------------------------------------------------------------------------------------


def test_all_workspaces_selected_by_default():
    n = sum(1 for s in registry.load_registry() if s.executable)
    ws = client.get("/api/workspaces").json()
    all_ids = sorted(w["workspace_id"] for w in ws)
    assert all_ids == sorted(ALL_WORKSPACE_IDS)

    default = client.get("/api/findings", params={"window": 30}).json()
    explicit = client.get("/api/findings", params={"window": 30, "workspace_ids": all_ids}).json()
    assert default["count"] == explicit["count"] == n

    default_counts = {f["query_id"]: f["row_count"] for f in default["findings"]}
    explicit_counts = {f["query_id"]: f["row_count"] for f in explicit["findings"]}

    # "No filter" (app/web's own definition of "every workspace selected") is >= an explicit
    # workspace_ids IN (...) list for every query, never less -- and strictly greater for a query
    # with an account-level (workspace_id IS NULL) row, which an explicit IN-list can never match.
    assert all(default_counts[q] >= explicit_counts[q] for q in default_counts)
    assert default_counts[ACCOUNT_WIDE_QUERY_ID] > explicit_counts[ACCOUNT_WIDE_QUERY_ID]
    # A query with no account-wide row agrees exactly either way.
    assert default_counts[NO_ACCOUNT_WIDE_QUERY_ID] == explicit_counts[NO_ACCOUNT_WIDE_QUERY_ID]

    # Restricting to one workspace must never show MORE than the all-workspaces default.
    one_ws = client.get("/api/findings", params={"window": 30, "workspace_ids": [all_ids[0]]}).json()
    one_counts = {f["query_id"]: f["row_count"] for f in one_ws["findings"]}
    assert all(one_counts[q] <= default_counts[q] for q in one_counts)


# -------------------------------------------------------------------------------------------
# GET /api/finding/{query_id} -- outcome: ok_rows
# -------------------------------------------------------------------------------------------


def test_finding_detail_ok_rows():
    r = client.get(f"/api/finding/{PLAIN_QUERY_ID}", params={"window": 30, "limit": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "ok_rows"
    assert body["window_days"] == 30
    assert len(body["rows"]) == 5
    assert body["returned"] == 5
    assert body["rows_total"] > 5

    header = body["header"]
    for key in ("read_this", "healthy", "investigate_if", "actions", "next", "caveats"):
        assert key in header
    assert header["read_this"]
    assert isinstance(header["actions"], list)
    assert isinstance(header["next"], list)

    cols = {c["name"]: c for c in body["columns"]}
    assert cols["workspace_id"]["kind"] == "id"
    assert isinstance(body["rows"][0]["workspace_id"], str)  # never a bare/float id
    assert body["order_by"] is None or isinstance(body["order_by"], str)


def test_finding_detail_pagination_offset():
    page1 = client.get(f"/api/finding/{PLAIN_QUERY_ID}", params={"window": 30, "limit": 5, "offset": 0}).json()
    page2 = client.get(f"/api/finding/{PLAIN_QUERY_ID}", params={"window": 30, "limit": 5, "offset": 5}).json()
    assert page1["offset"] == 0 and page2["offset"] == 5
    assert page1["rows"] != page2["rows"]
    assert page1["rows_total"] == page2["rows_total"]


def test_finding_detail_money_label_reflects_discount(monkeypatch, tmp_path):
    """Money columns keep the "list price (effective)" / "what-if: ..." labelling semantics
    (app/core/pricing.py): the raw (list-price) column always carries the 0% default, and only
    its "_disc" twin carries the account's real discount_pct as a what-if."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    monkeypatch.setenv("AUDIT_CONFIG_DIR", str(cfg_dir))
    settings = dict(app_config.DEFAULT_SETTINGS, ui=dict(app_config.DEFAULT_SETTINGS["ui"]))
    settings["discount_pct"] = 0.2
    app_config.save_settings(settings)

    r = client.get("/api/finding/cost_actual_vs_list_by_sku", params={"window": 30, "limit": 3})
    body = r.json()
    assert body["outcome"] == "ok_rows"
    assert body["discount_pct"] == 0.2
    cols = {c["name"]: c for c in body["columns"]}
    assert cols["net_list_cost"]["kind"] == "money"
    assert cols["net_list_cost"]["label"] == "list price (effective)"
    assert cols["net_list_cost_disc"]["kind"] == "money"
    assert cols["net_list_cost_disc"]["label"] == "what-if: list price (effective), -20%"
    # DEC-66.1/T-69A (integrator pass): cost_actual_vs_list_by_sku used to carry a second,
    # own-basis dollar column (net_default_cost) here too, but T-69A dropped it entirely -- the
    # query now emits only net_list_cost, and always reads NOT_ASSESSED (no negotiated-rate
    # source exists anywhere in system.billing; see config/library_corrections.yml). Every dollar
    # column in this app is now the one list-price basis, so NON_DISCOUNT_MONEY_LABELS
    # (app/core/pricing.py) is empty and no real finding exercises its own-basis label path any
    # more; that path is unit-tested directly (below) so it stays provably correct for a future
    # dollar column whose basis genuinely is not the list price.
    assert "net_default_cost" not in cols


def test_money_label_own_basis_path_still_works(monkeypatch):
    """app/api/service._money_label's NON_DISCOUNT_MONEY_LABELS branch (DEC-66.1's escape hatch
    for a future dollar column whose basis is not the list price) is unit-tested directly, since
    no live finding exercises it today (see the test above) -- the mechanism itself must stay
    correct even while it is unused."""
    monkeypatch.setitem(service.app_pricing.NON_DISCOUNT_MONEY_LABELS, "net_fake_cost", "as billed")
    assert service._money_label("net_fake_cost", 0.2) == "as billed"
    # pricing.apply() never actually creates a "_disc" twin for an own-basis column (it fails
    # _LIST_COL_RE on purpose, so no real "net_fake_cost_disc" column would ever reach this
    # function) -- but the own-basis lookup strips a "_disc" suffix before it looks up `base`
    # regardless, so it still wins over the list-price branch below if one somehow did.
    assert service._money_label("net_fake_cost_disc", 0.2) == "as billed"


def test_json_scalar_nat_is_null_not_the_literal_text_nat():
    """F5: pd.NaT is not a pd.Timestamp (the isinstance check further down misses it) but IS a
    datetime, so before this fix it fell through to the isinstance(value, (date, datetime))
    branch and rendered as the literal text "NaT" (NaT.isoformat() == "NaT") in an empty
    timestamp cell -- e.g. a never-run job's last_run_start."""
    assert service._json_scalar(service.pd.NaT) is None


# -------------------------------------------------------------------------------------------
# GET /api/finding/{query_id} -- outcome: ok_empty_filters
# -------------------------------------------------------------------------------------------


def test_finding_detail_ok_empty_filters():
    r = client.get(f"/api/finding/{PLAIN_QUERY_ID}", params={"window": 30, "workspace_ids": "not-a-real-workspace"})
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "ok_empty_filters"
    assert body["rows_in_window"] > 0
    assert "rows" not in body  # only ok_rows carries a data table


# -------------------------------------------------------------------------------------------
# GET /api/finding/{query_id} -- T-70: the optional job_id filter (the job focus panel's own
# server-side scope, app/core/data._build_filters' job_id clause)
# -------------------------------------------------------------------------------------------


def test_finding_detail_job_id_filter_scopes_to_one_job():
    # lakeflow_job_queue_time's grain is [workspace_id, job_id] (tests/fixtures/lakeflow.py SEC E)
    # -- lf_job_phase_crit is one of four jobs the fixture builds in workspace "1111".
    base = client.get(
        "/api/finding/lakeflow_job_queue_time", params={"window": 30, "workspace_ids": "1111"}
    ).json()
    assert base["outcome"] == "ok_rows"
    assert base["rows_total"] > 1

    r = client.get(
        "/api/finding/lakeflow_job_queue_time",
        params={"window": 30, "workspace_ids": "1111", "job_id": "lf_job_phase_crit"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "ok_rows"
    assert body["rows_total"] == 1
    assert body["returned"] == 1
    assert [row["job_id"] for row in body["rows"]] == ["lf_job_phase_crit"]


def test_finding_detail_job_id_filter_no_match_is_ok_empty_filters():
    # A job_id with no matching row reads ok_empty_filters, exactly like any other filter that
    # excludes every row -- never a false ok_rows with an empty page.
    r = client.get(
        "/api/finding/lakeflow_job_queue_time",
        params={"window": 30, "workspace_ids": "1111", "job_id": "no_such_job_xyz"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "ok_empty_filters"
    assert body["rows_in_window"] > 0
    assert "rows" not in body


def test_finding_detail_job_id_filter_ignored_without_job_id_column():
    # cost_totals_by_sku_day has no job_id column -- job_id must be a silent no-op, never a 500
    # and never a narrowed result.
    base = client.get("/api/finding/cost_totals_by_sku_day", params={"window": 30}).json()
    r = client.get(
        "/api/finding/cost_totals_by_sku_day", params={"window": 30, "job_id": "lf_job_phase_crit"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "ok_rows"
    assert body["rows_total"] == base["rows_total"]


# -------------------------------------------------------------------------------------------
# service.row_cap -- T-70 review round 2: the BUILT :top_n row cap for a query_id, resolved the
# same way dbt/macros/param.sql resolves it at build time (thresholds.yml[query_id]/[_all]['top_n'],
# else the query's own header default). The job focus panel (app/web) uses this, surfaced as
# GET /api/finding's own "row_cap" field, to tell a real "not flagged"/"OK" from one a build-time
# row cap might be hiding.
# -------------------------------------------------------------------------------------------


def test_row_cap_reads_header_defaults():
    # app/queries/vendored/jobs_pipelines/lakeflow_jobs_on_all_purpose.sql: ":top_n (default
    # 100000)" -- T-75B review fixes raised this from 500 (a per-placement finding, not a small
    # inventory; a low build-time cap could silently drop the flagged rows that matter).
    assert service.row_cap("lakeflow_jobs_on_all_purpose") == 100000
    # app/queries/app/jobs_pipelines/lakeflow_job_compute_pressure.sql: ":top_n (default 100000)".
    assert service.row_cap("lakeflow_job_compute_pressure") == 100000
    # app/queries/vendored/performance/query_costly_statements.sql: ":top_n (default 1000)" -- a
    # header default that differs from the two 100000 caps above, so this test still proves row_cap
    # reads EACH query's own header (a constant or wrong-spec lookup would fail here).
    assert service.row_cap("query_costly_statements") == 1000
    # lakeflow_job_queue_time has no :top_n param at all -- no cap, never a guessed 0/None-as-cap.
    assert service.row_cap("lakeflow_job_queue_time") is None
    assert service.row_cap("no_such_query_id") is None


def test_row_cap_honours_thresholds_override(monkeypatch, tmp_path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    monkeypatch.setenv("AUDIT_CONFIG_DIR", str(cfg_dir))
    app_config.save_thresholds({"lakeflow_jobs_on_all_purpose": {"top_n": 5000}})
    assert service.row_cap("lakeflow_jobs_on_all_purpose") == 5000
    # An id with no override of its own still falls through to its own header default.
    assert service.row_cap("lakeflow_job_compute_pressure") == 100000


def test_finding_detail_row_cap_field_on_ok_rows():
    r = client.get("/api/finding/lakeflow_jobs_on_all_purpose", params={"window": 30})
    body = r.json()
    assert body["outcome"] == "ok_rows"
    assert body["row_cap"] == 100000


def test_finding_detail_row_cap_field_on_ok_empty_filters():
    # cost_by_job (PLAIN_QUERY_ID) has no :top_n param -- row_cap must still be present, as None,
    # never simply absent from the body.
    r = client.get(f"/api/finding/{PLAIN_QUERY_ID}", params={"window": 30, "workspace_ids": "not-a-real-workspace"})
    body = r.json()
    assert body["outcome"] == "ok_empty_filters"
    assert "row_cap" in body
    assert body["row_cap"] is None


# -------------------------------------------------------------------------------------------
# GET /api/finding/{query_id} -- outcome: ok_empty_window
# -------------------------------------------------------------------------------------------


def test_finding_detail_ok_empty_window(empty_window_env):
    r = client.get(f"/api/finding/{EMPTY_WINDOW_QUERY_ID}", params={"window": EMPTY_WINDOW_AT})
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "ok_empty_window"
    assert "window_coverage" in body and body["window_coverage"] is not None
    assert "rows" not in body


def test_findings_list_reflects_ok_empty_window(empty_window_env):
    r = client.get("/api/findings", params={"window": EMPTY_WINDOW_AT})
    row = next(f for f in r.json()["findings"] if f["query_id"] == EMPTY_WINDOW_QUERY_ID)
    assert row["outcome"] == "ok_empty_window"
    assert row["row_count"] == 0


# -------------------------------------------------------------------------------------------
# GET /api/finding/{query_id} -- outcome: not_assessed (both sub-paths)
# -------------------------------------------------------------------------------------------


def test_finding_detail_not_assessed_missing_table(missing_table_env):
    """findings.f_<id> physically absent despite a run_results.json that still claims success --
    the FindingNotBuiltError / "not_built_reason" path, distinct from the next test."""
    r = client.get(f"/api/finding/{MISSING_TABLE_QUERY_ID}", params={"window": 30})
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "not_assessed"
    assert "not_built_reason" in body["status_info"]
    assert "rows" not in body


def test_finding_detail_not_assessed_model_error(error_status_env):
    """run_results.json itself reports the model as errored -- the "model/source bad" path."""
    r = client.get(f"/api/finding/{ERROR_STATUS_QUERY_ID}", params={"window": 30})
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "not_assessed"
    assert body["status_info"]["status"] == "error"
    assert body["status_info"]["error_class"]


def test_findings_list_reflects_not_assessed(missing_table_env):
    r = client.get("/api/findings", params={"window": 30})
    row = next(f for f in r.json()["findings"] if f["query_id"] == MISSING_TABLE_QUERY_ID)
    assert row["outcome"] == "not_assessed"
    assert row["row_count"] == 0
    assert row["status_counts"] is None


# -------------------------------------------------------------------------------------------
# GET /api/finding/{query_id} -- outcome: error (never a 500)
# -------------------------------------------------------------------------------------------


def test_finding_detail_error_corrupt_db(corrupt_db_env):
    r = client.get(f"/api/finding/{PLAIN_QUERY_ID}", params={"window": 30})
    assert r.status_code == 200  # a store-level read failure is a 200 + outcome="error", not a 500
    body = r.json()
    assert body["outcome"] == "error"
    assert body["error"]


def test_findings_list_error_on_corrupt_db(corrupt_db_env):
    r = client.get("/api/findings", params={"window": 30})
    assert r.status_code == 200
    outcomes = {f["outcome"] for f in r.json()["findings"]}
    assert outcomes == {"error"}


# -------------------------------------------------------------------------------------------
# Unknown query_id -> 404, never a 500.
# -------------------------------------------------------------------------------------------


def test_finding_detail_unknown_query_id_404():
    r = client.get("/api/finding/does_not_exist_xyz", params={"window": 30})
    assert r.status_code == 404
    assert "does_not_exist_xyz" in r.json()["detail"]


def test_finding_detail_bad_window_422():
    r = client.get(f"/api/finding/{PLAIN_QUERY_ID}", params={"window": 14})
    assert r.status_code == 422


# -------------------------------------------------------------------------------------------
# Static front end -- served from the same process, never shadowing an /api/* route.
# -------------------------------------------------------------------------------------------


def test_static_index_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert 'id="root"' in r.text


def test_static_assets_served():
    script = re.search(r'src="(/assets/[^"]+[.]js)"', client.get("/").text)
    assert script
    r = client.get(script.group(1))
    assert r.status_code == 200
    assert int(r.headers["content-length"]) > 1000
    assert r.headers["content-type"].startswith("text/javascript")
    sheet = re.search(r'href="(/assets/[^"]+[.]css)"', client.get("/").text)
    assert sheet
    assert client.get(sheet.group(1)).headers["content-type"].startswith("text/css")


def test_any_other_page_path_opens_the_app():
    r = client.get("/some/page")
    assert r.status_code == 200
    assert 'id="root"' in r.text


def test_unknown_api_path_is_404_not_the_page():
    r = client.get("/api/no_such_route")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


def test_api_routes_not_shadowed_by_static_mount():
    r = client.get("/api/meta")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")


def test_missing_metadata_is_unknown_not_not_assessed(monkeypatch, tmp_path):
    """Observed on a real account 2026-09-22: a zip re-extracted without the git-ignored
    snapshot/ and dbt/target/ folders showed "0/107 models ok" and every finding as a grey
    NOT_ASSESSED card -- against a database that plainly held 157 workspaces and full data.

    The cause was that absent metadata was treated as evidence of failure: no run_results.json
    made every model read "not_built", and no manifest.json made every source read blocked, both
    decided BEFORE the table was ever queried. NOT_ASSESSED must mean "we know we could not
    look", never "we have no metadata". A genuinely missing table still reports NOT_ASSESSED,
    because that is evidence -- it comes from FindingNotBuiltError, not from a missing json."""
    monkeypatch.setenv("AUDIT_SNAPSHOT_MANIFEST", str(tmp_path / "no_manifest.json"))
    monkeypatch.setenv("AUDIT_RUN_RESULTS", str(tmp_path / "no_run_results.json"))

    client = TestClient(app)
    listed = client.get("/api/findings?window=7").json()
    rows = listed if isinstance(listed, list) else listed["findings"]
    outcomes = {r["outcome"] for r in rows}
    assert "ok_rows" in outcomes, (
        "with no metadata the built tables must speak for themselves; got only "
        f"{outcomes}"
    )
    assert outcomes != {"not_assessed"}, "every finding NOT_ASSESSED is the bug this test exists for"

    detail = client.get("/api/finding/overview_dbu_by_sku?window=7").json()
    assert detail["outcome"] == "ok_rows"
    assert detail["rows"], "the detail endpoint must return the rows that are actually there"


# -------------------------------------------------------------------------------------------
# GET /api/finding/{query_id} -- T-68: the optional `status` filter (app/core/data._build_filters'
# new status clause). cost_by_job carries a real CRITICAL/WARN/OK status column (a field-heuristic
# magnitude band, cost_by_job.sql), so every assertion below is against a live, non-fixture-
# specific invariant rather than a hardcoded count.
# -------------------------------------------------------------------------------------------


def test_finding_detail_status_filter_rows_all_match_requested_status():
    r = client.get(f"/api/finding/{PLAIN_QUERY_ID}", params={"window": 30, "status": "CRITICAL", "limit": 100})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == ["CRITICAL"]
    if body["outcome"] == "ok_rows":
        assert body["rows"], "ok_rows must carry at least one row"
        assert all(row["status"] == "CRITICAL" for row in body["rows"])
    else:
        # No CRITICAL row exists in this window on the session fixture -- the filter excluding
        # every row is ok_empty_filters, same honesty-rule branch any other filter takes.
        assert body["outcome"] == "ok_empty_filters"


def test_finding_detail_status_filter_matches_bulk_status_counts():
    # /api/findings' own status_counts is a verified SQL GROUP BY over every row the build judged
    # (service.list_findings' own docstring) -- the exact ground truth the status-filtered read
    # must agree with, never a hardcoded number.
    findings = client.get("/api/findings", params={"window": 30}).json()["findings"]
    row = next(f for f in findings if f["query_id"] == PLAIN_QUERY_ID)
    sc = row["status_counts"] or {}
    flagged_total = sc.get("CRITICAL", 0) + sc.get("WARN", 0)

    r = client.get(
        f"/api/finding/{PLAIN_QUERY_ID}",
        params={"window": 30, "status": ["CRITICAL", "WARN"], "limit": 5000},
    )
    body = r.json()
    if flagged_total == 0:
        assert body["outcome"] == "ok_empty_filters"
    else:
        assert body["outcome"] == "ok_rows"
        assert body["rows_total"] == flagged_total
        assert all(row["status"] in ("CRITICAL", "WARN") for row in body["rows"])


def test_finding_detail_status_filter_bad_value_422():
    r = client.get(f"/api/finding/{PLAIN_QUERY_ID}", params={"window": 30, "status": "BOGUS_STATUS"})
    assert r.status_code == 422


def test_finding_detail_status_filter_ignored_without_status_column():
    # overview_dbu_by_sku is a pure inventory id (no status column, T-58's own 45-inventory-id
    # set) -- status must be a silent no-op, the same "unaffected, never silently empty" contract
    # job_id/tag/workspace_ids already carry.
    r = client.get("/api/finding/overview_dbu_by_sku", params={"window": 30, "status": "CRITICAL"})
    assert r.status_code == 200
    assert r.json()["outcome"] == "ok_rows"


# -------------------------------------------------------------------------------------------
# GET /api/finding/{query_id}/aggregate -- T-68's own new endpoint: a server-side SUM/COUNT over
# EVERY row matching the filters, the fix for "a Cost/ML & AI/Overview tile summed only the
# 5,000-row slice the browser fetched". Every numeric assertion below is checked against either a
# direct SQL query against the SAME db (never a hardcoded fixture number) or the independently
# verified GET /api/findings status_counts / GET /api/finding rows_total, so these tests hold
# whatever tests/fixtures/*.py's own row counts happen to be.
# -------------------------------------------------------------------------------------------


def test_finding_aggregate_matches_direct_sql_sum():
    con = duckdb.connect(str(app_core_data._db_path()), read_only=True)
    try:
        expected_total, expected_rows = con.execute(
            f'SELECT sum(net_usage_quantity), count(*) FROM findings."f_{PLAIN_QUERY_ID}" '
            "WHERE window_days = 30"
        ).fetchone()
    finally:
        con.close()

    r = client.get(
        f"/api/finding/{PLAIN_QUERY_ID}/aggregate",
        params={"window": 30, "group": "job_id", "agg": "sum", "value": "net_usage_quantity"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "ok_rows"
    assert body["matched_rows"] == expected_rows
    assert body["rows_total"] == expected_rows  # no status filter -- the two counts must agree
    got_total = sum(g["value"] for g in body["groups"]) + (body["other"]["value"] if body["other"] else 0)
    assert got_total == pytest.approx(expected_total)


# -------------------------------------------------------------------------------------------
# excluded_no_workspace (C1) -- a workspace/env/attribute filter silently drops account-level
# billing (no workspace_id) or a warehouse/cluster missing from its dim; these partitions must add
# back up to the unfiltered total.
# -------------------------------------------------------------------------------------------


def test_excluded_no_workspace_partitions_add_up_to_the_unfiltered_total():
    query_id = "cost_dollarized_by_sku_day"
    value_col = "net_list_cost"
    con = duckdb.connect(str(app_core_data._db_path()), read_only=True)
    try:
        other_ids = [
            r[0] for r in con.execute(
                f'SELECT DISTINCT workspace_id FROM findings."f_{query_id}" '
                "WHERE window_days = 30 AND workspace_id IS NOT NULL AND workspace_id != '0' "
                "AND workspace_id != ?",
                [COST_CENTER_WORKSPACE_ID],
            ).fetchall()
        ]
    finally:
        con.close()
    assert other_ids  # the fixture must have at least one other real workspace to be a real check

    def agg(**params):
        body = client.get(
            f"/api/finding/{query_id}/aggregate",
            params={"window": 30, "agg": "sum", "value": value_col, **params},
        ).json()
        assert body["outcome"] == "ok_rows"
        return body

    unfiltered = agg()
    ws_1111 = agg(workspace_ids=COST_CENTER_WORKSPACE_ID)
    others = agg(workspace_ids=other_ids)

    assert ws_1111["excluded_no_workspace"] is not None
    excluded_value = ws_1111["excluded_no_workspace"]["value"] or 0
    # The excluded set (no resolvable workspace) does not depend on WHICH real workspaces were
    # selected -- only that some workspace/env/attribute filter is active.
    assert others["excluded_no_workspace"]["value"] == pytest.approx(excluded_value)

    total = (ws_1111["total_value"] or 0) + (others["total_value"] or 0) + excluded_value
    assert total == pytest.approx(unfiltered["total_value"])
    assert unfiltered["excluded_no_workspace"] is None  # no filter active -- nothing excluded


def test_excluded_no_workspace_null_on_finding_endpoint():
    r = client.get(
        "/api/finding/cost_dollarized_by_sku_day",
        params={"window": 30, "workspace_ids": COST_CENTER_WORKSPACE_ID, "limit": 5},
    )
    body = r.json()
    assert body["outcome"] == "ok_rows"
    assert body["excluded_no_workspace"] is not None
    assert body["excluded_no_workspace"]["value"] is None  # never aggregated on this endpoint
    assert isinstance(body["excluded_no_workspace"]["rows"], int)


def test_excluded_no_workspace_reports_dim_missing_rows():
    # compute_warehouse_autoscale_churn is filtered through dims.dim_warehouse (no workspace_id of
    # its own) -- a warehouse missing from that dim is excluded the same way a NULL workspace is.
    query_id = "compute_warehouse_autoscale_churn"
    con = duckdb.connect(str(app_core_data._db_path()), read_only=True)
    try:
        rows_total, dim_missing = con.execute(
            f'SELECT count(*), '
            f'sum(CASE WHEN warehouse_id IS NULL OR warehouse_id NOT IN '
            f'(SELECT warehouse_id FROM dims.dim_warehouse) THEN 1 ELSE 0 END) '
            f'FROM findings."f_{query_id}" WHERE window_days = 30',
            [],
        ).fetchone()
    finally:
        con.close()
    if not rows_total or not dim_missing:
        pytest.skip("no dim-missing warehouse rows in this fixture window")

    body = client.get(
        f"/api/finding/{query_id}", params={"window": 30, "env": "prod", "limit": 5},
    ).json()
    assert body["outcome"] in ("ok_rows", "ok_empty_filters")
    assert body["excluded_no_workspace"] is not None
    assert body["excluded_no_workspace"]["rows"] == dim_missing


def test_finding_aggregate_count_agg_ignores_missing_value():
    r = client.get(f"/api/finding/{PLAIN_QUERY_ID}/aggregate", params={"window": 30, "agg": "count"})
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "ok_rows"
    assert len(body["groups"]) == 1  # group=[] -- exactly one (ungrouped) bucket, never zero
    assert body["other"] is None
    assert body["matched_rows"] == body["rows_total"]
    assert body["groups"][0]["value"] == body["rows_total"]  # count(*) == the row total


def test_finding_aggregate_max_per_group_and_overall():
    con = duckdb.connect(str(app_core_data._db_path()), read_only=True)
    try:
        expected = dict(con.execute(
            f'SELECT job_id, max(net_usage_quantity) FROM findings."f_{PLAIN_QUERY_ID}" '
            "WHERE window_days = 30 GROUP BY job_id"
        ).fetchall())
    finally:
        con.close()
    body = client.get(
        f"/api/finding/{PLAIN_QUERY_ID}/aggregate",
        params={"window": 30, "group": "job_id", "agg": "max", "value": "net_usage_quantity", "top": 1},
    ).json()
    assert body["outcome"] == "ok_rows"
    head = body["groups"][0]
    assert head["value"] == pytest.approx(max(v for v in expected.values() if v is not None))
    # The total and the folded rest are maxima too, never sums of maxima.
    assert body["total_value"] == pytest.approx(head["value"])
    if body["other"] is not None:
        assert body["other"]["value"] <= head["value"] + 1e-9


def test_finding_columns_returns_only_those_and_the_status_columns():
    full = client.get(f"/api/finding/{PLAIN_QUERY_ID}", params={"window": 30}).json()
    body = client.get(f"/api/finding/{PLAIN_QUERY_ID}", params={"window": 30, "columns": ["job_id", "no_such_column"]}).json()
    names = [c["name"] for c in body["columns"]]
    assert "job_id" in names
    assert set(names) <= {"job_id", "status", "status_raw", "below_floor", "workspace_id"}
    assert all(set(row) <= set(names) for row in body["rows"])
    assert body["rows_total"] == full["rows_total"]


def test_finding_aggregate_max_without_value_422():
    r = client.get(f"/api/finding/{PLAIN_QUERY_ID}/aggregate", params={"window": 30, "agg": "max"})
    assert r.status_code == 422


def test_finding_aggregate_top_folds_remainder_but_keeps_the_true_total():
    full = client.get(
        f"/api/finding/{PLAIN_QUERY_ID}/aggregate",
        params={"window": 30, "group": "job_id", "agg": "sum", "value": "net_usage_quantity"},
    ).json()
    if full["group_count"] < 2:
        pytest.skip("fixture does not carry enough distinct cost_by_job job_ids to test the top fold")

    topped = client.get(
        f"/api/finding/{PLAIN_QUERY_ID}/aggregate",
        params={"window": 30, "group": "job_id", "agg": "sum", "value": "net_usage_quantity", "top": 1},
    ).json()
    assert len(topped["groups"]) == 1
    assert topped["other"] is not None
    assert topped["group_count"] == full["group_count"]
    assert topped["other"]["row_count"] + topped["groups"][0]["row_count"] == topped["matched_rows"]
    combined = topped["groups"][0]["value"] + topped["other"]["value"]
    assert combined == pytest.approx(topped["total_value"])
    # The true total never changes just because the caller asked for fewer groups back -- this is
    # the exact property T-68 exists to guarantee (a capped/top-N view still reports the WHOLE sum).
    assert topped["total_value"] == pytest.approx(full["total_value"])


def test_finding_aggregate_status_filter_matches_bulk_status_counts():
    findings = client.get("/api/findings", params={"window": 30}).json()["findings"]
    row = next(f for f in findings if f["query_id"] == PLAIN_QUERY_ID)
    sc = row["status_counts"] or {}
    flagged_total = sc.get("CRITICAL", 0) + sc.get("WARN", 0)

    r = client.get(
        f"/api/finding/{PLAIN_QUERY_ID}/aggregate",
        params={"window": 30, "agg": "count", "status": ["CRITICAL", "WARN"]},
    )
    assert r.status_code == 200
    body = r.json()
    if flagged_total == 0:
        # No CRITICAL/WARN row exists -- the status filter excluding every row is
        # ok_empty_filters, exactly as /api/finding's own status filter test asserts (same
        # honesty-rule branch, same load_outcome() call underneath load_aggregate()).
        assert body["outcome"] == "ok_empty_filters"
    else:
        assert body["outcome"] == "ok_rows"
        assert body["matched_rows"] == flagged_total
        assert body["rows_total"] >= flagged_total  # rows_total ignores the status filter


def test_finding_aggregate_status_filter_absent_status_is_ok_empty_filters():
    # A status value that is a real STATUS_ORDER member but has zero rows in this finding's
    # status_counts (never bulk-asserted directly above, since that test skips its branch body
    # when flagged_total == 0) still reads ok_empty_filters on both endpoints -- proves the
    # load_aggregate -> load_outcome(statuses=...) fix independently of which branch the fixture
    # happens to hit.
    findings = client.get("/api/findings", params={"window": 30}).json()["findings"]
    row = next(f for f in findings if f["query_id"] == PLAIN_QUERY_ID)
    sc = row["status_counts"] or {}
    absent = next((s for s in ("CRITICAL", "WARN", "OK", "NOT_ASSESSED") if sc.get(s, 0) == 0), None)
    if absent is None:
        pytest.skip("fixture's cost_by_job carries rows in every status band")

    plain = client.get(
        f"/api/finding/{PLAIN_QUERY_ID}", params={"window": 30, "status": absent, "limit": 5000}
    ).json()
    agg = client.get(
        f"/api/finding/{PLAIN_QUERY_ID}/aggregate", params={"window": 30, "agg": "count", "status": absent}
    ).json()
    assert plain["outcome"] == "ok_empty_filters"
    assert agg["outcome"] == "ok_empty_filters"


def test_finding_aggregate_unknown_group_column_422():
    r = client.get(
        f"/api/finding/{PLAIN_QUERY_ID}/aggregate",
        params={"window": 30, "group": "not_a_real_column_xyz", "agg": "count"},
    )
    assert r.status_code == 422


def test_finding_aggregate_too_many_group_columns_422():
    r = client.get(
        f"/api/finding/{PLAIN_QUERY_ID}/aggregate",
        params={"window": 30, "group": ["workspace_id", "job_id", "status", "usage_date"], "agg": "count"},
    )
    assert r.status_code == 422


def test_finding_aggregate_bad_agg_422():
    r = client.get(f"/api/finding/{PLAIN_QUERY_ID}/aggregate", params={"window": 30, "agg": "average"})
    assert r.status_code == 422


def test_finding_aggregate_sum_without_value_422():
    r = client.get(f"/api/finding/{PLAIN_QUERY_ID}/aggregate", params={"window": 30, "agg": "sum"})
    assert r.status_code == 422


def test_finding_aggregate_bad_status_422():
    r = client.get(
        f"/api/finding/{PLAIN_QUERY_ID}/aggregate",
        params={"window": 30, "agg": "count", "status": "BOGUS_STATUS"},
    )
    assert r.status_code == 422


def test_finding_aggregate_bad_window_422():
    r = client.get(f"/api/finding/{PLAIN_QUERY_ID}/aggregate", params={"window": 14, "agg": "count"})
    assert r.status_code == 422


def test_finding_aggregate_unknown_query_id_404():
    r = client.get("/api/finding/does_not_exist_xyz/aggregate", params={"window": 30, "agg": "count"})
    assert r.status_code == 404


# T-69B review round 1 (must_fix 4): agg="sum" now also reports `null_rows` per group -- how many
# of that group's rows never entered the sum because their `value` column was NULL, the honest
# count behind a tile's "N SKU(s) unpriced" disclosure. tests/fixtures/billing.py's own module
# docstring names bl_SKU_NO_PRICE (SEC A, via bl_u_job_ok) as the one deliberately unpriced SKU in
# the whole fixture -- no system.billing.list_prices row matches it, so cost_dollarized_by_sku_day.
# sql's LEFT JOIN leaves net_list_cost NULL for every SKU-day row it produces for that SKU
# (independently pinned by tests/test_findings/test_cost_priced.py::test_cost_dollarized_by_sku_day,
# which asserts `unpriced["net_list_cost"] is None` for this same SKU/day). Checked against a direct
# SQL count on the SAME db, never a hardcoded row count, same discipline as every other aggregate
# test above.
def test_finding_aggregate_sum_reports_null_rows_for_unpriced_sku():
    con = duckdb.connect(str(app_core_data._db_path()), read_only=True)
    try:
        row_count, priced_count = con.execute(
            'SELECT count(*), count(net_list_cost) FROM findings."f_cost_dollarized_by_sku_day" '
            "WHERE window_days = 30 AND sku_name = 'bl_SKU_NO_PRICE'"
        ).fetchone()
    finally:
        con.close()
    assert row_count > 0, "fixture no longer carries bl_SKU_NO_PRICE at window_days=30"
    expected_null_rows = row_count - priced_count
    assert expected_null_rows > 0, "bl_SKU_NO_PRICE is supposed to be deliberately unpriced"

    r = client.get(
        "/api/finding/cost_dollarized_by_sku_day/aggregate",
        params={"window": 30, "group": "sku_name", "agg": "sum", "value": "net_list_cost"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "ok_rows"
    group = next(g for g in body["groups"] if g["key"] == ["bl_SKU_NO_PRICE"])
    assert group["row_count"] == row_count
    assert group["null_rows"] == expected_null_rows

    # A fully-priced SKU's group reports null_rows == 0 -- proves the field is not just always
    # equal to row_count, only ever set when the sum genuinely skipped a NULL.
    priced_group = next(g for g in body["groups"] if g["key"] and g["key"][0] == "bl_JOBS_COMPUTE_CLASSIC")
    assert priced_group["null_rows"] == 0
    assert priced_group["row_count"] > 0

    # agg="count" never reports a null_rows distinction (row_count already answers "how many").
    r_count = client.get(
        "/api/finding/cost_dollarized_by_sku_day/aggregate",
        params={"window": 30, "group": "sku_name", "agg": "count"},
    )
    count_group = next(g for g in r_count.json()["groups"] if g["key"] == ["bl_SKU_NO_PRICE"])
    assert count_group["null_rows"] == 0


def test_finding_aggregate_ok_empty_window(empty_window_env):
    r = client.get(
        f"/api/finding/{EMPTY_WINDOW_QUERY_ID}/aggregate",
        params={"window": EMPTY_WINDOW_AT, "group": "job_id", "agg": "sum", "value": "net_usage_quantity"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "ok_empty_window"
    assert "groups" not in body


def test_finding_aggregate_not_assessed_missing_table(missing_table_env):
    r = client.get(f"/api/finding/{MISSING_TABLE_QUERY_ID}/aggregate", params={"window": 30, "agg": "count"})
    assert r.status_code == 200
    assert r.json()["outcome"] == "not_assessed"


def test_finding_aggregate_error_corrupt_db(corrupt_db_env):
    r = client.get(f"/api/finding/{PLAIN_QUERY_ID}/aggregate", params={"window": 30, "agg": "count"})
    assert r.status_code == 200  # a store-level read failure is a 200 + outcome="error", never a 500
    body = r.json()
    assert body["outcome"] == "error"
    assert body["error"]


# -------------------------------------------------------------------------------------------
# T-68's own scenario (fix_plan.txt batch 3): a 5,100-row flood under one job_id proves a plain
# /api/finding fetch (capped at 5,000) undercounts while GET .../aggregate does not.
# -------------------------------------------------------------------------------------------


def test_finding_detail_slice_undercounts_a_flooded_group(flood_env):
    r = client.get(f"/api/finding/{PLAIN_QUERY_ID}", params={"window": 30, "limit": 5000})
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "ok_rows"
    assert body["rows_total"] >= FLOOD_ROWS
    assert body["returned"] == 5000
    assert body["rows_total"] > body["returned"], "the flood must make this a genuine slice"


def test_finding_aggregate_captures_the_full_flooded_group(flood_env):
    r = client.get(
        f"/api/finding/{PLAIN_QUERY_ID}/aggregate",
        params={"window": 30, "group": "job_id", "agg": "sum", "value": "net_usage_quantity"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "ok_rows"
    assert body["matched_rows"] >= FLOOD_ROWS

    flood_group = next(g for g in body["groups"] if g["key"] == [FLOOD_JOB_ID])
    # Every flood row is net_usage_quantity=1.0 and shares job_id=FLOOD_JOB_ID -- unlike the
    # plain-finding read above, no LIMIT sits between the WHERE clause and this SUM, so the group's
    # row_count and value both cover the WHOLE flood, never a 5,000-row slice of it.
    assert flood_group["row_count"] == FLOOD_ROWS
    assert flood_group["value"] == pytest.approx(float(FLOOD_ROWS))


# -------------------------------------------------------------------------------------------
# P2-FILTERS: the T-63/DEC-60 workspace-attribute filters (cost_center/team/business_unit/domain)
# reach GET /api/findings and GET /api/finding/{id}/aggregate. tests/test_attribute_filter.py
# already proves GET /api/finding/{id} itself (T-63's own task, DEC-60 rules 1/5/6); before this
# task the OTHER two endpoints either ignored the filter entirely (GET /api/findings never even
# read the query params) or were simply untested (the aggregate endpoint's own plumbing already
# threaded `attributes` through, app/api/app.py's get_finding_aggregate, but nothing exercised it).
#
# Same fixture fact tests/test_attribute_filter.py relies on (tests/fixtures/billing.py's "SEC L
# (T-63)"): workspace 1111 (WS_PROD) is the only workspace tagged cost_center="engineering", and
# PLAIN_QUERY_ID ("cost_by_job") carries real window=30 rows for it with no account-level
# (workspace_id IS NULL) row to confuse an explicit-workspace-list comparison.
# -------------------------------------------------------------------------------------------

COST_CENTER_WORKSPACE_ID = "1111"  # WS_PROD -- tests/fixtures/billing.py's own constant


def test_findings_list_attribute_filter_matches_the_explicit_workspace_filter():
    """?cost_center=engineering must narrow GET /api/findings' bulk row/status counts exactly the
    way an explicit workspace_ids=[1111] filter already does."""
    by_attribute = client.get("/api/findings", params={"window": 30, "cost_center": "engineering"}).json()
    by_workspace = client.get(
        "/api/findings", params={"window": 30, "workspace_ids": [COST_CENTER_WORKSPACE_ID]}
    ).json()
    assert by_attribute["attributes"] == {"cost_center": ["engineering"]}

    row_by_attribute = next(f for f in by_attribute["findings"] if f["query_id"] == PLAIN_QUERY_ID)
    row_by_workspace = next(f for f in by_workspace["findings"] if f["query_id"] == PLAIN_QUERY_ID)
    assert row_by_attribute["outcome"] == row_by_workspace["outcome"] == "ok_rows"
    assert row_by_attribute["row_count"] == row_by_workspace["row_count"] > 0
    assert row_by_attribute["status_counts"] == row_by_workspace["status_counts"]


def test_findings_list_attribute_filter_excludes_non_matching_workspaces():
    """The same filter, scoped to workspaces that are NOT 'engineering', must exclude every one of
    cost_by_job's rows -- proving this is a real join against dims.dim_workspace, not a no-op that
    happens to pass the previous test by coincidence."""
    r = client.get(
        "/api/findings",
        params={"window": 30, "workspace_ids": ["2222", "3333"], "cost_center": "engineering"},
    ).json()
    row = next(f for f in r["findings"] if f["query_id"] == PLAIN_QUERY_ID)
    assert row["outcome"] == "ok_empty_filters"
    assert row["row_count"] == 0


def test_findings_list_region_gap_reflects_the_attribute_filter():
    # T-71: region_gap is computed over the SAME selection this list endpoint's rows are -- an
    # attribute filter must narrow it exactly the way workspace_ids already does (both go through
    # service.region_gap(workspace_ids, env, attributes) now).
    by_attribute = client.get("/api/findings", params={"window": 30, "cost_center": "engineering"}).json()
    by_workspace = client.get(
        "/api/findings", params={"window": 30, "workspace_ids": [COST_CENTER_WORKSPACE_ID]}
    ).json()
    assert by_attribute["region_gap"] == by_workspace["region_gap"]


def test_findings_list_unknown_attribute_query_param_is_ignored_not_an_error():
    r = client.get("/api/findings", params={"window": 30, "not_a_real_canonical_key": "drop table x"})
    assert r.status_code == 200
    assert r.json()["attributes"] == {}


def test_finding_aggregate_attribute_filter_matches_the_explicit_workspace_filter():
    """The same DEC-60 rule 1 proof tests/test_attribute_filter.py runs against GET /api/finding,
    but through the T-68 aggregate endpoint: a real server-side SQL SUM, not a row page."""
    by_attribute = client.get(
        f"/api/finding/{PLAIN_QUERY_ID}/aggregate",
        params={
            "window": 30, "cost_center": "engineering",
            "group": "job_id", "agg": "sum", "value": "net_usage_quantity",
        },
    ).json()
    by_workspace = client.get(
        f"/api/finding/{PLAIN_QUERY_ID}/aggregate",
        params={
            "window": 30, "workspace_ids": [COST_CENTER_WORKSPACE_ID],
            "group": "job_id", "agg": "sum", "value": "net_usage_quantity",
        },
    ).json()
    assert by_attribute["outcome"] == by_workspace["outcome"] == "ok_rows"
    assert by_attribute["matched_rows"] == by_workspace["matched_rows"] > 0
    assert by_attribute["total_value"] == pytest.approx(by_workspace["total_value"])


def test_finding_aggregate_attribute_filter_excludes_non_matching_workspaces():
    r = client.get(
        f"/api/finding/{PLAIN_QUERY_ID}/aggregate",
        params={
            "window": 30, "workspace_ids": ["2222", "3333"], "cost_center": "engineering",
            "group": "job_id", "agg": "sum", "value": "net_usage_quantity",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "ok_empty_filters"
    assert "groups" not in body


# -------------------------------------------------------------------------------------------
# P4-03 review fix: real thresholds and plain words in header text -- resolve_params,
# substitute_params, substitute_header_params, and that GET /api/finding actually serves the
# substituted header, never a raw ':param' token or a raw not_assessed_reason code.
# -------------------------------------------------------------------------------------------


def test_finding_header_investigate_if_substitutes_param_tokens_not_raw():
    r = client.get("/api/finding/lakeflow_job_reliability", params={"window": 30})
    body = r.json()
    assert body["outcome"] == "ok_rows"
    investigate_if = body["header"]["investigate_if"]
    assert ":min_runs" not in investigate_if
    assert ":crit_consecutive_failures" not in investigate_if
    # config/thresholds.yml carries no override for this query_id -- both header defaults
    # (min_runs=5, crit_consecutive_failures=3) are substituted with their real value, tagged
    # "(default)", never left as the raw token.
    assert "5 (default)" in investigate_if
    assert "3 (default)" in investigate_if


def test_finding_header_not_assessed_reasons_are_plain_words_not_raw_codes():
    r = client.get("/api/finding/lakeflow_job_reliability", params={"window": 30})
    body = r.json()
    assert body["header"]["not_assessed_reasons"]["too_few_runs"] == (
        "too few runs in the window to judge reliability"
    )


def test_substitute_params_leaves_an_undeclared_token_untouched():
    # ':foo' is not a param this (made-up) resolved dict declares -- left alone rather than eaten,
    # so plain prose that happens to start with a colon-letter, or a lint_headers.py gap, is never
    # silently swallowed.
    resolved = {"min_runs": (5, "default")}
    text = "at least :min_runs runs, or contact :foo for help"
    out = service.substitute_params(text, resolved)
    assert ":foo" in out
    assert ":min_runs" not in out
    assert "5 (default)" in out


def test_finding_header_period_days_reflects_the_effective_window_not_the_default():
    # Review fix: :period_days is not a thresholds.yml setting like the others -- at build time
    # it becomes the window the reader picked (7/30/90), never the header default (30) or a
    # thresholds.yml override. The header text must say the window actually on screen.
    r7 = client.get("/api/finding/instance_pools_idle_capacity", params={"window": 7})
    r90 = client.get("/api/finding/instance_pools_idle_capacity", params={"window": 90})
    read_this_7 = r7.json()["header"]["read_this"]
    read_this_90 = r90.json()["header"]["read_this"]
    assert ":period_days" not in read_this_7
    assert "7 (the window you picked)" in read_this_7
    assert "30 (default)" not in read_this_7
    assert "90 (the window you picked)" in read_this_90


def test_finding_header_period_days_reflects_partial_export_coverage(monkeypatch):
    # Review fix: a direct export that only ran 10 of the 30 days must say so in the header text,
    # never claim the full window was queried.
    monkeypatch.setattr(service.app_core_data, "exported_window_coverage", lambda: {30: 10})
    spec = registry.by_id("instance_pools_idle_capacity")
    header = service.substitute_header_params(spec, 30)
    assert "10 (the export covered 10 of the 30 days you picked)" in header["read_this"]
    assert "30 (the window you picked)" not in header["read_this"]


def test_finding_header_period_days_reflects_partial_export_coverage_at_90d(monkeypatch):
    # Same wording at the 90d alias window: a 30-day export covers 90 partially too.
    monkeypatch.setattr(service.app_core_data, "exported_window_coverage", lambda: {90: 30})
    spec = registry.by_id("instance_pools_idle_capacity")
    header = service.substitute_header_params(spec, 90)
    assert "30 (the export covered 30 of the 90 days you picked)" in header["read_this"]
    assert "90 (the window you picked)" not in header["read_this"]


def test_resolve_params_reports_your_setting_when_thresholds_yml_overrides(monkeypatch):
    spec = registry.by_id("lakeflow_job_reliability")
    monkeypatch.setattr(
        service.app_config, "load_thresholds",
        lambda: {"lakeflow_job_reliability": {"min_runs": 9}},
    )
    resolved = service.resolve_params(spec)
    assert resolved["min_runs"] == (9, "your setting")
    # An id with no override for this param still falls through to its own header default.
    assert resolved["crit_consecutive_failures"] == (3, "default")


# -------------------------------------------------------------------------------------------
# money/affected (U8/U4) -- /api/findings' own Possible waste/Other $/Affected columns, computed
# in the same bulk pass as status_counts, never one fetch per row.
# -------------------------------------------------------------------------------------------


def test_findings_money_and_affected_shapes():
    resp = client.get("/api/findings", params={"window": 30})
    assert resp.status_code == 200
    rows = {r["query_id"]: r for r in resp.json()["findings"]}

    waste_row = rows["compute_warehouse_idle_minutes"]
    assert waste_row["money"] is not None
    assert waste_row["money"]["column"] == "est_wasted_usd_list"
    assert waste_row["money"]["kind"] == "waste"
    assert isinstance(waste_row["money"]["usd"], (int, float))

    change_row = rows["cost_chargeback_by_workspace"]
    assert change_row["money"] is not None
    assert change_row["money"]["column"] == "change_usd_list"
    assert change_row["money"]["kind"] == "change"

    affected = waste_row["affected"]
    assert affected is not None
    assert affected["column"] == "warehouse_id"
    assert affected["noun"] == "warehouses"
    assert affected["total"] >= affected["flagged"] >= 0

    con = duckdb.connect(str(app_core_data._db_path()), read_only=True)
    try:
        dim_total = con.execute("SELECT count(DISTINCT warehouse_id) FROM dims.dim_warehouse").fetchone()[0]
        key_sql = app_core_data._entity_key_sql("warehouse_id", True)
        own_total = con.execute(
            f"SELECT count(DISTINCT {key_sql}) FROM findings.f_compute_warehouse_idle_minutes "
            "WHERE window_days = 30"
        ).fetchone()[0]
    finally:
        con.close()
    # affected.total is never fewer than the check's own count: the dim can miss a
    # one-time submit run's warehouse id that the check itself still saw.
    assert affected["total"] == max(dim_total, own_total)


def test_findings_money_null_for_a_check_with_no_money_column():
    """Not a hard-coded query_id: any ok_rows check whose own table carries no money-shaped
    column at all (app.core.finding_columns.money_column's own rule -- MONEY_COLUMN_KIND's
    explicit names, e.g. change_usd_list/total_est_wasted_usd_list, count as money too, not just
    a column matching pricing's plain list-price regex) must report money=null."""
    from app.core import finding_columns as app_finding_columns

    body = client.get("/api/findings", params={"window": 30}).json()
    con = duckdb.connect(str(app_core_data._db_path()), read_only=True)
    try:
        table_cols = con.execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'findings'"
        ).fetchall()
    finally:
        con.close()
    cols_by_table: dict[str, list[str]] = {}
    for table_name, column_name in table_cols:
        cols_by_table.setdefault(table_name, []).append(column_name)

    checked = 0
    for row in body["findings"]:
        if row["outcome"] != "ok_rows":
            continue
        cols = cols_by_table.get(f"f_{row['query_id']}", [])
        if app_finding_columns.money_column(cols) is not None:
            continue
        assert row["money"] is None, row["query_id"]
        checked += 1
    assert checked > 0, "no ok_rows check without a money column found in this fixture"


def test_findings_stays_fast_with_money_and_affected():
    import time

    start = time.monotonic()
    resp = client.get("/api/findings", params={"window": 30})
    elapsed = time.monotonic() - start
    assert resp.status_code == 200
    # Catches the old per-check N+1 (~90 s) with room for a loaded machine (2-7 s observed).
    assert elapsed < 10.0


def test_task_rows_add_their_job_spend_once():
    """A job's tasks each repeat the job's spend: the check's $ counts it once per job."""
    from app.core import finding_columns
    assert finding_columns.money_parent(["workspace_id", "job_id", "task_key", "est_usd_list"]) == "job_id"
    assert finding_columns.money_parent(["workspace_id", "job_id", "est_usd_list"]) is None
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES ('1', 'a', 't1', 100.0), ('1', 'a', 't2', 100.0), "
                "('1', 'b', 't1', 50.0), ('2', 'a', 't1', 25.0)) v(workspace_id, job_id, task_key, est_usd_list)")
    key = app_core_data._entity_key_sql("job_id", True)
    assert con.execute(app_core_data._money_sum_sql("t", "est_usd_list", "TRUE", key)).fetchone()[0] == 175.0
    assert con.execute(app_core_data._money_sum_sql("t", "est_usd_list", "TRUE", None)).fetchone()[0] == 275.0
