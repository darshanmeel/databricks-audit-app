"""tests/test_generate_models.py -- the contract tests t01..t12 of tasks/T-06 (PLAN.md 7.5),
plus the DEC-13 drilldown tag, the manifest shape, DEC-24 sibling survival and the runner
byte-equivalence of the Databricks branch.

Modules under test are loaded by file path (importlib), the same pattern as tests/test_ddl.py.
Everything that writes files writes into tmp_path; the repo's dbt/models/findings is never
touched by this file.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import re
import sys
from pathlib import Path

import duckdb
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


gm = _load(ROOT / "tools" / "generate_models.py", "generate_models_under_test")
_ORIG_CHECK = gm.check     # the real check(), kept for the CLI tests that monkeypatch gm.check
stub = _load(ROOT / "tests" / "fixtures" / "jinja_stub.py", "jinja_stub_under_test")
ddl = _load(ROOT / "tests" / "fixtures" / "ddl.py", "ddl_under_test")
tr = sys.modules["app.core.translate"]

REGISTRY = {s.query_id: s for s in gm.load_registry()}
EXECUTABLE = [s for s in REGISTRY.values() if s.executable]
CONTRACT = gm.load_source_contract()
LOCK_COMMIT = gm.load_lock_commit()

PARAM_TOKEN = re.compile(r"(?<![:\w]):([a-z_][a-z0-9_]*)\b")
SOURCE_CALL = re.compile(r"\{\{ source\('system_([a-z_]+)', '([a-z_]+)'\) \}\}")
SYSTEM_TOKEN = re.compile(r"\bsystem\.")


def _branches(qid: str) -> dict[str, str]:
    return gm.render_branches(REGISTRY[qid], CONTRACT)


def _model(qid: str) -> str:
    return gm.render_model(REGISTRY[qid], lock_commit=LOCK_COMMIT, contract=CONTRACT)


def _with_body(spec, body: str):
    return dataclasses.replace(spec, body=body)


def _connection_for(spec) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    for schema, table in spec.sources:
        con.execute(ddl.DDL[f"{schema}__{table}"])
    return con


# ------------------------------------------------------------------------------------------
# t01 .. t12
# ------------------------------------------------------------------------------------------


def test_t01_params_tokenised():
    b = _branches("cost_by_job")
    for text in b.values():
        assert "{{ param('cost_by_job', 'warn_job_dbus_per_day', 50) }}" in text
        assert "{{ param('cost_by_job', 'crit_job_dbus_per_day', 200) }}" in text
        assert ":period_days" not in text and "{{ w }}" in text
    # no :param token survives outside a string literal / comment, in any branch of any model
    leftovers = []
    for spec in EXECUTABLE:
        for dialect, text in gm.render_branches(spec, CONTRACT).items():
            for m in PARAM_TOKEN.finditer(tr.mask(text)):
                leftovers.append((spec.query_id, dialect, m.group(0)))
    assert leftovers == []
    # an unknown :name raises; a time literal's ':00' is never a param
    spec = REGISTRY["cost_by_job"]
    with pytest.raises(gm.GenerationError) as e:
        gm.render_branches(_with_body(spec, spec.body.replace(":crit_job_dbus_per_day", ":nope")), CONTRACT)
    assert "nope" in str(e.value)
    tok = gm.tokenise_params("x", "WHERE ts >= '12:00' AND n > :top_n AND m > :top_n_extra", [{"name": "top_n", "default": 1}, {"name": "top_n_extra", "default": 2}])
    assert tok == "WHERE ts >= '12:00' AND n > __P__top_n__ AND m > __P__top_n_extra__"


def test_t02_window_loop():
    windowed = stub.render(_model("cost_by_job"), target_type="duckdb", windows=(7, 30, 90))
    assert windowed.count(" AS window_days") == 3
    assert windowed.count("UNION ALL") == 2
    assert [int(x) for x in re.findall(r"SELECT (\d+) AS window_days", windowed)] == [7, 30, 90]
    model = _model("cost_workspace_names")
    assert "{%- for w in windows %}" not in model and "UNION ALL" not in model
    snapshot = stub.render(model, target_type="duckdb")
    assert snapshot.count("SELECT 0 AS window_days, q.*") == 1
    assert "UNION ALL" not in snapshot and " AS window_days" in snapshot


def test_t03_dialect_branch():
    b = _branches("compute_warehouse_idle_gaps")
    duck, dbx = tr.strip_comments(b["duckdb"]), tr.strip_comments(b["databricks"])
    assert "epoch(" in duck and "unix_timestamp(" not in duck
    assert "unix_timestamp(" in dbx and "epoch(" not in dbx
    # and through the model's own {% if target.type == 'duckdb' %} switch
    model = _model("compute_warehouse_idle_gaps")
    assert "epoch(" in stub.render(model, target_type="duckdb")
    rendered_dbx = stub.render(model, target_type="databricks", target_name="databricks")
    assert "unix_timestamp(" in rendered_dbx and "epoch(" not in rendered_dbx


def test_t04_sources_substituted():
    offenders = []
    for spec in EXECUTABLE:
        expected = {(s, t) for s, t in spec.sources}
        uses_list_prices = ("billing", "list_prices") in expected and spec.query_id not in gm.RAW_LIST_PRICES_IDS
        want = expected - {("billing", "list_prices")} if uses_list_prices else expected
        for dialect, text in gm.render_branches(spec, CONTRACT).items():
            found = {(m.group(1), m.group(2)) for m in SOURCE_CALL.finditer(text)}
            if found != want:
                offenders.append((spec.query_id, dialect, "source() calls", sorted(found ^ want)))
            if uses_list_prices and "{{ list_prices() }}" not in text:
                offenders.append((spec.query_id, dialect, "list_prices() call missing"))
            if SYSTEM_TOKEN.search(tr.mask(text)):
                offenders.append((spec.query_id, dialect, "system. token left"))
        header_only, body_only = gm.reads_drift(spec)
        if header_only or body_only:
            offenders.append((spec.query_id, "header/body drift", header_only, body_only))
    assert offenders == []
    # drift is reported, never silently ignored
    spec = REGISTRY["cost_workspace_names"]
    drifted = _with_body(spec, spec.body.replace("system.access.workspaces_latest", "system.billing.usage"))
    assert gm.reads_drift(drifted) == (["system.access.workspaces_latest"], ["system.billing.usage"])
    with pytest.raises(gm.GenerationError) as e:
        gm.render_branches(drifted, CONTRACT)
    assert "drift" in str(e.value)
    # an unknown table under a known schema is an error too
    with pytest.raises(gm.GenerationError):
        gm.substitute_sources("x", "SELECT 1 FROM system.billing.nope", CONTRACT)
    # a comment or a literal that names a source is left alone
    text, pairs = gm.substitute_sources("x", "SELECT 'system.billing.usage' FROM system.billing.usage -- system.access.audit", CONTRACT)
    assert pairs == [("billing", "usage")]
    assert text == "SELECT 'system.billing.usage' FROM {{ source('system_billing', 'usage') }} -- system.access.audit"


def test_t05_time_macros():
    seen_today = seen_now = 0
    offenders = []
    for spec in EXECUTABLE:
        body = tr.strip_comments(gm.runner_body(spec.body))
        uses_today = "current_date" in body
        uses_now = "current_timestamp" in body
        for dialect, text in gm.render_branches(spec, CONTRACT).items():
            clean = tr.mask(text)
            if re.search(r"\bcurrent_date\b", clean) or re.search(r"\bcurrent_timestamp\b", clean):
                offenders.append((spec.query_id, dialect, "native time function left"))
            if uses_today and "{{ audit_today() }}" not in text:
                offenders.append((spec.query_id, dialect, "audit_today missing"))
            if uses_now and "{{ audit_now() }}" not in text:
                offenders.append((spec.query_id, dialect, "audit_now missing"))
        seen_today += uses_today
        seen_now += uses_now
    assert offenders == []
    assert seen_today > 50 and seen_now > 20     # PLAN.md 5.3: "62 and 23 bodies use them"
    # the Databricks branch gets the pinning and NOTHING else from the translation table
    b = _branches("lakeflow_long_running_runs")
    assert "timestampdiff(SECOND" in b["databricks"] and "{{ audit_now() }}" in b["databricks"]
    assert "date_diff('second'" in b["duckdb"]
    for target in ("duckdb", "databricks"):
        out = stub.render(_model("cost_by_job"), target_type=target)
        assert "DATE '2026-09-21'" in out and "current_date" not in out


def test_t06_jinja_safe():
    # access_broad_grants' grantee_type classification (not the identity mask, which moves into
    # the mask_user() macro) still carries the literal regex braces and '%@%' inline.
    spec = REGISTRY["access_broad_grants"]
    assert "{8}" in spec.body and "'%@%'" in spec.body
    model = _model("access_broad_grants")
    for target in ("duckdb", "databricks"):
        out = stub.render(model, target_type=target)
        assert "[0-9a-fA-F]{8}-" in out and "LIKE '%@%'" in out
    # cost_by_job's body carries no braces at all, so a literal {{ is a synthetic edge case
    base = REGISTRY["cost_by_job"]
    for bad in ("{{", "{%", "{#"):
        with pytest.raises(gm.GenerationError) as e:
            gm.render_branches(_with_body(base, base.body + f"\n  -- {bad} oops"), CONTRACT)
        assert "Jinja" in str(e.value)
    with pytest.raises(gm.GenerationError):
        gm.render_branches(_with_body(base, base.body.replace("net_usage_quantity", "window_days", 1)), CONTRACT)
    with pytest.raises(gm.GenerationError):
        gm.render_branches(_with_body(base, "DESCRIBE TABLE x"), CONTRACT)


def test_t07_lateral_view_and_explode():
    tag = _branches("cost_chargeback_by_tag")
    assert ("LEFT JOIN LATERAL (SELECT e.key AS tag_key, e.value AS tag_value "
            "FROM unnest(map_entries(custom_tags)) AS x(e)) t ON TRUE") in tag["duckdb"]
    assert "LATERAL VIEW OUTER explode(custom_tags) t AS tag_key, tag_value" in tag["databricks"]
    ap = _branches("lakeflow_jobs_on_all_purpose")
    assert "UNNEST(compute_ids) AS compute_id" in ap["duckdb"]
    assert "EXPLODE(compute_ids) AS compute_id" in ap["databricks"]
    for qid in ("cost_chargeback_by_tag", "lakeflow_jobs_on_all_purpose"):
        spec = REGISTRY[qid]
        con = _connection_for(spec)
        sql = stub.render(_model(qid), target_type="duckdb")
        assert con.execute(sql).fetchall() == []


def test_t08_order_by_meta():
    def order_by(qid: str) -> str:
        spec = REGISTRY[qid]
        doc = yaml.safe_load(gm.render_domain_yml(spec.domain, [spec], grains={}))
        (entry,) = doc["models"]
        assert entry["name"] == f"f_{qid}"
        return entry["meta"]["order_by"]

    assert order_by("lakeflow_long_running_runs").startswith("CASE status WHEN 'CRITICAL'")
    assert order_by("lakeflow_long_running_runs").endswith("run_hours DESC")
    assert order_by("cost_workspace_names") == "workspace_name, workspace_id"
    # the LIMIT after the ORDER BY is not part of it; a window's ORDER BY is not top level. The
    # UNION ALL is wrapped in a derived table (`) placements`) so the extracted order_by is the
    # OUTER ORDER BY only, not anything inside either UNION ALL branch.
    assert order_by("lakeflow_jobs_on_all_purpose").startswith("CASE status WHEN 'CRITICAL' THEN 0")
    assert order_by("lakeflow_jobs_on_all_purpose").endswith("est_usd_list_share DESC")
    assert order_by("task_cluster_utilization").endswith("task_hours DESC, workspace_id, job_id, job_run_id, task_key")
    assert gm.extract_order_by("SELECT x FROM (SELECT x FROM t ORDER BY x) s") == ""
    assert gm.extract_order_by("SELECT x, ROW_NUMBER() OVER (ORDER BY y) FROM t") == ""
    assert gm.extract_order_by("SELECT x FROM t\nORDER BY x DESC   -- worst first\n  , y\nLIMIT :top_n") == "x DESC , y"
    spec = REGISTRY["cost_workspace_names"]
    no_order = _with_body(spec, "SELECT workspace_id FROM system.access.workspaces_latest")
    doc = yaml.safe_load(gm.render_domain_yml("cost", [no_order], grains={}))
    assert doc["models"][0]["meta"]["order_by"] == ""


def test_t09_yml_tests(tmp_path):
    grains_dir = tmp_path / "grains"
    grains_dir.mkdir()
    grain = ["usage_date", "cloud", "workspace_id", "billing_origin_product", "job_id", "is_serverless"]
    (grains_dir / "cost_usage_only.yml").write_text(yaml.safe_dump({"cost_by_job": grain}), encoding="utf-8")
    grains = gm.load_grains(grains_dir)
    assert grains == {"cost_by_job": grain}
    specs = [REGISTRY[q] for q in ("cost_by_job", "cost_workspace_names", "cost_premium_serverless_photon")]
    doc = yaml.safe_load(gm.render_domain_yml("cost", specs, grains=grains))
    by_name = {m["name"]: m for m in doc["models"]}
    assert list(by_name) == ["f_cost_by_job", "f_cost_premium_serverless_photon", "f_cost_workspace_names"]

    def col_tests(entry, col):
        for c in entry["columns"]:
            if c["name"] == col:
                return c["data_tests"]
        return None

    for entry in by_name.values():
        wd = col_tests(entry, "window_days")
        assert wd[0] == "not_null"
        assert wd[1] == {"accepted_values": {"arguments": {"values": [0, 7, 30, 90], "quote": False}}}
    # status accepted_values iff is_finding (DEC-08's counterexample has no status column)
    assert col_tests(by_name["f_cost_by_job"], "status") == [
        {"accepted_values": {"arguments": {"values": ["OK", "WARN", "CRITICAL", "NOT_ASSESSED"]}}}
    ]
    assert col_tests(by_name["f_cost_workspace_names"], "status") is None
    assert REGISTRY["cost_premium_serverless_photon"].is_finding is False
    assert col_tests(by_name["f_cost_premium_serverless_photon"], "status") is None
    # unique_grain iff the id is named in config/grains/*.yml, with window_days prepended (DEC-24)
    assert by_name["f_cost_by_job"]["data_tests"] == [
        {"unique_grain": {"arguments": {"columns": ["window_days"] + grain}}}
    ]
    assert "data_tests" not in by_name["f_cost_workspace_names"]
    # meta carries the header fields the task names
    meta = by_name["f_cost_by_job"]["meta"]
    for key in ("query_id", "title", "tier", "stars", "origin", "windowed", "empty_if", "reads", "requires",
                "confidence", "confidence_note", "healthy", "investigate_if", "actions", "next", "caveats",
                "sql_url", "order_by"):
        assert key in meta, key
    assert meta["origin"] == "vendored" and meta["windowed"] is True and meta["is_finding"] is True
    assert by_name["f_cost_by_job"]["description"] == REGISTRY["cost_by_job"].read_this
    # a grain file that lists window_days itself, or names an id twice, is refused
    (grains_dir / "dup.yml").write_text(yaml.safe_dump({"cost_by_job": ["job_id"]}), encoding="utf-8")
    with pytest.raises(gm.GenerationError):
        gm.load_grains(grains_dir)
    (grains_dir / "dup.yml").write_text(yaml.safe_dump({"cost_by_notebook": ["window_days", "x"]}), encoding="utf-8")
    with pytest.raises(gm.GenerationError):
        gm.load_grains(grains_dir)


def test_t10_skips_non_executable(tmp_path, capsys, monkeypatch):
    out = tmp_path / "findings"
    templates = ("storage_breakdown_analyze", "iceberg_uniform_metadata", "table_props_time_travel_config")
    r = gm.generate(out_dir=out, grains_dir=tmp_path / "no_grains")     # every executable query
    n_vendored = sum(1 for s in EXECUTABLE if s.origin == "vendored")
    n_models = len(EXECUTABLE)  # 100 vendored (DEC-18) + the app-owned ports as T-10/T-11 land them
    assert n_vendored == 100
    assert len(r["models"]) == n_models
    assert sorted(s["query_id"] for s in r["skipped"]) == sorted(templates)
    for qid in templates:
        assert qid not in r["models"]
        assert not list(out.rglob(f"f_{qid}.sql"))
    manifest = json.loads((out / gm.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert [s["query_id"] for s in manifest["skipped"]] == sorted(templates)
    assert all(s["reason"] for s in manifest["skipped"])
    assert len(manifest["models"]) == n_models and len(manifest["ymls"]) == 7
    c = gm.check(out_dir=out, grains_dir=tmp_path / "no_grains")
    assert c["ok"] and c["models"] == n_models
    assert sorted(s["query_id"] for s in c["skipped"]) == sorted(templates)
    # the CLI prints them in its --check output, before the summary line
    monkeypatch.setattr(gm, "check", lambda *a, **k: _ORIG_CHECK(*a, out_dir=out, grains_dir=tmp_path / "no_grains", **k))
    assert gm.main(["--check"]) == 0
    printed = capsys.readouterr().out
    for qid in templates:
        assert f"skipped: {qid} - " in printed
    assert printed.rstrip().endswith(f"models in sync ({n_models} models, 3 skipped)")


def test_t11_check_mode_and_idempotence(tmp_path, capsys, monkeypatch):
    out = tmp_path / "findings"
    kw = dict(out_dir=out, grains_dir=tmp_path / "no_grains")
    ids = ["cost_by_job", "cost_workspace_names", "lakeflow_failed_runs"]
    r = gm.generate(only=ids, **kw)
    assert r["models"] == ids and r["skipped"] == []
    assert sorted(r["ymls"]) == ["cost/_findings__cost.yml", "jobs_pipelines/_findings__jobs_pipelines.yml"]
    before = {p: p.read_bytes() for p in out.rglob("*") if p.is_file()}
    assert len(before) == 3 + 2 + 1
    assert gm.check(only=ids, **kw) == {"ok": True, "stale": [], "missing": [], "extra": [], "models": 3, "skipped": []}
    assert gm.check(**kw)["ok"]
    # byte-identical rerun, with and without --force
    gm.generate(only=ids, **kw)
    gm.generate(only=ids, force=True, **kw)
    after = {p: p.read_bytes() for p in out.rglob("*") if p.is_file()}
    assert after == before
    for data in before.values():
        assert b"\r\n" not in data
    # stale / missing / extra are each reported and fail the check
    model = out / "cost" / "f_cost_by_job.sql"
    model.write_bytes(model.read_bytes() + b"-- tampered\n")
    c = gm.check(**kw)
    assert not c["ok"] and c["stale"] == ["cost/f_cost_by_job.sql"] and c["missing"] == [] and c["extra"] == []
    (out / "jobs_pipelines" / "f_lakeflow_failed_runs.sql").unlink()
    (out / "cost" / "f_handwritten.sql").write_text("select 1\n", encoding="utf-8")
    c = gm.check(**kw)
    assert c["missing"] == ["jobs_pipelines/f_lakeflow_failed_runs.sql"]
    assert c["extra"] == ["cost/f_handwritten.sql"]
    monkeypatch.setattr(gm, "check", lambda *a, **k: _ORIG_CHECK(*a, **kw, **k))
    assert gm.main(["--check"]) == 1
    printed = capsys.readouterr()
    assert "stale: cost/f_cost_by_job.sql" in printed.out
    assert "missing: jobs_pipelines/f_lakeflow_failed_runs.sql" in printed.out
    assert "extra: cost/f_handwritten.sql" in printed.out
    assert "models out of sync: 1 stale, 1 missing, 1 extra" in printed.err
    # a scoped check of an id never generated is 'missing', not silently in sync
    c = _ORIG_CHECK(only=["cost_by_notebook"], **kw)
    assert c["missing"] == ["cost/f_cost_by_notebook.sql (never generated)"]
    # a hand-written file in a model's place is refused without --force, overwritten with it
    (out / "cost" / "f_handwritten.sql").unlink()
    model.write_text("select 1 -- mine\n", encoding="utf-8")
    with pytest.raises(gm.GenerationError):
        gm.generate(only=["cost_by_job"], **kw)
    gm.generate(only=["cost_by_job"], force=True, **kw)
    assert model.read_bytes() == before[model]


def test_t12_duckdb_executes():
    ids = ("lakeflow_failed_runs", "cost_by_job", "lakeflow_long_running_runs",
           "task_cluster_utilization", "query_task_statement_breakdown")
    for qid in ids:
        spec = REGISTRY[qid]
        con = _connection_for(spec)
        sql = stub.render(_model(qid), target_type="duckdb", windows=(7, 30, 90))
        cur = con.execute(sql)
        rows = cur.fetchall()
        assert rows == [], qid
        assert cur.description[0][0] == "window_days", qid


# ------------------------------------------------------------------------------------------
# beyond t01..t12: DEC-13, manifest, DEC-24, runner equivalence
# ------------------------------------------------------------------------------------------


def test_drilldown_tag_dec13():
    assert gm.DRILLDOWN_IDS == ("lakeflow_long_running_runs", "task_cluster_utilization",
                                "query_task_statement_breakdown")
    for qid in gm.DRILLDOWN_IDS:
        head = _model(qid).splitlines()[0]
        assert head.startswith("{{ config(materialized='table', tags=[") and "'drilldown'" in head
        assert "'finding'" in head
    assert "'drilldown'" not in _model("cost_by_job")
    assert _model("cost_by_job").splitlines()[0] == (
        "{{ config(materialized='table', tags=['finding', 'domain:cost', 'tier:standard', 'stars']) }}"
    )
    assert _model("cost_workspace_names").splitlines()[0] == (
        "{{ config(materialized='table', tags=['inventory', 'domain:cost', 'tier:lite']) }}"
    )


def test_generated_header_stamps_lock_commit():
    lock = json.loads((ROOT / "config" / "vendored.lock").read_text(encoding="utf-8"))
    line = _model("cost_by_job").splitlines()[1]
    assert line == (f"-- GENERATED by tools/generate_models.py from app/queries/vendored/cost/cost_by_job.sql "
                    f"(vendored @ {lock['commit']}). Do not edit.")


def test_manifest_shape_and_domain_scope(tmp_path):
    out = tmp_path / "findings"
    kw = dict(out_dir=out, grains_dir=tmp_path / "no_grains")
    r = gm.generate(domain="serving_ai", **kw)
    serving = sorted(s.query_id for s in EXECUTABLE if s.domain == "serving_ai")
    assert sorted(r["models"]) == serving and r["ymls"] == ["serving_ai/_findings__serving_ai.yml"]
    manifest = json.loads((out / gm.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert set(manifest) == {"commit", "models", "skipped", "ymls"}
    assert manifest["commit"] == LOCK_COMMIT
    assert [m["query_id"] for m in manifest["models"]] == serving
    for m in manifest["models"]:
        assert set(m) == {"query_id", "domain", "model", "path", "origin", "windowed", "is_finding", "tags"}
        assert m["model"] == f"f_{m['query_id']}" and m["path"] == f"serving_ai/{m['model']}.sql"
    with pytest.raises(gm.GenerationError):
        gm.generate(only=["not_a_query"], **kw)
    with pytest.raises(gm.GenerationError):
        gm.generate(domain="nope", **kw)


def test_sibling_models_survive_a_scoped_regeneration_dec24(tmp_path):
    out = tmp_path / "findings"
    grains_dir = tmp_path / "grains"
    grains_dir.mkdir()
    kw = dict(out_dir=out, grains_dir=grains_dir)
    gm.generate(only=["cost_by_job", "cost_workspace_names"], **kw)
    yml = out / "cost" / "_findings__cost.yml"
    first = yaml.safe_load(yml.read_text(encoding="utf-8"))
    assert [m["name"] for m in first["models"]] == ["f_cost_by_job", "f_cost_workspace_names"]
    # a grain task adds its grain and regenerates ONLY its id
    (grains_dir / "g.yml").write_text(yaml.safe_dump({"cost_workspace_names": ["workspace_id"]}), encoding="utf-8")
    r = gm.generate(only=["cost_workspace_names"], **kw)
    assert r["models"] == ["cost_workspace_names"]
    second = yaml.safe_load(yml.read_text(encoding="utf-8"))
    assert [m["name"] for m in second["models"]] == ["f_cost_by_job", "f_cost_workspace_names"]
    assert second["models"][0] == first["models"][0]           # the sibling block is untouched
    assert second["models"][1]["data_tests"] == [{"unique_grain": {"arguments": {"columns": ["window_days", "workspace_id"]}}}]
    assert gm.check(**kw)["ok"]
    manifest = json.loads((out / gm.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert [m["query_id"] for m in manifest["models"]] == ["cost_by_job", "cost_workspace_names"]


def _runner_resolve(spec) -> str:
    """tools/run_audit.py resolve_sql() of the public repo, reproduced: drop every line that
    starts with `--`, substitute every known :param with its header default (longest name
    first), strip, drop a trailing ';'."""
    text = (ROOT / spec.path).read_text(encoding="utf-8")
    body = "\n".join(l for l in text.splitlines() if not l.startswith("--"))
    values = {p["name"]: p["default"] for p in spec.params}
    if values:
        pattern = r":(" + "|".join(sorted(values, key=len, reverse=True)) + r")\b"
        body = re.sub(pattern, lambda m: str(values[m.group(1)]), body)
    # the app-only query_source placeholder renders as its dbt var default
    body = body.replace(gm.QUERY_SOURCE_MARKER, gm.QUERY_SOURCE_DEFAULT)
    return body.strip().rstrip(";")


def _has_a_masked_case(text: str) -> bool:
    """True when `text` (the runner's raw, unrewritten SQL) carries a resource-name or identity
    mask CASE -- generate_models.py rewrites those before either branch is rendered, so such a
    query is exempt from the byte-equivalence check below."""
    return bool(
        tr._RESOURCE_MASK_RE.search(text)
        or tr._IDENTITY_MASK_RE.search(text)
        or tr._IDENTITY_MASK_COALESCE_RE.search(text)
    )


def test_databricks_branch_is_byte_equivalent_to_the_runner():
    """PLAN.md 5.3: with no as_of var the Databricks branch renders to exactly what the public
    runner executes -- modulo the T-05 macro spelling `current_date` (no parentheses) on a
    non-duckdb target, which this stub pins back to the parenthesised form, modulo the mask
    rewrite (a resource-name or identity mask CASE is not the runner's own text any more; each is
    covered by its own test in test_translate.py and test_mask_user_setting.py), and modulo the
    list_prices() de-duplication (covered by test_t04 and test_list_prices_deduplicated below)."""
    dbx_source = lambda name, table: f"system.{name[len('system_'):]}.{table}"  # noqa: E731
    mismatches = []
    for spec in EXECUTABLE:
        window = next((p["default"] for p in spec.params if p["name"] == "period_days"), 0)
        rendered = stub.render(
            _model(spec.query_id), target_type="databricks", target_name="databricks",
            windows=(window,), source=dbx_source,
            audit_today="current_date()", audit_now="current_timestamp()",
        )
        inner = rendered.split("FROM (\n", 1)[1].rsplit("\n) q", 1)[0]
        runner_text = _runner_resolve(spec)
        uses_list_prices = ("billing", "list_prices") in spec.sources and spec.query_id not in gm.RAW_LIST_PRICES_IDS
        if _has_a_masked_case(runner_text) or uses_list_prices:
            continue
        if inner.strip() != runner_text.strip():
            mismatches.append(spec.query_id)
    assert mismatches == []


def test_list_prices_deduplicated():
    """dbt/macros/list_prices.sql folds two overlapping price rows into one open interval -- a
    usage row that matched both in the raw join now matches exactly one."""
    text = _model("cost_dollarized_by_sku_day")
    assert "{{ list_prices() }}" in text
    assert "{{ source('system_billing', 'list_prices') }}" not in text
    reconcile = _model("cost_chargeback_reconcile")
    assert "{{ source('system_billing', 'list_prices') }}" in reconcile
    assert "{{ list_prices() }}" not in reconcile

    rendered = stub.render(
        text, target_type="duckdb", windows=(30,),
        source=stub.parquet_source((ROOT / "tests" / "fixtures" / "parquet").as_posix()),
    )
    con = duckdb.connect()
    cur = con.execute(rendered)
    cols = [d[0] for d in cur.description]
    idx = {c: i for i, c in enumerate(cols)}
    got = sum(
        r[idx["net_usage_quantity"]] for r in cur.fetchall()
        if r[idx["window_days"]] == 30 and r[idx["sku_name"]] == "rec_SKU_FANOUT"
    )
    dbutil = _load(ROOT / "tests" / "dbutil.py", "dbutil_under_test")
    want = dbutil.usage_sum(
        f"sku_name = 'rec_SKU_FANOUT' AND usage_date >= {stub.DEFAULT_AUDIT_TODAY} - INTERVAL 30 DAY "
        f"AND usage_date < {stub.DEFAULT_AUDIT_TODAY}"
    )
    assert got == pytest.approx(want)
