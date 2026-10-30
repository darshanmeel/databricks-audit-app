"""Tests for tools/dbt_run.py's `run_build` swap-vs-discard decision on `--target dev` (T-39).

Context: tools/dbt_skeleton.py already covers construct_dbt_command / build_vars /
resolve_snapshot_dir (dbt_run.py's pure, subprocess-free helpers) -- those are untouched by T-39
and stay there. This file is new and covers only the behaviour T-39 added/changed: what
`run_build` does with the just-built tmp database once the (faked) `dbt build` subprocess exits,
for both --target dev (atomic swap or discard, decided from target/run_results.json, never from
the exit code alone) and --target test (must be completely unaffected).

The subprocess layer is fully faked (FakeDbtSubprocess below, monkeypatched onto
tools/dbt_run.py's own `subprocess.run`) -- no real dbt is ever invoked, per this task's hard
constraint. tools/dbt_run.py is loaded by file path (the same importlib.util pattern
tests/test_dbt_skeleton.py and tests/test_first_run.py already use), so this runs correctly
under a plain `pytest tests/test_dbt_run.py -q` regardless of pytest's rootdir/sys.path
behaviour.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "dbt_run.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("dbt_run_t39_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


dbt_run = _load_module()


class FakeCompletedProcess:
    def __init__(self, returncode: int):
        self.returncode = returncode


class FakeDbtSubprocess:
    """Stands in for `subprocess.run` inside tools/dbt_run.py's run_build: simulates exactly what
    a real `dbt build` invocation would leave behind (target/run_results.json under the project
    dir it was given, and -- when the build got far enough to touch the database -- content
    written to the AUDIT_DB path it was pointed at), then returns a scripted exit code. Never
    spawns a real process.
    """

    def __init__(
        self,
        returncode: int,
        results: list[dict] | None = None,
        write_run_results: bool = True,
        write_db_file: bool = True,
        db_content: bytes = b"NEW_BUILD_CONTENT",
    ):
        self.returncode = returncode
        self.results = results if results is not None else []
        self.write_run_results = write_run_results
        self.write_db_file = write_db_file
        self.db_content = db_content
        self.calls: list[dict] = []

    def __call__(self, cmd, cwd=None, env=None):
        self.calls.append({"cmd": list(cmd), "cwd": cwd, "env": dict(env) if env else {}})
        if self.write_run_results:
            project_dir = Path(cmd[cmd.index("--project-dir") + 1])
            target_dir = project_dir / "target"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "run_results.json").write_text(
                json.dumps({"results": self.results}), encoding="utf-8"
            )
        if self.write_db_file and env is not None and env.get("AUDIT_DB"):
            _guard_not_the_real_fixture_db(Path(env["AUDIT_DB"]))
            Path(env["AUDIT_DB"]).write_bytes(self.db_content)
        return FakeCompletedProcess(self.returncode)


# The real, shared fixture database every other test module's tests/conftest.py session fixture
# depends on. A test in this file must NEVER write to it -- it is rebuilt by
# tests/fixtures/build_fixtures.py + `dbt build --target test` and is orchestrator-owned, not
# something any one test file may clobber. This is a hard, structural guard (raises), not a
# convention: it fires even if a future test in this file forgets to request the
# isolated_audit_db fixture below or otherwise ends up with a real path in AUDIT_DB.
_REAL_FIXTURE_DB = (ROOT / "tests" / "db_audit_test.duckdb").resolve()
_TESTS_DIR = (ROOT / "tests").resolve()


def _guard_not_the_real_fixture_db(path: Path) -> None:
    resolved = path.resolve()
    if resolved == _REAL_FIXTURE_DB or resolved.name == "db_audit_test.duckdb" or _TESTS_DIR in resolved.parents:
        raise AssertionError(
            f"refusing to write a fake build's db content to {resolved} -- this resolves inside "
            "tests/ (or is literally named db_audit_test.duckdb), which looks like the real "
            "shared fixture database, not a throwaway tmp_path. Point AUDIT_DB at a tmp_path "
            "location instead (see the isolated_audit_db fixture)."
        )


def _model_result(uid: str, status: str) -> dict:
    return {"unique_id": f"model.audit.{uid}", "status": status}


@pytest.fixture(autouse=True)
def project_dir(tmp_path, monkeypatch):
    """Redirects DBT_PROJECT_DIR (and therefore target/run_results.json) at a throwaway dir, so
    these tests never touch the real repo's dbt/target/run_results.json. autouse: applies to
    every test in this module even if a test forgets to request it by name -- no test here may
    ever unlink or read the real dbt/target/run_results.json, which other concurrent dbt/pytest
    activity in this repo may depend on."""
    proj = tmp_path / "dbt_project"
    proj.mkdir()
    monkeypatch.setattr(dbt_run, "DBT_PROJECT_DIR", proj)
    return proj


@pytest.fixture(autouse=True)
def isolated_audit_db(tmp_path, monkeypatch):
    """Points AUDIT_DB at a throwaway tmp_path file, and clears AUDIT_TEST_DB, for every test in
    this module. autouse: tests/conftest.py's own session-scoped autouse `_app_env` fixture sets
    AUDIT_DB to the REAL, shared tests/db_audit_test.duckdb for the whole pytest session (so app
    tests read the built fixture db) -- without this override, any test here that omitted the
    fixture from its parameter list would silently inherit that real path and a
    FakeDbtSubprocess with write_db_file=True (the default) would write straight over it. Making
    this autouse means that inheritance is structurally impossible, not merely avoided by
    convention; _guard_not_the_real_fixture_db above is the second, independent line of defense
    in case this fixture is ever bypassed some other way (e.g. a test setting AUDIT_DB itself)."""
    path = tmp_path / "db" / "db_audit.duckdb"
    path.parent.mkdir(parents=True, exist_ok=True)  # real run_build does this before setting AUDIT_DB
    monkeypatch.setenv("AUDIT_DB", str(path))
    monkeypatch.delenv("AUDIT_TEST_DB", raising=False)
    return path


def _run_dev(monkeypatch, fake: FakeDbtSubprocess) -> int:
    monkeypatch.setattr(dbt_run, "subprocess", types.SimpleNamespace(run=fake))
    return dbt_run.run_build("dev", None, None)


# ---------------------------------------------------------------------------
# Clean build: every node ok -> swap, exit 0.
# ---------------------------------------------------------------------------


def test_clean_build_swaps_and_returns_0(project_dir, isolated_audit_db, monkeypatch):
    fake = FakeDbtSubprocess(
        returncode=0,
        results=[_model_result("f_a", "success"), _model_result("f_b", "success")],
    )
    rc = _run_dev(monkeypatch, fake)
    assert rc == 0
    assert isolated_audit_db.exists()
    assert isolated_audit_db.read_bytes() == b"NEW_BUILD_CONTENT"
    # tmp file was consumed by the atomic swap (os.replace), not left behind.
    assert list(isolated_audit_db.parent.glob("*-tmp-*")) == []


# ---------------------------------------------------------------------------
# Partial build: one node errored among many -> still swap, exit 3.
# ---------------------------------------------------------------------------


def test_one_errored_model_of_many_still_swaps_and_returns_3(project_dir, isolated_audit_db, monkeypatch, capsys):
    fake = FakeDbtSubprocess(
        returncode=1,  # dbt's own contract: nonzero the moment ANY node errors
        results=[
            _model_result("f_a", "success"),
            _model_result("f_b", "success"),
            _model_result("f_c", "success"),
            _model_result("f_access_classified_unmasked", "error"),
            {"unique_id": "test.audit.some_schema_test", "status": "pass"},  # ignored: not a model
        ],
    )
    rc = _run_dev(monkeypatch, fake)
    assert rc == 3
    assert isolated_audit_db.exists()
    assert isolated_audit_db.read_bytes() == b"NEW_BUILD_CONTENT"  # the new (partial) db WAS kept
    out = capsys.readouterr().out
    assert "WAS updated" in out
    assert "3 model(s) built" in out
    assert "1 model(s) failed" in out
    assert "NOT_ASSESSED" in out
    assert "Coverage & Gaps" in out


def test_skipped_models_count_as_failed_for_the_3_1_message(project_dir, isolated_audit_db, monkeypatch, capsys):
    fake = FakeDbtSubprocess(
        returncode=1,
        results=[
            _model_result("f_a", "success"),
            _model_result("f_downstream_of_a_failure", "skipped"),
        ],
    )
    rc = _run_dev(monkeypatch, fake)
    assert rc == 3
    out = capsys.readouterr().out
    assert "1 model(s) built" in out
    assert "1 model(s) failed" in out


# ---------------------------------------------------------------------------
# Nothing usable: zero successful models -> discard, previous db untouched, exit nonzero-not-3.
# ---------------------------------------------------------------------------


def test_all_models_failed_does_not_swap_and_leaves_previous_db_untouched(
    project_dir, isolated_audit_db, monkeypatch, capsys
):
    isolated_audit_db.parent.mkdir(parents=True, exist_ok=True)
    isolated_audit_db.write_bytes(b"OLD_GOOD_CONTENT")

    fake = FakeDbtSubprocess(
        returncode=1,
        results=[_model_result("f_a", "error"), _model_result("f_b", "skipped")],
    )
    rc = _run_dev(monkeypatch, fake)

    assert rc != 0
    assert rc != 3
    assert isolated_audit_db.read_bytes() == b"OLD_GOOD_CONTENT"  # unchanged
    assert list(isolated_audit_db.parent.glob("*-tmp-*")) == []  # tmp discarded, not left behind
    err = capsys.readouterr().err
    assert "NOT updated" in err


def test_all_models_failed_with_no_previous_db_leaves_none_created(project_dir, isolated_audit_db, monkeypatch):
    fake = FakeDbtSubprocess(
        returncode=1,
        results=[_model_result("f_a", "error")],
    )
    rc = _run_dev(monkeypatch, fake)
    assert rc != 0 and rc != 3
    assert not isolated_audit_db.exists()


# ---------------------------------------------------------------------------
# Missing run_results.json (parse/compile failure before any node ran) -> unusable, discard.
# ---------------------------------------------------------------------------


def test_missing_run_results_json_is_treated_as_unusable(project_dir, isolated_audit_db, monkeypatch):
    isolated_audit_db.parent.mkdir(parents=True, exist_ok=True)
    isolated_audit_db.write_bytes(b"OLD_GOOD_CONTENT")

    fake = FakeDbtSubprocess(
        returncode=2,
        write_run_results=False,  # dbt never got past parsing -- no run_results.json at all
        write_db_file=False,
    )
    rc = _run_dev(monkeypatch, fake)

    assert rc != 0
    assert rc != 3
    assert isolated_audit_db.read_bytes() == b"OLD_GOOD_CONTENT"
    assert not (project_dir / "target" / "run_results.json").exists()


# ---------------------------------------------------------------------------
# Exit-code contract: the discard branch's fallback (dbt's own returncode, or 1 if that code
# happened to already be 0 or 3) never itself produces the "swapped, partial" code 3. Real
# dbt-core only ever emits 0/1/2 here, but the fallback is asserted directly for robustness.
# ---------------------------------------------------------------------------


def test_discard_case_fallback_never_reports_3_even_if_dbt_rc_was_3(project_dir, isolated_audit_db, monkeypatch):
    fake = FakeDbtSubprocess(returncode=3, results=[_model_result("f_a", "error")])
    monkeypatch.setattr(dbt_run, "subprocess", types.SimpleNamespace(run=fake))
    rc = dbt_run.run_build("dev", None, None)
    assert rc != 3
    assert rc == 1
    assert not isolated_audit_db.exists()


# ---------------------------------------------------------------------------
# --target test must be completely unaffected: no swap machinery at all, dbt's own exit code
# propagates unchanged.
# ---------------------------------------------------------------------------


def test_target_test_propagates_dbt_exit_code_unchanged_on_error(project_dir, monkeypatch):
    fake = FakeDbtSubprocess(
        returncode=1,
        results=[_model_result("f_a", "success"), _model_result("f_b", "error")],
    )
    monkeypatch.setattr(dbt_run, "subprocess", types.SimpleNamespace(run=fake))
    rc = dbt_run.run_build("test", None, None)
    assert rc == 1  # NOT remapped to 3 -- the gate must still fail hard on any error


def test_target_test_propagates_0_on_clean_build(project_dir, monkeypatch):
    fake = FakeDbtSubprocess(returncode=0, results=[_model_result("f_a", "success")])
    monkeypatch.setattr(dbt_run, "subprocess", types.SimpleNamespace(run=fake))
    rc = dbt_run.run_build("test", None, None)
    assert rc == 0


def test_target_test_never_touches_audit_db_env(project_dir, monkeypatch):
    monkeypatch.delenv("AUDIT_DB", raising=False)
    fake = FakeDbtSubprocess(returncode=1, results=[_model_result("f_a", "error")])
    monkeypatch.setattr(dbt_run, "subprocess", types.SimpleNamespace(run=fake))
    dbt_run.run_build("test", None, None)
    call_env = fake.calls[0]["env"]
    # target=="test" never enters the dev-only tmp-db/AUDIT_DB-override wiring at all.
    assert "AUDIT_DB" not in call_env
