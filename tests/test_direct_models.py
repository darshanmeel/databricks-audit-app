"""tests/test_direct_models.py -- item DBX-DIRECT: tools/generate_direct_models.py, the
Databricks-direct model generator.

No Databricks, no network, no real dbt invocation anywhere in this file.

Modules under test are loaded by file path (importlib), the same pattern
tests/test_generate_models.py and tests/test_dbt_run.py already use.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DIRECT_DIR = ROOT / "dbt" / "models" / "databricks_direct"
FINDINGS_DIR = ROOT / "dbt" / "models" / "findings"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


gdm = _load(ROOT / "tools" / "generate_direct_models.py", "generate_direct_models_under_test")
gm = _load(ROOT / "tools" / "generate_models.py", "generate_models_for_direct_test")

from app.core.registry import load_registry  # noqa: E402

REGISTRY = {s.query_id: s for s in load_registry()}


# ------------------------------------------------------------------------------------------
# the generator: the committed folder is in sync, and it covers exactly the f_ model set
# ------------------------------------------------------------------------------------------


def test_check_passes_on_the_committed_folder():
    r = gdm.check()
    assert r["stale"] == [], f"stale: {r['stale']}"
    assert r["missing"] == [], f"missing: {r['missing']}"
    assert r["extra"] == [], f"extra: {r['extra']}"
    assert r["ok"] is True
    assert r["models"] > 0


def test_check_scoped_by_only_and_domain_also_passes():
    assert gdm.check(only=["cost_by_job"])["ok"] is True
    assert gdm.check(domain="storage")["ok"] is True


def _findings_manifest() -> dict:
    return json.loads((FINDINGS_DIR / "generated_manifest.json").read_text(encoding="utf-8"))


def _direct_manifest() -> dict:
    return json.loads((DIRECT_DIR / "generated_manifest.json").read_text(encoding="utf-8"))


def test_every_finding_model_has_exactly_one_direct_model_and_vice_versa():
    f_models = _findings_manifest()["models"]
    d_models = _direct_manifest()["models"]
    f_ids = [m["query_id"] for m in f_models]
    d_ids = [m["query_id"] for m in d_models]
    # no duplicates on either side (the "exactly one" half of "exactly one ... and vice versa")
    assert len(f_ids) == len(set(f_ids))
    assert len(d_ids) == len(set(d_ids))
    assert set(f_ids) == set(d_ids)
    assert set(f_ids) == {s.query_id for s in REGISTRY.values() if s.executable}


def test_direct_model_names_and_paths_match_the_query_id():
    for m in _direct_manifest()["models"]:
        assert m["model"] == f"d_{m['query_id']}"
        assert m["path"] == f"dbt/models/databricks_direct/{m['domain']}/d_{m['query_id']}.sql"
        assert (ROOT / m["path"]).exists()


def test_direct_models_have_no_duckdb_branch_and_the_right_config_line():
    manifest = _direct_manifest()["models"]
    assert manifest, "expected at least one generated direct model"
    for m in manifest:
        text = (ROOT / m["path"]).read_text(encoding="utf-8")
        # single-branch (Databricks-only): no {% if target.type == 'duckdb' %} switch survives
        assert "target.type == 'duckdb'" not in text
        assert 'target.type == "duckdb"' not in text
        first_line = text.splitlines()[0]
        assert 'enabled=(target.type == "databricks")' in first_line
        assert 'schema="audit_direct"' in first_line
        assert "'databricks_direct'" in first_line
        assert 'materialized=("table" if var("direct_mode", "run") == "table" else "direct_check")' in first_line
        # review fix (DBX-DIRECT): the config line's trailing `}}` was previously emitted as a
        # single `}` by an f-string escaping bug -- every generated file failed to parse as Jinja.
        # Pin the whole-line shape so that regresses loudly here, not only in dbt's own parser.
        assert first_line.startswith("{{ config(") and first_line.endswith(") }}")
        # the marker _write()/gate.py's --check rely on to avoid clobbering a hand-written file
        assert "fix the source query and regenerate, never edit this file" in text.splitlines()[1]


def test_direct_models_and_macro_parse_as_valid_jinja():
    """Review fix (DBX-DIRECT): the config-line brace bug above made every generated file fail
    dbt's own Jinja parse (TemplateSyntaxError: unexpected '}'), but the generator's own tests only
    ever checked substrings of line 1, so nothing here caught it. dbt-core is already a dependency
    (tools/dbt_run.py, tools/gate.py), so dbt_common's own Jinja environment is always importable --
    this needs no dbt-databricks and touches no filesystem beyond reading the committed files."""
    from dbt_common.clients.jinja import get_environment

    env = get_environment()
    manifest = _direct_manifest()["models"]
    assert manifest, "expected at least one generated direct model"
    for m in manifest:
        text = (ROOT / m["path"]).read_text(encoding="utf-8")
        env.parse(text)  # raises jinja2.TemplateSyntaxError on failure
    macro_text = (ROOT / "dbt" / "macros" / "direct_check.sql").read_text(encoding="utf-8")
    env.parse(macro_text)


def test_direct_model_tags_match_the_finding_models_tags_plus_databricks_direct():
    for m in _direct_manifest()["models"]:
        spec = REGISTRY[m["query_id"]]
        assert m["tags"] == gm.model_tags(spec) + ["databricks_direct"]


def test_combined_yml_lists_every_model_with_a_title_description():
    text = (DIRECT_DIR / "_databricks_direct.yml").read_text(encoding="utf-8")
    assert "fix the source query and regenerate, never edit this file" in text.splitlines()[0]
    import yaml
    doc = yaml.safe_load(text)
    names = {m["name"] for m in doc["models"]}
    assert names == {m["model"] for m in _direct_manifest()["models"]}
    for m in doc["models"]:
        assert m["description"]  # the query title, never blank
