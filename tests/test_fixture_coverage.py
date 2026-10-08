"""Tests for tools/fixture_coverage.py (T-24).

Almost every test here builds its own tiny fake `config/grains/` + `tests/test_findings/` tree
under `tmp_path` and calls the module's functions directly against that tree -- it never depends
on (and cannot be broken by) the real repo's id count, so a later fixture batch landing more query
ids can never make this file fail. `test_real_repo_reports_full_coverage` is the one deliberate
exception (an integration-style check against the real repo, per this task's Steps 3). It derives
its expected count from `fc.executable_ids()` rather than pinning a magic number. It reads files
only -- no `dbt build`, no DuckDB connection, nothing that could race a concurrent `tools/gate.py`
run elsewhere.

Loaded via importlib.util.spec_from_file_location (the same pattern tests/test_sync_queries.py
uses for tools/sync_queries.py), so this file is correct under a plain `pytest <file> -q`
invocation regardless of pytest's rootdir/sys.path behaviour.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FC_PATH = ROOT / "tools" / "fixture_coverage.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("fixture_coverage_under_test", FC_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


fc = _load_module()


# --------------------------------------------------------------------------------------------
# extract_referenced_ids -- what counts as a genuine rows()/dbutil.rows() reference.
# --------------------------------------------------------------------------------------------
def test_literal_double_and_single_quotes_both_match():
    text = '''
import dbutil

def test_a():
    dbutil.rows("id_double", 30)

def test_b():
    dbutil.rows('id_single', 30)
'''
    ids = fc.extract_referenced_ids(text, "fake.py")
    assert ids == {"id_double", "id_single"}


def test_bare_rows_import_form_matches():
    text = '''
from dbutil import rows

def test_a():
    rows("id_bare", 30)
'''
    assert fc.extract_referenced_ids(text, "fake.py") == {"id_bare"}


def test_query_id_keyword_form_matches_both_quote_styles():
    text = '''
import dbutil
from dbutil import rows

def test_a():
    dbutil.rows(query_id="id_kw_double", window_days=30)

def test_b():
    rows(query_id='id_kw_single', window_days=30)
'''
    assert fc.extract_referenced_ids(text, "fake.py") == {"id_kw_double", "id_kw_single"}


def test_variable_assigned_then_passed_to_rows_matches():
    """The real, load-bearing convention in tests/test_findings/test_overview_dbu_by_sku.py and
    others: `query_id = "<id>"` on its own line, then `rows(query_id, ...)` later in the SAME
    function -- never a literal directly inside the call."""
    text = '''
from dbutil import rows

def test_a():
    query_id = "id_via_var"
    rows_30 = rows(query_id, 30)
    assert rows_30
'''
    assert fc.extract_referenced_ids(text, "fake.py") == {"id_via_var"}


def test_loop_over_list_literal_matches_every_element():
    """The real convention in tests/test_findings/test_compute_activity.py: a module-level
    ALL_IDS list of literals, looped with `for qid in ALL_IDS: dbutil.rows(qid, ...)`."""
    text = '''
import dbutil

ALL_IDS = ["id_loop_a", "id_loop_b", "id_loop_c"]

def test_all():
    for qid in ALL_IDS:
        dbutil.rows(qid, 30)
'''
    assert fc.extract_referenced_ids(text, "fake.py") == {"id_loop_a", "id_loop_b", "id_loop_c"}


def test_variable_scoped_to_its_own_function_does_not_leak_ids_without_a_call():
    """A variable assigned in one function is never treated as evidence for a DIFFERENT
    function's un-called rows() argument name -- each function's local bindings start as a copy
    of the module-level ones, not a shared mutable dict."""
    text = '''
import dbutil

def test_a():
    qid = "id_only_in_a"
    # never passed to rows() in this function

def test_b():
    dbutil.rows(qid, 30)  # qid is not defined in this function's own scope
'''
    assert fc.extract_referenced_ids(text, "fake.py") == set()


def test_comment_only_mention_is_not_a_reference():
    """Quality requirement 1's probe, made permanent: an id that appears ONLY inside a `#`
    comment, a module docstring, or "next: <id>"-style prose is never counted -- comments are not
    part of the AST, and docstring prose is a bare Expr statement, never a call. A naive
    `query_id in open(file).read()` scan WOULD wrongly count both of these; this is exactly the
    false-pass this checker must avoid."""
    text = '''
"""fake module.

