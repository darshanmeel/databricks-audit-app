"""tests/conftest.py

Session-scoped setup every tests/test_app/*.py test relies on (PLAN.md 6.5, DEC-25/DEC-26). No
tests/conftest.py existed on disk before this task (checked at write time) -- this is a new file,
not an extension of an earlier task's incidental work. `tests/fixtures/manifest.json` (a fixture
"parquet contents" manifest the task file's context list names as "created by T-07") does not
exist anywhere on this board either -- see Hand-off notes in tasks/T-25-store-shell-theme-filters
.md; it is unrelated to the snapshot manifest.json this file synthesises below.

  * The fixture database (tests/db_audit_test.duckdb) build is ORCHESTRATOR-ONLY: this fixture
    never runs `python tools/dbt_run.py build --target test` itself unless AUDIT_BUILD_FIXTURES=1
    is set in the environment (multiple agents share this one db file, so an agent-triggered
    rebuild here would race a concurrent orchestrator/agent build). When the db looks stale (newer
    source file than the db's own mtime, `_stale_sources()` below) and AUDIT_BUILD_FIXTURES is not
    "1", this prints one WARNING line naming the newer files and continues against the existing
    (possibly stale) db -- it never fails the session over staleness alone.
  * AUDIT_DB points the app at that database file (absolute path).
  * AUDIT_CONFIG_DIR (DEC-26) points at a session-scoped tmp_path_factory directory seeded with a
    COPY of the repo's config/ (settings.yml, thresholds.yml -- including the
    task_cluster_utilization seed -- and tag_aliases.yml), so no test ever writes the repo's own
    config/ files (PLAN.md 11 point 4: `git status --short` after `pytest -q`, run inside
    `tools/gate.py --quick`, must show only this task's owned paths). T-28's own config
    round-trip tests (a later task) reuse this exact tmp-path strategy.
  * AUDIT_SNAPSHOT_MANIFEST / AUDIT_RUN_RESULTS point at two files this fixture SYNTHESISES into
    the session tmp dir at session start, rather than at the repo's real snapshot/manifest.json
    (which does not exist in dev -- tools/snapshot.py is never run here -- and would leak a real
    future snapshot into every test and into `--boot` if it ever did) or the live
    dbt/target/run_results.json (which reflects whatever another concurrent agent's last dbt
    invocation happened to be -- often not a full model build at all, e.g. a single
    `sql_operation` from `dbt debug`/a connection probe). The synthesis is read-only against
    already-built state, never a build of its own:
      - run_results: one `status: "success"` result per `findings.f_*` and `dims.*` table
        actually present in the built test db right now (a read-only DuckDB connection against
        information_schema.tables) -- a faithful, deterministic stand-in for "the last build was
        clean", which is the only thing this task's own tests need by default.
      - snapshot manifest: `as_of`/`as_of_date` pinned to the fixed fixture instant
        (2026-09-21T12:00:00, matching tests/fixtures/base.py's AS_OF and the pinned
        dbt/macros/audit_time.sql test-target literals), and one `state: "ok"` entry per
        `tests/fixtures/parquet/<schema>__<table>/` folder with `rows` = the real row count read
        from its parquet files (0 for an empty-typed source, >0 for a populated one -- min/max
        time are left None, not needed by any test this task owns).
    The two Step 10 (g)/(h) scenario tests still override both env vars for just that one test via
    monkeypatch.setenv, pointing at the crafted fixtures under tests/test_app/scenarios/ (DEC-20).

Every test module gets this for free via the autouse session fixture below; no test file needs to
import or apply it explicitly.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "tests" / "db_audit_test.duckdb"
PARQUET_DIR = ROOT / "tests" / "fixtures" / "parquet"

AS_OF = "2026-09-21T12:00:00"
AS_OF_DATE = "2026-09-21"

SNAPSHOT_TOOL_PATH = ROOT / "tools" / "snapshot.py"
# T-71: the metastore the synthesised snapshot "connected through". Parsed by the exporter's own
# parse_metastore_id (DEC-54: a fixture for a producer/consumer format comes from the PRODUCER).
FIXTURE_METASTORE_ID = "aws:us-east-1:00000000-0000-4000-8000-000000000071"


def _fixture_metastore() -> dict | None:
    spec = importlib.util.spec_from_file_location("snapshot_for_conftest", SNAPSHOT_TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.parse_metastore_id(FIXTURE_METASTORE_ID)

# Deliberately excludes dbt/profiles.yml -- its `settings:` block (DEC-47) only tunes DuckDB's
# runtime memory/temp-dir, never a model's content, so editing it (as T-25 does, DEC-47 point 1)
# must never be reported as making the already-built database stale.
_STALENESS_GLOBS = [
    "tests/fixtures/parquet/**/*.parquet",
    "tests/fixtures/*.py",
    "dbt/models/**/*.sql",
    "dbt/models/**/*.yml",
    "dbt/seeds/*.csv",
    "dbt/macros/**/*.sql",
    "dbt/dbt_project.yml",
    # tag_aliases.yml and settings.yml (tag_share_floor) feed the build as --vars through
    # tools/dbt_run.py's build_vars, exactly like thresholds.yml, so they change model content too.
    "config/thresholds.yml",
    "config/tag_aliases.yml",
    "config/settings.yml",
]


def _stale_sources() -> list[Path]:
    """Every file from _STALENESS_GLOBS newer than tests/db_audit_test.duckdb's own mtime (or,
    if the db does not exist at all, every matching file). [] means "not stale"."""
    if not DB_PATH.exists():
        return [p for pattern in _STALENESS_GLOBS for p in ROOT.glob(pattern) if p.is_file()]
    db_mtime = DB_PATH.stat().st_mtime
    newer: list[Path] = []
    for pattern in _STALENESS_GLOBS:
        for path in ROOT.glob(pattern):
            if path.is_file() and path.stat().st_mtime > db_mtime:
                newer.append(path)
    return newer


def _build_fixture_db() -> None:
    build_fixtures = ROOT / "tests" / "fixtures" / "build_fixtures.py"
    if build_fixtures.exists():
        subprocess.run([sys.executable, str(build_fixtures)], cwd=str(ROOT), check=True)
    # Build goes through tools/dbt_run.py (DEC-12), the same as tools/gate.py's dbt_build_test
    # step, so its --vars wiring (tag_aliases, share_floor, thresholds -- T-63/DEC-60) reach dbt --
    # a bare `dbt build` here would omit them and silently drop dim_workspace's tag-attribute
    # columns.
    subprocess.run(
        [sys.executable, str(ROOT / "tools" / "dbt_run.py"), "build", "--target", "test"],
        cwd=str(ROOT),
        check=True,
    )


def _warn_stale(newer: list[Path]) -> None:
    names = [p.relative_to(ROOT).as_posix() for p in newer[:10]]
    more = " ... (+%d more)" % (len(newer) - 10) if len(newer) > 10 else ""
    print(
        "WARNING: tests/db_audit_test.duckdb may be stale -- newer than: "
        + ", ".join(names)
        + more
        + ". Builds are orchestrator-only; set AUDIT_BUILD_FIXTURES=1 to rebuild here. "
        "Continuing against the existing database."
    )


def _seed_config_dir(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    src = ROOT / "config"
    for name in ("settings.yml", "thresholds.yml", "tag_aliases.yml", "materiality.yml"):
        source_path = src / name
        if source_path.exists():
            shutil.copy2(source_path, dest / name)


def _synthesize_run_results(db_path: Path, dest: Path) -> None:
    """One `status: "success"` result per findings.f_*/dims.* table actually present in the
    built test db (read-only connection) -- see module docstring for why this is synthesised
    rather than read from the live dbt/target/run_results.json."""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        tables = con.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema IN ('findings', 'dims') ORDER BY 1, 2"
        ).fetchall()
    finally:
        con.close()
    results = [
        {
            "status": "success",
            "timing": [],
            "thread_id": "Thread-1 (tests/conftest.py synthesised)",
            "execution_time": 0.0,
            "adapter_response": {},
            "message": None,
            "failures": None,
            "unique_id": f"model.databricks_audit.{table_name}",
            "compiled": True,
            "compiled_code": None,
            "relation_name": f'"{schema}"."{table_name}"',
            "batch_results": None,
        }
        for schema, table_name in tables
    ]
    payload = {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/run-results/v6.json",
            "dbt_version": "synthesised-by-tests/conftest.py",
            "generated_at": AS_OF + ".000000Z",
            "invocation_id": "tests-conftest-synthesised",
            "env": {},
        },
        "results": results,
        "elapsed_time": 0.0,
        "args": {},
    }
    dest.write_text(json.dumps(payload), encoding="utf-8")


def _synthesize_snapshot_manifest(dest: Path) -> None:
    """A snapshot manifest.json built from tests/fixtures/parquet's actual contents: `state: "ok"`
    for every `<schema>__<table>` folder present, `rows` = the real row count (0 for an
    empty-typed source parquet, >0 for a populated one). See module docstring for why."""
    tables: dict[str, dict] = {}
    if PARQUET_DIR.exists():
        con = duckdb.connect()
        try:
            for folder in sorted(p for p in PARQUET_DIR.iterdir() if p.is_dir()):
                if "__" not in folder.name:
                    continue
                schema, table_name = folder.name.split("__", 1)
                files = sorted(folder.glob("*.parquet"))
                rows = 0
                if files:
                    pattern = (folder / "*.parquet").as_posix()
                    try:
                        rows = int(
                            con.execute(
                                f"SELECT count(*) FROM read_parquet('{pattern}', union_by_name=true)"
                            ).fetchone()[0]
                        )
                    except duckdb.Error:
                        rows = 0
                # Mirror the REAL exporter's key spelling ("<schema>__<table>", the parquet
                # folder name) rather than the app's canonical dotted form. The fixture used to
                # write the dotted form, which matched app/core/data.py's lookups and therefore
                # hid the fact that no real snapshot ever does (DEC-54).
                tables[f"{schema}__{table_name}"] = {
                    "state": "ok",
                    "rows": rows,
                    "files": len(files),
                    "time_column": None,
                    "days_requested": None,
                    "days_effective": None,
                    "min_time": None,
                    "max_time": None,
                    "predicate": None,
                    "elapsed_s": 0.0,
                    "slices_failed": [],
                    "error_class": None,
                    "reason": None,
                    "message": None,
                }
        finally:
            con.close()
    payload = {
        "as_of": AS_OF,
        "as_of_date": AS_OF_DATE,
        "days": 90,
        "billing_days": 365,
        "workspace_ids": ["1111", "2222", "3333", "9001", "9002"],
        "host_fingerprint": "tests-conftest-synthesised",
        "metastore": _fixture_metastore(),
        "connector_version": "tests-conftest-synthesised",
        "tables": tables,
    }
    dest.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture(scope="session", autouse=True)
def _app_env(tmp_path_factory: pytest.TempPathFactory):
    """Autouse so every test in the session gets these env vars without asking; yields a small
    dict a test can use directly instead of re-deriving the paths."""
    newer = _stale_sources()
    if newer:
        if os.environ.get("AUDIT_BUILD_FIXTURES") == "1":
            _build_fixture_db()
        else:
            _warn_stale(newer)

    session_dir = tmp_path_factory.mktemp("audit_session")

    config_dir = session_dir / "config"
    _seed_config_dir(config_dir)

    run_results_path = session_dir / "run_results.json"
    if DB_PATH.exists():
        _synthesize_run_results(DB_PATH, run_results_path)

    manifest_path = session_dir / "manifest.json"
    _synthesize_snapshot_manifest(manifest_path)

    env_keys = ("AUDIT_DB", "AUDIT_CONFIG_DIR", "AUDIT_SNAPSHOT_MANIFEST", "AUDIT_RUN_RESULTS", "AUDIT_DATA_DIR")
    previous = {key: os.environ.get(key) for key in env_keys}
    os.environ["AUDIT_DB"] = str(DB_PATH)
    # A build or load under test points current_db.txt at its db; never the user's real data folder.
    os.environ["AUDIT_DATA_DIR"] = str(session_dir / "data")
    os.environ["AUDIT_CONFIG_DIR"] = str(config_dir)
    os.environ["AUDIT_SNAPSHOT_MANIFEST"] = str(manifest_path)
    if run_results_path.exists():
        os.environ["AUDIT_RUN_RESULTS"] = str(run_results_path)
    try:
        yield {
            "db_path": DB_PATH,
            "config_dir": config_dir,
            "manifest_path": manifest_path,
            "run_results_path": run_results_path,
        }
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
