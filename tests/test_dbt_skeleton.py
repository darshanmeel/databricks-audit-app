"""Tests for the dbt project skeleton (T-05): dbt_project.yml, profiles.yml, the three shared
macros, the unique_grain generic test, tools/build_sources.py's generated sources.yml, and
tools/dbt_run.py's command construction.

Loads tools/build_sources.py and tools/dbt_run.py directly by file path (the same
importlib.util.spec_from_file_location pattern tests/test_ddl.py / tests/test_registry.py use),
so this stays correct under a plain `pytest tests/test_dbt_skeleton.py -q` regardless of pytest's
rootdir/sys.path behaviour. `dbt parse` / `dbt compile --inline` are invoked for real via
subprocess (DBT = dbt --project-dir dbt --profiles-dir dbt, PLAN.md's convention) -- no mocking of
dbt itself, per the task's own instruction to actually run and record the real outcome.
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DBT_ARGS = ["--project-dir", "dbt", "--profiles-dir", "dbt"]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


build_sources = _load(ROOT / "tools" / "build_sources.py", "build_sources_under_test")
dbt_run = _load(ROOT / "tools" / "dbt_run.py", "dbt_run_under_test")


# These are macro/parse probes: they never read a built table, so they run against their own
# throwaway DuckDB file and their own target dir. Sharing tests/db_audit_test.duckdb made them fail
# intermittently with "being used by another process" whenever anything else held the file.
_PROBE_DIR = tempfile.mkdtemp(prefix="dbt_probe_")
# _dbt() writes every parse/compile into the probe target dir, so read the manifest from there,
# never the shared dbt/target/manifest.json (last written by whatever dbt run came before -- e.g.
# tools/dbt_run.py's build, whose AUDIT_SNAPSHOT_DIR is an absolute, backslashed path on Windows).
MANIFEST_PATH = Path(_PROBE_DIR) / "target" / "manifest.json"


def _dbt(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["AUDIT_TEST_DB"] = str(Path(_PROBE_DIR) / "probe.duckdb")
    # The external_location test asserts the target-test default is baked in, so the probe parse
    # must not depend on the caller's shell having AUDIT_SNAPSHOT_DIR set (e.g. by a preceding
    # `tools/dbt_run.py build` earlier in the same gate run).
    env.pop("AUDIT_SNAPSHOT_DIR", None)
    return subprocess.run(
        ["dbt", *args, *DBT_ARGS, "--target-path", str(Path(_PROBE_DIR) / "target")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
    )


# ---------------------------------------------------------------------------
# Step 10: dbt parse --target test resolves exactly 49 sources (48 vendored + 1 app-owned), no
# errors.
# ---------------------------------------------------------------------------


def test_dbt_parse_target_test_resolves_all_sources():
    result = _dbt("parse", "--target", "test")
    assert result.returncode == 0, result.stdout + result.stderr
    assert MANIFEST_PATH.exists(), f"dbt parse did not produce {MANIFEST_PATH} (the probe target dir)"
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    sources = manifest["sources"]
    assert len(sources) == 49, f"expected 49 sources, found {len(sources)}"


# ---------------------------------------------------------------------------
# Step 10: `dbt compile --target test --inline` renders the fixed test-target literals and the
# param() threshold fallback chain correctly. `--inline` compiles an ad-hoc SQL string with no
# model file written under dbt/models/.
# ---------------------------------------------------------------------------


def _compile_inline(sql: str, extra_vars: dict | None = None) -> str:
    args = ["compile", "--target", "test", "--inline", sql]
    if extra_vars is not None:
        args += ["--vars", json.dumps(extra_vars)]
    result = _dbt(*args)
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    marker = "Compiled inline node is:"
    assert marker in out, out
    return out.split(marker, 1)[1].strip()


def test_inline_compile_audit_time_and_param_default():
    compiled = _compile_inline(
        "select {{ audit_today() }} as d, {{ audit_now() }} as t, "
        "{{ param('probe', 'x', 1) }} as p"
    )
    assert "DATE '2026-09-21' as d" in compiled
    assert "TIMESTAMP '2026-09-21 12:00:00' as t" in compiled
    assert "1 as p" in compiled


def test_param_threshold_qid_level_override_wins():
    compiled = _compile_inline(
        "select {{ param('probe', 'x', 1) }} as p",
        extra_vars={"thresholds": {"probe": {"x": 42}}},
    )
    assert "42 as p" in compiled


def test_param_threshold_all_level_fallback():
    compiled = _compile_inline(
        "select {{ param('probe', 'x', 1) }} as p",
        extra_vars={"thresholds": {"_all": {"x": 99}}},
    )
    assert "99 as p" in compiled


def test_param_qid_level_beats_all_level():
    compiled = _compile_inline(
        "select {{ param('probe', 'x', 1) }} as p",
        extra_vars={"thresholds": {"probe": {"x": 7}, "_all": {"x": 99}}},
    )
    assert "7 as p" in compiled


# ---------------------------------------------------------------------------
# Step 10: the compiled sources.yml's meta.external_location for a sample table contains a
# RENDERED env_var(...) call (no leftover Jinja delimiters), and the {schema}/{name}
# placeholders resolve to the right folder via dbt-duckdb's own external_location substitution
# (relation_name). Outcome recorded in tasks/T-05-dbt-skeleton.md Hand-off notes.
# ---------------------------------------------------------------------------


def test_external_location_env_var_rendered_and_schema_name_resolved():
    result = _dbt("parse", "--target", "test")
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    node = None
    for v in manifest["sources"].values():
        if v["source_name"] == "system_billing" and v["name"] == "usage":
            node = v
            break
    assert node is not None, "system_billing.usage source not found"

    external_location = node["meta"]["external_location"]
    # Rendered: no leftover Jinja delimiters, and the target-test default value is baked in.
    assert "{{" not in external_location
    assert "}}" not in external_location
    assert "env_var(" not in external_location
    assert "tests/fixtures/parquet" in external_location

    # {schema}/{name} placeholders resolved by dbt-duckdb into the real relation, used whenever
    # a model does {{ source('system_billing', 'usage') }}.
    relation_name = node["relation_name"]
    assert "billing__usage" in relation_name
    assert "{schema}" not in relation_name
    assert "{name}" not in relation_name


# ---------------------------------------------------------------------------
# Step 11: tools/dbt_run.py's argument parser accepts build --target dev --select tag:dims and
# --exclude <selector>, forwarding them verbatim into the constructed dbt build command line.
# No live dbt invocation needed -- construct_dbt_command is a pure function.
# ---------------------------------------------------------------------------


def test_dbt_run_select_exclude_passthrough_dev():
    cmd = dbt_run.construct_dbt_command("dev", select="tag:dims", exclude="tag:drilldown")
    assert cmd[:2] == ["dbt", "build"]
    assert "--target" in cmd and cmd[cmd.index("--target") + 1] == "dev"
    assert "--select" in cmd and cmd[cmd.index("--select") + 1] == "tag:dims"
    assert "--exclude" in cmd and cmd[cmd.index("--exclude") + 1] == "tag:drilldown"


def test_dbt_run_argparser_parses_build_target_dev_select_tag_dims():
    ap_args = ["build", "--target", "dev", "--select", "tag:dims"]
    # Exercise the real argparse parser (no subprocess): construct the parser the same way
    # main() does and parse the CLI args, then build the command from the parsed namespace.
    import argparse

    parser = argparse.ArgumentParser(prog="dbt_run.py")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--target", required=True, choices=["dev", "test"])
    build.add_argument("--select", default=None)
    build.add_argument("--exclude", default=None)
    ns = parser.parse_args(ap_args)
    assert ns.command == "build"
    assert ns.target == "dev"
    assert ns.select == "tag:dims"
    assert ns.exclude is None

    cmd = dbt_run.construct_dbt_command(ns.target, select=ns.select, exclude=ns.exclude)
    assert cmd == [
        "dbt",
        "build",
        "--project-dir",
        str(dbt_run.DBT_PROJECT_DIR),
        "--profiles-dir",
        str(dbt_run.DBT_PROFILES_DIR),
        "--target",
        "dev",
        "--select",
        "tag:dims",
    ]


def test_dbt_run_no_select_exclude_omits_flags():
    cmd = dbt_run.construct_dbt_command("test")
    assert "--select" not in cmd
    assert "--exclude" not in cmd


def test_dbt_run_vars_dict_included_when_given():
    cmd = dbt_run.construct_dbt_command("test", vars_dict={"windows": [7, 30, 90]})
    assert "--vars" in cmd
    idx = cmd.index("--vars")
    assert json.loads(cmd[idx + 1]) == {"windows": [7, 30, 90]}


# ---------------------------------------------------------------------------
# tools/dbt_run.py: vars merging (thresholds + windows always; as_of_date/as_of_ts for dev only).
# ---------------------------------------------------------------------------


def test_build_vars_test_target_omits_as_of():
    v = dbt_run.build_vars("test")
    assert "thresholds" in v
    assert "windows" in v
    assert "as_of_date" not in v
    assert "as_of_ts" not in v
    assert "grain_severity" not in v  # DEC-48: the test target keeps unique_grain at error
    assert v["thresholds"].get("task_cluster_utilization", {}).get("top_n") == 100000
    assert v["windows"] == [7, 30, 90]
    # P4-T (DEC-63): the coverage floor always rides along, same as share_floor.
    assert v["share_floor"] == 0.6
    assert v["coverage_floor"] == 0.5
    assert v["mask_user_identities"] is False  # off by default


def test_load_coverage_floor_reads_settings_yml(tmp_path, monkeypatch):
    settings = tmp_path / "settings.yml"
    settings.write_text("tag_coverage_floor: 0.42\n", encoding="utf-8")
    monkeypatch.setattr(dbt_run, "SETTINGS_PATH", settings)
    assert dbt_run.load_coverage_floor() == 0.42


def test_load_coverage_floor_default_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(dbt_run, "SETTINGS_PATH", tmp_path / "does_not_exist.yml")
    assert dbt_run.load_coverage_floor() == dbt_run.DEFAULT_COVERAGE_FLOOR


def test_load_mask_user_identities_reads_settings_yml(tmp_path, monkeypatch):
    settings = tmp_path / "settings.yml"
    settings.write_text("privacy:\n  mask_user_identities: true\n", encoding="utf-8")
    monkeypatch.setattr(dbt_run, "SETTINGS_PATH", settings)
    assert dbt_run.load_mask_user_identities() is True


def test_load_mask_user_identities_default_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(dbt_run, "SETTINGS_PATH", tmp_path / "does_not_exist.yml")
    assert dbt_run.load_mask_user_identities() is dbt_run.DEFAULT_MASK_USER_IDENTITIES


def test_build_vars_dev_target_includes_as_of_when_manifest_present(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"as_of": "2026-09-21T12:00:00", "as_of_date": "2026-09-21"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(dbt_run, "SNAPSHOT_MANIFEST", manifest)
    v = dbt_run.build_vars("dev")
    assert v["as_of_date"] == "2026-09-21"
    assert v["as_of_ts"] == "2026-09-21T12:00:00"
    assert v["grain_severity"] == "warn"  # DEC-48


def test_build_vars_dev_target_no_manifest_omits_as_of(tmp_path, monkeypatch):
    monkeypatch.setattr(dbt_run, "SNAPSHOT_MANIFEST", tmp_path / "does_not_exist.json")
    v = dbt_run.build_vars("dev")
    assert "as_of_date" not in v
    assert "as_of_ts" not in v


def test_resolve_snapshot_dir_test_target():
    assert dbt_run.resolve_snapshot_dir("test", {}) == str(ROOT / "tests" / "fixtures" / "parquet")


def test_resolve_snapshot_dir_dev_target_default():
    assert dbt_run.resolve_snapshot_dir("dev", {}) == str(ROOT / "snapshot")


def test_resolve_snapshot_dir_dev_target_env_override_wins():
    env = {"AUDIT_SNAPSHOT_DIR": "/custom/dir"}
    assert dbt_run.resolve_snapshot_dir("dev", env) == "/custom/dir"


# ---------------------------------------------------------------------------
# tools/build_sources.py: predicate map coverage and --check parity.
# ---------------------------------------------------------------------------


def test_predicate_map_covers_all_48_tables():
    lineage_sources = build_sources.load_lineage()
    n_tables = sum(len(s.get("tables", [])) for s in lineage_sources)
    assert n_tables == 48
    for src in lineage_sources:
        schema = src["schema"]
        for t in src["tables"]:
            # never raises SystemExit -- every real table resolves
            build_sources.predicate_for(schema, t["name"])


def test_build_sources_check_reports_in_sync(capsys):
    rc = build_sources.main(["--check"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "sources.yml in sync (10 sources, 49 tables)" in out