next: comment_only_id is a good follow-up once a fixture exists. See also
dbutil.rows("docstring_example_id", 30) for the shape other modules use.
"""
import dbutil

# TODO: dbutil.rows("todo_comment_id", 30) once the builder lands.
def test_real():
    dbutil.rows("real_id", 30)
'''
    ids = fc.extract_referenced_ids(text, "fake.py")
    assert ids == {"real_id"}
    assert "comment_only_id" not in ids
    assert "docstring_example_id" not in ids
    assert "todo_comment_id" not in ids


def test_unparseable_file_contributes_no_ids_without_raising():
    assert fc.extract_referenced_ids("def broken(:\n", "fake.py") == set()


def test_load_tested_ids_walks_every_py_file_and_skips_pycache(tmp_path: Path):
    tf_dir = tmp_path / "test_findings"
    tf_dir.mkdir()
    (tf_dir / "test_one.py").write_text('import dbutil\ndbutil.rows("id_one", 30)\n', encoding="utf-8")
    sub = tf_dir / "__pycache__"
    sub.mkdir()
    (sub / "test_one.cpython-312.pyc").write_bytes(b"not python source")
    ids = fc.load_tested_ids(tf_dir)
    assert ids == {"id_one"}


# --------------------------------------------------------------------------------------------
# load_grains + duplicate_grains
# --------------------------------------------------------------------------------------------
def test_load_grains_reads_every_yml_under_the_dir(tmp_path: Path):
    grains_dir = tmp_path / "grains"
    grains_dir.mkdir()
    (grains_dir / "batch_a.yml").write_text("id_a:\n  - col1\n  - col2\n", encoding="utf-8")
    (grains_dir / "batch_b.yml").write_text("id_b: [col3]\n", encoding="utf-8")
    columns_by_id, files_by_id = fc.load_grains(grains_dir)
    assert columns_by_id == {"id_a": ["col1", "col2"], "id_b": ["col3"]}
    assert files_by_id["id_a"] == [grains_dir / "batch_a.yml"]
    assert files_by_id["id_b"] == [grains_dir / "batch_b.yml"]


def test_duplicate_grain_across_two_files_is_detected(tmp_path: Path):
    grains_dir = tmp_path / "grains"
    grains_dir.mkdir()
    (grains_dir / "batch_a.yml").write_text("dup_id: [col1]\n", encoding="utf-8")
    (grains_dir / "batch_b.yml").write_text("dup_id: [col1, col2]\n", encoding="utf-8")
    _, files_by_id = fc.load_grains(grains_dir)
    dupes = fc.duplicate_grains(files_by_id)
    assert set(dupes.keys()) == {"dup_id"}
    assert len(dupes["dup_id"]) == 2


def test_no_false_duplicate_when_id_defined_once(tmp_path: Path):
    grains_dir = tmp_path / "grains"
    grains_dir.mkdir()
    (grains_dir / "batch_a.yml").write_text("solo_id: [col1]\n", encoding="utf-8")
    _, files_by_id = fc.load_grains(grains_dir)
    assert fc.duplicate_grains(files_by_id) == {}


# --------------------------------------------------------------------------------------------
# load_model_grain_columns + grain_column_mismatches
# --------------------------------------------------------------------------------------------
def _write_findings_yml(path: Path, query_id: str, columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "models:\n"
        f"- name: f_{query_id}\n"
        "  meta:\n"
        f"    query_id: {query_id}\n"
        "  data_tests:\n"
        "  - unique_grain:\n"
        "      arguments:\n"
        "        columns:\n"
        + "".join(f"        - {c}\n" for c in columns),
        encoding="utf-8",
    )


def test_grain_column_mismatch_detected(tmp_path: Path):
    models_dir = tmp_path / "models"
    _write_findings_yml(models_dir / "d" / "_findings__d.yml", "mismatch_id", ["window_days", "col_a", "col_b"])
    model_columns = fc.load_model_grain_columns(models_dir)
    grain_columns = {"mismatch_id": ["col_a", "col_x"]}  # differs from the model's [col_a, col_b]
    mismatches = fc.grain_column_mismatches(grain_columns, model_columns)
    assert len(mismatches) == 1
    qid, grain_cols, model_cols = mismatches[0]
    assert qid == "mismatch_id"
    assert grain_cols == ["col_a", "col_x"]
    assert model_cols == ["col_a", "col_b"]


def test_grain_column_match_is_not_reported(tmp_path: Path):
    models_dir = tmp_path / "models"
    _write_findings_yml(models_dir / "d" / "_findings__d.yml", "match_id", ["window_days", "col_a", "col_b"])
    model_columns = fc.load_model_grain_columns(models_dir)
    grain_columns = {"match_id": ["col_a", "col_b"]}
    assert fc.grain_column_mismatches(grain_columns, model_columns) == []


def test_load_model_grain_columns_returns_none_when_no_generated_yml_exists(tmp_path: Path):
    assert fc.load_model_grain_columns(tmp_path / "does_not_exist") is None


def test_grain_column_mismatches_short_circuits_when_model_columns_is_none():
    assert fc.grain_column_mismatches({"any_id": ["x"]}, None) == []


# --------------------------------------------------------------------------------------------
# Report: the pass / fail-one-half / fail-both classification, end to end over fabricated data.
# --------------------------------------------------------------------------------------------
def test_report_classifies_pass_grain_only_test_only_and_neither():
    ids = ["pass_id", "grain_only_id", "test_only_id", "neither_id"]
    grain_columns = {"pass_id": ["c"], "grain_only_id": ["c"]}
    grain_files = {"pass_id": [Path("a.yml")], "grain_only_id": [Path("a.yml")]}
    tested_ids = {"pass_id", "test_only_id"}
    report = fc.Report(ids, grain_columns, grain_files, tested_ids, model_columns={})

    assert report.covered_count == 1
    assert report.total == 4
    assert report.summary_line() == "fixture coverage: 1/4 models have grain + pytest assertions"
    assert not report.ok()
    lines = report.offender_lines()
    assert "grain_only_id: missing pytest assertion" in lines
    assert "test_only_id: missing grain" in lines
    assert "neither_id: missing both" in lines
    assert not any(line.startswith("pass_id:") for line in lines)


def test_report_ok_when_all_ids_have_grain_and_test_and_no_extra_errors():
    ids = ["only_id"]
    grain_columns = {"only_id": ["c"]}
    grain_files = {"only_id": [Path("a.yml")]}
    tested_ids = {"only_id"}
    report = fc.Report(ids, grain_columns, grain_files, tested_ids, model_columns={})
    assert report.ok()
    assert report.summary_line() == "fixture coverage: 1/1 models have grain + pytest assertions"
    assert report.offender_lines() == []


def test_report_not_ok_when_duplicate_grain_present_even_if_coverage_is_complete():
    ids = ["dup_id"]
    grain_columns = {"dup_id": ["c"]}
    grain_files = {"dup_id": [Path("a.yml"), Path("b.yml")]}
    tested_ids = {"dup_id"}
    report = fc.Report(ids, grain_columns, grain_files, tested_ids, model_columns={})
    assert not report.ok()
    assert any("duplicate grain" in line for line in report.offender_lines())


def test_report_not_ok_when_grain_column_mismatch_present_even_if_coverage_is_complete():
    ids = ["bad_col_id"]
    grain_columns = {"bad_col_id": ["c_wrong"]}
    grain_files = {"bad_col_id": [Path("a.yml")]}
    tested_ids = {"bad_col_id"}
    model_columns = {"bad_col_id": ["window_days", "c_right"]}
    report = fc.Report(ids, grain_columns, grain_files, tested_ids, model_columns)
    assert not report.ok()
    assert any("do not match generated model" in line for line in report.offender_lines())


# --------------------------------------------------------------------------------------------
# main() end to end over a fabricated tree (monkeypatching the module's own directory constants
# and executable_ids(), never the real repo).
# --------------------------------------------------------------------------------------------
def _build_fake_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    grains_dir = tmp_path / "config" / "grains"
    tf_dir = tmp_path / "tests" / "test_findings"
    models_dir = tmp_path / "dbt_models"
    grains_dir.mkdir(parents=True)
    tf_dir.mkdir(parents=True)
    (grains_dir / "batch.yml").write_text("covered_id: [colA]\n", encoding="utf-8")
    (tf_dir / "test_covered.py").write_text(
        'import dbutil\n\n\ndef test_x():\n    dbutil.rows("covered_id", 30)\n',
        encoding="utf-8",
    )
    return grains_dir, tf_dir, models_dir


def test_main_check_success_prints_exact_line_and_exits_0(tmp_path, monkeypatch, capsys):
    grains_dir, tf_dir, models_dir = _build_fake_tree(tmp_path)
    monkeypatch.setattr(fc, "GRAINS_DIR", grains_dir)
    monkeypatch.setattr(fc, "TEST_FINDINGS_DIR", tf_dir)
    monkeypatch.setattr(fc, "FINDINGS_MODELS_DIR", models_dir)
    monkeypatch.setattr(fc, "executable_ids", lambda: ["covered_id"])

    rc = fc.main(["--check"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == "fixture coverage: 1/1 models have grain + pytest assertions"


def test_main_check_failure_lists_offenders_and_exits_1(tmp_path, monkeypatch, capsys):
    grains_dir, tf_dir, models_dir = _build_fake_tree(tmp_path)
    monkeypatch.setattr(fc, "GRAINS_DIR", grains_dir)
    monkeypatch.setattr(fc, "TEST_FINDINGS_DIR", tf_dir)
    monkeypatch.setattr(fc, "FINDINGS_MODELS_DIR", models_dir)
    monkeypatch.setattr(fc, "executable_ids", lambda: ["covered_id", "uncovered_id"])

    rc = fc.main(["--check"])
    out = capsys.readouterr().out.strip().splitlines()
    assert rc == 1
    assert out == ["uncovered_id: missing both"]


def test_main_default_mode_always_exits_0_even_with_offenders(tmp_path, monkeypatch, capsys):
    grains_dir, tf_dir, models_dir = _build_fake_tree(tmp_path)
    monkeypatch.setattr(fc, "GRAINS_DIR", grains_dir)
    monkeypatch.setattr(fc, "TEST_FINDINGS_DIR", tf_dir)
    monkeypatch.setattr(fc, "FINDINGS_MODELS_DIR", models_dir)
    monkeypatch.setattr(fc, "executable_ids", lambda: ["covered_id", "uncovered_id"])

    rc = fc.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "fixture coverage: 1/2 models have grain + pytest assertions" in out
    assert "uncovered_id: missing both" in out


# --------------------------------------------------------------------------------------------
# Integration: the real repo. The one test in this file allowed to depend on real repo state
# (Steps 3's own "plus one integration-style test that runs the checker against the REAL repo").
# Reads files only (registry headers, config/grains/*.yml, tests/test_findings/**/*.py text,
# dbt/models/findings/**/_findings__*.yml) -- no dbt build, no DuckDB connection.
# --------------------------------------------------------------------------------------------
def test_real_repo_reports_full_coverage():
    report = fc.build_report()
    n = len(fc.executable_ids())
    assert report.total == n
    assert report.covered_count == n, report.offender_lines()
    assert report.ok(), report.offender_lines()
    assert report.summary_line() == f"fixture coverage: {n}/{n} models have grain + pytest assertions"
